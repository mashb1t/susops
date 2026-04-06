"""ConnectionCard widget — per-connection status card with bandwidth sparklines."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import Label, Sparkline, Static

from susops.core.types import ConnectionStatus, ProcessState

_DOT_COLORS = {
    True: "green",
    False: "red",
}


class ConnectionCard(Static):
    """Displays status, SOCKS port, PID, and bandwidth for one SSH connection."""

    DEFAULT_CSS = """
    ConnectionCard {
        height: auto;
        border: round $surface-darken-1;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    ConnectionCard .card-title {
        text-style: bold;
        height: 1;
    }
    ConnectionCard .card-meta {
        color: $text-muted;
        height: 1;
    }
    ConnectionCard Sparkline {
        height: 3;
        margin-top: 1;
    }
    """

    _MAX_SAMPLES = 30

    def __init__(self, tag: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tag = tag
        self._rx_data: list[float] = [0.0] * self._MAX_SAMPLES
        self._tx_data: list[float] = [0.0] * self._MAX_SAMPLES

    def compose(self) -> ComposeResult:
        yield Label("", id=f"title-{self.tag}", classes="card-title")
        yield Label("", id=f"meta-{self.tag}", classes="card-meta")
        yield Sparkline(self._rx_data, id=f"rx-{self.tag}", summary_function=max)
        yield Sparkline(self._tx_data, id=f"tx-{self.tag}", summary_function=max)

    def refresh_status(self, status: ConnectionStatus) -> None:
        dot_color = _DOT_COLORS[status.running]
        dot = f"[{dot_color}]●[/{dot_color}]"
        port_str = f" :{status.socks_port}" if status.socks_port else ""
        pid_str = f" [dim]pid={status.pid}[/dim]" if status.pid else ""
        self.query_one(f"#title-{self.tag}", Label).update(
            f"{dot} [bold]{status.tag}[/bold]{port_str}{pid_str}"
        )

    def refresh_bandwidth(self, rx: float, tx: float) -> None:
        self._rx_data = self._rx_data[1:] + [rx]
        self._tx_data = self._tx_data[1:] + [tx]
        self.query_one(f"#rx-{self.tag}", Sparkline).data = self._rx_data
        self.query_one(f"#tx-{self.tag}", Sparkline).data = self._tx_data

        def _fmt(b: float) -> str:
            if b >= 1_048_576:
                return f"{b / 1_048_576:.1f} MB/s"
            if b >= 1024:
                return f"{b / 1024:.0f} kB/s"
            return f"{b:.0f} B/s"

        self.query_one(f"#meta-{self.tag}", Label).update(
            f"[dim]↓ {_fmt(rx)}  ↑ {_fmt(tx)}[/dim]"
        )
