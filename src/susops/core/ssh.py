"""SSH tunnel management using autossh (or ssh fallback) + ProcessManager."""
from __future__ import annotations
import shutil
import subprocess
from pathlib import Path

from susops.core.config import Connection
from susops.core.process import ProcessManager

__all__ = [
    "build_ssh_cmd",
    "start_tunnel",
    "stop_tunnel",
    "is_tunnel_running",
    "test_ssh_connectivity",
    "SSH_PROCESS_PREFIX",
]

SSH_PROCESS_PREFIX = "susops-ssh"


def _ssh_binary() -> str:
    """Return 'autossh' if available, else 'ssh'."""
    return "autossh" if shutil.which("autossh") else "ssh"


def _process_name(tag: str) -> str:
    return f"{SSH_PROCESS_PREFIX}-{tag}"


def build_ssh_cmd(conn: Connection) -> list[str]:
    """Build the SSH command list for a connection.

    Uses autossh if available, falls back to ssh.
    The SOCKS port must already be assigned (non-zero) in conn.socks_proxy_port.
    """
    binary = _ssh_binary()

    if binary == "autossh":
        cmd: list[str] = ["autossh", "-M", "0"]
    else:
        cmd = ["ssh"]

    cmd += [
        "-N", "-T",
        "-D", str(conn.socks_proxy_port),
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ]

    # Local forwards: -L src_addr:src_port:dst_addr:dst_port
    for fw in conn.forwards.local:
        cmd += ["-L", f"{fw.src_addr}:{fw.src_port}:{fw.dst_addr}:{fw.dst_port}"]

    # Remote forwards: -R src_addr:src_port:dst_addr:dst_port
    for fw in conn.forwards.remote:
        cmd += ["-R", f"{fw.src_addr}:{fw.src_port}:{fw.dst_addr}:{fw.dst_port}"]

    cmd.append(conn.ssh_host)
    return cmd


def start_tunnel(
    conn: Connection,
    process_mgr: ProcessManager,
    workspace: Path,
) -> int:
    """Start an SSH tunnel for the given connection.

    The SOCKS port must already be assigned (non-zero) in conn.socks_proxy_port.
    Returns the PID of the started process.
    Raises ValueError if socks_proxy_port is 0.
    Raises RuntimeError if the process fails to start.
    """
    if conn.socks_proxy_port == 0:
        raise ValueError(
            f"Connection '{conn.tag}' has no SOCKS port assigned. "
            "Assign one before starting."
        )

    name = _process_name(conn.tag)
    cmd = build_ssh_cmd(conn)

    log_file = workspace / "logs" / f"{name}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with open(log_file, "a") as log:
        pid = process_mgr.start(name, cmd, stdout=log, stderr=log)

    return pid


def stop_tunnel(tag: str, process_mgr: ProcessManager) -> bool:
    """Stop the SSH tunnel for the given connection tag.

    Returns True if the tunnel was stopped, False if it wasn't running.
    """
    return process_mgr.stop(_process_name(tag))


def is_tunnel_running(tag: str, process_mgr: ProcessManager) -> bool:
    """Return True if the SSH tunnel for tag is currently running."""
    return process_mgr.is_running(_process_name(tag))


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
