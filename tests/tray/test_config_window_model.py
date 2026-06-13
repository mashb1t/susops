"""Headless contract for the v2 (3-column) tray view-model.

All fixtures are duck-typed SimpleNamespaces. pac_hosts and
pac_hosts_disabled are DISJOINT lists (a disabled host lives only in
pac_hosts_disabled), exactly as the facade emits them."""
from types import SimpleNamespace as NS

from susops.tray.config_window_model import (
    Action,
    DetailSpec,
    FormField,
    ListRow,
    NavItem,
    build_connection_detail,
    build_connection_form,
    build_connection_rows,
    build_domain_form,
    build_domain_rows,
    build_fetch_form,
    build_forward_form,
    build_forward_rows,
    build_nav,
    build_share_detail,
    build_share_form,
    build_share_rows,
    filter_rows,
)


# ---- fixtures ----

def _conn(tag="work", enabled=True, **kw):
    return NS(
        tag=tag,
        ssh_host=kw.get("ssh_host", "user@bastion"),
        socks_proxy_port=kw.get("socks_proxy_port", 1080),
        enabled=enabled,
        pac_hosts=kw.get("pac_hosts", ["blabla.de"]),
        pac_hosts_disabled=kw.get("pac_hosts_disabled", ["10.0.0.0/8"]),
        forwards=NS(
            local=kw.get("local", [NS(src_port=5432, src_addr="localhost",
                                      dst_port=5432, dst_addr="db.internal",
                                      tag="postgres", tcp=True, udp=False,
                                      enabled=True)]),
            remote=kw.get("remote", [NS(src_port=8080, src_addr="localhost",
                                        dst_port=8080, dst_addr="localhost",
                                        tag=None, tcp=True, udp=True,
                                        enabled=False)]),
        ),
    )


def _cfg(*conns):
    return NS(connections=list(conns) or [_conn()])


def _status(tag="work", running=True, pid=4711, pending=False):
    return NS(tag=tag, running=running, pid=pid, socks_port=1080,
              enabled=True, pending=pending)


def _share(port=44001, running=True, stopped=False, conn_tag="work",
           file_path="/tmp/file.bin", access_count=3, failed_count=0):
    return NS(file_path=file_path, port=port, running=running,
              stopped=stopped, password="pw", access_count=access_count,
              failed_count=failed_count, conn_tag=conn_tag)


# ---- nav ----

def test_build_nav_categories_counts_and_icons():
    cfg = _cfg(_conn("work"), _conn("home", pac_hosts=["intra.home"],
                                    pac_hosts_disabled=[],
                                    local=[], remote=[]))
    nav = build_nav(cfg, [_share(), _share(port=44002)])
    by_key = {n.key: n for n in nav}
    assert by_key["connections"].count == 2
    assert by_key["connections"].icon == "bolt.horizontal"
    # work has 2 domains (1 enabled + 1 disabled), home has 1 -> 3
    assert by_key["domains"].count == 3
    assert by_key["domains"].icon == "globe"
    # work has 1 local + 1 remote forward, home none -> 2
    assert by_key["forwards"].count == 2
    assert by_key["forwards"].icon == "arrow.left.arrow.right"
    assert by_key["shares"].count == 2
    assert by_key["shares"].icon == "square.and.arrow.up"
    assert by_key["settings"].count is None
    assert by_key["settings"].icon == "gearshape"


def test_build_nav_order_settings_last():
    nav = build_nav(_cfg(), [])
    assert [n.key for n in nav] == [
        "connections", "domains", "forwards", "shares", "settings"]
    assert isinstance(nav[0], NavItem)


# ---- connection rows ----

def test_connection_rows_dot_colors_and_identity():
    cfg = _cfg(_conn("work"), _conn("home"), _conn("off", enabled=False))
    statuses = [_status("work", running=True),
                _status("home", running=False),
                _status("off", running=False)]
    rows = build_connection_rows(cfg, statuses)
    assert all(r.kind == "item" for r in rows)
    assert rows[0].title == "work"
    assert rows[0].subtitle == "user@bastion"
    assert rows[0].badge == ""
    assert rows[0].identity == ("connection", "work")
    assert rows[0].dot == "green"
    assert rows[0].dimmed is False
    assert rows[1].dot == "gray"          # not running
    assert rows[2].dot == "gray"          # disabled
    assert rows[2].dimmed is True         # disabled -> dimmed


