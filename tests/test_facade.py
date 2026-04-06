"""Tests for susops.facade — SusOpsManager integration."""
from __future__ import annotations

import pytest
from pathlib import Path

from susops.core.config import PortForward
from susops.core.types import ProcessState
from susops.facade import SusOpsManager


@pytest.fixture
def mgr(tmp_path):
    return SusOpsManager(workspace=tmp_path)


# ------------------------------------------------------------------ #
# Config mutation
# ------------------------------------------------------------------ #

def test_add_connection(mgr):
    conn = mgr.add_connection("work", "user@work.example.com")
    assert conn.tag == "work"
    assert conn.ssh_host == "user@work.example.com"
    assert conn.socks_proxy_port == 0
    # Persisted
    cfg = mgr.list_config()
    assert len(cfg.connections) == 1


def test_add_duplicate_connection_raises(mgr):
    mgr.add_connection("work", "user@work.example.com")
    with pytest.raises(ValueError, match="already exists"):
        mgr.add_connection("work", "other@host.com")


def test_remove_connection(mgr):
    mgr.add_connection("work", "user@host.com")
    mgr.remove_connection("work")
    assert mgr.list_config().connections == []


def test_remove_nonexistent_connection_raises(mgr):
    with pytest.raises(ValueError, match="not found"):
        mgr.remove_connection("nonexistent")


def test_add_pac_host(mgr):
    mgr.add_connection("work", "user@host.com")
    mgr.add_pac_host("*.internal.example.com", conn_tag="work")
    conn = mgr.list_config().connections[0]
    assert "*.internal.example.com" in conn.pac_hosts


def test_add_duplicate_pac_host_raises(mgr):
    mgr.add_connection("work", "user@host.com")
    mgr.add_pac_host("host.com", conn_tag="work")
    with pytest.raises(ValueError):
        mgr.add_pac_host("host.com", conn_tag="work")


def test_remove_pac_host(mgr):
    mgr.add_connection("work", "user@host.com")
    mgr.add_pac_host("host.com", conn_tag="work")
    mgr.remove_pac_host("host.com")
    conn = mgr.list_config().connections[0]
    assert "host.com" not in conn.pac_hosts


def test_add_local_forward(mgr):
    mgr.add_connection("work", "user@host.com")
    fw = PortForward(src_port=3306, dst_port=3306)
    mgr.add_local_forward("work", fw)
    conn = mgr.list_config().connections[0]
    assert len(conn.forwards.local) == 1
    assert conn.forwards.local[0].src_port == 3306


def test_remove_local_forward(mgr):
    mgr.add_connection("work", "user@host.com")
    fw = PortForward(src_port=3306, dst_port=3306)
    mgr.add_local_forward("work", fw)
    mgr.remove_local_forward(3306)
    conn = mgr.list_config().connections[0]
    assert conn.forwards.local == []


# ------------------------------------------------------------------ #
# Status
# ------------------------------------------------------------------ #

def test_status_empty_config(mgr):
    result = mgr.status()
    assert result.state == ProcessState.STOPPED
    assert result.connection_statuses == ()


def test_status_with_connection(mgr):
    mgr.add_connection("work", "user@host.com")
    result = mgr.status()
    assert len(result.connection_statuses) == 1
    assert result.connection_statuses[0].running is False


# ------------------------------------------------------------------ #
# Logging
# ------------------------------------------------------------------ #

def test_get_logs_initially_empty(mgr):
    assert mgr.get_logs() == []


def test_log_callback(mgr):
    received = []
    mgr.on_log = received.append
    mgr.add_connection("t", "h")  # triggers _log
    assert any("Added" in msg for msg in received)


# ------------------------------------------------------------------ #
# Reset
# ------------------------------------------------------------------ #

def test_reset_clears_config(mgr):
    mgr.add_connection("work", "user@host.com")
    mgr.reset()
    assert mgr.list_config().connections == []


# ------------------------------------------------------------------ #
# PAC URL
# ------------------------------------------------------------------ #

def test_get_pac_url_when_not_running(mgr):
    assert mgr.get_pac_url() == ""
