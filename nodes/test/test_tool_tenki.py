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

"""Unit tests for tool_tenki: session lifecycle, tool groups and tools (no network, no real keys)."""

from __future__ import annotations

import importlib
import importlib.util
import json
import posixpath
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: when run under a bare interpreter that lacks the engine runtime
# (rocketlib, ai.common, the tenki SDK), inject lightweight stubs ONLY for
# modules that are not already present, import the module under test, then
# REMOVE the stubs we added so they never leak into the shared pytest session
# (see test_tool_tavily.py for the full rationale).
# ---------------------------------------------------------------------------

_NODES_SRC = Path(__file__).resolve().parents[1] / 'src'
_TOOL_ARGS = (
    Path(__file__).resolve().parents[2] / 'packages' / 'ai' / 'src' / 'ai' / 'common' / 'utils' / 'tool_args.py'
)
if str(_NODES_SRC) not in sys.path:
    sys.path.insert(0, str(_NODES_SRC))

# Placeholder only. The tk_ prefix matters: the real SDK rejects any other key
# locally, before a request is made. Every SDK entry point is faked below, so it
# is never sent anywhere.
_KEY = 'tk_mock-tenki-placeholder-for-tests'


class _StubSandboxError(Exception):
    """Real exception classes, so IGlobal's except clauses catch them under the stub."""


class _StubSessionNotFoundError(_StubSandboxError):
    """Mirror of the SDK hierarchy: every Tenki error subclasses SandboxError directly."""


class _StubSessionTerminatedError(_StubSandboxError):
    def __init__(self, *args, termination_cause=None):
        super().__init__(*args)
        self.termination_cause = termination_cause


class _StubInvalidStateError(_StubSandboxError):
    pass


class _StubCommandTimeoutError(_StubSandboxError):
    pass


class _StubFileNotFoundError(_StubSandboxError, FileNotFoundError):
    """Mirror of the SDK, whose FileNotFoundError also subclasses the builtin."""


class _StubPermissionDeniedError(_StubSandboxError):
    pass


class _StubWaitReadyFailedError(_StubSandboxError):
    def __init__(self, message, sandbox=None):
        super().__init__(message)
        self.sandbox = sandbox


class _StubTemplateRuntimeFailedError(_StubSandboxError):
    def __init__(self, message, sandbox=None):
        super().__init__(message)
        self.sandbox = sandbox
        self.reason = message


def _tool_function(*, input_schema=None, description=None, output_schema=None):
    """Mirror of rocketlib.tool_function: stamps the metadata that tool.query reads."""

    def decorator(fn):
        fn.__tool_meta__ = {'input_schema': input_schema, 'description': description, 'output_schema': output_schema}
        return fn

    return decorator


class _StubInstanceBase:
    """Mirror of rocketlib.IInstanceBase._collect_tool_methods, the hook the node filters."""

    def _collect_tool_methods(self):
        methods = {}
        for name in dir(type(self)):
            attr = getattr(type(self), name, None)
            if attr is not None and hasattr(attr, '__tool_meta__'):
                methods[name] = getattr(self, name)
        return methods


def _build_import_stubs():
    """Return {module_name: stub} for the deps needed only to import the module."""
    rocketlib = MagicMock()
    rocketlib.IInstanceBase = _StubInstanceBase
    rocketlib.IGlobalBase = object
    rocketlib.tool_function = _tool_function
    rocketlib.debug = lambda *a, **kw: None
    rocketlib.error = lambda *a, **kw: None
    rocketlib.warning = lambda *a, **kw: None
    rocketlib.OPEN_MODE = MagicMock()

    depends = MagicMock()
    depends.depends = lambda *a, **kw: None

    tenki = MagicMock()
    tenki.Client = MagicMock()
    tenki.Sandbox = MagicMock()
    tenki.SandboxError = _StubSandboxError
    tenki.SessionNotFoundError = _StubSessionNotFoundError
    tenki.SessionTerminatedError = _StubSessionTerminatedError
    tenki.InvalidStateError = _StubInvalidStateError
    tenki.CommandTimeoutError = _StubCommandTimeoutError
    tenki.FileNotFoundError = _StubFileNotFoundError
    tenki.PermissionDeniedError = _StubPermissionDeniedError
    tenki.WaitReadyFailedError = _StubWaitReadyFailedError
    tenki.TemplateRuntimeFailedError = _StubTemplateRuntimeFailedError

    return {
        'rocketlib': rocketlib,
        'depends': depends,
        'ai': MagicMock(),
        'ai.common': MagicMock(),
        'ai.common.config': MagicMock(),
        'tenki': tenki,
    }


_added_stubs = []
for _name, _stub in _build_import_stubs().items():
    if _name not in sys.modules:
        sys.modules[_name] = _stub
        _added_stubs.append(_name)

# The real tool-argument helpers, loaded from source as tool_gohighlevel's tests do: a
# hand-written stand-in would stop tracking normalize_tool_input the moment it changes.
# rocketlib is already in sys.modules here, and tool_args imports warning from it.
if 'ai.common.utils' not in sys.modules:
    _spec = importlib.util.spec_from_file_location('ai.common.utils', _TOOL_ARGS)
    sys.modules['ai.common.utils'] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules['ai.common.utils'])
    _added_stubs.append('ai.common.utils')

mod = importlib.import_module('nodes.tool_tenki.IGlobal')
inst_mod = importlib.import_module('nodes.tool_tenki.IInstance')
groups = importlib.import_module('nodes.tool_tenki.tool_groups')

for _name in _added_stubs:
    sys.modules.pop(_name, None)


# ---------------------------------------------------------------------------
# Fakes for the Tenki SDK boundary (the network)
# ---------------------------------------------------------------------------


# What Tenki answers a resume while it is still tearing the paused VM down (measured: one to three
# times over 18 to 48 seconds after a pause, then the resume is accepted).
_NOT_YET = 'paused source is still being torn down, retry shortly: deadline_exceeded: context deadline exceeded'


class _FakeSession:
    """Stand-in for tenki.Sandbox, with the SDK's semantics for the lifecycle calls.

    ``refresh`` returns the session's info record and ``close_if_open`` skips a
    session that is already terminating or terminated, as the real handle does.
    """

    def __init__(
        self,
        session_id,
        *,
        state='RUNNING',
        refresh_error=None,
        resume_delay=0.0,
        close_error=None,
        exec_result=None,
        exec_error=None,
        files=None,
        dirs=None,
        git_outputs=None,
        fail=None,
        stalls=None,
        flood=None,
        run_error=None,
        ready_state=None,
        ready_error=None,
        paused_error=None,
        resume_refusals=0,
    ):
        self.id = session_id
        # How many times resume is refused first, as Tenki does while it is still tearing the paused VM down.
        self._resume_refusals = resume_refusals
        self.state = state
        # What a call on this session raises while it is paused. Tenki usually names the state; it
        # has also been seen to answer with an error the SDK cannot classify.
        self.paused_error = paused_error or mod.InvalidStateError('session is paused')
        self._refresh_error = refresh_error
        self._resume_delay = resume_delay
        self._close_error = close_error
        self._exec_result = exec_result
        self._exec_error = exec_error
        self._stalls = stalls
        self._flood = flood
        self._run_error = run_error
        self._ready_state = ready_state
        self._ready_error = ready_error
        self.processes = []
        self.calls = []
        self.ready_timeouts = []
        self.fs = _FakeFS(self, files=files, dirs=dirs, fail=fail)
        self.git = _FakeGit(self, outputs=git_outputs, fail=fail)

    def start(self, *argv, cwd=None, env=None, timeout=None, stdin=None, privileged=False):
        kwargs = {'cwd': cwd, 'env': env, 'timeout': timeout, 'stdin': stdin, 'privileged': privileged}
        self.calls.append(('start', argv, kwargs))
        if self.state == 'PAUSED':
            raise self.paused_error
        if self._exec_error is not None:
            raise self._exec_error
        result = self._exec_result if self._exec_result is not None else _CommandResult(argv=list(argv), exit_code=0)
        process = _FakeProcess(self, result, stalls=self._stalls, flood=self._flood, run_error=self._run_error)
        self.processes.append(process)
        return process

    def refresh(self):
        self.calls.append('refresh')
        if self._refresh_error is not None:
            raise self._refresh_error
        return SimpleNamespace(id=self.id, state=self.state)

    def resume(self):
        self.calls.append('resume')
        if self._resume_refusals:
            self._resume_refusals -= 1
            raise mod.SandboxError(_NOT_YET)
        time.sleep(self._resume_delay)
        self.state = 'RUNNING'

    def wait_ready(self, timeout=180):
        self.calls.append('wait_ready')
        self.ready_timeouts.append(timeout)
        # The SDK raises a plain RuntimeError when the session went terminal while it waited, and a
        # plain TimeoutError when it never got to RUNNING; it updates the session's state first.
        if self._ready_state is not None:
            self.state = self._ready_state
        if self._ready_error is not None:
            raise self._ready_error

    def close(self):
        self.calls.append('close')
        if self._close_error is not None:
            raise self._close_error
        self.state = 'TERMINATED'

    def close_if_open(self):
        if self.state not in ('TERMINATING', 'TERMINATED'):
            self.close()


class _FakeStream:
    """Stand-in for tenki's ProcessStream: a queue the run pushes chunks into, read by iterating.

    Iteration blocks until the next chunk or the end, as the SDK's does. ``taken`` is set each time
    a reader takes something from it, which is how a flooding command is paced by its reader, as a
    real one is paced by the network.
    """

    def __init__(self):
        self._queue = queue.Queue()
        self.taken = threading.Event()

    def push(self, data):
        if data:
            self._queue.put(data)

    def close(self):
        self._queue.put(None)

    def fail(self, error):
        self._queue.put(error)

    def __iter__(self):
        while True:
            item = self._queue.get()
            self.taken.set()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item


