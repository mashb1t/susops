"""SusOpsManager — the single public API for all SusOps frontends."""
from __future__ import annotations

import dataclasses
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from rich.markup import escape as markup_escape

from susops.core.config import (
    Connection,
    FileShare,
    PortForward,
    SusOpsConfig,
    get_connection,
    get_default_connection,
    load_config,
    save_config,
)
from susops.core.pac import PacServer, write_pac_file
from susops.core.ports import get_random_free_port
from susops.core.process import ProcessManager
from susops.core.share import ShareServer, fetch_file, generate_password
from susops.core.ssh import (
    FWD_PROCESS_PREFIX,
    SSH_PROCESS_PREFIX,
    find_master_pid,
    is_socket_alive,
    is_tunnel_running,
    socket_path,
    start_forward,
    start_master,
    stop_forward,
    stop_tunnel,
    test_ssh_connectivity,
)
from susops.core.status import StatusServer
from susops.core.types import (
    ConnectionStatus,
    ProcessState,
    ShareInfo,
    StartResult,
    StatusResult,
    StopResult,
    TestResult,
)

__all__ = ["SusOpsManager"]

_WORKSPACE_DEFAULT = Path.home() / ".susops"


class _BandwidthSampler:
    """Background thread that samples per-connection bandwidth every 2 seconds."""

    INTERVAL = 2.0

    def __init__(
            self,
            process_mgr: ProcessManager,
            on_sample: Callable[[str, float, float], None] | None = None,
    ) -> None:
        self._mgr = process_mgr
        self._rates: dict[str, tuple[float, float]] = {}
        self._totals: dict[str, tuple[float, float]] = {}  # tag -> (rx_total_bytes, tx_total_bytes)
        self._prev_net: tuple[float, float, float] | None = None
        self._prev_chars: dict[str, float] = {}
        self._lock = threading.Lock()
        self._on_sample = on_sample
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="susops-bw-sampler"
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                self._sample()
            except Exception:
                pass
            time.sleep(self.INTERVAL)

    def _sample(self) -> None:
        try:
            import psutil
        except ImportError:
            return

        now = time.monotonic()
        net = psutil.net_io_counters()
        if net is None:
            return
        sys_rx = float(net.bytes_recv)
        sys_tx = float(net.bytes_sent)

        # Build tag → list[pid] covering master + all slave processes.
        # Forward slaves are NOT OS children of the master (start_new_session=True),
        # so proc.children() misses them.
        all_entries = self._mgr.status_all()
        master_tags: dict[str, int] = {}
        for key in all_entries:
            if key.startswith(SSH_PROCESS_PREFIX + "-"):
                tag = key[len(SSH_PROCESS_PREFIX) + 1:]
                pid = self._mgr.get_pid(key)
                if pid:
                    master_tags[tag] = pid

        tag_pids: dict[str, list[int]] = {tag: [pid] for tag, pid in master_tags.items()}
        for key in all_entries:
            if key.startswith(FWD_PROCESS_PREFIX + "-"):
                remainder = key[len(FWD_PROCESS_PREFIX) + 1:]
                for tag in master_tags:
                    if remainder.startswith(tag + "-"):
                        pid = self._mgr.get_pid(key)
                        if pid:
                            tag_pids[tag].append(pid)
                        break

        proc_chars: dict[str, float] = {}
        for tag, pids in tag_pids.items():
            chars = 0.0
            for pid in pids:
                try:
                    proc = psutil.Process(pid)
                    chars += float(getattr(proc.io_counters(), "read_chars", 0))
                except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                    pass
            proc_chars[tag] = chars

        with self._lock:
            if self._prev_net is not None:
                prev_rx, prev_tx, prev_t = self._prev_net
                dt = now - prev_t
                if dt > 0:
                    delta_rx = max(0.0, sys_rx - prev_rx) / dt
                    delta_tx = max(0.0, sys_tx - prev_tx) / dt

                    deltas: dict[str, float] = {}
                    for tag, chars in proc_chars.items():
                        prev = self._prev_chars.get(tag, chars)
                        deltas[tag] = max(0.0, chars - prev)
                    total_delta = sum(deltas.values()) or 1.0

                    new_rates: dict[str, tuple[float, float]] = {}
                    for tag in proc_chars:
                        weight = deltas.get(tag, 0.0) / total_delta
                        rx = delta_rx * weight
                        tx = delta_tx * weight
                        new_rates[tag] = (rx, tx)
                        if self._on_sample:
                            try:
                                self._on_sample(tag, rx, tx)
                            except Exception:
                                pass
                    self._rates = new_rates

                    # Accumulate cumulative byte totals (rate × elapsed time = bytes this interval)
                    for tag, (rx, tx) in new_rates.items():
                        prev_rx, prev_tx = self._totals.get(tag, (0.0, 0.0))
                        self._totals[tag] = (prev_rx + rx * dt, prev_tx + tx * dt)

            self._prev_net = (sys_rx, sys_tx, now)
            self._prev_chars = dict(proc_chars)

    def get_rate(self, tag: str) -> tuple[float, float]:
        with self._lock:
            return self._rates.get(tag, (0.0, 0.0))

    def get_totals(self, tag: str) -> tuple[float, float]:
        """Return (rx_total_bytes, tx_total_bytes) accumulated since last reset."""
        with self._lock:
            return self._totals.get(tag, (0.0, 0.0))

    def reset_totals(self, tag: str | None = None) -> None:
        """Reset cumulative counters. Pass tag=None to reset all."""
        with self._lock:
            if tag is None:
                self._totals.clear()
            else:
                self._totals.pop(tag, None)


