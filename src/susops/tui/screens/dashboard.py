"""Dashboard screen — live status overview with per-connection bandwidth sparklines."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
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
        yield Label("", id="pac-status")

    def refresh_status(self, running: bool, port: int) -> None:
        dot = "[green]●[/green]" if running else "[red]○[/red]"
        port_str = f" :{port}" if port else ""
        self.query_one("#pac-status", Label).update(
            f"{dot} PAC server{port_str}"
        )


class _ShareCard(Static):
    DEFAULT_CSS = """
    _ShareCard {
        height: auto;
        border: round $surface-darken-1;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("", id="share-status")

    def refresh_status(self, running: bool, url: str = "") -> None:
        if running:
            self.display = True
            self.query_one("#share-status", Label).update(
                f"[green]●[/green] Share active — {url}"
            )
        else:
            self.display = False


class DashboardScreen(Screen):
    """Main dashboard with live connection status and bandwidth sparklines."""

    BINDINGS = [
        Binding("s", "start_all", "Start"),
        Binding("x", "stop_all", "Stop"),
        Binding("r", "restart_all", "Restart"),
        Binding("c", "push_screen('connections')", "Connections"),
        Binding("l", "push_screen('logs')", "Logs"),
        Binding("f", "push_screen('share')", "Share"),
    ]

    DEFAULT_CSS = """
    DashboardScreen {
        layout: vertical;
    }
    #toolbar {
        height: 3;
        layout: horizontal;
        padding: 0 1;
        background: $surface-darken-1;
    }
    #toolbar Button {
        margin: 0 1 0 0;
        min-width: 10;
    }
    #cards-area {
        height: 1fr;
        padding: 1;
    }
    #state-label {
        height: 1;
        margin-bottom: 1;
        text-align: center;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="toolbar"):
            yield Button("▶ Start", id="btn-start", variant="success")
            yield Button("■ Stop", id="btn-stop", variant="error")
            yield Button("↺ Restart", id="btn-restart", variant="warning")
        with ScrollableContainer(id="cards-area"):
            yield Label("", id="state-label")
            yield _PacCard(id="pac-card")
            yield _ShareCard(id="share-card")
        yield Footer()

    def on_mount(self) -> None:
        self._cards: dict[str, ConnectionCard] = {}
        self.refresh_status()
        self.set_interval(3.0, self.refresh_status)
        self.set_interval(5.0, self._refresh_bandwidth)

    @work(thread=True)
    def refresh_status(self) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        result: StatusResult = mgr.status()
        self.call_from_thread(self._apply_status, result)

    def _apply_status(self, result: StatusResult) -> None:
        # Update global state label
        state_colors = {
            ProcessState.RUNNING: "green",
            ProcessState.STOPPED_PARTIALLY: "yellow",
            ProcessState.STOPPED: "red",
            ProcessState.ERROR: "red",
            ProcessState.INITIAL: "dim",
        }
        color = state_colors.get(result.state, "dim")
        self.query_one("#state-label", Label).update(
            f"[{color}]{result.state.value}[/{color}]"
        )

        cards_area = self.query_one("#cards-area")

        # Ensure a card exists for each connection
        for cs in result.connection_statuses:
            if cs.tag not in self._cards:
                card = ConnectionCard(cs.tag, id=f"card-{cs.tag}")
                self._cards[cs.tag] = card
                cards_area.mount(card, before="#pac-card")
            self._cards[cs.tag].refresh_status(cs)

        # Remove cards for deleted connections
        current_tags = {cs.tag for cs in result.connection_statuses}
        for tag in list(self._cards):
            if tag not in current_tags:
                self._cards[tag].remove()
                del self._cards[tag]

        # PAC card
        self.query_one("#pac-card", _PacCard).refresh_status(result.pac_running, result.pac_port)

        # Share card
        mgr = self.app.manager  # type: ignore[attr-defined]
        share_running = mgr.share_is_running()
        share_url = mgr.get_pac_url() if share_running else ""
        self.query_one("#share-card", _ShareCard).refresh_status(share_running, share_url)

        # Update button states
        is_running = result.state == ProcessState.RUNNING
        is_stopped = result.state == ProcessState.STOPPED
        self.query_one("#btn-start", Button).disabled = is_running
        self.query_one("#btn-stop", Button).disabled = is_stopped

    @work(thread=True)
    def _refresh_bandwidth(self) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        for tag, card in list(self._cards.items()):
            rx, tx = mgr.get_bandwidth(tag)
            self.call_from_thread(card.refresh_bandwidth, rx, tx)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-start":
            self.action_start_all()
        elif event.button.id == "btn-stop":
            self.action_stop_all()
        elif event.button.id == "btn-restart":
            self.action_restart_all()

    @work(thread=True)
    def action_start_all(self) -> None:
        self.app.manager.start()  # type: ignore[attr-defined]
        self.call_from_thread(self.refresh_status)

    @work(thread=True)
    def action_stop_all(self) -> None:
        self.app.manager.stop()  # type: ignore[attr-defined]
        self.call_from_thread(self.refresh_status)

    @work(thread=True)
    def action_restart_all(self) -> None:
        self.app.manager.restart()  # type: ignore[attr-defined]
        self.call_from_thread(self.refresh_status)
