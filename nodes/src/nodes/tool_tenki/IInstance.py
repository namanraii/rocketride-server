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
Tenki Sandbox tool node instance.

Exposes the agent tools, all operating on one shared, lazily created Tenki session.
Every tool is tagged with a tool group (see tool_groups.py), and only the groups named
in the ``tenki.toolGroups`` config field are published. Each tool reaches the session
through ``IGlobal.call_with_session``, which resumes a paused session and replaces a
terminated one. A command whose start the sandbox has confirmed is the exception: the node does
not start it again (see ``_StartedOnce``).
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import threading
import urllib.parse
import uuid
from typing import Callable

from rocketlib import IInstanceBase

from ai.common.utils import normalize_tool_input, optional_bool, optional_int

from tenki import CommandTimeoutError, PermissionDeniedError, SandboxError
from tenki import FileNotFoundError as TenkiFileNotFoundError  # also subclasses the builtin it shadows

from .IGlobal import CommandNotStartedError, IGlobal, SessionEndedError, about_the_vm_link
from .tool_groups import tenki_tool

#: The session's home and default working directory, and the only root Tenki's file API accepts.
_HOME = '/home/tenki'

# Tenki-specific on purpose. Daytona's sandbox is deleted when idle and comes back empty, but a
# Tenki session persists until it is terminated, and is paused rather than ended when its lifetime
# runs out: memory and /home/tenki survive a pause, /tmp does not. An agent given Daytona's account
# would reinstall and re-clone for nothing, and not know to keep work out of /tmp.
_SESSION_NOTE = (
    'All Tenki Sandbox tools share one Linux VM, with /home/tenki as the home and working '
    'directory; files and installed packages persist between calls. A paused session is resumed '
    'on the next call: memory and files under /home/tenki survive, but /tmp is cleared and open '
    'network connections drop, so keep work under /home/tenki. Tenki pauses a session that reaches '
    'its maximum lifetime, and the next call resumes it the same way, which can take a minute. Only '
    'a session that has been terminated is replaced by a fresh, empty one.'
)

_PATH_NOTE = (
    'Paths are relative to /home/tenki or absolute under it; anything outside /home/tenki, including /tmp, is rejected.'
)

#: git_log's commit count when none is given, and the most it accepts. The whole log comes back
#: in one response, so an unbounded log of a large repository would sit in memory in full before
#: it could be truncated.
_DEFAULT_LOG_COUNT = 20
_MAX_LOG_COUNT = 1000

#: How long past the execution timeout a command's result may take to arrive. Tenki stops the command
#: at the timeout and reports it on the result; only a stalled stream ever reaches this limit.
_RESULT_GRACE_SECS = 30
# The most output one command may stream back before it is treated as runaway and killed. The SDK
# keeps everything a command prints in memory until the command exits, with no limit of its own, so
# a command that floods its output (`yes`, `cat /dev/urandom | base64`) would grow the engine for as
# long as the execution timeout allows. This is a memory ceiling, not the output cap:
# max_output_chars still decides what the agent is shown, and a chatty but finite command (a large
# apt-get install, pytest -v, a build) runs to completion and is merely truncated. It sits above the
# most the agent can ever be shown (1M characters, at most 4 MB of UTF-8), so it never cuts into that.
_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
# The largest file read_file fetches. It reads through the SDK's unary ReadFile call, which the node
# sends with a deadline (see file_deadline); the streaming read has none, so a half-open connection
# could hang it for good.
# ReadFile takes no length and returns the whole file, so the size is checked with stat first and a
# larger file is refused without being fetched. The limit sits just under gRPC's default 4 MiB receive
# limit, which the SDK's channels keep, so a file that grows between the two calls fails there rather
# than arriving whole.
_READ_LIMIT_BYTES = 4_000_000
_KILLED_NOTICE = (
    '[tool_tenki: the command was killed before it finished, because it produced more than {limit:,} bytes '
    'of output, the most a command may stream back. Send large output to a file instead (for example '
    '`> out.log 2>&1`) and read back the part you need.]'
)

# What Tenki documents its proxy answers ("403 secret_injection_denied") when it refuses to put a secret
# into a request, for one of several reasons: no attached policy allows that secret for that host,
# method and path, the secret is not active, or the placeholder is somewhere a policy does not cover.
# NOT MEASURED: no denial has been produced against a real session, so it is unknown whether this
# text reaches a command's output at all (git and `curl -f` print the status, not the body). The
# note is keyed on the text alone, so output that merely quotes it gets the note as well.
_INJECTION_DENIED = 'secret_injection_denied'
_DENIED_NOTICE = (
    '[tool_tenki: this output contains "secret_injection_denied". Tenki documents that as its answer '
    '(HTTP 403) when it refuses to put a secret into a request, for example because no attached secret '
    'policy allows that secret for that host, method and path. If that is what happened here, repeating '
    'the request will not help: the policy is for the operator of this pipeline to change.]'
)


def _explained(text: str) -> str:
    """``text``, followed by what a denied secret injection means when it reports one."""
    return f'{text}\n{_DENIED_NOTICE}' if _INJECTION_DENIED in text else text


_SESSION_EVENT = {
    'type': 'string',
    'enum': ['created', 'resumed', 'replaced', 'ended'],
    'description': (
        'Present only when the sandbox session changed during this call. "created": this call started a new, '
        'empty sandbox (the first call, or the first after one ended). "resumed": it had been paused, and memory '
        'and /home/tenki survived but /tmp was cleared. "replaced": it had ended, so earlier files and installed '
        'packages are gone and this call ran on a new, empty sandbox. "ended": it had ended and this call did not '
        'continue: earlier files are gone, and the next call starts a new, empty sandbox.'
    ),
}