def test_connection_row_pending_is_amber():
    cfg = _cfg(_conn("work"))
    rows = build_connection_rows(cfg, [_status("work", running=False,
                                               pending=True)])
    assert rows[0].dot == "amber"


def test_connection_row_pending_running_stays_green():
    # running takes precedence; pending only matters when not running
    cfg = _cfg(_conn("work"))
    rows = build_connection_rows(cfg, [_status("work", running=True,
                                               pending=True)])
    assert rows[0].dot == "green"


# ---- domain rows ----

def test_domain_rows_alphabetical_dim_in_place():
    # Disabled hosts keep their alphabetical position, dimmed in place; they
    # do NOT sink to the bottom. Case-insensitive sort across all connections,
    # tie-break by conn_tag.
    cfg = _cfg(_conn("work", pac_hosts=["zebra.com", "b.de"],
                     pac_hosts_disabled=["alpha.com"]),
               _conn("home", pac_hosts=["middle.lan"], pac_hosts_disabled=[]))
    rows = build_domain_rows(cfg, [_status("work", running=True),
                                   _status("home", running=False)])
    # alpha.com (disabled) < b.de < middle.lan < zebra.com, all alphabetical
    assert [r.title for r in rows] == [
        "alpha.com", "b.de", "middle.lan", "zebra.com"]
    assert [r.badge for r in rows] == ["work", "work", "home", "work"]
    # The disabled host kept its alphabetical position AND is dimmed.
    assert rows[0].title == "alpha.com"
    assert rows[0].dimmed is True
    assert rows[0].dot == "gray"
    assert rows[0].subtitle == ""
    assert rows[0].identity == ("domain", "work", "alpha.com")


def test_domain_rows_case_insensitive_sort_tiebreak_conn_tag():
    # Same host text under two connections sorts case-insensitively, then by
    # conn_tag.
    cfg = _cfg(_conn("work", pac_hosts=["Beta.io"], pac_hosts_disabled=[]),
               _conn("home", pac_hosts=["beta.io", "Alpha.io"],
                     pac_hosts_disabled=[]))
    rows = build_domain_rows(cfg, [_status("work"), _status("home")])
    # Alpha.io < beta.io (home) == Beta.io (work) tie-broken home<work
    assert [(r.title, r.badge) for r in rows] == [
        ("Alpha.io", "home"), ("beta.io", "home"), ("Beta.io", "work")]


def test_domain_rows_dot_and_dim():
    cfg = _cfg(_conn("work", pac_hosts=["a.de"], pac_hosts_disabled=["c.de"]),
               _conn("home", pac_hosts=["x.lan"], pac_hosts_disabled=[]))
    rows = build_domain_rows(cfg, [_status("work", running=True),
                                   _status("home", running=False)])
    by_host = {r.title: r for r in rows}
    # connection running AND host enabled -> green
    assert by_host["a.de"].dot == "green"
    assert by_host["a.de"].dimmed is False
    # connection running but host disabled -> gray + dimmed
    assert by_host["c.de"].dot == "gray"
    assert by_host["c.de"].dimmed is True
    # host enabled but connection not running -> gray, not dimmed
    assert by_host["x.lan"].dot == "gray"
    assert by_host["x.lan"].dimmed is False


# ---- forward rows ----

def test_forward_rows_sections_info_ordering():
    cfg = _cfg(_conn("work"))
    rows = build_forward_rows(cfg, [_status("work", running=True)])
    assert rows[0] == ListRow(kind="section", title="Local")
    assert rows[1].kind == "info"
    assert rows[1].title == "Reach a remote service on a local port"
    assert rows[2].kind == "item"
    assert rows[2].title == "postgres"
    assert rows[2].subtitle == ":5432 → db.internal:5432"
    assert rows[2].badge == "work"
    assert rows[2].identity == ("forward", "work", "local", 5432)
    # Remote section follows all local rows
    assert rows[3] == ListRow(kind="section", title="Remote")
    assert rows[4].kind == "info"
    assert rows[4].title == "Expose a local service on the SSH server"
    assert rows[5].kind == "item"


