"""Tests for susops.facade — SusOpsManager integration."""
from __future__ import annotations

from pathlib import Path

import pytest

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


# ------------------------------------------------------------------ #
# update_connection (inline edit, children-preserving)
# ------------------------------------------------------------------ #

def test_update_connection_changes_host_and_port(mgr):
    mgr.add_connection("work", "user@host.com", socks_port=1080)
    updated = mgr.update_connection(
        "work", ssh_host="user@newhost.com", socks_proxy_port=1081, restart=False
    )
    assert updated.ssh_host == "user@newhost.com"
    assert updated.socks_proxy_port == 1081
    conn = mgr.list_config().connections[0]
    assert conn.tag == "work"
    assert conn.ssh_host == "user@newhost.com"
    assert conn.socks_proxy_port == 1081


def test_update_connection_preserves_children_on_rename(mgr):
    mgr.add_connection("work", "user@host.com", socks_port=1080)
    mgr.add_pac_host("ex.com", conn_tag="work")
    mgr.add_local_forward("work", PortForward(src_port=5432, dst_port=5432, tag="pg"))
    # Seed a file_share entry directly in config (no real server needed).
    mgr._add_file_share_to_config("work", "/tmp/secret.bin", "pw", 9999)

    mgr.update_connection(
        "work", new_tag="renamed", ssh_host="new@host", socks_proxy_port=1081,
        restart=False,
    )

    cfg = mgr.list_config()
    assert [c.tag for c in cfg.connections] == ["renamed"]
    conn = cfg.connections[0]
    assert conn.ssh_host == "new@host"
    assert conn.socks_proxy_port == 1081
    # The whole point: children survive the rename (no cascade).
    assert "ex.com" in conn.pac_hosts
    assert any(f.src_port == 5432 and f.tag == "pg" for f in conn.forwards.local)
    assert any(f.port == 9999 and f.file_path == "/tmp/secret.bin" for f in conn.file_shares)


def test_update_connection_rename_to_existing_raises(mgr):
    mgr.add_connection("work", "user@host.com")
    mgr.add_connection("other", "user@other.com")
    with pytest.raises(ValueError, match="already exists"):
        mgr.update_connection("work", new_tag="other", restart=False)


def test_update_connection_empty_tag_raises(mgr):
    mgr.add_connection("work", "user@host.com")
    with pytest.raises(ValueError):
        mgr.update_connection("work", new_tag="   ", restart=False)


def test_update_connection_empty_host_raises(mgr):
    mgr.add_connection("work", "user@host.com")
    with pytest.raises(ValueError, match="ssh_host"):
        mgr.update_connection("work", ssh_host="  ", restart=False)


def test_update_connection_invalid_port_raises(mgr):
    mgr.add_connection("work", "user@host.com")
    with pytest.raises(ValueError, match="Invalid socks_proxy_port"):
        mgr.update_connection("work", socks_proxy_port=99999, restart=False)


def test_update_connection_same_tag_no_false_conflict(mgr):
    mgr.add_connection("work", "user@host.com", socks_port=1080)
    # new_tag=None means keep the tag — must not trip the "already exists" check.
    updated = mgr.update_connection("work", ssh_host="user@changed.com", restart=False)
    assert updated.tag == "work"
    assert mgr.list_config().connections[0].ssh_host == "user@changed.com"


def test_update_connection_restart_false_leaves_tunnel_untouched(mgr):
    # Stopped connection — no SSH needed. restart=False must not start it.
    mgr.add_connection("work", "user@host.com", socks_port=1080)
    mgr.update_connection(
        "work", ssh_host="user@newhost.com", socks_proxy_port=1090, restart=False
    )
    status = mgr.status()
    assert all(not s.running for s in status.connection_statuses)
    conn = mgr.list_config().connections[0]
    assert conn.ssh_host == "user@newhost.com"
    assert conn.socks_proxy_port == 1090


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


def test_stop_share_keeps_config_entry(mgr_with_conn, tmp_path):
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    info = mgr_with_conn.share(test_file, "work")
    port = info.port
    mgr_with_conn.stop_share(port)

    # stop_share keeps the FileShare entry in config (shows as stopped)
    conn = mgr_with_conn.list_config().connections[0]
    fs = next(f for f in conn.file_shares if f.port == port)
    assert fs is not None
    # Entry is marked as manually stopped
    assert fs.stopped is True
    # But the server is no longer running
    shares = mgr_with_conn.list_shares()
    assert any(s.port == port and not s.running for s in shares)


