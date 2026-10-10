# =============================================================================
# RocketRide Engine
# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# =============================================================================

"""
Tenki Sandbox tool node - global (shared) state.

Reads the Tenki workspace API key, the sandbox sizing and the published tool
groups from config and creates a Tenki client. The session itself is created
lazily on the first tool call (creating one costs money and time, and a
pipeline may never invoke the tool) and is closed in ``endGlobal`` when the
engine runs node teardown at all (see the note below). Tool logic
lives on IInstance via @tool_function, and every tool reaches the session
through ``call_with_session``, which owns recovery.

Three things differ from tool_daytona and shape this file:

* Tenki has no ephemeral flag and no idle timeout: the SDK's idle-timeout
  option never paused anything (Tenki's changelog of 2026-09-18 removed it as
  having "never had any effect"), so an unused session bills compute until
  it is closed or ``max_duration`` runs out. Tenki then pauses it and keeps
  its state (changelog of 2026-09-11) rather than ending it. ``endGlobal``
  closes a session promptly, but teardown is not reached after an engine
  crash, and engines before the fix in #2396 skipped it for chat- and
  webhook-sourced tasks (measured on engine 3.3.0, #2300). ``max_duration``
  is the backstop for both, which is why its default is short.
* A create whose readiness wait fails can leave a live, billing sandbox that
  nothing references, so the create path closes it explicitly.
* Recovery follows the session's actual state, not the exception alone: a
  paused session is resumed (its memory and files survive), a terminated one
  is replaced, and anything else is surfaced.

Tenancy: the session belongs to the pipeline, not to a user. Every caller of a
running pipeline (every conversation of a team-deployed one, for example)
shares it, with its files and installed packages. Nothing secret is placed in
the VM: its user has passwordless sudo, so no command run there could be kept
from reading it.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from contextlib import nullcontext

from ai.common.config import Config
from rocketlib import IGlobalBase, OPEN_MODE, debug, warning

from tenki import (
    Client,
    CommandTimeoutError,
    FileNotFoundError as TenkiFileNotFoundError,
    InvalidStateError,
    PermissionDeniedError,
    Sandbox,
    SandboxError,
    SessionNotFoundError,
    SessionTerminatedError,
    TemplateRuntimeFailedError,
    WaitReadyFailedError,
)

from .file_deadline import bound_file_calls
from .tool_groups import DEFAULT_GROUPS, normalize_groups, unknown_groups

# The deadline for every control-plane call (create, refresh, resume, close, and git operations) and
# for every file call. Git clones share it, so it never drops below the execution timeout, and the
# floor keeps a small execution timeout from starving session creation.
_MIN_RPC_TIMEOUT_SECS = 60

# How long recovery waits for a resumed session to run again: the SDK's own readiness budget.
_READY_WAIT_SECS = 180

# How long recovery keeps asking for a resume that Tenki refuses with its catch-all "not yet", and the
# pause between asks. Measured after a pause at max_duration: one to three refusals ("paused source is
# still being torn down, retry shortly") over 18 to 48 seconds, each taking about 10 seconds itself.
# No new ask starts once the budget is spent, so a call can wait about one ask longer than this.
_RESUME_RETRY_SECS = 90
_RESUME_RETRY_PAUSE_SECS = 5

# Given an empty endpoint the SDK resolves one from TENKI_API_ENDPOINT / TENKI_API_URL,
# so the public default is passed explicitly rather than left to the host's environment.
_DEFAULT_BASE_URL = 'https://api.tenki.cloud'

# States in which a session is treated as unable to serve another call. USER_SHUTDOWN is
# not in Tenki's documented lifecycle, but it is in the protocol, and a session that has
# shut down is handled like a terminated one.
_GONE_STATES = frozenset({'TERMINATING', 'TERMINATED', 'USER_SHUTDOWN'})

# Errors that can mean the session itself stopped being usable. Each is only a hint:
# the SDK maps every NOT_FOUND it cannot attribute to SessionNotFoundError and every
# unrecognised FAILED_PRECONDITION to InvalidStateError, so a healthy session can raise
# either. recover_session checks the real state before acting on one.
_LIFECYCLE_ERRORS = (SessionNotFoundError, SessionTerminatedError, InvalidStateError)


class SessionEndedError(Exception):
    """The session has ended, and the call was one a fresh, empty session could not serve."""

    def __init__(self) -> None:
        super().__init__(
            'the sandbox session has ended, so its files are gone; the next command or file write '
            'starts a fresh, empty sandbox'
        )


class SessionUnavailableError(SandboxError):
    """No usable session could be had: it could not be created or resumed, or could not be reached.

    The SDK reports some of these as a plain RuntimeError or TimeoutError rather than as one of its
    own errors. This is a SandboxError, so each tool's error handling returns it to the agent as an
    error result, as it does every other sandbox failure, instead of letting it out of the tool.
    """


class CommandNotStartedError(CommandTimeoutError):
    """The sandbox never confirmed that a command had started, so the command was cancelled.

    That is what a command sent to a paused session looks like when no error names the pause, so it
    is one of the failures that prompt a look at the session's state. The node treats the command as
    not run and, if the session turns out to have been paused, runs it after the resume.
    """


# How Tenki's host words a failure to reach a VM's socket, in both forms measured. Matched as a phrase
# rather than as the bare word "vsock", which a file or folder the agent named could contain: the
# SDK builds stat's own "no such file" message from the path it was asked for.
_VM_LINK_DOWN = re.compile(r'failed to (?:connect to|read) vsock')


def about_the_vm_link(error: Exception) -> bool:
    """Whether a "no such file" error is about Tenki's own socket to the VM, not a path inside it.

    The SDK classes any error whose text contains "no such file or directory" as a missing file.
    Measured on a session Tenki had paused, a read failed with "failed to read vsock CONNECT
    response ... failed to connect to vsock at ... rootfs.img.vsock: connect: no such file or
    directory": the missing thing is the paused VM's socket on Tenki's host. Passing that on as
    "no such file: <the agent's path>" would tell the agent a file was gone when it was not.
    """
    return isinstance(error, TenkiFileNotFoundError) and _VM_LINK_DOWN.search(str(error)) is not None


def _may_hide_a_pause(error: SandboxError) -> bool:
    """Whether a failed call says too little about the session to be reported without a look at it.

    Tenki does not always name a pause. Measured on real sessions it had paused: a file read came
    back as an infrastructure error the SDK has no class for ("dial guest-agent stream ... failed to
    connect to vsock"), or as one the SDK mistakes for a missing file (see ``about_the_vm_link``),
    and a command simply never started. Any other error the SDK did classify, such as a file that
    really is missing, is an answer in its own right and costs no extra call.
    """
    return type(error) is SandboxError or isinstance(error, CommandNotStartedError) or about_the_vm_link(error)


# A hostname Tenki accepts in an outbound allowlist: at least two dot-separated labels of letters,
# digits and inner hyphens, the last containing a letter (an IP address is a CIDR to Tenki, not a
# domain), optionally after a single leading "*." wildcard.
_DOMAIN = re.compile(
    r'^(\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?=[a-z0-9-]*[a-z])[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$'
)


#: The most domains Tenki documents that it accepts in one allowlist, and the longest name DNS allows.
_MAX_DOMAINS = 256
_MAX_DOMAIN_CHARS = 253


def _names(raw, field: str) -> list[str]:
    """The distinct, non-empty entries of a list setting, given as a list or a comma-separated string.

    Anything else is refused rather than read as "nothing configured" or turned into text: an
    allowlist typed in the wrong shape would otherwise start the sandbox with every destination
    open, and an entry such as ``None`` would reach Tenki as a name nobody typed.

    Raises:
        ValueError: If the setting is not a list or a string, or holds an entry that is not text.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            f'tenki.{field} must be a list of names, or one comma-separated string, not a {type(raw).__name__}'
        )
    names = []
    for position, entry in enumerate(raw, 1):
        if not isinstance(entry, str):
            raise ValueError(f'tenki.{field}: entry {position} is a {type(entry).__name__}, not text')
        name = entry.strip()
        if name and name not in names:
            names.append(name)
    return names


