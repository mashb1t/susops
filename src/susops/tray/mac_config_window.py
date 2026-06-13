"""3-column config window - raw AppKit (rumps has no window classes).

Layout per docs/superpowers/specs/2026-06-13-mac-tray-3col-redesign-design.md:
column 1 = nav (categories + Settings), column 2 = global list with search +
add buttons, column 3 = detail/editor (read-only connection pane, inline
edit/create forms for domains/forwards/shares, settings pane).

Lifecycle copies _open_live_text_window in mac.py: non-modal NSWindow,
held-open _RegularPolicyScope, close via delegate, module-level cached
NSObject subclasses (PyObjC re-registration bug - see mac.py). Two table data
sources share one cached class via a `role` attr ("nav" / "list").
"""
from __future__ import annotations

import warnings

import objc  # type: ignore[import]

# Setting layer.setBackgroundColor_/setBorderColor_ with NSColor.CGColor() emits
# a benign ObjCPointerWarning (the layer retains the CGColor, no leak). Silence
# the known pattern so it does not spam the tray log.
warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)

from susops.tray.config_window_model import (
    ListRow,
    NavItem,
    build_connection_detail,
    build_connection_form,
    build_connection_rows,
    build_domain_form,
    build_domain_rows,
    build_fetch_form,
    build_forward_form,
    build_forward_rows,
    build_nav,
    build_share_detail,
    build_share_form,
    build_share_rows,
    filter_rows,
)

_table_ds_cls = None
_window_delegate_cls = None
_action_handler_cls = None
_text_delegate_cls = None
_row_view_cls = None
_vcenter_text_cell_cls = None
_TEXT_CELL_FLAG_ACCESSORS = (
    ("isEditable", "setEditable_"),
    ("isSelectable", "setSelectable_"),
    ("isScrollable", "setScrollable_"),
    ("wraps", "setWraps_"),
    ("usesSingleLineMode", "setUsesSingleLineMode_"),
    ("lineBreakMode", "setLineBreakMode_"),
)


def _make_vcenter_cell_cls(base_cell_cls):
    """Build a cached NSCell subclass that vertically centers text for both
    display and edit rects."""
    class _SusOpsVCenterCell(base_cell_cls):
        def _baseTitleRect_(self, frame):
            try:
                rect = objc.super(_SusOpsVCenterCell, self).titleRectForBounds_(
                    frame)
            except Exception:
                rect = objc.super(_SusOpsVCenterCell, self).drawingRectForBounds_(
                    frame)
            return rect

        def _centeredRect_(self, frame):
            rect = self._baseTitleRect_(frame)
            # titleRectForBounds_ can already be full-height on borderless fields,
            # so derive a one-line text height from cellSizeForBounds_.
            text_h = rect.size.height
            try:
                size = objc.super(_SusOpsVCenterCell, self).cellSizeForBounds_(
                    frame)
                if size is not None and getattr(size, "height", 0) > 0:
                    text_h = min(rect.size.height, float(size.height))
            except Exception:
                pass
            text_h = max(1.0, min(float(frame.size.height), float(text_h)))
            rect.origin.y = (
                float(frame.origin.y)
                + max(0.0, (float(frame.size.height) - text_h) / 2.0)
            )
            rect.size.height = text_h
            return rect

        def titleRectForBounds_(self, frame):
            return self._centeredRect_(frame)

        def drawingRectForBounds_(self, frame):
            return self._centeredRect_(frame)

        def drawInteriorWithFrame_inView_(self, frame, view):
            objc.super(_SusOpsVCenterCell, self).drawInteriorWithFrame_inView_(
                self._centeredRect_(frame), view)

        def selectWithFrame_inView_editor_delegate_start_length_(
            self, frame, view, editor, delegate, start, length
        ):
            objc.super(_SusOpsVCenterCell, self).selectWithFrame_inView_editor_delegate_start_length_(
                self._centeredRect_(frame), view, editor, delegate, start, length
            )

        def editWithFrame_inView_editor_delegate_event_(
            self, frame, view, editor, delegate, event
        ):
            objc.super(_SusOpsVCenterCell, self).editWithFrame_inView_editor_delegate_event_(
                self._centeredRect_(frame), view, editor, delegate, event
            )

    return _SusOpsVCenterCell


def _get_vcenter_text_cell_cls():
    global _vcenter_text_cell_cls
    if _vcenter_text_cell_cls is not None:
        return _vcenter_text_cell_cls
    from Cocoa import NSTextFieldCell  # type: ignore[import]

    _vcenter_text_cell_cls = _make_vcenter_cell_cls(NSTextFieldCell)
    return _vcenter_text_cell_cls


def _truncate_tail(field) -> None:
    """Single-line + truncate an NSTextField so overflow shows an ellipsis
    instead of a raw hard cut at the field edge. NSLineBreakByTruncatingTail = 4
    (a label-style cell still draws the ellipsis mid-string, but never a raw
    cut). Call after the field's string/font/color are set."""
    try:
        field.setUsesSingleLineMode_(True)
        field.cell().setLineBreakMode_(4)
    except Exception:
        pass


def _hex_color(hex_str: str, alpha: float = 1.0):
    """NSColor from an "rrggbb" hex string (sRGB). Module-level so the future
    settings pane can reuse the same palette."""
    from Cocoa import NSColor  # type: ignore[import]
    h = hex_str.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


# Always-dark palette (Tailscale-like), pinned regardless of system mode. Hex
# values are the mockup contract; keep in sync with the spec appearance row.
PALETTE = {
    "window": "17181c",      # col3 base + window
    "col1": "25262c",        # nav band
    "col2": "1f2026",        # list band
    "card": "222329",        # elevated card in col3
    "input_fill": "2a2b31",  # text/secure field fill
    "input_border": "3f4147",
    "input_text": "e8e9ed",
    "badge_fill": "3a3c44",
    "badge_text": "c7c9d1",
    "separator": "0a0a0c",   # near-black hairline
}


def _get_text_delegate_cls():
    """Cached NSTextField/NSComboBox delegate that flags the owner dirty on
    every keystroke (controlTextDidChange_). Instances live in _handlers and
    are released by the col-3 trim."""
    global _text_delegate_cls
    if _text_delegate_cls is not None:
        return _text_delegate_cls
    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _SusOpsTextDelegate(NSObject):
        def initWithCallback_(self, cb):
            self = objc.super(_SusOpsTextDelegate, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        def controlTextDidChange_(self, _note):
            try:
                self._cb()
            except Exception:
                pass

        def comboBoxSelectionDidChange_(self, _note):
            try:
                self._cb()
            except Exception:
                pass

        def comboBoxSelectionIsChanging_(self, _note):
            try:
                self._cb()
            except Exception:
                pass

    _text_delegate_cls = _SusOpsTextDelegate
    return _SusOpsTextDelegate


def _apply_vcenter_cell(control, cell_cls) -> None:
    """Swap in a vertically-centered cell while preserving common text attrs."""
    try:
        old = control.cell()
        try:
            value = str(control.stringValue() or "")
        except Exception:
            value = ""
        cell = cell_cls.alloc().init()
        try:
            cell.setFont_(old.font())
        except Exception:
            pass
        try:
            cell.setPlaceholderString_(old.placeholderString())
        except Exception:
            pass
        # Keep old text-field behavior (single-line editable entry) so swapping
        # the cell does not fall back to a label-like top-left layout.
        for getter, setter in _TEXT_CELL_FLAG_ACCESSORS:
            try:
                getattr(cell, setter)(getattr(old, getter)())
            except Exception:
                pass
        control.setCell_(cell)
        try:
            control.setStringValue_(value)
        except Exception:
            pass
    except Exception:
        pass


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

        def tableView_rowViewForRow_(self, _tv, _row):
            rv = _get_row_view_cls().alloc().init()
            rv.susopsRole = self._role
            return rv

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


def _get_row_view_cls():
    """Cached NSTableRowView subclass drawing a rounded accent selection pill
    instead of the square system highlight. Used by both tables."""
    global _row_view_cls
    if _row_view_cls is not None:
        return _row_view_cls
    from Cocoa import NSBezierPath, NSColor, NSTableRowView  # type: ignore[import]
    from Foundation import NSMakeRect  # type: ignore[import]

    class _SusOpsRowView(NSTableRowView):
        susopsRole = "list"

        def drawSelectionInRect_(self, rect):
            if not self.isSelected():
                return
            b = self.bounds()
            inset_y = 0.0
            inset = 9.0  # equal floating margin both sides (macOS System Settings)
            radius = 7.0 if getattr(self, "susopsRole", "list") == "nav" else 6.0
            # The table view can be wider than its visible column (the row view
            # overflows the clip), which would push the pill's right edge under
            # the next column and make it look flush. Clamp the pill to the
            # visible clip width so it floats with an equal margin both sides.
            width = b.size.width
            try:
                sv = self.enclosingScrollView()
                if sv is not None:
                    cw = sv.contentView().bounds().size.width
                    if cw > 0:
                        width = cw
            except Exception:
                pass
            r = NSMakeRect(b.origin.x + inset, b.origin.y + inset_y,
                           width - 2 * inset,
                           b.size.height - 2 * inset_y)
            try:
                accent = NSColor.controlAccentColor()
            except Exception:
                accent = NSColor.alternateSelectedControlColor()
            accent.set()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                r, radius, radius)
            path.fill()

    _row_view_cls = _SusOpsRowView
    return _SusOpsRowView


# ---- geometry ----
WIN_W = 1024
WIN_H = 640
MIN_W = 1024
MIN_H = 640
COL1_W = 180
COL2_W = 270
TOP_INSET = 38          # traffic lights overlay col 1; start content below them
SIDEBAR_TOP_INSET = 23  # nav-only top inset (kept independent for fine tuning)
SEARCH_H = 30
ADDBAR_H = 40

# Column-3 content is constrained to a fixed-width column anchored top-left,
# NOT stretched to the window edge. The Enabled toggle anchors to the RIGHT
# EDGE of this content column (near the title), not the window border.
CONTENT_MAX_W = 540
CONTENT_PAD = 16