class SusOpsManager:
    """Unified manager for SSH tunnels, PAC server, and file sharing."""

    def __init__(self, workspace: Path = _WORKSPACE_DEFAULT, verbose: bool = False) -> None:
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._verbose = verbose

        self.config: SusOpsConfig = load_config(workspace)
        self._process_mgr = ProcessManager(workspace)
        self._pac_server = PacServer()
        self._status_server = StatusServer()
        self._share_servers: dict[int, tuple[ShareServer, ShareInfo]] = {}
        self._log_buffer: deque[str] = deque(maxlen=500)
        self._bw_sampler = _BandwidthSampler(
            self._process_mgr, on_sample=self._on_bandwidth
        )
        self._start_times: dict[str, float] = {}  # tag -> time.monotonic() when started

        self.on_state_change: Callable[[ProcessState], None] | None = None
        self.on_log: Callable[[str], None] | None = None

        # Auto-restart PAC server when tunnels are running but this is a
        # fresh process (e.g. TUI restarted without stop_on_quit).
        self._restore_pac()

        if self.config.susops_app.restore_shares_on_start:
            self._restore_shares()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _log(self, msg: str) -> None:
        msg = markup_escape(msg)

        self._log_buffer.append(msg)
        if self.on_log:
            self.on_log(msg)

    def _debug(self, msg: str) -> None:
        """Log a debug message. Only active when verbose=True.

        In TUI/tray mode the message goes to the Logs tab via on_log.
        In CLI mode (no on_log handler) it is printed to stderr.
        """
        if not self._verbose:
            return
        full = f"[debug] {msg}"
        self._log_buffer.append(full)
        if self.on_log:
            self.on_log(full)
        else:
            import sys
            print(full, file=sys.stderr)

    def _emit(self, event: str, data: dict) -> None:
        """Emit an SSE event and log it when verbose (bandwidth excluded — too noisy)."""
        self._status_server.emit(event, data)
        if event != "bandwidth":
            self._debug(f"event:{event} {data}")

    def _emit_state(self, state: ProcessState) -> None:
        if self.on_state_change:
            self.on_state_change(state)

    def _reload_config(self) -> None:
        self.config = load_config(self.workspace)

    def _save(self) -> None:
        save_config(self.config, self.workspace)

    def _on_bandwidth(self, tag: str, rx: float, tx: float) -> None:
        self._status_server.emit("bandwidth", {"tag": tag, "rx_bps": rx, "tx_bps": tx})

    def _connection_status(self, conn: Connection) -> ConnectionStatus:
        running = is_tunnel_running(conn.tag, self._process_mgr)
        # Fall back to socket liveness when PID file is stale (zombie reaped,
        # or master restarted outside our control).
        if not running and is_socket_alive(conn.tag, self.workspace):
            running = True
            # Try to recover the PID from /proc so the dashboard can show it.
            recovered = find_master_pid(conn.tag, self.workspace)
            if recovered:
                name = f"{SSH_PROCESS_PREFIX}-{conn.tag}"
                self._process_mgr._pid_file(name).write_text(str(recovered))
        pid = self._process_mgr.get_pid(f"{SSH_PROCESS_PREFIX}-{conn.tag}")
        return ConnectionStatus(
            tag=conn.tag,
            running=running,
            pid=pid,
            socks_port=conn.socks_proxy_port,
        )

    def _ensure_socks_port(self, conn: Connection) -> Connection:
        if conn.socks_proxy_port != 0:
            return conn
        port = get_random_free_port()
        updated = conn.model_copy(update={"socks_proxy_port": port})
        new_connections = [
            updated if c.tag == conn.tag else c
            for c in self.config.connections
        ]
        self.config = self.config.model_copy(update={"connections": new_connections})
        self._save()
        self._log(f"[{conn.tag}] Assigned SOCKS port {port}")
        return updated

    # ------------------------------------------------------------------ #
    # PAC port-file helpers (cross-process PAC status detection)
    # ------------------------------------------------------------------ #

    @property
    def _pac_port_file(self) -> "Path":
        return self.workspace / "pids" / "susops-pac.port"

    def _write_pac_port_file(self, port: int) -> None:
        self._pac_port_file.parent.mkdir(parents=True, exist_ok=True)
        self._pac_port_file.write_text(str(port))

    def _remove_pac_port_file(self) -> None:
        self._pac_port_file.unlink(missing_ok=True)

    def _read_pac_port_file(self) -> int:
        try:
            return int(self._pac_port_file.read_text().strip())
        except Exception:
            return 0

    @staticmethod
    def _probe_port(port: int) -> bool:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False

    def _ensure_pac_port(self) -> int:
        if self.config.pac_server_port != 0:
            return self.config.pac_server_port
        port = get_random_free_port()
        self.config = self.config.model_copy(update={"pac_server_port": port})
        self._save()
        return port

    def _compute_state(
            self,
            statuses: tuple[ConnectionStatus, ...] | None = None,
            pac_running: bool | None = None,
    ) -> ProcessState:
        if statuses is None:
            statuses = tuple(self._connection_status(c) for c in self.config.connections)
        if pac_running is None:
            pac_running = self._pac_server.is_running()
        if not self.config.connections:
            return ProcessState.STOPPED
        running_count = sum(1 for s in statuses if s.running)
        total = len(statuses)
        if running_count == total and pac_running:
            return ProcessState.RUNNING
        if running_count == 0 and not pac_running:
            return ProcessState.STOPPED
        return ProcessState.STOPPED_PARTIALLY

    # ------------------------------------------------------------------ #
    # Share persistence helpers
    # ------------------------------------------------------------------ #

    def _restore_pac(self) -> None:
        """Restart the PAC server if SSH tunnels are running but PAC is dead.

        Called on __init__ so the PAC server is recovered after a TUI restart
        without stop_on_quit (the daemon thread died with the previous process).
        Uses both PID-file and socket-liveness checks so a stale PID file
        (daemon thread deleted it mid-shutdown) doesn't prevent PAC restore.
        """
        any_tunnel = False
        for conn in self.config.connections:
            if is_tunnel_running(conn.tag, self._process_mgr):
                any_tunnel = True
            elif is_socket_alive(conn.tag, self.workspace):
                any_tunnel = True
                # PID file is stale — recover PID so future checks don't re-enter here
                recovered = find_master_pid(conn.tag, self.workspace)
                if recovered:
                    name = f"{SSH_PROCESS_PREFIX}-{conn.tag}"
                    self._process_mgr._pid_file(name).write_text(str(recovered))
        if not any_tunnel:
            return
        port = self.config.pac_server_port
        if not port:
            # Port unknown — let start() allocate one when user next calls start
            return
        if self._probe_port(port):
            # A cross-process PAC server is still serving (e.g. tray app)
            self._log(f"PAC server already running (cross-process) on port {port}")
            return
        try:
            pac_path = write_pac_file(self.config, self.workspace, active_tags=self._active_tags())
            self._pac_server.start(port, pac_path)
            self._write_pac_port_file(port)
            self._log(f"PAC server restored on port {port}")
        except Exception as exc:
            self._log(f"PAC restore failed: {exc}")

    def _restore_shares(self) -> None:
        """Restart share servers for persisted FileShare entries whose connection is running.

        Skips entries the user manually stopped (stopped=True).
        """
        for conn in self.config.connections:
            if not is_tunnel_running(conn.tag, self._process_mgr):
                continue  # shares are meaningless without a live tunnel
            for fs in conn.file_shares:
                if fs.stopped:
                    continue  # user manually stopped this share — don't auto-restart
                file_path = Path(fs.file_path)
                if not file_path.exists():
                    self._log(
                        f"[{conn.tag}] Share '{fs.file_path}' not found on disk — skipping restore"
                    )
                    continue
                try:
                    server = ShareServer()
                    info_raw = server.start(
                        file_path=file_path,
                        password=fs.password,
                        port=fs.port,
                        workspace=self.workspace,
                    )
                    # Write back the actual port if it changed
                    actual_port = info_raw.port
                    info = ShareInfo(
                        file_path=str(file_path),
                        port=actual_port,
                        password=fs.password,
                        url=f"http://localhost:{actual_port}",
                        conn_tag=conn.tag,
                        running=True,
                    )
                    self._share_servers[actual_port] = (server, info)
                    if actual_port != fs.port:
                        self._update_file_share_port(conn.tag, fs, actual_port)
                    self._log(f"[{conn.tag}] Restored share '{file_path.name}' on port {actual_port}")
                except Exception as exc:
                    self._log(f"[{conn.tag}] Failed to restore share '{fs.file_path}': {exc}")

    def _update_file_share_port(
            self, conn_tag: str, fs: FileShare, new_port: int
    ) -> None:
        """Update the stored port for a FileShare entry in config."""
        conn = get_connection(self.config, conn_tag)
        if conn is None:
            return
        updated_shares = [
            fs.model_copy(update={"port": new_port}) if f.file_path == fs.file_path else f
            for f in conn.file_shares
        ]
        updated_conn = conn.model_copy(update={"file_shares": updated_shares})
        self.config = self.config.model_copy(
            update={
                "connections": [
                    updated_conn if c.tag == conn_tag else c
                    for c in self.config.connections
                ]
            }
        )
        self._save()

    def _add_file_share_to_config(
            self, conn_tag: str, file_path: str, password: str, port: int
    ) -> None:
        conn = get_connection(self.config, conn_tag)
        if conn is None:
            return
        # Update existing entry (clear stopped flag on re-share) or append new
        existing = [f for f in conn.file_shares if f.file_path == file_path]
        if existing:
            new_shares = [
                fs.model_copy(update={"password": password, "port": port, "stopped": False})
                if fs.file_path == file_path else fs
                for fs in conn.file_shares
            ]
        else:
            new_shares = list(conn.file_shares) + [
                FileShare(file_path=file_path, password=password, port=port)
            ]
        updated = conn.model_copy(update={"file_shares": new_shares})
        self.config = self.config.model_copy(
            update={
                "connections": [
                    updated if c.tag == conn_tag else c
                    for c in self.config.connections
                ]
            }
        )
        self._save()

    def _remove_file_share_from_config(self, port: int) -> None:
        new_conns = []
        for conn in self.config.connections:
            updated_shares = [f for f in conn.file_shares if f.port != port]
            if len(updated_shares) != len(conn.file_shares):
                new_conns.append(conn.model_copy(update={"file_shares": updated_shares}))
            else:
                new_conns.append(conn)
        self.config = self.config.model_copy(update={"connections": new_conns})
        self._save()

    def _set_file_share_stopped(self, port: int, stopped: bool) -> None:
        """Update the stopped flag on a persisted FileShare entry."""
        new_conns = []
        for conn in self.config.connections:
            updated = [
                fs.model_copy(update={"stopped": stopped}) if fs.port == port else fs
                for fs in conn.file_shares
            ]
            new_conns.append(conn.model_copy(update={"file_shares": updated}))
        self.config = self.config.model_copy(update={"connections": new_conns})
        self._save()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self, tag: str | None = None) -> StartResult:
        self._reload_config()
        connections = (
            [get_connection(self.config, tag)] if tag
            else list(self.config.connections)
        )
        connections = [c for c in connections if c is not None]

        if not connections:
            return StartResult(success=False, message="No connections configured")

        statuses = []
        errors = []

        for conn in connections:
            if is_tunnel_running(conn.tag, self._process_mgr):
                self._log(f"[{conn.tag}] Already running")
                statuses.append(self._connection_status(conn))
                continue
            if is_socket_alive(conn.tag, self.workspace):
                # ControlMaster is alive but our PID file is stale (e.g.
                # process was a zombie that was reaped). Don't start a
                # second master — re-adopt by re-tracking the socket owner.
                self._log(f"[{conn.tag}] Socket alive but PID stale — skipping new master")
                statuses.append(self._connection_status(conn))
                continue
            try:
                conn = self._ensure_socks_port(conn)
                pid = start_master(conn, self._process_mgr, self.workspace)
                self._log(f"[{conn.tag}] Master started (PID {pid})")

                # Start configured local/remote forwards as slaves
                for fw in conn.forwards.local:
                    try:
                        start_forward(conn, fw, "local", self._process_mgr, self.workspace)
                    except Exception as exc:
                        self._log(f"[{conn.tag}] Forward {fw.src_port} failed: {exc}")

                for fw in conn.forwards.remote:
                    try:
                        start_forward(conn, fw, "remote", self._process_mgr, self.workspace)
                    except Exception as exc:
                        self._log(f"[{conn.tag}] Forward {fw.src_port} failed: {exc}")

                # Start HTTP servers for config-only (stopped) shares, then forward slaves
                # for all running share servers belonging to this connection.
                for fs in conn.file_shares:
                    if fs.stopped:
                        continue  # user manually stopped — do not auto-restart
                    if fs.port in self._share_servers:
                        continue  # already running
                    fp = Path(fs.file_path)
                    if not fp.exists():
                        self._log(f"[{conn.tag}] Share '{fs.file_path}' not found — skipping")
                        continue
                    try:
                        srv = ShareServer()
                        raw = srv.start(file_path=fp, password=fs.password,
                                        port=fs.port, workspace=self.workspace)
                        si = ShareInfo(
                            file_path=str(fp), port=raw.port, password=fs.password,
                            url=f"http://localhost:{raw.port}", conn_tag=conn.tag, running=True,
                        )
                        self._share_servers[raw.port] = (srv, si)
                        if raw.port != fs.port:
                            self._update_file_share_port(conn.tag, fs, raw.port)
                        self._log(f"[{conn.tag}] Started share '{fp.name}' on port {raw.port}")
                        self._emit("share", {
                            "port": raw.port,
                            "file": fp.name,
                            "running": True,
                            "conn_tag": conn.tag,
                        })
                    except Exception as exc:
                        self._log(f"[{conn.tag}] Failed to start share '{fs.file_path}': {exc}")

                for share_port, (_server, share_info) in list(self._share_servers.items()):
                    if share_info.conn_tag == conn.tag:
                        fw = PortForward(
                            src_port=share_port,
                            dst_port=share_port,
                            src_addr="localhost",
                            dst_addr="localhost",
                            tag=f"share-{share_port}",
                        )
                        try:
                            start_forward(conn, fw, "remote", self._process_mgr, self.workspace)
                        except Exception as exc:
                            self._log(f"[{conn.tag}] Share forward {share_port} failed: {exc}")

                statuses.append(ConnectionStatus(
                    tag=conn.tag, running=True, pid=pid,
                    socks_port=conn.socks_proxy_port,
                ))
                self._start_times[conn.tag] = time.monotonic()
                self._emit("state", {"tag": conn.tag, "running": True, "pid": pid})
            except Exception as exc:
                msg = f"[{conn.tag}] Failed: {exc}"
                self._log(msg)
                errors.append(msg)
                statuses.append(ConnectionStatus(tag=conn.tag, running=False))
                self._emit("state", {"tag": conn.tag, "running": False, "pid": None})

        if not self._pac_server.is_running():
            cross_port = self._read_pac_port_file()
            if cross_port and self._probe_port(cross_port):
                self._log(f"PAC server already running (cross-process) on port {cross_port}")
            else:
                if cross_port:
                    self._remove_pac_port_file()
                try:
                    self._reload_config()
                    pac_port = self._ensure_pac_port()
                    pac_path = write_pac_file(self.config, self.workspace, active_tags=self._active_tags())
                    self._pac_server.start(pac_port, pac_path)
                    self._write_pac_port_file(self._pac_server.get_port())
                    self._log(f"PAC server started on port {pac_port}")
                except Exception as exc:
                    errors.append(f"PAC server failed: {exc}")

        # Start status server if not already running
        if not self._status_server.is_running():
            try:
                status_port = self.config.susops_app.status_server_port
                actual_port = self._status_server.start(port=status_port)
                if actual_port != status_port and status_port == 0:
                    self.config = self.config.model_copy(
                        update={
                            "susops_app": self.config.susops_app.model_copy(
                                update={"status_server_port": actual_port}
                            )
                        }
                    )
                    self._save()
                self._log(f"Status server started on port {actual_port}")
            except Exception as exc:
                self._log(f"Status server failed: {exc}")

        self._emit_state(self._compute_state())
        return StartResult(
            success=not errors,
            message="; ".join(errors) if errors else "Started",
            connection_statuses=tuple(statuses),
        )

    def detach_pac(self) -> None:
        """Hand the PAC server off to a background process so it survives TUI quit.

        Stops the in-process daemon thread and spawns an identical standalone
        process on the same port.  The port file is left intact so other processes
        (tray, next TUI session) can find and stop the server via POST /stop.
        """
        if not self._pac_server.is_running():
            return
        port = self._pac_server.get_port()
        pac_path = self._pac_server.get_pac_path()
        if not pac_path:
            return
        self._pac_server.stop()
        subprocess.Popen(
            [sys.executable, "-m", "susops.core.pac", "--port", str(port), "--pac-file", str(pac_path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._write_pac_port_file(port)
        self._log(f"PAC server detached to background process on port {port}")

    def _active_tags(self) -> set[str]:
        """Return the set of connection tags that are currently running."""
        return {
            conn.tag for conn in self.config.connections
            if is_tunnel_running(conn.tag, self._process_mgr) or is_socket_alive(conn.tag, self.workspace)
        }

    def _start_master_only(self, conn_tag: str) -> None:
        """Start only the SSH ControlMaster for conn_tag — no forwards, PAC, or shares.

        Used by fetch() to establish connectivity without touching any other
        configured services.  Returns immediately once the socket appears.
        """
        conn = get_connection(self.config, conn_tag)
        if conn is None:
            raise ValueError(f"Connection '{conn_tag}' not found")
        if is_tunnel_running(conn_tag, self._process_mgr) or is_socket_alive(conn_tag, self.workspace):
            return  # already up
        conn = self._ensure_socks_port(conn)
        pid = start_master(conn, self._process_mgr, self.workspace)
        self._log(f"[{conn_tag}] Master started for fetch (PID {pid})")
        sock = socket_path(conn_tag, self.workspace)
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.1)

    def stop(self, keep_ports: bool = False, tag: str | None = None) -> StopResult:
        self._reload_config()
        errors = []

        ephemeral = self.config.susops_app.ephemeral_ports
        connections = (
            [get_connection(self.config, tag)] if tag
            else list(self.config.connections)
        )
        connections = [c for c in connections if c is not None]
        for conn in connections:
            try:
                if stop_tunnel(conn.tag, self._process_mgr, self.workspace, conn.ssh_host):
                    self._log(f"[{conn.tag}] Stopped")
                    self._bw_sampler.reset_totals(conn.tag)
                    self._start_times.pop(conn.tag, None)
                    self._emit("state", {"tag": conn.tag, "running": False, "pid": None})
                if not keep_ports and ephemeral and conn.socks_proxy_port != 0:
                    updated = conn.model_copy(update={"socks_proxy_port": 0})
                    new_conns = [
                        updated if c.tag == conn.tag else c
                        for c in self.config.connections
                    ]
                    self.config = self.config.model_copy(update={"connections": new_conns})
            except Exception as exc:
                errors.append(f"[{conn.tag}] {exc}")

        # Stop share servers for the affected connections
        stopped_tags = {c.tag for c in connections}
        for p, (server, info) in list(self._share_servers.items()):
            if info.conn_tag in stopped_tags:
                try:
                    server.stop()
                    self._log(f"File share on port {p} stopped")
                    self._emit("share", {
                        "port": p,
                        "file": Path(info.file_path).name,
                        "running": False,
                        "conn_tag": info.conn_tag,
                    })
                    del self._share_servers[p]
                except Exception as exc:
                    errors.append(f"Share {p}: {exc}")

        if tag is None:
            if self._pac_server.is_running():
                try:
                    self._pac_server.stop()
                    self._remove_pac_port_file()
                    self._log("PAC server stopped")
                    if not keep_ports and ephemeral:
                        self.config = self.config.model_copy(update={"pac_server_port": 0})
                except Exception as exc:
                    errors.append(f"PAC: {exc}")
            else:
                cross_port = self._read_pac_port_file()
                if cross_port:
                    try:
                        import urllib.request
                        urllib.request.urlopen(
                            f"http://127.0.0.1:{cross_port}/stop",
                            data=b"",
                            timeout=2,
                        )
                    except Exception:
                        pass
                    self._remove_pac_port_file()
                    self._log("PAC server stopped (remote)")

        # Regenerate PAC (or stop it) when stopping a single connection
        if tag is not None:
            remaining = self._active_tags()
            if not remaining:
                # Last connection stopped — shut down PAC server entirely
                if self._pac_server.is_running():
                    try:
                        self._pac_server.stop()
                        self._remove_pac_port_file()
                        self._log("PAC server stopped (no active connections)")
                        if not keep_ports and ephemeral:
                            self.config = self.config.model_copy(update={"pac_server_port": 0})
                    except Exception as exc:
                        errors.append(f"PAC: {exc}")
                else:
                    cross_port = self._read_pac_port_file()
                    if cross_port:
                        try:
                            import urllib.request
                            urllib.request.urlopen(
                                f"http://127.0.0.1:{cross_port}/stop",
                                data=b"",
                                timeout=2,
                            )
                        except Exception:
                            pass
                        self._remove_pac_port_file()
                        self._log("PAC server stopped (no active connections, remote)")
            elif self._pac_server.is_running():
                self._pac_server.reload(write_pac_file(self.config, self.workspace, active_tags=remaining))

        self._save()
        self._emit_state(self._compute_state())
        return StopResult(
            success=not errors,
            message="; ".join(errors) if errors else "Stopped",
        )

    def restart(self, tag: str | None = None) -> StartResult:
        self.stop(keep_ports=True, tag=tag)
        time.sleep(0.5)
        return self.start(tag)

    def status(self) -> StatusResult:
        self._reload_config()
        statuses = tuple(self._connection_status(c) for c in self.config.connections)
        pac_running = self._pac_server.is_running()
        pac_port = self._pac_server.get_port()
        if not pac_running:
            pac_port = pac_port or self._read_pac_port_file()
            if pac_port:
                pac_running = self._probe_port(pac_port)
        pac_port = pac_port or self.config.pac_server_port
        return StatusResult(
            state=self._compute_state(statuses, pac_running),
            connection_statuses=statuses,
            pac_running=pac_running,
            pac_port=pac_port,
        )

    # ------------------------------------------------------------------ #
    # Connection CRUD
    # ------------------------------------------------------------------ #

    def add_connection(self, tag: str, ssh_host: str, socks_port: int = 0) -> Connection:
        self._reload_config()
        if get_connection(self.config, tag) is not None:
            raise ValueError(f"Connection '{tag}' already exists")
        conn = Connection(tag=tag, ssh_host=ssh_host, socks_proxy_port=socks_port)
        self.config = self.config.model_copy(
            update={"connections": list(self.config.connections) + [conn]}
        )
        self._save()
        self._log(f"Added connection '{tag}' → {ssh_host}")
        return conn

    def remove_connection(self, tag: str) -> None:
        self._reload_config()
        conn = get_connection(self.config, tag)
        if conn is None:
            raise ValueError(f"Connection '{tag}' not found")
        stop_tunnel(tag, self._process_mgr, self.workspace, conn.ssh_host)
        self.config = self.config.model_copy(
            update={"connections": [c for c in self.config.connections if c.tag != tag]}
        )
        self._save()
        self._log(f"Removed connection '{tag}'")

    def test_ssh(self, ssh_host: str) -> bool:
        return test_ssh_connectivity(ssh_host)

    # ------------------------------------------------------------------ #
    # PAC hosts
    # ------------------------------------------------------------------ #

    def add_pac_host(self, host: str, conn_tag: str | None = None) -> None:
        self._reload_config()
        default = get_default_connection(self.config)
        tag = conn_tag or (default.tag if default else None)
        if tag is None:
            raise ValueError("No connections configured")
        conn = get_connection(self.config, tag)
        if conn is None:
            raise ValueError(f"Connection '{tag}' not found")
        if host in conn.pac_hosts:
            raise ValueError(f"Host '{host}' already in PAC list for '{tag}'")
        updated = conn.model_copy(update={"pac_hosts": list(conn.pac_hosts) + [host]})
        self.config = self.config.model_copy(
            update={"connections": [updated if c.tag == tag else c for c in self.config.connections]}
        )
        self._save()
        if self._pac_server.is_running():
            self._pac_server.reload(write_pac_file(self.config, self.workspace, active_tags=self._active_tags()))
        self._log(f"[{tag}] Added PAC host '{host}'")
        self._emit_state(self._compute_state())

    def remove_pac_host(self, host: str) -> None:
        self._reload_config()
        found = False
        new_conns = []
        for conn in self.config.connections:
            if host in conn.pac_hosts:
                found = True
                new_conns.append(
                    conn.model_copy(update={"pac_hosts": [h for h in conn.pac_hosts if h != host]})
                )
            else:
                new_conns.append(conn)
        if not found:
            raise ValueError(f"Host '{host}' not found in any PAC list")
        self.config = self.config.model_copy(update={"connections": new_conns})
        self._save()
        if self._pac_server.is_running():
            self._pac_server.reload(write_pac_file(self.config, self.workspace, active_tags=self._active_tags()))
        self._log(f"Removed PAC host '{host}'")
        self._emit_state(self._compute_state())

    # ------------------------------------------------------------------ #
    # Port forwards
    # ------------------------------------------------------------------ #

    def _add_forward(self, conn_tag: str, fw: PortForward, direction: str) -> None:
        self._reload_config()
        conn = get_connection(self.config, conn_tag)
        if conn is None:
            raise ValueError(f"Connection '{conn_tag}' not found")
        if direction == "local":
            if any(f.src_port == fw.src_port for f in conn.forwards.local):
                raise ValueError(f"Local forward on port {fw.src_port} already exists")
            new_fwds = conn.forwards.model_copy(
                update={"local": list(conn.forwards.local) + [fw]}
            )
        else:
            if any(f.src_port == fw.src_port for f in conn.forwards.remote):
                raise ValueError(f"Remote forward on port {fw.src_port} already exists")
            new_fwds = conn.forwards.model_copy(
                update={"remote": list(conn.forwards.remote) + [fw]}
            )
        updated = conn.model_copy(update={"forwards": new_fwds})
        self.config = self.config.model_copy(
            update={"connections": [updated if c.tag == conn_tag else c for c in self.config.connections]}
        )
        self._save()
        self._log(f"[{conn_tag}] Added {direction} forward {fw.src_port}→{fw.dst_port}")

    def add_local_forward(self, conn_tag: str, fw: PortForward) -> None:
        self._add_forward(conn_tag, fw, "local")
        # If master is running, start the slave immediately (no restart needed)
        conn = get_connection(self.config, conn_tag)
        if conn and is_tunnel_running(conn_tag, self._process_mgr):
            try:
                start_forward(conn, fw, "local", self._process_mgr, self.workspace)
                self._emit("forward", {
                    "tag": conn_tag, "fw_tag": fw.tag or f"local-{fw.src_port}",
                    "direction": "local", "running": True,
                })
            except Exception as exc:
                self._log(f"[{conn_tag}] Could not start forward slave: {exc}")

    def add_remote_forward(self, conn_tag: str, fw: PortForward) -> None:
        self._add_forward(conn_tag, fw, "remote")
        conn = get_connection(self.config, conn_tag)
        if conn and is_tunnel_running(conn_tag, self._process_mgr):
            try:
                start_forward(conn, fw, "remote", self._process_mgr, self.workspace)
                self._emit("forward", {
                    "tag": conn_tag, "fw_tag": fw.tag or f"remote-{fw.src_port}",
                    "direction": "remote", "running": True,
                })
            except Exception as exc:
                self._log(f"[{conn_tag}] Could not start forward slave: {exc}")

    def _remove_forward(self, src_port: int, direction: str) -> None:
        self._reload_config()
        found = False
        new_conns = []
        for conn in self.config.connections:
            fwds = conn.forwards.local if direction == "local" else conn.forwards.remote
            updated_fwds = [f for f in fwds if f.src_port != src_port]
            if len(updated_fwds) != len(fwds):
                found = True
                removed_fw = next(f for f in fwds if f.src_port == src_port)
                key = "local" if direction == "local" else "remote"
                new_fwds = conn.forwards.model_copy(update={key: updated_fwds})
                new_conns.append(conn.model_copy(update={"forwards": new_fwds}))
                # Stop the slave process if it exists
                fw_tag = removed_fw.tag or f"{direction}-{src_port}"
                stop_forward(conn.tag, fw_tag, self._process_mgr)
                self._emit("forward", {
                    "tag": conn.tag, "fw_tag": fw_tag,
                    "direction": direction, "running": False,
                })
            else:
                new_conns.append(conn)
        if not found:
            raise ValueError(f"{direction.capitalize()} forward on port {src_port} not found")
        self.config = self.config.model_copy(update={"connections": new_conns})
        self._save()
        self._log(f"Removed {direction} forward on port {src_port}")

    def remove_local_forward(self, src_port: int) -> None:
        self._remove_forward(src_port, "local")

    def remove_remote_forward(self, src_port: int) -> None:
        self._remove_forward(src_port, "remote")

    # ------------------------------------------------------------------ #
    # File sharing
    # ------------------------------------------------------------------ #

    def share(
            self,
            file: Path,
            conn_tag: str,
            password: str | None = None,
            port: int | None = None,
    ) -> ShareInfo:
        """Start serving an encrypted file share and persist it to config.

        If the connection's SSH tunnel is not running it is started automatically
        so the remote forward slave can be established immediately.
        """
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file}")
        self._reload_config()
        if get_connection(self.config, conn_tag) is None:
            raise ValueError(f"Connection '{conn_tag}' not found")

        pw = password or generate_password()
        server = ShareServer()
        _raw = server.start(file_path=file, password=pw, port=port or 0, workspace=self.workspace)

        info = ShareInfo(
            file_path=_raw.file_path,
            port=_raw.port,
            password=_raw.password,
            url=_raw.url,
            conn_tag=conn_tag,
            running=True,
        )
        # Register in memory and config BEFORE checking tunnel state so that
        # self.start() (below) can pick up this share when iterating _share_servers.
        self._share_servers[info.port] = (server, info)
        self._log(f"Sharing '{file.name}' on port {info.port}")
        self._add_file_share_to_config(conn_tag, str(file), pw, info.port)

        conn = get_connection(self.config, conn_tag)
        if conn and not is_tunnel_running(conn_tag, self._process_mgr):
            # Tunnel not running — start it; start() will also launch the remote forward slave.
            self.start(conn_tag)
        elif conn and is_tunnel_running(conn_tag, self._process_mgr):
            # Tunnel already running — start the slave directly.
            fw = PortForward(
                src_port=info.port,
                dst_port=info.port,
                src_addr="localhost",
                dst_addr="localhost",
                tag=f"share-{info.port}",
            )
            try:
                start_forward(conn, fw, "remote", self._process_mgr, self.workspace)
            except Exception as exc:
                self._log(f"[{conn_tag}] Share forward {info.port} failed: {exc}")

        self._emit("share", {
            "port": info.port,
            "file": file.name,
            "running": True,
            "conn_tag": conn_tag,
        })
        return info

    def stop_share(self, port: int | None = None) -> None:
        """Stop share server(s) without removing from config (entry shows as stopped).

        Sets stopped=True on the config entry so the share is not auto-restarted
        on the next start() or restore cycle.
        """
        if port is not None:
            entry = self._share_servers.pop(port, None)
            if entry:
                entry[0].stop()
                info = entry[1]
                self._log(f"File share on port {port} stopped")
                if info.conn_tag:
                    stop_forward(info.conn_tag, f"share-{port}", self._process_mgr)
                self._set_file_share_stopped(port, True)
                self._emit("share", {
                    "port": port,
                    "file": Path(info.file_path).name,
                    "running": False,
                    "conn_tag": info.conn_tag,
                })
            else:
                # Offline share (not in _share_servers): mark as manually stopped in config
                self._set_file_share_stopped(port, True)
        else:
            for p, (server, info) in list(self._share_servers.items()):
                server.stop()
                self._log(f"File share on port {p} stopped")
                if info.conn_tag:
                    stop_forward(info.conn_tag, f"share-{p}", self._process_mgr)
                self._emit("share", {
                    "port": p,
                    "file": Path(info.file_path).name,
                    "running": False,
                    "conn_tag": info.conn_tag,
                })
            self._share_servers.clear()

    def delete_share(self, port: int) -> None:
        """Stop and permanently remove a share from config."""
        self.stop_share(port)
        self._remove_file_share_from_config(port)
        self._emit("share", {
            "port": port,
            "file": "",
            "running": False,
            "conn_tag": None,
        })

    def list_shares(self) -> list[ShareInfo]:
        """Return info for all shares: running (in-memory) and stopped (config-only)."""
        # Clean up dead in-memory servers
        dead = [p for p, (s, _) in self._share_servers.items() if not s.is_running()]
        for p in dead:
            del self._share_servers[p]

        # In-memory running shares — attach live access counters
        running_ports = set(self._share_servers.keys())
        result: list[ShareInfo] = []
        for server, info in self._share_servers.values():
            result.append(dataclasses.replace(
                info,
                access_count=server.access_count,
                failed_count=server.failed_count,
            ))

        # Config-only stopped shares (persisted but server not running in this process)
        self._reload_config()
        for conn in self.config.connections:
            for fs in conn.file_shares:
                if fs.port not in running_ports:
                    result.append(ShareInfo(
                        file_path=fs.file_path,
                        port=fs.port,
                        password=fs.password,
                        url=f"http://localhost:{fs.port}",
                        conn_tag=conn.tag,
                        running=False,
                        stopped=fs.stopped,
                    ))

        return result

    def share_is_running(self) -> bool:
        return bool(self._share_servers)

    def fetch(
            self,
            port: int,
            password: str,
            conn_tag: str,
            outfile: Path | None = None,
    ) -> Path:
        """Download and decrypt a shared file via a transient local forward slave.

        The local forward slave is started, the file is downloaded through
        localhost, then the slave is stopped. No tunnel restart required.
        """
        self._reload_config()
        if get_connection(self.config, conn_tag) is None:
            raise ValueError(f"Connection '{conn_tag}' not found")

        local_port = get_random_free_port()
        fw = PortForward(
            src_port=local_port,
            dst_port=port,
            src_addr="localhost",
            dst_addr="localhost",
            tag=f"fetch-{port}",
        )

        forward_started = False

        # Record whether the tunnel was already running so we know whether to
        # tear it down after the fetch.  Then always call _start_master_only:
        # it is a no-op when the master is already up, but crucially it waits
        # for the socket to appear — which the running-connection path previously
        # skipped, causing the forward to be silently omitted.
        tunnel_was_running = is_tunnel_running(conn_tag, self._process_mgr) or is_socket_alive(conn_tag, self.workspace)
        self._start_master_only(conn_tag)
        conn = get_connection(self.config, conn_tag)  # refresh after potential port assignment

        # Use a transient forward slave if ControlMaster socket is alive
        sock = socket_path(conn_tag, self.workspace) if conn else None
        if conn and sock is not None and sock.exists():
            try:
                start_forward(conn, fw, "local", self._process_mgr, self.workspace)
                # Poll until local port is accessible (up to 5 s)
                for _ in range(50):
                    if self._probe_port(local_port):
                        break
                    time.sleep(0.1)
                forward_started = True
            except Exception as exc:
                self._log(f"[{conn_tag}] Fetch forward {port} failed: {exc}")

        try:
            # If forward was started, fetch from local_port; otherwise fall back to original port
            # (useful in test/dev scenarios where share server is running locally)
            fetch_port = local_port if forward_started else port
            result = fetch_file(host="localhost", port=fetch_port, password=password, outfile=outfile)
        finally:
            if forward_started:
                stop_forward(conn_tag, f"fetch-{port}", self._process_mgr)
            if not tunnel_was_running:
                stop_tunnel(conn_tag, self._process_mgr, self.workspace, conn.ssh_host if conn else None)
                self._emit("state", {"tag": conn_tag, "running": False, "pid": None})

        self._log(f"Fetched file to {result}")
        return result

    # ------------------------------------------------------------------ #
    # Testing
    # ------------------------------------------------------------------ #

    def test(self, target: str) -> TestResult:
        conn = get_default_connection(self.config)
        if conn is None or conn.socks_proxy_port == 0:
            return TestResult(target=target, success=False, message="No active SOCKS proxy")
        proxy = f"socks5h://127.0.0.1:{conn.socks_proxy_port}"
        start = time.monotonic()
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--proxy", proxy, "--max-time", "10", f"http://{target}"],
                capture_output=True, timeout=15, text=True,
            )
            latency = (time.monotonic() - start) * 1000
            success = result.returncode == 0
            return TestResult(
                target=target, success=success,
                message=f"HTTP {result.stdout.strip()}" if success else result.stderr.strip(),
                latency_ms=latency if success else None,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return TestResult(target=target, success=False, message=str(exc))

    def test_all(self) -> list[TestResult]:
        return [self.test(host) for conn in self.config.connections for host in conn.pac_hosts]

    # ------------------------------------------------------------------ #
    # Utilities
    # ------------------------------------------------------------------ #

    def list_config(self) -> SusOpsConfig:
        self._reload_config()
        return self.config

    def reset(self) -> None:
        self.stop()
        self.stop_share()
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.config = SusOpsConfig()
        self._save()
        self._log("Workspace reset")

    def get_logs(self, n: int = 100) -> list[str]:
        return list(self._log_buffer)[-n:]

    def get_bandwidth(self, tag: str) -> tuple[float, float]:
        return self._bw_sampler.get_rate(tag)

    def get_bandwidth_totals(self, tag: str) -> tuple[float, float]:
        """Return cumulative (rx_bytes, tx_bytes) since last start. Resets on stop."""
        return self._bw_sampler.get_totals(tag)

    def get_uptime(self, tag: str) -> float | None:
        """Return seconds since connection started, or None if not recorded."""
        start = self._start_times.get(tag)
        return time.monotonic() - start if start is not None else None

    def get_process_info(self, tag: str) -> dict:
        try:
            import psutil
        except ImportError:
            return {}

        pid = self._process_mgr.get_pid(f"{SSH_PROCESS_PREFIX}-{tag}")
        if pid is None:
            return {}

        self._reload_config()
        conn = get_connection(self.config, tag)
        socks_port = conn.socks_proxy_port if conn else 0

        # Collect master PID + all forward slave PIDs for this tag.
        # Slaves are not OS children (start_new_session=True), so children() misses them.
        all_pids = [pid]
        prefix = f"{FWD_PROCESS_PREFIX}-{tag}-"
        for key in self._process_mgr.status_all():
            if key.startswith(prefix):
                slave_pid = self._process_mgr.get_pid(key)
                if slave_pid:
                    all_pids.append(slave_pid)

        cpu = 0.0
        mem_mb = 0.0
        for p_pid in all_pids:
            try:
                proc = psutil.Process(p_pid)
                cpu += proc.cpu_percent(interval=None)
                mem_mb += proc.memory_info().rss / 1_048_576
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        active_conns = 0
        if socks_port:
            try:
                active_conns = sum(
                    1 for c in psutil.net_connections("tcp")
                    if c.laddr.port == socks_port and c.status == "ESTABLISHED"
                )
            except (psutil.AccessDenied, OSError):
                pass
        return {"cpu": cpu, "mem_mb": mem_mb, "conns": active_conns}

    def get_pac_url(self) -> str:
        port = self._pac_server.get_port() or self.config.pac_server_port
        return f"http://localhost:{port}/susops.pac" if port else ""

    def get_status_url(self) -> str:
        port = self._status_server.get_port()
        return f"http://localhost:{port}/events" if port else ""

    @property
    def app_config(self):
        return self.config.susops_app

    def update_app_config(self, **kwargs) -> None:
        self._reload_config()
        self.config = self.config.model_copy(
            update={"susops_app": self.config.susops_app.model_copy(update=kwargs)}
        )
        self._save()