class _FakeProcess:
    """Stand-in for tenki's Process, whose stream can stall before the command starts or before it ends.

    As in the SDK, close_stdin waits for the start and raises TimeoutError when it never comes,
    without cancelling anything, while a wait that runs out kills the process before it raises.
    Output streams in while the command runs and the result arrives once it exits. ``flood``, a
    ``(chunk, budget)`` pair, makes the command print ``chunk`` until it is killed, or until it has
    printed ``budget`` bytes or nothing reads it, either of which ends it on its own.
    """

    def __init__(self, session, result, *, stalls=None, flood=None, run_error=None):
        self._session = session
        self._result = result
        self._stalls = stalls
        self._error = run_error
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.printed = 0
        self._killed = threading.Event()
        self._exited = threading.Event()
        if stalls is None:
            threading.Thread(target=self._run, args=(flood,), daemon=True).start()

    def _run(self, flood):
        if self._error is not None:
            # As in the SDK: a run that fails ends both streams with its error.
            self.stdout.fail(self._error)
            self.stderr.fail(self._error)
        elif flood is None:
            self.stdout.push(self._result.stdout)
            self.stderr.push(self._result.stderr)
        else:
            chunk, budget = flood
            while self.printed < budget and not self._killed.is_set():
                self.stdout.taken.clear()
                self.stdout.push(chunk)
                self.printed += len(chunk)
                if not self.stdout.taken.wait(timeout=1):
                    break
            killed = self._killed.is_set()
            self._result = _CommandResult(
                argv=self._result.argv,
                exit_code=-1 if killed else 0,
                stdout=chunk * (self.printed // len(chunk)),
                signal='SIGKILL' if killed else None,
            )
        # As in the SDK: the result is ready before the streams are ended.
        self._exited.set()
        self.stdout.close()
        self.stderr.close()

    def close_stdin(self):
        self._session.calls.append(('close_stdin',))
        if self._stalls == 'start':
            raise TimeoutError('timed out waiting for command start')

    def wait(self, timeout=None):
        self._session.calls.append(('wait', timeout))
        if self._stalls == 'result' or not self._exited.wait(timeout):
            self.kill()
            raise TimeoutError('timed out waiting for command')
        if self._error is not None:
            raise self._error
        return self._result

    def kill(self):
        self._session.calls.append(('kill',))
        self._killed.set()


class _FakeFS:
    """Stand-in for tenki's SandboxFS over an in-memory tree, with the SDK's signatures.

    Calls are logged into the session's call log, a paused session refuses them like the fake
    exec does, and ``fail`` maps an operation name to the error it raises. There is deliberately
    no ``read_stream``: it has no deadline, so the node must never call it.
    """

    def __init__(self, session, *, files=None, dirs=None, fail=None):
        self._session = session
        self._fail = dict(fail or {})
        self.files = dict(files or {})
        self.dirs = {'/home/tenki', *(dirs or ())}
        self.written = {}

    def _enter(self, operation):
        if self._session.state == 'PAUSED':
            raise self._session.paused_error
        if operation in self._fail:
            raise self._fail[operation]

    def _missing(self, path):
        # Without the path, as a terse service message may be: the tool has to name it.
        return inst_mod.TenkiFileNotFoundError('no such file or directory')

    def write_text(self, path, text, *, encoding='utf-8'):
        self._session.calls.append(('write_text', path))
        self._enter('write_text')
        if posixpath.dirname(path) not in self.dirs:
            raise self._missing(path)
        self.files[path] = text
        self.written[path] = text

    def stat(self, path):
        self._session.calls.append(('stat', path))
        self._enter('stat')
        if path in self.dirs:
            return _FileInfo(path=path, size=4096, mode=0o755, is_dir=True, modified_unix_ns=0)
        if path not in self.files:
            raise self._missing(path)
        return _FileInfo(path=path, size=len(self.files[path].encode()), mode=0o644, is_dir=False, modified_unix_ns=0)

    def read_bytes(self, path):
        self._session.calls.append(('read_bytes', path))
        self._enter('read_bytes')
        if path not in self.files:
            raise self._missing(path)
        return self.files[path].encode()

    def list(self, path, *, include_hidden=False):
        self._session.calls.append(('list', path, include_hidden))
        self._enter('list')
        if path not in self.dirs:
            raise self._missing(path)
        children = {}
        for child in self.dirs:
            if child != path and posixpath.dirname(child) == path:
                name = posixpath.basename(child)
                children[name] = _FileInfo(path=name, size=0, mode=0o755, is_dir=True, modified_unix_ns=0)
        for file_path, text in self.files.items():
            if posixpath.dirname(file_path) == path:
                name = posixpath.basename(file_path)
                children[name] = _FileInfo(
                    path=name, size=len(text.encode()), mode=0o644, is_dir=False, modified_unix_ns=0
                )
        return [info for name, info in children.items() if include_hidden or not name.startswith('.')]

    def mkdir(self, path, *, recursive=True, mode=0o755):
        self._session.calls.append(('mkdir', path))
        self._enter('mkdir')
        if not recursive and posixpath.dirname(path) not in self.dirs:
            raise self._missing(path)
        while path not in self.dirs:
            self.dirs.add(path)
            path = posixpath.dirname(path)

    def remove(self, path, *, recursive=True):
        self._session.calls.append(('remove', path, recursive))
        self._enter('remove')
        inside = [p for p in (*self.files, *self.dirs) if p.startswith(path + '/')]
        if path not in self.files and path not in self.dirs:
            raise self._missing(path)
        if inside and not recursive:
            raise mod.InvalidStateError(f'directory not empty: {path}')
        self.files = {p: t for p, t in self.files.items() if p != path and not p.startswith(path + '/')}
        self.dirs = {d for d in self.dirs if d != path and not d.startswith(path + '/')}


class _FakeGit:
    """Stand-in for tenki's SandboxGit, with the SDK's signatures; each call returns git's raw output."""

    def __init__(self, session, *, outputs=None, fail=None):
        self._session = session
        self._outputs = dict(outputs or {})
        self._fail = dict(fail or {})

    def _run(self, operation, kwargs):
        self._session.calls.append((operation, kwargs))
        if self._session.state == 'PAUSED':
            raise mod.InvalidStateError('session is paused')
        if operation in self._fail:
            raise self._fail[operation]
        return self._outputs.get(operation, '')

    def clone(self, repo, *, branch=None, depth=None, directory=None):
        return self._run('clone', {'repo': repo, 'branch': branch, 'depth': depth, 'directory': directory})

    def checkout(self, ref, *, create=False, directory=None):
        return self._run('checkout', {'ref': ref, 'create': create, 'directory': directory})

    def diff(self, *, range=None, base=None, head=None, path=None, directory=None):
        return self._run('diff', {'range': range, 'base': base, 'head': head, 'path': path, 'directory': directory})

    def log(self, *, max_count=None, range=None, path=None, directory=None):
        return self._run('log', {'max_count': max_count, 'range': range, 'path': path, 'directory': directory})


@dataclass(frozen=True)
class _FileInfo:
    """Mirror of tenki.FileInfo. In a directory listing, ``path`` holds the entry's name."""

    path: str
    size: int
    mode: int
    is_dir: bool
    modified_unix_ns: int
    is_symlink: bool = False
    symlink_target: str = ''


@dataclass(frozen=True)
class _CommandResult:
    """Mirror of tenki.CommandResult: the SDK's fields and its text helpers."""

    argv: list
    exit_code: int
    stdout: bytes = b''
    stderr: bytes = b''
    signal: str | None = None
    duration_ms: int | None = None
    reason: str | None = None
    errno: int | None = None
    timed_out: bool = False

    @property
    def ok(self):
        return self.exit_code == 0 and not self.signal and not self.timed_out

    @property
    def stdout_text(self):
        return self.stdout.decode(errors='replace')

    @property
    def stderr_text(self):
        return self.stderr.decode(errors='replace')


class _FakeClient:
    """Stand-in for tenki.Client: hands out the given sessions in order and records calls."""

    def __init__(
        self, *sessions, create_error=None, create_delay=0.0, leftover=None, list_error=None, close_error=None
    ):
        self._sessions = list(sessions)
        self._create_error = create_error
        self._create_delay = create_delay
        self._leftover = list(leftover or [])
        self._list_error = list_error
        self._close_error = close_error
        self.create_started = threading.Event()
        self.closing = threading.Event()
        self.close_gate = None
        self.create_calls = []
        self.list_calls = []
        self.fetched = {}
        self.closed = False

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self.create_started.set()
        time.sleep(self._create_delay)
        if self._create_error is not None:
            raise self._create_error
        return self._sessions.pop(0)

    def get(self, session_id):
        session = self.fetched[session_id] = _FakeSession(session_id)
        return session

    def list(self, *, tags=None, **kwargs):
        self.list_calls.append(tags)
        if self._list_error is not None:
            raise self._list_error
        return list(self._leftover)

    def close(self):
        self.closing.set()
        if self.close_gate is not None:
            self.close_gate.wait(timeout=5)
        if self._close_error is not None:
            raise self._close_error
        self.closed = True


@pytest.fixture
def logs(monkeypatch):
    """Capture IGlobal's warnings and silence its debug output."""
    captured = []
    monkeypatch.setattr(mod, 'warning', lambda message, *a, **kw: captured.append(str(message)), raising=False)
    monkeypatch.setattr(mod, 'debug', lambda *a, **kw: None, raising=False)
    return captured


def _make_global(monkeypatch, cfg, client=None):
    """Return an IGlobal wired to an in-memory config and client, plus the Client(...) kwargs seen."""
    constructed = []
    fake_client = client if client is not None else _FakeClient()

    def _client(**kwargs):
        constructed.append(kwargs)
        return fake_client

    monkeypatch.setattr(mod, 'Client', _client, raising=False)
    config = SimpleNamespace(getNodeConfig=lambda logical_type, conn_config: dict(cfg))
    monkeypatch.setattr(mod, 'Config', config, raising=False)
    glb = mod.IGlobal()
    glb.IEndpoint = SimpleNamespace(endpoint=SimpleNamespace(openMode=object()))
    glb.glb = SimpleNamespace(logicalType='tool_tenki', connConfig={})
    return glb, constructed


def _started(monkeypatch, *sessions, cfg=None, **client_kwargs):
    """Return an IGlobal after beginGlobal, whose client hands out ``sessions`` in order."""
    client = _FakeClient(*sessions, **client_kwargs)
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, **(cfg or {})}, client)
    glb.beginGlobal()
    return glb, client


def _instance(monkeypatch, *sessions, cfg=None, **client_kwargs):
    """Return an IInstance on a real, started IGlobal whose client hands out ``sessions``."""
    glb, client = _started(monkeypatch, *sessions, cfg=cfg, **client_kwargs)
    inst = inst_mod.IInstance()
    inst.IGlobal = glb
    return inst, client


def _execs(session):
    """The (argv, kwargs) of every process started on a fake session."""
    return [(call[1], call[2]) for call in session.calls if isinstance(call, tuple) and call[0] == 'start']


def _git_calls(session, operation):
    """The keyword arguments of every call of one git operation on a fake session."""
    return [call[1] for call in session.calls if isinstance(call, tuple) and call[0] == operation]


def _raise(error):
    raise error


def _run_in_threads(count, target):
    start = threading.Barrier(count)

    def runner():
        start.wait()
        target()

    threads = [threading.Thread(target=runner) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


# ---------------------------------------------------------------------------
# beginGlobal / validateConfig: config handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('cfg', [{}, {'apikey': ''}, {'apikey': '   '}, {'apikey': None}])
def test_missing_apikey_stops_before_a_client_exists(monkeypatch, logs, cfg):
    # Given an empty token the SDK reads TENKI_AUTH_TOKEN / TENKI_API_KEY from the host
    # instead, so without this check a host variable would pick the billed workspace.
    glb, constructed = _make_global(monkeypatch, cfg)
    with pytest.raises(Exception, match='apikey is required'):
        glb.beginGlobal()
    assert constructed == []


def test_client_gets_the_stripped_key_and_the_public_endpoint_explicitly(monkeypatch, logs):
    # An empty endpoint would otherwise be resolved from TENKI_API_ENDPOINT / TENKI_API_URL.
    glb, constructed = _make_global(monkeypatch, {'apikey': f'  {_KEY}  ', 'base_url': ''})
    glb.beginGlobal()
    assert constructed == [{'auth_token': _KEY, 'base_url': 'https://api.tenki.cloud', 'timeout': 120}]


def test_configured_endpoint_is_used_as_given(monkeypatch, logs):
    glb, constructed = _make_global(monkeypatch, {'apikey': _KEY, 'base_url': ' https://sandbox.example.test '})
    glb.beginGlobal()
    assert constructed[0]['base_url'] == 'https://sandbox.example.test'


def test_config_mode_tolerates_a_missing_key(monkeypatch, logs):
    # The editor opens the node before a key is typed; that must not raise.
    glb, constructed = _make_global(monkeypatch, {})
    glb.IEndpoint.endpoint.openMode = mod.OPEN_MODE.CONFIG
    glb.beginGlobal()
    assert constructed == []


@pytest.mark.parametrize(
    ('field', 'configured', 'expected'),
    [
        ('cpu_cores', 0, 1),
        ('cpu_cores', 64, 16),
        ('memory_mb', 128, 512),
        ('memory_mb', 131072, 65536),
        ('memory_mb', 4097, 4096),  # Tenki rejects odd sizes
        ('disk_size_gb', 1, 5),  # the SDK rejects anything under 5 before sending
        ('disk_size_gb', 500, 100),
        ('max_duration_minutes', 0, 1),
        ('max_duration_minutes', 100000, 1440),
        ('exec_timeout_secs', 0, 1),
        ('exec_timeout_secs', 99999, 1200),
        ('max_output_chars', 10, 1000),
        ('max_output_chars', 10**9, 1000000),
    ],
)
def test_integer_config_is_clamped_to_the_accepted_range(monkeypatch, logs, field, configured, expected):
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, field: configured})
    glb.beginGlobal()
    assert getattr(glb, field) == expected


def test_unset_or_unparseable_integers_fall_back_to_defaults(monkeypatch, logs):
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, 'cpu_cores': 'four', 'memory_mb': None})
    glb.beginGlobal()
    settings = (
        glb.cpu_cores,
        glb.memory_mb,
        glb.disk_size_gb,
        glb.max_duration_minutes,
        glb.exec_timeout_secs,
        glb.max_output_chars,
    )
    assert settings == (2, 4096, 5, 15, 120, 50000)


@pytest.mark.parametrize(
    ('cfg', 'warns'),
    [
        ({}, True),
        ({'apikey': 'mock-tenki-placeholder-for-tests'}, True),  # no tk_ prefix: the SDK would reject it
        ({'apikey': _KEY}, False),
    ],
)
def test_validate_config_warns_about_a_missing_or_non_tenki_key(monkeypatch, logs, cfg, warns):
    glb, _ = _make_global(monkeypatch, cfg)
    glb.validateConfig()
    assert bool(logs) is warns


def test_validate_config_reports_a_config_it_cannot_read(monkeypatch, logs):
    glb, _ = _make_global(monkeypatch, {})

    def unreadable(logical_type, conn_config):
        raise KeyError('connConfig')

    monkeypatch.setattr(mod, 'Config', SimpleNamespace(getNodeConfig=unreadable), raising=False)
    glb.validateConfig()
    assert any('connConfig' in message for message in logs)


# ---------------------------------------------------------------------------
# get_session: lazy creation
# ---------------------------------------------------------------------------


def test_session_is_created_on_first_use_and_then_reused(monkeypatch, logs):
    session = _FakeSession('sb-1')
    glb, client = _started(monkeypatch, session)
    assert client.create_calls == []  # beginGlobal never provisions: sessions bill
    assert glb.get_session() is session
    assert glb.get_session() is session
    assert len(client.create_calls) == 1


def test_create_sends_sizing_a_lifetime_cap_in_seconds_and_no_inbound(monkeypatch, logs):
    cfg = {
        'cpu_cores': 4,
        'memory_mb': 8192,
        'disk_size_gb': 20,
        'max_duration_minutes': 90,
    }
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg=cfg)
    glb.get_session()
    [call] = client.create_calls
    for label in ('name', 'tags', 'metadata'):  # covered by the labelling test
        call.pop(label)
    assert [call] == [
        {
            'cpu_cores': 4,
            'memory_mb': 8192,
            'disk_size_gb': 20,
            'max_duration': 5400,
            'allow_inbound': False,
        }
    ]


def test_create_sends_the_default_lifetime_cap_of_fifteen_minutes(monkeypatch, logs):
    # Tenki's idle timeout never paused anything (its changelog, 2026-09-18), and the engine does not
    # run teardown for chat or webhook pipelines (#2300), so this cap is the only guaranteed end for
    # a session an agent stopped using. 15 minutes bounds what an orphaned VM can bill.
    glb, client = _started(monkeypatch, _FakeSession('sb-1'))
    glb.get_session()
    [call] = client.create_calls
    assert call['max_duration'] == 15 * 60


def test_a_pipeline_saved_with_the_retired_idle_timeout_field_still_starts_without_sending_it(monkeypatch, logs):
    # Pipelines saved before the field was retired still carry it. They must keep starting, and the
    # value must not reach Tenki, which ignores it on 1.0.6 and warns on it from 1.4.0.
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg={'idle_timeout_minutes': 10})
    glb.get_session()
    [call] = client.create_calls
    assert 'idle_timeout_minutes' not in call
    assert not hasattr(glb, 'idle_timeout_minutes')


def test_image_is_sent_when_configured(monkeypatch, logs):
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg={'image': ' myworkspace/node-env '})
    glb.get_session()
    assert client.create_calls[0]['image'] == 'myworkspace/node-env'


def test_parallel_first_calls_create_exactly_one_session(monkeypatch, logs):
    # deepagent fans tool calls out with asyncio.gather; unsynchronized, each call would
    # see no session and create one, and all but one would be orphaned while billing.
    glb, client = _started(monkeypatch, *[_FakeSession(f'sb-{i}') for i in range(8)], create_delay=0.05)
    sessions = []
    _run_in_threads(8, lambda: sessions.append(glb.get_session()))
    assert len(client.create_calls) == 1
    assert [session.id for session in sessions] == ['sb-0'] * 8


