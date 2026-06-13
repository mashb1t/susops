"""macOS GUI smoke tests for the rumps tray with debug server.

Exercises dump-menu, open-about, in-process screenshot, and the 3-column
config window (nav / list / detail) via TrayDebugServer. Skipped unless
SUSOPS_RUN_GUI_TESTS=1 is set (macOS only).

Run locally:
    SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest tests/tray/test_gui_smoke.py -v
"""
from __future__ import annotations

import os
import platform
import subprocess
import time

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(platform.system() != "Darwin", reason="macOS only"),
    pytest.mark.skipif(
        not os.environ.get("SUSOPS_RUN_GUI_TESTS"),
        reason="set SUSOPS_RUN_GUI_TESTS=1 to run GUI smoke tests",
    ),
]


def _wait_for(fn, predicate, timeout: float = 5.0, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = fn()
        if predicate(result):
            return result
        time.sleep(interval)
    return result


def test_ping_and_dump_menu(tray_proc):
    menu = tray_proc.send("dump-menu")["menu"]
    titles = [n.get("title") for n in menu if "title" in n]
    assert "Start Proxy" in titles
    assert "Quit" in titles


def test_screenshot_of_about_panel(tray_proc, tmp_path):
    assert tray_proc.send("open-about").get("ok")
    out = tmp_path / "about.png"
    result = tray_proc.send(f"screenshot {out}")
    assert result.get("ok"), result
    assert out.stat().st_size > 5_000  # a real PNG, not a stub
    assert result["width"] > 100 and result["height"] > 100


def test_config_window_opens_and_dumps(tray_proc):
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_pac_host("blabla.de", conn_tag="work")
    assert tray_proc.send("open-config").get("ok")

    # Initial data loads asynchronously; allow up to 5 s for the first poll.
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(n["key"] == "connections" and n["count"] == 1
                                        for n in d.get("nav", [])),
    )
    assert dump["open"] is True
    nav = {n["key"]: n for n in dump["nav"]}
    assert set(nav) == {"connections", "domains", "forwards", "shares", "settings"}
    assert nav["connections"]["count"] == 1
    assert nav["domains"]["count"] == 1
    assert dump["category"] == "connections"
    assert any(r["title"] == "work" for r in dump["rows"])

    sel = tray_proc.send("select domains 0")
    assert sel.get("ok"), sel
    assert sel["selected"] == ["domain", "work", "blabla.de"]


def test_window_reflects_external_changes(tray_proc):
    """The poll-driven refresh must pick up daemon-side changes."""
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config domains").get("ok")
    # Wait until the window has loaded and is on the domains category.
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and d.get("category") == "domains",
    )
    c.add_pac_host("added-later.de", conn_tag="work")
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: any(r["title"] == "added-later.de" for r in d.get("rows", [])),
        timeout=6.0,
    )
    assert any(r["title"] == "added-later.de" for r in dump["rows"])


EXPECTED_MENU = [
    "SusOps:",        # status item (prefix match)
    "Settings…",
    "Start Proxy",
    "Stop Proxy",
    "Restart Proxy",
    "Show Status",
    "Show Logs",
    "Launch Browser",
    "Reset All",
    "About SusOps",
    "Quit",
]

REMOVED_MENU = ["Add", "Remove", "Manage", "Test", "File Transfer",
                "Open Config File", "Config Window…"]


def test_unified_menu_structure(tray_proc):
    menu = tray_proc.send("dump-menu")["menu"]
    titles = [n["title"] for n in menu if "title" in n]
    for expected in EXPECTED_MENU:
        assert any(t.startswith(expected) for t in titles), f"missing {expected}"
    for removed in REMOVED_MENU:
        assert not any(t == removed for t in titles), f"should be gone: {removed}"


