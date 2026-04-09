"""SSH tunnel management using autossh (or ssh fallback) + ProcessManager.

Architecture:
  - One ControlMaster process per connection (manages the multiplexed socket).
  - Each port forward (local, remote, share) is its own lightweight slave process
    that attaches to the master via the Unix socket.
  - Forwards can be added/removed without restarting the master.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from susops.core.config import Connection, PortForward
from susops.core.process import ProcessManager

__all__ = [
    "build_ssh_cmd",          # legacy alias — kept for CLI compat
    "build_master_cmd",
    "build_forward_cmd",
    "start_tunnel",           # starts master + all configured forwards
    "start_master",
    "start_forward",
    "stop_tunnel",            # stops all forward slaves + master
    "stop_forward",
    "is_tunnel_running",
    "is_socket_alive",
    "socket_path",
    "test_ssh_connectivity",
    "SSH_PROCESS_PREFIX",
    "FWD_PROCESS_PREFIX",
]

SSH_PROCESS_PREFIX = "susops-ssh"
FWD_PROCESS_PREFIX = "susops-fwd"
_SOCKET_DIR = "sockets"


def _master_name(tag: str) -> str:
    return f"{SSH_PROCESS_PREFIX}-{tag}"


def _forward_name(conn_tag: str, fw_tag: str) -> str:
    return f"{FWD_PROCESS_PREFIX}-{conn_tag}-{fw_tag}"


def socket_path(tag: str, workspace: Path) -> Path:
    """Return the ControlMaster Unix socket path for a connection."""
    return workspace / _SOCKET_DIR / f"{tag}.sock"


def build_master_cmd(conn: Connection, sock: Path) -> list[str]:
    """Build the ControlMaster SSH command.

    Always uses plain ssh. ControlPersist keeps the master alive, and
    ServerAliveInterval handles dead connections. Reconnection on failure
    is handled by the facade's polling loop. autossh is incompatible with
    ControlPersist (it monitors a child that immediately forks and exits).
    """
    cmd: list[str] = ["ssh"]

    cmd += [
        "-N", "-T",
        "-D", str(conn.socks_proxy_port),
        "-o", "ControlMaster=yes",
        "-o", f"ControlPath={sock}",
        "-o", "ControlPersist=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ]

    cmd.append(conn.ssh_host)
    return cmd


def build_forward_cmd(
    conn: Connection,
    fw: PortForward,
    direction: str,
    sock: Path,
) -> list[str]:
    """Build a ControlSlave SSH command for a single port forward.

    direction: "local" → -L, "remote" → -R
    Attaches to the existing ControlMaster socket.
    """
    flag = "-L" if direction == "local" else "-R"
    fwd_spec = f"{fw.src_addr}:{fw.src_port}:{fw.dst_addr}:{fw.dst_port}"

    return [
        "ssh",
        "-N", "-T",
        "-o", f"ControlPath={sock}",
        flag, fwd_spec,
        conn.ssh_host,
    ]


# Legacy alias — keeps existing callers (CLI, old tests) working.
def build_ssh_cmd(conn: Connection) -> list[str]:
    """Legacy: build the monolithic SSH command (no ControlMaster).

    Kept for backwards compatibility. New code should use build_master_cmd.
    """
    cmd: list[str] = ["ssh"]

    cmd += [
        "-N", "-T",
        "-D", str(conn.socks_proxy_port),
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ]

    for fw in conn.forwards.local:
        cmd += ["-L", f"{fw.src_addr}:{fw.src_port}:{fw.dst_addr}:{fw.dst_port}"]

    for fw in conn.forwards.remote:
        cmd += ["-R", f"{fw.src_addr}:{fw.src_port}:{fw.dst_addr}:{fw.dst_port}"]

    cmd.append(conn.ssh_host)
    return cmd


def start_master(
    conn: Connection,
    process_mgr: ProcessManager,
    workspace: Path,
) -> int:
    """Start the ControlMaster SSH process for a connection.

    Returns the PID of the started process.
    Raises ValueError if socks_proxy_port is 0.
    Raises RuntimeError if the process fails to start.
    """
    if conn.socks_proxy_port == 0:
        raise ValueError(
            f"Connection '{conn.tag}' has no SOCKS port assigned. "
            "Assign one before starting."
        )

    sock = socket_path(conn.tag, workspace)
    sock.parent.mkdir(parents=True, exist_ok=True)

    name = _master_name(conn.tag)
    cmd = build_master_cmd(conn, sock)

    log_file = workspace / "logs" / f"{name}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with open(log_file, "a") as log:
        pid = process_mgr.start(name, cmd, stdout=log, stderr=log)

    return pid


def start_forward(
    conn: Connection,
    fw: PortForward,
    direction: str,
    process_mgr: ProcessManager,
    workspace: Path,
) -> int:
    """Start a ControlSlave process for a single port forward.

    Attaches to the existing ControlMaster socket.
    Returns the PID of the started process.
    """
    sock = socket_path(conn.tag, workspace)
    name = _forward_name(conn.tag, fw.tag or f"{direction}-{fw.src_port}")
    cmd = build_forward_cmd(conn, fw, direction, sock)

    log_file = workspace / "logs" / f"{name}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with open(log_file, "a") as log:
        pid = process_mgr.start(name, cmd, stdout=log, stderr=log)

    return pid


def stop_forward(
    conn_tag: str,
    fw_tag: str,
    process_mgr: ProcessManager,
) -> bool:
    """Stop a single forward slave process.

    Returns True if the process was stopped, False if it wasn't running.
    """
    name = _forward_name(conn_tag, fw_tag)
    return process_mgr.stop(name)


def start_tunnel(
    conn: Connection,
    process_mgr: ProcessManager,
    workspace: Path,
) -> int:
    """Start the ControlMaster + all configured forward slaves.

    Returns the PID of the master process.
    """
    master_pid = start_master(conn, process_mgr, workspace)

    for fw in conn.forwards.local:
        try:
            start_forward(conn, fw, "local", process_mgr, workspace)
        except Exception:
            pass  # individual forward failures don't abort the tunnel

    for fw in conn.forwards.remote:
        try:
            start_forward(conn, fw, "remote", process_mgr, workspace)
        except Exception:
            pass

    return master_pid


def stop_tunnel(
    tag: str,
    process_mgr: ProcessManager,
    workspace: Path | None = None,
    ssh_host: str | None = None,
) -> bool:
    """Stop all forward slaves for a connection, then stop the ControlMaster.

    If workspace and ssh_host are provided, sends ``ssh -O exit`` first so
    that the ControlPersist background master exits cleanly instead of
    remaining as a zombie.

    Returns True if the master was stopped, False if it wasn't running.
    """
    # Stop all forward slaves (susops-fwd-{tag}-*)
    prefix = f"{FWD_PROCESS_PREFIX}-{tag}-"
    for name in list(process_mgr.status_all().keys()):
        if name.startswith(prefix):
            process_mgr.stop(name)

    # Tell the ControlPersist background master to exit gracefully before
    # we SIGTERM autossh; without this the backgrounded ssh master keeps
    # running after autossh dies.
    if workspace is not None and ssh_host is not None:
        sock = socket_path(tag, workspace)
        if sock.exists():
            try:
                subprocess.run(
                    ["ssh", "-O", "exit", "-o", f"ControlPath={sock}", ssh_host],
                    capture_output=True,
                    timeout=3,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

    return process_mgr.stop(_master_name(tag))


def is_tunnel_running(tag: str, process_mgr: ProcessManager) -> bool:
    """Return True if the ControlMaster SSH process for tag is currently running."""
    return process_mgr.is_running(_master_name(tag))


def is_socket_alive(tag: str, workspace: Path) -> bool:
    """Return True if the ControlMaster socket is responsive.

    Uses `ssh -O check` to verify the master is healthy.
    """
    sock = socket_path(tag, workspace)
    if not sock.exists():
        return False
    try:
        result = subprocess.run(
            ["ssh", "-O", "check", "-o", f"ControlPath={sock}", "placeholder"],
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def test_ssh_connectivity(ssh_host: str, timeout: int = 5) -> bool:
    """Test SSH connectivity to a host without establishing a full tunnel.

    Uses ssh with BatchMode=yes and a short timeout. Returns True if the
    host is reachable (even if auth fails — we just need network reachability).

    Note: A return code of 255 means connection failed. Code 1 means auth
    failed (host is reachable but key not accepted). We treat both 0 and 1
    as "reachable" since the SSH port is open.
    """
    cmd = [
        "ssh",
        "-q",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=no",
        "-T",
        ssh_host,
        "true",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout + 2,
        )
        # rc=0: success, rc=1: connected but auth failed, rc=255: connection error
        return result.returncode != 255
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
