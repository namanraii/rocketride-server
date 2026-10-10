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


class _StubSnapshotFailedError(_StubSandboxError, RuntimeError):
    """Mirror of tenki 1.4.1's SnapshotFailedError: an SDK error that is also a plain RuntimeError."""


class _StubSnapshotWaitTimeoutError(_StubSandboxError, TimeoutError):
    """Mirror of tenki 1.4.1's SnapshotWaitTimeoutError: an SDK error that is also a plain TimeoutError."""


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
        warm_up_faults=False,
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
        # The faults above (exec_error, stalls, flood, run_error) are aimed at the tools' commands. The node's
        # no-op command meets them too only when warm_up_faults is set; a pause (state PAUSED) it always meets.
        self.warm_up_faults = warm_up_faults
        self.processes = []
        self.warm_up_processes = []
        self.calls = []
        self.ready_timeouts = []
        self.fs = _FakeFS(self, files=files, dirs=dirs, fail=fail)
        self.git = _FakeGit(self, outputs=git_outputs, fail=fail)

    def start(self, *argv, cwd=None, env=None, timeout=None, stdin=None, privileged=False):
        kwargs = {'cwd': cwd, 'env': env, 'timeout': timeout, 'stdin': stdin, 'privileged': privileged}
        self.calls.append(('start', argv, kwargs))
        if self.state == 'PAUSED':
            raise self.paused_error
        warm_up = argv == _WARM_UP and not self.warm_up_faults
        if self._exec_error is not None and not warm_up:
            raise self._exec_error
        if warm_up:
            process = _FakeProcess(self, _CommandResult(argv=list(argv), exit_code=0))
            self.warm_up_processes.append(process)
            return process
        result = self._exec_result if self._exec_result is not None else _CommandResult(argv=list(argv), exit_code=0)
        process = _FakeProcess(self, result, stalls=self._stalls, flood=self._flood, run_error=self._run_error)
        if argv == _WARM_UP:
            self.warm_up_processes.append(process)
        else:
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


class _FakeFileCalls:
    """Stand-in for the object SandboxFS sends every call through, which is where a deadline is passed.

    The real one is the session's RPC handle, called as ``rpc.stat(request)``. This records the
    ``timeout`` each call was given, under the RPC's name, and then runs the operation.
    """

    def __init__(self, deadlines):
        self._deadlines = deadlines

    def __getattr__(self, name):
        def call(operation, timeout=None):
            self._deadlines.append((name, timeout))
            return operation()

        return call


class _FakeFS:
    """Stand-in for tenki's SandboxFS over an in-memory tree, with the SDK's signatures.

    Calls are logged into the session's call log, a paused session refuses them like the fake
    exec does, and ``fail`` maps an operation name to the error it raises. There is deliberately
    no ``read_stream``: it has no deadline, so the node must never call it. As in the SDK, every
    operation goes out through ``_rpc``; ``deadlines`` lists the timeout each one was sent with.
    """

    def __init__(self, session, *, files=None, dirs=None, fail=None):
        self._session = session
        self._fail = dict(fail or {})
        self.files = dict(files or {})
        self.dirs = {'/home/tenki', *(dirs or ())}
        self.written = {}
        self.deadlines = []
        self._rpc = _FakeFileCalls(self.deadlines)

    def _enter(self, operation):
        if self._session.state == 'PAUSED':
            raise self._session.paused_error
        if operation in self._fail:
            raise self._fail[operation]

    def _missing(self, path):
        # Without the path, as a terse service message may be: the tool has to name it.
        return inst_mod.TenkiFileNotFoundError('no such file or directory')

    def write_text(self, path, text, *, encoding='utf-8'):
        return self._rpc.write_file(lambda: self._write_text(path, text))

    def _write_text(self, path, text):
        self._session.calls.append(('write_text', path))
        self._enter('write_text')
        if posixpath.dirname(path) not in self.dirs:
            raise self._missing(path)
        self.files[path] = text
        self.written[path] = text

    def stat(self, path):
        return self._rpc.stat(lambda: self._stat(path))

    def _stat(self, path):
        self._session.calls.append(('stat', path))
        self._enter('stat')
        if path in self.dirs:
            return _FileInfo(path=path, size=4096, mode=0o755, is_dir=True, modified_unix_ns=0)
        if path not in self.files:
            raise self._missing(path)
        return _FileInfo(path=path, size=len(self.files[path].encode()), mode=0o644, is_dir=False, modified_unix_ns=0)

    def read_bytes(self, path):
        return self._rpc.read_file(lambda: self._read_bytes(path))

    def _read_bytes(self, path):
        self._session.calls.append(('read_bytes', path))
        self._enter('read_bytes')
        if path not in self.files:
            raise self._missing(path)
        return self.files[path].encode()

    def list(self, path, *, include_hidden=False):
        return self._rpc.list(lambda: self._list(path, include_hidden))

    def _list(self, path, include_hidden):
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
        return self._rpc.mkdir(lambda: self._mkdir(path, recursive))

    def _mkdir(self, path, recursive):
        self._session.calls.append(('mkdir', path))
        self._enter('mkdir')
        if not recursive and posixpath.dirname(path) not in self.dirs:
            raise self._missing(path)
        while path not in self.dirs:
            self.dirs.add(path)
            path = posixpath.dirname(path)

    def remove(self, path, *, recursive=True):
        return self._rpc.remove(lambda: self._remove(path, recursive))

    def _remove(self, path, recursive):
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
    disk: object | None = None  # tenki.DiskUsage; the node never reads it

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


# The bounded no-op command the node runs on a session before its first use after a create or a resume (see
# IGlobal._warm_up). Kept out of _execs, which counts the commands the tools themselves ran.
_WARM_UP = ('true',)


def _execs(session):
    """The (argv, kwargs) of every process a tool started on a fake session (the node's warm-up excluded)."""
    return [
        (call[1], call[2])
        for call in session.calls
        if isinstance(call, tuple) and call[0] == 'start' and call[1] != _WARM_UP
    ]


def _warm_ups(session):
    """How many times the node ran its no-op command on a fake session."""
    return sum(1 for call in session.calls if isinstance(call, tuple) and call[0] == 'start' and call[1] == _WARM_UP)


def _without_warm_ups(calls):
    """A fake session's call log without the node's no-op commands (their start, close_stdin and wait)."""
    kept, skipping = [], 0
    for call in calls:
        if isinstance(call, tuple) and call[0] == 'start' and call[1] == _WARM_UP:
            skipping = 2
            continue
        if skipping and isinstance(call, tuple) and call[0] in ('close_stdin', 'wait', 'kill'):
            skipping -= call[0] != 'kill'
            continue
        skipping = 0
        kept.append(call)
    return kept


def _first_data_call(session):
    """The first call on a fake session that is not part of the warm-up or the lifecycle."""
    for call in session.calls:
        if (
            isinstance(call, tuple)
            and not (call[0] == 'start' and call[1] == _WARM_UP)
            and call[0] not in ('close_stdin', 'wait', 'kill')
        ):
            return call
    return None


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
        ('pause_retention_hours', 0, 1),  # Tenki refuses a retention of 0 ("must be positive")
        ('pause_retention_hours', 100000, 8760),  # a year, the longest value measured as accepted
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
        glb.pause_retention_hours,
    )
    assert settings == (2, 4096, 5, 60, 120, 50000, 24)


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
            'pause_retention': 24 * 3600,
            'allow_inbound': False,
        }
    ]


def test_create_sends_the_default_lifetime_cap_of_sixty_minutes(monkeypatch, logs):
    # Tenki pauses a session at this cap rather than ending it (measured), and since #2396 the engine runs
    # teardown for chat and webhook pipelines too. So a short cap no longer buys much cost safety, while
    # every pause cuts off a running command and costs about a minute to resume. 60 minutes is the
    # backstop for a session the pipeline never shut down (an engine crash).
    glb, client = _started(monkeypatch, _FakeSession('sb-1'))
    glb.get_session()
    [call] = client.create_calls
    assert call['max_duration'] == 60 * 60