def test_forward_row_title_falls_back_to_port():
    cfg = _cfg(_conn("work"))
    rows = build_forward_rows(cfg, [_status("work", running=True)])
    remote_item = [r for r in rows if r.kind == "item"][1]
    assert remote_item.title == ":8080"          # remote fw has tag=None


def test_forward_row_dot_and_dim():
    cfg = _cfg(_conn("work"))
    rows = build_forward_rows(cfg, [_status("work", running=True)])
    items = [r for r in rows if r.kind == "item"]
    local, remote = items
    # connection running + fw.enabled -> green
    assert local.dot == "green"
    assert local.dimmed is False
    # connection running but fw disabled -> gray + dimmed
    assert remote.dot == "gray"
    assert remote.dimmed is True


def test_forward_row_gray_when_connection_stopped():
    cfg = _cfg(_conn("work"))
    rows = build_forward_rows(cfg, [_status("work", running=False)])
    items = [r for r in rows if r.kind == "item"]
    assert items[0].dot == "gray"            # enabled fw but conn down
    assert items[0].dimmed is False


def test_forward_rows_all_connections_local_then_remote():
    cfg = _cfg(
        _conn("work",
              local=[NS(src_port=5432, src_addr="localhost", dst_port=5432,
                        dst_addr="db", tag="pg", tcp=True, udp=False,
                        enabled=True)],
              remote=[NS(src_port=8080, src_addr="localhost", dst_port=8080,
                         dst_addr="localhost", tag="web", tcp=True, udp=False,
                         enabled=True)]),
        _conn("home",
              local=[NS(src_port=6000, src_addr="localhost", dst_port=6000,
                        dst_addr="x", tag="six", tcp=True, udp=False,
                        enabled=True)],
              remote=[]),
    )
    rows = build_forward_rows(cfg, [_status("work", running=True),
                                    _status("home", running=True)])
    items_by_section = []
    section = None
    for r in rows:
        if r.kind == "section":
            section = r.title
        elif r.kind == "item":
            items_by_section.append((section, r.title, r.badge))
    assert items_by_section == [
        ("Local", "pg", "work"),
        ("Local", "six", "home"),
        ("Remote", "web", "work"),
    ]


# ---- share rows ----

def test_share_rows_three_state_dots_and_fields():
    cfg = _cfg(_conn("work"))
    shares = [_share(port=44001, running=True),
              _share(port=44002, running=False, stopped=True),
              _share(port=44003, running=False, stopped=False)]
    rows = build_share_rows(cfg, shares, [_status("work")])
    assert rows[0].dot == "green"
    assert rows[1].dot == "gray"             # manual stop
    assert rows[2].dot == "red"              # connection down
    assert rows[0].title == "file.bin"
    assert rows[0].subtitle == "port 44001 · 3 ok"
    assert rows[0].badge == "work"
    assert rows[0].identity == ("share", 44001)


# ---- filter_rows ----

def test_filter_rows_empty_query_unchanged():
    cfg = _cfg(_conn("work"))
    rows = build_forward_rows(cfg, [_status("work")])
    assert filter_rows(rows, "") == rows
    assert filter_rows(rows, "   ") == rows


def test_filter_rows_case_insensitive_match():
    rows = build_connection_rows(_cfg(_conn("work"), _conn("home")),
                                 [_status("work"), _status("home")])
    out = filter_rows(rows, "HOME")
    assert [r.title for r in out] == ["home"]


