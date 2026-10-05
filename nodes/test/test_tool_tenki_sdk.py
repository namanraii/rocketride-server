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

"""Contract tests: the parts of the real tenki SDK that tool_tenki depends on.

test_tool_tenki.py replaces the SDK with stubs and fakes, so it cannot notice an SDK release that
renames or reshapes something the node uses. This module imports the real package and checks exactly
those things; it is skipped when tenki is not installed. requirements.txt pins the version these were
written against, so run this module against any new version before bumping the pin.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

tenki = pytest.importorskip('tenki')

from tenki_sandbox import errors as sdk_errors
from tenki_sandbox._pb import pb
from tenki_sandbox._transport import _STREAMING_OUT, GrpcConfig, GrpcSandboxRPC
from tenki_sandbox.client import build_create_session_request
from tenki_sandbox.data_plane import GrpcDataPlaneRPC
from tenki_sandbox.errors import map_rpc_error
from tenki_sandbox.fs import SandboxFS
from tenki_sandbox.git import SandboxGit
from tenki_sandbox.process import Process, ProcessStream

_REQUIRED = inspect.Parameter.empty

# The node's own module, loaded from source: it has no engine imports, so it runs against the real SDK.
_SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'nodes' / 'tool_tenki' / 'file_deadline.py'
_spec = importlib.util.spec_from_file_location('tool_tenki_file_deadline', _SOURCE)
file_deadline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(file_deadline)


def _parameters(function):
    """Map each parameter name to its default (_REQUIRED when it has none), leaving out self."""
    return {
        name: parameter.default for name, parameter in inspect.signature(function).parameters.items() if name != 'self'
    }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'name',
    [
        'SessionNotFoundError',
        'SessionTerminatedError',
        'InvalidStateError',
        'WaitReadyFailedError',
        'TemplateRuntimeFailedError',
        'CommandTimeoutError',
        'PermissionDeniedError',
        'FileNotFoundError',
    ],
)
def test_the_errors_the_node_handles_are_sandbox_errors(name):
    # The tools catch SandboxError; an error that stopped subclassing it would escape as a crash.
    assert issubclass(getattr(tenki, name), tenki.SandboxError)


def test_no_sdk_error_is_also_a_plain_runtime_or_timeout_error():
    # call_with_session turns a plain RuntimeError or TimeoutError into SessionUnavailableError. An SDK
    # error that also subclassed either would be swallowed into it and lose its type: a
    # CommandTimeoutError would stop coming back to the agent as timed_out.
    errors = [
        value
        for value in vars(sdk_errors).values()
        if isinstance(value, type) and issubclass(value, Exception) and value.__module__ == sdk_errors.__name__
    ]
    assert len(errors) > 10
    assert [error.__name__ for error in errors if issubclass(error, (RuntimeError, TimeoutError))] == []


def test_the_sdk_file_not_found_error_still_subclasses_the_builtin():
    assert issubclass(tenki.FileNotFoundError, FileNotFoundError)


@pytest.mark.parametrize('name', ['WaitReadyFailedError', 'TemplateRuntimeFailedError'])
def test_a_failed_create_still_carries_what_it_left_running(name):
    # IGlobal reads error.sandbox to close a billing VM; after a rename it would silently close nothing.
    carried = object()
    assert getattr(tenki, name)('not ready', carried).sandbox is carried


@pytest.mark.parametrize(
    ('code', 'details', 'expected'),
    [
        ('FAILED_PRECONDITION', 'a condition the SDK does not recognise', 'InvalidStateError'),
        ('NOT_FOUND', 'a resource the SDK does not recognise', 'SessionNotFoundError'),
        ('NOT_FOUND', 'open /home/tenki/a.txt: no such file or directory', 'FileNotFoundError'),
        ('DEADLINE_EXCEEDED', 'deadline exceeded', 'CommandTimeoutError'),
        ('PERMISSION_DENIED', 'permission denied', 'PermissionDeniedError'),
        ('UNAVAILABLE', 'Received http2 header with status: 502', 'SandboxError'),
        ('UNKNOWN', 'paused source is still being torn down, retry shortly', 'SandboxError'),
    ],
)
def test_the_error_mapping_the_node_is_designed_around(code, details, expected):
    # The two catch-alls are why recovery checks a session's real state instead of trusting the error;
    # the file-not-found mapping is what lets write_file create a missing parent directory. A failure
    # the SDK has no class for arrives as exactly SandboxError, which is how the node tells "this says
    # nothing about the session" from an answer such as a missing file.
    class _RpcError(Exception):
        def code(self):
            return SimpleNamespace(name=code)

        def details(self):
            return details

    assert type(map_rpc_error(_RpcError())) is getattr(tenki, expected)


# ---------------------------------------------------------------------------
# Client and session creation
# ---------------------------------------------------------------------------


def test_the_client_takes_an_explicit_key_endpoint_and_deadline():
    offline = object()
    client = tenki.Client(
        auth_token='tk_mock-tenki-placeholder-for-tests', base_url='https://api.tenki.cloud', timeout=60, rpc=offline
    )
    assert client.base_url == 'https://api.tenki.cloud'
    assert callable(client.get) and callable(client.close)


def test_the_client_refuses_a_key_without_the_tk_prefix_before_any_request():
    with pytest.raises(tenki.InvalidAuthTokenError):
        tenki.Client(auth_token='mock-tenki-placeholder-for-tests', rpc=object())


def test_the_client_deadline_covers_every_control_plane_call_the_node_makes():
    # The deadline IGlobal passes, and the clone limit git_clone describes, apply only to unary calls
    # on the control plane. Git has no data-plane call, so it always goes there.
    unary = {'create_session', 'get_session', 'resume_session', 'terminate_session', 'git_operation'}
    assert not unary & _STREAMING_OUT
    assert 'git_operation' not in GrpcDataPlaneRPC._methods


class _RecordingStub:
    """In place of a gRPC stub: notes the deadline each call arrived with, and has no network."""

    def __init__(self):
        self.deadlines = {}

    def __getattr__(self, name):
        def method(request, *args, **kwargs):
            self.deadlines[name] = kwargs.get('timeout', 'none sent')
            raise ConnectionError('no network in contract tests')

        return method


_FILE_RPCS = {'Stat', 'ReadFile', 'WriteFile', 'List', 'Mkdir', 'Remove'}


def _session_with_stubbed_planes():
    """A real Sandbox whose control plane and data plane are the SDK's own, over recording stubs."""
    control = GrpcSandboxRPC(
        GrpcConfig(base_url='https://127.0.0.1:9', auth_token='tk_mock-tenki-placeholder-for-tests', timeout=60)
    )
    control_stub = _RecordingStub()
    object.__setattr__(control, '_stub', control_stub)
    session = tenki.Sandbox(SimpleNamespace(_rpc=control), tenki.SandboxInfo(id='sb-contract'))
    data = GrpcDataPlaneRPC('https://127.0.0.1:9', SimpleNamespace(current=lambda: ''))
    data_stub = data._stub = _RecordingStub()
    # As after the session's first exec or file call, which is when the SDK sets its data plane up.
    session._data_plane_rpc, session._data_plane_verified = data, True
    assert session._session_rpc is data
    return session, control_stub, data_stub