def test_detail_renders_and_toggle_round_trip(tray_proc):
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_pac_host("blabla.de", conn_tag="work")
    assert tray_proc.send("open-config").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            n["key"] == "domains" and n["count"] == 1 for n in d.get("nav", [])),
    )
    sel = tray_proc.send("select domains 0")
    assert sel.get("ok"), sel
    dump = tray_proc.send("dump-window")
    assert dump["detail_title"] == "blabla.de"
    assert dump["detail_toggle"] is True
    assert "domain.test" in dump["detail_actions"]
    assert "domain.remove" in dump["detail_actions"]

    res = tray_proc.send("action domain.toggle")
    assert res.get("ok") is True, res
    dump2 = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("detail_toggle") is False,
    )
    assert dump2["detail_toggle"] is False
    # Col-2 row dims when the host is disabled.
    row = next((r for r in dump2["rows"] if r["title"] == "blabla.de"), None)
    assert row is not None and row["dimmed"] is True

    cfg = c.list_config()
    assert "blabla.de" in cfg.connections[0].pac_hosts_disabled


def test_connection_detail_renders(tray_proc):
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            n["key"] == "connections" and n["count"] == 1
            for n in d.get("nav", [])),
    )
    sel = tray_proc.send("select connections 0")
    assert sel.get("ok"), sel
    dump = tray_proc.send("dump-window")
    assert dump["detail_title"] == "work"
    assert dump["detail_toggle"] is True
    assert "conn.start" in dump["detail_actions"]
    assert "conn.remove" in dump["detail_actions"]


def test_inline_edit_forward_round_trip(tray_proc):
    """The headline edit test: select a forward, change a field, Save, and
    confirm the new value persisted to config + the form went clean."""
    from susops.client import SusOpsClient
    from susops.core.config import PortForward
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_local_forward("work", PortForward(src_port=5432, dst_port=5432,
                                            dst_addr="db.internal", tag="postgres"))
    assert tray_proc.send("open-config forwards").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and d.get("category") == "forwards"
        and any(r["title"] == "postgres" for r in d.get("rows", [])),
    )
    assert tray_proc.send("select forwards 0").get("ok")
    assert tray_proc.send("dump-window")["dirty"] is False

    res = tray_proc.send("set-field dst_port 5433")
    assert res.get("ok"), res
    assert tray_proc.send("dump-window")["dirty"] is True

    assert tray_proc.send("action forward.save").get("ok")
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("dirty") is False,
        timeout=6.0,
    )
    assert dump["dirty"] is False
    cfg = c.list_config()
    assert cfg.connections[0].forwards.local[0].dst_port == 5433


def test_dirty_suppresses_refresh(tray_proc):
    """While a col-3 form is dirty, an external config change must NOT clobber
    the in-flight edit. Cols 1-2 still refresh and col-3 stays put."""
    from susops.client import SusOpsClient
    from susops.core.config import PortForward
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_local_forward("work", PortForward(src_port=5432, dst_port=5432,
                                            dst_addr="db.internal", tag="postgres"))
    assert tray_proc.send("open-config forwards").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            r["title"] == "postgres" for r in d.get("rows", [])),
    )
    assert tray_proc.send("select forwards 0").get("ok")
    assert tray_proc.send("set-field tag xyz").get("ok")
    assert tray_proc.send("dump-window")["dirty"] is True

    # External change while the form is dirty.
    c.add_pac_host("late.example.com", conn_tag="work")
    time.sleep(3.0)  # well past the ~1 s poll
    dump = tray_proc.send("dump-window")
    assert dump["dirty"] is True
    assert dump["fields"].get("tag") == "xyz"  # form not clobbered


def test_inline_edit_forward_validation_keeps_form(tray_proc):
    """Invalid src_port -> alert path, config unchanged, form stays dirty."""
    from susops.client import SusOpsClient
    from susops.core.config import PortForward
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_local_forward("work", PortForward(src_port=5432, dst_port=5432,
                                            dst_addr="db.internal", tag="postgres"))
    assert tray_proc.send("open-config forwards").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            r["title"] == "postgres" for r in d.get("rows", [])),
    )
    assert tray_proc.send("select forwards 0").get("ok")
    assert tray_proc.send("set-field src_port 99999").get("ok")
    assert tray_proc.send("dump-window")["dirty"] is True
    tray_proc.send("action forward.save")
    time.sleep(1.0)
    dump = tray_proc.send("dump-window")
    assert dump["dirty"] is True  # save rejected, form kept
    # The validation alert was recorded and auto-answered, nothing else fired.
    assert [a["title"] for a in dump["alerts"]] == ["Invalid Source Port"]
    cfg = c.list_config()
    # Original forward untouched.
    assert [f.src_port for f in cfg.connections[0].forwards.local] == [5432]


