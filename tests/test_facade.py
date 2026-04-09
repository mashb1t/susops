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


# ------------------------------------------------------------------ #
# File sharing — port-forward integration
# ------------------------------------------------------------------ #

pytest.importorskip("cryptography", reason="cryptography package required")


@pytest.fixture
def mgr_with_conn(mgr):
    mgr.add_connection("work", "user@host.com")
    return mgr


def test_share_adds_remote_forward(mgr_with_conn, tmp_path):
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    info = mgr_with_conn.share(test_file, "work")

    conn = mgr_with_conn.list_config().connections[0]
    # In the new architecture, shares are stored in file_shares (not forwards.remote)
    assert any(
        f.port == info.port
        for f in conn.file_shares
    )
    mgr_with_conn.stop_share(info.port)


def test_share_records_conn_tag(mgr_with_conn, tmp_path):
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    info = mgr_with_conn.share(test_file, "work")

    assert info.conn_tag == "work"
    assert mgr_with_conn.list_shares()[0].conn_tag == "work"
    mgr_with_conn.stop_share(info.port)


def test_stop_share_removes_remote_forward(mgr_with_conn, tmp_path):
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    info = mgr_with_conn.share(test_file, "work")
    port = info.port
    mgr_with_conn.stop_share(port)

    conn = mgr_with_conn.list_config().connections[0]
    # FileShare entry must be removed from config after stop
    assert not any(f.port == port for f in conn.file_shares)


def test_stop_all_shares_removes_all_remote_forwards(mgr_with_conn, tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("a")
    f2.write_text("b")

    i1 = mgr_with_conn.share(f1, "work")
    i2 = mgr_with_conn.share(f2, "work")
    mgr_with_conn.stop_share()  # stop all

    conn = mgr_with_conn.list_config().connections[0]
    # All FileShare entries must be removed after stop_share()
    assert not any(f.port in (i1.port, i2.port) for f in conn.file_shares)


def test_share_unknown_connection_raises(mgr, tmp_path):
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    with pytest.raises(ValueError, match="not found"):
        mgr.share(test_file, "nonexistent")


def test_fetch_adds_then_removes_local_forward(mgr_with_conn, tmp_path):
    from susops.core.share import ShareServer, generate_password

    test_file = tmp_path / "payload.txt"
    test_file.write_text("fetch me")
    pw = generate_password()

    server = ShareServer()
    share_info = server.start(file_path=test_file, password=pw, port=0)
    try:
        outfile = tmp_path / "out.txt"
        mgr_with_conn.fetch(port=share_info.port, password=pw, conn_tag="work", outfile=outfile)

        assert outfile.read_text() == "fetch me"
        # No leftover local forward entries in config (tunnel was not running,
        # so the code path that adds to config and restarts is used — and it
        # cleans up after itself)
        conn = mgr_with_conn.list_config().connections[0]
        assert not any(f.src_port == share_info.port for f in conn.forwards.local)
    finally:
        server.stop()


def test_fetch_removes_local_forward_on_failure(mgr_with_conn):
    # Nothing is listening on this port — fetch_file will raise
    port = 19876
    with pytest.raises(Exception):
        mgr_with_conn.fetch(port=port, password="pw", conn_tag="work")

    # Forward must be cleaned up from config even on failure
    conn = mgr_with_conn.list_config().connections[0]
    assert not any(f.src_port == port for f in conn.forwards.local)


def test_fetch_unknown_connection_raises(mgr):
    with pytest.raises(ValueError, match="not found"):
        mgr.fetch(port=12345, password="pw", conn_tag="nonexistent")
