"""Config editor screen — YAML view with live edit via $EDITOR."""
from __future__ import annotations

import os
import subprocess

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, TextArea
from textual.containers import Horizontal
from textual import work


class ConfigEditorScreen(Screen):
    """Shows current config.yaml with option to open in $EDITOR."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "reload", "Reload"),
        Binding("e", "open_editor", "Edit in $EDITOR"),
    ]

    DEFAULT_CSS = """
    ConfigEditorScreen { layout: vertical; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="config-toolbar"):
            yield Button("↺ Reload", id="btn-reload")
            yield Button("✎ Edit in $EDITOR", id="btn-editor")
        area = TextArea(language="yaml", id="config-area", read_only=True)
        area.border_title = "config.yaml"
        yield area
        yield Footer()

    def on_mount(self) -> None:
        self._load_yaml()

    def _load_yaml(self) -> None:
        workspace = self.app.manager.workspace  # type: ignore[attr-defined]
        config_path = workspace / "config.yaml"
        if config_path.exists():
            content = config_path.read_text()
        else:
            content = "# No config file found"
        self.query_one("#config-area", TextArea).load_text(content)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-reload":
            self.action_reload()
        elif event.button.id == "btn-editor":
            self.action_open_editor()

    def action_reload(self) -> None:
        self._load_yaml()
        self.app.notify("Config reloaded")

    @work(thread=True)
    def action_open_editor(self) -> None:
        workspace = self.app.manager.workspace  # type: ignore[attr-defined]
        config_path = workspace / "config.yaml"
        editor = os.environ.get("EDITOR", "nano")
        try:
            subprocess.run([editor, str(config_path)], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            self.app.call_from_thread(
                self.app.notify, f"Editor failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._load_yaml)
        self.app.call_from_thread(self.app.notify, "Config saved")
