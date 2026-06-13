from pathlib import Path


def test_resolve_workspace_default(monkeypatch):
    monkeypatch.delenv("SUSOPS_TRAY_WORKSPACE", raising=False)
    from susops.tray.base import _resolve_workspace
    assert _resolve_workspace() == Path.home() / ".susops"


def test_resolve_workspace_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SUSOPS_TRAY_WORKSPACE", str(tmp_path / "ws"))
    from susops.tray.base import _resolve_workspace
    assert _resolve_workspace() == tmp_path / "ws"


def test_resolve_workspace_expands_user(monkeypatch):
    monkeypatch.setenv("SUSOPS_TRAY_WORKSPACE", "~/somewhere")
    from susops.tray.base import _resolve_workspace
    assert _resolve_workspace() == Path.home() / "somewhere"