def test_stop_all_shares_keeps_config_entries(mgr_with_conn, tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("a")
    f2.write_text("b")

    i1 = mgr_with_conn.share(f1, "work")
    i2 = mgr_with_conn.share(f2, "work")
    mgr_with_conn.stop_share()  # stop all (keeps config entries)

    # Entries still in config, but not running
    conn = mgr_with_conn.list_config().connections[0]
    assert any(f.port == i1.port for f in conn.file_shares)
    assert any(f.port == i2.port for f in conn.file_shares)
    shares = mgr_with_conn.list_shares()
    assert all(not s.running for s in shares if s.port in (i1.port, i2.port))


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


# ------------------------------------------------------------------ #
# FileShare persistence
# ------------------------------------------------------------------ #

def test_share_saves_file_share_to_config(mgr_with_conn, tmp_path):
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    info = mgr_with_conn.share(test_file, "work")

    conn = mgr_with_conn.list_config().connections[0]
    assert any(fs.port == info.port for fs in conn.file_shares)
    mgr_with_conn.stop_share(info.port)


def test_delete_share_removes_file_share_from_config(mgr_with_conn, tmp_path):
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    info = mgr_with_conn.share(test_file, "work")
    mgr_with_conn.delete_share(info.port)

    conn = mgr_with_conn.list_config().connections[0]
    assert not any(fs.port == info.port for fs in conn.file_shares)


def test_stop_share_does_not_clobber_concurrent_config_write(mgr_with_conn, tmp_path):
    """stop_share must reload config under the lock so a stale in-memory copy
    does not overwrite a write made elsewhere on the same workspace.

    Regression: _set_file_share_stopped wrote self.config without _config_lock
    or _reload_config, so a concurrent list_shares/mutation (the tray poll does
    this) could lose the stopped flag or drop an unrelated entry.
    """
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")
    info = mgr_with_conn.share(test_file, "work")
    port = info.port

    # A second manager on the same workspace writes to disk, leaving
    # mgr_with_conn's in-memory config stale (missing "second").
    other = SusOpsManager(workspace=tmp_path)
    other.add_connection("second", "user@second.com")

    mgr_with_conn.stop_share(port)

    fresh = SusOpsManager(workspace=tmp_path).list_config()
    tags = {c.tag for c in fresh.connections}
    assert "second" in tags, "stop_share clobbered a concurrent connection write"
    work = next(c for c in fresh.connections if c.tag == "work")
    fs = next(f for f in work.file_shares if f.port == port)
    assert fs.stopped is True


def test_stop_share_then_start_again(mgr_with_conn, tmp_path):
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    info = mgr_with_conn.share(test_file, "work")
    mgr_with_conn.stop_share(info.port)

    # Manually stopped — stopped=True in config
    conn = mgr_with_conn.list_config().connections[0]
    fs = next(f for f in conn.file_shares if f.port == info.port)
    assert fs.stopped is True

    # Re-sharing clears the stopped flag
    info2 = mgr_with_conn.share(Path(fs.file_path), "work", password=fs.password, port=fs.port)
    assert info2.running is True
    conn2 = mgr_with_conn.list_config().connections[0]
    fs2 = next(f for f in conn2.file_shares if f.port == info2.port)
    assert fs2.stopped is False

    # Clean up
    mgr_with_conn.delete_share(info2.port)


def test_list_shares_shows_running_and_stopped(mgr_with_conn, tmp_path):
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    # Add a running share
    info = mgr_with_conn.share(test_file, "work")

    # Manually add a stopped FileShare to config (simulate a persisted-but-not-running share)
    from susops.core.config import FileShare
    conn = mgr_with_conn.list_config().connections[0]
    stopped_fs = FileShare(file_path=str(tmp_path / "ghost.txt"), password="pw", port=55555)
    updated_conn = conn.model_copy(
        update={"file_shares": list(conn.file_shares) + [stopped_fs]}
    )
    mgr_with_conn.config = mgr_with_conn.config.model_copy(
        update={"connections": [updated_conn]}
    )
    mgr_with_conn._save()

    shares = mgr_with_conn.list_shares()
    running = [s for s in shares if s.running]
    stopped = [s for s in shares if not s.running]

    assert any(s.port == info.port for s in running)
    assert any(s.port == 55555 for s in stopped)

    mgr_with_conn.stop_share(info.port)


def test_stopped_share_not_auto_restarted(mgr_with_conn, tmp_path):
    """A manually stopped share (stopped=True) must not be restarted by start()."""
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    info = mgr_with_conn.share(test_file, "work")
    mgr_with_conn.stop_share(info.port)

    # Simulate what start() does: auto-start config-only shares
    # It should skip this share because stopped=True
    mgr_with_conn.start("work")

    shares = mgr_with_conn.list_shares()
    assert any(s.port == info.port and not s.running for s in shares), (
        "Manually stopped share must remain stopped after start()"
    )
    mgr_with_conn.delete_share(info.port)


def test_stop_tag_also_stops_connection_shares(mgr_with_conn, tmp_path):
    """stop(tag=) must stop file shares belonging to that connection."""
    test_file = tmp_path / "data.txt"
    test_file.write_text("hello")

    info = mgr_with_conn.share(test_file, "work")
    assert any(s.running for s in mgr_with_conn.list_shares())

    # Stop the connection by tag — should also stop its shares
    mgr_with_conn.stop(tag="work")

    shares = mgr_with_conn.list_shares()
    assert not any(s.port == info.port and s.running for s in shares), (
        "Share must be stopped when its connection is stopped by tag"
    )


def test_stop_tag_does_not_stop_other_connection_shares(tmp_path):
    """stop(tag='a') must leave shares belonging to other connections running."""
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("a", "user@a.example.com")
    mgr.add_connection("b", "user@b.example.com")

    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("aaa")
    file_b.write_text("bbb")

    info_a = mgr.share(file_a, "a")
    info_b = mgr.share(file_b, "b")

    # Stop only connection 'a'
    mgr.stop(tag="a")

    shares = mgr.list_shares()
    assert not any(s.port == info_a.port and s.running for s in shares), (
        "Share for connection 'a' must be stopped"
    )
    assert any(s.port == info_b.port and s.running for s in shares), (
        "Share for connection 'b' must still be running"
    )

    mgr.stop_share(info_b.port)


def test_three_state_share_offline(mgr_with_conn, tmp_path):
    """A FileShare in config with stopped=False but no running server is 'offline'."""
    from susops.core.config import FileShare

    # Inject a FileShare into config without starting a server (simulates
    # connection having gone down without a manual stop)
    conn = mgr_with_conn.list_config().connections[0]
    offline_fs = FileShare(file_path=str(tmp_path / "ghost.txt"), password="pw", port=55500, stopped=False)
    mgr_with_conn.config = mgr_with_conn.config.model_copy(
        update={"connections": [conn.model_copy(update={"file_shares": list(conn.file_shares) + [offline_fs]})]}
    )
    mgr_with_conn._save()

    shares = mgr_with_conn.list_shares()
    offline = next(s for s in shares if s.port == 55500)
    assert offline.running is False
    assert offline.stopped is False  # not manually stopped → offline state


def test_list_shares_populates_access_count(mgr_with_conn, tmp_path):
    """list_shares() must reflect live access_count from the running ShareServer."""
    from susops.core.share import fetch_file

    test_file = tmp_path / "data.txt"
    test_file.write_text("content")

    info = mgr_with_conn.share(test_file, "work")

    # Fetch twice to drive the counter
    for i in range(2):
        fetch_file(host="localhost", port=info.port, password=info.password,
                   outfile=tmp_path / f"out{i}.txt")

    shares = mgr_with_conn.list_shares()
    share = next(s for s in shares if s.port == info.port)
    assert share.access_count == 2
    assert share.failed_count == 0

    mgr_with_conn.stop_share(info.port)


def test_list_shares_populates_failed_count(mgr_with_conn, tmp_path):
    """list_shares() must reflect live failed_count from the running ShareServer."""
    from susops.core.share import fetch_file

    test_file = tmp_path / "data.txt"
    test_file.write_text("content")

    info = mgr_with_conn.share(test_file, "work")

    with pytest.raises(Exception):
        fetch_file(host="localhost", port=info.port, password="wrong",
                   outfile=tmp_path / "fail.txt")

    shares = mgr_with_conn.list_shares()
    share = next(s for s in shares if s.port == info.port)
    assert share.failed_count == 1
    assert share.access_count == 0

    mgr_with_conn.stop_share(info.port)


def test_restore_shares_disabled(tmp_path):
    """With restore_shares_on_start=False, no share servers are started."""
    from susops.facade import SusOpsManager
    from susops.core.config import FileShare, Connection, SusOpsConfig, AppConfig, save_config

    # Write a config with a FileShare and restore=False
    test_file = tmp_path / "file.txt"
    test_file.write_text("data")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    conn = Connection(
        tag="work",
        ssh_host="user@host.com",
        file_shares=[FileShare(file_path=str(test_file), password="pw", port=0)],
    )
    cfg = SusOpsConfig(
        connections=[conn],
        susops_app=AppConfig(restore_shares_on_start=False),
    )
    save_config(cfg, workspace)

    mgr = SusOpsManager(workspace=workspace)
    # No servers should be running since restore is disabled
    running = [s for s in mgr.list_shares() if s.running]
    assert running == []


def test_restore_shares_on_start(tmp_path):
    """With restore_shares_on_start=True and a live tunnel, shares are auto-started."""
    import os
    from susops.facade import SusOpsManager
    from susops.core.config import FileShare, Connection, SusOpsConfig, AppConfig, save_config

    test_file = tmp_path / "restore_me.txt"
    test_file.write_text("restore content")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    conn = Connection(
        tag="work",
        ssh_host="user@host.com",
        file_shares=[FileShare(file_path=str(test_file), password="pw", port=0)],
    )
    cfg = SusOpsConfig(
        connections=[conn],
        susops_app=AppConfig(restore_shares_on_start=True),
    )
    save_config(cfg, workspace)

    # Fake a running tunnel by writing the current PID as the master PID file
    pid_dir = workspace / "pids"
    pid_dir.mkdir(exist_ok=True)
    (pid_dir / "susops-ssh-work.pid").write_text(str(os.getpid()))

    mgr = SusOpsManager(workspace=workspace)
    running = [s for s in mgr.list_shares() if s.running]
    assert len(running) == 1
    assert running[0].conn_tag == "work"

    mgr.stop_share(running[0].port)


def test_add_local_udp_forward_persisted(tmp_path):
    """UDP forward is saved to config with correct flags."""
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host", 1080)
    fw = PortForward(src_port=53, dst_port=53, dst_addr="dns.internal", tcp=False, udp=True)
    mgr.add_local_forward("work", fw)
    config = mgr.list_config()
    saved = config.connections[0].forwards.local[0]
    assert saved.tcp is False
    assert saved.udp is True
    assert saved.dst_addr == "dns.internal"


def test_start_logs_ssh_tail_on_failure(tmp_path, monkeypatch):
    """When SSH fails to start, the facade logs the last lines of the SSH log."""
    from susops.facade import SusOpsManager
    import susops.facade as facade_mod

    # Pre-write a fake SSH log that would normally be written by the SSH process
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "susops-ssh-demo.log").write_text(
        "OpenSSH_9.0\nConnection refused (port 22)\n"
    )

    # Force start_master to raise so the exception handler is exercised
    monkeypatch.setattr(facade_mod, "start_master",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Connection refused (port 22)")))

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("demo", "user@nonexistent.invalid")

    log_lines = []
    mgr.on_log = log_lines.append

    mgr.start(tag="demo")  # will fail — start_master raises

    combined = "\n".join(log_lines)
    assert "Connection refused" in combined, (
        f"SSH log tail not surfaced in log output:\n{combined}"
    )


def test_reconnect_monitor_tracks_intended_tags(tmp_path):
    """mark_running and mark_stopped maintain the intended set correctly."""
    from susops.facade import _ReconnectMonitor

    class _FakeMgr:
        pass

    monitor = _ReconnectMonitor(_FakeMgr())
    assert "work" not in monitor._intended

    monitor.mark_running("work")
    assert "work" in monitor._intended

    monitor.mark_running("home")
    assert "home" in monitor._intended

    monitor.mark_stopped("work")
    assert "work" not in monitor._intended
    assert "home" in monitor._intended


def test_stop_halts_monitor_when_last_tag_stopped(tmp_path):
    """Per-tag stop that empties the intended set should halt the monitor —
    otherwise the status display keeps showing '● Reconnect' for a
    thread polling an empty set every 5 s.

    Note: conftest autouse-mocks ReconnectMonitor.start so the real thread
    never runs in tests. We instead verify that the facade calls
    `monitor.stop()` exactly when intended becomes empty.
    """
    from unittest.mock import patch

    from susops.facade import SusOpsManager

    mgr = SusOpsManager(workspace=tmp_path, _enable_background_threads=True)
    mgr.add_connection("only", "u@h", socks_port=0)
    mgr._reconnect_monitor.mark_running("only")
    assert "only" in mgr._reconnect_monitor._intended

    with patch.object(mgr._reconnect_monitor, "stop") as mock_stop:
        mgr.stop(tag="only")
        mock_stop.assert_called_once()
    assert mgr._reconnect_monitor._intended == set()


def test_stop_keeps_monitor_alive_when_other_tags_still_live(tmp_path):
    """Per-tag stop that leaves OTHER tags still intended must NOT halt
    the monitor — those other tags still need watching."""
    from unittest.mock import patch

    from susops.facade import SusOpsManager

    mgr = SusOpsManager(workspace=tmp_path, _enable_background_threads=True)
    mgr.add_connection("a", "u@h", socks_port=0)
    mgr.add_connection("b", "u@h", socks_port=0)
    mgr._reconnect_monitor.mark_running("a")
    mgr._reconnect_monitor.mark_running("b")

    with patch.object(mgr._reconnect_monitor, "stop") as mock_stop:
        mgr.stop(tag="a")
        mock_stop.assert_not_called()
    assert mgr._reconnect_monitor._intended == {"b"}


def test_error_calls_both_on_log_and_on_error(tmp_path):
    """_error() must invoke both on_log and on_error callbacks."""
    from susops.facade import SusOpsManager

    mgr = SusOpsManager(workspace=tmp_path)

    log_msgs = []
    error_msgs = []
    mgr.on_log = log_msgs.append
    mgr.on_error = error_msgs.append

    mgr._error("something went wrong")

    assert any("something went wrong" in m for m in log_msgs), "on_log not called"
    assert any("something went wrong" in m for m in error_msgs), "on_error not called"


def test_error_tolerates_missing_on_error(tmp_path):
    """_error() must not raise when on_error is None."""
    from susops.facade import SusOpsManager

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.on_error = None
    mgr._error("oops")  # must not raise


def test_add_local_forward_both_protocols_persisted(tmp_path):
    """Forward with tcp=True and udp=True is saved with both flags."""
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host", 1080)
    fw = PortForward(src_port=53, dst_port=53, tcp=True, udp=True)
    mgr.add_local_forward("work", fw)
    saved = mgr.list_config().connections[0].forwards.local[0]
    assert saved.tcp is True
    assert saved.udp is True


def test_restore_shares_missing_file_logs_warning(tmp_path):
    """A FileShare pointing to a nonexistent file must be skipped with a warning logged."""
    import os
    from susops.facade import SusOpsManager
    from susops.core.config import FileShare, Connection, SusOpsConfig, AppConfig, save_config

    workspace = tmp_path / "ws"
    workspace.mkdir()

    conn = Connection(
        tag="work",
        ssh_host="user@host.com",
        file_shares=[FileShare(file_path=str(tmp_path / "missing.txt"), password="pw", port=0)],
    )
    cfg = SusOpsConfig(
        connections=[conn],
        susops_app=AppConfig(restore_shares_on_start=True),
    )
    save_config(cfg, workspace)

    # Fake a running tunnel
    pid_dir = workspace / "pids"
    pid_dir.mkdir(exist_ok=True)
    (pid_dir / "susops-ssh-work.pid").write_text(str(os.getpid()))

    logs: list[str] = []
    mgr = SusOpsManager(workspace=workspace)
    mgr.on_log = logs.append

    # Re-trigger _restore_shares directly (the init call above already ran it,
    # but on_log was not yet set; call it again to capture the warning)
    mgr._restore_shares()

    assert any("not found" in m.lower() or "missing" in m.lower() for m in logs), (
        f"Expected a warning about missing file; got: {logs}"
    )
    # No server should be running for this share
    assert mgr.list_shares() == [] or all(not s.running for s in mgr.list_shares())


def test_disabled_forward_not_started(tmp_path):
    """Forwards with enabled=False are not registered via ssh -O forward during start()."""
    from unittest.mock import patch
    from susops.facade import SusOpsManager
    from susops.core.config import PortForward

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("demo", "user@host")
    mgr.add_local_forward("demo", PortForward(
        src_port=5432, dst_port=5432, tag="pg", enabled=False
    ))

    started_forwards = []

    def fake_start_forward(conn, fw, direction, ws):
        started_forwards.append(fw.tag or str(fw.src_port))

    with patch("susops.facade.start_forward", side_effect=fake_start_forward), \
            patch("susops.facade.start_master", return_value=1234), \
            patch("susops.core.ssh.is_socket_alive", return_value=True):
        mgr.start(tag="demo")

    assert "pg" not in started_forwards, "Disabled forward must not be registered"


# ------------------------------------------------------------------ #
# test_connection / test_domain / test_forward
# ------------------------------------------------------------------ #

def test_test_connection_success(tmp_path, monkeypatch):
    """test_connection returns success=True and a latency when SSH is reachable."""
    import susops.facade as facade_mod

    monkeypatch.setattr(facade_mod, "test_ssh_connectivity", lambda host: True)

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@work.example.com")

    result = mgr.test_connection("work")

    assert result.success is True
    assert result.target == "user@work.example.com"
    assert result.message == "SSH reachable"
    assert result.latency_ms is not None
    assert result.latency_ms >= 0


def test_test_connection_unreachable(tmp_path, monkeypatch):
    """test_connection returns success=False when SSH is not reachable."""
    import susops.facade as facade_mod

    monkeypatch.setattr(facade_mod, "test_ssh_connectivity", lambda host: False)

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@work.example.com")

    result = mgr.test_connection("work")

    assert result.success is False
    assert result.message == "SSH unreachable"
    assert result.latency_ms is None


def test_test_connection_unknown_conn(tmp_path, monkeypatch):
    """test_connection returns failure when connection tag does not exist."""
    import susops.facade as facade_mod

    monkeypatch.setattr(facade_mod, "test_ssh_connectivity", lambda host: True)

    mgr = SusOpsManager(workspace=tmp_path)

    result = mgr.test_connection("nonexistent")

    assert result.success is False
    assert result.target == "nonexistent"
    assert "not found" in result.message.lower()


def test_test_domain_success(tmp_path, monkeypatch):
    """test_domain returns success when curl exits 0 through the SOCKS proxy."""
    import susops.facade as facade_mod

    class _FakeResult:
        returncode = 0
        stdout = "200"
        stderr = ""

    def fake_run(cmd, **kwargs):
        assert "socks5h://127.0.0.1:1080" in cmd
        assert "internal.example.com" in " ".join(cmd)
        return _FakeResult()

    monkeypatch.setattr(facade_mod.subprocess, "run", fake_run)

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com", socks_port=1080)

    result = mgr.test_domain("internal.example.com", "work")

    assert result.success is True
    assert result.target == "internal.example.com"
    assert "200" in result.message
    assert result.latency_ms is not None


def test_test_domain_strips_wildcard_prefix(tmp_path, monkeypatch):
    """test_domain strips '*.' from wildcard hosts before passing to curl."""
    import susops.facade as facade_mod

    seen_cmds = []

    class _FakeResult:
        returncode = 0
        stdout = "200"
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        return _FakeResult()

    monkeypatch.setattr(facade_mod.subprocess, "run", fake_run)

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com", socks_port=1080)

    result = mgr.test_domain("*.internal.example.com", "work")

    assert result.success is True
    # The wildcard prefix must have been stripped — curl must see the bare hostname
    full_cmd = " ".join(seen_cmds[0])
    assert "*.internal.example.com" not in full_cmd
    assert "internal.example.com" in full_cmd


def test_test_domain_curl_failure(tmp_path, monkeypatch):
    """test_domain returns success=False when curl exits non-zero."""
    import susops.facade as facade_mod

    class _FakeResult:
        returncode = 7  # connection refused
        stdout = ""
        stderr = "Connection refused"

    monkeypatch.setattr(facade_mod.subprocess, "run", lambda cmd, **kwargs: _FakeResult())

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com", socks_port=1080)

    result = mgr.test_domain("internal.example.com", "work")

    assert result.success is False
    assert result.latency_ms is None


def test_test_domain_no_socks_port(tmp_path):
    """test_domain returns failure when socks_proxy_port is 0 (proxy not configured)."""
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")  # socks_proxy_port defaults to 0

    result = mgr.test_domain("internal.example.com", "work")

    assert result.success is False
    assert "socks" in result.message.lower() or "proxy" in result.message.lower()


def test_test_domain_unknown_conn(tmp_path):
    """test_domain returns failure when connection tag does not exist."""
    mgr = SusOpsManager(workspace=tmp_path)

    result = mgr.test_domain("internal.example.com", "nonexistent")

    assert result.success is False
    assert "socks" in result.message.lower() or "proxy" in result.message.lower()


def test_test_domain_curl_timeout(tmp_path, monkeypatch):
    """test_domain returns success=False and includes the exception message on timeout."""
    import subprocess
    import susops.facade as facade_mod

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=15)

    monkeypatch.setattr(facade_mod.subprocess, "run", fake_run)

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com", socks_port=1080)

    result = mgr.test_domain("internal.example.com", "work")

    assert result.success is False
    assert result.latency_ms is None


