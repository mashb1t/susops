"""Connection editor screen — CRUD for connections, PAC hosts, port forwards."""
from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen, ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)
from susops.core.config import PortForward


class _AddConnectionDialog(ModalScreen):
    """Modal for adding a new SSH connection."""

    def compose(self) -> ComposeResult:
        with Static(classes="modal-dialog"):
            yield Label("[bold]Add Connection[/bold]")
            yield Label("Tag:")
            yield Input(placeholder="e.g. work", id="tag")
            yield Label("SSH host (user@host):")
            yield Input(placeholder="user@hostname", id="ssh-host")
            yield Label("SOCKS port (0 = auto):")
            yield Input(placeholder="0", id="socks-port", value="0")
            yield Label("", id="error", classes="modal-error")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Add", id="btn-add", variant="success")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        tag = self.query_one("#tag", Input).value.strip()
        host = self.query_one("#ssh-host", Input).value.strip()
        port_str = self.query_one("#socks-port", Input).value.strip()
        error_label = self.query_one(".modal-error", Label)
        if not tag:
            error_label.update("Tag is required.")
            return
        if not host:
            error_label.update("SSH host is required.")
            return
        try:
            port = int(port_str or "0")
        except ValueError:
            error_label.update("SOCKS port must be a number.")
            return
        self.dismiss({"tag": tag, "host": host, "port": port})