# ---------------------------------------------------------------------------
# Failed create: a readiness failure can leave a billing sandbox behind
# ---------------------------------------------------------------------------


def test_readiness_timeout_closes_the_sandbox_it_carries_and_reraises(monkeypatch, logs):
    left_running = _FakeSession('sb-orphan')
    error = mod.WaitReadyFailedError('sandbox sb-orphan created but not ready within wait budget', left_running)
    glb, _ = _started(monkeypatch, create_error=error)
    with pytest.raises(mod.WaitReadyFailedError):
        glb.get_session()
    assert left_running.calls == ['close']
    assert glb.session is None


def test_template_failure_raised_by_create_closes_the_session_by_id(monkeypatch, logs):
    # Raised by the create call itself, this error carries the session's info record,
    # which has no close(), so the live handle has to be fetched first.
    info = SimpleNamespace(id='sb-orphan', state='RUNNING')
    error = mod.TemplateRuntimeFailedError('template runtime failed', info)
    glb, client = _started(monkeypatch, create_error=error)
    with pytest.raises(mod.TemplateRuntimeFailedError):
        glb.get_session()
    assert client.fetched['sb-orphan'].calls == ['close']


@pytest.mark.parametrize('carried', [None, SimpleNamespace(id='sb-orphan', state='TERMINATED')])
def test_template_failure_with_nothing_left_running_closes_nothing(monkeypatch, logs, carried):
    error = mod.TemplateRuntimeFailedError('template runtime failed', carried)
    glb, client = _started(monkeypatch, create_error=error)
    with pytest.raises(mod.TemplateRuntimeFailedError):
        glb.get_session()
    assert client.fetched == {}


def test_failed_cleanup_is_logged_and_the_create_error_still_surfaces(monkeypatch, logs):
    unclosable = _FakeSession('sb-orphan', close_error=RuntimeError('connection reset'))
    error = mod.WaitReadyFailedError('not ready within wait budget', unclosable)
    glb, _ = _started(monkeypatch, create_error=error)
    with pytest.raises(mod.WaitReadyFailedError) as raised:
        glb.get_session()
    assert raised.value is error
    assert any('connection reset' in message for message in logs)


# ---------------------------------------------------------------------------
# call_with_session: recovery follows the session's real state
# ---------------------------------------------------------------------------


def test_terminated_session_is_replaced_and_the_call_retried(monkeypatch, logs):
    dead = _FakeSession('sb-dead', state='TERMINATED')
    fresh = _FakeSession('sb-fresh')
    glb, client = _started(monkeypatch, dead, fresh)
    ran_on = []

    def call(session):
        ran_on.append(session.id)
        if session is dead:
            raise mod.SessionTerminatedError('session_terminated:guest_agent_liveness')
        return 'done'

    assert glb.call_with_session(call) == 'done'
    assert ran_on == ['sb-dead', 'sb-fresh']
    assert len(client.create_calls) == 2
    assert glb.get_session() is fresh


def test_session_the_service_no_longer_knows_is_replaced(monkeypatch, logs):
    gone = _FakeSession('sb-gone', refresh_error=mod.SessionNotFoundError('session not found'))
    fresh = _FakeSession('sb-fresh')
    glb, client = _started(monkeypatch, gone, fresh)
    result = glb.call_with_session(
        lambda session: _raise(mod.SessionNotFoundError('session not found')) if session is gone else session.id
    )
    assert result == 'sb-fresh'
    assert len(client.create_calls) == 2


def test_an_ended_session_whose_close_fails_is_still_replaced(monkeypatch, logs):
    # A failing close is expected when the session really is gone, so it must not stop the replacement.
    ended = _FakeSession('sb-1', state='USER_SHUTDOWN', close_error=mod.SessionNotFoundError('not found'))
    glb, client = _started(monkeypatch, ended, _FakeSession('sb-fresh'))
    reported = []
    outcome = glb.call_with_session(
        lambda session: _raise(mod.SessionTerminatedError('session_terminated')) if session is ended else 'ok',
        on_recovery=reported.append,
    )
    assert outcome == 'ok'
    assert reported == ['replaced']
    assert len(client.create_calls) == 2


def test_session_shut_down_from_inside_is_closed_before_it_is_replaced(monkeypatch, logs):
    # USER_SHUTDOWN is outside the SDK's closed states, so it is terminated explicitly
    # rather than assumed gone and left holding resources.
    off = _FakeSession('sb-off', state='USER_SHUTDOWN')
    fresh = _FakeSession('sb-fresh')
    glb, _ = _started(monkeypatch, off, fresh)
    result = glb.call_with_session(
        lambda session: _raise(mod.InvalidStateError('session is not running')) if session is off else session.id
    )
    assert result == 'sb-fresh'
    assert 'close' in off.calls


def test_paused_session_is_resumed_and_never_replaced(monkeypatch, logs):
    # A pause keeps the agent's memory and files; recreating would silently destroy its work.
    paused = _FakeSession('sb-1', state='PAUSED')
    glb, client = _started(monkeypatch, paused)
    seen_states = []

    def call(session):
        seen_states.append(session.state)
        if session.state == 'PAUSED':
            raise mod.InvalidStateError('session is paused')
        return 'done'

    assert glb.call_with_session(call) == 'done'
    assert seen_states == ['PAUSED', 'RUNNING']
    assert paused.calls.count('resume') == 1
    assert paused.calls.index('resume') < paused.calls.index('wait_ready')
    assert len(client.create_calls) == 1


@pytest.mark.parametrize(
    'make_error',
    [
        lambda: mod.InvalidStateError('git checkout failed: pathspec did not match'),
        lambda: mod.SessionNotFoundError('no such path'),
    ],
)
def test_lifecycle_error_from_a_running_session_surfaces_untouched(monkeypatch, logs, make_error):
    # The SDK maps every NOT_FOUND it cannot attribute to SessionNotFoundError and every
    # unrecognised FAILED_PRECONDITION to InvalidStateError, so a healthy session raises
    # both. Replacing it would orphan a billing session and lose the agent's work.
    running = _FakeSession('sb-1')
    glb, client = _started(monkeypatch, running, _FakeSession('sb-2'))
    error = make_error()
    with pytest.raises(type(error)) as raised:
        glb.call_with_session(lambda session: _raise(error))
    assert raised.value is error
    assert 'resume' not in running.calls
    assert 'close' not in running.calls
    assert len(client.create_calls) == 1
    assert glb.get_session() is running


def test_other_errors_propagate_without_a_state_check(monkeypatch, logs):
    running = _FakeSession('sb-1')
    glb, _ = _started(monkeypatch, running)
    with pytest.raises(ValueError):
        glb.call_with_session(lambda session: _raise(ValueError('empty command')))
    assert running.calls == []


def test_recovery_happens_once_then_the_error_surfaces(monkeypatch, logs):
    paused = _FakeSession('sb-1', state='PAUSED')
    glb, _ = _started(monkeypatch, paused)
    with pytest.raises(mod.InvalidStateError):
        glb.call_with_session(lambda session: _raise(mod.InvalidStateError('session is paused')))
    assert paused.calls.count('resume') == 1


def test_parallel_calls_on_a_paused_session_resume_it_once_and_all_succeed(monkeypatch, logs):
    # Calls that failed against the paused session must retry once it is resumed, not
    # re-check the now-running session and report their own failure as unrelated.
    paused = _FakeSession('sb-1', state='PAUSED', resume_delay=0.05)
    glb, client = _started(monkeypatch, paused)
    glb.get_session()
    results, errors = [], []

    def call(session):
        if session.state == 'PAUSED':
            raise mod.InvalidStateError('session is paused')
        return 'done'

    def tool_call():
        try:
            results.append(glb.call_with_session(call))
        except Exception as e:  # collected, so a failure in a thread is reported
            errors.append(e)

    _run_in_threads(6, tool_call)
    assert errors == []
    assert results == ['done'] * 6
    assert paused.calls.count('resume') == 1
    assert len(client.create_calls) == 1


# ---------------------------------------------------------------------------
# endGlobal: nothing else stops a session that a pause would otherwise keep
# ---------------------------------------------------------------------------


def test_end_global_closes_the_session_and_the_client(monkeypatch, logs):
    session = _FakeSession('sb-1')
    glb, client = _started(monkeypatch, session)
    glb.get_session()
    glb.endGlobal()
    assert session.calls == ['close']
    assert client.closed is True
    assert glb.session is None


def test_end_global_without_a_session_closes_only_the_client(monkeypatch, logs):
    glb, client = _started(monkeypatch)
    glb.endGlobal()
    assert client.create_calls == []
    assert client.closed is True


def test_end_global_logs_a_failed_close_and_still_forgets_the_session(monkeypatch, logs):
    session = _FakeSession('sb-1', close_error=RuntimeError('connection reset'))
    glb, client = _started(monkeypatch, session)
    glb.get_session()
    glb.endGlobal()
    assert glb.session is None
    assert client.closed is True
    assert any('connection reset' in message for message in logs)


def test_end_global_waits_for_an_in_flight_create_and_closes_that_session(monkeypatch, logs):
    session = _FakeSession('sb-1')
    glb, client = _started(monkeypatch, session, create_delay=0.1)
    creator = threading.Thread(target=glb.get_session)
    creator.start()
    assert client.create_started.wait(timeout=5)
    glb.endGlobal()
    creator.join()
    assert session.calls == ['close']


# ---------------------------------------------------------------------------
# tool_groups: which tools an agent is offered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('raw', [None, [], '', ' , ', ()])
def test_unset_tool_groups_mean_execution_filesystem_and_git(raw):
    assert groups.normalize_groups(raw) == frozenset({'execution', 'filesystem', 'git'})


@pytest.mark.parametrize('raw', ['all', ['ALL'], ['*'], 'git, all'])
def test_all_publishes_every_group(raw):
    assert groups.normalize_groups(raw) == frozenset({'execution', 'filesystem', 'git'})


@pytest.mark.parametrize(('raw', 'expected'), [(['Git'], {'git'}), ('git, filesystem', {'git', 'filesystem'})])
def test_named_groups_replace_the_defaults(raw, expected):
    assert groups.normalize_groups(raw) == frozenset(expected)


def test_partly_unknown_groups_narrow_to_the_names_that_matched():
    assert groups.normalize_groups(['git', 'gti']) == frozenset({'git'})
    assert groups.unknown_groups(['git', 'gti']) == ['gti']


@pytest.mark.parametrize('raw', [['gti'], 'shell, sockets'])
def test_groups_that_match_nothing_raise_instead_of_widening_to_the_defaults(raw):
    with pytest.raises(ValueError):
        groups.normalize_groups(raw)


def test_tool_decorator_rejects_an_unknown_group():
    with pytest.raises(ValueError):
        groups.tenki_tool(group='shell')


def test_editor_offers_exactly_the_implemented_groups():
    # services.json lists the groups for the editor; a group added on only one side is
    # either impossible to select or rejected at startup.
    services = json.loads((_NODES_SRC / 'nodes' / 'tool_tenki' / 'services.json').read_text())
    offered = {value for value, _label in services['fields']['tenki.toolGroups']['items']['enum']}
    assert offered == groups.ALL_GROUPS | {'all'}


def test_tool_groups_that_match_nothing_stop_startup_before_a_client_exists(monkeypatch, logs):
    glb, constructed = _make_global(monkeypatch, {'apikey': _KEY, 'toolGroups': ['gti']})
    with pytest.raises(ValueError):
        glb.beginGlobal()
    assert constructed == []


def test_partly_unknown_tool_groups_are_logged_and_narrowed(monkeypatch, logs):
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, 'toolGroups': ['git', 'gti']})
    glb.beginGlobal()
    assert glb.tool_groups == frozenset({'git'})
    assert any('gti' in message for message in logs)


def test_validate_config_warns_about_unknown_tool_groups(monkeypatch, logs):
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, 'toolGroups': ['gti']})
    glb.validateConfig()
    assert any('gti' in message for message in logs)


_EXECUTION_TOOLS = {'run_command', 'run_code'}
_FILESYSTEM_TOOLS = {'write_file', 'read_file', 'list_files', 'make_directory', 'delete_path'}
_GIT_TOOLS = {'git_clone', 'git_checkout', 'git_diff', 'git_log'}


def test_default_groups_publish_the_execution_filesystem_and_git_tools(monkeypatch, logs):
    inst, _ = _instance(monkeypatch)
    assert set(inst._collect_tool_methods()) == _EXECUTION_TOOLS | _FILESYSTEM_TOOLS | _GIT_TOOLS


@pytest.mark.parametrize(
    ('group', 'tools'),
    [('execution', _EXECUTION_TOOLS), ('filesystem', _FILESYSTEM_TOOLS), ('git', _GIT_TOOLS)],
)
def test_each_group_publishes_exactly_its_own_tools(monkeypatch, logs, group, tools):
    # tool.invoke looks tool names up in the same collection, so this also refuses the others.
    inst, _ = _instance(monkeypatch, cfg={'toolGroups': [group]})
    assert set(inst._collect_tool_methods()) == tools


def test_every_tool_is_tagged_with_a_known_group():
    # An untagged tool is never published, so a missing tag would hide the tool silently.
    tagged = {}
    for name in dir(inst_mod.IInstance):
        attr = getattr(inst_mod.IInstance, name)
        if hasattr(attr, '__tool_meta__'):
            tagged[name] = getattr(attr, '__tenki_group__', None)
    assert tagged
    assert all(group in groups.ALL_GROUPS for group in tagged.values()), tagged


def test_tool_descriptions_resolve_and_state_the_configured_limits(monkeypatch, logs):
    # Descriptions are evaluated at tool.query time; one that raises breaks the whole catalog.
    inst, _ = _instance(monkeypatch, cfg={'toolGroups': ['all'], 'exec_timeout_secs': 45})
    published = inst._collect_tool_methods()
    assert published
    for name, method in published.items():
        description = method.__tool_meta__['description']
        text = description(inst) if callable(description) else description
        assert isinstance(text, str) and text, name
        if name in _EXECUTION_TOOLS:
            assert '45s' in text, name


