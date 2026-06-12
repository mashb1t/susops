"""macOS tray app — rumps + PyObjC.

Requires: pip install 'susops[tray-mac]'  (rumps>=0.4)

Matches the Linux tray feature-set: single multi-field dialogs (NSAlert +
accessoryView), logo-style picker with live preview, launch-at-login,
auto-discovered browser submenu, NSPopUpButton-based pickers, native
file-open panel for share, and a custom About panel.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from susops.core.config import PortForward
from susops.core.ports import is_port_free, validate_port
from susops.core.types import LogoStyle, ProcessState
from susops.tray.base import AbstractTrayApp, get_icon_path, get_ssh_hosts

BIND_ADDRESSES = ["localhost", "172.17.0.1", "0.0.0.0"]


# ---------------------------------------------------------------------------
# Appearance + icon helpers
# ---------------------------------------------------------------------------


def _is_dark_theme() -> bool:
    """Return True when macOS is using Dark Mode."""
    try:
        from AppKit import NSApplication  # type: ignore[import]
        appearance = NSApplication.sharedApplication().effectiveAppearance().name()
        return "dark" in appearance.lower()
    except Exception:
        return False


def _get_icon_path(state: ProcessState, logo_style: str = "colored_glasses") -> str | None:
    """Return icon path for state, respecting macOS light/dark appearance.

    Appearance is inverted: dark menu bar → light icons (and vice versa) so
    the asset is visible against the bar background.
    """
    variant = "light" if _is_dark_theme() else "dark"
    return get_icon_path(state, logo_style=logo_style, variant=variant, prefer_png=True)


_STATUS_ICONS_DIR = Path(__file__).parent.parent / "assets" / "icons" / "status"

_STATUS_ICON_NAMES = {
    ProcessState.RUNNING: "running",
    ProcessState.STOPPED_PARTIALLY: "stopped_partially",
    ProcessState.STOPPED: "stopped",
    ProcessState.ERROR: "error",
    ProcessState.INITIAL: "stopped",
}


def _get_status_icon_path(state: ProcessState) -> str | None:
    """Return path to the colored-circle SVG used as the status menu item's icon."""
    name = _STATUS_ICON_NAMES.get(state, "stopped")
    p = _STATUS_ICONS_DIR / f"{name}.svg"
    return str(p) if p.exists() else None


# ---------------------------------------------------------------------------
# Browser discovery (macOS)
# ---------------------------------------------------------------------------


def _find_installed_browsers() -> list[tuple[str, str, bool]]:
    """Return list of (app_bundle, display_name, is_chromium) for installed browsers.

    Thin adapter over susops.core.browsers.detect_browsers() — the menu
    construction code below was written before the shared module existed
    and expects this 3-tuple shape. Kept stable for that reason.
    """
    from susops.core.browsers import detect_browsers
    return [(b.bundle, b.name, b.is_chromium) for b in detect_browsers() if b.bundle]


# ---------------------------------------------------------------------------
# Module-level NSObject helper for live segment preview
# ---------------------------------------------------------------------------

_segmented_handler_cls = None
_switch_handler_cls = None
_main_dispatcher_cls = None
_button_handler_cls = None
_tagged_button_handler_cls = None
_window_close_delegate_cls = None
_url_handler_cls = None
_modal_panel_cls = None


def _get_modal_panel_cls():
    """Lazily build an NSPanel subclass that always becomes key/main.

    NSPanel's default behavior is `becomesKeyOnlyIfNeeded == YES` and
    `canBecomeKeyWindow` returns NO for some configurations. That's why text
    fields can look focused (border highlighted) yet silently reject keystrokes:
    the panel itself never becomes key, so the first responder never receives
    keyDown events. Subclassing and forcing both predicates to True fixes it.
    """
    global _modal_panel_cls
    if _modal_panel_cls is not None:
        return _modal_panel_cls

    import objc  # type: ignore[import]
    from AppKit import NSPanel  # type: ignore[import]

    class _SusOpsModalPanel(NSPanel):
        def canBecomeKeyWindow(self):
            return True

        def canBecomeMainWindow(self):
            return True

    _modal_panel_cls = _SusOpsModalPanel
    return _SusOpsModalPanel


def _get_url_handler_cls():
    """Lazily build the NSObject subclass that opens a URL when its button is clicked."""
    global _url_handler_cls
    if _url_handler_cls is not None:
        return _url_handler_cls

    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _URLHandler(NSObject):
        def initWithURL_(self, url):
            self = objc.super(_URLHandler, self).init()
            if self is None:
                return None
            self._url = url
            return self

        def openURL_(self, _):
            try:
                from AppKit import NSWorkspace  # type: ignore[import]
                from Foundation import NSURL  # type: ignore[import]
                ns_url = NSURL.URLWithString_(self._url)
                if ns_url is not None:
                    NSWorkspace.sharedWorkspace().openURL_(ns_url)
            except Exception:
                pass

    _url_handler_cls = _URLHandler
    return _URLHandler


def _get_button_handler_cls():
    """Lazily build the NSObject subclass used to handle OK/Cancel buttons on form panels.

    The handler stops the NSApplication modal session with a response code:
      1 = OK, 0 = Cancel.
    """
    global _button_handler_cls
    if _button_handler_cls is not None:
        return _button_handler_cls

    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _ButtonHandler(NSObject):
        def okClicked_(self, _):
            try:
                from AppKit import NSApplication  # type: ignore[import]
                NSApplication.sharedApplication().stopModalWithCode_(1)
            except Exception:
                pass

        def cancelClicked_(self, _):
            try:
                from AppKit import NSApplication  # type: ignore[import]
                NSApplication.sharedApplication().stopModalWithCode_(0)
            except Exception:
                pass

    _button_handler_cls = _ButtonHandler
    return _ButtonHandler


def _get_tagged_button_handler_cls():
    """Lazily build the NSObject subclass that stops the modal with the sender's tag."""
    global _tagged_button_handler_cls
    if _tagged_button_handler_cls is not None:
        return _tagged_button_handler_cls

    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _TaggedButtonHandler(NSObject):
        def buttonClicked_(self, sender):
            try:
                from AppKit import NSApplication  # type: ignore[import]
                NSApplication.sharedApplication().stopModalWithCode_(int(sender.tag()))
            except Exception:
                pass

    _tagged_button_handler_cls = _TaggedButtonHandler
    return _TaggedButtonHandler


def _get_window_close_delegate_cls():
    """Lazily build the NSObject subclass used as window delegate to handle the X button.

    When the user closes the panel via the close button, the modal is stopped
    with response code 0 (treated as cancel).
    """
    global _window_close_delegate_cls
    if _window_close_delegate_cls is not None:
        return _window_close_delegate_cls

    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _WindowCloseDelegate(NSObject):
        def windowShouldClose_(self, _sender):
            try:
                from AppKit import NSApplication  # type: ignore[import]
                NSApplication.sharedApplication().stopModalWithCode_(0)
            except Exception:
                pass
            return True

    _window_close_delegate_cls = _WindowCloseDelegate
    return _WindowCloseDelegate


def _get_segmented_handler_cls():
    """Lazily build the NSObject subclass used as target for segmented controls."""
    global _segmented_handler_cls
    if _segmented_handler_cls is not None:
        return _segmented_handler_cls

    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _SegmentedHandler(NSObject):
        def initWithCallback_(self, callback):
            self = objc.super(_SegmentedHandler, self).init()
            if self is None:
                return None
            self._cb = callback
            return self

        def segmentChanged_(self, sender):
            try:
                self._cb(sender.selectedSegment())
            except Exception:
                pass

    _segmented_handler_cls = _SegmentedHandler
    return _SegmentedHandler


def _get_switch_handler_cls():
    """Lazily build the NSObject subclass used as target for NSSwitch buttons."""
    global _switch_handler_cls
    if _switch_handler_cls is not None:
        return _switch_handler_cls

    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _SwitchHandler(NSObject):
        def initWithCallback_(self, callback):
            self = objc.super(_SwitchHandler, self).init()
            if self is None:
                return None
            self._cb = callback
            return self

        def switchChanged_(self, sender):
            try:
                self._cb(bool(sender.state()))
            except Exception:
                pass

    _switch_handler_cls = _SwitchHandler
    return _SwitchHandler


def _get_main_dispatcher_cls():
    """Lazily build the NSObject subclass used to dispatch callables onto the main thread."""
    global _main_dispatcher_cls
    if _main_dispatcher_cls is not None:
        return _main_dispatcher_cls

    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _MainDispatcher(NSObject):
        def initWithCallable_(self, callable_):
            self = objc.super(_MainDispatcher, self).init()
            if self is None:
                return None
            self._callable = callable_
            return self

        def fire_(self, _):
            try:
                self._callable()
            except Exception:
                pass

    _main_dispatcher_cls = _MainDispatcher
    return _MainDispatcher


def _on_main(callable_) -> None:
    """Schedule `callable_` to run on the main (NSApplication) thread.

    Use this to marshal UI updates triggered from background threads
    (e.g. the SSE listener) onto the Cocoa main runloop.
    """
    try:
        cls = _get_main_dispatcher_cls()
        disp = cls.alloc().initWithCallable_(callable_)
        disp.performSelectorOnMainThread_withObject_waitUntilDone_("fire:", None, False)
    except Exception:
        # Last-resort fallback: run inline (matches pre-existing behavior).
        try:
            callable_()
        except Exception:
            pass


def _run_on_main(fn, timeout: float = 5.0) -> dict:
    """Run fn on the main thread, wait for the result. For debug-server
    handlers, which run on socket threads but must touch AppKit."""
    if threading.current_thread() is threading.main_thread():
        return {"error": "_run_on_main must be called off the main thread"}
    box: dict = {}
    done = threading.Event()

    def _wrap():
        try:
            box["value"] = fn()
        except Exception as exc:
            box["value"] = {"error": str(exc)}
        finally:
            done.set()

    _on_main(_wrap)
    if not done.wait(timeout):
        return {"error": "main-thread timeout"}
    value = box.get("value")
    return value if isinstance(value, dict) else {"value": value}


def _menu_tree(menu) -> list:
    """Walk a rumps Menu/MenuItem mapping into a JSON-able tree."""
    tree: list = []
    try:
        items = list(menu.values())
    except Exception:
        return tree
    for item in items:
        title = getattr(item, "title", None)
        if item is None or title is None:
            tree.append({"separator": True})
            continue
        node: dict = {"title": str(title)}
        ns = getattr(item, "_menuitem", None)
        if ns is not None:
            try:
                node["enabled"] = bool(ns.isEnabled())
                key = str(ns.keyEquivalent() or "")
                if key:
                    node["key"] = key
            except Exception:
                pass
        try:
            children = _menu_tree(item)
        except Exception:
            children = []
        if children:
            node["children"] = children
        tree.append(node)
    return tree


def _screenshot_window(window, path: str) -> dict:
    """Render `window`'s content view to a PNG, in-process (no TCC needed)."""
    from AppKit import NSBitmapImageFileTypePNG  # type: ignore[import]
    view = window.contentView()
    bounds = view.bounds()
    rep = view.bitmapImageRepForCachingDisplayInRect_(bounds)
    if rep is None:
        return {"error": "could not create bitmap rep"}
    view.cacheDisplayInRect_toBitmapImageRep_(bounds, rep)
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
    if data is None or not data.writeToFile_atomically_(path, True):
        return {"error": f"could not write {path}"}
    return {"ok": True, "path": path,
            "width": int(rep.pixelsWide()), "height": int(rep.pixelsHigh())}


# ---------------------------------------------------------------------------
# Dialog helpers
# ---------------------------------------------------------------------------


def _activate_app() -> None:
    """Bring the SusOps app to front so dialogs receive focus."""
    try:
        from AppKit import NSApplication  # type: ignore[import]
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


# Depth-counted activation-policy management — only switches policy when the
# app is currently in .accessory mode (the bundled .app sets LSUIElement=YES).
# When running `.venv/bin/susops-tray` directly (.regular policy), this is a
# no-op: no Dock-icon flash, no behavioural change.
#
# Why this exists at all: `activateIgnoringOtherApps:` does NOT reliably
# activate accessory-mode apps on macOS, so dialogs from the bundled tray
# fail to receive key-window status on second/subsequent open. The standard
# workaround is to switch to .regular for the duration of any modal session,
# then restore the previous policy.
#
# Restoration is delayed via NSTimer so a quickly-following chained dialog
# (e.g. a success alert opened from the previous dialog's OK handler) shares
# the same policy transition — otherwise the chained alert opens behind the
# foreground app and looks like a freeze.

_policy_depth = 0
_policy_prev: int | None = None
_policy_restore_timer = None


def _cancel_policy_restore_timer() -> None:
    global _policy_restore_timer
    if _policy_restore_timer is not None:
        try:
            _policy_restore_timer.invalidate()
        except Exception:
            pass
        _policy_restore_timer = None


def _try_restore_policy(_timer=None) -> None:
    global _policy_depth, _policy_prev, _policy_restore_timer
    _policy_restore_timer = None
    if _policy_depth > 0:
        return  # another modal opened; keep .regular
    if _policy_prev is None:
        return
    try:
        from AppKit import NSApplication  # type: ignore[import]
        NSApplication.sharedApplication().setActivationPolicy_(_policy_prev)
    except Exception:
        pass
    _policy_prev = None


_edit_menu_installed = False