def test_filter_rows_matches_subtitle_and_badge():
    cfg = _cfg(_conn("work", ssh_host="user@bastion"))
    rows = build_forward_rows(cfg, [_status("work")])
    # subtitle of postgres row contains "db.internal"
    out = filter_rows(rows, "db.internal")
    titles = [r.title for r in out if r.kind == "item"]
    assert titles == ["postgres"]
    # badge match
    out2 = filter_rows(rows, "work")
    assert len([r for r in out2 if r.kind == "item"]) == 2


def test_filter_rows_prunes_sections_without_matches():
    cfg = _cfg(_conn("work"))
    rows = build_forward_rows(cfg, [_status("work")])
    out = filter_rows(rows, "postgres")
    kinds = [(r.kind, r.title) for r in out]
    # Local section + info kept (postgres matched), Remote section dropped
    assert ("section", "Local") in kinds
    assert ("section", "Remote") not in kinds
    assert ("info", "Reach a remote service on a local port") in kinds
    assert ("info", "Expose a local service on the SSH server") not in kinds


def test_filter_rows_no_match_drops_everything():
    cfg = _cfg(_conn("work"))
    rows = build_forward_rows(cfg, [_status("work")])
    assert filter_rows(rows, "zzz-nope") == []


# ---- connection detail ----

def test_connection_detail_running():
    spec = build_connection_detail(_conn("work"), _status("work", running=True))
    assert isinstance(spec, DetailSpec)
    assert spec.title == "work"
    assert spec.status_text == "running · pid 4711"
    assert spec.status_dot == "green"
    assert spec.editable is True
    assert spec.toggle == ("Enabled", True, "conn.toggle")
    assert spec.toggle_note == (
        "Disabled connections are skipped when the proxy starts.")
    # editable text field rows
    by_label = {f.label: f for f in spec.fields}
    assert by_label["Tag"].kind == "text"
    assert by_label["Tag"].value == "work"
    assert by_label["SSH Host"].kind == "combo"
    assert by_label["SSH Host"].value == "user@bastion"
    assert by_label["SOCKS Port"].kind == "text"
    assert by_label["SOCKS Port"].value == "1080"
    by_id = {a.action_id: a for a in spec.actions}
    assert by_id["conn.start"].enabled is False
    assert by_id["conn.stop"].enabled is True
    assert by_id["conn.restart"].enabled is True
    assert by_id["conn.test"].enabled is True
    assert by_id["conn.remove"].enabled is True
    assert by_id["conn.remove"].destructive is True
    assert by_id["conn.remove"].title == "Delete…"
    # Save action present (only enabled-on-dirty handled by the renderer)
    assert "conn.save" in by_id
    # destructive first
    assert spec.actions[0].action_id == "conn.remove"


def test_connection_detail_stopped():
    spec = build_connection_detail(_conn("work"),
                                   _status("work", running=False, pid=None))
    assert spec.status_text == "stopped"
    assert spec.status_dot == "gray"
    by_id = {a.action_id: a for a in spec.actions}
    assert by_id["conn.start"].enabled is True
    assert by_id["conn.stop"].enabled is False
    assert by_id["conn.restart"].enabled is False
    assert by_id["conn.test"].enabled is False
    assert by_id["conn.remove"].enabled is True


def test_connection_detail_pending_amber():
    spec = build_connection_detail(_conn("work"),
                                   _status("work", running=False, pending=True))
    assert spec.status_dot == "amber"


def test_connection_detail_socks_port_fallback():
    conn = NS(tag="work", ssh_host="h", socks_port=2200, enabled=True,
              pac_hosts=[], pac_hosts_disabled=[],
              forwards=NS(local=[], remote=[]))
    spec = build_connection_detail(conn, _status("work", running=False))
    by_label = {f.label: f for f in spec.fields}
    assert by_label["SOCKS Port"].value == "2200"


