"""Connection editor screen — CRUD for connections, PAC hosts, port forwards."""
from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen, ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
)
from susops.core.config import PortForward
from susops.core.ports import is_port_free, validate_port
from susops.core.ssh_config import get_ssh_hosts
from susops.tui.screens import compose_footer, proto_label


class _AddConnectionDialog(ModalScreen):
    """Modal for adding a new SSH connection."""

    def compose(self) -> ComposeResult:
        ssh_hosts = get_ssh_hosts()
        with Static(classes="modal-dialog"):
            yield Label("[bold]Add Connection[/bold]")
            yield Label("Tag:")
            yield Input(placeholder="e.g. work", id="tag")
            if ssh_hosts:
                yield Label("SSH host (pick from ~/.ssh/config or type below):")
                options = [(h, h) for h in ssh_hosts]
                yield Select(options, prompt="— pick from SSH config —", id="ssh-host-select", allow_blank=True)
            else:
                yield Label("SSH host (user@host):")
            yield Input(placeholder="user@hostname", id="ssh-host")
            yield Label("SOCKS port (0 = auto):")
            yield Input(placeholder="0", id="socks-port", value="0")
            yield Label("", id="error", classes="modal-error")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Add", id="btn-ok", variant="success")
                yield Button("Cancel", id="btn-cancel")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "ssh-host-select" and isinstance(event.value, str):
            self.query_one("#ssh-host", Input).value = event.value

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
        if not validate_port(port, allow_zero=True):
            error_label.update("SOCKS port must be 0 (auto) or between 1 and 65535.")
            return
        if port != 0 and not is_port_free(port):
            error_label.update(f"Port {port} is already in use.")
            return
        self.dismiss({"tag": tag, "host": host, "port": port})


class _AddPacHostDialog(ModalScreen):
    """Modal for adding a PAC host."""

    def __init__(self, connections: list[str], pac_hosts: dict[str, list[str]], **kwargs) -> None:
        super().__init__(**kwargs)
        self._connections = connections
        self._pac_hosts = pac_hosts  # {conn_tag: [host, ...]}

    def compose(self) -> ComposeResult:
        options = [(tag, tag) for tag in self._connections]
        with Static(classes="modal-dialog"):
            yield Label("[bold]Add PAC Host[/bold]")
            yield Label("Host / wildcard / CIDR:")
            yield Input(placeholder="*.example.com or 10.0.0.0/8", id="host")
            yield Label("Connection")
            yield Select(options, allow_blank=False, id="conn")
            yield Label("", id="hint", classes="modal-hint")
            yield Label("", id="error", classes="modal-error")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Add", id="btn-ok", variant="success")
                yield Button("Cancel", id="btn-cancel")

    def _update_hint(self) -> None:
        host = self.query_one("#host", Input).value.strip()
        conn_val = self.query_one("#conn", Select).value
        selected_conn = conn_val if isinstance(conn_val, str) else None
        hint = self.query_one("#hint", Label)
        if not host:
            hint.update("")
            return
        others = [tag for tag, hosts in self._pac_hosts.items() if host in hosts and tag != selected_conn]
        if others:
            hint.update(f"[yellow]Already in: {', '.join(others)}[/yellow]")
        else:
            hint.update("")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "host":
            self._update_hint()

    def on_select_changed(self, event) -> None:
        if event.select.id == "conn":
            self._update_hint()

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        host = self.query_one("#host", Input).value.strip()
        conn_val = self.query_one("#conn", Select).value
        conn = conn_val if isinstance(conn_val, str) else None
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
        conn_options = [(tag, tag) for tag in self._connections]
        bind_options = [("localhost", "localhost"), ("172.17.0.1", "172.17.0.1"), ("0.0.0.0", "0.0.0.0")]
        with Static(classes="modal-dialog"):
            yield Label(f"[bold]Add {d.capitalize()} Forward[/bold]")
            yield Label("Connection:")
            yield Select(conn_options, allow_blank=False, id="conn")
            yield Label("Label (optional):")
            yield Input(placeholder="", id="tag")
            yield Label("Forward Local Port *:" if d == "local" else "Forward Remote Port *:")
            yield Input(placeholder="8080", id="src-port")
            yield Label("To Remote Port *:" if d == "local" else "To Local Port *:")
            yield Input(placeholder="8080", id="dst-port")
            yield Label("Local Bind:" if d == "local" else "Remote Bind:")
            yield Select(bind_options, allow_blank=False, id="src-addr")
            yield Label("Remote Bind:" if d == "local" else "Local Bind:")
            yield Select(bind_options, allow_blank=False, id="dst-addr")
            yield Label("Protocol:")
            yield Checkbox("TCP", value=True, id="proto-tcp")
            yield Checkbox("UDP", value=False, id="proto-udp")
            yield Label("", id="error", classes="modal-error")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Add", id="btn-ok", variant="success")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        conn_val = self.query_one("#conn", Select).value
        conn = conn_val if isinstance(conn_val, str) else ""
        src_addr_val = self.query_one("#src-addr", Select).value
        src_addr = src_addr_val if isinstance(src_addr_val, str) else "localhost"
        dst_addr_val = self.query_one("#dst-addr", Select).value
        dst_addr = dst_addr_val if isinstance(dst_addr_val, str) else "localhost"
        tag = self.query_one("#tag", Input).value.strip()
        tcp = self.query_one("#proto-tcp", Checkbox).value
        udp = self.query_one("#proto-udp", Checkbox).value
        error_label = self.query_one(".modal-error", Label)
        if not tcp and not udp:
            error_label.update("Select at least one protocol (TCP or UDP).")
            return
        try:
            src = int(self.query_one("#src-port", Input).value.strip())
            dst = int(self.query_one("#dst-port", Input).value.strip())
        except ValueError:
            error_label.update("Ports must be valid numbers.")
            return
        if not validate_port(src) or not validate_port(dst):
            error_label.update("Ports must be between 1 and 65535.")
            return
        if self._direction == "local" and not is_port_free(src):
            error_label.update(f"Local port {src} is already in use.")
            return
        if self._direction == "remote" and not is_port_free(dst):
            error_label.update(f"Local port {dst} is already in use.")
            return
        self.dismiss({
            "conn": conn, "src": src, "dst": dst,
            "src_addr": src_addr, "dst_addr": dst_addr,
            "tag": tag, "dir": self._direction,
            "tcp": tcp, "udp": udp,
        })