def test_create_sends_the_default_pause_retention_of_a_day(monkeypatch, logs):
    # Tenki's default keeps a paused session's state for seven days (measured). A session the pipeline
    # left behind should not hold its code and files that long.
    glb, client = _started(monkeypatch, _FakeSession('sb-1'))
    glb.get_session()
    [call] = client.create_calls
    assert call['pause_retention'] == 24 * 3600


def test_create_sends_the_configured_pause_retention_in_seconds(monkeypatch, logs):
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg={'pause_retention_hours': 2})
    glb.get_session()
    [call] = client.create_calls
    assert call['pause_retention'] == 7200


@pytest.mark.parametrize(
    ('exec_timeout', 'minutes', 'warns'), [(120, 60, False), (60, 1, True), (61, 1, True), (59, 1, False)]
)
def test_a_command_that_could_outlive_the_session_is_warned_about_at_startup(
    monkeypatch, logs, exec_timeout, minutes, warns
):
    # Tenki pauses the session at max_duration and the pause cuts a running command off. A command allowed
    # to run that long can never finish, so the operator is told when the pipeline starts.
    cfg = {'apikey': _KEY, 'exec_timeout_secs': exec_timeout, 'max_duration_minutes': minutes}
    glb, _ = _make_global(monkeypatch, cfg)
    glb.beginGlobal()
    assert any('can never finish' in line for line in logs) is warns


def test_validate_config_warns_about_a_command_that_could_outlive_the_session(monkeypatch, logs):
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, 'exec_timeout_secs': 600, 'max_duration_minutes': 5})
    glb.validateConfig()
    assert any('can never finish' in line and 'exec_timeout_secs' in line for line in logs)


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


def test_create_attaches_the_configured_secret_policies(monkeypatch, logs):
    # Only policy names go to Tenki. It keeps the secret values and substitutes them outside the VM,
    # into requests a policy allows, so the sandbox only ever sees placeholders.
    cfg = {'secret_policies': [' github ', 'internal-api', 'github'], 'allow_domains': ['github.com']}
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg=cfg)
    glb.get_session()
    [kwargs] = client.create_calls
    assert kwargs['secret_policies'] == ['github', 'internal-api']
    assert kwargs['allow_domains'] == ['github.com']


@pytest.mark.parametrize('domains', [None, [], '', ' , ', ['  ']])
def test_secret_policies_without_an_outbound_allowlist_stop_the_pipeline_at_startup(monkeypatch, logs, domains):
    # Fail closed. A policy decides which requests get the credential, not where the sandbox may send
    # what those requests return, so a sandbox that can use a secret must not have every destination open.
    cfg = {'apikey': _KEY, 'secret_policies': ['github']}
    if domains is not None:
        cfg['allow_domains'] = domains
    glb, _ = _make_global(monkeypatch, cfg)
    with pytest.raises(ValueError) as refused:
        glb.beginGlobal()
    assert 'tenki.secret_policies' in str(refused.value) and 'tenki.allow_domains' in str(refused.value)


def test_validate_config_warns_about_secret_policies_without_an_allowlist(monkeypatch, logs):
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, 'secret_policies': ['github']})
    glb.validateConfig()
    assert any('tenki.allow_domains' in message and 'fail to start' in message for message in logs)


def test_an_allowlist_alone_needs_no_secret_policy(monkeypatch, logs):
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg={'allow_domains': ['pypi.org']})
    glb.get_session()
    [kwargs] = client.create_calls
    assert kwargs['allow_domains'] == ['pypi.org'] and 'secret_policies' not in kwargs


def test_secret_files_and_overrides_are_never_sent(monkeypatch, logs):
    # Tenki's other ways of delivering a secret put the real value inside the VM, where an agent with
    # passwordless sudo can read it. Whatever a pipeline's config carries, the node sends none of them.
    cfg = {
        'secret_policies': ['github'],
        'allow_domains': ['github.com'],
        'secret_files': [{'path': '/app/.env', 'content': 'TOKEN=secrets://GITHUB_TOKEN'}],
        'secret_overrides': {'GITHUB_TOKEN': 'OTHER_TOKEN'},
        'setup_secrets': {'GITHUB_TOKEN': 'x'},
        'env': {'GITHUB_TOKEN': 'x'},
        'github_token': 'x',
    }
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg=cfg)
    glb.get_session()
    [kwargs] = client.create_calls
    assert not {'secret_files', 'secret_overrides', 'setup_secrets', 'env', 'github_token', 'clone_repo_url'} & set(
        kwargs
    )


@pytest.mark.parametrize(
    ('cfg', 'extra'),
    [
        ({}, set()),
        ({'allow_domains': ['pypi.org']}, {'allow_domains'}),
        ({'secret_policies': ['github'], 'allow_domains': ['github.com']}, {'secret_policies', 'allow_domains'}),
    ],
)
def test_the_create_request_carries_exactly_the_options_the_node_means_to_send(monkeypatch, logs, cfg, extra):
    # Not "none of the options known to be dangerous": none at all beyond these. A new SDK option
    # that opens the VM up (a tailnet, SSH keys, extra network ranges) must be a decision, not a default.
    cfg = {**cfg, 'direct_runtime': True, 'tailnet': 'x', 'ssh_authorized_keys': ['k'], 'allow_cidrs': ['0.0.0.0/0']}
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg=cfg)
    glb.get_session()
    [kwargs] = client.create_calls
    assert (
        set(kwargs)
        == {
            'cpu_cores',
            'memory_mb',
            'disk_size_gb',
            'max_duration',
            'allow_inbound',
            'name',
            'tags',
            'metadata',
            'pause_retention',
        }
        | extra
    )


def test_a_replacement_session_gets_the_same_policies_and_allowlist(monkeypatch, logs):
    # A session that ended is replaced by a new create. If that one dropped the allowlist, the agent
    # would be handed an open network the first time anything went wrong.
    cfg = {'secret_policies': ['github'], 'allow_domains': ['github.com']}
    dead = _FakeSession('sb-dead', state='TERMINATED', exec_error=mod.SessionTerminatedError('session_terminated'))
    inst, client = _instance(monkeypatch, dead, _FakeSession('sb-fresh'), cfg=cfg)
    assert inst.run_command({'command': 'ls'})['session'] == 'replaced'
    first, second = client.create_calls
    assert second['secret_policies'] == first['secret_policies'] == ['github']
    assert second['allow_domains'] == first['allow_domains'] == ['github.com']


@pytest.mark.parametrize('field', ['secret_policies', 'allow_domains'])
@pytest.mark.parametrize('value', [{'github.com': True}, 5, True, 1.5])
def test_a_list_setting_given_as_something_else_stops_the_pipeline_at_startup(monkeypatch, logs, field, value):
    # Read as "nothing configured", an allowlist typed in the wrong shape would start the sandbox with
    # every destination open: the outcome the setting exists to prevent.
    cfg = {'apikey': _KEY, 'secret_policies': ['github'], 'allow_domains': ['github.com'], field: value}
    glb, _ = _make_global(monkeypatch, cfg)
    with pytest.raises(ValueError) as refused:
        glb.beginGlobal()
    assert f'tenki.{field}' in str(refused.value) and 'list' in str(refused.value)


@pytest.mark.parametrize('field', ['secret_policies', 'allow_domains'])
@pytest.mark.parametrize('entry', [None, 5, ['a', 'b'], b'github.com', {'name': 'x'}])
def test_a_list_entry_that_is_not_text_stops_the_pipeline_at_startup(monkeypatch, logs, field, entry):
    # Turned into text, these would reach Tenki as names nobody typed ("None", "['a', 'b']").
    cfg = {'apikey': _KEY, 'secret_policies': ['github'], 'allow_domains': ['github.com']}
    cfg[field] = [*cfg[field], entry]
    glb, _ = _make_global(monkeypatch, cfg)
    with pytest.raises(ValueError) as refused:
        glb.beginGlobal()
    assert f'tenki.{field}' in str(refused.value)