def test_tool_descriptions_say_a_session_past_its_lifetime_is_paused_not_replaced(monkeypatch, logs):
    # Tenki pauses a session at its max duration and keeps its state (changelog of 2026-09-11; measured
    # on 2026-10-05), and the node resumes it. An agent told it had been replaced would redo work that
    # survived, so every description that mentions the lifetime has to say what really happens.
    inst, _ = _instance(monkeypatch, cfg={'toolGroups': ['all']})
    mentions = 0
    for name, method in inst._collect_tool_methods().items():
        description = method.__tool_meta__['description']
        text = description(inst) if callable(description) else description
        for sentence in text.split('. '):
            if 'maximum lifetime' in sentence:
                mentions += 1
                assert 'paus' in sentence and 'resum' in sentence, (name, sentence)
                assert 'replaced' not in sentence and 'fresh' not in sentence, (name, sentence)
    assert mentions, 'no tool description says what happens at the maximum lifetime'


def test_the_max_duration_help_says_tenki_pauses_the_session():
    # The editor's help for the one cost setting: it must not promise that the session ends.
    services = json.loads((_NODES_SRC / 'nodes' / 'tool_tenki' / 'services.json').read_text())
    description = services['fields']['tenki.max_duration_minutes']['description']
    assert 'pauses' in description
    assert 'once a session ends' not in description


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'args', [{}, {'command': ''}, {'command': '   '}, {'command': 42}, {'command': 'ls', 'cwd': 42}]
)
def test_run_command_rejects_invalid_input_without_touching_the_sandbox(monkeypatch, logs, args):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.run_command(args)
    assert client.create_calls == []


def test_run_command_runs_bash_as_a_login_shell_with_the_configured_timeout(monkeypatch, logs):
    # The timeout is always passed: left out, Tenki applies its own 30-second default.
    session = _FakeSession('sb-1', exec_result=_CommandResult(argv=['bash'], exit_code=0, stdout=b'total 0\n'))
    inst, _ = _instance(monkeypatch, session, cfg={'exec_timeout_secs': 45})
    assert inst.run_command({'command': 'ls -la'}) == {
        'exit_code': 0,
        'stdout': 'total 0\n',
        'stderr': '',
        'timed_out': False,
        'truncated': False,
    }
    assert _execs(session) == [
        (
            ('bash', '-lc', 'ls -la'),
            {'cwd': None, 'env': None, 'timeout': 45, 'stdin': None, 'privileged': False},
        )
    ]


@pytest.mark.parametrize(
    ('cwd', 'script'),
    [
        ('app', 'cd /home/tenki/app && make test'),
        ('my project', "cd '/home/tenki/my project' && make test"),
        ('   ', 'make test'),
    ],
)
def test_run_command_changes_directory_inside_the_shell(monkeypatch, logs, cwd, script):
    # A login shell runs the guest's startup files before the command, and a cd in them would
    # win over exec's cwd, so the directory change is part of the script itself.
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    inst.run_command({'command': 'make test', 'cwd': cwd})
    [(argv, kwargs)] = _execs(session)
    assert argv == ('bash', '-lc', script)
    assert kwargs['cwd'] is None


@pytest.mark.parametrize('cwd', ['../../tmp', '/tmp', '..', '/home/tenki/../etc'])
def test_run_command_refuses_a_working_directory_outside_the_home(monkeypatch, logs, cwd):
    # cwd is the one path argument that used to skip _normalize_path, so '../../tmp' resolved
    # outside the documented boundary. run_command can still reach the whole VM through the
    # command itself; this keeps cwd's contract the same as every other path argument.
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    with pytest.raises(ValueError, match='/home/tenki'):
        inst.run_command({'command': 'make test', 'cwd': cwd})
    assert _execs(session) == []


def test_run_command_reports_a_command_stopped_at_the_timeout(monkeypatch, logs):
    # Tenki stops the command and reports it on the result, possibly with exit code 0.
    session = _FakeSession('sb-1', exec_result=_CommandResult(argv=['bash'], exit_code=0, timed_out=True))
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'sleep 999'})
    assert result['timed_out'] is True
    assert 'error' not in result


def test_run_command_reports_a_deadline_error_as_timed_out(monkeypatch, logs):
    session = _FakeSession('sb-1', exec_error=inst_mod.CommandTimeoutError('deadline exceeded'))
    inst, _ = _instance(monkeypatch, session)
    assert inst.run_command({'command': 'sleep 999'}) == {
        'error': 'deadline exceeded',
        'exit_code': -1,
        'stdout': '',
        'stderr': '',
        'timed_out': True,
        'truncated': False,
    }


def test_run_command_returns_sandbox_errors_to_the_agent(monkeypatch, logs):
    session = _FakeSession('sb-1', exec_error=inst_mod.SandboxError('workspace quota exceeded'))
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'ls'})
    assert result['error'] == 'workspace quota exceeded'
    assert result['exit_code'] == -1
    assert result['timed_out'] is False


@pytest.mark.parametrize(
    ('stdout_len', 'stderr_len', 'kept'),
    [
        (600, 400, (600, 400)),  # within the cap: untouched
        (1500, 0, (1000, 0)),  # one stream over: cut to the cap
        (5000, 10, (990, 10)),  # stdout floods: stderr, where errors land, keeps its text
        (4, 5000, (4, 996)),  # stderr floods: stdout keeps its text
        (5000, 5000, (500, 500)),  # both flood: an even split
    ],
)
def test_output_shares_the_cap_across_both_streams(stdout_len, stderr_len, kept):
    result = _CommandResult(argv=['bash'], exit_code=0, stdout=b'o' * stdout_len, stderr=b'e' * stderr_len)
    shaped = inst_mod._exec_result(result, 1000)
    assert (len(shaped['stdout']), len(shaped['stderr'])) == kept
    assert shaped['truncated'] is (kept != (stdout_len, stderr_len))


# ---------------------------------------------------------------------------
# run_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'args', [{}, {'code': ''}, {'code': '  \n '}, {'code': 7}, {'code': 'print(1)', 'language': 'ruby'}]
)
def test_run_code_rejects_invalid_input_without_touching_the_sandbox(monkeypatch, logs, args):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.run_code(args)
    assert client.create_calls == []


@pytest.mark.parametrize(
    ('language', 'interpreter', 'extension'),
    [
        (None, 'python3', '.py'),
        ('python', 'python3', '.py'),
        ('javascript', 'node', '.js'),
    ],
)
def test_run_code_writes_the_code_to_a_file_then_runs_the_interpreter_on_it(
    monkeypatch, logs, language, interpreter, extension
):
    # Multi-line code with quotes and a heredoc marker reaches the file unchanged, because it
    # goes through the file API rather than through shell escaping.
    code = 'print("it\'s")\nprint("""EOF""")\n'
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session, cfg={'exec_timeout_secs': 45})
    args = {'code': code} if language is None else {'code': code, 'language': language}
    assert inst.run_code(args)['exit_code'] == 0
    written, executed, removed = [call for call in session.calls if call[0] in ('write_text', 'start', 'remove')]
    path = written[1]
    assert written == ('write_text', path)
    assert path.startswith('/home/tenki/') and path.endswith(extension)
    assert session.fs.written[path] == code
    assert executed[0] == 'start'
    assert executed[1][-2:] == (interpreter, path)
    assert executed[2]['timeout'] == 45
    assert removed[:2] == ('remove', path)


def test_typescript_is_not_offered_because_the_base_image_lacks_ts_node(monkeypatch, logs):
    # Checked live against Tenki's image: ts-node is not installed there, and run_code on a .ts file
    # came back with exit 0 and empty output - a silent failure an agent would read as success.
    meta = inst_mod.IInstance.run_code.__tool_meta__
    assert meta['input_schema']['properties']['language']['enum'] == ['javascript', 'python']
    description = meta['description'](SimpleNamespace(IGlobal=SimpleNamespace(exec_timeout_secs=120)))
    assert 'ts-node' not in description and 'typescript' not in description.lower()
    inst, _ = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.run_code({'code': 'const x: number = 1', 'language': 'typescript'})


def test_run_code_uses_a_new_file_for_every_call(monkeypatch, logs):
    # Parallel calls share one session, so a fixed name would let one call run another's code.
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    inst.run_code({'code': 'print(1)'})
    inst.run_code({'code': 'print(2)'})
    paths = [call[1] for call in session.calls if call[0] == 'write_text']
    assert len(set(paths)) == 2


def test_run_code_returns_the_result_even_if_cleanup_fails(monkeypatch, logs):
    session = _FakeSession(
        'sb-1',
        exec_result=_CommandResult(argv=['bash'], exit_code=0, stdout=b'42\n'),
        fail={'remove': inst_mod.SandboxError('remove failed')},
    )
    inst, _ = _instance(monkeypatch, session)
    assert inst.run_code({'code': 'print(42)'})['stdout'] == '42\n'


def test_run_code_on_a_paused_session_resumes_it_and_runs_the_code(monkeypatch, logs):
    session = _FakeSession(
        'sb-1', state='PAUSED', exec_result=_CommandResult(argv=['bash'], exit_code=0, stdout=b'ok\n')
    )
    inst, client = _instance(monkeypatch, session)
    assert inst.run_code({'code': 'print("ok")'})['stdout'] == 'ok\n'
    assert session.calls.count('resume') == 1
    assert len(client.create_calls) == 1


# ---------------------------------------------------------------------------
# Paths: every file tool is confined to /home/tenki
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('given', 'resolved'),
    [
        ('notes.txt', '/home/tenki/notes.txt'),
        ('  app/main.py  ', '/home/tenki/app/main.py'),
        ('.', '/home/tenki'),
        ('~', '/home/tenki'),
        ('~/app', '/home/tenki/app'),
        ('/home/tenki/app/../lib', '/home/tenki/lib'),
        ('//home//tenki///app', '/home/tenki/app'),
    ],
)
def test_paths_resolve_under_home(given, resolved):
    assert inst_mod._normalize_path(given) == resolved


@pytest.mark.parametrize(
    'given',
    ['/tmp', '/tmp/build.log', '..', '../etc/passwd', 'app/../../x', '/home/tenki2/x', '/home/tenki/../tenki2', '/etc'],
)
def test_paths_outside_home_are_rejected_with_a_message_naming_home(given):
    # Tenki's file API refuses these with a bare permission error, and agents reach for /tmp first.
    with pytest.raises(ValueError, match='/home/tenki'):
        inst_mod._normalize_path(given)


@pytest.mark.parametrize('given', [None, '', '   ', 42])
def test_a_missing_path_is_rejected(given):
    with pytest.raises(ValueError):
        inst_mod._normalize_path(given)


# ---------------------------------------------------------------------------
# write_file / make_directory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'args',
    [
        {},
        {'path': 'a.txt'},
        {'path': '', 'content': 'x'},
        {'path': 'a.txt', 'content': 7},
        {'path': '/tmp/a.txt', 'content': 'x'},
    ],
)
def test_write_file_rejects_invalid_input_without_touching_the_sandbox(monkeypatch, logs, args):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.write_file(args)
    assert client.create_calls == []


@pytest.mark.parametrize('content', ['line one\n  "quoted" \\n and a tab\t\n', ''])
def test_write_file_writes_the_content_verbatim(monkeypatch, logs, content):
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    assert inst.write_file({'path': 'notes.md', 'content': content}) == {
        'success': True,
        'path': '/home/tenki/notes.md',
    }
    assert session.fs.files['/home/tenki/notes.md'] == content


def test_write_file_creates_missing_parent_directories(monkeypatch, logs):
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    assert inst.write_file({'path': 'src/app/main.py', 'content': 'print(1)'})['success'] is True
    assert session.fs.files['/home/tenki/src/app/main.py'] == 'print(1)'


def test_write_file_explains_a_refused_path(monkeypatch, logs):
    # The lexical check cannot see everything the service refuses, such as a symlink out of home.
    refused = inst_mod.PermissionDeniedError('permission denied')
    session = _FakeSession('sb-1', fail={'write_text': refused})
    inst, _ = _instance(monkeypatch, session)
    result = inst.write_file({'path': 'escape/out.txt', 'content': 'x'})
    assert result['success'] is False
    assert '/home/tenki' in result['error']


@pytest.mark.parametrize('args', [{}, {'path': ''}, {'path': '/tmp/x'}])
def test_make_directory_rejects_invalid_input_without_touching_the_sandbox(monkeypatch, logs, args):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.make_directory(args)
    assert client.create_calls == []


def test_make_directory_creates_the_directory_and_its_parents(monkeypatch, logs):
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    assert inst.make_directory({'path': 'data/raw'}) == {'success': True, 'path': '/home/tenki/data/raw'}
    assert {'/home/tenki/data', '/home/tenki/data/raw'} <= session.fs.dirs


def test_make_directory_reports_a_failure_to_the_agent(monkeypatch, logs):
    session = _FakeSession('sb-1', fail={'mkdir': inst_mod.PermissionDeniedError('read-only file system')})
    inst, _ = _instance(monkeypatch, session)
    result = inst.make_directory({'path': 'data'})
    assert result['success'] is False
    assert 'read-only file system' in result['error']


@pytest.mark.parametrize(
    ('tool', 'args'), [('write_file', {'path': 'a.txt', 'content': 'x'}), ('make_directory', {'path': 'data'})]
)
def test_writing_to_an_ended_session_continues_on_a_fresh_one(monkeypatch, logs, tool, args):
    ended = mod.SessionTerminatedError('session_terminated')
    dead = _FakeSession('sb-dead', state='TERMINATED', fail={'write_text': ended, 'mkdir': ended})
    fresh = _FakeSession('sb-fresh')
    inst, client = _instance(monkeypatch, dead, fresh)
    assert getattr(inst, tool)(args)['success'] is True
    assert len(client.create_calls) == 2


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('args', [{}, {'path': ''}, {'path': 5}, {'path': '../etc/passwd'}])
def test_read_file_rejects_invalid_input_without_touching_the_sandbox(monkeypatch, logs, args):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.read_file(args)
    assert client.create_calls == []