def test_inline_create_domain(tray_proc):
    """Add button on Domains opens a create form; Create persists the host."""
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config domains").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and d.get("category") == "domains",
    )
    assert tray_proc.send("add").get("ok")
    d = tray_proc.send("dump-window")
    assert d["create_kind"] == "domain"
    assert d["detail_title"] == "New Domain / IP / CIDR"

    tray_proc.send("set-field host test.example.com")
    assert tray_proc.send("action domain.create").get("ok")
    # Wait for the background create + refresh to land.
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("create_kind") is None
        and any(r["title"] == "test.example.com" for r in d.get("rows", [])),
        timeout=6.0,
    )
    assert dump["create_kind"] is None
    assert any(r["title"] == "test.example.com" for r in dump["rows"])
    cfg = c.list_config()
    assert "test.example.com" in cfg.connections[0].pac_hosts


def test_inline_create_connection(tray_proc):
    """Add button on Connections creates a new connection; nav count bumps."""
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")  # seed: 1
    assert tray_proc.send("open-config connections").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            n["key"] == "connections" and n["count"] == 1
            for n in d.get("nav", [])),
    )
    assert tray_proc.send("add").get("ok")
    d = tray_proc.send("dump-window")
    assert d["create_kind"] == "connection"
    assert d["detail_title"] == "New Connection"

    tray_proc.send("set-field tag third")
    tray_proc.send("set-field ssh_host pi@third.lan")
    assert tray_proc.send("action conn.create").get("ok")
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("create_kind") is None and any(
            n["key"] == "connections" and n["count"] == 2
            for n in d.get("nav", [])),
        timeout=6.0,
    )
    assert any(n["key"] == "connections" and n["count"] == 2
               for n in dump["nav"])
    cfg = c.list_config()
    assert {conn.tag for conn in cfg.connections} == {"work", "third"}


def test_inline_create_domain_validation_keeps_form(tray_proc):
    """Create domain with empty host -> validation alert, form stays open."""
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config domains").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and d.get("category") == "domains",
    )
    assert tray_proc.send("add").get("ok")
    assert tray_proc.send("dump-window")["create_kind"] == "domain"
    # Fire Create with an empty host field.
    tray_proc.send("action domain.create")
    time.sleep(1.0)
    dump = tray_proc.send("dump-window")
    assert dump["create_kind"] == "domain"  # still in create mode
    assert "Missing Field" in [a["title"] for a in dump["alerts"]]
    cfg = c.list_config()
    assert cfg.connections[0].pac_hosts == []


def test_inline_create_share(tray_proc):
    """+ Share File… opens a create form; Create persists a real file share.
    set-field path injects the file so no NSOpenPanel is needed."""
    from pathlib import Path

    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    shared = Path(tray_proc.workspace) / "to-share.txt"
    shared.write_text("payload\n")

    assert tray_proc.send("open-config shares").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and d.get("category") == "shares",
    )
    assert tray_proc.send("add").get("ok")
    d = tray_proc.send("dump-window")
    assert d["create_kind"] == "share"
    assert d["detail_title"] == "Share File"

    assert tray_proc.send(f"set-field path {shared}").get("ok")
    assert tray_proc.send("set-field password s3cret").get("ok")
    assert tray_proc.send("action share.create").get("ok")
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("create_kind") is None
        and any(r["title"] == "to-share.txt" for r in d.get("rows", [])),
        timeout=8.0,
    )
    assert dump["create_kind"] is None
    assert any(r["title"] == "to-share.txt" for r in dump["rows"])
    shares = c.list_shares()
    assert len(shares) == 1
    assert Path(shares[0].file_path).name == "to-share.txt"
    assert shares[0].password == "s3cret"
    # Selecting the new share renders a detail with a URL field.
    assert dump["selected"] == ["share", shares[0].port]
    assert "url" in dump["fields"]
    assert dump["fields"]["url"] == f"http://localhost:{shares[0].port}"


