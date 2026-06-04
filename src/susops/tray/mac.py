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


_STATUS_ICONS_DIR = Path(__file__).parent.parent.parent.parent / "assets" / "icons" / "status"

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


_MAC_BROWSERS: list[tuple[str, str, bool]] = [
    # (app bundle name, display name, is_chromium)
    ("Google Chrome", "Chrome", True),
    ("Chromium", "Chromium", True),
    ("Brave Browser", "Brave", True),
    ("Vivaldi", "Vivaldi", True),
    ("Microsoft Edge", "Edge", True),
    ("Arc", "Arc", True),
    ("Firefox", "Firefox", False),
]


def _find_installed_browsers() -> list[tuple[str, str, bool]]:
    """Return list of (app_bundle, display_name, is_chromium) found on disk."""
    found: list[tuple[str, str, bool]] = []
    for bundle, name, chromium in _MAC_BROWSERS:
        for base in (Path("/Applications"), Path.home() / "Applications"):
            if (base / f"{bundle}.app").exists():
                found.append((bundle, name, chromium))
                break
    return found


# ---------------------------------------------------------------------------
# Module-level NSObject helper for live segment preview
# ---------------------------------------------------------------------------

_segmented_handler_cls = None
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


class _RegularPolicyScope:
    """Context manager: ensure app is in .regular activation policy while open.

    No-op when the app is already .regular (e.g. running from `.venv/bin/`).
    Switches accessory → regular for bundled .app (LSUIElement=YES) so modal
    dialogs reliably receive key-window focus. Stacks safely: chained scopes
    share one policy transition; restoration is delayed by 0.3 s so a chained
    dialog cancels the pending restore.
    """

    def __enter__(self):
        global _policy_depth, _policy_prev
        try:
            from AppKit import (  # type: ignore[import]
                NSApplication,
                NSApplicationActivationPolicyRegular,
            )
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

    # Rough message height estimate (good enough — NSTextField will wrap).
    line_count = message.count("\n") + 1
    longest = max((len(line) for line in message.split("\n")), default=0)
    wraps_per_long_line = max(0, longest // 56)
    msg_h = max(36, (line_count + wraps_per_long_line) * 18 + 10)

    # Compute button widths based on label lengths so long labels fit.
    button_widths = [max(BUTTON_MIN_W, 16 + 7 * len(label)) for label, _ in buttons]
    buttons_total_w = sum(button_widths) + BUTTON_GAP * max(0, len(buttons) - 1)

    content_w = max(MSG_W + PAD_X * 2, buttons_total_w + PAD_X * 2)
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

    # Message body
    msg_y = content_h - PAD_TOP - msg_h
    msg_lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD_X, msg_y, content_w - 2 * PAD_X, msg_h))
    msg_lbl.setStringValue_(message or "")
    msg_lbl.setBezeled_(False)
    msg_lbl.setDrawsBackground_(False)
    msg_lbl.setEditable_(False)
    msg_lbl.setSelectable_(True)
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


