"""Share screen — split-pane share list with modal dialogs for add/fetch."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static
from textual import work


class _AddShareDialog(ModalScreen):
    """Modal: start sharing a file."""

    def compose(self) -> ComposeResult:
        with Static(classes="modal-dialog"):
            yield Label("[bold]Share a file[/bold]")
            yield Label("File path:")
            yield Input(placeholder="/path/to/file", id="path")
            yield Label("Password (blank = auto-generate):")
            yield Input(placeholder="", id="password")
            yield Label("Port (0 = auto):")
            yield Input(placeholder="0", value="0", id="port")
            yield Label("", id="error", classes="modal-error")
            with Horizontal(classes="modal-btn-row"):
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


class _FetchDialog(ModalScreen):
    """Modal: fetch a shared file."""

    def compose(self) -> ComposeResult:
        with Static(classes="modal-dialog"):
            yield Label("[bold]Fetch a shared file[/bold]")
            yield Label("Port:")
            yield Input(placeholder="52100", id="port")
            yield Label("Password:")
            yield Input(placeholder="", id="password")
            yield Label("Save to (blank = ~/Downloads/<filename>):")
            yield Input(placeholder="", id="outfile")
            yield Label("", id="error", classes="modal-error")
            with Horizontal(classes="modal-btn-row"):
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
    """Split-pane share screen: active shares list + detail panel."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "add_share", "Add share"),
        Binding("f", "fetch_file", "Fetch"),
        Binding("d", "stop_share", "Stop share"),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="share-split"):
            with Vertical(id="share-list-panel"):
                yield ListView(id="share-list")
            yield Static("", id="share-detail", markup=True)
        yield Label("", id="share-status")
        yield Footer()

    def on_mount(self) -> None:
        lv = self.query_one("#share-list", ListView)
        lv.border_title = "Active Shares"
        self._shares: list = []
        self._reload()

    def _reload(self) -> None:
        self._shares = self.app.manager.list_shares()  # type: ignore[attr-defined]
        lv = self.query_one("#share-list", ListView)
        lv.clear()
        for info in self._shares:
            name = Path(info.file_path).name
            lv.append(ListItem(Label(f"[green]●[/green] {name}  :{info.port}")))
        count = len(self._shares)
        self.query_one("#share-status", Label).update(
            f"{count} active share(s)" if count else "No active shares"
        )
        if self._shares:
            self._show_detail(self._shares[0])
        else:
            self.query_one("#share-detail", Static).update(
                "[dim]No active shares.\n\nPress [bold]a[/bold] to share a file.[/dim]"
            )

    def _show_detail(self, info) -> None:
        name = Path(info.file_path).name
        fetch_cmd = f"susops fetch {info.port} {info.password}"
        text = (
            f"[bold]File:[/bold]     {info.file_path}\n"
            f"[bold]Name:[/bold]     {name}\n"
            f"[bold]Port:[/bold]     {info.port}\n"
            f"[bold]URL:[/bold]      {info.url}\n"
            f"[bold]Password:[/bold] {info.password}\n\n"
            f"[bold]Fetch command:[/bold]\n"
            f"  [dim]{fetch_cmd}[/dim]"
        )
        self.query_one("#share-detail", Static).update(text)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._shares):
            self._show_detail(self._shares[idx])

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
        idx = self.query_one("#share-list", ListView).index
        if idx is None or idx >= len(self._shares):
            return
        port = self._shares[idx].port
        self.app.manager.stop_share(port)  # type: ignore[attr-defined]
        self._reload()

    def action_refresh(self) -> None:
        self._reload()

    @work(thread=True)
    def _do_share(self, path: str, password: str | None, port: int) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            info = mgr.share(Path(path), password=password, port=port or None)
            msg = f"[green]Sharing {Path(path).name} on :{info.port}  pw: {info.password}[/green]"
        except Exception as e:
            msg = f"[red]Error: {e}[/red]"
        self.app.call_from_thread(self.query_one("#share-status", Label).update, msg)
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
        self.app.call_from_thread(self.query_one("#share-status", Label).update, msg)
