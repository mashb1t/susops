"""Tests for tray app initialisation and basic lifecycle do_* methods."""
from __future__ import annotations


def test_tray_harness_initialises(tray):
    assert tray.manager is not None
    cfg = tray.manager.list_config()
    assert cfg.connections == []


def test_do_poll_updates_icon_and_menu(tray):
    tray.do_poll()
    assert len(tray.icon_updates) == 1
    assert len(tray.menu_states) == 1


def test_do_status_calls_show_output_dialog(tray):
    tray.do_status()
    # do_status hits run_in_background → synchronous in tests → output dialog fires
    assert len(tray.output_dialogs) == 1
    title, output = tray.output_dialogs[0]
    assert title == "Status"
    # Output uses sectioned layout: header + CONNECTIONS + PAC SERVER
    assert "SusOps" in output
    assert "CONNECTIONS" in output
    assert "PAC SERVER" in output


def test_do_logs_calls_show_output_dialog(tray):
    tray.do_logs()
    assert len(tray.output_dialogs) == 1
    title, _ = tray.output_dialogs[0]
    assert title == "Logs"


def test_do_start_no_connections_does_not_crash(tray):
    """start() with no connections configured must not raise."""
    tray.do_start()


def test_do_stop_no_connections_does_not_crash(tray):
    """stop() with no connections configured must not raise."""
    tray.do_stop()


def test_do_restart_no_connections_does_not_crash(tray):
    """restart() with no connections configured must not raise."""
    tray.do_restart()
