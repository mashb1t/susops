"""Pure-Python view-model builders for the tray config window (v2).

No AppKit imports, testable headlessly, reusable by a future GTK port.
All builders read duck-typed attributes off the facade's pydantic/dataclass
objects (Connection, ConnectionStatus, ShareInfo).

v2 is the 3-column Tailscale-style layout: nav (column 1), global lists
(column 2), detail/editor (column 3). Identities carry the connection tag:
  ("connection", tag)
  ("domain", conn_tag, host)
  ("forward", conn_tag, direction, src_port)
  ("share", port)

Status dots mean RUN STATE only, colored: green=active/running,
amber=pending, gray=stopped/inactive, red=error/connection-down. "Enabled"
is never a dot - disabled rows render dimmed."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DIRECTION_LABELS = {"local": "Local (-L)", "remote": "Remote (-R)"}


@dataclass(frozen=True)
class NavItem:
    key: str               # connections | domains | forwards | shares | settings
    title: str
    icon: str              # SF Symbol name ("" = none)
    count: int | None


@dataclass(frozen=True)
class ListRow:
    kind: str              # "item" | "section" | "info"
    title: str
    subtitle: str = ""
    dot: str = ""          # "" | "green" | "amber" | "gray" | "red"
    badge: str = ""        # connection tag pill ("" = none)
    dimmed: bool = False
    identity: tuple = ()


@dataclass(frozen=True)
class FormField:
    key: str
    label: str
    kind: str              # text|secure|popup|combo|check_pair|static|path
    value: object = ""
    options: list = field(default_factory=list)
    placeholder: str = ""
    note: str = ""         # secondary inline note


@dataclass(frozen=True)
class Action:
    action_id: str
    title: str
    enabled: bool = True
    destructive: bool = False


@dataclass(frozen=True)
class DetailSpec:
    title: str
    status_text: str = ""
    status_dot: str = ""
    toggle: tuple | None = None    # ("Enabled", bool, action_id) top-right
    toggle_note: str = ""          # connection-pane explainer
    fields: list = field(default_factory=list)   # list[FormField]
    actions: list = field(default_factory=list)  # list[Action]
    editable: bool = False         # False = static rows, True = form + Save


# ---- helpers ----

def _running(status) -> bool:
    return bool(status is not None and getattr(status, "running", False))


def _pending(status) -> bool:
    return bool(status is not None and getattr(status, "pending", False))


def _conn_dot(conn, status) -> str:
    if not getattr(conn, "enabled", True):
        return "gray"
    if _running(status):
        return "green"
    if _pending(status):
        return "amber"
    return "gray"


def _status_for(statuses, tag):
    return next((s for s in statuses if getattr(s, "tag", None) == tag), None)


def _socks_port(conn):
    port = getattr(conn, "socks_proxy_port", None)
    if port is None:
        port = getattr(conn, "socks_port", None)
    return port


def _disabled_hosts(conn) -> set:
    return set(getattr(conn, "pac_hosts_disabled", []) or [])


def _forward_title(fw) -> str:
    return fw.tag or f":{fw.src_port}"


def _forward_subtitle(fw) -> str:
    return f":{fw.src_port} → {fw.dst_addr}:{fw.dst_port}"


def _share_dot(info) -> str:
    if info.running:
        return "green"
    return "gray" if info.stopped else "red"


# ---- nav ----

def build_nav(cfg, shares) -> list[NavItem]:
    conns = cfg.connections
    domains = 0
    forwards = 0
    for conn in conns:
        domains += len(conn.pac_hosts) + len(_disabled_hosts(conn))
        forwards += len(conn.forwards.local) + len(conn.forwards.remote)
    return [
        NavItem("connections", "Connections", "bolt.horizontal", len(conns)),
        NavItem("domains", "Domains", "globe", domains),
        NavItem("forwards", "Forwards", "arrow.left.arrow.right", forwards),
        NavItem("shares", "Shares", "square.and.arrow.up", len(shares)),
        NavItem("settings", "Settings", "gearshape", None),
    ]


# ---- column-2 row builders ----

def build_connection_rows(cfg, statuses) -> list[ListRow]:
    rows: list[ListRow] = []
    for conn in cfg.connections:
        st = _status_for(statuses, conn.tag)
        rows.append(ListRow(
            kind="item",
            title=conn.tag,
            subtitle=conn.ssh_host,
            dot=_conn_dot(conn, st),
            badge="",
            dimmed=not getattr(conn, "enabled", True),
            identity=("connection", conn.tag),
        ))
    return rows


def build_domain_rows(cfg, statuses) -> list[ListRow]:
    rows: list[ListRow] = []
    for conn in cfg.connections:
        st = _status_for(statuses, conn.tag)
        running = _running(st)
        # Enabled hosts first, then disabled, both in config order.
        # pac_hosts and pac_hosts_disabled are disjoint lists.
        for host in list(conn.pac_hosts) + list(_disabled_hosts(conn)):
            enabled = host not in _disabled_hosts(conn)
            rows.append(ListRow(
                kind="item",
                title=host,
                subtitle="",
                dot="green" if (running and enabled) else "gray",
                badge=conn.tag,
                dimmed=not enabled,
                identity=("domain", conn.tag, host),
            ))
    return rows


def build_forward_rows(cfg, statuses) -> list[ListRow]:
    rows: list[ListRow] = [
        ListRow(kind="section", title="Local"),
        ListRow(kind="info", title="Reach a remote service on a local port"),
    ]
    rows += _forward_items(cfg, statuses, "local")
    rows.append(ListRow(kind="section", title="Remote"))
    rows.append(ListRow(kind="info",
                        title="Expose a local service on the SSH server"))
    rows += _forward_items(cfg, statuses, "remote")
    return rows


def _forward_items(cfg, statuses, direction) -> list[ListRow]:
    rows: list[ListRow] = []
    for conn in cfg.connections:
        running = _running(_status_for(statuses, conn.tag))
        fws = conn.forwards.local if direction == "local" else conn.forwards.remote
        for fw in fws:
            rows.append(ListRow(
                kind="item",
                title=_forward_title(fw),
                subtitle=_forward_subtitle(fw),
                dot="green" if (running and fw.enabled) else "gray",
                badge=conn.tag,
                dimmed=not fw.enabled,
                identity=("forward", conn.tag, direction, fw.src_port),
            ))
    return rows


def build_share_rows(cfg, shares, statuses) -> list[ListRow]:
    rows: list[ListRow] = []
    for info in shares:
        name = Path(info.file_path).name
        rows.append(ListRow(
            kind="item",
            title=name,
            subtitle=f"port {info.port} · {info.access_count} ok",
            dot=_share_dot(info),
            badge=getattr(info, "conn_tag", "") or "",
            dimmed=False,
            identity=("share", info.port),
        ))
    return rows


# ---- filtering ----

def filter_rows(rows, query) -> list[ListRow]:
    """Case-insensitive substring match over title+subtitle+badge of item
    rows. section/info rows are kept only when at least one item row that
    follows them (before the next section) matched. Empty/whitespace query
    returns rows unchanged."""
    if not query or not query.strip():
        return rows
    q = query.strip().lower()

    def _matches(r: ListRow) -> bool:
        haystack = f"{r.title} {r.subtitle} {r.badge}".lower()
        return q in haystack

    # Group rows into (header_rows, item_rows) segments. A header run is the
    # contiguous section/info rows preceding a block of item rows.
    out: list[ListRow] = []
    pending_headers: list[ListRow] = []
    kept_in_segment = False
    for r in rows:
        if r.kind in ("section", "info"):
            # A new section starts a fresh segment. Headers without
            # surviving items are simply dropped.
            if r.kind == "section":
                pending_headers = [r]
                kept_in_segment = False
            else:
                pending_headers.append(r)
        else:  # item
            if _matches(r):
                if not kept_in_segment and pending_headers:
                    out.extend(pending_headers)
                    pending_headers = []
                    kept_in_segment = True
                out.append(r)
    return out


# ---- detail / form builders ----

def build_connection_detail(conn, status) -> DetailSpec:
    running = _running(status)
    pid = getattr(status, "pid", None) if status is not None else None
    if running:
        status_text = "running" + (f" · pid {pid}" if pid else "")
    elif _pending(status):
        status_text = "pending"
    else:
        status_text = "stopped"
    fields = [
        FormField(key="tag", label="Tag", kind="static", value=conn.tag),
        FormField(key="ssh_host", label="SSH Host", kind="static",
                  value=conn.ssh_host),
        FormField(key="socks_port", label="SOCKS Port", kind="static",
                  value=str(_socks_port(conn) or "auto")),
    ]
    actions = [
        Action("conn.remove", "Remove Connection…", destructive=True),
        Action("conn.test", "Test", enabled=running),
        Action("conn.restart", "Restart", enabled=running),
        Action("conn.stop", "Stop", enabled=running),
        Action("conn.start", "Start", enabled=not running),
    ]
    return DetailSpec(
        title=conn.tag,
        status_text=status_text,
        status_dot=_conn_dot(conn, status),
        toggle=("Enabled", bool(conn.enabled), "conn.toggle"),
        toggle_note="Disabled connections are skipped when the proxy starts.",
        fields=fields,
        actions=actions,
        editable=False,
    )


def build_domain_form(conn_tags, *, conn_tag=None, host=None,
                      status=None, conn=None) -> DetailSpec:
    is_edit = host is not None
    default_tag = conn_tag or (conn_tags[0] if conn_tags else "")
    fields = [
        FormField(key="host", label="Host", kind="text",
                  value=host or "", placeholder="example.com / 10.0.0.0/8"),
        FormField(key="conn_tag", label="Connection", kind="popup",
                  value=default_tag, options=list(conn_tags)),
    ]
    if is_edit:
        enabled = host not in _disabled_hosts(conn) if conn is not None else True
        running = _running(status)
        active = running and enabled
        return DetailSpec(
            title=host,
            status_text=f"active on {conn_tag}" if active
            else f"inactive on {conn_tag}",
            status_dot="green" if active else "gray",
            toggle=("Enabled", enabled, "domain.toggle"),
            fields=fields,
            actions=[
                Action("domain.remove", "Remove…", destructive=True),
                Action("domain.test", "Test"),
                Action("domain.save", "Save"),
            ],
            editable=True,
        )
    return DetailSpec(
        title="New Domain / IP / CIDR",
        fields=fields,
        actions=[Action("domain.create", "Create")],
        toggle=None,
        editable=True,
    )


def build_forward_form(conn_tags, *, fw=None, direction=None,
                       conn_tag=None, statuses=()) -> DetailSpec:
    is_edit = fw is not None
    default_tag = conn_tag or (conn_tags[0] if conn_tags else "")
    dir_value = DIRECTION_LABELS.get(direction or "local", "Local (-L)")
    if is_edit:
        fields = [
            FormField(key="tag", label="Tag", kind="text",
                      value=fw.tag or ""),
            FormField(key="conn_tag", label="Connection", kind="popup",
                      value=default_tag, options=list(conn_tags)),
            FormField(key="direction", label="Direction", kind="popup",
                      value=dir_value,
                      options=["Local (-L)", "Remote (-R)"]),
            FormField(key="src_addr", label="Source", kind="text",
                      value=fw.src_addr),
            FormField(key="src_port", label="Port", kind="text",
                      value=str(fw.src_port)),
            FormField(key="dst_addr", label="Destination", kind="text",
                      value=fw.dst_addr),
            FormField(key="dst_port", label="Port", kind="text",
                      value=str(fw.dst_port)),
            FormField(key="protocols", label="Protocols", kind="check_pair",
                      value=(bool(fw.tcp), bool(fw.udp))),
        ]
        running = _running(_status_for(statuses, conn_tag))
        active = running and fw.enabled
        return DetailSpec(
            title=_forward_title(fw),
            status_text=f"{'active' if active else 'inactive'} · "
                        f"{direction} forward on {conn_tag}",
            status_dot="green" if active else "gray",
            toggle=("Enabled", bool(fw.enabled), "forward.toggle"),
            fields=fields,
            actions=[
                Action("forward.remove", "Remove…", destructive=True),
                Action("forward.test", "Test"),
                Action("forward.save", "Save"),
            ],
            editable=True,
        )
    fields = [
        FormField(key="tag", label="Tag", kind="text", value=""),
        FormField(key="conn_tag", label="Connection", kind="popup",
                  value=default_tag, options=list(conn_tags)),
        FormField(key="direction", label="Direction", kind="popup",
                  value=dir_value, options=["Local (-L)", "Remote (-R)"]),
        FormField(key="src_addr", label="Source", kind="text",
                  value="localhost"),
        FormField(key="src_port", label="Port", kind="text", value=""),
        FormField(key="dst_addr", label="Destination", kind="text",
                  value="localhost"),
        FormField(key="dst_port", label="Port", kind="text", value=""),
        FormField(key="protocols", label="Protocols", kind="check_pair",
                  value=(True, False)),
    ]
    return DetailSpec(
        title="New Forward",
        fields=fields,
        actions=[Action("forward.create", "Create")],
        toggle=None,
        editable=True,
    )


def build_share_detail(info, status) -> DetailSpec:
    name = Path(info.file_path).name
    if info.running:
        status_text = "running"
        status_dot = "green"
    elif info.stopped:
        status_text = "stopped (manual)"
        status_dot = "gray"
    else:
        status_text = "connection down"
        status_dot = "red"
    fields = [
        FormField(key="file", label="File", kind="static",
                  value=str(info.file_path)),
        FormField(key="url", label="URL", kind="static",
                  value=f"http://localhost:{info.port}"),
        FormField(key="port", label="Port", kind="text", value=str(info.port)),
        FormField(key="password", label="Password", kind="secure",
                  value=getattr(info, "password", "") or ""),
        FormField(key="downloads", label="Downloads", kind="static",
                  value=f"{info.access_count} ok · {info.failed_count} failed"),
    ]
    actions = [
        Action("share.delete", "Delete…", destructive=True),
        Action("share.copy_url", "Copy URL"),
        Action("share.copy_password", "Copy Password"),
        Action("share.stop", "Stop Share") if info.running
        else Action("share.start", "Start Share"),
        Action("share.save", "Save"),
    ]
    return DetailSpec(
        title=name,
        status_text=status_text,
        status_dot=status_dot,
        toggle=None,
        fields=fields,
        actions=actions,
        editable=True,
    )


def build_fetch_form(conn_tags) -> DetailSpec:
    default_tag = conn_tags[0] if conn_tags else ""
    fields = [
        FormField(key="conn_tag", label="Connection", kind="popup",
                  value=default_tag, options=list(conn_tags)),
        FormField(key="port", label="Port", kind="text", value=""),
        FormField(key="password", label="Password", kind="secure", value=""),
        FormField(key="output", label="Output", kind="path", value=""),
    ]
    return DetailSpec(
        title="Fetch File",
        fields=fields,
        actions=[Action("fetch.run", "Fetch")],
        toggle=None,
        editable=True,
    )

