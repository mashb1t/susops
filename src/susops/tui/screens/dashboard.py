"""Dashboard screen — lazydocker+nvtop-inspired split-pane live overview."""
from __future__ import annotations

from collections import deque
from pathlib import Path

from rich.markup import escape as markup_escape
from rich.text import Text as RichText

from susops.core.log_style import style_log_line
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Footer,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual_plotext import PlotextPlot

import susops
from susops.core.types import StatusResult
from susops.tui.screens import fmt_bps, fmt_bytes, open_in_explorer, open_path, proto_label, share_name_markup, status_dot, share_status_dot


_fmt_bps = fmt_bps


# Map log_style labels to Rich style strings. None means no styling.
_LOG_STYLE_RICH: dict[str | None, str | None] = {
    None: None,
    "tag": "bold cyan",
    "ok": "green",
    "warn": "yellow",
    "err": "bold red",
    "dim": "dim",
    "info": "blue",
}


def _format_log_line(line: str) -> RichText:
    """Render a raw log line as a colored Rich Text object."""
    text = RichText(no_wrap=False)
    for chunk, label in style_log_line(line):
        style = _LOG_STYLE_RICH.get(label)
        text.append(chunk, style=style)
    return text


def _scale_data(data: list[float]) -> tuple[list[float], str]:
    """Scale bandwidth data to a human-readable unit. Returns (scaled_data, unit_label)."""
    peak = max(data) if data else 0.0
    if peak >= 1_048_576:
        return [v / 1_048_576 for v in data], "MB/s"
    if peak >= 1024:
        return [v / 1024 for v in data], "kB/s"
    return list(data), "B/s"


def _yticks(max_val: float, unit: str, n: int = 6) -> tuple[list[float], list[str]]:
    """Generate n+1 evenly-spaced Y-axis ticks from 0 to max_val with unit labels."""
    step = max_val / n
    ticks = [i * step for i in range(n + 1)]
    labels = [f"{v:.2f} {unit}" for v in ticks]
    return ticks, labels


_fmt_bytes = fmt_bytes


def _fmt_uptime(seconds: float) -> str:
    """Format elapsed seconds as 'Xh Ym', 'Xm', or 'Xs'."""
    if seconds >= 3600:
        h = int(seconds / 3600)
        m = int((seconds % 3600) / 60)
        return f"{h}h {m}m"
    if seconds >= 60:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds)}s"


def _fmt_bw_line(
    tag: str,
    running: bool,
    rx: float,
    tx: float,
    rx_t: float,
    tx_t: float,
    tag_width: int = 25,
    show_dot: bool = True,
    enabled: bool = True,
) -> str:
    dot = status_dot(running, enabled)
    prefix = f"  {dot} " if show_dot else "    "
    return (
        f"{prefix}{tag:<{tag_width}}  "
        f"{_fmt_bps(rx):>7} [cyan]{_fmt_bytes(rx_t):>7}[/cyan]  "
        f"{_fmt_bps(tx):>7} [cyan]{_fmt_bytes(tx_t):>7}[/cyan]"
    )


def _fmt_domain_line(host: str, prefix: str = "") -> str:
    pre = f"[dim]{prefix}[/dim] " if prefix else ""
    return f"{pre}[link='http://{host}']{host}[/link]"

def _fmt_forward_local(fw, prefix: str = "") -> str:
    pre = f"[dim]{prefix}[/dim] " if prefix else ""
    label = f"\n  [dim]{fw.tag}[/dim]" if fw.tag else ""
    proto = f" [dim]{proto_label(fw)}[/dim]"
    return f"{pre}[green]L[/green] {fw.src_addr}:{fw.src_port} [green]→[/green] {fw.dst_addr}:{fw.dst_port}{proto}{label}"


def _fmt_forward_remote(fw, prefix: str = "") -> str:
    pre = f"[dim]{prefix}[/dim] " if prefix else ""
    label = f"\n  [dim]{fw.tag}[/dim]" if fw.tag else ""
    proto = f" [dim]{proto_label(fw)}[/dim]"
    return f"{pre}[yellow]R[/yellow] {fw.src_addr}:{fw.src_port} [yellow]←[/yellow] {fw.dst_addr}:{fw.dst_port}{proto}{label}"


