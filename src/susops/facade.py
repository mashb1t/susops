"""SusOpsManager — the single public API for all SusOps frontends."""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from susops.core.config import (
    Connection,
    PortForward,
    SusOpsConfig,
    get_connection,
    get_default_connection,
    load_config,
    save_config,
)
from susops.core.pac import PacServer, write_pac_file
from susops.core.ports import check_local_port_conflict, get_random_free_port
from susops.core.process import ProcessManager
from susops.core.share import ShareServer, fetch_file, generate_password
from susops.core.ssh import (
    SSH_PROCESS_PREFIX,
    is_tunnel_running,
    start_tunnel,
    stop_tunnel,
    test_ssh_connectivity,
)
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
    """Background thread that samples per-connection bandwidth every 2 seconds.

    Strategy: combine system-wide net_io_counters() for true RX/TX direction
    with per-process read_chars deltas as activity weights. Each SSH connection
    is attributed a fraction of system-wide RX and TX proportional to its share
    of total SSH process I/O activity in the sampling interval.

    With a single connection it gets 100% of system traffic. With multiple
    connections, traffic is split by relative activity. Direction is always
    accurate (bytes_recv vs bytes_sent).
    """

    INTERVAL = 2.0

    def __init__(self, process_mgr: ProcessManager) -> None:
        self._mgr = process_mgr
        self._rates: dict[str, tuple[float, float]] = {}  # tag -> (rx_bps, tx_bps)
        self._prev_net: tuple[float, float, float] | None = None  # (rx, tx, t)
        self._prev_chars: dict[str, float] = {}  # tag -> read_chars snapshot
        self._lock = threading.Lock()
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

        # System-wide directional counters
        net = psutil.net_io_counters()
        if net is None:
            return
        sys_rx = float(net.bytes_recv)
        sys_tx = float(net.bytes_sent)

        # Per-process I/O activity (read_chars as activity proxy)
        proc_chars: dict[str, float] = {}
        for key, _running in self._mgr.status_all().items():
            if not key.startswith(SSH_PROCESS_PREFIX):
                continue
            tag = key[len(SSH_PROCESS_PREFIX) + 1:]
            pid = self._mgr.get_pid(key)
            if pid is None:
                continue
            try:
                proc = psutil.Process(pid)
                all_procs = [proc] + proc.children(recursive=True)
                chars = sum(
                    getattr(p.io_counters(), "read_chars", 0)
                    for p in all_procs
                    if p.is_running()
                )
                proc_chars[tag] = float(chars)
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                pass

        with self._lock:
            if self._prev_net is not None:
                prev_rx, prev_tx, prev_t = self._prev_net
                dt = now - prev_t
                if dt > 0:
                    delta_rx = max(0.0, sys_rx - prev_rx) / dt
                    delta_tx = max(0.0, sys_tx - prev_tx) / dt

                    # Compute per-connection activity deltas
                    deltas: dict[str, float] = {}
                    for tag, chars in proc_chars.items():
                        prev = self._prev_chars.get(tag, chars)
                        deltas[tag] = max(0.0, chars - prev)
                    total_delta = sum(deltas.values()) or 1.0

                    # Attribute system traffic by each connection's share
                    new_rates: dict[str, tuple[float, float]] = {}
                    for tag in proc_chars:
                        weight = deltas.get(tag, 0.0) / total_delta
                        new_rates[tag] = (delta_rx * weight, delta_tx * weight)
                    self._rates = new_rates

            self._prev_net = (sys_rx, sys_tx, now)
            self._prev_chars = dict(proc_chars)

    def get_rate(self, tag: str) -> tuple[float, float]:
        """Return (rx_bps, tx_bps) attributed to this connection."""
        with self._lock:
            return self._rates.get(tag, (0.0, 0.0))


