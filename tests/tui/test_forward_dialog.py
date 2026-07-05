"""Tests: adding a Unix-socket forward through the TUI add-forward dialog."""
from __future__ import annotations

import asyncio

from textual.widgets import Input, Checkbox

from susops.tui.app import SusOpsTuiApp
from susops.tui.screens.connections import _AddForwardDialog


def test_add_unix_socket_forward_via_dialog(tui_workspace):
    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(160, 55)) as pilot:
            await pilot.pause(1.0)
            # Seed a connection to attach the forward to.
            app.manager.add_connection("c", "user@host")
            await pilot.press("c")            # -> ConnectionsScreen
            await pilot.pause(0.3)
            screen = app.screen
            # Open the add-local-forward dialog directly and fill a unix->unix
            # (docker-style) forward: local socket -> remote socket, no ports.
            dlg = _AddForwardDialog("local", ["c"])
            app.push_screen(dlg)
            await pilot.pause(0.3)
            s = app.screen
            s.query_one("#src-socket", Input).value = "/tmp/docker.sock"
            s.query_one("#dst-socket", Input).value = "/var/run/docker.sock"
            # ports left blank; TCP on, UDP off (defaults)
            result = {}

            def _capture(data):
                result["data"] = data

            # Emulate pressing "Add": call the handler and read the dismissed dict.
            s.dismiss = _capture  # type: ignore[method-assign]
            s.on_button_pressed(type("E", (), {"button": type("B", (), {"id": "btn-ok"})()})())
            data = result["data"]
            assert data is not None, "dialog rejected a valid socket forward"
            assert data["src_socket"] == "/tmp/docker.sock"
            assert data["dst_socket"] == "/var/run/docker.sock"
            assert data["src"] == 0 and data["dst"] == 0

    asyncio.run(_run())


def test_bind_field_is_editable_with_suggestions(tui_workspace):
    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(160, 55)) as pilot:
            await pilot.pause(1.0)
            from susops.core.config import PortForward
            app.manager.add_connection("c", "user@host")
            # A forward with a custom (non-preset) bind so it gets harvested.
            app.manager.add_local_forward(
                "c", PortForward(src_addr="172.120.0.1", src_port=9000,
                                 dst_addr="localhost", dst_port=9000))
            await pilot.press("c")
            await pilot.pause(0.3)
            binds = app.screen._existing_binds()
            assert "172.120.0.1" in binds
            dlg = _AddForwardDialog("local", ["c"], binds)
            app.push_screen(dlg)
            await pilot.pause(0.3)
            s = app.screen
            # Bind field is now a free-text Input that accepts a custom address…
            addr = s.query_one("#src-addr", Input)
            addr.value = "10.9.9.9"
            assert s.query_one("#src-addr", Input).value == "10.9.9.9"
            # …and its suggester knows the presets + the harvested custom bind.
            assert "172.120.0.1" in dlg._bind_suggestions
            assert "localhost" in dlg._bind_suggestions
    asyncio.run(_run())


def test_dialog_rejects_port_and_socket_on_same_side(tui_workspace):
    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(160, 55)) as pilot:
            await pilot.pause(1.0)
            dlg = _AddForwardDialog("local", ["c"])
            app.push_screen(dlg)
            await pilot.pause(0.3)
            s = app.screen
            s.query_one("#src-port", Input).value = "5432"
            s.query_one("#src-socket", Input).value = "/tmp/x.sock"
            s.query_one("#dst-port", Input).value = "80"
            captured = {"called": False}
            s.dismiss = lambda d: captured.update(called=True)  # type: ignore[method-assign]
            s.on_button_pressed(type("E", (), {"button": type("B", (), {"id": "btn-ok"})()})())
            assert captured["called"] is False, "dialog should not dismiss on invalid input"

    asyncio.run(_run())