def _make_every_file_call_the_node_makes(session):
    files = session.fs
    for call in (
        lambda: files.stat('/home/tenki/a.txt'),
        lambda: files.read_bytes('/home/tenki/a.txt'),
        lambda: files.write_text('/home/tenki/a.txt', 'x'),
        lambda: files.list('/home/tenki', include_hidden=False),
        lambda: files.mkdir('/home/tenki/d'),
        lambda: files.remove('/home/tenki/d'),
    ):
        with pytest.raises(tenki.SandboxError):
            call()


def test_the_sdk_sends_a_sessions_file_calls_to_its_data_plane_with_no_deadline():
    # Why the node adds one. If this fails because the SDK started sending its own, file_deadline
    # can go; until then a stalled connection would hang these calls for good.
    session, control_stub, data_stub = _session_with_stubbed_planes()
    _make_every_file_call_the_node_makes(session)
    assert data_stub.deadlines == dict.fromkeys(_FILE_RPCS, 'none sent')
    assert control_stub.deadlines == {}


def test_the_nodes_deadline_reaches_grpc_on_every_file_call_of_a_real_session():
    session, control_stub, data_stub = _session_with_stubbed_planes()
    assert isinstance(session.fs, SandboxFS) and hasattr(session.fs, '_rpc')  # what bound_file_calls reaches into
    file_deadline.bound_file_calls(session, 75)
    _make_every_file_call_the_node_makes(session)
    assert data_stub.deadlines == dict.fromkeys(_FILE_RPCS, 75)
    assert control_stub.deadlines == {}


def test_the_nodes_deadline_also_applies_when_file_calls_fall_back_to_the_control_plane():
    # A session with no data plane sends file calls through the client; the node's deadline is the
    # one used there too, in place of the client's own.
    session, control_stub, data_stub = _session_with_stubbed_planes()
    file_deadline.bound_file_calls(session, 75)
    session._data_plane_rpc = None
    session._ensure_data_plane = lambda **kwargs: None
    _make_every_file_call_the_node_makes(session)
    assert control_stub.deadlines == dict.fromkeys(_FILE_RPCS, 75)
    assert data_stub.deadlines == {}


