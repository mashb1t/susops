"""Tests for PAC host management do_* methods on AbstractTrayApp."""
from __future__ import annotations


def test_do_add_pac_host(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_pac_host("example.com")
    cfg = tray.manager.list_config()
    assert "example.com" in cfg.connections[0].pac_hosts


def test_do_remove_pac_host(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_pac_host("example.com")
    tray.do_remove_pac_host("example.com")
    cfg = tray.manager.list_config()
    assert "example.com" not in cfg.connections[0].pac_hosts


def test_do_toggle_pac_host_enabled(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_pac_host("example.com")
    tray.do_toggle_pac_host_enabled("example.com")
    cfg = tray.manager.list_config()
    # host should have moved to pac_hosts_disabled
    assert "example.com" in cfg.connections[0].pac_hosts_disabled


def test_do_add_pac_host_no_connection_alerts_error(tray):
    tray.do_add_pac_host("example.com")
    assert any(t == "Error" for t, _ in tray.alerts)


def test_do_add_pac_host_duplicate_alerts_error(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_pac_host("example.com")
    tray.do_add_pac_host("example.com")  # duplicate
    assert any(t == "Error" for t, _ in tray.alerts)


def test_do_remove_pac_host_nonexistent_alerts_error(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_remove_pac_host("nothere.com")
    assert any(t == "Error" for t, _ in tray.alerts)