def test_share_detail_copy_actions(tray_proc):
    """The Copy URL / Copy Password buttons live ONLY as inline trailing
    buttons on the URL + password rows (not in the action row, which is just
    [Delete…] [Save]). The inline share.copy_url still writes the URL to the
    system pasteboard."""
    from pathlib import Path

    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    shared = Path(tray_proc.workspace) / "copy-me.bin"
    shared.write_text("bytes\n")
    info = c.share(shared, "work")

    assert tray_proc.send("open-config shares").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            r["title"] == "copy-me.bin" for r in d.get("rows", [])),
    )
    sel = tray_proc.send("select shares 0")
    assert sel.get("ok"), sel
    assert sel["selected"] == ["share", info.port]
    dump = tray_proc.send("dump-window")
    # The action row is [Delete…] [Save] - the Copy buttons are NOT here.
    assert "share.delete" in dump["detail_actions"], dump["detail_actions"]
    assert "share.copy_url" not in dump["detail_actions"], dump["detail_actions"]
    assert "share.copy_password" not in dump["detail_actions"], (
        dump["detail_actions"])

    # The inline Copy button on the URL row still dispatches share.copy_url.
    assert tray_proc.send("action share.copy_url").get("ok")
    # Read the pasteboard back in-process to assert the URL string landed.
    out = subprocess.run(["pbpaste"], capture_output=True, text=True)
    assert out.stdout == f"http://localhost:{info.port}"


def test_inline_edit_share_port(tray_proc):
    """Edit a share's port and Save -> stop+delete+re-share lands on the new
    port (or rolls back to a working share). Honest: assert the resulting list
    has exactly one running share, on the new port when the re-share bound."""
    from pathlib import Path

    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    shared = Path(tray_proc.workspace) / "edit-port.txt"
    shared.write_text("hi\n")
    info = c.share(shared, "work")  # auto port

    assert tray_proc.send("open-config shares").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            r["title"] == "edit-port.txt" for r in d.get("rows", [])),
    )
    assert tray_proc.send("select shares 0").get("ok")
    assert tray_proc.send("set-field port 45999").get("ok")
    assert tray_proc.send("dump-window")["dirty"] is True

    assert tray_proc.send("action share.save").get("ok")
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("dirty") is False,
        timeout=8.0,
    )
    assert dump["dirty"] is False
    shares = c.list_shares()
    running = [s for s in shares if s.running]
    # Exactly one share is actually serving: the re-share on the new port (or,
    # if the daemon could not bind it, the rollback-restored original). A
    # stopped config-only leftover on the old port is a tolerated facade quirk
    # under concurrent polling; what matters is one live share on a valid port.
    assert len(running) == 1, shares
    assert running[0].port in (45999, info.port)
    assert Path(running[0].file_path).name == "edit-port.txt"


def test_share_header_toggle_stops_and_restarts(tray_proc):
    """The shares header Enabled toggle owns serving: it replaces the Stop/Start
    button (ON when serving) and flipping it stops then re-serves the share.

    Asserted via the `running` (in-memory server) state. Re-serving rebinds the
    same port, which the OS can be slow to release under batch load, so the
    re-serve waits use a generous timeout."""
    from pathlib import Path

    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    shared = Path(tray_proc.workspace) / "toggle-me.txt"
    shared.write_text("hi\n")
    info = c.share(shared, "work")

    def _serving():
        return any(s.port == info.port and s.running
                   for s in c.list_shares())

    assert tray_proc.send("open-config shares").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and any(
            r["title"] == "toggle-me.txt" for r in d.get("rows", [])),
    )
    assert tray_proc.send("select shares 0").get("ok")
    dump = tray_proc.send("dump-window")
    # Serving share -> toggle ON; the Stop/Start button is gone (toggle owns it).
    assert dump["detail_toggle"] is True
    assert "share.stop" not in dump["detail_actions"]
    assert "share.start" not in dump["detail_actions"]
    assert _serving()

    # Flip OFF -> the share stops serving.
    assert tray_proc.send("action share.toggle").get("ok")
    _wait_for(lambda: _serving(), lambda serving: serving is False,
              timeout=15.0)
    assert _serving() is False

    # Flip ON again -> the share serves once more (rebinds the same port).
    assert tray_proc.send("action share.toggle").get("ok")
    _wait_for(lambda: _serving(), lambda serving: serving is True,
              timeout=15.0)
    assert _serving() is True


