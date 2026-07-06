"""Tests: the TUI add-forward dialog's per-endpoint Port/Socket mode toggle."""
from __future__ import annotations

import asyncio

from textual.widgets import Input, RadioButton, RadioSet

from susops.tui.app import SusOpsTuiApp
from susops.tui.screens.connections import _AddForwardDialog


def _press_ok(screen, capture):
    screen.dismiss = capture  # type: ignore[method-assign]
    screen.on_button_pressed(
        type("E", (), {"button": type("B", (), {"id": "btn-ok"})()})())


def test_socket_mode_forward_via_dialog(tui_workspace):
    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(160, 60)) as pilot:
            await pilot.pause(1.0)
            app.manager.add_connection("c", "user@host")
            await pilot.press("c")
            await pilot.pause(0.3)
            app.push_screen(_AddForwardDialog("local", ["c"]))
            await pilot.pause(0.3)
            s = app.screen
            # Flip both endpoints to Socket mode.
            s.query_one("#src-mode-socket", RadioButton).value = True
            s.query_one("#dst-mode-socket", RadioButton).value = True
            await pilot.pause(0.2)
            # Socket sub-fields are now visible, port groups hidden.
            assert s.query_one("#src-socket-group").display is True
            assert s.query_one("#src-port-group").display is False
            s.query_one("#src-socket", Input).value = "/tmp/docker.sock"
            s.query_one("#dst-socket", Input).value = "/var/run/docker.sock"
            result = {}
            _press_ok(s, lambda d: result.update(data=d))
            data = result["data"]
            assert data is not None, "dialog rejected a valid socket forward"
            assert data["src_socket"] == "/tmp/docker.sock"
            assert data["dst_socket"] == "/var/run/docker.sock"
            assert data["src"] == 0 and data["dst"] == 0
    asyncio.run(_run())


def test_port_mode_is_default_and_reads_bind_and_port(tui_workspace):
    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(160, 60)) as pilot:
            await pilot.pause(1.0)
            app.push_screen(_AddForwardDialog("local", ["c"]))
            await pilot.pause(0.3)
            s = app.screen
            # Default mode is Port: port group visible, socket group hidden.
            assert s.query_one("#src-mode", RadioSet).pressed_index == 0
            assert s.query_one("#src-port-group").display is True
            assert s.query_one("#src-socket-group").display is False
            s.query_one("#src-addr", Input).value = "0.0.0.0"
            s.query_one("#src-port", Input).value = "5432"
            s.query_one("#dst-port", Input).value = "5432"
            result = {}
            _press_ok(s, lambda d: result.update(data=d))
            data = result["data"]
            assert data is not None
            assert data["src"] == 5432 and data["src_socket"] == ""
            assert data["src_addr"] == "0.0.0.0"
    asyncio.run(_run())


def test_socket_mode_requires_a_path(tui_workspace):
    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(160, 60)) as pilot:
            await pilot.pause(1.0)
            app.push_screen(_AddForwardDialog("local", ["c"]))
            await pilot.pause(0.3)
            s = app.screen
            s.query_one("#src-mode-socket", RadioButton).value = True
            await pilot.pause(0.2)
            # leave socket path empty -> must not dismiss
            called = {"v": False}
            _press_ok(s, lambda d: called.update(v=True))
            assert called["v"] is False
    asyncio.run(_run())


def test_bind_field_suggestions_include_harvested_binds(tui_workspace):
    async def _run():
        app = SusOpsTuiApp()
        async with app.run_test(headless=True, size=(160, 60)) as pilot:
            await pilot.pause(1.0)
            from susops.core.config import PortForward
            app.manager.add_connection("c", "user@host")
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
            # bind field editable + suggester knows presets + harvested bind
            app.screen.query_one("#src-addr", Input).value = "10.9.9.9"
            assert app.screen.query_one("#src-addr", Input).value == "10.9.9.9"
            assert "172.120.0.1" in dlg._bind_suggestions
            assert "localhost" in dlg._bind_suggestions
    asyncio.run(_run())
