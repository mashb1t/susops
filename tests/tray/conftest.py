"""Shared fixtures for headless tray business-logic tests.

_TestTrayApp subclasses AbstractTrayApp directly and replaces every
platform-abstract method with a simple recorder so tests can assert on
what icon updates, alerts, and dialogs were produced — without any
dependency on rumps, AppKit, or GTK.

The fixture bypasses AbstractTrayApp.__init__ to point the underlying
SusOpsClient at the tmp-path daemon spawned by the ``daemon`` fixture
(defined in the root tests/conftest.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from susops.client import SusOpsClient
from susops.core.types import ProcessState
from susops.tray.base import AbstractTrayApp


class _TestTrayApp(AbstractTrayApp):
    """Headless tray app for testing the do_* business logic.

    Records every platform-layer call (icon updates, alerts, dialogs,
    background jobs) without depending on rumps / AppKit / GTK.

    Bypasses AbstractTrayApp.__init__ so we can point the underlying
    SusOpsClient at a fixture workspace.
    """

    def __init__(self, workspace: Path) -> None:
        # Don't call super().__init__() — that constructs a SusOpsClient
        # against the real ~/.susops. Build state manually.
        self.manager = SusOpsClient(workspace=workspace, process_name="susops-tray-test")
        self.state: ProcessState = ProcessState.INITIAL
        self.icon_updates: list[ProcessState] = []
        self.menu_states: list[ProcessState] = []
        self.alerts: list[tuple[str, str]] = []
        self.output_dialogs: list[tuple[str, str]] = []
        self.bg_jobs: list[tuple] = []
        self.poll_intervals: list[int] = []

    # ---- Platform-abstract overrides → recorders ----

    def update_icon(self, state: ProcessState) -> None:
        self.icon_updates.append(state)

    def update_menu_sensitivity(self, state: ProcessState) -> None:
        self.menu_states.append(state)

    def show_alert(self, title: str, msg: str) -> None:
        self.alerts.append((title, msg))

    def show_output_dialog(self, title: str, output: str) -> None:
        self.output_dialogs.append((title, output))

    def run_in_background(self, fn: Callable, callback: Callable | None = None) -> None:
        """Synchronous in tests — call fn() immediately and invoke callback."""
        self.bg_jobs.append((fn, callback))
        result = fn()
        if callback is not None:
            callback(result)

    def schedule_poll(self, interval_seconds: int) -> None:
        self.poll_intervals.append(interval_seconds)


@pytest.fixture
def tray(daemon):
    """Fresh tray harness wired to the fixture daemon's workspace."""
    return _TestTrayApp(workspace=daemon)
