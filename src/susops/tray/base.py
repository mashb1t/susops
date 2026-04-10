"""AbstractTrayApp — shared tray app logic for Linux and macOS."""
from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from susops.core.types import ProcessState

_ASSETS_DIR = Path(__file__).parent.parent.parent.parent / "assets" / "icons"

_STATE_FILENAMES = {
    ProcessState.RUNNING: "running",
    ProcessState.STOPPED_PARTIALLY: "stopped_partially",
    ProcessState.STOPPED: "stopped",
    ProcessState.ERROR: "error",
    ProcessState.INITIAL: "stopped",
}


def get_icon_path(
    state: ProcessState,
    logo_style: str = "colored_glasses",
    variant: str = "dark",
    prefer_png: bool = False,
) -> str | None:
    """Return the icon path for a given state, style, and variant.

    Tries the requested variant first, then falls back to the other.
    If prefer_png is True, checks .png before .svg; otherwise SVG first.
    """
    name = _STATE_FILENAMES.get(state, "stopped")
    exts = ("png", "svg") if prefer_png else ("svg", "png")
    other_variant = "dark" if variant == "light" else "light"

    for v in (variant, other_variant):
        base = _ASSETS_DIR / logo_style.lower() / v / name
        for ext in exts:
            p = base.with_suffix(f".{ext}")
            if p.exists():
                return str(p)
    return None


def get_ssh_hosts() -> list[str]:
    """Return non-wildcard Host entries from ~/.ssh/config."""
    cfg = Path.home() / ".ssh" / "config"
    if not cfg.exists():
        return []
    hosts = []
    pattern = re.compile(r"^\s*Host\s+(.*)$", re.IGNORECASE)
    for line in cfg.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = pattern.match(line)
        if m:
            for h in m.group(1).split():
                if "*" not in h and "?" not in h:
                    hosts.append(h)
    return hosts

from susops.core.config import PortForward
from susops.facade import SusOpsManager


