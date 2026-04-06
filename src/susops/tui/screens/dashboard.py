"""Dashboard screen — htop-style live overview with per-process metrics."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, Static
from textual import work

from susops.core.types import ProcessState, StatusResult
from susops.tui.widgets.connection_card import ConnectionCard


class _PacCard(Static):
    DEFAULT_CSS = """
    _PacCard {
        height: auto;
        border: round $surface-darken-1;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("", id="pac-label")
        yield Label("", id="pac-hosts")

    def refresh_status(self, running: bool, port: int, host_count: int) -> None:
        dot = "[green]●[/green]" if running else "[red]○[/red]"
        port_str = f"  :{port}" if port else ""
        self.query_one("#pac-label", Label).update(
            f"{dot} PAC server{port_str}"
        )
        if running:
            self.query_one("#pac-hosts", Label).update(
                f"[dim]  Routing {host_count} host pattern(s)[/dim]"
            )
        else:
            self.query_one("#pac-hosts", Label).update("")


class _SharesSummary(Static):
    DEFAULT_CSS = """
    _SharesSummary {
        height: auto;
        border: round $surface-darken-1;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("", id="shares-label")

    def refresh_status(self, shares: list) -> None:
        if not shares:
            self.display = False
            return
        self.display = True
        lines = ["[bold]File shares[/bold]"]
        for info in shares:
            from pathlib import Path
            name = Path(info.file_path).name
            lines.append(f"  [green]●[/green] {name}  :{info.port}  [dim]pw: {info.password[:8]}…[/dim]")
        self.query_one("#shares-label", Label).update("\n".join(lines))


class DashboardScreen(Screen):
    """Htop-style dashboard: per-connection process metrics + bandwidth sparklines."""

    BINDINGS = [
        Binding("s", "start_all", "Start"),
        Binding("x", "stop_all", "Stop"),
        Binding("r", "restart_all", "Restart"),
        Binding("c", "push_screen('connections')", "Connections"),
        Binding("l", "push_screen('logs')", "Logs"),
        Binding("f", "push_screen('share')", "Share"),
    ]

    DEFAULT_CSS = """
    DashboardScreen { layout: vertical; }
    #toolbar {
        height: 3;
        layout: horizontal;
        padding: 0 1;
        background: $surface-darken-1;
    }
    #toolbar Button { margin: 0 1 0 0; min-width: 10; }
    #state-bar {
        height: 1;
        padding: 0 1;
        background: $surface-darken-2;
        color: $text-muted;
    }
    #cards-area { height: 1fr; padding: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="toolbar"):
            yield Button("▶ Start", id="btn-start", variant="success")
            yield Button("■ Stop", id="btn-stop", variant="error")
            yield Button("↺ Restart", id="btn-restart", variant="warning")
        yield Label("", id="state-bar")
        with ScrollableContainer(id="cards-area"):
            yield _PacCard(id="pac-card")
            yield _SharesSummary(id="shares-card")
        yield Footer()

    def on_mount(self) -> None:
        self._cards: dict[str, ConnectionCard] = {}
        self.refresh_status()
        self.set_interval(3.0, self.refresh_status)

    @work(thread=True)
    def refresh_status(self) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        result: StatusResult = mgr.status()

        # Gather per-connection extras in the worker thread (no blocking sleep)
        extras: dict[str, dict] = {}
        bw: dict[str, tuple[float, float]] = {}
        for cs in result.connection_statuses:
            extras[cs.tag] = mgr.get_process_info(cs.tag)
            bw[cs.tag] = mgr.get_bandwidth(cs.tag)

        shares = mgr.list_shares()
        self.app.call_from_thread(self._apply_status, result, extras, bw, shares)

    def _apply_status(
        self,
        result: StatusResult,
        extras: dict,
        bw: dict,
        shares: list,
    ) -> None:
        state_colors = {
            ProcessState.RUNNING: "green",
            ProcessState.STOPPED_PARTIALLY: "yellow",
            ProcessState.STOPPED: "red",
            ProcessState.ERROR: "red",
            ProcessState.INITIAL: "dim",
        }
        color = state_colors.get(result.state, "dim")
        self.query_one("#state-bar", Label).update(
            f"[{color}]● {result.state.value}[/{color}]"
            f"  PAC: {'[green]up[/green]' if result.pac_running else '[red]down[/red]'}"
            + (f"  :{result.pac_port}" if result.pac_port else "")
            + (f"  [dim]shares: {len(shares)}[/dim]" if shares else "")
        )

        cards_area = self.query_one("#cards-area")
        mgr = self.app.manager  # type: ignore[attr-defined]
        config = mgr.list_config()

        # Build tag→connection map for forwards
        conn_map = {c.tag: c for c in config.connections}

        for cs in result.connection_statuses:
            if cs.tag not in self._cards:
                card = ConnectionCard(cs.tag, id=f"card-{cs.tag}")
                self._cards[cs.tag] = card
                cards_area.mount(card, before="#pac-card")

            conn = conn_map.get(cs.tag)
            all_forwards = []
            if conn:
                all_forwards = list(conn.forwards.local) + list(conn.forwards.remote)

            self._cards[cs.tag].refresh_status(
                cs,
                proc_info=extras.get(cs.tag),
                forwards=all_forwards,
            )
            rx, tx = bw.get(cs.tag, (0.0, 0.0))
            self._cards[cs.tag].refresh_bandwidth(rx, tx)

        # Remove stale cards
        current_tags = {cs.tag for cs in result.connection_statuses}
        for tag in list(self._cards):
            if tag not in current_tags:
                self._cards[tag].remove()
                del self._cards[tag]

        # PAC card
        host_count = sum(len(c.pac_hosts) for c in config.connections)
        self.query_one("#pac-card", _PacCard).refresh_status(
            result.pac_running, result.pac_port, host_count
        )

        # Shares summary
        self.query_one("#shares-card", _SharesSummary).refresh_status(shares)

        # Button states
        self.query_one("#btn-start", Button).disabled = result.state == ProcessState.RUNNING
        self.query_one("#btn-stop", Button).disabled = result.state == ProcessState.STOPPED

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "btn-start": self.action_start_all,
            "btn-stop": self.action_stop_all,
            "btn-restart": self.action_restart_all,
        }
        fn = actions.get(event.button.id or "")
        if fn:
            fn()

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
