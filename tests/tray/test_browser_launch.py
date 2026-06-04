"""Tests for browser-launch do_* methods on AbstractTrayApp."""
from __future__ import annotations


def test_do_launch_chrome_calls_subprocess(tray, monkeypatch):
    """do_launch_chrome finds a browser via shutil.which and calls Popen."""
    calls = []

    class _FakeProc:
        pass

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/google-chrome-stable" if name == "google-chrome-stable" else None,
    )
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *a, **kw: calls.append((a, kw)) or _FakeProc(),
    )
    # start() will fail to connect SSH but will bring up PAC server
    tray.do_add_connection("work", "user@host")
    tray.do_add_pac_host("example.com")
    tray.manager.start()
    tray.do_launch_chrome()
    assert calls, "subprocess.Popen should have been called"


def test_do_launch_chrome_no_pac_alerts(tray, monkeypatch):
    """When PAC isn't running, do_launch_chrome shows an alert instead of launching."""
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/google-chrome-stable" if name == "google-chrome-stable" else None,
    )
    # Do NOT start the PAC server — no connections, no start() call
    tray.do_launch_chrome()
    # No PAC → manager.get_pac_url() returns "" → show_alert fires
    assert any("PAC" in title or "PAC" in msg for title, msg in tray.alerts)


def test_do_launch_chrome_no_browser_alerts(tray, monkeypatch):
    """When no Chrome/Chromium is found, an appropriate alert is shown."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    tray.do_add_connection("work", "user@host")
    tray.manager.start()
    tray.do_launch_chrome()
    # Either "Error" for no browser found, or "Error" for no PAC
    assert any(t == "Error" for t, _ in tray.alerts)


def test_do_launch_firefox_calls_subprocess(tray, tmp_path, monkeypatch):
    """do_launch_firefox finds firefox and calls Popen with a profile."""
    calls = []

    class _FakeProc:
        pass

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/firefox" if name == "firefox" else None,
    )
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *a, **kw: calls.append((a, kw)) or _FakeProc(),
    )
    tray.do_add_connection("work", "user@host")
    tray.manager.start()
    tray.do_launch_firefox()
    assert calls, "subprocess.Popen should have been called for Firefox"


def test_do_launch_firefox_no_pac_alerts(tray, monkeypatch):
    """When PAC isn't running, do_launch_firefox shows an alert."""
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/firefox" if name == "firefox" else None,
    )
    tray.do_launch_firefox()
    assert any("PAC" in title or "PAC" in msg for title, msg in tray.alerts)


def test_do_open_config_file_calls_subprocess(tray, monkeypatch):
    """do_open_config_file calls Popen with an opener."""
    calls = []

    class _FakeProc:
        pass

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
    )
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *a, **kw: calls.append((a, kw)) or _FakeProc(),
    )
    tray.do_open_config_file()
    assert calls, "subprocess.Popen should have been called to open config"