def _allowed_domains(raw) -> list[str]:
    """The configured outbound allowlist, normalized as Tenki does: lowercase, without a trailing dot.

    The node accepts plain DNS hostnames with at least two labels, optionally under one leading
    ``*.``. That is narrower than Tenki may be (its documentation gives examples, not a grammar), and
    it is the node's rule, not Tenki's. An entry outside it stops the pipeline instead of being
    dropped: had every entry been dropped, the empty list left would mean no allowlist at all,
    opening outbound traffic wider than the operator asked for.

    Raises:
        ValueError: Naming every entry the node does not accept, or for more entries than Tenki takes.
    """
    domains, refused = [], []
    for position, name in enumerate(_names(raw, 'allow_domains'), 1):
        domain = name.lower().rstrip('.')
        if len(domain) > _MAX_DOMAIN_CHARS or not _DOMAIN.match(domain):
            # An entry with an "@" may be a URL that carries credentials: say where it is, not what it says.
            refused.append(f'entry {position}' if '@' in name else name)
        elif domain not in domains:
            domains.append(domain)
    if refused:
        raise ValueError(
            f'tenki.allow_domains has entries tool_tenki does not accept: {", ".join(refused)}. Use bare '
            'hostnames such as pypi.org, optionally with one leading "*." wildcard, and no scheme, path or port.'
        )
    if len(domains) > _MAX_DOMAINS:
        raise ValueError(f'tenki.allow_domains lists {len(domains)} hosts, and Tenki accepts up to {_MAX_DOMAINS}')
    return domains


