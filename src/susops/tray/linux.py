"""Linux tray app — GTK3 + AyatanaAppIndicator3.

Requires: python-gobject, gtk3, libayatana-appindicator (system packages).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from susops.core.config import PortForward
from susops.core.types import ProcessState
from susops.tray.base import AbstractTrayApp, get_icon_path, get_ssh_hosts


def _is_dark_theme() -> bool:
    """Return True when the desktop colour scheme is dark."""
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if "dark" in out.lower():
            return True
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return "dark" in out.lower()
    except Exception:
        return False


def _get_icon_path(state: ProcessState, logo_style: str = "colored_glasses") -> str | None:
    """Return icon path for state, picking light/dark variant based on desktop theme."""
    variant = "light" if _is_dark_theme() else "dark"
    return get_icon_path(state, logo_style=logo_style, variant=variant)


def _is_valid_port(value: str) -> bool:
    return value.isdigit() and 1 <= int(value) <= 65535



def _polish_dialog(Gtk, dlg) -> None:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            aa = dlg.get_action_area()
            aa.set_margin_start(16)
            aa.set_margin_end(16)
            aa.set_margin_top(8)
            aa.set_margin_bottom(16)
            aa.set_spacing(6)
            aa.set_layout(Gtk.ButtonBoxStyle.EXPAND)
            aa.set_homogeneous(True)
        except Exception:
            pass


def _alert(Gtk, parent, title: str, body: str = "", msg_type=None) -> None:
    if msg_type is None:
        msg_type = Gtk.MessageType.INFO
    dlg = Gtk.MessageDialog(
        transient_for=parent, modal=True,
        message_type=msg_type,
        buttons=Gtk.ButtonsType.CLOSE,
        text=title,
    )
    if body:
        dlg.format_secondary_text(body)
    _polish_dialog(Gtk, dlg)
    dlg.run()
    dlg.destroy()


def _labeled_grid(Gtk, fields: list):
    grid = Gtk.Grid(
        column_spacing=12, row_spacing=8,
        margin_start=16, margin_end=16,
        margin_top=16, margin_bottom=16,
    )
    widgets = {}
    for row, (key, label, widget) in enumerate(fields):
        lbl = Gtk.Label(label=label, xalign=1.0)
        lbl.set_width_chars(22)
        grid.attach(lbl, 0, row, 1, 1)
        widget.set_hexpand(True)
        grid.attach(widget, 1, row, 1, 1)
        widgets[key] = widget
    return grid, widgets


class SusOpsLinuxTray(AbstractTrayApp):
    """GTK3 system tray application."""

    def __init__(self) -> None:
        super().__init__()
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3, Gtk, GLib
        self._Gtk = Gtk
        self._GLib = GLib
        self._AyatanaAppIndicator3 = AyatanaAppIndicator3

        # Apply dark-mode preference to dialogs immediately
        self._apply_gtk_theme_preference()

        self._root = Gtk.Window()
        self._root.set_title("SusOps")

        self._indicator = AyatanaAppIndicator3.Indicator.new(
            "susops",
            _get_icon_path(ProcessState.STOPPED) or "application-exit",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

        self._menu = Gtk.Menu()
        self._build_menu()
        self._indicator.set_menu(self._menu)

    # ------------------------------------------------------------------ #
    # AbstractTrayApp implementation
    # ------------------------------------------------------------------ #

    def _apply_gtk_theme_preference(self) -> None:
        """Tell GTK to use dark widgets when the desktop is in dark mode."""
        try:
            settings = self._Gtk.Settings.get_default()
            if settings is not None:
                settings.set_property("gtk-application-prefer-dark-theme", _is_dark_theme())
        except Exception:
            pass

    def update_icon(self, state: ProcessState) -> None:
        def _update():
            self._apply_gtk_theme_preference()
            logo_style = self.manager.app_config.logo_style.value.lower()
            icon_path = _get_icon_path(state, logo_style)
            if icon_path:
                self._indicator.set_icon_full(icon_path, state.value)
            return False
        self._GLib.idle_add(_update)

    def update_menu_sensitivity(self, state: ProcessState) -> None:
        running = state == ProcessState.RUNNING
        stopped = state == ProcessState.STOPPED

        def _update():
            if hasattr(self, "_item_start"):
                self._item_start.set_sensitive(not running)
            if hasattr(self, "_item_stop"):
                self._item_stop.set_sensitive(not stopped)
            if hasattr(self, "_item_restart"):
                self._item_restart.set_sensitive(not stopped)
            if hasattr(self, "_item_test_any"):
                self._item_test_any.set_sensitive(not stopped)
            if hasattr(self, "_item_test_all"):
                self._item_test_all.set_sensitive(not stopped)
            self._rebuild_status_item(state)
            return False

        self._GLib.idle_add(_update)

    def show_alert(self, title: str, msg: str) -> None:
        def _show():
            _alert(self._Gtk, self._root, title, msg)
            return False
        self._GLib.idle_add(_show)

    def show_output_dialog(self, title: str, output: str) -> None:
        def _show():
            Gtk = self._Gtk
            dlg = Gtk.Dialog(title=title, transient_for=self._root, modal=False)
            dlg.add_button("Close", Gtk.ResponseType.CLOSE)
            dlg.set_default_size(600, 380)
            dlg.connect("response", lambda d, _r: d.destroy())
            sw = Gtk.ScrolledWindow(
                vexpand=True,
                margin_start=12, margin_end=12,
                margin_top=12, margin_bottom=6,
            )
            tv = Gtk.TextView(
                editable=False,
                monospace=True,
                wrap_mode=Gtk.WrapMode.WORD_CHAR,
                left_margin=4,
            )
            tv.get_buffer().set_text(output)
            sw.add(tv)
            dlg.get_content_area().add(sw)
            _polish_dialog(Gtk, dlg)
            dlg.show_all()
            return False
        self._GLib.idle_add(_show)

    def run_in_background(self, fn: Callable, callback: Callable | None = None) -> None:
        def _worker():
            result = fn()
            if callback is not None:
                self._GLib.idle_add(callback, result)
        threading.Thread(target=_worker, daemon=True).start()

    def schedule_poll(self, interval_seconds: int) -> None:
        def _poll():
            self.do_poll()
            return True  # keep repeating
        self._GLib.timeout_add_seconds(interval_seconds, _poll)

    # ------------------------------------------------------------------ #
    # Menu building
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> None:
        Gtk = self._Gtk

        # ── Status row ────────────────────────────────────────────────────
        self._item_status = Gtk.MenuItem(label="SusOps: checking…")
        self._item_status.set_sensitive(False)
        self._menu.append(self._item_status)
        self._menu.append(Gtk.SeparatorMenuItem())

        # ── Settings ──────────────────────────────────────────────────────
        i = Gtk.MenuItem(label="Settings…")
        i.connect("activate", lambda _: self._on_settings())
        self._menu.append(i)
        self._menu.append(Gtk.SeparatorMenuItem())

        # ── Add submenu ───────────────────────────────────────────────────
        add_item = Gtk.MenuItem(label="Add")
        add_sub = Gtk.Menu()
        for label, cb in [
            ("Add Connection", self._on_add_connection),
            ("Add Domain / IP / CIDR", self._on_add_host),
            ("Add Local Forward", self._on_add_local),
            ("Add Remote Forward", self._on_add_remote),
        ]:
            si = Gtk.MenuItem(label=label)
            si.connect("activate", cb)
            add_sub.append(si)
        add_item.set_submenu(add_sub)
        self._menu.append(add_item)

        # ── Remove submenu ────────────────────────────────────────────────
        rm_item = Gtk.MenuItem(label="Remove")
        rm_sub = Gtk.Menu()
        for label, cb in [
            ("Remove Connection", self._on_rm_connection),
            ("Remove Domain / IP / CIDR", self._on_rm_host),
            ("Remove Local Forward", self._on_rm_local),
            ("Remove Remote Forward", self._on_rm_remote),
        ]:
            si = Gtk.MenuItem(label=label)
            si.connect("activate", cb)
            rm_sub.append(si)
        rm_item.set_submenu(rm_sub)
        self._menu.append(rm_item)

        # ── List All / Open Config ─────────────────────────────────────────
        i = Gtk.MenuItem(label="List All")
        i.connect("activate", lambda _: self.do_list_all())
        self._menu.append(i)

        i = Gtk.MenuItem(label="Open Config File")
        i.connect("activate", lambda _: self.do_open_config_file())
        self._menu.append(i)
        self._menu.append(Gtk.SeparatorMenuItem())

        # ── Proxy controls ────────────────────────────────────────────────
        self._item_start = Gtk.MenuItem(label="Start Proxy")
        self._item_start.connect("activate", lambda _: self.do_start())
        self._menu.append(self._item_start)

        self._item_stop = Gtk.MenuItem(label="Stop Proxy")
        self._item_stop.connect("activate", lambda _: self.do_stop())
        self._menu.append(self._item_stop)

        self._item_restart = Gtk.MenuItem(label="Restart Proxy")
        self._item_restart.connect("activate", lambda _: self.do_restart())
        self._menu.append(self._item_restart)
        self._menu.append(Gtk.SeparatorMenuItem())

        # ── Test submenu ──────────────────────────────────────────────────
        test_item = Gtk.MenuItem(label="Test")
        test_sub = Gtk.Menu()
        self._item_test_any = Gtk.MenuItem(label="Test Any")
        self._item_test_any.connect("activate", lambda _: self.do_test())
        test_sub.append(self._item_test_any)
        self._item_test_all = Gtk.MenuItem(label="Test All")
        self._item_test_all.connect("activate", lambda _: self.do_test())
        test_sub.append(self._item_test_all)
        test_item.set_submenu(test_sub)
        self._menu.append(test_item)

        # ── Show status ───────────────────────────────────────────────────
        i = Gtk.MenuItem(label="Show Status")
        i.connect("activate", lambda _: self.do_status())
        self._menu.append(i)

        # ── Launch Browser ────────────────────────────────────────────────
        self._browser_item = Gtk.MenuItem(label="Launch Browser")
        self._menu.append(self._browser_item)
        self._rebuild_browser_submenu()
        self._menu.append(Gtk.SeparatorMenuItem())

        # ── Reset All ─────────────────────────────────────────────────────
        i = Gtk.MenuItem(label="Reset All")
        i.connect("activate", lambda _: self._on_reset())
        self._menu.append(i)
        self._menu.append(Gtk.SeparatorMenuItem())

        # ── About / Quit ──────────────────────────────────────────────────
        i = Gtk.MenuItem(label="About SusOps")
        i.connect("activate", lambda _: self._on_about())
        self._menu.append(i)

        i = Gtk.MenuItem(label="Quit")
        i.connect("activate", self._on_quit)
        self._menu.append(i)

        self._menu.show_all()

    def _rebuild_browser_submenu(self) -> None:
        Gtk = self._Gtk
        browser_sub = Gtk.Menu()

        _BROWSER_DEFS = [
            ("Chrome", ["google-chrome", "google-chrome-stable"], True),
            ("Chromium", ["chromium", "chromium-browser"], True),
            ("Brave", ["brave-browser", "brave", "brave-browser-stable"], True),
            ("Vivaldi", ["vivaldi", "vivaldi-stable"], True),
            ("Edge", ["microsoft-edge", "microsoft-edge-stable"], True),
            ("Firefox", ["firefox", "firefox-bin"], False),
        ]
        found = []
        for name, exes, chromium in _BROWSER_DEFS:
            exe = next((shutil.which(e) for e in exes if shutil.which(e)), None)
            if exe:
                found.append((name, exe, chromium))

        if not found:
            ni = Gtk.MenuItem(label="No browsers found")
            ni.set_sensitive(False)
            browser_sub.append(ni)
        else:
            for name, exe, chromium in found:
                parent = Gtk.MenuItem(label=name)
                sub = Gtk.Menu()
                li = Gtk.MenuItem(label=f"Launch {name}")
                if chromium:
                    li.connect("activate", self._make_chromium_launch(exe))
                else:
                    li.connect("activate", self._make_firefox_launch(exe))
                sub.append(li)
                if chromium:
                    si = Gtk.MenuItem(label=f"Open {name} Proxy Settings")
                    si.connect("activate", self._make_chromium_settings(exe))
                    sub.append(si)
                parent.set_submenu(sub)
                browser_sub.append(parent)

        browser_sub.show_all()
        self._browser_item.set_submenu(browser_sub)

    def _make_chromium_launch(self, exe: str):
        def handler(_item):
            pac_url = self.manager.get_pac_url()
            if not pac_url:
                self._GLib.idle_add(
                    lambda: _alert(self._Gtk, self._root, "Proxy Not Running",
                                   "Start the proxy first so the PAC port is known.")
                )
                return
            try:
                subprocess.Popen([exe, f"--proxy-pac-url={pac_url}"])
            except Exception as exc:
                self.show_alert("Launch Failed", str(exc))
        return handler

    def _make_chromium_settings(self, exe: str):
        def handler(_item):
            try:
                subprocess.Popen([exe])
            except Exception:
                pass
            url = "chrome://net-internals/#proxy"
            def _show():
                dlg = self._Gtk.Dialog(title="Open Proxy Settings",
                                       transient_for=self._root, modal=True)
                dlg.add_button("_OK", self._Gtk.ResponseType.OK)
                dlg.set_default_response(self._Gtk.ResponseType.OK)
                box = dlg.get_content_area()
                box.set_spacing(8)
                box.set_margin_start(16)
                box.set_margin_end(16)
                box.set_margin_top(12)
                box.set_margin_bottom(8)
                box.add(self._Gtk.Label(label="Paste this URL into the address bar:", xalign=0.0))
                tv = self._Gtk.TextView()
                tv.get_buffer().set_text(url)
                tv.set_monospace(True)
                tv.set_hexpand(True)
                box.add(tv)
                dlg.show_all()
                buf = tv.get_buffer()
                buf.select_range(buf.get_start_iter(), buf.get_end_iter())
                tv.grab_focus()
                dlg.run()
                dlg.destroy()
                return False
            self._GLib.idle_add(_show)
        return handler

    def _make_firefox_launch(self, exe: str):
        def handler(_item):
            pac_url = self.manager.get_pac_url()
            if not pac_url:
                self.show_alert("Proxy Not Running", "Start the proxy first.")
                return
            profile_dir = self.manager.workspace / "firefox_profile"
            profile_dir.mkdir(exist_ok=True)
            (profile_dir / "user.js").write_text(
                f'user_pref("network.proxy.type", 2);\n'
                f'user_pref("network.proxy.autoconfig_url", "{pac_url}");\n'
                f'user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1");\n'
            )
            try:
                subprocess.Popen([exe, "-profile", str(profile_dir), "-no-remote"])
            except Exception as exc:
                self.show_alert("Launch Failed", str(exc))
        return handler

    def _rebuild_status_item(self, state: ProcessState) -> None:
        dot = {
            ProcessState.RUNNING: "🟢",
            ProcessState.STOPPED_PARTIALLY: "🟠",
            ProcessState.STOPPED: "⚫",
            ProcessState.ERROR: "🔴",
            ProcessState.INITIAL: "⚫",
        }.get(state, "⚫")
        self._item_status.set_label(f"{dot} SusOps: {state.value}")

    # ------------------------------------------------------------------ #
    # Dialog handlers
    # ------------------------------------------------------------------ #

    def _on_settings(self) -> None:
        self._GLib.idle_add(self._show_settings_dialog)

    def _show_settings_dialog(self) -> bool:
        from susops.core.types import LogoStyle
        Gtk = self._Gtk
        ac = self.manager.app_config
        dlg = Gtk.Dialog(title="Settings", transient_for=self._root, modal=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Save", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(360, -1)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10,
                        margin_start=16, margin_end=16,
                        margin_top=16, margin_bottom=16)
        dlg.get_content_area().add(grid)

        row = 0

        # Launch at Login
        _AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "org.susops.App.desktop"
        lbl = Gtk.Label(label="Launch at Login:", xalign=1.0)
        lbl.set_width_chars(24)
        grid.attach(lbl, 0, row, 1, 1)
        sw_login = Gtk.Switch(halign=Gtk.Align.START)
        sw_login.set_active(_AUTOSTART_FILE.exists())
        grid.attach(sw_login, 1, row, 1, 1)
        row += 1

        # Stop Proxy On Quit
        lbl = Gtk.Label(label="Stop Proxy On Quit:", xalign=1.0)
        lbl.set_width_chars(24)
        grid.attach(lbl, 0, row, 1, 1)
        sw_stop = Gtk.Switch(halign=Gtk.Align.START)
        sw_stop.set_active(ac.stop_on_quit)
        grid.attach(sw_stop, 1, row, 1, 1)
        row += 1

        # Random SSH Ports On Start
        lbl = Gtk.Label(label="Random SSH Ports On Start:", xalign=1.0)
        lbl.set_width_chars(24)
        grid.attach(lbl, 0, row, 1, 1)
        sw_eph = Gtk.Switch(halign=Gtk.Align.START)
        sw_eph.set_active(ac.ephemeral_ports)
        grid.attach(sw_eph, 1, row, 1, 1)
        row += 1

        # Logo Style
        lbl = Gtk.Label(label="Logo Style:", xalign=1.0)
        lbl.set_width_chars(24)
        grid.attach(lbl, 0, row, 1, 1)
        combo_logo = Gtk.ComboBoxText(halign=Gtk.Align.START)
        logo_styles = list(LogoStyle)
        for style in logo_styles:
            combo_logo.append(style.value, style.value.replace("_", " ").title())
        combo_logo.set_active_id(ac.logo_style.value)
        combo_logo.connect("changed", lambda cb: self._on_logo_style_preview(cb, logo_styles))
        grid.attach(combo_logo, 1, row, 1, 1)
        row += 1

        # PAC Server Port
        lbl = Gtk.Label(label="PAC Server Port:", xalign=1.0)
        lbl.set_width_chars(24)
        grid.attach(lbl, 0, row, 1, 1)
        entry_pac = Gtk.Entry(activates_default=True)
        pac_val = self.manager.config.pac_server_port
        entry_pac.set_text(str(pac_val) if pac_val else "")
        entry_pac.set_placeholder_text("auto (0)")
        grid.attach(entry_pac, 1, row, 1, 1)

        _polish_dialog(Gtk, dlg)
        dlg.show_all()

        _saved_logo = ac.logo_style  # track original for cancel revert

        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                # Revert live-preview logo change
                self.manager.update_app_config(logo_style=_saved_logo)
                self.update_icon(self.state)
                break

            pac_text = entry_pac.get_text().strip() or "0"
            if pac_text != "0" and not _is_valid_port(pac_text):
                _alert(Gtk, dlg, "Invalid Port", "PAC Server Port must be between 1 and 65535.")
                continue

            new_logo = logo_styles[combo_logo.get_active()] if combo_logo.get_active() >= 0 else _saved_logo
            self.manager.update_app_config(
                stop_on_quit=sw_stop.get_active(),
                ephemeral_ports=sw_eph.get_active(),
                logo_style=new_logo,
            )
            self.manager._reload_config()
            self.manager.config = self.manager.config.model_copy(
                update={"pac_server_port": int(pac_text)}
            )
            self.manager._save()
            self._apply_autostart(sw_login.get_active())
            self.update_icon(self.state)
            break

        dlg.destroy()
        return False

    def _on_logo_style_preview(self, combo, logo_styles: list) -> None:
        """Live-preview the selected logo style — update icon immediately, no disk write."""
        idx = combo.get_active()
        if not (0 <= idx < len(logo_styles)):
            return
        # Update in-memory only so the preview is instant (saved on OK)
        self.manager.config = self.manager.config.model_copy(
            update={"susops_app": self.manager.config.susops_app.model_copy(
                update={"logo_style": logo_styles[idx]}
            )}
        )
        icon_path = _get_icon_path(self.state, logo_styles[idx].value.lower())
        if icon_path:
            self._indicator.set_icon_full(icon_path, self.state.value)

    def _apply_autostart(self, enable: bool) -> None:
        autostart_dir = Path.home() / ".config" / "autostart"
        autostart_file = autostart_dir / "org.susops.App.desktop"
        if enable:
            autostart_dir.mkdir(parents=True, exist_ok=True)
            import sys
            exec_path = sys.executable
            autostart_file.write_text(
                "[Desktop Entry]\n"
                "Name=SusOps\n"
                f"Exec={exec_path} -m susops.tray.linux\n"
                "Icon=org.susops.App\n"
                "Type=Application\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
        else:
            autostart_file.unlink(missing_ok=True)

    def _on_add_connection(self, _) -> None:
        self._GLib.idle_add(self._show_add_connection_dialog)

    def _show_add_connection_dialog(self) -> bool:
        Gtk = self._Gtk
        dlg = Gtk.Dialog(title="Add Connection", transient_for=self._root, modal=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Add", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(440, -1)

        tag_entry = Gtk.Entry(activates_default=True)
        host_combo = Gtk.ComboBoxText(has_entry=True)
        for h in get_ssh_hosts():
            host_combo.append_text(h)
        host_combo.get_child().set_placeholder_text("hostname, IP, or SSH alias")
        host_combo.get_child().set_activates_default(True)
        port_entry = Gtk.Entry(placeholder_text="auto if blank", activates_default=True)

        grid, _ = _labeled_grid(Gtk, [
            ("tag", "Connection Tag *:", tag_entry),
            ("host", "SSH Host *:", host_combo),
            ("port", "SOCKS Proxy Port (optional):", port_entry),
        ])
        dlg.get_content_area().add(grid)
        _polish_dialog(Gtk, dlg)
        dlg.show_all()

        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                break
            tag = tag_entry.get_text().strip()
            host = (host_combo.get_active_text() or "").strip()
            port = port_entry.get_text().strip()

            if not tag:
                _alert(Gtk, dlg, "Missing Field", "Connection Tag must not be empty.", Gtk.MessageType.ERROR)
                continue
            if not host:
                _alert(Gtk, dlg, "Missing Field", "SSH Host must not be empty.", Gtk.MessageType.ERROR)
                continue
            if port and not _is_valid_port(port):
                _alert(Gtk, dlg, "Invalid Port", "SOCKS Proxy Port must be between 1 and 65535.", Gtk.MessageType.ERROR)
                continue

            dlg.destroy()
            port_int = int(port) if port else 0
            self.do_add_connection(tag, host, port_int)
            return False

        dlg.destroy()
        return False

    def _on_add_host(self, _) -> None:
        self._GLib.idle_add(self._show_add_host_dialog)

    def _show_add_host_dialog(self) -> bool:
        Gtk = self._Gtk
        dlg = Gtk.Dialog(title="Add Domain / IP / CIDR", transient_for=self._root, modal=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Add", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(380, -1)

        conn_combo = Gtk.ComboBoxText()
        tags = [c.tag for c in self.manager.list_config().connections]
        for t in tags:
            conn_combo.append_text(t)
        if tags:
            conn_combo.set_active(0)
        host_entry = Gtk.Entry(activates_default=True)

        grid, _ = _labeled_grid(Gtk, [
            ("conn", "Connection *:", conn_combo),
            ("host", "Host / IP / CIDR *:", host_entry),
        ])
        dlg.get_content_area().add(grid)
        _polish_dialog(Gtk, dlg)
        dlg.show_all()

        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                break
            tag = conn_combo.get_active_text() or ""
            host = host_entry.get_text().strip()
            if not tag:
                _alert(Gtk, dlg, "No Connection", "Add a connection first.", Gtk.MessageType.ERROR)
                continue
            if not host:
                _alert(Gtk, dlg, "Missing Field", "Host must not be empty.", Gtk.MessageType.ERROR)
                continue
            dlg.destroy()
            self.do_add_pac_host(host, conn_tag=tag)
            return False

        dlg.destroy()
        return False

    def _on_add_local(self, _) -> None:
        self._GLib.idle_add(self._show_add_local_dialog)

    def _show_add_local_dialog(self) -> bool:
        Gtk = self._Gtk
        BIND_ADDRESSES = ["localhost", "172.17.0.1", "0.0.0.0"]
        dlg = Gtk.Dialog(title="Add Local Forward", transient_for=self._root, modal=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Add", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(420, -1)

        conn_combo = Gtk.ComboBoxText()
        tags = [c.tag for c in self.manager.list_config().connections]
        for t in tags:
            conn_combo.append_text(t)
        if tags:
            conn_combo.set_active(0)
        tag_entry = Gtk.Entry(placeholder_text="optional", activates_default=True)
        src_port_entry = Gtk.Entry(placeholder_text="e.g. 8080", activates_default=True)
        dst_port_entry = Gtk.Entry(placeholder_text="e.g. 80", activates_default=True)
        src_addr_combo = Gtk.ComboBoxText(has_entry=True)
        for addr in BIND_ADDRESSES:
            src_addr_combo.append_text(addr)
        src_addr_combo.get_child().set_text("localhost")
        dst_addr_combo = Gtk.ComboBoxText(has_entry=True)
        for addr in BIND_ADDRESSES:
            dst_addr_combo.append_text(addr)
        dst_addr_combo.get_child().set_text("localhost")

        grid, _ = _labeled_grid(Gtk, [
            ("conn", "Connection *:", conn_combo),
            ("tag", "Tag (optional):", tag_entry),
            ("src", "Forward Local Port *:", src_port_entry),
            ("dst", "To Remote Port *:", dst_port_entry),
            ("src_addr", "Local Bind (optional):", src_addr_combo),
            ("dst_addr", "Remote Bind (optional):", dst_addr_combo),
        ])
        dlg.get_content_area().add(grid)
        _polish_dialog(Gtk, dlg)
        dlg.show_all()

        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                break
            conn_tag = conn_combo.get_active_text() or ""
            tag = tag_entry.get_text().strip()
            src = src_port_entry.get_text().strip()
            dst = dst_port_entry.get_text().strip()
            src_addr = src_addr_combo.get_child().get_text().strip() or "localhost"
            dst_addr = dst_addr_combo.get_child().get_text().strip() or "localhost"

            if not conn_tag:
                _alert(Gtk, dlg, "No Connection", "Add a connection first.", Gtk.MessageType.ERROR)
                continue
            if not _is_valid_port(src):
                _alert(Gtk, dlg, "Invalid Port", "Forward Local Port must be 1–65535.", Gtk.MessageType.ERROR)
                continue
            if not _is_valid_port(dst):
                _alert(Gtk, dlg, "Invalid Port", "To Remote Port must be 1–65535.", Gtk.MessageType.ERROR)
                continue

            fw = PortForward(src_addr=src_addr, src_port=int(src), dst_addr=dst_addr, dst_port=int(dst), tag=tag or None)
            dlg.destroy()
            self.do_add_local_forward(conn_tag, fw)
            return False

        dlg.destroy()
        return False

    def _on_add_remote(self, _) -> None:
        self._GLib.idle_add(self._show_add_remote_dialog)

    def _show_add_remote_dialog(self) -> bool:
        Gtk = self._Gtk
        dlg = Gtk.Dialog(title="Add Remote Forward", transient_for=self._root, modal=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Add", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(420, -1)

        BIND_ADDRESSES = ["localhost", "172.17.0.1", "0.0.0.0"]
        conn_combo = Gtk.ComboBoxText()
        tags = [c.tag for c in self.manager.list_config().connections]
        for t in tags:
            conn_combo.append_text(t)
        if tags:
            conn_combo.set_active(0)
        tag_entry = Gtk.Entry(placeholder_text="optional", activates_default=True)
        remote_port_entry = Gtk.Entry(placeholder_text="e.g. 8080", activates_default=True)
        local_port_entry = Gtk.Entry(placeholder_text="e.g. 3000", activates_default=True)
        src_addr_combo = Gtk.ComboBoxText(has_entry=True)
        for addr in BIND_ADDRESSES:
            src_addr_combo.append_text(addr)
        src_addr_combo.get_child().set_text("localhost")
        dst_addr_combo = Gtk.ComboBoxText(has_entry=True)
        for addr in BIND_ADDRESSES:
            dst_addr_combo.append_text(addr)
        dst_addr_combo.get_child().set_text("localhost")

        grid, _ = _labeled_grid(Gtk, [
            ("conn", "Connection *:", conn_combo),
            ("tag", "Tag (optional):", tag_entry),
            ("rport", "Forward Remote Port *:", remote_port_entry),
            ("lport", "To Local Port *:", local_port_entry),
            ("src_addr", "Remote Bind (optional):", src_addr_combo),
            ("dst_addr", "Local Bind (optional):", dst_addr_combo),
        ])
        dlg.get_content_area().add(grid)
        _polish_dialog(Gtk, dlg)
        dlg.show_all()

        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                break
            conn_tag = conn_combo.get_active_text() or ""
            tag = tag_entry.get_text().strip()
            rport = remote_port_entry.get_text().strip()
            lport = local_port_entry.get_text().strip()
            src_addr = src_addr_combo.get_child().get_text().strip() or "localhost"
            dst_addr = dst_addr_combo.get_child().get_text().strip() or "localhost"

            if not conn_tag:
                _alert(Gtk, dlg, "No Connection", "Add a connection first.", Gtk.MessageType.ERROR)
                continue
            if not _is_valid_port(rport):
                _alert(Gtk, dlg, "Invalid Port", "Forward Remote Port must be 1–65535.", Gtk.MessageType.ERROR)
                continue
            if not _is_valid_port(lport):
                _alert(Gtk, dlg, "Invalid Port", "To Local Port must be 1–65535.", Gtk.MessageType.ERROR)
                continue

            fw = PortForward(src_addr=src_addr, src_port=int(rport), dst_addr=dst_addr, dst_port=int(lport), tag=tag or None)
            dlg.destroy()
            self.do_add_remote_forward(conn_tag, fw)
            return False

        dlg.destroy()
        return False

    def _on_rm_connection(self, _) -> None:
        self._GLib.idle_add(self._show_rm_connection_dialog)

    def _show_rm_connection_dialog(self) -> bool:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = self._pick_from_list("Remove Connection", "Connection Tag:", tags)
        if selected:
            self.do_remove_connection(selected)
        return False

    def _on_rm_host(self, _) -> None:
        self._GLib.idle_add(self._show_rm_host_dialog)

    def _show_rm_host_dialog(self) -> bool:
        config = self.manager.list_config()
        hosts = [h for c in config.connections for h in c.pac_hosts]
        selected = self._pick_from_list("Remove Domain / IP / CIDR", "Host:", hosts)
        if selected:
            self.do_remove_pac_host(selected)
        return False

    def _on_rm_local(self, _) -> None:
        self._GLib.idle_add(self._show_rm_local_dialog)

    def _show_rm_local_dialog(self) -> bool:
        config = self.manager.list_config()
        items = []
        for c in config.connections:
            for fw in c.forwards.local:
                items.append(f"[{c.tag}] :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
        selected = self._pick_from_list("Remove Local Forward", "Local Forward:", items)
        if selected:
            m = re.search(r":(\d+)→", selected)
            if m:
                self.do_remove_local_forward(int(m.group(1)))
        return False

    def _on_rm_remote(self, _) -> None:
        self._GLib.idle_add(self._show_rm_remote_dialog)

    def _show_rm_remote_dialog(self) -> bool:
        config = self.manager.list_config()
        items = []
        for c in config.connections:
            for fw in c.forwards.remote:
                items.append(f"[{c.tag}] :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
        selected = self._pick_from_list("Remove Remote Forward", "Remote Forward:", items)
        if selected:
            m = re.search(r":(\d+)→", selected)
            if m:
                self.do_remove_remote_forward(int(m.group(1)))
        return False

    def _pick_from_list(self, title: str, label: str, items: list[str]) -> str | None:
        """Show a dialog with a dropdown list. Returns selected item or None."""
        Gtk = self._Gtk
        if not items:
            _alert(Gtk, self._root, "Nothing to Remove", "The list is empty.")
            return None

        dlg = Gtk.Dialog(title=title, transient_for=self._root, modal=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Remove", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(340, -1)

        combo = Gtk.ComboBoxText(hexpand=True)
        for item in items:
            combo.append_text(item)
        combo.set_active(0)

        grid, _ = _labeled_grid(Gtk, [(label, label, combo)])
        dlg.get_content_area().add(grid)
        _polish_dialog(Gtk, dlg)
        dlg.show_all()

        resp = dlg.run()
        selected = combo.get_active_text() if resp == Gtk.ResponseType.OK else None
        dlg.destroy()
        return selected

    def _on_reset(self) -> None:
        def _ask():
            Gtk = self._Gtk
            dlg = Gtk.MessageDialog(
                transient_for=self._root, modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.NONE,
                text="Reset All?",
            )
            dlg.format_secondary_text(
                "This will stop all tunnels and delete the workspace. This cannot be undone."
            )
            dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Reset", Gtk.ResponseType.OK)
            dlg.set_default_response(Gtk.ResponseType.CANCEL)
            _polish_dialog(Gtk, dlg)
            resp = dlg.run()
            dlg.destroy()
            if resp == Gtk.ResponseType.OK:
                self.run_in_background(
                    lambda: self.do_reset(),
                    lambda _: None,
                )
            return False
        self._GLib.idle_add(_ask)

    def _on_about(self) -> None:
        def _show():
            Gtk = self._Gtk
            dlg = Gtk.Dialog(title="About SusOps", transient_for=self._root, modal=True)
            dlg.add_button("_Close", Gtk.ResponseType.CLOSE)
            dlg.set_default_size(280, -1)
            box = dlg.get_content_area()
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                           margin_start=20, margin_end=20,
                           margin_top=16, margin_bottom=12,
                           halign=Gtk.Align.CENTER)
            box.add(vbox)
            name_lbl = Gtk.Label()
            name_lbl.set_markup("<b><big>SusOps</big></b>")
            vbox.pack_start(name_lbl, False, False, 2)
            desc = Gtk.Label(label="SSH Tunnel & PAC Manager")
            desc.get_style_context().add_class("dim-label")
            vbox.pack_start(desc, False, False, 0)
            for text, url in [
                ("GitHub", "https://github.com/mashb1t/susops"),
                ("Report a Bug", "https://github.com/mashb1t/susops/issues/new"),
            ]:
                btn = Gtk.LinkButton(uri=url, label=text)
                vbox.pack_start(btn, False, False, 0)
            copy_lbl = Gtk.Label(label="Copyright © Manuel Schmid")
            copy_lbl.get_style_context().add_class("dim-label")
            vbox.pack_start(copy_lbl, False, False, 4)
            _polish_dialog(Gtk, dlg)
            dlg.show_all()
            dlg.run()
            dlg.destroy()
            return False
        self._GLib.idle_add(_show)

    def _on_quit(self, _widget) -> None:
        self.do_quit()
        self._Gtk.main_quit()

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Start the GTK main loop."""
        self.do_poll()
        self.schedule_poll(5)
        self._Gtk.main()


def main() -> None:
    app = SusOpsLinuxTray()
    app.run()