def test_test_forward_tcp_local_port_bound(tmp_path, monkeypatch):
    """test_forward returns tcp=True for a local TCP forward when src_port is bound."""
    import susops.facade as facade_mod

    monkeypatch.setattr(facade_mod, "is_port_free", lambda port: False)

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_local_forward("work", PortForward(src_port=5432, dst_port=5432, tcp=True, udp=False))

    result = mgr.test_forward("work", 5432, "local")

    assert result == {"tcp": True}


def test_test_forward_tcp_local_port_free(tmp_path, monkeypatch):
    """test_forward returns tcp=False for a local TCP forward when src_port is free."""
    import susops.facade as facade_mod

    monkeypatch.setattr(facade_mod, "is_port_free", lambda port: True)

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_local_forward("work", PortForward(src_port=5432, dst_port=5432, tcp=True, udp=False))

    result = mgr.test_forward("work", 5432, "local")

    assert result == {"tcp": False}


def test_test_forward_tcp_remote_checks_socket(tmp_path, monkeypatch):
    """test_forward for a remote TCP forward delegates to is_socket_alive."""
    import susops.facade as facade_mod

    monkeypatch.setattr(facade_mod, "is_socket_alive", lambda tag, ws: True)

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_remote_forward("work", PortForward(src_port=8080, dst_port=8080, tcp=True, udp=False))

    result = mgr.test_forward("work", 8080, "remote")

    assert result == {"tcp": True}


