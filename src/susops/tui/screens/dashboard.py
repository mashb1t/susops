"""Dashboard screen — lazydocker+nvtop-inspired split-pane live overview."""
from __future__ import annotations

from collections import deque
from pathlib import Path

from rich.markup import escape as markup_escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
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
from susops.tui.screens import open_in_explorer, open_path, proto_label, share_name_markup


def _fmt_bps(bps: float) -> str:
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.1f}MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f}kB/s"
    return f"{bps:.0f}B/s"


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


def _fmt_bytes(b: float) -> str:
    """Format raw bytes as a human-readable string (e.g. 1.2 GB, 450 MB, 12 kB)."""
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f}GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f}MB"
    if b >= 1024:
        return f"{b / 1024:.0f}kB"
    return f"{b:.0f}B"


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
) -> str:
    dot = "[green]●[/green]" if running else "[red]○[/red]"
    prefix = f"  {dot} " if show_dot else "    "
    return (
        f"{prefix}{tag:<{tag_width}}  "
        f"{_fmt_bps(rx):>7} [cyan]{_fmt_bytes(rx_t):>7}[/cyan]  "
        f"{_fmt_bps(tx):>7} [cyan]{_fmt_bytes(tx_t):>7}[/cyan]"
    )


def _fmt_domain_line(host: str, prefix: str = "") -> str:
    pre = f"[dim]{prefix}[/dim] " if prefix else ""
    return f"{pre}{host}"


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
    dot = "[green]●[/green]" if info.running else ("[dim]○[/dim]" if info.stopped else "[red]○[/red]")
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
        Binding("e", "edit_config", "Edit config"),
    ]

    DEFAULT_CSS = """
    DashboardScreen { layout: vertical; }
    #main-split { height: 1fr; }
    #conn-panel { width: 49; background: $surface-darken-1; border-right: solid $primary-darken-2; }
    #conn-list  { height: 1fr; border: round $primary-darken-1; margin: 1 1 0 1; border-title-align: left; }
    #pac-info   { height: auto; padding: 0 1; border: round $primary-darken-1; margin: 1; border-title-align: left; }
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
            # Left: connection list + PAC status
            with Vertical(id="conn-panel"):
                yield ListView(id="conn-list")
                yield Static(id="pac-info", markup=True)
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
        self.query_one("#pac-info", Static).border_title = "PAC"
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
        try:
            self.app.call_from_thread(self.query_one("#detail-logs", RichLog).write, msg)
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
        self.app.call_from_thread(
            self._apply_status, result, extras, bw, bw_totals, uptimes, shares, config
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
            dot = "[green]●[/green]" if cs.running else "[red]○[/red]"
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

        # PAC server status
        if result.pac_running and result.pac_port:
            pac_text = f"[green]●[/green] http://localhost:{result.pac_port}/susops.pac"
        else:
            pac_text = "[dim]○ stopped[/dim]"
        self.query_one("#pac-info", Static).update(pac_text)

        # Refresh detail and context panels for currently selected tag
        self._update_detail_panel(self._selected_tag)
        self._update_context_panel(self._selected_tag)

        # Keep PAC tab content in sync whenever it's visible
        tabs = self.query_one("#detail-tabs", TabbedContent)
        if tabs.active == "tab-pac":
            self._load_pac_tab()

    def _render_all_stats(self) -> str:
        """Render aggregate stats for the 'All' view."""
        running = sum(1 for d in self._conn_data.values() if d["cs"].running)
        total = len(self._conn_data)
        if total == 0:
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
            lines.append(_fmt_bw_line(tag, data["cs"].running, *data["bw"], *data["bw_total"]))
        return "\n".join(lines)

    def _update_detail_panel(self, tag: str | None) -> None:
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
        status_str = "[green]● running[/green]" if cs.running else "[red]○ stopped[/red]"
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

        # Logs
        log_widget = self.query_one("#detail-logs", RichLog)
        log_widget.clear()
        mgr = self.app.manager  # type: ignore[attr-defined]
        for line in mgr.get_logs(500):
            log_widget.write(line)

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
                req = urllib.request.Request(status_url)
                # Short timeout so the thread wakes up and can exit promptly
                with urllib.request.urlopen(req, timeout=2) as resp:
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


    def action_open_share(self, file_path: str) -> None:
        open_in_explorer(file_path)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "edit_config":
            active = self.query_one("#detail-tabs", TabbedContent).active
            return active == "tab-config"
        return True
