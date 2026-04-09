"""Dashboard screen — lazydocker+nvtop-inspired split-pane live overview."""
from __future__ import annotations

from collections import deque
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)
from textual import work
from textual_plotext import PlotextPlot

from susops.core.types import ProcessState, StatusResult


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


class DashboardScreen(Screen):

    BINDINGS = [
        Binding("s", "start_all", "Start"),
        Binding("x", "stop_all", "Stop"),
        Binding("r", "restart_all", "Restart"),
        Binding("c", "push_screen('connections')", "Connections"),
        Binding("e", "push_screen('config')", "Config"),
        Binding("f", "push_screen('share')", "Share"),
    ]

    DEFAULT_CSS = """
    DashboardScreen { layout: vertical; }
    #status-bar { height: 1; padding: 0 1; background: $surface-darken-2; color: $text-muted; }
    #split { height: 1fr; }
    #sidebar { width: 36; background: $surface-darken-1; border-right: solid $primary-darken-2; }
    #pac-info { height: auto; padding: 0 1; margin: 0 0 1 0; border: round $primary-darken-1; }
    #shares-info { height: auto; padding: 0 1; margin: 0 0 1 0; border: round $primary-darken-1; }
    #detail-tabs { width: 1fr; }
    #bw-container { height: 1fr; }
    #rx-chart { height: 1fr; width: 1fr; border: round $primary-darken-1; margin: 0 1 1 1; }
    #tx-chart { height: 1fr; width: 1fr; border: round $primary-darken-1; margin: 0 1 1 0; }
    #stats-content { height: auto; padding: 1 2; }
    #fwd-table { height: 1fr; margin: 1; }
    #detail-logs { height: 1fr; margin: 1; border: round $primary-darken-1; }
    #conn-list { height: auto; min-height: 3; border: round $primary-darken-1; margin: 1 0 1 0; border-title-align: left; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._conn_tags: list[str] = []
        self._selected_tag: str | None = None
        self._conn_data: dict = {}
        self._rx_history: dict = {}
        self._tx_history: dict = {}
        self._idle_ticks: int = 0  # ticks since last active connection

    def compose(self) -> ComposeResult:
        #yield Header()
        with Horizontal(id="split"):
            with VerticalScroll(id="sidebar"):
                yield Static("", id="status-bar")
                yield ListView(id="conn-list")
                yield Static("", id="pac-info")
                yield Static("", id="shares-info")
            with TabbedContent(id="detail-tabs"):
                with TabPane("Stats", id="tab-stats"):
                    yield Static("Select a connection.", id="stats-content")
                with TabPane("Bandwidth", id="tab-bw"):
                    with Horizontal(id="bw-container"):
                        yield PlotextPlot(id="rx-chart")
                        yield PlotextPlot(id="tx-chart")
                with TabPane("Forwards", id="tab-fwd"):
                    yield DataTable(id="fwd-table", cursor_type="row")
                with TabPane("Logs", id="tab-logs"):
                    yield RichLog(id="detail-logs", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#conn-list", ListView).border_title = "Connections"
        self.query_one("#pac-info", Static).border_title = "PAC Server"
        self.query_one("#shares-info", Static).border_title = "Shares"
        self.query_one("#fwd-table", DataTable).add_columns(
            "Direction", "Local Port", "Local Bind", "Remote Port", "Remote Bind", "Label"
        )
        mgr = self.app.manager  # type: ignore[attr-defined]
        self._prev_on_log = mgr.on_log
        mgr.on_log = self._on_new_log
        self.set_interval(2.0, self._tick_refresh)
        self.refresh_status()
        self._start_sse_listener()

    def on_screen_resume(self) -> None:
        """Refresh immediately when returning to dashboard from another screen."""
        self.refresh_status()

    def on_unmount(self) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        mgr.on_log = self._prev_on_log

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

    @work(thread=True)
    def refresh_status(self) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        result: StatusResult = mgr.status()

        extras: dict[str, dict] = {}
        bw: dict[str, tuple[float, float]] = {}
        for cs in result.connection_statuses:
            extras[cs.tag] = mgr.get_process_info(cs.tag)
            bw[cs.tag] = mgr.get_bandwidth(cs.tag)

        shares = mgr.list_shares()
        config = mgr.list_config()
        self.app.call_from_thread(self._apply_status, result, extras, bw, shares, config)

    def _apply_status(
        self,
        result: StatusResult,
        extras: dict,
        bw: dict,
        shares: list,
        config,
    ) -> None:
        state_colors = {
            ProcessState.RUNNING: "green",
            ProcessState.STOPPED_PARTIALLY: "yellow",
            ProcessState.STOPPED: "red",
            ProcessState.ERROR: "red",
            ProcessState.INITIAL: "dim",
        }
        color = state_colors.get(result.state, "dim")
        running_count = sum(1 for cs in result.connection_statuses if cs.running)
        total_count = len(result.connection_statuses)
        pac_str = "[green]up[/green]" if result.pac_running else "[red]down[/red]"
        pac_port_str = f"{result.pac_port}" if result.pac_port else ""
        share_str = f"  [dim]shares: {len(shares)}[/dim]" if shares else ""
        self.query_one("#status-bar", Static).update(
            f"[{color}]●[/{color}] {running_count}/{total_count} running"
            f"{share_str}"
        )

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

        label_texts = []
        for cs in result.connection_statuses:
            dot = "[green]●[/green]" if cs.running else "[red]○[/red]"
            port_str = str(cs.socks_port) if cs.socks_port else "auto"
            rx, _tx = bw.get(cs.tag, (0.0, 0.0))
            label_texts.append(f"{dot} {cs.tag:<12} {port_str:<6} {_fmt_bps(rx):>9}↓")

        if new_tags == self._conn_tags:
            # Same connections — update text in-place, selection stays intact
            for item, text in zip(conn_list.query(ListItem), label_texts):
                item.query_one(Label).update(text)
        else:
            # Connections added/removed — rebuild and restore cursor
            cur = conn_list.index or 0
            conn_list.clear()
            self._conn_tags = new_tags
            for text in label_texts:
                conn_list.append(ListItem(Label(text)))
            if self._conn_tags:
                conn_list.index = min(cur, len(self._conn_tags) - 1)

        if self._conn_tags:
            idx = conn_list.index or 0
            self._selected_tag = self._conn_tags[min(idx, len(self._conn_tags) - 1)]
        else:
            self._selected_tag = None

        # Update PAC info (keep to 2 lines — sidebar is only 32 chars wide)
        host_count = sum(len(c.pac_hosts) for c in config.connections)
        pac_dot = "[green]●[/green]" if result.pac_running else "[red]○[/red]"
        pac_port_display = f" {result.pac_port}" if result.pac_port else ""
        pac_line = f"{pac_dot} PAC{pac_port_display}"
        if result.pac_running:
            pac_line += f"  [dim]{host_count} host(s)[/dim]"
        self.query_one("#pac-info", Static).update(pac_line)

        # Update shares info
        shares_widget = self.query_one("#shares-info", Static)
        if not shares:
            shares_widget.display = False
        else:
            shares_widget.display = True
            share_lines = []
            for info in shares:
                name = Path(info.file_path).name
                dot = "[green]●[/green]" if info.running else "[dim]○[/dim]"
                share_lines.append(f"{dot} {name}  :{info.port}")
            shares_widget.update("\n".join(share_lines))

        # Refresh detail panel for currently selected tag
        self._update_detail_panel(self._selected_tag)

    def _update_detail_panel(self, tag: str | None) -> None:
        if not tag or tag not in self._conn_data:
            self.query_one("#stats-content", Static).update(
                "[dim]Select a connection from the sidebar.[/dim]"
            )
            fwd_table = self.query_one("#fwd-table", DataTable)
            fwd_table.clear()
            return

        data = self._conn_data[tag]
        cs = data["cs"]
        proc_info = data["proc_info"]
        conn = data.get("conn")
        forwards_local = data.get("forwards_local", [])
        forwards_remote = data.get("forwards_remote", [])

        # Stats tab
        cpu = proc_info.get("cpu", 0.0) if proc_info else 0.0
        mem_mb = proc_info.get("mem_mb", 0.0) if proc_info else 0.0
        conns = proc_info.get("conns", 0) if proc_info else 0
        pid_str = str(cs.pid) if cs.pid else "—"
        ssh_host = conn.ssh_host if conn else "—"
        socks_port_str = str(cs.socks_port) if cs.socks_port else "auto"
        status_str = "[green]running[/green]" if cs.running else "[red]stopped[/red]"

        stats_lines = [
            f"[bold]{tag}[/bold]  {status_str}",
            "",
            f"  SSH host   : {ssh_host}",
            f"  SOCKS port : {socks_port_str}",
            f"  PID        : {pid_str}",
            f"  CPU        : {cpu:.1f}%",
            f"  Memory     : {mem_mb:.1f} MB",
            f"  Connections: {conns}",
        ]
        self.query_one("#stats-content", Static).update("\n".join(stats_lines))

        # Bandwidth tab
        rx_data = list(self._rx_history.get(tag, [0.0] * 60))
        tx_data = list(self._tx_history.get(tag, [0.0] * 60))
        rx_chart = self.query_one("#rx-chart", PlotextPlot)
        tx_chart = self.query_one("#tx-chart", PlotextPlot)

        rx, tx = data["bw"]
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

        # Logs tab — show all logs (no per-connection filter)
        log_widget = self.query_one("#detail-logs", RichLog)
        log_widget.clear()
        mgr = self.app.manager  # type: ignore[attr-defined]
        for line in mgr.get_logs(500):
            log_widget.write(line)

        # Forwards tab
        fwd_table = self.query_one("#fwd-table", DataTable)
        fwd_table.clear()
        for fw in forwards_local:
            fwd_table.add_row(
                "local",
                str(fw.src_port), fw.src_addr,
                str(fw.dst_port), fw.dst_addr,
                fw.tag or "",
            )
        for fw in forwards_remote:
            fwd_table.add_row(
                "remote",
                str(fw.src_port), fw.src_addr,
                str(fw.dst_port), fw.dst_addr,
                fw.tag or "",
            )

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is not None and index < len(self._conn_tags):
            tag = self._conn_tags[index]
            self._selected_tag = tag
            self._update_detail_panel(tag)

    @work(thread=True)
    def _start_sse_listener(self) -> None:
        """Connect to SSE /events and trigger fast refresh on state/share/forward events."""
        import time
        mgr = self.app.manager  # type: ignore[attr-defined]
        backoff = 1.0
        while True:
            status_url = mgr.get_status_url()
            if not status_url:
                time.sleep(2.0)
                continue
            try:
                import urllib.request
                req = urllib.request.Request(status_url)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    backoff = 1.0
                    buf = ""
                    for raw in resp:
                        line = raw.decode("utf-8", errors="replace")
                        buf += line
                        if buf.endswith("\n\n"):
                            if any(e in buf for e in ("event: state", "event: share", "event: forward")):
                                self.app.call_from_thread(self.refresh_status)
                            buf = ""
            except Exception:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    @work(thread=True)
    def action_start_all(self) -> None:
        self.app.manager.start()  # type: ignore[attr-defined]
        self.app.call_from_thread(self.refresh_status)

    @work(thread=True)
    def action_stop_all(self) -> None:
        self.app.manager.stop()  # type: ignore[attr-defined]
        self.app.call_from_thread(self.refresh_status)

    @work(thread=True)
    def action_restart_all(self) -> None:
        self.app.manager.restart()  # type: ignore[attr-defined]
        self.app.call_from_thread(self.refresh_status)
