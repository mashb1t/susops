"""SusOps Textual TUI application."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.widgets import Footer, Header

from susops.tui.screens.config_editor import ConfigEditorScreen
from susops.tui.screens.connection_editor import ConnectionEditorScreen
from susops.tui.screens.dashboard import DashboardScreen
from susops.tui.screens.share import ShareScreen


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
            ("Share", lambda: app.push_screen("share"), "Share/fetch files"),
            ("Config", lambda: app.push_screen("config"), "Edit config"),
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


class SusOpsTuiApp(App):
    """SusOps Textual TUI — SSH tunnel + PAC proxy manager."""

    TITLE = "SusOps"
    SUB_TITLE = "SSH Tunnel & PAC Manager"

    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Commands"),
        Binding("d", "push_screen('dashboard')", "Dashboard", show=True),
        Binding("c", "push_screen('connections')", "Connections", show=True),
        Binding("f", "push_screen('share')", "Share", show=True),
        Binding("e", "push_screen('config')", "Config", show=False),
        Binding("s", "start_all", "Start", show=False),
        Binding("x", "stop_all", "Stop", show=False),
        Binding("r", "restart_all", "Restart", show=False),
        Binding("q", "quit", "Quit"),
    ]

    SCREENS = {
        "dashboard": DashboardScreen,
        "connections": ConnectionEditorScreen,
        "share": ShareScreen,
        "config": ConfigEditorScreen,
    }

    COMMANDS = App.COMMANDS | {_SusOpsCommands}

    def __init__(self) -> None:
        super().__init__()
        from susops.facade import SusOpsManager
        self.manager = SusOpsManager()

    def on_mount(self) -> None:
        self.push_screen("dashboard")

    def action_start_all(self) -> None:
        self._bg_start()

    def action_stop_all(self) -> None:
        self._bg_stop()

    def action_restart_all(self) -> None:
        self._bg_restart()

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