def test_test_forward_udp_only_local(tmp_path, monkeypatch):
    """test_forward returns only a 'udp' key for a UDP-only local forward."""
    import susops.facade as facade_mod

    monkeypatch.setattr(facade_mod, "_is_udp_forward_running",
                        lambda conn_tag, fw, direction, pm: True)

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_local_forward("work", PortForward(src_port=53, dst_port=53, tcp=False, udp=True))

    result = mgr.test_forward("work", 53, "local")

    assert result == {"udp": True}
    assert "tcp" not in result


def test_test_forward_tcp_and_udp(tmp_path, monkeypatch):
    """test_forward returns both 'tcp' and 'udp' keys for a dual-protocol forward."""
    import susops.facade as facade_mod

    monkeypatch.setattr(facade_mod, "is_port_free", lambda port: False)
    monkeypatch.setattr(facade_mod, "_is_udp_forward_running",
                        lambda conn_tag, fw, direction, pm: True)

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_local_forward("work", PortForward(src_port=53, dst_port=53, tcp=True, udp=True))

    result = mgr.test_forward("work", 53, "local")

    assert result == {"tcp": True, "udp": True}


def test_test_forward_unknown_connection_raises(tmp_path):
    """test_forward raises ValueError when the connection tag does not exist."""
    mgr = SusOpsManager(workspace=tmp_path)

    with pytest.raises(ValueError, match="not found"):
        mgr.test_forward("nonexistent", 5432, "local")


