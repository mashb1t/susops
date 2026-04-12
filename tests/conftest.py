"""Pytest configuration and shared fixtures."""
from __future__ import annotations

import os
import signal
import subprocess
from unittest.mock import patch


def _kill_susops_ssh_processes() -> None:
    """Kill ssh processes that were spawned by tests.

    Test-spawned processes are identified by having a ControlPath socket
    inside a pytest temp directory (e.g. /tmp/pytest-of-<user>/...).
    This avoids touching the user's own SSH connections.
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-a", "-x", "ssh"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except subprocess.CalledProcessError:
        return  # no matching processes

    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if "/tmp/pytest" not in line and "/pytest-of-" not in line:
            continue
        pid_str = line.split()[0]
        try:
            pid = int(pid_str)
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass


import pytest


@pytest.fixture(autouse=True, scope="session")
def _suppress_background_threads():
    """Disable reconnect monitor thread and desktop notifications for all tests."""
    with patch("susops.facade._ReconnectMonitor.start"), \
         patch("susops.facade.SusOpsManager._notify"):
        yield


@pytest.fixture(autouse=True, scope="session")
def _cleanup_ssh_after_session():
    """Session-scoped fixture: kill test-spawned ssh processes after the suite."""
    yield
    _kill_susops_ssh_processes()


@pytest.fixture(autouse=True)
def _cleanup_ssh_after_test():
    """Per-test fixture: kill test-spawned ssh processes after each test."""
    yield
    _kill_susops_ssh_processes()
