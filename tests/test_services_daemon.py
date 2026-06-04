import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def ws(tmp_path):
    return tmp_path


def _spawn_daemon(ws: Path, port: int = 0) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "susops.core.services_daemon",
         "--workspace", str(ws), "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def test_daemon_writes_pid_file(ws):
    proc = _spawn_daemon(ws)
    try:
        pid_file = ws / "pids" / "susops-services.pid"
        for _ in range(30):
            if pid_file.exists():
                break
            time.sleep(0.1)
        assert pid_file.exists(), "daemon did not write its PID file"
        assert int(pid_file.read_text().strip()) == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_daemon_removes_pid_file_on_sigterm(ws):
    proc = _spawn_daemon(ws)
    try:
        pid_file = ws / "pids" / "susops-services.pid"
        for _ in range(30):
            if pid_file.exists():
                break
            time.sleep(0.1)
        assert pid_file.exists()
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3)
        assert not pid_file.exists(), "daemon did not clean up its PID file on SIGTERM"
    finally:
        if proc.poll() is None:
            proc.kill()
