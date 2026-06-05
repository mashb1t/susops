"""Pytest configuration and shared fixtures."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

pytest_plugins = ("aiohttp.pytest_plugin",)

# Add scripts/ to sys.path for packaging helper tests
_scripts_dir = str(Path(__file__).parent.parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)


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


import time


@pytest.fixture
def daemon(tmp_path):
    """Spawn a fresh susops-services daemon in tmp_path; tear it down after the test.

    Yields the workspace path. The daemon writes its PID + port files into
    ``<workspace>/pids/``. Stderr is captured so a preflight failure shows
    up if the spawn never completes.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "susops.core.services_daemon",
         "--workspace", str(tmp_path), "--port", "0"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    port_file = tmp_path / "pids" / "susops-services.port"
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
            f"daemon never came up; stderr: {err.decode(errors='replace')!r}"
        )
    try:
        yield tmp_path
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
