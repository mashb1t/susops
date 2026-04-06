"""Share screen — file share/fetch wizard."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static
from textual import work


class ShareScreen(Screen):
    """Wizard for sharing and fetching encrypted files."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    DEFAULT_CSS = """
    ShareScreen {
        layout: vertical;
    }
    #content {
        height: 1fr;
        padding: 1 2;
    }
    .section {
        height: auto;
        border: round $surface-darken-1;
        padding: 1;
        margin-bottom: 1;
    }
    .section Label {
        margin-bottom: 1;
    }
    .section Input {
        margin-bottom: 1;
    }
    .section Button {
        margin-right: 1;
    }
    #share-info {
        color: $success;
        height: auto;
        margin-top: 1;
    }
    #fetch-result {
        color: $success;
        height: auto;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="content"):
            # Share section
            with Container(classes="section"):
                yield Label("[bold]Share a file[/bold]")
                yield Input(placeholder="/path/to/file", id="share-path")
                yield Input(placeholder="password (leave blank to auto-generate)", id="share-pass")
                yield Input(placeholder="port (0 = auto)", id="share-port", value="0")
                yield Button("Start sharing", id="btn-share", variant="success")
                yield Button("Stop sharing", id="btn-stop-share", variant="error")
                yield Static("", id="share-info")
            # Fetch section
            with Container(classes="section"):
                yield Label("[bold]Fetch a shared file[/bold]")
                yield Input(placeholder="port", id="fetch-port")
                yield Input(placeholder="password", id="fetch-pass")
                yield Input(placeholder="save to (optional)", id="fetch-out")
                yield Button("Fetch", id="btn-fetch", variant="primary")
                yield Static("", id="fetch-result")
        yield Footer()

    def on_mount(self) -> None:
        self._update_share_button_state()

    def _update_share_button_state(self) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        running = mgr.share_is_running()
        self.query_one("#btn-share", Button).disabled = running
        self.query_one("#btn-stop-share", Button).disabled = not running

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-share":
            self._do_share()
        elif event.button.id == "btn-stop-share":
            self._do_stop_share()
        elif event.button.id == "btn-fetch":
            self._do_fetch()

    @work(thread=True)
    def _do_share(self) -> None:
        path_str = self.query_one("#share-path", Input).value.strip()
        password = self.query_one("#share-pass", Input).value.strip() or None
        port_str = self.query_one("#share-port", Input).value.strip()
        try:
            port = int(port_str or "0")
        except ValueError:
            port = 0

        if not path_str:
            self.app.call_from_thread(
                self.query_one("#share-info", Static).update,
                "[red]Please enter a file path.[/red]",
            )
            return

        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            info = mgr.share(Path(path_str), password=password, port=port or None)
            msg = (
                f"[green]Sharing:[/green] {info.file_path}\n"
                f"[green]URL:[/green]     {info.url}\n"
                f"[green]Password:[/green] {info.password}\n"
                f"[green]Port:[/green]    {info.port}\n\n"
                f"[dim]susops fetch {info.port} {info.password}[/dim]"
            )
        except Exception as e:
            msg = f"[red]Error: {e}[/red]"

        self.app.call_from_thread(self.query_one("#share-info", Static).update, msg)
        self.app.call_from_thread(self._update_share_button_state)

    def _do_stop_share(self) -> None:
        self.app.manager.stop_share()  # type: ignore[attr-defined]
        self.query_one("#share-info", Static).update("[dim]Share stopped.[/dim]")
        self._update_share_button_state()

    @work(thread=True)
    def _do_fetch(self) -> None:
        port_str = self.query_one("#fetch-port", Input).value.strip()
        password = self.query_one("#fetch-pass", Input).value.strip()
        out_str = self.query_one("#fetch-out", Input).value.strip()

        if not port_str or not password:
            self.app.call_from_thread(
                self.query_one("#fetch-result", Static).update,
                "[red]Port and password are required.[/red]",
            )
            return

        try:
            port = int(port_str)
        except ValueError:
            self.app.call_from_thread(
                self.query_one("#fetch-result", Static).update,
                "[red]Invalid port.[/red]",
            )
            return

        outfile = Path(out_str) if out_str else None
        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            result = mgr.fetch(port=port, password=password, outfile=outfile)
            msg = f"[green]Downloaded to:[/green] {result}"
        except Exception as e:
            msg = f"[red]Error: {e}[/red]"

        self.app.call_from_thread(self.query_one("#fetch-result", Static).update, msg)
