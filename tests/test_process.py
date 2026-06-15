"""Tests for susops.core.process — ProcessManager with PID files."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import psutil
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


def test_is_running_false_for_dead_pid(mgr, tmp_path):
    """A PID file pointing at a non-existent process reads as not running
    and the stale file is cleaned up."""
    (tmp_path / "pids" / "ghost.pid").write_text("999999999")
    assert mgr.is_running("ghost") is False
    assert mgr.get_pid("ghost") is None


def test_force_stop(mgr):
    cmd = [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"]
    mgr.start("unkillable", cmd)
    # SIGKILL should work regardless
    mgr.stop("unkillable", force=True)
    assert not mgr.is_running("unkillable")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork (POSIX)")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_is_running_returns_false_for_zombie(mgr, tmp_path):
    """is_running() must return False for zombie processes and clean up the
    PID file. psutil detects zombie status on macOS and Linux alike."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)  # child exits immediately → becomes zombie

    # Give the OS a moment to transition the child to zombie state
    time.sleep(0.15)

    # Manually register the zombie PID in the process manager
    (tmp_path / "pids" / "zombie.pid").write_text(str(pid))

    result = mgr.is_running("zombie")

    assert result is False
    # PID file must have been cleaned up
    assert mgr.get_pid("zombie") is None

    # Reap in case is_running didn't (should be a no-op if already reaped)
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def test_sigterm_ignored_escalates_to_sigkill(mgr):
    """stop(force=False) must escalate to SIGKILL when SIGTERM is ignored."""
    cmd = [sys.executable, "-c",
           "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]
    mgr.start("stubborn", cmd)
    assert mgr.stop("stubborn") is True  # escalation kills it
    assert not mgr.is_running("stubborn")
    assert mgr.get_pid("stubborn") is None


def test_reused_pid_is_not_signalled(mgr, tmp_path):
    """A PID whose recorded create_time no longer matches the live process
    (PID reuse) must not be signalled — stop() leaves it alive and unlinks."""
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    pid = mgr.start("victim", cmd)
    # Corrupt the recorded create_time so identity no longer matches.
    (tmp_path / "pids" / "victim.pid").write_text(f"{pid}:1.0")

    assert mgr.stop("victim") is False        # refused to signal
    assert psutil.Process(pid).is_running()   # process untouched
    assert mgr.get_pid("victim") is None       # stale file removed

    # is_running must also reject the reused PID.
    (tmp_path / "pids" / "victim.pid").write_text(f"{pid}:1.0")
    assert mgr.is_running("victim") is False

    psutil.Process(pid).kill()  # cleanup


def test_track_existing_records_create_time(mgr, tmp_path):
    """track_existing stamps create_time so an adopted PID is reuse-safe."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        mgr.track_existing("adopted", proc.pid)
        assert mgr.get_pid("adopted") == proc.pid
        assert mgr.is_running("adopted") is True
        # The file carries a create_time (not a bare PID).
        raw = (tmp_path / "pids" / "adopted.pid").read_text()
        assert ":" in raw
    finally:
        proc.kill()
        proc.wait()
