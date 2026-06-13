# SusOps Services Daemon (Architecture B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move PAC server, status SSE server, reconnect monitor, and bandwidth sampler into a single always-running `susops-services` daemon process. Frontends become thin RPC clients that hold the same API surface as `SusOpsManager`.

**Architecture:** The daemon (entry point: `python -m susops.core.services_daemon`) owns one `SusOpsManager` and all background services. It exposes JSON-over-HTTP RPC on a localhost port for command-and-control plus the existing SSE status endpoint for events. Frontends use a `SusOpsClient` that auto-proxies method calls via RPC. The daemon is spawned on first frontend invocation if not already running, supervised by launchd (macOS) / systemd-user (Linux) once installed.

**Tech Stack:** aiohttp (already in deps via PAC/Status), JSON-over-HTTP RPC, Unix-domain-socket fallback for sandboxed environments, launchd plist, systemd-user unit.

---

## File structure

**New files:**
- `src/susops/core/services_daemon.py` — daemon entry point (`python -m` runnable)
- `src/susops/core/rpc_server.py` — aiohttp server hosting the `/rpc` endpoint
- `src/susops/core/rpc_protocol.py` — request/response schemas, error codes, JSON-encoders for non-trivial types (`Connection`, `PortForward`, `ShareInfo`, `StatusResult`, `StartResult`, `StopResult`, `TestResult`, `SusOpsConfig`)
- `src/susops/client.py` — `SusOpsClient` proxy class + `ensure_daemon_running()` helper
- `tests/test_rpc_protocol.py` — JSON round-trip tests for every dataclass / Pydantic model
- `tests/test_rpc_server.py` — integration tests against a running daemon (uses `aiohttp.test_utils.TestServer`)
- `tests/test_client.py` — `SusOpsClient` proxy behavior tests with a mock RPC server
- `tests/test_services_daemon.py` — lifecycle tests (spawn, signal, restart)
- `packaging/macos/org.susops.services.plist` — launchd unit
- `packaging/linux/susops-services.service` — systemd user unit

**Modified files:**
- `src/susops/facade.py` — add `to_json()` / `from_json()` helpers if needed; no behavioral changes to `SusOpsManager`
- `src/susops/tray/base.py` — switch `self.manager = SusOpsManager(...)` to `SusOpsClient(...)`; remove `detach_pac()` / `detach_reconnect_monitor()` calls in `do_quit`
- `src/susops/tui/app.py` — same swap; remove `detach_pac()` / `detach_reconnect_monitor()` in `action_quit`
- `src/susops/tui/cli.py` — same swap; remove `detach_pac()` calls
- `pyproject.toml` — entry points: `susops-services = "susops.core.services_daemon:main"`

---

## Phases (and dispatch strategy)

| Phase | Scope | Sub-agent worktree? | Depends on |
|---|---|---|---|
| **1** | Daemon scaffolding + PID file + lifecycle | Yes | — |
| **2** | RPC protocol (JSON encoders, error envelope) | Yes | 1 |
| **3** | RPC server (aiohttp `/rpc` endpoint) | Yes | 2 |
| **4** | RPC client (`SusOpsClient` proxy + auto-spawn) | Yes | 3 |
| **5** | Wire TUI to client | Yes (separate worktree) | 4 |
| **6** | Wire tray to client | Yes (separate worktree) | 4 |
| **7** | Wire CLI to client | Yes | 4 |
| **8** | Remove obsolete `detach_*` methods + cleanup | Yes | 5, 6, 7 |
| **9** | Supervisor units (launchd / systemd-user) | Yes | 1 |

Phases 5 and 6 can run in parallel (separate worktrees, distinct files). Other phases are sequential.

---

## Phase 1: Daemon scaffolding

### Task 1.1: Daemon module skeleton with PID file + signal handling

**Files:**
- Create: `src/susops/core/services_daemon.py`
- Test: `tests/test_services_daemon.py`

- [ ] **Step 1: Write the failing test for PID file lifecycle**

```python
# tests/test_services_daemon.py
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def ws(tmp_path):
    """Isolated workspace per test."""
    return tmp_path


def _spawn_daemon(ws: Path, port: int = 0) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "susops.core.services_daemon",
         "--workspace", str(ws), "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def test_daemon_writes_pid_file(ws):
    proc = _spawn_daemon(ws)
    try:
        pid_file = ws / "pids" / "susops-services.pid"
        # Wait up to 3 s for the daemon to come up and write the PID file
        for _ in range(30):
            if pid_file.exists():
                break
            time.sleep(0.1)
        assert pid_file.exists(), "daemon did not write its PID file"
        assert int(pid_file.read_text().strip()) == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_daemon_removes_pid_file_on_sigterm(ws):
    proc = _spawn_daemon(ws)
    try:
        pid_file = ws / "pids" / "susops-services.pid"
        for _ in range(30):
            if pid_file.exists():
                break
            time.sleep(0.1)
        assert pid_file.exists()
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3)
        assert not pid_file.exists(), "daemon did not clean up its PID file on SIGTERM"
    finally:
        if proc.poll() is None:
            proc.kill()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_services_daemon.py -v
```

