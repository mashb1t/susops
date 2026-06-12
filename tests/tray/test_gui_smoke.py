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