def _ensure_edit_menu() -> None:
    """Install a minimal app-level Edit menu so Cmd+C/V/X/A/Z work in dialogs.

    LSUIElement-style apps (no Dock icon, no menubar by default) get NO app
    menu from AppKit. Without an Edit menu, ``performKeyEquivalent:`` walks
    the menu hierarchy looking for an item whose key equivalent matches the
    pressed combo + whose action selector exists somewhere on the responder
    chain. With no menu, the lookup fails and Cmd+C / Cmd+V / Cmd+X /
    Cmd+A / Cmd+Z fall through — text fields appear to "swallow" the
    shortcut.

    We install an Edit menu with nil-targeted items so AppKit routes the
    selectors (``copy:``, ``paste:``, ``cut:``, ``selectAll:``, ``undo:``,
    ``redo:``) up the responder chain to the field editor (NSText), which
    has built-in implementations.

    Idempotent — runs at most once per process.
    """
    global _edit_menu_installed
    if _edit_menu_installed:
        return
    try:
        from AppKit import NSApplication, NSMenu, NSMenuItem  # type: ignore[import]

        app = NSApplication.sharedApplication()
        main_menu = app.mainMenu()
        if main_menu is None:
            main_menu = NSMenu.alloc().init()
            app.setMainMenu_(main_menu)

        # AppKit conventions: the first top-level item is the "app menu"
        # (whose label gets ignored — macOS uses the app name). Even though
        # we don't populate it, having it present makes the menubar render
        # correctly during the activation-policy regular scope.
        if main_menu.numberOfItems() == 0:
            app_item = NSMenuItem.alloc().init()
            app_item.setSubmenu_(NSMenu.alloc().initWithTitle_("SusOps"))
            main_menu.addItem_(app_item)

        edit_item = NSMenuItem.alloc().init()
        edit_item.setTitle_("Edit")
        edit_submenu = NSMenu.alloc().initWithTitle_("Edit")

        # (label, selector, key-equivalent). Nil target → walks responder
        # chain → reaches the text field's editor, which implements these.
        spec = [
            ("Undo", "undo:", "z"),
            ("Redo", "redo:", "Z"),  # Cmd+Shift+Z
            (None, None, None),  # separator
            ("Cut", "cut:", "x"),
            ("Copy", "copy:", "c"),
            ("Paste", "paste:", "v"),
            (None, None, None),
            ("Select All", "selectAll:", "a"),
        ]
        for label, selector, key in spec:
            if label is None:
                edit_submenu.addItem_(NSMenuItem.separatorItem())
                continue
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, selector, key,
            )
            edit_submenu.addItem_(item)

        edit_item.setSubmenu_(edit_submenu)
        main_menu.addItem_(edit_item)
        _edit_menu_installed = True
    except Exception:
        pass


class _RegularPolicyScope:
    """Context manager: ensure app is in .regular activation policy while open.

    No-op when the app is already .regular (e.g. running from `.venv/bin/`).
    Switches accessory → regular for bundled .app (LSUIElement=YES) so modal
    dialogs reliably receive key-window focus. Stacks safely: chained scopes
    share one policy transition; restoration is delayed by 0.3 s so a chained
    dialog cancels the pending restore.

    Also lazily installs the app-level Edit menu on first entry so Cmd+C /
    Cmd+V / etc. work in text fields inside dialogs — see _ensure_edit_menu.
    """

    def __enter__(self):
        global _policy_depth, _policy_prev
        try:
            from AppKit import (  # type: ignore[import]
                NSApplication,
                NSApplicationActivationPolicyRegular,
            )
            _ensure_edit_menu()
            _cancel_policy_restore_timer()
            app = NSApplication.sharedApplication()
            current = app.activationPolicy()
            if _policy_depth == 0:
                _policy_prev = current
            if current != NSApplicationActivationPolicyRegular:
                app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            app.activateIgnoringOtherApps_(True)
            _policy_depth += 1
        except Exception:
            pass
        return self

    def __exit__(self, *_):
        global _policy_depth, _policy_restore_timer
        if _policy_depth > 0:
            _policy_depth -= 1
        if _policy_depth == 0:
            try:
                from Foundation import NSTimer  # type: ignore[import]
                _policy_restore_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                    0.3, False, _try_restore_policy
                )
            except Exception:
                _try_restore_policy()
        return False


def _make_label(text: str, x: float, y: float, w: float, h: float, *, right: bool = True):
    from Cocoa import NSTextField, NSMakeRect  # type: ignore[import]
    lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    lbl.setStringValue_(text)
    lbl.setBezeled_(False)
    lbl.setDrawsBackground_(False)
    lbl.setEditable_(False)
    lbl.setSelectable_(False)
    lbl.setAlignment_(2 if right else 0)  # 2 = right, 0 = left
    return lbl


def _show_form_dialog(
        title: str,
        fields: list[dict],
        *,
        ok_title: str = "OK",
        cancel_title: str = "Cancel",
        informative: str | None = None,
        icon_path: str | None = None,
) -> dict | None:
    """Show a multi-field modal NSPanel form.

    Built as a floating NSPanel + NSApplication.runModalForWindow_ rather than
    NSAlert+accessoryView — the latter has focus/window-order quirks in
    menu-extra (LSUIElement) rumps apps that can leave the dialog invisible
    while the main thread is blocked in runModal.

    fields: list of dicts with keys:
        - key:     str (result key)
        - label:   str (display label; trailing colon recommended)
        - kind:    "text" | "secure" | "popup" | "combo" | "switch" | "segmented"
        - default: default value (str for text/secure, str for popup/combo selection,
                   bool for switch, int index for segmented)
        - options: list — required for popup/combo/segmented
        - hint:    optional placeholder text (text/secure/combo)
        - on_change: optional callback(index) for live preview (segmented)

    Returns dict {key: value} or None if cancelled.
    """
    from AppKit import (  # type: ignore[import]
        NSApplication,
        NSBackingStoreBuffered,
        NSFloatingWindowLevel,
        NSImage,
        NSImageScaleProportionallyDown,
        NSOffState,
        NSOnState,
        NSPanel,
        NSRegularControlSize,
        NSSegmentSwitchTrackingSelectOne,
        NSSwitchButton,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskTitled,
    )
    from Cocoa import (  # type: ignore[import]
        NSButton,
        NSComboBox,
        NSMakeRect,
        NSPopUpButton,
        NSSecureTextField,
        NSSegmentedControl,
        NSTextField,
    )

    # Layout constants tuned to match susops-mac's GenericFieldPanel:
    #   - 40-px row pitch (24-px field + 16-px gap)
    #   - 80x30 buttons positioned at the edges of the input column
    #   - Cancel on the left (just outside the input column), OK on the right
    LABEL_W = 160
    LABEL_GAP = 10
    INPUT_W = 200
    ROW_H = 24
    ROW_GAP = 16
    PAD_X = 16
    PAD_TOP = 16
    BUTTON_H = 30
    BUTTON_W = 80
    BUTTON_BOTTOM = 16
    GAP_BEFORE_BUTTONS = 16
    BUTTON_AREA = GAP_BEFORE_BUTTONS + BUTTON_H + BUTTON_BOTTOM

    rows = len(fields)
    # Info rows can be multi-line; estimate their extra height so the panel
    # is tall enough.
    info_extra_h = 0
    for f in fields:
        if f.get("kind") == "info":
            text_value = str(f.get("default", "") or "")
            line_count = text_value.count("\n") + 1
            info_h = max(ROW_H, line_count * 16 + 4)
            info_extra_h += max(0, info_h - ROW_H)
    fields_h = rows * ROW_H + max(0, rows - 1) * ROW_GAP + info_extra_h
    content_w = PAD_X + LABEL_W + LABEL_GAP + INPUT_W + PAD_X
    content_h = PAD_TOP + fields_h + BUTTON_AREA

    style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, content_w, content_h),
        style,
        NSBackingStoreBuffered,
        False,
    )
    panel.setTitle_(title)
    panel.setReleasedWhenClosed_(False)
    panel.setHidesOnDeactivate_(False)
    panel.setLevel_(NSFloatingWindowLevel)
    # NSPanel default is becomesKeyOnlyIfNeeded=YES which can prevent the
    # panel from becoming key for our modal — explicitly disable it so
    # text fields receive keystrokes.
    try:
        panel.setBecomesKeyOnlyIfNeeded_(False)
    except Exception:
        pass

    content = panel.contentView()
    widgets: dict[str, object] = {}
    handlers: list = []

    label_x = PAD_X
    input_x = PAD_X + LABEL_W + LABEL_GAP

    # Layout top-to-bottom (content view uses bottom-left origin).
    y = content_h - PAD_TOP - ROW_H
    for f in fields:
        key = f["key"]
        kind = f.get("kind", "text")
        default = f.get("default", "")
        options = f.get("options") or []
        hint = f.get("hint")
        on_change = f.get("on_change")

        lbl = _make_label(f.get("label", ""), label_x, y, LABEL_W, ROW_H, right=True)
        content.addSubview_(lbl)

        if kind == "text":
            # Use full ROW_H height — text fields shorter than ~22 px don't
            # receive keyboard input correctly on macOS (cursor can't fit).
            field = NSTextField.alloc().initWithFrame_(NSMakeRect(input_x, y, INPUT_W, ROW_H))
            field.setStringValue_(str(default) if default is not None else "")
            field.setEditable_(True)
            field.setSelectable_(True)
            field.setEnabled_(True)
            field.setBezeled_(True)
            if hint:
                try:
                    field.cell().setPlaceholderString_(hint)
                except Exception:
                    pass
            content.addSubview_(field)
            widgets[key] = field

        elif kind == "secure":
            field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(input_x, y, INPUT_W, ROW_H))
            field.setStringValue_(str(default) if default is not None else "")
            field.setEditable_(True)
            field.setSelectable_(True)
            field.setEnabled_(True)
            field.setBezeled_(True)
            if hint:
                try:
                    field.cell().setPlaceholderString_(hint)
                except Exception:
                    pass
            content.addSubview_(field)
            widgets[key] = field

        elif kind == "popup":
            popup = NSPopUpButton.alloc().initWithFrame_(NSMakeRect(input_x, y, INPUT_W, ROW_H))
            popup.setPullsDown_(False)
            titles = [str(o) for o in options]
            popup.addItemsWithTitles_(titles)
            if default is not None and str(default) in titles:
                popup.selectItemWithTitle_(str(default))
            elif titles:
                popup.selectItemAtIndex_(0)
            content.addSubview_(popup)
            widgets[key] = popup

        elif kind == "combo":
            combo = NSComboBox.alloc().initWithFrame_(NSMakeRect(input_x, y, INPUT_W, ROW_H))
            combo.addItemsWithObjectValues_([str(o) for o in options])
            # Enable inline autocomplete against the list (so typing "myh"
            # completes to "myhost" from ~/.ssh/config et al).
            try:
                combo.setCompletes_(True)
            except Exception:
                pass
            if default:
                combo.setStringValue_(str(default))
            # Leave blank if no explicit default so the placeholder shows and
            # the user can immediately type/autocomplete.
            if hint:
                try:
                    combo.cell().setPlaceholderString_(hint)
                except Exception:
                    pass
            content.addSubview_(combo)
            widgets[key] = combo

        elif kind == "switch":
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(input_x, y, INPUT_W, ROW_H))
            btn.setButtonType_(NSSwitchButton)
            btn.setTitle_("")
            btn.setState_(NSOnState if default else NSOffState)
            if on_change is not None:
                cls = _get_switch_handler_cls()
                handler = cls.alloc().initWithCallback_(on_change)
                handlers.append(handler)
                btn.setTarget_(handler)
                btn.setAction_("switchChanged:")
            content.addSubview_(btn)
            widgets[key] = btn

        elif kind == "segmented":
            seg = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(input_x, y, INPUT_W, ROW_H))
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
                            img.setSize_((24, 24))
                            seg.setImage_forSegment_(img, idx)
                            try:
                                seg.cell().setImageScaling_forSegment_(NSImageScaleProportionallyDown, idx)
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
                cls = _get_segmented_handler_cls()
                handler = cls.alloc().initWithCallback_(on_change)
                handlers.append(handler)
                seg.setTarget_(handler)
                seg.setAction_("segmentChanged:")
            content.addSubview_(seg)
            widgets[key] = seg

        elif kind == "info":
            # Multi-line static note spanning both columns. Used for the
            # "Host can be: domain / IP / CIDR" hint in susops-mac's
            # AddHostPanel. The label column is left empty for this kind.
            text_value = str(default) if default is not None else ""
            line_count = text_value.count("\n") + 1
            info_h = max(ROW_H, line_count * 16 + 4)
            # Remove the empty right-aligned label that the outer loop already
            # placed for this row (we want the info text to span full width).
            try:
                content.subviews()[-1].removeFromSuperview()
            except Exception:
                pass
            info_y = y + (ROW_H - info_h)
            info = NSTextField.alloc().initWithFrame_(
                NSMakeRect(label_x, info_y, content_w - 2 * PAD_X, info_h)
            )
            info.setStringValue_(text_value)
            info.setBezeled_(False)
            info.setDrawsBackground_(False)
            info.setEditable_(False)
            info.setSelectable_(False)
            info.setAlignment_(0)  # left
            try:
                cell = info.cell()
                cell.setWraps_(True)
                cell.setLineBreakMode_(0)
            except Exception:
                pass
            content.addSubview_(info)
            widgets[key] = info
            # Bump the row stride if this info row is taller than ROW_H.
            extra = max(0, info_h - ROW_H)
            y -= extra

        else:
            field = NSTextField.alloc().initWithFrame_(NSMakeRect(input_x, y, INPUT_W, ROW_H))
            content.addSubview_(field)
            widgets[key] = field

        y -= ROW_H + ROW_GAP

    # OK + Cancel buttons in the bottom-right corner.
    button_handler = _get_button_handler_cls().alloc().init()
    handlers.append(button_handler)

    # susops-mac places Cancel at the left edge of the input column and OK at
    # the right edge — visually anchoring both buttons within the same vertical
    # band as the input fields.
    cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(
        input_x, BUTTON_BOTTOM, BUTTON_W, BUTTON_H,
    ))
    cancel_btn.setTitle_(cancel_title)
    cancel_btn.setBezelStyle_(1)
    cancel_btn.setKeyEquivalent_("\x1b")  # Esc
    cancel_btn.setTarget_(button_handler)
    cancel_btn.setAction_("cancelClicked:")
    content.addSubview_(cancel_btn)

    ok_btn = NSButton.alloc().initWithFrame_(NSMakeRect(
        input_x + INPUT_W - BUTTON_W, BUTTON_BOTTOM, BUTTON_W, BUTTON_H,
    ))
    ok_btn.setTitle_(ok_title)
    ok_btn.setBezelStyle_(1)
    ok_btn.setKeyEquivalent_("\r")  # Enter — default button
    ok_btn.setTarget_(button_handler)
    ok_btn.setAction_("okClicked:")
    content.addSubview_(ok_btn)

    # Initial first responder + Tab navigation across all interactive widgets.
    # NSPanel needs setInitialFirstResponder_ so the first field is focused
    # when the panel becomes key — otherwise text fields appear "inactive" and
    # the user has to click into them before typing.
    keyable = [
        widgets.get(f["key"])
        for f in fields
        if widgets.get(f["key"]) is not None
           and f.get("kind", "text") in ("text", "secure", "combo", "popup", "switch", "segmented")
    ]
    for i in range(len(keyable) - 1):
        try:
            keyable[i].setNextKeyView_(keyable[i + 1])
        except Exception:
            pass
    if keyable:
        try:
            panel.setInitialFirstResponder_(keyable[0])
            keyable[-1].setNextKeyView_(keyable[0])  # cycle Tab
        except Exception:
            pass

    # Title-bar X / Cmd-W → treat as cancel. Without this delegate, closing
    # the panel via X just hides the window without stopping the modal
    # session, so the app stays in modal mode forever (menu greyed out,
    # no further dialogs can open).
    close_delegate = _get_window_close_delegate_cls().alloc().init()
    handlers.append(close_delegate)
    panel.setDelegate_(close_delegate)

    # Show + run modal inside _RegularPolicyScope — no-op when running from
    # `.venv/bin/` (already .regular), switches accessory→regular for the
    # bundled .app so the panel reliably receives key-window focus.
    with _RegularPolicyScope():
        panel.center()
        panel.makeKeyAndOrderFront_(None)
        if keyable:
            try:
                panel.makeFirstResponder_(keyable[0])
            except Exception:
                pass
        try:
            response = NSApplication.sharedApplication().runModalForWindow_(panel)
        finally:
            try:
                panel.setDelegate_(None)
            except Exception:
                pass
            try:
                panel.orderOut_(None)
            except Exception:
                pass
            try:
                panel.close()
            except Exception:
                pass

    if response != 1:
        handlers.clear()
        return None

    result: dict = {}
    for f in fields:
        key = f["key"]
        kind = f.get("kind", "text")
        w = widgets.get(key)
        if w is None:
            continue
        if kind in ("text", "secure"):
            result[key] = str(w.stringValue()).strip()
        elif kind == "combo":
            result[key] = str(w.stringValue()).strip()
        elif kind == "popup":
            item = w.titleOfSelectedItem()
            result[key] = str(item) if item is not None else ""
        elif kind == "switch":
            result[key] = bool(w.state())
        elif kind == "segmented":
            result[key] = int(w.selectedSegment())
        else:
            result[key] = ""

    handlers.clear()
    return result