def _show_about_panel(version: str, *, icon_state: ProcessState = ProcessState.RUNNING) -> None:
    """Show the About panel, styled to match susops-mac's AboutPanel.

    Layout matches the original exactly:
        ┌──────────────────────────────────────────┐
        │              [ icon (64x64) ]            │
        │                 SusOps                   │
        │              Version X.Y.Z               │
        │                                          │
        │   GitHub | CLI | Sponsor | Report a Bug  │
        │                                          │
        │        Copyright © Manuel Schmid         │
        └──────────────────────────────────────────┘

    Links are rendered as borderless NSButtons (not NSTextField attributed
    strings — those don't reliably receive click events in PyObjC).
    """
    from AppKit import (  # type: ignore[import]
        NSApplication,
        NSBackingStoreBuffered,
        NSButton,
        NSFloatingWindowLevel,
        NSImage,
        NSImageView,
        NSPanel,
        NSWindowStyleMaskClosable,
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

    win_w = 360
    win_h = 220
    icon_size = 64

    style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, win_w, win_h),
        style,
        NSBackingStoreBuffered,
        False,
    )
    panel.setTitle_("")
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

    # ── App icon (centered at top) ──
    icon_path = _get_icon_path(icon_state)
    y_icon = win_h - icon_size - 14
    if icon_path:
        try:
            img = NSImage.alloc().initWithContentsOfFile_(icon_path)
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
    name_y = y_icon - 22
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
        ("CLI", "https://github.com/mashb1t/susops#cli"),
        ("Sponsor", "https://github.com/sponsors/mashb1t"),
        ("Report a Bug", "https://github.com/mashb1t/susops/issues/new"),
    ]
    link_font = NSFont.systemFontOfSize_(12)
    try:
        link_color = NSColor.linkColor()
    except Exception:
        link_color = NSColor.blueColor()

    # Measure widths so we can center the whole row.
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
        handlers.append(handler)
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
    copy_y = link_y - 28
    _add_centered_label("Copyright © Manuel Schmid", copy_y, 14, NSFont.systemFontOfSize_(11), secondary)

    # Window-X delegate (no buttons inside the panel — close via title bar X).
    delegate = _get_window_close_delegate_cls().alloc().init()
    handlers.append(delegate)
    panel.setDelegate_(delegate)

    with _RegularPolicyScope():
        panel.center()
        panel.makeKeyAndOrderFront_(None)
        try:
            NSApplication.sharedApplication().runModalForWindow_(panel)
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
    # AbstractTrayApp implementation
    # ------------------------------------------------------------------ #

    def update_icon(self, state: ProcessState) -> None:
        logo_style = self.manager.app_config.logo_style.value.lower()
        icon_path = _get_icon_path(state, logo_style)
        if icon_path:
            self._app.icon = icon_path

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

    def schedule_poll(self, interval_seconds: int) -> None:
        @self._rumps.timer(interval_seconds)
        def _poll(_sender):
            self.do_poll()

    # ------------------------------------------------------------------ #
    # Browser launch overrides (macOS)
    # ------------------------------------------------------------------ #

    def _launch_chromium_app(self, bundle_name: str) -> None:
        pac_url = self.manager.get_pac_url()
        if not pac_url:
            self.show_alert("Proxy Not Running", "Start the proxy first so the PAC port is known.")
            return
        # Spawn in a background thread — `open -na` can block briefly on macOS
        # while LaunchServices coordinates with the new app instance, and we
        # don't want to freeze the menu bar app while that happens.
        def _spawn():
            try:
                subprocess.Popen(
                    ["open", "-na", bundle_name, "--args", f"--proxy-pac-url={pac_url}"]
                )
            except Exception as exc:
                _on_main(lambda: self.show_alert("Launch Failed", str(exc)))
        threading.Thread(target=_spawn, daemon=True, name="susops-launch-chrome").start()

    def _open_chromium_proxy_settings(self, bundle_name: str) -> None:
        def _spawn():
            try:
                subprocess.Popen(["open", "-a", bundle_name])
            except Exception:
                pass
        threading.Thread(target=_spawn, daemon=True, name="susops-open-browser").start()
        _show_message(
            "Open Proxy Settings",
            f"Paste this URL into the {bundle_name} address bar:\n\nchrome://net-internals/#proxy",
        )

    def _launch_firefox_app(self, bundle_name: str = "Firefox") -> None:
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
        def _spawn():
            try:
                subprocess.Popen(
                    ["open", "-na", bundle_name, "--args", "-profile", str(profile_dir), "-no-remote"]
                )
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
        pac_port = self.manager.config.pac_server_port
        saved_logo = ac.logo_style
        logo_styles = list(LogoStyle)

        # Build segment images for logo styles (current state, current appearance)
        seg_options: list[tuple[str, str | None]] = []
        for style in logo_styles:
            img_path = _get_icon_path(self.state, style.value.lower())
            label_text = style.value.replace("_", " ").title()
            seg_options.append((label_text, img_path))

        def _preview(idx: int) -> None:
            if 0 <= idx < len(logo_styles):
                style = logo_styles[idx]
                icon_path = _get_icon_path(self.state, style.value.lower())
                if icon_path:
                    self._app.icon = icon_path

        # Initial defaults — updated after each invalid attempt so the user keeps state.
        # Launch-at-login state is read from a background-populated cache to avoid
        # blocking the main thread on osascript / TCC prompts when opening Settings.
        defaults = {
            "launch_at_login": bool(self._launch_at_login_cached),
            "stop_on_quit": ac.stop_on_quit,
            "ephemeral_ports": ac.ephemeral_ports,
            "restore_shares": ac.restore_shares_on_start,
            "logo_style": logo_styles.index(saved_logo),
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
                {"key": "logo_style", "label": "Logo Style:", "kind": "segmented",
                 "options": seg_options, "default": defaults["logo_style"],
                 "on_change": _preview},
                {"key": "pac_port", "label": "PAC Server Port:", "kind": "text",
                 "default": defaults["pac_port"],
                 "hint": "auto (0)"},
            ]

            result = _show_form_dialog("Settings", fields, ok_title="Save", cancel_title="Cancel")
            if result is None:
                # Revert any preview
                self.update_icon(self.state)
                return

            # Refresh defaults so a re-show on validation failure keeps user edits.
            defaults.update(result)

            pac_text = (result.get("pac_port") or "").strip() or "0"
            try:
                pac_int = int(pac_text)
            except ValueError:
                _show_message("Invalid Port", f"'{pac_text}' is not a valid port number.")
                continue
            if not validate_port(pac_int, allow_zero=True):
                _show_message("Invalid Port", "PAC port must be 0 (auto) or between 1 and 65535.")
                continue
            if pac_int != 0 and pac_int != pac_port and not is_port_free(pac_int):
                _show_message("Port In Use", f"Port {pac_int} is already in use.")
                continue

            new_logo = logo_styles[result["logo_style"]] if 0 <= result["logo_style"] < len(logo_styles) else saved_logo

            self.manager.update_app_config(
                stop_on_quit=result["stop_on_quit"],
                ephemeral_ports=result["ephemeral_ports"],
                restore_shares_on_start=result["restore_shares"],
                logo_style=new_logo,
            )
            self.manager._reload_config()
            self.manager.config = self.manager.config.model_copy(update={"pac_server_port": pac_int})
            self.manager._save()
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
        _show_about_panel(susops.__version__, icon_state=self.state or ProcessState.RUNNING)

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
                    req = urllib.request.Request(status_url)
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        backoff = 1.0
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
                    backoff = min(backoff * 2, 30.0)

        threading.Thread(target=_listen, daemon=True, name="susops-sse-mac").start()

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        self.do_poll()
        self.schedule_poll(5)
        self._start_sse_listener()
        self._app.run()


def main() -> None:
    app = SusOpsMacTray()
    app.run()
