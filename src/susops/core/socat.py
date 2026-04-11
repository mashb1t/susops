"""UDP port forwarding via socat over SSH ControlMaster.

Architecture:
  - Local UDP forward: socat EXEC approach — no SSH port forward slave needed.
    One process: local socat pipes each UDP conversation through ControlMaster
    to a remote socat instance (spawned per conversation via EXEC).
  - Remote UDP forward: SSH -R slave + remote socat + local socat.
    Three processes: an intermediate TCP port bridges the two socat instances.

Error handling: FileNotFoundError when socat is not installed locally;
subprocess exit errors when the remote host blocks command execution or
lacks socat — both surface through the process manager log file.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from susops.core.config import Connection, PortForward
from susops.core.ports import get_random_free_port
from susops.core.process import ProcessManager
from susops.core.ssh import socket_path

__all__ = [
    "UDP_PROCESS_PREFIX",
    "start_udp_forward",
    "stop_udp_forward",
    "stop_all_udp_forwards_for_connection",
]

UDP_PROCESS_PREFIX = "susops-udp"


def _fw_tag(fw: PortForward, direction: str) -> str:
    """Return the identifying tag for a forward (tag field or direction-port)."""
    return fw.tag or f"{direction}-{fw.src_port}"


def _udp_process_name(conn_tag: str, fw_tag: str, suffix: str) -> str:
    """Build a process name like susops-udp-<conn>-<fw_tag>-<suffix>."""
    return f"{UDP_PROCESS_PREFIX}-{conn_tag}-{fw_tag}-{suffix}"


def start_udp_forward(
    conn: Connection,
    fw: PortForward,
    direction: str,
    process_mgr: ProcessManager,
    workspace: Path,
) -> None:
    """Start socat process(es) for a UDP port forward.

    direction="local":  one local socat process using EXEC through ControlMaster.
    direction="remote": SSH -R slave + remote socat (via SSH) + local socat.

    Raises FileNotFoundError if socat is not installed locally.
    Remote errors (socat missing, shell access blocked) surface as immediate
    process exit — check the log file at workspace/logs/<name>.log.
    """
    sock = socket_path(conn.tag, workspace)
    tag = _fw_tag(fw, direction)
    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if direction == "local":
        _start_local_udp(conn, fw, sock, tag, process_mgr, log_dir)
    else:
        _start_remote_udp(conn, fw, sock, tag, process_mgr, log_dir)


def _start_local_udp(
    conn: Connection,
    fw: PortForward,
    sock: Path,
    tag: str,
    process_mgr: ProcessManager,
    log_dir: Path,
) -> None:
    """Local UDP forward: socat EXEC piped through SSH ControlMaster.

    Each UDP conversation forks one SSH session (multiplexed via ControlMaster).
    -T15 closes idle forked children after 15 seconds.
    """
    name = _udp_process_name(conn.tag, tag, "lsocat")
    ssh_exec = (
        f"ssh -o ControlPath={shlex.quote(str(sock))} -T {conn.ssh_host} "
        f"socat - UDP4-SENDTO:{fw.dst_addr}:{fw.dst_port}"
    )
    cmd = [
        "socat",
        "-T15",
        f"UDP4-RECVFROM:{fw.src_port},reuseaddr,fork",
        f"EXEC:{ssh_exec}",
    ]
    log_file = log_dir / f"{name}.log"
    with open(log_file, "a") as log:
        process_mgr.start(name, cmd, stdout=log, stderr=log)


def _start_remote_udp(
    conn: Connection,
    fw: PortForward,
    sock: Path,
    tag: str,
    process_mgr: ProcessManager,
    log_dir: Path,
) -> None:
    """Remote UDP forward: SSH -R + remote socat (via SSH) + local socat.

    Allocates a random intermediate TCP port for bridging the two socat instances.

    Note: the three processes are started sequentially without explicit synchronisation.
    The remote socat (rsocat) may start before the SSH -R slave has finished binding
    the intermediate port on the remote host. On high-latency connections this can
    cause rsocat to fail immediately; the process manager will log the exit. The
    facade's polling loop will surface the failure to the user.
    """
    intermediate = get_random_free_port()

    # 1. SSH -R slave: binds intermediate port on remote, forwards to local
    ssh_name = _udp_process_name(conn.tag, tag, "ssh")
    ssh_cmd = [
        "ssh", "-N", "-T",
        "-o", f"ControlPath={sock}",
        "-R", f"{intermediate}:localhost:{intermediate}",
        conn.ssh_host,
    ]
    log_file = log_dir / f"{ssh_name}.log"
    with open(log_file, "a") as log:
        process_mgr.start(ssh_name, ssh_cmd, stdout=log, stderr=log)

    # 2. Remote socat (runs on remote host via SSH): UDP → TCP intermediate
    rsocat_name = _udp_process_name(conn.tag, tag, "rsocat")
    rsocat_cmd = [
        "ssh", "-T",
        "-o", f"ControlPath={sock}",
        conn.ssh_host,
        f"socat -T15 UDP4-RECVFROM:{fw.src_port},reuseaddr,fork TCP4:localhost:{intermediate}",
    ]
    log_file = log_dir / f"{rsocat_name}.log"
    with open(log_file, "a") as log:
        process_mgr.start(rsocat_name, rsocat_cmd, stdout=log, stderr=log)

    # 3. Local socat: TCP intermediate → UDP local service
    lsocat_name = _udp_process_name(conn.tag, tag, "lsocat")
    lsocat_cmd = [
        "socat",
        f"TCP4-LISTEN:{intermediate},reuseaddr,fork",
        f"UDP4-SENDTO:{fw.dst_addr}:{fw.dst_port}",
    ]
    log_file = log_dir / f"{lsocat_name}.log"
    with open(log_file, "a") as log:
        process_mgr.start(lsocat_name, lsocat_cmd, stdout=log, stderr=log)


def stop_udp_forward(
    conn_tag: str,
    fw_tag: str,
    process_mgr: ProcessManager,
) -> bool:
    """Stop all socat/SSH processes for a single UDP forward.

    Returns True if at least one process was stopped.
    """
    prefix = f"{UDP_PROCESS_PREFIX}-{conn_tag}-{fw_tag}-"
    stopped_any = False
    for name in list(process_mgr.status_all().keys()):
        if name.startswith(prefix):
            if process_mgr.stop(name):
                stopped_any = True
    return stopped_any


def stop_all_udp_forwards_for_connection(
    conn_tag: str,
    process_mgr: ProcessManager,
) -> None:
    """Stop all UDP socat processes for every forward on a connection."""
    prefix = f"{UDP_PROCESS_PREFIX}-{conn_tag}-"
    for name in list(process_mgr.status_all().keys()):
        if name.startswith(prefix):
            process_mgr.stop(name)