def _show_pick_dialog(
        title: str,
        label: str,
        items: list[str],
        *,
        ok_title: str = "Select",
        cancel_title: str = "Cancel",
) -> str | None:
    """Show an NSAlert with a single NSPopUpButton; returns selected item or None."""
    if not items:
        _show_message("Nothing to Select", "The list is empty.")
        return None
    result = _show_form_dialog(
        title,
        [{"key": "value", "label": label, "kind": "popup", "options": items, "default": items[0]}],
        ok_title=ok_title,
        cancel_title=cancel_title,
    )
    if result is None:
        return None
    return result.get("value") or None


def _show_message_panel(
        title: str,
        message: str,
        buttons: list[tuple[str, int]],
        *,
        default_index: int = 0,
        cancel_index: int | None = None,
) -> int:
    """Show a modal message NSPanel with a multiline body and 1–3 buttons.

    NSAlert.runModal from rumps menu callbacks reliably hangs on macOS Tahoe
    (the alert window comes up in an unfocused state and is invisible to the
    user, leaving the main thread blocked in a modal that can't be dismissed —
    Ctrl+C in the launching terminal won't even kill the Python process).
    We sidestep NSAlert entirely and build the same kind of NSPanel that
    _show_form_dialog uses (it's the proven path).

    buttons: list of (label, response_code) — leftmost button is buttons[0].
             Idiomatic order on macOS is: rightmost = default (primary),
             leftmost = cancel. The helper places button[0] rightmost.
    default_index: which button is the default (Enter triggers it).
    cancel_index:  which button is Cancel (Esc + window-X triggers it).
                   Defaults to the last entry.

    Returns the response_code of the clicked button.
    """
    from AppKit import (  # type: ignore[import]
        NSApplication,
        NSBackingStoreBuffered,
        NSFloatingWindowLevel,
        NSPanel,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskTitled,
    )
    from Cocoa import (  # type: ignore[import]
        NSButton,
        NSMakeRect,
        NSTextField,
    )

    # Match susops-mac proportions: 30-px buttons, 16-px paddings.
    PAD_X = 16
    PAD_TOP = 18
    PAD_BOTTOM = 16
    GAP_BETWEEN = 18
    BUTTON_H = 30
    BUTTON_MIN_W = 80
    BUTTON_GAP = 10
    MSG_W = 360
    # Hard cap on the body height — beyond this we wrap the content in an
    # NSScrollView so the panel stays usable even for log dumps with hundreds
    # of lines.
    MSG_H_MAX = 460
    # Widen the panel for content with long lines (e.g. log entries) so the
    # text doesn't have to wrap as aggressively.
    MSG_W_WIDE = 640

    # Rough message height estimate (good enough — NSTextField will wrap).
    line_count = message.count("\n") + 1
    longest = max((len(line) for line in message.split("\n")), default=0)
    use_wide = longest > 56
    msg_w_target = MSG_W_WIDE if use_wide else MSG_W
    wraps_per_long_line = max(0, longest // (96 if use_wide else 56))
    msg_h_natural = max(36, (line_count + wraps_per_long_line) * 18 + 10)
    scrollable = msg_h_natural > MSG_H_MAX
    msg_h = min(msg_h_natural, MSG_H_MAX)

    # Compute button widths based on label lengths so long labels fit.
    button_widths = [max(BUTTON_MIN_W, 16 + 7 * len(label)) for label, _ in buttons]
    buttons_total_w = sum(button_widths) + BUTTON_GAP * max(0, len(buttons) - 1)

    content_w = max(msg_w_target + PAD_X * 2, buttons_total_w + PAD_X * 2)
    content_h = PAD_TOP + msg_h + GAP_BETWEEN + BUTTON_H + PAD_BOTTOM

    if cancel_index is None:
        cancel_index = len(buttons) - 1

    style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, content_w, content_h),
        style,
        NSBackingStoreBuffered,
        False,
    )
    panel.setTitle_(title or "")
    panel.setReleasedWhenClosed_(False)
    panel.setHidesOnDeactivate_(False)
    panel.setLevel_(NSFloatingWindowLevel)
    # NSPanel default is becomesKeyOnlyIfNeeded=YES which can prevent the
    # panel from becoming key for our modal — explicitly disable it so
    # text fields receive keystrokes.
    try:
        panel.setBecomesKeyOnlyIfNeeded_(False)
    except Exception:
        pass

    content = panel.contentView()
    handlers: list = []

    # Message body. Multi-line messages need monospaced text so column-
    # aligned output (status tables, log excerpts) renders correctly; for
    # single-line alerts the difference is barely noticeable. When the
    # natural content height exceeds MSG_H_MAX we wrap it in an NSScrollView
    # so log dumps stay browsable instead of producing a panel taller than
    # the screen.
    msg_y = content_h - PAD_TOP - msg_h
    use_mono = "\n" in (message or "")
    if scrollable:
        from AppKit import (  # type: ignore[import]
            NSScrollView,
            NSTextView,
            NSBezelBorder,
        )
        from Cocoa import NSMakeSize  # type: ignore[import]

        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(PAD_X, msg_y, content_w - 2 * PAD_X, msg_h)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(NSBezelBorder)
        tv = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, content_w - 2 * PAD_X, msg_h)
        )
        tv.setEditable_(False)
        tv.setSelectable_(True)
        tv.setRichText_(False)
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)
        tv.setAutoresizingMask_(2)  # NSViewWidthSizable
        if use_mono:
            try:
                from AppKit import NSFont, NSFontWeightRegular  # type: ignore[import]
                tv.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, NSFontWeightRegular))
            except Exception:
                pass
        tv.setString_(message or "")
        try:
            tv.textContainer().setContainerSize_(
                NSMakeSize(content_w - 2 * PAD_X, 1.0e7)
            )
            tv.textContainer().setWidthTracksTextView_(True)
        except Exception:
            pass
        scroll.setDocumentView_(tv)
        content.addSubview_(scroll)
    else:
        msg_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD_X, msg_y, content_w - 2 * PAD_X, msg_h))
        msg_lbl.setStringValue_(message or "")
        msg_lbl.setBezeled_(False)
        msg_lbl.setDrawsBackground_(False)
        msg_lbl.setEditable_(False)
        msg_lbl.setSelectable_(True)
        if use_mono:
            try:
                from AppKit import NSFont, NSFontWeightRegular  # type: ignore[import]
                msg_lbl.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, NSFontWeightRegular))
            except Exception:
                pass
        try:
            cell = msg_lbl.cell()
            cell.setWraps_(True)
            cell.setLineBreakMode_(0)  # NSLineBreakByWordWrapping
        except Exception:
            pass
        content.addSubview_(msg_lbl)

    # Buttons (button[0] = rightmost)
    button_handler = _get_tagged_button_handler_cls().alloc().init()
    handlers.append(button_handler)

    x_right = content_w - PAD_X
    for idx, (label, code) in enumerate(buttons):
        w = button_widths[idx]
        x = x_right - w
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, PAD_BOTTOM, w, BUTTON_H))
        btn.setTitle_(label)
        btn.setBezelStyle_(1)
        btn.setTag_(int(code))
        btn.setTarget_(button_handler)
        btn.setAction_("buttonClicked:")
        if idx == default_index:
            btn.setKeyEquivalent_("\r")  # Enter
        if idx == cancel_index and idx != default_index:
            btn.setKeyEquivalent_("\x1b")  # Esc
        content.addSubview_(btn)
        x_right = x - BUTTON_GAP

    # Close-button (X) → treat as cancel
    cancel_code = buttons[cancel_index][1] if 0 <= cancel_index < len(buttons) else 0
    delegate = _get_window_close_delegate_cls().alloc().init()
    handlers.append(delegate)
    panel.setDelegate_(delegate)

    # Show + run modal inside the activation-policy scope so the panel
    # receives key-window focus regardless of bundled vs dev-run.
    with _RegularPolicyScope():
        panel.center()
        panel.makeKeyAndOrderFront_(None)
        try:
            response = NSApplication.sharedApplication().runModalForWindow_(panel)
        finally:
            try:
                panel.setDelegate_(None)
            except Exception:
                pass
            try:
                panel.orderOut_(None)
            except Exception:
                pass
            try:
                panel.close()
            except Exception:
                pass

    handlers.clear()
    # If the delegate fired (X / Cmd-W), it stopped the modal with 0 — translate
    # to the actual cancel button's response code so callers get a consistent value.
    if response == 0 and cancel_code != 0:
        return cancel_code
    return int(response)


def _show_message(title: str, message: str, *, ok: str = "OK") -> None:
    """Show a simple modal info panel with a single OK/Close button."""
    _show_message_panel(title, message, [(ok, 1)])


# Strong refs to keep live windows + their timers/delegates alive after the
# Python caller returns. Indexed by id(panel) to allow multiple windows.
_LIVE_WINDOWS: dict[int, dict] = {}

# NSObject subclasses are registered globally by name in the Objective-C
# runtime. Defining them inside a function means the SECOND call re-registers
# the same class name and PyObjC returns a stale class whose selectors no
# longer dispatch to fresh Python state — that's why the logs panel only
# worked on its first invocation. Build these lazily once and reuse.
_live_tick_target_cls = None
_live_close_handler_cls = None
_live_window_delegate_cls = None


