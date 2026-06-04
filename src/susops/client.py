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
        os.kill(pid, 0)  # signal 0 = liveness probe
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


# Map of error_type strings to Python exception classes the client
# re-raises. Anything not listed falls back to RuntimeError so we never
# silently swallow a server-side failure.
_EXC_MAP: dict[str, type] = {
    "ValueError": ValueError,
    "RuntimeError": RuntimeError,
    "FileNotFoundError": FileNotFoundError,
    "PermissionError": PermissionError,
    "KeyError": KeyError,
    "AttributeError": AttributeError,
}


class SusOpsClient:
    """RPC proxy with the same API surface as `SusOpsManager`.

    Lazy: only resolves the daemon on first call. If it isn't running,
    auto-spawns it via `ensure_daemon_running`.
    """

    def __init__(self, workspace: Path = _WORKSPACE_DEFAULT,
                 process_name: str = "susops-client") -> None:
        self.workspace = workspace
        # process_name kept for API compatibility with frontends.
        self._process_name = process_name
        self._port: int | None = None

    # ------------------------------------------------------------------ #
    # Compatibility shims that some frontends read directly.
    # ------------------------------------------------------------------ #

    @property
    def app_config(self):
        """Frontends sometimes read `manager.app_config.<field>` directly."""
        return self.list_config().susops_app

    @property
    def config(self):
        """Snapshot of the current config. Per call → fresh RPC."""
        return self.list_config()

    # ------------------------------------------------------------------ #
    # Auto-proxy: any unknown public attribute becomes an RPC call.
    # ------------------------------------------------------------------ #

    def __getattr__(self, name: str):
        # Block dunders + private names so they never reach /rpc.
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
        except urllib.error.HTTPError as exc:
            # 4xx/5xx — body should still be a valid InvocationResponse JSON.
            try:
                body = InvocationResponse.from_json(exc.read().decode("utf-8"))
            except Exception:
                raise DaemonUnavailableError(f"Daemon HTTP error: {exc}") from exc
        except urllib.error.URLError as exc:
            # Connection refused → daemon dead or wrong port.
            self._port = None
            raise DaemonUnavailableError(f"Daemon unreachable: {exc}") from exc
        except Exception as exc:
            self._port = None
            raise DaemonUnavailableError(f"Daemon RPC failed: {exc}") from exc

        if body.ok:
            return body.result

        exc_cls = _EXC_MAP.get(body.error_type or "", RuntimeError)
        raise exc_cls(body.error or f"RPC {method} failed")
