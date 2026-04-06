"""SusOpsManager — the single public API for all SusOps frontends.

Both the TUI and the tray apps (Linux/Mac) use this facade exclusively.
No frontend should import from susops.core directly; use this module instead.
"""
from __future__ import annotations

import subprocess
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


class SusOpsManager:
    """Unified manager for SSH tunnels, PAC server, and file sharing.

    Frontends (TUI, Linux tray, Mac tray) instantiate this once and call
    its methods. Event callbacks allow reactive UI updates.
    """

    def __init__(self, workspace: Path = _WORKSPACE_DEFAULT) -> None:
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)

        self.config: SusOpsConfig = load_config(workspace)
        self._process_mgr = ProcessManager(workspace)
        self._pac_server = PacServer()
        self._share_server: ShareServer | None = None
        self._log_buffer: deque[str] = deque(maxlen=500)

        # Event callbacks — set by the frontend
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
        """Assign a random SOCKS port if the connection has port=0."""
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
        """Start SSH tunnel(s) and the PAC server."""
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
            try:
                self._reload_config()
                pac_port = self._ensure_pac_port()
                pac_path = write_pac_file(self.config, self.workspace)
                self._pac_server.start(pac_port, pac_path)
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
        """Stop all SSH tunnels and the PAC server."""
        self._reload_config()
        errors = []

        for conn in self.config.connections:
            try:
                if stop_tunnel(conn.tag, self._process_mgr):
                    self._log(f"[{conn.tag}] Stopped")
                if not keep_ports and conn.socks_proxy_port != 0:
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
                self._log("PAC server stopped")
                if not keep_ports:
                    self.config = self.config.model_copy(update={"pac_server_port": 0})
            except Exception as exc:
                errors.append(f"PAC: {exc}")

        self._save()
        self._emit_state(self._compute_state())
        return StopResult(
            success=not errors,
            message="; ".join(errors) if errors else "Stopped",
        )

    def restart(self, tag: str | None = None) -> StartResult:
        """Stop and restart tunnel(s), preserving port assignments."""
        self.stop(keep_ports=True)
        time.sleep(0.5)
        return self.start(tag)

    def status(self) -> StatusResult:
        """Return current state for all connections and the PAC server."""
        self._reload_config()
        statuses = tuple(self._connection_status(c) for c in self.config.connections)
        pac_running = self._pac_server.is_running()
        pac_port = self._pac_server.get_port() or self.config.pac_server_port
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
        """Add a new SSH connection profile."""
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
        """Remove a connection, stopping its tunnel if running."""
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
        """Test SSH connectivity to a host."""
        return test_ssh_connectivity(ssh_host)

    # ------------------------------------------------------------------ #
    # PAC hosts
    # ------------------------------------------------------------------ #

    def add_pac_host(self, host: str, conn_tag: str | None = None) -> None:
        """Add a PAC host entry. Reloads PAC server if running."""
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
        """Remove a PAC host from whichever connection has it."""
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
        """Add a local port forward (-L) to a connection."""
        self._add_forward(conn_tag, fw, "local")

    def add_remote_forward(self, conn_tag: str, fw: PortForward) -> None:
        """Add a remote port forward (-R) to a connection."""
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
    # File sharing
    # ------------------------------------------------------------------ #

    def share(self, file: Path, password: str | None = None, port: int | None = None) -> ShareInfo:
        """Start serving an encrypted file share."""
        if self._share_server is not None and self._share_server.is_running():
            raise RuntimeError("A file share is already active. Stop it first.")
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file}")
        pw = password or generate_password()
        self._share_server = ShareServer()
        info = self._share_server.start(file_path=file, password=pw, port=port or 0, workspace=self.workspace)
        self._log(f"Sharing '{file.name}' on port {info.port}")
        return info

    def stop_share(self) -> None:
        """Stop the active file share."""
        if self._share_server is not None:
            self._share_server.stop()
            self._share_server = None
            self._log("File share stopped")

    def fetch(self, port: int, password: str, host: str = "localhost", outfile: Path | None = None) -> Path:
        """Download and decrypt a shared file."""
        result = fetch_file(host=host, port=port, password=password, outfile=outfile)
        self._log(f"Fetched file to {result}")
        return result

    def share_is_running(self) -> bool:
        return self._share_server is not None and self._share_server.is_running()

    # ------------------------------------------------------------------ #
    # Testing
    # ------------------------------------------------------------------ #

    def test(self, target: str) -> TestResult:
        """Test connectivity through the SOCKS proxy to a hostname."""
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
        """Test all PAC hosts across all connections."""
        return [self.test(host) for conn in self.config.connections for host in conn.pac_hosts]

    # ------------------------------------------------------------------ #
    # Utilities
    # ------------------------------------------------------------------ #

    def list_config(self) -> SusOpsConfig:
        """Return the current config, reloaded from disk."""
        self._reload_config()
        return self.config

    def reset(self) -> None:
        """Kill all processes and wipe the workspace. Irreversible."""
        self.stop(force=True)
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.config = SusOpsConfig()
        self._save()
        self._log("Workspace reset")

    def get_logs(self, n: int = 100) -> list[str]:
        """Return the last n log entries."""
        return list(self._log_buffer)[-n:]

    def get_bandwidth(self, tag: str) -> tuple[float, float]:
        """Return (rx_bytes_per_sec, tx_bytes_per_sec) for a connection's SSH tunnel.

        Samples psutil io_counters over 1 second. Returns (0.0, 0.0) on error.
        """
        try:
            import psutil
        except ImportError:
            return (0.0, 0.0)
        pid = self._process_mgr.get_pid(f"{SSH_PROCESS_PREFIX}-{tag}")
        if pid is None:
            return (0.0, 0.0)
        try:
            proc = psutil.Process(pid)
            c1 = proc.io_counters()
            time.sleep(1.0)
            c2 = proc.io_counters()
            return (max(0.0, c2.read_bytes - c1.read_bytes),
                    max(0.0, c2.write_bytes - c1.write_bytes))
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            return (0.0, 0.0)

    def get_pac_url(self) -> str:
        """Return the PAC server URL, or empty string if not running."""
        port = self._pac_server.get_port() or self.config.pac_server_port
        return f"http://localhost:{port}/susops.pac" if port else ""

    @property
    def app_config(self):
        return self.config.susops_app

    def update_app_config(self, **kwargs) -> None:
        """Update app-level settings (stop_on_quit, ephemeral_ports, logo_style)."""
        self._reload_config()
        self.config = self.config.model_copy(
            update={"susops_app": self.config.susops_app.model_copy(update=kwargs)}
        )
        self._save()