class SusOpsManager:
    """Unified manager for SSH tunnels, PAC server, and file sharing."""

    def __init__(self, workspace: Path = _WORKSPACE_DEFAULT) -> None:
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)

        self.config: SusOpsConfig = load_config(workspace)
        self._process_mgr = ProcessManager(workspace)
        self._pac_server = PacServer()
        self._share_servers: dict[int, tuple[ShareServer, ShareInfo]] = {}
        self._log_buffer: deque[str] = deque(maxlen=500)
        self._bw_sampler = _BandwidthSampler(self._process_mgr)

        self.on_state_change: Callable[[ProcessState], None] | None = None
        self.on_log: Callable[[str], None] | None = None

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _log(self, msg: str) -> None:
        self._log_buffer.append(msg)
        if self.on_log:
            self.on_log(msg)

    def _emit_state(self, state: ProcessState) -> None:
        if self.on_state_change:
            self.on_state_change(state)

    def _reload_config(self) -> None:
        self.config = load_config(self.workspace)

    def _save(self) -> None:
        save_config(self.config, self.workspace)

    def _connection_status(self, conn: Connection) -> ConnectionStatus:
        running = is_tunnel_running(conn.tag, self._process_mgr)
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
        """Return True if something is listening on localhost:port."""
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
            try:
                conn = self._ensure_socks_port(conn)
                pid = start_tunnel(conn, self._process_mgr, self.workspace)
                self._log(f"[{conn.tag}] Started (PID {pid})")
                statuses.append(ConnectionStatus(
                    tag=conn.tag, running=True, pid=pid,
                    socks_port=conn.socks_proxy_port,
                ))
            except Exception as exc:
                msg = f"[{conn.tag}] Failed: {exc}"
                self._log(msg)
                errors.append(msg)
                statuses.append(ConnectionStatus(tag=conn.tag, running=False))

        if not self._pac_server.is_running():
            # Check if another process already has the PAC server running.
            cross_port = self._read_pac_port_file()
            if cross_port and self._probe_port(cross_port):
                self._log(f"PAC server already running (cross-process) on port {cross_port}")
            else:
                # Stale port file (server gone) — clean it up before starting fresh.
                if cross_port:
                    self._remove_pac_port_file()
                try:
                    self._reload_config()
                    pac_port = self._ensure_pac_port()
                    pac_path = write_pac_file(self.config, self.workspace)
                    self._pac_server.start(pac_port, pac_path)
                    self._write_pac_port_file(self._pac_server.get_port())
                    self._log(f"PAC server started on port {pac_port}")
                except Exception as exc:
                    errors.append(f"PAC server failed: {exc}")

        self._emit_state(self._compute_state())
        return StartResult(
            success=not errors,
            message="; ".join(errors) if errors else "Started",
            connection_statuses=tuple(statuses),
        )

    def stop(self, keep_ports: bool = False, force: bool = False) -> StopResult:
        self._reload_config()
        errors = []

        ephemeral = self.config.susops_app.ephemeral_ports
        for conn in self.config.connections:
            try:
                if stop_tunnel(conn.tag, self._process_mgr):
                    self._log(f"[{conn.tag}] Stopped")
                if not keep_ports and ephemeral and conn.socks_proxy_port != 0:
                    updated = conn.model_copy(update={"socks_proxy_port": 0})
                    new_conns = [
                        updated if c.tag == conn.tag else c
                        for c in self.config.connections
                    ]
                    self.config = self.config.model_copy(update={"connections": new_conns})
            except Exception as exc:
                errors.append(f"[{conn.tag}] {exc}")

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
                # PAC server was started by another process — stop it via HTTP.
                try:
                    import urllib.request
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{cross_port}/stop",
                        data=b"",
                        timeout=2,
                    )
                except Exception:
                    pass  # server may already be gone
                self._remove_pac_port_file()
                self._log("PAC server stopped (remote)")

        self._save()
        self._emit_state(self._compute_state())
        return StopResult(
            success=not errors,
            message="; ".join(errors) if errors else "Stopped",
        )

    def restart(self, tag: str | None = None) -> StartResult:
        self.stop(keep_ports=True)
        time.sleep(0.5)
        return self.start(tag)

    def status(self) -> StatusResult:
        self._reload_config()
        statuses = tuple(self._connection_status(c) for c in self.config.connections)
        pac_running = self._pac_server.is_running()
        pac_port = self._pac_server.get_port()
        # Cross-process PAC detection: check port file written by whichever
        # process owns the PAC server (TUI, tray, or CLI are all independent).
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
        if get_connection(self.config, tag) is None:
            raise ValueError(f"Connection '{tag}' not found")
        stop_tunnel(tag, self._process_mgr)
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
            self._pac_server.reload(write_pac_file(self.config, self.workspace))
        self._log(f"[{tag}] Added PAC host '{host}'")

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
            self._pac_server.reload(write_pac_file(self.config, self.workspace))
        self._log(f"Removed PAC host '{host}'")

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

    def add_remote_forward(self, conn_tag: str, fw: PortForward) -> None:
        self._add_forward(conn_tag, fw, "remote")

    def _remove_forward(self, src_port: int, direction: str) -> None:
        self._reload_config()
        found = False
        new_conns = []
        for conn in self.config.connections:
            fwds = conn.forwards.local if direction == "local" else conn.forwards.remote
            updated_fwds = [f for f in fwds if f.src_port != src_port]
            if len(updated_fwds) != len(fwds):
                found = True
                key = "local" if direction == "local" else "remote"
                new_fwds = conn.forwards.model_copy(update={key: updated_fwds})
                new_conns.append(conn.model_copy(update={"forwards": new_fwds}))
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
    # File sharing — multiple concurrent shares
    # ------------------------------------------------------------------ #

    def share(self, file: Path, password: str | None = None, port: int | None = None) -> ShareInfo:
        """Start serving an encrypted file share. Multiple simultaneous shares are supported."""
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file}")
        pw = password or generate_password()
        server = ShareServer()
        info = server.start(file_path=file, password=pw, port=port or 0, workspace=self.workspace)
        self._share_servers[info.port] = (server, info)
        self._log(f"Sharing '{file.name}' on port {info.port}")
        return info

    def stop_share(self, port: int | None = None) -> None:
        """Stop a specific share by port, or all shares if port is None."""
        if port is not None:
            entry = self._share_servers.pop(port, None)
            if entry:
                entry[0].stop()
                self._log(f"File share on port {port} stopped")
        else:
            for p, (server, _) in list(self._share_servers.items()):
                server.stop()
                self._log(f"File share on port {p} stopped")
            self._share_servers.clear()

    def list_shares(self) -> list[ShareInfo]:
        """Return info for all currently active file shares."""
        # Clean up any shares whose server has stopped
        dead = [p for p, (s, _) in self._share_servers.items() if not s.is_running()]
        for p in dead:
            del self._share_servers[p]
        return [info for _, info in self._share_servers.values()]

    def share_is_running(self) -> bool:
        return bool(self.list_shares())

    def fetch(self, port: int, password: str, host: str = "localhost", outfile: Path | None = None) -> Path:
        result = fetch_file(host=host, port=port, password=password, outfile=outfile)
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
        self.stop(force=True)
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
        """Return (rx_bps, tx_bps) instantly from the background sampler."""
        return self._bw_sampler.get_rate(tag)

    def get_process_info(self, tag: str) -> dict:
        """Return CPU%, memory (MB), and active SOCKS connection count for a tunnel.

        Returns empty dict if the process is not running or psutil is unavailable.
        """
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

        try:
            proc = psutil.Process(pid)
            all_procs = [proc] + proc.children(recursive=True)
            cpu = sum(p.cpu_percent(interval=None) for p in all_procs if p.is_running())
            mem_mb = sum(p.memory_info().rss for p in all_procs if p.is_running()) / 1_048_576
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
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return {}

    def get_pac_url(self) -> str:
        port = self._pac_server.get_port() or self.config.pac_server_port
        return f"http://localhost:{port}/susops.pac" if port else ""

    @property
    def app_config(self):
        return self.config.susops_app

    def update_app_config(self, **kwargs) -> None:
        self._reload_config()
        self.config = self.config.model_copy(
            update={"susops_app": self.config.susops_app.model_copy(update=kwargs)}
        )
        self._save()