def _get_live_window_classes():
    """Lazily build the timer-target / close-button / window-delegate NSObject
    subclasses used by _open_live_text_window. Cached at module scope so
    repeated opens reuse the same registered ObjC classes."""
    global _live_tick_target_cls, _live_close_handler_cls, _live_window_delegate_cls
    if _live_tick_target_cls is not None:
        return (
            _live_tick_target_cls,
            _live_close_handler_cls,
            _live_window_delegate_cls,
        )

    import objc  # type: ignore[import]
    from Foundation import NSObject  # type: ignore[import]

    class _LiveTickTarget(NSObject):
        def initWithCallback_(self, cb):  # noqa: N802
            self = objc.super(_LiveTickTarget, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        def tick_(self, _timer):  # noqa: N802
            try:
                self._cb()
            except Exception:
                pass

    class _LiveCloseHandler(NSObject):
        def initWithCallback_(self, cb):  # noqa: N802
            self = objc.super(_LiveCloseHandler, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        def click_(self, _sender):  # noqa: N802
            try:
                self._cb()
            except Exception:
                pass

    class _LiveWindowDelegate(NSObject):
        def initWithCallback_(self, cb):  # noqa: N802
            self = objc.super(_LiveWindowDelegate, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        def windowShouldClose_(self, _sender):  # noqa: N802
            try:
                self._cb()
            except Exception:
                pass
            return True

    _live_tick_target_cls = _LiveTickTarget
    _live_close_handler_cls = _LiveCloseHandler
    _live_window_delegate_cls = _LiveWindowDelegate
    return _LiveTickTarget, _LiveCloseHandler, _LiveWindowDelegate


def _open_live_text_window(title: str, get_text: Callable[[], str],
                           interval_ms: int = 1000) -> None:
    """Open a non-modal panel that periodically refreshes its text body and
    auto-scrolls to the bottom. The tray menu stays responsive while it's open.

    `get_text` is called on the main thread by an NSTimer every interval_ms;
    if the result changed since the last tick the NSTextView is updated and
    scrolled to the bottom.
    """
    from AppKit import (  # type: ignore[import]
        NSApplication,
        NSBackingStoreBuffered,
        NSBezelBorder,
        NSFont,
        NSFontWeightRegular,
        NSPanel,
        NSScrollView,
        NSStatusWindowLevel,
        NSTextView,
        NSTimer,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskNonactivatingPanel,
        NSWindowStyleMaskResizable,
        NSWindowStyleMaskTitled,
    )
    from Cocoa import (  # type: ignore[import]
        NSMakeRect,
        NSMakeSize,
    )
    from PyObjCTools import AppHelper  # noqa: F401  (ensures runloop available)

    content_w = 912
    content_h = 513

    style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskResizable
            | NSWindowStyleMaskNonactivatingPanel
    )
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, content_w, content_h),
        style,
        NSBackingStoreBuffered,
        False,
    )
    panel.setTitle_(title or "Logs")
    panel.setReleasedWhenClosed_(False)
    panel.setHidesOnDeactivate_(False)
    # Status-window level keeps the panel above other app windows; the panel
    # also joins every Space + survives full-screen so it really does stay on
    # top regardless of where the user is.
    panel.setLevel_(NSStatusWindowLevel)
    try:
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
    except Exception:
        pass
    try:
        panel.setBecomesKeyOnlyIfNeeded_(False)
    except Exception:
        pass
    try:
        panel.setFloatingPanel_(True)
    except Exception:
        pass

    content = panel.contentView()

    # Scroll view fills the entire panel edge-to-edge — no in-panel close
    # button; the titlebar X handles dismissal via the window delegate below.
    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(0, 0, content_w, content_h)
    )
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(False)
    scroll.setAutohidesScrollers_(True)
    scroll.setBorderType_(NSBezelBorder)
    scroll.setAutoresizingMask_(2 | 16)  # WidthSizable | HeightSizable

    tv = NSTextView.alloc().initWithFrame_(
        NSMakeRect(0, 0, content_w, content_h)
    )
    tv.setEditable_(False)
    tv.setSelectable_(True)
    tv.setRichText_(False)
    tv.setVerticallyResizable_(True)
    tv.setHorizontallyResizable_(False)
    tv.setAutoresizingMask_(2)  # NSViewWidthSizable
    try:
        tv.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, NSFontWeightRegular))
    except Exception:
        pass
    try:
        tv.textContainer().setContainerSize_(NSMakeSize(content_w, 1.0e7))
        tv.textContainer().setWidthTracksTextView_(True)
    except Exception:
        pass
    scroll.setDocumentView_(tv)
    content.addSubview_(scroll)

    state: dict = {"last": None, "closed": False}

    def _build_attributed(text: str):
        """Build an NSAttributedString with colored segments per the shared
        log_style rules. Each line is parsed independently."""
        from AppKit import (  # type: ignore[import]
            NSAttributedString,
            NSColor,
            NSFont,
            NSFontWeightBold,
            NSFontWeightRegular,
            NSMutableAttributedString,
            NSForegroundColorAttributeName,
            NSFontAttributeName,
        )
        from susops.core.log_style import style_log_line

        base_font = NSFont.monospacedSystemFontOfSize_weight_(12, NSFontWeightRegular)
        bold_font = NSFont.monospacedSystemFontOfSize_weight_(12, NSFontWeightBold)

        # Pre-resolve colors for each label.
        palette = {
            "tag": (NSColor.systemTealColor(), True),
            "ok": (NSColor.systemGreenColor(), False),
            "warn": (NSColor.systemYellowColor(), False),
            "err": (NSColor.systemRedColor(), True),
            "dim": (NSColor.tertiaryLabelColor(), False),
            "info": (NSColor.systemBlueColor(), False),
        }
        default_color = NSColor.labelColor()

        result = NSMutableAttributedString.alloc().init()
        lines = text.split("\n")
        for i, line in enumerate(lines):
            for chunk, label in style_log_line(line):
                if not chunk:
                    continue
                color, bold = palette.get(label, (default_color, False))
                attrs = {
                    NSForegroundColorAttributeName: color,
                    NSFontAttributeName: bold_font if bold else base_font,
                }
                seg = NSAttributedString.alloc().initWithString_attributes_(chunk, attrs)
                result.appendAttributedString_(seg)
            if i < len(lines) - 1:
                nl = NSAttributedString.alloc().initWithString_attributes_(
                    "\n",
                    {NSFontAttributeName: base_font, NSForegroundColorAttributeName: default_color},
                )
                result.appendAttributedString_(nl)
        return result

    def _refresh_now() -> None:
        if state["closed"]:
            return
        try:
            text = get_text()
        except Exception as exc:  # never let a timer crash the runloop
            text = f"(log fetch failed: {exc})"
        if text == state["last"]:
            return
        state["last"] = text
        try:
            attr = _build_attributed(text)
            tv.textStorage().setAttributedString_(attr)
        except Exception:
            tv.setString_(text)
        # Scroll to bottom.
        try:
            length = tv.string().length() if tv.string() else 0
            tv.scrollRangeToVisible_((length, 0))
        except Exception:
            pass

    # Resolve cached NSObject classes (see _get_live_window_classes for why
    # they must NOT be defined inline inside this function).
    TickTargetCls, _CloseHandlerCls_unused, WindowDelegateCls = _get_live_window_classes()

    # Forward-declared so the delegate closure can call it. Real implementation
    # is assigned below once `policy_scope` and `state` exist.
    teardown_box: dict = {"fn": None}

    def _teardown_proxy():
        fn = teardown_box["fn"]
        if fn is not None:
            fn()

    tick_target = TickTargetCls.alloc().initWithCallback_(_refresh_now)
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        interval_ms / 1000.0, tick_target, "tick:", None, True,
    )

    # Titlebar close (X) → tear down via the window delegate. No in-panel
    # close button.
    delegate = WindowDelegateCls.alloc().initWithCallback_(_teardown_proxy)
    panel.setDelegate_(delegate)

    # Hold the activation-policy scope open across the window's lifetime —
    # using `with _RegularPolicyScope()` here would exit immediately after
    # ordering the window front, and 0.3 s later the app flips back to
    # accessory mode which hides regular windows. Enter the scope manually
    # on open and exit in _teardown so the panel stays visible until closed.
    policy_scope = _RegularPolicyScope()
    policy_scope.__enter__()

    def _teardown():
        if state["closed"]:
            return
        state["closed"] = True
        try:
            timer.invalidate()
        except Exception:
            pass
        try:
            panel.orderOut_(None)
        except Exception:
            pass
        try:
            policy_scope.__exit__(None, None, None)
        except Exception:
            pass
        _LIVE_WINDOWS.pop(id(panel), None)

    teardown_box["fn"] = _teardown

    _LIVE_WINDOWS[id(panel)] = {
        "panel": panel,
        "timer": timer,
        "tick_target": tick_target,
        "delegate": delegate,
        "tv": tv,
        "policy_scope": policy_scope,
    }

    # Initial fill before the first timer tick.
    _refresh_now()

    panel.center()
    panel.makeKeyAndOrderFront_(None)
    # orderFrontRegardless makes the panel show even if the app would
    # otherwise lose focus during the activation-policy transition.
    try:
        panel.orderFrontRegardless()
    except Exception:
        pass
    try:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def _show_confirm(title: str, message: str, *, ok: str = "OK", cancel: str = "Cancel") -> bool:
    """Show a confirmation panel; return True if OK clicked."""
    r = _show_message_panel(
        title, message, [(ok, 1), (cancel, 0)], default_index=0, cancel_index=1,
    )
    return r == 1


def _show_three_way(title: str, message: str, primary: str, secondary: str, cancel: str = "Cancel") -> int:
    """Three-button panel. Returns 1 if primary, 2 if secondary, 0 if cancel."""
    r = _show_message_panel(
        title, message,
        [(primary, 1), (secondary, 2), (cancel, 0)],
        default_index=0,
        cancel_index=2,
    )
    if r in (0, 1, 2):
        return r
    return 0


def _pick_file() -> str | None:
    """Show NSOpenPanel and return the chosen file path or None.

    Wrapped in _RegularPolicyScope so the app is in .regular activation
    policy for the duration — menu-extra apps on macOS Tahoe otherwise have
    NSOpenPanel come up behind/unfocused. Also forces floating window
    level + center + makeKeyAndOrderFront for extra safety.
    """
    from AppKit import NSFloatingWindowLevel, NSModalResponseOK  # type: ignore[import]
    from Cocoa import NSOpenPanel  # type: ignore[import]
    panel = NSOpenPanel.openPanel()
    panel.setCanChooseFiles_(True)
    panel.setCanChooseDirectories_(False)
    panel.setAllowsMultipleSelection_(False)
    try:
        panel.setLevel_(NSFloatingWindowLevel)
    except Exception:
        pass
    with _RegularPolicyScope():
        try:
            panel.center()
        except Exception:
            pass
        try:
            panel.makeKeyAndOrderFront_(None)
        except Exception:
            pass
        result = panel.runModal()
    if result != NSModalResponseOK:
        return None
    urls = panel.URLs()
    if not urls:
        return None
    return str(urls[0].path())


_ABOUT_WINDOWS: dict[int, dict] = {}


