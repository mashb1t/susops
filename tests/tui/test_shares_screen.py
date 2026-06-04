"""Shares screen tests: navigation, list rendering, stop/delete actions."""
from __future__ import annotations

import asyncio

from textual.widgets import Label, ListView

from susops.client import SusOpsClient
from susops.tui.app import SusOpsTuiApp


def test_shares_screen_empty_by_default(tui_workspace):
    """With no active shares, the shares screen shows the 'No shares.' prompt."""

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("f")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "SharesScreen"
            lv = app.screen.query_one("#share-list", ListView)
            assert len(list(lv.children)) == 0
            # Label.content returns the markup string passed to update()
            status_label = app.screen.query_one("#share-status", Label)
            assert "No shares" in status_label.content

    asyncio.run(_run())


def test_shares_screen_shows_active_share(tui_workspace, tmp_path):
    """A file share started via the client appears in the shares list."""
    # Set up a connection and a real file to share
    client = SusOpsClient(workspace=tui_workspace)
    client.add_connection("work", "user@host.example.com")

    # Create a test file
    test_file = tmp_path / "testfile.txt"
    test_file.write_text("hello world")

    # Start a share via the client
    share_info = client.share(test_file, conn_tag="work", port=None)

    try:
        async def _run():
            app = SusOpsTuiApp()
            async with app.run_test(headless=True, size=(140, 50)) as pilot:
                await pilot.pause(1.0)
                await pilot.press("f")
                await pilot.pause(0.5)
                snap = app.export_screenshot()
                assert "testfile.txt" in snap

        asyncio.run(_run())
    finally:
        # Clean up the share server
        try:
            client.stop_share(share_info.port)
            client.delete_share(share_info.port)
        except Exception:
            pass


def test_shares_screen_stop_share(tui_workspace, tmp_path):
    """Pressing 'd' on a running share stops it (marks it stopped)."""
    client = SusOpsClient(workspace=tui_workspace)
    client.add_connection("work", "user@host.example.com")

    test_file = tmp_path / "tostop.txt"
    test_file.write_text("stop me")

    share_info = client.share(test_file, conn_tag="work", port=None)

    try:
        async def _run():
            app = SusOpsTuiApp()
            async with app.run_test(headless=True, size=(140, 50)) as pilot:
                await pilot.pause(1.0)
                await pilot.press("f")
                await pilot.pause(0.5)

                # The share should show as running
                lv = app.screen.query_one("#share-list", ListView)
                assert len(list(lv.children)) == 1

                # Press 'd' to stop the share
                await pilot.press("d")
                await pilot.pause(0.3)

                # Share is still in the list but stopped
                shares_after = client.list_shares()
                assert len(shares_after) == 1
                assert not shares_after[0].running
                assert shares_after[0].stopped

        asyncio.run(_run())
    finally:
        try:
            client.delete_share(share_info.port)
        except Exception:
            pass


def test_shares_screen_delete_share(tui_workspace, tmp_path):
    """Pressing 'x' on a share removes it from the list entirely."""
    client = SusOpsClient(workspace=tui_workspace)
    client.add_connection("work", "user@host.example.com")

    test_file = tmp_path / "todelete.txt"
    test_file.write_text("delete me")

    share_info = client.share(test_file, conn_tag="work", port=None)

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("f")
            await pilot.pause(0.5)

            lv = app.screen.query_one("#share-list", ListView)
            assert len(list(lv.children)) == 1

            # Stop the share first (so it's safe to delete), then delete
            await pilot.press("d")  # stop
            await pilot.pause(0.2)
            await pilot.press("x")  # delete
            await pilot.pause(0.3)

            shares_after = client.list_shares()
            assert len(shares_after) == 0

    asyncio.run(_run())


def test_shares_screen_add_dialog_opens(tui_workspace):
    """Pressing 'a' on the shares screen opens the add-share dialog.

    The dialog is cancelled via the Cancel button. Full dialog-filling is noted
    as a TODO pending better headless Input interaction support.

    TODO: Fill in file path and connection then click Share once headless Input
    widget interaction is reliable without pytest-asyncio.
    """
    SusOpsClient(workspace=tui_workspace).add_connection("work", "user@host")

    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("f")
            await pilot.pause(0.3)
            await pilot.press("a")
            await pilot.pause(0.3)
            # The add-share dialog should now be on the screen stack
            assert type(app.screen).__name__ == "_AddShareDialog"
            # Cancel via the Cancel button (ModalScreen has no escape binding)
            await pilot.click("#btn-cancel")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "SharesScreen"

    asyncio.run(_run())
