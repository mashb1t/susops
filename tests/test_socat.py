"""Tests for susops.core.socat — UDP socat command building and process management."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from susops.core.config import Connection, PortForward
from susops.core.socat import (
    UDP_PROCESS_PREFIX,
    _fw_tag,
    _udp_process_name,
    stop_udp_forward,
    stop_all_udp_forwards_for_connection,
)


@pytest.fixture
def conn():
    return Connection(tag="work", ssh_host="user@host.example.com", socks_proxy_port=1080)


@pytest.fixture
def sock(tmp_path):
    return tmp_path / "sockets" / "work.sock"


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