def test_more_domains_than_tenki_accepts_stop_the_pipeline_at_startup(monkeypatch, logs):
    # Tenki documents a limit of 256 domains. Past it the create call would fail on the first tool
    # call instead, long after the pipeline had started.
    domains = [f'host-{n}.example.com' for n in range(257)]
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, 'allow_domains': domains})
    with pytest.raises(ValueError) as refused:
        glb.beginGlobal()
    assert '256' in str(refused.value)
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, 'allow_domains': domains[:256]})
    glb.beginGlobal()
    assert len(glb.allow_domains) == 256


def test_a_refused_domain_that_carries_credentials_is_not_repeated_in_the_error(monkeypatch, logs):
    # A URL with a token pasted into the wrong field must not travel on into the job log.
    cfg = {'apikey': _KEY, 'allow_domains': ['pypi.org', 'https://user:t0k3n-value@host.example/path']}
    glb, _ = _make_global(monkeypatch, cfg)
    with pytest.raises(ValueError) as refused:
        glb.beginGlobal()
    assert 't0k3n-value' not in str(refused.value) and 'entry 2' in str(refused.value)


def test_the_editor_offers_secret_policies_and_the_allowlist_as_lists_of_names():
    # Both are plain lists of names. Neither is a secret field: a policy name is not a credential.
    services = json.loads((_NODES_SRC / 'nodes' / 'tool_tenki' / 'services.json').read_text())
    shown = next(shape['properties'] for shape in services['shape'] if shape['section'] == 'Pipe')
    for name in ('tenki.secret_policies', 'tenki.allow_domains'):
        field = services['fields'][name]
        assert field['type'] == 'array' and field['items'] == {'type': 'string'} and field['default'] == [], name
        assert not field.get('secure'), name
        assert name in shown, name


def test_the_editor_offers_no_way_to_put_a_secret_value_in_the_vm():
    services = json.loads((_NODES_SRC / 'nodes' / 'tool_tenki' / 'services.json').read_text())
    offered = ' '.join(services['fields']).lower()
    assert 'secret_files' not in offered and 'secret_overrides' not in offered and 'setup_secrets' not in offered


def test_a_key_without_secrets_access_is_reported_as_a_setup_problem(monkeypatch, logs):
    # What Tenki answers (measured with tenki 1.4.0) when a session names a policy and the API key was
    # created without Secrets access. Nothing an agent does can fix it, so the result has to say whose fix it is.
    denied = inst_mod.PermissionDeniedError('API key requires Secrets access; create a key with Secrets access enabled')
    cfg = {'secret_policies': ['policy-name-in-config'], 'allow_domains': ['github.com']}
    inst, _ = _instance(monkeypatch, cfg=cfg, create_error=denied)
    result = inst.run_command({'command': 'ls'})
    assert result['exit_code'] == -1
    assert 'Secrets access' in result['error'] and 'tenki.secret_policies' in result['error']
    assert 'operator' in result['error'] and 'will not help' in result['error']
    # The agent has no use for what the operator typed into that field, and it may not be a name at all.
    assert 'policy-name-in-config' not in result['error']


def test_another_permission_error_is_not_blamed_on_secrets_access(monkeypatch, logs):
    # Policies are configured, but this refusal is about something else. Telling the operator to fix
    # the key's Secrets access would send them the wrong way.
    denied = inst_mod.PermissionDeniedError('workspace suspended')
    cfg = {'secret_policies': ['github'], 'allow_domains': ['github.com']}
    inst, _ = _instance(monkeypatch, cfg=cfg, create_error=denied)
    assert inst.run_command({'command': 'ls'})['error'] == 'workspace suspended'


def test_a_permission_error_without_secret_policies_is_reported_as_it_is(monkeypatch, logs):
    denied = inst_mod.PermissionDeniedError('workspace suspended')
    inst, _ = _instance(monkeypatch, create_error=denied)
    assert inst.run_command({'command': 'ls'})['error'] == 'workspace suspended'


def test_create_restricts_outbound_traffic_to_the_configured_domains(monkeypatch, logs):
    cfg = {'allow_domains': 'PyPI.org., files.pythonhosted.org, *.github.com, pypi.org'}
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg=cfg)
    glb.get_session()
    [kwargs] = client.create_calls
    assert kwargs['allow_domains'] == ['pypi.org', 'files.pythonhosted.org', '*.github.com']


@pytest.mark.parametrize(
    'cfg', [{}, {'secret_policies': [], 'allow_domains': []}, {'secret_policies': ['  '], 'allow_domains': ' , '}]
)
def test_unset_secret_policies_and_domains_leave_the_create_request_as_before(monkeypatch, logs, cfg):
    # No policies and no allowlist: outbound traffic stays open, exactly as before these options existed.
    glb, client = _started(monkeypatch, _FakeSession('sb-1'), cfg=cfg)
    glb.get_session()
    [kwargs] = client.create_calls
    assert 'secret_policies' not in kwargs
    assert 'allow_domains' not in kwargs


@pytest.mark.parametrize(
    'domain',
    [
        'https://pypi.org',
        'pypi.org/simple',
        'pypi.org:443',
        'py pi.org',
        'a..b.com',
        '-pypi.org',
        '*.*.example.com',
        'example.*',
        'localhost',
        '10.0.0.1',
        'a' * 64 + '.example.com',
        '.'.join(['a' * 60] * 5),
    ],
)
def test_an_outbound_domain_the_node_does_not_accept_stops_the_pipeline_at_startup(monkeypatch, logs, domain):
    # Dropping it would be worse than failing: had every entry been dropped, the empty list left
    # would mean no allowlist at all, a wider sandbox than the operator asked for.
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, 'allow_domains': ['pypi.org', domain]})
    with pytest.raises(ValueError) as refused:
        glb.beginGlobal()
    assert domain in str(refused.value)


def test_validate_config_warns_about_an_outbound_domain_the_node_does_not_accept(monkeypatch, logs):
    glb, _ = _make_global(monkeypatch, {'apikey': _KEY, 'allow_domains': ['https://pypi.org']})
    glb.validateConfig()
    assert any('https://pypi.org' in message for message in logs)


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
        on_event=reported.append,
    )
    assert outcome == 'ok'
    assert reported == ['created', 'replaced']
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
    paused = _FakeSession('sb-1')
    glb, client = _started(monkeypatch, paused)
    glb.call_with_session(lambda session: None)  # first use: the session is created and warmed
    paused.state = 'PAUSED'
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


@pytest.mark.parametrize(
    'error', [_StubSnapshotFailedError('snapshot failed'), _StubSnapshotWaitTimeoutError('snapshot wait')]
)
def test_an_sdk_error_that_is_also_a_plain_error_keeps_its_own_type_in_a_call(monkeypatch, logs, error):
    # Since tenki 1.4.1 two SDK errors also subclass RuntimeError or TimeoutError. The node turns a PLAIN one of those into
    # SessionUnavailableError; an SDK error must come back as the SDK error it is, like every other.
    session = _FakeSession('sb-1', fail={'stat': error})
    inst, _ = _instance(monkeypatch, session)
    glb = inst.IGlobal
    with pytest.raises(type(error)):
        glb.call_with_session(lambda s: s.fs.stat('/home/tenki/a.txt'))
    assert not any('sandbox call failed' in line for line in logs)


@pytest.mark.parametrize(
    'error', [_StubSnapshotFailedError('snapshot failed'), _StubSnapshotWaitTimeoutError('snapshot wait')]
)
def test_an_sdk_error_that_is_also_a_plain_error_keeps_its_own_type_at_create(monkeypatch, logs, error):
    inst, _ = _instance(monkeypatch, create_error=error)
    assert inst.run_command({'command': 'ls'})['error'] == str(error)


def test_an_sdk_error_that_is_also_a_plain_error_keeps_its_own_type_while_resuming(monkeypatch, logs):
    error = _StubSnapshotWaitTimeoutError('snapshot wait')
    session = _FakeSession('sb-1', state='PAUSED', ready_error=error, files={'/home/tenki/notes.txt': 'kept'})
    inst, _ = _instance(monkeypatch, session)
    assert inst.read_file({'path': 'notes.txt'})['error'] == 'snapshot wait'