def _secret_settings(cfg) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The secret policies to attach and the outbound allowlist, which are only valid together.

    A policy decides which requests Tenki puts a credential into. It does not decide where the
    sandbox may send what those requests return, so policies without an allowlist would hand an
    agent a usable credential and every destination to carry the results to. That combination
    stops the pipeline rather than starting it open.

    Raises:
        ValueError: For a setting the node does not accept, or for policies without an allowlist.
    """
    policies = tuple(_names(cfg.get('secret_policies'), 'secret_policies'))
    domains = tuple(_allowed_domains(cfg.get('allow_domains')))
    if policies and not domains:
        raise ValueError(
            'tenki.secret_policies is set but tenki.allow_domains is empty. A sandbox that can use a secret '
            'must not have unrestricted outbound access: list the hosts it needs in tenki.allow_domains '
            '(github.com, for example).'
        )
    return policies, domains


def _outlives_the_session(exec_timeout_secs: int, max_duration_minutes: int) -> str | None:
    """The warning for a command allowed to run as long as the session lives, or None.

    Tenki pauses a session at max_duration and the pause cuts a running command off, so such a command
    can never finish. One that starts late in a session's period can be cut off too; this is the case
    that holds every time.
    """
    if exec_timeout_secs < max_duration_minutes * 60:
        return None
    return (
        f'tool_tenki: exec_timeout_secs ({exec_timeout_secs}s) is not shorter than max_duration_minutes '
        f'({max_duration_minutes} min). Tenki pauses the session at max_duration and the pause cuts a running '
        'command off, so a command that needs the full execution timeout can never finish. Raise '
        'max_duration_minutes or lower exec_timeout_secs.'
    )


def _int_or(value, default: int, *, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


class IGlobal(IGlobalBase):
    """Global state for tool_tenki."""

    client: Client | None = None
    session: Sandbox | None = None  # created lazily, closed in endGlobal
    # Bumped under the lock whenever the session is resumed or dropped. A call that
    # failed against the old state compares it to decide that a concurrent call has
    # already recovered the session, and retries instead of re-checking a session that
    # is healthy by now and misreporting its own failure as unrelated.
    session_epoch: int = 0
    # Guards session creation and recovery: agents issue parallel tool calls (e.g.
    # deepagent's asyncio.gather fan-out), and an unsynchronized check-then-act would
    # create two billed sessions and orphan one, or resume one session several times.
    _session_lock: threading.Lock | None = None
    # How the most recent recovery went, 'resumed' or 'replaced'. A call that retries after a
    # concurrent call did the recovering reports this, since it did not see the recovery itself.
    last_recovery: str = ''
    # The epoch at which a session was last dropped. A call that captured an earlier epoch had its
    # own session replaced, so its files are gone no matter what a later recovery wrote to
    # last_recovery; comparing epochs keeps 'resumed' from reassuring it about a session it never ran on.
    last_replacement_epoch: int = -1
    # Set as endGlobal starts. No session may be created after that: nothing would close it.
    _ending: bool = False
    # A tag unique to this pipeline run, set in beginGlobal. Every session created here carries it,
    # so endGlobal can find and close any this run left running, without touching another run's.
    _run_tag: str = ''
    # Set once this run asks Tenki for a session, whether or not the create succeeds: only then can a
    # session carrying the run's tag exist for endGlobal to look for.
    _create_attempted: bool = False
    # When, on time.monotonic(), asking again for a refused resume stops. Read and written under the
    # session lock. It is one budget for every call waiting on the same resume, not one each: see _resume.
    _resume_retry_until: float | None = None

    rpc_timeout_secs: int = 120
    image: str = ''
    # Tenki request policies to attach, by name. Tenki holds the secret values and substitutes them
    # outside the VM, into requests a policy allows; the sandbox only ever sees placeholders.
    secret_policies: tuple[str, ...] = ()
    # Hostnames the sandbox may reach. Empty means no allowlist: outbound traffic is unrestricted.
    allow_domains: tuple[str, ...] = ()
    cpu_cores: int = 2
    memory_mb: int = 4096
    disk_size_gb: int = 5
    max_duration_minutes: int = 60
    pause_retention_hours: int = 24
    exec_timeout_secs: int = 120
    max_output_chars: int = 50000
    tool_groups: frozenset = DEFAULT_GROUPS

    def beginGlobal(self) -> None:
        if self.IEndpoint.endpoint.openMode == OPEN_MODE.CONFIG:
            return

        self._session_lock = threading.Lock()
        self._ending = False
        self._create_attempted = False
        self._resume_retry_until = None
        self._run_tag = f'run-{uuid.uuid4().hex[:12]}'

        cfg = Config.getNodeConfig(self.glb.logicalType, self.glb.connConfig)
        apikey = str((cfg.get('apikey') or '')).strip()

        # Checked here rather than left to the SDK: given an empty token it falls back to
        # TENKI_AUTH_TOKEN / TENKI_API_KEY, which would let a key in the engine host's
        # environment silently decide which workspace gets billed.
        if not apikey:
            raise Exception('tool_tenki: apikey is required')

        # A value naming only unknown groups raises in normalize_groups rather than falling back
        # to the defaults. A partially unknown one narrows to the names that matched, which is
        # what the operator asked for minus the typo, so it runs; the dropped names go to the
        # job log, because the editor warning in validateConfig never reaches a deployed pipeline.
        self.tool_groups = normalize_groups(cfg.get('toolGroups'))
        dropped = unknown_groups(cfg.get('toolGroups'))
        if dropped:
            warning(
                f'tool_tenki: ignoring unknown tool group(s): {", ".join(dropped)}. '
                f'Publishing: {", ".join(sorted(self.tool_groups))}'
            )

        base_url = str((cfg.get('base_url') or '')).strip() or _DEFAULT_BASE_URL
        self.image = str((cfg.get('image') or '')).strip()
        self.secret_policies, self.allow_domains = _secret_settings(cfg)
        self.cpu_cores = _int_or(cfg.get('cpu_cores'), 2, lo=1, hi=16)
        # Tenki rejects odd memory sizes (they must align to 2 MiB). Rounding down stays in
        # range because both bounds are even.
        memory_mb = _int_or(cfg.get('memory_mb'), 4096, lo=512, hi=65536)
        self.memory_mb = memory_mb - memory_mb % 2
        # The SDK refuses a disk under 5 GB before sending the request.
        self.disk_size_gb = _int_or(cfg.get('disk_size_gb'), 5, lo=5, hi=100)
        # Pipelines saved before the idle-timeout field was retired may still carry it; it is not
        # read, so it never reaches Tenki, which ignored it anyway.
        self.max_duration_minutes = _int_or(cfg.get('max_duration_minutes'), 60, lo=1, hi=1440)
        # How long Tenki keeps a paused session's state. Tenki refuses 0 and accepted every value tried from a
        # minute to a year (measured 2026-10-09); its own default is seven days.
        self.pause_retention_hours = _int_or(cfg.get('pause_retention_hours'), 24, lo=1, hi=8760)
        self.exec_timeout_secs = _int_or(cfg.get('exec_timeout_secs'), 120, lo=1, hi=1200)
        self.max_output_chars = _int_or(cfg.get('max_output_chars'), 50000, lo=1000, hi=1000000)

        outlives = _outlives_the_session(self.exec_timeout_secs, self.max_duration_minutes)
        if outlives:
            warning(outlives)
        self.rpc_timeout_secs = max(self.exec_timeout_secs, _MIN_RPC_TIMEOUT_SECS)
        # Without a deadline, a half-open connection would hang create, refresh, resume and close,
        # all of which run under the session lock, and endGlobal behind them while the VM bills.
        # The client applies it to those calls only; each session's file calls get it in _create_session.
        self.client = Client(auth_token=apikey, base_url=base_url, timeout=self.rpc_timeout_secs)

    def get_session(self) -> Sandbox:
        """Return the shared session, creating it on first use."""
        session = self.session
        if session is None:
            with self._session_lock:
                if self._ending:
                    raise SessionUnavailableError(
                        'tool_tenki: the pipeline is shutting down, so no new sandbox session is created'
                    )
                if self.session is None:
                    self.session = self._create_session()
                    debug(f'tool_tenki: created session {getattr(self.session, "id", "?")}')
                session = self.session
        return session

    def _create_session(self) -> Sandbox:
        create_kwargs = {
            'cpu_cores': self.cpu_cores,
            'memory_mb': self.memory_mb,
            'disk_size_gb': self.disk_size_gb,
            # Tenki has no ephemeral flag and no idle timeout, so this is the one thing that
            # stops the compute of a session whose pipeline never reached endGlobal (a crashed
            # engine, or one that skips teardown): Tenki pauses it when this runs out. The SDK
            # takes seconds. sticky stays at its default of False: a sticky session discards
            # max_duration.
            'max_duration': self.max_duration_minutes * 60,
            # Without it, a paused session left behind (an engine crash) holds its code and files for
            # Tenki's default of seven days. The SDK takes seconds.
            'pause_retention': self.pause_retention_hours * 3600,
            # Inbound exposure can only be set at create time, and no tool this node
            # publishes uses it, so sessions are created closed to inbound traffic.
            'allow_inbound': False,
            # So that a session a crashed engine left behind can be recognised in Tenki's console
            # and listed by tag. The suffix keeps names distinct in case the service requires it.
            'name': f'rocketride-tool-tenki-{uuid.uuid4().hex[:8]}',
            'tags': ['rocketride', self._run_tag],
            'metadata': {'created_by': 'rocketride', 'node': 'tool_tenki'},
        }
        if self.image:
            create_kwargs['image'] = self.image
        # Sent only when set, so an unconfigured node makes exactly the request it always has. Policy
        # names are all that is sent about secrets: never a value, and never secret_files or
        # secret_overrides, which would put the real value inside the VM.
        if self.secret_policies:
            create_kwargs['secret_policies'] = list(self.secret_policies)
        if self.allow_domains:
            create_kwargs['allow_domains'] = list(self.allow_domains)
        self._create_attempted = True
        try:
            session = self.client.create(**create_kwargs)
        except PermissionDeniedError as e:
            if not self.secret_policies or 'Secrets access' not in str(e):
                raise
            # What Tenki answers when the API key was created without Secrets access (measured with
            # tenki 1.4.0: "API key requires Secrets access; ..."). No sandbox is left behind, and
            # nothing an agent does can change the key. Any other refusal is reported as it is.
            raise SessionUnavailableError(
                f'Tenki refused to attach the secret policies named in tenki.secret_policies: {e}. The Tenki '
                'API key this pipeline uses needs Secrets access, which only its operator can grant; '
                'repeating the call will not help.'
            ) from e
        except (WaitReadyFailedError, TemplateRuntimeFailedError) as e:
            self._close_left_running(e)
            raise
        except SandboxError:
            # Before the plain catch below: since tenki 1.4.1 two SDK errors are also a RuntimeError or a
            # TimeoutError, and an SDK error is reported as itself.
            raise
        except (RuntimeError, TimeoutError) as e:
            # The SDK re-raises its readiness wait's own plain RuntimeError when the new sandbox went
            # straight to a terminal state. There is nothing left running to close.
            raise SessionUnavailableError(f'the new sandbox session did not start: {e}') from e
        try:
            # The client's deadline does not reach a session's file calls (see file_deadline).
            bound_file_calls(session, self.rpc_timeout_secs)
        except AttributeError as e:
            # Only an SDK whose file API changed shape gets here. Its file calls could hang for good,
            # so the session is closed rather than used, and not left billing.
            message = (
                'tool_tenki cannot put a deadline on file calls with this version of the Tenki SDK, '
                'so the new sandbox session was closed without being used'
            )
            warning(f'tool_tenki: {message} ({e})')
            try:
                session.close()
            except Exception as close_error:
                warning(f'tool_tenki: could not close that session: {close_error}')
            raise SessionUnavailableError(message) from e
        return session

    def _close_left_running(self, error: Exception) -> None:
        """Close the sandbox a failed create left running, so it stops billing.

        Tenki admits the sandbox before the readiness wait, so this failure can leave a
        live session that nothing references, and scope-based cleanup cannot catch it
        because create never returned. ``WaitReadyFailedError`` carries the live handle.
        ``TemplateRuntimeFailedError`` raised by the create call itself carries only the
        session's info record (or nothing), so the handle is fetched by id first. A close
        that fails is only logged: the create error is what the caller needs, and
        max_duration still stops the session's compute.
        """
        carried = getattr(error, 'sandbox', None)
        if carried is None:
            return
        try:
            if callable(getattr(carried, 'close', None)):
                carried.close()
            elif getattr(carried, 'id', '') and getattr(carried, 'state', '') not in _GONE_STATES:
                self.client.get(carried.id).close()
        except Exception as e:
            warning(f'tool_tenki: could not close the sandbox a failed create left running: {e}')

    def call_with_session(self, call, *, replace: bool = True, on_recovery=None):
        """Return ``call(session)``, recovering the session once if it stopped being usable.

        A lifecycle error only prompts a look at the session's state (see
        ``recover_session``), and so does a failure that says too little to rule a pause out (see
        ``_may_hide_a_pause``). The call is retried once, on the recovered session, when that state
        explains the failure; every other error propagates unchanged.

        ``replace=False`` is for calls that only look at existing state (reading, listing or
        deleting files). If the session turns out to have ended, a fresh, empty one could not
        serve them, so ``SessionEndedError`` is raised instead of creating a session for nothing.

        ``on_recovery``, if given, is called with 'resumed' or 'replaced' before the retry, so the tool
        can tell the agent that its call ran on a resumed session, or on a fresh and empty one.
        """
        epoch = self.session_epoch
        try:
            session = self.get_session()
            try:
                return call(session)
            except _LIFECYCLE_ERRORS:
                recovery = self.recover_session(session, epoch, replace=replace)
                if not recovery:
                    raise
            except SandboxError as e:
                if not _may_hide_a_pause(e):
                    raise
                recovery = self.recover_session(session, epoch, replace=replace)
                if not recovery:
                    raise
            if on_recovery is not None:
                on_recovery(recovery)
            return call(self.get_session())
        except SandboxError:
            # An SDK error is reported as itself, including the two that are also a plain RuntimeError or
            # TimeoutError since tenki 1.4.1. Only a plain one is wrapped below.
            raise
        except (RuntimeError, TimeoutError) as e:
            # The SDK raises a few failures as plain exceptions rather than its own errors (Sandbox.start
            # does when the data plane cannot be reached), which a tool's SandboxError handling would let
            # out of the tool. Logged as well: these are rare, and worth an operator's attention.
            warning(f'tool_tenki: sandbox call failed with {type(e).__name__}: {e}')
            raise SessionUnavailableError(str(e)) from e

    def recover_session(self, session: Sandbox, epoch: int, *, replace: bool = True) -> str | None:
        """Bring a session that failed a call back to a usable state.

        Returns how it was recovered, 'resumed' or 'replaced', when the call should be retried, and
        None when the failure had another cause. Decided by the session's real state, because the
        error that led here is only a hint:

        * Already recovered by a concurrent call (a different session or epoch): retry.
        * Unknown to the service, or in a gone state: forget it (closing it first unless the
          service already did), so the next ``get_session`` creates a fresh one. Its files
          are lost either way. With ``replace=False`` the caller is not retried on that fresh
          session: ``SessionEndedError`` is raised instead.
        * Paused (by Tenki when the session reaches max_duration or the workspace runs out of
          credit, or by hand in Tenki's console or CLI): resume it and wait until it runs.
          Never replace it, since a pause preserves the agent's memory and files.
        * Any other state (running, starting, pausing): the failure had another cause, so
          it is surfaced.
        """
        with self._session_lock:
            if self.session is not session or self.session_epoch != epoch:
                # A concurrent call already dealt with this session. If it dropped it, retrying
                # would create a fresh session, which a caller passing replace=False cannot use —
                # and which the replacement may already have been created for, so the test is the
                # epoch rather than self.session still being None.
                if self.session is None or self.last_replacement_epoch > epoch:
                    if not replace:
                        raise SessionEndedError()
                    return 'replaced'
                return self.last_recovery or None
            try:
                state = session.refresh().state
            except SessionNotFoundError:
                state = None
            if state is None or state in _GONE_STATES:
                return self._drop_session(session, state, replace=replace)
            if state == 'PAUSED':
                self._resume(session)
                if self._ending:
                    # The ask was on its way when shutdown began. endGlobal is waiting on this lock
                    # to close the session, so it is not made to wait for the VM to come up first.
                    raise SessionUnavailableError(
                        'tool_tenki: the pipeline is shutting down, so the resumed sandbox session is not waited for'
                    )
                try:
                    session.wait_ready(timeout=_READY_WAIT_SECS)
                except SandboxError:
                    raise
                except (RuntimeError, TimeoutError) as e:
                    # The SDK's plain errors for "went terminal while waiting" and "never got to
                    # RUNNING". A session terminated mid-resume is as gone as any other. One merely
                    # slow to come up keeps its files, so it is not dropped.
                    if session.state in _GONE_STATES:
                        return self._drop_session(session, session.state, replace=replace)
                    raise SessionUnavailableError(f'session {session.id} did not finish resuming: {e}') from e
                self.session_epoch += 1
                self.last_recovery = 'resumed'
                debug(f'tool_tenki: resumed paused session {session.id}')
                return 'resumed'
            return None

    def _resume(self, session: Sandbox) -> None:
        """Ask Tenki to resume ``session``, again for as long as it only answers "not yet".

        For up to a minute after a pause Tenki refuses a resume while it is still tearing the paused
        VM down, with an error the SDK has no class for. That is a wait, not a failure, and it comes
        at the moment every long-running pipeline reaches every max_duration. An error with a class
        of its own means something else and is raised at once; so is the catch-all once
        ``_RESUME_RETRY_SECS`` have passed, so that a call never waits on Tenki without bound.

        Called with the session lock held, as a resume must be: parallel calls wait for this one.
        They share its budget. A call that reaches this point while a spent budget is still recent
        asks once and reports, instead of starting a wait of its own behind the last, which would
        add up to minutes across a wave of calls, with ``endGlobal`` waiting on the same lock. For the
        same reason no new ask starts once the pipeline is shutting down: ``endGlobal`` closes the
        session, and a resume would only start a VM for it to end. An ask already on its way is
        not interrupted.

        The budget belongs to one pause. It ends when the resume takes, when Tenki refuses with a
        reason of its own, and when the session is dropped. One case is left: a spent budget is
        still shared for its own length afterwards, so a session resumed from outside the node and
        paused again within that time gets a single ask.
        """
        now = time.monotonic()
        if self._resume_retry_until is None or now >= self._resume_retry_until + _RESUME_RETRY_SECS:
            self._resume_retry_until = now + _RESUME_RETRY_SECS
        while True:
            if self._ending:
                raise SessionUnavailableError(
                    'tool_tenki: the pipeline is shutting down, so the paused sandbox session is not resumed'
                )
            try:
                session.resume()
            except SandboxError as e:
                if type(e) is not SandboxError:
                    # A refusal with a reason of its own ends this wait. What is left of the budget
                    # must not be all that a later pause gets.
                    self._resume_retry_until = None
                    raise
                if time.monotonic() >= self._resume_retry_until:
                    raise
                debug(f'tool_tenki: resume of {session.id} not accepted yet ({e}); asking again')
                time.sleep(_RESUME_RETRY_PAUSE_SECS)
            else:
                self._resume_retry_until = None
                return

    def _drop_session(self, session: Sandbox, state: str | None, *, replace: bool) -> str:
        """Forget a session that has ended, so the next ``get_session`` creates a fresh one.

        Called with the session lock held. Returns 'replaced', or raises ``SessionEndedError`` for a
        caller passing ``replace=False``, whom a fresh, empty session could not serve.
        """
        # Close it even when the service reported it not-found (state None): that report can be
        # wrong, and a still-running VM would otherwise bill until max_duration. A close that fails
        # here is expected when the session really is gone, so it is only logged.
        try:
            session.close_if_open()
        except Exception as e:
            debug(f'tool_tenki: could not close dropped session {session.id}: {e}')
        self.session = None
        self.session_epoch += 1
        self.last_replacement_epoch = self.session_epoch
        self.last_recovery = 'replaced'
        self._resume_retry_until = None
        # A warning rather than debug output: the next call pays for a new VM and starts empty.
        warning(f'tool_tenki: session {session.id} is {state or "gone"}; the next call starts a new, empty session')
        if not replace:
            raise SessionEndedError()
        return 'replaced'

    def validateConfig(self) -> None:
        try:
            cfg = Config.getNodeConfig(self.glb.logicalType, self.glb.connConfig)
            apikey = str((cfg.get('apikey') or '')).strip()
            if not apikey:
                warning('apikey is required')
            elif not apikey.startswith('tk_'):
                warning('apikey must be a Tenki workspace API key, which starts with tk_')
            outlives = _outlives_the_session(
                _int_or(cfg.get('exec_timeout_secs'), 120, lo=1, hi=1200),
                _int_or(cfg.get('max_duration_minutes'), 60, lo=1, hi=1440),
            )
            if outlives:
                warning(outlives)
            unknown = unknown_groups(cfg.get('toolGroups'))
            if unknown:
                warning(f'unknown tool group(s): {", ".join(unknown)}')
            try:
                normalize_groups(cfg.get('toolGroups'))
            except ValueError:
                warning('toolGroups matches no known group, so the pipeline will fail to start')
            try:
                _secret_settings(cfg)
            except ValueError as e:
                warning(f'{e} The pipeline will fail to start.')
        except Exception as e:
            warning(str(e))

    def _close_tagged_sessions(self) -> None:
        """Close any still-running session carrying this run's tag.

        The scoped close above handles the session the node is holding. This catches the ones it is
        not: a session dropped in recovery whose close did not take, or a create that raced the RPC
        deadline and left a VM the node never got a handle for. The unique run tag keeps the sweep to
        this run's own sessions. Best-effort: listing needs the network, and max_duration is the
        final backstop.
        """
        if not self._run_tag:
            return
        if not self._create_attempted:
            # Nothing can carry the tag, and an unused node then shuts down without the network.
            return
        try:
            leftover = self.client.list(tags=[self._run_tag])
        except Exception as e:
            warning(f"tool_tenki: could not list this run's sessions to clean up: {e}")
            return
        for sandbox in leftover:
            if getattr(sandbox, 'state', '') in ('TERMINATING', 'TERMINATED'):
                # Already on its way out, and almost always the session the close above just ended:
                # terminating takes a moment, so a list right after still returns it. Reporting a
                # close here would fire on every clean shutdown and bury the case below.
                continue
            try:
                sandbox.close_if_open()
                # A warning, not debug: something escaped the scoped close and was still billing.
                warning(f'tool_tenki: closed session {getattr(sandbox, "id", "?")}, which shutdown had left running')
            except Exception as e:
                warning(f'tool_tenki: could not close leftover session {getattr(sandbox, "id", "?")}: {e}')

    def endGlobal(self) -> None:
        # First, so that no tool call can create a session after the close below.
        self._ending = True
        # Load-bearing for Tenki: a session left open keeps billing compute until max_duration
        # pauses it, since nothing pauses an idle session sooner. Taking the lock lets a
        # create still in flight finish first, so the session it produces is closed too.
        with self._session_lock or nullcontext():
            if self.session is not None:
                try:
                    self.session.close()
                except Exception as e:
                    warning(f'tool_tenki: session close failed: {e}')
                finally:
                    self.session = None
        if self.client is not None:
            self._close_tagged_sessions()
            try:
                self.client.close()
            except Exception as e:
                warning(f'tool_tenki: client close failed: {e}')
            finally:
                self.client = None
