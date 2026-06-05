from pathlib import Path

from susops.core.config import Connection
from susops.core.rpc_protocol import (
    decode_arg,
    encode_value,
    InvocationRequest,
    InvocationResponse,
)
from susops.core.types import (
    ConnectionStatus,
    ProcessState,
    StartResult,
    StatusResult,
)


def test_encode_decode_primitive():
    assert encode_value(42) == 42
    assert encode_value("hi") == "hi"
    assert encode_value(True) is True
    assert encode_value(None) is None


def test_encode_decode_path():
    p = Path("/tmp/foo")
    encoded = encode_value(p)
    assert encoded == {"__type__": "Path", "value": "/tmp/foo"}
    assert decode_arg(encoded) == p


def test_encode_decode_enum_process_state():
    encoded = encode_value(ProcessState.RUNNING)
    assert encoded["__type__"] == "ProcessState"
    assert decode_arg(encoded) is ProcessState.RUNNING


def test_encode_decode_connection_model():
    c = Connection(tag="work", ssh_host="user@host", socks_proxy_port=1080)
    encoded = encode_value(c)
    assert encoded["__type__"] == "Connection"
    decoded = decode_arg(encoded)
    assert isinstance(decoded, Connection)
    assert decoded.tag == "work"
    assert decoded.ssh_host == "user@host"
    assert decoded.socks_proxy_port == 1080


def test_encode_decode_start_result_dataclass():
    r = StartResult(success=True, message="ok")
    encoded = encode_value(r)
    assert encoded["__type__"] == "StartResult"
    decoded = decode_arg(encoded)
    assert isinstance(decoded, StartResult)
    assert decoded.success is True
    assert decoded.message == "ok"


def test_invocation_request_roundtrip():
    req = InvocationRequest(method="start", args=[], kwargs={"tag": "work"})
    payload = req.to_json()
    parsed = InvocationRequest.from_json(payload)
    assert parsed.method == "start"
    assert parsed.kwargs == {"tag": "work"}


def test_invocation_response_error():
    resp = InvocationResponse(ok=False, error="boom", error_type="ValueError")
    payload = resp.to_json()
    parsed = InvocationResponse.from_json(payload)
    assert parsed.ok is False
    assert parsed.error == "boom"
    assert parsed.error_type == "ValueError"


def test_encode_set_to_list():
    """Sets are not JSON-native; encoder flattens to a sorted list.

    reconnect_monitor_info() returns a dict with a `watching: set[str]`
    field — without this branch it'd fail with `Cannot encode value of
    type set` and break the TUI's dashboard.
    """
    encoded = encode_value({"a", "b", "c"})
    assert isinstance(encoded, list)
    assert sorted(encoded) == ["a", "b", "c"]


def test_encode_decode_nested_dataclass_keeps_types():
    """StatusResult contains tuple[ConnectionStatus, ...]; the inner
    ConnectionStatus instances must survive the round-trip as real
    dataclass instances, not bare dicts. Regression test for the
    `susops ps` AttributeError when CS came back as dict.
    """
    cs1 = ConnectionStatus(tag="work", running=True, socks_port=1080, pid=1234)
    cs2 = ConnectionStatus(tag="staging", running=False, socks_port=0, pid=None)
    sr = StatusResult(
        state=ProcessState.RUNNING,
        connection_statuses=(cs1, cs2),
        pac_running=True,
        pac_port=51234,
        message="",
    )
    decoded = decode_arg(encode_value(sr))
    assert isinstance(decoded, StatusResult)
    assert decoded.state is ProcessState.RUNNING
    assert len(decoded.connection_statuses) == 2
    for cs in decoded.connection_statuses:
        assert isinstance(cs, ConnectionStatus), \
            f"expected ConnectionStatus, got {type(cs).__name__}: {cs!r}"
    assert decoded.connection_statuses[0].tag == "work"
    assert decoded.connection_statuses[0].running is True
    assert decoded.connection_statuses[1].tag == "staging"
