"""Cross-platform browser detection + PAC-aware launch.

Single source of truth for browser metadata and launch logic so every
frontend (TUI, macOS tray, Linux tray) shares one detection pipeline.

Detection is **auto-discovery** — we scan platform-native registries for
HTTP-scheme handlers and classify chromium-vs-firefox via well-known
bundle-id / executable-name patterns. The result is that out-of-the-box
forks (Chrome Beta/Canary, LibreWolf, Waterfox, Tor Browser, Arc, etc.)
appear in the list without us maintaining a hardcoded table.

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
    is_chromium: bool       # True for Chrome/Brave/Edge/Vivaldi/Chromium/Arc; False for Firefox-family
    bundle: str | None = None  # macOS app-bundle name (None on Linux)


_PROXY_SETTINGS_URL = "chrome://net-internals/#proxy"


# Bundle-id substrings that mark a browser as chromium-family on macOS.
# Match is case-insensitive `in` substring (so "google.chrome.beta" matches).
_MAC_CHROMIUM_BUNDLE_IDS = (
    "com.google.chrome",
    "com.brave.browser",
    "org.chromium",
    "com.microsoft.edgemac",
    "com.vivaldi",
    "com.thebrowser.browser",      # Arc
    "company.thebrowser.browser",
    "com.operasoftware.opera",
)

# Same for Firefox-family. Anything matching neither is dropped (e.g. Safari
# can't be steered via `--proxy-pac-url` or chrome://-style URLs, so we
# don't surface it).
_MAC_FIREFOX_BUNDLE_IDS = (
    "org.mozilla.firefox",
    "org.mozilla.nightly",
    "io.gitlab.librewolf",
    "net.waterfox",
    "org.torproject.torbrowser",
)


# Linux: executable basename substrings (case-insensitive).
_LINUX_CHROMIUM_EXES = (
    "google-chrome", "chrome", "chromium",
    "brave", "vivaldi", "microsoft-edge", "edge", "opera",
    "arc",  # speculative — Arc on Linux is in beta as of writing
)
_LINUX_FIREFOX_EXES = (
    "firefox", "librewolf", "waterfox", "torbrowser", "tor-browser",
)


def detect_browsers() -> list[Browser]:
    """Return browsers detected on the current platform.

    Result is ordered: chromium-family first (alphabetised by display name),
    Firefox-family last. Order matters for UX — chromium-style PAC support
    is more reliable, so it goes first in pickers.
    """
    if sys.platform == "darwin":
        browsers = _detect_macos()
    else:
        browsers = _detect_linux()
    return sorted(browsers, key=lambda b: (not b.is_chromium, b.name.lower()))


# ---------------------------------------------------------------------------
# macOS — scan .app bundles for HTTP-scheme handlers via Info.plist
# ---------------------------------------------------------------------------


def _detect_macos() -> list[Browser]:
    import plistlib

    found: list[Browser] = []
    seen_bundles: set[str] = set()
    candidate_dirs = [Path("/Applications"), Path.home() / "Applications"]
    for d in candidate_dirs:
        if not d.is_dir():
            continue
        for app_dir in d.glob("*.app"):
            bundle_name = app_dir.stem  # e.g. "Google Chrome"
            if bundle_name in seen_bundles:
                continue
            plist_path = app_dir / "Contents" / "Info.plist"
            if not plist_path.is_file():
                continue
            try:
                with open(plist_path, "rb") as f:
                    info = plistlib.load(f)
            except Exception:
                continue
            if not _macos_handles_http(info):
                continue
            bundle_id = (info.get("CFBundleIdentifier") or "").lower()
            is_chromium = any(p in bundle_id for p in _MAC_CHROMIUM_BUNDLE_IDS)
            is_firefox = any(p in bundle_id for p in _MAC_FIREFOX_BUNDLE_IDS)
            if not (is_chromium or is_firefox):
                # Surfacing Safari et al. would be pointless — they can't
                # accept a PAC URL via the command line. Skip.
                continue
            seen_bundles.add(bundle_name)
            display_name = (
                info.get("CFBundleDisplayName")
                or info.get("CFBundleName")
                or bundle_name
            )
            found.append(Browser(
                name=str(display_name),
                launch_cmd=["open", "-a", bundle_name],
                is_chromium=is_chromium,
                bundle=bundle_name,
            ))
    return found


def _macos_handles_http(info: dict) -> bool:
    """Return True if the Info.plist declares an http/https URL handler."""
    for url_type in info.get("CFBundleURLTypes", []) or []:
        for scheme in url_type.get("CFBundleURLSchemes", []) or []:
            if str(scheme).lower() in ("http", "https"):
                return True
    return False


# ---------------------------------------------------------------------------
# Linux — scan .desktop files in standard XDG application directories
# ---------------------------------------------------------------------------


def _detect_linux() -> list[Browser]:
    import os

    desktop_dirs: list[Path] = []
    xdg_data_dirs = os.environ.get(
        "XDG_DATA_DIRS", "/usr/local/share:/usr/share"
    ).split(":")
    xdg_data_home = os.environ.get(
        "XDG_DATA_HOME", str(Path.home() / ".local" / "share")
    )
    for base in [xdg_data_home, *xdg_data_dirs]:
        p = Path(base) / "applications"
        if p.is_dir():
            desktop_dirs.append(p)
    # Flatpak / Snap exports often land here too.
    for extra in (
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local" / "share" / "flatpak" / "exports" / "share" / "applications",
        Path("/var/lib/snapd/desktop/applications"),
    ):
        if extra.is_dir():
            desktop_dirs.append(extra)

    found: list[Browser] = []
    seen_exes: set[str] = set()
    for d in desktop_dirs:
        for path in d.glob("*.desktop"):
            entry = _parse_desktop_entry(path)
            if entry is None:
                continue
            if not _linux_handles_http(entry):
                continue
            exec_cmd = _linux_resolve_exec(entry.get("Exec", ""))
            if not exec_cmd:
                continue
            exe_path = exec_cmd[0]
            exe_basename = Path(exe_path).name.lower()
            if exe_basename in seen_exes:
                continue
            is_chromium = any(p in exe_basename for p in _LINUX_CHROMIUM_EXES)
            is_firefox = any(p in exe_basename for p in _LINUX_FIREFOX_EXES)
            if not (is_chromium or is_firefox):
                continue
            seen_exes.add(exe_basename)
            display_name = entry.get("Name", exe_basename)
            found.append(Browser(
                name=display_name,
                launch_cmd=[exe_path],
                is_chromium=is_chromium,
            ))
    return found


def _parse_desktop_entry(path: Path) -> dict | None:
    """Parse the [Desktop Entry] section of a .desktop file into a dict.

    Returns None if the file isn't a valid desktop entry (NoDisplay=true,
    Type != Application, etc.).
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    entry: dict = {}
    in_section = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[Desktop Entry]"):
            in_section = True
            continue
        if line.startswith("[") and in_section:
            break
        if not in_section or "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        # Skip locale-specific variants (Name[de], etc.) — we want the
        # default. Lookup is first-occurrence-wins.
        if "[" in key:
            continue
        entry.setdefault(key.strip(), value.strip())
    if entry.get("Type", "Application") != "Application":
        return None
    if entry.get("NoDisplay", "false").lower() == "true":
        return None
    if entry.get("Hidden", "false").lower() == "true":
        return None
    return entry


def _linux_handles_http(entry: dict) -> bool:
    mime = entry.get("MimeType", "")
    return ("x-scheme-handler/http" in mime) or ("x-scheme-handler/https" in mime)


def _linux_resolve_exec(exec_field: str) -> list[str]:
    """Resolve the Exec= field to an absolute command list.

    `Exec=` may contain field codes (%u, %U, %f, %F) — we strip them.
    The first token is the executable; we resolve it via shutil.which if
    it's not already an absolute path. Returns [] if the executable can't
    be found on PATH (skip it).
    """
    if not exec_field:
        return []
    # Strip %X field codes — they're placeholders for URL/file args we
    # don't pass.
    tokens = [t for t in exec_field.split() if not (t.startswith("%") and len(t) == 2)]
    if not tokens:
        return []
    exe = tokens[0]
    if not exe.startswith("/"):
        resolved = shutil.which(exe)
        if resolved is None:
            return []
        exe = resolved
    return [exe, *tokens[1:]]


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


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