def test_test_forward_unknown_forward_raises(tmp_path):
    """test_forward raises ValueError when src_port is not configured for the connection."""
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")

    with pytest.raises(ValueError, match="not found"):
        mgr.test_forward("work", 9999, "local")


# ------------------------------------------------------------------ #
# process_info()
# ------------------------------------------------------------------ #

def test_process_info_empty_config(mgr):
    info = mgr.process_info()
    assert info["conn_children"] == {}
    assert info["reconnect"]["daemon_running"] is False
    assert info["reconnect"]["pid"] is None


def test_process_info_no_children_without_forwards(mgr):
    mgr.add_connection("work", "user@host.com")
    info = mgr.process_info()
    assert "work" not in info["conn_children"]


def test_process_info_tcp_forward_master_down(tmp_path):
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_local_forward("work", PortForward(src_port=5432, dst_port=5432, tcp=True))
    info = mgr.process_info()
    children = info["conn_children"].get("work", [])
    assert len(children) == 1
    assert children[0]["running"] is False
    assert children[0]["pid"] is None
    assert "fwd local" in children[0]["display"]
    assert "5432" in children[0]["display"]


def test_process_info_tcp_forward_master_up(tmp_path):
    import os
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_local_forward("work", PortForward(src_port=5432, dst_port=5432, tcp=True))
    (tmp_path / "pids" / "susops-ssh-work.pid").write_text(str(os.getpid()))
    info = mgr.process_info()
    children = info["conn_children"]["work"]
    assert children[0]["running"] is True