class ConnectionEditorScreen(Screen):
    """TabbedContent CRUD screen for connections, PAC hosts, and port forwards."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "add_item", "Add"),
        Binding("d", "delete_item", "Delete"),
        Binding("s", "start_conn", "Start"),
        Binding("x", "stop_conn", "Stop"),
        Binding("r", "restart_conn", "Restart"),
        Binding("t", "toggle_forward", "Toggle enable"),
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
        #yield Header()
        with TabbedContent(id="editor-tabs"):
            with TabPane("Connections", id="tab-connections"):
                yield DataTable(id="tbl-connections", cursor_type="row")
            with TabPane("Domain / IP / CIDR", id="tab-pac"):
                yield DataTable(id="tbl-pac", cursor_type="row")
            with TabPane("Local Forwards", id="tab-local"):
                yield DataTable(id="tbl-local", cursor_type="row")
            with TabPane("Remote Forwards", id="tab-remote"):
                yield DataTable(id="tbl-remote", cursor_type="row")
        yield Static("", id="detail-preview")
        yield from compose_footer()

    def on_mount(self) -> None:
        self.query_one("#detail-preview", Static).border_title = "Details"
        self._setup_tables()
        self._reload()
        self.set_interval(5.0, self._bg_reload)

    def _setup_tables(self) -> None:
        tbl = self.query_one("#tbl-connections", DataTable)
        tbl.add_columns("Status", "Tag", "SSH Host", "SOCKS Port", "Domains", "Forwards")

        tbl = self.query_one("#tbl-pac", DataTable)
        tbl.add_columns("Host", "Connection")

        tbl = self.query_one("#tbl-local", DataTable)
        tbl.add_columns("", "Connection", "Local Port", "Local Bind", "Remote Port", "Remote Bind", "Protocol", "Label")

        tbl = self.query_one("#tbl-remote", DataTable)
        tbl.add_columns("", "Connection", "Remote Port", "Remote Bind", "Local Port", "Local Bind", "Protocol", "Label")

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
        """Repopulate all tables, preserving each table's cursor position."""
        if status_map is None:
            try:
                status_result = self.app.manager.status()  # type: ignore[attr-defined]
                status_map = {cs.tag: cs.running for cs in status_result.connection_statuses}
            except Exception:
                status_map = {}

        config = self.app.manager.list_config()  # type: ignore[attr-defined]

        tbl = self.query_one("#tbl-connections", DataTable)
        cur = tbl.cursor_row
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
        if tbl.row_count:
            tbl.move_cursor(row=min(cur, tbl.row_count - 1))

        tbl = self.query_one("#tbl-pac", DataTable)
        cur = tbl.cursor_row
        tbl.clear()
        for conn in config.connections:
            for host in conn.pac_hosts:
                tbl.add_row(host, conn.tag, key=f"{conn.tag}:{host}")
        if tbl.row_count:
            tbl.move_cursor(row=min(cur, tbl.row_count - 1))

        tbl = self.query_one("#tbl-local", DataTable)
        cur = tbl.cursor_row
        tbl.clear()
        for conn in config.connections:
            for fw in conn.forwards.local:
                dot = "[green]●[/green]" if fw.enabled else "[dim]○[/dim]"
                tbl.add_row(
                    dot, conn.tag, str(fw.src_port), fw.src_addr,
                    str(fw.dst_port), fw.dst_addr, proto_label(fw), fw.tag or "",
                    key=f"{conn.tag}:L:{fw.src_port}",
                )
        if tbl.row_count:
            tbl.move_cursor(row=min(cur, tbl.row_count - 1))

        tbl = self.query_one("#tbl-remote", DataTable)
        cur = tbl.cursor_row
        tbl.clear()
        for conn in config.connections:
            for fw in conn.forwards.remote:
                dot = "[green]●[/green]" if fw.enabled else "[dim]○[/dim]"
                tbl.add_row(
                    dot, conn.tag, str(fw.src_port), fw.src_addr,
                    str(fw.dst_port), fw.dst_addr, proto_label(fw), fw.tag or "",
                    key=f"{conn.tag}:R:{fw.src_port}",
                )
        if tbl.row_count:
            tbl.move_cursor(row=min(cur, tbl.row_count - 1))

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
                        f"  |  Domains: {len(conn.pac_hosts)}"
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

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        active = self.query_one("#editor-tabs", TabbedContent).active
        if action == "toggle_forward":
            return active in ("tab-local", "tab-remote")
        if action in ("start_conn", "stop_conn", "restart_conn"):
            return active == "tab-connections"
        return True

    def _selected_conn_tag(self) -> str | None:
        tbl = self.query_one("#tbl-connections", DataTable)
        if tbl.row_count == 0:
            return None
        try:
            return str(tbl.get_row_at(tbl.cursor_row)[1])
        except (IndexError, Exception):
            return None

    def action_start_conn(self) -> None:
        if tag := self._selected_conn_tag():
            self._run_conn_action("start", tag)

    def action_stop_conn(self) -> None:
        if tag := self._selected_conn_tag():
            self._run_conn_action("stop", tag)

    def action_restart_conn(self) -> None:
        if tag := self._selected_conn_tag():
            self._run_conn_action("restart", tag)

    @work(thread=True)
    def _run_conn_action(self, action: str, tag: str) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        if action == "start":
            mgr.start(tag=tag)
        elif action == "stop":
            mgr.stop(tag=tag)
        elif action == "restart":
            mgr.restart(tag=tag)
        self.app.call_from_thread(self._bg_reload)

    def action_toggle_forward(self) -> None:
        """Toggle enabled on the currently selected forward row."""
        active = self.query_one("#editor-tabs", TabbedContent).active
        if active == "tab-local":
            direction = "local"
            tbl_id = "#tbl-local"
        elif active == "tab-remote":
            direction = "remote"
            tbl_id = "#tbl-remote"
        else:
            return
        tbl = self.query_one(tbl_id, DataTable)
        if tbl.row_count == 0:
            return
        try:
            row = tbl.get_row_at(tbl.cursor_row)
            conn_tag = str(row[1])
            src_port = int(str(row[2]))
        except (IndexError, ValueError):
            return
        try:
            new_state = self.app.manager.toggle_forward_enabled(conn_tag, src_port, direction)  # type: ignore[attr-defined]
            self.notify(
                f"Forward {src_port} {'enabled' if new_state else 'disabled'}",
                timeout=2,
            )
            self._bg_reload()
        except Exception as exc:
            self.notify(str(exc), severity="error", timeout=3)

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

        config = self.app.manager.list_config()  # type: ignore[attr-defined]
        pac_hosts = {c.tag: list(c.pac_hosts) for c in config.connections}
        self.app.push_screen(_AddPacHostDialog(self._conn_tags(), pac_hosts), _on_result)

    def _do_rm_pac(self) -> None:
        tbl = self.query_one("#tbl-pac", DataTable)
        if tbl.row_count == 0:
            return
        row = tbl.get_row_at(tbl.cursor_row)
        host = str(row[0])
        conn_tag = str(row[1])
        try:
            self.app.manager.remove_pac_host(host, conn_tag=conn_tag)  # type: ignore[attr-defined]
            self._bg_reload()
        except ValueError as e:
            self.app.notify(str(e), severity="error")

    # --- Port forward CRUD ---

    def _do_add_forward(self, direction: str) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            fw = PortForward(
                src_addr=data["src_addr"],
                src_port=data["src"],
                dst_addr=data["dst_addr"],
                dst_port=data["dst"],
                tag=data["tag"],
                tcp=data["tcp"],
                udp=data["udp"],
            )
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
        port = int(str(row[2]))
        try:
            if direction == "local":
                self.app.manager.remove_local_forward(port)  # type: ignore[attr-defined]
            else:
                self.app.manager.remove_remote_forward(port)  # type: ignore[attr-defined]
            self._bg_reload()
        except ValueError as e:
            self.app.notify(str(e), severity="error")
