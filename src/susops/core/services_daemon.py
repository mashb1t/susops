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