def test_process_info_disabled_forward_excluded(tmp_path):
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_local_forward("work", PortForward(src_port=5432, dst_port=5432, tcp=True, enabled=False))
    info = mgr.process_info()
    assert "work" not in info["conn_children"]


def test_process_info_remote_forward_shown(tmp_path):
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_remote_forward("work", PortForward(src_port=8080, dst_port=8080, tcp=True))
    info = mgr.process_info()
    children = info["conn_children"].get("work", [])
    assert len(children) == 1
    assert "fwd remote" in children[0]["display"]


def test_process_info_udp_forward_no_process(tmp_path):
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_local_forward("work", PortForward(src_port=53, dst_port=53, tcp=False, udp=True, tag="dns"))
    info = mgr.process_info()
    children = info["conn_children"].get("work", [])
    assert len(children) == 1
    assert children[0]["running"] is False
    assert children[0]["pid"] is None
    assert "udp local" in children[0]["display"]


def test_process_info_udp_forward_running(tmp_path):
    import os
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_local_forward("work", PortForward(src_port=53, dst_port=53, tcp=False, udp=True, tag="dns"))
    our_pid = os.getpid()
    (tmp_path / "pids" / "susops-udp-work-dns-lsocat.pid").write_text(str(our_pid))
    info = mgr.process_info()
    children = info["conn_children"]["work"]
    assert children[0]["running"] is True
    assert children[0]["pid"] == our_pid


