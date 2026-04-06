"""Log viewer screen — real-time log display with filter and auto-scroll."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, RichLog, Select, Switch, Label
from textual.containers import Horizontal, Vertical


class LogViewerScreen(Screen):
    """Displays the SusOpsManager log buffer in real-time."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("c", "clear_logs", "Clear"),
        Binding("a", "toggle_autoscroll", "Auto-scroll"),
    ]

    DEFAULT_CSS = """
    LogViewerScreen { layout: vertical; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="log-toolbar"):
            yield Label("Filter:")
            yield Select(
                [("All", "")],
                value="",
                id="filter-select",
                allow_blank=False,
            )
            yield Label("Auto-scroll:")
            yield Switch(value=True, id="autoscroll-switch")
            yield Button("Clear", id="btn-clear")
        log = RichLog(highlight=True, markup=True, wrap=True, id="log-view")
        log.border_title = "Logs"
        yield log
        yield Footer()

    def on_mount(self) -> None:
        self._update_filter_options()
        self._load_logs()
        # Register log callback
        mgr = self.app.manager  # type: ignore[attr-defined]
        self._prev_on_log = mgr.on_log
        mgr.on_log = self._on_new_log

    def on_unmount(self) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        mgr.on_log = self._prev_on_log

    def _update_filter_options(self) -> None:
        config = self.app.manager.list_config()  # type: ignore[attr-defined]
        options = [("All", "")] + [(c.tag, c.tag) for c in config.connections]
        self.query_one("#filter-select", Select).set_options(options)

    def _load_logs(self) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        log_view = self.query_one("#log-view", RichLog)
        log_view.clear()
        filter_tag = self.query_one("#filter-select", Select).value or ""
        for line in mgr.get_logs(500):
            if not filter_tag or f"[{filter_tag}]" in line:
                log_view.write(line)
        if self.query_one("#autoscroll-switch", Switch).value:
            log_view.scroll_end(animate=False)

    def _on_new_log(self, msg: str) -> None:
        filter_tag = self.query_one("#filter-select", Select).value or ""
        if filter_tag and f"[{filter_tag}]" not in msg:
            return
        log_view = self.query_one("#log-view", RichLog)
        log_view.write(msg)
        if self.query_one("#autoscroll-switch", Switch).value:
            log_view.scroll_end(animate=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-clear":
            self.action_clear_logs()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "filter-select":
            self._load_logs()

    def action_clear_logs(self) -> None:
        self.query_one("#log-view", RichLog).clear()

    def action_toggle_autoscroll(self) -> None:
        sw = self.query_one("#autoscroll-switch", Switch)
        sw.value = not sw.value