def test_read_file_returns_the_file_text(monkeypatch, logs):
    session = _FakeSession('sb-1', files={'/home/tenki/notes.md': 'h\u00e9llo\n'})
    inst, _ = _instance(monkeypatch, session)
    assert inst.read_file({'path': 'notes.md'}) == {
        'path': '/home/tenki/notes.md',
        'content': 'h\u00e9llo\n',
        'truncated': False,
    }


def test_read_file_checks_the_size_then_reads_through_calls_that_carry_the_client_deadline(monkeypatch, logs):
    # The SDK's streaming read has no deadline, so a half-open connection would hang it for good.
    # stat and read_bytes are unary calls, bounded by the deadline IGlobal gives the client.
    session = _FakeSession('sb-1', files={'/home/tenki/notes.md': 'x'})
    inst, _ = _instance(monkeypatch, session)
    inst.read_file({'path': 'notes.md'})
    assert [call for call in session.calls if call[0] in ('stat', 'read_bytes')] == [
        ('stat', '/home/tenki/notes.md'),
        ('read_bytes', '/home/tenki/notes.md'),
    ]


def test_read_file_refuses_a_file_over_the_read_limit_without_fetching_it(monkeypatch, logs):
    # read_bytes returns the whole file, so a huge log must be refused before it is pulled into the
    # engine's memory, not cut down afterwards.
    monkeypatch.setattr(inst_mod, '_READ_LIMIT_BYTES', 4096)
    session = _FakeSession('sb-1', files={'/home/tenki/huge.log': 'x' * 4097})
    inst, _ = _instance(monkeypatch, session)
    result = inst.read_file({'path': 'huge.log'})
    assert result['content'] == ''
    assert result['truncated'] is False
    assert '4,097 bytes' in result['error']
    assert 'run_command' in result['error']
    assert not any(call[0] == 'read_bytes' for call in session.calls)


def test_read_file_reads_a_file_exactly_at_the_read_limit(monkeypatch, logs):
    monkeypatch.setattr(inst_mod, '_READ_LIMIT_BYTES', 4096)
    session = _FakeSession('sb-1', files={'/home/tenki/edge.txt': 'x' * 4096})
    inst, _ = _instance(monkeypatch, session)
    assert inst.read_file({'path': 'edge.txt'})['content'] == 'x' * 4096


def test_read_file_on_a_directory_says_to_list_it(monkeypatch, logs):
    session = _FakeSession('sb-1', dirs={'/home/tenki/src'})
    inst, _ = _instance(monkeypatch, session)
    result = inst.read_file({'path': 'src'})
    assert result['content'] == ''
    assert 'is a directory' in result['error']
    assert 'list_files' in result['error']
    assert not any(call[0] == 'read_bytes' for call in session.calls)


@pytest.mark.parametrize(
    ('text', 'truncated'),
    [
        ('\u00e9' * 1000, False),  # two bytes a character, exactly at the cap
        ('\u00e9' * 1001, True),
        ('\U0001f600' * 1001, True),  # four bytes a character, just past the byte bound
    ],
)
def test_read_file_truncates_by_characters_not_bytes(monkeypatch, logs, text, truncated):
    session = _FakeSession('sb-1', files={'/home/tenki/t.txt': text})
    inst, _ = _instance(monkeypatch, session, cfg={'max_output_chars': 1000})
    result = inst.read_file({'path': 't.txt'})
    assert result['content'] == text[:1000]
    assert result['truncated'] is truncated


def test_read_file_reports_a_missing_file(monkeypatch, logs):
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    result = inst.read_file({'path': 'missing.txt'})
    assert result['content'] == ''
    assert '/home/tenki/missing.txt' in result['error']


def test_read_file_on_a_paused_session_resumes_it_and_reads(monkeypatch, logs):
    session = _FakeSession('sb-1', state='PAUSED', files={'/home/tenki/notes.md': 'kept across the pause'})
    inst, client = _instance(monkeypatch, session)
    assert inst.read_file({'path': 'notes.md'})['content'] == 'kept across the pause'
    assert session.calls.count('resume') == 1
    assert len(client.create_calls) == 1


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('args', [{'path': '/tmp'}, {'path': 3}, {'include_hidden': 'yes'}])
def test_list_files_rejects_invalid_input_without_touching_the_sandbox(monkeypatch, logs, args):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.list_files(args)
    assert client.create_calls == []


def test_list_files_lists_home_by_name_without_hidden_entries(monkeypatch, logs):
    session = _FakeSession(
        'sb-1',
        files={
            '/home/tenki/zeta.txt': 'zz',
            '/home/tenki/.bashrc': 'export A=1',
            '/home/tenki/app/main.py': 'print(1)',
        },
        dirs={'/home/tenki/app'},
    )
    inst, _ = _instance(monkeypatch, session)
    assert inst.list_files({}) == {
        'path': '/home/tenki',
        'entries': [{'name': 'app', 'is_dir': True, 'size': 0}, {'name': 'zeta.txt', 'is_dir': False, 'size': 2}],
        'truncated': False,
    }


def test_list_files_includes_hidden_entries_when_asked(monkeypatch, logs):
    session = _FakeSession('sb-1', files={'/home/tenki/.gitignore': 'node_modules\n'})
    inst, _ = _instance(monkeypatch, session)
    result = inst.list_files({'path': '~', 'include_hidden': True})
    assert [entry['name'] for entry in result['entries']] == ['.gitignore']


def test_list_files_cuts_a_huge_listing_to_the_cap(monkeypatch, logs):
    session = _FakeSession('sb-1', files={f'/home/tenki/file-{i:05d}.txt': '' for i in reversed(range(5000))})
    inst, _ = _instance(monkeypatch, session, cfg={'max_output_chars': 1000})
    result = inst.list_files({})
    names = [entry['name'] for entry in result['entries']]
    assert result['truncated'] is True
    assert 0 < len(names) < 5000
    assert names == [f'file-{i:05d}.txt' for i in range(len(names))]
    assert len(json.dumps(result['entries'])) <= 1000


def test_list_files_reports_a_missing_directory(monkeypatch, logs):
    inst, _ = _instance(monkeypatch, _FakeSession('sb-1'))
    result = inst.list_files({'path': 'nope'})
    assert result['entries'] == []
    assert '/home/tenki/nope' in result['error']


# ---------------------------------------------------------------------------
# delete_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'args',
    [{}, {'path': ''}, {'path': '/tmp/x'}, {'path': '.'}, {'path': '~'}, {'path': '/home/tenki'}, {'path': 'app/..'}],
)
def test_delete_path_refuses_home_itself_and_invalid_paths_without_touching_the_sandbox(monkeypatch, logs, args):
    # Deleting /home/tenki would take the agent's whole workspace with it.
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.delete_path(args)
    assert client.create_calls == []


def test_delete_path_removes_a_directory_with_everything_in_it(monkeypatch, logs):
    session = _FakeSession(
        'sb-1',
        files={'/home/tenki/build/out/app.bin': 'x', '/home/tenki/keep.txt': 'y'},
        dirs={'/home/tenki/build', '/home/tenki/build/out'},
    )
    inst, _ = _instance(monkeypatch, session)
    assert inst.delete_path({'path': 'build'}) == {'success': True, 'path': '/home/tenki/build'}
    assert session.fs.files == {'/home/tenki/keep.txt': 'y'}
    assert session.fs.dirs == {'/home/tenki'}


def test_delete_path_reports_a_missing_path(monkeypatch, logs):
    inst, _ = _instance(monkeypatch, _FakeSession('sb-1'))
    result = inst.delete_path({'path': 'gone.txt'})
    assert result['success'] is False
    assert '/home/tenki/gone.txt' in result['error']


# ---------------------------------------------------------------------------
# A session that ended cannot answer a read, a listing or a delete
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('tool', 'args'), [('read_file', {'path': 'a.txt'}), ('list_files', {}), ('delete_path', {'path': 'a.txt'})]
)
def test_looking_at_an_ended_session_does_not_start_a_new_one(monkeypatch, logs, tool, args):
    # A fresh, empty session cannot contain what these look for, so creating one only adds cost.
    ended = mod.SessionTerminatedError('session_terminated')
    dead = _FakeSession('sb-dead', state='TERMINATED', fail={'stat': ended, 'list': ended, 'remove': ended})
    inst, client = _instance(monkeypatch, dead, _FakeSession('sb-fresh'))
    result = getattr(inst, tool)(args)
    assert result['error']
    assert len(client.create_calls) == 1
    # The ended session was forgotten, so the next call that can use a fresh session gets one.
    assert inst.write_file({'path': 'b.txt', 'content': 'x'})['success'] is True
    assert len(client.create_calls) == 2


def test_a_call_that_cannot_use_a_fresh_session_is_not_given_one(monkeypatch, logs):
    dead = _FakeSession('sb-dead', state='TERMINATED')
    glb, client = _started(monkeypatch, dead, _FakeSession('sb-fresh'))
    with pytest.raises(mod.SessionEndedError):
        glb.call_with_session(lambda session: _raise(mod.SessionTerminatedError('session_terminated')), replace=False)
    assert len(client.create_calls) == 1
    assert glb.session is None


def test_a_concurrently_dropped_session_is_not_replaced_for_a_call_that_cannot_use_it(monkeypatch, logs):
    dead = _FakeSession('sb-dead', state='TERMINATED')
    glb, client = _started(monkeypatch, dead, _FakeSession('sb-fresh'))

    def call(session):
        # Another read found the session ended and dropped it while this call was failing.
        with glb._session_lock:
            glb.session = None
            glb.session_epoch += 1
        raise mod.SessionTerminatedError('session_terminated')

    with pytest.raises(mod.SessionEndedError):
        glb.call_with_session(call, replace=False)
    assert len(client.create_calls) == 1


def test_a_call_that_cannot_use_a_fresh_session_is_refused_after_a_concurrent_replacement(monkeypatch, logs):
    # Unlike the test above, the concurrent call has already created the replacement, so
    # self.session is not None by the time this caller looks. Its own session's files are gone
    # either way, so a read must still be refused rather than answered from an empty VM.
    dead = _FakeSession('sb-dead', state='TERMINATED')
    glb, client = _started(monkeypatch, dead, _FakeSession('sb-fresh'))

    def call(session):
        # A concurrent call hits the same ended session first, drops it, and retries on a new one.
        glb.recover_session(session, glb.session_epoch, replace=True)
        glb.get_session()
        raise mod.SessionTerminatedError('session_terminated')

    with pytest.raises(mod.SessionEndedError):
        glb.call_with_session(call, replace=False)
    assert len(client.create_calls) == 2
    assert glb.session is not None


def test_a_replaced_session_is_never_reported_to_the_agent_as_merely_resumed(monkeypatch, logs):
    # 'resumed' tells the agent its files survived. A later resume must not overwrite the
    # replacement this caller actually suffered, or the agent trusts files that are gone.
    dead = _FakeSession('sb-dead', state='TERMINATED')
    glb, _ = _started(monkeypatch, dead, _FakeSession('sb-fresh'))
    seen = []

    def call(session):
        seen.append(session)
        if len(seen) > 1:
            return 'ok'
        glb.recover_session(session, glb.session_epoch, replace=True)
        replacement = glb.get_session()
        # A third call then finds that replacement paused and resumes it.
        replacement.state = 'PAUSED'
        glb.recover_session(replacement, glb.session_epoch, replace=True)
        raise mod.SessionTerminatedError('session_terminated')

    recoveries = []
    assert glb.call_with_session(call, on_recovery=recoveries.append) == 'ok'
    assert recoveries == ['replaced'], recoveries


# ---------------------------------------------------------------------------
# No secret enters the VM: its user has passwordless sudo, so nothing placed there
# could be kept from the commands an agent runs
# ---------------------------------------------------------------------------


def test_nothing_secret_is_sent_into_the_vm(monkeypatch, logs):
    # A github_token left in an older pipeline's config is ignored rather than injected.
    cfg = {'github_token': 'mock-github-token-placeholder-for-tests'}
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg=cfg)
    glb.get_session()
    [call] = client.create_calls
    assert not {'github_token', 'env', 'setup_env', 'setup_secrets', 'clone_repo_url'} & set(call)


def test_the_api_key_is_the_only_secret_field():
    services = json.loads((_NODES_SRC / 'nodes' / 'tool_tenki' / 'services.json').read_text())
    secret = {name for name, field in services['fields'].items() if field.get('secure')}
    assert secret == {'tenki.apikey'}


# ---------------------------------------------------------------------------
# git: arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(('value', 'expected'), [(' main ', 'main'), (None, None), ('   ', None)])
def test_optional_git_arguments_are_stripped_or_absent(value, expected):
    assert inst_mod._git_arg({'ref': value}, 'ref') == expected


@pytest.mark.parametrize('value', ['--upload-pack=touch /home/tenki/x', '-b', 7])
def test_git_arguments_git_would_read_as_options_or_that_are_not_text_are_refused(value):
    with pytest.raises(ValueError):
        inst_mod._git_arg({'ref': value}, 'ref')


def test_a_required_git_argument_must_be_present():
    with pytest.raises(ValueError):
        inst_mod._git_arg({'repo': '  '}, 'repo', required=True)


# ---------------------------------------------------------------------------
# git_clone
# ---------------------------------------------------------------------------

_REPO = 'https://github.com/octocat/Hello-World.git'


@pytest.mark.parametrize(
    'args',
    [
        {},
        {'repo': ''},
        {'repo': 3},
        {'repo': '--upload-pack=touch /home/tenki/x'},
        {'repo': _REPO, 'directory': '/tmp/hello'},
        {'repo': _REPO, 'directory': 9},
        {'repo': _REPO, 'branch': '-b'},
        {'repo': _REPO, 'depth': 0},
        {'repo': _REPO, 'depth': 'shallow'},
    ],
)
def test_git_clone_rejects_invalid_input_without_touching_the_sandbox(monkeypatch, logs, args):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.git_clone(args)
    assert client.create_calls == []