def test_process_info_tcp_and_udp_forward(tmp_path):
    """A dual-protocol forward produces two children: one fwd and one udp."""
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host.com")
    mgr.add_local_forward("work", PortForward(src_port=53, dst_port=53, tcp=True, udp=True, tag="dns"))
    info = mgr.process_info()
    children = info["conn_children"].get("work", [])
    assert len(children) == 2
    displays = [c["display"] for c in children]
    assert any("fwd local" in d for d in displays)
    assert any("udp local" in d for d in displays)


def test_compute_state_running_when_only_enabled_are_running(tmp_path):
    """Disabled connections must not count toward STOPPED_PARTIALLY.

    Regression for the tray showing 'stopped_partially' when one connection
    is up and another is intentionally disabled.
    """
    from susops.facade import SusOpsManager
    from susops.core.types import ProcessState

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("active", "u@h")
    mgr.add_connection("benched", "u@h")
    mgr.set_connection_enabled("benched", False)

    # Pretend "active" is running and PAC is up; "benched" is not running
    # (which is the *correct* state for a disabled connection).
    from susops.core.types import ConnectionStatus
    statuses = (
        ConnectionStatus(tag="active", running=True, socks_port=1080, pid=42, enabled=True),
        ConnectionStatus(tag="benched", running=False, socks_port=0, pid=None, enabled=False),
    )
    assert mgr._compute_state(statuses=statuses, pac_running=True) is ProcessState.RUNNING


def test_compute_state_all_disabled_treated_as_stopped(tmp_path):
    """If every connection is disabled, there's effectively nothing to run."""
    from susops.facade import SusOpsManager
    from susops.core.types import ProcessState, ConnectionStatus

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("a", "u@h")
    mgr.set_connection_enabled("a", False)

    statuses = (
        ConnectionStatus(tag="a", running=False, socks_port=0, pid=None, enabled=False),
    )
    assert mgr._compute_state(statuses=statuses, pac_running=False) is ProcessState.STOPPED


def test_compute_state_partial_when_an_enabled_one_is_down(tmp_path):
    """Mixed enabled-running + enabled-down is still partial."""
    from susops.facade import SusOpsManager
    from susops.core.types import ProcessState, ConnectionStatus

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("up", "u@h")
    mgr.add_connection("down", "u@h")

    statuses = (
        ConnectionStatus(tag="up", running=True, socks_port=1080, pid=42, enabled=True),
        ConnectionStatus(tag="down", running=False, socks_port=0, pid=None, enabled=True),
    )
    assert mgr._compute_state(statuses=statuses, pac_running=True) is ProcessState.STOPPED_PARTIALLY


def test_set_connection_enabled_stops_pac_when_last_running(tmp_path):
    """Disabling the last enabled+running connection must also stop PAC.

    Without this, the aggregate state sticks at STOPPED_PARTIALLY (PAC up,
    no connections enabled) and the tray icon never settles on stopped.
    """
    from unittest.mock import patch
    from susops.facade import SusOpsManager

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("solo", "u@h", socks_port=0)

    with patch("susops.facade.is_tunnel_running", return_value=True), \
            patch("susops.facade.is_socket_alive", return_value=True), \
            patch("susops.facade.stop_tunnel"), \
            patch("susops.facade.stop_all_udp_forwards_for_connection"), \
            patch.object(mgr, "_active_tags", return_value=set()), \
            patch.object(mgr, "_stop_pac_server") as mock_stop_pac, \
            patch.object(mgr, "_update_pac") as mock_update_pac:
        mgr.set_connection_enabled("solo", False)

    mock_stop_pac.assert_called_once()
    mock_update_pac.assert_not_called()


def test_set_connection_enabled_keeps_pac_when_other_still_active(tmp_path):
    """Disabling one of several running connections must NOT stop PAC if
    another enabled connection is still active."""
    from unittest.mock import patch
    from susops.facade import SusOpsManager

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("a", "u@h", socks_port=0)
    mgr.add_connection("b", "u@h", socks_port=0)

    def fake_active_tags():
        return {"b"}

    with patch("susops.facade.is_tunnel_running", return_value=True), \
            patch("susops.facade.is_socket_alive", return_value=True), \
            patch("susops.facade.stop_tunnel"), \
            patch("susops.facade.stop_all_udp_forwards_for_connection"), \
            patch.object(mgr, "_active_tags", side_effect=fake_active_tags), \
            patch.object(mgr, "_stop_pac_server") as mock_stop_pac, \
            patch.object(mgr, "_update_pac") as mock_update_pac:
        mgr.set_connection_enabled("a", False)

    mock_stop_pac.assert_not_called()
    mock_update_pac.assert_called_once()