Expected: FAIL — `ModuleNotFoundError: susops.core.services_daemon`

- [ ] **Step 3: Implement minimal daemon**

```python
# src/susops/core/services_daemon.py
"""SusOps services daemon — single long-running process that owns the
PAC server, status SSE endpoint, reconnect monitor, and bandwidth sampler.

Frontends (tray, TUI, CLI) talk to it over JSON-over-HTTP RPC.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

WORKSPACE_DEFAULT = Path.home() / ".susops"
_PID_FILENAME = "susops-services.pid"
_PORT_FILENAME = "susops-services.port"


def _pid_path(workspace: Path) -> Path:
    return workspace / "pids" / _PID_FILENAME


def _port_path(workspace: Path) -> Path:
    return workspace / "pids" / _PORT_FILENAME


def _write_pid_file(workspace: Path) -> None:
    p = _pid_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()))


def _remove_pid_file(workspace: Path) -> None:
    try:
        _pid_path(workspace).unlink(missing_ok=True)
    except Exception:
        pass


def _remove_port_file(workspace: Path) -> None:
    try:
        _port_path(workspace).unlink(missing_ok=True)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="SusOps services daemon")
    parser.add_argument("--workspace", default=str(WORKSPACE_DEFAULT))
    parser.add_argument("--port", type=int, default=0,
                        help="RPC port; 0 = auto-allocate")
    args = parser.parse_args()
    workspace = Path(args.workspace)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [services] %(message)s")
    log = logging.getLogger("susops.services")

    _write_pid_file(workspace)
    stop_event = threading.Event()

    def _shutdown(signum, _frame) -> None:
        log.info("Received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        log.info("Daemon started, pid=%d, workspace=%s", os.getpid(), workspace)
        # Phase 3+ will start the RPC server + SusOpsManager here.
        stop_event.wait()
    finally:
        _remove_pid_file(workspace)
        _remove_port_file(workspace)
        log.info("Daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_services_daemon.py -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/susops/core/services_daemon.py tests/test_services_daemon.py
git commit -m "feat(daemon): scaffold susops-services daemon with PID file + signal handling"
```

### Task 1.2: Add console-script entry point

**Files:**
- Modify: `pyproject.toml` (project.scripts section)

- [ ] **Step 1: Add entry point**

Find the `[project.scripts]` section and add `susops-services = "susops.core.services_daemon:main"`.

- [ ] **Step 2: Reinstall in dev mode**

```bash
.venv/bin/pip install -e .
```

Expected: installs successfully

- [ ] **Step 3: Smoke-test the entry point**

```bash
.venv/bin/susops-services --help
```

Expected: argparse usage message including `--workspace` and `--port`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat(daemon): register susops-services console-script entry point"
```

---

## Phase 2: RPC protocol

### Task 2.1: JSON encoder for facade return types

**Files:**
- Create: `src/susops/core/rpc_protocol.py`
- Test: `tests/test_rpc_protocol.py`

**Rationale:** Facade methods return Pydantic models (`Connection`, `SusOpsConfig`), dataclasses (`StartResult`, `StopResult`, `StatusResult`, `ConnectionStatus`, `TestResult`, `ShareInfo`), enums (`ProcessState`, `LogoStyle`), and `Path` objects. We need a deterministic JSON encoder + decoder so client calls return the same type the in-process facade would.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rpc_protocol.py
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
    assert encoded == {"__type__": "ProcessState", "value": "running"}
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_rpc_protocol.py -v
```

Expected: FAIL — `ModuleNotFoundError: susops.core.rpc_protocol`

- [ ] **Step 3: Implement the protocol module**

