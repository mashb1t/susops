"""Linux tray app — GTK3 + AyatanaAppIndicator3.

Requires: python-gobject, gtk3, libayatana-appindicator (system packages).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from susops.core.types import ProcessState
from susops.tray.base import AbstractTrayApp

_STATE_ICON_NAMES = {
    ProcessState.RUNNING: "susops-running",
    ProcessState.STOPPED_PARTIALLY: "susops-partial",
    ProcessState.STOPPED: "susops-stopped",
    ProcessState.ERROR: "susops-error",
    ProcessState.INITIAL: "susops-stopped",
}

_ASSETS_DIR = Path(__file__).parent.parent.parent.parent / "assets" / "icons"


def _get_icon_path(state: ProcessState) -> str:
    """Return path to SVG icon for this state (falls back to generic)."""
    subdir = "colored_glasses"
    variant = "dark"
    state_file_map = {
        ProcessState.RUNNING: "active",
        ProcessState.STOPPED_PARTIALLY: "partial",
        ProcessState.STOPPED: "inactive",
        ProcessState.ERROR: "inactive",
        ProcessState.INITIAL: "inactive",
    }
    name = state_file_map.get(state, "inactive")
    candidate = _ASSETS_DIR / subdir / variant / f"{name}.svg"
    if candidate.exists():
        return str(candidate)
    # Fallback: look for any SVG
    for svg in (_ASSETS_DIR / subdir / variant).glob("*.svg"):
        return str(svg)
    return ""


class SusOpsLinuxTray(AbstractTrayApp):
    """GTK3 system tray application."""

    def __init__(self) -> None:
        super().__init__()
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3, Gtk, GLib
        self._Gtk = Gtk
        self._GLib = GLib
        self._AyatanaAppIndicator3 = AyatanaAppIndicator3

        self._indicator = AyatanaAppIndicator3.Indicator.new(
            "susops",
            _get_icon_path(ProcessState.STOPPED) or "application-exit",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

        self._menu = Gtk.Menu()
        self._build_menu()
        self._indicator.set_menu(self._menu)

    # ------------------------------------------------------------------ #
    # AbstractTrayApp implementation
    # ------------------------------------------------------------------ #

    def update_icon(self, state: ProcessState) -> None:
        icon_path = _get_icon_path(state)
        if icon_path:
            self._GLib.idle_add(self._indicator.set_icon_full, icon_path, state.value)

    def update_menu_sensitivity(self, state: ProcessState) -> None:
        running = state == ProcessState.RUNNING
        stopped = state == ProcessState.STOPPED

        def _update():
            if hasattr(self, "_item_start"):
                self._item_start.set_sensitive(not running)
            if hasattr(self, "_item_stop"):
                self._item_stop.set_sensitive(not stopped)
            if hasattr(self, "_item_restart"):
                self._item_restart.set_sensitive(not stopped)
            self._rebuild_status_item(state)
            return False

        self._GLib.idle_add(_update)

    def show_alert(self, title: str, msg: str) -> None:
        def _show():
            dialog = self._Gtk.MessageDialog(
                transient_for=None,
                flags=0,
                message_type=self._Gtk.MessageType.INFO,
                buttons=self._Gtk.ButtonsType.OK,
                text=title,
            )
            dialog.format_secondary_text(msg)
            dialog.run()
            dialog.destroy()
            return False
        self._GLib.idle_add(_show)

    def show_output_dialog(self, title: str, output: str) -> None:
        def _show():
            dialog = self._Gtk.Dialog(title=title, transient_for=None, flags=0)
            dialog.add_button("Close", self._Gtk.ResponseType.CLOSE)
            dialog.set_default_size(400, 300)
            sw = self._Gtk.ScrolledWindow()
            tv = self._Gtk.TextView()
            tv.set_editable(False)
            tv.set_monospace(True)
            tv.get_buffer().set_text(output)
            sw.add(tv)
            dialog.get_content_area().add(sw)
            dialog.show_all()
            dialog.run()
            dialog.destroy()
            return False
        self._GLib.idle_add(_show)

    def run_in_background(self, fn: Callable, callback: Callable | None = None) -> None:
        def _worker():
            result = fn()
            if callback is not None:
                self._GLib.idle_add(callback, result)
        threading.Thread(target=_worker, daemon=True).start()

    def schedule_poll(self, interval_seconds: int) -> None:
        def _poll():
            self.do_poll()
            return True  # keep repeating
        self._GLib.timeout_add_seconds(interval_seconds, _poll)

    # ------------------------------------------------------------------ #
    # Menu building
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> None:
        Gtk = self._Gtk
        self._item_status = Gtk.MenuItem(label="SusOps — unknown")
        self._item_status.set_sensitive(False)
        self._menu.append(self._item_status)

        self._menu.append(Gtk.SeparatorMenuItem())

        self._item_start = Gtk.MenuItem(label="▶ Start")
        self._item_start.connect("activate", lambda _: self.do_start())
        self._menu.append(self._item_start)

        self._item_stop = Gtk.MenuItem(label="■ Stop")
        self._item_stop.connect("activate", lambda _: self.do_stop())
        self._menu.append(self._item_stop)

        self._item_restart = Gtk.MenuItem(label="↺ Restart")
        self._item_restart.connect("activate", lambda _: self.do_restart())
        self._menu.append(self._item_restart)

        self._menu.append(Gtk.SeparatorMenuItem())

        item_test = Gtk.MenuItem(label="Test connections")
        item_test.connect("activate", lambda _: self.do_test())
        self._menu.append(item_test)

        item_status = Gtk.MenuItem(label="Show status")
        item_status.connect("activate", lambda _: self.do_status())
        self._menu.append(item_status)

        self._menu.append(Gtk.SeparatorMenuItem())

        item_chrome = Gtk.MenuItem(label="Launch Chrome with PAC")
        item_chrome.connect("activate", lambda _: self.do_launch_chrome())
        self._menu.append(item_chrome)

        item_firefox = Gtk.MenuItem(label="Launch Firefox with PAC")
        item_firefox.connect("activate", lambda _: self.do_launch_firefox())
        self._menu.append(item_firefox)

        self._menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self._on_quit)
        self._menu.append(item_quit)

        self._menu.show_all()

    def _rebuild_status_item(self, state: ProcessState) -> None:
        self._item_status.set_label(f"SusOps — {state.value}")

    def _on_quit(self, _widget) -> None:
        self.do_quit()
        self._Gtk.main_quit()

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Start the GTK main loop."""
        self.do_poll()
        self.schedule_poll(5)
        self._Gtk.main()


def main() -> None:
    app = SusOpsLinuxTray()
    app.run()
