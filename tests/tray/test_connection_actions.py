"""Tests for connection-management do_* methods on AbstractTrayApp."""
from __future__ import annotations


def test_do_add_connection(tray):
    tray.do_add_connection("work", "user@host", port=0)
    cfg = tray.manager.list_config()
    assert any(c.tag == "work" and c.ssh_host == "user@host" for c in cfg.connections)
    # User-facing confirmation alert fires when state is INITIAL (not RUNNING)
    assert any(t == "Added" for t, _ in tray.alerts)


def test_do_add_connection_duplicate_alerts_error(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_connection("work", "user@host")  # duplicate
    assert any(t == "Error" for t, _ in tray.alerts)


def test_do_remove_connection(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_remove_connection("work")
    cfg = tray.manager.list_config()
    assert not any(c.tag == "work" for c in cfg.connections)


def test_do_remove_nonexistent_connection_alerts_error(tray):
    tray.do_remove_connection("nope")
    assert any(t == "Error" for t, _ in tray.alerts)


def test_do_toggle_connection_enabled(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_toggle_connection_enabled("work")
    cfg = tray.manager.list_config()
    assert cfg.connections[0].enabled is False
    tray.do_toggle_connection_enabled("work")
    assert tray.manager.list_config().connections[0].enabled is True


def test_do_start_stop_restart_connection_no_crash(tray):
    """SSH won't actually connect to user@host, but the calls must not raise."""
    tray.do_add_connection("work", "user@host")
    tray.do_start_connection("work")
    tray.do_stop_connection("work")
    tray.do_restart_connection("work")
