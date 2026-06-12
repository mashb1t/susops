from types import SimpleNamespace as NS

from susops.tray.config_window_model import (
    Action,
    DetailSpec,
    SidebarRow,
    TabSpec,
    build_connection_detail,
    build_domain_detail,
    build_forward_detail,
    build_share_detail,
    build_sidebar_rows,
    build_tab_specs,
)


def _conn(tag="work", enabled=True, **kw):
    return NS(
        tag=tag, ssh_host="user@bastion", socks_proxy_port=1080, enabled=enabled,
        pac_hosts=kw.get("pac_hosts", ["blabla.de", "10.0.0.0/8"]),
        pac_hosts_disabled=kw.get("pac_hosts_disabled", ["10.0.0.0/8"]),
        forwards=NS(
            local=kw.get("local", [NS(src_port=5432, src_addr="localhost",
                                      dst_port=5432, dst_addr="db.internal",
                                      tag="postgres", tcp=True, udp=False, enabled=True)]),
            remote=kw.get("remote", [NS(src_port=8080, src_addr="localhost",
                                        dst_port=8080, dst_addr="localhost",
                                        tag=None, tcp=True, udp=True, enabled=False)]),
        ),
    )


def _status(tag="work", running=True, pid=4711):
    return NS(tag=tag, running=running, pid=pid, socks_port=1080,
              enabled=True, pending=False)


def _share(port=44001, running=True, stopped=False):
    return NS(file_path="/tmp/file.bin", port=port, running=running,
              stopped=stopped, password="pw", access_count=3, failed_count=0,
              conn_tag="work")


# ---- tabs ----

def test_tab_specs_running_dot_and_synthetic_tabs():
    cfg = NS(connections=[_conn("work"), _conn("home", enabled=True)])
    tabs = build_tab_specs(cfg, [_status("work", running=True),
                                 _status("home", running=False)])
    assert tabs[0] == TabSpec(tag="work", title="● work", kind="connection")
    assert tabs[1] == TabSpec(tag="home", title="○ home", kind="connection")
    assert tabs[-2].kind == "add" and tabs[-2].title == "+"
    assert tabs[-1].kind == "gear"


def test_tab_specs_disabled_connection_dash():
    cfg = NS(connections=[_conn("work", enabled=False)])
    tabs = build_tab_specs(cfg, [])
    assert tabs[0].title == "– work"


# ---- sidebar ----

def test_sidebar_rows_groups_and_items():
    rows = build_sidebar_rows(_conn(), [_share()])
    headers = [r.label for r in rows if r.kind == "header"]
    assert headers == ["DOMAINS", "FORWARDS", "SHARES", "CONNECTION"]
    domain_rows = [r for r in rows if r.kind == "domain"]
    assert domain_rows[0].label == "● blabla.de"
    assert domain_rows[1].label == "○ 10.0.0.0/8"
    assert domain_rows[0].identity == ("domain", "blabla.de")
    fwd_rows = [r for r in rows if r.kind == "forward"]
    assert fwd_rows[0].label == "● L :5432→db.internal:5432"
    assert fwd_rows[1].label == "○ R :8080→localhost:8080"
    assert fwd_rows[0].identity == ("forward", "local", 5432)
    share_rows = [r for r in rows if r.kind == "share"]
    assert share_rows[0].label == "● file.bin (44001)"
    assert share_rows[0].identity == ("share", 44001)
    assert rows[-1] == SidebarRow(kind="connection", label="Settings",
                                  identity=("connection",))


def test_sidebar_share_three_state_dots():
    running = build_sidebar_rows(_conn(), [_share(running=True)])
    stopped = build_sidebar_rows(_conn(), [_share(running=False, stopped=True)])
    down = build_sidebar_rows(_conn(), [_share(running=False, stopped=False)])
    get = lambda rows: [r for r in rows if r.kind == "share"][0].label[0]
    assert get(running) == "●"
    assert get(stopped) == "○"
    assert get(down) == "◌"


# ---- details ----

def test_connection_detail_running():
    spec = build_connection_detail(_conn(), _status(running=True))
    assert spec.title == "work"
    assert ("SSH Host", "user@bastion") in spec.rows
    assert ("Status", "● running · pid 4711") in spec.rows
    assert spec.toggle == ("Enabled", True, "conn.toggle")
    by_id = {a.action_id: a for a in spec.actions}
    assert by_id["conn.start"].enabled is False
    assert by_id["conn.stop"].enabled is True
    assert by_id["conn.test"].enabled is True
    assert by_id["conn.remove"].destructive is True


def test_connection_detail_stopped():
    spec = build_connection_detail(_conn(), _status(running=False, pid=None))
    by_id = {a.action_id: a for a in spec.actions}
    assert by_id["conn.start"].enabled is True
    assert by_id["conn.stop"].enabled is False
    assert by_id["conn.test"].enabled is False


def test_domain_detail():
    spec = build_domain_detail(_conn(), "10.0.0.0/8")
    assert spec.title == "10.0.0.0/8"
    assert ("Connection", "work") in spec.rows
    assert spec.toggle == ("Enabled", False, "domain.toggle")
    assert {a.action_id for a in spec.actions} == {"domain.test", "domain.remove"}


def test_forward_detail():
    fw = _conn().forwards.remote[0]
    spec = build_forward_detail(_conn(), fw, "remote")
    assert ("Direction", "remote (-R)") in spec.rows
    assert ("Forward", "localhost:8080 → localhost:8080") in spec.rows
    assert ("Protocols", "TCP + UDP") in spec.rows
    assert spec.toggle == ("Enabled", False, "forward.toggle")
    assert {a.action_id for a in spec.actions} == {"forward.test", "forward.remove"}


def test_forward_detail_local_direction():
    fw = _conn().forwards.local[0]
    spec = build_forward_detail(_conn(), fw, "local")
    assert ("Direction", "local (-L)") in spec.rows
    assert ("Forward", "localhost:5432 → db.internal:5432") in spec.rows
    assert ("Protocols", "TCP") in spec.rows


def test_share_detail_running():
    spec = build_share_detail(_share())
    assert spec.title == "file.bin"
    assert ("Port", "44001") in spec.rows
    assert ("Downloads", "3 ok · 0 failed") in spec.rows
    by_id = {a.action_id: a for a in spec.actions}
    assert "share.stop" in by_id and "share.delete" in by_id
    assert "share.reveal" in by_id


def test_share_detail_stopped_offers_restart():
    spec = build_share_detail(_share(running=False, stopped=True))
    assert "share.start" in {a.action_id for a in spec.actions}