def _show_about_panel(version: str = "", *, icon_state=None) -> None:
    """Show the About panel — non-modal, always-on-top, titlebar-X-to-close.

    Behavioural changes vs the original:
      * Non-modal — no ``runModalForWindow_``. The tray menu stays usable.
      * Always on top — ``NSStatusWindowLevel`` + canJoinAllSpaces +
        fullScreenAuxiliary, plus a held-open ``_RegularPolicyScope`` so the
        panel doesn't vanish when accessory mode reasserts.
      * Static icon — ``assets/icon.png`` instead of the state-dependent
        per-style status icon.

    ``icon_state`` is accepted and ignored to keep the old call sites working.
    """
    if version == "":
        try:
            import susops
            version = getattr(susops, "__version__", "")
        except Exception:
            version = ""

    from AppKit import (  # type: ignore[import]
        NSApplication,
        NSBackingStoreBuffered,
        NSButton,
        NSImage,
        NSImageView,
        NSPanel,
        NSStatusWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskNonactivatingPanel,
        NSWindowStyleMaskTitled,
    )
    from Cocoa import (  # type: ignore[import]
        NSAttributedString,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSMakeRect,
        NSTextField,
    )

    win_w = 250
    win_h = 170
    icon_size = 64

    style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskNonactivatingPanel
    )
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, win_w, win_h),
        style,
        NSBackingStoreBuffered,
        False,
    )
    panel.setTitle_("")
    panel.setReleasedWhenClosed_(False)
    panel.setHidesOnDeactivate_(False)
    panel.setLevel_(NSStatusWindowLevel)
    try:
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
    except Exception:
        pass
    try:
        panel.setFloatingPanel_(True)
    except Exception:
        pass
    try:
        panel.setBecomesKeyOnlyIfNeeded_(False)
    except Exception:
        pass

    content = panel.contentView()
    url_handlers: list = []

    # ── App icon (centered at top) — static assets/icon.png ──
    icon_path = Path(__file__).parent.parent / "assets" / "icon.png"
    y_icon = win_h - icon_size - 14
    if icon_path.exists():
        try:
            img = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
            if img is not None:
                img.setSize_((icon_size, icon_size))
                x_icon = (win_w - icon_size) / 2
                image_view = NSImageView.alloc().initWithFrame_(
                    NSMakeRect(x_icon, y_icon, icon_size, icon_size)
                )
                image_view.setImage_(img)
                content.addSubview_(image_view)
        except Exception:
            pass

    def _add_centered_label(text: str, y: float, h: float, font, color=None) -> None:
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(0, y, win_w, h))
        lbl.setStringValue_(text)
        lbl.setAlignment_(1)  # center
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        lbl.setFont_(font)
        if color is not None:
            try:
                lbl.setTextColor_(color)
            except Exception:
                pass
        content.addSubview_(lbl)

    # ── "SusOps" bold name ──
    name_y = y_icon - 30
    _add_centered_label("SusOps", name_y, 20, NSFont.boldSystemFontOfSize_(14))

    # ── Version subtitle (dimmed) ──
    ver_y = name_y - 18
    try:
        secondary = NSColor.secondaryLabelColor()
    except Exception:
        secondary = None
    _add_centered_label(f"Version {version}", ver_y, 14, NSFont.systemFontOfSize_(11), secondary)

    # ── Link row: 4 borderless NSButtons separated by " | " labels ──
    links: list[tuple[str, str]] = [
        ("GitHub", "https://github.com/mashb1t/susops"),
        ("Sponsor", "https://github.com/sponsors/mashb1t"),
        ("Report a Bug", "https://github.com/mashb1t/susops/issues/new"),
    ]
    link_font = NSFont.systemFontOfSize_(12)
    try:
        link_color = NSColor.linkColor()
    except Exception:
        link_color = NSColor.blueColor()

    sep_attr = NSAttributedString.alloc().initWithString_attributes_(
        " | ", {NSFontAttributeName: link_font}
    )
    sep_w = float(sep_attr.size().width) + 2
    link_widths: list[float] = []
    for label, _ in links:
        attr = NSAttributedString.alloc().initWithString_attributes_(
            label, {NSFontAttributeName: link_font, NSForegroundColorAttributeName: link_color}
        )
        link_widths.append(float(attr.size().width) + 4)
    total_w = sum(link_widths) + sep_w * (len(links) - 1)

    link_y = ver_y - 26
    link_h = 18
    x = (win_w - total_w) / 2
    for idx, (label, url) in enumerate(links):
        w = link_widths[idx]
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, link_y, w, link_h))
        btn.setBordered_(False)
        btn.setButtonType_(7)  # NSButtonTypeMomentaryChange — visual feedback only
        attr_title = NSAttributedString.alloc().initWithString_attributes_(
            label,
            {
                NSFontAttributeName: link_font,
                NSForegroundColorAttributeName: link_color,
            },
        )
        btn.setAttributedTitle_(attr_title)
        handler = _get_url_handler_cls().alloc().initWithURL_(url)
        url_handlers.append(handler)
        btn.setTarget_(handler)
        btn.setAction_("openURL:")
        content.addSubview_(btn)
        x += w

        if idx != len(links) - 1:
            sep = NSTextField.alloc().initWithFrame_(NSMakeRect(x, link_y, sep_w, link_h))
            sep.setStringValue_("|")
            sep.setAlignment_(1)
            sep.setBezeled_(False)
            sep.setDrawsBackground_(False)
            sep.setEditable_(False)
            sep.setSelectable_(False)
            sep.setFont_(link_font)
            try:
                sep.setTextColor_(NSColor.tertiaryLabelColor())
            except Exception:
                pass
            content.addSubview_(sep)
            x += sep_w

    # ── Copyright (dimmed footer) ──
    # copy_y = link_y - 28
    # _add_centered_label("GPLv3, by mashb1t", copy_y, 14, NSFont.systemFontOfSize_(11), secondary)

    # Reuse the WindowDelegateCls from the live-logs helper — same close-via-X
    # semantics, callback-based, registered once at module scope (avoids the
    # second-open-is-invisible PyObjC re-registration bug).
    _TickTargetCls, _CloseHandlerCls, WindowDelegateCls = _get_live_window_classes()

    teardown_box: dict = {"fn": None}

    def _teardown_proxy():
        fn = teardown_box["fn"]
        if fn is not None:
            fn()

    delegate = WindowDelegateCls.alloc().initWithCallback_(_teardown_proxy)
    panel.setDelegate_(delegate)

    # Hold the activation-policy scope open across the window's lifetime —
    # exit immediately would let accessory mode reassert 0.3 s later and hide
    # the panel. See _open_live_text_window for the full rationale.
    policy_scope = _RegularPolicyScope()
    policy_scope.__enter__()

    state = {"closed": False}

    def _teardown():
        if state["closed"]:
            return
        state["closed"] = True
        try:
            panel.orderOut_(None)
        except Exception:
            pass
        try:
            policy_scope.__exit__(None, None, None)
        except Exception:
            pass
        _ABOUT_WINDOWS.pop(id(panel), None)

    teardown_box["fn"] = _teardown

    _ABOUT_WINDOWS[id(panel)] = {
        "panel": panel,
        "delegate": delegate,
        "url_handlers": url_handlers,
        "policy_scope": policy_scope,
    }

    panel.center()
    panel.makeKeyAndOrderFront_(None)
    try:
        panel.orderFrontRegardless()
    except Exception:
        pass
    try:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Launch at Login (AppleScript-based)
# ---------------------------------------------------------------------------


def _login_item_path() -> str:
    """Return the path that should be registered as the login item.

    When running from a frozen .app bundle, returns the bundle path. Otherwise
    returns the Python interpreter — best effort.
    """
    try:
        from Foundation import NSBundle  # type: ignore[import]
        bundle_path = NSBundle.mainBundle().bundlePath()
        if bundle_path and bundle_path.endswith(".app"):
            return str(bundle_path)
    except Exception:
        pass
    import sys
    return sys.executable


