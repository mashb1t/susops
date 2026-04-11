"""SusOps Textual TUI application."""
from __future__ import annotations

import threading

from textual.app import App
from textual.binding import Binding
from textual.command import Hit, Hits, Provider

from susops.core.types import ProcessState
from susops.tui.screens.config_editor import ConfigEditorScreen

_LOGO_MARKUP: dict[ProcessState, str] = {
    ProcessState.RUNNING: "[green]S[/green]usOps",
    ProcessState.STOPPED_PARTIALLY: "[dark_orange]S[/dark_orange]usOps",
    ProcessState.STOPPED: "[red]S[/red]usOps",
    ProcessState.ERROR: "[bold red]S[/bold red]usOps",
}
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

    @property
    def SUB_TITLE(self) -> str:  # type: ignore[override]
        import susops
        return f"SSH Tunnel & PAC Manager  v{susops.__version__}"

    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("c", "push_screen('connections')", "Connections", show=False),
        Binding("e", "push_screen('config')", "Config", show=False),
        Binding("f", "push_screen('share')", "Share", show=False),
        Binding("s", "start_all", "Start", show=False),
        Binding("x", "stop_all", "Stop", show=False),
        Binding("r", "restart_all", "Restart", show=False),
        Binding("ctrl+p", "command_palette", "Commands"),
        Binding("q", "quit", "Quit"),
    ]

    SCREENS = {
        "dashboard": DashboardScreen,
        "connections": ConnectionEditorScreen,
        "share": ShareScreen,
        "config": ConfigEditorScreen,
    }

    COMMANDS = App.COMMANDS | {_SusOpsCommands}

    def __init__(self, verbose: bool = False) -> None:
        super().__init__()
        from susops.facade import SusOpsManager
        self.manager = SusOpsManager(verbose=verbose)

    def on_mount(self) -> None:
        self.push_screen("dashboard")

    def action_quit(self) -> None:
        if self.manager.app_config.stop_on_quit:
            # Run stop in a daemon thread so Python won't wait for it if the
            # user presses ctrl+c before it finishes. SIGTERM is sent to all
            # processes immediately; the wait loops are best-effort.
            threading.Thread(target=self.manager.stop, daemon=True).start()
        self.exit()

    def action_start_all(self) -> None:
        self._bg_start()

    def action_stop_all(self) -> None:
        self._bg_stop()

    def action_restart_all(self) -> None:
        self._bg_restart()

    def set_logo_state(self, state: ProcessState) -> None:
        """Update the footer logo color to reflect the current process state."""
        text = _LOGO_MARKUP.get(state, "[dim]S[/dim]usOps")
        try:
            self.query_one(".footer-logo").update(text)
        except Exception:
            pass

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
