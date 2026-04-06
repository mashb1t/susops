"""BandwidthChart widget — rolling SSH bandwidth sparkline using psutil."""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from textual.reactive import reactive
from textual.widgets import Sparkline
from textual.widget import Widget
from textual.app import ComposeResult

if TYPE_CHECKING:
    from susops.facade import SusOpsManager


def _fmt_bps(bps: float) -> str:
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} kB/s"
    return f"{bps:.0f} B/s"


class BandwidthChart(Widget):
    """Shows ↓ rx / ↑ tx sparklines for one SSH tunnel tag.

    The parent screen is responsible for calling `update(rx, tx)` periodically.
    """

    DEFAULT_CSS = """
    BandwidthChart {
        height: 4;
        border: round $surface-darken-1;
        padding: 0 1;
    }
    BandwidthChart Label {
        height: 1;
        color: $text-muted;
    }
    """

    _MAX_SAMPLES = 30

    def __init__(self, tag: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tag = tag
        self._rx: deque[float] = deque([0.0] * self._MAX_SAMPLES, maxlen=self._MAX_SAMPLES)
        self._tx: deque[float] = deque([0.0] * self._MAX_SAMPLES, maxlen=self._MAX_SAMPLES)

    def compose(self) -> ComposeResult:
        yield Sparkline(list(self._rx), id=f"rx-{self.tag}", summary_function=max)
        yield Sparkline(list(self._tx), id=f"tx-{self.tag}", summary_function=max)

    def update(self, rx: float, tx: float) -> None:
        self._rx.append(rx)
        self._tx.append(tx)
        rx_label = f"↓ {_fmt_bps(rx)}"
        tx_label = f"↑ {_fmt_bps(tx)}"
        rx_spark = self.query_one(f"#rx-{self.tag}", Sparkline)
        tx_spark = self.query_one(f"#tx-{self.tag}", Sparkline)
        rx_spark.data = list(self._rx)
        tx_spark.data = list(self._tx)
        # Update border subtitle to show current values
        self.border_subtitle = f"{rx_label}  {tx_label}"
