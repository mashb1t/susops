"""ConnectionCard widget — htop-style per-connection status card."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Label, Sparkline, Static

from susops.core.types import ConnectionStatus


def _fmt_bps(bps: float) -> str:
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} kB/s"
    return f"{bps:.0f} B/s"


class ConnectionCard(Static):
    """Htop-style card: status dot, SSH info, CPU/mem/conns, bandwidth sparklines, forwards."""

    DEFAULT_CSS = """
    ConnectionCard {
        height: auto;
        border: round $surface-darken-1;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    ConnectionCard .row-title {
        height: 1;
        text-style: bold;
    }
    ConnectionCard .row-sys {
        height: 1;
        color: $text-muted;
    }
    ConnectionCard .row-fwd {
        height: 1;
        color: $text-muted;
    }
    ConnectionCard .bw-label {
        height: 1;
        color: $text-muted;
        width: 18;
    }
    ConnectionCard Sparkline {
        height: 3;
    }
    """

    _MAX_SAMPLES = 40

    _title: reactive[str] = reactive("")
    _sys_info: reactive[str] = reactive("")
    _fwd_info: reactive[str] = reactive("")
    _rx_label: reactive[str] = reactive("↓  0 B/s")
    _tx_label: reactive[str] = reactive("↑  0 B/s")

    def __init__(self, tag: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tag = tag
        self._rx_data: list[float] = [0.0] * self._MAX_SAMPLES
        self._tx_data: list[float] = [0.0] * self._MAX_SAMPLES

    def compose(self) -> ComposeResult:
        yield Label(self._title, id=f"title-{self.tag}", classes="row-title")
        yield Label(self._sys_info, id=f"sys-{self.tag}", classes="row-sys")
        with Horizontal():
            yield Label(self._rx_label, id=f"rxlbl-{self.tag}", classes="bw-label")
            yield Sparkline(self._rx_data, id=f"rx-{self.tag}", summary_function=max)
        with Horizontal():
            yield Label(self._tx_label, id=f"txlbl-{self.tag}", classes="bw-label")
            yield Sparkline(self._tx_data, id=f"tx-{self.tag}", summary_function=max)
        yield Label(self._fwd_info, id=f"fwd-{self.tag}", classes="row-fwd")

    def _safe_update(self, widget_id: str, cls, value) -> None:
        try:
            self.query_one(widget_id, cls).update(value)
        except Exception:
            pass

    def watch__title(self, v: str) -> None:
        self._safe_update(f"#title-{self.tag}", Label, v)

    def watch__sys_info(self, v: str) -> None:
        self._safe_update(f"#sys-{self.tag}", Label, v)

    def watch__fwd_info(self, v: str) -> None:
        self._safe_update(f"#fwd-{self.tag}", Label, v)

    def watch__rx_label(self, v: str) -> None:
        self._safe_update(f"#rxlbl-{self.tag}", Label, v)

    def watch__tx_label(self, v: str) -> None:
        self._safe_update(f"#txlbl-{self.tag}", Label, v)

    def refresh_status(self, status: ConnectionStatus, proc_info: dict | None = None, forwards: list | None = None) -> None:
        dot_color = "green" if status.running else "red"
        dot = f"[{dot_color}]●[/{dot_color}]"
        port_str = f"  SOCKS :{status.socks_port}" if status.socks_port else ""
        pid_str = f"  pid={status.pid}" if status.pid else ""
        self._title = f"{dot} [bold]{status.tag}[/bold]{port_str}{pid_str}"

        if proc_info and status.running:
            cpu = proc_info.get("cpu", 0.0)
            mem = proc_info.get("mem_mb", 0.0)
            conns = proc_info.get("conns", 0)
            self._sys_info = (
                f"[dim]CPU: {cpu:.1f}%  MEM: {mem:.1f} MB  "
                f"Active conns: {conns}[/dim]"
            )
        elif not status.running:
            self._sys_info = "[dim]stopped[/dim]"
        else:
            self._sys_info = ""

        if forwards is not None:
            parts = []
            for fw in forwards:
                label = f" [{fw.tag}]" if fw.tag else ""
                parts.append(f":{fw.src_port}→{fw.dst_addr}:{fw.dst_port}{label}")
            self._fwd_info = (
                f"[dim]Forwards: {', '.join(parts)}[/dim]" if parts else ""
            )

    def refresh_bandwidth(self, rx: float, tx: float) -> None:
        self._rx_data = self._rx_data[1:] + [rx]
        self._tx_data = self._tx_data[1:] + [tx]
        try:
            self.query_one(f"#rx-{self.tag}", Sparkline).data = self._rx_data
            self.query_one(f"#tx-{self.tag}", Sparkline).data = self._tx_data
        except Exception:
            pass
        self._rx_label = f"↓ {_fmt_bps(rx):>10}"
        self._tx_label = f"↑ {_fmt_bps(tx):>10}"