#: run_code languages: the interpreter to run, and the file extension it expects. TypeScript is
#: absent on purpose: ts-node is not in Tenki's base image, and running a .ts file through it
#: returned exit 0 with no output, which an agent would read as a successful run. Node 24 in the
#: image can strip types from a .ts file directly, so this can come back once that is verified live.
_LANGUAGES = {
    'python': ('python3', '.py'),
    'javascript': ('node', '.js'),
}

_EXEC_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'exit_code': {
            'type': 'integer',
            'description': 'Process exit code (0 = success, -1 if the sandbox call failed).',
        },
        'stdout': {'type': 'string', 'description': 'Captured standard output.'},
        'stderr': {'type': 'string', 'description': 'Captured standard error.'},
        'timed_out': {
            'type': 'boolean',
            'description': 'True if the command was stopped at the execution timeout; the exit code can still be 0.',
        },
        'truncated': {
            'type': 'boolean',
            'description': 'True if stdout or stderr was cut to fit the output cap, or the command was killed for '
            'producing too much output (stderr then says so).',
        },
        'error': {'type': 'string', 'description': 'Error message if the sandbox call failed.'},
        'session': _SESSION_EVENT,
    },
}


_CHANGE_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'success': {'type': 'boolean'},
        'path': {'type': 'string', 'description': 'Absolute path in the sandbox.'},
        'error': {'type': 'string', 'description': 'Error message if the operation failed.'},
        'session': _SESSION_EVENT,
    },
}


def _normalize_path(path) -> str:
    """Resolve an agent-supplied path to an absolute path under /home/tenki.

    Tenki's file API accepts nothing outside /home/tenki, including /tmp, and refuses it with a
    bare permission error. Agents reach for /tmp first, so paths are checked here instead, and
    refused with a message that says where files belong. Relative paths, and ``~``, resolve
    against /home/tenki. This is a usability check, not a security boundary: run_command can
    reach the whole VM anyway.

    Raises:
        ValueError: If ``path`` is empty or resolves outside /home/tenki.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError('"path" is required and must be a non-empty string')
    given = path.strip()
    if given == '~' or given.startswith('~/'):
        given = _HOME + given[1:]
    # Slash runs collapsed first, because normpath keeps a leading '//'.
    resolved = posixpath.normpath(re.sub(r'/+', '/', posixpath.join(_HOME, given)))
    if resolved != _HOME and not resolved.startswith(_HOME + '/'):
        raise ValueError(f'paths must be under /home/tenki (relative paths resolve there), but {given!r} is outside it')
    return resolved


def _fs_error(error: Exception, path: str) -> str:
    """A readable message for a failed file operation."""
    if isinstance(error, TenkiFileNotFoundError) and not about_the_vm_link(error):
        return f'no such file or directory: {path}'
    if isinstance(error, PermissionDeniedError):
        return f'{error} (file operations are limited to paths under /home/tenki)'
    return str(error)


def _listing(entries, cap: int) -> tuple[list[dict], bool]:
    """Shape directory entries by name, stopping before the listing's JSON would pass ``cap``."""
    shaped, used = [], 2  # the enclosing brackets
    # In a listing, a FileInfo's path holds the entry's name.
    for entry in sorted(entries, key=lambda info: info.path):
        item = {'name': entry.path, 'is_dir': bool(entry.is_dir), 'size': int(entry.size)}
        used += len(json.dumps(item)) + 2  # the entry and its separator
        if used > cap:
            return shaped, True
        shaped.append(item)
    return shaped, False


_GIT_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'output': {'type': 'string', 'description': "git's output."},
        'truncated': {'type': 'boolean', 'description': 'True if the output was cut at the output cap.'},
        'error': {'type': 'string', 'description': 'Error message if the git operation failed.'},
        'session': _SESSION_EVENT,
    },
}

_REPO_DIRECTORY = {
    'type': 'string',
    'description': (
        "The repository's folder (the equivalent of git -C), relative to /home/tenki or absolute "
        'under it. Defaults to /home/tenki.'
    ),
}


def _git_arg(args: dict, key: str, *, required: bool = False) -> str | None:
    """Return a stripped git argument from the tool input, or None when optional and not given.

    A value starting with '-' is refused: git would read it as an option rather than as the
    repository, ref or path it was meant to be, and none of those legitimately starts that way.
    """
    value = args.get(key)
    if value is None or (not required and isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f'"{key}" is required and must be a non-empty string')
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'"{key}" must be a non-empty string')
    value = value.strip()
    if value.startswith('-'):
        raise ValueError(f'"{key}" must not start with "-", which git would read as an option')
    return value


def _git_directory(args: dict) -> str | None:
    """The repository folder from the tool input, resolved under /home/tenki, or None if not given."""
    directory = args.get('directory')
    if directory is None or (isinstance(directory, str) and not directory.strip()):
        return None
    if not isinstance(directory, str):
        raise ValueError('"directory" must be a string')
    return _normalize_path(directory)


