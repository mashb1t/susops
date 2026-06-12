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

    def is_open(self) -> bool:
        return self.window is not None and bool(self.window.isVisible())

    def open(self, tab: str | None = None) -> None:
        if self.window is None:
            self._build()
        self.refresh()
        if tab:
            self._select_tab_by_tag(tab)
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

    def close(self) -> None:
        if self.window is not None:
            self.window.orderOut_(None)
        self._on_closed()

    def refresh(self) -> None:
        if self.window is None:
            return
        mgr = self.tray.manager
        self._cfg = mgr.list_config()
        try:
            self._statuses = list(mgr.status().connection_statuses)
        except Exception:
            self._statuses = []
        try:
            self._shares = list(mgr.list_shares())
        except Exception:
            self._shares = []
        self._reload_tabs()
        self._reload_sidebar(preserve=True)
        self._render_current_detail()

    def dump(self) -> dict:
        return {
            "open": self.is_open(),
            "tabs": [t.title for t in self.tabs],
            "current_tag": self.current_tag,
            "sidebar": [
                {"kind": r.kind, "label": r.label} for r in self.sidebar_rows
            ],
            "selected": self._selected_identity(),
            "detail_title": self._current_detail_title,
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

        # Pull-down items + handler wired in Task 8 (per-group add actions).
        add_btn = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(12, 12, SIDEBAR_W, ADD_BTN_H), True)
        add_btn.addItemWithTitle_("Add…")
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
        if self.current_tag is not None:
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
            self.current_tag = spec.tag
            self._reload_sidebar(preserve=False)
            self._render_current_detail()
        elif spec.kind == "add":
            self._restore_segment_selection()
            self.tray.run_add_connection_from_window()
        elif spec.kind == "gear":
            self._restore_segment_selection()
            self._render_placeholder("App settings move here in Task 9.")

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
        self._render_current_detail()

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
            return build_domain_detail(conn, host) if host in conn.pac_hosts else None
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
