"""Tests for the consolidated do_launch_browser on AbstractTrayApp.

These exercise the *real* launch path (core.browsers.launch_with_pac /
open_proxy_settings) — the old tests only hit a since-deleted base method that
no shipped frontend called.
"""
from __future__ import annotations

from susops.core.browsers import Browser


def _chromium() -> Browser:
    # No bundle → launch_with_pac uses launch_cmd + flag (Linux-style), which
    # is the deterministic, platform-independent path for these assertions.
    return Browser(name="Chromium", launch_cmd=["/usr/bin/chromium"], is_chromium=True)


def _firefox() -> Browser:
    return Browser(name="Firefox", launch_cmd=["/usr/bin/firefox"], is_chromium=False)


def test_launch_chromium_uses_transformed_command(tray, monkeypatch):
    """do_launch_browser launches the detected Browser with the PAC flag
    appended — the transformed command, not the raw launch_cmd."""
    calls = []
    monkeypatch.setattr("susops.core.browsers.subprocess.Popen",
                        lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(tray.manager, "get_pac_url",
                        lambda: "http://localhost:9000/proxy.pac")

    tray.do_launch_browser(_chromium())

    assert calls == [["/usr/bin/chromium",
                      "--proxy-pac-url=http://localhost:9000/proxy.pac"]]
    assert not tray.alerts


def test_launch_without_pac_alerts_and_does_not_launch(tray, monkeypatch):
    calls = []
    monkeypatch.setattr("susops.core.browsers.subprocess.Popen",
                        lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(tray.manager, "get_pac_url", lambda: "")

    tray.do_launch_browser(_chromium())

    assert not calls
    assert any("Proxy" in t or "PAC" in m for t, m in tray.alerts)


def test_launch_firefox_writes_profile_and_launches(tray, monkeypatch):
    calls = []
    monkeypatch.setattr("susops.core.browsers.subprocess.Popen",
                        lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(tray.manager, "get_pac_url",
                        lambda: "http://localhost:9000/proxy.pac")

    tray.do_launch_browser(_firefox())

    profile = tray.manager.workspace / "firefox_profile"
    assert (profile / "user.js").exists()
    assert calls and "-profile" in calls[0] and str(profile) in calls[0]
    assert not tray.alerts


def test_settings_only_opens_proxy_page(tray, monkeypatch):
    calls = []
    monkeypatch.setattr("susops.core.browsers.subprocess.Popen",
                        lambda cmd, *a, **k: calls.append(cmd))
    # The proxy-settings page doesn't need the PAC URL.
    tray.do_launch_browser(_chromium(), settings_only=True)

    assert calls and any("net-internals" in part for part in calls[0])


def test_launch_error_surfaces_via_alert(tray, monkeypatch):
    def boom(cmd, *a, **k):
        raise OSError("no such binary")

    monkeypatch.setattr("susops.core.browsers.subprocess.Popen", boom)
    monkeypatch.setattr(tray.manager, "get_pac_url",
                        lambda: "http://localhost:9000/proxy.pac")

    tray.do_launch_browser(_chromium())

    assert any(t == "Launch Failed" for t, _ in tray.alerts)


def test_do_open_config_file_calls_subprocess(tray, monkeypatch):
    """do_open_config_file calls Popen with an opener."""
    calls = []

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
    )
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *a, **kw: calls.append((a, kw)),
    )
    tray.do_open_config_file()
    assert calls, "subprocess.Popen should have been called to open config"
