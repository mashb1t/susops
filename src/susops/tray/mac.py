"""macOS tray app — rumps + PyObjC.

Requires: pip install 'susops[tray-mac]'  (rumps>=0.4)
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from susops.core.types import ProcessState
from susops.tray.base import AbstractTrayApp

_ASSETS_DIR = Path(__file__).parent.parent.parent.parent / "assets" / "icons"


def _get_icon_path(state: ProcessState) -> str | None:
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
    candidate = _ASSETS_DIR / subdir / variant / f"{name}.png"
    if candidate.exists():
        return str(candidate)
    # Try SVG fallback
    svg = _ASSETS_DIR / subdir / variant / f"{name}.svg"
    if svg.exists():
        return str(svg)
    return None


class SusOpsMacTray(AbstractTrayApp):
    """macOS system tray application using rumps."""

    def __init__(self) -> None:
        super().__init__()
        import rumps
        self._rumps = rumps

        icon_path = _get_icon_path(ProcessState.STOPPED)
        self._app = rumps.App(
            "SusOps",
            icon=icon_path,
            template=True,
            quit_button=None,
        )
        self._build_menu()

    # ------------------------------------------------------------------ #
    # AbstractTrayApp implementation
    # ------------------------------------------------------------------ #

    def update_icon(self, state: ProcessState) -> None:
        icon_path = _get_icon_path(state)
        if icon_path:
            self._app.icon = icon_path

    def update_menu_sensitivity(self, state: ProcessState) -> None:
        running = state == ProcessState.RUNNING
        stopped = state == ProcessState.STOPPED
        if hasattr(self, "_item_start"):
            self._item_start._menuitem.setEnabled_(not running)  # type: ignore[attr-defined]
        if hasattr(self, "_item_stop"):
            self._item_stop._menuitem.setEnabled_(not stopped)  # type: ignore[attr-defined]
        if hasattr(self, "_item_restart"):
            self._item_restart._menuitem.setEnabled_(not stopped)  # type: ignore[attr-defined]
        if hasattr(self, "_item_status"):
            self._item_status.title = f"SusOps — {state.value}"

    def show_alert(self, title: str, msg: str) -> None:
        self._rumps.alert(title=title, message=msg, ok="OK")

    def show_output_dialog(self, title: str, output: str) -> None:
        self._rumps.alert(title=title, message=output, ok="Close")

    def run_in_background(self, fn: Callable, callback: Callable | None = None) -> None:
        def _worker():
            result = fn()
            if callback is not None:
                self._rumps.rumps._call_as_function_or_bound_method(callback, result)
        threading.Thread(target=_worker, daemon=True).start()

    def schedule_poll(self, interval_seconds: int) -> None:
        @self._rumps.timer(interval_seconds)
        def _poll(_sender):
            self.do_poll()

    # ------------------------------------------------------------------ #
    # Menu building
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> None:
        rumps = self._rumps

        self._item_status = rumps.MenuItem("SusOps — unknown")
        self._item_status._menuitem.setEnabled_(False)

        self._item_start = rumps.MenuItem("▶ Start", callback=lambda _: self.do_start())
        self._item_stop = rumps.MenuItem("■ Stop", callback=lambda _: self.do_stop())
        self._item_restart = rumps.MenuItem("↺ Restart", callback=lambda _: self.do_restart())

        self._app.menu = [
            self._item_status,
            None,  # separator
            self._item_start,
            self._item_stop,
            self._item_restart,
            None,
            rumps.MenuItem("Test connections", callback=lambda _: self.do_test()),
            rumps.MenuItem("Show status", callback=lambda _: self.do_status()),
            None,
            rumps.MenuItem("Launch Chrome with PAC", callback=lambda _: self.do_launch_chrome()),
            rumps.MenuItem("Launch Firefox with PAC", callback=lambda _: self.do_launch_firefox()),
            None,
            rumps.MenuItem("Quit", callback=self._on_quit),
        ]

    def _on_quit(self, _sender) -> None:
        self.do_quit()
        self._rumps.quit_application()

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Start the rumps main loop."""
        self.do_poll()
        self.schedule_poll(5)
        self._app.run()


def main() -> None:
    app = SusOpsMacTray()
    app.run()
