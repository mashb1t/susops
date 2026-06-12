"""3-column config window - raw AppKit (rumps has no window classes).

Layout per docs/superpowers/specs/2026-06-13-mac-tray-3col-redesign-design.md:
column 1 = nav (categories + Settings), column 2 = global list with search +
add buttons, column 3 = detail/editor (placeholder in Task 2, real panes in
Tasks 3-7).

Lifecycle copies _open_live_text_window in mac.py: non-modal NSWindow,
held-open _RegularPolicyScope, close via delegate, module-level cached
NSObject subclasses (PyObjC re-registration bug - see mac.py). Two table data
sources share one cached class via a `role` attr ("nav" / "list").
"""
from __future__ import annotations

from susops.tray.config_window_model import (
    ListRow,
    NavItem,
    build_connection_rows,
    build_domain_rows,
    build_forward_rows,
    build_nav,
    build_share_rows,
    filter_rows,
)

_table_ds_cls = None
_window_delegate_cls = None
_action_handler_cls = None


def _get_table_ds_cls():
    """Cached data-source/delegate class for both view-based NSTableViews.

    One class, two instances distinguished by `role` ("nav" / "list"). The
    owner builds the cell views; the DS just routes callbacks.
    """
    global _table_ds_cls
    if _table_ds_cls is not None:
        return _table_ds_cls
    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _SusOpsTableDS(NSObject):
        def initWithOwner_role_(self, owner, role):
            self = objc.super(_SusOpsTableDS, self).init()
            if self is None:
                return None
            self._owner = owner
            self._role = role
            return self

        def numberOfRowsInTableView_(self, _tv):
            return self._owner._row_count(self._role)

        def tableView_viewForTableColumn_row_(self, _tv, _col, row):
            return self._owner._make_cell(self._role, row)

        def tableView_heightOfRow_(self, _tv, row):
            return self._owner._row_height(self._role, row)

        def tableView_shouldSelectRow_(self, _tv, row):
            return self._owner._row_selectable(self._role, row)

        def tableView_isGroupRow_(self, _tv, row):
            return False

        def tableViewSelectionDidChange_(self, _note):
            self._owner._on_selection(self._role)

    _table_ds_cls = _SusOpsTableDS
    return _SusOpsTableDS


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
        """Generic target for buttons/controls/search; calls back with sender."""

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


# ---- geometry ----
WIN_W = 1080
WIN_H = 640
MIN_W = 980
MIN_H = 560
COL1_W = 180
COL2_W = 270
TOP_INSET = 38          # traffic lights overlay col 1; start content below them
SEARCH_H = 24
ADDBAR_H = 40

# ---- dot colors ----
_DOT_SELECTORS = {
    "green": "systemGreenColor",
    "amber": "systemOrangeColor",
    "gray": "systemGrayColor",
    "red": "systemRedColor",
}

CATEGORIES = ("connections", "domains", "forwards", "shares", "settings")


def _ns_dot_color(word: str):
    from Cocoa import NSColor  # type: ignore[import]
    sel = _DOT_SELECTORS.get(word, "systemGrayColor")
    return getattr(NSColor, sel)()


