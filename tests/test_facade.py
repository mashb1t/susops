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
    from susops.core.share import ShareServer, generate_password

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
    monkeypatch.setattr(facade_mod, "start_master", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Connection refused (port 22)")))

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
