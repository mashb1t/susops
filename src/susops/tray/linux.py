"""Linux tray app — GTK3 + AyatanaAppIndicator3.

Requires: python-gobject, gtk3, libayatana-appindicator (system packages).
"""
from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from susops.core.config import PortForward
from susops.core.ports import is_port_free, validate_port
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


def _is_valid_port(value: str, allow_zero: bool = False) -> bool:
    if not value.isdigit():
        return False
    return validate_port(int(value), allow_zero=allow_zero)


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
        if state == getattr(self, "_last_menu_state", None):
            return  # nothing changed — skip idle_add entirely
        self._last_menu_state = state
        running = state == ProcessState.RUNNING
        stopped = state == ProcessState.STOPPED

        def _update():
            if hasattr(self, "_item_start"):
                self._item_start.set_sensitive(not running)
            if hasattr(self, "_item_stop"):
                self._item_stop.set_sensitive(not stopped)
            if hasattr(self, "_item_restart"):
                self._item_restart.set_sensitive(not stopped)
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

    def show_live_logs(self, get_text, *, title: str = "Logs",
                       interval_ms: int = 1000) -> None:
        """Non-modal, auto-refreshing log window that tails get_text().

        Polls get_text() every interval_ms; if the text changed, replaces the
        buffer and scrolls to the bottom. The user can keep using the tray
        menu while it's open. Each line is colored via the shared log_style
        rules to match the TUI's Logs tab.
        """
        Gtk = self._Gtk
        GLib = self._GLib
        from susops.core.log_style import style_log_line

        def _show():
            dlg = Gtk.Dialog(title=title, transient_for=self._root, modal=False)
            dlg.set_default_size(912, 513)

            # Scroll view fills the dialog edge-to-edge; dismissal is via the
            # titlebar close (X) only — no explicit Close button.
            sw = Gtk.ScrolledWindow(vexpand=True)
            tv = Gtk.TextView(
                editable=False,
                monospace=True,
                wrap_mode=Gtk.WrapMode.NONE,
                left_margin=0,
            )
            buf = tv.get_buffer()

            # Register color tags. GTK named foreground colors look reasonable
            # against both light and dark themes.
            tag_table = {
                "tag": {"foreground": "#3DB6C9", "weight": 700},
                "ok": {"foreground": "#2EA043"},
                "warn": {"foreground": "#D29922"},
                "err": {"foreground": "#F85149", "weight": 700},
                "dim": {"foreground": "#7D8590"},
                "info": {"foreground": "#58A6FF"},
            }
            tags: dict[str, object] = {}
            for label, props in tag_table.items():
                tags[label] = buf.create_tag(label, **props)

            sw.add(tv)
            dlg.get_content_area().add(sw)

            state = {"closed": False, "last": None}

            def _autoscroll():
                end_iter = buf.get_end_iter()
                tv.scroll_to_iter(end_iter, 0.0, False, 0.0, 1.0)

            def _apply_colored(text: str) -> None:
                buf.set_text("")
                end = buf.get_end_iter()
                for i, line in enumerate(text.split("\n")):
                    for chunk, label in style_log_line(line):
                        if not chunk:
                            continue
                        tag = tags.get(label) if label else None
                        if tag is None:
                            buf.insert(end, chunk)
                        else:
                            buf.insert_with_tags(end, chunk, tag)
                    if i < len(text.split("\n")) - 1:
                        buf.insert(end, "\n")

            def _refresh():
                if state["closed"]:
                    return False
                try:
                    text = get_text()
                except Exception as exc:
                    text = f"(log fetch failed: {exc})"
                if text != state["last"]:
                    state["last"] = text
                    _apply_colored(text)
                    GLib.idle_add(_autoscroll)
                return True  # keep ticking

            def _on_response(d, _r):
                state["closed"] = True
                d.destroy()

            dlg.connect("response", _on_response)
            dlg.show_all()
            _refresh()  # initial fill
            GLib.timeout_add(interval_ms, _refresh)
            return False

        GLib.idle_add(_show)

    def run_in_background(self, fn: Callable, callback: Callable | None = None) -> None:
        def _worker():
            result = fn()
            if callback is not None:
                self._GLib.idle_add(callback, result)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Menu building
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> None:
        Gtk = self._Gtk
        self._active_shares: list = []

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

        # ── Manage submenu ────────────────────────────────────────────────
        manage_item = Gtk.MenuItem(label="Manage")
        manage_sub = Gtk.Menu()
        for label, cb in [
            ("Toggle Connection Enabled…", self._on_toggle_connection),
            ("Toggle Domain Enabled…", self._on_toggle_domain),
            ("Toggle Forward Enabled…", self._on_toggle_forward),
            None,  # separator
            ("Start Connection…", self._on_start_connection),
            ("Stop Connection…", self._on_stop_connection),
            ("Restart Connection…", self._on_restart_connection),
        ]:
            if label is None:
                manage_sub.append(Gtk.SeparatorMenuItem())
            else:
                si = Gtk.MenuItem(label=label)
                si.connect("activate", cb)
                manage_sub.append(si)
        manage_item.set_submenu(manage_sub)
        self._menu.append(manage_item)

        # ── Open Config ───────────────────────────────────────────────────
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
        ti_conn = Gtk.MenuItem(label="Test Connection…")
        ti_conn.connect("activate", self._on_test_connection)
        test_sub.append(ti_conn)
        ti_domain = Gtk.MenuItem(label="Test Domain…")
        ti_domain.connect("activate", self._on_test_domain)
        test_sub.append(ti_domain)
        ti_fwd = Gtk.MenuItem(label="Test Forward…")
        ti_fwd.connect("activate", self._on_test_forward)
        test_sub.append(ti_fwd)
        test_sub.append(Gtk.SeparatorMenuItem())
        self._item_test_all = Gtk.MenuItem(label="Test All PAC Hosts")
        self._item_test_all.connect("activate", lambda _: self.do_test())
        test_sub.append(self._item_test_all)
        test_item.set_submenu(test_sub)
        self._menu.append(test_item)

        # ── Show status ───────────────────────────────────────────────────
        i = Gtk.MenuItem(label="Show Status")
        i.connect("activate", lambda _: self.do_status())
        self._menu.append(i)

        # ── Show logs ─────────────────────────────────────────────────────
        i = Gtk.MenuItem(label="Show Logs")
        i.connect("activate", lambda _: self.do_logs())
        self._menu.append(i)

        # ── Launch Browser ────────────────────────────────────────────────
        self._browser_item = Gtk.MenuItem(label="Launch Browser")
        self._menu.append(self._browser_item)
        self._rebuild_browser_submenu()
        self._menu.append(Gtk.SeparatorMenuItem())

        # ── File Transfer submenu ─────────────────────────────────────────
        ft_item = Gtk.MenuItem(label="File Transfer")
        self._ft_sub = Gtk.Menu()
        si = Gtk.MenuItem(label="Share File…")
        si.connect("activate", lambda _: self._on_share_file())
        self._ft_sub.append(si)
        si2 = Gtk.MenuItem(label="Fetch File…")
        si2.connect("activate", lambda _: self._on_fetch_file())
        self._ft_sub.append(si2)
        self._ft_sep = Gtk.SeparatorMenuItem()
        self._ft_sub.append(self._ft_sep)
        ft_item.set_submenu(self._ft_sub)
        self._menu.append(ft_item)
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

        # Detection delegated to susops.core.browsers — same table used by
        # the macOS tray and the TUI so a new browser only needs registering
        # in one place.
        from susops.core.browsers import detect_browsers
        found = detect_browsers()

        if not found:
            ni = Gtk.MenuItem(label="No browsers found")
            ni.set_sensitive(False)
            browser_sub.append(ni)
        else:
            for browser in found:
                parent = Gtk.MenuItem(label=browser.name)
                sub = Gtk.Menu()
                li = Gtk.MenuItem(label=f"Launch {browser.name}")
                li.connect("activate", self._make_browser_activate(browser))
                sub.append(li)
                if browser.is_chromium:
                    si = Gtk.MenuItem(label=f"Open {browser.name} Proxy Settings")
                    si.connect("activate",
                               self._make_browser_activate(browser, settings_only=True))
                    sub.append(si)
                parent.set_submenu(sub)
                browser_sub.append(parent)

        browser_sub.show_all()
        self._browser_item.set_submenu(browser_sub)

    def _make_browser_activate(self, browser, *, settings_only: bool = False):
        """GTK activate handler delegating to the shared base launch policy.

        Passes the full detected Browser through (preserving launch_cmd), so
        the shared do_launch_browser owns the PAC guard + profile path.
        """
        def handler(_item):
            self.do_launch_browser(browser, settings_only=settings_only)

        return handler

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

    def _refresh_share_submenu(self) -> bool:
        """Rebuild the dynamic share items in the File Transfer submenu only when changed."""
        from pathlib import Path as _Path
        Gtk = self._Gtk
        new_shares = self.manager.list_shares()

        def _share_key(info):
            return (info.port, info.running, info.file_path)

        old_keys = [_share_key(s) for s in self._active_shares]
        new_keys = [_share_key(s) for s in new_shares]
        if old_keys == new_keys:
            return False  # nothing changed — don't touch the menu

        self._active_shares = new_shares

        # Remove old dynamic items (everything after the separator)
        sep_reached = False
        for child in list(self._ft_sub.get_children()):
            if sep_reached:
                self._ft_sub.remove(child)
            if child is self._ft_sep:
                sep_reached = True

        self._ft_sep.set_visible(bool(self._active_shares))

        for info in self._active_shares:
            name = _Path(info.file_path).name
            dot = "●" if info.running else "○"
            label = f"{dot} {name} ({info.port})"
            item = Gtk.MenuItem(label=label)
            item.connect("activate", self._make_share_info_handler(info))
            self._ft_sub.append(item)

        self._ft_sub.show_all()
        return False

    def do_poll(self) -> None:
        super().do_poll()
        self._GLib.idle_add(self._refresh_share_submenu)

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

        # Restore File Shares On Start
        lbl = Gtk.Label(label="Restore Shares On Start:", xalign=1.0)
        lbl.set_width_chars(24)
        grid.attach(lbl, 0, row, 1, 1)
        sw_restore = Gtk.Switch(halign=Gtk.Align.START)
        sw_restore.set_active(ac.restore_shares_on_start)
        grid.attach(sw_restore, 1, row, 1, 1)
        row += 1

        # Show Bandwidth In Tray
        lbl = Gtk.Label(label="Show Bandwidth In Tray:", xalign=1.0)
        lbl.set_width_chars(24)
        grid.attach(lbl, 0, row, 1, 1)
        sw_bw = Gtk.Switch(halign=Gtk.Align.START)
        sw_bw.set_active(ac.tray_show_bandwidth)
        grid.attach(sw_bw, 1, row, 1, 1)
        row += 1

        # Desktop Notifications
        lbl = Gtk.Label(label="Desktop Notifications:", xalign=1.0)
        lbl.set_width_chars(24)
        grid.attach(lbl, 0, row, 1, 1)
        sw_notif = Gtk.Switch(halign=Gtk.Align.START)
        sw_notif.set_active(ac.notifications_enabled)
        grid.attach(sw_notif, 1, row, 1, 1)
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

        # Server ports — RPC + SSE require daemon restart to apply, PAC is
        # hot-restarted by the facade. Shown before the existing PAC field.
        lbl = Gtk.Label(label="RPC Server Port:", xalign=1.0)
        lbl.set_width_chars(24)
        grid.attach(lbl, 0, row, 1, 1)
        entry_rpc = Gtk.Entry(activates_default=True)
        rpc_val = self.manager.config.rpc_server_port
        entry_rpc.set_text(str(rpc_val) if rpc_val else "")
        entry_rpc.set_placeholder_text("auto (0) — restart daemon to apply")
        grid.attach(entry_rpc, 1, row, 1, 1)
        row += 1

        lbl = Gtk.Label(label="SSE Server Port:", xalign=1.0)
        lbl.set_width_chars(24)
        grid.attach(lbl, 0, row, 1, 1)
        entry_sse = Gtk.Entry(activates_default=True)
        sse_val = self.manager.config.status_server_port
        entry_sse.set_text(str(sse_val) if sse_val else "")
        entry_sse.set_placeholder_text("auto (0) — restart daemon to apply")
        grid.attach(entry_sse, 1, row, 1, 1)
        row += 1

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

            # Validate all three ports together — short-circuit on first
            # invalid so the user keeps their other edits in the dialog.
            cfg = self.manager.config
            port_specs = [
                ("RPC", entry_rpc, cfg.rpc_server_port),
                ("SSE", entry_sse, cfg.status_server_port),
                ("PAC", entry_pac, cfg.pac_server_port),
            ]
            port_values: dict[str, int] = {}
            invalid = False
            for label, entry, current in port_specs:
                text = entry.get_text().strip() or "0"
                if text != "0" and not _is_valid_port(text):
                    _alert(Gtk, dlg, "Invalid Port",
                           f"{label} Server Port must be between 1 and 65535.")
                    invalid = True
                    break
                n = int(text)
                if n != 0 and n != current and not is_port_free(n):
                    _alert(Gtk, dlg, "Port In Use", f"Port {n} is already in use.")
                    invalid = True
                    break
                port_values[label] = n
            if invalid:
                continue

            new_logo = logo_styles[combo_logo.get_active()] if combo_logo.get_active() >= 0 else _saved_logo
            self.manager.update_app_config(
                stop_on_quit=sw_stop.get_active(),
                ephemeral_ports=sw_eph.get_active(),
                restore_shares_on_start=sw_restore.get_active(),
                tray_show_bandwidth=sw_bw.get_active(),
                notifications_enabled=sw_notif.get_active(),
                logo_style=new_logo,
            )
            self.refresh_bandwidth_title()
            self.manager.update_config(
                rpc_server_port=port_values["RPC"],
                status_server_port=port_values["SSE"],
                pac_server_port=port_values["PAC"],
            )
            self._apply_autostart(sw_login.get_active())
            self.update_icon(self.state)
            break

        dlg.destroy()
        return False

    def _on_logo_style_preview(self, combo, logo_styles: list) -> None:
        """Live-preview the selected logo style — icon swap only, no config write.

        The icon path is computed directly from the chosen style enum; we don't
        need to mutate manager state for the preview. The actual save happens
        in the Settings handler via update_app_config().
        """
        idx = combo.get_active()
        if not (0 <= idx < len(logo_styles)):
            return
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
            if port and not is_port_free(int(port)):
                _alert(Gtk, dlg, "Port In Use", f"Port {port} is already in use.", Gtk.MessageType.ERROR)
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
        tcp_check = Gtk.CheckButton(label="TCP (SSH -L forward)")
        tcp_check.set_active(True)
        udp_check = Gtk.CheckButton(label="UDP (socat relay)")
        udp_check.set_active(False)

        grid, _ = _labeled_grid(Gtk, [
            ("conn", "Connection *:", conn_combo),
            ("tag", "Tag (optional):", tag_entry),
            ("src", "Forward Local Port *:", src_port_entry),
            ("dst", "To Remote Port *:", dst_port_entry),
            ("src_addr", "Local Bind (optional):", src_addr_combo),
            ("dst_addr", "Remote Bind (optional):", dst_addr_combo),
            ("tcp", "Protocol:", tcp_check),
            ("udp", "", udp_check),
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
            tcp = tcp_check.get_active()
            udp = udp_check.get_active()

            if not conn_tag:
                _alert(Gtk, dlg, "No Connection", "Add a connection first.", Gtk.MessageType.ERROR)
                continue
            if not tcp and not udp:
                _alert(Gtk, dlg, "Protocol Required", "Select at least one protocol (TCP or UDP).",
                       Gtk.MessageType.ERROR)
                continue
            if not _is_valid_port(src):
                _alert(Gtk, dlg, "Invalid Port", "Forward Local Port must be 1–65535.", Gtk.MessageType.ERROR)
                continue
            if not is_port_free(int(src)):
                _alert(Gtk, dlg, "Port In Use", f"Local port {src} is already in use.", Gtk.MessageType.ERROR)
                continue
            if not _is_valid_port(dst):
                _alert(Gtk, dlg, "Invalid Port", "To Remote Port must be 1–65535.", Gtk.MessageType.ERROR)
                continue

            fw = PortForward(src_addr=src_addr, src_port=int(src), dst_addr=dst_addr, dst_port=int(dst),
                             tag=tag or None, tcp=tcp, udp=udp)
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
        tcp_check = Gtk.CheckButton(label="TCP (SSH -R forward)")
        tcp_check.set_active(True)
        udp_check = Gtk.CheckButton(label="UDP (socat relay)")
        udp_check.set_active(False)

        grid, _ = _labeled_grid(Gtk, [
            ("conn", "Connection *:", conn_combo),
            ("tag", "Tag (optional):", tag_entry),
            ("rport", "Forward Remote Port *:", remote_port_entry),
            ("lport", "To Local Port *:", local_port_entry),
            ("src_addr", "Remote Bind (optional):", src_addr_combo),
            ("dst_addr", "Local Bind (optional):", dst_addr_combo),
            ("tcp", "Protocol:", tcp_check),
            ("udp", "", udp_check),
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
            tcp = tcp_check.get_active()
            udp = udp_check.get_active()

            if not conn_tag:
                _alert(Gtk, dlg, "No Connection", "Add a connection first.", Gtk.MessageType.ERROR)
                continue
            if not tcp and not udp:
                _alert(Gtk, dlg, "Protocol Required", "Select at least one protocol (TCP or UDP).",
                       Gtk.MessageType.ERROR)
                continue
            if not _is_valid_port(rport):
                _alert(Gtk, dlg, "Invalid Port", "Forward Remote Port must be 1–65535.", Gtk.MessageType.ERROR)
                continue
            if not _is_valid_port(lport):
                _alert(Gtk, dlg, "Invalid Port", "To Local Port must be 1–65535.", Gtk.MessageType.ERROR)
                continue
            fw = PortForward(src_addr=src_addr, src_port=int(rport), dst_addr=dst_addr, dst_port=int(lport),
                             tag=tag or None, tcp=tcp, udp=udp)
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
                items.append(f"[{c.tag}] {fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
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
                items.append(f"[{c.tag}] {fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
        selected = self._pick_from_list("Remove Remote Forward", "Remote Forward:", items)
        if selected:
            m = re.search(r":(\d+)→", selected)
            if m:
                self.do_remove_remote_forward(int(m.group(1)))
        return False

    def _on_toggle_connection(self, _) -> None:
        self._GLib.idle_add(self._show_toggle_connection_dialog)

    def _show_toggle_connection_dialog(self) -> bool:
        cfg = self.manager.list_config()
        items = [f"[{'✓' if c.enabled else '✗'}] {c.tag}" for c in cfg.connections]
        selected = self._pick_from_list("Toggle Connection Enabled", "Connection:", items, ok_label="Toggle")
        if selected:
            tag = selected.split("] ", 1)[-1]
            self.do_toggle_connection_enabled(tag)
        return False

    def _on_toggle_domain(self, _) -> None:
        self._GLib.idle_add(self._show_toggle_domain_dialog)

    def _show_toggle_domain_dialog(self) -> bool:
        cfg = self.manager.list_config()
        items = []
        for c in cfg.connections:
            for h in c.pac_hosts:
                enabled = h not in c.pac_hosts_disabled
                items.append(f"[{'✓' if enabled else '✗'}] {h}")
        selected = self._pick_from_list("Toggle Domain Enabled", "Domain:", items, ok_label="Toggle")
        if selected:
            host = selected.split("] ", 1)[-1]
            self.do_toggle_pac_host_enabled(host)
        return False

    def _on_toggle_forward(self, _) -> None:
        self._GLib.idle_add(self._show_toggle_forward_dialog)

    def _show_toggle_forward_dialog(self) -> bool:
        cfg = self.manager.list_config()
        items = []
        for c in cfg.connections:
            for fw in c.forwards.local:
                state = "✓" if fw.enabled else "✗"
                items.append(f"[{state}] [{c.tag}] local :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
            for fw in c.forwards.remote:
                state = "✓" if fw.enabled else "✗"
                items.append(f"[{state}] [{c.tag}] remote :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
        selected = self._pick_from_list("Toggle Forward Enabled", "Forward:", items, ok_label="Toggle")
        if selected:
            m = re.search(r"\[([^\]]+)\] (local|remote) :(\d+)", selected)
            if m:
                conn_tag, direction, src_port = m.group(1), m.group(2), int(m.group(3))
                self.do_toggle_forward_enabled(conn_tag, src_port, direction)
        return False

    def _on_start_connection(self, _) -> None:
        self._GLib.idle_add(self._show_start_connection_dialog)

    def _show_start_connection_dialog(self) -> bool:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = self._pick_from_list("Start Connection", "Connection:", tags, ok_label="Start")
        if selected:
            self.do_start_connection(selected)
        return False

    def _on_stop_connection(self, _) -> None:
        self._GLib.idle_add(self._show_stop_connection_dialog)

    def _show_stop_connection_dialog(self) -> bool:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = self._pick_from_list("Stop Connection", "Connection:", tags, ok_label="Stop")
        if selected:
            self.do_stop_connection(selected)
        return False

    def _on_restart_connection(self, _) -> None:
        self._GLib.idle_add(self._show_restart_connection_dialog)

    def _show_restart_connection_dialog(self) -> bool:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = self._pick_from_list("Restart Connection", "Connection:", tags, ok_label="Restart")
        if selected:
            self.do_restart_connection(selected)
        return False

    def _on_test_connection(self, _) -> None:
        self._GLib.idle_add(self._show_test_connection_dialog)

    def _show_test_connection_dialog(self) -> bool:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = self._pick_from_list("Test Connection", "Connection:", tags, ok_label="Test")
        if selected:
            self.do_test_connection(selected)
        return False

    def _on_test_domain(self, _) -> None:
        self._GLib.idle_add(self._show_test_domain_dialog)

    def _show_test_domain_dialog(self) -> bool:
        cfg = self.manager.list_config()
        items = []
        for c in cfg.connections:
            for h in c.pac_hosts:
                items.append(f"[{c.tag}] {h}")
        selected = self._pick_from_list("Test Domain", "Domain (via connection):", items, ok_label="Test")
        if selected:
            m = re.match(r"\[([^\]]+)\] (.+)", selected)
            if m:
                self.do_test_domain(m.group(2), m.group(1))
        return False

    def _on_test_forward(self, _) -> None:
        self._GLib.idle_add(self._show_test_forward_dialog)

    def _show_test_forward_dialog(self) -> bool:
        cfg = self.manager.list_config()
        items = []
        for c in cfg.connections:
            for fw in c.forwards.local:
                items.append(f"[{c.tag}] local :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
            for fw in c.forwards.remote:
                items.append(f"[{c.tag}] remote :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
        selected = self._pick_from_list("Test Forward", "Forward:", items, ok_label="Test")
        if selected:
            m = re.search(r"\[([^\]]+)\] (local|remote) :(\d+)", selected)
            if m:
                self.do_test_forward(m.group(1), int(m.group(3)), m.group(2))
        return False

    def _pick_from_list(self, title: str, label: str, items: list[str], ok_label: str = "Remove") -> str | None:
        """Show a dialog with a dropdown list. Returns selected item or None."""
        Gtk = self._Gtk
        if not items:
            _alert(Gtk, self._root, "Nothing to Select", "The list is empty.")
            return None

        dlg = Gtk.Dialog(title=title, transient_for=self._root, modal=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, f"_{ok_label}", Gtk.ResponseType.OK)
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

    def _on_share_file(self) -> None:
        self._GLib.idle_add(self._show_share_file_dialog)

    def _show_share_file_dialog(self) -> bool:
        Gtk = self._Gtk
        tags = [c.tag for c in self.manager.list_config().connections]
        if not tags:
            _alert(Gtk, self._root, "No Connections", "Add a connection first.")
            return False

        dlg = Gtk.Dialog(title="Share File", transient_for=self._root, modal=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Share", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(440, -1)

        conn_combo = Gtk.ComboBoxText()
        for t in tags:
            conn_combo.append_text(t)
        conn_combo.set_active(0)

        fc = Gtk.FileChooserButton(title="Select file", action=Gtk.FileChooserAction.OPEN)
        pw_entry = Gtk.Entry(placeholder_text="auto-generate if blank", activates_default=True)
        port_entry = Gtk.Entry(placeholder_text="0 (auto)", activates_default=True)
        port_entry.set_text("0")

        grid, _ = _labeled_grid(Gtk, [
            ("conn", "Connection *:", conn_combo),
            ("file", "File *:", fc),
            ("pw", "Password:", pw_entry),
            ("port", "Port (0=auto):", port_entry),
        ])
        dlg.get_content_area().add(grid)
        _polish_dialog(Gtk, dlg)
        dlg.show_all()

        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                break
            conn_tag = conn_combo.get_active_text() or ""
            file_path = fc.get_filename() or ""
            pw = pw_entry.get_text().strip() or None
            port_text = port_entry.get_text().strip() or "0"
            if not conn_tag:
                _alert(Gtk, dlg, "Missing Field", "Select a connection.", Gtk.MessageType.ERROR)
                continue
            if not file_path:
                _alert(Gtk, dlg, "Missing Field", "Select a file to share.", Gtk.MessageType.ERROR)
                continue
            try:
                port_int = int(port_text)
            except ValueError:
                _alert(Gtk, dlg, "Invalid Port", "Port must be a number.", Gtk.MessageType.ERROR)
                continue
            dlg.destroy()
            self.do_share(conn_tag, file_path, password=pw, port=port_int)
            return False

        dlg.destroy()
        return False

    def _on_fetch_file(self) -> None:
        self._GLib.idle_add(self._show_fetch_file_dialog)

    def _show_fetch_file_dialog(self) -> bool:
        Gtk = self._Gtk
        tags = [c.tag for c in self.manager.list_config().connections]
        if not tags:
            _alert(Gtk, self._root, "No Connections", "Add a connection first.")
            return False

        dlg = Gtk.Dialog(title="Fetch File", transient_for=self._root, modal=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Fetch", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(440, -1)

        conn_combo = Gtk.ComboBoxText()
        for t in tags:
            conn_combo.append_text(t)
        conn_combo.set_active(0)

        port_entry = Gtk.Entry(placeholder_text="e.g. 52100", activates_default=True)
        pw_entry = Gtk.Entry(placeholder_text="", activates_default=True)
        outfile_entry = Gtk.Entry(placeholder_text="blank = ~/Downloads/<filename>", activates_default=True)

        grid, _ = _labeled_grid(Gtk, [
            ("conn", "Connection *:", conn_combo),
            ("port", "Port *:", port_entry),
            ("pw", "Password *:", pw_entry),
            ("out", "Save to (optional):", outfile_entry),
        ])
        dlg.get_content_area().add(grid)
        _polish_dialog(Gtk, dlg)
        dlg.show_all()

        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                break
            conn_tag = conn_combo.get_active_text() or ""
            port_text = port_entry.get_text().strip()
            pw = pw_entry.get_text().strip()
            outfile = outfile_entry.get_text().strip() or None
            if not conn_tag:
                _alert(Gtk, dlg, "Missing Field", "Select a connection.", Gtk.MessageType.ERROR)
                continue
            if not port_text or not pw:
                _alert(Gtk, dlg, "Missing Field", "Port and password are required.", Gtk.MessageType.ERROR)
                continue
            try:
                port_int = int(port_text)
            except ValueError:
                _alert(Gtk, dlg, "Invalid Port", "Port must be a number.", Gtk.MessageType.ERROR)
                continue
            dlg.destroy()
            self.do_fetch(conn_tag, port_int, pw, outfile=outfile)
            return False

        dlg.destroy()
        return False

    def _make_share_info_handler(self, info):
        def handler(_item):
            self._GLib.idle_add(lambda: self._show_share_info_dialog(info))

        return handler

    def _show_share_info_dialog(self, info) -> bool:
        import pathlib
        Gtk = self._Gtk
        name = pathlib.Path(info.file_path).name
        state = "running" if info.running else "stopped"
        dlg = Gtk.Dialog(title=f"Share: {name}", transient_for=self._root, modal=True)
        # Buttons: Stop/Start (context-sensitive), Delete, Close
        _RESP_TOGGLE = 10  # Stop if running, Start if stopped
        _RESP_DELETE = 11
        if info.running:
            dlg.add_button("_Stop", _RESP_TOGGLE)
        else:
            dlg.add_button("_Start", _RESP_TOGGLE)
        dlg.add_button("_Delete", _RESP_DELETE)
        dlg.add_button("_Close", Gtk.ResponseType.CLOSE)
        dlg.set_default_response(Gtk.ResponseType.CLOSE)
        dlg.set_default_size(380, -1)

        box = dlg.get_content_area()
        box.set_spacing(4)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(8)

        for lbl, val in [
            ("File", info.file_path),
            ("Port", str(info.port)),
            ("Password", info.password),
            ("Connection", info.conn_tag or "—"),
            ("State", state),
        ]:
            row_box = Gtk.Box(spacing=8)
            row_box.pack_start(Gtk.Label(label=f"{lbl}:", xalign=1.0, width_chars=12), False, False, 0)
            row_box.pack_start(Gtk.Label(label=val, xalign=0.0, selectable=True), True, True, 0)
            box.add(row_box)

        _polish_dialog(Gtk, dlg)
        dlg.show_all()
        resp = dlg.run()
        dlg.destroy()
        if resp == _RESP_TOGGLE:
            if info.running:
                self.do_stop_share(info.port)
            else:
                self.do_share(info.conn_tag or "", info.file_path, info.password, info.port)
            self._GLib.idle_add(self._refresh_share_submenu)
        elif resp == _RESP_DELETE:
            self.do_delete_share(info.port)
            self._GLib.idle_add(self._refresh_share_submenu)
        return False

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
        """About dialog. Original layout (name / version / description / link
        buttons / copyright). Non-modal so the tray menu stays usable, and
        keep-above so the window stays in the foreground."""
        from pathlib import Path

        def _show():
            Gtk = self._Gtk
            dlg = Gtk.Dialog(title="About SusOps", transient_for=self._root, modal=False)
            dlg.add_button("_Close", Gtk.ResponseType.CLOSE)
            dlg.set_default_size(280, -1)
            try:
                dlg.set_keep_above(True)
            except Exception:
                pass
            box = dlg.get_content_area()
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                           margin_start=20, margin_end=20,
                           margin_top=16, margin_bottom=12,
                           halign=Gtk.Align.CENTER)
            box.add(vbox)

            # Static logo at the top, if available.
            icon_path = Path(__file__).parent.parent / "assets" / "icon.png"
            if icon_path.exists():
                try:
                    from gi.repository import GdkPixbuf  # type: ignore[import]
                    pix = GdkPixbuf.Pixbuf.new_from_file_at_size(str(icon_path), 64, 64)
                    img = Gtk.Image.new_from_pixbuf(pix)
                    vbox.pack_start(img, False, False, 4)
                except Exception:
                    pass

            import susops
            name_lbl = Gtk.Label()
            name_lbl.set_markup("<b><big>SusOps</big></b>")
            vbox.pack_start(name_lbl, False, False, 2)
            ver_lbl = Gtk.Label(label=f"Version {susops.__version__}")
            ver_lbl.get_style_context().add_class("dim-label")
            vbox.pack_start(ver_lbl, False, False, 0)
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
            dlg.connect("response", lambda d, _r: d.destroy())
            dlg.show_all()
            return False

        self._GLib.idle_add(_show)

    def _on_quit(self, _widget) -> None:
        self.do_quit()
        self._Gtk.main_quit()

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def _start_sse_listener(self) -> None:
        """Background thread: connect to SSE /events and update UI on events."""
        import threading, time

        def _listen():
            backoff = 1.0
            while True:
                status_url = self.manager.get_status_url()
                if not status_url:
                    time.sleep(2.0)
                    continue
                try:
                    import os
                    import urllib.request
                    import susops as _susops_pkg
                    req = urllib.request.Request(status_url, headers={
                        "X-Susops-Client": "tray-linux",
                        "X-Susops-Client-Version": _susops_pkg.__version__,
                        "X-Susops-Pid": str(os.getpid()),
                        # Tray only reacts to state + share events; skip the
                        # high-frequency `bandwidth` broadcasts.
                        "X-Susops-Events": "state,share",
                    })
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        backoff = 1.0
                        # Refresh state on every (re)connect — without this the
                        # tray keeps its last cached state after a daemon
                        # restart and only updates when the new daemon happens
                        # to emit a state event.
                        self._GLib.idle_add(self._on_sse_state)
                        buf = ""
                        for raw in resp:
                            line = raw.decode("utf-8", errors="replace")
                            buf += line
                            if buf.endswith("\n\n"):
                                if "event: state" in buf:
                                    self._GLib.idle_add(self._on_sse_state)
                                if "event: share" in buf:
                                    self._GLib.idle_add(self._refresh_share_submenu)
                                buf = ""
                except Exception:
                    time.sleep(backoff)
                    # Cap at 5s — short enough that the user never sees more
                    # than 5s of staleness, even when the daemon is bouncing.
                    backoff = min(backoff * 2, 5.0)

        threading.Thread(target=_listen, daemon=True, name="susops-sse-linux").start()

    def _on_sse_state(self) -> bool:
        self.do_poll()
        return False

    def update_title(self, rx_bps: float | None, tx_bps: float | None) -> None:
        def _apply():
            if rx_bps is None or tx_bps is None:
                self._indicator.set_label("", "")
                return False
            up = self._format_rate(tx_bps)
            down = self._format_rate(rx_bps)
            self._indicator.set_label(f"↑ {up}   ↓ {down}", "")
            return False
        self._GLib.idle_add(_apply)

    def _tick_bandwidth(self) -> bool:
        self.refresh_bandwidth_title()
        return True

    def run(self) -> None:
        """Start the GTK main loop."""
        # Initial state pull on startup; SSE listener drives every refresh
        # after that. No periodic polling fallback — SSE reconnects within
        # 5 s on its own.
        self.do_poll()
        self.refresh_bandwidth_title()
        self._start_sse_listener()
        self._GLib.timeout_add(1000, self._tick_bandwidth)
        self._Gtk.main()


def main() -> None:
    app = SusOpsLinuxTray()
    app.run()