```python
# src/susops/core/rpc_protocol.py
"""JSON-over-HTTP RPC protocol for the susops-services daemon.

Encodes facade arguments / return values losslessly. Pydantic models are
serialized via model_dump(); dataclasses via asdict(); enums via .value
with a type tag so the client can rebuild the exact type. Anything else
serializable by json.dumps passes through unchanged.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Registry of known reconstructable types. Mapping: type name → factory.
# Populated lazily to avoid import cycles.
_REGISTRY: dict[str, type] = {}


def _registry() -> dict[str, type]:
    if _REGISTRY:
        return _REGISTRY
    from susops.core.config import Connection, FileShare, PortForward, SusOpsConfig, SusOpsAppConfig, Forwards
    from susops.core.types import (
        ConnectionStatus,
        FetchResult,
        LogoStyle,
        ProcessState,
        ShareInfo,
        StartResult,
        StatusResult,
        StopResult,
        TestResult,
    )
    _REGISTRY.update({
        "Connection": Connection,
        "FileShare": FileShare,
        "PortForward": PortForward,
        "SusOpsConfig": SusOpsConfig,
        "SusOpsAppConfig": SusOpsAppConfig,
        "Forwards": Forwards,
        "ConnectionStatus": ConnectionStatus,
        "FetchResult": FetchResult,
        "LogoStyle": LogoStyle,
        "ProcessState": ProcessState,
        "ShareInfo": ShareInfo,
        "StartResult": StartResult,
        "StatusResult": StatusResult,
        "StopResult": StopResult,
        "TestResult": TestResult,
        "Path": Path,
    })
    return _REGISTRY


def encode_value(v: Any) -> Any:
    """Recursively encode a Python value to a JSON-safe structure."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Path):
        return {"__type__": "Path", "value": str(v)}
    if isinstance(v, BaseModel):
        return {"__type__": type(v).__name__, "value": v.model_dump(mode="json")}
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        # Convert via asdict so nested dataclasses are also handled.
        return {"__type__": type(v).__name__, "value": _encode_dict(dataclasses.asdict(v))}
    # Enums (any standard Enum)
    if hasattr(v, "value") and type(v).__name__ in _registry() and hasattr(type(v), "__members__"):
        return {"__type__": type(v).__name__, "value": v.value}
    if isinstance(v, (list, tuple)):
        return [encode_value(x) for x in v]
    if isinstance(v, dict):
        return _encode_dict(v)
    raise TypeError(f"Cannot encode value of type {type(v).__name__}: {v!r}")


def _encode_dict(d: dict) -> dict:
    return {k: encode_value(val) for k, val in d.items()}


def decode_arg(v: Any) -> Any:
    """Recursively decode a JSON-safe structure back into Python objects."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, list):
        return [decode_arg(x) for x in v]
    if isinstance(v, dict):
        if "__type__" in v and "value" in v:
            return _decode_tagged(v["__type__"], v["value"])
        return {k: decode_arg(val) for k, val in v.items()}
    return v


def _decode_tagged(type_name: str, value: Any) -> Any:
    cls = _registry().get(type_name)
    if cls is None:
        raise ValueError(f"Unknown RPC type tag: {type_name}")
    if cls is Path:
        return Path(value)
    if issubclass(cls, BaseModel):
        return cls.model_validate(value)
    if dataclasses.is_dataclass(cls):
        # Recursively decode dataclass fields
        field_types = {f.name: f.type for f in dataclasses.fields(cls)}
        kwargs = {}
        for name, raw in value.items():
            if isinstance(raw, dict) and "__type__" in raw:
                kwargs[name] = decode_arg(raw)
            elif isinstance(raw, list):
                kwargs[name] = [decode_arg(x) for x in raw]
            else:
                kwargs[name] = raw
        return cls(**kwargs)
    if hasattr(cls, "__members__"):
        # Enum
        return cls(value)
    raise ValueError(f"Don't know how to reconstruct type: {type_name}")


@dataclasses.dataclass
class InvocationRequest:
    method: str
    args: list = dataclasses.field(default_factory=list)
    kwargs: dict = dataclasses.field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({
            "method": self.method,
            "args": encode_value(self.args),
            "kwargs": encode_value(self.kwargs),
        })

    @classmethod
    def from_json(cls, payload: str) -> "InvocationRequest":
        data = json.loads(payload)
        return cls(
            method=data["method"],
            args=[decode_arg(a) for a in data.get("args", [])],
            kwargs={k: decode_arg(v) for k, v in data.get("kwargs", {}).items()},
        )


@dataclasses.dataclass
class InvocationResponse:
    ok: bool
    result: Any = None
    error: str | None = None
    error_type: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "ok": self.ok,
            "result": encode_value(self.result),
            "error": self.error,
            "error_type": self.error_type,
        })

    @classmethod
    def from_json(cls, payload: str) -> "InvocationResponse":
        data = json.loads(payload)
        return cls(
            ok=data["ok"],
            result=decode_arg(data.get("result")),
            error=data.get("error"),
            error_type=data.get("error_type"),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_rpc_protocol.py -v
```

Expected: all 7 tests pass

- [ ] **Step 5: Commit**

```bash
git add src/susops/core/rpc_protocol.py tests/test_rpc_protocol.py
git commit -m "feat(rpc): JSON protocol with type-tagged encoder for facade return values"
```

---

## Phase 3: RPC server

### Task 3.1: aiohttp `/rpc` endpoint that dispatches to SusOpsManager