def test_the_deadline_survives_the_data_plane_being_set_up_again_after_a_resume():
    # resume() drops the session's data plane and the next call sets up a new one. The deadline sits
    # on the file API itself, not on the plane, so the calls that follow still carry it.
    session, _, _ = _session_with_stubbed_planes()
    file_deadline.bound_file_calls(session, 75)
    session._reset_data_plane()
    fresh = GrpcDataPlaneRPC('https://127.0.0.1:9', SimpleNamespace(current=lambda: ''))
    fresh_stub = fresh._stub = _RecordingStub()
    session._data_plane_rpc, session._data_plane_verified = fresh, True
    _make_every_file_call_the_node_makes(session)
    assert fresh_stub.deadlines == dict.fromkeys(_FILE_RPCS, 75)


def test_the_deadline_is_added_to_the_unary_file_calls_only():
    assert file_deadline.UNARY_FILE_CALLS <= set(GrpcDataPlaneRPC._methods)
    assert {GrpcDataPlaneRPC._methods[name] for name in file_deadline.UNARY_FILE_CALLS} == _FILE_RPCS
    seen = {}
    streaming = SimpleNamespace(read_file_stream=lambda request, **kwargs: seen.update(kwargs) or iter(()))
    assert list(file_deadline.FileCallDeadline(streaming, 75).read_file_stream(object())) == []
    assert seen == {}


def test_a_call_that_reaches_its_deadline_is_the_sdks_timeout_error():
    # What a file call that runs past the node's deadline raises: an error result with a class of
    # its own, so it is reported as it is and prompts no look at the session.
    class _DeadlineExceeded(Exception):
        def code(self):
            return SimpleNamespace(name='DEADLINE_EXCEEDED')

        def details(self):
            return 'Deadline Exceeded'

    assert type(map_rpc_error(_DeadlineExceeded())) is tenki.CommandTimeoutError


def test_create_accepts_every_option_the_node_sends():
    parameters = _parameters(tenki.Client.create)
    for name in (
        'cpu_cores',
        'memory_mb',
        'disk_size_gb',
        'max_duration',
        'allow_inbound',
        'image',
        'name',
        'tags',
        'metadata',
    ):
        assert name in parameters, name
    assert parameters['sticky'] is False  # relied on: a sticky session would discard max_duration
    assert parameters['allow_inbound'] is True  # so the node has to pass False itself


@pytest.mark.parametrize('options', [{'disk_size_gb': 4}, {'memory_mb': 4097}])
def test_the_resource_rules_the_node_clamps_to(options):
    # IGlobal clamps disk_size_gb to at least 5 and rounds memory_mb down to even because of these.
    with pytest.raises(tenki.InvalidResourceConfigError):
        build_create_session_request(**options)


# ---------------------------------------------------------------------------
# Sessions, processes and results
# ---------------------------------------------------------------------------


def test_sessions_can_be_listed_by_tag_and_closed_for_shutdown_cleanup():
    # endGlobal lists this run's sessions by tag and closes each; a rename here would let a
    # leaked, billing VM survive teardown while the stubbed unit tests stayed green.
    assert 'tags' in _parameters(tenki.Client.list)
    assert callable(tenki.Sandbox.close_if_open)


def test_the_session_methods_the_node_calls():
    for name in ('start', 'refresh', 'resume', 'wait_ready', 'close', 'close_if_open'):
        assert callable(getattr(tenki.Sandbox, name)), name
    assert {'cwd', 'env', 'timeout', 'stdin', 'privileged'} <= set(_parameters(tenki.Sandbox.start))
    assert 'timeout' in _parameters(tenki.Sandbox.wait_ready)
    assert isinstance(inspect.getattr_static(tenki.Sandbox, 'id'), property)


def test_a_process_can_be_waited_on_with_a_limit_and_cancelled():
    assert _parameters(Process.close_stdin) == {}
    # The node takes "close_stdin returned" to mean the sandbox confirmed the start: that is what
    # decides whether a command may be run again after a pause.
    assert '_wait_started' in inspect.getsource(Process.close_stdin)
    assert 'timeout' in _parameters(Process.wait)
    assert _parameters(Process.kill) == {}


def test_a_process_stream_yields_as_it_arrives_and_ends_when_closed():
    # _OutputGuard reads stdout and stderr while the command runs, and ends them itself after a stall.
    stream = ProcessStream()
    stream.push(b'a')
    stream.push(b'b')
    stream.close()
    assert list(stream) == [b'a', b'b']
    # The SDK keeps its own copy of everything read, which is why the guard kills instead of only reading.
    assert stream.read() == b'ab'