def _is_launch_at_login_enabled() -> bool:
    try:
        path = _login_item_path()
        name = Path(path).stem
        out = subprocess.check_output(
            ["osascript", "-e", 'tell application "System Events" to get name of every login item'],
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode("utf-8", errors="ignore")
        return name in out
    except Exception:
        return False


def _set_launch_at_login(enable: bool) -> None:
    path = _login_item_path()
    name = Path(path).stem
    if enable:
        script = (
            'tell application "System Events" to make login item '
            f'at end with properties {{path:"{path}", hidden:false}}'
        )
    else:
        script = f'tell application "System Events" to delete login item "{name}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=3)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tray app
# ---------------------------------------------------------------------------


class SusOpsMacTray(AbstractTrayApp):
    """macOS system tray application using rumps + native PyObjC dialogs."""

    def __init__(self) -> None:
        super().__init__()
        import rumps  # type: ignore[import]
        self._rumps = rumps

        icon_path = _get_icon_path(ProcessState.STOPPED, self.manager.app_config.logo_style.value.lower())
        self._app = rumps.App(
            "SusOps",
            icon=icon_path,
            template=False,
            quit_button=None,
        )
        self._active_shares: list = []
        # Tri-state cache for launch-at-login: None until the background probe finishes.
        # Synchronous osascript would block the main thread (and can trigger a TCC prompt
        # the first time), so we never query it from the menu callback.
        self._launch_at_login_cached: bool | None = None
        self._refresh_launch_at_login_async()
        self._build_menu()
        self._register_appearance_observer()
        self._config_window = None  # set in Phase 1
        self._debug_server = None
        debug_port = os.environ.get("SUSOPS_TRAY_DEBUG_PORT")
        if debug_port:
            from susops.tray.debug_server import TrayDebugServer
            self._debug_server = TrayDebugServer(
                self._debug_handlers(), port=int(debug_port),
            )
            self._debug_server.start()

    def _refresh_launch_at_login_async(self) -> None:
        def _probe():
            try:
                self._launch_at_login_cached = _is_launch_at_login_enabled()
            except Exception:
                self._launch_at_login_cached = False

        threading.Thread(target=_probe, daemon=True, name="susops-loginitem-probe").start()

    # ------------------------------------------------------------------ #
    # Appearance observer
    # ------------------------------------------------------------------ #

    def _register_appearance_observer(self) -> None:
        try:
            from Foundation import NSDistributedNotificationCenter  # type: ignore[import]

            def _on_appearance_changed(_notification):
                _on_main(lambda: self.update_icon(self.state))

            self._appearance_observer = _on_appearance_changed
            center = NSDistributedNotificationCenter.defaultCenter()
            center.addObserverForName_object_queue_usingBlock_(
                "AppleInterfaceThemeChangedNotification",
                None,
                None,
                _on_appearance_changed,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Debug server (opt-in, SUSOPS_TRAY_DEBUG_PORT)
    # ------------------------------------------------------------------ #

    def _debug_handlers(self) -> dict:
        """Debug-server command table. Every UI-touching handler marshals via
        _run_on_main. Extended in Phase 1 with open-config/select/dump-window."""

        def _screenshot(args):
            if not args:
                return {"error": "usage: screenshot <path>"}
            path = args[0]

            def _shot():
                win = self._debug_target_window()
                if win is None:
                    return {"error": "no window open"}
                return _screenshot_window(win, path)

            return _run_on_main(_shot)

        def _quit(args):
            _on_main(lambda: self._rumps.quit_application())
            return {"ok": True}

        return {
            "ping": lambda args: {"ok": True},
            "dump-menu": lambda args: _run_on_main(
                lambda: {"menu": _menu_tree(self._app.menu)}),
            "open-about": lambda args: _run_on_main(
                lambda: (_show_about_panel(), {"ok": True})[1]),
            "screenshot": _screenshot,
            "quit": _quit,
        }

    def _debug_target_window(self):
        """Window the screenshot command captures: config window when open
        (Phase 1), else any open About/live panel (Phase 0 verification)."""
        cw = getattr(self, "_config_window", None)
        if cw is not None and cw.is_open():
            return cw.window
        for store in (_ABOUT_WINDOWS, _LIVE_WINDOWS):
            for entry in store.values():
                return entry["panel"]
        return None

    # ------------------------------------------------------------------ #
    # AbstractTrayApp implementation
    # ------------------------------------------------------------------ #

    def _apply_icon_path(self, icon_path: str) -> None:
        """Route an icon path through the bandwidth subview when active, else
        fall back to rumps's normal app.icon setter."""
        if icon_path:
            self._last_icon_path = icon_path
        iv = getattr(self, "_bw_icon_view", None)
        if iv is None or not icon_path:
            if icon_path:
                self._app.icon = icon_path
            return
        from AppKit import NSImage  # type: ignore[import]
        img = NSImage.alloc().initByReferencingFile_(icon_path)
        if img is not None:
            iv.setImage_(img)
        try:
            button = self._app._nsapp.nsstatusitem.button()
            if button is not None:
                button.setImage_(None)
        except Exception:
            pass

    def _current_icon_path(self) -> str | None:
        """Most recently applied icon path. Falls back to the saved logo style
        so the first read after launch (before any _apply_icon_path) works."""
        cached = getattr(self, "_last_icon_path", None)
        if cached:
            return cached
        logo_style = self.manager.app_config.logo_style.value.lower()
        return _get_icon_path(self.state, logo_style)

    def update_icon(self, state: ProcessState) -> None:
        logo_style = self.manager.app_config.logo_style.value.lower()
        icon_path = _get_icon_path(state, logo_style)
        if icon_path:
            self._apply_icon_path(icon_path)

    def update_menu_sensitivity(self, state: ProcessState) -> None:
        # Match susops-mac's per-state enablement table:
        #   RUNNING            → Start off,  Stop on,  Restart on,  TestAll on
        #   STOPPED_PARTIALLY  → Start on,   Stop on,  Restart on,  TestAll on
        #   STOPPED  / INITIAL → Start on,   Stop off, Restart off, TestAll off
        #   ERROR              → all off (recovery is via Reset)
        running_like = state in (ProcessState.RUNNING, ProcessState.STOPPED_PARTIALLY)
        start_on = state in (ProcessState.STOPPED, ProcessState.STOPPED_PARTIALLY, ProcessState.INITIAL)
        action_on = running_like  # Stop / Restart / Test All

        if hasattr(self, "_item_start"):
            self._item_start._menuitem.setEnabled_(start_on)  # type: ignore[attr-defined]
        if hasattr(self, "_item_stop"):
            self._item_stop._menuitem.setEnabled_(action_on)  # type: ignore[attr-defined]
        if hasattr(self, "_item_restart"):
            self._item_restart._menuitem.setEnabled_(action_on)  # type: ignore[attr-defined]
        if hasattr(self, "_item_test_all"):
            self._item_test_all._menuitem.setEnabled_(action_on)  # type: ignore[attr-defined]
        if hasattr(self, "_item_status"):
            # SVG status indicator (green / orange / grey / red) shown as the
            # menu item's icon — matches susops-mac's status-icon pattern.
            label = state.value.lower().replace("_", " ")
            self._item_status.title = f"SusOps: {label}"
            icon_path = _get_status_icon_path(state)
            if icon_path:
                try:
                    self._item_status.icon = icon_path
                except Exception:
                    pass

    def show_alert(self, title: str, msg: str) -> None:
        _show_message(title, msg)

    def show_output_dialog(self, title: str, output: str) -> None:
        _show_message(title, output, ok="Close")

    def show_live_logs(self, get_text: Callable[[], str], *, title: str = "Logs",
                       interval_ms: int = 1000) -> None:
        """Open a non-modal, auto-refreshing logs panel on the main thread."""
        _on_main(lambda: _open_live_text_window(title, get_text, interval_ms))

    def run_in_background(self, fn: Callable, callback: Callable | None = None) -> None:
        """Run fn on a worker thread; marshal the callback back to the main thread.

        AppKit (NSWindow, NSAlert, …) MUST be touched only from the main thread.
        Background-task callbacks frequently end up calling show_alert / show_output_dialog,
        so we dispatch them via _on_main rather than running them on the worker. Without
        this, AppKit raises NSInternalInconsistencyException and the app gets stuck.
        """

        def _worker():
            result = fn()
            if callback is not None:
                _on_main(lambda: callback(result))

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Browser launch overrides (macOS)
    # ------------------------------------------------------------------ #

    def _launch_chromium_app(self, bundle_name: str) -> None:
        pac_url = self.manager.get_pac_url()
        if not pac_url:
            self.show_alert("Proxy Not Running", "Start the proxy first so the PAC port is known.")
            return
        from susops.core.browsers import Browser, launch_with_pac
        browser = Browser(
            name=bundle_name,
            launch_cmd=["open", "-a", bundle_name],
            is_chromium=True,
            bundle=bundle_name,
        )

        # Spawn in a background thread — `open -na` can block briefly on macOS
        # while LaunchServices coordinates with the new app instance, and we
        # don't want to freeze the menu bar app while that happens.
        def _spawn():
            try:
                launch_with_pac(browser, pac_url)
            except Exception as exc:
                _on_main(lambda: self.show_alert("Launch Failed", str(exc)))

        threading.Thread(target=_spawn, daemon=True, name="susops-launch-chrome").start()

    def _open_chromium_proxy_settings(self, bundle_name: str) -> None:
        """Open Chrome/Edge/Brave/... directly on chrome://net-internals/#proxy."""
        from susops.core.browsers import Browser, open_proxy_settings
        browser = Browser(
            name=bundle_name,
            launch_cmd=["open", "-a", bundle_name],
            is_chromium=True,
            bundle=bundle_name,
        )

        def _spawn():
            try:
                open_proxy_settings(browser)
            except Exception as exc:
                _on_main(lambda: self.show_alert("Launch Failed", str(exc)))

        threading.Thread(target=_spawn, daemon=True, name="susops-open-browser").start()

    def _launch_firefox_app(self, bundle_name: str = "Firefox") -> None:
        pac_url = self.manager.get_pac_url()
        if not pac_url:
            self.show_alert("Proxy Not Running", "Start the proxy first.")
            return
        from susops.core.browsers import Browser, launch_with_pac
        browser = Browser(
            name=bundle_name,
            launch_cmd=["open", "-a", bundle_name],
            is_chromium=False,
            bundle=bundle_name,
        )
        profile_dir = self.manager.workspace / "firefox_profile"

        def _spawn():
            try:
                launch_with_pac(browser, pac_url, profile_dir=profile_dir)
            except Exception as exc:
                _on_main(lambda: self.show_alert("Launch Failed", str(exc)))

        threading.Thread(target=_spawn, daemon=True, name="susops-launch-firefox").start()

    def do_launch_chrome(self) -> None:
        self._launch_chromium_app("Google Chrome")

    def do_launch_firefox(self) -> None:
        self._launch_firefox_app("Firefox")

    # ------------------------------------------------------------------ #
    # Menu building
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> None:
        rumps = self._rumps

        self._item_status = rumps.MenuItem("SusOps: …")
        # Seed the SVG icon at startup so the menu has the right circle before
        # the first poll fires.
        initial_icon = _get_status_icon_path(ProcessState.INITIAL)
        if initial_icon:
            try:
                self._item_status.icon = initial_icon
            except Exception:
                pass
        self._item_status._menuitem.setEnabled_(False)  # type: ignore[attr-defined]

        self._item_start = rumps.MenuItem("Start Proxy", callback=lambda _: self.do_start())
        self._item_stop = rumps.MenuItem("Stop Proxy", callback=lambda _: self.do_stop())
        self._item_restart = rumps.MenuItem("Restart Proxy", callback=lambda _: self.do_restart(), key="r")

        # Add submenu
        add_menu = rumps.MenuItem("Add")
        add_menu["Add Connection"] = rumps.MenuItem(
            "Add Connection", callback=lambda _: self._show_add_connection_dialog()
        )
        add_menu["Add Domain / IP / CIDR"] = rumps.MenuItem(
            "Add Domain / IP / CIDR", callback=lambda _: self._show_add_host_dialog()
        )
        add_menu["Add Local Forward"] = rumps.MenuItem(
            "Add Local Forward", callback=lambda _: self._show_add_forward_dialog(remote=False)
        )
        add_menu["Add Remote Forward"] = rumps.MenuItem(
            "Add Remote Forward", callback=lambda _: self._show_add_forward_dialog(remote=True)
        )

        # Remove submenu
        rm_menu = rumps.MenuItem("Remove")
        rm_menu["Remove Connection"] = rumps.MenuItem(
            "Remove Connection", callback=lambda _: self._show_rm_connection_dialog()
        )
        rm_menu["Remove Domain / IP / CIDR"] = rumps.MenuItem(
            "Remove Domain / IP / CIDR", callback=lambda _: self._show_rm_host_dialog()
        )
        rm_menu["Remove Local Forward"] = rumps.MenuItem(
            "Remove Local Forward", callback=lambda _: self._show_rm_local_dialog()
        )
        rm_menu["Remove Remote Forward"] = rumps.MenuItem(
            "Remove Remote Forward", callback=lambda _: self._show_rm_remote_dialog()
        )

        # Manage submenu
        manage_menu = rumps.MenuItem("Manage")
        manage_menu["Toggle Connection Enabled…"] = rumps.MenuItem(
            "Toggle Connection Enabled…", callback=lambda _: self._show_toggle_connection_dialog()
        )
        manage_menu["Toggle Domain Enabled…"] = rumps.MenuItem(
            "Toggle Domain Enabled…", callback=lambda _: self._show_toggle_domain_dialog()
        )
        manage_menu["Toggle Forward Enabled…"] = rumps.MenuItem(
            "Toggle Forward Enabled…", callback=lambda _: self._show_toggle_forward_dialog()
        )
        manage_menu["---1"] = None
        manage_menu["Start Connection…"] = rumps.MenuItem(
            "Start Connection…", callback=lambda _: self._show_start_connection_dialog()
        )
        manage_menu["Stop Connection…"] = rumps.MenuItem(
            "Stop Connection…", callback=lambda _: self._show_stop_connection_dialog()
        )
        manage_menu["Restart Connection…"] = rumps.MenuItem(
            "Restart Connection…", callback=lambda _: self._show_restart_connection_dialog()
        )

        # Test submenu
        self._item_test_all = rumps.MenuItem(
            "Test All PAC Hosts", callback=lambda _: self.do_test()
        )
        test_menu = rumps.MenuItem("Test")
        test_menu["Test Connection…"] = rumps.MenuItem(
            "Test Connection…", callback=lambda _: self._show_test_connection_dialog()
        )
        test_menu["Test Domain…"] = rumps.MenuItem(
            "Test Domain…", callback=lambda _: self._show_test_domain_dialog()
        )
        test_menu["Test Forward…"] = rumps.MenuItem(
            "Test Forward…", callback=lambda _: self._show_test_forward_dialog()
        )
        test_menu["---"] = None
        test_menu["Test All PAC Hosts"] = self._item_test_all

        # Launch Browser submenu — built dynamically from installed apps
        self._browser_menu = rumps.MenuItem("Launch Browser")
        self._rebuild_browser_submenu()

        # File Transfer submenu
        self._ft_menu = rumps.MenuItem("File Transfer")
        self._ft_menu["Share File…"] = rumps.MenuItem(
            "Share File…", callback=lambda _: self._show_share_file_dialog()
        )
        self._ft_menu["Fetch File…"] = rumps.MenuItem(
            "Fetch File…", callback=lambda _: self._show_fetch_file_dialog()
        )

        self._app.menu = [
            self._item_status,
            None,
            rumps.MenuItem("Settings…", callback=lambda _: self._show_settings_dialog(), key=","),
            None,
            add_menu,
            rm_menu,
            manage_menu,
            rumps.MenuItem("Open Config File", callback=lambda _: self.do_open_config_file()),
            None,
            self._item_start,
            self._item_stop,
            self._item_restart,
            None,
            test_menu,
            rumps.MenuItem("Show Status", callback=lambda _: self.do_status()),
            rumps.MenuItem("Show Logs", callback=lambda _: self.do_logs()),
            self._browser_menu,
            self._ft_menu,
            None,
            rumps.MenuItem("Reset All", callback=lambda _: self._confirm_reset()),
            None,
            rumps.MenuItem("About SusOps", callback=lambda _: self._show_about_dialog()),
            rumps.MenuItem("Quit", callback=self._on_quit, key="q"),
        ]

    def _rebuild_browser_submenu(self) -> None:
        rumps = self._rumps
        # Clear existing items
        for k in list(self._browser_menu.keys()):
            del self._browser_menu[k]

        installed = _find_installed_browsers()
        if not installed:
            none_item = rumps.MenuItem("No browsers found")
            none_item._menuitem.setEnabled_(False)  # type: ignore[attr-defined]
            self._browser_menu["No browsers found"] = none_item
            return

        for bundle, name, chromium in installed:
            sub = rumps.MenuItem(name)
            sub[f"Launch {name}"] = rumps.MenuItem(
                f"Launch {name}",
                callback=self._make_browser_launch(bundle, chromium),
            )
            if chromium:
                sub[f"Open {name} Proxy Settings"] = rumps.MenuItem(
                    f"Open {name} Proxy Settings",
                    callback=self._make_browser_settings(bundle),
                )
            self._browser_menu[name] = sub

    def _make_browser_launch(self, bundle: str, chromium: bool):
        def handler(_sender):
            if chromium:
                self._launch_chromium_app(bundle)
            else:
                self._launch_firefox_app(bundle)

        return handler

    def _make_browser_settings(self, bundle: str):
        def handler(_sender):
            self._open_chromium_proxy_settings(bundle)

        return handler

    # ------------------------------------------------------------------ #
    # File-transfer share submenu refresh (optimized — only on change)
    # ------------------------------------------------------------------ #

    def _refresh_share_submenu(self) -> None:
        import pathlib
        rumps = self._rumps
        new_shares = self.manager.list_shares()

        def _share_key(info):
            return (info.port, info.running, info.file_path)

        old_keys = [_share_key(s) for s in self._active_shares]
        new_keys = [_share_key(s) for s in new_shares]
        if old_keys == new_keys:
            return

        self._active_shares = new_shares

        # Remove old dynamic entries (everything except Share File… and Fetch File…)
        for key in list(self._ft_menu.keys()):
            if key not in ("Share File…", "Fetch File…"):
                del self._ft_menu[key]

        if not self._active_shares:
            return

        self._ft_menu["---"] = None
        for info in self._active_shares:
            name = pathlib.Path(info.file_path).name
            dot = "●" if info.running else "○"
            label = f"{dot} {name} ({info.port})"
            self._ft_menu[label] = rumps.MenuItem(
                label,
                callback=self._make_share_info_handler(info),
            )

    def do_poll(self) -> None:
        super().do_poll()
        self._refresh_share_submenu()

    # ------------------------------------------------------------------ #
    # Settings dialog (with live logo preview)
    # ------------------------------------------------------------------ #

    def _show_settings_dialog(self) -> None:
        ac = self.manager.app_config
        cfg = self.manager.config
        pac_port = cfg.pac_server_port
        rpc_port = cfg.rpc_server_port
        sse_port = cfg.status_server_port
        saved_logo = ac.logo_style
        logo_styles = list(LogoStyle)

        # Build segment images for logo styles (current state, current appearance)
        seg_options: list[tuple[str, str | None]] = []
        for style in logo_styles:
            img_path = _get_icon_path(self.state, style.value.lower())
            label_text = style.value.replace("_", " ").title()
            seg_options.append(("", img_path))

        def _preview(idx: int) -> None:
            if 0 <= idx < len(logo_styles):
                style = logo_styles[idx]
                icon_path = _get_icon_path(self.state, style.value.lower())
                if icon_path:
                    self._apply_icon_path(icon_path)

        # Initial defaults — updated after each invalid attempt so the user keeps state.
        # Launch-at-login state is read from a background-populated cache to avoid
        # blocking the main thread on osascript / TCC prompts when opening Settings.
        defaults = {
            "launch_at_login": bool(self._launch_at_login_cached),
            "stop_on_quit": ac.stop_on_quit,
            "ephemeral_ports": ac.ephemeral_ports,
            "restore_shares": ac.restore_shares_on_start,
            "show_bandwidth": ac.tray_show_bandwidth,
            "notifications": ac.notifications_enabled,
            "logo_style": logo_styles.index(saved_logo),
            "rpc_port": str(rpc_port) if rpc_port else "",
            "sse_port": str(sse_port) if sse_port else "",
            "pac_port": str(pac_port) if pac_port else "",
        }

        while True:
            fields = [
                {"key": "launch_at_login", "label": "Launch at Login:", "kind": "switch",
                 "default": defaults["launch_at_login"]},
                {"key": "stop_on_quit", "label": "Stop Proxy On Quit:", "kind": "switch",
                 "default": defaults["stop_on_quit"]},
                {"key": "ephemeral_ports", "label": "Random SSH Ports On Start:", "kind": "switch",
                 "default": defaults["ephemeral_ports"]},
                {"key": "restore_shares", "label": "Restore Shares On Start:", "kind": "switch",
                 "default": defaults["restore_shares"]},
                {"key": "show_bandwidth", "label": "Show Bandwidth In Menu Bar:", "kind": "switch",
                 "default": defaults["show_bandwidth"],
                 "on_change": self._preview_bandwidth_visibility},
                {"key": "notifications", "label": "Desktop Notifications:", "kind": "switch",
                 "default": defaults["notifications"]},
                {"key": "logo_style", "label": "Logo Style:", "kind": "segmented",
                 "options": seg_options, "default": defaults["logo_style"],
                 "on_change": _preview},
                # Server ports — RPC + SSE require a daemon restart to take
                # effect; PAC is hot-restarted by the facade.
                {"key": "rpc_port", "label": "RPC Server Port:", "kind": "text",
                 "default": defaults["rpc_port"],
                 "hint": "auto (0) — restart daemon to apply"},
                {"key": "sse_port", "label": "SSE Server Port:", "kind": "text",
                 "default": defaults["sse_port"],
                 "hint": "auto (0) — restart daemon to apply"},
                {"key": "pac_port", "label": "PAC Server Port:", "kind": "text",
                 "default": defaults["pac_port"],
                 "hint": "auto (0)"},
            ]

            result = _show_form_dialog("Settings", fields, ok_title="Save", cancel_title="Cancel")
            if result is None:
                # Revert any preview
                self.update_icon(self.state)
                self.refresh_bandwidth_title()
                return

            # Refresh defaults so a re-show on validation failure keeps user edits.
            defaults.update(result)

            # Validate all three port fields; the helper short-circuits to
            # the inner loop on failure so the user re-edits the offending
            # value without losing their other edits.
            port_ints: dict[str, int] = {}
            port_specs = [
                ("rpc_port", "RPC", rpc_port),
                ("sse_port", "SSE", sse_port),
                ("pac_port", "PAC", pac_port),
            ]
            invalid = False
            for key, label, current in port_specs:
                raw = (result.get(key) or "").strip() or "0"
                try:
                    n = int(raw)
                except ValueError:
                    _show_message("Invalid Port", f"'{raw}' is not a valid {label} port.")
                    invalid = True
                    break
                if not validate_port(n, allow_zero=True):
                    _show_message("Invalid Port",
                                  f"{label} port must be 0 (auto) or between 1 and 65535.")
                    invalid = True
                    break
                if n != 0 and n != current and not is_port_free(n):
                    _show_message("Port In Use", f"Port {n} is already in use.")
                    invalid = True
                    break
                port_ints[key] = n
            if invalid:
                continue

            new_logo = logo_styles[result["logo_style"]] if 0 <= result["logo_style"] < len(logo_styles) else saved_logo

            self.manager.update_app_config(
                stop_on_quit=result["stop_on_quit"],
                ephemeral_ports=result["ephemeral_ports"],
                restore_shares_on_start=result["restore_shares"],
                tray_show_bandwidth=result["show_bandwidth"],
                notifications_enabled=result["notifications"],
                logo_style=new_logo,
            )
            # No refresh_bandwidth_title() here: preview already left the
            # menu bar in its final state. Re-running update_title rebuilds
            # the subview frame and momentarily nudges the icon size.
            self.manager.update_config(
                rpc_server_port=port_ints["rpc_port"],
                status_server_port=port_ints["sse_port"],
                pac_server_port=port_ints["pac_port"],
            )
            # Apply Launch at Login off the main thread — osascript may block
            # for several seconds the first time (TCC prompt).
            desired_login = bool(result["launch_at_login"])
            self._launch_at_login_cached = desired_login  # optimistic update
            threading.Thread(
                target=lambda: _set_launch_at_login(desired_login),
                daemon=True,
                name="susops-loginitem-apply",
            ).start()
            self.update_icon(self.state)
            return

    # ------------------------------------------------------------------ #
    # Add dialogs
    # ------------------------------------------------------------------ #

    def _show_add_connection_dialog(self) -> None:
        try:
            ssh_hosts = get_ssh_hosts()
        except Exception:
            ssh_hosts = []

        fields = [
            {"key": "tag", "label": "Connection Tag *:", "kind": "text",
             "hint": "e.g. work"},
            {"key": "host", "label": "SSH Host *:", "kind": "combo",
             "options": ssh_hosts, "hint": "hostname, IP, or SSH alias"},
            {"key": "port", "label": "SOCKS Proxy Port (optional):", "kind": "text",
             "hint": "auto if blank"},
        ]
        while True:
            result = _show_form_dialog("Add Connection", fields, ok_title="Add", cancel_title="Cancel")
            if result is None:
                return
            tag = result["tag"]
            host = result["host"]
            port_text = result["port"]
            if not tag:
                _show_message("Missing Field", "Connection Tag must not be empty.")
                continue
            if not host:
                _show_message("Missing Field", "SSH Host must not be empty.")
                continue
            port_int = 0
            if port_text:
                if not port_text.isdigit() or not validate_port(int(port_text)):
                    _show_message("Invalid Port", "SOCKS Proxy Port must be between 1 and 65535.")
                    continue
                port_int = int(port_text)
                if not is_port_free(port_int):
                    _show_message("Port In Use", f"Port {port_int} is already in use.")
                    continue
            self.do_add_connection(tag, host, port_int)
            return

    def _show_add_host_dialog(self) -> None:
        cfg = self.manager.list_config()
        tags = [c.tag for c in cfg.connections]
        if not tags:
            _show_message("No Connections", "Add a connection first.")
            return
        fields = [
            {"key": "conn", "label": "Connection *:", "kind": "popup", "options": tags, "default": tags[0]},
            {"key": "host", "label": "Host / IP / CIDR *:", "kind": "text",
             "hint": "domain, IP address, or CIDR"},
            {"key": "_info", "label": "", "kind": "info",
             "default": "Host can be:\n  • Domain (subdomains & wildcards supported)\n  • IP address (CIDR notation supported)"},
        ]
        while True:
            result = _show_form_dialog("Add Domain / IP / CIDR", fields, ok_title="Add", cancel_title="Cancel")
            if result is None:
                return
            host = result["host"]
            conn_tag = result["conn"]
            if not host:
                _show_message("Missing Field", "Host must not be empty.")
                continue
            if not conn_tag:
                _show_message("Missing Field", "Select a connection.")
                continue
            self.do_add_pac_host(host, conn_tag=conn_tag)
            return

    def _show_add_forward_dialog(self, *, remote: bool) -> None:
        cfg = self.manager.list_config()
        tags = [c.tag for c in cfg.connections]
        if not tags:
            _show_message("No Connections", "Add a connection first.")
            return

        title = "Add Remote Forward" if remote else "Add Local Forward"
        src_label = "Forward Remote Port *:" if remote else "Forward Local Port *:"
        dst_label = "To Local Port *:" if remote else "To Remote Port *:"
        src_bind_label = "Remote Bind (optional):" if remote else "Local Bind (optional):"
        dst_bind_label = "Local Bind (optional):" if remote else "Remote Bind (optional):"

        fields = [
            {"key": "conn", "label": "Connection *:", "kind": "popup", "options": tags, "default": tags[0]},
            {"key": "tag", "label": "Tag (optional):", "kind": "text", "hint": "optional label"},
            {"key": "src", "label": src_label, "kind": "text", "hint": "e.g. 8080"},
            {"key": "dst", "label": dst_label, "kind": "text", "hint": "e.g. 80"},
            {"key": "src_addr", "label": src_bind_label, "kind": "combo",
             "options": BIND_ADDRESSES, "default": "localhost"},
            {"key": "dst_addr", "label": dst_bind_label, "kind": "combo",
             "options": BIND_ADDRESSES, "default": "localhost"},
            {"key": "tcp", "label": "TCP:", "kind": "switch", "default": True},
            {"key": "udp", "label": "UDP:", "kind": "switch", "default": False},
        ]

        while True:
            result = _show_form_dialog(title, fields, ok_title="Add", cancel_title="Cancel")
            if result is None:
                return

            conn_tag = result["conn"]
            tag = result["tag"]
            src = result["src"]
            dst = result["dst"]
            src_addr = result["src_addr"] or "localhost"
            dst_addr = result["dst_addr"] or "localhost"
            tcp = result["tcp"]
            udp = result["udp"]

            if not conn_tag:
                _show_message("No Connection", "Add a connection first.")
                continue
            if not tcp and not udp:
                _show_message("Protocol Required", "Select at least one protocol (TCP or UDP).")
                continue
            if not src.isdigit() or not validate_port(int(src)):
                _show_message("Invalid Port", f"{src_label.rstrip(':*').strip()} must be 1–65535.")
                continue
            if not dst.isdigit() or not validate_port(int(dst)):
                _show_message("Invalid Port", f"{dst_label.rstrip(':*').strip()} must be 1–65535.")
                continue
            if not remote and not is_port_free(int(src)):
                _show_message("Port In Use", f"Local port {src} is already in use.")
                continue

            fw = PortForward(
                src_addr=src_addr,
                src_port=int(src),
                dst_addr=dst_addr,
                dst_port=int(dst),
                tag=tag or None,
                tcp=tcp,
                udp=udp,
            )
            if remote:
                self.do_add_remote_forward(conn_tag, fw)
            else:
                self.do_add_local_forward(conn_tag, fw)
            return

    # ------------------------------------------------------------------ #
    # Remove dialogs
    # ------------------------------------------------------------------ #

    def _show_rm_connection_dialog(self) -> None:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = _show_pick_dialog("Remove Connection", "Connection:", tags, ok_title="Remove")
        if selected:
            self.do_remove_connection(selected)

    def _show_rm_host_dialog(self) -> None:
        cfg = self.manager.list_config()
        hosts = [h for c in cfg.connections for h in c.pac_hosts]
        selected = _show_pick_dialog("Remove Domain / IP / CIDR", "Host:", hosts, ok_title="Remove")
        if selected:
            self.do_remove_pac_host(selected)

    def _show_rm_local_dialog(self) -> None:
        cfg = self.manager.list_config()
        items: list[str] = []
        port_map: dict[str, int] = {}
        for c in cfg.connections:
            for fw in c.forwards.local:
                label = f"[{c.tag}] {fw.src_port}→{fw.dst_addr}:{fw.dst_port}"
                items.append(label)
                port_map[label] = fw.src_port
        selected = _show_pick_dialog("Remove Local Forward", "Local Forward:", items, ok_title="Remove")
        if selected and selected in port_map:
            self.do_remove_local_forward(port_map[selected])

    def _show_rm_remote_dialog(self) -> None:
        cfg = self.manager.list_config()
        items: list[str] = []
        port_map: dict[str, int] = {}
        for c in cfg.connections:
            for fw in c.forwards.remote:
                label = f"[{c.tag}] {fw.src_port}→{fw.dst_addr}:{fw.dst_port}"
                items.append(label)
                port_map[label] = fw.src_port
        selected = _show_pick_dialog("Remove Remote Forward", "Remote Forward:", items, ok_title="Remove")
        if selected and selected in port_map:
            self.do_remove_remote_forward(port_map[selected])

    # ------------------------------------------------------------------ #
    # Manage / toggle dialogs
    # ------------------------------------------------------------------ #

    def _show_toggle_connection_dialog(self) -> None:
        cfg = self.manager.list_config()
        items = [f"[{'✓' if c.enabled else '✗'}] {c.tag}" for c in cfg.connections]
        selected = _show_pick_dialog("Toggle Connection Enabled", "Connection:", items, ok_title="Toggle")
        if selected:
            tag = selected.split("] ", 1)[-1]
            self.do_toggle_connection_enabled(tag)

    def _show_toggle_domain_dialog(self) -> None:
        cfg = self.manager.list_config()
        items: list[str] = []
        for c in cfg.connections:
            for h in c.pac_hosts:
                enabled = h not in c.pac_hosts_disabled
                items.append(f"[{'✓' if enabled else '✗'}] {h}")
        selected = _show_pick_dialog("Toggle Domain Enabled", "Domain:", items, ok_title="Toggle")
        if selected:
            host = selected.split("] ", 1)[-1]
            self.do_toggle_pac_host_enabled(host)

    def _show_toggle_forward_dialog(self) -> None:
        cfg = self.manager.list_config()
        items: list[str] = []
        for c in cfg.connections:
            for fw in c.forwards.local:
                state = "✓" if fw.enabled else "✗"
                items.append(f"[{state}] [{c.tag}] local :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
            for fw in c.forwards.remote:
                state = "✓" if fw.enabled else "✗"
                items.append(f"[{state}] [{c.tag}] remote :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
        selected = _show_pick_dialog("Toggle Forward Enabled", "Forward:", items, ok_title="Toggle")
        if selected:
            m = re.search(r"\[([^\]]+)\] (local|remote) :(\d+)", selected)
            if m:
                conn_tag, direction, src_port = m.group(1), m.group(2), int(m.group(3))
                self.do_toggle_forward_enabled(conn_tag, src_port, direction)

    def _show_start_connection_dialog(self) -> None:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = _show_pick_dialog("Start Connection", "Connection:", tags, ok_title="Start")
        if selected:
            self.do_start_connection(selected)

    def _show_stop_connection_dialog(self) -> None:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = _show_pick_dialog("Stop Connection", "Connection:", tags, ok_title="Stop")
        if selected:
            self.do_stop_connection(selected)

    def _show_restart_connection_dialog(self) -> None:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = _show_pick_dialog("Restart Connection", "Connection:", tags, ok_title="Restart")
        if selected:
            self.do_restart_connection(selected)

    # ------------------------------------------------------------------ #
    # Test dialogs
    # ------------------------------------------------------------------ #

    def _show_test_connection_dialog(self) -> None:
        tags = [c.tag for c in self.manager.list_config().connections]
        selected = _show_pick_dialog("Test Connection", "Connection:", tags, ok_title="Test")
        if selected:
            self.do_test_connection(selected)

    def _show_test_domain_dialog(self) -> None:
        cfg = self.manager.list_config()
        items: list[str] = []
        for c in cfg.connections:
            for h in c.pac_hosts:
                items.append(f"[{c.tag}] {h}")
        selected = _show_pick_dialog("Test Domain", "Domain (via connection):", items, ok_title="Test")
        if selected:
            m = re.match(r"\[([^\]]+)\] (.+)", selected)
            if m:
                self.do_test_domain(m.group(2), m.group(1))

    def _show_test_forward_dialog(self) -> None:
        cfg = self.manager.list_config()
        items: list[str] = []
        for c in cfg.connections:
            for fw in c.forwards.local:
                items.append(f"[{c.tag}] local :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
            for fw in c.forwards.remote:
                items.append(f"[{c.tag}] remote :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}")
        selected = _show_pick_dialog("Test Forward", "Forward:", items, ok_title="Test")
        if selected:
            m = re.search(r"\[([^\]]+)\] (local|remote) :(\d+)", selected)
            if m:
                self.do_test_forward(m.group(1), int(m.group(3)), m.group(2))

    # ------------------------------------------------------------------ #
    # File transfer dialogs
    # ------------------------------------------------------------------ #

    def _show_share_file_dialog(self) -> None:
        tags = [c.tag for c in self.manager.list_config().connections]
        if not tags:
            _show_message("No Connections", "Add a connection first.")
            return

        file_path = _pick_file()
        if not file_path:
            return

        fields = [
            {"key": "conn", "label": "Connection *:", "kind": "popup", "options": tags, "default": tags[0]},
            {"key": "file", "label": "File:", "kind": "text", "default": file_path},
            {"key": "pw", "label": "Password (optional):", "kind": "text", "hint": "auto-generate if blank"},
            {"key": "port", "label": "Port:", "kind": "text", "default": "0", "hint": "0 = auto"},
        ]
        while True:
            result = _show_form_dialog(
                "Share File",
                fields,
                ok_title="Share",
                cancel_title="Cancel",
                informative=f"Sharing: {Path(file_path).name}",
            )
            if result is None:
                return
            conn_tag = result["conn"]
            fp = result["file"] or file_path
            pw = (result["pw"] or "").strip() or None
            port_text = (result["port"] or "0").strip() or "0"
            if not conn_tag:
                _show_message("Missing Field", "Select a connection.")
                continue
            if not fp or not Path(fp).exists():
                _show_message("File Not Found", f"'{fp}' does not exist.")
                continue
            try:
                port_int = int(port_text)
            except ValueError:
                _show_message("Invalid Port", f"'{port_text}' is not a valid port number.")
                continue
            self.do_share(conn_tag, fp, password=pw, port=port_int)
            return

    def _show_fetch_file_dialog(self) -> None:
        tags = [c.tag for c in self.manager.list_config().connections]
        if not tags:
            _show_message("No Connections", "Add a connection first.")
            return

        fields = [
            {"key": "conn", "label": "Connection *:", "kind": "popup", "options": tags, "default": tags[0]},
            {"key": "port", "label": "Port *:", "kind": "text", "hint": "e.g. 52100"},
            {"key": "pw", "label": "Password *:", "kind": "text"},
            {"key": "out", "label": "Save to (optional):", "kind": "text",
             "hint": "blank = ~/Downloads/<filename>"},
        ]
        while True:
            result = _show_form_dialog("Fetch File", fields, ok_title="Fetch", cancel_title="Cancel")
            if result is None:
                return
            conn_tag = result["conn"]
            port_text = (result["port"] or "").strip()
            pw = (result["pw"] or "").strip()
            outfile = (result["out"] or "").strip() or None
            if not conn_tag:
                _show_message("Missing Field", "Select a connection.")
                continue
            if not port_text or not pw:
                _show_message("Missing Field", "Port and password are required.")
                continue
            try:
                port_int = int(port_text)
            except ValueError:
                _show_message("Invalid Port", f"'{port_text}' is not a valid port number.")
                continue
            self.do_fetch(conn_tag, port_int, pw, outfile=outfile)
            return

    def _make_share_info_handler(self, info):
        def handler(_sender):
            self._show_share_info_dialog(info)

        return handler

    def _show_share_info_dialog(self, info) -> None:
        name = Path(info.file_path).name
        state = "running" if info.running else "stopped"
        toggle_label = "Stop" if info.running else "Start"
        message = (
            f"File: {info.file_path}\n"
            f"Port: {info.port}\n"
            f"Password: {info.password}\n"
            f"Connection: {info.conn_tag or '—'}\n"
            f"State: {state}"
        )
        choice = _show_three_way(
            f"Share: {name}",
            message,
            primary=toggle_label,
            secondary="Delete",
            cancel="Close",
        )
        if choice == 1:
            if info.running:
                self.do_stop_share(info.port)
            else:
                self.do_share(info.conn_tag or "", info.file_path, info.password, info.port)
            self._refresh_share_submenu()
        elif choice == 2:
            self.do_delete_share(info.port)
            self._refresh_share_submenu()

    # ------------------------------------------------------------------ #
    # Reset / About / Quit
    # ------------------------------------------------------------------ #

    def _confirm_reset(self) -> None:
        if not _show_confirm(
                "Reset All?",
                "This will stop all tunnels and delete the workspace. This cannot be undone.",
                ok="Reset",
                cancel="Cancel",
        ):
            return
        self.run_in_background(lambda: self.do_reset(), lambda _: None)

    def _show_about_dialog(self) -> None:
        import susops
        _show_about_panel(susops.__version__)

    def _on_quit(self, _sender) -> None:
        self.do_quit()
        self._rumps.quit_application()

    # ------------------------------------------------------------------ #
    # SSE listener
    # ------------------------------------------------------------------ #

    def _start_sse_listener(self) -> None:
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
                    import susops as _susops_pkg
                    req = urllib.request.Request(status_url, headers={
                        "X-Susops-Client": "tray-mac",
                        "X-Susops-Client-Version": _susops_pkg.__version__,
                        "X-Susops-Pid": str(os.getpid()),
                        # Tray only reacts to state + share events. Filtering
                        # out `bandwidth` (high-frequency) and `forward`
                        # spares the daemon some serialisation work and us
                        # some wakeups for events we never act on.
                        "X-Susops-Events": "state,share",
                    })
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        backoff = 1.0
                        # Refresh state on every (re)connect — without this the
                        # tray keeps its last cached state after a daemon
                        # restart and only updates when the new daemon happens
                        # to emit a state event.
                        _on_main(self.do_poll)
                        buf = ""
                        for raw in resp:
                            line = raw.decode("utf-8", errors="replace")
                            buf += line
                            if buf.endswith("\n\n"):
                                if "event: state" in buf:
                                    _on_main(self.do_poll)
                                if "event: share" in buf:
                                    _on_main(self._refresh_share_submenu)
                                buf = ""
                except Exception:
                    time.sleep(backoff)
                    # Cap at 5s — short enough that the user never sees more
                    # than 5s of staleness, even when the daemon is bouncing.
                    backoff = min(backoff * 2, 5.0)

        threading.Thread(target=_listen, daemon=True, name="susops-sse-mac").start()

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    _BW_RATE_COL_WIDTH = 9  # widest expected rate, e.g. "99.9 MB/s"

    def _clear_bw_views(self, button) -> None:
        iv = getattr(self, "_bw_icon_view", None)
        if iv is not None:
            iv.removeFromSuperview()
            self._bw_icon_view = None
        tf = getattr(self, "_bw_text_view", None)
        if tf is not None:
            tf.removeFromSuperview()
            self._bw_text_view = None
        try:
            self._app._nsapp.nsstatusitem.setLength_(-1.0)  # NSVariableStatusItemLength
        except Exception:
            pass
        # Restore the button's icon via rumps's own path so it picks up the
        # exact NSImage sizing rumps applies at launch. Poke the internal
        # path cache first so the setter doesn't short-circuit on a value
        # that matches the last one we routed through it.
        icon_path = self._current_icon_path()
        if icon_path:
            try:
                self._app._icon = None
            except Exception:
                pass
            self._app.icon = icon_path

    def update_title(self, rx_bps: float | None, tx_bps: float | None) -> None:
        try:
            status_item = self._app._nsapp.nsstatusitem
            button = status_item.button()
        except Exception:
            status_item, button = None, None

        if rx_bps is None or tx_bps is None:
            self._clear_bw_views(button)
            self._app.title = ""
            return

        up = self._format_rate(tx_bps).rjust(self._BW_RATE_COL_WIDTH)
        down = self._format_rate(rx_bps).rjust(self._BW_RATE_COL_WIDTH)
        text = f"{up}\n{down}"
        if button is None:
            self._app.title = text
            return

        from AppKit import (  # type: ignore[import]
            NSAttributedString,
            NSColor,
            NSFont,
            NSFontAttributeName,
            NSFontWeightRegular,
            NSForegroundColorAttributeName,
            NSImageView,
            NSLineBreakByClipping,
            NSMutableParagraphStyle,
            NSParagraphStyleAttributeName,
            NSTextAlignmentRight,
            NSTextField,
        )
        from Foundation import NSMakeRect  # type: ignore[import]

        font = NSFont.systemFontOfSize_weight_(8, NSFontWeightRegular)
        para = NSMutableParagraphStyle.alloc().init()
        para.setLineSpacing_(0)
        para.setAlignment_(NSTextAlignmentRight)
        attrs = {
            NSFontAttributeName: font,
            NSParagraphStyleAttributeName: para,
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }
        attr = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        # Lock text-view dimensions to the widest possible two-line block
        # so the frame stays the same even as digits / units fluctuate.
        max_text = (
            f"↑ {'99.9 MB/s'.rjust(self._BW_RATE_COL_WIDTH)}\n"
            f"↓ {'99.9 MB/s'.rjust(self._BW_RATE_COL_WIDTH)}"
        )
        max_attr = NSAttributedString.alloc().initWithString_attributes_(max_text, attrs)
        max_size = max_attr.size()
        fixed_w = max_size.width
        fixed_h = max_size.height

        iv = getattr(self, "_bw_icon_view", None)
        if iv is None:
            iv = NSImageView.alloc().init()
            iv.setImageScaling_(0)  # NSImageScaleProportionallyDown
            button.addSubview_(iv)
            self._bw_icon_view = iv

        tf = getattr(self, "_bw_text_view", None)
        if tf is None:
            tf = NSTextField.alloc().init()
            tf.setBezeled_(False)
            tf.setDrawsBackground_(False)
            tf.setEditable_(False)
            tf.setSelectable_(False)
            tf.setBordered_(False)
            tf.setAlignment_(NSTextAlignmentRight)
            tf.cell().setLineBreakMode_(NSLineBreakByClipping)
            button.addSubview_(tf)
            self._bw_text_view = tf

        # Load the current icon directly into NSImageView so we don't depend
        # on button.image() (which can be None when rumps short-circuits on
        # an unchanged path). NSImageView scales the native asset to fit
        # the view frame. Honour the last-previewed logo, not just the
        # saved one, so the Settings preview survives toggling Bandwidth.
        btn_h = button.frame().size.height
        # macOS draws menu-bar item icons at 20pt with a small inset on a
        # 22pt-tall button. Match that so the subview doesn't render the
        # icon noticeably larger than rumps's bare-button path.
        icon_box = 20.0
        icon_path = self._current_icon_path()
        if icon_path:
            from AppKit import NSImage as _NSImage  # type: ignore[import]
            img = _NSImage.alloc().initByReferencingFile_(icon_path)
            if img is not None:
                iv.setImage_(img)
        button.setImage_(None)
        button.setAttributedTitle_(NSAttributedString.alloc().initWithString_(""))
        tf.setAttributedStringValue_(attr)

        icon_w = icon_box
        icon_h = icon_box
        gap = 6.0
        right_pad = 4.0
        total_w = icon_w + gap + fixed_w + right_pad

        if status_item is not None:
            status_item.setLength_(total_w)
        iv.setFrame_(NSMakeRect(0, (btn_h - icon_h) / 2.0, icon_w, icon_h))
        y = (btn_h - fixed_h) / 2.0
        x = icon_w + gap
        tf.setFrame_(NSMakeRect(x, y, fixed_w, fixed_h))

    def _tick_bandwidth(self, _timer=None) -> None:
        self.refresh_bandwidth_title()

    def _preview_bandwidth_visibility(self, checked: bool) -> None:
        """Live-toggle the menu-bar bandwidth title from the Settings switch.
        Config isn't persisted until the user clicks Save; Cancel reverts via
        refresh_bandwidth_title() reading the unchanged saved value."""
        if checked:
            try:
                rx, tx = self.manager.get_bandwidth_global()
            except Exception:
                rx, tx = 0.0, 0.0
            self.update_title(rx, tx)
        else:
            self.update_title(None, None)

    def run(self) -> None:
        # Initial state pull on startup; from then on the SSE listener drives
        # every refresh. No periodic polling fallback — SSE reconnects with
        # a small backoff cap on its own.
        self.do_poll()
        # rumps creates the NSStatusItem inside applicationDidFinishLaunching,
        # which only fires once _app.run() starts the runloop. Schedule the
        # bandwidth subview population for the first runloop tick so the
        # status item never paints in its "icon-only, default width" state
        # before we widen it.
        from Foundation import NSTimer  # type: ignore[import]
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.0, False, lambda _t: self.refresh_bandwidth_title()
        )
        self._start_sse_listener()
        self._bw_timer = self._rumps.Timer(self._tick_bandwidth, 1)
        self._bw_timer.start()
        self._app.run()


def main() -> None:
    app = SusOpsMacTray()
    app.run()