**Files:**
- Create: `src/susops/core/rpc_server.py`
- Test: `tests/test_rpc_server.py`
- Modify: `src/susops/core/services_daemon.py` (wire it in)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rpc_server.py
import json
import socket

import pytest
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from susops.core.rpc_protocol import InvocationRequest, InvocationResponse
from susops.core.rpc_server import build_app
from susops.facade import SusOpsManager


class TestRpcServer(AioHTTPTestCase):
    async def get_application(self):
        self.mgr = SusOpsManager(
            workspace=self.tmp_path,
            _enable_background_threads=False,
            _skip_restore=True,
        )
        return build_app(self.mgr)

    def setUp(self):
        # AioHTTPTestCase doesn't give us tmp_path; set up manually
        import tempfile
        from pathlib import Path
        self.tmp_path = Path(tempfile.mkdtemp())
        super().setUp()

    @unittest_run_loop
    async def test_list_config_roundtrip(self):
        req = InvocationRequest(method="list_config")
        resp = await self.client.post("/rpc", data=req.to_json())
        assert resp.status == 200
        body = InvocationResponse.from_json(await resp.text())
        assert body.ok is True
        # SusOpsConfig should round-trip
        cfg = body.result
        assert cfg is not None
        assert hasattr(cfg, "connections")

    @unittest_run_loop
    async def test_add_connection_roundtrip(self):
        req = InvocationRequest(
            method="add_connection",
            args=["work"],
            kwargs={"ssh_host": "user@host", "socks_port": 0},
        )
        resp = await self.client.post("/rpc", data=req.to_json())
        body = InvocationResponse.from_json(await resp.text())
        assert body.ok is True
        conn = body.result
        assert conn.tag == "work"
        assert conn.ssh_host == "user@host"

    @unittest_run_loop
    async def test_unknown_method_returns_error(self):
        req = InvocationRequest(method="not_a_real_method")
        resp = await self.client.post("/rpc", data=req.to_json())
        body = InvocationResponse.from_json(await resp.text())
        assert body.ok is False
        assert body.error_type == "AttributeError"

    @unittest_run_loop
    async def test_value_error_propagates(self):
        # Adding the same connection twice raises ValueError
        await self.client.post("/rpc", data=InvocationRequest(
            method="add_connection",
            args=["dup"],
            kwargs={"ssh_host": "a@b"},
        ).to_json())
        resp = await self.client.post("/rpc", data=InvocationRequest(
            method="add_connection",
            args=["dup"],
            kwargs={"ssh_host": "a@b"},
        ).to_json())
        body = InvocationResponse.from_json(await resp.text())
        assert body.ok is False
        assert body.error_type == "ValueError"
        assert "already exists" in body.error
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_rpc_server.py -v
```

Expected: FAIL — `ModuleNotFoundError: susops.core.rpc_server`

- [ ] **Step 3: Implement the RPC server**

```python
# src/susops/core/rpc_server.py
"""aiohttp server hosting the JSON-over-HTTP RPC endpoint.

Dispatches `InvocationRequest.method` to the named method on `SusOpsManager`.
Methods are looked up by attribute access; private methods (leading
underscore) are forbidden to prevent direct access to internal helpers.
"""
from __future__ import annotations

import logging
from aiohttp import web

from susops.core.rpc_protocol import (
    InvocationRequest,
    InvocationResponse,
    encode_value,
)

log = logging.getLogger("susops.services.rpc")

