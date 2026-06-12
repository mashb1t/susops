"""Unified config window - raw AppKit (rumps has no window classes).

Layout per docs/superpowers/specs/2026-06-12-mac-tray-config-window-design.md:
tab strip (one segment per connection + "+" + gear), grouped sidebar
(DOMAINS/FORWARDS/SHARES/CONNECTION), detail panel for the selection.

Lifecycle copies _open_live_text_window in mac.py: non-modal NSWindow,
held-open _RegularPolicyScope, close via delegate, module-level cached
NSObject subclasses (PyObjC re-registration bug - see mac.py).
"""
from __future__ import annotations

from susops.tray.config_window_model import (
    DetailSpec,
    SidebarRow,
    TabSpec,
    build_connection_detail,
    build_domain_detail,
    build_forward_detail,
    build_share_detail,
    build_sidebar_rows,
    build_tab_specs,
)

_sidebar_ds_cls = None
_window_delegate_cls = None
_action_handler_cls = None


def _get_sidebar_ds_cls():
    global _sidebar_ds_cls
    if _sidebar_ds_cls is not None:
        return _sidebar_ds_cls
    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _SusOpsSidebarDS(NSObject):
        """Data source + delegate for the sidebar NSTableView."""

        def initWithOwner_(self, owner):
            self = objc.super(_SusOpsSidebarDS, self).init()
            if self is None:
                return None
            self._owner = owner
            return self

        def numberOfRowsInTableView_(self, _tv):
            return len(self._owner.sidebar_rows)

        def tableView_objectValueForTableColumn_row_(self, _tv, _col, row):
            return self._owner.sidebar_rows[row].label

        def tableView_shouldSelectRow_(self, _tv, row):
            return self._owner.sidebar_rows[row].kind != "header"

        def tableView_isGroupRow_(self, _tv, row):
            return self._owner.sidebar_rows[row].kind == "header"

        def tableViewSelectionDidChange_(self, _note):
            self._owner._on_sidebar_selection()

    _sidebar_ds_cls = _SusOpsSidebarDS
    return _SusOpsSidebarDS


