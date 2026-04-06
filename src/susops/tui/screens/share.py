"""Share screen — split-pane share list with modal dialogs for add/fetch."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Select, Static
from textual import work
from susops.core.ports import is_port_free, validate_port


class _AddShareDialog(ModalScreen):
    """Modal: start sharing a file."""

    def __init__(self, conn_hosts: dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._conn_hosts = conn_hosts  # tag -> ssh_host

    def compose(self) -> ComposeResult:
        options = [(tag, tag) for tag in self._conn_hosts]
        with Static(classes="modal-dialog"):
            yield Label("[bold]Share a file[/bold]")
            yield Label("File path:")
            yield Input(placeholder="/path/to/file", id="path")
            yield Label("Password (blank = auto-generate):")
            yield Input(placeholder="", id="password")
            yield Label("Port (0 = auto):")
            yield Input(placeholder="0", value="0", id="port")
            yield Label("Connection:")
            yield Select(options, allow_blank=False, id="conn")
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
        error_label = self.query_one("#error", Label)
        try:
            port = int(self.query_one("#port", Input).value.strip() or "0")
        except ValueError:
            error_label.update("Port must be a number.")
            return
        if not validate_port(port, allow_zero=True):
            error_label.update("Port must be 0 (auto) or between 1 and 65535.")
            return
        if port != 0 and not is_port_free(port):
            error_label.update(f"Port {port} is already in use.")
            return
        conn_val = self.query_one("#conn", Select).value
        conn = conn_val if isinstance(conn_val, str) else None
        self.dismiss({"path": path, "password": pw, "port": port, "conn": conn})


class _FetchDialog(ModalScreen):
    """Modal: fetch a shared file."""

    def __init__(self, conn_hosts: dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._conn_hosts = conn_hosts  # tag -> ssh_host

    def compose(self) -> ComposeResult:
        options = [(tag, tag) for tag in self._conn_hosts]
        with Static(classes="modal-dialog"):
            yield Label("[bold]Fetch a shared file[/bold]")
            yield Label("Connection:")
            yield Select(options, allow_blank=False, id="conn")
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
            self.query_one("#error", Label).update("Port must be a number.")
            return
        if not validate_port(port):
            self.query_one("#error", Label).update("Port must be between 1 and 65535.")
            return
        conn_val = self.query_one("#conn", Select).value
        host = "localhost"
        if isinstance(conn_val, str):
            raw = self._conn_hosts.get(conn_val, conn_val)
            host = raw.split("@")[-1]
        outfile = self.query_one("#outfile", Input).value.strip() or None
        self.dismiss({"port": port, "password": pw, "host": host, "outfile": outfile})


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
        #yield Header()
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

    def _conn_hosts(self) -> dict[str, str]:
        try:
            cfg = self.app.manager.list_config()  # type: ignore[attr-defined]
            return {c.tag: c.ssh_host for c in cfg.connections}
        except Exception:
            return {}

    def _show_detail(self, info) -> None:
        name = Path(info.file_path).name
        fetch_local = f"susops fetch {info.port} {info.password}"
        conn_hosts = self._conn_hosts()
        remote_lines = ""
        if conn_hosts:
            remote_lines = "\n[bold]Remote fetch (via connection):[/bold]"
            for tag, ssh_host in conn_hosts.items():
                host = ssh_host.split("@")[-1]
                remote_lines += f"\n  [dim]{tag}: susops fetch {info.port} {info.password} --host {host}[/dim]"
        text = (
            f"[bold]File:[/bold]     {info.file_path}\n"
            f"[bold]Name:[/bold]     {name}\n"
            f"[bold]Port:[/bold]     {info.port}\n"
            f"[bold]URL:[/bold]      {info.url}\n"
            f"[bold]Password:[/bold] {info.password}\n\n"
            f"[bold]Local fetch:[/bold]\n"
            f"  [dim]{fetch_local}[/dim]"
            f"{remote_lines}"
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
        self.app.push_screen(_AddShareDialog(self._conn_hosts()), _on_result)

    def action_fetch_file(self) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            self._do_fetch(data["port"], data["password"], data["host"], data["outfile"])
        self.app.push_screen(_FetchDialog(self._conn_hosts()), _on_result)

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
    def _do_fetch(self, port: int, password: str, host: str, outfile: str | None) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        out = Path(outfile) if outfile else None
        try:
            result = mgr.fetch(port=port, password=password, host=host, outfile=out)
            msg = f"[green]Downloaded to: {result}[/green]"
        except Exception as e:
            msg = f"[red]Error: {e}[/red]"
        self.app.call_from_thread(self.query_one("#share-status", Label).update, msg)
