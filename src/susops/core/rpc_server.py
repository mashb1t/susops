"""aiohttp server hosting the JSON-over-HTTP RPC endpoint.

Dispatches `InvocationRequest.method` to the named method on `SusOpsManager`.
Methods are looked up by attribute access; private methods (leading
underscore) are rejected to prevent direct access to internal helpers.
Anything outside the allowlist below is rejected too — deny-by-default.
"""
from __future__ import annotations

import logging
from aiohttp import web

from susops.core.rpc_protocol import (
    InvocationRequest,
    InvocationResponse,
)

log = logging.getLogger("susops.services.rpc")

# Methods explicitly exposed to RPC clients. Deny by default.
_ALLOWED_METHODS: set[str] = {
    # Lifecycle
    "start", "stop", "restart", "status", "stop_quick",
    # Config introspection
    "list_config",
    # Connection CRUD
    "add_connection", "remove_connection", "set_connection_enabled",
    # PAC hosts
    "add_pac_host", "remove_pac_host", "set_pac_host_enabled",
    # Forwards
    "add_local_forward", "add_remote_forward",
    "remove_local_forward", "remove_remote_forward",
    "toggle_forward_enabled", "set_forward_enabled",
    "is_udp_forward_running",
    # File sharing
    "share", "stop_share", "delete_share", "list_shares",
    "share_is_running", "fetch",
    # Testing
    "test", "test_all", "test_connection", "test_domain", "test_forward",
    "test_ssh",
    # App-level
    "reset", "update_app_config", "update_config",
    # URLs
    "get_pac_url", "get_status_url",
    # Bandwidth
    "get_bandwidth", "get_bandwidth_totals",
    # Reconnect introspection
    "reconnect_monitor_info",
    # Process introspection
    "process_info",          # global — used by `susops ps`
    "get_process_info",      # per-tag — used by TUI dashboard
    "get_uptime",            # per-tag — used by TUI dashboard / connections panel
    "get_logs",              # used by TUI logs panel
}


async def _handle_rpc(request: web.Request) -> web.Response:
    mgr = request.app["manager"]
    try:
        payload = await request.text()
        req = InvocationRequest.from_json(payload)
    except Exception as exc:
        log.exception("Malformed RPC request")
        resp = InvocationResponse(ok=False, error=str(exc), error_type=type(exc).__name__)
        return web.Response(text=resp.to_json(), status=400, content_type="application/json")

    if req.method.startswith("_") or req.method not in _ALLOWED_METHODS:
        resp = InvocationResponse(
            ok=False,
            error=f"method '{req.method}' not allowed",
            error_type="AttributeError",
        )
        return web.Response(text=resp.to_json(), status=404, content_type="application/json")

    method = getattr(mgr, req.method, None)
    if method is None or not callable(method):
        resp = InvocationResponse(
            ok=False,
            error=f"no callable named '{req.method}'",
            error_type="AttributeError",
        )
        return web.Response(text=resp.to_json(), status=404, content_type="application/json")

    try:
        result = method(*req.args, **req.kwargs)
        resp = InvocationResponse(ok=True, result=result)
        return web.Response(text=resp.to_json(), content_type="application/json")
    except Exception as exc:
        log.exception("RPC %s failed", req.method)
        resp = InvocationResponse(
            ok=False,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return web.Response(text=resp.to_json(), status=500, content_type="application/json")


def build_app(manager) -> web.Application:
    """Build the aiohttp Application that exposes /rpc."""
    app = web.Application()
    app["manager"] = manager
    app.router.add_post("/rpc", _handle_rpc)
    return app


def serve(manager, host: str = "127.0.0.1", port: int = 0) -> tuple:
    """Start the RPC server on a background daemon thread with its own event loop.

    Returns (runner, actual_port). The runner can be cleaned up by calling
    `await runner.cleanup()` from any asyncio context, or by calling
    `runner.shutdown()` synchronously (less clean). For daemon shutdown the
    process exit handles it.
    """
    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    app = build_app(manager)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host=host, port=port)
    loop.run_until_complete(site.start())
    # Bound port (in case 0 was requested)
    sock_list = list(site._server.sockets) if site._server is not None else []
    actual_port = sock_list[0].getsockname()[1] if sock_list else port

    thread = threading.Thread(target=loop.run_forever, daemon=True, name="susops-rpc")
    thread.start()
    return runner, actual_port
