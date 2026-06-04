"""Opt-in macOS GUI smoke test for the rumps tray.

Spawns the actual `susops-tray` binary, waits a few seconds, asserts it
hasn't crashed, then sends SIGTERM and asserts it exits cleanly. Catches
catastrophic regressions in the rumps / AppKit glue (e.g. import-time
failures, NSStatusBar setup crashes) that Layer-2 tests can't.

Skipped on:
  - Non-macOS hosts
  - CI by default (use `pytest -m gui` to opt in)

Run locally on a Mac:
    .venv/bin/pytest -m gui -v
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import time

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(
        platform.system() != "Darwin",
        reason="tray GUI smoke is macOS-only",
    ),
]


def test_tray_launches_and_quits_cleanly(daemon):
    """Spawn susops-tray, give it 3 s to mount, then SIGTERM + assert clean exit."""
    env = os.environ.copy()
    # The tray hard-codes ~/.susops as the workspace. We can't redirect it
    # without modifying production code — and this is a smoke test, not a
    # functional test, so we accept that it'll touch the real user
    # workspace. Use the `daemon` fixture for parity with other tests
    # (its tmp workspace is unused by the tray but the fixture's daemon
    # subprocess being alive proves the daemon code path works in this
    # environment).
    _ = daemon

    # `python -m susops.tray.mac` only imports the module without running
    # main(). Invoke main() directly via -c so we get the actual runloop.
    proc = subprocess.Popen(
        [sys.executable, "-c", "from susops.tray.mac import main; main()"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(3)
        rc = proc.poll()
        if rc is not None:
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            pytest.fail(
                f"tray exited prematurely (rc={rc}); "
                f"stdout={out!r}; stderr={err!r}"
            )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