def _get_window_delegate_cls():
    global _window_delegate_cls
    if _window_delegate_cls is not None:
        return _window_delegate_cls
    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _SusOpsConfigWindowDelegate(NSObject):
        def initWithCallback_(self, cb):
            self = objc.super(_SusOpsConfigWindowDelegate, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        def windowShouldClose_(self, _sender):
            try:
                self._cb()
            except Exception:
                pass
            return True

    _window_delegate_cls = _SusOpsConfigWindowDelegate
    return _SusOpsConfigWindowDelegate


def _get_action_handler_cls():
    global _action_handler_cls
    if _action_handler_cls is not None:
        return _action_handler_cls
    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _SusOpsActionHandler(NSObject):
        """Generic target for buttons/controls; calls back with the sender."""

        def initWithCallback_(self, cb):
            self = objc.super(_SusOpsActionHandler, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        def fire_(self, sender):
            try:
                self._cb(sender)
            except Exception:
                pass

    _action_handler_cls = _SusOpsActionHandler
    return _SusOpsActionHandler


SIDEBAR_W = 220
TAB_H = 28
ADD_BTN_H = 26
WIN_W = 900
WIN_H = 560


class ConfigWindow:
    """Controller for the unified config window. All methods MUST be called
    on the main thread (callers marshal via mac._on_main)."""

    def __init__(self, tray) -> None:
        self.tray = tray
        self.window = None
        self.tabs: list[TabSpec] = []
        self.sidebar_rows: list[SidebarRow] = []
        self.current_tag: str | None = None
        self._policy_scope = None
        self._handlers: list = []
        self._permanent_handler_count = 0
        self._cfg = None
        self._statuses: list = []
        self._shares: list = []
        self._current_detail_title = None
        self._suppress_selection_cb = False  # True while _reload_sidebar is rebuilding rows
        self._gear_mode = False  # True while the gear (app-settings) pane is shown
        self._settings_ctx = None  # ctx from tray._settings_fields while gear pane open
        self._settings_widgets = None  # {key: NSControl} for the gear pane
        self._settings_fields_spec = None  # field spec list for the gear pane
        self._pending_tab = None  # tab requested via open() before tabs loaded

    def is_open(self) -> bool:
        return self.window is not None and bool(self.window.isVisible())

    def open(self, tab: str | None = None) -> None:
        if self.window is None:
            self._build()
        if tab:
            # If tabs aren't built yet (first open before the async data load),
            # defer the selection until _apply_data populates them.
            if self.tabs:
                self._select_tab_by_tag(tab)
            else:
                self._pending_tab = tab
        from susops.tray.mac import _RegularPolicyScope
        if self._policy_scope is None:
            self._policy_scope = _RegularPolicyScope()
            self._policy_scope.__enter__()
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        try:
            self.window.orderFrontRegardless()
        except Exception:
            pass
        # Trigger async refresh so the main run loop is never blocked
        # while opening the window.
        self.tray._refresh_config_window()

    def close(self) -> None:
        if self.window is not None:
            self.window.orderOut_(None)
        self._on_closed()

    def refresh(self) -> None:
        """Fetch config + status from daemon, then repaint. Must be called on
        the main thread (blocks briefly for RPC)."""
        if self.window is None:
            return
        mgr = self.tray.manager
        cfg = mgr.list_config()
        try:
            statuses = list(mgr.status().connection_statuses)
        except Exception:
            statuses = []
        try:
            shares = list(mgr.list_shares())
        except Exception:
            shares = []
        self._apply_data(cfg, statuses, shares)

    def _apply_data(self, cfg, statuses: list, shares: list) -> None:
        """Apply freshly fetched data and repaint. Must be called on the main thread."""
        if self.window is None:
            return
        self._cfg = cfg
        self._statuses = statuses
        self._shares = shares
        self._reload_tabs()
        # Apply a tab requested via open() before tabs were built.
        if self._pending_tab is not None and self.tabs:
            pending = self._pending_tab
            self._pending_tab = None
            self._select_tab_by_tag(pending)
            return
        # While the gear pane is shown, a periodic refresh must not clobber the
        # in-progress settings form (re-rendering would discard the user's
        # edits) nor un-hide the sidebar. _reload_tabs already refreshed the tab
        # labels; leave the gear pane and hidden sidebar untouched.
        if self._gear_mode:
            self._set_sidebar_visible(False)
            return
        self._reload_sidebar(preserve=True)
        self._schedule_render()

    def dump(self) -> dict:
        sidebar_hidden = False
        try:
            sidebar_hidden = bool(self._sidebar_tv.enclosingScrollView().isHidden())
        except Exception:
            pass
        return {
            "open": self.is_open(),
            "mode": "gear" if self._gear_mode else (self.current_tag or None),
            "gear": self._gear_mode,
            "sidebar_hidden": sidebar_hidden,
            "tabs": [t.title for t in self.tabs],
            "current_tag": self.current_tag,
            "sidebar": [
                {"kind": r.kind, "label": r.label} for r in self.sidebar_rows
            ],
            "selected": self._selected_identity(),
            "detail_title": self._current_detail_title,
            "add_menu": [str(self._add_btn.itemTitleAtIndex_(i))
                         for i in range(self._add_btn.numberOfItems())],
        }

    def select(self, tag: str, group: str | None = None, index: int = 0) -> dict:
        self._select_tab_by_tag(tag)
        if group in (None, "", "connection"):
            target_kinds = {"connection"}
        else:
            target_kinds = {{"domains": "domain", "forwards": "forward",
                             "shares": "share"}.get(group, group)}
        matches = [i for i, r in enumerate(self.sidebar_rows)
                   if r.kind in target_kinds]
        if not matches or index >= len(matches):
            return {"error": f"no row for group={group} index={index}"}
        self._select_sidebar_row(matches[index])
        return {"ok": True, "selected": self._selected_identity()}

    def _build(self) -> None:
        from AppKit import (  # type: ignore[import]
            NSBackingStoreBuffered,
            NSFloatingWindowLevel,
            NSSegmentSwitchTrackingSelectOne,
            NSWindow,
            NSWindowStyleMaskClosable,
            NSWindowStyleMaskResizable,
            NSWindowStyleMaskTitled,
        )
        from Cocoa import (  # type: ignore[import]
            NSMakeRect,
            NSScrollView,
            NSSegmentedControl,
            NSPopUpButton,
            NSTableColumn,
            NSTableView,
            NSView,
        )

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIN_W, WIN_H), style, NSBackingStoreBuffered, False,
        )
        win.setTitle_("SusOps Settings")
        win.setReleasedWhenClosed_(False)
        win.setHidesOnDeactivate_(False)
        win.setLevel_(NSFloatingWindowLevel)
        # The window respects the system appearance (light/dark). Detail-panel
        # text fields set an explicit NSColor.labelColor() so they resolve to a
        # legible foreground in either mode (see _render_detail / _placeholder).
        content = win.contentView()
        # The detail panel is a plain (transparent) NSView; without an opaque
        # backing it captures as white in screenshots and clashes with the
        # dynamic labelColor() text in Dark Mode (light text on white). Make the
        # content view layer-backed with the dynamic windowBackgroundColor so the
        # backing tracks the effective appearance and text stays legible.
        try:
            from Cocoa import NSColor  # type: ignore[import]
            content.setWantsLayer_(True)
            content.layer().setBackgroundColor_(
                NSColor.windowBackgroundColor().CGColor())
        except Exception:
            pass

        seg = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(12, WIN_H - TAB_H - 10, WIN_W - 24, TAB_H))
        try:
            from AppKit import NSSegmentStyleRounded  # type: ignore[import]
            seg.setSegmentStyle_(NSSegmentStyleRounded)
        except Exception:
            pass
        seg.setTrackingMode_(NSSegmentSwitchTrackingSelectOne)
        seg.setAutoresizingMask_(2 | 8)  # WidthSizable | MinYMargin
        seg_handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda sender: self._on_segment(int(sender.selectedSegment())))
        self._handlers.append(seg_handler)
        seg.setTarget_(seg_handler)
        seg.setAction_("fire:")
        content.addSubview_(seg)
        self._seg = seg

        body_h = WIN_H - TAB_H - 28

        tv = NSTableView.alloc().initWithFrame_(
            NSMakeRect(0, 0, SIDEBAR_W, body_h - ADD_BTN_H - 16))
        col = NSTableColumn.alloc().initWithIdentifier_("item")
        col.setWidth_(SIDEBAR_W - 20)
        tv.addTableColumn_(col)
        tv.setHeaderView_(None)
        ds = _get_sidebar_ds_cls().alloc().initWithOwner_(self)
        self._handlers.append(ds)
        tv.setDataSource_(ds)
        tv.setDelegate_(ds)
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(12, ADD_BTN_H + 20, SIDEBAR_W, body_h - ADD_BTN_H - 20))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDocumentView_(tv)
        scroll.setAutoresizingMask_(16)  # HeightSizable
        content.addSubview_(scroll)
        self._sidebar_tv = tv

        # Pull-down: item 0 is the visible title, items 1+ are commands.
        add_btn = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(12, 12, SIDEBAR_W, ADD_BTN_H), True)
        add_btn.removeAllItems()
        add_btn.addItemsWithTitles_([
            "Add…",
            "Add Domain / IP / CIDR…",
            "Add Local Forward…",
            "Add Remote Forward…",
            "Share File…",
            "Fetch File…",
        ])
        add_handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda sender: self._on_add_command(str(sender.titleOfSelectedItem() or "")))
        self._handlers.append(add_handler)
        add_btn.setTarget_(add_handler)
        add_btn.setAction_("fire:")
        content.addSubview_(add_btn)
        self._add_btn = add_btn

        detail = NSView.alloc().initWithFrame_(
            NSMakeRect(SIDEBAR_W + 24, 12, WIN_W - SIDEBAR_W - 36, body_h))
        detail.setAutoresizingMask_(2 | 16)  # Width+HeightSizable
        content.addSubview_(detail)
        self._detail = detail

        delegate = _get_window_delegate_cls().alloc().initWithCallback_(self._on_closed)
        self._handlers.append(delegate)
        win.setDelegate_(delegate)
        self.window = win
        # Handlers added so far (seg, sidebar DS, window delegate) live for the
        # window's lifetime. Per-render handlers are appended after this and
        # trimmed back to this count in _clear_detail (see I1 review note).
        self._permanent_handler_count = len(self._handlers)

    def _on_closed(self) -> None:
        if self._gear_mode:
            # Closing the window from the gear pane: discard any unsaved preview.
            self._revert_settings_preview()
            self._gear_mode = False
        if self._policy_scope is not None:
            try:
                self._policy_scope.__exit__(None, None, None)
            except Exception:
                pass
            self._policy_scope = None

    def _reload_tabs(self) -> None:
        self.tabs = build_tab_specs(self._cfg, self._statuses)
        conn_tags = [t.tag for t in self.tabs if t.kind == "connection"]
        if self.current_tag not in conn_tags:
            self.current_tag = conn_tags[0] if conn_tags else None
        seg = self._seg
        seg.setSegmentCount_(len(self.tabs))
        for i, t in enumerate(self.tabs):
            seg.setLabel_forSegment_(t.title, i)
            seg.setWidth_forSegment_(0, i)  # autosize
        if self._gear_mode:
            # Keep the gear segment (last) selected across periodic refreshes.
            seg.setSelectedSegment_(len(self.tabs) - 1)
        elif self.current_tag is not None:
            seg.setSelectedSegment_(conn_tags.index(self.current_tag))

    def _select_tab_by_tag(self, tag: str) -> None:
        if tag == "gear":
            self._on_segment(len(self.tabs) - 1)
            return
        for i, t in enumerate(self.tabs):
            if t.kind == "connection" and t.tag == tag:
                self._seg.setSelectedSegment_(i)
                self._on_segment(i)
                return

    def _on_segment(self, idx: int) -> None:
        if not (0 <= idx < len(self.tabs)):
            return
        spec = self.tabs[idx]
        if spec.kind == "connection":
            if self._gear_mode:
                # Leaving the gear pane without saving: discard any live logo /
                # bandwidth preview so an unsaved style does not stick.
                self._revert_settings_preview()
            self._gear_mode = False
            self._set_sidebar_visible(True)
            self.current_tag = spec.tag
            self._reload_sidebar(preserve=False)
            self._schedule_render()
        elif spec.kind == "add":
            self._restore_segment_selection()
            self.tray.run_add_connection_from_window()
        elif spec.kind == "gear":
            # Gear is the last segment and has no underlying connection tab to
            # restore to. App settings are app-level, so hide the per-connection
            # sidebar + Add… control while the gear pane is shown.
            self._seg.setSelectedSegment_(idx)
            self._gear_mode = True
            self._set_sidebar_visible(False)
            from Foundation import NSTimer  # type: ignore[import]
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                0.0, False, lambda _t: self._render_settings_pane()
            )

    def _set_sidebar_visible(self, visible: bool) -> None:
        try:
            self._sidebar_tv.enclosingScrollView().setHidden_(not visible)
        except Exception:
            pass
        try:
            self._add_btn.setHidden_(not visible)
        except Exception:
            pass

    def _on_add_command(self, title: str) -> None:
        tag = self.current_tag
        if tag is None:
            return
        if title.startswith("Add Domain"):
            self.tray._show_add_host_dialog(conn_tag=tag)
        elif title.startswith("Add Local"):
            self.tray._show_add_forward_dialog(remote=False, conn_tag=tag)
        elif title.startswith("Add Remote"):
            self.tray._show_add_forward_dialog(remote=True, conn_tag=tag)
        elif title.startswith("Share File"):
            self.tray._show_share_file_dialog(conn_tag=tag)
        elif title.startswith("Fetch File"):
            self.tray._show_fetch_file_dialog(conn_tag=tag)
        self.tray._refresh_config_window()

    def _restore_segment_selection(self) -> None:
        conn_tags = [t.tag for t in self.tabs if t.kind == "connection"]
        if self.current_tag in conn_tags:
            self._seg.setSelectedSegment_(conn_tags.index(self.current_tag))

    def _current_conn(self):
        if self._cfg is None or self.current_tag is None:
            return None
        return next((c for c in self._cfg.connections
                     if c.tag == self.current_tag), None)

    def _reload_sidebar(self, *, preserve: bool) -> None:
        prev = self._selected_identity() if preserve else None
        conn = self._current_conn()
        if conn is None:
            self.sidebar_rows = []
        else:
            conn_shares = [s for s in self._shares
                           if getattr(s, "conn_tag", None) == conn.tag]
            self.sidebar_rows = build_sidebar_rows(conn, conn_shares)
        # Suppress selection callbacks during rebuild so _on_sidebar_selection
        # doesn't call _render_current_detail in the middle of a table reload.
        self._suppress_selection_cb = True
        try:
            self._sidebar_tv.reloadData()
            target = None
            if prev is not None:
                target = next((i for i, r in enumerate(self.sidebar_rows)
                               if r.identity == prev), None)
            if target is None:
                target = next((i for i, r in enumerate(self.sidebar_rows)
                               if r.kind == "connection"), None)
            if target is not None:
                self._select_sidebar_row(target)
        finally:
            self._suppress_selection_cb = False

    def _select_sidebar_row(self, row: int) -> None:
        from Foundation import NSIndexSet  # type: ignore[import]
        self._sidebar_tv.selectRowIndexes_byExtendingSelection_(
            NSIndexSet.indexSetWithIndex_(row), False)

    def _selected_identity(self) -> tuple | None:
        row = int(self._sidebar_tv.selectedRow()) if self.window else -1
        if 0 <= row < len(self.sidebar_rows):
            r = self.sidebar_rows[row]
            if r.kind != "header":
                return r.identity
        return None

    def _on_sidebar_selection(self) -> None:
        if not self._suppress_selection_cb:
            self._schedule_render()

    def _schedule_render(self) -> None:
        """Defer _render_current_detail to the next run-loop iteration.

        Calling _render_current_detail directly from within a
        performSelectorOnMainThread dispatch (e.g. _run_on_main, or the
        rumps timer callback) kills the rumps NSTimer. Scheduling through a
        0-second one-shot NSTimer puts the work in a fresh run-loop pass and
        avoids the interference.
        """
        from Foundation import NSTimer  # type: ignore[import]
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.0, False, lambda _t: self._render_current_detail()
        )

    def _render_current_detail(self) -> None:
        identity = self._selected_identity()
        conn = self._current_conn()
        if conn is None or identity is None:
            self._render_placeholder("No selection.")
            return
        spec = self._detail_spec_for(conn, identity)
        if spec is None:
            self._render_placeholder("Item no longer exists.")
            return
        self._render_detail(spec, identity)

    def _detail_spec_for(self, conn, identity: tuple) -> DetailSpec | None:
        kind = identity[0]
        if kind == "connection":
            st = next((s for s in self._statuses if s.tag == conn.tag), None)
            return build_connection_detail(conn, st)
        if kind == "domain":
            host = identity[1]
            disabled = set(getattr(conn, "pac_hosts_disabled", []) or [])
            all_hosts = set(conn.pac_hosts) | disabled
            return build_domain_detail(conn, host) if host in all_hosts else None
        if kind == "forward":
            _, direction, src_port = identity
            fws = (conn.forwards.local if direction == "local"
                   else conn.forwards.remote)
            fw = next((f for f in fws if f.src_port == src_port), None)
            return build_forward_detail(conn, fw, direction) if fw else None
        if kind == "share":
            info = next((s for s in self._shares if s.port == identity[1]), None)
            return build_share_detail(info) if info else None
        return None

    def _clear_detail(self) -> None:
        for v in list(self._detail.subviews()):
            v.removeFromSuperview()
        # Drop per-render button/toggle handlers so _handlers doesn't grow
        # unbounded across refresh() calls (one per poll).
        del self._handlers[self._permanent_handler_count:]

    def _render_placeholder(self, text: str) -> None:
        from Cocoa import NSColor, NSMakeRect, NSTextField  # type: ignore[import]
        self._clear_detail()
        self._current_detail_title = None
        h = self._detail.frame().size.height
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(8, h - 40, 400, 24))
        lbl.setStringValue_(text)
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setTextColor_(NSColor.labelColor())
        self._detail.addSubview_(lbl)

    def _render_detail(self, spec: DetailSpec, identity: tuple) -> None:
        from AppKit import (  # type: ignore[import]
            NSFont,
            NSOffState,
            NSOnState,
            NSSwitchButton,
        )
        from Cocoa import (  # type: ignore[import]
            NSButton,
            NSColor,
            NSMakeRect,
            NSTextField,
        )

        self._clear_detail()
        self._current_detail_title = spec.title
        w = self._detail.frame().size.width
        h = self._detail.frame().size.height
        y = h - 36

        title = NSTextField.alloc().initWithFrame_(NSMakeRect(8, y, w - 16, 24))
        title.setStringValue_(f"Config for {spec.title}")
        title.setFont_(NSFont.boldSystemFontOfSize_(16))
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setTextColor_(NSColor.labelColor())
        self._detail.addSubview_(title)
        y -= 40

        def _row(label: str, value: str) -> None:
            nonlocal y
            lab = NSTextField.alloc().initWithFrame_(NSMakeRect(8, y, 130, 20))
            lab.setStringValue_(label)
            lab.setAlignment_(2)
            lab.setBezeled_(False)
            lab.setDrawsBackground_(False)
            lab.setEditable_(False)
            lab.setTextColor_(NSColor.labelColor())
            val = NSTextField.alloc().initWithFrame_(NSMakeRect(148, y, w - 160, 20))
            val.setStringValue_(value)
            val.setBezeled_(False)
            val.setDrawsBackground_(False)
            val.setEditable_(False)
            val.setSelectable_(True)
            val.setTextColor_(NSColor.labelColor())
            self._detail.addSubview_(lab)
            self._detail.addSubview_(val)
            y -= 26

        for label, value in spec.rows:
            _row(label, str(value))

        if spec.toggle is not None:
            t_label, t_value, t_action = spec.toggle
            lab = NSTextField.alloc().initWithFrame_(NSMakeRect(8, y, 130, 20))
            lab.setStringValue_(t_label)
            lab.setAlignment_(2)
            lab.setBezeled_(False)
            lab.setDrawsBackground_(False)
            lab.setEditable_(False)
            lab.setTextColor_(NSColor.labelColor())
            self._detail.addSubview_(lab)
            sw = NSButton.alloc().initWithFrame_(NSMakeRect(148, y, 60, 20))
            sw.setButtonType_(NSSwitchButton)
            sw.setTitle_("")
            sw.setState_(NSOnState if t_value else NSOffState)
            handler = _get_action_handler_cls().alloc().initWithCallback_(
                lambda _s, aid=t_action, ident=identity: self.tray.dispatch_window_action(aid, ident))
            self._handlers.append(handler)
            sw.setTarget_(handler)
            sw.setAction_("fire:")
            self._detail.addSubview_(sw)
            y -= 32

        x = 8
        for action in spec.actions:
            bw = max(80, 24 + 9 * len(action.title))
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y - 6, bw, 28))
            btn.setTitle_(action.title)
            btn.setBezelStyle_(1)
            btn.setEnabled_(action.enabled)
            handler = _get_action_handler_cls().alloc().initWithCallback_(
                lambda _s, aid=action.action_id, ident=identity: self.tray.dispatch_window_action(aid, ident))
            self._handlers.append(handler)
            btn.setTarget_(handler)
            btn.setAction_("fire:")
            self._detail.addSubview_(btn)
            x += bw + 8

    # ------------------------------------------------------------------ #
    # Gear (app settings) pane
    # ------------------------------------------------------------------ #

    def _render_settings_pane(self, defaults: dict | None = None) -> None:
        """Render the app-settings form into the detail panel.

        The field spec + validation/persist live on the tray
        (_settings_fields / _apply_settings) so this pane and the modal
        Settings dialog share one source of truth. This renderer only handles
        the field kinds the settings form uses: switch, segmented, text.
        """
        if self.window is None or not self._gear_mode:
            return
        from AppKit import (  # type: ignore[import]
            NSFont,
            NSImage,
            NSImageScaleProportionallyDown,
            NSOffState,
            NSOnState,
            NSRegularControlSize,
            NSSegmentSwitchTrackingSelectOne,
            NSSwitchButton,
        )
        from Cocoa import (  # type: ignore[import]
            NSButton,
            NSColor,
            NSMakeRect,
            NSSegmentedControl,
            NSTextField,
        )
        import os

        fields, ctx = self.tray._settings_fields(defaults)
        self._settings_ctx = ctx
        self._settings_fields_spec = fields

        self._clear_detail()
        self._current_detail_title = "App Settings"
        self._gear_mode = True

        w = self._detail.frame().size.width
        h = self._detail.frame().size.height
        y = h - 36

        title = NSTextField.alloc().initWithFrame_(NSMakeRect(8, y, w - 16, 24))
        title.setStringValue_("App Settings")
        title.setFont_(NSFont.boldSystemFontOfSize_(16))
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setTextColor_(NSColor.labelColor())
        self._detail.addSubview_(title)
        y -= 36

        label_w = 200
        input_x = 8 + label_w + 10
        input_w = min(220, w - input_x - 8)
        row_h = 22
        row_gap = 12

        widgets: dict[str, object] = {}

        def _label(text: str, ly: float) -> None:
            lab = NSTextField.alloc().initWithFrame_(NSMakeRect(8, ly, label_w, row_h))
            lab.setStringValue_(text)
            lab.setAlignment_(2)  # right
            lab.setBezeled_(False)
            lab.setDrawsBackground_(False)
            lab.setEditable_(False)
            lab.setTextColor_(NSColor.labelColor())
            self._detail.addSubview_(lab)

        for f in fields:
            key = f["key"]
            kind = f.get("kind", "text")
            default = f.get("default")
            on_change = f.get("on_change")
            _label(f.get("label", ""), y)

            if kind == "switch":
                sw = NSButton.alloc().initWithFrame_(NSMakeRect(input_x, y, input_w, row_h))
                sw.setButtonType_(NSSwitchButton)
                sw.setTitle_("")
                sw.setState_(NSOnState if default else NSOffState)
                if on_change is not None:
                    handler = _get_action_handler_cls().alloc().initWithCallback_(
                        lambda sender, cb=on_change: cb(bool(sender.state())))
                    self._handlers.append(handler)
                    sw.setTarget_(handler)
                    sw.setAction_("fire:")
                self._detail.addSubview_(sw)
                widgets[key] = sw

            elif kind == "segmented":
                options = f.get("options") or []
                seg = NSSegmentedControl.alloc().initWithFrame_(
                    NSMakeRect(input_x, y, input_w, row_h))
                seg.setSegmentCount_(len(options))
                seg.setTrackingMode_(NSSegmentSwitchTrackingSelectOne)
                seg.setControlSize_(NSRegularControlSize)
                for idx, opt in enumerate(options):
                    if isinstance(opt, tuple):
                        label_text, image_path = opt
                    else:
                        label_text, image_path = str(opt), None
                    if image_path and os.path.exists(image_path):
                        try:
                            img = NSImage.alloc().initWithContentsOfFile_(image_path)
                            if img is not None:
                                img.setSize_((20, 20))
                                seg.setImage_forSegment_(img, idx)
                                try:
                                    seg.cell().setImageScaling_forSegment_(
                                        NSImageScaleProportionallyDown, idx)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    try:
                        seg.setLabel_forSegment_(label_text, idx)
                    except Exception:
                        pass
                try:
                    seg.setSelectedSegment_(int(default) if default is not None else 0)
                except Exception:
                    pass
                if on_change is not None:
                    handler = _get_action_handler_cls().alloc().initWithCallback_(
                        lambda sender, cb=on_change: cb(int(sender.selectedSegment())))
                    self._handlers.append(handler)
                    seg.setTarget_(handler)
                    seg.setAction_("fire:")
                self._detail.addSubview_(seg)
                widgets[key] = seg

            else:  # text
                field = NSTextField.alloc().initWithFrame_(
                    NSMakeRect(input_x, y, input_w, row_h))
                field.setStringValue_(str(default) if default is not None else "")
                field.setEditable_(True)
                field.setSelectable_(True)
                field.setBezeled_(True)
                field.setTextColor_(NSColor.labelColor())
                hint = f.get("hint")
                if hint:
                    try:
                        field.cell().setPlaceholderString_(hint)
                    except Exception:
                        pass
                self._detail.addSubview_(field)
                widgets[key] = field
                # Show the hint to the right of the port field too (the modal
                # uses a placeholder; here the field often already has a value
                # so surface the hint as a trailing note).
                note_x = input_x + input_w + 10
                if hint and note_x < w - 8:
                    note = NSTextField.alloc().initWithFrame_(
                        NSMakeRect(note_x, y, w - note_x - 8, row_h))
                    note.setStringValue_(str(hint))
                    note.setBezeled_(False)
                    note.setDrawsBackground_(False)
                    note.setEditable_(False)
                    note.setFont_(NSFont.systemFontOfSize_(10))
                    note.setTextColor_(NSColor.secondaryLabelColor())
                    self._detail.addSubview_(note)

            y -= row_h + row_gap

        self._settings_widgets = widgets

        # Buttons: Save / Revert / Open Config File
        y -= 6
        x = 8
        for btn_title, cb in (
            ("Save", self._on_settings_save),
            ("Revert", self._on_settings_revert),
            ("Open Config File", lambda: self.tray.do_open_config_file()),
        ):
            bw = max(80, 24 + 9 * len(btn_title))
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, bw, 28))
            btn.setTitle_(btn_title)
            btn.setBezelStyle_(1)
            handler = _get_action_handler_cls().alloc().initWithCallback_(
                lambda _s, fn=cb: fn())
            self._handlers.append(handler)
            btn.setTarget_(handler)
            btn.setAction_("fire:")
            self._detail.addSubview_(btn)
            x += bw + 8

    def _read_settings_widgets(self) -> dict:
        """Read the gear-pane widget values into a result dict keyed like the
        field spec (same keys _apply_settings expects)."""
        result: dict = {}
        widgets = self._settings_widgets or {}
        for f in self._settings_fields_spec or []:
            key = f["key"]
            kind = f.get("kind", "text")
            w = widgets.get(key)
            if w is None:
                continue
            if kind == "switch":
                result[key] = bool(w.state())
            elif kind == "segmented":
                result[key] = int(w.selectedSegment())
            else:
                result[key] = str(w.stringValue()).strip()
        return result

    def _on_settings_save(self) -> None:
        if self._settings_ctx is None:
            return
        result = self._read_settings_widgets()
        err = self.tray._apply_settings(result, self._settings_ctx)
        if err is not None:
            from susops.tray.mac import _show_message
            _show_message(err[0], err[1])
            # Keep the pane open with the user's edits.
            self._render_settings_pane(defaults=result)
            return
        # Saved: re-render from the now-persisted config (resets ctx so a
        # follow-up unchanged-port check uses the new saved values).
        self._render_settings_pane()

    def _on_settings_revert(self) -> None:
        # Discard edits + any live logo / bandwidth preview, then re-render
        # from the current saved config.
        self._revert_settings_preview()
        self._render_settings_pane()

    def _revert_settings_preview(self) -> None:
        """Restore the menu-bar icon + bandwidth title to the saved config so a
        previewed-but-unsaved logo style does not stick. Mirrors the modal
        dialog's Cancel path."""
        try:
            self.tray.update_icon(self.tray.state)
        except Exception:
            pass
        try:
            self.tray.refresh_bandwidth_title()
        except Exception:
            pass
