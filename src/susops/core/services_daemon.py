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


def _claim_pid_file(workspace: Path) -> bool:
    """Atomically create the PID file with this process's PID.

    Uses O_CREAT | O_EXCL so two daemons racing each other can't both
    succeed — at most one will create the file, the other gets
    FileExistsError. Returns True if we claimed it, False otherwise.
    """
    p = _pid_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


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

    Atomic via O_EXCL on the PID file — two daemons racing each other
    can't both pass this check. Converts the silent-failure modes that
    bit us during development (orphan accumulation, double-spawn races)
    into loud, actionable exits.

    On success the PID file is written with our pid (which means the
    daemon's shutdown finally block MUST clean it up).
    """
    # Try to claim the PID file atomically.
    if _claim_pid_file(workspace):
        # Won the race uncontested.
        pass
    else:
        # File exists. Is the holder alive?
        pid_file = _pid_path(workspace)
        try:
            existing_pid = int(pid_file.read_text().strip())
            os.kill(existing_pid, 0)  # liveness probe
        except (OSError, ValueError):
            # Stale PID file. Remove + retry the atomic claim once.
            pid_file.unlink(missing_ok=True)
            _port_path(workspace).unlink(missing_ok=True)
            if not _claim_pid_file(workspace):
                # Some other daemon claimed it between our cleanup and retry.
                log.error(
                    "another susops-services daemon raced us to start. "
                    "Try again — or run "
                    "`pgrep -lf susops-services` to inspect."
                )
                sys.exit(_EXIT_ANOTHER_DAEMON_ALIVE)
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
        # We claimed the PID file above; clean it up so the next attempt
        # isn't blocked by us.
        _remove_pid_file(workspace)
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

    # Atomically claim the PID file. Past this point we own it.
    _preflight(workspace, log)

    # Install signal handlers IMMEDIATELY after claiming the PID file so
    # a SIGTERM arriving during the (relatively slow) SusOpsManager init
    # routes through our finally block and cleans up the PID file. The
    # default SIGTERM handler in Python terminates the process without
    # running finally.
    stop_event = threading.Event()

    def _shutdown(signum, _frame) -> None:
        log.info("Received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    mgr = None
    try:
        import time as _time
        from susops.core.rpc_server import serve
        from susops.facade import SusOpsManager

        mgr = SusOpsManager(workspace=workspace, _enable_background_threads=True)
        # CLI `--port` wins; fall back to the configured `rpc_server_port`,
        # then 0 (auto-allocate). When we auto-allocate, persist the bound
        # port back to config so subsequent spawns reuse it.
        requested_port = args.port or mgr.config.rpc_server_port or 0
        runner, actual_port = serve(mgr, port=requested_port)
        if requested_port == 0 and actual_port != mgr.config.rpc_server_port:
            mgr.config = mgr.config.model_copy(
                update={"rpc_server_port": actual_port}
            )
            mgr._save()
        _port_path(workspace).write_text(str(actual_port))
        log.info("RPC listening on 127.0.0.1:%d", actual_port)

        # Start the SSE status server eagerly so frontends can connect
        # immediately on daemon spawn, and so the daemon-startup log can
        # report the SSE port too. Previously this was lazy (started on the
        # first tunnel `start()` call), which left SSE listeners spinning in
        # backoff for several seconds after a fresh daemon spawn.
        sse_port = mgr.ensure_sse_status_server()

        # Surface daemon-startup details in the in-memory log buffer too so
        # they show up in the TUI Logs tab and the tray Logs window — the
        # `log.info` calls only land on the daemon process's stderr.
        try:
            sse_str = f"SSE port {sse_port}" if sse_port else "SSE port unavailable"
            mgr._log(
                f"Daemon started — RPC port {actual_port}, {sse_str} (PID {os.getpid()})"
            )
            mgr._log(f"Workspace: {workspace}")
        except Exception:
            pass

        # Idle-shutdown. Exit when the last SSE client disconnects AND there's
        # no in-flight work (no SSH masters, no shares, no PAC, no watched
        # connections). Startup gets a small grace so a frontend that calls
        # ensure_daemon_running() has time to make its first SSE connection
        # before the periodic check fires.
        _IDLE_STARTUP_GRACE_S = 3.0
        _IDLE_CHECK_INTERVAL_S = 5.0
        startup_time = _time.monotonic()

        def _should_exit() -> bool:
            if _time.monotonic() - startup_time < _IDLE_STARTUP_GRACE_S:
                return False
            if mgr is None:
                return False
            if mgr._status_server.client_count() > 0:
                return False
            return mgr.is_idle()

        def _on_clients_changed(count: int) -> None:
            # Fast path — react immediately when the last SSE client drops.
            # The periodic check below handles the "no SSE was ever opened"
            # case (e.g. `susops ps` fires one RPC and exits).
            if count > 0:
                return
            if _should_exit():
                msg = "Last client disconnected and no work pending — shutting down"
                log.info(msg)
                try:
                    mgr._log(msg)
                except Exception:
                    pass
                stop_event.set()

        mgr._status_server.on_clients_changed = _on_clients_changed
        # Surface SSE connect/disconnect in the in-memory log buffer so the
        # TUI Logs tab and tray Logs window can show "tui connected" etc.
        mgr._status_server.on_log = mgr._log

        def _idle_watcher() -> None:
            while not stop_event.is_set():
                if stop_event.wait(_IDLE_CHECK_INTERVAL_S):
                    return
                if _should_exit():
                    msg = (f"Idle for {_IDLE_CHECK_INTERVAL_S:.0f}s with "
                           f"no clients — shutting down")
                    log.info(msg)
                    try:
                        mgr._log(msg)
                    except Exception:
                        pass
                    stop_event.set()
                    return

        threading.Thread(
            target=_idle_watcher, daemon=True, name="susops-idle-watcher",
        ).start()

        log.info("Daemon started, pid=%d, workspace=%s", os.getpid(), workspace)
        stop_event.wait()
        # If we land here it's because the idle-watcher or a signal asked us
        # to exit — surface it in the in-memory log so frontends can see
        # *why* the daemon went away rather than just noticing it's gone.
        if mgr is not None:
            try:
                mgr._log("Daemon shutting down")
            except Exception:
                pass
    finally:
        # Remove PID + port files FIRST so subsequent ensure_daemon_running()
        # calls don't think we're still alive while we're shutting down.
        # Anything that hangs below can't strand us with a stale PID file.
        _remove_pid_file(workspace)
        _remove_port_file(workspace)

        # Stop the manager (kills SSH masters, stops PAC/status threads).
        # Wrapped in a watchdog timer — if a child won't die we still exit.
        def _watchdog():
            import time as _t
            _t.sleep(5.0)
            log.error("Shutdown watchdog tripped, force-exiting")
            os._exit(1)
        threading.Thread(target=_watchdog, daemon=True, name="susops-services-watchdog").start()

        if mgr is not None:
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
