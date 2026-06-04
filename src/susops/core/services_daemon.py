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