def test_git_clone_passes_its_arguments_through(monkeypatch, logs):
    session = _FakeSession('sb-1', git_outputs={'clone': "Cloning into '/home/tenki/src/hello'...\n"})
    inst, _ = _instance(monkeypatch, session)
    result = inst.git_clone({'repo': _REPO, 'branch': 'main', 'depth': 1, 'directory': 'src/hello'})
    assert result == {
        'directory': '/home/tenki/src/hello',
        'output': "Cloning into '/home/tenki/src/hello'...\n",
        'truncated': False,
    }
    assert _git_calls(session, 'clone') == [
        {'repo': _REPO, 'branch': 'main', 'depth': 1, 'directory': '/home/tenki/src/hello'}
    ]


@pytest.mark.parametrize(
    'repo',
    [
        'https://github.com/octocat/Hello-World.git',
        'https://github.com/octocat/Hello-World',
        'https://github.com/octocat/Hello-World/',
        'https://github.com:443/octocat/Hello-World.git',
    ],
)
def test_git_clone_defaults_to_a_folder_named_after_the_repository(monkeypatch, logs, repo):
    # git's own default, made explicit so the result can tell the agent where the repository went.
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    assert inst.git_clone({'repo': repo})['directory'] == '/home/tenki/Hello-World'
    assert _git_calls(session, 'clone')[0]['directory'] == '/home/tenki/Hello-World'


def test_git_clone_asks_for_a_directory_when_the_repository_names_no_folder(monkeypatch, logs):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError, match='"directory"'):
        inst.git_clone({'repo': 'https://example.com/.git'})
    assert client.create_calls == []


@pytest.mark.parametrize(
    'repo',
    [
        'https://token@github.com/org/private.git',
        'https://user:pass@github.com/org/private.git',
        'git@github.com:org/private.git',
        'ssh://git@github.com/org/private.git',
        'file:///home/tenki/secret',
        'git://github.com/org/repo.git',
        '/home/tenki/local-repo',
        'https:///org/repo.git',
    ],
)
def test_git_clone_refuses_a_repository_that_is_not_a_public_http_url(monkeypatch, logs, repo):
    # The node's whole premise is that no credential reaches the VM. A URL carrying userinfo
    # would write that credential into .git/config inside a VM whose user has passwordless sudo,
    # and which every caller of a shared pipeline can read.
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    with pytest.raises(ValueError):
        inst.git_clone({'repo': repo})
    assert _git_calls(session, 'clone') == []


def test_absent_git_output_is_returned_as_empty_text():
    # The SDK declares git output as str; were it ever absent, the result must still be well formed.
    assert inst_mod._git_result(None, 100) == {'output': '', 'truncated': False}


def test_git_clone_returns_git_failures_to_the_agent(monkeypatch, logs):
    failure = inst_mod.SandboxError(
        'git clone failed (exit_code=128); check the repository, ref and access token: Repository not found'
    )
    inst, _ = _instance(monkeypatch, _FakeSession('sb-1', fail={'clone': failure}))
    result = inst.git_clone({'repo': _REPO})
    assert result['output'] == ''
    assert 'Repository not found' in result['error']


def test_git_clone_on_an_ended_session_clones_into_a_fresh_one(monkeypatch, logs):
    dead = _FakeSession('sb-dead', state='TERMINATED', fail={'clone': mod.SessionTerminatedError('session_terminated')})
    fresh = _FakeSession('sb-fresh', git_outputs={'clone': 'cloned'})
    inst, client = _instance(monkeypatch, dead, fresh)
    assert inst.git_clone({'repo': _REPO})['output'] == 'cloned'
    assert len(client.create_calls) == 2


# ---------------------------------------------------------------------------
# git_checkout / git_diff / git_log
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'args',
    [{}, {'ref': ''}, {'ref': '-b'}, {'ref': 'main', 'create': 'yes'}, {'ref': 'main', 'directory': '../elsewhere'}],
)
def test_git_checkout_rejects_invalid_input_without_touching_the_sandbox(monkeypatch, logs, args):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.git_checkout(args)
    assert client.create_calls == []


@pytest.mark.parametrize(
    ('args', 'passed'),
    [
        ({'ref': 'main'}, {'ref': 'main', 'create': False, 'directory': None}),
        (
            {'ref': 'fix/login', 'create': True, 'directory': 'Hello-World'},
            {'ref': 'fix/login', 'create': True, 'directory': '/home/tenki/Hello-World'},
        ),
    ],
)
def test_git_checkout_passes_its_arguments_through(monkeypatch, logs, args, passed):
    session = _FakeSession('sb-1', git_outputs={'checkout': "Switched to branch 'main'\n"})
    inst, _ = _instance(monkeypatch, session)
    assert inst.git_checkout(args) == {'output': "Switched to branch 'main'\n", 'truncated': False}
    assert _git_calls(session, 'checkout') == [passed]


@pytest.mark.parametrize(
    'args',
    [
        {'range': '-p'},
        {'range': 'main..fix', 'base': 'main'},
        {'range': 'main..fix', 'head': 'fix'},
        {'path': 5},
        {'directory': '/etc'},
    ],
)
def test_git_diff_rejects_invalid_input_without_touching_the_sandbox(monkeypatch, logs, args):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.git_diff(args)
    assert client.create_calls == []


@pytest.mark.parametrize(
    ('args', 'passed'),
    [
        ({}, {'range': None, 'base': None, 'head': None, 'path': None, 'directory': None}),
        (
            {'range': 'main..fix', 'path': 'src/app.py', 'directory': 'repo'},
            {'range': 'main..fix', 'base': None, 'head': None, 'path': 'src/app.py', 'directory': '/home/tenki/repo'},
        ),
        (
            {'base': 'main', 'head': 'fix'},
            {'range': None, 'base': 'main', 'head': 'fix', 'path': None, 'directory': None},
        ),
    ],
)
def test_git_diff_passes_its_arguments_through(monkeypatch, logs, args, passed):
    # path is a pathspec inside the repository, so it is passed as given rather than resolved.
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    inst.git_diff(args)
    assert _git_calls(session, 'diff') == [passed]


def test_git_diff_output_is_truncated_at_the_cap(monkeypatch, logs):
    session = _FakeSession('sb-1', git_outputs={'diff': '+' * 5000})
    inst, _ = _instance(monkeypatch, session, cfg={'max_output_chars': 1000})
    assert inst.git_diff({}) == {'output': '+' * 1000, 'truncated': True}


@pytest.mark.parametrize('args', [{'max_count': 0}, {'max_count': 1001}, {'max_count': 'ten'}, {'range': '--all'}])
def test_git_log_rejects_invalid_input_without_touching_the_sandbox(monkeypatch, logs, args):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    with pytest.raises(ValueError):
        inst.git_log(args)
    assert client.create_calls == []


def test_git_log_is_bounded_even_when_no_count_is_given(monkeypatch, logs):
    # The whole log comes back in one response, so an unbounded one of a large repository would
    # land in memory in full before it could be truncated.
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    inst.git_log({})
    [passed] = _git_calls(session, 'log')
    assert isinstance(passed['max_count'], int) and 0 < passed['max_count'] <= 100


def test_git_log_passes_its_arguments_through(monkeypatch, logs):
    session = _FakeSession('sb-1', git_outputs={'log': 'commit abc123\n'})
    inst, _ = _instance(monkeypatch, session)
    args = {'max_count': 5, 'range': 'main..fix', 'path': 'README.md', 'directory': 'repo'}
    assert inst.git_log(args) == {'output': 'commit abc123\n', 'truncated': False}
    assert _git_calls(session, 'log') == [
        {'max_count': 5, 'range': 'main..fix', 'path': 'README.md', 'directory': '/home/tenki/repo'}
    ]


@pytest.mark.parametrize(('tool', 'args'), [('git_checkout', {'ref': 'main'}), ('git_diff', {}), ('git_log', {})])
def test_inspecting_a_repository_in_an_ended_session_does_not_start_a_new_one(monkeypatch, logs, tool, args):
    # The repository went with the session, so a fresh, empty one could only fail to find it.
    ended = mod.SessionTerminatedError('session_terminated')
    dead = _FakeSession('sb-dead', state='TERMINATED', fail={'checkout': ended, 'diff': ended, 'log': ended})
    inst, client = _instance(monkeypatch, dead, _FakeSession('sb-fresh'))
    assert getattr(inst, tool)(args)['error']
    assert len(client.create_calls) == 1


# ---------------------------------------------------------------------------
# Hardening: bounded calls, recovery the agent can see, findable sessions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(('exec_timeout', 'deadline'), [(45, 60), (300, 300)])
def test_every_control_plane_call_gets_a_deadline(monkeypatch, logs, exec_timeout, deadline):
    # Create, refresh, resume and close run under the session lock. Without a deadline, one
    # half-open connection hangs them, and endGlobal behind them, while the VM keeps billing.
    # Git operations use the same deadline, so it is never below the execution timeout.
    glb, constructed = _make_global(monkeypatch, {'apikey': _KEY, 'exec_timeout_secs': exec_timeout})
    glb.beginGlobal()
    assert constructed[0]['timeout'] == deadline


@pytest.mark.parametrize(('tool', 'args'), [('run_command', {'command': 'sleep 999'}), ('run_code', {'code': 'x'})])
def test_a_stalled_command_stream_is_cut_off_and_reported_as_a_timeout(monkeypatch, logs, tool, args):
    # Sandbox.exec waits on the output stream with no local limit, so a stream that stalls would
    # hang the tool call forever. The local limit leaves room for Tenki's own timeout report first.
    session = _FakeSession('sb-1', stalls='result')
    inst, _ = _instance(monkeypatch, session, cfg={'exec_timeout_secs': 45})
    result = getattr(inst, tool)(args)
    assert result['timed_out'] is True
    assert result['exit_code'] == -1
    [(_, waited)] = [call for call in session.calls if call[0] == 'wait']
    assert 45 < waited <= 105
    assert f'no result within {waited}s' in result['error']


@pytest.mark.parametrize(('tool', 'args'), [('run_command', {'command': 'make test'}), ('run_code', {'code': 'x'})])
def test_a_command_that_never_starts_is_cancelled_and_reported_as_a_timeout(monkeypatch, logs, tool, args):
    # The SDK stops waiting for the start after 30 seconds but cancels nothing, so a start that
    # arrived late would run a command the agent had already been told timed out.
    session = _FakeSession('sb-1', stalls='start')
    inst, _ = _instance(monkeypatch, session, cfg={'exec_timeout_secs': 45})
    result = getattr(inst, tool)(args)
    assert result['timed_out'] is True
    assert result['exit_code'] == -1
    assert 'did not start' in result['error']
    assert ('kill',) in session.calls


# The SDK keeps everything a command prints in memory until it exits, with no limit of its own.
_FLOOD = b'y\n' * 4096  # what `yes` prints, 8 KiB at a time


@pytest.mark.parametrize(
    ('tool', 'args'), [('run_command', {'command': 'yes'}), ('run_code', {'code': 'while True: print("y")'})]
)
def test_a_command_that_floods_its_output_is_killed_and_the_agent_told_why(monkeypatch, logs, tool, args):
    # Left alone, `yes` would grow the engine's memory for the whole execution timeout.
    limit = 64 * 1024
    monkeypatch.setattr(inst_mod, '_OUTPUT_LIMIT_BYTES', limit)
    session = _FakeSession('sb-1', flood=(_FLOOD, 64 * limit))
    inst, _ = _instance(monkeypatch, session, cfg={'max_output_chars': 1000})
    result = getattr(inst, tool)(args)
    assert ('kill',) in session.calls
    [process] = session.processes
    # Killed on the chunk that crossed the limit, not left to print until it stopped on its own.
    assert process.printed <= limit + 2 * len(_FLOOD)
    assert result['truncated'] is True
    assert 'killed' in result['stderr']
    assert f'{limit:,} bytes' in result['stderr']
    assert 'to a file' in result['stderr']


def test_a_chatty_command_that_finishes_is_truncated_but_not_killed(monkeypatch, logs):
    # max_output_chars caps what the agent sees, not how long a command may run: a large apt-get
    # install prints far more than that, and killing it mid-dpkg would break the VM's packages.
    output = b'Setting up libfoo (1.2.3) ...\n' * 5000
    session = _FakeSession('sb-1', exec_result=_CommandResult(argv=['bash'], exit_code=0, stdout=output))
    inst, _ = _instance(monkeypatch, session, cfg={'max_output_chars': 1000})
    result = inst.run_command({'command': 'sudo apt-get install -y build-essential'})
    assert ('kill',) not in session.calls
    assert result['exit_code'] == 0
    assert result['truncated'] is True
    assert 'killed' not in result['stderr']


def test_a_stalled_command_leaves_no_output_reader_running(monkeypatch, logs):
    # The readers block until their stream ends, and a stalled stream never does on its own.
    session = _FakeSession('sb-1', stalls='result')
    inst, _ = _instance(monkeypatch, session, cfg={'exec_timeout_secs': 45})
    before = set(threading.enumerate())
    returned = threading.Event()
    worker = threading.Thread(target=lambda: (inst.run_command({'command': 'sleep 999'}), returned.set()), daemon=True)
    worker.start()
    assert returned.wait(timeout=5), 'the call hung waiting for its output readers'
    worker.join(timeout=5)
    assert [thread for thread in threading.enumerate() if thread not in before] == []


def test_a_run_that_fails_partway_is_reported_once_and_not_from_a_reader_thread(monkeypatch, logs):
    # The SDK ends both output streams with the run's error, so the readers see it as well as wait().
    escaped = []
    monkeypatch.setattr(threading, 'excepthook', escaped.append)
    session = _FakeSession('sb-1', run_error=inst_mod.SandboxError('run stream reset'))
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'make'})
    # Reported once, with its cause, and as what it is: the stream broke, the command may not have.
    assert result['error'].count('run stream reset') == 1 and 'not run again' in result['error']
    assert len(session.processes) == 1
    assert escaped == []


