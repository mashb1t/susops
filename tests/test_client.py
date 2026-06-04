import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from susops.client import (
    DaemonUnavailableError,
    SusOpsClient,
    ensure_daemon_running,
)


@pytest.fixture
def ws(tmp_path):
    return tmp_path


@pytest.fixture
def running_daemon(ws):
    proc = subprocess.Popen(
        [sys.executable, "-m", "susops.core.services_daemon",
         "--workspace", str(ws)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    port_file = ws / "pids" / "susops-services.port"
    for _ in range(50):
        if port_file.exists():
            break
        time.sleep(0.1)
    if not port_file.exists():
        proc.terminate()
        pytest.fail("daemon never wrote port file")
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_client_list_config(running_daemon, ws):
    client = SusOpsClient(workspace=ws)
    cfg = client.list_config()
    assert hasattr(cfg, "connections")
    assert cfg.connections == []


def test_client_add_then_remove_connection(running_daemon, ws):
    client = SusOpsClient(workspace=ws)
    conn = client.add_connection("work", "user@host")
    assert conn.tag == "work"
    cfg = client.list_config()
    assert [c.tag for c in cfg.connections] == ["work"]
    client.remove_connection("work")
    cfg = client.list_config()
    assert cfg.connections == []


def test_client_app_config_property(running_daemon, ws):
    client = SusOpsClient(workspace=ws)
    # app_config is a convenience property frontends use directly.
    ac = client.app_config
    assert ac is not None
    # AppConfig has stop_on_quit and ephemeral_ports (bool defaults)
    assert hasattr(ac, "stop_on_quit")


def test_client_raises_on_remote_error(running_daemon, ws):
    client = SusOpsClient(workspace=ws)
    with pytest.raises(ValueError, match="not found"):
        client.remove_connection("nonexistent")


def test_client_blocks_private_method(running_daemon, ws):
    client = SusOpsClient(workspace=ws)
    with pytest.raises(AttributeError):
        client._reload_config()  # noqa: SLF001 — testing the guard


def test_ensure_daemon_running_spawns(ws):
    assert not (ws / "pids" / "susops-services.pid").exists()
    port = ensure_daemon_running(ws)
    assert isinstance(port, int) and port > 0
    assert (ws / "pids" / "susops-services.port").exists()
    # Cleanup: kill spawned daemon
    pid = int((ws / "pids" / "susops-services.pid").read_text())
    os.kill(pid, signal.SIGTERM)
    # Wait for the daemon to exit so its workspace files are cleaned up
    for _ in range(30):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except OSError:
            break


def test_ensure_daemon_running_idempotent(running_daemon, ws):
    # Daemon already running via the fixture
    port_a = ensure_daemon_running(ws)
    port_b = ensure_daemon_running(ws)
    assert port_a == port_b


def test_client_raises_daemon_unavailable_when_dead(ws):
    # No daemon ever started, and we'll bypass ensure_daemon_running by
    # pre-poking a stale port file.
    pids = ws / "pids"
    pids.mkdir()
    (pids / "susops-services.port").write_text("1")  # port 1 isn't bound
    # Pid file is missing, so ensure_daemon_running will spawn — which is
    # NOT what we want for this test. We instead drive _invoke directly by
    # pre-setting client._port. Override the spawn path by writing a fake
    # alive PID:
    fake_pid_path = pids / "susops-services.pid"
    fake_pid_path.write_text(str(os.getpid()))  # liveness check sees current proc

    client = SusOpsClient(workspace=ws)
    # First call should attempt RPC against port 1 (nothing listening) and
    # raise DaemonUnavailableError.
    with pytest.raises(DaemonUnavailableError):
        client.list_config()
