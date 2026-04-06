"""LogPanel widget — scrollable log view with auto-scroll."""
from __future__ import annotations

from textual.widgets import RichLog
from textual.widget import Widget
from textual.app import ComposeResult
from textual.reactive import reactive


class LogPanel(Widget):
    """Scrollable RichLog with optional auto-scroll toggle."""

    DEFAULT_CSS = """
    LogPanel {
        height: 1fr;
    }
    LogPanel RichLog {
        height: 1fr;
        border: round $surface-darken-1;
    }
    """

    auto_scroll: reactive[bool] = reactive(True)

    def compose(self) -> ComposeResult:
        log = RichLog(highlight=True, markup=True, wrap=True, id="log-content")
        log.can_focus = True
        yield log

    def write(self, text: str) -> None:
        log = self.query_one("#log-content", RichLog)
        log.write(text)
        if self.auto_scroll:
            log.scroll_end(animate=False)

    def clear(self) -> None:
        self.query_one("#log-content", RichLog).clear()

    def toggle_auto_scroll(self) -> None:
        self.auto_scroll = not self.auto_scroll
