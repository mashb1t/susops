"""Cross-platform browser detection + PAC-aware launch.

Single source of truth for browser metadata and launch logic so every
frontend (TUI, macOS tray, Linux tray) shares one detection table and
one set of platform-specific launch incantations.

Public API:
    detect_browsers()           → list[Browser]
    launch_with_pac(browser, pac_url, profile_dir)
    open_proxy_settings(browser)
"""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
import sys
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Browser:
    """A detected browser installation."""
    name: str               # display name, e.g. "Chrome"
    launch_cmd: list[str]   # base command — e.g. ["open", "-a", "Google Chrome"] or ["/usr/bin/google-chrome"]
    is_chromium: bool       # True for Chrome/Brave/Edge/Vivaldi/Chromium/Arc; False for Firefox
    bundle: str | None = None  # macOS app-bundle name (None on Linux)


# Browser metadata — extend here when a new browser is added.
#   (name, macOS bundle, Linux executables, is_chromium)
_BROWSER_DEFS: list[tuple[str, str, list[str], bool]] = [
    ("Chrome",   "Google Chrome",   ["google-chrome", "google-chrome-stable"],          True),
    ("Chromium", "Chromium",        ["chromium", "chromium-browser"],                   True),
    ("Brave",    "Brave Browser",   ["brave-browser", "brave", "brave-browser-stable"], True),
    ("Vivaldi",  "Vivaldi",         ["vivaldi", "vivaldi-stable"],                      True),
    ("Edge",     "Microsoft Edge",  ["microsoft-edge", "microsoft-edge-stable"],        True),
    ("Arc",      "Arc",             [],                                                 True),  # macOS-only
    ("Firefox",  "Firefox",         ["firefox", "firefox-bin"],                         False),
]

_PROXY_SETTINGS_URL = "chrome://net-internals/#proxy"


def detect_browsers() -> list[Browser]:
    """Return browsers detected on the current platform.

    Order follows _BROWSER_DEFS — Chromium-family first, Firefox last.
    """
    if sys.platform == "darwin":
        return _detect_macos()
    return _detect_linux()


def _detect_macos() -> list[Browser]:
    found: list[Browser] = []
    for name, bundle, _exes, chromium in _BROWSER_DEFS:
        for base in (Path("/Applications"), Path.home() / "Applications"):
            if (base / f"{bundle}.app").exists():
                found.append(Browser(
                    name=name,
                    launch_cmd=["open", "-a", bundle],
                    is_chromium=chromium,
                    bundle=bundle,
                ))
                break
    return found


def _detect_linux() -> list[Browser]:
    found: list[Browser] = []
    for name, _bundle, exes, chromium in _BROWSER_DEFS:
        exe = next((shutil.which(e) for e in exes if shutil.which(e)), None)
        if exe:
            found.append(Browser(
                name=name,
                launch_cmd=[exe],
                is_chromium=chromium,
            ))
    return found


def launch_with_pac(browser: Browser, pac_url: str,
                    profile_dir: Path | None = None) -> None:
    """Launch the browser with PAC URL pre-configured.

    Chromium-family: passed as ``--proxy-pac-url=<url>``. macOS uses
    ``open -na`` so a new instance picks up the flag rather than the
    existing process ignoring it.

    Firefox: requires a profile directory with ``user.js`` written —
    Firefox doesn't accept a PAC URL via command-line flag. The caller
    is expected to provide a workspace-owned profile dir; we write the
    prefs and launch with ``-profile <dir> -no-remote``.

    Raises subprocess.SubprocessError or OSError on launch failure;
    caller is responsible for surfacing.
    """
    if browser.is_chromium:
        if sys.platform == "darwin" and browser.bundle is not None:
            cmd = ["open", "-na", browser.bundle, "--args", f"--proxy-pac-url={pac_url}"]
        else:
            cmd = browser.launch_cmd + [f"--proxy-pac-url={pac_url}"]
        subprocess.Popen(cmd)
        return

    # Firefox path
    if profile_dir is None:
        raise ValueError(
            "Firefox launch requires a profile_dir (Firefox doesn't accept "
            "a PAC URL as a command-line flag)."
        )
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "user.js").write_text(
        f'user_pref("network.proxy.type", 2);\n'
        f'user_pref("network.proxy.autoconfig_url", "{pac_url}");\n'
        f'user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1");\n'
    )
    if sys.platform == "darwin" and browser.bundle is not None:
        cmd = ["open", "-na", browser.bundle, "--args",
               "-profile", str(profile_dir), "-no-remote"]
    else:
        cmd = browser.launch_cmd + ["-profile", str(profile_dir), "-no-remote"]
    subprocess.Popen(cmd)


def open_proxy_settings(browser: Browser) -> None:
    """Open the browser at its internal proxy debug page.

    Chromium-family browsers interpret ``chrome://net-internals/#proxy``
    as an internal page; macOS ``open -a <bundle> <url>`` and Linux
    ``<exe> <url>`` both navigate straight to it.

    Firefox doesn't have a single chrome://-style proxy debug URL, so
    this is a no-op for non-Chromium browsers.
    """
    if not browser.is_chromium:
        return
    if sys.platform == "darwin" and browser.bundle is not None:
        cmd = ["open", "-a", browser.bundle, _PROXY_SETTINGS_URL]
    else:
        cmd = browser.launch_cmd + [_PROXY_SETTINGS_URL]
    subprocess.Popen(cmd)
