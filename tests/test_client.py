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


def test_client_recovers_when_daemon_restarts_between_calls(running_daemon, ws):
    """If the daemon dies between calls (or hasn't quite come up yet), the
    NEXT call should auto-respawn and retry transparently. This is the
    fix for the tray crashing with `DaemonUnavailableError: Daemon
    unreachable: Connection refused` on user clicks.
    """
    client = SusOpsClient(workspace=ws)
    # Warm the client (cache the port + verify alive).
    assert client.list_config() is not None

    # Kill the daemon hard. The fixture's `proc` is still tracked so it
    # gets cleaned up at the end; we send SIGKILL ourselves so the
    # finally block in the daemon can't run cleanup before we test the
    # client's recovery.
    pid = int((ws / "pids" / "susops-services.pid").read_text())
    os.kill(pid, signal.SIGKILL)
    # Wait until it's really gone + pid file is removed (manually here
    # since SIGKILL skips the daemon's finally block).
    for _ in range(50):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except OSError:
            break
    (ws / "pids" / "susops-services.pid").unlink(missing_ok=True)
    (ws / "pids" / "susops-services.port").unlink(missing_ok=True)

    # Next call should NOT raise — the client should respawn the daemon
    # and retry. This is the regression the tray crash exposed.
    cfg = client.list_config()
    assert cfg is not None
    assert hasattr(cfg, "connections")

    # Confirm we have a brand-new daemon.
    new_pid = int((ws / "pids" / "susops-services.pid").read_text())
    assert new_pid != pid, "client retry should have respawned the daemon"

    # Clean up the spawned daemon (the fixture only tracks the original).
    os.kill(new_pid, signal.SIGTERM)
    for _ in range(30):
        try:
            os.kill(new_pid, 0)
            time.sleep(0.1)
        except OSError:
            break


def test_ensure_daemon_running_surfaces_preflight_error(ws):
    """When the daemon spawn fails preflight (PAC port squatted), the
    user should see the daemon's actual error message, not a generic
    'didn't come up within timeout'.
    """
    import socket as _socket
    from susops.core.config import SusOpsConfig, save_config

    # Pre-write a config pointing at a squatted port.
    ws.mkdir(parents=True, exist_ok=True)
    squat = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    squat.bind(("127.0.0.1", 0))
    squat.listen(1)
    squat_port = squat.getsockname()[1]
    try:
        cfg = SusOpsConfig().model_copy(update={"pac_server_port": squat_port})
        save_config(cfg, ws)

        with pytest.raises(DaemonUnavailableError) as excinfo:
            ensure_daemon_running(ws)
        # The surfaced error should mention the actual cause.
        assert "bound by another process" in str(excinfo.value), (
            f"expected preflight error message, got: {excinfo.value}"
        )
    finally:
        squat.close()
