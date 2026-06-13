# macOS Tray — Unified Config Window

**Date:** 2026-06-12
**Status:** Approved (design + layout confirmed by user against hand-drawn mockup)
**Layout reference:** hand-drawn "Config Screen" mockup — per-connection tabs, grouped sidebar,
detail panel. (A Tailscale-style flat-sidebar variant was considered and rejected in favor of the
mockup layout.)
**Scope:** macOS tray only. Linux GTK parity is a follow-up. TUI/CLI untouched.

## Goal

Replace the tray's scattered modal dialogs (Settings form + Add/Remove/Manage/Test/File-Transfer
submenus, ~14 pick-and-form dialogs) with one unified, non-modal config window matching the
mockup: one tab per connection, a sidebar listing that connection's domains/forwards/shares, and a
detail panel for the selected item. Slim the tray menu down accordingly. Build an agent-drivable
screenshot/feedback loop first so every UI change can be self-verified without manual click-through.

## Decisions (locked with user)

| Decision | Choice |
|---|---|
| Tech stack | Native AppKit via PyObjC (no new dependencies). rumps 0.4.0 verified: menu-only API plus a one-text-field modal `Window` — the config window must be raw AppKit |
| Layout | Mockup layout: per-connection tabs + grouped sidebar + detail panel (user confirmed over Tailscale-style flat sidebar) |
| File sharing | Moves into the window (Shares sidebar group + Share/Fetch in the Add… menu) |
| App settings | Gear (⚙) tab inside the window; separate Settings dialog removed |
| Test actions | Move into detail panes; Test submenu removed |
| About | Standalone About panel stays as-is |
| Platform scope | macOS first; `AbstractTrayApp.do_*` stays shared so Linux keeps working |
| Editing model (v1) | Add/remove/toggle only — facade has no field-update methods today; inline editing is optional Phase 3 |

## Architecture

### New files

- `src/susops/tray/mac_config_window.py` — the window (mac.py is 3,115 lines; window adds ~1,000+).
- `src/susops/tray/debug_server.py` — opt-in debug command socket (platform-neutral core).

### Changed files

- `src/susops/tray/mac.py` — slim menu, open-window action, remove dead dialog paths (Phase 2).
- `src/susops/tray/base.py` — `SUSOPS_TRAY_WORKSPACE` env override; hook for per-poll window refresh.

### Window lifecycle

Non-modal, resizable NSWindow (~900×560 initial), title "SusOps Settings". Singleton: reopening
orders the existing window front (optionally jumping to a requested tab). Reuses the proven
patterns from `_open_live_text_window` / `_show_about_panel`:

- held-open `_RegularPolicyScope` for the window's lifetime (released on close),
- close via titlebar X through a window delegate (module-level cached NSObject subclasses — never
  define NSObject subclasses inside functions; see the PyObjC re-registration bug note in mac.py),
- all AppKit access on the main thread via `_on_main`; blocking work via `run_in_background`.

### AppKit component mapping (all verified PyObjC-reachable)