def test_recovery_waits_a_bounded_time_for_a_resumed_session(monkeypatch, logs):
    paused = _FakeSession('sb-1', state='PAUSED')
    glb, _ = _started(monkeypatch, paused)
    glb.call_with_session(
        lambda session: _raise(mod.InvalidStateError('session is paused')) if session.state == 'PAUSED' else 'done'
    )
    assert paused.ready_timeouts
    assert all(timeout is not None and 0 < timeout <= 600 for timeout in paused.ready_timeouts)


# ---------------------------------------------------------------------------
# Failures the SDK raises as plain RuntimeError or TimeoutError, which a tool's
# `except SandboxError` would otherwise let out of the tool
# ---------------------------------------------------------------------------

_ENDED_WHILE_WAITING = RuntimeError('sandbox entered terminal state: TERMINATED')


@pytest.mark.parametrize(
    ('tool', 'args'), [('run_command', {'command': 'ls'}), ('write_file', {'path': 'a.txt', 'content': 'x'})]
)
def test_a_session_that_ends_while_it_resumes_is_replaced_and_the_call_retried(monkeypatch, logs, tool, args):
    # It can be terminated mid-resume: the dead session must not stay in place, and the
    # call can still be served by a fresh one.
    paused = _FakeSession('sb-1', state='PAUSED', ready_state='TERMINATED', ready_error=_ENDED_WHILE_WAITING)
    inst, client = _instance(monkeypatch, paused, _FakeSession('sb-fresh'))
    result = getattr(inst, tool)(args)
    assert result['session'] == 'replaced'
    assert 'error' not in result
    assert len(client.create_calls) == 2


def test_a_read_from_a_session_that_ends_while_it_resumes_says_the_session_ended(monkeypatch, logs):
    # A fresh, empty session could not contain the file, so none is created for the read.
    paused = _FakeSession('sb-1', state='PAUSED', ready_state='TERMINATED', ready_error=_ENDED_WHILE_WAITING)
    inst, client = _instance(monkeypatch, paused, _FakeSession('sb-fresh'))
    result = inst.read_file({'path': 'notes.md'})
    assert result['content'] == ''
    assert 'session has ended' in result['error']
    assert len(client.create_calls) == 1


def test_a_resume_that_runs_out_of_time_comes_back_to_the_agent_as_an_error(monkeypatch, logs):
    # Not replaced: the session may still come up, and with it every file the agent wrote.
    stuck = TimeoutError('timeout waiting for sandbox sb-1')
    paused = _FakeSession('sb-1', state='PAUSED', ready_state='RESUMING', ready_error=stuck)
    inst, client = _instance(monkeypatch, paused)
    result = inst.run_command({'command': 'ls'})
    assert result['exit_code'] == -1
    assert 'did not finish resuming' in result['error']
    assert len(client.create_calls) == 1
    assert inst.IGlobal.session is paused


def test_a_session_that_ends_as_it_is_created_comes_back_to_the_agent_as_an_error(monkeypatch, logs):
    # Client.create re-raises its readiness wait's plain RuntimeError when the new sandbox went
    # straight to a terminal state.
    inst, client = _instance(monkeypatch, create_error=_ENDED_WHILE_WAITING)
    result = inst.run_command({'command': 'ls'})
    assert result['exit_code'] == -1
    assert 'did not start' in result['error']
    assert 'terminal state' in result['error']


def test_a_tool_called_after_shutdown_comes_back_to_the_agent_as_an_error(monkeypatch, logs):
    inst, client = _instance(monkeypatch, _FakeSession('sb-1'))
    inst.IGlobal.endGlobal()
    result = inst.run_command({'command': 'ls'})
    assert result['exit_code'] == -1
    assert 'shutting down' in result['error']
    assert client.create_calls == []


@pytest.mark.parametrize(
    ('tool', 'args'),
    [('run_command', {'command': 'ls'}), ('run_code', {'code': 'print(1)'}), ('list_files', {})],
)
def test_a_plain_runtime_error_from_the_sdk_comes_back_to_the_agent_as_an_error(monkeypatch, logs, tool, args):
    # Sandbox.start raises one when the session's data plane cannot be reached, and nothing in the
    # SDK promises it is the only one, so no tool may let one out.
    unreachable = RuntimeError('sandbox data-plane endpoint unavailable')
    session = _FakeSession('sb-1', exec_error=unreachable, fail={'list': unreachable})
    inst, _ = _instance(monkeypatch, session)
    result = getattr(inst, tool)(args)
    assert 'data-plane endpoint unavailable' in result['error']
    # Unexpected enough to be worth an operator's attention, not only the agent's.
    assert any('data-plane endpoint unavailable' in message for message in logs)


def test_a_tool_call_racing_shutdown_cannot_create_a_session(monkeypatch, logs):
    # endGlobal closes the session and then the client. A call arriving in between must not create
    # a session that nothing would ever close.
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), _FakeSession('sb-2'))
    glb.get_session()
    client.close_gate = threading.Event()
    ender = threading.Thread(target=glb.endGlobal)
    ender.start()
    try:
        assert client.closing.wait(timeout=5)
        with pytest.raises(mod.SessionUnavailableError):
            glb.get_session()
    finally:
        client.close_gate.set()
        ender.join()
    assert len(client.create_calls) == 1


def test_a_call_that_ran_on_a_resumed_session_says_so(monkeypatch, logs):
    inst, _ = _instance(monkeypatch, _FakeSession('sb-1', state='PAUSED'))
    assert inst.run_command({'command': 'ls'})['session'] == 'resumed'


# What Tenki answered a read on a session it had paused 45 seconds earlier (measured through the node on
# 2026-10-05). The SDK has no class for it, so it arrives as a plain SandboxError.
_VSOCK = (
    'dial guest-agent stream after 3 attempts: failed to connect to vsock at /opt/cloud-hypervisor/data/'
    'instances/sandbox/sb-1/rootfs.img.vsock: connect: no such file or directory'
)


@pytest.mark.parametrize(
    ('tool', 'args', 'key', 'expected'),
    [
        ('read_file', {'path': 'notes.txt'}, 'content', 'kept'),
        ('write_file', {'path': 'new.txt', 'content': 'x'}, 'success', True),
        ('run_command', {'command': 'ls'}, 'exit_code', 0),
    ],
)
def test_a_pause_reported_as_an_unclassified_error_is_still_resumed(monkeypatch, logs, tool, args, key, expected):
    # Recovery follows the session's real state, not the error that surfaced. Seen on a real VM: the
    # first read after a lifetime pause came back as this infrastructure error, and only the agent's
    # own retry resumed the session.
    session = _FakeSession(
        'sb-1', state='PAUSED', paused_error=mod.SandboxError(_VSOCK), files={'/home/tenki/notes.txt': 'kept'}
    )
    inst, client = _instance(monkeypatch, session)
    result = getattr(inst, tool)(args)
    assert 'error' not in result, result
    assert result[key] == expected and result['session'] == 'resumed'
    assert session.calls.count('resume') == 1 and len(client.create_calls) == 1


def test_an_unclassified_error_on_a_running_session_is_reported_as_it_is(monkeypatch, logs):
    # The look at the state must not turn every failure into a retry: a session that is running has
    # nothing to recover, so the error goes back unchanged and the call is not made a second time.
    session = _FakeSession('sb-1', fail={'stat': mod.SandboxError('upstream reset')})
    inst, _ = _instance(monkeypatch, session)
    result = inst.read_file({'path': 'notes.txt'})
    assert result['error'] == 'upstream reset'
    assert session.calls.count('refresh') == 1 and 'resume' not in session.calls
    assert [call for call in session.calls if call[0] == 'stat'] == [('stat', '/home/tenki/notes.txt')]


def test_an_error_the_sdk_did_classify_costs_no_look_at_the_session(monkeypatch, logs):
    # A missing file is an answer, not a symptom. It must not cost a control-plane call.
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    assert 'no such file' in inst.read_file({'path': 'absent.txt'})['error']
    assert 'refresh' not in session.calls


def test_a_resume_tenki_refuses_at_first_is_asked_again_until_it_takes(monkeypatch, logs):
    # "Not yet" is not a failure. Reporting it would hand the agent an error for a session that is
    # about to be fine, at the moment every long-running pipeline reaches every max_duration.
    monkeypatch.setattr(mod.time, 'sleep', lambda seconds: None)
    session = _FakeSession('sb-1', state='PAUSED', resume_refusals=3, files={'/home/tenki/notes.txt': 'kept'})
    inst, client = _instance(monkeypatch, session)
    result = inst.read_file({'path': 'notes.txt'})
    assert result.get('content') == 'kept' and result['session'] == 'resumed', result
    assert session.calls.count('resume') == 4 and len(client.create_calls) == 1


def test_a_resume_that_keeps_being_refused_is_reported_and_the_session_kept(monkeypatch, logs):
    # Bounded: a call must not wait on Tenki for ever. The session is not replaced, since its files
    # are still there for the call that finally gets through.
    monkeypatch.setattr(mod.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(mod, '_RESUME_RETRY_SECS', 0)
    session = _FakeSession('sb-1', state='PAUSED', resume_refusals=1000)
    inst, client = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'ls'})
    assert result['exit_code'] == -1 and 'still being torn down' in result['error']
    assert session.calls.count('resume') == 1 and len(client.create_calls) == 1
    assert inst.IGlobal.session is session


def test_a_resume_refused_for_a_reason_tenki_names_is_not_asked_again(monkeypatch, logs):
    # Only the catch-all "not yet" is worth repeating. An error with a class of its own means something.
    monkeypatch.setattr(mod.time, 'sleep', lambda seconds: None)
    session = _FakeSession('sb-1', state='PAUSED')
    refused = inst_mod.PermissionDeniedError('workspace suspended')

    def resume():
        session.calls.append('resume')
        raise refused

    session.resume = resume
    inst, _ = _instance(monkeypatch, session)
    assert inst.run_command({'command': 'ls'})['error'] == 'workspace suspended'
    assert session.calls.count('resume') == 1


class _PausedBeforeStart(_FakeSession):
    """A session whose pause surfaces as a command that never starts.

    As measured: no error names the pause, the SDK just times out waiting for the start. After the
    resume, commands start normally.
    """

    def __init__(self, session_id, **kwargs):
        super().__init__(session_id, stalls='start', **kwargs)
        self.paused = True

    def refresh(self):
        self.calls.append('refresh')
        return SimpleNamespace(id=self.id, state='PAUSED' if self.paused else 'RUNNING')

    def resume(self):
        super().resume()
        self.paused = False
        self._stalls = None


@pytest.mark.parametrize(('tool', 'args'), [('run_command', {'command': 'ls'}), ('run_code', {'code': 'print(1)'})])
def test_a_command_a_paused_session_never_started_is_run_after_the_resume(monkeypatch, logs, tool, args):
    # Measured on a real VM: a command sent as the session read PAUSED waited 30 seconds, failed with
    # "did not start the command in time", and left the session paused. It had not started, so running
    # it once the session is back repeats nothing.
    session = _PausedBeforeStart('sb-1', exec_result=_CommandResult(argv=['bash'], exit_code=0, stdout=b'ok\n'))
    inst, client = _instance(monkeypatch, session)
    result = getattr(inst, tool)(args)
    assert result.get('stdout') == 'ok\n' and result['session'] == 'resumed', result
    assert session.calls.count('resume') == 1 and len(_execs(session)) == 2 and len(client.create_calls) == 1


def test_a_command_that_does_not_start_on_a_running_session_still_times_out(monkeypatch, logs):
    session = _FakeSession('sb-1', stalls='start')
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'ls'})
    assert result['timed_out'] is True and 'did not start' in result['error']
    assert 'resume' not in session.calls and len(_execs(session)) == 1


class _PausedMidCommand(_FakeSession):
    """A session Tenki pauses while its first command is running.

    As measured on a real VM: the command's streams end with the SDK's termination error, the session
    then reads PAUSED, and commands started after the resume run normally.
    """

    _CUT = 'session_terminated:terminate_requested: termination requested'

    def __init__(self, session_id, **kwargs):
        super().__init__(session_id, run_error=mod.SessionTerminatedError(self._CUT), **kwargs)

    def start(self, *argv, **kwargs):
        process = super().start(*argv, **kwargs)
        if self._run_error is not None:
            self.state = 'PAUSED'
        return process

    def resume(self):
        super().resume()
        self._run_error = None


@pytest.mark.parametrize(
    ('tool', 'args'), [('run_command', {'command': 'make deploy'}), ('run_code', {'code': 'print(1)'})]
)
def test_a_command_running_when_the_session_was_paused_is_not_run_again(monkeypatch, logs, tool, args):
    # Measured on a real VM (2026-10-05): the retry after the resume started the command a second time
    # while the first copy's process still existed. Whether that first copy then carries on (Tenki
    # documents that it survives a pause) or dies soon after the resume (what later measurements
    # showed), it had already done part of its work. Whatever the command does, it must not be done twice.
    session = _PausedMidCommand('sb-1')
    inst, client = _instance(monkeypatch, session)
    result = getattr(inst, tool)(args)
    assert len(_execs(session)) == 1
    assert session.calls.count('resume') == 1
    assert result['session'] == 'resumed'
    assert result['exit_code'] == -1 and result['timed_out'] is False
    assert 'paused' in result['error'] and 'not run again' in result['error']
    assert 'part of its work may be done' in result['error'] and 'Check what it did' in result['error']
    assert 'survives' not in result['error']  # not something the node can promise: see the measurements
    assert len(client.create_calls) == 1


class _CutAndStuck(_FakeSession):
    """A session that cuts its first command off and then cannot be brought back within the call.

    ``stuck`` says how: it still reads PAUSING when recovery looks, its resume keeps being refused, or
    it never becomes ready after the resume. All three were seen, or follow from what was seen, on
    real sessions in the minute after a pause.
    """

    def __init__(self, session_id, stuck, **kwargs):
        cut = mod.SessionTerminatedError('session_terminated:terminate_requested: termination requested')
        ready = TimeoutError('timeout waiting for sandbox') if stuck == 'never ready' else None
        super().__init__(
            session_id, run_error=cut, ready_state='RESUMING' if ready else None, ready_error=ready, **kwargs
        )
        self._stuck = stuck

    def refresh(self):
        self.calls.append('refresh')
        if not self.processes:
            return SimpleNamespace(id=self.id, state=self.state)
        return SimpleNamespace(id=self.id, state='PAUSING' if self._stuck == 'pausing' else 'PAUSED')

    def resume(self):
        if self._stuck == 'refused':
            self.calls.append('resume')
            raise mod.SandboxError(_NOT_YET)
        super().resume()


