"""PAC file viewer — read-only static view of the generated susops.pac."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import TextArea
from susops.tui.screens import compose_footer


class PacViewerScreen(Screen):
    """Shows the current susops.pac file content (read-only)."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "reload", "Reload"),
    ]

    DEFAULT_CSS = """
    PacViewerScreen { layout: vertical; }
    """

    def compose(self) -> ComposeResult:
        area = TextArea(language="javascript", id="pac-area", read_only=True)
        area.border_title = "susops.pac"
        yield area
        yield from compose_footer()

    def on_mount(self) -> None:
        self._load_pac()

    def _load_pac(self) -> None:
        workspace = self.app.manager.workspace  # type: ignore[attr-defined]
        pac_path = workspace / "susops.pac"
        content = pac_path.read_text() if pac_path.exists() else "// PAC file not found (start a connection first)"
        self.query_one("#pac-area", TextArea).load_text(content)

    def action_reload(self) -> None:
        self._load_pac()
        self.app.notify("PAC file reloaded")
