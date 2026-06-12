"""macOS GUI smoke tests for the rumps tray with debug server.

Phase 0 of the self-verification feedback loop: exercises dump-menu,
open-about, and in-process screenshot via TrayDebugServer.

Skipped unless SUSOPS_RUN_GUI_TESTS=1 is set (macOS only).

Run locally:
    SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest tests/tray/test_gui_smoke.py -v
"""
from __future__ import annotations

import os
import platform

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(platform.system() != "Darwin", reason="macOS only"),
    pytest.mark.skipif(
        not os.environ.get("SUSOPS_RUN_GUI_TESTS"),
        reason="set SUSOPS_RUN_GUI_TESTS=1 to run GUI smoke tests",
    ),
]


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
    import time
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_pac_host("blabla.de", conn_tag="work")
    assert tray_proc.send("open-config").get("ok")
    # Initial data loads asynchronously; allow up to 5 s for the first poll.
    deadline = time.monotonic() + 5.0
    dump = {}
    while time.monotonic() < deadline:
        dump = tray_proc.send("dump-window")
        if dump.get("open") and any("work" in t for t in dump.get("tabs", [])):
            break
        time.sleep(0.5)
    assert dump["open"] is True
    assert any("work" in t for t in dump["tabs"])
    labels = [r["label"] for r in dump["sidebar"]]
    assert "DOMAINS" in labels
    assert any("blabla.de" in l for l in labels)
    sel = tray_proc.send("select work domains 0")
    assert sel.get("ok"), sel


def test_window_reflects_external_changes(tray_proc):
    """The poll-driven refresh must pick up daemon-side changes."""
    import time
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config").get("ok")
    c.add_pac_host("added-later.de", conn_tag="work")
    time.sleep(4)  # > one poll interval
    labels = [r["label"] for r in tray_proc.send("dump-window")["sidebar"]]
    assert any("added-later.de" in l for l in labels)


def test_add_menu_populated(tray_proc):
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config").get("ok")
    dump = tray_proc.send("dump-window")
    menu = dump.get("add_menu", [])
    assert any("Add Domain" in m for m in menu)
    assert any("Add Local Forward" in m for m in menu)
    assert any("Add Remote Forward" in m for m in menu)
    assert any("Share File" in m for m in menu)
    assert any("Fetch File" in m for m in menu)


def test_gear_tab_shows_settings_and_hides_sidebar(tray_proc):
    """Selecting the gear tab renders app settings and hides the
    per-connection sidebar + Add control; switching back restores them."""
    import time
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config").get("ok")

    # Wait for the connection tab to be ready.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        dump = tray_proc.send("dump-window")
        if dump.get("open") and dump.get("current_tag") == "work":
            break
        time.sleep(0.5)
    assert dump.get("sidebar_hidden") is False
    assert dump.get("gear") is False

    # Jump to the gear tab. The pane render is deferred to the next run-loop
    # pass via NSTimer, so wait for detail_title to flip to App Settings.
    assert tray_proc.send("open-config gear").get("ok")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        dump = tray_proc.send("dump-window")
        if dump.get("gear") and dump.get("detail_title") == "App Settings":
            break
        time.sleep(0.25)
    assert dump.get("gear") is True
    assert dump.get("mode") == "gear"
    assert dump.get("sidebar_hidden") is True
    assert dump.get("detail_title") == "App Settings"

    # Switch back to the connection tab — sidebar + Add reappear.
    assert tray_proc.send("open-config work").get("ok")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        dump = tray_proc.send("dump-window")
        if not dump.get("gear"):
            break
        time.sleep(0.25)
    assert dump.get("gear") is False
    assert dump.get("sidebar_hidden") is False


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