def test_connection_detail_editable_with_save():
    spec = build_connection_detail(_conn("work"),
                                   _status("work", running=False, pid=None))
    assert spec.editable is True
    by_key = {f.key: f for f in spec.fields}
    assert set(by_key) >= {"tag", "ssh_host", "socks_port"}
    assert by_key["tag"].kind == "text"
    assert by_key["ssh_host"].kind == "combo"
    assert by_key["socks_port"].kind == "text"
    by_id = {a.action_id: a for a in spec.actions}
    assert "conn.save" in by_id
    assert by_id["conn.save"].title == "Save"
    # Auto socks port renders empty (with an "auto" placeholder), not "auto".
    conn0 = NS(tag="work", ssh_host="h", socks_proxy_port=0, enabled=True,
               pac_hosts=[], pac_hosts_disabled=[],
               forwards=NS(local=[], remote=[]))
    spec0 = build_connection_detail(conn0, _status("work", running=False))
    sp = {f.key: f for f in spec0.fields}["socks_port"]
    assert sp.value == ""
    assert sp.placeholder == "auto"


# ---- connection form (create only) ----

def test_connection_form_create_mode():
    spec = build_connection_form(["host.lan", "1.2.3.4", "bastion"])
    assert isinstance(spec, DetailSpec)
    assert spec.editable is True
    assert spec.title == "New Connection"
    assert spec.status_text == ""
    assert spec.status_dot == ""
    assert spec.toggle is None
    by_key = {f.key: f for f in spec.fields}
    assert set(by_key) == {"tag", "ssh_host", "socks_port"}
    assert by_key["tag"].kind == "text"
    assert by_key["tag"].value == ""
    assert by_key["ssh_host"].kind == "combo"
    assert by_key["ssh_host"].options == ["host.lan", "1.2.3.4", "bastion"]
    assert by_key["ssh_host"].value == ""
    assert by_key["socks_port"].kind == "text"
    assert by_key["socks_port"].value == ""
    assert by_key["socks_port"].note == "leave empty for auto"
    assert [a.action_id for a in spec.actions] == ["conn.create"]


def test_connection_form_empty_ssh_hosts():
    spec = build_connection_form([])
    by_key = {f.key: f for f in spec.fields}
    assert by_key["ssh_host"].options == []


# ---- domain form ----

def test_domain_form_edit_mode():
    conn = _conn("work")
    spec = build_domain_form(["work", "home"], host="10.0.0.0/8",
                             conn_tag="work", status=_status("work"),
                             conn=conn)
    assert spec.editable is True
    assert spec.title == "10.0.0.0/8"
    by_key = {f.key: f for f in spec.fields}
    assert by_key["host"].kind == "text"
    assert by_key["host"].value == "10.0.0.0/8"
    assert by_key["conn_tag"].kind == "popup"
    assert by_key["conn_tag"].options == ["work", "home"]
    assert by_key["conn_tag"].value == "work"
    # disabled host -> toggle False, status inactive
    assert spec.status_text == "inactive on work"
    assert spec.toggle == ("Enabled", False, "domain.toggle")
    ids = [a.action_id for a in spec.actions]
    assert ids == ["domain.remove", "domain.test", "domain.save"]
    assert spec.actions[0].destructive is True
    assert spec.actions[0].title == "Delete…"


def test_domain_form_edit_enabled_host_active():
    conn = _conn("work")
    spec = build_domain_form(["work"], host="blabla.de", conn_tag="work",
                             status=_status("work", running=True), conn=conn)
    assert spec.status_text == "active on work"
    assert spec.status_dot == "green"
    assert spec.toggle == ("Enabled", True, "domain.toggle")


def test_domain_form_create_mode():
    spec = build_domain_form(["work", "home"])
    assert spec.editable is True
    assert spec.title == "New Domain / IP / CIDR"
    by_key = {f.key: f for f in spec.fields}
    assert by_key["host"].value == ""
    assert by_key["conn_tag"].value == "work"      # first of conn_tags
    assert spec.toggle is None
    assert [a.action_id for a in spec.actions] == ["domain.create"]


def test_domain_form_create_with_default_conn_tag():
    spec = build_domain_form(["work", "home"], conn_tag="home")
    by_key = {f.key: f for f in spec.fields}
    assert by_key["conn_tag"].value == "home"


# ---- forward form ----

