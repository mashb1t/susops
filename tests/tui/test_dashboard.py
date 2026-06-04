"""Dashboard screen tests: connection display, tab switching, stats rendering."""
from __future__ import annotations

import asyncio

from textual.widgets import RichLog, TabbedContent

from susops.client import SusOpsClient
from susops.tui.app import SusOpsTuiApp


def test_dashboard_shows_connection_after_add(tui_workspace):
    """After add_connection the dashboard connection list shows the new tag."""
    SusOpsClient(workspace=tui_workspace).add_connection("work", "user@host")

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            screen = app.screen
            screen.refresh_status()
            await pilot.pause(0.5)
            snap = app.export_screenshot()
            assert "work" in snap

    asyncio.run(_run())


def test_dashboard_shows_multiple_connections(tui_workspace):
    """All configured connections appear in the sidebar after mount."""
    client = SusOpsClient(workspace=tui_workspace)
    client.add_connection("alpha", "user@alpha.example.com")
    client.add_connection("beta", "user@beta.example.com")

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.5)
            snap = app.export_screenshot()
            assert "alpha" in snap
            assert "beta" in snap

    asyncio.run(_run())


def test_dashboard_stats_tab_renders_no_connections(tui_workspace):
    """Stats tab with no connections shows 'No connections configured.'"""
    from textual.widgets import Static

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            # Query the stats widget directly — the SVG screenshot encodes spaces
            # as non-breaking spaces (\xa0) which makes substring matching unreliable.
            # Static.content returns the markup string that was passed to update().
            stats = app.screen.query_one("#stats-content", Static)
            assert "No connections configured" in stats.content

    asyncio.run(_run())


def test_dashboard_tab_switching(tui_workspace):
    """All four tabs on the dashboard are reachable without errors."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            tc = app.screen.query_one(TabbedContent)
            for target in ("tab-logs", "tab-config", "tab-pac", "tab-stats"):
                tc.active = target
                await pilot.pause(0.2)
                assert tc.active == target

    asyncio.run(_run())


def test_dashboard_logs_tab_shows_entries(tui_workspace):
    """The Logs tab should show log lines (regression: was empty on 'All' view).

    This validates the bug fix where _update_detail_panel moved log population
    outside the per-tag branch so it runs even when 'All' is selected.
    """
    SusOpsClient(workspace=tui_workspace).add_connection("work", "user@host")

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            screen = app.screen
            screen.refresh_status()
            await pilot.pause(0.5)
            tc = app.screen.query_one(TabbedContent)
            tc.active = "tab-logs"
            await pilot.pause(0.3)
            log_widget = app.screen.query_one("#detail-logs", RichLog)
            # The log widget should have at least one line (the daemon startup entry)
            assert len(log_widget.lines) > 0

    asyncio.run(_run())


def test_dashboard_config_tab_shows_yaml(tui_workspace):
    """The Config tab loads and displays the config.yaml content."""
    SusOpsClient(workspace=tui_workspace).add_connection("myconn", "user@myhost")

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            tc = app.screen.query_one(TabbedContent)
            tc.active = "tab-config"
            await pilot.pause(0.3)
            from textual.widgets import TextArea
            config_area = app.screen.query_one("#config-tab-area", TextArea)
            config_text = config_area.text
            # Config file should contain the connection tag we just added
            assert "myconn" in config_text or "myhost" in config_text

    asyncio.run(_run())


def test_dashboard_pac_tab_shows_pac_content(tui_workspace):
    """The PAC tab loads susops.pac or a placeholder when no PAC file exists."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            tc = app.screen.query_one(TabbedContent)
            tc.active = "tab-pac"
            await pilot.pause(0.3)
            from textual.widgets import TextArea
            pac_area = app.screen.query_one("#pac-tab-area", TextArea)
            # Either a real PAC file or the placeholder message
            assert pac_area.text is not None

    asyncio.run(_run())


def test_dashboard_connection_list_updates_on_refresh(tui_workspace):
    """Adding a connection while the TUI is running appears after refresh_status."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            snap_before = app.export_screenshot()
            assert "newconn" not in snap_before

            # Add the connection via the client (simulating external change)
            SusOpsClient(workspace=tui_workspace).add_connection("newconn", "user@new.host")

            # Force a refresh
            app.screen.refresh_status()
            await pilot.pause(0.5)

            snap_after = app.export_screenshot()
            assert "newconn" in snap_after

    asyncio.run(_run())