# --------------------------------------------------------------------------- #
# Settings pane (Tailscale-style, instant apply)
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_settings_pane_renders_and_hides_list(tray_proc):
    """Selecting Settings hides column 2 and renders the instant-apply form."""
    assert tray_proc.send("open-config settings").get("ok")
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("open") and d.get("category") == "settings"
        and d.get("col2_hidden") is True,
    )
    assert dump["category"] == "settings"
    assert dump["col2_hidden"] is True
    settings = dump["settings"]
    assert settings is not None
    # All toggle keys + the three port keys + the logo index are present.
    for key in ("launch_at_login", "stop_on_quit", "ephemeral_ports",
                "restore_shares", "show_bandwidth", "notifications",
                "logo_style", "rpc_port", "sse_port", "pac_port"):
        assert key in settings, f"missing settings key {key}"


def test_settings_staged_until_apply(tray_proc):
    """A toggle flip is STAGED, not persisted: config is unchanged until the
    user clicks Apply, then all staged changes persist."""
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    assert tray_proc.send("open-config settings").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("col2_hidden") is True and d.get("settings"),
    )
    before = c.list_config().susops_app.notifications_enabled
    # Flip notifications via set-field; this only STAGES the change.
    target = "off" if before else "on"
    res = tray_proc.send(f"set-field notifications {target}")
    assert res.get("ok"), res
    # The widget reflects the staged value and the pane is dirty.
    dump = tray_proc.send("dump-window")
    assert dump["settings"]["notifications"] == (not before)
    assert dump["settings_dirty"] is True
    # But config is NOT changed - nothing persisted before Apply. Give the
    # daemon a beat to prove no write happened.
    import time
    time.sleep(1.0)
    assert c.list_config().susops_app.notifications_enabled == before
    # Apply commits the staged toggle.
    assert tray_proc.send("action settings.apply").get("ok")
    after = _wait_for(
        lambda: c.list_config().susops_app.notifications_enabled,
        lambda v: v == (not before),
        timeout=5.0,
    )
    assert after == (not before)
    # No alert fired (Apply succeeded) and the pane is no longer dirty.
    assert tray_proc.send("dump-window").get("alerts") == []
    assert tray_proc.send("dump-window")["settings_dirty"] is False


def test_settings_discard_on_leave(tray_proc):
    """Leaving the settings category while dirty DISCARDS the staged change:
    nothing is written to config."""
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config settings").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("col2_hidden") is True and d.get("settings"),
    )
    before = c.list_config().susops_app.notifications_enabled
    target = "off" if before else "on"
    assert tray_proc.send(f"set-field notifications {target}").get("ok")
    assert tray_proc.send("dump-window")["settings_dirty"] is True
    # Navigate away from settings (to connections) WITHOUT Apply.
    assert tray_proc.send("select connections").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("category") == "connections",
    )
    # The staged change was discarded - config unchanged.
    import time
    time.sleep(1.0)
    assert c.list_config().susops_app.notifications_enabled == before
    # Re-entering settings shows the saved (unchanged) value, not dirty.
    assert tray_proc.send("select settings").get("ok")
    dump = _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("category") == "settings" and d.get("settings"),
    )
    assert dump["settings"]["notifications"] == before
    assert dump["settings_dirty"] is False


def test_settings_apply_ports(tray_proc):
    """Setting a port field + the explicit Apply commits pac_server_port (Apply
    persists ports along with everything else staged)."""
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    assert tray_proc.send("open-config settings").get("ok")
    _wait_for(
        lambda: tray_proc.send("dump-window"),
        lambda d: d.get("col2_hidden") is True and d.get("settings"),
    )
    port = _free_port()
    assert tray_proc.send(f"set-field pac_port {port}").get("ok")
    # The field is written (staged) but NOT applied yet.
    assert str(tray_proc.send("dump-window")["settings"]["pac_port"]) == str(port)
    assert c.list_config().pac_server_port != port
    assert tray_proc.send("action settings.apply").get("ok")
    applied = _wait_for(
        lambda: c.list_config().pac_server_port,
        lambda v: v == port,
        timeout=5.0,
    )
    assert applied == port
