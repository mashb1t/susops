"""Share screen — split-pane share list with modal dialogs for add/fetch."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Label, ListItem, ListView, Select, Static
from textual import work
from susops.core.ports import is_port_free, validate_port
from susops.tui.screens import compose_footer


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
    """Modal: fetch a shared file.

    Performs the download inside the dialog so the user can see progress,
    retry with corrected inputs on error, and only leaves when the fetch
    succeeds (or they cancel).  Dismisses with the downloaded Path on success.
    """

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
        error_label = self.query_one("#error", Label)
        if not port_str or not pw:
            error_label.update("Port and password are required.")
            return
        try:
            port = int(port_str)
        except ValueError:
            error_label.update("Port must be a number.")
            return
        if not validate_port(port):
            error_label.update("Port must be between 1 and 65535.")
            return
        conn_val = self.query_one("#conn", Select).value
        conn = conn_val if isinstance(conn_val, str) else None
        if not conn:
            error_label.update("Connection is required.")
            return
        outfile = self.query_one("#outfile", Input).value.strip() or None
        self._start_fetch(port, pw, conn, outfile)

    def _set_busy(self, busy: bool) -> None:
        btn = self.query_one("#btn-ok", Button)
        btn.disabled = busy
        btn.label = "Fetching…" if busy else "Fetch"
        for widget_id in ("#port", "#password", "#outfile"):
            self.query_one(widget_id, Input).disabled = busy
        self.query_one("#conn", Select).disabled = busy

    @work(thread=True)
    def _start_fetch(self, port: int, password: str, conn: str, outfile: str | None) -> None:
        self.app.call_from_thread(self._set_busy, True)
        self.app.call_from_thread(self.query_one("#error", Label).update, "[dim]Fetching…[/dim]")
        try:
            out = Path(outfile) if outfile else None
            result = self.app.manager.fetch(  # type: ignore[attr-defined]
                port=port, password=password, conn_tag=conn, outfile=out
            )
            self.app.call_from_thread(self.dismiss, result)
        except Exception as e:
            self.app.call_from_thread(self._set_busy, False)
            self.app.call_from_thread(self.query_one("#error", Label).update, f"[red]{e}[/red]")


class ShareScreen(Screen):
    """Split-pane share screen: active shares list + detail panel."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "add_share", "Add share"),
        Binding("f", "fetch_file", "Fetch"),
        Binding("d", "stop_share", "Stop"),
        Binding("s", "start_share", "Start"),
        Binding("x", "delete_share", "Delete"),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="share-split"):
            with Vertical(id="share-list-panel"):
                yield ListView(id="share-list")
            yield Static("", id="share-detail", markup=True)
        yield Label("", id="share-status")
        yield from compose_footer()

    def on_mount(self) -> None:
        lv = self.query_one("#share-list", ListView)
        lv.border_title = "Shares"
        self._shares: list = []
        self._reload()
        self.set_interval(2.0, self._reload)

    def _reload(self) -> None:
        new_shares = self.app.manager.list_shares()  # type: ignore[attr-defined]
        lv = self.query_one("#share-list", ListView)

        def _label(info) -> str:
            name = Path(info.file_path).name
            if info.running:
                dot = "[green]●[/green]"
            elif info.stopped:
                dot = "[dim]○[/dim]"
            else:
                dot = "[red]○[/red]"
            return f"{dot} {name}  {info.port}"

        new_ports = [i.port for i in new_shares]
        old_ports = [i.port for i in self._shares]

        if new_ports == old_ports:
            # Same shares — update labels in-place so selection is never disturbed
            for item, info in zip(lv.query(ListItem), new_shares):
                item.query_one(Label).update(_label(info))
        else:
            # Shares added/removed — rebuild and restore cursor
            cur = lv.index or 0
            lv.clear()
            for info in new_shares:
                lv.append(ListItem(Label(_label(info))))
            if new_shares:
                lv.index = min(cur, len(new_shares) - 1)

        self._shares = new_shares

        n_running = sum(1 for i in self._shares if i.running)
        n_stopped = sum(1 for i in self._shares if not i.running and i.stopped)
        n_offline = sum(1 for i in self._shares if not i.running and not i.stopped)
        count = len(self._shares)
        if count == 0:
            status = "No shares"
        elif n_running == count:
            status = f"{count} share(s) running"
        else:
            parts = []
            if n_running:
                parts.append(f"{n_running} running")
            if n_stopped:
                parts.append(f"{n_stopped} stopped")
            if n_offline:
                parts.append(f"{n_offline} offline")
            status = f"{count} shares  ({', '.join(parts)})"

        self.query_one("#share-status", Label).update(status)
        if self._shares:
            idx = lv.index or 0
            self._show_detail(self._shares[idx])
        else:
            self.query_one("#share-detail", Static).update(
                "[dim]No shares.\n\nPress [bold]a[/bold] to share a file.[/dim]"
            )

    def _conn_hosts(self) -> dict[str, str]:
        try:
            cfg = self.app.manager.list_config()  # type: ignore[attr-defined]
            return {c.tag: c.ssh_host for c in cfg.connections}
        except Exception:
            return {}

    def _show_detail(self, info) -> None:
        name = Path(info.file_path).name
        if info.running:
            state_str = "[green]running[/green]"
        elif info.stopped:
            state_str = "[dim]stopped[/dim]"
        else:
            state_str = "[red]offline[/red]"

        if "'" not in info.file_path:
            file_display = f"[@click=screen.open_share('{info.file_path}')]{info.file_path}[/]"
        else:
            file_display = info.file_path
        text = (
            f"[bold]File:[/bold]       {file_display}\n"
            f"[bold]Name:[/bold]       {name}\n"
            f"[bold]Connection:[/bold] {info.conn_tag or '—'}\n"
            f"[bold]Port:[/bold]       {info.port}\n"
            f"[bold]Password:[/bold]   {info.password}\n"
        )
        if info.running:
            access_str = f"[green]{info.access_count} ok[/green]"
            if info.failed_count:
                access_str += f"  [red]{info.failed_count} failed[/red]"
            text += (
                f"[bold]URL:[/bold]        {info.url}\n"
                f"[bold]State:[/bold]      {state_str}\n"
                f"[bold]Access:[/bold]     {access_str}\n"
            )
        else:
            text += f"[bold]State:[/bold]      {state_str}\n"

        if info.running:
            text += (
                f"\n[bold]Fetch command:[/bold]\n"
                f"  [dim]susops -c {info.conn_tag} fetch {info.port} {info.password}[/dim]"
                f"\n\n[dim]Press [bold]d[/bold] to stop · [bold]x[/bold] to delete[/dim]"
            )
        elif info.stopped:
            text += "\n[dim]Press [bold]s[/bold] to restart · [bold]x[/bold] to delete[/dim]"
        else:
            text += "\n[dim]Will auto-resume when connection starts · Press [bold]d[/bold] to stop · [bold]x[/bold] to delete[/dim]"
        self.query_one("#share-detail", Static).update(text)

    def action_open_share(self, file_path: str) -> None:
        parent = str(Path(file_path).parent)
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", file_path])
        else:
            subprocess.Popen(["xdg-open", parent])

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._shares):
            self._show_detail(self._shares[idx])

    def action_add_share(self) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            self._do_share(data["path"], data["conn"], data["password"], data["port"])
        self.app.push_screen(_AddShareDialog(self._conn_hosts()), _on_result)

    def action_fetch_file(self) -> None:
        def _on_result(result) -> None:
            if result is None:
                return
            self.query_one("#share-status", Label).update(
                f"[green]Downloaded to: {result}[/green]"
            )
        self.app.push_screen(_FetchDialog(self._conn_hosts()), _on_result)

    def action_stop_share(self) -> None:
        idx = self.query_one("#share-list", ListView).index
        if idx is None or idx >= len(self._shares):
            return
        port = self._shares[idx].port
        self.app.manager.stop_share(port)  # type: ignore[attr-defined]
        self._reload()

    def action_delete_share(self) -> None:
        idx = self.query_one("#share-list", ListView).index
        if idx is None or idx >= len(self._shares):
            return
        port = self._shares[idx].port
        self.app.manager.delete_share(port)  # type: ignore[attr-defined]
        self._reload()

    def action_start_share(self) -> None:
        """Restart a stopped share."""
        idx = self.query_one("#share-list", ListView).index
        if idx is None or idx >= len(self._shares):
            return
        info = self._shares[idx]
        if info.running:
            return
        self._do_share(info.file_path, info.conn_tag, info.password, info.port)

    def action_refresh(self) -> None:
        self._reload()

    @work(thread=True)
    def _do_share(self, path: str, conn_tag: str, password: str | None, port: int) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            info = mgr.share(Path(path), conn_tag, password=password, port=port or None)
            msg = f"[green]Sharing {Path(path).name} on port {info.port},  pw: {info.password}[/green]"
        except Exception as e:
            msg = f"[red]Error: {e}[/red]"
        self.app.call_from_thread(self.query_one("#share-status", Label).update, msg)
        self.app.call_from_thread(self._reload)

