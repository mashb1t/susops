"""Smoke tests for the shared daemon pytest fixture."""


def test_daemon_fixture_starts_and_stops(daemon):
    """Daemon fixture spawns a real daemon and tears it down cleanly."""
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=daemon)
    cfg = c.list_config()
    assert cfg.connections == []


def test_daemon_fixture_pid_and_port_files_exist(daemon):
    """Daemon writes its PID + port files in the workspace."""
    pid_file = daemon / "pids" / "susops-services.pid"
    port_file = daemon / "pids" / "susops-services.port"
    assert pid_file.exists()
    assert port_file.exists()
    pid = int(pid_file.read_text().strip())
    port = int(port_file.read_text().strip())
    assert pid > 0
    assert port > 0


def test_daemon_fixture_isolated_per_test_workspace_a(daemon):
    """Two tests get separate workspaces — connections added in one don't leak."""
    from susops.client import SusOpsClient
    SusOpsClient(workspace=daemon).add_connection("alpha", "u@h")
    cfg = SusOpsClient(workspace=daemon).list_config()
    assert any(c.tag == "alpha" for c in cfg.connections)


def test_daemon_fixture_isolated_per_test_workspace_b(daemon):
    """Companion to the previous test — must see empty config."""
    from susops.client import SusOpsClient
    cfg = SusOpsClient(workspace=daemon).list_config()
    assert cfg.connections == [], (
        f"workspace not isolated; saw connections: {cfg.connections}"
    )