class AbstractTrayApp(ABC):
    """Base class for tray apps. Subclasses implement the platform-specific UI layer.

    All business logic lives here; linux.py and mac.py each provide:
      - update_icon(state)
      - update_menu_sensitivity(state)
      - show_alert(title, msg)
      - show_output_dialog(title, output)
      - run_in_background(fn, callback)
      - schedule_poll(interval_seconds)
    """

    def __init__(self) -> None:
        self.manager = SusOpsManager()
        self.manager.on_state_change = self._on_state_change_safe
        self.state = ProcessState.INITIAL

    # ------------------------------------------------------------------ #
    # Platform abstractions (must be implemented by subclass)
    # ------------------------------------------------------------------ #

    @abstractmethod
    def update_icon(self, state: ProcessState) -> None: ...

    @abstractmethod
    def update_menu_sensitivity(self, state: ProcessState) -> None: ...

    @abstractmethod
    def show_alert(self, title: str, msg: str) -> None: ...

    @abstractmethod
    def show_output_dialog(self, title: str, output: str) -> None: ...

    @abstractmethod
    def run_in_background(self, fn: Callable, callback: Callable | None = None) -> None: ...

    @abstractmethod
    def schedule_poll(self, interval_seconds: int) -> None: ...

    # ------------------------------------------------------------------ #
    # State management
    # ------------------------------------------------------------------ #

    def _on_state_change_safe(self, state: ProcessState) -> None:
        """Called from background threads; subclass must thread-marshal if needed."""
        self.state = state
        self.update_icon(state)
        self.update_menu_sensitivity(state)

    def do_poll(self) -> None:
        """Poll SusOpsManager status and update UI. Called on a timer."""
        result = self.manager.status()
        self._on_state_change_safe(result.state)

    # ------------------------------------------------------------------ #
    # Shared actions
    # ------------------------------------------------------------------ #

    def do_start(self) -> None:
        def _run():
            result = self.manager.start()
            return result.message, result.success

        def _done(result):
            msg, ok = result
            if not ok:
                self.show_alert("Start failed", msg)

        self.run_in_background(_run, _done)

    def do_stop(self) -> None:
        def _run():
            result = self.manager.stop()
            return result.message, result.success

        def _done(result):
            msg, ok = result
            if not ok:
                self.show_alert("Stop failed", msg)

        self.run_in_background(_run, _done)

    def do_restart(self) -> None:
        def _run():
            result = self.manager.restart()
            return result.message, result.success

        def _done(result):
            msg, ok = result
            if not ok:
                self.show_alert("Restart failed", msg)

        self.run_in_background(_run, _done)

    def do_test(self, target: str = "") -> None:
        if target:
            def _run():
                r = self.manager.test(target)
                icon = "✓" if r.success else "✗"
                lat = f" ({r.latency_ms:.0f}ms)" if r.latency_ms else ""
                return f"{icon} {r.target}{lat}: {r.message}"
            self.run_in_background(_run, lambda msg: self.show_output_dialog("Test result", msg))
        else:
            def _run():
                results = self.manager.test_all()
                lines = []
                for r in results:
                    icon = "✓" if r.success else "✗"
                    lat = f" ({r.latency_ms:.0f}ms)" if r.latency_ms else ""
                    lines.append(f"{icon} {r.target}{lat}: {r.message}")
                return "\n".join(lines) or "No PAC hosts configured."
            self.run_in_background(_run, lambda msg: self.show_output_dialog("Test all results", msg))

    def do_status(self) -> None:
        def _run():
            result = self.manager.status()
            lines = []
            for cs in result.connection_statuses:
                dot = "●" if cs.running else "○"
                port = f" ({cs.socks_port})" if cs.socks_port else ""
                pid = f" pid={cs.pid}" if cs.pid else ""
                lines.append(f"  {dot} [{cs.tag}]{port}{pid}")
            pac = "●" if result.pac_running else "○"
            pac_port = f" ({result.pac_port})" if result.pac_port else ""
            lines.append(f"  {pac} PAC server{pac_port}")
            lines.append(f"State: {result.state.value}")
            return "\n".join(lines)
        self.run_in_background(_run, lambda msg: self.show_output_dialog("Status", msg))

    def do_add_connection(self, tag: str, host: str, port: int = 0) -> None:
        try:
            conn = self.manager.add_connection(tag, host, port)
            if self._should_restart_after_change():
                self.do_restart()
            else:
                self.show_alert("Added", f"Connection '{conn.tag}' → {conn.ssh_host}")
        except ValueError as e:
            self.show_alert("Error", str(e))

    def do_remove_connection(self, tag: str) -> None:
        try:
            self.manager.remove_connection(tag)
            if self._should_restart_after_change():
                self.do_restart()
        except ValueError as e:
            self.show_alert("Error", str(e))

    def do_add_pac_host(self, host: str, conn_tag: str | None = None) -> None:
        try:
            self.manager.add_pac_host(host, conn_tag=conn_tag)
        except ValueError as e:
            self.show_alert("Error", str(e))

    def do_remove_pac_host(self, host: str) -> None:
        try:
            self.manager.remove_pac_host(host)
        except ValueError as e:
            self.show_alert("Error", str(e))

    def do_add_local_forward(self, conn_tag: str, fw: PortForward) -> None:
        try:
            # Facade starts the slave immediately if ControlMaster is running — no restart needed.
            self.manager.add_local_forward(conn_tag, fw)
        except ValueError as e:
            self.show_alert("Error", str(e))

    def do_add_remote_forward(self, conn_tag: str, fw: PortForward) -> None:
        try:
            self.manager.add_remote_forward(conn_tag, fw)
        except ValueError as e:
            self.show_alert("Error", str(e))

    def do_remove_local_forward(self, port: int) -> None:
        try:
            # Facade kills the slave immediately — no restart needed.
            self.manager.remove_local_forward(port)
        except ValueError as e:
            self.show_alert("Error", str(e))

    def do_remove_remote_forward(self, port: int) -> None:
        try:
            self.manager.remove_remote_forward(port)
        except ValueError as e:
            self.show_alert("Error", str(e))

    def do_launch_chrome(self) -> None:
        import shutil, subprocess
        pac_url = self.manager.get_pac_url()
        if not pac_url:
            self.show_alert("Error", "PAC server is not running")
            return
        for browser in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "brave-browser"):
            if shutil.which(browser):
                subprocess.Popen([browser, f"--proxy-pac-url={pac_url}"])
                return
        self.show_alert("Error", "No Chrome/Chromium browser found")

    def do_launch_firefox(self) -> None:
        import shutil, subprocess
        pac_url = self.manager.get_pac_url()
        if not pac_url:
            self.show_alert("Error", "PAC server is not running")
            return
        profile_dir = self.manager.workspace / "firefox_profile"
        profile_dir.mkdir(exist_ok=True)
        (profile_dir / "user.js").write_text(
            f'user_pref("network.proxy.type", 2);\n'
            f'user_pref("network.proxy.autoconfig_url", "{pac_url}");\n'
            f'user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1");\n'
        )
        if shutil.which("firefox"):
            subprocess.Popen(["firefox", "-profile", str(profile_dir), "-no-remote"])
        else:
            self.show_alert("Error", "Firefox not found")

    def do_open_config_file(self) -> None:
        import subprocess, shutil
        config_path = self.manager.workspace / "config.yaml"
        for opener in ("xdg-open", "open", "notepad"):
            if shutil.which(opener):
                subprocess.Popen([opener, str(config_path)])
                return

    # ------------------------------------------------------------------ #
    # File sharing
    # ------------------------------------------------------------------ #

    def do_share(
        self,
        conn_tag: str,
        file_path: str,
        password: str | None = None,
        port: int = 0,
    ) -> None:
        def _run():
            try:
                info = self.manager.share(
                    __import__("pathlib").Path(file_path),
                    conn_tag,
                    password=password or None,
                    port=port or None,
                )
                return info, None
            except Exception as exc:
                return None, str(exc)

        def _done(result):
            info, err = result
            if err:
                self.show_alert("Share Failed", err)
            elif info:
                self.show_alert(
                    "Share Started",
                    f"Sharing {__import__('pathlib').Path(info.file_path).name}\n"
                    f"Port:     {info.port}\n"
                    f"Password: {info.password}",
                )

        self.run_in_background(_run, _done)

    def do_stop_share(self, port: int | None = None) -> None:
        def _run():
            self.manager.stop_share(port)
        self.run_in_background(_run)

    def do_delete_share(self, port: int) -> None:
        def _run():
            self.manager.delete_share(port)
        self.run_in_background(_run)

    def do_fetch(
        self,
        conn_tag: str,
        port: int,
        password: str,
        outfile: str | None = None,
    ) -> None:
        def _run():
            try:
                out = __import__("pathlib").Path(outfile) if outfile else None
                result = self.manager.fetch(
                    port=port, password=password, conn_tag=conn_tag, outfile=out
                )
                return str(result), None
            except Exception as exc:
                return None, str(exc)

        def _done(result):
            path, err = result
            if err:
                self.show_alert("Fetch Failed", err)
            else:
                self.show_alert("Download Complete", f"Saved to:\n{path}")

        self.run_in_background(_run, _done)

    def do_list_shares(self) -> list:
        return self.manager.list_shares()

    def do_reset(self, force: bool = False) -> None:
        self.manager.reset()
        self.show_alert("Reset", "Workspace has been reset.")

    def do_quit(self) -> None:
        if self.manager.app_config.stop_on_quit:
            self.manager.stop()

    def _should_restart_after_change(self) -> bool:
        return self.state == ProcessState.RUNNING