def test_forward_form_edit_mode_fields():
    fw = _conn().forwards.local[0]
    spec = build_forward_form(["work", "home"], fw=fw, direction="local",
                              conn_tag="work",
                              statuses=[_status("work", running=True)])
    assert spec.editable is True
    by_key = {f.key: f for f in spec.fields}
    assert by_key["tag"].kind == "text"
    assert by_key["tag"].value == "postgres"
    assert by_key["conn_tag"].kind == "popup"
    assert by_key["conn_tag"].options == ["work", "home"]
    assert by_key["direction"].kind == "popup"
    assert by_key["direction"].options == ["Local (-L)", "Remote (-R)"]
    assert by_key["direction"].value == "Local (-L)"
    assert by_key["src_addr"].value == "localhost"
    assert by_key["src_port"].value == "5432"
    assert by_key["dst_addr"].value == "db.internal"
    assert by_key["dst_port"].value == "5432"
    # Labels carry no "Source Addr"/"Source Port" text. Paired rows are
    # labeled "Source"/"Destination" by the renderer, the bare addr fields
    # become the row label and the port fields collapse to "Port".
    labels = {f.label for f in spec.fields}
    assert "Source Addr" not in labels and "Source Port" not in labels
    assert "Dest Addr" not in labels and "Dest Port" not in labels
    assert by_key["src_addr"].label == "Source"
    assert by_key["dst_addr"].label == "Destination"
    assert by_key["protocols"].kind == "check_pair"
    assert by_key["protocols"].value == (True, False)
    assert spec.status_text == "active · local forward on work"
    assert spec.status_dot == "green"
    assert spec.toggle == ("Enabled", True, "forward.toggle")
    ids = [a.action_id for a in spec.actions]
    assert ids == ["forward.remove", "forward.test", "forward.save"]
    assert spec.actions[0].title == "Delete…"


def test_forward_form_edit_remote_inactive():
    fw = _conn().forwards.remote[0]
    spec = build_forward_form(["work"], fw=fw, direction="remote",
                              conn_tag="work",
                              statuses=[_status("work", running=True)])
    by_key = {f.key: f for f in spec.fields}
    assert by_key["direction"].value == "Remote (-R)"
    assert by_key["protocols"].value == (True, True)
    # fw disabled -> inactive even though connection running
    assert spec.status_text == "inactive · remote forward on work"
    assert spec.status_dot == "gray"
    assert spec.toggle == ("Enabled", False, "forward.toggle")


def test_forward_form_create_mode():
    spec = build_forward_form(["work", "home"])
    assert spec.title == "New Forward"
    assert spec.editable is True
    by_key = {f.key: f for f in spec.fields}
    assert by_key["tag"].value == ""
    assert by_key["src_addr"].value == "localhost"
    assert by_key["dst_addr"].value == "localhost"
    assert by_key["src_port"].value == ""
    assert by_key["dst_port"].value == ""
    assert by_key["protocols"].value == (True, False)
    assert by_key["conn_tag"].value == "work"
    assert by_key["direction"].value == "Local (-L)"
    assert spec.toggle is None
    assert [a.action_id for a in spec.actions] == ["forward.create"]


# ---- share detail ----

def test_share_detail_running_fields_and_url():
    info = _share(port=44001, running=True, access_count=3, failed_count=1)
    spec = build_share_detail(info, _status("work"), ["work", "home"])
    assert spec.editable is True
    assert spec.title == "file.bin"
    assert spec.status_text == "running"
    assert spec.status_dot == "green"
    # Header Enabled toggle owns serving on/off; ON when not manually stopped.
    assert spec.toggle == ("Enabled", True, "share.toggle")
    by_key = {f.key: f for f in spec.fields}
    assert by_key["file"].kind == "static"
    assert by_key["conn_tag"].kind == "popup"
    assert by_key["conn_tag"].value == "work"
    assert by_key["conn_tag"].options == ["work", "home"]
    assert by_key["url"].kind == "static"
    assert by_key["url"].value == "http://localhost:44001"
    assert by_key["port"].kind == "text"
    assert by_key["port"].value == "44001"
    assert by_key["password"].kind == "secure"
    assert by_key["downloads"].kind == "static"
    assert by_key["downloads"].value == "3 ok · 1 failed"
    ids = [a.action_id for a in spec.actions]
    # The Stop/Start button is gone (header toggle replaces it) and the Copy
    # URL / Copy Password buttons are gone from the action row - they live ONLY
    # as the inline trailing buttons on the URL + password rows.
    assert ids == ["share.delete", "share.save"]
    assert "share.stop" not in ids and "share.start" not in ids
    assert "share.copy_url" not in ids and "share.copy_password" not in ids
    assert spec.actions[0].destructive is True
    # URL row carries a Copy button; password row carries Reveal + Copy.
    assert by_key["url"].trailing == (("share.copy_url", "Copy"),)
    assert by_key["password"].trailing == (("share.reveal", "Reveal"),
                                           ("share.copy_password", "Copy"))