def test_an_sdk_error_that_is_also_a_timeout_is_not_taken_for_a_command_that_never_started(monkeypatch, logs):
    error = _StubSnapshotWaitTimeoutError('snapshot wait')
    session = _FakeSession('sb-1')
    start = session.start

    def start_then_fail(*argv, **kwargs):
        process = start(*argv, **kwargs)
        if argv != _WARM_UP:
            process.close_stdin = lambda: _raise(error)
        return process

    session.start = start_then_fail
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'ls'})
    assert result['error'] == 'snapshot wait' and 'did not start' not in result['error']


def test_an_sdk_error_that_is_also_a_timeout_is_not_taken_for_a_missing_result(monkeypatch, logs):
    session = _FakeSession('sb-1', run_error=_StubSnapshotWaitTimeoutError('snapshot wait'))
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'ls'})
    assert 'snapshot wait' in result['error'] and 'no result within' not in result['error']


def test_other_errors_propagate_without_a_state_check(monkeypatch, logs):
    running = _FakeSession('sb-1')
    glb, _ = _started(monkeypatch, running)
    with pytest.raises(ValueError):
        glb.call_with_session(lambda session: _raise(ValueError('empty command')))
    assert _without_warm_ups(running.calls) == []


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
    field = services['fields']['tenki.max_duration_minutes']
    assert 'pauses' in field['description']
    assert 'once a session ends' not in field['description']
    assert field['default'] == 60


def test_the_editor_offers_pause_retention_in_hours_with_the_measured_bounds():
    services = json.loads((_NODES_SRC / 'nodes' / 'tool_tenki' / 'services.json').read_text())
    field = services['fields']['tenki.pause_retention_hours']
    assert (field['type'], field['default'], field['minimum'], field['maximum']) == ('integer', 24, 1, 8760)
    shown = next(shape['properties'] for shape in services['shape'] if shape['section'] == 'Pipe')
    assert shown.index('tenki.pause_retention_hours') == shown.index('tenki.max_duration_minutes') + 1


_DENIED = "fatal: unable to access 'https://github.com/acme/private.git/': 403 secret_injection_denied"


def test_a_denied_secret_injection_in_a_commands_output_is_explained(monkeypatch, logs):
    # Tenki answers 403 secret_injection_denied when no attached policy allows the placeholder a request
    # carried. An agent that only sees a 403 retries; it has to be told that repeating will not help.
    failed = _CommandResult(argv=['bash'], exit_code=128, stderr=_DENIED.encode())
    inst, _ = _instance(monkeypatch, _FakeSession('sb-1', exec_result=failed))
    result = inst.run_command({'command': 'git clone https://github.com/acme/private.git'})
    assert result['exit_code'] == 128 and result['stderr'].startswith(_DENIED)
    assert 'no attached secret policy allows' in result['stderr'] and 'will not help' in result['stderr']


def test_output_without_a_denied_injection_is_left_alone(monkeypatch, logs):
    plain = _CommandResult(argv=['bash'], exit_code=22, stderr=b'curl: (22) The requested URL returned error: 403')
    inst, _ = _instance(monkeypatch, _FakeSession('sb-1', exec_result=plain))
    assert (
        inst.run_command({'command': 'curl -f https://example.com'})['stderr']
        == 'curl: (22) The requested URL returned error: 403'
    )


def test_a_denied_secret_injection_in_a_git_error_is_explained(monkeypatch, logs):
    refused = mod.SandboxError(_DENIED)
    inst, _ = _instance(monkeypatch, _FakeSession('sb-1', fail={'clone': refused}))
    result = inst.git_clone({'repo': _REPO})
    assert result['error'].startswith(_DENIED)
    assert 'no attached secret policy allows' in result['error'] and 'will not help' in result['error']


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
        'session': 'created',
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
        'session': 'created',
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
    written, executed, removed = [
        call for call in _without_warm_ups(session.calls) if call[0] in ('write_text', 'start', 'remove')
    ]
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
        'session': 'created',
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
    assert inst.make_directory({'path': 'data/raw'}) == {
        'success': True,
        'path': '/home/tenki/data/raw',
        'session': 'created',
    }
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
        'session': 'created',
    }


def test_read_file_checks_the_size_then_reads_through_unary_calls_sent_with_a_deadline(monkeypatch, logs):
    # The SDK's streaming read cannot be given a deadline, so a half-open connection would hang it
    # for good. stat and read_bytes are unary calls, and the node sends each with one.
    session = _FakeSession('sb-1', files={'/home/tenki/notes.md': 'x'})
    inst, _ = _instance(monkeypatch, session)
    inst.read_file({'path': 'notes.md'})
    assert [call for call in session.calls if call[0] in ('stat', 'read_bytes')] == [
        ('stat', '/home/tenki/notes.md'),
        ('read_bytes', '/home/tenki/notes.md'),
    ]
    assert session.fs.deadlines == [('stat', 120), ('read_file', 120)]  # the default execution timeout


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
        'session': 'created',
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
    assert inst.delete_path({'path': 'build'}) == {'success': True, 'path': '/home/tenki/build', 'session': 'created'}
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

    events = []
    assert glb.call_with_session(call, on_event=events.append) == 'ok'
    assert events == ['created', 'replaced'], events


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


def test_every_field_the_editor_offers_is_read_by_the_node():
    # The engine strips the "tenki." prefix before IGlobal sees the config. A field whose key the
    # node never reads is a setting that silently does nothing, which unit tests passing their own
    # config dicts would not notice.
    services = json.loads((_NODES_SRC / 'nodes' / 'tool_tenki' / 'services.json').read_text())
    source = (_NODES_SRC / 'nodes' / 'tool_tenki' / 'IGlobal.py').read_text()
    keys = [name.split('.', 1)[1] for name in services['fields']]
    assert [key for key in keys if f"cfg.get('{key}')" not in source] == []


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
        'session': 'created',
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
    assert inst.git_checkout(args) == {
        'output': "Switched to branch 'main'\n",
        'truncated': False,
        'session': 'created',
    }
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
    assert inst.git_diff({}) == {'output': '+' * 1000, 'truncated': True, 'session': 'created'}


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
    assert inst.git_log(args) == {'output': 'commit abc123\n', 'truncated': False, 'session': 'created'}
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


@pytest.mark.parametrize(('exec_timeout', 'deadline'), [(45, 60), (300, 300)])
def test_every_file_call_is_sent_with_a_deadline(monkeypatch, logs, exec_timeout, deadline):
    # The SDK applies the client's deadline to control-plane calls only. A session's file calls go
    # to its data plane, where the SDK passes none (tenki 1.0.6 and 1.4.0), and its file API takes
    # no timeout: left alone, one stalled connection hangs the tool call for good.
    session = _FakeSession('sb-1', files={'/home/tenki/notes.md': 'x'})
    inst, _ = _instance(monkeypatch, session, cfg={'exec_timeout_secs': exec_timeout})
    assert inst.write_file({'path': 'new/deep/a.txt', 'content': 'a'})['success'] is True
    assert inst.read_file({'path': 'notes.md'})['content'] == 'x'
    assert 'error' not in inst.list_files({'path': '.'})
    assert inst.make_directory({'path': 'made'})['success'] is True
    assert inst.delete_path({'path': 'made'})['success'] is True
    assert inst.run_code({'code': 'print(1)'})['exit_code'] == 0
    sent = session.fs.deadlines
    assert {name for name, _ in sent} == {'write_file', 'mkdir', 'stat', 'read_file', 'list', 'remove'}
    assert {timeout for _, timeout in sent} == {deadline}


def test_a_session_whose_file_calls_cannot_be_given_a_deadline_is_closed_instead_of_used(monkeypatch, logs):
    # The deadline goes on a private part of the SDK. If a release moves it, the node must not carry
    # on with file calls that can hang for good, and must not leave the VM it just created billing.
    session = _FakeSession('sb-1')
    del session.fs._rpc
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'true'})
    assert 'deadline on file calls' in result['error']
    assert session.calls == ['close']
    assert inst.IGlobal.session is None
    assert any('deadline on file calls' in line for line in logs)