class ConfigWindow:
    """Controller for the 3-column config window. All methods MUST be called
    on the main thread (callers marshal via mac._on_main / _run_on_main)."""

    def __init__(self, tray) -> None:
        self.tray = tray
        self.window = None
        self.category = "connections"
        self.nav_items: list[NavItem] = []
        self.rows: list[ListRow] = []          # filtered col-2 rows
        self._all_rows: list[ListRow] = []      # unfiltered col-2 rows
        self.selected_identity: tuple | None = None
        self.search_text = ""
        self._policy_scope = None
        self._handlers: list = []
        self._permanent_handler_count = 0
        self._cfg = None
        self._statuses: list = []
        self._shares: list = []
        self._suppress_selection_cb = False
        self._pending_category = None  # category requested via open() pre-load

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def is_open(self) -> bool:
        return self.window is not None and bool(self.window.isVisible())

    def open(self, category: str | None = None) -> None:
        if self.window is None:
            self._build()
        if category in CATEGORIES:
            if self._cfg is not None:
                self._select_nav(category)
            else:
                self._pending_category = category
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
        self.tray._refresh_config_window()

    def close(self) -> None:
        if self.window is not None:
            self.window.orderOut_(None)
        self._on_closed()

    def _on_closed(self) -> None:
        if self._policy_scope is not None:
            try:
                self._policy_scope.__exit__(None, None, None)
            except Exception:
                pass
            self._policy_scope = None

    # ------------------------------------------------------------------ #
    # Build
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        from AppKit import (  # type: ignore[import]
            NSBackingStoreBuffered,
            NSWindow,
            NSWindowStyleMaskClosable,
            NSWindowStyleMaskFullSizeContentView,
            NSWindowStyleMaskResizable,
            NSWindowStyleMaskTitled,
            NSWindowTitleHidden,
        )
        from Cocoa import (  # type: ignore[import]
            NSColor,
            NSMakeRect,
            NSView,
        )

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskFullSizeContentView)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIN_W, WIN_H), style, NSBackingStoreBuffered, False,
        )
        win.setTitle_("SusOps")
        win.setReleasedWhenClosed_(False)
        win.setHidesOnDeactivate_(False)
        win.setTitlebarAppearsTransparent_(True)
        try:
            win.setTitleVisibility_(NSWindowTitleHidden)
        except Exception:
            pass
        try:
            win.setMinSize_(NSMakeRect(0, 0, MIN_W, MIN_H).size)
            win.setContentMinSize_(NSMakeRect(0, 0, MIN_W, MIN_H).size)
        except Exception:
            pass
        # Normal window level (no NSFloatingWindowLevel). System appearance, no pin.

        content = win.contentView()
        try:
            content.setWantsLayer_(True)
            content.layer().setBackgroundColor_(
                NSColor.windowBackgroundColor().CGColor())
        except Exception:
            pass

        ch = WIN_H

        # --- Column 1 (nav band, darkest) ---
        col1 = self._make_band(NSMakeRect(0, 0, COL1_W, ch), band="col1")
        col1.setAutoresizingMask_(32)  # MinXMargin off; height tracks via subviews
        col1.setAutoresizingMask_(16 | 32)  # HeightSizable | MaxXMargin
        content.addSubview_(col1)
        self._col1 = col1

        # --- Column 2 (list band, mid) ---
        col2 = self._make_band(NSMakeRect(COL1_W, 0, COL2_W, ch), band="col2")
        col2.setAutoresizingMask_(16)  # HeightSizable; fixed width, fixed left
        content.addSubview_(col2)
        self._col2 = col2

        # --- Column 3 (detail band, flexible) ---
        col3_x = COL1_W + COL2_W
        col3 = NSView.alloc().initWithFrame_(
            NSMakeRect(col3_x, 0, WIN_W - col3_x, ch))
        try:
            col3.setWantsLayer_(True)
            col3.layer().setBackgroundColor_(
                NSColor.windowBackgroundColor().CGColor())
        except Exception:
            pass
        col3.setAutoresizingMask_(2 | 16)  # Width+HeightSizable
        content.addSubview_(col3)
        self._col3 = col3

        # --- thin separators between columns ---
        self._add_separator(content, COL1_W, ch)
        self._add_separator(content, COL1_W + COL2_W, ch)

        self._build_nav_table(col1, ch)
        self._build_list_column(col2, ch)

        delegate = _get_window_delegate_cls().alloc().initWithCallback_(
            self._on_closed)
        self._handlers.append(delegate)
        win.setDelegate_(delegate)
        self.window = win
        self._permanent_handler_count = len(self._handlers)
        self._render_col3_placeholder("Select an item.")

    def _make_band(self, frame, *, band: str):
        """Layer-backed NSView with a distinct dynamic color so the three
        columns read as layered. col1 darkest < col2 < col3 (windowBackground).

        Blend the window background toward black by a per-band fraction so the
        layering is visible in both light and dark mode (underPageBackground /
        controlBackground resolve too close in dark mode)."""
        from Cocoa import NSColor, NSView  # type: ignore[import]
        view = NSView.alloc().initWithFrame_(frame)
        try:
            view.setWantsLayer_(True)
            base = NSColor.windowBackgroundColor()
            black = NSColor.blackColor()
            frac = 0.22 if band == "col1" else 0.10  # col1 darkest
            color = base.blendedColorWithFraction_ofColor_(frac, black) or base
            view.layer().setBackgroundColor_(color.CGColor())
        except Exception:
            pass
        return view

    def _add_separator(self, content, x: float, ch: float) -> None:
        from Cocoa import NSColor, NSMakeRect, NSView  # type: ignore[import]
        line = NSView.alloc().initWithFrame_(NSMakeRect(x - 0.5, 0, 1, ch))
        try:
            line.setWantsLayer_(True)
            line.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
        except Exception:
            pass
        line.setAutoresizingMask_(16)  # HeightSizable
        content.addSubview_(line)

    def _build_nav_table(self, col1, ch: float) -> None:
        from Cocoa import (  # type: ignore[import]
            NSMakeRect,
            NSScrollView,
            NSTableColumn,
            NSTableView,
        )
        tv = NSTableView.alloc().initWithFrame_(
            NSMakeRect(0, 0, COL1_W, ch - TOP_INSET))
        col = NSTableColumn.alloc().initWithIdentifier_("nav")
        col.setWidth_(COL1_W - 8)
        tv.addTableColumn_(col)
        tv.setHeaderView_(None)
        tv.setBackgroundColor_(self._clear_color())
        tv.setRowHeight_(30)
        try:
            if hasattr(tv, "setStyle_"):
                tv.setStyle_(1)  # NSTableViewStyleSourceList
        except Exception:
            pass
        ds = _get_table_ds_cls().alloc().initWithOwner_role_(self, "nav")
        self._handlers.append(ds)
        self._nav_ds = ds
        tv.setDataSource_(ds)
        tv.setDelegate_(ds)
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 0, COL1_W, ch - TOP_INSET))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDocumentView_(tv)
        scroll.setAutoresizingMask_(2 | 16)  # Width+HeightSizable
        col1.addSubview_(scroll)
        self._nav_tv = tv

    def _build_list_column(self, col2, ch: float) -> None:
        from Cocoa import (  # type: ignore[import]
            NSMakeRect,
            NSScrollView,
            NSSearchField,
            NSTableColumn,
            NSTableView,
        )
        # Search field at the top.
        sf = NSSearchField.alloc().initWithFrame_(
            NSMakeRect(8, ch - TOP_INSET - SEARCH_H, COL2_W - 16, SEARCH_H))
        sf.setAutoresizingMask_(2 | 8)  # WidthSizable | MinYMargin
        sf_handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda sender: self._on_search(str(sender.stringValue() or "")))
        self._handlers.append(sf_handler)
        sf.setTarget_(sf_handler)
        sf.setAction_("fire:")
        try:
            sf.cell().setSendsSearchStringImmediately_(True)
            sf.cell().setSendsWholeSearchString_(False)
        except Exception:
            pass
        col2.addSubview_(sf)
        self._search_field = sf

        list_top = ch - TOP_INSET - SEARCH_H - 8
        list_h = list_top - ADDBAR_H - 8
        tv = NSTableView.alloc().initWithFrame_(
            NSMakeRect(0, 0, COL2_W, list_h))
        col = NSTableColumn.alloc().initWithIdentifier_("list")
        col.setWidth_(COL2_W - 4)
        tv.addTableColumn_(col)
        tv.setHeaderView_(None)
        tv.setBackgroundColor_(self._clear_color())
        ds = _get_table_ds_cls().alloc().initWithOwner_role_(self, "list")
        self._handlers.append(ds)
        self._list_ds = ds
        tv.setDataSource_(ds)
        tv.setDelegate_(ds)
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, ADDBAR_H + 8, COL2_W, list_h))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDocumentView_(tv)
        scroll.setAutoresizingMask_(2 | 16)
        col2.addSubview_(scroll)
        self._list_tv = tv

        # Add-button bar at the bottom (rebuilt per category).
        self._addbar = None
        self._rebuild_add_buttons()

    def _clear_color(self):
        from Cocoa import NSColor  # type: ignore[import]
        return NSColor.clearColor()

    # ------------------------------------------------------------------ #
    # Data application + render
    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
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
        """Apply freshly fetched data and repaint, preserving nav + list
        selection and the search text. Main thread only."""
        if self.window is None:
            return
        self._cfg = cfg
        self._statuses = statuses
        self._shares = shares
        if self._pending_category is not None:
            cat = self._pending_category
            self._pending_category = None
            self.category = cat if cat in CATEGORIES else self.category
        self._reload_nav()
        self._reload_list(preserve=True)

    def _reload_nav(self) -> None:
        self.nav_items = build_nav(self._cfg, self._shares)
        self._suppress_selection_cb = True
        try:
            self._nav_tv.reloadData()
            idx = next((i for i, n in enumerate(self.nav_items)
                        if n.key == self.category), 0)
            self._select_table_row(self._nav_tv, idx)
        finally:
            self._suppress_selection_cb = False

    def _build_category_rows(self) -> list[ListRow]:
        cfg, statuses, shares = self._cfg, self._statuses, self._shares
        if cfg is None:
            return []
        if self.category == "connections":
            return build_connection_rows(cfg, statuses)
        if self.category == "domains":
            return build_domain_rows(cfg, statuses)
        if self.category == "forwards":
            return build_forward_rows(cfg, statuses)
        if self.category == "shares":
            return build_share_rows(cfg, shares, statuses)
        return []  # settings has no list

    def _reload_list(self, *, preserve: bool) -> None:
        prev = self.selected_identity if preserve else None
        self._all_rows = self._build_category_rows()
        self.rows = filter_rows(self._all_rows, self.search_text)
        self._suppress_selection_cb = True
        try:
            self._list_tv.reloadData()
            target = None
            if prev is not None:
                target = next((i for i, r in enumerate(self.rows)
                               if r.identity == prev), None)
            if target is None:
                target = next((i for i, r in enumerate(self.rows)
                               if r.kind == "item"), None)
            if target is not None:
                self._select_table_row(self._list_tv, target)
                self.selected_identity = self.rows[target].identity
            else:
                self.selected_identity = None
        finally:
            self._suppress_selection_cb = False
        self._rebuild_add_buttons()
        self._render_selection_placeholder()

    def _select_table_row(self, tv, row: int) -> None:
        from Foundation import NSIndexSet  # type: ignore[import]
        tv.selectRowIndexes_byExtendingSelection_(
            NSIndexSet.indexSetWithIndex_(row), False)

    # ------------------------------------------------------------------ #
    # Selection plumbing
    # ------------------------------------------------------------------ #

    def _select_nav(self, category: str) -> None:
        self.category = category
        idx = next((i for i, n in enumerate(self.nav_items)
                    if n.key == category), None)
        if idx is not None:
            self._suppress_selection_cb = True
            try:
                self._select_table_row(self._nav_tv, idx)
            finally:
                self._suppress_selection_cb = False
        self.selected_identity = None
        self._reload_list(preserve=False)

    def _on_selection(self, role: str) -> None:
        if self._suppress_selection_cb:
            return
        if role == "nav":
            row = int(self._nav_tv.selectedRow())
            if 0 <= row < len(self.nav_items):
                self.category = self.nav_items[row].key
                self.selected_identity = None
                self.search_text = ""
                try:
                    self._search_field.setStringValue_("")
                except Exception:
                    pass
                self._reload_list(preserve=False)
        else:  # list
            row = int(self._list_tv.selectedRow())
            if 0 <= row < len(self.rows) and self.rows[row].kind == "item":
                self.selected_identity = self.rows[row].identity
                self._render_selection_placeholder()

    def _on_search(self, text: str) -> None:
        self.search_text = text
        self.rows = filter_rows(self._all_rows, self.search_text)
        prev = self.selected_identity
        self._suppress_selection_cb = True
        try:
            self._list_tv.reloadData()
            target = next((i for i, r in enumerate(self.rows)
                           if r.identity == prev and r.kind == "item"), None)
            if target is None:
                target = next((i for i, r in enumerate(self.rows)
                               if r.kind == "item"), None)
            if target is not None:
                self._select_table_row(self._list_tv, target)
                self.selected_identity = self.rows[target].identity
            else:
                self.selected_identity = None
        finally:
            self._suppress_selection_cb = False
        self._render_selection_placeholder()

    # ------------------------------------------------------------------ #
    # Table data-source callbacks (owner side)
    # ------------------------------------------------------------------ #

    def _row_count(self, role: str) -> int:
        return len(self.nav_items) if role == "nav" else len(self.rows)

    def _row_height(self, role: str, row: int) -> float:
        if role == "nav":
            return 30.0
        if 0 <= row < len(self.rows):
            kind = self.rows[row].kind
            if kind == "item":
                return 38.0
            if kind == "section":
                return 24.0
            return 18.0  # info
        return 20.0

    def _row_selectable(self, role: str, row: int) -> bool:
        if role == "nav":
            return True
        return 0 <= row < len(self.rows) and self.rows[row].kind == "item"

    def _make_cell(self, role: str, row: int):
        if role == "nav":
            return self._make_nav_cell(self.nav_items[row])
        return self._make_list_cell(self.rows[row])

    def _make_nav_cell(self, item: NavItem):
        from AppKit import NSFont, NSImage  # type: ignore[import]
        from Cocoa import (  # type: ignore[import]
            NSColor,
            NSImageView,
            NSMakeRect,
            NSTextField,
            NSView,
        )
        cell = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, COL1_W, 30))
        x = 8
        if item.icon:
            img = self._sf_symbol(item.icon)
            if img is not None:
                iv = NSImageView.alloc().initWithFrame_(NSMakeRect(x, 6, 18, 18))
                iv.setImage_(img)
                cell.addSubview_(iv)
                x += 24
        title = NSTextField.alloc().initWithFrame_(
            NSMakeRect(x, 5, COL1_W - x - 44, 20))
        title.setStringValue_(item.title)
        title.setFont_(NSFont.systemFontOfSize_(13))
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setTextColor_(NSColor.labelColor())
        cell.addSubview_(title)
        if item.count is not None:
            count = NSTextField.alloc().initWithFrame_(
                NSMakeRect(COL1_W - 40, 5, 32, 20))
            count.setStringValue_(str(item.count))
            count.setFont_(NSFont.systemFontOfSize_(12))
            count.setAlignment_(1)  # right
            count.setBezeled_(False)
            count.setDrawsBackground_(False)
            count.setEditable_(False)
            count.setTextColor_(NSColor.secondaryLabelColor())
            cell.addSubview_(count)
        return cell

    def _make_list_cell(self, r: ListRow):
        if r.kind == "section":
            return self._make_section_cell(r)
        if r.kind == "info":
            return self._make_info_cell(r)
        return self._make_item_cell(r)

    def _make_section_cell(self, r: ListRow):
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import NSColor, NSMakeRect, NSTextField, NSView  # type: ignore[import]
        cell = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, COL2_W, 24))
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 4, COL2_W - 20, 16))
        lbl.setStringValue_(r.title.upper())
        lbl.setFont_(NSFont.boldSystemFontOfSize_(11))
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setTextColor_(NSColor.secondaryLabelColor())
        cell.addSubview_(lbl)
        return cell

    def _make_info_cell(self, r: ListRow):
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import NSColor, NSMakeRect, NSTextField, NSView  # type: ignore[import]
        cell = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, COL2_W, 18))
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 1, COL2_W - 20, 16))
        lbl.setStringValue_(r.title)
        lbl.setFont_(NSFont.systemFontOfSize_(11))
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setTextColor_(NSColor.tertiaryLabelColor())
        cell.addSubview_(lbl)
        return cell

    def _make_item_cell(self, r: ListRow):
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import NSColor, NSMakeRect, NSTextField, NSView  # type: ignore[import]
        cell = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, COL2_W, 38))
        title_color = (NSColor.secondaryLabelColor() if r.dimmed
                       else NSColor.labelColor())

        # Colored dot (10px circle, layer-backed).
        if r.dot:
            dot = NSView.alloc().initWithFrame_(NSMakeRect(12, 20, 10, 10))
            try:
                dot.setWantsLayer_(True)
                color = _ns_dot_color(r.dot)
                if r.dimmed:
                    color = color.colorWithAlphaComponent_(0.5)
                dot.layer().setBackgroundColor_(color.CGColor())
                dot.layer().setCornerRadius_(5.0)
            except Exception:
                pass
            cell.addSubview_(dot)
        text_x = 30

        # Badge pill on the right (rounded layer-backed text field).
        badge_w = 0
        if r.badge:
            badge_w = max(34, 14 + 7 * len(r.badge))
            badge = NSTextField.alloc().initWithFrame_(
                NSMakeRect(COL2_W - badge_w - 10, 18, badge_w, 16))
            badge.setStringValue_(r.badge)
            badge.setFont_(NSFont.systemFontOfSize_(10))
            badge.setAlignment_(1)  # center
            badge.setBezeled_(False)
            badge.setEditable_(False)
            badge.setSelectable_(False)
            badge.setTextColor_(NSColor.secondaryLabelColor())
            try:
                badge.setWantsLayer_(True)
                badge.setDrawsBackground_(False)
                badge.layer().setBackgroundColor_(
                    NSColor.quaternaryLabelColor().CGColor())
                badge.layer().setCornerRadius_(7.0)
            except Exception:
                pass
            cell.addSubview_(badge)

        title_w = COL2_W - text_x - (badge_w + 16 if badge_w else 12)
        title = NSTextField.alloc().initWithFrame_(
            NSMakeRect(text_x, 18, max(40, title_w), 18))
        title.setStringValue_(r.title)
        title.setFont_(NSFont.systemFontOfSize_(13))
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setTextColor_(title_color)
        cell.addSubview_(title)

        if r.subtitle:
            sub = NSTextField.alloc().initWithFrame_(
                NSMakeRect(text_x, 2, COL2_W - text_x - 12, 14))
            sub.setStringValue_(r.subtitle)
            sub.setFont_(NSFont.systemFontOfSize_(11))
            sub.setBezeled_(False)
            sub.setDrawsBackground_(False)
            sub.setEditable_(False)
            sub.setTextColor_(NSColor.secondaryLabelColor())
            cell.addSubview_(sub)
        return cell

    def _sf_symbol(self, name: str):
        from AppKit import NSImage  # type: ignore[import]
        try:
            if hasattr(NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
                return NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    name, None)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ #
    # Add buttons (column 2 bottom)
    # ------------------------------------------------------------------ #

    def _rebuild_add_buttons(self) -> None:
        from Cocoa import NSButton, NSMakeRect, NSView  # type: ignore[import]
        # Tear down the previous bar.
        if self._addbar is not None:
            try:
                self._addbar.removeFromSuperview()
            except Exception:
                pass
            self._addbar = None
        specs = self._add_button_specs()
        if not specs:
            return
        bar = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, COL2_W, ADDBAR_H))
        bar.setAutoresizingMask_(2 | 32)  # WidthSizable | MaxYMargin
        n = len(specs)
        gap = 8
        bw = (COL2_W - gap * (n + 1)) / n
        x = gap
        for label, kind in specs:
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, 6, bw, 26))
            btn.setTitle_(label)
            btn.setBezelStyle_(1)
            handler = _get_action_handler_cls().alloc().initWithCallback_(
                lambda _s, k=kind: self._on_add_clicked(k))
            self._handlers.append(handler)
            btn.setTarget_(handler)
            btn.setAction_("fire:")
            bar.addSubview_(btn)
            x += bw + gap
        self._col2.addSubview_(bar)
        self._addbar = bar

    def _add_button_specs(self) -> list[tuple[str, str]]:
        if self.category == "connections":
            return [("＋ Add Connection", "connection")]
        if self.category == "domains":
            return [("＋ Add Domain / IP / CIDR", "domain")]
        if self.category == "forwards":
            return [("＋ Add Forward", "forward")]
        if self.category == "shares":
            return [("＋ Share File…", "share"),
                    ("Fetch…", "fetch")]
        return []  # settings

    def _on_add_clicked(self, kind: str) -> None:
        # Task 2: no-op placeholder; real create forms land in Tasks 5/6.
        self._render_col3_placeholder("Create forms land in Task 5/6.")

    # ------------------------------------------------------------------ #
    # Column 3 (placeholder in Task 2)
    # ------------------------------------------------------------------ #

    def _render_selection_placeholder(self) -> None:
        if self.category == "settings":
            self._render_col3_placeholder("Settings pane lands in Task 7.")
            return
        if self.selected_identity is None:
            self._render_col3_placeholder("Select an item.")
            return
        # Task 3 renders the real detail; for now show the identity tuple as
        # text (useful for dump/tests).
        self._render_col3_placeholder(
            "Detail pane lands in Task 3.\n"
            + " · ".join(str(p) for p in self.selected_identity))

    def _render_col3_placeholder(self, text: str) -> None:
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import NSColor, NSMakeRect, NSTextField  # type: ignore[import]
        for v in list(self._col3.subviews()):
            v.removeFromSuperview()
        del self._handlers[self._permanent_handler_count:]
        w = self._col3.frame().size.width
        h = self._col3.frame().size.height
        lbl = NSTextField.alloc().initWithFrame_(
            NSMakeRect(24, h - TOP_INSET - 80, w - 48, 80))
        lbl.setStringValue_(text)
        lbl.setFont_(NSFont.systemFontOfSize_(13))
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(True)
        lbl.setTextColor_(NSColor.secondaryLabelColor())
        try:
            lbl.cell().setWraps_(True)
        except Exception:
            pass
        self._col3.addSubview_(lbl)
        self._col3_text = text

    # ------------------------------------------------------------------ #
    # Debug surface
    # ------------------------------------------------------------------ #

    def dump(self) -> dict:
        return {
            "open": self.is_open(),
            "nav": [{"key": n.key, "title": n.title, "count": n.count}
                    for n in self.nav_items],
            "category": self.category,
            "search": self.search_text,
            "rows": [{"kind": r.kind, "title": r.title, "subtitle": r.subtitle,
                      "dot": r.dot, "badge": r.badge, "dimmed": r.dimmed}
                     for r in self.rows],
            "selected": list(self.selected_identity)
            if self.selected_identity else None,
            "detail_title": None,
            "dirty": False,
        }

    def select(self, category: str, index: int | None = None) -> dict:
        if category not in CATEGORIES:
            return {"error": f"unknown category: {category}"}
        self._select_nav(category)
        if index is None:
            return {"ok": True, "selected": None, "category": category}
        item_rows = [i for i, r in enumerate(self.rows) if r.kind == "item"]
        if index < 0 or index >= len(item_rows):
            return {"error": f"no item row at index {index} in {category}"}
        target = item_rows[index]
        self._select_table_row(self._list_tv, target)
        self.selected_identity = self.rows[target].identity
        self._render_selection_placeholder()
        return {"ok": True, "selected": list(self.selected_identity)}

    def set_search(self, text: str) -> dict:
        try:
            self._search_field.setStringValue_(text)
        except Exception:
            pass
        self._on_search(text)
        return {"ok": True, "search": self.search_text,
                "rows": len([r for r in self.rows if r.kind == "item"])}
