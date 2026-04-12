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
from susops.tui.screens import compose_footer, fmt_bps, fmt_bytes, proto_label, status_dot


def _fw_dot(mgr, fw: PortForward, conn_tag: str, direction: str, conn_running: bool) -> str:
    """Return the status dot for a port forward, handling TCP+UDP partial state."""
    if not fw.enabled:
        return "─"
    if not conn_running:
        return "[red]○[/red]"
    # Connection is running: TCP is up (via master). For TCP+UDP, also check UDP.
    if fw.tcp and fw.udp:
        try:
            udp_up = mgr.is_udp_forward_running(conn_tag, fw.src_port, direction)
        except Exception:
            udp_up = False
        partial = not udp_up  # TCP up, UDP down
        return status_dot(running=True, partial=partial)
    return "[green]●[/green]"


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
        self.dismiss({
            "conn": conn, "src": src, "dst": dst,
            "src_addr": src_addr, "dst_addr": dst_addr,
            "tag": tag, "dir": self._direction,
            "tcp": tcp, "udp": udp,
        })


class _TestResultsDialog(ModalScreen):
    """Modal: shows test results for a connection, domain, or forward."""

    BINDINGS = [Binding("escape,q", "dismiss", "Close")]

    def __init__(self, title: str, lines: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Static(classes="modal-dialog"):
            yield Label(f"[bold]{self._title}[/bold]")
            yield Static("\n".join(self._lines), id="test-results", markup=True)
            with Horizontal(classes="modal-btn-row"):
                yield Button("Close", id="btn-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class ConnectionsScreen(Screen):
    """TabbedContent CRUD screen for connections, PAC hosts, and port forwards."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "add_item", "Add"),
        Binding("d", "delete_item", "Delete"),
        Binding("t", "toggle_enabled", "Toggle"),
        Binding("e", "test_item", "Test"),
        Binding("s", "start_item", "Start"),
        Binding("x", "stop_item", "Stop"),
        Binding("r", "restart_item", "Restart"),  # connections only
    ]

    DEFAULT_CSS = """
    ConnectionEditorScreen { layout: vertical; }
    #editor-tabs { height: 1fr; }
    DataTable { height: 1fr; }
    #detail-preview {
        height: 7;
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
        tbl.add_columns("Status", "Host", "Connection")

        tbl = self.query_one("#tbl-local", DataTable)
        tbl.add_columns("Status", "Connection", "Local Port", "Local Bind", "Remote Port", "Remote Bind", "Protocol", "Label")

        tbl = self.query_one("#tbl-remote", DataTable)
        tbl.add_columns("Status", "Connection", "Remote Port", "Remote Bind", "Local Port", "Local Bind", "Protocol", "Label")

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

        mgr = self.app.manager  # type: ignore[attr-defined]
        config = mgr.list_config()

        tbl = self.query_one("#tbl-connections", DataTable)
        cur = tbl.cursor_row
        tbl.clear()
        for conn in config.connections:
            running = status_map.get(conn.tag, False)
            dot = status_dot(running, conn.enabled)
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
            conn_running = status_map.get(conn.tag, False)
            for host in conn.pac_hosts:
                dot = status_dot(conn_running)
                tbl.add_row(dot, host, conn.tag, key=f"{conn.tag}:{host}:on")
            for host in conn.pac_hosts_disabled:
                tbl.add_row("─", host, conn.tag, key=f"{conn.tag}:{host}:off")
        if tbl.row_count:
            tbl.move_cursor(row=min(cur, tbl.row_count - 1))

        tbl = self.query_one("#tbl-local", DataTable)
        cur = tbl.cursor_row
        tbl.clear()
        for conn in config.connections:
            conn_running = status_map.get(conn.tag, False)
            for fw in conn.forwards.local:
                dot = _fw_dot(mgr, fw, conn.tag, "local", conn_running)
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
            conn_running = status_map.get(conn.tag, False)
            for fw in conn.forwards.remote:
                dot = _fw_dot(mgr, fw, conn.tag, "remote", conn_running)
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
            mgr = self.app.manager  # type: ignore[attr-defined]
            config = mgr.list_config()
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
                if not conn:
                    preview.update("")
                    return
                rx, tx = mgr.get_bandwidth(tag)
                rx_total, tx_total = mgr.get_bandwidth_totals(tag)
                uptime = mgr.get_uptime(tag)
                port = conn.socks_proxy_port or "auto"
                enabled_local = sum(1 for f in conn.forwards.local if f.enabled)
                enabled_remote = sum(1 for f in conn.forwards.remote if f.enabled)
                total_fwd = len(conn.forwards.local) + len(conn.forwards.remote)
                enabled_fwd = enabled_local + enabled_remote
                uptime_str = f"{int(uptime)}s" if uptime and uptime < 60 else (f"{int(uptime // 60)}m{int(uptime % 60)}s" if uptime else "—")
                lines = [
                    f"[bold]{conn.ssh_host}[/bold]   SOCKS ::{port}   up {uptime_str}",
                    f"[green]↓[/green] {fmt_bps(rx):>8}  total [cyan]{fmt_bytes(rx_total)}[/cyan]   "
                    f"[yellow]↑[/yellow] {fmt_bps(tx):>8}  total [cyan]{fmt_bytes(tx_total)}[/cyan]",
                    f"Forwards  {enabled_fwd}/{total_fwd} enabled  "
                    f"({enabled_local} local · {enabled_remote} remote)   "
                    f"PAC domains  {len(conn.pac_hosts)}",
                    f"Proxy  curl --proxy socks5h://127.0.0.1:{port} http://example.com",
                ]
                preview.update("\n".join(lines))
            except Exception:
                preview.update("")

        elif active == "tab-pac":
            tbl = self.query_one("#tbl-pac", DataTable)
            if tbl.row_count == 0:
                preview.update("")
                return
            try:
                row = tbl.get_row_at(tbl.cursor_row)
                pattern, conn_tag = str(row[1]), str(row[2])
                conn = conn_map.get(conn_tag)
                port = conn.socks_proxy_port if conn else "?"
                example = pattern.lstrip("*.")
                lines = [
                    f"Pattern     [bold]{pattern}[/bold]",
                    f"Connection  {conn_tag}",
                    f"Proxy       SOCKS5 127.0.0.1:{port}",
                    f"Example     curl --proxy socks5h://127.0.0.1:{port} http://{example}",
                ]
                preview.update("\n".join(lines))
            except Exception:
                preview.update("")

        elif active in ("tab-local", "tab-remote"):
            direction = "local" if active == "tab-local" else "remote"
            tbl_id = f"#tbl-{direction}"
            tbl = self.query_one(tbl_id, DataTable)
            if tbl.row_count == 0:
                preview.update("")
                return
            try:
                row = tbl.get_row_at(tbl.cursor_row)
                # row: Status, Connection, src_port, src_addr, dst_port, dst_addr, Protocol, Label
                conn_tag = str(row[1])
                src_port, src_addr = int(str(row[2])), str(row[3])
                dst_port, dst_addr = int(str(row[4])), str(row[5])
                proto = str(row[6])
                label = str(row[7])
                conn = conn_map.get(conn_tag)

                enabled_fwds = (conn.forwards.local if direction == "local" else conn.forwards.remote) if conn else []
                fw = next((f for f in enabled_fwds if f.src_port == src_port), None)
                enabled = fw.enabled if fw else False
                state = "[green]enabled[/green]" if enabled else "[dim]disabled[/dim]"

                port_free = is_port_free(src_port)
                port_status = "[green]✓ free[/green]" if port_free else "[red]✗ in use[/red]"

                fwd_spec = f"{src_addr}:{src_port}:{dst_addr}:{dst_port}"
                flag = "-L" if direction == "local" else "-R"

                lines = [
                    f"[bold]{label or conn_tag}[/bold]   {state}   {proto}   via {conn_tag}",
                    f"Bind     {src_addr}:{src_port}  →  {dst_addr}:{dst_port}",
                    f"Port {src_port}  {port_status}",
                    f"Forward  ssh -O forward {flag} {fwd_spec}",
                ]
                preview.update("\n".join(lines))
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
        on_forward = active in ("tab-local", "tab-remote")
        on_conn = active == "tab-connections"
        if action in ("start_item", "stop_item"):
            return on_conn or on_forward
        if action == "restart_item":
            return on_conn  # restart not applicable to forwards
        if action == "toggle_enabled":
            return active in ("tab-connections", "tab-pac", "tab-local", "tab-remote")
        return True

    def _selected_conn_tag(self) -> str | None:
        tbl = self.query_one("#tbl-connections", DataTable)
        if tbl.row_count == 0:
            return None
        try:
            return str(tbl.get_row_at(tbl.cursor_row)[1])
        except (IndexError, Exception):
            return None

    def _selected_forward(self, direction: str) -> tuple[str, int] | None:
        tbl_id = "#tbl-local" if direction == "local" else "#tbl-remote"
        tbl = self.query_one(tbl_id, DataTable)
        if tbl.row_count == 0:
            return None
        try:
            row = tbl.get_row_at(tbl.cursor_row)
            return str(row[1]), int(str(row[2]))
        except (IndexError, ValueError):
            return None

    def action_start_item(self) -> None:
        active = self.query_one("#editor-tabs", TabbedContent).active
        if active == "tab-connections":
            if tag := self._selected_conn_tag():
                self._run_conn_op("start", tag)
        elif active in ("tab-local", "tab-remote"):
            direction = "local" if active == "tab-local" else "remote"
            if sel := self._selected_forward(direction):
                self._run_forward_op("start", sel[0], sel[1], direction)

    def action_stop_item(self) -> None:
        active = self.query_one("#editor-tabs", TabbedContent).active
        if active == "tab-connections":
            if tag := self._selected_conn_tag():
                self._run_conn_op("stop", tag)
        elif active in ("tab-local", "tab-remote"):
            direction = "local" if active == "tab-local" else "remote"
            if sel := self._selected_forward(direction):
                self._run_forward_op("stop", sel[0], sel[1], direction)

    def action_restart_item(self) -> None:
        active = self.query_one("#editor-tabs", TabbedContent).active
        if active == "tab-connections":
            if tag := self._selected_conn_tag():
                self._run_conn_op("restart", tag)
        elif active in ("tab-local", "tab-remote"):
            direction = "local" if active == "tab-local" else "remote"
            if sel := self._selected_forward(direction):
                self._run_forward_op("restart", sel[0], sel[1], direction)

    def action_test_item(self) -> None:
        active = self.query_one("#editor-tabs", TabbedContent).active
        if active == "tab-connections":
            if tag := self._selected_conn_tag():
                self._run_test_conn(tag)
        elif active == "tab-pac":
            tbl = self.query_one("#tbl-pac", DataTable)
            if tbl.row_count == 0:
                return
            try:
                row = tbl.get_row_at(tbl.cursor_row)
                host, conn_tag = str(row[1]), str(row[2])
                self._run_test_domain(host, conn_tag)
            except (IndexError, ValueError):
                pass
        elif active in ("tab-local", "tab-remote"):
            direction = "local" if active == "tab-local" else "remote"
            if sel := self._selected_forward(direction):
                self._run_test_forward(sel[0], sel[1], direction)

    @work(thread=True)
    def _run_test_conn(self, conn_tag: str) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        config = mgr.list_config()
        conn = next((c for c in config.connections if c.tag == conn_tag), None)
        ssh_host = conn.ssh_host if conn else conn_tag
        result = mgr.test_connection(conn_tag)
        if result.success:
            dot = "[green]●[/green]"
            msg = f"{result.message}  [dim]{result.latency_ms:.0f}ms[/dim]"
        else:
            dot = "[red]○[/red]"
            msg = result.message
        lines = [
            f"[dim]SSH host:[/dim]  {ssh_host}",
            f"{dot}  {msg}",
        ]
        self.app.call_from_thread(
            self.app.push_screen, _TestResultsDialog(f"Test: {conn_tag}", lines)
        )

    @work(thread=True)
    def _run_test_domain(self, host: str, conn_tag: str) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        result = mgr.test_domain(host, conn_tag)
        if result.success:
            dot = "[green]●[/green]"
            msg = f"{result.message}  [dim]{result.latency_ms:.0f}ms[/dim]"
        else:
            dot = "[red]○[/red]"
            msg = result.message
        lines = [
            f"[dim]Connection:[/dim] {conn_tag}",
            f"[dim]Host:[/dim]       {host}",
            f"{dot}  {msg}",
        ]
        self.app.call_from_thread(
            self.app.push_screen, _TestResultsDialog(f"Test: {host}", lines)
        )

    @work(thread=True)
    def _run_test_forward(self, conn_tag: str, src_port: int, direction: str) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            results = mgr.test_forward(conn_tag, src_port, direction)
        except Exception as exc:
            self.app.call_from_thread(
                self.app.push_screen,
                _TestResultsDialog(f"Test: {direction} {src_port}", [f"[red]{exc}[/red]"]),
            )
            return
        lines: list[str] = [f"[dim]Connection:[/dim]  {conn_tag}",
                             f"[dim]Direction:[/dim]   {direction}  port {src_port}"]
        for proto, ok in results.items():
            dot = "[green]●[/green]" if ok else "[red]○[/red]"
            if proto == "tcp":
                detail = "port bound" if ok else "port not bound"
                if direction == "remote":
                    detail = "master socket alive" if ok else "master socket dead"
            else:
                detail = "socat process running" if ok else "socat process not running"
            lines.append(f"{dot}  {proto.upper()}  {detail}")
        self.app.call_from_thread(
            self.app.push_screen, _TestResultsDialog(f"Test: {direction} :{src_port}", lines)
        )

    def action_toggle_enabled(self) -> None:
        active = self.query_one("#editor-tabs", TabbedContent).active
        if active == "tab-connections":
            if tag := self._selected_conn_tag():
                self._run_toggle_conn(tag)
        elif active == "tab-pac":
            tbl = self.query_one("#tbl-pac", DataTable)
            if tbl.row_count == 0:
                return
            try:
                row = tbl.get_row_at(tbl.cursor_row)
                host, conn_tag = str(row[1]), str(row[2])
                self._run_toggle_pac(host, conn_tag)
            except (IndexError, ValueError):
                pass
        elif active in ("tab-local", "tab-remote"):
            direction = "local" if active == "tab-local" else "remote"
            if sel := self._selected_forward(direction):
                self._run_toggle_forward(sel[0], sel[1], direction)

    @work(thread=True)
    def _run_toggle_conn(self, tag: str) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        config = mgr.list_config()
        conn = next((c for c in config.connections if c.tag == tag), None)
        if conn:
            try:
                mgr.set_connection_enabled(tag, not conn.enabled)
            except Exception as exc:
                self.app.call_from_thread(self.notify, str(exc), severity="error", timeout=3)
        self.app.call_from_thread(self._bg_reload)

    @work(thread=True)
    def _run_toggle_pac(self, host: str, conn_tag: str) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        config = mgr.list_config()
        conn = next((c for c in config.connections if c.tag == conn_tag), None)
        if conn:
            currently_enabled = host in conn.pac_hosts
            try:
                mgr.set_pac_host_enabled(host, not currently_enabled, conn_tag=conn_tag)
            except Exception as exc:
                self.app.call_from_thread(self.notify, str(exc), severity="error", timeout=3)
        self.app.call_from_thread(self._bg_reload)

    @work(thread=True)
    def _run_toggle_forward(self, conn_tag: str, src_port: int, direction: str) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            mgr.toggle_forward_enabled(conn_tag, src_port, direction)
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error", timeout=3)
        self.app.call_from_thread(self._bg_reload)

    @work(thread=True)
    def _run_conn_op(self, op: str, tag: str) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        if op == "start":
            mgr.start(tag=tag)
        elif op == "stop":
            mgr.stop(tag=tag)
        elif op == "restart":
            mgr.restart(tag=tag)
        self.app.call_from_thread(self._bg_reload)

    @work(thread=True)
    def _run_forward_op(self, op: str, conn_tag: str, src_port: int, direction: str) -> None:
        mgr = self.app.manager  # type: ignore[attr-defined]
        try:
            if op == "start":
                mgr.set_forward_enabled(conn_tag, src_port, direction, True)
            elif op == "stop":
                mgr.set_forward_enabled(conn_tag, src_port, direction, False)
            elif op == "restart":
                mgr.set_forward_enabled(conn_tag, src_port, direction, False)
                mgr.set_forward_enabled(conn_tag, src_port, direction, True)
        except Exception as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error", timeout=3)
        self.app.call_from_thread(self._bg_reload)

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
        host = str(row[1])
        conn_tag = str(row[2])
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