def test_a_session_that_could_not_be_bounded_and_will_not_close_is_still_not_used(monkeypatch, logs):
    session = _FakeSession('sb-1', close_error=mod.SandboxError('close refused'))
    del session.fs._rpc
    inst, _ = _instance(monkeypatch, session)
    assert 'deadline on file calls' in inst.run_command({'command': 'true'})['error']
    assert inst.IGlobal.session is None
    assert any('close refused' in line for line in logs)


@pytest.mark.parametrize(('tool', 'args'), [('run_command', {'command': 'sleep 999'}), ('run_code', {'code': 'x'})])
def test_a_stalled_command_stream_is_cut_off_and_reported_as_a_timeout(monkeypatch, logs, tool, args):
    # Sandbox.exec waits on the output stream with no local limit, so a stream that stalls would
    # hang the tool call forever. The local limit leaves room for Tenki's own timeout report first.
    session = _FakeSession('sb-1', stalls='result')
    inst, _ = _instance(monkeypatch, session, cfg={'exec_timeout_secs': 45})
    result = getattr(inst, tool)(args)
    assert result['timed_out'] is True
    assert result['exit_code'] == -1
    [(_, waited)] = [call for call in _without_warm_ups(session.calls) if call[0] == 'wait']
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


# What one read on a session Tenki had paused raised on SDK 1.4.0 (measured 2026-10-05, 2 seconds after
# PAUSED). The text is about the VM's socket on Tenki's host, but it ends in "no such file or directory",
# which is all the SDK goes by, so it arrives as the SDK's file-not-found error.
_VSOCK_AS_A_MISSING_FILE = (
    'read file failed: failed to read vsock CONNECT response: EOF (fallback exec: failed to connect to '
    'vsock at /opt/cloud-hypervisor/data/instances/sandbox/sb-1/rootfs.img.vsock: dial unix /opt/'
    'cloud-hypervisor/data/instances/sandbox/sb-1/rootfs.img.vsock: connect: no such file or directory)'
)


@pytest.mark.parametrize(
    ('tool', 'args', 'key', 'expected'),
    [
        ('read_file', {'path': 'notes.txt'}, 'content', 'kept'),
        ('write_file', {'path': 'new.txt', 'content': 'x'}, 'success', True),
        ('list_files', {'path': '.'}, 'truncated', False),
        ('make_directory', {'path': 'made'}, 'success', True),
        ('delete_path', {'path': 'notes.txt'}, 'success', True),
    ],
)
def test_a_pause_the_sdk_reports_as_a_missing_file_is_still_resumed(monkeypatch, logs, tool, args, key, expected):
    # Telling the agent "no such file" about a file that is there, on a session left paused, is the
    # worst answer available: it would recreate work that was never lost.
    session = _FakeSession(
        'sb-1',
        state='PAUSED',
        paused_error=inst_mod.TenkiFileNotFoundError(_VSOCK_AS_A_MISSING_FILE),
        files={'/home/tenki/notes.txt': 'kept'},
    )
    inst, client = _instance(monkeypatch, session)
    result = getattr(inst, tool)(args)
    assert 'error' not in result, result
    assert result[key] == expected and result['session'] == 'resumed'
    assert session.calls.count('resume') == 1 and len(client.create_calls) == 1


@pytest.mark.parametrize(
    ('tool', 'args', 'operation'),
    [
        ('read_file', {'path': 'notes.txt'}, 'stat'),
        ('list_files', {'path': '.'}, 'list'),
        ('delete_path', {'path': 'notes.txt'}, 'remove'),
        ('make_directory', {'path': 'made'}, 'mkdir'),
    ],
)
def test_a_missing_file_error_about_the_vm_link_is_not_reported_as_a_missing_path(
    monkeypatch, logs, tool, args, operation
):
    # On a session that turns out to be running there is nothing to recover, and the error is still
    # not about the agent's path: it gets the real text, once, and the call is not repeated.
    session = _FakeSession(
        'sb-1',
        files={'/home/tenki/notes.txt': 'kept'},
        fail={operation: inst_mod.TenkiFileNotFoundError(_VSOCK_AS_A_MISSING_FILE)},
    )
    inst, _ = _instance(monkeypatch, session)
    error = getattr(inst, tool)(args)['error']
    assert 'vsock' in error and not error.startswith('no such file or directory: ')
    assert session.calls.count('refresh') == 1 and 'resume' not in session.calls
    assert sum(1 for call in session.calls if call[0] == operation) == 1


def test_a_missing_file_whose_own_path_contains_vsock_is_still_just_a_missing_file(monkeypatch, logs):
    # The SDK builds stat's message from the path it was asked for. A file that really is missing
    # must not cost a look at the session because of what the agent called it.
    missing = inst_mod.TenkiFileNotFoundError('no such file or directory: /home/tenki/vsock_test.py')
    session = _FakeSession('sb-1', fail={'stat': missing})
    inst, _ = _instance(monkeypatch, session)
    assert inst.read_file({'path': 'vsock_test.py'})['error'] == 'no such file or directory: /home/tenki/vsock_test.py'
    assert 'refresh' not in session.calls


def test_write_file_still_creates_a_missing_parent_whose_name_contains_vsock(monkeypatch, logs):
    # A guest that names the path in its error must not turn the agent's own folder name into
    # "the VM link is down": the parent is created and the file written, as for any other name.
    session = _FakeSession('sb-1')
    session.fs._missing = lambda path: inst_mod.TenkiFileNotFoundError(f'open {path}: no such file or directory')
    inst, _ = _instance(monkeypatch, session)
    result = inst.write_file({'path': 'vsock-notes/rootfs.img.vsock.txt', 'content': 'x'})
    assert result == {'success': True, 'path': '/home/tenki/vsock-notes/rootfs.img.vsock.txt', 'session': 'created'}
    assert 'refresh' not in session.calls


def test_write_file_does_not_take_an_error_about_the_vm_link_for_a_missing_parent_directory(monkeypatch, logs):
    # A missing parent is answered by creating it and writing again. This error only looks like one.
    session = _FakeSession('sb-1', fail={'write_text': inst_mod.TenkiFileNotFoundError(_VSOCK_AS_A_MISSING_FILE)})
    inst, _ = _instance(monkeypatch, session)
    result = inst.write_file({'path': 'new.txt', 'content': 'x'})
    assert result['success'] is False and 'vsock' in result['error']
    assert not [call for call in session.calls if call[0] == 'mkdir']
    assert sum(1 for call in session.calls if call[0] == 'write_text') == 1


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


