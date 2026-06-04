"""Shared fixtures for headless TUI tests.

Design notes:
- pytest-asyncio is NOT installed. All async work runs inside asyncio.run()
  within plain sync test functions. The aiohttp pytest plugin (used by the
  root conftest) provides a `loop` fixture, but mixing it with Textual's
  run_test context manager produces ContextVar teardown errors.
- The TUI app hardcodes ``Path.home() / ".susops"`` in on_mount(), so we
  must patch Path.home (not susops.client._WORKSPACE_DEFAULT).
- The DashboardScreen._start_sse_listener() spawns a background worker
  thread that calls refresh_status from outside the Textual event loop.
  Under tight test timing this can race with a ListView rebuild, causing
  ``NoMatches("No nodes match 'Label' on ListItem()")``. We suppress the
  SSE listener for all TUI tests via monkeypatch.
  TODO: the race is a real production bug — dashboard SSE thread can fire
  refresh_status while ListView items are mid-rebuild. Fix separately.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def tui_workspace(tmp_path, monkeypatch):
    """Spawn a fresh daemon at ``tmp_path/.susops`` and redirect Path.home().

    Yields the workspace path (``tmp_path/.susops``). The daemon is torn
    down after the test. The SSE listener background worker is suppressed
    to prevent thread/ContextVar races.
    """
    susops_dir = tmp_path / ".susops"
    susops_dir.mkdir()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "susops.core.services_daemon",
            "--workspace",
            str(susops_dir),
            "--port",
            "0",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    port_file = susops_dir / "pids" / "susops-services.port"
    for _ in range(50):
        if port_file.exists():
            break
        time.sleep(0.1)
    if not port_file.exists():
        proc.kill()
        try:
            _, err = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            err = b""
        pytest.fail(
            f"TUI daemon never came up; stderr: {err.decode(errors='replace')!r}"
        )

    # Redirect the TUI's hardcoded Path.home() / ".susops" → susops_dir
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Suppress the SSE background listener to avoid ContextVar races
    monkeypatch.setattr(
        "susops.tui.screens.dashboard.DashboardScreen._start_sse_listener",
        lambda self: None,
    )

    try:
        yield susops_dir
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
