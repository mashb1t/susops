"""SSH tunnel management using ssh + ProcessManager.

Architecture:
  - One ControlMaster process per connection (manages the multiplexed socket).
  - Each port forward (local, remote, share) is its own lightweight slave process
    that attaches to the master via the Unix socket.
  - Forwards can be added/removed without restarting the master.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from susops.core.config import Connection, PortForward
from susops.core.process import ProcessManager

__all__ = [
    "build_ssh_cmd",          # legacy alias — kept for CLI compat
    "build_master_cmd",
    "start_tunnel",           # starts master (forwards bundled in cmd)
    "start_master",
    "start_forward",          # ssh -O forward — registers live forward via socket
    "cancel_forward",         # ssh -O cancel — releases master-held port
    "stop_tunnel",            # stops master (and all its forwards)
    "is_tunnel_running",
    "is_socket_alive",
    "find_master_pid",
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
    """Build the ControlMaster command.

    Intentionally contains no -L/-R forward flags — forwards are registered
    live via ``start_forward`` (``ssh -O forward``) after the master socket is
    ready. This keeps process arguments minimal and avoids exposing forward
    destinations in ``ps aux``.

    ControlPersist is intentionally omitted: with -N the process stays in the
    foreground with a stable, trackable PID. Reconnection is handled by the
    Python-side _ReconnectMonitor which restarts the master on dropout.
    """
    return [
        "ssh",
        "-N", "-T",
        "-D", str(conn.socks_proxy_port),
        "-o", "ControlMaster=yes",
        "-o", f"ControlPath={sock}",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
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

    # Remove stale socket so the new master can take ownership.
    # A live master is never reached here (facade checks is_tunnel_running +
    # is_socket_alive before calling start_master), so a socket at this
    # point is always left over from a dead master.
    if sock.exists():
        sock.unlink()

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
    workspace: Path,
) -> None:
    """Register a TCP port forward with the running ControlMaster via ``ssh -O forward``.

    Sends the forward request through the Unix socket — no SSH handshake,
    no new TCP connection. The master holds the port until ``cancel_forward``
    is called or the master exits.

    Raises RuntimeError if the socket is not ready or the forward fails.
    """
    sock = socket_path(conn.tag, workspace)

    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"ControlMaster socket {sock} not ready after 10 s")

    flag = "-L" if direction == "local" else "-R"
    fwd_spec = f"{fw.src_addr}:{fw.src_port}:{fw.dst_addr}:{fw.dst_port}"
    result = subprocess.run(
        ["ssh", "-O", "forward", "-o", f"ControlPath={sock}", flag, fwd_spec, conn.ssh_host],
        capture_output=True,
        timeout=5,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"ssh -O forward failed: {stderr}")


def cancel_forward(
    conn: Connection,
    fw: PortForward,
    direction: str,
    workspace: Path,
) -> None:
    """Release a master-held TCP port forward via ``ssh -O cancel``.

    Best-effort: silently ignores errors (master may already be gone).
    """
    sock = socket_path(conn.tag, workspace)
    if not sock.exists():
        return
    flag = "-L" if direction == "local" else "-R"
    fwd_spec = f"{fw.src_addr}:{fw.src_port}:{fw.dst_addr}:{fw.dst_port}"
    try:
        subprocess.run(
            ["ssh", "-O", "cancel", "-o", f"ControlPath={sock}", flag, fwd_spec, conn.ssh_host],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


def start_tunnel(
    conn: Connection,
    process_mgr: ProcessManager,
    workspace: Path,
) -> int:
    """Start the ControlMaster for a connection.

    All enabled TCP forwards are bundled in the master command and python
    restarts them automatically on reconnect. Returns the PID of the master.
    """
    return start_master(conn, process_mgr, workspace)


def stop_tunnel(
    tag: str,
    process_mgr: ProcessManager,
    workspace: Path | None = None,
    ssh_host: str | None = None,
) -> bool:
    """Stop the ControlMaster for a connection.

    Sends SIGTERM to the ssh master process via ProcessManager, then sends
    ``ssh -O exit`` through the socket for a clean shutdown. workspace and
    ssh_host are both required for the -O exit step; if either is absent that
    step is skipped.

    Returns True if the master was stopped, False if it wasn't running.
    """
    # Clean up any lingering forward slave PIDs (no-op with Approach B but safe
    # to keep for processes left over from older susops versions).
    prefix = f"{FWD_PROCESS_PREFIX}-{tag}-"
    for name in list(process_mgr.status_all().keys()):
        if name.startswith(prefix):
            process_mgr.stop(name)

    stopped = process_mgr.stop(_master_name(tag))

    # Best-effort clean shutdown via the ControlMaster socket.
    if workspace is not None and ssh_host is not None:
        sock = socket_path(tag, workspace)
        if sock.exists():
            try:
                subprocess.run(
                    ["ssh", "-O", "exit", "-o", f"ControlPath={sock}", ssh_host],
                    capture_output=True,
                    timeout=3,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

    return stopped


def is_tunnel_running(tag: str, process_mgr: ProcessManager) -> bool:
    """Return True if the ControlMaster SSH process for tag is currently running."""
    return process_mgr.is_running(_master_name(tag))


def find_master_pid(tag: str, workspace: Path) -> int | None:
    """Find the PID of the ControlMaster by scanning /proc cmdline (Linux only).

    Used to recover the PID when the PID file is stale but the socket is alive.
    Returns None if the process cannot be found or /proc is unavailable.
    """
    sock_str = str(socket_path(tag, workspace))
    proc = Path("/proc")
    if not proc.exists():
        return None
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmdline = (pid_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            if "ControlMaster=yes" in cmdline and sock_str in cmdline:
                return int(pid_dir.name)
        except OSError:
            continue
    return None


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