def test_a_failed_run_ends_both_output_streams_with_its_error():
    # Otherwise a reader would block for good on a run that had already failed.
    class _NoNetwork:
        def run(self, requests):
            raise ConnectionError('no network in contract tests')

    process = Process(_NoNetwork(), 'sb-contract', ['true'])
    for stream in (process.stdout, process.stderr):
        assert isinstance(stream, ProcessStream)
        with pytest.raises(tenki.SandboxError):
            list(stream)
    with pytest.raises(tenki.SandboxError):
        process.wait(timeout=5)


def test_the_session_states_the_node_compares_against_still_exist():
    # IGlobal compares SandboxInfo.state with these names; a renamed state would break recovery quietly.
    names = {pb.SessionState.Name(value).removeprefix('SESSION_STATE_') for value in pb.SessionState.values()}
    assert {'RUNNING', 'PAUSED', 'TERMINATING', 'TERMINATED', 'USER_SHUTDOWN'} <= names


def test_records_and_results_have_the_fields_the_node_reads():
    assert {'id', 'state'} <= set(tenki.SandboxInfo.__dataclass_fields__)
    assert not hasattr(tenki.SandboxInfo(id='x'), 'close')  # why a template failure is closed by id
    # test_tool_tenki.py mirrors these two dataclasses; keep the mirrors in step with them.
    assert list(tenki.CommandResult.__dataclass_fields__) == [
        'argv',
        'exit_code',
        'stdout',
        'stderr',
        'signal',
        'duration_ms',
        'reason',
        'errno',
        'timed_out',
    ]
    assert list(tenki.FileInfo.__dataclass_fields__) == [
        'path',
        'size',
        'mode',
        'is_dir',
        'modified_unix_ns',
        'is_symlink',
        'symlink_target',
    ]
    result = tenki.CommandResult(argv=['bash'], exit_code=0, stdout='é'.encode(), timed_out=True)
    assert (result.stdout_text, result.stderr_text, result.timed_out) == ('é', '', True)


# ---------------------------------------------------------------------------
# File and git APIs
# ---------------------------------------------------------------------------


def test_the_file_api_signatures():
    assert _parameters(SandboxFS.write_text) == {'path': _REQUIRED, 'text': _REQUIRED, 'encoding': 'utf-8'}
    assert _parameters(SandboxFS.stat) == {'path': _REQUIRED}
    assert _parameters(SandboxFS.read_bytes) == {'path': _REQUIRED}
    assert _parameters(SandboxFS.list) == {'path': _REQUIRED, 'include_hidden': False}
    assert _parameters(SandboxFS.mkdir) == {'path': _REQUIRED, 'recursive': True, 'mode': 0o755}
    assert _parameters(SandboxFS.remove) == {'path': _REQUIRED, 'recursive': True}


def test_stat_reports_size_and_kind_and_read_bytes_is_one_unary_request():
    # read_file checks the size before fetching, because ReadFile has no length and returns it all.
    calls = []

    class _Rpc:
        def stat(self, request):
            calls.append(('stat', request.path))
            return pb.StatResponse(exists=True, size=5, is_dir=False)

        def read_file(self, request):
            calls.append(('read_file', request.path))
            return pb.ReadFileResponse(path=request.path, content=b'hello')

    fs = SandboxFS(_Rpc(), 'sb-contract')
    info = fs.stat('/home/tenki/a.txt')
    assert (info.size, info.is_dir) == (5, False)
    assert fs.read_bytes('/home/tenki/a.txt') == b'hello'
    assert calls == [('stat', '/home/tenki/a.txt'), ('read_file', '/home/tenki/a.txt')]
    assert [field.name for field in pb.ReadFileRequest.DESCRIPTOR.fields] == ['session_id', 'path']


def test_stat_on_a_missing_path_raises_the_sdk_file_not_found_error():
    # _fs_error turns this into "no such file or directory: <path>" for the agent.
    class _Rpc:
        def stat(self, request):
            return pb.StatResponse(exists=False)

    with pytest.raises(tenki.FileNotFoundError):
        SandboxFS(_Rpc(), 'sb-contract').stat('/home/tenki/missing.txt')


def test_the_git_api_signatures():
    assert _parameters(SandboxGit.clone) == {'repo': _REQUIRED, 'branch': None, 'depth': None, 'directory': None}
    assert _parameters(SandboxGit.checkout) == {'ref': _REQUIRED, 'create': False, 'directory': None}
    assert _parameters(SandboxGit.diff) == {'range': None, 'base': None, 'head': None, 'path': None, 'directory': None}
    assert _parameters(SandboxGit.log) == {'max_count': None, 'range': None, 'path': None, 'directory': None}
