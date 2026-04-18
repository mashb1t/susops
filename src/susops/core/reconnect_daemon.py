"""Background reconnect daemon — spawned by SusOpsManager.detach_reconnect_monitor().

Monitors all currently live SSH connections and restarts them on dropout,
exactly like _ReconnectMonitor, but persists after the TUI/tray process exits.
Terminated when a new TUI/tray session starts (which takes over monitoring) or
when stop() is called.
"""
from __future__ import annotations

import argparse
import signal
import threading
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="SusOps reconnect daemon")
    parser.add_argument("--workspace", default=str(Path.home() / ".susops"))
    args = parser.parse_args()
    workspace = Path(args.workspace)

    from susops.core.ssh import is_socket_alive
    from susops.facade import SusOpsManager

    mgr = SusOpsManager(
        workspace=workspace,
        _enable_background_threads=False,
        _skip_restore=True,
    )

    for conn in mgr.config.connections:
        if conn.enabled and is_socket_alive(conn.tag, workspace):
            mgr._reconnect_monitor.mark_running(conn.tag)

    mgr._reconnect_monitor.start()

    stop_event = threading.Event()

    def _shutdown(signum, frame) -> None:
        mgr._reconnect_monitor.stop()
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    stop_event.wait()


if __name__ == "__main__":
    main()
