"""StatusIndicator widget — colored dot + state label."""
from __future__ import annotations

from textual.app import RenderResult
from textual.reactive import reactive
from textual.widget import Widget

from susops.core.types import ProcessState

_STATE_COLORS = {
    ProcessState.RUNNING: "green",
    ProcessState.STOPPED_PARTIALLY: "yellow",
    ProcessState.STOPPED: "red",
    ProcessState.ERROR: "red",
    ProcessState.INITIAL: "dim",
}

_STATE_LABELS = {
    ProcessState.RUNNING: "running",
    ProcessState.STOPPED_PARTIALLY: "partial",
    ProcessState.STOPPED: "stopped",
    ProcessState.ERROR: "error",
    ProcessState.INITIAL: "unknown",
}


class StatusIndicator(Widget):
    """A colored dot with a state label."""

    DEFAULT_CSS = """
    StatusIndicator {
        height: 1;
        width: auto;
    }
    """

    state: reactive[ProcessState] = reactive(ProcessState.INITIAL)

    def render(self) -> RenderResult:
        color = _STATE_COLORS.get(self.state, "dim")
        label = _STATE_LABELS.get(self.state, "unknown")
        return f"[{color}]●[/{color}] {label}"
