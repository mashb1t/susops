"""macOS GUI smoke tests for the rumps tray with debug server.

Exercises dump-menu, open-about, in-process screenshot, and the 3-column
config window (nav / list / detail) via TrayDebugServer. Skipped unless
SUSOPS_RUN_GUI_TESTS=1 is set (macOS only).

Run locally:
    SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest tests/tray/test_gui_smoke.py -v
"""
from __future__ import annotations

import os
import platform
import time

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(platform.system() != "Darwin", reason="macOS only"),
    pytest.mark.skipif(
        not os.environ.get("SUSOPS_RUN_GUI_TESTS"),
        reason="set SUSOPS_RUN_GUI_TESTS=1 to run GUI smoke tests",
    ),
]


def _wait_for(fn, predicate, timeout: float = 5.0, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = fn()
        if predicate(result):
            return result
        time.sleep(interval)
    return result


def test_ping_and_dump_menu(tray_proc):
    menu = tray_proc.send("dump-menu")["menu"]
    titles = [n.get("title") for n in menu if "title" in n]
    assert "Start Proxy" in titles
    assert "Quit" in titles


def test_screenshot_of_about_panel(tray_proc, tmp_path):
    assert tray_proc.send("open-about").get("ok")
    out = tmp_path / "about.png"
    result = tray_proc.send(f"screenshot {out}")
    assert result.get("ok"), result
    assert out.stat().st_size > 5_000  # a real PNG, not a stub
    assert result["width"] > 100 and result["height"] > 100


def test_config_window_opens_and_dumps(tray_proc):
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_pac_host("blabla.de", conn_tag="work")
    assert tray_proc.send("open-config").get("ok")

    # Initial data loads asynchronously; allow up to 5 s for the first poll.
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(n["key"] == "connections" and n["count"] == 1
                                        for n in d.get("nav", [])),
    )
    assert dump["open"] is True
    nav = {n["key"]: n for n in dump["nav"]}
    assert set(nav) == {"connections", "domains", "forwards", "shares", "settings"}
    assert nav["connections"]["count"] == 1
    assert nav["domains"]["count"] == 1
    assert dump["category"] == "connections"
    assert any(r["title"] == "work" for r in dump["rows"])

    sel = tray_proc.send("select domains 0")
    assert sel.get("ok"), sel
    assert sel["selected"] == ["domain", "work", "blabla.de"]


def test_window_reflects_external_changes(tray_proc):
    """The poll-driven refresh must pick up daemon-side changes."""
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config domains").get("ok")
    # Wait until the window has loaded and is on the domains category.
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and d.get("category") == "domains",
    )
    c.add_pac_host("added-later.de", conn_tag="work")
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: any(r["title"] == "added-later.de" for r in d.get("rows", [])),
        timeout=6.0,
    )
    assert any(r["title"] == "added-later.de" for r in dump["rows"])


EXPECTED_MENU = [
    "SusOps:",        # status item (prefix match)
    "Settings…",
    "Start Proxy",
    "Stop Proxy",
    "Restart Proxy",
    "Show Status",
    "Show Logs",
    "Launch Browser",
    "Reset All",
    "About SusOps",
    "Quit",
]

REMOVED_MENU = ["Add", "Remove", "Manage", "Test", "File Transfer",
                "Open Config File", "Config Window…"]


def test_unified_menu_structure(tray_proc):
    menu = tray_proc.send("dump-menu")["menu"]
    titles = [n["title"] for n in menu if "title" in n]
    for expected in EXPECTED_MENU:
        assert any(t.startswith(expected) for t in titles), f"missing {expected}"
    for removed in REMOVED_MENU:
        assert not any(t == removed for t in titles), f"should be gone: {removed}"


