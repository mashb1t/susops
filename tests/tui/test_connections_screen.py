"""Connections screen tests: navigation, table rendering, CRUD actions."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable, TabbedContent

from susops.client import SusOpsClient
from susops.tui.app import SusOpsTuiApp


def test_connections_screen_shows_connection(tui_workspace):
    """ConnectionsScreen should list a pre-existing connection in the table."""
    SusOpsClient(workspace=tui_workspace).add_connection("work", "user@host.example.com")

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("c")
            await pilot.pause(0.5)
            assert type(app.screen).__name__ == "ConnectionsScreen"
            tbl = app.screen.query_one("#tbl-connections", DataTable)
            assert tbl.row_count == 1
            row = tbl.get_row_at(0)
            assert str(row[1]) == "work"
            assert "user@host.example.com" in str(row[2])

    asyncio.run(_run())


def test_connections_screen_empty_by_default(tui_workspace):
    """With no connections configured, the connections table is empty."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("c")
            await pilot.pause(0.3)
            tbl = app.screen.query_one("#tbl-connections", DataTable)
            assert tbl.row_count == 0

    asyncio.run(_run())


def test_connections_screen_multiple_connections(tui_workspace):
    """Multiple connections all appear as rows in the connections table."""
    client = SusOpsClient(workspace=tui_workspace)
    client.add_connection("alpha", "user@alpha.example.com")
    client.add_connection("beta", "user@beta.example.com")
    client.add_connection("gamma", "user@gamma.example.com")

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("c")
            await pilot.pause(0.5)
            tbl = app.screen.query_one("#tbl-connections", DataTable)
            assert tbl.row_count == 3
            tags = {str(tbl.get_row_at(i)[1]) for i in range(tbl.row_count)}
            assert tags == {"alpha", "beta", "gamma"}

    asyncio.run(_run())


def test_connections_screen_tab_switching(tui_workspace):
    """All four tabs in ConnectionsScreen (Connections/PAC/Local/Remote) are reachable."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("c")
            await pilot.pause(0.3)
            tc = app.screen.query_one("#editor-tabs", TabbedContent)
            for tab_id in ("tab-connections", "tab-pac", "tab-local", "tab-remote"):
                tc.active = tab_id
                await pilot.pause(0.2)
                assert tc.active == tab_id

    asyncio.run(_run())


def test_connections_screen_delete_connection(tui_workspace):
    """Pressing 'd' on a selected connection removes it from the table."""
    SusOpsClient(workspace=tui_workspace).add_connection("todelete", "user@host")

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("c")
            await pilot.pause(0.5)
            tbl = app.screen.query_one("#tbl-connections", DataTable)
            assert tbl.row_count == 1

            # Press 'd' to delete the selected (only) connection
            await pilot.press("d")
            await pilot.pause(0.5)

            tbl = app.screen.query_one("#tbl-connections", DataTable)
            assert tbl.row_count == 0

    asyncio.run(_run())


def test_connections_screen_add_connection_via_modal(tui_workspace):
    """Pressing 'a' on the Connections tab opens the add-connection dialog.

    We verify the modal is present and then cancel it via the Cancel button.
    The modal (a ModalScreen) has no default escape binding, so we must click
    the Cancel button (id="btn-cancel") to dismiss it.

    TODO: Fill in tag/host and confirm Add once headless Input widget interaction
    is reliably supported — currently pressing keys into Input requires the
    widget to have focus, which requires an explicit pilot.click() first.
    """

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("c")
            await pilot.pause(0.3)
            # Trigger add — should push the modal
            await pilot.press("a")
            await pilot.pause(0.3)
            # The modal is on the screen stack now
            assert type(app.screen).__name__ == "_AddConnectionDialog"
            # Cancel the modal by clicking the Cancel button (no escape binding)
            await pilot.click("#btn-cancel")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "ConnectionsScreen"

    asyncio.run(_run())


def test_connections_screen_toggle_enabled(tui_workspace):
    """Pressing 't' toggles the enabled state of the selected connection."""
    client = SusOpsClient(workspace=tui_workspace)
    client.add_connection("myconn", "user@host")

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("c")
            await pilot.pause(0.5)
            # Read initial enabled state from config
            cfg_before = SusOpsClient(workspace=tui_workspace).list_config()
            conn_before = next(c for c in cfg_before.connections if c.tag == "myconn")
            enabled_before = conn_before.enabled

            # Toggle
            await pilot.press("t")
            await pilot.pause(0.5)

            cfg_after = SusOpsClient(workspace=tui_workspace).list_config()
            conn_after = next(c for c in cfg_after.connections if c.tag == "myconn")
            assert conn_after.enabled != enabled_before

    asyncio.run(_run())