class _AddPacHostDialog(ModalScreen):
    """Modal for adding a PAC host."""

    def __init__(self, connections: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._connections = connections

    def compose(self) -> ComposeResult:
        with Static(classes="modal-dialog"):
            yield Label("[bold]Add PAC Host[/bold]")
            yield Label("Host / wildcard / CIDR:")
            yield Input(placeholder="*.example.com or 10.0.0.0/8", id="host")
            yield Label("Connection (leave blank for default):")
            yield Input(placeholder=self._connections[0] if self._connections else "", id="conn")
            yield Label("", id="error", classes="modal-error")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Add", id="btn-add", variant="success")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        host = self.query_one("#host", Input).value.strip()
        conn = self.query_one("#conn", Input).value.strip() or None
        if not host:
            self.query_one(".modal-error", Label).update("Host pattern is required.")
            return
        self.dismiss({"host": host, "conn": conn})


class _AddForwardDialog(ModalScreen):
    """Modal for adding a local or remote port forward."""

    def __init__(self, direction: str, connections: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._direction = direction
        self._connections = connections

    def compose(self) -> ComposeResult:
        d = self._direction
        with Static(classes="modal-dialog"):
            yield Label(f"[bold]Add {d.capitalize()} Forward[/bold]")
            yield Label("Connection tag:")
            yield Input(placeholder=self._connections[0] if self._connections else "", id="conn")
            yield Label("Local port:" if d == "local" else "Remote port:")
            yield Input(placeholder="8080", id="src-port")
            yield Label("Remote port:" if d == "local" else "Local port:")
            yield Input(placeholder="8080", id="dst-port")
            yield Label("Label (optional):")
            yield Input(placeholder="", id="tag")
            yield Label("", id="error", classes="modal-error")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Add", id="btn-add", variant="success")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        conn = self.query_one("#conn", Input).value.strip()
        tag = self.query_one("#tag", Input).value.strip()
        error_label = self.query_one(".modal-error", Label)
        try:
            src = int(self.query_one("#src-port", Input).value.strip())
            dst = int(self.query_one("#dst-port", Input).value.strip())
        except ValueError:
            error_label.update("Local and remote ports must be valid numbers.")
            return
        self.dismiss({"conn": conn, "src": src, "dst": dst, "tag": tag, "dir": self._direction})


class ConnectionEditorScreen(Screen):
    """TabbedContent CRUD screen for connections, PAC hosts, and port forwards."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "add_item", "Add"),
        Binding("d", "delete_item", "Delete"),
    ]

    DEFAULT_CSS = """
    ConnectionEditorScreen { layout: vertical; }
    #editor-tabs { height: 1fr; }
    DataTable { height: 1fr; }
    #detail-preview {
        height: 6;
        border: round $primary-darken-1;
        border-title-align: left;
        padding: 0 1;
        margin: 0 1 1 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="editor-tabs"):
            with TabPane("Connections", id="tab-connections"):
                yield DataTable(id="tbl-connections", cursor_type="row")
            with TabPane("PAC Hosts", id="tab-pac"):
                yield DataTable(id="tbl-pac", cursor_type="row")
            with TabPane("Local Forwards", id="tab-local"):
                yield DataTable(id="tbl-local", cursor_type="row")
            with TabPane("Remote Forwards", id="tab-remote"):
                yield DataTable(id="tbl-remote", cursor_type="row")
        yield Static("", id="detail-preview")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#detail-preview", Static).border_title = "Details"
        self._setup_tables()
        self._reload()
        self.set_interval(5.0, self._bg_reload)

    def _setup_tables(self) -> None:
        tbl = self.query_one("#tbl-connections", DataTable)
        tbl.add_columns("Status", "Tag", "SSH Host", "SOCKS Port", "PAC Hosts", "Forwards")

        tbl = self.query_one("#tbl-pac", DataTable)
        tbl.add_columns("Host", "Connection")

        tbl = self.query_one("#tbl-local", DataTable)
        tbl.add_columns("Connection", "Local Port", "Remote Port", "Label")

        tbl = self.query_one("#tbl-remote", DataTable)
        tbl.add_columns("Connection", "Remote Port", "Local Port", "Label")

    @work(thread=True)
    def _bg_reload(self) -> None:
        """Background status refresh — fetches live tunnel state then updates UI."""
        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            status_result = mgr.status()
            status_map = {cs.tag: cs.running for cs in status_result.connection_statuses}
        except Exception:
            status_map = {}
        self.app.call_from_thread(self._reload, status_map)

    def _reload(self, status_map: dict | None = None) -> None:
        """Repopulate all tables. Pass a pre-fetched status_map to avoid blocking."""
        if status_map is None:
            try:
                status_result = self.app.manager.status()  # type: ignore[attr-defined]
                status_map = {cs.tag: cs.running for cs in status_result.connection_statuses}
            except Exception:
                status_map = {}

        config = self.app.manager.list_config()  # type: ignore[attr-defined]

        tbl = self.query_one("#tbl-connections", DataTable)
        tbl.clear()
        for conn in config.connections:
            running = status_map.get(conn.tag, False)
            dot = "[green]●[/green]" if running else "[red]○[/red]"
            tbl.add_row(
                dot,
                conn.tag,
                conn.ssh_host,
                str(conn.socks_proxy_port) if conn.socks_proxy_port else "auto",
                str(len(conn.pac_hosts)),
                str(len(conn.forwards.local) + len(conn.forwards.remote)),
                key=conn.tag,
            )

        tbl = self.query_one("#tbl-pac", DataTable)
        tbl.clear()
        for conn in config.connections:
            for host in conn.pac_hosts:
                tbl.add_row(host, conn.tag, key=f"{conn.tag}:{host}")

        tbl = self.query_one("#tbl-local", DataTable)
        tbl.clear()
        for conn in config.connections:
            for fw in conn.forwards.local:
                tbl.add_row(
                    conn.tag, str(fw.src_port), str(fw.dst_port), fw.tag or "",
                    key=f"{conn.tag}:L:{fw.src_port}",
                )

        tbl = self.query_one("#tbl-remote", DataTable)
        tbl.clear()
        for conn in config.connections:
            for fw in conn.forwards.remote:
                tbl.add_row(
                    conn.tag, str(fw.src_port), str(fw.dst_port), fw.tag or "",
                    key=f"{conn.tag}:R:{fw.src_port}",
                )

    def _conn_tags(self) -> list[str]:
        return [c.tag for c in self.app.manager.list_config().connections]  # type: ignore[attr-defined]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        active = self.query_one("#editor-tabs", TabbedContent).active
        preview = self.query_one("#detail-preview", Static)

        try:
            config = self.app.manager.list_config()  # type: ignore[attr-defined]
            conn_map = {c.tag: c for c in config.connections}
        except Exception:
            preview.update("")
            return

        if active == "tab-connections":
            tbl = self.query_one("#tbl-connections", DataTable)
            if tbl.row_count == 0:
                preview.update("")
                return
            try:
                row = tbl.get_row_at(tbl.cursor_row)
                tag = str(row[1])
                conn = conn_map.get(tag)
                if conn:
                    preview.update(
                        f"SSH host: {conn.ssh_host}  |  Port: {conn.socks_proxy_port or 'auto'}"
                        f"  |  PAC hosts: {len(conn.pac_hosts)}"
                        f"  |  Forwards: {len(conn.forwards.local) + len(conn.forwards.remote)}"
                    )
                else:
                    preview.update(f"Tag: {tag}")
            except Exception:
                preview.update("")

        elif active == "tab-pac":
            tbl = self.query_one("#tbl-pac", DataTable)
            if tbl.row_count == 0:
                preview.update("")
                return
            try:
                row = tbl.get_row_at(tbl.cursor_row)
                preview.update(f"Pattern: {row[0]}  |  Connection: {row[1]}")
            except Exception:
                preview.update("")

        elif active in ("tab-local", "tab-remote"):
            tbl_id = "#tbl-local" if active == "tab-local" else "#tbl-remote"
            tbl = self.query_one(tbl_id, DataTable)
            if tbl.row_count == 0:
                preview.update("")
                return
            try:
                row = tbl.get_row_at(tbl.cursor_row)
                preview.update(f"Connection: {row[0]}  |  Port: {row[1]}")
            except Exception:
                preview.update("")
        else:
            preview.update("")

    def action_add_item(self) -> None:
        active = self.query_one("#editor-tabs", TabbedContent).active
        if active == "tab-connections":
            self._do_add_connection()
        elif active == "tab-pac":
            self._do_add_pac()
        elif active == "tab-local":
            self._do_add_forward("local")
        elif active == "tab-remote":
            self._do_add_forward("remote")

    def action_delete_item(self) -> None:
        active = self.query_one("#editor-tabs", TabbedContent).active
        if active == "tab-connections":
            self._do_rm_connection()
        elif active == "tab-pac":
            self._do_rm_pac()
        elif active == "tab-local":
            self._do_rm_forward("local")
        elif active == "tab-remote":
            self._do_rm_forward("remote")

    # --- Connection CRUD ---

    def _do_add_connection(self) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            try:
                self.app.manager.add_connection(data["tag"], data["host"], data["port"])  # type: ignore[attr-defined]
                self._bg_reload()
            except ValueError as e:
                self.app.notify(str(e), severity="error")

        self.app.push_screen(_AddConnectionDialog(), _on_result)

    def _do_rm_connection(self) -> None:
        tbl = self.query_one("#tbl-connections", DataTable)
        if tbl.row_count == 0:
            return
        row = tbl.get_row_at(tbl.cursor_row)
        tag = str(row[1])
        try:
            self.app.manager.remove_connection(tag)  # type: ignore[attr-defined]
            self._bg_reload()
        except ValueError as e:
            self.app.notify(str(e), severity="error")

    # --- PAC host CRUD ---

    def _do_add_pac(self) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            try:
                self.app.manager.add_pac_host(data["host"], conn_tag=data["conn"])  # type: ignore[attr-defined]
                self._bg_reload()
            except ValueError as e:
                self.app.notify(str(e), severity="error")

        self.app.push_screen(_AddPacHostDialog(self._conn_tags()), _on_result)

    def _do_rm_pac(self) -> None:
        tbl = self.query_one("#tbl-pac", DataTable)
        if tbl.row_count == 0:
            return
        row = tbl.get_row_at(tbl.cursor_row)
        host = str(row[0])
        try:
            self.app.manager.remove_pac_host(host)  # type: ignore[attr-defined]
            self._bg_reload()
        except ValueError as e:
            self.app.notify(str(e), severity="error")

    # --- Port forward CRUD ---

    def _do_add_forward(self, direction: str) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            fw = PortForward(src_port=data["src"], dst_port=data["dst"], tag=data["tag"])
            try:
                if direction == "local":
                    self.app.manager.add_local_forward(data["conn"], fw)  # type: ignore[attr-defined]
                else:
                    self.app.manager.add_remote_forward(data["conn"], fw)  # type: ignore[attr-defined]
                self._bg_reload()
            except ValueError as e:
                self.app.notify(str(e), severity="error")

        self.app.push_screen(_AddForwardDialog(direction, self._conn_tags()), _on_result)

    def _do_rm_forward(self, direction: str) -> None:
        tbl_id = "#tbl-local" if direction == "local" else "#tbl-remote"
        tbl = self.query_one(tbl_id, DataTable)
        if tbl.row_count == 0:
            return
        row = tbl.get_row_at(tbl.cursor_row)
        port = int(str(row[1]))
        try:
            if direction == "local":
                self.app.manager.remove_local_forward(port)  # type: ignore[attr-defined]
            else:
                self.app.manager.remove_remote_forward(port)  # type: ignore[attr-defined]
            self._bg_reload()
        except ValueError as e:
            self.app.notify(str(e), severity="error")