def test_share_detail_stopped_manual():
    spec = build_share_detail(_share(running=False, stopped=True),
                              _status("work"))
    assert spec.status_text == "stopped (manual)"
    assert spec.status_dot == "gray"
    # Manually stopped -> toggle OFF; serving intent is off.
    assert spec.toggle == ("Enabled", False, "share.toggle")
    ids = [a.action_id for a in spec.actions]
    assert "share.start" not in ids
    assert "share.stop" not in ids


def test_share_detail_connection_down():
    spec = build_share_detail(_share(running=False, stopped=False),
                              _status("work", running=False))
    assert spec.status_text == "connection down"
    assert spec.status_dot == "red"
    # Connection down (not manually stopped) -> toggle stays ON (wants to serve).
    assert spec.toggle == ("Enabled", True, "share.toggle")
    ids = [a.action_id for a in spec.actions]
    assert "share.start" not in ids
    assert "share.stop" not in ids


# ---- share create form ----

def test_share_form_create_mode():
    spec = build_share_form(["work", "home"])
    assert spec.title == "Share File"
    assert spec.editable is True
    assert spec.toggle is None
    by_key = {f.key: f for f in spec.fields}
    assert by_key["file"].kind == "path"
    assert by_key["file"].value == ""
    assert by_key["file"].placeholder == "Choose a file to share…"
    assert by_key["conn_tag"].kind == "popup"
    assert by_key["conn_tag"].options == ["work", "home"]
    assert by_key["conn_tag"].value == "work"
    assert by_key["password"].kind == "text"
    assert by_key["password"].placeholder.startswith("optional")
    assert by_key["port"].kind == "text"
    assert by_key["port"].placeholder == "auto"
    assert [a.action_id for a in spec.actions] == ["share.create"]


def test_share_form_preselects_conn_tag():
    spec = build_share_form(["work", "home"], conn_tag="home")
    by_key = {f.key: f for f in spec.fields}
    assert by_key["conn_tag"].value == "home"


# ---- fetch form ----

def test_fetch_form():
    spec = build_fetch_form(["work", "home"])
    assert spec.title == "Fetch File"
    assert spec.editable is True
    assert spec.toggle is None
    by_key = {f.key: f for f in spec.fields}
    assert by_key["conn_tag"].kind == "popup"
    assert by_key["conn_tag"].options == ["work", "home"]
    assert by_key["conn_tag"].value == "work"
    assert by_key["port"].kind == "text"
    assert by_key["password"].kind == "secure"
    assert by_key["output"].kind == "path"
    assert [a.action_id for a in spec.actions] == ["fetch.run"]


# ---- Action / dataclass sanity ----

def test_action_defaults():
    a = Action("x.y", "Title")
    assert a.enabled is True
    assert a.destructive is False


def test_listrow_defaults():
    r = ListRow(kind="item", title="t")
    assert r.subtitle == ""
    assert r.dot == ""
    assert r.badge == ""
    assert r.dimmed is False
    assert r.identity == ()


def test_formfield_defaults():
    f = FormField(key="k", label="L", kind="text")
    assert f.value == ""
    assert f.options == []
    assert f.placeholder == ""
    assert f.note == ""