def test_add_connection_rejects_invalid_tags(tmp_path):
    """Tag validation: reject empty, traversal, slash, overlong, leading punctuation."""
    import pytest
    from susops.facade import SusOpsManager
    mgr = SusOpsManager(workspace=tmp_path)
    bad_tags = [
        "", " ", "../etc/passwd", "/abs", "a\\b", "..", ".", "-leading",
        "x" * 100, "tag with space", "tag\nwith\nnewline",
    ]
    for tag in bad_tags:
        with pytest.raises(ValueError, match="Invalid tag|Tag must"):
            mgr.add_connection(tag, "u@h")


def test_add_connection_accepts_safe_tags(tmp_path):
    """Tag validation: accept reasonable identifiers."""
    from susops.facade import SusOpsManager
    mgr = SusOpsManager(workspace=tmp_path)
    for tag in ["work", "my-host", "host_42", "a.b.c", "X9"]:
        mgr.add_connection(tag, "u@h")
        mgr.remove_connection(tag)


def test_stopped_marker_path_rejects_traversal(tmp_path):
    """Defense in depth: marker path constructor refuses traversal-bearing tags."""
    import pytest
    from susops.facade import _stopped_marker_path
    for bad in ["../etc/passwd", "/abs", "a\\b", "", ".", ".."]:
        with pytest.raises(ValueError, match="Unsafe tag"):
            _stopped_marker_path(tmp_path, bad)


def test_add_forward_validates_ports(tmp_path):
    """Port validation: src/dst must be 1-65535."""
    import pytest
    from susops.facade import SusOpsManager
    from susops.core.config import PortForward
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "u@h")
    for bad in [-1, 0, 65536, 99999]:
        with pytest.raises(ValueError, match="Invalid (src_port|dst_port)"):
            mgr.add_local_forward("work", PortForward(src_port=bad, dst_port=80))
        with pytest.raises(ValueError, match="Invalid (src_port|dst_port)"):
            mgr.add_local_forward("work", PortForward(src_port=8080, dst_port=bad))


def test_start_raises_on_unknown_tag(tmp_path):
    """start(tag='nonexistent') must raise, not silently return success=False."""
    import pytest
    from susops.facade import SusOpsManager
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "u@h")
    with pytest.raises(ValueError, match="Connection 'nope' not found"):
        mgr.start(tag="nope")


def test_stop_raises_on_unknown_tag(tmp_path):
    """stop(tag='nonexistent') must raise, not silently no-op."""
    import pytest
    from susops.facade import SusOpsManager
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "u@h")
    with pytest.raises(ValueError, match="Connection 'nope' not found"):
        mgr.stop(tag="nope")


def test_concurrent_add_forward_does_not_lose_updates(tmp_path):
    """Parallel add_local_forward calls each persist their forward.

    Regression for chaos2 phaseA: 5 parallel adds, only 1 persisted because
    read-modify-write on self.config wasn't serialized. The new _config_lock
    around _add_forward must make all N concurrent adds succeed.
    """
    import threading
    from susops.facade import SusOpsManager
    from susops.core.config import PortForward

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "u@h")

    errors: list[BaseException] = []
    def worker(i):
        try:
            mgr.add_local_forward("work", PortForward(
                src_port=40000 + i, dst_port=80, tag=f"fw-{i}"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors, f"concurrent adds raised: {errors!r}"

    conn = mgr.config.connections[0]
    tags = sorted(fw.tag for fw in conn.forwards.local if fw.tag and fw.tag.startswith("fw-"))
    assert tags == [f"fw-{i}" for i in range(8)], f"lost updates: only {tags} persisted"


def test_is_idle_fresh_workspace(tmp_path):
    """A brand-new workspace with no tracked processes is idle."""
    from susops.facade import SusOpsManager
    mgr = SusOpsManager(workspace=tmp_path)
    assert mgr.is_idle() is True


def test_is_idle_false_when_pid_file_present(tmp_path):
    """A tracked SSH master pid file blocks idle shutdown."""
    from susops.facade import SusOpsManager
    mgr = SusOpsManager(workspace=tmp_path)
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir(parents=True, exist_ok=True)
    # Pid 1 (init) is always alive, so the pid file is non-stale.
    (pid_dir / "susops-ssh-work.pid").write_text("1")
    assert mgr.is_idle() is False


def test_is_idle_ignores_own_services_pid(tmp_path):
    """The daemon's own pid file must not block idle shutdown."""
    from susops.facade import SusOpsManager
    mgr = SusOpsManager(workspace=tmp_path)
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "susops-services.pid").write_text("1")
    assert mgr.is_idle() is True


def test_is_idle_false_when_reconnect_watching(tmp_path):
    """Active reconnect monitor watching a tag blocks idle shutdown."""
    from susops.facade import SusOpsManager
    mgr = SusOpsManager(workspace=tmp_path)
    with mgr._reconnect_monitor._lock:
        mgr._reconnect_monitor._intended.add("work")
    try:
        assert mgr.is_idle() is False
    finally:
        with mgr._reconnect_monitor._lock:
            mgr._reconnect_monitor._intended.discard("work")
