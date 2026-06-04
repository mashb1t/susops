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


_EXIT_ANOTHER_DAEMON_ALIVE = 2
_EXIT_PAC_PORT_SQUATTED = 3


def _preflight(workspace: Path, log: "logging.Logger") -> None:
    """Refuse to start if another daemon is alive or PAC port is squatted.

    Converts the silent-failure modes that bit us during development
    (orphan accumulation → "PAC server failed: Address already in use"
    buried in the facade's log buffer) into loud, actionable exits.
    """
    # 1. Another services daemon already alive?
    pid_file = _pid_path(workspace)
    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text().strip())
            os.kill(existing_pid, 0)  # signal 0 = liveness probe
        except (OSError, ValueError):
            # Stale PID file; clean it up and proceed.
            pid_file.unlink(missing_ok=True)
            _port_path(workspace).unlink(missing_ok=True)
        else:
            log.error(
                "another susops-services daemon is already running (pid=%d). "
                "Stop it first with `kill %d` (or `kill -9 %d` if it's wedged) "
                "before starting a new one.",
                existing_pid, existing_pid, existing_pid,
            )
            sys.exit(_EXIT_ANOTHER_DAEMON_ALIVE)

    # 2. Is the configured PAC port held by an untracked process?
    try:
        from susops.core.config import load_config
        cfg = load_config(workspace)
        pac_port = cfg.pac_server_port
    except Exception:
        pac_port = 0
    if not pac_port:
        return  # 0 means auto-allocate, no port to preflight

    import socket as _socket
    probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", pac_port))
    except OSError as exc:
        log.error(
            "PAC port %d is bound by another process (%s). "
            "Likely an orphan susops-services or susops.core.pac from a "
            "previous run. Recover with:\n"
            "  pkill -9 -f 'susops-services|susops.core.services_daemon|susops.core.pac'\n"
            "  rm -f %s/pids/susops-services.{pid,port}\n"
            "Then start this daemon again.",
            pac_port, exc, workspace,
        )
        sys.exit(_EXIT_PAC_PORT_SQUATTED)
    finally:
        probe.close()


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

    _preflight(workspace, log)

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
        # Remove PID + port files FIRST so subsequent ensure_daemon_running()
        # calls don't think we're still alive while we're shutting down.
        # Anything that hangs below can't strand us with a stale PID file.
        _remove_pid_file(workspace)
        _remove_port_file(workspace)

        # Stop the manager (kills SSH masters, stops PAC/status threads).
        # Wrapped in a watchdog timer — if a child won't die we still exit.
        def _watchdog():
            # If we're still alive 5 s after starting cleanup, the OS gets
            # to reap us with prejudice. Better than zombies.
            import time as _t
            _t.sleep(5.0)
            log.error("Shutdown watchdog tripped, force-exiting")
            os._exit(1)
        threading.Thread(target=_watchdog, daemon=True, name="susops-services-watchdog").start()

        try:
            mgr.stop_quick()
        except Exception:
            log.exception("Error during manager stop")
        # NOTE: we intentionally do NOT call `asyncio.run(runner.cleanup())`.
        # The aiohttp runner lives on a separate event loop (see serve() in
        # rpc_server.py) running on a daemon thread. Spawning a fresh loop
        # here to await an object from a different loop deadlocks. Process
        # exit closes the listening sockets cleanly anyway.
        log.info("Daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
