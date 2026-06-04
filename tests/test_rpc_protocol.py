from pathlib import Path

from susops.core.config import Connection, PortForward, SusOpsConfig
from susops.core.rpc_protocol import (
    decode_arg,
    encode_value,
    InvocationRequest,
    InvocationResponse,
)
from susops.core.types import (
    ConnectionStatus,
    LogoStyle,
    ProcessState,
    ShareInfo,
    StartResult,
    StatusResult,
    StopResult,
    TestResult,
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