def _public_repo_url(repo: str) -> str:
    """Return ``repo`` unchanged if it is a public http(s) URL, and raise otherwise.

    ``repo`` is agent-controlled, and git records the remote it cloned from in .git/config. A URL
    carrying userinfo (``https://token@host/org/private.git``) would therefore write that credential
    into the sandbox, whose user has passwordless sudo and whose network is open, and which every
    caller of a shared pipeline reaches. The node holds no git credentials by design, so the only
    thing to do with a credential-bearing URL is refuse it before it reaches Tenki.

    ssh, git, file and scp-style (``git@host:org/repo``) forms are refused for the same reason: each
    names a repository this node has no way to authenticate to, so git would fail inside the VM with
    a far less obvious error.

    Raises:
        ValueError: If ``repo`` is not a public http(s) URL, or carries credentials.
    """
    parsed = urllib.parse.urlsplit(repo)
    if parsed.scheme.lower() not in ('http', 'https'):
        raise ValueError(
            f'"repo" must be a public http(s) URL, but {repo!r} is not. This node holds no git '
            'credentials, so ssh, git, file and local repositories cannot be cloned.'
        )
    if parsed.username or parsed.password or '@' in parsed.netloc:
        raise ValueError(
            '"repo" must not carry credentials in the URL: they would be written into the '
            'sandbox, which this node never places a secret in. Public repositories only.'
        )
    if not parsed.hostname:
        raise ValueError(f'"repo" must be a public http(s) URL with a host, but {repo!r} has none')
    return repo


def _clone_folder(repo: str) -> str | None:
    """The folder name git itself clones ``repo`` into: its last path segment, without ``.git``."""
    name = repo.rstrip('/').rsplit('/', 1)[-1]
    if name.endswith('.git'):
        name = name[: -len('.git')]
    return None if name in ('', '.', '..') else name


