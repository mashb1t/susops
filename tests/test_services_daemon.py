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


def test_preflight_rejects_when_another_daemon_alive(ws):
    """Two daemons on the same workspace = trouble. The second must refuse."""
    first = _spawn_daemon(ws)
    try:
        # Wait for first to come up
        pid_file = ws / "pids" / "susops-services.pid"
        for _ in range(50):
            if pid_file.exists():
                break
            time.sleep(0.1)
        assert pid_file.exists()

        # Try to start a second one against the same workspace
        second = subprocess.Popen(
            [sys.executable, "-m", "susops.core.services_daemon",
             "--workspace", str(ws), "--port", "0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        second.wait(timeout=5)
        assert second.returncode == 2, (
            f"expected exit code 2 (another daemon alive), got {second.returncode}"
        )
        # Failure reason now lives in the workspace log file, not stderr.
        log_path = ws / "logs" / "susops-services.log"
        log_text = log_path.read_text() if log_path.exists() else ""
        assert "already running" in log_text, (
            f"expected 'already running' in log file; contents:\n{log_text}"
        )
    finally:
        first.terminate()
        first.wait(timeout=3)


def test_preflight_rejects_when_pac_port_squatted(ws):
    """A non-susops process holding the configured PAC port must trigger
    a loud failure (exit 3) rather than the silent half-failure we saw
    in development."""
    import socket

    from susops.core.config import SusOpsConfig, save_config

    # Bind a squatter on an arbitrary high port. Pre-write a config that
    # points the daemon at that port.
    squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squat.bind(("127.0.0.1", 0))  # ephemeral
    squat.listen(1)
    squat_port = squat.getsockname()[1]
    try:
        ws.mkdir(parents=True, exist_ok=True)
        cfg = SusOpsConfig()
        cfg = cfg.model_copy(update={"pac_server_port": squat_port})
        save_config(cfg, ws)

        proc = subprocess.Popen(
            [sys.executable, "-m", "susops.core.services_daemon",
             "--workspace", str(ws), "--port", "0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=5)
        assert proc.returncode == 3, (
            f"expected exit code 3 (PAC port squatted), got {proc.returncode}"
        )
        # Failure reason now lives in the workspace log file, not stderr.
        log_path = ws / "logs" / "susops-services.log"
        log_text = log_path.read_text() if log_path.exists() else ""
        assert "bound by another process" in log_text, (
            f"expected 'bound by another process' in log file; contents:\n{log_text}"
        )
    finally:
        squat.close()


def test_pid_file_claim_is_atomic_under_simultaneous_spawn(ws):
    """Two daemons spawned simultaneously must NOT both succeed.

    Before atomic claim, both would pass preflight (PID file didn't exist
    yet) and then both bind PAC — one wins, the other logs the bind
    failure silently and keeps running, ending up as a zombie daemon
    holding the PID file write race.

    Now PID-file creation uses O_EXCL so exactly one wins.
    """
    # Spawn many in parallel against the same workspace; one should win,
    # the rest should exit with code 2 ("already running").
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "susops.core.services_daemon",
             "--workspace", str(ws), "--port", "0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for _ in range(5)
    ]
    try:
        # Give them all up to 3 s to either win or lose the race.
        results = []
        for p in procs:
            try:
                _, err = p.communicate(timeout=3)
                results.append((p.returncode, err.decode()))
            except subprocess.TimeoutExpired:
                # This one won — it's running the main loop. Don't include
                # in results yet; we'll terminate it below.
                results.append(("running", ""))

        winners = [r for r in results if r[0] == "running"]
        losers = [r for r in results if r[0] != "running"]

        # Exactly one daemon should be alive.
        assert len(winners) == 1, (
            f"expected exactly 1 winner, got {len(winners)}; "
            f"all results: {results}"
        )
        # The rest must have exited with the "another daemon" code.
        for rc, err in losers:
            assert rc == 2, (
                f"loser daemon should exit 2 (another daemon alive), got {rc}; "
                f"stderr: {err!r}"
            )
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()


def test_preflight_accepts_when_stale_pid_belongs_to_non_daemon(ws):
    """Stale PID file pointing at an unrelated live process (PID reuse).

    Reproduces the chaos finding: SIGKILL the daemon, an ssh fork inherits
    the freed PID before a new daemon spawns. The stale PID file now points
    at the impostor — a bare os.kill(pid, 0) probe sees it as alive and the
    new daemon's preflight refuses ("another daemon alive"), wedging the
    user for ~100 s until the impostor exits.

    Fix: preflight now verifies the holder's cmdline contains
    'services_daemon'. An unrelated process (here: pytest) is rejected
    and the stale file is removed so the new daemon can start.
    """
    pids = ws / "pids"
    pids.mkdir()
    # Write our own PID into the daemon's pid file — pytest is alive but
    # is decisively NOT a susops daemon.
    (pids / "susops-services.pid").write_text(str(os.getpid()))

    proc = subprocess.Popen(
        [sys.executable, "-m", "susops.core.services_daemon",
         "--workspace", str(ws), "--port", "0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # New daemon should start successfully despite the stale PID file
        # pointing at our pytest process.
        port_file = pids / "susops-services.port"
        for _ in range(50):
            if port_file.exists():
                break
            time.sleep(0.1)
        assert port_file.exists(), (
            "new daemon failed to start — preflight likely treated the "
            "impostor PID as an existing daemon"
        )
        # PID file should now hold the new daemon's PID, not ours.
        new_pid = int((pids / "susops-services.pid").read_text().strip())
        assert new_pid != os.getpid()
        assert new_pid == proc.pid
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
