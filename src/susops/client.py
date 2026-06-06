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
# 15s rather than 5s gives headroom for slow init paths (ssh agent
# prompts, network probes) on top of the daemon's "publish port first,
# restore async" startup ordering. Frontends will still detect a truly
# dead daemon quickly because they poll the port file every 100ms — the
# timeout only fires for genuine hangs.
_DAEMON_SPAWN_TIMEOUT = 15.0


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

    Captures the spawned daemon's stderr so a preflight failure
    (PAC port squatted, peer daemon alive, …) is surfaced to the caller
    instead of getting buried under a generic "didn't come up" message.
    """
    if _is_daemon_alive(workspace):
        port = _read_port(workspace)
        if port:
            return port
        # Daemon claimed its PID file but hasn't published the port yet.
        # Wait for it instead of spawning a competitor that would race the
        # O_EXCL claim and exit rc=2 ("another daemon is already running").
        deadline = time.monotonic() + _DAEMON_SPAWN_TIMEOUT
        while time.monotonic() < deadline:
            if not _is_daemon_alive(workspace):
                break  # died mid-startup, fall through to spawn a new one
            port = _read_port(workspace)
            if port:
                return port
            time.sleep(0.1)
        else:
            raise DaemonUnavailableError(
                "An existing susops-services daemon is alive but never "
                "published an RPC port. Try `kill " +
                (_pid_path(workspace).read_text().strip() or "<pid>") +
                "` and start again."
            )

    # DEVNULL (not PIPE) for stderr — the daemon writes its log to
    # ~/.susops/logs/susops-services.log directly. Capturing as a pipe
    # would back up once the daemon's log volume exceeded the kernel
    # buffer (~64 KB on macOS) and freeze every subsequent log call.
    proc = subprocess.Popen(
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
        # If the daemon exited (e.g. preflight rejected it), don't keep
        # polling — surface the reason and bail out immediately. The
        # daemon's log file is the new source of truth for failure
        # reasons (stderr is no longer piped — see Popen above).
        rc = proc.poll()
        if rc is not None:
            log_path = workspace / "logs" / "susops-services.log"
            tail = ""
            try:
                if log_path.exists():
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()[-15:]
                        tail = "".join(lines).strip()
            except Exception:
                pass
            msg = (
                    f"Daemon exited during startup (rc={rc})"
                    + (f":\n{tail}" if tail else "")
            )
            raise DaemonUnavailableError(msg)
        time.sleep(0.1)

    # Spawn is still alive but never wrote its PID/port file in time.
    try:
        proc.terminate()
    except Exception:
        pass
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
        """Issue one RPC call. On connection-refused (daemon died / wasn't
        running yet), reset the cached port, re-run ensure_daemon_running
        (which respawns if needed), and retry exactly once. Tray + TUI
        otherwise crash on the first menu click after a daemon restart.
        """
        req = InvocationRequest(method=method, args=args, kwargs=kwargs)
        last_exc: Exception | None = None

        for attempt in (1, 2):
            if self._port is None:
                try:
                    self._port = ensure_daemon_running(self.workspace)
                except DaemonUnavailableError as exc:
                    last_exc = exc
                    continue

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
                # Don't retry; the daemon answered, the call just failed.
                try:
                    body = InvocationResponse.from_json(exc.read().decode("utf-8"))
                except Exception:
                    raise DaemonUnavailableError(f"Daemon HTTP error: {exc}") from exc
            except urllib.error.URLError as exc:
                # Connection refused / unreachable. The daemon died, was
                # killed, or hasn't come back up yet. Drop the cached port
                # so the next iteration ensures+respawns.
                last_exc = exc
                self._port = None
                if attempt == 1:
                    continue
                raise DaemonUnavailableError(f"Daemon unreachable: {exc}") from exc
            except Exception as exc:
                # Unknown transport error. Reset and retry once.
                last_exc = exc
                self._port = None
                if attempt == 1:
                    continue
                raise DaemonUnavailableError(f"Daemon RPC failed: {exc}") from exc

            if body.ok:
                return body.result
            exc_cls = _EXC_MAP.get(body.error_type or "", RuntimeError)
            raise exc_cls(body.error or f"RPC {method} failed")

        # Both attempts exhausted (e.g. ensure_daemon_running kept failing).
        raise DaemonUnavailableError(
            f"Daemon unreachable after retry: {last_exc}"
        ) from last_exc
