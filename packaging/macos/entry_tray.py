"""PyInstaller entry point.

The .app bundle has a single binary at Contents/MacOS/SusOps that ignores
Python's -m flag — invoking it with `[sys.executable, "-m",
susops.core.services_daemon", ...]` (which client.py does to spawn the
daemon) would otherwise spawn another tray instead, recursively, until the
laptop melts. Sniff argv here and dispatch manually.
"""
import sys


def _run_daemon() -> int:
    # Strip the "-m <module>" prefix so the daemon's argparse sees a
    # clean argv compatible with `python -m susops.core.services_daemon`.
    sys.argv = [sys.argv[0]] + sys.argv[3:]
    from susops.core.services_daemon import main as daemon_main
    return daemon_main()


def _run_tray() -> int:
    from susops.tray import main as tray_main
    tray_main()
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "-m" \
            and sys.argv[2] == "susops.core.services_daemon":
        sys.exit(_run_daemon())
    sys.exit(_run_tray())
