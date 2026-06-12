"""Pure-Python view-model builders for the tray config window.

No AppKit imports, testable headlessly, reusable by a future GTK port.
All builders read duck-typed attributes off the facade's pydantic/dataclass
objects (Connection, ConnectionStatus, ShareInfo)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DOT_ON = "●"        # filled circle
DOT_OFF = "○"       # open circle
DOT_DOWN = "◌"      # dotted circle - share whose connection is down
DOT_DISABLED = "–"  # en-dash


@dataclass(frozen=True)
class TabSpec:
    tag: str | None
    title: str
    kind: str              # "connection" | "add" | "gear"


@dataclass(frozen=True)
class SidebarRow:
    kind: str              # "header" | "domain" | "forward" | "share" | "connection"
    label: str
    identity: tuple


@dataclass(frozen=True)
class Action:
    action_id: str
    title: str
    enabled: bool = True
    destructive: bool = False


@dataclass(frozen=True)
class DetailSpec:
    title: str
    rows: list[tuple[str, str]] = field(default_factory=list)
    toggle: tuple | None = None
    actions: list[Action] = field(default_factory=list)


def build_tab_specs(cfg, statuses) -> list[TabSpec]:
    by_tag = {s.tag: s for s in statuses}
    tabs: list[TabSpec] = []
    for conn in cfg.connections:
        st = by_tag.get(conn.tag)
        if not conn.enabled:
            dot = DOT_DISABLED
        elif st is not None and getattr(st, "running", False):
            dot = DOT_ON
        else:
            dot = DOT_OFF
        tabs.append(TabSpec(tag=conn.tag, title=f"{dot} {conn.tag}", kind="connection"))
    tabs.append(TabSpec(tag=None, title="+", kind="add"))
    tabs.append(TabSpec(tag=None, title="⚙", kind="gear"))
    return tabs


def _forward_label(fw, direction: str) -> str:
    dot = DOT_ON if fw.enabled else DOT_OFF
    prefix = "L" if direction == "local" else "R"
    return f"{dot} {prefix} :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}"


def _share_dot(info) -> str:
    if info.running:
        return DOT_ON
    return DOT_OFF if info.stopped else DOT_DOWN


def build_sidebar_rows(conn, shares) -> list[SidebarRow]:
    """Flattened sidebar rows for one connection: group headers + items.
    `shares` must already be filtered to this connection's tag."""
    rows: list[SidebarRow] = [SidebarRow("header", "DOMAINS", ("header", "domains"))]
    disabled = set(getattr(conn, "pac_hosts_disabled", []) or [])
    for host in conn.pac_hosts:
        dot = DOT_OFF if host in disabled else DOT_ON
        rows.append(SidebarRow("domain", f"{dot} {host}", ("domain", host)))

    rows.append(SidebarRow("header", "FORWARDS", ("header", "forwards")))
    for fw in conn.forwards.local:
        rows.append(SidebarRow("forward", _forward_label(fw, "local"),
                               ("forward", "local", fw.src_port)))
    for fw in conn.forwards.remote:
        rows.append(SidebarRow("forward", _forward_label(fw, "remote"),
                               ("forward", "remote", fw.src_port)))

    rows.append(SidebarRow("header", "SHARES", ("header", "shares")))
    for info in shares:
        name = Path(info.file_path).name
        rows.append(SidebarRow("share", f"{_share_dot(info)} {name} ({info.port})",
                               ("share", info.port)))

    rows.append(SidebarRow("header", "CONNECTION", ("header", "connection")))
    rows.append(SidebarRow("connection", "Settings", ("connection",)))
    return rows


def build_connection_detail(conn, status) -> DetailSpec:
    running = bool(status is not None and getattr(status, "running", False))
    pid = getattr(status, "pid", None) if status is not None else None
    if running:
        status_text = f"{DOT_ON} running" + (f" · pid {pid}" if pid else "")
    else:
        status_text = f"{DOT_OFF} stopped"
    socks_port = getattr(conn, "socks_proxy_port", None)
    if socks_port is None:
        socks_port = getattr(conn, "socks_port", None)
    rows = [
        ("Tag", conn.tag),
        ("SSH Host", conn.ssh_host),
        ("SOCKS Port", str(socks_port or "auto")),
        ("Status", status_text),
    ]
    actions = [
        Action("conn.start", "Start", enabled=not running),
        Action("conn.stop", "Stop", enabled=running),
        Action("conn.restart", "Restart", enabled=running),
        Action("conn.test", "Test", enabled=running),
        Action("conn.remove", "Remove Connection…", destructive=True),
    ]
    return DetailSpec(title=conn.tag, rows=rows,
                      toggle=("Enabled", bool(conn.enabled), "conn.toggle"),
                      actions=actions)


def build_domain_detail(conn, host: str) -> DetailSpec:
    disabled = set(getattr(conn, "pac_hosts_disabled", []) or [])
    rows = [("Host", host), ("Connection", conn.tag)]
    actions = [
        Action("domain.test", "Test"),
        Action("domain.remove", "Remove", destructive=True),
    ]
    return DetailSpec(title=host, rows=rows,
                      toggle=("Enabled", host not in disabled, "domain.toggle"),
                      actions=actions)


def build_forward_detail(conn, fw, direction: str) -> DetailSpec:
    protos = [p for p, on in (("TCP", fw.tcp), ("UDP", fw.udp)) if on]
    rows = [
        ("Direction", {"local": f"{direction} (-L)", "remote": f"{direction} (-R)"}[direction]),
        ("Forward", f"{fw.src_addr}:{fw.src_port} → {fw.dst_addr}:{fw.dst_port}"),
        ("Protocols", " + ".join(protos)),
        ("Tag", fw.tag or "—"),
        ("Connection", conn.tag),
    ]
    actions = [
        Action("forward.test", "Test"),
        Action("forward.remove", "Remove", destructive=True),
    ]
    return DetailSpec(title=f":{fw.src_port}", rows=rows,
                      toggle=("Enabled", bool(fw.enabled), "forward.toggle"),
                      actions=actions)


def build_share_detail(info) -> DetailSpec:
    name = Path(info.file_path).name
    if info.running:
        status_text = f"{DOT_ON} running"
    elif info.stopped:
        status_text = f"{DOT_OFF} stopped (manual)"
    else:
        status_text = f"{DOT_DOWN} connection down"
    rows = [
        ("File", str(info.file_path)),
        ("Port", str(info.port)),
        ("Status", status_text),
        ("Downloads", f"{info.access_count} ok · {info.failed_count} failed"),
        ("Connection", info.conn_tag),
    ]
    actions = [Action("share.reveal", "Reveal Password")]
    if info.running:
        actions.append(Action("share.stop", "Stop Share"))
    else:
        actions.append(Action("share.start", "Start Share"))
    actions.append(Action("share.delete", "Delete", destructive=True))
    return DetailSpec(title=name, rows=rows, toggle=None, actions=actions)
