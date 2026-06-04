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


async def test_allowlist_covers_known_frontend_methods(client):
    """Every facade method that frontends actually call must be on the
    allowlist. This is a regression test for the steady trickle of
    AttributeError: method 'X' not allowed bugs the TUI / tray hit
    during real usage.

    The list below is derived from grep across src/susops/tui and
    src/susops/tray. Keep it in sync when frontends start calling new
    facade methods.
    """
    # We use unknown_method to inspect the allowlist response shape.
    body = await _rpc(client, InvocationRequest(method="definitely_not_a_method"))
    assert body.ok is False
    assert body.error_type == "AttributeError"

    # Methods that MUST be allowlisted. We don't care if the call fails
    # for business reasons (e.g. unknown tag) — only that the allowlist
    # doesn't reject it.
    frontend_methods = [
        "process_info", "get_process_info", "get_uptime", "get_logs",
        "set_forward_enabled", "is_udp_forward_running",
        "update_config", "update_app_config",
    ]
    for m in frontend_methods:
        body = await _rpc(client, InvocationRequest(
            method=m, args=[], kwargs={},
        ))
        # If the allowlist rejects it, error_type will be AttributeError
        # with "not allowed" in the message. Anything else is OK (TypeError
        # for missing args, ValueError for unknown tag, etc.) — we only
        # guard against the allowlist itself misfiring.
        if not body.ok and body.error_type == "AttributeError":
            assert "not allowed" not in (body.error or ""), \
                f"method {m!r} missing from RPC allowlist: {body.error}"
