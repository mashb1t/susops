"""Connection editor screen — CRUD for connections, PAC hosts, port forwards."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    TabbedContent,
    TabPane,
)
from susops.core.config import Connection, Forwards, PortForward


class _AddConnectionDialog(Screen):
    """Modal for adding a new SSH connection."""

    def compose(self) -> ComposeResult:
        yield Container(
            Label("[bold]Add Connection[/bold]"),
            Label("Tag:"),
            Input(placeholder="e.g. work", id="tag"),
            Label("SSH host (user@host):"),
            Input(placeholder="user@hostname", id="ssh-host"),
            Label("SOCKS port (0 = auto):"),
            Input(placeholder="0", id="socks-port", value="0"),
            Button("Add", id="btn-add", variant="success"),
            Button("Cancel", id="btn-cancel"),
            id="dialog",
        )

    DEFAULT_CSS = """
    _AddConnectionDialog {
        align: center middle;
    }
    #dialog {
        width: 50;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #dialog Label {
        margin-top: 1;
    }
    #dialog Button {
        margin-top: 1;
        margin-right: 1;
    }
    """

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        tag = self.query_one("#tag", Input).value.strip()
        host = self.query_one("#ssh-host", Input).value.strip()
        port_str = self.query_one("#socks-port", Input).value.strip()
        if not tag or not host:
            return
        try:
            port = int(port_str or "0")
        except ValueError:
            port = 0
        self.dismiss({"tag": tag, "host": host, "port": port})


class _AddPacHostDialog(Screen):
    """Modal for adding a PAC host."""

    def __init__(self, connections: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._connections = connections

    def compose(self) -> ComposeResult:
        yield Container(
            Label("[bold]Add PAC Host[/bold]"),
            Label("Host / wildcard / CIDR:"),
            Input(placeholder="*.example.com or 10.0.0.0/8", id="host"),
            Label("Connection (leave blank for default):"),
            Input(placeholder=self._connections[0] if self._connections else "", id="conn"),
            Button("Add", id="btn-add", variant="success"),
            Button("Cancel", id="btn-cancel"),
            id="dialog",
        )

    DEFAULT_CSS = """
    _AddPacHostDialog {
        align: center middle;
    }
    #dialog {
        width: 50;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #dialog Label { margin-top: 1; }
    #dialog Button { margin-top: 1; margin-right: 1; }
    """

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        host = self.query_one("#host", Input).value.strip()
        conn = self.query_one("#conn", Input).value.strip() or None
        if not host:
            return
        self.dismiss({"host": host, "conn": conn})


class _AddForwardDialog(Screen):
    """Modal for adding a local or remote port forward."""

    def __init__(self, direction: str, connections: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._direction = direction
        self._connections = connections

    def compose(self) -> ComposeResult:
        yield Container(
            Label(f"[bold]Add {self._direction.capitalize()} Forward[/bold]"),
            Label("Connection tag:"),
            Input(placeholder=self._connections[0] if self._connections else "", id="conn"),
            Label("Local port:" if self._direction == "local" else "Remote port:"),
            Input(placeholder="8080", id="src-port"),
            Label("Remote port:" if self._direction == "local" else "Local port:"),
            Input(placeholder="8080", id="dst-port"),
            Label("Label (optional):"),
            Input(placeholder="", id="tag"),
            Button("Add", id="btn-add", variant="success"),
            Button("Cancel", id="btn-cancel"),
            id="dialog",
        )

    DEFAULT_CSS = """
    _AddForwardDialog {
        align: center middle;
    }
    #dialog {
        width: 50;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #dialog Label { margin-top: 1; }
    #dialog Button { margin-top: 1; margin-right: 1; }
    """

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        conn = self.query_one("#conn", Input).value.strip()
        tag = self.query_one("#tag", Input).value.strip()
        try:
            src = int(self.query_one("#src-port", Input).value.strip())
            dst = int(self.query_one("#dst-port", Input).value.strip())
        except ValueError:
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
    ConnectionEditorScreen {
        layout: vertical;
    }
    #editor-tabs {
        height: 1fr;
    }
    DataTable {
        height: 1fr;
    }
    .tab-actions {
        height: 3;
        layout: horizontal;
        padding: 0 1;
    }
    .tab-actions Button {
        margin-right: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="editor-tabs"):
            with TabPane("Connections", id="tab-connections"):
                with Vertical():
                    with Container(classes="tab-actions"):
                        yield Button("+ Add", id="btn-add-conn", variant="success")
                        yield Button("- Remove", id="btn-rm-conn", variant="error")
                    yield DataTable(id="tbl-connections", cursor_type="row")
            with TabPane("PAC Hosts", id="tab-pac"):
                with Vertical():
                    with Container(classes="tab-actions"):
                        yield Button("+ Add", id="btn-add-pac", variant="success")
                        yield Button("- Remove", id="btn-rm-pac", variant="error")
                    yield DataTable(id="tbl-pac", cursor_type="row")
            with TabPane("Local Forwards", id="tab-local"):
                with Vertical():
                    with Container(classes="tab-actions"):
                        yield Button("+ Add", id="btn-add-local", variant="success")
                        yield Button("- Remove", id="btn-rm-local", variant="error")
                    yield DataTable(id="tbl-local", cursor_type="row")
            with TabPane("Remote Forwards", id="tab-remote"):
                with Vertical():
                    with Container(classes="tab-actions"):
                        yield Button("+ Add", id="btn-add-remote", variant="success")
                        yield Button("- Remove", id="btn-rm-remote", variant="error")
                    yield DataTable(id="tbl-remote", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self._reload()

    def _setup_tables(self) -> None:
        tbl = self.query_one("#tbl-connections", DataTable)
        tbl.add_columns("Tag", "SSH Host", "SOCKS Port", "PAC Hosts", "Forwards")

        tbl = self.query_one("#tbl-pac", DataTable)
        tbl.add_columns("Host", "Connection")

        tbl = self.query_one("#tbl-local", DataTable)
        tbl.add_columns("Connection", "Local Port", "Remote Port", "Label")

        tbl = self.query_one("#tbl-remote", DataTable)
        tbl.add_columns("Connection", "Remote Port", "Local Port", "Label")

    def _reload(self) -> None:
        config = self.app.manager.list_config()  # type: ignore[attr-defined]

        tbl = self.query_one("#tbl-connections", DataTable)
        tbl.clear()
        for conn in config.connections:
            tbl.add_row(
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "btn-add-conn": self._do_add_connection,
            "btn-rm-conn": self._do_rm_connection,
            "btn-add-pac": self._do_add_pac,
            "btn-rm-pac": self._do_rm_pac,
            "btn-add-local": lambda: self._do_add_forward("local"),
            "btn-rm-local": lambda: self._do_rm_forward("local"),
            "btn-add-remote": lambda: self._do_add_forward("remote"),
            "btn-rm-remote": lambda: self._do_rm_forward("remote"),
        }
        fn = mapping.get(event.button.id or "")
        if fn:
            fn()

    # --- Connection CRUD ---

    def _do_add_connection(self) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            try:
                self.app.manager.add_connection(data["tag"], data["host"], data["port"])  # type: ignore[attr-defined]
                self._reload()
            except ValueError as e:
                self.app.notify(str(e), severity="error")

        self.app.push_screen(_AddConnectionDialog(), _on_result)

    def _do_rm_connection(self) -> None:
        tbl = self.query_one("#tbl-connections", DataTable)
        row_key = tbl.cursor_row
        if tbl.row_count == 0:
            return
        row = tbl.get_row_at(row_key)
        tag = str(row[0])
        try:
            self.app.manager.remove_connection(tag)  # type: ignore[attr-defined]
            self._reload()
        except ValueError as e:
            self.app.notify(str(e), severity="error")

    # --- PAC host CRUD ---

    def _do_add_pac(self) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            try:
                self.app.manager.add_pac_host(data["host"], conn_tag=data["conn"])  # type: ignore[attr-defined]
                self._reload()
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
            self._reload()
        except ValueError as e:
            self.app.notify(str(e), severity="error")

    # --- Port forward CRUD ---

    def _do_add_forward(self, direction: str) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            fw = PortForward(
                src_port=data["src"],
                dst_port=data["dst"],
                tag=data["tag"],
            )
            try:
                if direction == "local":
                    self.app.manager.add_local_forward(data["conn"], fw)  # type: ignore[attr-defined]
                else:
                    self.app.manager.add_remote_forward(data["conn"], fw)  # type: ignore[attr-defined]
                self._reload()
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
            self._reload()
        except ValueError as e:
            self.app.notify(str(e), severity="error")
