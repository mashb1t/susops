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

import json
import os
import signal
import socket
import subprocess
import sys
import time
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


@pytest.fixture
def tray(daemon):
    """Fresh tray harness wired to the fixture daemon's workspace."""
    return _TestTrayApp(workspace=daemon)


# ---------------------------------------------------------------------------
# Live GUI fixture (opt-in, macOS only)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TrayProc:
    def __init__(self, proc: subprocess.Popen, port: int, workspace: Path):
        self.proc = proc
        self.port = port
        self.workspace = workspace

    def send(self, line: str, timeout: float = 15.0) -> dict:
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as s:
            f = s.makefile("rw", encoding="utf-8")
            f.write(line + "\n")
            f.flush()
            return json.loads(f.readline())


@pytest.fixture
def tray_proc(tmp_path: Path):
    """Spawn a real susops-tray with isolated workspace + debug server."""
    port = _free_port()
    env = os.environ.copy()
    env["SUSOPS_TRAY_WORKSPACE"] = str(tmp_path)
    env["SUSOPS_TRAY_DEBUG_PORT"] = str(port)
    tray_bin = Path(sys.executable).parent / "susops-tray"
    proc = subprocess.Popen(
        [str(tray_bin)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    tp = TrayProc(proc, port, tmp_path)
    deadline = time.time() + 20
    last_err: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"tray died on startup: {proc.stderr.read().decode(errors='replace')!r}")
        try:
            assert tp.send("ping") == {"ok": True}
            break
        except Exception as exc:  # noqa: BLE001 - retry until deadline
            last_err = exc
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail(f"debug server never came up: {last_err!r}")
    yield tp
    try:
        tp.send("quit", timeout=5)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    pid_file = tmp_path / "pids" / "susops-services.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