| UI element | Class |
|---|---|
| Per-connection tab strip | `NSTabView` with delegate intercept for the `+` tab (or `NSSegmentedControl`-driven view swap if the intercept proves unreliable — implementer's choice, behavior below is the contract) |
| Sidebar | `NSOutlineView`, source-list style, group rows + custom cell (status dot + label) inside `NSSplitView` |
| Add… button | pull-down `NSPopUpButton` |
| Detail label/value rows | `NSGridView` (or the manual-frame layout mac.py's dialogs already use) |
| Item state dots | colored dot images (reuse `assets/icons/status/*.svg` pattern) |
| Action buttons | `NSButton` — same as existing dialogs |

### Layout

```
┌──────────────────────────────────────────────────────┐
│ [● work] [○ home] [+]                            [⚙] │  ← tab strip
├───────────────┬──────────────────────────────────────┤
│ DOMAINS       │  Detail panel (swaps with selection) │
│   blabla.de   │                                      │
│   10.0.0.0/8  │  e.g. domain selected:               │
│ FORWARDS      │    Config for blabla.de              │
│   L :5432→…   │    Connection   work                 │
│   R :8080→…   │    Enabled      [✓]                  │
│ SHARES        │    [Test] [Remove]                   │
│   ● file.bin  │                                      │
│ CONNECTION    │                                      │
│   Settings    │                                      │
├───────────────┤                                      │
│ [Add… ▾]      │                                      │
└───────────────┴──────────────────────────────────────┘
```

**Tab strip.** One tab per connection, title = status dot + tag (`● work`, `○ home`; dot updates on
poll). Trailing `+` tab triggers the Add Connection flow instead of showing a pane (delegate
intercepts selection, opens the existing validated add-connection dialog, inserts + selects the new
tab on success). Gear (⚙) tab last. Tab order: connections in config order, then `+`, then ⚙.
Known limit: comfortable up to ~6–8 connections; accepted per mockup.

**Sidebar.** NSOutlineView, source-list style, ~220 px, group headers DOMAINS / FORWARDS / SHARES /
CONNECTION (always all four; an empty group simply has no rows). Items:

- Domain: `● blabla.de` (● enabled, ○ disabled via `pac_hosts_disabled`).
- Forward: `● L :5432→db:5432` / `● R :8080→localhost:8080` (direction prefix, ●/○ = enabled).
- Share: `● file.bin (44001)` — ● running, ○ dim = manually stopped, ○ red = connection down
  (matches the three-state ShareInfo semantics).
- Connection group has a single fixed item "Settings".

Default selection on tab open: Connection → Settings.

**Add… pull-down** (bottom of sidebar): Add Domain / IP / CIDR… · Add Local Forward… · Add Remote
Forward… · Share File… · Fetch File…. Each reuses the existing validated `_show_form_dialog` /
`NSOpenPanel` flows from mac.py with the connection popup pre-set to the current tab (field hidden
or disabled). Add Connection is NOT here; it's the `+` tab. This guarantees: **connections,
domains/IPs/CIDRs, local and remote forwards are all addable** (plus shares/fetch).

### Detail panes

All panes are read/display + actions in v1 (no field editing — facade limitation). Action buttons
call existing `AbstractTrayApp.do_*` methods through `run_in_background`; the pane refreshes after
the callback. Destructive actions (Remove, Delete) confirm via `_show_confirm`.

- **Connection / Settings**: tag, ssh host, SOCKS port, enabled switch (live toggle via
  `do_toggle_connection_enabled`), live status block (state dot, pid — from `manager.status()`);
  buttons: Start · Stop · Restart · Test (`do_test_connection`) · Remove Connection…. Button
  enablement follows the connection's running state (same rules as the menu).
- **Domain**: host, owning connection, enabled switch (`do_toggle_pac_host_enabled`); buttons:
  Test (`do_test_domain`) · Remove (`do_remove_pac_host`).
- **Forward**: direction, src addr:port → dst addr:port, tag, TCP/UDP flags, enabled switch
  (`do_toggle_forward_enabled`); buttons: Test (`do_test_forward`) · Remove
  (`do_remove_local_forward` / `do_remove_remote_forward`).
- **Share**: file path, port, password (hidden, "Reveal" toggle), access/failed counts, three-state
  status; buttons: Stop Share / (re-)Share · Delete (`do_stop_share` / `do_share` /
  `do_delete_share`).

### Gear (⚙) tab — app settings

Same fields + validation as the current `_show_settings_dialog`: launch at login (background-cached
probe), stop proxy on quit, random SSH ports, restore shares, show bandwidth (live preview),
desktop notifications, logo style (segmented with images + live icon preview), RPC/SSE/PAC ports
(validate_port/is_port_free, RPC+SSE hint "restart daemon to apply"). Save / Revert buttons; Save
applies via `update_app_config` + `update_config` exactly as today. Plus an **Open Config File**
button (`do_open_config_file`). Invalid input shows `_show_message` and keeps edits.

### Refresh model

- The tray's existing `do_poll` tick calls `config_window.refresh()` when the window is open
  (marshaled via `_on_main`): updates tab titles/dots, sidebar item states, and the visible detail
  pane's live status. In-place updates only — selection and scroll position must survive refresh
  (same principle as the TUI's list-refresh-without-deselection pattern).
- Structural changes (item added/removed) rebuild only the affected outline group or tab strip,
  restoring selection by identity (host string / direction+src_port / share port / tag).
- After any action initiated from the window, refresh immediately on the action's callback rather
  than waiting for the next poll.

## Unified tray menu (after Phase 2)

```
● SusOps: <state>
─────────────────
Settings…            ⌘,   ← opens the unified config window
─────────────────
Start Proxy
Stop Proxy
Restart Proxy        ⌘R
─────────────────
Show Status
Show Logs
Launch Browser       ▸
─────────────────
Reset All
─────────────────
About SusOps
Quit                 ⌘Q
```

Removed: Add ▸, Remove ▸, Manage ▸, Test ▸, File Transfer ▸ (incl. active-share entries), Open
Config File. Their menu-only dialog methods (`_show_rm_*`, `_show_toggle_*`,
`_show_start/stop/restart_connection_dialog`, `_show_test_*` pickers, `_show_settings_dialog`,
`_refresh_share_submenu`) are deleted; the add/share/fetch form dialogs are kept (reused by the
window), and `_show_about_panel` stays. Menu sensitivity rules for Start/Stop/Restart are unchanged.

## Debug / self-verification infrastructure (Phase 0)

- **`SUSOPS_TRAY_WORKSPACE`**: when set, `AbstractTrayApp.__init__` uses it instead of
  `~/.susops`. Lets a dev instance run against an isolated workspace alongside the user's real tray.
- **`SUSOPS_TRAY_DEBUG_PORT=<n>`**: when set, the tray starts a localhost-only TCP server
  (daemon thread). Newline-delimited commands, JSON-per-line responses, UI work marshaled via
  `_on_main` and awaited with a threading.Event + timeout. Commands:
  - `open-config [tab]` — open/front the window, optionally on a connection tag or `gear`.
  - `select <tag> <group> <index>` — select tab + sidebar item
    (`group` ∈ domains/forwards/shares/connection).
  - `screenshot <path>` — render the window in-process via
    `contentView.cacheDisplayInRect_toBitmapImageRep_` → PNG. No Screen Recording (TCC) permission
    required — verified that `screencapture` is currently TCC-blocked on the dev machine, which is
    why in-process rendering is the primary mechanism.
  - `dump-menu` — JSON tree of the tray menu (titles, enabled, key equivalents).
  - `dump-window` — JSON of window state (tabs, sidebar items, selection, detail fields, button
    states).
  - `quit` — clean shutdown.
- Never started unless the env var is set; binds 127.0.0.1 only.
- **Feedback loop:** seed sample config in a temp workspace (2 connections, domains incl. a CIDR,
  local+remote forwards, one share) → launch `.venv/bin/susops-tray` with both env vars →
  drive socket → read PNG → iterate.

## Testing

- **Layer 2 (headless, default `pytest`)**: keep all `do_*` coverage per the existing
  test-automation plan; add pure-Python tests for new extractable logic (sidebar/tab label
  building, menu-tree spec, debug-server command parsing) without AppKit.
- **Layer 3 (`pytest -m gui`, macOS opt-in)**: launch tray with debug env vars; assert
  `dump-menu` matches the unified structure, `open-config` + `dump-window` shows expected
  tabs/groups/selection, `screenshot` produces a non-trivial PNG.
- Visual verification during development via the feedback loop (agent reads PNGs).

## Error handling

- All `do_*` failures already surface via `show_alert` — unchanged.
- Debug server: malformed command → `{"error": ...}` response, never crashes the app; UI-command
  timeout (5 s) reports an error instead of hanging the socket.
- Window actions on stale state (e.g. removing an item that vanished) refresh and no-op gracefully —
  facade raises ValueError, which surfaces as the existing Error alert.

## Phasing

1. **Phase 0 — feedback loop**: workspace override, debug server, screenshot/dump commands,
   loop verified end-to-end with the *current* UI.
2. **Phase 1 — window shell**: window + tab strip + sidebar + detail panes + Add…/actions wiring.
   Menu's existing "Settings…" item now opens the window; old menu items remain during this phase.
3. **Phase 2 — unification**: slim menu, gear tab replaces the Settings dialog, delete dead dialog
   code, README/docs update.
4. **Phase 3 (optional, not in this plan)** — inline field editing. Requires new facade methods
   (`update_connection`, `update_forward`, `update_pac_host`) → OpenAPI regen
   (`python tools/gen_openapi.py`) + TUI/Linux/CLI parity per the project checklist.

Each phase ends with passing tests, screenshot verification, and granular commits.

## Out of scope

- Linux GTK parity (follow-up; base.py contract keeps Linux working).
- Inline editing (Phase 3, separate spec/plan if wanted).
- Web-based config UI.
- Bandwidth plots in the window (tray menu-bar bandwidth display unchanged).
- Localization.