def _git_result(output, cap: int) -> dict:
    text, truncated = _truncate(output, cap)
    return {'output': text, 'truncated': truncated}


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    """Cap tool output so a chatty script cannot flood the agent context."""
    if text is None:
        return '', False
    text = str(text)
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _exec_result(result, cap: int, *, killed: bool = False) -> dict:
    """Shape a Tenki CommandResult into the tool output, with ``cap`` shared by both streams.

    Errors usually land on stderr, so a flood on stdout must not push them out: stderr may
    always keep up to half of the cap, and either stream may use whatever the other leaves.
    ``killed`` means the command was stopped for producing too much output; the agent is told
    so on stderr, where a shell would report the kill, after the cap so it is never cut off.
    """
    stdout_text, stderr_text = result.stdout_text, result.stderr_text
    stderr, stderr_cut = _truncate(stderr_text, max(cap // 2, cap - len(stdout_text)))
    stdout, stdout_cut = _truncate(stdout_text, cap - len(stderr))
    if killed:
        notice = _KILLED_NOTICE.format(limit=_OUTPUT_LIMIT_BYTES)
        stderr = f'{stderr}\n{notice}' if stderr else notice
    if _INJECTION_DENIED in stdout_text or _INJECTION_DENIED in stderr_text:
        # After the cap, like the kill notice, so it is never the part that gets cut off.
        stderr = f'{stderr}\n{_DENIED_NOTICE}' if stderr else _DENIED_NOTICE
    return {
        'exit_code': result.exit_code,
        'stdout': stdout,
        'stderr': stderr,
        'timed_out': bool(result.timed_out),
        'truncated': killed or stdout_cut or stderr_cut,
    }


class _OutputGuard:
    """Reads a running command's output as it arrives, and kills the command once there is too much.

    Reading as it arrives is what lets the limit be noticed while the command runs rather than after
    it exits. It does not shrink what the SDK holds, which keeps its own copy of every chunk until
    the command exits; that is why the command is killed rather than merely no longer read.
    """

    def __init__(self, process, limit: int):
        self._process = process
        self._limit = limit
        self._lock = threading.Lock()
        self._received = 0
        self.killed = False
        self._readers = [
            threading.Thread(target=self._read, args=(stream,), name='tool_tenki-output', daemon=True)
            for stream in (process.stdout, process.stderr)
        ]
        for reader in self._readers:
            reader.start()

    def _read(self, stream) -> None:
        try:
            for chunk in stream:
                with self._lock:
                    self._received += len(chunk)
                    kill = self._received > self._limit and not self.killed
                    self.killed = self.killed or kill
                if kill:
                    self._process.kill()
        except Exception:
            # A stream fails when its run does, and wait() raises that same error, so it is reported there.
            pass

    def stop(self) -> None:
        """End both streams and wait for the readers.

        Once a command exits the SDK has ended its streams already, and this only adds an end marker
        nobody reads. After a stalled run it never will, and without this the readers would block for
        good: the one thing that releases them is ending the streams they are reading.
        """
        for stream in (self._process.stdout, self._process.stderr):
            stream.close()
        for reader in self._readers:
            reader.join()


class ResultTimeoutError(CommandTimeoutError):
    """No result arrived for a command within the node's own limit (see ``_run_process``)."""


class CommandInterruptedError(SandboxError):
    """A command had started when its call lost the session, and it was not started again.

    ``recovery`` is how the session came back, if it did; ``reason`` is what ended the call otherwise.
    """

    def __init__(self, recovery: str | None = None, reason: str | None = None) -> None:
        if recovery == 'resumed':
            # Measured on real sessions paused at max_duration: the command's process was gone within two
            # seconds of the resume and did no further work, so this says "cut off", not "still running".
            message = (
                'the sandbox was paused while this command was running, and has been resumed. The command was '
                'cut off part-way and was not run again, so part of its work may be done. Check what it did '
                'before running it again.'
            )
        elif recovery == 'replaced':
            message = (
                'the sandbox session ended while this command was running, so its files are gone, along with '
                'whatever the command had done inside the sandbox. The command was not run again: the next '
                'command or file write starts a fresh, empty sandbox. Anything the command did outside the '
                'sandbox is still done, so check that before running it again.'
            )
        else:
            message = (
                f'the call lost the sandbox while this command was running ({reason}). The command was not run '
                'again: it may still be running, or it may have been cut off part-way, so part of its work may '
                'be done. Check what it did before running it again.'
            )
        super().__init__(message)


class _StartedOnce:
    """Runs ``run(session, started)`` for ``call_with_session`` without ever starting one command twice.

    ``call_with_session`` calls again after it recovers the session. That is right when the command
    never started. Once it had, running it again repeats work. On a resumed session the files are
    still there, and so is whatever the command did before the pause cut it off: measured on real
    VMs, the cut-off process was still listed for about a second after the resume, did no further
    work and was gone after that. On a replaced session nothing of the first run is left inside the
    sandbox, but whatever it did outside (a deploy, a request to some service) is done. So once a
    command's start is confirmed, the node does not start it again. ``run`` appends to ``started``
    when that happens, and ``recovered``, passed as ``on_event``, stops the retry from then on.

    Two things this does not cover. A start the sandbox never confirmed is treated as not run and is
    sent again after a resume (see ``CommandNotStartedError``). And the SDK itself sends a start
    again when the first got no answer at all (``Process._pump`` in the SDK), below anything the node
    sees.

    The same fact shapes what the agent is told when the call fails for good (see ``reported``).
    """

    def __init__(self, run, events: list):
        self._run = run
        self._events = events
        self.started = []

    def __call__(self, session):
        return self._run(session, self.started)

    def recovered(self, event: str) -> None:
        """Note how the session changed, and stop the retry if the command had started.

        Raised here, before the retry asks for a session, so that a session that ended is not
        replaced by a new VM for a call that will not use it. The result then says "ended", not
        "replaced": nothing replaced it yet.
        """
        if self.started and event in ('resumed', 'replaced'):
            self._events.append('resumed' if event == 'resumed' else 'ended')
            raise CommandInterruptedError(event)
        self._events.append(event)

    def reported(self, error: SandboxError) -> SandboxError:
        """The error to hand the agent for a call that failed.

        A command that had started is not reported as simply failed. Whatever ended the call (the
        session still pausing, a resume Tenki would not take yet, Tenki's own "termination
        requested", a look at the session that ran past its deadline), part of the command's work
        may be done, and an agent told only the cause repeats it blind. The exception is the node's
        own limit on the wait for a result, which is reported as the timeout it is.
        """
        if not self.started or isinstance(error, (CommandInterruptedError, ResultTimeoutError)):
            return error
        return CommandInterruptedError(reason=str(error))


class ChangeInterruptedError(SandboxError):
    """A change had been sent when its call lost the session to a pause, and it was not sent again."""

    def __init__(self, what: str) -> None:
        super().__init__(
            f'the sandbox was paused while this {what} was in flight, and has been resumed. It was not sent again, '
            'so it may or may not have taken effect. Check before retrying.'
        )


class _ChangeOnce:
    """Runs ``change(session, sent)`` for ``call_with_session`` without sending one change twice across a pause.

    Reads are safe to send again after a resume; a change is not. A request that went out when the pause
    came may or may not have been applied, and sending it again could apply it twice: a branch created
    twice, a directory removed after something else was put in its place. So once ``change`` has appended to
    ``sent`` (just before the request that changes something goes out), a resume ends the call with
    ``ChangeInterruptedError`` instead of a retry. A change that failed before it was sent, in the no-op
    command for example, is sent after the resume as usual. A session that ended is different: nothing of the
    first attempt is left in a terminated sandbox, and a file or git change has no effect outside it, so the
    retry runs on the new session.
    """

    def __init__(self, change, events: list, what: str):
        self._change = change
        self._events = events
        self._what = what
        self.sent = []

    def __call__(self, session):
        return self._change(session, self.sent)

    def recovered(self, event: str) -> None:
        """Note how the session changed, and stop the retry after a resume if the change had been sent."""
        self._events.append(event)
        if event == 'resumed' and self.sent:
            raise ChangeInterruptedError(self._what)


def _run_process(session, argv, timeout: int, started: list):
    """Run ``argv`` in ``session``, without ever waiting unboundedly or holding unbounded output.

    Returns the CommandResult, and whether the command was killed for producing more than
    _OUTPUT_LIMIT_BYTES of output (see _OutputGuard). ``started`` is appended to once the sandbox
    has confirmed the start (see _StartedOnce).

    Sandbox.exec waits on the output stream with no local limit, so a stream that stalls (on a
    half-open connection, say) would hang the tool call for good. Tenki stops the command at
    ``timeout`` and reports that on the result; the grace period leaves room for the report, so a
    wait that still runs out means the stream stalled, and it is reported as a timeout. The SDK
    sends a kill when that wait runs out, down the same stream, so it may not arrive. Waiting for
    the start sends nothing: the SDK gives up after 30 seconds and cancels nothing, so a start that
    arrived late would still run the command. A kill is sent here instead.
    """
    limit = timeout + _RESULT_GRACE_SECS
    process = session.start(*argv, timeout=timeout)
    try:
        process.close_stdin()
    except SandboxError:
        # Not the SDK's own plain "timed out waiting for command start": an SDK error is reported as itself.
        raise
    except TimeoutError as e:
        process.kill()
        raise CommandNotStartedError('the sandbox did not start the command in time, so it was cancelled') from e
    started.append(True)
    guard = _OutputGuard(process, _OUTPUT_LIMIT_BYTES)
    try:
        result = process.wait(timeout=limit)
    except SandboxError:
        raise
    except TimeoutError as e:
        raise ResultTimeoutError(
            f'the sandbox reported no result within {limit}s, {_RESULT_GRACE_SECS}s past the execution timeout '
            'at which Tenki stops a command. A kill was sent as well, but over a stalled connection it may '
            'not have arrived.'
        ) from e
    finally:
        guard.stop()
    return result, guard.killed


def _with_session_event(output: dict, events: list) -> dict:
    """Tell the agent how the session changed during its call, if it did (see _SESSION_EVENT): the last change."""
    if events:
        output['session'] = events[-1]
    return output


def _exec_error(error: SandboxError) -> dict:
    """Shape a failed sandbox call into the tool output."""
    return {
        'error': _explained(str(error)),
        'exit_code': -1,
        'stdout': '',
        'stderr': '',
        # A command stopped at its timeout comes back as a result with timed_out set; this is
        # the other path, where the call itself ran past the deadline.
        'timed_out': isinstance(error, CommandTimeoutError),
        'truncated': False,
    }


class IInstance(IInstanceBase):
    IGlobal: IGlobal

    def _collect_tool_methods(self) -> dict[str, Callable]:
        """Publish only the tools whose group is enabled in ``tenki.toolGroups``.

        Filtering here covers ``tool.query`` (the agent never sees the tool) and
        ``tool.invoke`` (calling it anyway is refused) alike. A tool without a group is never
        published, so a missing tag fails closed instead of widening what an agent can reach.
        """
        enabled = self.IGlobal.tool_groups
        return {
            name: method
            for name, method in super()._collect_tool_methods().items()
            if getattr(getattr(type(self), name, None), '__tenki_group__', None) in enabled
        }

    # -----------------------------------------------------------------------
    # Group: execution
    #
    # Both tools run through a bash login shell. Tenki's base image installs node with nvm,
    # which puts it on PATH through the shell's startup files, and Tenki's own shell helper
    # runs commands the same way.
    # -----------------------------------------------------------------------

    @tenki_tool(
        group='execution',
        input_schema={
            'type': 'object',
            'required': ['command'],
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'Shell command to run with bash, e.g. "pip install requests && python app.py".',
                },
                'cwd': {
                    'type': 'string',
                    'description': 'Directory to run in (optional); must be under /home/tenki, where a relative path resolves.',
                },
            },
        },
        output_schema=_EXEC_OUTPUT_SCHEMA,
        description=lambda self: (
            'Run a shell command in the remote Tenki sandbox (isolated from this machine): install '
            'dependencies, build, run tests. On the default image commands run as user tenki with '
            'passwordless sudo, so install system packages with sudo apt-get. The call waits until '
            "the command's output streams close, so start a long-running server in the background "
            'with its output redirected, e.g. "python3 -m http.server 3000 >/home/tenki/server.log '
            '2>&1 </dev/null &". '
            f'{_SESSION_NOTE} Execution timeout: {self.IGlobal.exec_timeout_secs}s.'
        ),
    )
    def run_command(self, args):
        """Run a shell command in the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        command = args.get('command')
        if not command or not isinstance(command, str) or not command.strip():
            raise ValueError('"command" is required and must be a non-empty string')

        cwd = args.get('cwd')
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError('"cwd" must be a string when provided')

        if cwd and cwd.strip():
            # Changed inside the script rather than passed as exec's cwd: a login shell runs the
            # guest's startup files first, and a cd in them would win over exec's cwd.
            command = f'cd {shlex.quote(_normalize_path(cwd))} && {command}'

        def run(session, started):
            return _run_process(session, ('bash', '-lc', command), self.IGlobal.exec_timeout_secs, started)

        events = []
        once = _StartedOnce(run, events)
        try:
            result, killed = self.IGlobal.call_with_session(once, on_event=once.recovered)
        except SandboxError as e:
            return _with_session_event(_exec_error(once.reported(e)), events)
        return _with_session_event(_exec_result(result, self.IGlobal.max_output_chars, killed=killed), events)

    @tenki_tool(
        group='execution',
        input_schema={
            'type': 'object',
            'required': ['code'],
            'properties': {
                'code': {
                    'type': 'string',
                    'description': 'Source code to execute. Print what you need to see: only stdout and stderr come back.',
                },
                'language': {
                    'type': 'string',
                    'enum': sorted(_LANGUAGES),
                    'description': ('Language of the code: python (python3) or javascript (node). Defaults to python.'),
                },
            },
        },
        output_schema=_EXEC_OUTPUT_SCHEMA,
        description=lambda self: (
            'Execute a code snippet in the remote Tenki sandbox (isolated from this machine) and return '
            'its output. The code is saved to a temporary file under /home/tenki, run, and the file is '
            'removed afterwards. '
            f'{_SESSION_NOTE} Execution timeout: {self.IGlobal.exec_timeout_secs}s.'
        ),
    )
    def run_code(self, args):
        """Execute a code snippet in the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        code = args.get('code')
        if not code or not isinstance(code, str) or not code.strip():
            raise ValueError('"code" is required and must be a non-empty string')

        language = args.get('language') or 'python'
        if language not in _LANGUAGES:
            raise ValueError(f'"language" must be one of: {", ".join(sorted(_LANGUAGES))}')
        interpreter, extension = _LANGUAGES[language]
        # A fresh name per call: parallel calls share the session, and a fixed name would let
        # one call overwrite the file another is about to run.
        path = f'{_HOME}/.tenki-run-{uuid.uuid4().hex}{extension}'

        def run(session, started):
            # Tenki has no code-run primitive. The code goes in through the file API instead of a
            # shell heredoc, whose escaping would mangle multi-line code.
            session.fs.write_text(path, code)
            try:
                # "$@" hands the interpreter and path to exec as separate arguments, unquoted.
                argv = ('bash', '-lc', 'exec "$@"', 'bash', interpreter, path)
                return _run_process(session, argv, self.IGlobal.exec_timeout_secs, started)
            finally:
                try:
                    session.fs.remove(path)
                except Exception:
                    # Best effort: a leftover file under /home/tenki is harmless, and failing
                    # here would hide the result (or the real error) of the run itself.
                    pass

        events = []
        once = _StartedOnce(run, events)
        try:
            result, killed = self.IGlobal.call_with_session(once, on_event=once.recovered)
        except SandboxError as e:
            return _with_session_event(_exec_error(once.reported(e)), events)
        return _with_session_event(_exec_result(result, self.IGlobal.max_output_chars, killed=killed), events)

    # -----------------------------------------------------------------------
    # Group: filesystem
    #
    # Every path goes through _normalize_path before any call. Reads, listings and deletes pass
    # replace=False: the call that finds the session ended is not answered from a fresh, empty one,
    # which could not contain what it looks for. It says the session ended instead. The call after
    # that starts a session like any first call does, whichever tool it is.
    # -----------------------------------------------------------------------

    @tenki_tool(
        group='filesystem',
        input_schema={
            'type': 'object',
            'required': ['path', 'content'],
            'properties': {
                'path': {'type': 'string', 'description': 'File to write, e.g. "app/main.py".'},
                'content': {'type': 'string', 'description': 'Text to write (UTF-8); it replaces the whole file.'},
            },
        },
        output_schema=_CHANGE_OUTPUT_SCHEMA,
        description=lambda self: (
            'Write a text file in the remote Tenki sandbox, creating it or replacing its content, '
            f'along with any missing parent directories. {_PATH_NOTE} {_SESSION_NOTE}'
        ),
    )
    def write_file(self, args):
        """Write a text file in the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        path = _normalize_path(args.get('path'))
        content = args.get('content')
        if not isinstance(content, str):
            raise ValueError('"content" is required and must be a string')

        def write(session, sent):
            sent.append(True)
            try:
                session.fs.write_text(path, content)
            except TenkiFileNotFoundError as e:
                if about_the_vm_link(e):
                    raise
                # The parent directory is missing: create it, with its own parents, and write again.
                session.fs.mkdir(posixpath.dirname(path))
                session.fs.write_text(path, content)

        events = []
        once = _ChangeOnce(write, events, 'file write')
        try:
            self.IGlobal.call_with_session(once, on_event=once.recovered)
        except SandboxError as e:
            return _with_session_event({'success': False, 'path': path, 'error': _fs_error(e, path)}, events)
        return _with_session_event({'success': True, 'path': path}, events)

    @tenki_tool(
        group='filesystem',
        input_schema={
            'type': 'object',
            'required': ['path'],
            'properties': {
                'path': {'type': 'string', 'description': 'File to read, e.g. "app/main.py".'},
            },
        },
        output_schema={
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'Absolute path in the sandbox.'},
                'content': {'type': 'string', 'description': 'File content decoded as UTF-8.'},
                'truncated': {'type': 'boolean', 'description': 'True if the content was cut at the output cap.'},
                'error': {'type': 'string', 'description': 'Error message if the file could not be read.'},
                'session': _SESSION_EVENT,
            },
        },
        description=lambda self: (
            'Read a text file from the remote Tenki sandbox, decoded as UTF-8. At most '
            f'{self.IGlobal.max_output_chars} characters are returned, and a file over '
            f'{_READ_LIMIT_BYTES:,} bytes is refused. {_PATH_NOTE} {_SESSION_NOTE}'
        ),
    )
    def read_file(self, args):
        """Read a text file from the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        path = _normalize_path(args.get('path'))

        def read(session):
            info = session.fs.stat(path)
            if info.is_dir or info.size > _READ_LIMIT_BYTES:
                return info, None
            return info, session.fs.read_bytes(path)

        events = []
        try:
            info, data = self.IGlobal.call_with_session(read, replace=False, on_event=events.append)
        except (SandboxError, SessionEndedError) as e:
            failed = {'path': path, 'content': '', 'truncated': False, 'error': _fs_error(e, path)}
            return _with_session_event(failed, events)
        if data is None:
            if info.is_dir:
                error = f'{path} is a directory; use list_files to see what is in it'
            else:
                error = (
                    f'{path} is {info.size:,} bytes, more than the {_READ_LIMIT_BYTES:,} read_file fetches; '
                    'read the part you need with run_command (head, tail or sed -n)'
                )
            return _with_session_event({'path': path, 'content': '', 'truncated': False, 'error': error}, events)
        content, truncated = _truncate(data.decode('utf-8', errors='replace'), self.IGlobal.max_output_chars)
        return _with_session_event({'path': path, 'content': content, 'truncated': truncated}, events)

    @tenki_tool(
        group='filesystem',
        input_schema={
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'Directory to list. Defaults to /home/tenki.'},
                'include_hidden': {
                    'type': 'boolean',
                    'description': 'Include entries whose names start with a dot. Defaults to false.',
                },
            },
        },
        output_schema={
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'Absolute path of the listed directory.'},
                'entries': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string'},
                            'is_dir': {'type': 'boolean'},
                            'size': {'type': 'integer', 'description': 'Size in bytes.'},
                        },
                    },
                },
                'truncated': {'type': 'boolean', 'description': 'True if the listing was cut at the output cap.'},
                'error': {'type': 'string', 'description': 'Error message if the directory could not be listed.'},
                'session': _SESSION_EVENT,
            },
        },
        description=lambda self: (
            "List a directory in the remote Tenki sandbox: each entry's name, whether it is a "
            f'directory, and its size, sorted by name. {_PATH_NOTE} {_SESSION_NOTE}'
        ),
    )
    def list_files(self, args):
        """List a directory in the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        path = _normalize_path(args.get('path') or '.')
        include_hidden = optional_bool(args, 'include_hidden', default=False, tool_name='tenki')

        events = []
        try:
            entries = self.IGlobal.call_with_session(
                lambda session: session.fs.list(path, include_hidden=include_hidden),
                replace=False,
                on_event=events.append,
            )
        except (SandboxError, SessionEndedError) as e:
            failed = {'path': path, 'entries': [], 'truncated': False, 'error': _fs_error(e, path)}
            return _with_session_event(failed, events)
        shaped, truncated = _listing(entries, self.IGlobal.max_output_chars)
        return _with_session_event({'path': path, 'entries': shaped, 'truncated': truncated}, events)

    @tenki_tool(
        group='filesystem',
        input_schema={
            'type': 'object',
            'required': ['path'],
            'properties': {
                'path': {'type': 'string', 'description': 'Directory to create, e.g. "data/raw".'},
            },
        },
        output_schema=_CHANGE_OUTPUT_SCHEMA,
        description=lambda self: (
            'Create a directory in the remote Tenki sandbox, including any missing parent directories. '
            f'{_PATH_NOTE} {_SESSION_NOTE}'
        ),
    )
    def make_directory(self, args):
        """Create a directory in the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        path = _normalize_path(args.get('path'))

        def mkdir(session, sent):
            sent.append(True)
            session.fs.mkdir(path)

        events = []
        once = _ChangeOnce(mkdir, events, 'directory creation')
        try:
            self.IGlobal.call_with_session(once, on_event=once.recovered)
        except SandboxError as e:
            return _with_session_event({'success': False, 'path': path, 'error': _fs_error(e, path)}, events)
        return _with_session_event({'success': True, 'path': path}, events)

    @tenki_tool(
        group='filesystem',
        input_schema={
            'type': 'object',
            'required': ['path'],
            'properties': {
                'path': {'type': 'string', 'description': 'File or directory to delete, e.g. "build".'},
            },
        },
        output_schema=_CHANGE_OUTPUT_SCHEMA,
        description=lambda self: (
            'Delete a file or directory in the remote Tenki sandbox. A directory is deleted recursively, '
            'with everything inside it, and cannot be recovered. /home/tenki itself cannot be deleted. '
            f'{_PATH_NOTE} {_SESSION_NOTE}'
        ),
    )
    def delete_path(self, args):
        """Delete a file or directory, recursively, in the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        path = _normalize_path(args.get('path'))
        if path == _HOME:
            # The agent's whole workspace: never a sensible target for one tool call.
            raise ValueError('refusing to delete /home/tenki itself; name a file or directory inside it')

        def remove(session, sent):
            sent.append(True)
            session.fs.remove(path)

        events = []
        once = _ChangeOnce(remove, events, 'delete')
        try:
            self.IGlobal.call_with_session(once, replace=False, on_event=once.recovered)
        except (SandboxError, SessionEndedError) as e:
            return _with_session_event({'success': False, 'path': path, 'error': _fs_error(e, path)}, events)
        return _with_session_event({'success': True, 'path': path}, events)

    # -----------------------------------------------------------------------
    # Group: git
    #
    # Tenki's structured git helpers, which return git's raw output. Only git_clone may start a
    # fresh session: the others work on a repository that an ended session took with it.
    # -----------------------------------------------------------------------

    @tenki_tool(
        group='git',
        input_schema={
            'type': 'object',
            'required': ['repo'],
            'properties': {
                'repo': {
                    'type': 'string',
                    'description': 'Repository URL, e.g. "https://github.com/octocat/Hello-World.git".',
                },
                'directory': {
                    'type': 'string',
                    'description': (
                        'Folder to clone into, relative to /home/tenki or absolute under it. Defaults to '
                        'a folder named after the repository.'
                    ),
                },
                'branch': {'type': 'string', 'description': 'Branch to check out after cloning (optional).'},
                'depth': {
                    'type': 'integer',
                    'minimum': 1,
                    'description': 'Clone only this many of the most recent commits (optional).',
                },
            },
        },
        output_schema={
            'type': 'object',
            'properties': {
                'directory': {'type': 'string', 'description': 'Absolute path of the cloned repository.'},
                **_GIT_OUTPUT_SCHEMA['properties'],
            },
        },
        description=lambda self: (
            "Clone a git repository into the remote Tenki sandbox, returning git's output and the folder "
            'it was cloned into. Private repositories cannot be cloned: this node holds no git credentials. '
            f'The clone must finish within {self.IGlobal.rpc_timeout_secs}s, so use depth for large '
            f'repositories. {_SESSION_NOTE}'
        ),
    )
    def git_clone(self, args):
        """Clone a git repository into the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        repo = _public_repo_url(_git_arg(args, 'repo', required=True))
        branch = _git_arg(args, 'branch')
        depth = optional_int(args, 'depth', lo=1, tool_name='tenki')
        directory = _git_directory(args)
        if directory is None:
            # Chosen here rather than left to git, so the result can say where the repository went.
            folder = _clone_folder(repo)
            if folder is None:
                raise ValueError('could not derive a folder name from "repo"; pass "directory"')
            directory = _normalize_path(folder)

        def clone(session, sent):
            sent.append(True)
            return session.git.clone(repo, branch=branch, depth=depth, directory=directory)

        events = []
        once = _ChangeOnce(clone, events, 'clone')
        try:
            output = self.IGlobal.call_with_session(once, on_event=once.recovered)
        except SandboxError as e:
            failed = {'directory': directory, 'output': '', 'truncated': False, 'error': _explained(str(e))}
            return _with_session_event(failed, events)
        cloned = {'directory': directory, **_git_result(output, self.IGlobal.max_output_chars)}
        return _with_session_event(cloned, events)

    @tenki_tool(
        group='git',
        input_schema={
            'type': 'object',
            'required': ['ref'],
            'properties': {
                'ref': {'type': 'string', 'description': 'Branch, tag or commit to check out.'},
                'create': {
                    'type': 'boolean',
                    'description': 'Create ref as a new branch (like git checkout -b). Defaults to false.',
                },
                'directory': _REPO_DIRECTORY,
            },
        },
        output_schema=_GIT_OUTPUT_SCHEMA,
        description=lambda self: (
            'Check out a branch, tag or commit in a git repository in the remote Tenki sandbox, or create '
            f'a new branch. {_SESSION_NOTE}'
        ),
    )
    def git_checkout(self, args):
        """Check out a ref in a repository in the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        ref = _git_arg(args, 'ref', required=True)
        create = optional_bool(args, 'create', default=False, tool_name='tenki')
        directory = _git_directory(args)

        def checkout(session, sent):
            sent.append(True)
            return session.git.checkout(ref, create=create, directory=directory)

        events = []
        once = _ChangeOnce(checkout, events, 'checkout')
        try:
            output = self.IGlobal.call_with_session(once, replace=False, on_event=once.recovered)
        except (SandboxError, SessionEndedError) as e:
            return _with_session_event({'output': '', 'truncated': False, 'error': _explained(str(e))}, events)
        return _with_session_event(_git_result(output, self.IGlobal.max_output_chars), events)

    @tenki_tool(
        group='git',
        input_schema={
            'type': 'object',
            'properties': {
                'range': {'type': 'string', 'description': 'Revision range to compare, e.g. "main..fix" (optional).'},
                'base': {'type': 'string', 'description': 'Revision to compare from, instead of a range (optional).'},
                'head': {'type': 'string', 'description': 'Revision to compare to, instead of a range (optional).'},
                'path': {
                    'type': 'string',
                    'description': 'Limit the diff to this file or folder, relative to the repository (optional).',
                },
                'directory': _REPO_DIRECTORY,
            },
        },
        output_schema=_GIT_OUTPUT_SCHEMA,
        description=lambda self: (
            'Show a git diff for a repository in the remote Tenki sandbox. Choose the revisions with a '
            f'range, or with base and head, and narrow it with path. {_SESSION_NOTE}'
        ),
    )
    def git_diff(self, args):
        """Show a diff for a repository in the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        revisions = {key: _git_arg(args, key) for key in ('range', 'base', 'head')}
        if revisions['range'] and (revisions['base'] or revisions['head']):
            raise ValueError('pass either "range" or "base" and "head", not both')
        path = _git_arg(args, 'path')
        directory = _git_directory(args)

        events = []
        try:
            output = self.IGlobal.call_with_session(
                lambda session: session.git.diff(**revisions, path=path, directory=directory),
                replace=False,
                on_event=events.append,
            )
        except (SandboxError, SessionEndedError) as e:
            return _with_session_event({'output': '', 'truncated': False, 'error': _explained(str(e))}, events)
        return _with_session_event(_git_result(output, self.IGlobal.max_output_chars), events)

    @tenki_tool(
        group='git',
        input_schema={
            'type': 'object',
            'properties': {
                'max_count': {
                    'type': 'integer',
                    'minimum': 1,
                    'maximum': _MAX_LOG_COUNT,
                    'description': f'Number of commits to show. Defaults to {_DEFAULT_LOG_COUNT}.',
                },
                'range': {'type': 'string', 'description': 'Revision range to show, e.g. "main..fix" (optional).'},
                'path': {
                    'type': 'string',
                    'description': 'Only commits touching this file or folder, relative to the repository (optional).',
                },
                'directory': _REPO_DIRECTORY,
            },
        },
        output_schema=_GIT_OUTPUT_SCHEMA,
        description=lambda self: (
            'Show the commit log of a git repository in the remote Tenki sandbox, newest first, '
            f'{_DEFAULT_LOG_COUNT} commits unless max_count says otherwise. {_SESSION_NOTE}'
        ),
    )
    def git_log(self, args):
        """Show the commit log of a repository in the shared Tenki session."""
        args = normalize_tool_input(args, tool_name='tenki')
        max_count = optional_int(
            args, 'max_count', default=_DEFAULT_LOG_COUNT, lo=1, hi=_MAX_LOG_COUNT, tool_name='tenki'
        )
        revision_range = _git_arg(args, 'range')
        path = _git_arg(args, 'path')
        directory = _git_directory(args)

        events = []
        try:
            output = self.IGlobal.call_with_session(
                lambda session: session.git.log(
                    max_count=max_count, range=revision_range, path=path, directory=directory
                ),
                replace=False,
                on_event=events.append,
            )
        except (SandboxError, SessionEndedError) as e:
            return _with_session_event({'output': '', 'truncated': False, 'error': _explained(str(e))}, events)
        return _with_session_event(_git_result(output, self.IGlobal.max_output_chars), events)