class _Clock:
    """In place of the time module in IGlobal: a clock that only moves when told to, or slept on."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _refusing(session, clock, seconds_per_ask=10):
    """Make each resume of ``session`` take ``seconds_per_ask`` on ``clock``, as a refusal does (measured: about 10)."""
    resume = session.resume

    def slow_resume():
        clock.now += seconds_per_ask
        resume()

    session.resume = slow_resume


def test_the_resume_budget_runs_from_the_first_ask_and_is_not_renewed_by_each_one(monkeypatch, logs):
    # 90 seconds in all, asking every 5: with refusals that take 10 seconds each, the asks end at
    # 10, 25, 40, 55, 70, 85 and 100 seconds, and the seventh is the last.
    clock = _Clock()
    monkeypatch.setattr(mod, 'time', clock)
    session = _FakeSession('sb-1', state='PAUSED', resume_refusals=1000)
    _refusing(session, clock)
    inst, _ = _instance(monkeypatch, session)
    result = inst.read_file({'path': 'notes.txt'})
    assert 'still being torn down' in result['error']
    assert session.calls.count('resume') == 7
    assert clock.sleeps == [5] * 6
    assert clock.now - 1000.0 == 100


def test_calls_queued_behind_a_refused_resume_share_its_budget(monkeypatch, logs):
    # Parallel calls wait on the session lock while one of them resumes. If each then started a
    # budget of its own, six calls would take nine minutes to fail, with shutdown waiting behind them.
    clock = _Clock()
    monkeypatch.setattr(mod, 'time', clock)
    session = _FakeSession('sb-1', state='PAUSED', resume_refusals=1000, files={'/home/tenki/notes.txt': 'kept'})
    _refusing(session, clock)
    inst, _ = _instance(monkeypatch, session)
    assert 'still being torn down' in inst.read_file({'path': 'notes.txt'})['error']
    spent = session.calls.count('resume')
    # The next call still asks, once: Tenki may have finished in the meantime.
    assert 'still being torn down' in inst.read_file({'path': 'notes.txt'})['error']
    assert session.calls.count('resume') == spent + 1
    session._resume_refusals = 0
    assert inst.read_file({'path': 'notes.txt'})['session'] == 'resumed'


def test_the_next_pause_gets_a_resume_budget_of_its_own(monkeypatch, logs):
    clock = _Clock()
    monkeypatch.setattr(mod, 'time', clock)
    session = _FakeSession('sb-1', state='PAUSED', resume_refusals=1000, files={'/home/tenki/notes.txt': 'kept'})
    _refusing(session, clock)
    inst, _ = _instance(monkeypatch, session)
    assert 'still being torn down' in inst.read_file({'path': 'notes.txt'})['error']
    session._resume_refusals = 0
    assert inst.read_file({'path': 'notes.txt'})['session'] == 'resumed'
    # Hours later the session reaches its lifetime again, and Tenki refuses twice before it resumes.
    clock.now += 7200
    session.state, session._resume_refusals = 'PAUSED', 2
    asked = session.calls.count('resume')
    result = inst.read_file({'path': 'notes.txt'})
    assert result.get('content') == 'kept' and result['session'] == 'resumed', result
    assert session.calls.count('resume') == asked + 3


def test_a_resume_that_took_ends_its_budget_so_a_pause_soon_after_starts_a_new_one(monkeypatch, logs):
    # With a one minute lifetime the next pause comes about a minute after the last resume, well
    # within a budget's length of the old one. It must not inherit what is left of that.
    clock = _Clock()
    monkeypatch.setattr(mod, 'time', clock)
    session = _FakeSession('sb-1', state='PAUSED', resume_refusals=5, files={'/home/tenki/notes.txt': 'kept'})
    _refusing(session, clock)
    inst, _ = _instance(monkeypatch, session)
    assert inst.read_file({'path': 'notes.txt'})['session'] == 'resumed'  # after 85 of the 90 seconds
    clock.now += 60
    session.state, session._resume_refusals = 'PAUSED', 3
    result = inst.read_file({'path': 'notes.txt'})
    assert result.get('content') == 'kept' and result['session'] == 'resumed', result


def test_a_call_long_after_a_spent_budget_gets_a_budget_of_its_own(monkeypatch, logs):
    # Sharing is for the calls that were waiting behind the one that gave up. A call that comes later
    # is a new attempt, and waits like the first did.
    clock = _Clock()
    monkeypatch.setattr(mod, 'time', clock)
    session = _FakeSession('sb-1', state='PAUSED', resume_refusals=1000)
    _refusing(session, clock)
    inst, _ = _instance(monkeypatch, session)
    assert 'still being torn down' in inst.read_file({'path': 'notes.txt'})['error']
    spent = session.calls.count('resume')
    clock.now += 70  # the budget ran out 80 seconds ago by now: still within a budget's length of it
    assert 'still being torn down' in inst.read_file({'path': 'notes.txt'})['error']
    assert session.calls.count('resume') == spent + 1
    clock.now += 90
    assert 'still being torn down' in inst.read_file({'path': 'notes.txt'})['error']
    assert session.calls.count('resume') == spent + 1 + 7


def test_a_resume_refused_for_a_named_reason_ends_the_budget_too(monkeypatch, logs):
    # Otherwise what is left of it would be all the next pause gets: one ask, where three refusals
    # are normal.
    clock = _Clock()
    monkeypatch.setattr(mod, 'time', clock)
    session = _FakeSession('sb-1', state='PAUSED', files={'/home/tenki/notes.txt': 'kept'})
    answers = [mod.SandboxError(_NOT_YET), inst_mod.PermissionDeniedError('workspace suspended')]
    resume = session.resume

    def refused_then_named():
        clock.now += 10
        if answers:
            session.calls.append('resume')
            raise answers.pop(0)
        resume()

    session.resume = refused_then_named
    inst, _ = _instance(monkeypatch, session)
    assert inst.run_command({'command': 'ls'})['error'] == 'workspace suspended'
    # The workspace is restored and the session resumed from Tenki's console, not through the node.
    session.state = 'RUNNING'
    assert 'session' not in inst.read_file({'path': 'notes.txt'})
    # 100 seconds after the first ask it pauses again.
    clock.now = 1100.0
    session.state, session._resume_refusals = 'PAUSED', 3
    result = inst.read_file({'path': 'notes.txt'})
    assert result.get('content') == 'kept' and result['session'] == 'resumed', result


def test_a_replacement_session_does_not_inherit_a_spent_resume_budget(monkeypatch, logs):
    clock = _Clock()
    monkeypatch.setattr(mod, 'time', clock)
    old = _FakeSession('sb-old', state='PAUSED', resume_refusals=1000)
    _refusing(old, clock)
    fresh = _FakeSession('sb-fresh', exec_result=_CommandResult(argv=['bash'], exit_code=0, stdout=b'ok\n'))
    _refusing(fresh, clock)
    inst, _ = _instance(monkeypatch, old, fresh)
    assert 'still being torn down' in inst.run_command({'command': 'ls'})['error']
    old.state, old._exec_error = 'TERMINATED', mod.SessionTerminatedError('session_terminated')
    assert inst.run_command({'command': 'ls'})['session'] == 'replaced'
    # The new session is paused by hand moments later, and Tenki refuses its resume three times.
    fresh.state, fresh._resume_refusals = 'PAUSED', 3
    result = inst.run_command({'command': 'ls'})
    assert result.get('stdout') == 'ok\n' and result['session'] == 'resumed', result


def test_a_pipeline_that_is_started_again_starts_with_no_resume_budget_left_over(monkeypatch, logs):
    clock = _Clock()
    monkeypatch.setattr(mod, 'time', clock)
    session = _FakeSession('sb-1', state='PAUSED', resume_refusals=1000)
    _refusing(session, clock)
    inst, _ = _instance(monkeypatch, session)
    assert 'still being torn down' in inst.run_command({'command': 'ls'})['error']
    assert inst.IGlobal._resume_retry_until is not None
    inst.IGlobal.beginGlobal()
    assert inst.IGlobal._resume_retry_until is None


def test_a_resume_that_takes_during_shutdown_is_not_waited_on(monkeypatch, logs):
    # The ask was already on its way when shutdown began. Waiting up to three minutes for the VM to
    # come up would hold the lock endGlobal needs in order to close that same VM.
    session = _FakeSession('sb-1', state='PAUSED')
    inst, _ = _instance(monkeypatch, session)
    resume = session.resume

    def resume_as_shutdown_begins():
        inst.IGlobal._ending = True
        resume()

    session.resume = resume_as_shutdown_begins
    result = inst.run_command({'command': 'ls'})
    assert 'shutting down' in result['error']
    assert 'wait_ready' not in session.calls and inst.IGlobal.session is session


def test_a_paused_session_is_not_resumed_once_the_pipeline_is_shutting_down(monkeypatch, logs):
    # endGlobal is about to close the session. Resuming it first would restart a VM only to end it,
    # and the wait would hold the session lock that shutdown is waiting for.
    session = _FakeSession('sb-1', files={'/home/tenki/notes.txt': 'kept'})
    inst, _ = _instance(monkeypatch, session)
    assert inst.read_file({'path': 'notes.txt'})['content'] == 'kept'
    session.state = 'PAUSED'
    inst.IGlobal._ending = True
    result = inst.read_file({'path': 'notes.txt'})
    assert 'shutting down' in result['error'] and 'resume' not in session.calls


def test_resume_retries_stop_when_the_pipeline_starts_shutting_down(monkeypatch, logs):
    clock = _Clock()
    monkeypatch.setattr(mod, 'time', clock)
    session = _FakeSession('sb-1', state='PAUSED', resume_refusals=1000)
    _refusing(session, clock)
    inst, _ = _instance(monkeypatch, session)
    sleep = clock.sleep

    def sleep_until_shutdown_begins(seconds):
        sleep(seconds)
        inst.IGlobal._ending = True

    clock.sleep = sleep_until_shutdown_begins
    result = inst.run_command({'command': 'ls'})
    assert 'shutting down' in result['error']
    assert session.calls.count('resume') == 1 and inst.IGlobal.session is session


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
        if self._run_error is not None and argv != _WARM_UP:
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


def test_a_command_with_no_result_by_the_nodes_own_deadline_is_reported_as_timed_out(monkeypatch, logs):
    # The one failure after a start that is not "cut off, check what it did": no result arrived by
    # the node's own limit, which lies past the execution timeout at which Tenki stops a command.
    session = _FakeSession('sb-1', stalls='result')
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'sleep 999'})
    assert result['timed_out'] is True and 'not run again' not in result['error']
    assert 'no result within 150s' in result['error'] and 'kill' in result['error']
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
def test_a_command_cut_off_by_a_terminated_session_is_not_run_again_either(monkeypatch, logs, tool, args):
    # Nothing of the first run is left inside a terminated sandbox, but what it did outside (a deploy,
    # a request to some service) is done, and a second run would do it twice. The node never starts a
    # command a second time on its own: the agent is told, and decides.
    ended = mod.SessionTerminatedError('session_terminated')
    dead = _FakeSession('sb-dead', run_error=ended)
    dead.refresh = lambda: SimpleNamespace(id=dead.id, state='TERMINATED')
    fresh = _FakeSession('sb-fresh', exec_result=_CommandResult(argv=['bash'], exit_code=0, stdout=b'ok\n'))
    inst, client = _instance(monkeypatch, dead, fresh)
    result = getattr(inst, tool)(args)
    # "ended", not "replaced": the call stops there, and no new sandbox exists until the next call.
    assert result['exit_code'] == -1 and result['timed_out'] is False and result['session'] == 'ended'
    assert 'ended while this command was running' in result['error'] and 'not run again' in result['error']
    assert 'outside the sandbox' in result['error'] and 'paused' not in result['error']
    assert len(_execs(dead)) == 1 and len(_execs(fresh)) == 0
    # No VM is created for a call that will not use it. The next command starts the fresh one.
    assert len(client.create_calls) == 1
    after = inst.run_command({'command': 'ls'})
    assert after['stdout'] == 'ok\n' and after['session'] == 'created' and len(client.create_calls) == 2


def test_a_started_command_whose_recovery_ran_past_the_deadline_is_not_reported_as_a_plain_timeout(monkeypatch, logs):
    # The look at the session after a cut-off command is a network call too, and can hit the client
    # deadline. That is a timeout of the look, not of the command, which had started all the same.
    cut = mod.SessionTerminatedError('session_terminated:terminate_requested: termination requested')
    session = _FakeSession('sb-1', run_error=cut, refresh_error=mod.CommandTimeoutError('Deadline Exceeded'))
    inst, _ = _instance(monkeypatch, session)
    result = inst.run_command({'command': 'make deploy'})
    assert result['timed_out'] is False
    assert 'Deadline Exceeded' in result['error'] and 'not run again' in result['error'], result['error']
    assert 'part of its work may be done' in result['error']


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

    assert glb.call_with_session(call, on_event=reported.append) == 'done'
    assert reported == ['created', 'resumed']


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


# ---------------------------------------------------------------------------
# Loop 2: the no-op command after a create or a resume (1b), retrying only what is safe to repeat (1c),
# and one session-status contract on every result (1d)
# ---------------------------------------------------------------------------


def test_a_new_session_runs_the_no_op_command_before_its_first_call(monkeypatch, logs):
    # The SDK checks a session's data plane with a request that has no deadline before a file call, unless a
    # command has already been answered on it. A bounded no-op command first means that check never runs.
    session = _FakeSession('sb-1', files={'/home/tenki/notes.txt': 'kept'})
    inst, _ = _instance(monkeypatch, session)
    assert inst.read_file({'path': 'notes.txt'})['content'] == 'kept'
    starts = [call for call in session.calls if isinstance(call, tuple) and call[0] == 'start']
    assert starts[0][1] == _WARM_UP
    assert session.calls.index(starts[0]) < session.calls.index(('stat', '/home/tenki/notes.txt'))
    inst.read_file({'path': 'notes.txt'})
    inst.list_files({'path': '.'})
    assert _warm_ups(session) == 1


def test_the_no_op_command_is_bounded(monkeypatch, logs):
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    inst.list_files({'path': '.'})
    [(argv, kwargs)] = [(call[1], call[2]) for call in session.calls if isinstance(call, tuple) and call[0] == 'start']
    assert argv == _WARM_UP and kwargs['timeout'] == mod._WARM_UP_TIMEOUT_SECS
    waits = [call[1] for call in session.calls if isinstance(call, tuple) and call[0] == 'wait']
    assert waits == [mod._WARM_UP_WAIT_SECS]


def test_a_resumed_session_runs_the_no_op_command_again_before_the_retry(monkeypatch, logs):
    session = _FakeSession('sb-1', files={'/home/tenki/notes.txt': 'kept'})
    inst, _ = _instance(monkeypatch, session)
    inst.read_file({'path': 'notes.txt'})
    session.state = 'PAUSED'
    result = inst.read_file({'path': 'notes.txt'})
    assert result['content'] == 'kept' and result['session'] == 'resumed'
    assert _warm_ups(session) == 2
    resumed_at = session.calls.index('resume')
    second_warm_up = [
        i
        for i, call in enumerate(session.calls)
        if isinstance(call, tuple) and call[0] == 'start' and call[1] == _WARM_UP
    ][1]
    assert second_warm_up > resumed_at


def test_a_no_op_command_that_never_starts_is_reported_and_tried_again_on_the_next_call(monkeypatch, logs):
    session = _FakeSession('sb-1', stalls='start', warm_up_faults=True, files={'/home/tenki/notes.txt': 'kept'})
    inst, _ = _instance(monkeypatch, session)
    result = inst.read_file({'path': 'notes.txt'})
    assert 'did not start' in result['error'] and _first_data_call(session) is None
    assert ('kill',) in session.calls  # a start that arrives late must not run it after all
    session._stalls = None
    assert inst.read_file({'path': 'notes.txt'})['content'] == 'kept'
    assert _warm_ups(session) == 2


def test_the_no_op_command_is_not_run_once_the_pipeline_is_shutting_down(monkeypatch, logs):
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    inst.IGlobal.get_session()
    inst.IGlobal._ending = True
    assert 'shutting down' in inst.list_files({'path': '.'})['error']
    assert _warm_ups(session) == 0


def _paused_after_first_use(session_kwargs=None):
    """A session that served one call (so it is warm), then was paused by Tenki."""
    return _FakeSession('sb-1', **(session_kwargs or {}))


@pytest.mark.parametrize(
    ('tool', 'args', 'operation'),
    [
        ('write_file', {'path': 'a.txt', 'content': 'x'}, 'write_text'),
        ('make_directory', {'path': 'made'}, 'mkdir'),
        ('delete_path', {'path': 'notes.txt'}, 'remove'),
        ('git_clone', {'repo': _REPO}, 'clone'),
        ('git_checkout', {'ref': 'feature', 'create': True}, 'checkout'),
    ],
)
def test_a_change_in_flight_when_the_session_was_paused_is_not_sent_again(monkeypatch, logs, tool, args, operation):
    # The request went out and the pause came; whether it took effect is unknown. Sending it again could do it
    # twice (a branch created twice, a delete of what was put back), so the agent is told and checks.
    session = _FakeSession('sb-1', files={'/home/tenki/notes.txt': 'kept'})
    inst, _ = _instance(monkeypatch, session)
    inst.list_files({'path': '.'})
    session.state = 'PAUSED'
    result = getattr(inst, tool)(args)
    assert result['session'] == 'resumed'
    assert 'not sent again' in result['error'] and 'Check' in result['error'], result
    sent = [call for call in session.calls if isinstance(call, tuple) and call[0] == operation]
    assert len(sent) == 1
    assert session.calls.count('resume') == 1


@pytest.mark.parametrize(
    ('tool', 'args', 'operation', 'key'),
    [
        ('read_file', {'path': 'notes.txt'}, 'read_bytes', 'content'),
        ('list_files', {'path': '.'}, 'list', 'entries'),
        ('git_diff', {}, 'diff', 'output'),
        ('git_log', {}, 'log', 'output'),
    ],
)
def test_a_read_in_flight_when_the_session_was_paused_is_sent_again(monkeypatch, logs, tool, args, operation, key):
    session = _FakeSession('sb-1', files={'/home/tenki/notes.txt': 'kept'})
    inst, _ = _instance(monkeypatch, session)
    inst.list_files({'path': '.'})
    session.state = 'PAUSED'
    result = getattr(inst, tool)(args)
    assert 'error' not in result and key in result and result['session'] == 'resumed', result


def test_a_change_that_failed_before_it_was_sent_is_sent_after_the_resume(monkeypatch, logs):
    # Here the pause surfaced in the no-op command, before the write went out: nothing to be unsure about.
    session = _FakeSession('sb-1', state='PAUSED')
    inst, _ = _instance(monkeypatch, session)
    result = inst.write_file({'path': 'a.txt', 'content': 'x'})
    assert result == {'success': True, 'path': '/home/tenki/a.txt', 'session': 'resumed'}
    assert session.fs.files['/home/tenki/a.txt'] == 'x'


def test_a_change_on_a_session_that_ended_runs_on_the_fresh_one(monkeypatch, logs):
    # Nothing of the first attempt survives a terminated sandbox, and a file write has no effect outside it.
    ended = mod.SessionTerminatedError('session_terminated')
    dead = _FakeSession('sb-dead', fail={'write_text': ended})
    fresh = _FakeSession('sb-fresh')
    inst, client = _instance(monkeypatch, dead, fresh)
    inst.list_files({'path': '.'})
    dead.refresh = lambda: SimpleNamespace(id=dead.id, state='TERMINATED')
    result = inst.write_file({'path': 'a.txt', 'content': 'x'})
    assert result == {'success': True, 'path': '/home/tenki/a.txt', 'session': 'replaced'}
    assert fresh.fs.files['/home/tenki/a.txt'] == 'x'


def test_the_first_call_says_it_created_the_session_and_later_calls_say_nothing(monkeypatch, logs):
    session = _FakeSession('sb-1')
    inst, _ = _instance(monkeypatch, session)
    assert inst.list_files({'path': '.'})['session'] == 'created'
    assert 'session' not in inst.list_files({'path': '.'})
    assert 'session' not in inst.run_command({'command': 'ls'})


@pytest.mark.parametrize(
    ('tool', 'args', 'operation'),
    [
        ('read_file', {'path': 'notes.txt'}, 'stat'),
        ('list_files', {'path': '.'}, 'list'),
        ('delete_path', {'path': 'notes.txt'}, 'remove'),
        ('git_diff', {}, 'diff'),
        ('git_log', {}, 'log'),
        ('git_checkout', {'ref': 'main'}, 'checkout'),
    ],
)
def test_a_call_that_finds_the_session_ended_says_so_in_its_session_field(monkeypatch, logs, tool, args, operation):
    ended = mod.SessionTerminatedError('session_terminated')
    dead = _FakeSession('sb-dead', files={'/home/tenki/notes.txt': 'kept'}, fail={operation: ended})
    inst, client = _instance(monkeypatch, dead, _FakeSession('sb-fresh'))
    inst.run_command({'command': 'ls'})
    dead.refresh = lambda: SimpleNamespace(id=dead.id, state='TERMINATED')
    result = getattr(inst, tool)(args)
    assert result['session'] == 'ended' and 'ended' in result['error']
    assert len(client.create_calls) == 1
    # The call after it starts the fresh sandbox, and says so.
    assert inst.run_command({'command': 'ls'})['session'] == 'created'
    assert len(client.create_calls) == 2


def test_a_command_cut_off_by_a_terminated_session_says_the_session_ended(monkeypatch, logs):
    ended = mod.SessionTerminatedError('session_terminated')
    dead = _FakeSession('sb-dead')
    inst, client = _instance(monkeypatch, dead, _FakeSession('sb-fresh'))
    inst.list_files({'path': '.'})
    dead._run_error = ended
    dead.refresh = lambda: SimpleNamespace(id=dead.id, state='TERMINATED')
    result = inst.run_command({'command': 'make deploy'})
    assert result['session'] == 'ended' and 'not run again' in result['error']
    assert len(client.create_calls) == 1


def test_the_session_field_schema_lists_every_value_the_node_reports():
    assert inst_mod._SESSION_EVENT['enum'] == ['created', 'resumed', 'replaced', 'ended']
    for value in ('created', 'resumed', 'replaced', 'ended'):
        assert f'"{value}"' in inst_mod._SESSION_EVENT['description']


def test_a_command_whose_follow_up_look_at_the_session_runs_past_the_deadline_is_not_reported_as_timed_out(
    monkeypatch, logs
):
    # The command never started, and the look at the session that followed hit the client deadline. That is
    # not the command timing out, and the agent is told what it was.
    session = _FakeSession('sb-1', stalls='start', refresh_error=mod.CommandTimeoutError('Deadline Exceeded'))
    inst, _ = _instance(monkeypatch, session)
    session._stalls = None
    inst.list_files({'path': '.'})
    session._stalls = 'start'
    result = inst.run_command({'command': 'ls'})
    assert result['timed_out'] is False
    assert 'Deadline Exceeded' in result['error'] and "checking the sandbox's state" in result['error'], result['error']


def test_a_no_op_command_with_no_result_in_time_is_reported_without_touching_the_files(monkeypatch, logs):
    session = _FakeSession('sb-1', stalls='result', warm_up_faults=True, files={'/home/tenki/notes.txt': 'kept'})
    inst, _ = _instance(monkeypatch, session)
    result = inst.read_file({'path': 'notes.txt'})
    assert "did not finish the node's first command" in result['error'] and _first_data_call(session) is None
    assert session.calls.count('refresh') == 1  # it says nothing about a pause, so the session's state is looked at


def test_a_no_op_command_that_finds_the_session_terminated_leads_to_a_replacement(monkeypatch, logs):
    ended = mod.SessionTerminatedError('session_terminated')
    dead = _FakeSession('sb-dead', run_error=ended, warm_up_faults=True)
    dead.refresh = lambda: SimpleNamespace(id=dead.id, state='TERMINATED')
    fresh = _FakeSession('sb-fresh')
    inst, client = _instance(monkeypatch, dead, fresh)
    result = inst.write_file({'path': 'a.txt', 'content': 'x'})
    assert result == {'success': True, 'path': '/home/tenki/a.txt', 'session': 'replaced'}
    assert len(client.create_calls) == 2 and fresh.fs.files['/home/tenki/a.txt'] == 'x'


def test_an_sdk_error_while_the_no_op_command_starts_is_reported_as_itself(monkeypatch, logs):
    session = _FakeSession('sb-1', files={'/home/tenki/notes.txt': 'kept'})
    start = session.start

    def start_then_fail(*argv, **kwargs):
        process = start(*argv, **kwargs)
        if argv == _WARM_UP:
            process.close_stdin = lambda: _raise(mod.SandboxError('quota exceeded'))
        return process

    session.start = start_then_fail
    inst, _ = _instance(monkeypatch, session)
    assert inst.read_file({'path': 'notes.txt'})['error'] == 'quota exceeded'
    assert _first_data_call(session) is None
