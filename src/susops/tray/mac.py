"""macOS tray app — rumps + PyObjC.

Requires: pip install 'susops[tray-mac]'  (rumps>=0.4)

Matches the Linux tray feature-set: single multi-field dialogs (NSAlert +
accessoryView), logo-style picker with live preview, launch-at-login,
auto-discovered browser submenu, NSPopUpButton-based pickers, native
file-open panel for share, and a custom About panel.
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

from susops.core.config import PortForward
from susops.core.ports import is_port_free, validate_port
from susops.core.types import LogoStyle, ProcessState
from susops.tray.base import AbstractTrayApp, get_icon_path, get_ssh_hosts
from susops.tray.mac_config_window import _hex_color, PALETTE

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

_main_dispatcher_cls = None
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


# Debug-server test mode. Modal alert panels are recorded and auto-answered
# instead of blocking on a modal run loop, because GUI smoke tests drive the
# window over the debug socket on the main thread. Policy: single-button
# panels answer with their only button. Multi-button panels (confirms) answer
# with CANCEL unless an explicit answer was queued via `confirm-next`, so a
# destructive default can never fire by accident. False in production.
_DEBUG_ALERT_MODE = False
_DEBUG_ALERTS: list[dict] = []         # [{"title": ..., "answered": label}]
_DEBUG_CONFIRM_QUEUE: list[str] = []   # queued "ok"/"cancel" for next confirms


def _style_dialog_button(btn, *, accent: bool) -> None:
    """ponytail: mirror the config window's button look (mac_config_window
    _styled_save_button / _styled_neutral_button) on a dialog button. accent =
    filled blue + white title (default action), else dark fill + border."""
    from Cocoa import (  # type: ignore[import]
        NSAttributedString,
        NSColor,
        NSForegroundColorAttributeName,
    )

    def _hex(h):
        return NSColor.colorWithSRGBRed_green_blue_alpha_(
            int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0, 1.0)

    btn.setBordered_(False)
    try:
        btn.setWantsLayer_(True)
        btn.layer().setCornerRadius_(6.0)
        if accent:
            try:
                fill = NSColor.controlAccentColor()
            except Exception:
                fill = _hex("0a84ff")
            btn.layer().setBackgroundColor_(fill.CGColor())
            title_color = NSColor.whiteColor()
        else:
            btn.layer().setBackgroundColor_(_hex("2a2b31").CGColor())
            btn.layer().setBorderWidth_(1.0)
            btn.layer().setBorderColor_(_hex("3f4147").CGColor())
            title_color = _hex("e8e9ed")
        btn.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            btn.title(), {NSForegroundColorAttributeName: title_color}))
    except Exception:
        pass


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
    We sidestep NSAlert entirely and build a floating NSPanel driven by
    runModalForWindow_ (the proven path).

    buttons: list of (label, response_code) — leftmost button is buttons[0].
             Idiomatic order on macOS is: rightmost = default (primary),
             leftmost = cancel. The helper places button[0] rightmost.
    default_index: which button is the default (Enter triggers it).
    cancel_index:  which button is Cancel (Esc + window-X triggers it).
                   Defaults to the last entry.

    Returns the response_code of the clicked button.
    """
    # Headless/debug escape hatch. A blocking runModalForWindow_ would
    # deadlock the test harness, so in debug mode the panel is recorded and
    # auto-answered. Single-button panels use their only button. Multi-button
    # panels answer with CANCEL unless `confirm-next` queued an explicit
    # answer, so a destructive default can never fire by accident.
    if _DEBUG_ALERT_MODE:
        eff_cancel = cancel_index if cancel_index is not None \
            else len(buttons) - 1
        if len(buttons) == 1:
            idx = 0
        elif _DEBUG_CONFIRM_QUEUE:
            answer = _DEBUG_CONFIRM_QUEUE.pop(0)
            idx = default_index if answer == "ok" else eff_cancel
        else:
            idx = eff_cancel
        idx = idx if 0 <= idx < len(buttons) else 0
        _DEBUG_ALERTS.append({"title": title, "answered": buttons[idx][0]})
        return int(buttons[idx][1])

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
    # Hard cap on the body height — beyond this we wrap the content in an
    # NSScrollView so the panel stays usable even for log dumps with hundreds
    # of lines.
    MSG_H_MAX = 460
    # Widen the panel for content with long lines (e.g. log entries) so the
    # text doesn't have to wrap as aggressively.
    MSG_W_WIDE = 640

    # Measure the message's natural width so the panel fits the text (no dead
    # space to the right of a short message), capped at MSG_W_WIDE.
    import math
    from AppKit import NSFont, NSFontAttributeName  # type: ignore[import]
    from Foundation import NSAttributedString  # type: ignore[import]
    use_mono = "\n" in (message or "")
    font = (NSFont.monospacedSystemFontOfSize_weight_(12, 0.0) if use_mono
            else NSFont.systemFontOfSize_(13))
    lines = (message or "").split("\n")
    line_ws = [float(NSAttributedString.alloc().initWithString_attributes_(
        ln, {NSFontAttributeName: font}).size().width) for ln in lines]
    natural = max(line_ws, default=0.0)
    # No fixed floor: content_w below still keeps the panel at least as wide as
    # the buttons, so a short message fits snugly with no dead space.
    msg_w_target = min(MSG_W_WIDE, max(120, int(natural) + 8))
    wrap_lines = sum(max(1, math.ceil(w / msg_w_target)) for w in line_ws) or 1
    msg_h_natural = max(36, wrap_lines * 18 + 10)
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
    # ponytail: match the dark config window. Pin DarkAqua so the chrome +
    # system buttons render dark, and paint the body the window background
    # color (mirrors PALETTE["window"] #17181c in mac_config_window).
    try:
        from AppKit import (  # type: ignore[import]
            NSAppearance,
            NSAppearanceNameDarkAqua,
            NSColor,
        )
        panel.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua))
        content.setWantsLayer_(True)
        content.layer().setBackgroundColor_(_hex_color(PALETTE["dialog"]).CGColor())
    except Exception:
        pass
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
        btn.setTag_(int(code))
        btn.setTarget_(button_handler)
        btn.setAction_("buttonClicked:")
        _style_dialog_button(btn, accent=(idx == default_index))
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

    Only one live window exists globally: teardown pops its _LIVE_WINDOWS entry
    on close, so a non-empty registry means one is already open — reuse it
    (raise + refocus) instead of spawning a second.
    """
    if _LIVE_WINDOWS:
        entry = next(iter(_LIVE_WINDOWS.values()))
        panel = entry["panel"]
        panel.setTitle_(title or "Logs")
        panel.makeKeyAndOrderFront_(None)
        try:
            panel.orderFrontRegardless()
        except Exception:
            pass
        try:
            from AppKit import NSApplication  # type: ignore[import]
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        return

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


def _pick_save_path() -> str | None:
    """Show an NSSavePanel and return the chosen destination path or None.
    Same activation-policy + floating-level handling as _pick_file so the
    panel comes up focused on a menu-extra app."""
    from AppKit import NSFloatingWindowLevel, NSModalResponseOK  # type: ignore[import]
    from Cocoa import NSSavePanel  # type: ignore[import]
    panel = NSSavePanel.savePanel()
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
    url = panel.URL()
    if url is None:
        return None
    return str(url.path())


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
        # Wrap in try-catch to handle the case where the login item already exists.
        # Creating a duplicate will fail; error suppression allows idempotency.
        script = (
            'try\n'
            '  tell application "System Events" to make login item '
            f'at end with properties {{path:"{path}", hidden:false}}\n'
            'end try'
        )
    else:
        # Wrap delete in try-catch to gracefully handle missing login items.
        # Error -1728 occurs when the item doesn't exist (e.g., in dev environments).
        script = (
            'try\n'
            f'  tell application "System Events" to delete login item "{name}"\n'
            'end try'
        )
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
        # Tri-state cache for launch-at-login: None until the background probe finishes.
        # Synchronous osascript would block the main thread (and can trigger a TCC prompt
        # the first time), so we never query it from the menu callback.
        self._launch_at_login_cached: bool | None = None
        self._refresh_launch_at_login_async()
        self._build_menu()
        self._register_appearance_observer()
        self._config_window = None  # created lazily by _ensure_config_window
        self._debug_server = None
        debug_port = os.environ.get("SUSOPS_TRAY_DEBUG_PORT")
        if debug_port:
            # Auto-answer modal alerts in test mode (see _DEBUG_ALERT_MODE) so
            # GUI smoke tests driving the window over the socket don't
            # deadlock on a blocking runModalForWindow_.
            global _DEBUG_ALERT_MODE
            _DEBUG_ALERT_MODE = True
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
        """Debug-server command table (ping, dump-menu, open-about, open-config,
        select, dump-window, search, set-field, add, confirm-next, action,
        screenshot, quit). Every UI-touching handler marshals via _run_on_main.

        open-config [category]: category key (connections/domains/forwards/
            shares/settings) or omitted.
        select <category> [index]: switch nav, then select the index-th
            selectable item row in column 2.
        search [text]: set the search field string (empty clears) + filter.
        set-field <key> <value>: write a live col-3 form widget, mark dirty.
        add: trigger the current category's primary add button (enters create
            mode for connections/domains/forwards/shares).
        add-fetch: enter the fetch create form (shares' secondary button).
        confirm-next <ok|cancel>: queue the answer for the next multi-button
            panel (confirms default to cancel in debug mode otherwise).
        """

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

        def _set_state(args):
            """Force a run-state transition (no SSH) so state-driven UI like the
            Settings logo preview can be exercised deterministically."""
            if not args:
                return {"error": "usage: set-state "
                        "<running|stopped|stopped_partially|error|initial>"}
            states = {s.value.lower(): s for s in ProcessState}
            # Allow the friendly names too (enum .value may differ from these).
            states.update({
                "running": ProcessState.RUNNING,
                "stopped": ProcessState.STOPPED,
                "stopped_partially": ProcessState.STOPPED_PARTIALLY,
                "error": ProcessState.ERROR,
                "initial": ProcessState.INITIAL,
            })
            st = states.get(args[0].lower())
            if st is None:
                return {"error": f"unknown state: {args[0]}"}
            _on_main(lambda: self._on_state_change_safe(st))
            return {"ok": True, "state": st.value}

        def _logs(args):
            """Open the live logs window and report how many live windows exist
            (asserts the global singleton — a second call must reuse, not spawn)."""
            return _run_on_main(
                lambda: (_open_live_text_window("Logs", lambda: "log line", 1000),
                         {"ok": True, "count": len(_LIVE_WINDOWS)})[1])

        def _bw_render(args):
            """Push synthetic samples straight into the connection chart view and
            repaint — exercises the AppKit drawing without a live tunnel (the real
            history lives in the RPC daemon, unreachable from here). usage:
            bw-render [n]."""
            n = int(args[0]) if args else 40

            def _do():
                cw = self._config_window
                view = getattr(cw, "_bw_chart_view", None) if cw else None
                if view is None:
                    return {"error": "no bandwidth chart (select a connection)"}
                import math
                series = [[200000 + 150000 * math.sin(i / 4.0),
                           80000 + 40000 * math.sin(i / 3.0)] for i in range(n)]
                view._samples = series
                view._running = True
                view.setNeedsDisplay_(True)
                rx, tx = series[-1]
                cw._set_bw_stats(rx, tx, sum(s[0] for s in series),
                                 sum(s[1] for s in series), True)
                return {"ok": True, "samples": len(series)}
            return _run_on_main(_do)

        def _bw_dump(args):
            """Run the real repaint (RPC fetch → chart) and report its state.
            With an idle daemon, samples is 0 — proving the fetch path runs clean."""
            def _do():
                cw = self._config_window
                if cw is None or not cw.is_open():
                    return {"error": "config window not open"}
                cw.tick_bandwidth()
                view = getattr(cw, "_bw_chart_view", None)
                return {
                    "ok": True,
                    "has_chart": view is not None,
                    "samples": len(getattr(view, "_samples", [])) if view else 0,
                }
            return _run_on_main(_do)

        return {
            "ping": lambda args: {"ok": True},
            "dump-menu": lambda args: _run_on_main(
                lambda: {"menu": _menu_tree(self._app.menu)}),
            "open-about": lambda args: _run_on_main(
                lambda: (_show_about_panel(), {"ok": True})[1]),
            "open-config": lambda args: _run_on_main(
                lambda: (self._ensure_config_window().open(args[0] if args else None),
                         {"ok": True})[1]),
            "select": lambda args: (_run_on_main(
                lambda: self._ensure_config_window().select(
                    args[0],
                    int(args[1]) if len(args) > 1 else None))
                if args else {"error": "usage: select <category> [index]"}),
            "dump-window": lambda args: _run_on_main(
                lambda: self._ensure_config_window().dump()),
            "close-config": lambda args: _run_on_main(
                lambda: (self._ensure_config_window().close(), {"ok": True})[1]),
            "resize": lambda args: (_run_on_main(
                lambda: self._ensure_config_window().resize(
                    float(args[0]), float(args[1])))
                if len(args) >= 2 else {"error": "usage: resize <w> <h>"}),
            "set-col2-width": lambda args: (_run_on_main(
                lambda: self._ensure_config_window().set_col2_width(
                    float(args[0])))
                if args else {"error": "usage: set-col2-width <w>"}),
            "set-state": _set_state,
            "logs": _logs,
            "bw-render": _bw_render,
            "bw-dump": _bw_dump,
            "search": lambda args: _run_on_main(
                lambda: self._ensure_config_window().set_search(" ".join(args))),
            "set-field": lambda args: (_run_on_main(
                lambda: self._ensure_config_window().set_field(
                    args[0], " ".join(args[1:])))
                if args else {"error": "usage: set-field <key> <value…>"}),
            "set-settings-field": lambda args: (_run_on_main(
                lambda: self._ensure_config_window().set_settings_field(
                    args[0], " ".join(args[1:])))
                if args else {"error": "usage: set-settings-field <key> <value…>"}),
            "add": lambda args: _run_on_main(
                lambda: self._ensure_config_window().add()),
            "add-fetch": lambda args: _run_on_main(
                lambda: self._ensure_config_window().add_fetch()),
            "confirm-next": lambda args: (
                (_DEBUG_CONFIRM_QUEUE.append(args[0]),
                 {"ok": True, "queued": args[0]})[1]
                if args and args[0] in ("ok", "cancel")
                else {"error": "usage: confirm-next <ok|cancel>"}),
            "action": lambda args: (_run_on_main(
                lambda: (self.dispatch_window_action(
                    args[0],
                    tuple(self._ensure_config_window().selected_identity or ())),
                    {"ok": True})[1])
                if args else {"error": "usage: action <action_id>"}),
            "screenshot": _screenshot,
            "quit": _quit,
        }

    def _debug_target_window(self):
        """Window the screenshot command captures: the config window when open,
        else any open About or live panel."""
        cw = getattr(self, "_config_window", None)
        if cw is not None and cw.is_open():
            return cw.window
        for store in (_ABOUT_WINDOWS, _LIVE_WINDOWS):
            for entry in store.values():
                return entry["panel"]
        return None

    # ------------------------------------------------------------------ #
    # Config window
    # ------------------------------------------------------------------ #

    def _ensure_config_window(self):
        if self._config_window is None:
            from susops.tray.mac_config_window import ConfigWindow
            self._config_window = ConfigWindow(self)
        return self._config_window

    def _show_config_window(self, tab: str | None = None) -> None:
        def _open():
            self._ensure_config_window().open(tab)
        _on_main(_open)

    def dispatch_window_action(self, action_id: str, identity: tuple) -> None:
        """Map a detail-pane action id onto the corresponding do_* method.

        v2: conn_tag is read from the identity tuple, never window state.
          ("connection", tag)
          ("domain", conn_tag, host)
          ("forward", conn_tag, direction, src_port)
          ("share", port)
        Destructive actions confirm first. *.save routes to save_window_form
        with the live form values. *.create and fetch.run route to
        create_window_item using the window's live form values."""
        # *.create is dispatched through the window's create handler, which
        # reads _create_kind + the live form values, so it has no identity.
        if action_id.endswith(".create"):
            cw = getattr(self, "_config_window", None)
            if cw is None or cw._create_kind is None:
                return
            self.create_window_item(cw._create_kind, cw.collect_form_values())
            return

        # Settings-pane actions carry no identity (col 2 is hidden). Apply
        # commits ALL staged settings at once (toggles + logo + login + ports);
        # nothing persists until then.
        if action_id in ("settings.apply", "settings.apply_ports"):
            cw = getattr(self, "_config_window", None)
            if cw is None:
                return
            self.apply_all_settings(cw._settings_values())
            return
        if action_id == "settings.open_config":
            self.do_open_config_file()
            return

        kind = identity[0] if identity else None
        if kind is None:
            return

        # Route *.save through save_window_form with the live form values
        # collected from the window.
        if action_id.endswith(".save"):
            cw = getattr(self, "_config_window", None)
            values = cw.collect_form_values() if cw is not None else {}
            self.save_window_form(identity, values)
            return

        if action_id == "fetch.run":
            cw = getattr(self, "_config_window", None)
            if cw is None or cw._create_kind != "fetch":
                return
            self.create_window_item("fetch", cw.collect_form_values())
            return

        if kind == "connection":
            conn_tag = identity[1]
            if action_id == "conn.start":
                self.do_start_connection(conn_tag)
            elif action_id == "conn.stop":
                self.do_stop_connection(conn_tag)
            elif action_id == "conn.restart":
                self.do_restart_connection(conn_tag)
            elif action_id == "conn.test":
                self.do_test_connection(conn_tag)
            elif action_id == "conn.toggle":
                self.do_toggle_connection_enabled(conn_tag)
            elif action_id == "conn.remove":
                if _show_confirm("Delete Connection",
                                 f"Delete connection '{conn_tag}' and all its "
                                 f"domains, forwards and shares?", ok="Delete"):
                    self.do_remove_connection(conn_tag)
        elif kind == "domain":
            _, conn_tag, host = identity
            if action_id == "domain.test":
                self.do_test_domain(host, conn_tag)
            elif action_id == "domain.toggle":
                self.do_toggle_pac_host_enabled(host)
            elif action_id == "domain.remove":
                if _show_confirm("Delete Domain", f"Delete '{host}'?", ok="Delete"):
                    self.do_remove_pac_host(host)
        elif kind == "forward":
            _, conn_tag, direction, src_port = identity
            if action_id == "forward.test":
                self.do_test_forward(conn_tag, src_port, direction)
            elif action_id == "forward.toggle":
                self.do_toggle_forward_enabled(conn_tag, src_port, direction)
            elif action_id == "forward.remove":
                if _show_confirm("Delete Forward",
                                 f"Delete :{src_port} ({direction})?", ok="Delete"):
                    if direction == "local":
                        self.do_remove_local_forward(src_port)
                    else:
                        self.do_remove_remote_forward(src_port)
        elif kind == "share":
            port = identity[1]
            info = next((s for s in self.manager.list_shares()
                         if s.port == port), None)
            if info is None:
                pass  # vanished; refresh below handles it
            elif action_id == "share.toggle":
                # Flip serving on/off. Decide by the live in-memory `running`
                # state (reliable) rather than the persisted `stopped` flag,
                # which a concurrent list_shares poll can clobber at the daemon
                # (facade thread-safety bug, tracked separately): currently
                # serving -> stop; not serving -> re-share. Merges the old
                # share.stop/share.start.
                if info.running:
                    self.do_stop_share(port)
                elif not info.conn_tag:
                    self.show_alert("Cannot Start Share",
                                    "This share has no connection configured.")
                else:
                    self._reserve_share_silent(info)
            elif action_id == "share.stop":
                self.do_stop_share(port)
            elif action_id == "share.start":
                if not info.conn_tag:
                    self.show_alert("Cannot Start Share",
                                    "This share has no connection configured.")
                else:
                    self._reserve_share_silent(info)
            elif action_id == "share.delete":
                if _show_confirm("Delete Share",
                                 f"Delete share on port {port}?", ok="Delete"):
                    self.do_delete_share(port)
            elif action_id == "share.copy_url":
                self._copy_to_pasteboard(f"http://localhost:{port}")
            elif action_id == "share.copy_password":
                self._copy_to_pasteboard(info.password or "")
            elif action_id == "share.open_folder":
                try:
                    from AppKit import NSWorkspace, NSURL  # type: ignore[import]

                    p = Path(str(info.file_path)).expanduser()
                    if p.exists():
                        u = NSURL.fileURLWithPath_(str(p))
                        NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([u])
                    else:
                        self.show_alert("File Not Found", str(p))
                except Exception as e:
                    self.show_alert("Could Not Open Folder", str(e))
        self._refresh_config_window()

    def _reserve_share_silent(self, info) -> None:
        """Re-serve a share from the config window with NO success popup.

        The window reflects the new state on the next refresh, so a "Share
        Started" alert is redundant noise here. Goes straight to
        manager.share on a worker (NOT do_share, which alerts and the Linux
        tray relies on). Errors still alert.
        """
        conn_tag = info.conn_tag
        file_path = info.file_path
        password = info.password
        port = info.port

        def _work():
            try:
                self.manager.share(Path(file_path), conn_tag,
                                   password=password or None,
                                   port=port or None)
                return None
            except Exception as exc:
                return str(exc)

        def _done(err):
            if err:
                self.show_alert("Share Failed", err)
            self._refresh_config_window()

        self.run_in_background(_work, _done)

    # ------------------------------------------------------------------ #
    # Inline-edit save. Validation lives here (single place). Remove+re-add
    # with rollback runs on a worker thread. Alerts, refresh and reselect
    # marshal back to the main thread. On any error the form stays dirty.
    # ------------------------------------------------------------------ #

    _DIRECTION_FROM_LABEL = {"Local (-L)": "local", "Remote (-R)": "remote"}

    def save_window_form(self, identity: tuple, values: dict) -> None:
        kind = identity[0] if identity else None
        if kind == "connection":
            self._save_connection(identity, values)
        elif kind == "forward":
            self._save_forward(identity, values)
        elif kind == "domain":
            self._save_domain(identity, values)
        elif kind == "share":
            self._save_share(identity, values)

    def _clear_window_dirty_and_reselect(self, new_identity: tuple) -> None:
        """Main-thread: clear the dirty flag and reselect the new identity so
        the freshly-saved item is shown."""
        cw = getattr(self, "_config_window", None)
        if cw is None:
            return
        cw._dirty = False
        cw._dirty_identity = None
        cw.selected_identity = tuple(new_identity)
        self._refresh_config_window()

    # ------------------------------------------------------------------ #
    # Inline create. Pure-input validation lives here (single place),
    # mirroring save_window_form. The add runs on a worker thread. Alerts,
    # refresh and reselect marshal back to the main thread. On any error the
    # create form stays open with its edits.
    # ------------------------------------------------------------------ #

    def create_window_item(self, kind: str, values: dict) -> None:
        if kind == "connection":
            self._create_connection(values)
        elif kind == "domain":
            self._create_domain(values)
        elif kind == "forward":
            self._create_forward(values)
        elif kind == "share":
            self._create_share(values)
        elif kind == "fetch":
            self._run_fetch(values)

    def _exit_create_and_select(self, new_identity: tuple) -> None:
        """Main-thread: leave create mode, refresh, and select the new row by
        identity. No success alert - the selected new row is the feedback."""
        cw = getattr(self, "_config_window", None)
        if cw is None:
            return
        cw._create_kind = None
        cw._dirty = False
        cw._dirty_identity = None
        cw.selected_identity = tuple(new_identity)
        self._refresh_config_window()

    def _create_connection(self, values: dict) -> None:
        tag = (values.get("tag") or "").strip()
        ssh_host = (values.get("ssh_host") or "").strip()
        port_text = str(values.get("socks_port") or "").strip()
        if not tag:
            _show_message("Missing Field", "Connection Tag must not be empty.")
            return
        if not ssh_host:
            _show_message("Missing Field", "SSH Host must not be empty.")
            return
        port_int = 0
        if port_text:
            if not port_text.isdigit() or not validate_port(int(port_text)):
                _show_message("Invalid Port",
                              "SOCKS Proxy Port must be between 1 and 65535.")
                return
            port_int = int(port_text)
            if not is_port_free(port_int):
                _show_message("Port In Use",
                              f"Port {port_int} is already in use.")
                return

        new_identity = ("connection", tag)
        # Auto-start the new connection when the proxy is already running,
        # matching do_add_connection's behavior (but without its alert).
        autostart = self.state == ProcessState.RUNNING

        def _work():
            try:
                self.manager.add_connection(tag, ssh_host, port_int)
            except Exception as exc:
                return {"error": str(exc)}
            if autostart:
                try:
                    self.manager.start(tag=tag)
                except Exception as exc:
                    return {"error": f"Connection added but failed to "
                                     f"start: {exc}"}
            return {"ok": True}

        self.run_in_background(_work,
                               lambda r: self._after_create(r, new_identity))

    def _create_domain(self, values: dict) -> None:
        host = (values.get("host") or "").strip()
        conn_tag = (values.get("conn_tag") or "").strip()
        if not host:
            _show_message("Missing Field", "Host must not be empty.")
            return
        if not conn_tag:
            _show_message("Missing Field", "Select a connection.")
            return

        new_identity = ("domain", conn_tag, host)

        def _work():
            try:
                self.manager.add_pac_host(host, conn_tag=conn_tag)
            except Exception as exc:
                return {"error": str(exc)}
            return {"ok": True}

        self.run_in_background(_work,
                               lambda r: self._after_create(r, new_identity))

    def _create_forward(self, values: dict) -> None:
        conn_tag = (values.get("conn_tag") or "").strip()
        dir_label = values.get("direction") or ""
        direction = self._DIRECTION_FROM_LABEL.get(dir_label, "local")
        remote = direction == "remote"
        src_addr = (values.get("src_addr") or "localhost").strip()
        dst_addr = (values.get("dst_addr") or "localhost").strip()
        src_txt = str(values.get("src_port") or "").strip()
        dst_txt = str(values.get("dst_port") or "").strip()
        protocols = values.get("protocols") or (False, False)
        tcp, udp = bool(protocols[0]), bool(protocols[1])
        tag = (values.get("tag") or "").strip()

        if not conn_tag:
            _show_message("No Connection", "Select a connection.")
            return
        if not tcp and not udp:
            _show_message("Protocol Required",
                          "Select at least one protocol (TCP or UDP).")
            return
        if not src_txt.isdigit() or not validate_port(int(src_txt)):
            _show_message("Invalid Source Port",
                          "Source port must be a number between 1 and 65535.")
            return
        if not dst_txt.isdigit() or not validate_port(int(dst_txt)):
            _show_message("Invalid Destination Port",
                          "Destination port must be a number between 1 and "
                          "65535.")
            return
        src_port = int(src_txt)
        dst_port = int(dst_txt)
        if not remote and not is_port_free(src_port):
            _show_message("Port In Use",
                          f"Local port {src_port} is already in use.")
            return

        new_identity = ("forward", conn_tag, direction, src_port)

        def _work():
            fw = PortForward(tag=tag, src_addr=src_addr, src_port=src_port,
                             dst_addr=dst_addr, dst_port=dst_port,
                             tcp=tcp, udp=udp)
            try:
                self._add_forward_rpc(conn_tag, fw, direction)
            except Exception as exc:
                return {"error": str(exc)}
            return {"ok": True}

        self.run_in_background(_work,
                               lambda r: self._after_create(r, new_identity))

    def _create_share(self, values: dict) -> None:
        from pathlib import Path
        file_path = (values.get("file") or "").strip()
        conn_tag = (values.get("conn_tag") or "").strip()
        password = (values.get("password") or "").strip()
        port_text = str(values.get("port") or "").strip()
        if not file_path:
            _show_message("Missing Field", "Choose a file to share.")
            return
        if not conn_tag:
            _show_message("Missing Field", "Select a connection.")
            return
        port_int = 0
        if port_text:
            if not port_text.isdigit() or not validate_port(int(port_text)):
                _show_message("Invalid Port",
                              "Port must be between 1 and 65535.")
                return
            port_int = int(port_text)

        def _work():
            try:
                info = self.manager.share(
                    Path(file_path), conn_tag,
                    password=password or None,
                    port=port_int or None)
            except Exception as exc:
                return {"error": str(exc)}
            return {"ok": True, "port": info.port}

        def _done(result):
            if isinstance(result, dict) and result.get("error"):
                _show_message("Share Failed", result["error"])
                self._refresh_config_window()
                return
            self._exit_create_and_select(("share", result["port"]))

        self.run_in_background(_work, _done)

    def _run_fetch(self, values: dict) -> None:
        from pathlib import Path
        conn_tag = (values.get("conn_tag") or "").strip()
        password = (values.get("password") or "").strip()
        port_text = str(values.get("port") or "").strip()
        outfile = (values.get("output") or "").strip()
        if not conn_tag:
            _show_message("Missing Field", "Select a connection.")
            return
        if not port_text:
            _show_message("Missing Field", "Port is required.")
            return
        if not port_text.isdigit() or not validate_port(int(port_text)):
            _show_message("Invalid Port",
                          "Port must be between 1 and 65535.")
            return
        if not password:
            _show_message("Missing Field", "Password must not be empty.")
            return
        port_int = int(port_text)

        def _work():
            try:
                out = Path(outfile) if outfile else None
                path = self.manager.fetch(
                    port=port_int, password=password,
                    conn_tag=conn_tag, outfile=out)
            except Exception as exc:
                return {"error": str(exc)}
            return {"ok": True, "path": str(path)}

        def _done(result):
            if isinstance(result, dict) and result.get("error"):
                _show_message("Fetch Failed", result["error"])
                self._refresh_config_window()
                return
            # Fetch is an action, not a persistent item: leave create mode back
            # to the shares list and report the saved path.
            cw = getattr(self, "_config_window", None)
            if cw is not None:
                cw.exit_create_mode()
            _show_message("Download Complete", f"Saved to {result['path']}")
            self._refresh_config_window()

        self.run_in_background(_work, _done)

    def _after_create(self, result, new_identity: tuple) -> None:
        """Main-thread create callback. Error -> alert, form stays open with its
        edits. Success -> exit create mode + select the new row."""
        if isinstance(result, dict) and result.get("error"):
            _show_message("Add Failed", result["error"])
            self._refresh_config_window()
            return
        self._exit_create_and_select(new_identity)

    def _connection_tags(self) -> list:
        try:
            return [c.tag for c in self.manager.list_config().connections]
        except Exception:
            return []

    def _save_connection(self, identity: tuple, values: dict) -> None:
        old_tag = identity[1]
        tag = (values.get("tag") or "").strip()
        ssh_host = (values.get("ssh_host") or "").strip()
        port_text = str(values.get("socks_port") or "").strip()
        # Pure-input validation on the main thread (no RPC).
        if not tag:
            _show_message("Missing Field", "Connection Tag must not be empty.")
            return
        if not ssh_host:
            _show_message("Missing Field", "SSH Host must not be empty.")
            return
        port_int = 0
        if port_text:
            if not port_text.isdigit() or not validate_port(int(port_text)):
                _show_message("Invalid Port",
                              "SOCKS port must be empty (auto) or 1–65535.")
                return
            port_int = int(port_text)

        new_identity = ("connection", tag)

        def _work():
            # update_connection edits in place (preserving forwards/domains/
            # shares) and, when the connection was running, restarts it under
            # the new config. was_running + restart are handled in the facade.
            try:
                self.manager.update_connection(
                    old_tag, new_tag=tag, ssh_host=ssh_host,
                    socks_proxy_port=port_int, restart=True)
                return {"ok": True}
            except Exception as exc:
                return {"error": str(exc)}

        def _done(result):
            if isinstance(result, dict) and result.get("error"):
                _show_message("Save Failed", result["error"])
                self._refresh_config_window()
                return
            self._clear_window_dirty_and_reselect(new_identity)

        self.run_in_background(_work, _done)

    def _save_forward(self, identity: tuple, values: dict) -> None:
        _, old_conn_tag, old_direction, old_src_port = identity
        # Pure-input validation on the main thread (no RPC).
        new_conn_tag = (values.get("conn_tag") or old_conn_tag).strip()
        dir_label = values.get("direction") or ""
        new_direction = self._DIRECTION_FROM_LABEL.get(dir_label, old_direction)
        src_addr = (values.get("src_addr") or "localhost").strip()
        dst_addr = (values.get("dst_addr") or "localhost").strip()
        src_txt = str(values.get("src_port") or "").strip()
        dst_txt = str(values.get("dst_port") or "").strip()
        protocols = values.get("protocols") or (False, False)
        tcp, udp = bool(protocols[0]), bool(protocols[1])
        tag = (values.get("tag") or "").strip()

        if not src_txt.isdigit() or not validate_port(int(src_txt)):
            _show_message("Invalid Source Port",
                          "Source port must be a number between 1 and 65535.")
            return
        if not dst_txt.isdigit() or not validate_port(int(dst_txt)):
            _show_message("Invalid Destination Port",
                          "Destination port must be a number between 1 and 65535.")
            return
        new_src_port = int(src_txt)
        new_dst_port = int(dst_txt)
        if not tcp and not udp:
            _show_message("No Protocol",
                          "Enable at least one protocol (TCP or UDP).")
            return
        # A locally-bound src port must be free when it changed (or the
        # direction changed to local) to avoid colliding with another listener.
        port_changed = (new_src_port != old_src_port
                        or new_direction != old_direction)
        if new_direction == "local" and port_changed and not is_port_free(new_src_port):
            _show_message("Port In Use",
                          f"Local port {new_src_port} is already in use.")
            return

        new_identity = ("forward", new_conn_tag, new_direction, new_src_port)

        def _work():
            # Config lookup is blocking RPC, so it runs here on the worker.
            # The old forward is captured so rollback can restore it verbatim.
            old_fw = self._find_forward(old_conn_tag, old_direction,
                                        old_src_port)
            if old_fw is None:
                return {"error": "The forward no longer exists. "
                                 "Reopen the window."}
            new_fw = PortForward(tag=tag, src_addr=src_addr,
                                 src_port=new_src_port, dst_addr=dst_addr,
                                 dst_port=new_dst_port, tcp=tcp, udp=udp,
                                 enabled=bool(old_fw.enabled))
            try:
                self._remove_forward_rpc(old_direction, old_src_port)
            except Exception as exc:
                return {"error": f"Could not remove old forward: {exc}"}
            try:
                self._add_forward_rpc(new_conn_tag, new_fw, new_direction)
            except Exception as exc:
                # Rollback: re-add the original forward to its old connection.
                # A rollback failure is reported, never swallowed.
                try:
                    self._add_forward_rpc(old_conn_tag, old_fw, old_direction)
                except Exception as exc2:
                    return {"error": f"Could not save forward: {exc}. "
                                     f"WARNING: the original forward could "
                                     f"not be restored: {exc2}"}
                return {"error": f"Could not save forward: {exc}"}
            return {"ok": True}

        def _done(result):
            if isinstance(result, dict) and result.get("error"):
                _show_message("Save Failed", result["error"])
                self._refresh_config_window()
                return
            self._clear_window_dirty_and_reselect(new_identity)

        self.run_in_background(_work, _done)

    def _save_domain(self, identity: tuple, values: dict) -> None:
        _, old_conn_tag, old_host = identity
        new_host = (values.get("host") or "").strip()
        new_conn_tag = (values.get("conn_tag") or old_conn_tag).strip()
        if not new_host:
            _show_message("Invalid Host", "Host must not be empty.")
            return
        new_identity = ("domain", new_conn_tag, new_host)

        def _work():
            # Config lookup is blocking RPC, so it runs here on the worker.
            was_disabled = self._is_pac_host_disabled(old_conn_tag, old_host)
            try:
                self.manager.remove_pac_host(old_host, conn_tag=old_conn_tag)
            except Exception as exc:
                return {"error": f"Could not remove old domain: {exc}"}
            try:
                self.manager.add_pac_host(new_host, conn_tag=new_conn_tag)
                if was_disabled:
                    self.manager.set_pac_host_enabled(
                        new_host, False, conn_tag=new_conn_tag)
            except Exception as exc:
                # Rollback: re-add the original host to its old connection.
                # A rollback failure is reported, never swallowed.
                try:
                    self.manager.add_pac_host(old_host, conn_tag=old_conn_tag)
                    if was_disabled:
                        self.manager.set_pac_host_enabled(
                            old_host, False, conn_tag=old_conn_tag)
                except Exception as exc2:
                    return {"error": f"Could not save domain: {exc}. "
                                     f"WARNING: the original domain could "
                                     f"not be restored: {exc2}"}
                return {"error": f"Could not save domain: {exc}"}
            return {"ok": True}

        def _done(result):
            if isinstance(result, dict) and result.get("error"):
                _show_message("Save Failed", result["error"])
                self._refresh_config_window()
                return
            self._clear_window_dirty_and_reselect(new_identity)

        self.run_in_background(_work, _done)

    def _save_share(self, identity: tuple, values: dict) -> None:
        from pathlib import Path
        _, old_port = identity
        new_port_text = str(values.get("port") or "").strip()
        new_password = (values.get("password") or "").strip()
        requested_conn_tag = (values.get("conn_tag") or "").strip()
        if not new_port_text.isdigit() or not validate_port(int(new_port_text)):
            _show_message("Invalid Port",
                          "Port must be a number between 1 and 65535.")
            return
        new_port = int(new_port_text)

        def _work():
            # Config lookup is blocking RPC, so it runs here on the worker. The
            # old share is captured so rollback can re-share it verbatim.
            info = next((s for s in self.manager.list_shares()
                         if s.port == old_port), None)
            if info is None:
                return {"error": "The share no longer exists. "
                                 "Reopen the window."}
            old_pw = info.password
            file_path = info.file_path
            old_conn_tag = (info.conn_tag or "").strip()
            conn_tag = requested_conn_tag or old_conn_tag
            new_pw = new_password or old_pw
            if not conn_tag:
                return {"error": "This share has no connection configured."}
            try:
                cfg = self.manager.list_config()
                known_tags = {c.tag for c in cfg.connections}
            except Exception as exc:
                return {"error": f"Could not validate connection: {exc}"}
            if conn_tag not in known_tags:
                return {"error": f"Connection '{conn_tag}' does not exist."}
            try:
                # delete_share stops the server and removes the config entry in
                # one call (it calls stop_share internally).
                self.manager.delete_share(old_port)
            except Exception as exc:
                return {"error": f"Could not remove old share: {exc}"}
            try:
                self.manager.share(Path(file_path), conn_tag,
                                   password=new_pw, port=new_port)
            except Exception as exc:
                # Rollback: re-share the original file on its old port/password.
                # A rollback failure is reported, never swallowed.
                try:
                    self.manager.share(Path(file_path), old_conn_tag,
                                       password=old_pw, port=old_port)
                except Exception as exc2:
                    return {"error": f"Could not save share: {exc}. "
                                     f"WARNING: the original share could "
                                     f"not be restored: {exc2}"}
                return {"error": f"Could not save share: {exc}"}
            return {"ok": True}

        def _done(result):
            if isinstance(result, dict) and result.get("error"):
                _show_message("Save Failed", result["error"])
                self._refresh_config_window()
                return
            self._clear_window_dirty_and_reselect(("share", new_port))

        self.run_in_background(_work, _done)

    # ---- save helpers (RPC + config lookups) ----

    def _remove_forward_rpc(self, direction: str, src_port: int) -> None:
        if direction == "local":
            self.manager.remove_local_forward(src_port)
        else:
            self.manager.remove_remote_forward(src_port)

    def _add_forward_rpc(self, conn_tag: str, fw: PortForward,
                         direction: str) -> None:
        if direction == "local":
            self.manager.add_local_forward(conn_tag, fw)
        else:
            self.manager.add_remote_forward(conn_tag, fw)

    def _find_forward(self, conn_tag: str, direction: str, src_port: int):
        cfg = self.manager.list_config()
        conn = next((c for c in cfg.connections if c.tag == conn_tag), None)
        if conn is None:
            return None
        fws = conn.forwards.local if direction == "local" else conn.forwards.remote
        return next((f for f in fws if f.src_port == src_port), None)

    def _is_pac_host_disabled(self, conn_tag: str, host: str) -> bool:
        cfg = self.manager.list_config()
        conn = next((c for c in cfg.connections if c.tag == conn_tag), None)
        if conn is None:
            return False
        return host in (getattr(conn, "pac_hosts_disabled", []) or [])

    def pick_path_for_window(self, *, save: bool = False) -> str | None:
        """Window hook: open the file picker (NSOpenPanel) or, when save=True,
        a save panel (fetch output). Runs on the main thread - the window's
        button handler dispatch path already runs there, and the debug action
        path marshals via _run_on_main. Returns the chosen path or None."""
        return _pick_save_path() if save else _pick_file()

    def _copy_to_pasteboard(self, text: str) -> None:
        """Put text on the general pasteboard. AppKit on the main thread.
        The button-handler dispatch path already runs there and the debug
        `action` path marshals via _run_on_main, so it is safe to call
        directly."""
        from AppKit import NSPasteboard  # type: ignore[import]
        try:
            from AppKit import NSPasteboardTypeString  # type: ignore[import]
        except Exception:
            NSPasteboardTypeString = "public.utf8-plain-text"
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

    _cw_refresh_gen: int = 0         # monotonic; incremented on each request
    _cw_pending_data: tuple | None = None  # (cfg, statuses, shares) waiting to apply

    def _refresh_config_window(self) -> None:
        """Refresh the config window in the background (non-blocking).

        RPC I/O runs on a worker thread; the result is stored in
        _cw_pending_data and picked up by the next _tick_bandwidth call
        (which already runs on the main thread). This avoids any
        performSelectorOnMainThread interaction with the NSTimer.
        """
        cw = self._config_window
        if cw is None or not cw.is_open():
            return

        self._cw_refresh_gen += 1
        my_gen = self._cw_refresh_gen

        def _fetch():
            mgr = self.manager
            try:
                cfg = mgr.list_config()
            except Exception:
                return
            try:
                statuses = list(mgr.status().connection_statuses)
            except Exception:
                statuses = []
            try:
                shares = list(mgr.list_shares())
            except Exception:
                shares = []
            # Only store if no newer refresh was requested while we were fetching.
            if my_gen == self._cw_refresh_gen:
                self._cw_pending_data = (cfg, statuses, shares)

        threading.Thread(target=_fetch, daemon=True,
                         name="susops-cw-refresh").start()

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
        # Keep the Settings "Logo style" preview in sync with the run state.
        cw = getattr(self, "_config_window", None)
        if cw is not None and cw.is_open():
            try:
                cw.refresh_logo_segment_images()
            except Exception:
                pass

    def preview_logo_style(self, idx: int) -> None:
        """Live-preview a logo style by index WITHOUT persisting it. The config
        is untouched; only the menu-bar icon changes. revert_logo_preview puts
        the saved logo back if the user leaves Settings without Apply."""
        logo_styles = list(LogoStyle)
        if not (0 <= idx < len(logo_styles)):
            return
        icon_path = _get_icon_path(self.state, logo_styles[idx].value.lower())
        if icon_path:
            _on_main(lambda: self._apply_icon_path(icon_path))

    def revert_logo_preview(self) -> None:
        """Re-apply the saved logo's icon, undoing any live preview."""
        _on_main(lambda: self.update_icon(self.state))

    def update_menu_sensitivity(self, state: ProcessState) -> None:
        # Match susops-mac's per-state enablement table:
        #   RUNNING            → Start off,  Stop on,  Restart on
        #   STOPPED_PARTIALLY  → Start on,   Stop on,  Restart on
        #   STOPPED  / INITIAL → Start on,   Stop off, Restart off
        #   ERROR              → all off (recovery is via Reset)
        running_like = state in (ProcessState.RUNNING, ProcessState.STOPPED_PARTIALLY)
        start_on = state in (ProcessState.STOPPED, ProcessState.STOPPED_PARTIALLY, ProcessState.INITIAL)
        action_on = running_like  # Stop / Restart

        if hasattr(self, "_item_start"):
            self._item_start._menuitem.setEnabled_(start_on)  # type: ignore[attr-defined]
        if hasattr(self, "_item_stop"):
            self._item_stop._menuitem.setEnabled_(action_on)  # type: ignore[attr-defined]
        if hasattr(self, "_item_restart"):
            self._item_restart._menuitem.setEnabled_(action_on)  # type: ignore[attr-defined]
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

        # Launch Browser submenu — built dynamically from installed apps
        self._browser_menu = rumps.MenuItem("Launch Browser")
        self._rebuild_browser_submenu()

        self._app.menu = [
            self._item_status,
            None,
            rumps.MenuItem("Settings…", callback=lambda _: self._show_config_window(), key=","),
            None,
            self._item_start,
            self._item_stop,
            self._item_restart,
            None,
            rumps.MenuItem("Show Status", callback=lambda _: self.do_status()),
            rumps.MenuItem("Show Logs", callback=lambda _: self.do_logs()),
            self._browser_menu,
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

    def do_poll(self) -> None:
        super().do_poll()
        self._refresh_config_window()

    # ------------------------------------------------------------------ #
    # Settings dialog (with live logo preview)
    # ------------------------------------------------------------------ #

    def _settings_fields(self) -> tuple[list[dict], dict]:
        """Build the app-settings field spec + a context dict.

        Single source of truth for the settings pane (mac_config_window).
        Returns (fields, ctx): fields carry per-field section/description and
        the current saved values; ctx carries the saved port values, the
        LogoStyle list, and the saved logo so _apply_server_ports can treat
        "unchanged" ports specially and _apply_setting_toggle can resolve the
        segmented index back to a LogoStyle.
        """
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
            seg_options.append(("", img_path))

        # Initial values. Launch-at-login state is read from a
        # background-populated cache to avoid blocking the main thread on
        # osascript / TCC prompts when opening Settings.
        base = {
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

        # The settings pane (mac_config_window) renders these grouped by
        # `section`. ALL changes (toggles + logo + launch-at-login + the three
        # ports) are staged locally and persist only on the single Apply
        # button; leaving or closing without Apply discards them. `description`
        # is the indented gray explainer under a checkbox (empty = no row).
        fields = [
            {"key": "launch_at_login", "label": "Launch SusOps at login", "kind": "switch",
             "default": base["launch_at_login"], "section": "General"},
            {"key": "stop_on_quit", "label": "Stop proxy on quit", "kind": "switch",
             "default": base["stop_on_quit"], "section": "General",
             "description": "Skipped when another frontend is attached to the daemon."},
            {"key": "ephemeral_ports", "label": "Random SSH ports on start", "kind": "switch",
             "default": base["ephemeral_ports"], "section": "General",
             "description": "Pick a free SOCKS port each start instead of the saved one."},
            {"key": "restore_shares", "label": "Restore shares on start", "kind": "switch",
             "default": base["restore_shares"], "section": "General"},
            {"key": "show_bandwidth", "label": "Show bandwidth", "kind": "switch",
             "default": base["show_bandwidth"], "section": "Menu bar",
             "description": "Show live throughput beside the menu-bar icon."},
            {"key": "notifications", "label": "Desktop notifications", "kind": "switch",
             "default": base["notifications"], "section": "Menu bar"},
            {"key": "logo_style", "label": "Logo style", "kind": "segmented",
             "options": seg_options, "default": base["logo_style"],
             "section": "Menu bar"},
            # Server ports — RPC + SSE require a daemon restart to take
            # effect; PAC is hot-restarted by the facade.
            {"key": "rpc_port", "label": "RPC port", "kind": "text",
             "default": base["rpc_port"], "section": "Servers",
             "placeholder": "auto", "note": "restart daemon to apply"},
            {"key": "sse_port", "label": "SSE port", "kind": "text",
             "default": base["sse_port"], "section": "Servers",
             "placeholder": "auto", "note": "restart daemon to apply"},
            {"key": "pac_port", "label": "PAC port", "kind": "text",
             "default": base["pac_port"], "section": "Servers",
             "placeholder": "auto"},
        ]
        ctx = {
            "rpc_port": rpc_port,
            "sse_port": sse_port,
            "pac_port": pac_port,
            "logo_styles": logo_styles,
            "saved_logo": saved_logo,
        }
        return fields, ctx

    # App-config field key (from _settings_fields) -> SusOpsApp field name.
    # launch_at_login and logo_style are handled separately (login-item thread,
    # LogoStyle resolution).
    _TOGGLE_APP_CONFIG_KEYS = {
        "stop_on_quit": "stop_on_quit",
        "ephemeral_ports": "ephemeral_ports",
        "restore_shares": "restore_shares_on_start",
        "show_bandwidth": "tray_show_bandwidth",
        "notifications": "notifications_enabled",
    }

    def _apply_setting_toggle(self, key: str, value) -> str | None:
        """Persist ONE settings toggle/logo/login value. Returns an error
        string on failure, else None. Called per-field by apply_all_settings
        when the user clicks Apply (settings are staged until then).

        Single update_app_config kwarg for the boolean toggles; logo_style
        resolves the segmented index back to a LogoStyle and repaints the icon;
        launch_at_login keeps its osascript background thread (may block on a
        first-run TCC prompt). No port validation here — ports go through
        _apply_server_ports behind the explicit Apply button.
        """
        try:
            if key == "launch_at_login":
                desired = bool(value)
                self._launch_at_login_cached = desired  # optimistic
                threading.Thread(
                    target=lambda: _set_launch_at_login(desired),
                    daemon=True,
                    name="susops-loginitem-apply",
                ).start()
                return None
            if key == "logo_style":
                logo_styles = list(LogoStyle)
                idx = int(value)
                if not (0 <= idx < len(logo_styles)):
                    return "Invalid logo style."
                self.manager.update_app_config(logo_style=logo_styles[idx])
                # update_icon -> _apply_icon_path touches NSImage + the rumps
                # app.icon setter, which must run on the main thread (this
                # method runs on a run_in_background worker).
                _on_main(lambda: self.update_icon(self.state))
                return None
            field = self._TOGGLE_APP_CONFIG_KEYS.get(key)
            if field is None:
                return f"Unknown setting '{key}'."
            self.manager.update_app_config(**{field: bool(value)})
            return None
        except Exception as exc:
            return str(exc)

    def _validate_server_ports(self, rpc, sse, pac, ctx: dict):
        """Validate the three server ports WITHOUT writing. Returns
        (error_string, None) on failure, else (None, port_ints). Port
        validation is verbatim from the old all-at-once path."""
        current = {
            "RPC": ctx.get("rpc_port", 0),
            "SSE": ctx.get("sse_port", 0),
            "PAC": ctx.get("pac_port", 0),
        }
        port_ints: dict[str, int] = {}
        for label, raw in (("RPC", rpc), ("SSE", sse), ("PAC", pac)):
            raw = (str(raw or "")).strip() or "0"
            try:
                n = int(raw)
            except ValueError:
                return f"'{raw}' is not a valid {label} port.", None
            if not validate_port(n, allow_zero=True):
                return (f"{label} port must be 0 (auto) or between 1 and "
                        f"65535."), None
            if n != 0 and n != current[label] and not is_port_free(n):
                return f"Port {n} is already in use ({label}).", None
            port_ints[label] = n
        return None, port_ints

    def _apply_server_ports(self, rpc, sse, pac, ctx: dict) -> str | None:
        """Validate + persist the three server ports behind the explicit Apply.
        Returns an error string on validation failure (caller shows an alert),
        else None."""
        err, port_ints = self._validate_server_ports(rpc, sse, pac, ctx)
        if err:
            return err
        try:
            self.manager.update_config(
                rpc_server_port=port_ints["RPC"],
                status_server_port=port_ints["SSE"],
                pac_server_port=port_ints["PAC"],
            )
        except Exception as exc:
            return str(exc)
        return None

    # ------------------------------------------------------------------ #
    # Settings pane hooks (called by mac_config_window). The pane renders
    # the field specs; these wrappers own validation + persistence so the
    # single-source-of-truth rule stays (spec + validation in mac.py).
    # ------------------------------------------------------------------ #

    def settings_field_specs(self) -> tuple[list[dict], dict]:
        """(fields, ctx) for the settings pane. Re-reads current config so the
        pane always opens with live values."""
        return self._settings_fields()

    # Settings field keys that are staged toggles/logo/login (everything that
    # is not a server port). Applied per-field via _apply_setting_toggle.
    _SETTINGS_TOGGLE_KEYS = (
        "launch_at_login", "logo_style", "stop_on_quit", "ephemeral_ports",
        "restore_shares", "show_bandwidth", "notifications",
    )

    def apply_all_settings(self, values: dict) -> None:
        """Commit ALL staged settings at once behind the single Apply button:
        toggles + logo + launch-at-login via _apply_setting_toggle, the three
        server ports via the validated _apply_server_ports. Nothing in the pane
        persists until this runs.

        Port validation runs FIRST so an invalid port keeps the whole pane and
        nothing is partially applied. Toggles are written one by one after that
        and are NOT rolled back if a later toggle's RPC raises (each is an
        independent app_config field and update_app_config rarely fails). On
        success the pane re-renders with the saved values and the staging dirty
        flag clears. Runs on a worker thread (RPCs block), alerts + re-render
        marshal back to the main thread."""
        _, ctx = self._settings_fields()
        rpc = values.get("rpc_port", "")
        sse = values.get("sse_port", "")
        pac = values.get("pac_port", "")

        def _work():
            # Validate ports up front and bail before ANY write on error so the
            # apply is all-or-nothing (no partial commit on a bad port).
            verr, _ = self._validate_server_ports(rpc, sse, pac, ctx)
            if verr:
                return verr
            for key in self._SETTINGS_TOGGLE_KEYS:
                if key not in values:
                    continue
                err = self._apply_setting_toggle(key, values[key])
                if err:
                    return err
            # Ports already validated above; persist them.
            return self._apply_server_ports(rpc, sse, pac, ctx)

        def _done(err):
            cw = getattr(self, "_config_window", None)
            if err:
                if cw is not None:
                    cw.mark_settings_save_failed()
                self.show_alert("Could Not Apply Settings", err)
                return
            if cw is not None:
                cw.clear_settings_dirty()
                cw.mark_settings_saved()
                cw.refresh()
        self.run_in_background(_work, _done)

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
                                    _on_main(self._refresh_config_window)
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

    _cw_tick_count: int = 0

    def _tick_bandwidth(self, _timer=None) -> None:
        self.refresh_bandwidth_title()
        cw = self._config_window
        if cw is not None and cw.is_open():
            try:
                cw.tick_bandwidth()
            except Exception:
                pass
        self._cw_tick_count += 1
        # Apply any pending config-window data fetched in the background.
        pending = self._cw_pending_data
        if pending is not None:
            self._cw_pending_data = None
            cw = self._config_window
            if cw is not None and cw.is_open():
                try:
                    cw._apply_data(*pending)
                except Exception:
                    pass
        # Kick off the next background fetch every ~2 s (every other tick).
        if self._cw_tick_count % 2 == 0:
            self._refresh_config_window()

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