def test_detail_renders_and_toggle_round_trip(tray_proc):
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_pac_host("blabla.de", conn_tag="work")
    assert tray_proc.send("open-config").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            n["key"] == "domains" and n["count"] == 1 for n in d.get("nav", [])),
    )
    sel = tray_proc.send("select domains 0")
    assert sel.get("ok"), sel
    dump = tray_proc.send("dump-window")
    assert dump["detail_title"] == "blabla.de"
    assert dump["detail_toggle"] is True
    assert "domain.test" in dump["detail_actions"]
    assert "domain.remove" in dump["detail_actions"]

    res = tray_proc.send("action domain.toggle")
    assert res.get("ok") is True, res
    dump2 = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("detail_toggle") is False,
    )
    assert dump2["detail_toggle"] is False
    # Col-2 row dims when the host is disabled.
    row = next((r for r in dump2["rows"] if r["title"] == "blabla.de"), None)
    assert row is not None and row["dimmed"] is True

    cfg = c.list_config()
    assert "blabla.de" in cfg.connections[0].pac_hosts_disabled


def test_connection_detail_renders(tray_proc):
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            n["key"] == "connections" and n["count"] == 1
            for n in d.get("nav", [])),
    )
    sel = tray_proc.send("select connections 0")
    assert sel.get("ok"), sel
    dump = tray_proc.send("dump-window")
    assert dump["detail_title"] == "work"
    assert dump["detail_toggle"] is True
    assert "conn.start" in dump["detail_actions"]
    assert "conn.remove" in dump["detail_actions"]


def test_inline_edit_forward_round_trip(tray_proc):
    """The headline edit test: select a forward, change a field, Save, and
    confirm the new value persisted to config + the form went clean."""
    from susops.client import SusOpsClient
    from susops.core.config import PortForward
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_local_forward("work", PortForward(src_port=5432, dst_port=5432,
                                            dst_addr="db.internal", tag="postgres"))
    assert tray_proc.send("open-config forwards").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and d.get("category") == "forwards"
        and any(r["title"] == "postgres" for r in d.get("rows", [])),
    )
    assert tray_proc.send("select forwards 0").get("ok")
    assert tray_proc.send("dump-window")["dirty"] is False

    res = tray_proc.send("set-field dst_port 5433")
    assert res.get("ok"), res
    assert tray_proc.send("dump-window")["dirty"] is True

    assert tray_proc.send("action forward.save").get("ok")
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("dirty") is False,
        timeout=6.0,
    )
    assert dump["dirty"] is False
    cfg = c.list_config()
    assert cfg.connections[0].forwards.local[0].dst_port == 5433


def test_dirty_suppresses_refresh(tray_proc):
    """While a col-3 form is dirty, an external config change must NOT clobber
    the in-flight edit. Cols 1-2 still refresh; col-3 stays put."""
    from susops.client import SusOpsClient
    from susops.core.config import PortForward
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_local_forward("work", PortForward(src_port=5432, dst_port=5432,
                                            dst_addr="db.internal", tag="postgres"))
    assert tray_proc.send("open-config forwards").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            r["title"] == "postgres" for r in d.get("rows", [])),
    )
    assert tray_proc.send("select forwards 0").get("ok")
    assert tray_proc.send("set-field tag xyz").get("ok")
    assert tray_proc.send("dump-window")["dirty"] is True

    # External change while the form is dirty.
    c.add_pac_host("late.example.com", conn_tag="work")
    time.sleep(3.0)  # well past the ~1 s poll
    dump = tray_proc.send("dump-window")
    assert dump["dirty"] is True
    assert dump["fields"].get("tag") == "xyz"  # form not clobbered


def test_inline_edit_forward_validation_keeps_form(tray_proc):
    """Invalid src_port -> alert path, config unchanged, form stays dirty."""
    from susops.client import SusOpsClient
    from susops.core.config import PortForward
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_local_forward("work", PortForward(src_port=5432, dst_port=5432,
                                            dst_addr="db.internal", tag="postgres"))
    assert tray_proc.send("open-config forwards").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            r["title"] == "postgres" for r in d.get("rows", [])),
    )
    assert tray_proc.send("select forwards 0").get("ok")
    assert tray_proc.send("set-field src_port 99999").get("ok")
    assert tray_proc.send("dump-window")["dirty"] is True
    tray_proc.send("action forward.save")
    time.sleep(1.0)
    dump = tray_proc.send("dump-window")
    assert dump["dirty"] is True  # save rejected, form kept
    cfg = c.list_config()
    # Original forward untouched.
    assert [f.src_port for f in cfg.connections[0].forwards.local] == [5432]