def _fmt_share_line(info, prefix: str = "") -> str:
    pre = f"[dim]{prefix}[/dim] " if prefix else ""
    dot = share_status_dot(info.running, info.stopped)
    name = Path(info.file_path).name
    link = share_name_markup(info.file_path, name)
    if info.running:
        return f"{pre}{dot} {link}  {info.port}"
    return f"{pre}{dot} [dim]{link}[/dim]"


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("r", "restart", "Restart"),

        Binding("c", "push_screen('connections')", "Connections"),
        Binding("f", "push_screen('share')", "Shares"),
        Binding("b", "launch_browser", "Browser"),
        Binding("e", "edit_config", "Edit config"),
    ]

    DEFAULT_CSS = """
    DashboardScreen { layout: vertical; }
    #main-split { height: 1fr; }
    #conn-panel { width: 49; background: $surface-darken-1; border-right: solid $primary-darken-2; }
    #conn-list  { height: 1fr; border: round $primary-darken-1; margin: 1 1 0 1; border-title-align: left; }
    #services-info  { height: auto; padding: 0 1; border: round $primary-darken-1; margin: 1; border-title-align: left; }
    #detail-panel { width: 1fr; }
    #detail-tabs  { height: 1fr; }
    #stats-content { height: auto; padding: 1 2; }
    #bw-container  { height: 1fr; min-height: 8; }
    #rx-chart { height: 1fr; width: 1fr; border: round $primary-darken-1; margin: 0 1 1 1; }
    #tx-chart { height: 1fr; width: 1fr; border: round $primary-darken-1; margin: 0 1 1 0; }
    #detail-logs { height: 1fr; margin: 1; border: round $primary-darken-1; }
    #config-tab-area { height: 1fr; margin: 1; border: round $primary-darken-1; border-title-align: left; }
    #pac-tab-area { height: 1fr; margin: 1; border: round $primary-darken-1; border-title-align: left; }
    #context-panel { width: 49; background: $surface-darken-1; border-left: solid $primary-darken-2; }
    #domain-section { height: 1fr; border: round $primary-darken-1; margin: 1 1 0 1; border-title-align: left; }
    #domain-content { padding: 0 1; }
    #forward-content { height: auto; padding: 0 1; border: round $primary-darken-1; margin: 1; border-title-align: left; }
    #share-content { height: auto; padding: 0 1; border: round $primary-darken-1; margin: 0 1 1 1; border-title-align: left; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._conn_tags: list[str] = []
        self._selected_tag: str | None = None
        self._conn_data: dict = {}
        self._rx_history: dict = {}
        self._tx_history: dict = {}
        self._idle_ticks: int = 0  # ticks since last active connection
        self._sse_active: bool = True
        self._last_config = None
        self._last_shares: list = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-split"):
            # Left: connection list + unified services panel (Daemon/PAC/Reconnect)
            with Vertical(id="conn-panel"):
                yield ListView(id="conn-list")
                yield Static(id="services-info", markup=True)
            # Centre: stats + bandwidth + logs
            with Vertical(id="detail-panel"):
                with TabbedContent(id="detail-tabs"):
                    with TabPane("Stats", id="tab-stats"):
                        yield Static("", id="stats-content")
                        with Horizontal(id="bw-container"):
                            yield PlotextPlot(id="rx-chart")
                            yield PlotextPlot(id="tx-chart")
                    with TabPane("Logs", id="tab-logs"):
                        yield RichLog(id="detail-logs", highlight=True, markup=True)
                    with TabPane("Config", id="tab-config"):
                        area = TextArea(language="yaml", id="config-tab-area", read_only=True)
                        area.border_title = "config.yaml"
                        yield area
                    with TabPane("PAC", id="tab-pac"):
                        area = TextArea(language="javascript", id="pac-tab-area", read_only=True)
                        area.border_title = "susops.pac"
                        yield area
            # Right: context panel (domains / forwards / shares)
            with Vertical(id="context-panel"):
                with VerticalScroll(id="domain-section"):
                    yield Static("", id="domain-content", markup=True)
                yield Static("", id="forward-content", markup=True)
                yield Static("", id="share-content", markup=True)
        with Horizontal(classes="footer-row"):
            yield Footer()
            yield Static(f"[@click=app.open_github()]v{susops.__version__}[/]", classes="footer-version", markup=True)

    def on_mount(self) -> None:
        self.query_one("#conn-list", ListView).border_title = "Connections"
        self.query_one("#services-info", Static).border_title = "Services"
        self.query_one("#domain-section", VerticalScroll).border_title = "Domain / IP / CIDR"
        self.query_one("#forward-content", Static).border_title = "Forwards"
        self.query_one("#share-content", Static).border_title = "Shares"
        mgr = self.app.manager  # type: ignore[attr-defined]
        self._prev_on_log = mgr.on_log
        mgr.on_log = self._on_new_log
        self._prev_on_error = mgr.on_error
        mgr.on_error = self._on_new_error
        self.set_interval(2.0, self._tick_refresh)
        self.refresh_status()
        self._start_sse_listener()

    def on_screen_resume(self) -> None:
        """Refresh immediately when returning to dashboard from another screen."""
        self.refresh_status()
        tabs = self.query_one("#detail-tabs", TabbedContent)
        if tabs.active == "tab-pac":
            self._load_pac_tab()
        elif tabs.active == "tab-config":
            self._load_config_tab()

    def on_unmount(self) -> None:
        self._sse_active = False
        mgr = self.app.manager  # type: ignore[attr-defined]
        mgr.on_log = self._prev_on_log
        mgr.on_error = self._prev_on_error

    def _tick_refresh(self) -> None:
        """Adaptive refresh: every 2s when connections are active, every 10s when idle."""
        has_active = any(d["cs"].running for d in self._conn_data.values())
        if not has_active:
            self._idle_ticks += 1
            if self._idle_ticks < 5:  # skip 4 ticks → refresh every 10s when idle
                return
            self._idle_ticks = 0
        else:
            self._idle_ticks = 0
        self.refresh_status()

    def _on_new_log(self, msg: str) -> None:
        # Log buffer stores raw text; render via the shared styler so the
        # connection-tag prefix, success/warn/error keywords, and PIDs each
        # get a consistent colour across all frontends.
        try:
            self.app.call_from_thread(
                self.query_one("#detail-logs", RichLog).write,
                _format_log_line(msg),
            )
        except Exception:
            pass

    def _on_new_error(self, msg: str) -> None:
        try:
            self.app.call_from_thread(
                self.notify,
                msg,
                severity="error",
                timeout=6,
            )
        except Exception:
            pass

    @work(thread=True)
    def refresh_status(self) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            result: StatusResult = mgr.status()

            extras: dict[str, dict] = {}
            bw: dict[str, tuple[float, float]] = {}
            bw_totals: dict[str, tuple[float, float]] = {}
            uptimes: dict[str, float | None] = {}
            for cs in result.connection_statuses:
                extras[cs.tag] = mgr.get_process_info(cs.tag)
                bw[cs.tag] = mgr.get_bandwidth(cs.tag)
                bw_totals[cs.tag] = mgr.get_bandwidth_totals(cs.tag)
                uptimes[cs.tag] = mgr.get_uptime(cs.tag)

            shares = mgr.list_shares()
            config = mgr.list_config()
            reconnect = mgr.reconnect_monitor_info()
        except Exception as exc:
            # Any RPC failure / YAML parse error / unexpected exception
            # raised here becomes a Textual WorkerFailed that crashes the
            # TUI. Surface it as a notification toast and stay on the
            # current screen; the next 2-second tick will retry.
            self.app.call_from_thread(
                self.notify,
                f"Refresh failed: {exc}",
                severity="error",
                timeout=6,
            )
            return
        self.app.call_from_thread(
            self._apply_status, result, extras, bw, bw_totals, uptimes, shares, config, reconnect
        )

    def _apply_status(
            self,
            result: StatusResult,
            extras: dict,
            bw: dict,
            bw_totals: dict,
            uptimes: dict,
            shares: list,
            config,
            reconnect: dict,
    ) -> None:
        self._last_config = config
        self._last_shares = shares

        # Build conn_data with forwards from config
        conn_map = {c.tag: c for c in config.connections}
        new_conn_data: dict = {}
        for cs in result.connection_statuses:
            conn = conn_map.get(cs.tag)
            if conn:
                forwards_local = list(conn.forwards.local)
                forwards_remote = list(conn.forwards.remote)
            else:
                forwards_local = []
                forwards_remote = []
            new_conn_data[cs.tag] = {
                "cs": cs,
                "proc_info": extras.get(cs.tag) or {},
                "bw": bw.get(cs.tag, (0.0, 0.0)),
                "bw_total": bw_totals.get(cs.tag, (0.0, 0.0)),
                "uptime": uptimes.get(cs.tag),
                "forwards_local": forwards_local,
                "forwards_remote": forwards_remote,
                "conn": conn,
            }
        self._conn_data = new_conn_data

        # Update rolling bandwidth history (60 samples)
        for cs in result.connection_statuses:
            tag = cs.tag
            rx, tx = bw.get(tag, (0.0, 0.0))
            if tag not in self._rx_history:
                self._rx_history[tag] = deque([0.0] * 60, maxlen=60)
                self._tx_history[tag] = deque([0.0] * 60, maxlen=60)
            self._rx_history[tag].append(rx)
            self._tx_history[tag].append(tx)

        # Remove history for tags that no longer exist
        current_tags = {cs.tag for cs in result.connection_statuses}
        for tag in list(self._rx_history):
            if tag not in current_tags:
                del self._rx_history[tag]
                del self._tx_history[tag]

        # Update connection list — update labels in-place when possible to
        # preserve the visual selection highlight; only rebuild if tags changed.
        conn_list = self.query_one("#conn-list", ListView)
        new_tags = [cs.tag for cs in result.connection_statuses]

        label_texts: list[str] = []
        for cs in result.connection_statuses:
            dot = status_dot(cs.running, cs.enabled)
            port_str = str(cs.socks_port) if cs.socks_port else "auto"
            rx, _tx = bw.get(cs.tag, (0.0, 0.0))
            label_texts.append(f"{dot} {cs.tag:<25} {port_str:<5} {_fmt_bps(rx):>7}↓")

        if new_tags == self._conn_tags:
            # Same connections — update connection rows in-place (index 0 is the All row, skip it)
            items = list(conn_list.query(ListItem))
            for item, text in zip(items[1:], label_texts):
                item.query_one(Label).update(text)
        else:
            # Tags changed — rebuild list, preserve whether All or a connection was selected
            prev_index = conn_list.index if conn_list.index is not None else 0
            conn_list.clear()
            conn_list.append(ListItem(Label("[dim]All[/dim]")))
            for text in label_texts:
                conn_list.append(ListItem(Label(text)))
            self._conn_tags = new_tags
            # Restore selection: clamp to valid range (All row = 0, connections = 1..N)
            conn_list.index = min(prev_index, len(self._conn_tags))

        # Derive _selected_tag from current list position
        idx = conn_list.index if conn_list.index is not None else 0
        if idx == 0 or not self._conn_tags:
            self._selected_tag = None  # All
        else:
            self._selected_tag = self._conn_tags[min(idx - 1, len(self._conn_tags) - 1)]

        # Unified services panel: Daemon (RPC) + PAC + Reconnect, one row each
        # with a "Label" prefix so the meaning is obvious. Pulls daemon info
        # from the local port/pid files written by services_daemon.py.
        mgr = self.app.manager  # type: ignore[attr-defined]
        workspace = mgr.workspace
        # Use `localhost:` consistently — matches what users see in PAC URLs
        # and browser proxy config. (127.0.0.1 is what aiohttp binds to, but
        # the display is for humans.) Label column padded to 10 chars so
        # "Daemon RPC" / "Daemon SSE" / "PAC" / "Reconnect" all align.
        try:
            rpc_port = int((workspace / "pids" / "susops-services.port").read_text().strip())
            rpc_line = (
                f"[green]●[/green] [bold]Daemon RPC[/bold] "
                f"[link='http://localhost:{rpc_port}/rpc']localhost:{rpc_port}/rpc[/link]"
            )
        except (OSError, ValueError):
            rpc_line = "[dim]○ [bold]Daemon RPC[/bold] not running[/dim]"

        # SSE port is held by mgr._status_server, exposed via get_status_url().
        try:
            sse_url = mgr.get_status_url() or ""
        except Exception:
            sse_url = ""
        if sse_url:
            # Strip scheme + path for the compact display form, mirroring
            # the RPC row: "localhost:<port>/events". get_status_url() returns
            # e.g. "http://127.0.0.1:9999/events".
            try:
                from urllib.parse import urlparse
                parsed = urlparse(sse_url)
                sse_display = f"localhost:{parsed.port}{parsed.path}"
            except Exception:
                sse_display = sse_url
            sse_line = (
                f"[green]●[/green] [bold]Daemon SSE[/bold] "
                f"[link='{sse_url}']{sse_display}[/link]"
            )
        else:
            sse_line = "[dim]○ [bold]Daemon SSE[/bold] stopped[/dim]"

        if result.pac_running and result.pac_port:
            # Compact form matching the Daemon rows (host:port). Modern
            # terminals auto-detect URLs as Cmd-clickable; the single-quoted
            # `[link='URL']` syntax is required so Textual's markup parser
            # accepts the `:` in the URL value.
            pac_line = (
                f"[green]●[/green] [bold]PAC[/bold]        "
                f"[link='http://localhost:{result.pac_port}/susops.pac']"
                f"localhost:{result.pac_port}/susops.pac[/link]"
            )
        else:
            pac_line = "[dim]○ [bold]PAC[/bold]        stopped[/dim]"

        # Reconnect monitor — "watching nothing" is functionally stopped from
        # the user's POV (no connections to reconnect), so collapse the
        # "thread alive but idle" state into the stopped one.
        if reconnect["thread_alive"] and reconnect["watching"]:
            n = len(reconnect["watching"])
            reconnect_line = (
                f"[green]●[/green] [bold]Reconnect[/bold]  "
                f"watching {n} connection{'s' if n != 1 else ''}"
            )
        elif reconnect["daemon_running"] and not reconnect["thread_alive"]:
            reconnect_line = "[yellow]●[/yellow] [bold]Reconnect[/bold]  daemon (bg)"
        else:
            reconnect_line = "[dim]○ [bold]Reconnect[/bold]  stopped[/dim]"

        self.query_one("#services-info", Static).update(
            "\n".join([rpc_line, sse_line, pac_line, reconnect_line])
        )

        # Refresh detail and context panels for currently selected tag
        self._update_detail_panel(self._selected_tag)
        self._update_context_panel(self._selected_tag)

        # Keep PAC tab content in sync whenever it's visible
        tabs = self.query_one("#detail-tabs", TabbedContent)
        if tabs.active == "tab-pac":
            self._load_pac_tab()

    def _render_all_stats(self) -> str:
        """Render aggregate stats for the 'All' view."""
        # Disabled connections are intentionally out of rotation — exclude them
        # from the "running / total" count so the total reflects what the user
        # actually expects to be up.
        enabled_data = {t: d for t, d in self._conn_data.items() if d["cs"].enabled}
        running = sum(1 for d in enabled_data.values() if d["cs"].running)
        total = len(enabled_data)
        if not self._conn_data:
            return "[dim]No connections configured.[/dim]"

        total_cpu = sum(d["proc_info"].get("cpu", 0.0) for d in self._conn_data.values())
        total_mem = sum(d["proc_info"].get("mem_mb", 0.0) for d in self._conn_data.values())
        total_rx = sum(d["bw"][0] for d in self._conn_data.values())
        total_tx = sum(d["bw"][1] for d in self._conn_data.values())
        total_rx_bytes = sum(d["bw_total"][0] for d in self._conn_data.values())
        total_tx_bytes = sum(d["bw_total"][1] for d in self._conn_data.values())
        total_conns = sum(d["proc_info"].get("conns", 0) for d in self._conn_data.values())
        total_fwds_l = sum(len(d.get("forwards_local", [])) for d in self._conn_data.values())
        total_fwds_r = sum(len(d.get("forwards_remote", [])) for d in self._conn_data.values())
        total_fwds = f"{total_fwds_l}L {total_fwds_r}R"

        lines = [
            f"[bold]All Connections[/bold]  {running} running / {total} total",
            "",
            f"  CPU total   {total_cpu:.1f}%{'':12} Memory  {total_mem:.1f} MB",
            f"  Connections {total_conns:<16} Fwds    {total_fwds}",
            "",
            f"  [dim]  {'Connection':<25}  [/dim]   [green]↓ RX[/green] [dim]{'Total':>7}[/dim]     [yellow]↑ TX[/yellow] [dim]{'Total':>7}[/dim]",            f"  [dim]{'─' * 61}[/dim]",
            _fmt_bw_line("[bold]All[/bold]", True, total_rx, total_tx, total_rx_bytes, total_tx_bytes, tag_width=25 + len("[bold][/bold]"), show_dot=False),
            f"  [dim]{'─' * 61}[/dim]",
        ]
        for tag, data in self._conn_data.items():
            lines.append(_fmt_bw_line(
                tag, data["cs"].running, *data["bw"], *data["bw_total"],
                enabled=data["cs"].enabled,
            ))
        return "\n".join(lines)

    def _update_detail_panel(self, tag: str | None) -> None:
        # Logs are global (not per-connection) — populate before any
        # per-tag branch returns. Previously this lived in the per-tag
        # block so the Logs tab was empty on the default "All" view.
        log_widget = self.query_one("#detail-logs", RichLog)
        log_widget.clear()
        mgr = self.app.manager  # type: ignore[attr-defined]
        for line in mgr.get_logs(500):
            log_widget.write(_format_log_line(line))

        # All view — aggregate stats + combined bandwidth charts
        if tag is None:
            self.query_one("#stats-content", Static).update(self._render_all_stats())
            total_rx = sum(d["bw"][0] for d in self._conn_data.values())
            total_tx = sum(d["bw"][1] for d in self._conn_data.values())
            tags = list(self._conn_data.keys())
            if tags:
                width = len(list(self._rx_history.get(tags[0], [0.0] * 60)))
                rx_agg = [
                    sum(self._rx_history.get(t, [0.0] * width)[i] for t in tags)
                    for i in range(width)
                ]
                tx_agg = [
                    sum(self._tx_history.get(t, [0.0] * width)[i] for t in tags)
                    for i in range(width)
                ]
            else:
                rx_agg = [0.0] * 60
                tx_agg = [0.0] * 60
            self.query_one("#bw-container", Horizontal).display = True
            rx_chart = self.query_one("#rx-chart", PlotextPlot)
            tx_chart = self.query_one("#tx-chart", PlotextPlot)
            rx_scaled, rx_unit = _scale_data(rx_agg)
            rx_max = max(1.0, max(rx_scaled))
            rx_ticks, rx_labels = _yticks(rx_max, rx_unit)
            rx_chart.plt.clear_data()
            rx_chart.plt.title(f"RX total  {_fmt_bps(total_rx)}")
            rx_chart.plt.ylim(0, rx_max)
            rx_chart.plt.yticks(rx_ticks, rx_labels)
            rx_chart.plt.plot(rx_scaled, color="green")
            rx_chart.refresh()
            tx_scaled, tx_unit = _scale_data(tx_agg)
            tx_max = max(1.0, max(tx_scaled))
            tx_ticks, tx_labels = _yticks(tx_max, tx_unit)
            tx_chart.plt.clear_data()
            tx_chart.plt.title(f"TX total  {_fmt_bps(total_tx)}")
            tx_chart.plt.ylim(0, tx_max)
            tx_chart.plt.yticks(tx_ticks, tx_labels)
            tx_chart.plt.plot(tx_scaled, color="yellow")
            tx_chart.refresh()
            return

        if tag not in self._conn_data:
            self.query_one("#stats-content", Static).update(
                "[dim]Select a connection.[/dim]"
            )
            self.query_one("#bw-container", Horizontal).display = False
            return

        self.query_one("#bw-container", Horizontal).display = True
        data = self._conn_data[tag]
        cs = data["cs"]
        proc_info = data["proc_info"]
        conn = data.get("conn")
        forwards_local = data.get("forwards_local", [])
        forwards_remote = data.get("forwards_remote", [])
        rx, tx = data["bw"]
        rx_total, tx_total = data["bw_total"]
        uptime = data.get("uptime")

        cpu = proc_info.get("cpu", 0.0) if proc_info else 0.0
        mem_mb = proc_info.get("mem_mb", 0.0) if proc_info else 0.0
        conns = proc_info.get("conns", 0) if proc_info else 0
        pid_str = str(cs.pid) if cs.pid else "—"
        ssh_host = conn.ssh_host if conn else "—"
        socks_port_str = str(cs.socks_port) if cs.socks_port else "auto"
        if cs.running:
            status_str = "[green]● running[/green]"
        elif not cs.enabled:
            status_str = "[dim]─ disabled[/dim]"
        else:
            status_str = "[red]○ stopped[/red]"
        uptime_str = _fmt_uptime(uptime) if uptime is not None else "—"
        fwd_summary = f"{len(forwards_local)}L {len(forwards_remote)}R"

        stats_lines = [
            f"[bold]{tag}[/bold]  {status_str}",
            "",
            f"  SSH host    {ssh_host:<16} SOCKS   {socks_port_str}",
            f"  PID         {pid_str:<16} Uptime  {uptime_str}",
            f"  CPU         {cpu:.1f}%{'':12} Memory  {mem_mb:.1f} MB",
            f"  Connections {conns:<16} Fwds    {fwd_summary}",
            "",
            f"  [green]↓ RX[/green]  rate  {_fmt_bps(rx):<16} total   [cyan]{_fmt_bytes(rx_total)}[/cyan]",
            f"  [yellow]↑ TX[/yellow]  rate  {_fmt_bps(tx):<16} total   [cyan]{_fmt_bytes(tx_total)}[/cyan]",
            f"  [dim]resets on stop[/dim]",
        ]
        self.query_one("#stats-content", Static).update("\n".join(stats_lines))

        # Bandwidth charts
        rx_data = list(self._rx_history.get(tag, [0.0] * 60))
        tx_data = list(self._tx_history.get(tag, [0.0] * 60))
        rx_chart = self.query_one("#rx-chart", PlotextPlot)
        tx_chart = self.query_one("#tx-chart", PlotextPlot)
        rx_scaled, rx_unit = _scale_data(rx_data)
        rx_max = max(1.0, max(rx_scaled))
        rx_ticks, rx_labels = _yticks(rx_max, rx_unit)
        rx_chart.plt.clear_data()
        rx_chart.plt.title(f"RX  {_fmt_bps(rx)}")
        rx_chart.plt.ylim(0, rx_max)
        rx_chart.plt.yticks(rx_ticks, rx_labels)
        rx_chart.plt.plot(rx_scaled, color="green")
        rx_chart.refresh()
        tx_scaled, tx_unit = _scale_data(tx_data)
        tx_max = max(1.0, max(tx_scaled))
        tx_ticks, tx_labels = _yticks(tx_max, tx_unit)
        tx_chart.plt.clear_data()
        tx_chart.plt.title(f"TX  {_fmt_bps(tx)}")
        tx_chart.plt.ylim(0, tx_max)
        tx_chart.plt.yticks(tx_ticks, tx_labels)
        tx_chart.plt.plot(tx_scaled, color="yellow")
        tx_chart.refresh()
        # (Logs already populated at the top of _update_detail_panel.)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is None or index == 0:
            self._selected_tag = None  # All row
        elif index - 1 < len(self._conn_tags):
            self._selected_tag = self._conn_tags[index - 1]
        else:
            self._selected_tag = None  # Stale index — fall back to All
        self._update_detail_panel(self._selected_tag)
        self._update_context_panel(self._selected_tag)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane_id = event.pane.id if event.pane else None
        if pane_id == "tab-config":
            self._load_config_tab()
        elif pane_id == "tab-pac":
            self._load_pac_tab()
        self.refresh_bindings()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one("#detail-tabs", TabbedContent).active = tab_id

    def action_edit_config(self) -> None:
        workspace = self.app.manager.workspace  # type: ignore[attr-defined]
        open_path(str(workspace / "config.yaml"))

    def _load_config_tab(self) -> None:
        workspace = self.app.manager.workspace  # type: ignore[attr-defined]
        path = workspace / "config.yaml"
        content = path.read_text() if path.exists() else "# No config file found"
        self.query_one("#config-tab-area", TextArea).load_text(content)

    def _load_pac_tab(self) -> None:
        workspace = self.app.manager.workspace  # type: ignore[attr-defined]
        path = workspace / "susops.pac"
        content = path.read_text() if path.exists() else "// PAC file not found"
        self.query_one("#pac-tab-area", TextArea).load_text(content)

    def _update_context_panel(self, tag: str | None) -> None:
        """Populate domain/forward/share sections. tag=None shows all connections."""
        config = getattr(self, "_last_config", None)
        shares = getattr(self, "_last_shares", [])
        conn_map = {c.tag: c for c in config.connections} if config else {}

        domain_lines: list[str] = []
        forward_lines: list[str] = []
        share_lines: list[str] = []

        conns = list(conn_map.values()) if tag is None else ([conn_map[tag]] if tag in conn_map else [])
        for conn in conns:
            prefix = markup_escape(f"[{conn.tag}]") if tag is None else ""
            for host in conn.pac_hosts:
                domain_lines.append(_fmt_domain_line(host, prefix))
            data = self._conn_data.get(conn.tag, {})
            for fw in data.get("forwards_local", []):
                if fw.enabled:
                    forward_lines.append(_fmt_forward_local(fw, prefix))
            for fw in data.get("forwards_remote", []):
                if fw.enabled:
                    forward_lines.append(_fmt_forward_remote(fw, prefix))

        for info in shares:
            if tag is not None and info.conn_tag != tag:
                continue
            if not info.running and info.stopped:
                continue  # manually stopped — hide from dashboard
            prefix = markup_escape(f"[{info.conn_tag}]") if tag is None else ""
            share_lines.append(_fmt_share_line(info, prefix))

        domain_text = "\n".join(domain_lines) if domain_lines else "[dim]—[/dim]"
        forward_text = "\n".join(forward_lines) if forward_lines else "[dim]—[/dim]"
        share_text = "\n".join(share_lines) if share_lines else "[dim]—[/dim]"

        self.query_one("#domain-content", Static).update(domain_text)
        self.query_one("#forward-content", Static).update(forward_text)
        self.query_one("#share-content", Static).update(share_text)

    @work(thread=True)
    def _start_sse_listener(self) -> None:
        """Connect to SSE /events and trigger fast refresh on state/share/forward events.

        Uses short connection timeouts (2 s) so the thread wakes up regularly
        and can exit cleanly when _sse_active is cleared on unmount.
        """
        import time
        import urllib.request
        mgr = self.app.manager  # type: ignore[attr-defined]
        backoff = 1.0
        while self._sse_active:
            status_url = mgr.get_status_url()
            if not status_url:
                for _ in range(20):
                    if not self._sse_active:
                        return
                    time.sleep(0.1)
                continue
            try:
                import os
                req = urllib.request.Request(status_url, headers={
                    "X-Susops-Client": "tui",
                    "X-Susops-Client-Version": susops.__version__,
                    "X-Susops-Pid": str(os.getpid()),
                })
                # 60 s read timeout — must exceed the server's 5 s SSE
                # heartbeat interval, otherwise every quiet stretch between
                # heartbeats triggers a fake "disconnect" and a reconnect
                # storm. The `_sse_active` check in the read loop still wakes
                # within ≤5 s thanks to the heartbeat itself.
                with urllib.request.urlopen(req, timeout=60) as resp:
                    backoff = 1.0
                    buf = ""
                    for raw in resp:
                        if not self._sse_active:
                            return
                        line = raw.decode("utf-8", errors="replace")
                        buf += line
                        if buf.endswith("\n\n"):
                            if any(e in buf for e in ("event: state", "event: share", "event: forward")):
                                self.app.call_from_thread(self.refresh_status)
                            buf = ""
            except Exception:
                for _ in range(max(1, int(backoff * 10))):
                    if not self._sse_active:
                        return
                    time.sleep(0.1)
                backoff = min(backoff * 2, 30.0)

    @work(thread=True)
    def action_start(self) -> None:
        self.app.manager.start(self._selected_tag)  # type: ignore[attr-defined]
        self.app.call_from_thread(self.refresh_status)

    @work(thread=True)
    def action_stop(self) -> None:
        self.app.manager.stop(tag=self._selected_tag)  # type: ignore[attr-defined]
        self.app.call_from_thread(self.refresh_status)

    @work(thread=True)
    def action_restart(self) -> None:
        self.app.manager.restart(self._selected_tag)  # type: ignore[attr-defined]
        self.app.call_from_thread(self.refresh_status)


    def action_launch_browser(self) -> None:
        """Open the modal browser picker — see BrowserScreen for the rest."""
        self.app.push_screen(BrowserScreen())

    def action_open_share(self, file_path: str) -> None:
        open_in_explorer(file_path)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "edit_config":
            active = self.query_one("#detail-tabs", TabbedContent).active
            return active == "tab-config"
        return True


class BrowserScreen(ModalScreen):
    """Modal: pick a detected browser to launch with the daemon's PAC URL.

    Enter on a row launches that browser with `--proxy-pac-url=…` (Chromium)
    or a workspace-owned Firefox profile (Firefox). `s` opens
    chrome://net-internals/#proxy on the selected Chromium browser.
    Esc / q closes the modal.

    Detection lives in susops.core.browsers — same table the macOS + Linux
    tray menus use, so adding a browser only happens in one place.
    """

    BINDINGS = [
        Binding("enter", "launch", "Launch", show=True),
        Binding("s", "settings", "Proxy Settings", show=True),
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    BrowserScreen { align: center middle; }
    #browser-dialog {
        width: 60; height: auto;
        background: $surface; border: round $primary;
        padding: 1 2;
    }
    #browser-list { height: auto; max-height: 14; margin: 1 0; }
    #browser-hint { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._browsers: list = []

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(id="browser-dialog"):
            yield Static("[bold]Launch Browser[/bold]", markup=True)
            yield ListView(id="browser-list")
            yield Static(
                "[dim]Enter = launch with PAC\n"
                "s = open proxy settings\n"
                "Esc = close[/dim]",
                id="browser-hint",
                markup=True,
            )

    def on_mount(self) -> None:
        from susops.core.browsers import detect_browsers
        self._browsers = detect_browsers()
        lv = self.query_one("#browser-list", ListView)
        if not self._browsers:
            lv.append(ListItem(Label("[dim]No browsers found[/dim]")))
            return
        for b in self._browsers:
            tag = "Chromium" if b.is_chromium else "Firefox"
            lv.append(ListItem(Label(f"  {b.name}  [dim]({tag})[/dim]")))
        lv.index = 0
        lv.focus()

    def _selected(self):
        lv = self.query_one("#browser-list", ListView)
        idx = lv.index
        if idx is None or not (0 <= idx < len(self._browsers)):
            return None
        return self._browsers[idx]

    def action_launch(self) -> None:
        browser = self._selected()
        if browser is None:
            return
        mgr = self.app.manager  # type: ignore[attr-defined]
        pac_url = ""
        try:
            pac_url = mgr.get_pac_url() or ""
        except Exception:
            pass
        if not pac_url:
            self.app.notify(
                "Proxy not running — start it first so the PAC URL is known.",
                severity="warning",
            )
            return
        from susops.core.browsers import launch_with_pac
        profile_dir = mgr.workspace / "firefox_profile"
        try:
            launch_with_pac(browser, pac_url, profile_dir=profile_dir)
            self.app.notify(f"Launched {browser.name} with PAC", timeout=3)
        except Exception as exc:
            self.app.notify(f"Launch failed: {exc}", severity="error")
        self.dismiss()

    def action_settings(self) -> None:
        browser = self._selected()
        if browser is None:
            return
        if not browser.is_chromium:
            self.app.notify(
                "Proxy settings shortcut works for Chromium-family browsers only.",
                severity="warning",
            )
            return
        from susops.core.browsers import open_proxy_settings
        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            open_proxy_settings(browser)
            self.app.notify(f"Opened proxy settings in {browser.name}", timeout=3)
        except Exception as exc:
            self.app.notify(f"Open failed: {exc}", severity="error")
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()

    def on_list_view_selected(self, _event) -> None:
        """ListView consumes Enter and emits a Selected message instead of
        letting the screen's `Binding("enter", "launch")` propagate — so we
        wire the message to action_launch directly. Without this, Enter
        appears to do nothing on the browser picker even though the binding
        is registered.
        """
        self.action_launch()
