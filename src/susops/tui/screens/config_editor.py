"""Config editor screen — YAML view with option to open in system editor."""
from __future__ import annotations

import shutil
import subprocess

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Static, TextArea


class ConfigEditorScreen(Screen):
    """Shows current config.yaml and opens it in the system default editor."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "reload", "Reload"),
        Binding("e", "open_editor", "Edit"),
    ]

    DEFAULT_CSS = """
    ConfigEditorScreen { layout: vertical; }
    """

    def compose(self) -> ComposeResult:
        area = TextArea(language="yaml", id="config-area", read_only=True)
        area.border_title = "config.yaml"
        yield area
        import susops
        with Horizontal(classes="footer-row"):
            yield Footer()
            yield Static(f"v{susops.__version__}", classes="footer-version")

    def on_mount(self) -> None:
        self._load_yaml()

    def _load_yaml(self) -> None:
        workspace = self.app.manager.workspace  # type: ignore[attr-defined]
        config_path = workspace / "config.yaml"
        content = config_path.read_text() if config_path.exists() else "# No config file found"
        self.query_one("#config-area", TextArea).load_text(content)

    def action_reload(self) -> None:
        self._load_yaml()
        self.app.notify("Config reloaded")

    def action_open_editor(self) -> None:
        """Open config.yaml with the system default file handler (non-blocking)."""
        workspace = self.app.manager.workspace  # type: ignore[attr-defined]
        config_path = workspace / "config.yaml"
        for opener in ("xdg-open", "open"):
            if shutil.which(opener):
                subprocess.Popen([opener, str(config_path)])
                return
        self.app.notify("No file opener found (xdg-open / open)", severity="error")
