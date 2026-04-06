"""Share screen — multi-share list with modal dialogs for add/fetch."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static
from textual import work


class _AddShareDialog(Screen):
    """Modal: start sharing a file."""

    DEFAULT_CSS = """
    _AddShareDialog { align: center middle; }
    #dialog {
        width: 60; height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #dialog Label { margin-top: 1; }
    #dialog Input { margin-bottom: 1; }
    .btn-row { layout: horizontal; margin-top: 1; }
    .btn-row Button { margin-right: 1; }
    #error { color: $error; height: 1; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label("[bold]Share a file[/bold]")
            yield Label("File path:")
            yield Input(placeholder="/path/to/file", id="path")
            yield Label("Password (blank = auto-generate):")
            yield Input(placeholder="", id="password")
            yield Label("Port (0 = auto):")
            yield Input(placeholder="0", value="0", id="port")
            yield Label("", id="error")
            with Horizontal(classes="btn-row"):
                yield Button("Share", id="btn-ok", variant="success")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        path = self.query_one("#path", Input).value.strip()
        if not path:
            self.query_one("#error", Label).update("File path is required.")
            return
        if not Path(path).exists():
            self.query_one("#error", Label).update("File not found.")
            return
        pw = self.query_one("#password", Input).value.strip() or None
        try:
            port = int(self.query_one("#port", Input).value.strip() or "0")
        except ValueError:
            port = 0
        self.dismiss({"path": path, "password": pw, "port": port})


class _FetchDialog(Screen):
    """Modal: fetch a shared file."""

    DEFAULT_CSS = """
    _FetchDialog { align: center middle; }
    #dialog {
        width: 60; height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #dialog Label { margin-top: 1; }
    #dialog Input { margin-bottom: 1; }
    .btn-row { layout: horizontal; margin-top: 1; }
    .btn-row Button { margin-right: 1; }
    #error { color: $error; height: 1; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label("[bold]Fetch a shared file[/bold]")
            yield Label("Port:")
            yield Input(placeholder="52100", id="port")
            yield Label("Password:")
            yield Input(placeholder="", id="password")
            yield Label("Save to (blank = ~/Downloads/<filename>):")
            yield Input(placeholder="", id="outfile")
            yield Label("", id="error")
            with Horizontal(classes="btn-row"):
                yield Button("Fetch", id="btn-ok", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        port_str = self.query_one("#port", Input).value.strip()
        pw = self.query_one("#password", Input).value.strip()
        if not port_str or not pw:
            self.query_one("#error", Label).update("Port and password are required.")
            return
        try:
            port = int(port_str)
        except ValueError:
            self.query_one("#error", Label).update("Invalid port.")
            return
        outfile = self.query_one("#outfile", Input).value.strip() or None
        self.dismiss({"port": port, "password": pw, "outfile": outfile})


class ShareScreen(Screen):
    """Active shares list + dialogs to add shares or fetch files."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "add_share", "Add share"),
        Binding("f", "fetch_file", "Fetch"),
        Binding("d", "stop_share", "Stop share"),
        Binding("r", "refresh", "Refresh"),
    ]

    DEFAULT_CSS = """
    ShareScreen { layout: vertical; }
    #toolbar {
        height: 3;
        layout: horizontal;
        padding: 0 1;
        background: $surface-darken-1;
    }
    #toolbar Button { margin-right: 1; }
    #shares-table { height: 1fr; margin: 1; }
    #status-bar {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="toolbar"):
            yield Button("+ Share file", id="btn-add", variant="success")
            yield Button("↓ Fetch file", id="btn-fetch", variant="primary")
            yield Button("■ Stop share", id="btn-stop", variant="error")
        yield DataTable(id="shares-table", cursor_type="row")
        yield Label("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        tbl = self.query_one("#shares-table", DataTable)
        tbl.add_columns("File", "Port", "URL", "Password", "CLI command")
        self._reload()

    def _reload(self) -> None:
        tbl = self.query_one("#shares-table", DataTable)
        tbl.clear()
        shares = self.app.manager.list_shares()  # type: ignore[attr-defined]
        for info in shares:
            name = Path(info.file_path).name
            tbl.add_row(
                name,
                str(info.port),
                info.url,
                info.password,
                f"susops fetch {info.port} {info.password}",
                key=str(info.port),
            )
        count = len(shares)
        self.query_one("#status-bar", Label).update(
            f"{count} active share(s)" if count else "No active shares"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-add":
            self.action_add_share()
        elif event.button.id == "btn-fetch":
            self.action_fetch_file()
        elif event.button.id == "btn-stop":
            self.action_stop_share()

    def action_add_share(self) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            self._do_share(data["path"], data["password"], data["port"])
        self.app.push_screen(_AddShareDialog(), _on_result)

    def action_fetch_file(self) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            self._do_fetch(data["port"], data["password"], data["outfile"])
        self.app.push_screen(_FetchDialog(), _on_result)

    def action_stop_share(self) -> None:
        tbl = self.query_one("#shares-table", DataTable)
        if tbl.row_count == 0:
            return
        row = tbl.get_row_at(tbl.cursor_row)
        port = int(str(row[1]))
        self.app.manager.stop_share(port)  # type: ignore[attr-defined]
        self._reload()

    def action_refresh(self) -> None:
        self._reload()

    @work(thread=True)
    def _do_share(self, path: str, password: str | None, port: int) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            info = mgr.share(Path(path), password=password, port=port or None)
            msg = (
                f"[green]Sharing {Path(path).name} on :{info.port}  "
                f"pw: {info.password}[/green]"
            )
        except Exception as e:
            msg = f"[red]Error: {e}[/red]"
        self.app.call_from_thread(self.query_one("#status-bar", Label).update, msg)
        self.app.call_from_thread(self._reload)

    @work(thread=True)
    def _do_fetch(self, port: int, password: str, outfile: str | None) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        out = Path(outfile) if outfile else None
        try:
            result = mgr.fetch(port=port, password=password, outfile=out)
            msg = f"[green]Downloaded to: {result}[/green]"
        except Exception as e:
            msg = f"[red]Error: {e}[/red]"
        self.app.call_from_thread(self.query_one("#status-bar", Label).update, msg)
