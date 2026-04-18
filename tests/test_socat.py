"""Tests for susops.core.socat — UDP socat command building and process management.

start_udp_forward (local direction) is tested via a mocked ProcessManager to verify
command construction without spawning real processes. The private helpers _fw_tag,
_udp_process_name, stop_udp_forward, and stop_all_udp_forwards_for_connection
are also tested via mocks.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from susops.core.config import Connection, PortForward
from susops.core.socat import (
    UDP_PROCESS_PREFIX,
    _fw_tag,
    _udp_process_name,
    start_udp_forward,
    stop_udp_forward,
    stop_all_udp_forwards_for_connection,
)


@pytest.fixture
def conn():
    return Connection(tag="work", ssh_host="user@host.example.com", socks_proxy_port=1080)


@pytest.fixture
def fw_local():
    return PortForward(src_port=53, dst_port=53, dst_addr="dns.internal", tcp=False, udp=True)


@pytest.fixture
def fw_remote():
    return PortForward(src_port=51820, dst_port=51820, tcp=False, udp=True)


def test_fw_tag_uses_tag_field():
    fw = PortForward(src_port=53, dst_port=53, tag="dns", udp=True, tcp=False)
    assert _fw_tag(fw, "local") == "dns"


def test_fw_tag_falls_back_to_direction_port():
    fw = PortForward(src_port=53, dst_port=53, udp=True, tcp=False)
    assert _fw_tag(fw, "local") == "local-53"
    assert _fw_tag(fw, "remote") == "remote-53"


def test_udp_process_name():
    name = _udp_process_name("work", "local-53", "lsocat")
    assert name == "susops-udp-work-local-53-lsocat"


def test_stop_udp_forward_stops_matching_processes():
    mgr = MagicMock()
    mgr.status_all.return_value = {
        "susops-udp-work-local-53-lsocat": True,
        "susops-udp-work-local-80-lsocat": True,  # different forward
        "susops-fwd-work-local-53": True,          # not a UDP process
    }
    mgr.stop.return_value = True
    result = stop_udp_forward("work", "local-53", mgr)
    assert result is True
    mgr.stop.assert_called_once_with("susops-udp-work-local-53-lsocat")


def test_stop_udp_forward_returns_false_when_nothing_running():
    mgr = MagicMock()
    mgr.status_all.return_value = {}
    result = stop_udp_forward("work", "local-53", mgr)
    assert result is False


def test_stop_all_udp_forwards_for_connection():
    mgr = MagicMock()
    mgr.status_all.return_value = {
        "susops-udp-work-local-53-lsocat": True,
        "susops-udp-work-remote-51820-ssh": True,
        "susops-udp-work-remote-51820-rsocat": True,
        "susops-udp-work-remote-51820-lsocat": True,
        "susops-udp-other-local-53-lsocat": True,  # different connection
    }
    stop_all_udp_forwards_for_connection("work", mgr)
    stopped = {call.args[0] for call in mgr.stop.call_args_list}
    assert "susops-udp-work-local-53-lsocat" in stopped
    assert "susops-udp-work-remote-51820-ssh" in stopped
    assert "susops-udp-work-remote-51820-rsocat" in stopped
    assert "susops-udp-work-remote-51820-lsocat" in stopped
    assert "susops-udp-other-local-53-lsocat" not in stopped


# ------------------------------------------------------------------ #
# start_udp_forward — local direction command construction
# ------------------------------------------------------------------ #

def test_start_local_udp_process_name(conn, fw_local, tmp_path):
    pm = MagicMock()
    start_udp_forward(conn, fw_local, "local", pm, tmp_path)
    pm.start.assert_called_once()
    name = pm.start.call_args[0][0]
    assert name == "susops-udp-work-local-53-lsocat"


def test_start_local_udp_exec_single_quoted(conn, fw_local, tmp_path):
    """EXEC argument must single-quote the SSH sub-command.

    socat splits EXEC on spaces; without single quotes it sees 'ssh', '-o',
    '...' as separate arguments and errors 'wrong number of parameters (3 instead of 1)'.
    """
    pm = MagicMock()
    start_udp_forward(conn, fw_local, "local", pm, tmp_path)
    cmd = pm.start.call_args[0][1]
    exec_arg = next(a for a in cmd if a.startswith("EXEC:"))
    assert exec_arg.startswith("EXEC:'ssh "), f"EXEC not single-quoted: {exec_arg!r}"
    assert exec_arg.endswith("'"), f"EXEC not closed with single quote: {exec_arg!r}"


def test_start_local_udp_destination_in_exec(conn, fw_local, tmp_path):
    pm = MagicMock()
    start_udp_forward(conn, fw_local, "local", pm, tmp_path)
    cmd = pm.start.call_args[0][1]
    exec_arg = next(a for a in cmd if a.startswith("EXEC:"))
    assert f"UDP4-SENDTO:{fw_local.dst_addr}:{fw_local.dst_port}" in exec_arg


def test_start_local_udp_listens_on_src_port(conn, fw_local, tmp_path):
    pm = MagicMock()
    start_udp_forward(conn, fw_local, "local", pm, tmp_path)
    cmd = pm.start.call_args[0][1]
    recvfrom_arg = next(a for a in cmd if "UDP4-RECVFROM" in a)
    assert f"UDP4-RECVFROM:{fw_local.src_port}" in recvfrom_arg
    assert "fork" in recvfrom_arg


# ------------------------------------------------------------------ #
# start_udp_forward — remote direction command construction
# ------------------------------------------------------------------ #

def test_start_remote_udp_spawns_three_processes(conn, fw_remote, tmp_path):
    pm = MagicMock()
    start_udp_forward(conn, fw_remote, "remote", pm, tmp_path)
    assert pm.start.call_count == 3


def test_start_remote_udp_process_names(conn, fw_remote, tmp_path):
    pm = MagicMock()
    start_udp_forward(conn, fw_remote, "remote", pm, tmp_path)
    names = [c[0][0] for c in pm.start.call_args_list]
    assert "susops-udp-work-remote-51820-ssh" in names
    assert "susops-udp-work-remote-51820-rsocat" in names
    assert "susops-udp-work-remote-51820-lsocat" in names


def test_start_remote_udp_lsocat_before_rsocat(conn, fw_remote, tmp_path):
    """lsocat (local TCP listener) must start before rsocat (remote socat).

    rsocat connects to the intermediate TCP port via the SSH -R tunnel.
    Starting lsocat first ensures the local end is ready before the remote
    end tries to connect.
    """
    pm = MagicMock()
    start_udp_forward(conn, fw_remote, "remote", pm, tmp_path)
    names = [c[0][0] for c in pm.start.call_args_list]
    lsocat_idx = names.index("susops-udp-work-remote-51820-lsocat")
    rsocat_idx = names.index("susops-udp-work-remote-51820-rsocat")
    assert lsocat_idx < rsocat_idx, f"lsocat must start before rsocat, got order: {names}"


def test_start_remote_udp_rsocat_has_retry(conn, fw_remote, tmp_path):
    """rsocat command must include a shell retry loop.

    The SSH -R slave may not finish binding the remote intermediate port
    before rsocat executes. A retry loop with sleep handles this gracefully.
    """
    pm = MagicMock()
    start_udp_forward(conn, fw_remote, "remote", pm, tmp_path)
    rsocat_call = next(c for c in pm.start.call_args_list if c[0][0].endswith("-rsocat"))
    remote_cmd = rsocat_call[0][1][-1]
    assert "sleep" in remote_cmd, f"rsocat must retry with sleep, got: {remote_cmd!r}"
    assert f"UDP4-RECVFROM:{fw_remote.src_port},reuseaddr,fork" in remote_cmd
