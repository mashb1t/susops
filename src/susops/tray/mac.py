"""macOS tray app — rumps + PyObjC.

Requires: pip install 'susops[tray-mac]'  (rumps>=0.4)
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from susops.core.config import PortForward
from susops.core.ports import is_port_free, validate_port
from susops.core.types import ProcessState
from susops.tray.base import AbstractTrayApp, get_icon_path, get_ssh_hosts


def _is_dark_theme() -> bool:
    """Return True when macOS is using Dark Mode."""
    try:
        from AppKit import NSApplication  # type: ignore[import]
        appearance = NSApplication.sharedApplication().effectiveAppearance().name()
        return "dark" in appearance.lower()
    except Exception:
        return False


def _get_icon_path(state: ProcessState) -> str | None:
    """Return icon path for state, respecting macOS light/dark appearance.

    Appearance is inverted: dark desktop → light icons (visible on dark menu bar).
    """
    variant = "light" if _is_dark_theme() else "dark"
    return get_icon_path(state, variant=variant, prefer_png=True)


class SusOpsMacTray(AbstractTrayApp):
    """macOS system tray application using rumps."""

    def __init__(self) -> None:
        super().__init__()
        import rumps
        self._rumps = rumps

        icon_path = _get_icon_path(ProcessState.STOPPED)
        self._app = rumps.App(
            "SusOps",
            icon=icon_path,
            template=True,
            quit_button=None,
        )
        self._build_menu()
        self._register_appearance_observer()

    def _register_appearance_observer(self) -> None:
        """Register for macOS theme-change notifications to update icon immediately."""
        try:
            from Foundation import NSDistributedNotificationCenter  # type: ignore[import]
            import objc  # type: ignore[import]

            def _on_appearance_changed(_notification):
                self.update_icon(self.state)

            self._appearance_observer = _on_appearance_changed
            center = NSDistributedNotificationCenter.defaultCenter()
            center.addObserverForName_object_queue_usingBlock_(
                "AppleInterfaceThemeChangedNotification",
                None,
                None,
                _on_appearance_changed,
            )
        except Exception:
            pass  # PyObjC not available or older macOS

    # ------------------------------------------------------------------ #
    # AbstractTrayApp implementation
    # ------------------------------------------------------------------ #

    def update_icon(self, state: ProcessState) -> None:
        icon_path = _get_icon_path(state)
        if icon_path:
            self._app.icon = icon_path

    def update_menu_sensitivity(self, state: ProcessState) -> None:
        running = state == ProcessState.RUNNING
        stopped = state == ProcessState.STOPPED
        if hasattr(self, "_item_start"):
            self._item_start._menuitem.setEnabled_(not running)  # type: ignore[attr-defined]
        if hasattr(self, "_item_stop"):
            self._item_stop._menuitem.setEnabled_(not stopped)  # type: ignore[attr-defined]
        if hasattr(self, "_item_restart"):
            self._item_restart._menuitem.setEnabled_(not stopped)  # type: ignore[attr-defined]
        if hasattr(self, "_item_test_any"):
            self._item_test_any._menuitem.setEnabled_(not stopped)  # type: ignore[attr-defined]
        if hasattr(self, "_item_test_all"):
            self._item_test_all._menuitem.setEnabled_(not stopped)  # type: ignore[attr-defined]
        if hasattr(self, "_item_status"):
            dot = {
                ProcessState.RUNNING: "🟢",
                ProcessState.STOPPED_PARTIALLY: "🟠",
                ProcessState.STOPPED: "⚫",
                ProcessState.ERROR: "🔴",
                ProcessState.INITIAL: "⚫",
            }.get(state, "⚫")
            self._item_status.title = f"{dot} SusOps — {state.value}"

    def show_alert(self, title: str, msg: str) -> None:
        self._rumps.alert(title=title, message=msg, ok="OK")

    def show_output_dialog(self, title: str, output: str) -> None:
        self._rumps.alert(title=title, message=output, ok="Close")

    def run_in_background(self, fn: Callable, callback: Callable | None = None) -> None:
        def _worker():
            result = fn()
            if callback is not None:
                callback(result)
        threading.Thread(target=_worker, daemon=True).start()

    def schedule_poll(self, interval_seconds: int) -> None:
        @self._rumps.timer(interval_seconds)
        def _poll(_sender):
            self.do_poll()

    # ------------------------------------------------------------------ #
    # Menu building
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> None:
        rumps = self._rumps

        self._item_status = rumps.MenuItem("⚫ SusOps — unknown")
        self._item_status._menuitem.setEnabled_(False)  # type: ignore[attr-defined]

        self._item_start = rumps.MenuItem("Start Proxy", callback=lambda _: self.do_start())
        self._item_stop = rumps.MenuItem("Stop Proxy", callback=lambda _: self.do_stop())
        self._item_restart = rumps.MenuItem("Restart Proxy", callback=lambda _: self.do_restart())

        # Add submenu
        add_menu = rumps.MenuItem("Add")
        add_menu["Add Connection"] = rumps.MenuItem(
            "Add Connection", callback=lambda _: self._prompt_add_connection()
        )
        add_menu["Add Domain / IP / CIDR"] = rumps.MenuItem(
            "Add Domain / IP / CIDR", callback=lambda _: self._prompt_add_host()
        )
        add_menu["Add Local Forward"] = rumps.MenuItem(
            "Add Local Forward", callback=lambda _: self._prompt_add_local()
        )
        add_menu["Add Remote Forward"] = rumps.MenuItem(
            "Add Remote Forward", callback=lambda _: self._prompt_add_remote()
        )

        # Remove submenu
        rm_menu = rumps.MenuItem("Remove")
        rm_menu["Remove Connection"] = rumps.MenuItem(
            "Remove Connection", callback=lambda _: self._prompt_rm_connection()
        )
        rm_menu["Remove Domain / IP / CIDR"] = rumps.MenuItem(
            "Remove Domain / IP / CIDR", callback=lambda _: self._prompt_rm_host()
        )
        rm_menu["Remove Local Forward"] = rumps.MenuItem(
            "Remove Local Forward", callback=lambda _: self._prompt_rm_local()
        )
        rm_menu["Remove Remote Forward"] = rumps.MenuItem(
            "Remove Remote Forward", callback=lambda _: self._prompt_rm_remote()
        )

        # Test submenu
        self._item_test_any = rumps.MenuItem(
            "Test Any", callback=lambda _: self.do_test()
        )
        self._item_test_all = rumps.MenuItem(
            "Test All", callback=lambda _: self.do_test()
        )
        test_menu = rumps.MenuItem("Test")
        test_menu["Test Any"] = self._item_test_any
        test_menu["Test All"] = self._item_test_all

        # Launch Browser submenu
        browser_menu = rumps.MenuItem("Launch Browser")
        chrome_menu = rumps.MenuItem("Chrome")
        chrome_menu["Launch Chrome"] = rumps.MenuItem(
            "Launch Chrome", callback=lambda _: self.do_launch_chrome()
        )
        chrome_menu["Open Chrome Proxy Settings"] = rumps.MenuItem(
            "Open Chrome Proxy Settings", callback=lambda _: self._open_chrome_proxy_settings()
        )
        browser_menu["Chrome"] = chrome_menu
        firefox_menu = rumps.MenuItem("Firefox")
        firefox_menu["Launch Firefox"] = rumps.MenuItem(
            "Launch Firefox", callback=lambda _: self.do_launch_firefox()
        )
        browser_menu["Firefox"] = firefox_menu

        # File Transfer submenu
        self._ft_menu = rumps.MenuItem("File Transfer")
        self._ft_menu["Share File…"] = rumps.MenuItem(
            "Share File…", callback=lambda _: self._prompt_share_file()
        )
        self._ft_menu["Fetch File…"] = rumps.MenuItem(
            "Fetch File…", callback=lambda _: self._prompt_fetch_file()
        )

        self._app.menu = [
            self._item_status,
            None,
            rumps.MenuItem("Settings…", callback=lambda _: self._show_settings_dialog()),
            None,
            add_menu,
            rm_menu,
            rumps.MenuItem("Open Config File", callback=lambda _: self.do_open_config_file()),
            None,
            self._item_start,
            self._item_stop,
            self._item_restart,
            None,
            test_menu,
            rumps.MenuItem("Show Status", callback=lambda _: self.do_status()),
            browser_menu,
            self._ft_menu,
            None,
            rumps.MenuItem("Reset All", callback=lambda _: self._confirm_reset()),
            None,
            rumps.MenuItem("About SusOps", callback=lambda _: self._show_about()),
            rumps.MenuItem("Quit", callback=self._on_quit),
        ]

        self._active_shares: list = []

    # ------------------------------------------------------------------ #
    # File Transfer helpers
    # ------------------------------------------------------------------ #

    def _refresh_share_submenu(self) -> None:
        """Rebuild dynamic share items in the File Transfer submenu."""
        import pathlib
        # Remove old dynamic share items (keep Share File… and Fetch File…)
        for key in list(self._ft_menu.keys()):
            if key not in ("Share File…", "Fetch File…"):
                del self._ft_menu[key]

        self._active_shares = self.manager.list_shares()
        if self._active_shares:
            self._ft_menu["---"] = None  # separator
        for info in self._active_shares:
            name = pathlib.Path(info.file_path).name
            dot = "●" if info.running else "○"
            label = f"{dot} {name} (:{info.port})"
            self._ft_menu[label] = self._rumps.MenuItem(
                label,
                callback=self._make_share_info_handler(info),
            )

    def do_poll(self) -> None:
        super().do_poll()
        self._refresh_share_submenu()

    def _prompt_share_file(self) -> None:
        rumps = self._rumps
        config = self.manager.list_config()
        tags = [c.tag for c in config.connections]
        if not tags:
            self.show_alert("No Connections", "Add a connection first.")
            return

        win = rumps.Window(
            message=f"Connection tag ({', '.join(tags)}):",
            title="Share File",
            default_text=tags[0],
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r = win.run()
        if r.clicked == 0:
            return
        conn_tag = r.text.strip()
        if conn_tag not in tags:
            self.show_alert("Invalid Tag", f"Connection '{conn_tag}' not found.")
            return

        win2 = rumps.Window(
            message="Full path to file:",
            title="Share File",
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r2 = win2.run()
        if r2.clicked == 0:
            return
        file_path = r2.text.strip()
        if not file_path or not __import__("pathlib").Path(file_path).exists():
            self.show_alert("File Not Found", f"'{file_path}' does not exist.")
            return

        win3 = rumps.Window(
            message="Password (blank = auto-generate):",
            title="Share File",
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r3 = win3.run()
        if r3.clicked == 0:
            return
        pw = r3.text.strip() or None

        win4 = rumps.Window(
            message="Port (0 = auto-assign):",
            title="Share File",
            default_text="0",
            ok="Share",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r4 = win4.run()
        if r4.clicked == 0:
            return
        port_text = r4.text.strip() or "0"
        try:
            port_int = int(port_text)
        except ValueError:
            self.show_alert("Invalid Port", f"'{port_text}' is not a valid port number.")
            return

        self.do_share(conn_tag, file_path, password=pw, port=port_int)

    def _prompt_fetch_file(self) -> None:
        rumps = self._rumps
        config = self.manager.list_config()
        tags = [c.tag for c in config.connections]
        if not tags:
            self.show_alert("No Connections", "Add a connection first.")
            return

        win = rumps.Window(
            message=f"Connection tag ({', '.join(tags)}):",
            title="Fetch File",
            default_text=tags[0],
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r = win.run()
        if r.clicked == 0:
            return
        conn_tag = r.text.strip()
        if conn_tag not in tags:
            self.show_alert("Invalid Tag", f"Connection '{conn_tag}' not found.")
            return

        win2 = rumps.Window(
            message="Port number:",
            title="Fetch File",
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r2 = win2.run()
        if r2.clicked == 0:
            return
        port_text = r2.text.strip()
        try:
            port_int = int(port_text)
        except ValueError:
            self.show_alert("Invalid Port", f"'{port_text}' is not a valid port number.")
            return

        win3 = rumps.Window(
            message="Password:",
            title="Fetch File",
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r3 = win3.run()
        if r3.clicked == 0:
            return
        pw = r3.text.strip()
        if not pw:
            self.show_alert("Missing Field", "Password is required.")
            return

        win4 = rumps.Window(
            message="Save to (blank = ~/Downloads/<filename>):",
            title="Fetch File",
            default_text="",
            ok="Fetch",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r4 = win4.run()
        if r4.clicked == 0:
            return
        outfile = r4.text.strip() or None

        self.do_fetch(conn_tag, port_int, pw, outfile=outfile)

    def _make_share_info_handler(self, info):
        def handler(_sender):
            import pathlib
            name = pathlib.Path(info.file_path).name
            state = "running" if info.running else "stopped"
            toggle_label = "Stop" if info.running else "Start"
            response = self._rumps.alert(
                title=f"Share: {name}",
                message=(
                    f"File: {info.file_path}\n"
                    f"Port: {info.port}\n"
                    f"Password: {info.password}\n"
                    f"Connection: {info.conn_tag or '—'}\n"
                    f"State: {state}"
                ),
                ok=toggle_label,
                cancel="Close",
                other="Delete",
            )
            if response == 1:  # OK = Stop/Start
                if info.running:
                    self.do_stop_share(info.port)
                else:
                    self.do_share(info.conn_tag or "", info.file_path, info.password, info.port)
                self._refresh_share_submenu()
            elif response == 0:  # other = Delete
                self.do_delete_share(info.port)
                self._refresh_share_submenu()
        return handler

    # ------------------------------------------------------------------ #
    # Platform-specific actions
    # ------------------------------------------------------------------ #

    def _open_chrome_proxy_settings(self) -> None:
        for browser in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
            if shutil.which(browser):
                subprocess.Popen([browser])
                break
        self.show_output_dialog(
            "Open Proxy Settings",
            "Paste this URL into the Chrome address bar:\nchrome://net-internals/#proxy",
        )

    def _show_settings_dialog(self) -> None:
        rumps = self._rumps
        ac = self.manager.app_config
        pac_port = self.manager.config.pac_server_port

        # Use rumps Window for text input
        win = rumps.Window(
            message=(
                f"PAC Port (0=auto): {pac_port}\n"
                f"Stop on quit: {'yes' if ac.stop_on_quit else 'no'}\n"
                f"Ephemeral ports: {'yes' if ac.ephemeral_ports else 'no'}\n\n"
                "Enter new PAC port (blank to keep, 0=auto):"
            ),
            title="Settings",
            default_text=str(pac_port) if pac_port else "0",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        response = win.run()
        if response.clicked == 0:  # Cancel
            return

        pac_text = response.text.strip() or "0"
        try:
            pac_int = int(pac_text)
        except ValueError:
            self.show_alert("Invalid Port", f"'{pac_text}' is not a valid port number.")
            return
        if not validate_port(pac_int, allow_zero=True):
            self.show_alert("Invalid Port", f"PAC port must be 0 (auto) or between 1 and 65535.")
            return
        if pac_int != 0 and not is_port_free(pac_int):
            self.show_alert("Port In Use", f"Port {pac_int} is already in use.")
            return

        # Toggle stop_on_quit
        win2 = rumps.Window(
            message="Stop proxy on quit? (yes/no)",
            title="Settings",
            default_text="yes" if ac.stop_on_quit else "no",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r2 = win2.run()
        if r2.clicked == 0:
            return
        stop_on_quit = r2.text.strip().lower() in ("yes", "y", "true", "1")

        # Toggle ephemeral
        win3 = rumps.Window(
            message="Use random ports on each start? (yes/no)",
            title="Settings",
            default_text="yes" if ac.ephemeral_ports else "no",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r3 = win3.run()
        if r3.clicked == 0:
            return
        ephemeral = r3.text.strip().lower() in ("yes", "y", "true", "1")

        # Step 4: Restore shares on start
        win4 = rumps.Window(
            message="Restore file shares on start? (yes/no)",
            title="Settings",
            default_text="yes" if ac.restore_shares_on_start else "no",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r4 = win4.run()
        if r4.clicked == 0:
            return
        restore_shares = r4.text.strip().lower() in ("yes", "y", "true", "1")

        self.manager.update_app_config(
            stop_on_quit=stop_on_quit,
            ephemeral_ports=ephemeral,
            restore_shares_on_start=restore_shares,
        )
        self.manager._reload_config()
        self.manager.config = self.manager.config.model_copy(update={"pac_server_port": pac_int})
        self.manager._save()
        self.show_alert("Settings Saved", "Settings have been updated.")

    def _prompt_add_connection(self) -> None:
        rumps = self._rumps
        win = rumps.Window(
            message="Connection Tag (e.g. work):",
            title="Add Connection",
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r = win.run()
        if r.clicked == 0:
            return
        tag = r.text.strip()
        if not tag:
            self.show_alert("Missing Field", "Connection Tag must not be empty.")
            return

        win2 = rumps.Window(
            message="SSH Host (e.g. user@host.example.com):",
            title="Add Connection",
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r2 = win2.run()
        if r2.clicked == 0:
            return
        host = r2.text.strip()
        if not host:
            self.show_alert("Missing Field", "SSH Host must not be empty.")
            return

        win3 = rumps.Window(
            message="SOCKS Proxy Port (leave blank for auto):",
            title="Add Connection",
            default_text="",
            ok="Add",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r3 = win3.run()
        if r3.clicked == 0:
            return
        port_text = r3.text.strip()
        port_int = 0
        if port_text:
            try:
                port_int = int(port_text)
            except ValueError:
                self.show_alert("Invalid Port", f"'{port_text}' is not a valid port.")
                return
            if not validate_port(port_int, allow_zero=True):
                self.show_alert("Invalid Port", "SOCKS port must be 0 (auto) or between 1 and 65535.")
                return
            if port_int != 0 and not is_port_free(port_int):
                self.show_alert("Port In Use", f"Port {port_int} is already in use.")
                return

        self.do_add_connection(tag, host, port_int)

    def _prompt_add_host(self) -> None:
        rumps = self._rumps
        config = self.manager.list_config()
        tags = [c.tag for c in config.connections]
        if not tags:
            self.show_alert("No Connections", "Add a connection first.")
            return

        win = rumps.Window(
            message=f"Connection tag ({', '.join(tags)}):",
            title="Add Domain / IP / CIDR",
            default_text=tags[0] if tags else "",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r = win.run()
        if r.clicked == 0:
            return
        conn_tag = r.text.strip()
        if conn_tag not in tags:
            self.show_alert("Invalid Tag", f"Connection '{conn_tag}' not found.")
            return

        win2 = rumps.Window(
            message="Host / IP / CIDR to add:",
            title="Add Domain / IP / CIDR",
            default_text="",
            ok="Add",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r2 = win2.run()
        if r2.clicked == 0:
            return
        host = r2.text.strip()
        if not host:
            self.show_alert("Missing Field", "Host must not be empty.")
            return

        self.do_add_pac_host(host, conn_tag=conn_tag)

    def _prompt_add_local(self) -> None:
        self._prompt_add_forward(remote=False)

    def _prompt_add_remote(self) -> None:
        self._prompt_add_forward(remote=True)

    def _prompt_add_forward(self, remote: bool) -> None:
        rumps = self._rumps
        title = "Add Remote Forward" if remote else "Add Local Forward"
        config = self.manager.list_config()
        tags = [c.tag for c in config.connections]
        if not tags:
            self.show_alert("No Connections", "Add a connection first.")
            return

        win = rumps.Window(
            message=f"Connection tag ({', '.join(tags)}):",
            title=title,
            default_text=tags[0] if tags else "",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r = win.run()
        if r.clicked == 0:
            return
        conn_tag = r.text.strip()
        if conn_tag not in tags:
            self.show_alert("Invalid Tag", f"Connection '{conn_tag}' not found.")
            return

        src_label = "Forward Remote Port" if remote else "Forward Local Port"
        win_src = rumps.Window(
            message=f"{src_label}:",
            title=title,
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r_src = win_src.run()
        if r_src.clicked == 0:
            return
        src = r_src.text.strip()

        dst_label = "To Local Port" if remote else "To Remote Port"
        win_dst = rumps.Window(
            message=f"{dst_label}:",
            title=title,
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r_dst = win_dst.run()
        if r_dst.clicked == 0:
            return
        dst = r_dst.text.strip()

        src_bind_label = "Remote Bind" if remote else "Local Bind"
        win_src_bind = rumps.Window(
            message=f"{src_bind_label} (optional, blank = localhost, options: localhost / 172.17.0.1 / 0.0.0.0):",
            title=title,
            default_text="localhost",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r_src_bind = win_src_bind.run()
        if r_src_bind.clicked == 0:
            return
        src_addr = r_src_bind.text.strip() or "localhost"

        dst_bind_label = "Local Bind" if remote else "Remote Bind"
        win_dst_bind = rumps.Window(
            message=f"{dst_bind_label} (optional, blank = localhost, options: localhost / 172.17.0.1 / 0.0.0.0):",
            title=title,
            default_text="localhost",
            ok="Add",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r_dst_bind = win_dst_bind.run()
        if r_dst_bind.clicked == 0:
            return
        dst_addr = r_dst_bind.text.strip() or "localhost"

        try:
            src_int = int(src)
            dst_int = int(dst)
        except ValueError:
            self.show_alert("Invalid Port", "Ports must be numbers.")
            return
        if not validate_port(src_int) or not validate_port(dst_int):
            self.show_alert("Invalid Port", "Ports must be between 1 and 65535.")
            return
        local_port = dst_int if remote else src_int
        if not is_port_free(local_port):
            self.show_alert("Port In Use", f"Local port {local_port} is already in use.")
            return

        fw = PortForward(src_addr=src_addr, src_port=src_int, dst_addr=dst_addr, dst_port=dst_int)
        if remote:
            self.do_add_remote_forward(conn_tag, fw)
        else:
            self.do_add_local_forward(conn_tag, fw)

    def _prompt_rm_connection(self) -> None:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = self._pick_from_list("Remove Connection", tags)
        if selected:
            self.do_remove_connection(selected)

    def _prompt_rm_host(self) -> None:
        config = self.manager.list_config()
        hosts = [h for c in config.connections for h in c.pac_hosts]
        selected = self._pick_from_list("Remove Domain / IP / CIDR", hosts)
        if selected:
            self.do_remove_pac_host(selected)

    def _prompt_rm_local(self) -> None:
        config = self.manager.list_config()
        items = []
        port_map = {}
        for c in config.connections:
            for fw in c.forwards.local:
                label = f"[{c.tag}] :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}"
                items.append(label)
                port_map[label] = fw.src_port
        selected = self._pick_from_list("Remove Local Forward", items)
        if selected and selected in port_map:
            self.do_remove_local_forward(port_map[selected])

    def _prompt_rm_remote(self) -> None:
        config = self.manager.list_config()
        items = []
        port_map = {}
        for c in config.connections:
            for fw in c.forwards.remote:
                label = f"[{c.tag}] :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}"
                items.append(label)
                port_map[label] = fw.src_port
        selected = self._pick_from_list("Remove Remote Forward", items)
        if selected and selected in port_map:
            self.do_remove_remote_forward(port_map[selected])

    def _pick_from_list(self, title: str, items: list[str]) -> str | None:
        """Show a rumps window to pick one item from a list."""
        if not items:
            self.show_alert("Nothing to Remove", "The list is empty.")
            return None
        numbered = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(items))
        win = self._rumps.Window(
            message=f"{numbered}\n\nEnter number:",
            title=title,
            default_text="1",
            ok="Remove",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r = win.run()
        if r.clicked == 0:
            return None
        try:
            idx = int(r.text.strip()) - 1
            if 0 <= idx < len(items):
                return items[idx]
        except (ValueError, IndexError):
            pass
        return None

    def _confirm_reset(self) -> None:
        response = self._rumps.alert(
            title="Reset All?",
            message="This will stop all tunnels and delete the workspace. This cannot be undone.",
            ok="Reset",
            cancel="Cancel",
        )
        if response == 1:  # OK
            self.run_in_background(lambda: self.do_reset(), lambda _: None)

    def _show_about(self) -> None:
        self.show_output_dialog(
            "About SusOps",
            "SusOps — SSH Tunnel & PAC Manager\n\n"
            "GitHub: https://github.com/mashb1t/susops\n"
            "Copyright © Manuel Schmid",
        )

    def _on_quit(self, _sender) -> None:
        self.do_quit()
        self._rumps.quit_application()

    # ------------------------------------------------------------------ #
    # SSE listener
    # ------------------------------------------------------------------ #

    def _start_sse_listener(self) -> None:
        """Background thread: connect to SSE /events and update UI on events."""
        import time

        def _listen():
            backoff = 1.0
            while True:
                status_url = self.manager.get_status_url()
                if not status_url:
                    time.sleep(2.0)
                    continue
                try:
                    import urllib.request
                    req = urllib.request.Request(status_url)
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        backoff = 1.0
                        buf = ""
                        for raw in resp:
                            line = raw.decode("utf-8", errors="replace")
                            buf += line
                            if buf.endswith("\n\n"):
                                if "event: state" in buf:
                                    self.do_poll()
                                if "event: share" in buf:
                                    self._refresh_share_submenu()
                                buf = ""
                except Exception:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

        threading.Thread(target=_listen, daemon=True, name="susops-sse-mac").start()

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Start the rumps main loop."""
        self.do_poll()
        self.schedule_poll(5)
        self._start_sse_listener()
        self._app.run()


def main() -> None:
    app = SusOpsMacTray()
    app.run()
