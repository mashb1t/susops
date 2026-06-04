"""Smoke tests: TUI starts, correct screen, quit action."""
from __future__ import annotations

import asyncio

from susops.tui.app import SusOpsTuiApp


def test_tui_starts_on_dashboard(tui_workspace):
    """TUI should mount directly on DashboardScreen (no error modal)."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            assert type(app.screen).__name__ == "DashboardScreen"
            # Sanity: the manager is wired to the fixture workspace
            assert app.manager.workspace == tui_workspace

    asyncio.run(_run())


def test_tui_bindings_registered(tui_workspace):
    """App-level bindings table should contain the expected keys."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(0.5)
            binding_keys = {b.key for b in app.BINDINGS}
            assert "c" in binding_keys
            assert "f" in binding_keys
            assert "q" in binding_keys
            assert "ctrl+p" in binding_keys

    asyncio.run(_run())


def test_navigate_to_connections_screen(tui_workspace):
    """Pressing 'c' from the dashboard pushes ConnectionsScreen."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            assert type(app.screen).__name__ == "DashboardScreen"
            await pilot.press("c")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "ConnectionsScreen"

    asyncio.run(_run())


def test_navigate_back_from_connections(tui_workspace):
    """Pressing 'escape' on ConnectionsScreen pops back to DashboardScreen."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("c")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "ConnectionsScreen"
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "DashboardScreen"

    asyncio.run(_run())


def test_navigate_to_shares_screen(tui_workspace):
    """Pressing 'f' from the dashboard pushes SharesScreen."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("f")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "SharesScreen"

    asyncio.run(_run())


def test_navigate_back_from_shares(tui_workspace):
    """Pressing 'escape' on SharesScreen returns to DashboardScreen."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("f")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "SharesScreen"
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "DashboardScreen"

    asyncio.run(_run())


def test_quit_exits_app(tui_workspace):
    """Calling action_quit() terminates the app cleanly without raising."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            # action_quit may call manager.stop_quick() depending on config.
            # We call it directly and confirm no exception propagates.
            app.action_quit()
            await pilot.pause(0.3)
            # If we reach here the app exited cleanly; the async-with block
            # will drain the app during __aexit__.

    asyncio.run(_run())
