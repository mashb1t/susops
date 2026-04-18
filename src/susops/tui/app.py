"""SusOps Textual TUI application."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from susops.tui.screens.connections import ConnectionsScreen
from susops.tui.screens.dashboard import DashboardScreen
from susops.tui.screens.shares import SharesScreen


class _SusOpsCommands(Provider):
    """Command palette provider for quick actions."""

    async def search(self, query: str) -> Hits:
        app: SusOpsTuiApp = self.app  # type: ignore[assignment]
        commands = [
            ("Start tunnels", app.action_start_all, "Start all SSH tunnels"),
            ("Stop tunnels", app.action_stop_all, "Stop all SSH tunnels"),
            ("Restart tunnels", app.action_restart_all, "Restart all SSH tunnels"),
            ("Dashboard", lambda: app.push_screen("dashboard"), "Go to dashboard"),
            ("Connections", lambda: app.push_screen("connections"), "Manage connections"),
            ("Shares", lambda: app.push_screen("shares"), "Share/fetch files"),
            ("Config", lambda: app.action_show_config(), "View config.yaml"),
            ("PAC file", lambda: app.action_show_pac(), "View PAC proxy config"),
            ("Quit", app.action_quit, "Quit SusOps"),
        ]
        q = query.lower()
        for name, action, description in commands:
            if q in name.lower() or q in description.lower():
                yield Hit(
                    score=1.0,
                    match_display=name,
                    command=action,
                    help=description,
                )


class _ConfigErrorScreen(ModalScreen):
    """Shown when config.yaml fails to load. Allows the user to open $EDITOR to fix it."""

    def __init__(self, error: str, config_path: str) -> None:
        super().__init__()
        self._error = error
        self._config_path = config_path

    def compose(self) -> ComposeResult:
        with Static(classes="modal-dialog"):
            yield Label("[bold red]Config file error[/bold red]")
            yield Label(f"[dim]{self._config_path}[/dim]")
            yield Label(self._error)
            yield Label("\nFix the file and restart susops, or press [bold]e[/bold] to open in $EDITOR.")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Open in $EDITOR", id="btn-edit", variant="warning")
                yield Button("Quit", id="btn-quit", variant="error")

    def on_button_pressed(self, event) -> None:
        import os
        import subprocess
        if event.button.id == "btn-edit":
            editor = os.environ.get("EDITOR", "nano")
            subprocess.run([editor, self._config_path])
            self.dismiss(None)
        else:
            self.app.exit(1)


class SusOpsTuiApp(App):
    """SusOps Textual TUI — SSH tunnel + PAC proxy manager."""

    TITLE = "SusOps"

    @property
    def SUB_TITLE(self) -> str:  # type: ignore[override]
        import susops
        return f"SSH Tunnel & PAC Manager  v{susops.__version__}"

    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("c", "push_screen('connections')", "Connections", show=False),
        Binding("f", "push_screen('shares')", "Shares", show=False),
        Binding("s", "start_all", "Start", show=False),
        Binding("x", "stop_all", "Stop", show=False),
        Binding("r", "restart_all", "Restart", show=False),
        Binding("ctrl+p", "command_palette", "Commands"),
        Binding("q", "quit", "Quit"),
    ]

    SCREENS = {
        "dashboard": DashboardScreen,
        "connections": ConnectionsScreen,
        "shares": SharesScreen,
    }

    COMMANDS = App.COMMANDS | {_SusOpsCommands}

    def __init__(self, verbose: bool = False) -> None:
        super().__init__()
        self._verbose = verbose
        self.manager = None  # type: ignore[assignment]

    def on_mount(self) -> None:
        from pathlib import Path
        from susops.facade import SusOpsManager
        workspace = Path.home() / ".susops"
        try:
            self.manager = SusOpsManager(verbose=self._verbose)
        except Exception as exc:
            config_path = str(workspace / "config.yaml")
            self.push_screen(_ConfigErrorScreen(str(exc), config_path))
            return
        self.push_screen("dashboard")

    def action_quit(self) -> None:
        if self.manager.app_config.stop_on_quit:
            self.manager.stop_quick()
        else:
            self.manager.detach_pac()
        self.exit()

    def action_start_all(self) -> None:
        self._bg_start()

    def action_stop_all(self) -> None:
        self._bg_stop()

    def action_restart_all(self) -> None:
        self._bg_restart()

    def action_open_github(self) -> None:
        import webbrowser
        webbrowser.open("https://github.com/mashb1t/susops")

    def action_show_config(self) -> None:
        from susops.tui.screens.dashboard import DashboardScreen
        screen = self.query_one(DashboardScreen)
        screen.action_show_tab("tab-config")

    def action_show_pac(self) -> None:
        from susops.tui.screens.dashboard import DashboardScreen
        screen = self.query_one(DashboardScreen)
        screen.action_show_tab("tab-pac")

    def _bg_start(self) -> None:
        self.run_worker(self._do_start, thread=True)

    def _bg_stop(self) -> None:
        self.run_worker(self._do_stop, thread=True)

    def _bg_restart(self) -> None:
        self.run_worker(self._do_restart, thread=True)

    def _do_start(self) -> None:
        result = self.manager.start()
        self.call_from_thread(
            self.notify,
            result.message,
            severity="information" if result.success else "error",
        )

    def _do_stop(self) -> None:
        result = self.manager.stop()
        self.call_from_thread(
            self.notify,
            result.message,
            severity="information" if result.success else "error",
        )

    def _do_restart(self) -> None:
        result = self.manager.restart()
        self.call_from_thread(
            self.notify,
            result.message,
            severity="information" if result.success else "error",
        )
