import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from susops.core.rpc_protocol import InvocationRequest, InvocationResponse


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


def test_daemon_serves_rpc_endpoint(ws):
    proc = _spawn_daemon(ws)
    try:
        port_file = ws / "pids" / "susops-services.port"
        for _ in range(50):
            if port_file.exists():
                break
            time.sleep(0.1)
        assert port_file.exists()
        port = int(port_file.read_text().strip())

        req = InvocationRequest(method="list_config")
        http = urllib.request.Request(
            f"http://127.0.0.1:{port}/rpc",
            data=req.to_json().encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(http, timeout=3) as r:
            body = InvocationResponse.from_json(r.read().decode())
        assert body.ok is True
    finally:
        proc.terminate()
        proc.wait(timeout=3)