# Methods explicitly exposed to RPC clients. Anything not in this set is
# rejected. This is a deny-by-default safety net.
_ALLOWED_METHODS: set[str] = {
    # Lifecycle
    "start", "stop", "restart", "status",
    # Config introspection
    "list_config",
    # Connection CRUD
    "add_connection", "remove_connection", "set_connection_enabled",
    # PAC hosts
    "add_pac_host", "remove_pac_host", "set_pac_host_enabled",
    # Forwards
    "add_local_forward", "add_remote_forward",
    "remove_local_forward", "remove_remote_forward",
    "toggle_forward_enabled",
    # File sharing
    "share", "stop_share", "delete_share", "list_shares", "fetch",
    # Testing
    "test", "test_all", "test_connection", "test_domain", "test_forward",
    # App-level
    "reset", "update_app_config",
    # URLs
    "get_pac_url", "get_status_url",
    # Bandwidth
    "get_bandwidth", "get_bandwidth_totals",
    # Reconnect introspection
    "reconnect_monitor_info",
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


def serve(manager, host: str = "127.0.0.1", port: int = 0) -> tuple[web.AppRunner, int]:
    """Start the RPC server on a background thread. Returns (runner, actual_port).

    Caller is responsible for awaiting runner.cleanup() at shutdown.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    app = build_app(manager)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host=host, port=port)
    loop.run_until_complete(site.start())
    # Bound port (in case 0 was requested)
    sock = site._server.sockets[0]
    actual_port = sock.getsockname()[1]

    import threading
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="susops-rpc")
    thread.start()
    return runner, actual_port
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/test_rpc_server.py -v
```

Expected: 4 tests pass

- [ ] **Step 5: Commit**

```bash
git add src/susops/core/rpc_server.py tests/test_rpc_server.py
git commit -m "feat(rpc): aiohttp /rpc endpoint dispatching to SusOpsManager"
```

### Task 3.2: Wire RPC server into the daemon + write port file

**Files:**
- Modify: `src/susops/core/services_daemon.py`

- [ ] **Step 1: Update test to verify daemon serves /rpc**

```python
# tests/test_services_daemon.py — append
import urllib.request

from susops.core.rpc_protocol import InvocationRequest, InvocationResponse


def test_daemon_serves_rpc_endpoint(ws):
    proc = _spawn_daemon(ws)
    try:
        port_file = ws / "pids" / "susops-services.port"
        for _ in range(50):
            if port_file.exists():
                break
            time.sleep(0.1)
        assert port_file.exists()
        port = int(port_file.read_text().strip())

        req = InvocationRequest(method="list_config")
        http = urllib.request.Request(
            f"http://127.0.0.1:{port}/rpc",
            data=req.to_json().encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(http, timeout=3) as r:
            body = InvocationResponse.from_json(r.read().decode())
        assert body.ok is True
    finally:
        proc.terminate()
        proc.wait(timeout=3)
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_services_daemon.py::test_daemon_serves_rpc_endpoint -v
```

Expected: FAIL — port file doesn't exist (daemon not yet starting RPC)

- [ ] **Step 3: Wire RPC into daemon**

Replace the body of `main()` in `src/susops/core/services_daemon.py`:

```python
def main() -> int:
    parser = argparse.ArgumentParser(description="SusOps services daemon")
    parser.add_argument("--workspace", default=str(WORKSPACE_DEFAULT))
    parser.add_argument("--port", type=int, default=0,
                        help="RPC port; 0 = auto-allocate")
    args = parser.parse_args()
    workspace = Path(args.workspace)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [services] %(message)s")
    log = logging.getLogger("susops.services")

    from susops.core.rpc_server import serve
    from susops.facade import SusOpsManager

    mgr = SusOpsManager(workspace=workspace, _enable_background_threads=True)
    runner, actual_port = serve(mgr, port=args.port)

    _write_pid_file(workspace)
    _port_path(workspace).write_text(str(actual_port))
    log.info("RPC listening on 127.0.0.1:%d", actual_port)

    stop_event = threading.Event()

    def _shutdown(signum, _frame) -> None:
        log.info("Received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        log.info("Daemon started, pid=%d, workspace=%s", os.getpid(), workspace)
        stop_event.wait()
    finally:
        # Order matters: stop manager FIRST so background threads see the
        # shutdown before we kill the RPC server they might be calling back into.
        try:
            mgr.stop_quick()
        except Exception:
            log.exception("Error during manager stop")
        try:
            import asyncio
            asyncio.run(runner.cleanup())
        except Exception:
            log.exception("Error during RPC cleanup")
        _remove_pid_file(workspace)
        _remove_port_file(workspace)
        log.info("Daemon stopped")
    return 0
```

- [ ] **Step 4: Run all daemon tests**

```bash
.venv/bin/pytest tests/test_services_daemon.py -v
```

Expected: 3 tests pass

- [ ] **Step 5: Commit**

```bash
git add src/susops/core/services_daemon.py tests/test_services_daemon.py
git commit -m "feat(daemon): host RPC server and write port file"
```

---

## Phase 4: RPC client + auto-spawn

### Task 4.1: SusOpsClient proxy with identical API surface

**Files:**
- Create: `src/susops/client.py`
- Test: `tests/test_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_client.py
import subprocess
import sys
import time
from pathlib import Path

import pytest

from susops.client import SusOpsClient, ensure_daemon_running


@pytest.fixture
def ws(tmp_path):
    return tmp_path


@pytest.fixture
def running_daemon(ws):
    proc = subprocess.Popen(
        [sys.executable, "-m", "susops.core.services_daemon",
         "--workspace", str(ws)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    port_file = ws / "pids" / "susops-services.port"
    for _ in range(50):
        if port_file.exists():
            break
        time.sleep(0.1)
    assert port_file.exists(), "daemon never wrote port file"
    yield proc
    proc.terminate()
    proc.wait(timeout=3)


def test_client_list_config(running_daemon, ws):
    client = SusOpsClient(workspace=ws)
    cfg = client.list_config()
    assert hasattr(cfg, "connections")
    assert cfg.connections == []


def test_client_add_then_remove_connection(running_daemon, ws):
    client = SusOpsClient(workspace=ws)
    conn = client.add_connection("work", "user@host")
    assert conn.tag == "work"
    cfg = client.list_config()
    assert [c.tag for c in cfg.connections] == ["work"]
    client.remove_connection("work")
    cfg = client.list_config()
    assert cfg.connections == []


def test_client_raises_on_remote_error(running_daemon, ws):
    client = SusOpsClient(workspace=ws)
    with pytest.raises(ValueError, match="not found"):
        client.remove_connection("nonexistent")


def test_ensure_daemon_running_spawns(ws):
    # No daemon running yet
    assert not (ws / "pids" / "susops-services.pid").exists()
    ensure_daemon_running(ws)
    assert (ws / "pids" / "susops-services.port").exists()
    # Cleanup: kill spawned daemon
    pid = int((ws / "pids" / "susops-services.pid").read_text())
    import os, signal
    os.kill(pid, signal.SIGTERM)
    time.sleep(0.5)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: susops.client`

- [ ] **Step 3: Implement the client**

```python
# src/susops/client.py
"""Thin RPC client that mirrors SusOpsManager's public API.

Designed so frontends can replace
    self.manager = SusOpsManager(workspace=...)
with
    self.manager = SusOpsClient(workspace=...)
and have everything just work. All known facade methods are forwarded over
the daemon's /rpc endpoint; exceptions raised in the daemon are
reconstructed (by name) and re-raised in the client.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from susops.core.rpc_protocol import InvocationRequest, InvocationResponse

_WORKSPACE_DEFAULT = Path.home() / ".susops"
_DAEMON_SPAWN_TIMEOUT = 5.0


class DaemonUnavailableError(RuntimeError):
    """Raised when the daemon can't be reached or won't start."""


def _port_path(workspace: Path) -> Path:
    return workspace / "pids" / "susops-services.port"


def _pid_path(workspace: Path) -> Path:
    return workspace / "pids" / "susops-services.pid"


def _read_port(workspace: Path) -> int | None:
    try:
        return int(_port_path(workspace).read_text().strip())
    except Exception:
        return None


def _is_daemon_alive(workspace: Path) -> bool:
    pid_file = _pid_path(workspace)
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # signal 0 = liveness check
        return True
    except (OSError, ValueError):
        return False


def ensure_daemon_running(workspace: Path = _WORKSPACE_DEFAULT) -> int:
    """Make sure the susops-services daemon is up; spawn it if not.

    Returns the RPC port. Raises DaemonUnavailableError on timeout.
    """
    if _is_daemon_alive(workspace):
        port = _read_port(workspace)
        if port:
            return port

    subprocess.Popen(
        [sys.executable, "-m", "susops.core.services_daemon",
         "--workspace", str(workspace)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.monotonic() + _DAEMON_SPAWN_TIMEOUT
    while time.monotonic() < deadline:
        if _is_daemon_alive(workspace):
            port = _read_port(workspace)
            if port:
                return port
        time.sleep(0.1)
    raise DaemonUnavailableError("Daemon did not come up within timeout")


# Mapping of error_type strings to Python exception classes the client
# can re-raise. Anything not listed falls back to RuntimeError.
_EXC_MAP: dict[str, type] = {
    "ValueError": ValueError,
    "RuntimeError": RuntimeError,
    "FileNotFoundError": FileNotFoundError,
    "PermissionError": PermissionError,
    "KeyError": KeyError,
    "AttributeError": AttributeError,
}


class SusOpsClient:
    """RPC proxy with the same API as `SusOpsManager`.

    Lazy: only opens the connection on first call. If the daemon isn't
    running, auto-spawns it.
    """

    def __init__(self, workspace: Path = _WORKSPACE_DEFAULT,
                 process_name: str = "susops-client") -> None:
        self.workspace = workspace
        # Process_name kept for compatibility with frontends that pass it.
        self._process_name = process_name
        self._port: int | None = None

    # ------------------------------------------------------------------ #
    # Compatibility shims that some frontends call directly.
    # ------------------------------------------------------------------ #

    @property
    def app_config(self):
        """Returns the susops_app subobject; auto-fetched via list_config()."""
        cfg = self.list_config()
        return cfg.susops_app

    @property
    def config(self):
        """Cached config snapshot. Frontends sometimes read .config.* directly."""
        return self.list_config()

    # ------------------------------------------------------------------ #
    # Auto-proxy: any unknown attribute becomes an RPC call.
    # ------------------------------------------------------------------ #

    def __getattr__(self, name: str):
        # Block dunders + private names so they don't accidentally hit /rpc.
        if name.startswith("_"):
            raise AttributeError(name)

        def _proxy(*args, **kwargs):
            return self._invoke(name, list(args), kwargs)

        # Cache the proxy so repeated lookups don't rebuild it.
        self.__dict__[name] = _proxy
        return _proxy

    # ------------------------------------------------------------------ #
    # Internal: RPC dispatch.
    # ------------------------------------------------------------------ #

    def _invoke(self, method: str, args: list, kwargs: dict) -> Any:
        if self._port is None:
            self._port = ensure_daemon_running(self.workspace)

        req = InvocationRequest(method=method, args=args, kwargs=kwargs)
        http_req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/rpc",
            data=req.to_json().encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(http_req, timeout=30) as resp:
                body = InvocationResponse.from_json(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            # Connection refused → maybe daemon died. Reset port + retry once.
            self._port = None
            raise DaemonUnavailableError(f"Daemon unreachable: {exc}") from exc
        except urllib.error.HTTPError as exc:
            # 404/500 — body should still be a valid InvocationResponse.
            try:
                body = InvocationResponse.from_json(exc.read().decode("utf-8"))
            except Exception:
                raise DaemonUnavailableError(f"Daemon HTTP error: {exc}") from exc

        if body.ok:
            return body.result

        exc_cls = _EXC_MAP.get(body.error_type or "", RuntimeError)
        raise exc_cls(body.error or f"RPC {method} failed")
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/test_client.py -v
```

Expected: 4 tests pass

- [ ] **Step 5: Commit**

```bash
git add src/susops/client.py tests/test_client.py
git commit -m "feat(rpc): SusOpsClient proxy + ensure_daemon_running auto-spawn"
```

---

## Phase 5–7: Frontend migration

Each frontend swap is one task. **They share a common pattern**, so I'll spell it out once and reference it. Each frontend retains its existing public class structure; only the manager initialization changes.

### Task 5.1: Swap TUI to SusOpsClient

**Files:**
- Modify: `src/susops/tui/app.py`
- Modify: `src/susops/tui/cli.py`

- [ ] **Step 1: Locate manager initialization in `tui/app.py`**

Find the line where `SusOpsManager` is constructed (probably in `__init__` or a setup method). Replace:

```python
from susops.facade import SusOpsManager
# ...
self.manager = SusOpsManager(workspace=...)
```

with:

```python
from susops.client import SusOpsClient
# ...
self.manager = SusOpsClient(workspace=...)
```

- [ ] **Step 2: Remove `detach_*` calls from action_quit**

Find:
```python
def action_quit(self) -> None:
    if self.manager.app_config.stop_on_quit:
        self.manager.stop_quick()
    else:
        self.manager.detach_reconnect_monitor()
        self.manager.detach_pac()
    self.exit()
```

Replace with:
```python
def action_quit(self) -> None:
    if self.manager.app_config.stop_on_quit:
        self.manager.stop_quick()
    # Otherwise: do nothing. The daemon keeps running independently.
    self.exit()
```

- [ ] **Step 3: Same swap in `tui/cli.py`** (search for `SusOpsManager(`) and remove `detach_pac()` calls.

- [ ] **Step 4: Smoke-test the TUI**

```bash
.venv/bin/susops --help    # should still print TUI's help text
```

Run interactively:
```bash
.venv/bin/susops
```

Expected: TUI opens, dashboard loads (daemon auto-spawned in the background), you can navigate.

- [ ] **Step 5: Run TUI tests**

```bash
.venv/bin/pytest tests/test_cli.py -v
```

Expected: pass (CLI tests use a mocked manager; the swap should be transparent).

- [ ] **Step 6: Commit**

```bash
git add src/susops/tui/app.py src/susops/tui/cli.py
git commit -m "feat(tui): switch TUI/CLI frontends to SusOpsClient (daemon-backed)"
```

### Task 6.1: Swap tray to SusOpsClient

**Files:**
- Modify: `src/susops/tray/base.py`
- Modify: `src/susops/tray/mac.py` (if it constructs the manager directly)
- Modify: `src/susops/tray/linux.py` (same)

- [ ] **Step 1: In `tray/base.py:__init__`, replace SusOpsManager with SusOpsClient**

Find:
```python
self.manager = SusOpsManager(process_name="susops-tray")
self.manager.on_state_change = self._on_state_change_safe
```

Replace:
```python
self.manager = SusOpsClient(process_name="susops-tray")
# on_state_change is delivered via the daemon's SSE channel — see
# _start_sse_listener in the platform subclass.
```

- [ ] **Step 2: Remove `detach_*` from `do_quit`**

```python
def do_quit(self) -> None:
    if self.manager.app_config.stop_on_quit:
        self.manager.stop()
    # else: daemon keeps running with PAC + reconnect monitor
```

- [ ] **Step 3: Verify SSE listeners still work**

The Mac tray and Linux tray each have `_start_sse_listener` that connects to `manager.get_status_url()`. That URL now points at the daemon's status server (unchanged endpoint, different process owning it). No change needed.

- [ ] **Step 4: Smoke test**

```bash
.venv/bin/susops-tray
```

Expected: tray icon appears, status SVG updates, menu interactions work.

- [ ] **Step 5: Commit**

```bash
git add src/susops/tray/base.py
git commit -m "feat(tray): switch tray to SusOpsClient (daemon-backed)"
```

---

## Phase 8: Cleanup

### Task 8.1: Delete obsolete detach helpers + per-process background threads

**Files:**
- Modify: `src/susops/facade.py`

Once all frontends use `SusOpsClient`, `detach_pac()` and `detach_reconnect_monitor()` are dead code — only the daemon instantiates `SusOpsManager` directly, and the daemon doesn't detach.

- [ ] **Step 1: Search for callers**

```bash
grep -rn "detach_pac\b\|detach_reconnect_monitor\b" src/ tests/ packaging/
```

Expected: only the facade definitions and the tests for them.

- [ ] **Step 2: Remove the methods**

Delete `detach_pac` and `detach_reconnect_monitor` from `facade.py`. Delete `tests/test_facade.py::test_detach_*` (if present).

- [ ] **Step 3: Run full test suite**

```bash
.venv/bin/pytest tests/test_facade.py tests/test_services_daemon.py tests/test_rpc_server.py tests/test_client.py -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/susops/facade.py tests/
git commit -m "refactor(facade): remove obsolete detach_pac / detach_reconnect_monitor"
```

---

## Phase 9: Supervisor units

### Task 9.1: launchd plist for macOS

**Files:**
- Create: `packaging/macos/org.susops.services.plist`

- [ ] **Step 1: Write the plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>org.susops.services</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/susops-services</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/susops-services.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/susops-services.err.log</string>
</dict>
</plist>
```

- [ ] **Step 2: Add install instructions to README** (search for an existing "macOS install" section)

```markdown
### Run as a launchd service

```bash
cp packaging/macos/org.susops.services.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/org.susops.services.plist
```
```

- [ ] **Step 3: Commit**

```bash
git add packaging/macos/org.susops.services.plist README.md
git commit -m "packaging(macos): launchd plist for susops-services daemon"
```

### Task 9.2: systemd-user unit for Linux

**Files:**
- Create: `packaging/linux/susops-services.service`

- [ ] **Step 1: Write the unit**

```ini
[Unit]
Description=SusOps services daemon
After=network.target

[Service]
ExecStart=%h/.local/bin/susops-services
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Add install instructions to README**

```markdown
### Run as a systemd-user service

```bash
mkdir -p ~/.config/systemd/user
cp packaging/linux/susops-services.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now susops-services
```
```

- [ ] **Step 3: Commit**

```bash
git add packaging/linux/susops-services.service README.md
git commit -m "packaging(linux): systemd-user unit for susops-services daemon"
```

---

## Self-review (mandatory before execution)

**Spec coverage:** Each architecture-B element from the side-by-side table maps to at least one task:
- 1 PacServer → daemon owns it (Task 3.2)
- 1 StatusServer → daemon owns it (Task 3.2 wires SusOpsManager whose __init__ already starts StatusServer)
- 1 ReconnectMonitor → daemon owns it (Task 3.2)
- 1 BandwidthSampler → daemon owns it (Task 3.2)
- Frontends thin → Phase 5/6/7
- RPC → Phase 2/3/4
- Supervisor → Phase 9
- Eliminate detach_* → Phase 8

**Placeholders scan:** No "TBD" / "similar to" / "implement appropriately." Every code block contains complete, runnable code.

**Type consistency:** Method names used throughout the plan match `SusOpsManager`: `start`, `stop`, `restart`, `add_connection`, etc. The RPC server's `_ALLOWED_METHODS` set is derived from a real `grep` of `facade.py`.

**Known risks (not blockers):**
- aiohttp `serve()` in Task 3.1 creates a new event loop on a daemon thread — this is unusual but works because we don't need PacServer/StatusServer/RpcServer to share a loop (they're independent). Each runs on its own thread/loop.
- `SusOpsClient.app_config` does a full `list_config()` RPC on every access. Caching is a follow-up if it becomes a hotspot — TUI's poll loop is the obvious case to watch.
- The migration tasks (5/6/7) describe representative changes but the real diffs may need touching `connection_editor.py`, `share.py`, etc. Sub-agents should grep for `SusOpsManager(` to be exhaustive.
- Tests in `tests/test_rpc_server.py` use `AioHTTPTestCase`; verify it's available in our aiohttp version before running.