# Balanced inner padding for the detail card: the same inset on all four sides
# so controls never touch any card edge. CARD_PAD_X reserves left AND right.
# CARD_PAD_Y reserves top AND bottom.
CARD_PAD_X = 16
CARD_PAD_Y = 14
# Bezeled editable controls (text/secure/combo) are 22 tall, popups 24. The
# label is vertically centered on this control band. CARD_LABEL_NUDGE shifts the
# top-drawn label text down so its baseline lines up with a bezeled field's
# inset text (a label and a field of equal frame height do NOT center the same).
CARD_ROW_H = 32
CARD_CTRL_H = 22
CARD_LABEL_NUDGE = -2

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
        self._addbar_handlers: list = []  # add-button targets, own lifetime
        self._permanent_handler_count = 0
        self._cfg = None
        self._statuses: list = []
        self._shares: list = []
        self._suppress_selection_cb = False
        self._pending_category = None  # category requested via open() pre-load
        self._detail_spec = None       # last DetailSpec rendered in col 3
        # --- editing / dirty tracking ---
        self._dirty = False
        self._field_widgets: dict = {}   # field key -> widget (live col-3 form)
        self._field_kinds: dict = {}     # field key -> FormField.kind
        self._secure_pair: dict = {}     # field key -> (secure, plain) fields
        self._save_button = None         # cached Save NSButton (enable on dirty)
        self._header_toggle_label = None  # cached header toggle word label
        self._dirty_identity = None      # identity the dirty form belongs to
        # Create mode: "connection" | "domain" | "forward" or None. While set,
        # col-2 selection is cleared and col 3 shows an inline create form with
        # an always-enabled Create button + a Cancel button.
        self._create_kind = None
        # Settings pane: when the Settings nav row is selected col 2 is hidden
        # and col 3 spans cols 2+3. Live setting widgets are cached here so the
        # debug surface (dump / set-field) and the value collector can read
        # them. Settings changes use their own staging flag (_settings_dirty),
        # separate from the detail-form _dirty machinery.
        self._col2_hidden = False
        self._settings_widgets: dict = {}   # field key -> control
        self._settings_kinds: dict = {}     # field key -> kind
        self._settings_ctx: dict = {}       # ctx from settings_field_specs
        # Settings staging: all settings changes are staged locally and only
        # persist on Apply. _settings_dirty tracks pending edits so leaving the
        # category or closing the window can discard them (and revert the logo
        # preview).
        self._settings_dirty = False

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
        # Closing the window without Apply discards any staged settings
        # changes (and reverts a logo icon preview to the saved logo).
        self.discard_settings()
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
            NSWindowTitleVisible,
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
        # Pin DarkAqua so the window is always dark regardless of system mode;
        # system controls (popups, switch, search) inherit it automatically.
        try:
            from AppKit import (  # type: ignore[import]
                NSAppearance,
                NSAppearanceNameDarkAqua,
            )
            win.setAppearance_(
                NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua))
        except Exception:
            pass
        # Title visible + centered over the dark chrome (mockup).
        try:
            win.setTitleVisibility_(NSWindowTitleVisible)
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
                _hex_color(PALETTE["window"]).CGColor())
        except Exception:
            pass

        ch = WIN_H

        # --- Column 1 (nav band, darkest) ---
        col1 = self._make_band(NSMakeRect(0, 0, COL1_W, ch), band="col1")
        col1.setAutoresizingMask_(16 | 32)  # HeightSizable | MaxYMargin
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
                _hex_color(PALETTE["window"]).CGColor())
        except Exception:
            pass
        col3.setAutoresizingMask_(2 | 16)  # Width+HeightSizable
        content.addSubview_(col3)
        self._col3 = col3

        # --- thin separators between columns ---
        self._add_separator(content, COL1_W, ch)
        # sep2 sits at the col2/col3 boundary; hidden with col2 in Settings.
        self._sep2 = self._add_separator(content, COL1_W + COL2_W, ch)

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
        """Layer-backed NSView painted with the exact mockup palette so the
        three columns read as layered near-black bands (col1 nav, col2 list)."""
        from Cocoa import NSView  # type: ignore[import]
        view = NSView.alloc().initWithFrame_(frame)
        try:
            view.setWantsLayer_(True)
            view.layer().setBackgroundColor_(_hex_color(PALETTE[band]).CGColor())
        except Exception:
            pass
        return view

    def _add_separator(self, content, x: float, ch: float) -> None:
        from Cocoa import NSMakeRect, NSView  # type: ignore[import]
        line = NSView.alloc().initWithFrame_(NSMakeRect(x - 0.5, 0, 1, ch))
        try:
            line.setWantsLayer_(True)
            line.layer().setBackgroundColor_(
                _hex_color(PALETTE["separator"]).CGColor())
        except Exception:
            pass
        line.setAutoresizingMask_(16)  # HeightSizable
        content.addSubview_(line)
        return line

    def _set_col2_visible(self, visible: bool) -> None:
        """Show/hide column 2 (+ its separator) and slide column 3 to fill the
        freed space. Settings spans cols 2+3 with col 2 hidden; every other
        category restores the 3-column geometry. Idempotent."""
        if visible == (not self._col2_hidden):
            return
        self._col2_hidden = not visible
        from Cocoa import NSMakeRect  # type: ignore[import]
        try:
            self._col2.setHidden_(not visible)
            self._sep2.setHidden_(not visible)
        except Exception:
            pass
        # Reposition col 3: start at COL1_W (col 2 hidden) or COL1_W+COL2_W.
        win_w = self.window.contentView().frame().size.width if self.window else WIN_W
        col3_x = COL1_W if not visible else COL1_W + COL2_W
        f = self._col3.frame()
        self._col3.setFrame_(NSMakeRect(col3_x, 0, win_w - col3_x, f.size.height))

    def _build_nav_table(self, col1, ch: float) -> None:
        from Cocoa import (  # type: ignore[import]
            NSMakeRect,
            NSScrollView,
            NSTableColumn,
            NSTableView,
        )
        tv = NSTableView.alloc().initWithFrame_(
            NSMakeRect(0, 0, COL1_W, ch - SIDEBAR_TOP_INSET))
        col = NSTableColumn.alloc().initWithIdentifier_("nav")
        col.setWidth_(COL1_W - 8)
        tv.addTableColumn_(col)
        tv.setHeaderView_(None)
        tv.setBackgroundColor_(self._clear_color())
        tv.setRowHeight_(30)
        # Regular highlight style so the custom row view's drawSelectionInRect_
        # is invoked; the override paints a rounded accent pill instead of the
        # stock square fill (no double-draw).
        try:
            tv.setSelectionHighlightStyle_(0)  # NSTableViewSelectionHighlightStyleRegular
        except Exception:
            pass
        ds = _get_table_ds_cls().alloc().initWithOwner_role_(self, "nav")
        self._handlers.append(ds)
        self._nav_ds = ds
        tv.setDataSource_(ds)
        tv.setDelegate_(ds)
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 0, COL1_W, ch - SIDEBAR_TOP_INSET))
        scroll.setDrawsBackground_(False)
        scroll.setHasHorizontalScroller_(False)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDocumentView_(tv)
        scroll.setAutoresizingMask_(2 | 16)  # Width+HeightSizable
        col1.addSubview_(scroll)
        self._fit_table_to_scroll_width(tv, scroll, col_inset=8)
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
        try:
            tv.setSelectionHighlightStyle_(0)  # Regular; custom pill draws it
        except Exception:
            pass
        ds = _get_table_ds_cls().alloc().initWithOwner_role_(self, "list")
        self._handlers.append(ds)
        self._list_ds = ds
        tv.setDataSource_(ds)
        tv.setDelegate_(ds)
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, ADDBAR_H + 8, COL2_W, list_h))
        scroll.setDrawsBackground_(False)
        scroll.setHasHorizontalScroller_(False)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        # Overlay scrollers float over content and reserve no gutter, so the
        # selection pill can float with an equal margin on both sides (matching
        # the nav) instead of insetting extra on the right for a legacy scrollbar.
        try:
            scroll.setScrollerStyle_(1)  # NSScrollerStyleOverlay
        except Exception:
            pass
        scroll.setDocumentView_(tv)
        scroll.setAutoresizingMask_(2 | 16)
        col2.addSubview_(scroll)
        self._fit_table_to_scroll_width(tv, scroll, col_inset=4)
        self._list_tv = tv

        # Add-button bar at the bottom (rebuilt per category).
        self._addbar = None
        self._rebuild_add_buttons()

    def _clear_color(self):
        from Cocoa import NSColor  # type: ignore[import]
        return NSColor.clearColor()

    def _fit_table_to_scroll_width(self, tv, scroll, *, col_inset: float) -> None:
        """Keep table document width and first-column width aligned to the
        scroll viewport width so horizontal movement cannot appear."""
        from Cocoa import NSMakeRect  # type: ignore[import]
        if scroll is None:
            try:
                scroll = tv.enclosingScrollView()
            except Exception:
                scroll = None
        if scroll is None:
            return
        try:
            clip_w = float(scroll.contentView().bounds().size.width)
        except Exception:
            clip_w = float(scroll.frame().size.width)
        if clip_w <= 0:
            return
        try:
            fr = tv.frame()
            tv.setFrame_(NSMakeRect(fr.origin.x, fr.origin.y, clip_w, fr.size.height))
        except Exception:
            pass
        try:
            cols = tv.tableColumns()
            if cols:
                cols[0].setWidth_(max(40, clip_w - float(col_inset)))
        except Exception:
            pass

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
        # Settings pane: col 2 is hidden and col 3 holds instant-apply controls
        # not driven by the col-2 list. A poll must not rebuild col 2 or stomp
        # the settings form. On the FIRST entry (no settings widgets yet, e.g.
        # opened via open("settings") before the initial data load) render the
        # pane; thereafter leave both columns untouched so a poll never clobbers
        # in-flight port edits.
        if self.category == "settings":
            if not self._col2_hidden:
                self._enter_settings()
            return
        # While a col-3 form is dirty OR a create form is open, cols 1-2 keep
        # refreshing but column 3 stays untouched. skip_detail pins the col-2
        # selection to the dirty identity (None during create, so no row is
        # forced) so neither an in-flight edit nor an open empty create form is
        # clobbered by a poll.
        self._reload_list(preserve=True,
                          skip_detail=self._dirty or self._create_kind is not None)

    def _reload_nav(self) -> None:
        self.nav_items = build_nav(self._cfg, self._shares)
        self._suppress_selection_cb = True
        try:
            self._nav_tv.reloadData()
            self._fit_table_to_scroll_width(self._nav_tv, None, col_inset=8)
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

    def _reload_list(self, *, preserve: bool, skip_detail: bool = False) -> None:
        # When a col-3 form is dirty (skip_detail), keep the col-2 highlight on
        # the dirty identity and do NOT touch column 3.
        prev = (self._dirty_identity if skip_detail
                else (self.selected_identity if preserve else None))
        self._all_rows = self._build_category_rows()
        self.rows = filter_rows(self._all_rows, self.search_text)
        self._suppress_selection_cb = True
        try:
            self._list_tv.reloadData()
            self._fit_table_to_scroll_width(self._list_tv, None, col_inset=4)
            target = None
            if prev is not None:
                target = next((i for i, r in enumerate(self.rows)
                               if r.identity == prev), None)
            if target is None and not skip_detail:
                target = next((i for i, r in enumerate(self.rows)
                               if r.kind == "item"), None)
            if target is not None:
                self._select_table_row(self._list_tv, target)
                if not skip_detail:
                    self.selected_identity = self.rows[target].identity
            elif not skip_detail:
                self.selected_identity = None
        finally:
            self._suppress_selection_cb = False
        self._rebuild_add_buttons()
        if not skip_detail:
            self._render_selection_placeholder()

    def _select_table_row(self, tv, row: int) -> None:
        from Foundation import NSIndexSet  # type: ignore[import]
        tv.selectRowIndexes_byExtendingSelection_(
            NSIndexSet.indexSetWithIndex_(row), False)

    # ------------------------------------------------------------------ #
    # Selection plumbing
    # ------------------------------------------------------------------ #

    def _select_nav(self, category: str) -> None:
        # Leaving the settings category without Apply discards staged changes
        # (reverts the logo preview too). Real clicks also pass through
        # _on_selection which discards; this covers the programmatic/debug path.
        if self.category == "settings" and category != "settings":
            self.discard_settings()
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
        if category == "settings":
            self._enter_settings()
            return
        self._set_col2_visible(True)
        self._reload_list(preserve=False)

    def _on_selection(self, role: str) -> None:
        if self._suppress_selection_cb:
            return
        if role == "nav":
            row = int(self._nav_tv.selectedRow())
            if 0 <= row < len(self.nav_items):
                new_cat = self.nav_items[row].key
                # Leaving the settings category without Apply discards all
                # staged settings changes (reverts the logo preview too).
                if self.category == "settings" and new_cat != "settings":
                    self.discard_settings()
                if (self._dirty and new_cat != self.category):
                    if not self._confirm_discard():
                        # No: keep editing - revert nav selection to the
                        # dirty form's category.
                        idx = next((i for i, n in enumerate(self.nav_items)
                                    if n.key == self.category), None)
                        if idx is not None:
                            self._suppress_selection_cb = True
                            try:
                                self._select_table_row(self._nav_tv, idx)
                            finally:
                                self._suppress_selection_cb = False
                        return
                    self._dirty = False
                    self._dirty_identity = None
                self._create_kind = None
                self.category = new_cat
                self.selected_identity = None
                self.search_text = ""
                try:
                    self._search_field.setStringValue_("")
                except Exception:
                    pass
                if new_cat == "settings":
                    self._enter_settings()
                    return
                self._set_col2_visible(True)
                self._reload_list(preserve=False)
        else:  # list
            row = int(self._list_tv.selectedRow())
            if 0 <= row < len(self.rows) and self.rows[row].kind == "item":
                new_identity = self.rows[row].identity
                # Leaving an edit form (dirty + identity) OR a dirty create form
                # both prompt the discard confirm.
                leaving_dirty = self._dirty and (
                    self._create_kind is not None
                    or (self._dirty_identity is not None
                        and new_identity != self._dirty_identity))
                if leaving_dirty:
                    if not self._confirm_discard():
                        # No: keep editing - revert the table selection.
                        if self._create_kind is not None:
                            self._reselect_none()
                        else:
                            self._reselect_dirty_row()
                        return
                    self._dirty = False
                    self._dirty_identity = None
                self._create_kind = None
                self.selected_identity = new_identity
                self._render_selection_placeholder()

    def _confirm_discard(self) -> bool:
        """Modal confirm when the user navigates away from a dirty form. Returns
        True to discard (proceed), False to keep editing."""
        from susops.tray.mac import _show_confirm
        try:
            return bool(_show_confirm(
                "Discard unsaved changes?",
                "You have unsaved edits in this form. Discard them?",
                ok="Discard", cancel="Keep Editing"))
        except Exception:
            return True

    def _reselect_dirty_row(self) -> None:
        """Restore the col-2 highlight to the dirty form's identity under the
        selection-suppression flag (No path of the discard confirm)."""
        target = next((i for i, r in enumerate(self.rows)
                       if r.identity == self._dirty_identity), None)
        if target is None:
            return
        self._suppress_selection_cb = True
        try:
            self._select_table_row(self._list_tv, target)
        finally:
            self._suppress_selection_cb = False

    def _reselect_none(self) -> None:
        """Clear the col-2 highlight under the suppression flag. Used on the No
        path of the discard confirm while a create form is open (create mode has
        no selected row)."""
        self._suppress_selection_cb = True
        try:
            from Foundation import NSIndexSet  # type: ignore[import]
            self._list_tv.selectRowIndexes_byExtendingSelection_(
                NSIndexSet.indexSet(), False)
        except Exception:
            pass
        finally:
            self._suppress_selection_cb = False

    def _on_search(self, text: str) -> None:
        self.search_text = text
        self.rows = filter_rows(self._all_rows, self.search_text)
        prev = self.selected_identity
        self._suppress_selection_cb = True
        try:
            self._list_tv.reloadData()
            self._fit_table_to_scroll_width(self._list_tv, None, col_inset=4)
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
        row_w = self._nav_viewport_width()
        cell_x = 9
        cell_w = max(40, row_w - 2 * cell_x)
        cell = NSView.alloc().initWithFrame_(NSMakeRect(cell_x, 0, cell_w, 30))
        self._style_debug_row_cell(cell)
        x = 8
        if item.icon:
            img = self._sf_symbol(item.icon)
            if img is not None:
                # Center the icon on the same line as the title and count (both
                # 20-tall boxes at y=5, center 15) so all three align.
                iv = NSImageView.alloc().initWithFrame_(NSMakeRect(x, 6, 18, 18))
                iv.setImage_(img)
                cell.addSubview_(iv)
                x += 24
        title = NSTextField.alloc().initWithFrame_(
            NSMakeRect(x, 4, cell_w - x - 44, 20))
        title.setStringValue_(item.title)
        title.setFont_(NSFont.systemFontOfSize_(13))
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setTextColor_(NSColor.labelColor())
        _truncate_tail(title)
        cell.addSubview_(title)
        if item.count is not None:
            # Sit inside the selection pill (which floats inset ~9px from the
            # column edge), not in the margin outside it.
            count = NSTextField.alloc().initWithFrame_(
                NSMakeRect(cell_w - 47, 3, 28, 20))
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
        row_w = self._list_viewport_width()
        cell_x = 9
        cell_w = max(40, row_w - 2 * cell_x)
        cell = NSView.alloc().initWithFrame_(NSMakeRect(cell_x, 0, cell_w, 24))
        self._style_debug_row_cell(cell)
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 4, cell_w - 20, 16))
        # Section titles render as the model emits them ("Local"/"Remote"),
        # NOT uppercased. Medium weight, secondary color.
        lbl.setStringValue_(r.title)
        try:
            lbl.setFont_(NSFont.systemFontOfSize_weight_(12, 0.23))  # medium
        except Exception:
            lbl.setFont_(NSFont.boldSystemFontOfSize_(11))
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setTextColor_(NSColor.secondaryLabelColor())
        _truncate_tail(lbl)
        cell.addSubview_(lbl)
        return cell

    def _make_info_cell(self, r: ListRow):
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import NSColor, NSMakeRect, NSTextField, NSView  # type: ignore[import]
        row_w = self._list_viewport_width()
        cell_x = 9
        cell_w = max(40, row_w - 2 * cell_x)
        cell = NSView.alloc().initWithFrame_(NSMakeRect(cell_x, 0, cell_w, 18))
        self._style_debug_row_cell(cell)
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 1, cell_w - 20, 16))
        lbl.setStringValue_(r.title)
        lbl.setFont_(NSFont.systemFontOfSize_(11))
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setTextColor_(NSColor.tertiaryLabelColor())
        _truncate_tail(lbl)
        cell.addSubview_(lbl)
        return cell

    def _make_item_cell(self, r: ListRow):
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import NSColor, NSMakeRect, NSTextField, NSView  # type: ignore[import]
        row_w = self._list_viewport_width()
        cell_x = 9
        cell_w = max(40, row_w - 2 * cell_x)
        cell = NSView.alloc().initWithFrame_(NSMakeRect(cell_x, 0, cell_w, 38))
        self._style_debug_row_cell(cell)
        title_color = (NSColor.secondaryLabelColor() if r.dimmed
                       else NSColor.labelColor())

        # Single-line rows (e.g. domains, no subtitle) center their title, dot
        # and badge vertically in the 38px row. Two-line rows keep the stacked
        # title (top) + subtitle (bottom) layout.
        two_line = bool(r.subtitle)
        dot_y = 20 if two_line else 14
        title_y = 18 if two_line else 10
        badge_y = 18 if two_line else 11

        # Colored dot (10px circle, layer-backed).
        if r.dot:
            dot = NSView.alloc().initWithFrame_(NSMakeRect(12, dot_y, 10, 10))
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

        # Badge pill on the right (rounded layer-backed text field). Inset from
        # the column edge enough to clear the scroller gutter so the FULL
        # rounded pill (both corners) shows clear of the vertical scroller and
        # the cell edge (mockup). The selection pill insets 4px, the scroller
        # overlays ~14-16px, so the pill needs a wider inset than that.
        badge_w = 0
        badge_right_inset = 27
        # Keep text inside the same floating row area as the selection pill.
        pill_right_inset = 9
        row_right_limit = cell_w - pill_right_inset - 18
        if r.badge:
            badge_w = max(34, 14 + 7 * len(r.badge))
            badge = NSTextField.alloc().initWithFrame_(
                NSMakeRect(cell_w - badge_w - badge_right_inset, badge_y, badge_w, 16))
            badge.setStringValue_(r.badge)
            badge.setFont_(NSFont.systemFontOfSize_(10))
            badge.setAlignment_(1)  # center
            badge.setBezeled_(False)
            badge.setEditable_(False)
            badge.setSelectable_(False)
            badge.setTextColor_(_hex_color(PALETTE["badge_text"]))
            try:
                badge.setWantsLayer_(True)
                badge.setDrawsBackground_(False)
                badge.layer().setBackgroundColor_(
                    _hex_color(PALETTE["badge_fill"]).CGColor())
                badge.layer().setCornerRadius_(8.0)
            except Exception:
                pass
            cell.addSubview_(badge)

        title_right_limit = (cell_w - badge_w - badge_right_inset - 12
                             if badge_w else row_right_limit)
        title_w = max(40, title_right_limit - text_x)
        title = NSTextField.alloc().initWithFrame_(
            NSMakeRect(text_x, title_y, title_w, 18))
        title.setStringValue_(r.title)
        title.setFont_(NSFont.systemFontOfSize_(13))
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setTextColor_(title_color)
        _truncate_tail(title)
        cell.addSubview_(title)

        if r.subtitle:
            sub = NSTextField.alloc().initWithFrame_(
                NSMakeRect(text_x, 1, max(40, row_right_limit - text_x), 16))
            sub.setStringValue_(r.subtitle)
            sub.setFont_(NSFont.systemFontOfSize_(11))
            sub.setBezeled_(False)
            sub.setDrawsBackground_(False)
            sub.setEditable_(False)
            sub.setTextColor_(NSColor.secondaryLabelColor())
            _truncate_tail(sub)
            cell.addSubview_(sub)
        return cell

    def _list_viewport_width(self) -> float:
        """Visible width of column 2's table area used for row content layout."""
        tv = getattr(self, "_list_tv", None)
        return self._table_viewport_width(tv, fallback=float(COL2_W))

    def _nav_viewport_width(self) -> float:
        """Visible width of column 1's table area used for row content layout."""
        tv = getattr(self, "_nav_tv", None)
        return self._table_viewport_width(tv, fallback=float(COL1_W))

    def _table_viewport_width(self, tv, *, fallback: float) -> float:
        """Best-effort visible width of a table's clip view."""
        if tv is None:
            return fallback
        try:
            scroll = tv.enclosingScrollView()
            if scroll is not None:
                w = float(scroll.contentView().bounds().size.width)
                if w > 0:
                    return w
        except Exception:
            pass
        try:
            w = float(tv.frame().size.width)
            if w > 0:
                return w
        except Exception:
            pass
        return fallback

    def _sf_symbol(self, name: str):
        from AppKit import NSImage  # type: ignore[import]
        try:
            if hasattr(NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
                return NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    name, None)
        except Exception:
            pass
        return None

    def _style_debug_row_cell(self, cell) -> None:
        """Debug visual aid: show explicit row bounds."""
        try:
            cell.setWantsLayer_(True)
            layer = cell.layer()
            layer.setBackgroundColor_(_hex_color("ff0000", 0.18).CGColor())
            layer.setBorderWidth_(1.0)
            layer.setBorderColor_(_hex_color("ff4d4d", 0.95).CGColor())
            layer.setCornerRadius_(4.0)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Add buttons (column 2 bottom)
    # ------------------------------------------------------------------ #

    def _rebuild_add_buttons(self) -> None:
        from Cocoa import NSMakeRect, NSView  # type: ignore[import]
        # Tear down the previous bar + its handlers. Button targets live in
        # _addbar_handlers, NOT _handlers: the col-3 placeholder render trims
        # _handlers back to _permanent_handler_count and NSButton does not
        # retain its target, so a shared list would free live targets.
        if self._addbar is not None:
            try:
                self._addbar.removeFromSuperview()
            except Exception:
                pass
            self._addbar = None
        self._addbar_handlers.clear()
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
            btn = self._styled_neutral_button(
                label, NSMakeRect(x, 6, bw, 28))
            handler = _get_action_handler_cls().alloc().initWithCallback_(
                lambda _s, k=kind: self._on_add_clicked(k))
            self._addbar_handlers.append(handler)
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
        self.enter_create_mode(kind)

    # ------------------------------------------------------------------ #
    # Create mode
    # ------------------------------------------------------------------ #

    def enter_create_mode(self, kind: str) -> None:
        """Show the inline create form for `kind` in column 3. Clears the col-2
        selection (under suppression) so no edit form competes with it. A dirty
        create form is guarded by the discard confirm exactly like an edit.

        kind in ("connection", "domain", "forward", "share", "fetch"). Fetch is
        not a persistent item but reuses the create-form machinery (Fetch button
        + Cancel)."""
        if self._dirty and not self._confirm_discard():
            return
        # Capture the highlighted row's conn tag BEFORE clearing the selection
        # so domain/forward create can preselect it.
        conn_tags = [c.tag for c in self._cfg.connections] if self._cfg else []
        preselect = self._preselected_conn_tag(conn_tags)
        self._dirty = False
        self._dirty_identity = None
        self._create_kind = kind
        self.selected_identity = None
        # Deselect any highlighted col-2 row without firing the selection cb.
        self._suppress_selection_cb = True
        try:
            from Foundation import NSIndexSet  # type: ignore[import]
            self._list_tv.selectRowIndexes_byExtendingSelection_(
                NSIndexSet.indexSet(), False)
        except Exception:
            pass
        finally:
            self._suppress_selection_cb = False
        spec = self._build_create_spec(kind, preselect)
        if spec is None:
            self._create_kind = None
            self._render_col3_placeholder("Cannot create here.")
            return
        self._detail_spec = spec
        self._render_detail(spec)

    def exit_create_mode(self) -> None:
        """Leave create mode and restore normal selection rendering."""
        self._create_kind = None
        self._dirty = False
        self._dirty_identity = None
        self._render_selection_placeholder()

    def _build_create_spec(self, kind: str, preselect: str):
        conn_tags = [c.tag for c in self._cfg.connections] if self._cfg else []
        if kind == "connection":
            try:
                from susops.tray.base import get_ssh_hosts
                ssh_hosts = get_ssh_hosts()
            except Exception:
                ssh_hosts = []
            return build_connection_form(ssh_hosts)
        # Domain/forward/share/fetch need a connection.
        if not conn_tags:
            return None
        if kind == "domain":
            return build_domain_form(conn_tags, conn_tag=preselect)
        if kind == "forward":
            return build_forward_form(conn_tags, conn_tag=preselect)
        if kind == "share":
            return build_share_form(conn_tags, conn_tag=preselect)
        if kind == "fetch":
            return build_fetch_form(conn_tags)
        return None

    def _preselected_conn_tag(self, conn_tags):
        """The conn tag of the currently selected col-2 row, if it carries one,
        else the first connection tag (or "" when none)."""
        ident = self.selected_identity
        if ident and len(ident) >= 2 and ident[0] in ("domain", "forward",
                                                       "connection"):
            tag = ident[1]
            if tag in conn_tags:
                return tag
        return conn_tags[0] if conn_tags else ""

    # ------------------------------------------------------------------ #
    # Column 3 - detail / editor
    # ------------------------------------------------------------------ #

    def _render_selection_placeholder(self) -> None:
        # Fresh render of column 3 - any prior dirty form is being replaced, so
        # clear the dirty flag (callers gate this behind the discard confirm).
        # This is the normal selection/detail path, never a create form.
        self._dirty = False
        self._dirty_identity = None
        self._create_kind = None
        if self.category == "settings":
            self._render_settings_pane()
            return
        if self.selected_identity is None:
            self._detail_spec = None
            self._render_col3_placeholder("Select an item.")
            return
        spec = self._build_detail_spec(self.selected_identity)
        if spec is None:
            self._detail_spec = None
            self._render_col3_placeholder("Item no longer exists.")
            return
        self._detail_spec = spec
        self._render_detail(spec)

    # ------------------------------------------------------------------ #
    # Detail spec routing (identity -> builder)
    # ------------------------------------------------------------------ #

    def _conn_by_tag(self, tag):
        if self._cfg is None:
            return None
        return next((c for c in self._cfg.connections if c.tag == tag), None)

    def _build_detail_spec(self, identity: tuple):
        """Route an identity tuple to its DetailSpec builder. Returns None when
        the referenced item has vanished from config."""
        if not identity:
            return None
        kind = identity[0]
        conn_tags = [c.tag for c in self._cfg.connections] if self._cfg else []
        if kind == "connection":
            conn = self._conn_by_tag(identity[1])
            if conn is None:
                return None
            st = self._status_for(identity[1])
            try:
                from susops.tray.base import get_ssh_hosts
                ssh_hosts = get_ssh_hosts()
            except Exception:
                ssh_hosts = []
            return build_connection_detail(conn, st, ssh_hosts)
        if kind == "domain":
            _, conn_tag, host = identity
            conn = self._conn_by_tag(conn_tag)
            if conn is None:
                return None
            all_hosts = list(conn.pac_hosts) + list(
                getattr(conn, "pac_hosts_disabled", []) or [])
            if host not in all_hosts:
                return None
            st = self._status_for(conn_tag)
            return build_domain_form(conn_tags, conn_tag=conn_tag, host=host,
                                     status=st, conn=conn)
        if kind == "forward":
            _, conn_tag, direction, src_port = identity
            conn = self._conn_by_tag(conn_tag)
            if conn is None:
                return None
            fws = conn.forwards.local if direction == "local" \
                else conn.forwards.remote
            fw = next((f for f in fws if f.src_port == src_port), None)
            if fw is None:
                return None
            return build_forward_form(conn_tags, fw=fw, direction=direction,
                                      conn_tag=conn_tag, statuses=self._statuses)
        if kind == "share":
            port = identity[1]
            info = next((s for s in self._shares if s.port == port), None)
            if info is None:
                return None
            st = self._status_for(getattr(info, "conn_tag", None))
            return build_share_detail(info, st, conn_tags)
        return None

    def _status_for(self, tag):
        return next((s for s in self._statuses
                     if getattr(s, "tag", None) == tag), None)

    # ------------------------------------------------------------------ #
    # Detail renderer
    # ------------------------------------------------------------------ #

    def _clear_col3(self) -> None:
        """Remove all col-3 subviews + per-render handlers + field-widget
        caches. Shared by every col-3 render path. Text-change tracking uses
        the delegate protocol and the delegates live in _handlers, so the trim
        above releases them with everything else."""
        for v in list(self._col3.subviews()):
            v.removeFromSuperview()
        del self._handlers[self._permanent_handler_count:]
        self._field_widgets = {}
        self._field_kinds = {}
        self._secure_pair = {}
        self._save_button = None
        self._header_toggle_label = None
        self._settings_widgets = {}
        self._settings_kinds = {}

    def _content_column(self):
        """Create the content column: a fixed CONTENT_MAX_W view anchored
        top-left with CONTENT_PAD. No horizontal autoresize bits are set, so
        width and left edge stay fixed and the column never stretches to the
        window edge on resize. Returns (container, content_w)."""
        from Cocoa import NSColor, NSMakeRect, NSView  # type: ignore[import]
        w = self._col3.frame().size.width
        h = self._col3.frame().size.height
        cw = min(CONTENT_MAX_W, max(0, w - 2 * CONTENT_PAD))
        container = NSView.alloc().initWithFrame_(
            NSMakeRect(CONTENT_PAD, 0, cw, h))
        try:
            container.setWantsLayer_(True)
            container.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
        except Exception:
            pass
        container.setAutoresizingMask_(16 | 32)  # HeightSizable | MaxYMargin
        self._col3.addSubview_(container)
        return container, cw

    def _render_detail(self, spec) -> None:
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import (  # type: ignore[import]
            NSColor,
            NSMakeRect,
            NSTextField,
            NSView,
        )
        self._clear_col3()

        container, cw = self._content_column()
        self._content_view = container
        h = container.frame().size.height
        # Align col-3 title top with col-2 search/top list origin.
        header_top = h - TOP_INSET

        # --- Header: title (left) + status line + Enabled toggle (right edge of
        #     the 540px content column, near the title) ---
        title = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, header_top - 24, cw - 130, 24))
        title.setStringValue_(spec.title)
        title.setFont_(NSFont.boldSystemFontOfSize_(16))
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setTextColor_(NSColor.labelColor())
        container.addSubview_(title)

        # Status line: colored dot + status text.
        status_y = header_top - 44
        if spec.status_dot:
            dot = NSView.alloc().initWithFrame_(
                NSMakeRect(0, status_y + 3, 10, 10))
            try:
                dot.setWantsLayer_(True)
                dot.layer().setBackgroundColor_(
                    _ns_dot_color(spec.status_dot).CGColor())
                dot.layer().setCornerRadius_(5.0)
            except Exception:
                pass
            container.addSubview_(dot)
        if spec.status_text:
            stext = NSTextField.alloc().initWithFrame_(
                NSMakeRect(16, status_y, cw - 16, 16))
            stext.setStringValue_(spec.status_text)
            stext.setFont_(NSFont.systemFontOfSize_(11))
            stext.setBezeled_(False)
            stext.setDrawsBackground_(False)
            stext.setEditable_(False)
            stext.setTextColor_(NSColor.secondaryLabelColor())
            container.addSubview_(stext)

        # Enabled toggle anchored to the RIGHT EDGE of the content column.
        if spec.toggle is not None:
            self._render_header_toggle(spec, container, cw, header_top)

        # --- Card with the field grid (editable controls when spec.editable) ---
        card_top = status_y - 16
        card = self._render_field_card(spec, container, cw, card_top)

        # --- Action row below the card ---
        card_bottom = card.frame().origin.y
        self._render_action_row(spec, container, cw, card_bottom - 14)

    def _render_header_toggle(self, spec, container, cw, header_top) -> None:
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import (  # type: ignore[import]
            NSColor,
            NSMakeRect,
            NSTextField,
        )
        # The model toggle tuple carries a generic role label; the displayed
        # word reflects the boolean: "Enabled" when on, "Disabled" when off.
        _role, value, action_id = spec.toggle
        toggle_w = 40
        toggle_x = cw - toggle_w
        toggle_y = header_top - 24

        def _on_flip(sender, aid=action_id):
            # Optimistically flip the label word so it switches immediately
            # without waiting for the poll re-render.
            try:
                on = bool(sender.state())
            except Exception:
                on = not value
            self._update_header_toggle_label(on)
            self._dispatch(aid)

        handler = _get_action_handler_cls().alloc().initWithCallback_(_on_flip)
        self._handlers.append(handler)
        try:
            import AppKit  # type: ignore[import]
            has_switch = hasattr(AppKit, "NSSwitch")
        except Exception:
            has_switch = False
        if has_switch:
            from AppKit import NSSwitch  # type: ignore[import]
            sw = NSSwitch.alloc().initWithFrame_(
                NSMakeRect(toggle_x, toggle_y, toggle_w, 22))
            sw.setState_(1 if value else 0)
            sw.setTarget_(handler)
            sw.setAction_("fire:")
            container.addSubview_(sw)
            ctrl_left = toggle_x
        else:
            from AppKit import NSButton  # type: ignore[import]
            btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(toggle_x, toggle_y, toggle_w, 22))
            try:
                btn.setButtonType_(3)  # NSButtonTypeSwitch
            except Exception:
                pass
            btn.setTitle_("")
            btn.setState_(1 if value else 0)
            btn.setTarget_(handler)
            btn.setAction_("fire:")
            container.addSubview_(btn)
            ctrl_left = toggle_x
        # "Enabled"/"Disabled" label to the LEFT of the control, reflecting the
        # toggle state. Cached so a flip can update the word optimistically.
        lbl = NSTextField.alloc().initWithFrame_(
            NSMakeRect(ctrl_left - 70, toggle_y + 3, 62, 16))
        lbl.setStringValue_("Enabled" if value else "Disabled")
        lbl.setFont_(NSFont.systemFontOfSize_(11))
        lbl.setAlignment_(1)  # right
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setTextColor_(NSColor.secondaryLabelColor())
        container.addSubview_(lbl)
        self._header_toggle_label = lbl
        # toggle_note: tertiary line right-aligned under the toggle, within the
        # content column.
        if spec.toggle_note:
            note = NSTextField.alloc().initWithFrame_(
                NSMakeRect(cw - 360, toggle_y - 18, 360, 14))
            note.setStringValue_(spec.toggle_note)
            note.setFont_(NSFont.systemFontOfSize_(11))
            note.setAlignment_(1)  # right
            note.setBezeled_(False)
            note.setDrawsBackground_(False)
            note.setEditable_(False)
            note.setTextColor_(NSColor.tertiaryLabelColor())
            container.addSubview_(note)

    def _update_header_toggle_label(self, on: bool) -> None:
        """Set the header toggle word to reflect its boolean state."""
        lbl = getattr(self, "_header_toggle_label", None)
        if lbl is None:
            return
        try:
            lbl.setStringValue_("Enabled" if on else "Disabled")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Field-row planning: pair src_addr/src_port and dst_addr/dst_port
    # ------------------------------------------------------------------ #

    def _plan_field_rows(self, fields):
        """Group FormFields into render rows. src_addr+src_port collapse to one
        "Source" row, dst_addr+dst_port to one "Destination" row. Every other
        field is its own single-control row. Returns a list of dicts:
          {"label": str, "fields": [FormField, ...]}
        preserving spec order. The MODEL still keeps 4 separate FormFields."""
        by_key = {f.key: f for f in fields}
        rows = []
        consumed = set()
        for f in fields:
            if f.key in consumed:
                continue
            if f.key == "src_addr" and "src_port" in by_key:
                rows.append({"label": "Source",
                             "fields": [f, by_key["src_port"]]})
                consumed.add("src_port")
            elif f.key == "dst_addr" and "dst_port" in by_key:
                rows.append({"label": "Destination",
                             "fields": [f, by_key["dst_port"]]})
                consumed.add("dst_port")
            else:
                rows.append({"label": f.label, "fields": [f]})
        return rows

    def _render_field_card(self, spec, container, cw, card_top):
        """Layer-backed rounded card holding the field grid. Renders editable
        controls when spec.editable, static text otherwise. Returns the card
        NSView so the caller can position the action row under it."""
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import (  # type: ignore[import]
            NSColor,
            NSMakeRect,
            NSTextField,
            NSView,
        )
        plan = self._plan_field_rows(list(spec.fields))
        row_h = CARD_ROW_H
        v_pad = CARD_PAD_Y
        card_h = max(row_h, len(plan) * row_h) + 2 * v_pad
        card_w = cw
        card_y = card_top - card_h
        card = NSView.alloc().initWithFrame_(
            NSMakeRect(0, card_y, card_w, card_h))
        try:
            card.setWantsLayer_(True)
            card.layer().setBackgroundColor_(_hex_color(PALETTE["card"]).CGColor())
            card.layer().setCornerRadius_(9.0)
        except Exception:
            pass
        container.addSubview_(card)

        # Right-aligned labels in a ~110px column, 10px gap, controls right
        # after so labels hug their controls. Left AND right insets are
        # CARD_PAD_X so no control touches a card edge.
        label_w = 110
        inner_pad = CARD_PAD_X
        gap = 10
        value_x = inner_pad + label_w + gap
        value_w = card_w - value_x - inner_pad
        lbl_h = 16
        for i, row in enumerate(plan):
            # Rows top-down inside the card. ry is the control-row baseline that
            # the render helpers offset from (controls nudge -3 for their
            # bezel). The label is vertically centered on the control on its row.
            ry = card_h - v_pad - (i + 1) * row_h + 5
            is_check_pair = any(f.kind == "check_pair" for f in row["fields"])
            if spec.editable and is_check_pair:
                # Checkboxes are drawn at ry-2, height 20 (center ry+8) with
                # their own labels already vertically centered in the box.
                # Align the row label to that center.
                lbl_y = (ry - 2) + 20 / 2.0 - lbl_h / 2.0 + CARD_LABEL_NUDGE
            elif spec.editable and self._row_has_tall_control(row):
                # Center the label on a 22-tall bezeled control band (drawn at
                # ry-3..ry+19) and nudge for the field's inset text.
                ctrl_center = (ry - 3) + CARD_CTRL_H / 2.0
                lbl_y = ctrl_center - lbl_h / 2.0 + CARD_LABEL_NUDGE
            else:
                # Static value: the value text sits at ry (18 tall, top-drawn).
                # Match the label's text line to it.
                lbl_y = ry + 1
            lbl = NSTextField.alloc().initWithFrame_(
                NSMakeRect(inner_pad, lbl_y, label_w, lbl_h))
            lbl.setStringValue_(row["label"])
            lbl.setFont_(NSFont.systemFontOfSize_(11))
            lbl.setAlignment_(1)  # right
            lbl.setBezeled_(False)
            lbl.setDrawsBackground_(False)
            lbl.setEditable_(False)
            lbl.setTextColor_(NSColor.secondaryLabelColor())
            card.addSubview_(lbl)

            if spec.editable:
                self._render_row_controls(card, row, value_x, ry, value_w)
            else:
                self._render_row_static(card, row, value_x, ry, value_w)
        return card

    def _row_has_tall_control(self, row) -> bool:
        """True when the row renders a bezeled control (text/secure/combo/popup/
        path) the label must vertically center against. check_pair (checkboxes)
        and static rows align to a plain text line instead."""
        kinds = {f.kind for f in row["fields"]}
        return bool(kinds & {"text", "secure", "combo", "popup", "path"})

    def _render_row_static(self, card, row, value_x, ry, value_w) -> None:
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import NSColor, NSMakeRect, NSTextField  # type: ignore[import]
        text = " ".join(self._static_value_text(f) for f in row["fields"])
        val = NSTextField.alloc().initWithFrame_(
            NSMakeRect(value_x, ry, value_w, 18))
        val.setStringValue_(text)
        val.setFont_(NSFont.systemFontOfSize_(13))
        val.setBezeled_(False)
        val.setDrawsBackground_(False)
        val.setEditable_(False)
        val.setSelectable_(True)
        val.setTextColor_(NSColor.labelColor())
        card.addSubview_(val)

    def _render_row_controls(self, card, row, value_x, ry, value_w) -> None:
        """Render one editable row. Paired Source/Destination rows place an
        addr field (~190) + port field (~110) side by side. Single-field rows
        fill the row width."""
        fields = row["fields"]
        if len(fields) == 2:
            # addr + port side by side.
            addr_f, port_f = fields
            addr_w = 190
            port_w = 110
            self._make_control(card, addr_f, value_x, ry, addr_w)
            self._make_control(card, port_f, value_x + addr_w + 10, ry, port_w)
        else:
            self._make_control(card, fields[0], value_x, ry, value_w)

    def _make_control(self, card, f, x, ry, width) -> None:
        """Instantiate the editable control for FormField f, cache it under its
        key, and wire dirty tracking. Field height/baseline aligns to ry.

        Trailing affordances reduce the control width: path fields get a
        Choose… button (file picker); fields with f.trailing get Copy/Reveal
        buttons (share URL/password)."""
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import (  # type: ignore[import]
            NSColor,
            NSComboBox,
            NSMakeRect,
            NSPopUpButton,
            NSSecureTextField,
            NSTextField,
        )
        kind = f.kind
        self._field_kinds[f.key] = kind
        if kind == "path":
            # Editable-looking text showing the chosen path (set via Choose… or
            # the debug set-field). Cached so collect_form_values reads it.
            choose_w = 84
            ctrl_w = max(60, width - choose_w - 8)
            tf = NSTextField.alloc().initWithFrame_(
                NSMakeRect(x, ry - 3, ctrl_w, 22))
            tf.setStringValue_(str(f.value or ""))
            tf.setFont_(NSFont.systemFontOfSize_(13))
            tf.setBezeled_(False)
            tf.setDrawsBackground_(False)
            tf.setEditable_(True)
            tf.setSelectable_(True)
            tf.setTextColor_(_hex_color(PALETTE["input_text"]))
            if f.placeholder:
                try:
                    tf.cell().setPlaceholderString_(f.placeholder)
                except Exception:
                    pass
            _apply_vcenter_cell(tf, _get_vcenter_text_cell_cls())
            self._style_input_field(tf)
            self._wire_text_dirty(tf)
            card.addSubview_(tf)
            self._field_widgets[f.key] = tf
            self._render_choose_button(card, f, x + ctrl_w + 8, ry - 3,
                                       choose_w)
            return
        # Reserve space at the right for any trailing buttons (Copy/Reveal),
        # then render the control in the remaining width.
        ctrl_w = self._reserve_trailing(card, f, x, ry, width)
        if kind == "static":
            # Static read-only text (e.g. share file/url/downloads).
            text = self._static_value_text(f)
            val = NSTextField.alloc().initWithFrame_(
                NSMakeRect(x, ry, ctrl_w, 18))
            val.setStringValue_(text)
            val.setFont_(NSFont.systemFontOfSize_(13))
            val.setBezeled_(False)
            val.setDrawsBackground_(False)
            val.setEditable_(False)
            val.setSelectable_(True)
            val.setTextColor_(NSColor.labelColor())
            card.addSubview_(val)
            self._field_widgets[f.key] = val
            return
        if kind == "check_pair":
            self._make_check_pair(card, f, x, ry, width)
            return
        cy = ry - 3  # nudge taller bezeled controls to align baselines
        if kind == "popup":
            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(x, cy, ctrl_w, 24), False)
            for opt in f.options:
                popup.addItemWithTitle_(str(opt))
            if f.value:
                popup.selectItemWithTitle_(str(f.value))
            handler = _get_action_handler_cls().alloc().initWithCallback_(
                lambda _s: self._mark_dirty())
            self._handlers.append(handler)
            popup.setTarget_(handler)
            popup.setAction_("fire:")
            card.addSubview_(popup)
            self._field_widgets[f.key] = popup
            return
        if kind == "combo":
            combo = NSComboBox.alloc().initWithFrame_(
                NSMakeRect(x, cy, ctrl_w, 24))
            for opt in f.options:
                combo.addItemWithObjectValue_(str(opt))
            combo.setStringValue_(str(f.value or ""))
            self._wire_text_dirty(combo)
            # NSComboBox selection changes do not always emit text-change
            # notifications, so wire action events too.
            handler = _get_action_handler_cls().alloc().initWithCallback_(
                lambda _s: self._mark_dirty())
            self._handlers.append(handler)
            combo.setTarget_(handler)
            combo.setAction_("fire:")
            card.addSubview_(combo)
            self._field_widgets[f.key] = combo
            return
        if kind == "secure":
            self._make_secure_control(card, f, x, cy, ctrl_w)
            return
        # text
        tf = NSTextField.alloc().initWithFrame_(NSMakeRect(x, cy, ctrl_w, 22))
        tf.setStringValue_(str(f.value or ""))
        tf.setFont_(NSFont.systemFontOfSize_(13))
        tf.setBezeled_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(True)
        tf.setSelectable_(True)
        tf.setTextColor_(_hex_color(PALETTE["input_text"]))
        if f.placeholder:
            try:
                tf.cell().setPlaceholderString_(f.placeholder)
            except Exception:
                pass
        _apply_vcenter_cell(tf, _get_vcenter_text_cell_cls())
        self._style_input_field(tf)
        self._wire_text_dirty(tf)
        card.addSubview_(tf)
        self._field_widgets[f.key] = tf

    def _make_secure_control(self, card, f, x, cy, ctrl_w) -> None:
        """A secure password control with a Reveal toggle: two stacked fields
        (NSSecureTextField masked + NSTextField plain) sharing one frame, one
        hidden. Reveal swaps which is visible (syncing the value first). The
        VISIBLE field is cached under f.key; collect_form_values reads whichever
        is showing."""
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import (  # type: ignore[import]
            NSMakeRect,
            NSSecureTextField,
            NSTextField,
        )
        frame = NSMakeRect(x, cy, ctrl_w, 22)
        secure = NSSecureTextField.alloc().initWithFrame_(frame)
        plain = NSTextField.alloc().initWithFrame_(frame)
        for tf in (secure, plain):
            tf.setStringValue_(str(f.value or ""))
            tf.setFont_(NSFont.systemFontOfSize_(13))
            tf.setBezeled_(False)
            tf.setDrawsBackground_(False)
            tf.setEditable_(True)
            tf.setSelectable_(True)
            tf.setTextColor_(_hex_color(PALETTE["input_text"]))
            if f.placeholder:
                try:
                    tf.cell().setPlaceholderString_(f.placeholder)
                except Exception:
                    pass
            self._style_input_field(tf)
            self._wire_text_dirty(tf)
            card.addSubview_(tf)
        plain.setHidden_(True)
        # The visible field is the one collect_form_values / set_field read.
        self._field_widgets[f.key] = secure
        self._secure_pair[f.key] = (secure, plain)

    def _reserve_trailing(self, card, f, x, ry, width) -> float:
        """Render f.trailing buttons right-aligned within [x, x+width] and
        return the control width remaining on the left. share.reveal is a local
        toggle; every other trailing id dispatches via the tray. Empty trailing
        returns the full width."""
        from Cocoa import NSMakeRect  # type: ignore[import]
        trailing = list(getattr(f, "trailing", ()) or ())
        if not trailing:
            return width
        gap = 6
        btn_h = 22
        # Vertically center the button on the field it sits beside. Static
        # (URL) text renders as an 18px field at ry, so its center is ry+9;
        # editable controls (the secure password) render at ry-3 with height
        # 22, center ry+8. Align the button center to the field's center.
        if f.kind == "static":
            field_center = ry + 9
        else:
            field_center = ry - 3 + 11
        btn_y = field_center - btn_h / 2
        # Lay buttons out from the right edge leftward, preserving spec order
        # left-to-right.
        widths = [max(48, 18 + 8 * len(label)) for _aid, label in trailing]
        total = sum(widths) + gap * (len(widths) - 1)
        rx = x + width - total
        ctrl_w = max(60, rx - x - 8)
        bx = rx
        for (aid, label), bw in zip(trailing, widths):
            btn = self._styled_neutral_button(
                label, NSMakeRect(bx, btn_y, bw, btn_h))
            if aid == "share.reveal":
                handler = _get_action_handler_cls().alloc().initWithCallback_(
                    lambda _s, key=f.key, b=btn: self._on_reveal_password(key, b))
            else:
                handler = _get_action_handler_cls().alloc().initWithCallback_(
                    lambda _s, a=aid: self._dispatch(a))
            self._handlers.append(handler)
            btn.setTarget_(handler)
            btn.setAction_("fire:")
            card.addSubview_(btn)
            bx += bw + gap
        return ctrl_w

    def _render_choose_button(self, card, f, x, ry, width) -> None:
        """A Choose… button beside a path field; opens a file/save panel via the
        tray and writes the result into the path field, marking dirty."""
        from Cocoa import NSMakeRect  # type: ignore[import]
        btn = self._styled_neutral_button(
            "Choose…", NSMakeRect(x, ry, width, 22))
        handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda _s, key=f.key: self._on_choose_path(key))
        self._handlers.append(handler)
        btn.setTarget_(handler)
        btn.setAction_("fire:")
        card.addSubview_(btn)

    def _on_choose_path(self, key: str) -> None:
        """Open the file picker (save panel for the fetch output) on the main
        thread, then write the chosen path into the field + mark dirty."""
        save = key == "output"
        try:
            path = self.tray.pick_path_for_window(save=save)
        except Exception:
            path = None
        if not path:
            return
        w = self._field_widgets.get(key)
        if w is not None:
            try:
                w.setStringValue_(str(path))
            except Exception:
                pass
            self._mark_dirty()

    def _on_reveal_password(self, key: str, btn) -> None:
        """Toggle the masked/plain password fields. Syncs the value from the
        currently-visible field to the other, then swaps visibility and the
        cached widget so collect_form_values reads the shown field."""
        pair = self._secure_pair.get(key)
        if pair is None:
            return
        secure, plain = pair
        showing_plain = bool(plain.isHidden()) is False
        try:
            if showing_plain:
                secure.setStringValue_(str(plain.stringValue() or ""))
                plain.setHidden_(True)
                secure.setHidden_(False)
                self._field_widgets[key] = secure
                btn.setAttributedTitle_(
                    self._attr_title("Reveal", _hex_color(PALETTE["input_text"])))
            else:
                plain.setStringValue_(str(secure.stringValue() or ""))
                secure.setHidden_(True)
                plain.setHidden_(False)
                self._field_widgets[key] = plain
                btn.setAttributedTitle_(
                    self._attr_title("Hide", _hex_color(PALETTE["input_text"])))
        except Exception:
            pass

    def _style_input_field(self, tf) -> None:
        """Layer-style an editable text/secure field to the mockup palette: fill
        #2a2b31, 1px #3f4147 border, radius 6. ~4px text inset comes from a
        slightly larger frame; the field editor inherits the dark appearance."""
        try:
            tf.setWantsLayer_(True)
            layer = tf.layer()
            layer.setBackgroundColor_(_hex_color(PALETTE["input_fill"]).CGColor())
            layer.setCornerRadius_(6.0)
            layer.setBorderWidth_(1.0)
            layer.setBorderColor_(_hex_color(PALETTE["input_border"]).CGColor())
        except Exception:
            pass

    def _make_check_pair(self, card, f, x, ry, width) -> None:
        """TCP + UDP checkboxes side by side."""
        from Cocoa import NSButton, NSMakeRect  # type: ignore[import]
        tcp, udp = (f.value if isinstance(f.value, (tuple, list))
                    else (False, False))
        handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda _s: self._mark_dirty())
        self._handlers.append(handler)
        tcp_btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, ry - 2, 70, 20))
        udp_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(x + 78, ry - 2, 70, 20))
        for btn, lbl, on in ((tcp_btn, "TCP", tcp), (udp_btn, "UDP", udp)):
            try:
                btn.setButtonType_(3)  # NSButtonTypeSwitch
            except Exception:
                pass
            btn.setTitle_(lbl)
            btn.setState_(1 if on else 0)
            btn.setTarget_(handler)
            btn.setAction_("fire:")
            card.addSubview_(btn)
        # Cache both under synthetic keys so the value collector can read them.
        self._field_widgets["protocols.tcp"] = tcp_btn
        self._field_widgets["protocols.udp"] = udp_btn
        self._field_kinds["protocols"] = "check_pair"

    def _wire_text_dirty(self, control) -> None:
        """Attach a controlTextDidChange_ delegate that flags dirty. The
        delegate instance lives in _handlers and is released by the col-3
        trim like every other per-render target."""
        delegate = _get_text_delegate_cls().alloc().initWithCallback_(
            self._mark_dirty)
        self._handlers.append(delegate)
        try:
            control.setDelegate_(delegate)
        except Exception:
            pass

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._dirty_identity = self.selected_identity
        if self._save_button is not None:
            try:
                self._save_button.setEnabled_(True)
                self._restyle_save_button(self._save_button, True)
            except Exception:
                pass

    def _static_value_text(self, f) -> str:
        """Render any FormField as a static string (read-only rows)."""
        kind = f.kind
        if kind == "secure":
            return "••••••••"
        if kind == "check_pair":
            tcp, udp = (f.value if isinstance(f.value, (tuple, list))
                        else (False, False))
            if tcp and udp:
                return "TCP + UDP"
            if tcp:
                return "TCP"
            if udp:
                return "UDP"
            return "—"
        return str(f.value) if f.value not in (None, "") else "—"

    def _attr_title(self, title: str, color):
        """Centered attributed title in `color` for a borderless layer button."""
        from AppKit import (  # type: ignore[import]
            NSCenterTextAlignment,
            NSFont,
            NSMutableParagraphStyle,
        )
        from Cocoa import (  # type: ignore[import]
            NSAttributedString,
            NSFontAttributeName,
            NSForegroundColorAttributeName,
            NSParagraphStyleAttributeName,
        )
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(NSCenterTextAlignment)
        attrs = {
            NSForegroundColorAttributeName: color,
            NSFontAttributeName: NSFont.systemFontOfSize_(13),
            NSParagraphStyleAttributeName: para,
        }
        return NSAttributedString.alloc().initWithString_attributes_(title, attrs)

    def _base_layer_button(self, title, frame):
        from Cocoa import NSButton  # type: ignore[import]
        btn = NSButton.alloc().initWithFrame_(frame)
        btn.setBordered_(False)
        btn.setTitle_(title)
        try:
            btn.setWantsLayer_(True)
            btn.layer().setCornerRadius_(6.0)
        except Exception:
            pass
        return btn

    def _styled_save_button(self, title, frame):
        """Filled accent-blue Save. _restyle_save_button owns all fill + title
        styling (called on creation and on dirty flips). Enter-to-save via the
        key equivalent, safe here since the borderless button has no bezel for
        AppKit to repaint."""
        btn = self._base_layer_button(title, frame)
        try:
            btn.setKeyEquivalent_("\r")
        except Exception:
            pass
        return btn

    def _restyle_save_button(self, btn, enabled: bool) -> None:
        from Cocoa import NSColor  # type: ignore[import]
        try:
            accent = NSColor.controlAccentColor()
        except Exception:
            accent = _hex_color("0a84ff")
        try:
            if enabled:
                btn.layer().setBackgroundColor_(accent.CGColor())
                btn.setAttributedTitle_(
                    self._attr_title(btn.title(), NSColor.whiteColor()))
            else:
                btn.layer().setBackgroundColor_(
                    accent.colorWithAlphaComponent_(0.40).CGColor())
                btn.setAttributedTitle_(self._attr_title(
                    btn.title(), NSColor.whiteColor().colorWithAlphaComponent_(0.65)))
        except Exception:
            pass

    def _styled_neutral_button(self, title, frame):
        """Dark filled rounded (Test / add buttons): fill #2a2b31, 1px border
        #3f4147, title #e8e9ed."""
        btn = self._base_layer_button(title, frame)
        try:
            btn.layer().setBackgroundColor_(_hex_color(PALETTE["input_fill"]).CGColor())
            btn.layer().setBorderWidth_(1.0)
            btn.layer().setBorderColor_(_hex_color(PALETTE["input_border"]).CGColor())
        except Exception:
            pass
        btn.setAttributedTitle_(
            self._attr_title(title, _hex_color(PALETTE["input_text"])))
        return btn

    def _styled_destructive_button(self, title, frame):
        """Transparent fill, 1px red border at 0.45 alpha, red title."""
        from Cocoa import NSColor  # type: ignore[import]
        btn = self._base_layer_button(title, frame)
        red = NSColor.systemRedColor()
        try:
            btn.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
            btn.layer().setBorderWidth_(1.0)
            btn.layer().setBorderColor_(red.colorWithAlphaComponent_(0.45).CGColor())
        except Exception:
            pass
        btn.setAttributedTitle_(self._attr_title(title, red))
        return btn

    def _render_action_row(self, spec, container, cw, top_y) -> None:
        from Cocoa import NSMakeRect  # type: ignore[import]
        btn_h = 30
        row_y = top_y - btn_h

        # Create mode: a primary Create/Fetch button (styled like Save, but
        # ALWAYS enabled) + a neutral Cancel to its right, both right-aligned.
        # No destructive button. Fetch reuses the same row (its action is
        # fetch.run, not *.create).
        if self._create_kind is not None:
            primary = next((a for a in spec.actions
                            if a.action_id.endswith(".create")
                            or a.action_id == "fetch.run"), None)
            if primary is not None:
                self._render_create_action_row(primary, container, cw, row_y,
                                               btn_h)
                return

        # Save starts disabled until the form is dirty.
        actions = list(spec.actions)

        def _make_button(action):
            title = action.title
            bw = max(80, 28 + 8 * len(title))
            is_save = action.action_id.endswith(".save")
            if is_save:
                btn = self._styled_save_button(title, NSMakeRect(0, row_y, bw, btn_h))
                btn.setEnabled_(bool(self._dirty))
                self._save_button = btn
                self._restyle_save_button(btn, bool(self._dirty))
            elif action.destructive:
                btn = self._styled_destructive_button(
                    title, NSMakeRect(0, row_y, bw, btn_h))
                btn.setEnabled_(bool(action.enabled))
            else:
                btn = self._styled_neutral_button(
                    title, NSMakeRect(0, row_y, bw, btn_h))
                btn.setEnabled_(bool(action.enabled))
            handler = _get_action_handler_cls().alloc().initWithCallback_(
                lambda _s, aid=action.action_id: self._dispatch(aid))
            self._handlers.append(handler)
            btn.setTarget_(handler)
            btn.setAction_("fire:")
            return btn, bw

        # Destructive actions on the LEFT.
        x = 0
        for a in actions:
            if not a.destructive:
                continue
            btn, bw = _make_button(a)
            frame = btn.frame()
            frame.origin.x = x
            btn.setFrame_(frame)
            container.addSubview_(btn)
            x += bw + 8

        # Non-destructive actions right-aligned (within the content column),
        # keeping spec order left-to-right.
        non_destr = [a for a in actions if not a.destructive]
        widths = [max(72, 28 + 8 * len(a.title)) for a in non_destr]
        total = sum(widths) + 8 * (len(non_destr) - 1 if non_destr else 0)
        rx = cw - total
        for a, bw in zip(non_destr, widths):
            btn, _ = _make_button(a)
            frame = btn.frame()
            frame.origin.x = rx
            frame.size.width = bw
            btn.setFrame_(frame)
            container.addSubview_(btn)
            rx += bw + 8

    def _render_create_action_row(self, action, container, cw, row_y,
                                  btn_h) -> None:
        """Create + Cancel buttons, right-aligned. Create is styled like Save
        (accent fill, return key equivalent) but ALWAYS enabled. Cancel exits
        create mode without dispatching."""
        from Cocoa import NSMakeRect  # type: ignore[import]
        create_w = max(80, 28 + 8 * len(action.title))
        cancel_w = max(72, 28 + 8 * len("Cancel"))
        gap = 8
        total = create_w + gap + cancel_w
        rx = cw - total

        cancel = self._styled_neutral_button(
            "Cancel", NSMakeRect(rx, row_y, cancel_w, btn_h))
        cancel_handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda _s: self.exit_create_mode())
        self._handlers.append(cancel_handler)
        cancel.setTarget_(cancel_handler)
        cancel.setAction_("fire:")
        container.addSubview_(cancel)

        create = self._styled_save_button(
            action.title, NSMakeRect(rx + cancel_w + gap, row_y, create_w,
                                     btn_h))
        create.setEnabled_(True)
        self._restyle_save_button(create, True)
        create_handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda _s, aid=action.action_id: self._dispatch_create(aid))
        self._handlers.append(create_handler)
        create.setTarget_(create_handler)
        create.setAction_("fire:")
        container.addSubview_(create)

    def _dispatch_create(self, action_id: str) -> None:
        """Forward a create action with the live form values. Runs on the main
        thread (button handler)."""
        try:
            values = self.collect_form_values()
            self.tray.create_window_item(self._create_kind, values)
        except Exception:
            pass

    def _dispatch(self, action_id: str) -> None:
        """Forward a detail-pane action to the tray dispatch with the current
        identity. Runs on the main thread (button/switch handler)."""
        try:
            self.tray.dispatch_window_action(
                action_id, tuple(self.selected_identity or ()))
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Form-value collection
    # ------------------------------------------------------------------ #

    def _read_widget(self, key: str):
        """Current value of a live col-3 form widget by field key. text/secure/
        combo -> str, popup -> selected title str, protocols -> (tcp, udp)."""
        if key == "protocols":
            tcp = self._field_widgets.get("protocols.tcp")
            udp = self._field_widgets.get("protocols.udp")
            return (
                bool(tcp.state()) if tcp is not None else False,
                bool(udp.state()) if udp is not None else False,
            )
        w = self._field_widgets.get(key)
        if w is None:
            return None
        kind = self._field_kinds.get(key)
        if kind == "popup":
            try:
                return str(w.titleOfSelectedItem() or "")
            except Exception:
                return ""
        if kind == "combo":
            try:
                value = str(w.stringValue() or "")
                if value:
                    return value
            except Exception:
                pass
            try:
                selected = w.objectValueOfSelectedItem()
                return "" if selected is None else str(selected)
            except Exception:
                return ""
        try:
            return str(w.stringValue() or "")
        except Exception:
            return ""

    def collect_form_values(self) -> dict:
        """All live form widget values keyed by field key. protocols collapses
        the two checkboxes to (tcp, udp)."""
        values: dict = {}
        keys = set(self._field_kinds.keys())
        for key in keys:
            values[key] = self._read_widget(key)
        return values

    def _render_col3_placeholder(self, text: str) -> None:
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import NSColor, NSMakeRect, NSTextField  # type: ignore[import]
        self._detail_spec = None
        self._dirty = False
        self._dirty_identity = None
        self._create_kind = None
        self._clear_col3()
        w = self._col3.frame().size.width
        h = self._col3.frame().size.height
        lbl = NSTextField.alloc().initWithFrame_(
            NSMakeRect(CONTENT_PAD, h - TOP_INSET - 80, min(CONTENT_MAX_W, w - 2 * CONTENT_PAD), 80))
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
    # Settings pane (Tailscale-style, Apply-gated). Col 2 is hidden; this pane
    # spans the freed area. ALL changes (toggles/logo/login/ports) stage
    # locally and persist only on the single Apply button; leaving the category
    # or closing the window without Apply discards them (and reverts the logo
    # icon preview). Field spec + validation live in mac.py (self.tray); this
    # only renders, stages, and collects.
    # ------------------------------------------------------------------ #

    def _enter_settings(self) -> None:
        self.category = "settings"
        self.selected_identity = None
        self._dirty = False
        self._dirty_identity = None
        self._create_kind = None
        self._settings_dirty = False
        self._set_col2_visible(False)
        self._render_settings_pane()

    SETTINGS_SECTIONS = ("General", "Menu bar", "Servers", "Config file")
    _SETTINGS_LABEL_W = 100
    _SETTINGS_LABEL_GAP = 14

    def _render_settings_pane(self) -> None:
        """Build the settings grid in column 3. Section labels are bold,
        right-aligned in a left column; rows render to their right. Mirrors the
        approved settings mockup."""
        from AppKit import NSFont, NSTextAlignmentRight  # type: ignore[import]
        from Cocoa import (  # type: ignore[import]
            NSColor,
            NSMakeRect,
            NSScrollView,
            NSTextField,
            NSView,
        )
        self._clear_col3()
        self._detail_spec = None
        self._settings_widgets = {}
        self._settings_kinds = {}

        fields, ctx = self.tray.settings_field_specs()
        self._settings_ctx = ctx
        # Group fields by section in spec order.
        by_section: dict[str, list] = {}
        for f in fields:
            by_section.setdefault(f.get("section", "General:"), []).append(f)

        col3_w = self._col3.frame().size.width
        col3_h = self._col3.frame().size.height
        # Cap the form width and center it horizontally in the (wide) settings
        # area so it reads as an intentional macOS settings layout rather than a
        # narrow form lost in a sea of empty space on the right.
        content_w = min(600, max(360, col3_w - 2 * CONTENT_PAD))
        label_x = 0
        label_w = self._SETTINGS_LABEL_W
        row_x = label_x + label_w + self._SETTINGS_LABEL_GAP
        row_w = content_w - (label_w + self._SETTINGS_LABEL_GAP)

        # Build into a tall document view inside a scroll view so the pane
        # scrolls if the window is short. Lay out top-down against a 4000-tall
        # doc, then trim the doc to the used height and shift subviews to the
        # top (non-flipped doc: top = max-y).
        doc = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, content_w, 4000))
        try:
            doc.setWantsLayer_(True)
            doc.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
        except Exception:
            pass

        # We lay out with a descending y cursor from the top of the doc; the
        # doc height is trimmed at the end and the scroll view scrolled to top.
        top = 4000 - 12
        y = top
        SECTION_GAP = 18
        ROW_GAP = 10

        def _section_label(title, sy):
            lbl = NSTextField.alloc().initWithFrame_(
                NSMakeRect(label_x, sy - 18, label_w, 18))
            lbl.setStringValue_(title)
            lbl.setFont_(NSFont.boldSystemFontOfSize_(12))
            lbl.setAlignment_(NSTextAlignmentRight)
            lbl.setBezeled_(False)
            lbl.setDrawsBackground_(False)
            lbl.setEditable_(False)
            lbl.setTextColor_(NSColor.labelColor())
            doc.addSubview_(lbl)

        for section in self.SETTINGS_SECTIONS:
            section_top = y
            _section_label(section, section_top - 2)
            if section == "Config file":
                y = self._render_config_file_row(doc, row_x, row_w, section_top)
            elif section == "Servers":
                y = self._render_server_rows(
                    doc, by_section.get(section, []), row_x, row_w, section_top)
            else:
                rows = by_section.get(section, [])
                ry = section_top
                for f in rows:
                    ry = self._render_setting_row(doc, f, row_x, row_w, ry)
                    ry -= ROW_GAP
                y = ry
            y -= SECTION_GAP

        used = top - y
        doc_h = used + 24
        # Reposition all subviews so the content sits at the TOP of a doc that
        # is exactly doc_h tall (subviews were placed against a 4000-tall doc).
        shift = doc_h - 4000
        for v in doc.subviews():
            fr = v.frame()
            v.setFrame_(NSMakeRect(fr.origin.x, fr.origin.y + shift,
                                   fr.size.width, fr.size.height))
        doc.setFrame_(NSMakeRect(0, 0, content_w, doc_h))

        # Center the capped-width form horizontally. Flexible left+right margins
        # keep it centered as the window resizes. A small top inset below the
        # transparent titlebar avoids a large gap above "General:".
        scroll_x = max(CONTENT_PAD, (col3_w - content_w) / 2.0)
        # Extra top breathing room so "General:" sits comfortably below the
        # titlebar rather than being clipped against the top edge.
        scroll_top_inset = TOP_INSET + 24
        avail_h = col3_h - scroll_top_inset
        # Pin the form to the TOP: when the content is shorter than the available
        # height, size the scroll to the content and place it at the top edge so
        # the doc does not float at the bottom of an oversized clip view (which
        # left a large empty band above "General:").
        scroll_h = min(avail_h, doc_h)
        scroll_y = (col3_h - scroll_top_inset) - scroll_h
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(scroll_x, scroll_y, content_w, scroll_h))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        # MinXMargin | MaxXMargin | MinYMargin -> fixed size, stays centered
        # horizontally and pinned to the top as the window resizes.
        scroll.setAutoresizingMask_(1 | 4 | 8)
        scroll.setDocumentView_(doc)
        self._col3.addSubview_(scroll)
        # Non-flipped doc: the top of the content sits at high y. Scroll the
        # clip view there so the pane opens at the top, not the bottom.
        try:
            clip = scroll.contentView()
            visible_h = clip.bounds().size.height
            from Foundation import NSMakePoint  # type: ignore[import]
            clip.scrollToPoint_(NSMakePoint(0, max(0, doc_h - visible_h)))
            scroll.reflectScrolledClipView_(clip)
        except Exception:
            pass

    def _render_setting_row(self, doc, f, x, width, top) -> float:
        """One checkbox row + an optional indented gray description. Returns the
        y of the bottom of the row (next row's top)."""
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import (  # type: ignore[import]
            NSButton,
            NSColor,
            NSMakeRect,
            NSTextField,
        )
        key = f["key"]
        kind = f["kind"]
        if kind == "segmented":
            return self._render_logo_row(doc, f, x, width, top)
        # switch -> checkbox.
        cb_y = top - 20
        cb = NSButton.alloc().initWithFrame_(NSMakeRect(x, cb_y, width, 20))
        try:
            cb.setButtonType_(3)  # NSButtonTypeSwitch
        except Exception:
            pass
        cb.setTitle_(f["label"])
        cb.setState_(1 if f.get("default") else 0)
        handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda sender, k=key: self._on_setting_toggle(k, bool(sender.state())))
        self._handlers.append(handler)
        cb.setTarget_(handler)
        cb.setAction_("fire:")
        doc.addSubview_(cb)
        self._settings_widgets[key] = cb
        self._settings_kinds[key] = "switch"
        y = cb_y
        desc = f.get("description")
        if desc:
            # Indented gray wrapped description under the checkbox.
            indent = 20
            d_h = 16
            d = NSTextField.alloc().initWithFrame_(
                NSMakeRect(x + indent, cb_y - d_h - 1, width - indent, d_h))
            d.setStringValue_(desc)
            d.setFont_(NSFont.systemFontOfSize_(11))
            d.setBezeled_(False)
            d.setDrawsBackground_(False)
            d.setEditable_(False)
            d.setTextColor_(NSColor.tertiaryLabelColor())
            try:
                d.cell().setWraps_(True)
                d.setFrameSize_(d.cell().cellSizeForBounds_(
                    NSMakeRect(0, 0, width - indent, 999)))
                d.setFrameOrigin_(NSMakeRect(
                    x + indent, cb_y - d.frame().size.height - 1, 0, 0).origin)
            except Exception:
                pass
            doc.addSubview_(d)
            y = d.frame().origin.y
        return y

    def _render_logo_row(self, doc, f, x, width, top) -> float:
        """Logo segmented control with live preview + instant persist."""
        from AppKit import NSFont, NSSegmentedControl  # type: ignore[import]
        from Cocoa import NSColor, NSMakeRect, NSTextField  # type: ignore[import]
        key = f["key"]
        lbl_y = top - 18
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, lbl_y, width, 16))
        lbl.setStringValue_(f["label"])
        lbl.setFont_(NSFont.systemFontOfSize_(12))
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setTextColor_(NSColor.labelColor())
        doc.addSubview_(lbl)

        options = f.get("options", [])
        # Wider per-segment width so the three logo icons have comfortable room
        # and read as evenly spaced, not squeezed.
        seg_w = 72
        seg_y = lbl_y - 30
        seg = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(x, seg_y, min(width, seg_w * max(1, len(options)) + 8), 26))
        try:
            seg.setSegmentCount_(len(options))
        except Exception:
            pass
        for i, (title, img_path) in enumerate(options):
            try:
                if img_path:
                    from AppKit import NSImage  # type: ignore[import]
                    img = NSImage.alloc().initWithContentsOfFile_(img_path)
                    if img is not None:
                        img.setSize_(NSMakeRect(0, 0, 18, 18).size)
                        seg.setImage_forSegment_(img, i)
                if title:
                    seg.setLabel_forSegment_(str(title), i)
                seg.setWidth_forSegment_(seg_w, i)
            except Exception:
                pass
        default_idx = int(f.get("default") or 0)
        try:
            seg.setSelectedSegment_(default_idx)
        except Exception:
            pass
        handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda sender, k=key: self._on_setting_toggle(
                k, int(sender.selectedSegment())))
        self._handlers.append(handler)
        seg.setTarget_(handler)
        seg.setAction_("fire:")
        doc.addSubview_(seg)
        self._settings_widgets[key] = seg
        self._settings_kinds[key] = "segmented"
        return seg_y

    def _render_server_rows(self, doc, fields, x, width, top) -> float:
        """RPC / SSE / PAC port text fields ("auto" placeholder) + per-field
        gray note. The Save button lives on the Config-file row, not here."""
        from AppKit import NSFont  # type: ignore[import]
        from Cocoa import (  # type: ignore[import]
            NSColor,
            NSMakeRect,
            NSTextField,
        )
        labels = {"rpc_port": "RPC", "sse_port": "SSE", "pac_port": "PAC"}
        field_w = 110
        sub_label_w = 46
        y = top
        for f in fields:
            key = f["key"]
            row_y = y - 24
            sub = NSTextField.alloc().initWithFrame_(
                NSMakeRect(x, row_y + 3, sub_label_w, 18))
            sub.setStringValue_(labels.get(key, key))
            sub.setFont_(NSFont.systemFontOfSize_(12))
            sub.setBezeled_(False)
            sub.setDrawsBackground_(False)
            sub.setEditable_(False)
            sub.setTextColor_(NSColor.labelColor())
            doc.addSubview_(sub)

            tf = NSTextField.alloc().initWithFrame_(
                NSMakeRect(x + sub_label_w + 6, row_y, field_w, 22))
            tf.setStringValue_(str(f.get("default") or ""))
            tf.setFont_(NSFont.systemFontOfSize_(13))
            tf.setBezeled_(False)
            tf.setDrawsBackground_(False)
            tf.setEditable_(True)
            tf.setSelectable_(True)
            tf.setTextColor_(_hex_color(PALETTE["input_text"]))
            if f.get("placeholder"):
                try:
                    tf.cell().setPlaceholderString_(f["placeholder"])
                except Exception:
                    pass
            _apply_vcenter_cell(tf, _get_vcenter_text_cell_cls())
            self._style_input_field(tf)
            doc.addSubview_(tf)
            self._settings_widgets[key] = tf
            self._settings_kinds[key] = "text"

            note = f.get("note")
            if note:
                n = NSTextField.alloc().initWithFrame_(
                    NSMakeRect(x + sub_label_w + 6 + field_w + 10, row_y + 3,
                               width - (sub_label_w + 6 + field_w + 10), 16))
                n.setStringValue_(note)
                n.setFont_(NSFont.systemFontOfSize_(11))
                n.setBezeled_(False)
                n.setDrawsBackground_(False)
                n.setEditable_(False)
                n.setTextColor_(NSColor.tertiaryLabelColor())
                doc.addSubview_(n)
            y = row_y - 6
        return y

    def _render_config_file_row(self, doc, x, width, top) -> float:
        """Config file row: "Open Config File…" and the blue "Save" button on a
        single row. Save commits ALL staged settings (toggles + logo + login +
        ports); it is always enabled. Open Config File opens the YAML in
        $EDITOR."""
        from Cocoa import NSMakeRect  # type: ignore[import]
        btn_h = 28
        # Match the button baseline to the section-label baseline used in
        # _section_label (label frame origin: section_top - 20, height: 18).
        label_center = (top - 20) + 9
        btn_y = label_center - btn_h / 2.0
        open_w = 150
        open_btn = self._styled_neutral_button(
            "Open Config File…", NSMakeRect(x, btn_y, open_w, btn_h))
        open_handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda _s: self._dispatch("settings.open_config"))
        self._handlers.append(open_handler)
        open_btn.setTarget_(open_handler)
        open_btn.setAction_("fire:")
        doc.addSubview_(open_btn)

        # Save sits immediately to the right of Open Config File…, same row.
        save_w = 84
        save_x = x + open_w + 10
        save_btn = self._styled_save_button(
            "Save", NSMakeRect(save_x, btn_y, save_w, btn_h))
        save_btn.setEnabled_(True)
        self._restyle_save_button(save_btn, True)
        save_handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda _s: self._on_apply_settings())
        self._handlers.append(save_handler)
        save_btn.setTarget_(save_handler)
        save_btn.setAction_("fire:")
        doc.addSubview_(save_btn)
        # Keep the section height consistent with other rows.
        return btn_y - 6

    def _on_setting_toggle(self, key: str, value) -> None:
        """Stage one toggle/logo change locally - NOTHING persists until Apply.
        Marks the settings pane dirty. For logo_style, show a LIVE icon preview
        (reverted on leave/close without Apply)."""
        self._settings_dirty = True
        if key == "logo_style":
            try:
                self.tray.preview_logo_style(int(value))
            except Exception:
                pass

    def _on_apply_settings(self) -> None:
        """Commit EVERYTHING staged in the settings pane at once: toggles, logo,
        launch-at-login, and the three server ports. Port validation errors keep
        the pane (no partial apply). On success the pane re-renders with the
        saved values and the dirty flag clears."""
        values = self._settings_values()
        try:
            self.tray.apply_all_settings(values)
        except Exception:
            pass

    def discard_settings(self) -> None:
        """Drop all staged settings changes: revert any logo icon preview to the
        saved logo and clear the dirty flag. Called when leaving the settings
        category or closing the window without Apply."""
        if not getattr(self, "_settings_dirty", False):
            return
        self._settings_dirty = False
        try:
            self.tray.revert_logo_preview()
        except Exception:
            pass

    def _settings_values(self) -> dict:
        """Snapshot of the live settings controls. Used both by the debug dump
        and by Apply (apply_all_settings reads every staged value from here)."""
        out: dict = {}
        for key, w in self._settings_widgets.items():
            kind = self._settings_kinds.get(key)
            try:
                if kind == "switch":
                    out[key] = bool(w.state())
                elif kind == "segmented":
                    out[key] = int(w.selectedSegment())
                elif kind == "text":
                    out[key] = str(w.stringValue() or "")
            except Exception:
                out[key] = None
        return out

    def set_settings_field(self, key: str, value: str) -> dict:
        """Debug: write a live settings control and STAGE the change exactly as
        a user click/edit would - nothing persists until `action
        settings.apply` (toggles, logo, ports all commit together)."""
        w = self._settings_widgets.get(key)
        if w is None:
            return {"error": f"no settings field '{key}'"}
        kind = self._settings_kinds.get(key)
        if kind == "switch":
            on = value.strip().lower() in ("1", "on", "true", "yes")
            w.setState_(1 if on else 0)
            self._on_setting_toggle(key, on)
            return {"ok": True, "key": key, "value": on}
        if kind == "segmented":
            try:
                idx = int(value)
            except ValueError:
                return {"error": f"'{value}' is not a segment index"}
            w.setSelectedSegment_(idx)
            self._on_setting_toggle(key, idx)
            return {"ok": True, "key": key, "value": idx}
        # text (port): stage the edit; commit via settings.apply.
        try:
            w.setStringValue_(value)
        except Exception:
            return {"error": f"could not set '{key}'"}
        self._settings_dirty = True
        return {"ok": True, "key": key, "value": value}

    def clear_settings_dirty(self) -> None:
        """Clear the settings dirty flag (called by the tray after a successful
        Apply commits all staged changes)."""
        self._settings_dirty = False

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
            "detail_title": self._detail_spec.title
            if self._detail_spec else None,
            "detail_actions": [a.action_id for a in self._detail_spec.actions
                               if not a.action_id.endswith(".save")]
            if self._detail_spec else [],
            "detail_toggle": (bool(self._detail_spec.toggle[1])
                              if self._detail_spec.toggle else None)
            if self._detail_spec else None,
            "dirty": bool(self._dirty),
            "create_kind": self._create_kind,
            "col2_hidden": bool(self._col2_hidden),
            "settings": (self._settings_values()
                         if self.category == "settings" else None),
            "settings_dirty": bool(getattr(self, "_settings_dirty", False)),
            "fields": self._dump_fields(),
            # Cumulative auto-answered modal panels (debug mode) so tests can
            # assert no unexpected dialog fired.
            "alerts": self._dump_alerts(),
            # Debug: add-button targets must survive col-3 handler trims
            # (NSButton does not retain its target).
            "addbar_handlers": len(self._addbar_handlers),
        }

    def _dump_alerts(self) -> list:
        from susops.tray.mac import _DEBUG_ALERTS
        return [dict(a) for a in _DEBUG_ALERTS]

    def _dump_fields(self) -> dict:
        """JSON-serializable snapshot of the live form widget values."""
        out: dict = {}
        for key, val in self.collect_form_values().items():
            if isinstance(val, tuple):
                out[key] = list(val)
            else:
                out[key] = val
        return out

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

    def add(self) -> dict:
        """Debug: trigger the current category's primary add button, entering
        create mode. For shares the primary button is + Share File…."""
        specs = self._add_button_specs()
        if not specs:
            return {"error": f"no add button for category '{self.category}'"}
        kind = specs[0][1]
        self.enter_create_mode(kind)
        if self._create_kind is None:
            return {"error": f"could not enter create mode for '{kind}'"}
        return {"ok": True, "create_kind": self._create_kind}

    def add_fetch(self) -> dict:
        """Debug: enter the fetch create form (the secondary shares button).
        Fetch is an action, not a persistent item, but reuses create mode."""
        self.enter_create_mode("fetch")
        if self._create_kind is None:
            return {"error": "could not enter fetch create mode"}
        return {"ok": True, "create_kind": self._create_kind}

    def set_field(self, key: str, value: str) -> dict:
        """Debug: write a value into a live col-3 form widget and mark dirty,
        exactly as a user edit would. Returns the field's new dump value.

        `path` is an alias for the form's single path-kind field (the share
        File / fetch Output) so tests can inject a path without the NSOpenPanel.
        """
        # Settings pane has its own widget set (no dirty/edit machinery).
        if self.category == "settings":
            return self.set_settings_field(key, value)
        if key == "protocols":
            return self._set_protocols(value)
        if key == "path":
            path_keys = [k for k, kind in self._field_kinds.items()
                         if kind == "path"]
            if not path_keys:
                return {"error": "no path field in current form"}
            key = path_keys[0]
        w = self._field_widgets.get(key)
        if w is None:
            return {"error": f"no field '{key}' in current form"}
        kind = self._field_kinds.get(key)
        if kind == "popup":
            try:
                w.selectItemWithTitle_(value)
            except Exception:
                return {"error": f"option '{value}' not in popup '{key}'"}
        else:  # text / secure / combo
            try:
                w.setStringValue_(value)
            except Exception:
                return {"error": f"could not set '{key}'"}
        self._mark_dirty()
        return {"ok": True, "key": key, "value": self._read_widget(key)}

    def _set_protocols(self, value: str) -> dict:
        """Accept 'tcp', 'udp', 'tcp,udp', 'on', etc. Sets both checkboxes."""
        tcp_btn = self._field_widgets.get("protocols.tcp")
        udp_btn = self._field_widgets.get("protocols.udp")
        if tcp_btn is None or udp_btn is None:
            return {"error": "no protocols field in current form"}
        tokens = {t.strip().lower() for t in value.replace("+", ",").split(",")}
        tcp_on = "tcp" in tokens
        udp_on = "udp" in tokens
        tcp_btn.setState_(1 if tcp_on else 0)
        udp_btn.setState_(1 if udp_on else 0)
        self._mark_dirty()
        return {"ok": True, "key": "protocols",
                "value": [tcp_on, udp_on]}
