"""Tests for port-forward management do_* methods on AbstractTrayApp."""
from __future__ import annotations

from susops.core.config import PortForward


def test_do_add_local_forward(tray):
    tray.do_add_connection("work", "user@host")
    fw = PortForward(src_port=8080, dst_port=80, tag="http")
    tray.do_add_local_forward("work", fw)
    cfg = tray.manager.list_config()
    assert any(f.src_port == 8080 for f in cfg.connections[0].forwards.local)


def test_do_add_remote_forward(tray):
    tray.do_add_connection("work", "user@host")
    fw = PortForward(src_port=9090, dst_port=90, tag="rev")
    tray.do_add_remote_forward("work", fw)
    cfg = tray.manager.list_config()
    assert any(f.src_port == 9090 for f in cfg.connections[0].forwards.remote)


def test_do_remove_local_forward(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_local_forward("work", PortForward(src_port=8080, dst_port=80))
    tray.do_remove_local_forward(8080)
    cfg = tray.manager.list_config()
    assert not cfg.connections[0].forwards.local


def test_do_remove_remote_forward(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_remote_forward("work", PortForward(src_port=9090, dst_port=90))
    tray.do_remove_remote_forward(9090)
    cfg = tray.manager.list_config()
    assert not cfg.connections[0].forwards.remote


def test_do_toggle_forward_enabled(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_local_forward("work", PortForward(src_port=8080, dst_port=80))
    tray.do_toggle_forward_enabled("work", 8080, "local")
    cfg = tray.manager.list_config()
    assert cfg.connections[0].forwards.local[0].enabled is False


def test_do_toggle_forward_enabled_twice_restores(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_local_forward("work", PortForward(src_port=8080, dst_port=80))
    tray.do_toggle_forward_enabled("work", 8080, "local")
    tray.do_toggle_forward_enabled("work", 8080, "local")
    cfg = tray.manager.list_config()
    assert cfg.connections[0].forwards.local[0].enabled is True
