"""Tests for susops.core.process — ProcessManager with PID files."""
from __future__ import annotations

import os
import sys
import time

import pytest

from susops.core.process import ProcessManager


@pytest.fixture
def mgr(tmp_path):
    return ProcessManager(tmp_path)


def test_is_running_unknown(mgr):
    assert mgr.is_running("nonexistent") is False


def test_get_pid_unknown(mgr):
    assert mgr.get_pid("nonexistent") is None


def test_start_and_is_running(mgr):
    # Start a long-running process
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    pid = mgr.start("test-proc", cmd)
    assert pid > 0
    assert mgr.is_running("test-proc")
    assert mgr.get_pid("test-proc") == pid
    # Cleanup
    mgr.stop("test-proc")


def test_stop_removes_pid_file(mgr, tmp_path):
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    mgr.start("test-proc", cmd)
    mgr.stop("test-proc")
    assert not mgr.is_running("test-proc")
    assert mgr.get_pid("test-proc") is None


def test_stop_nonexistent_is_safe(mgr):
    # Should not raise
    mgr.stop("does-not-exist")


def test_status_all(mgr):
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    mgr.start("p1", cmd)
    mgr.start("p2", cmd)
    statuses = mgr.status_all()
    assert statuses["p1"] is True
    assert statuses["p2"] is True
    mgr.stop("p1")
    mgr.stop("p2")


def test_cleanup_stale(mgr, tmp_path):
    """Stale PID file (dead process) should be cleaned up."""
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir(exist_ok=True)
    # Write a PID that definitely doesn't exist
    (pid_dir / "zombie.pid").write_text("999999999")
    mgr.cleanup_stale()
    assert mgr.is_running("zombie") is False


def test_force_stop(mgr):
    cmd = [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"]
    mgr.start("unkillable", cmd)
    # SIGKILL should work regardless
    mgr.stop("unkillable", force=True)
    assert not mgr.is_running("unkillable")
