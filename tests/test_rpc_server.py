# tests/test_rpc_server.py
import pytest

from susops.core.rpc_protocol import InvocationRequest, InvocationResponse
from susops.core.rpc_server import build_app
from susops.facade import SusOpsManager


@pytest.fixture
def manager(tmp_path):
    return SusOpsManager(
        workspace=tmp_path,
        _enable_background_threads=False,
        _skip_restore=True,
    )


@pytest.fixture
async def client(manager, aiohttp_client):
    return await aiohttp_client(build_app(manager))


async def _rpc(client, req: InvocationRequest) -> InvocationResponse:
    resp = await client.post("/rpc", data=req.to_json(),
                             headers={"Content-Type": "application/json"})
    text = await resp.text()
    return InvocationResponse.from_json(text)


async def test_list_config_roundtrip(client):
    body = await _rpc(client, InvocationRequest(method="list_config"))
    assert body.ok is True
    cfg = body.result
    assert cfg is not None
    assert hasattr(cfg, "connections")


async def test_add_connection_roundtrip(client):
    body = await _rpc(client, InvocationRequest(
        method="add_connection",
        args=["work"],
        kwargs={"ssh_host": "user@host", "socks_port": 0},
    ))
    assert body.ok is True
    conn = body.result
    assert conn.tag == "work"
    assert conn.ssh_host == "user@host"


async def test_unknown_method_returns_error(client):
    body = await _rpc(client, InvocationRequest(method="not_a_real_method"))
    assert body.ok is False
    assert body.error_type == "AttributeError"


async def test_private_method_blocked(client):
    body = await _rpc(client, InvocationRequest(method="_reload_config"))
    assert body.ok is False
    assert body.error_type == "AttributeError"


async def test_value_error_propagates(client):
    await _rpc(client, InvocationRequest(
        method="add_connection",
        args=["dup"],
        kwargs={"ssh_host": "a@b"},
    ))
    body = await _rpc(client, InvocationRequest(
        method="add_connection",
        args=["dup"],
        kwargs={"ssh_host": "a@b"},
    ))
    assert body.ok is False
    assert body.error_type == "ValueError"
    assert "already exists" in body.error