@pytest.mark.parametrize('stuck', ['pausing', 'refused', 'never ready'])
@pytest.mark.parametrize(
    ('tool', 'args'), [('run_command', {'command': 'make deploy'}), ('run_code', {'code': 'print(1)'})]
)
def test_a_started_command_is_never_reported_as_simply_failed(monkeypatch, logs, tool, args, stuck):
    # Whatever stopped the call, the command had started and a paused VM keeps it. An agent handed only
    # "termination requested" or "retry shortly" repeats the command and ends up running two.
    monkeypatch.setattr(mod.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(mod, '_RESUME_RETRY_SECS', 0)
    session = _CutAndStuck('sb-1', stuck)
    inst, client = _instance(monkeypatch, session)
    result = getattr(inst, tool)(args)
    assert result['exit_code'] == -1 and result['timed_out'] is False
    assert 'not run again' in result['error'] and 'part of its work may be done' in result['error'], result['error']
    assert len(session.processes) == 1 and len(client.create_calls) == 1


def test_a_command_started_by_the_retry_and_cut_again_is_reported_the_same_way(monkeypatch, logs):
    # Seen on a real VM with a one minute lifetime: the session was paused before the command started,
    # the retry after the resume started it, and the next pause cut that one. The agent got the raw
    # termination text. The command the retry started is as likely to be running as any other.
    cut = mod.SessionTerminatedError('session_terminated:terminate_requested: termination requested')
    session = _FakeSession('sb-1', state='PAUSED', run_error=cut)
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'make deploy'})
    assert 'not run again' in result['error'] and 'part of its work may be done' in result['error'], result['error']
    assert result['session'] == 'resumed' and len(session.processes) == 1


def test_a_command_the_node_stopped_at_its_deadline_is_reported_as_timed_out(monkeypatch, logs):
    # The one failure after a start that is not "cut off, check what it did": the wait ran out and
    # the process was killed.
    session = _FakeSession('sb-1', stalls='result')
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'sleep 999'})
    assert result['timed_out'] is True and 'not run again' not in result['error']
    assert ('kill',) in session.calls


def test_the_command_after_an_interrupted_one_runs_normally(monkeypatch, logs):
    session = _PausedMidCommand('sb-1', exec_result=_CommandResult(argv=['bash'], exit_code=0, stdout=b'ok\n'))
    inst, _ = _instance(monkeypatch, session)
    assert 'not run again' in inst.run_command({'command': 'make deploy'})['error']
    after = inst.run_command({'command': 'ls'})
    assert after['stdout'] == 'ok\n' and 'session' not in after
    assert len(_execs(session)) == 2


@pytest.mark.parametrize(
    ('tool', 'args'), [('run_command', {'command': 'make deploy'}), ('run_code', {'code': 'print(1)'})]
)
def test_a_command_cut_off_by_a_terminated_session_is_run_again_on_the_fresh_one(monkeypatch, logs, tool, args):
    # Nothing of the first run survives a terminated session, so starting over repeats nothing inside
    # the sandbox. (What a command did outside it, a deploy say, is the agent's to judge either way.)
    ended = mod.SessionTerminatedError('session_terminated')
    dead = _FakeSession('sb-dead', run_error=ended)
    dead.refresh = lambda: SimpleNamespace(id=dead.id, state='TERMINATED')
    fresh = _FakeSession('sb-fresh', exec_result=_CommandResult(argv=['bash'], exit_code=0, stdout=b'ok\n'))
    inst, _ = _instance(monkeypatch, dead, fresh)
    result = getattr(inst, tool)(args)
    assert result['session'] == 'replaced' and result['stdout'] == 'ok\n'
    assert len(_execs(dead)) == 1 and len(_execs(fresh)) == 1


@pytest.mark.parametrize(
    ('tool', 'args', 'operation'),
    [
        ('run_command', {'command': 'ls'}, 'start'),
        ('run_code', {'code': 'print(1)'}, 'write_text'),
        ('write_file', {'path': 'a.txt', 'content': 'x'}, 'write_text'),
        ('make_directory', {'path': 'data'}, 'mkdir'),
        ('git_clone', {'repo': _REPO}, 'clone'),
    ],
)
def test_a_call_that_ran_on_a_fresh_session_says_so(monkeypatch, logs, tool, args, operation):
    # Earlier files and installed packages are gone; a silent retry would let the agent assume otherwise.
    ended = mod.SessionTerminatedError('session_terminated')
    dead = _FakeSession('sb-dead', state='TERMINATED', exec_error=ended, fail={operation: ended})
    inst, _ = _instance(monkeypatch, dead, _FakeSession('sb-fresh'))
    assert getattr(inst, tool)(args)['session'] == 'replaced'


def test_a_recovery_done_by_a_concurrent_call_is_reported_too(monkeypatch, logs):
    session = _FakeSession('sb-1')
    glb, _ = _started(monkeypatch, session)
    reported, attempts = [], []

    def call(current):
        attempts.append(current.id)
        if len(attempts) == 1:
            # Another call resumed the paused session while this one was failing against it.
            glb.last_recovery = 'resumed'
            glb.session_epoch += 1
            raise mod.InvalidStateError('session is paused')
        return 'done'

    assert glb.call_with_session(call, on_recovery=reported.append) == 'done'
    assert reported == ['resumed']


def test_replacing_an_ended_session_is_logged_for_the_operator(monkeypatch, logs):
    # A replacement costs a new VM and drops the old one's state, so it must show up in the job log.
    dead = _FakeSession('sb-dead', state='TERMINATED')
    glb, _ = _started(monkeypatch, dead, _FakeSession('sb-fresh'))
    glb.call_with_session(
        lambda session: _raise(mod.SessionTerminatedError('session_terminated')) if session is dead else 'ok'
    )
    assert any('sb-dead' in message for message in logs)


def test_sessions_are_labelled_so_an_orphan_can_be_found(monkeypatch, logs):
    # A VM left behind by a crashed engine has to be recognisable in Tenki's console and listable by tag.
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), _FakeSession('sb-2'))
    glb.get_session()
    glb.session = None
    glb.get_session()
    first, second = client.create_calls
    assert first['tags'][0] == 'rocketride'
    run_tag = first['tags'][1]
    assert run_tag.startswith('run-')
    assert second['tags'] == first['tags']  # one run, one tag
    assert first['metadata'] == {'created_by': 'rocketride', 'node': 'tool_tenki'}
    assert first['name'].startswith('rocketride-tool-tenki-')
    assert first['name'] != second['name']


def test_groups_without_tools_are_no_longer_offered():
    # Selecting one used to validate and then publish nothing at all.
    with pytest.raises(ValueError):
        groups.normalize_groups(['ports'])


# ---------------------------------------------------------------------------
# Leak-proof cleanup: close a dropped session, and sweep this run's tag at shutdown
# ---------------------------------------------------------------------------


def test_a_session_reported_not_found_is_still_closed_before_being_forgotten(monkeypatch, logs):
    # "not found" can be a transient wrong answer, so the handle is closed anyway; otherwise a
    # still-running VM would bill until max_duration.
    gone = _FakeSession('sb-gone', refresh_error=mod.SessionNotFoundError('session not found'))
    glb, _ = _started(monkeypatch, gone, _FakeSession('sb-fresh'))
    glb.call_with_session(
        lambda session: _raise(mod.SessionNotFoundError('session not found')) if session is gone else session.id
    )
    assert 'close' in gone.calls


def test_two_runs_tag_their_sessions_differently(monkeypatch, logs):
    glb_a, client_a = _started(monkeypatch, _FakeSession('a'))
    glb_a.get_session()
    glb_b, client_b = _started(monkeypatch, _FakeSession('b'))
    glb_b.get_session()
    assert client_a.create_calls[0]['tags'][1] != client_b.create_calls[0]['tags'][1]


def test_shutdown_closes_sessions_left_running_under_this_runs_tag(monkeypatch, logs):
    # A create that raced the RPC deadline can leave a VM the node never got an id for; it still
    # carries this run's tag, so a sweep at shutdown finds and closes it.
    orphan = _FakeSession('sb-orphan')
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), leftover=[orphan])
    glb.get_session()
    glb.endGlobal()
    run_tag = client.create_calls[0]['tags'][1]
    assert client.list_calls == [[run_tag]]
    assert 'close' in orphan.calls


def test_parallel_tool_calls_on_one_instance_do_not_share_state(monkeypatch, logs):
    # An agent wave issues its tool calls concurrently against this single IInstance, on different
    # threads (agent_rocketride runs a wave in a thread pool). Each call needs its own temp file and
    # its own result; any state held on the instance instead of the call would cross them.
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    outputs = []

    _run_in_threads(8, lambda: outputs.append(inst.run_code({'code': 'print(1)'})))

    assert len(outputs) == 8
    assert all(out['exit_code'] == 0 and 'error' not in out for out in outputs)
    ran = [argv[-1] for argv, _ in _execs(session)]
    assert len(ran) == 8
    assert len(set(ran)) == 8, ran  # every call ran its own file, none reused another's


def test_a_parallel_wave_on_a_paused_session_resumes_once_and_tells_the_agent(monkeypatch, logs):
    # The real shape of it: Tenki pauses the VM between agent waves, and the next wave arrives as
    # several tool calls at once. One resume has to serve them all, and the wave has to tell the
    # agent the VM was resumed rather than replaced.
    paused = _FakeSession('sb-1', state='PAUSED', resume_delay=0.05)
    inst, client = _instance(monkeypatch, paused)
    outputs = []

    _run_in_threads(6, lambda: outputs.append(inst.run_command({'command': 'echo hi'})))

    assert len(outputs) == 6
    assert all(out['exit_code'] == 0 for out in outputs)
    reported = [out.get('session') for out in outputs]
    # A call arriving after the resume has finished never fails, so it recovered nothing and
    # reports nothing. Only 'resumed' or absent is correct; 'replaced' would mean lost files.
    assert set(reported) <= {'resumed', None}, reported
    assert 'resumed' in reported, reported
    assert paused.calls.count('resume') == 1
    assert len(client.create_calls) == 1  # resumed, never replaced


def test_a_clean_shutdown_does_not_claim_it_closed_anything(monkeypatch, logs):
    # The scoped close leaves the session TERMINATING, and a list right after still shows it. Saying
    # "closed" then would cry wolf on every shutdown and bury the one case that matters.
    messages = []
    monkeypatch.setattr(mod, 'debug', lambda message, *a, **kw: messages.append(str(message)), raising=False)
    already_going = _FakeSession('sb-going', state='TERMINATING')
    glb, _ = _started(monkeypatch, _FakeSession('sb-live'), leftover=[already_going])
    glb.get_session()
    glb.endGlobal()
    assert not [message for message in messages + logs if 'sb-going' in message]


def test_a_session_shutdown_left_running_is_closed_and_reported_to_the_operator(monkeypatch, logs):
    # This is the case worth hearing about: something escaped the scoped close and was still billing.
    leaked = _FakeSession('sb-leaked')
    glb, _ = _started(monkeypatch, _FakeSession('sb-live'), leftover=[leaked])
    glb.get_session()
    glb.endGlobal()
    assert 'close' in leaked.calls
    assert any('sb-leaked' in message for message in logs)


def test_shutdown_before_the_pipeline_ever_started_is_harmless(monkeypatch, logs):
    # In config mode, or after a failed start, there is no client and no session to close.
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY})
    glb.endGlobal()
    assert glb.client is None
    assert logs == []


def test_shutdown_of_a_run_that_never_created_a_session_stays_off_the_network(monkeypatch, logs):
    # Only a create can leave a session carrying this run's tag, and a pipeline that never needed the
    # sandbox should not need the network to shut down; a placeholder-key smoke test, for one.
    glb, client = _started(monkeypatch, _FakeSession('sb-1'))
    glb.endGlobal()
    assert client.list_calls == []
    assert client.closed is True


def test_shutdown_still_sweeps_after_a_create_that_failed(monkeypatch, logs):
    # A create can fail after Tenki admitted the VM, so an attempt is what counts, not a success.
    glb, client = _started(monkeypatch, create_error=mod.SandboxError('deadline exceeded'))
    with pytest.raises(mod.SandboxError):
        glb.get_session()
    run_tag = glb._run_tag
    glb.endGlobal()
    assert client.list_calls == [[run_tag]]


def test_the_shutdown_sweep_never_runs_without_this_runs_tag(monkeypatch, logs):
    # Not reachable today, since beginGlobal sets the tag before it creates the client. Kept as an
    # interlock: listing by an empty tag could match other runs' sessions, and close their work.
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), leftover=[_FakeSession('sb-other')])
    glb.get_session()  # so the sweep would otherwise run: only the missing tag may stop it
    glb._run_tag = ''
    glb.endGlobal()
    assert client.list_calls == []


def test_a_leftover_session_that_will_not_close_is_reported_to_the_operator(monkeypatch, logs):
    stuck = _FakeSession('sb-left', close_error=mod.SandboxError('permission denied'))
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), leftover=[stuck])
    glb.get_session()
    glb.endGlobal()
    assert client.closed is True
    assert any('could not close leftover session sb-left' in message for message in logs)


def test_a_failed_client_close_is_logged_and_the_client_still_dropped(monkeypatch, logs):
    glb, _ = _started(monkeypatch, _FakeSession('sb-1'), close_error=RuntimeError('channel already closed'))
    glb.endGlobal()
    assert glb.client is None
    assert any('channel already closed' in message for message in logs)


def test_a_failed_shutdown_sweep_is_logged_and_the_client_still_closes(monkeypatch, logs):
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), list_error=RuntimeError('connection reset'))
    glb.get_session()
    glb.endGlobal()
    assert client.closed is True
    assert any('connection reset' in message for message in logs)
