"""SSH tunnel management using autossh (or ssh fallback) + ProcessManager.

Architecture:
  - One ControlMaster process per connection (manages the multiplexed socket).
  - Each port forward (local, remote, share) is its own lightweight slave process
    that attaches to the master via the Unix socket.
  - Forwards can be added/removed without restarting the master.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from susops.core.config import Connection, PortForward
from susops.core.process import ProcessManager

__all__ = [
    "build_ssh_cmd",          # legacy alias — kept for CLI compat
    "build_master_cmd",
    "build_forward_cmd",
    "start_tunnel",           # starts master + all configured forwards
    "start_master",
    "start_forward",          # spawns ControlSlave; master holds port after slave exits
    "cancel_forward",         # ssh -O cancel — releases master-held port
    "stop_tunnel",            # stops all forward slaves + master
    "stop_forward",           # legacy: kills slave PID (unused for TCP; kept for tests)
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

    Uses autossh -M 0 when available: autossh restarts the ssh child
    automatically on disconnect so tunnels survive without a running Python
    process. Falls back to plain ssh when autossh is not installed — reconnect
    still works via _ReconnectMonitor polling but requires susops to be running.

    ControlPersist is intentionally omitted: with -N the process stays in the
    foreground with a stable, trackable PID.
    """
    binary = "autossh" if shutil.which("autossh") else "ssh"
    cmd: list[str] = [binary]
    if binary == "autossh":
        cmd += ["-M", "0"]
    cmd += [
        "-N", "-T",
        "-D", str(conn.socks_proxy_port),
        "-o", "ControlMaster=yes",
        "-o", f"ControlPath={sock}",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        conn.ssh_host,
    ]
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
        "-o", "ControlMaster=no",
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

    # AUTOSSH_GATETIME=0: don't penalise for failures during initial connection.
    # AUTOSSH_POLL=30:    monitoring poll interval — matches ServerAliveInterval.
    env = {"AUTOSSH_GATETIME": "0", "AUTOSSH_POLL": "30"}

    with open(log_file, "a") as log:
        pid = process_mgr.start(name, cmd, env=env, stdout=log, stderr=log)

    return pid


def start_forward(
    conn: Connection,
    fw: PortForward,
    direction: str,
    process_mgr: ProcessManager,
    workspace: Path,
) -> None:
    """Spawn a ControlSlave to register a TCP port forward with the master.

    The slave stays alive for the lifetime of the forward — it binds the
    local port and relays connections through the ControlMaster socket.
    The slave PID is tracked via ProcessManager so stop_forward can kill it.

    Waits up to 10 s for the master socket to be ready before spawning so
    the slave always connects as a ControlSlave rather than opening a direct
    SSH connection (which would bypass the master).
    """
    sock = socket_path(conn.tag, workspace)

    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"ControlMaster socket {sock} not ready after 10 s")

    name = _forward_name(conn.tag, fw.tag or f"{direction}-{fw.src_port}")
    cmd = build_forward_cmd(conn, fw, direction, sock)
    log_file = workspace / "logs" / f"{name}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with open(log_file, "a") as log:
        process_mgr.start(name, cmd, stdout=log, stderr=log)


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
    """Stop all forward slaves for a connection, then stop the autossh master.

    Kills autossh first to halt the restart loop, then sends ``ssh -O exit``
    through the socket to cleanly shut down any orphaned ssh child that autossh
    did not yet signal. workspace and ssh_host are both required for the -O exit
    step; if either is absent that step is skipped.

    Returns True if the master was stopped, False if it wasn't running.
    """
    # Stop all forward slaves (susops-fwd-{tag}-*)
    prefix = f"{FWD_PROCESS_PREFIX}-{tag}-"
    for name in list(process_mgr.status_all().keys()):
        if name.startswith(prefix):
            process_mgr.stop(name)

    # Kill autossh first — stops the restart loop so the ssh child is not
    # immediately restarted after we issue -O exit below.
    stopped = process_mgr.stop(_master_name(tag))

    # Best-effort: tell the ssh ControlMaster child to exit cleanly.
    # autossh sends SIGHUP to the child on its own exit, but a brief race
    # window means the child may still be alive for a moment.
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
