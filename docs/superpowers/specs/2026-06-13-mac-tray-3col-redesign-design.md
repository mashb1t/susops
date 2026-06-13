# macOS Tray Config Window v2 — 3-Column Tailscale-Style Redesign

**Date:** 2026-06-13
**Status:** Approved (user confirmed all decisions + 2 mockup tweaks)
**Rollback point:** git tag `before-3col-layout` (a619d79)
**Predecessor:** docs/superpowers/specs/2026-06-12-mac-tray-config-window-design.md (v1, shipped)
**Motivation:** judge panel ranked v1 at 4.5/10 usability, 4/10 native style. Worst defects: no
editing (remove + full re-entry), overloaded ●/○ glyph semantics, segmented-control misuse,
flat un-layered dark look, web-form gear pane, dead space.

## Locked decisions

| Decision | Choice |
|---|---|
| Layout | 3 columns: nav / list / detail-editor (Tailscale main-window reference) |
| Connections placement | Promoted into column 1 as first nav section. Per-connection tab strip REMOVED. Lists in col 2 are global with a connection badge per row |
| Editing (iteration 1) | Forwards/domains/shares editable inline via remove+re-add with rollback. Connections editable inline via facade `update_connection` (preserves children, auto-restarts a running connection under the new config) |
| Create flows | Inline col-3 forms replace the modal add dialogs (file/folder choosers still use NSOpenPanel) |
| Settings semantics | ALL changes (toggles + logo style + launch-at-login + the 3 server ports) are staged locally and persist only on a single explicit Apply button — no instant-apply. Leaving the settings category or closing the window without Apply discards the staged changes (and reverts any live logo icon preview to the saved logo). Port validation errors keep the pane (no partial apply) |
| Settings config-file row | Label column "Config file:" + right-aligned "Open Config File…" button ONLY — no explainer sentence (user tweak) |
| Detail header | Title (bold) + colored status line on the left; **Enabled toggle in the upper-right corner of the header** (user tweak). Toggle applies instantly |
| Appearance | Pinned DarkAqua + custom palette (user decision after seeing the system-appearance version). Always-dark window via `setAppearance_(DarkAqua)`; explicit hex palette (window/col3 #17181c, nav #25262c, list #1f2026, card #222329, inputs #2a2b31/#3f4147, badges #3a3c44) painted on layer-backed bands; system controls inherit DarkAqua. NOT behindWindow blur (would blind the in-process screenshot harness) |
| Window | ~1080×640 initial, min 980×560, normal window level (drop NSFloatingWindowLevel), full-size content view + transparent titlebar + hidden title (traffic lights overlay column 1) |
| Status-dot vocabulary | Dots mean RUN STATE only, colored: green=active/running, amber=pending, gray=stopped/inactive, red=error/connection-down. "Enabled" is exclusively the header toggle; disabled rows render dimmed. No more ●-means-enabled |
| minimum macOS | 11 (source-list style + SF Symbols). Runtime-guard with graceful fallback (plain highlight, no icons) |
| Platform scope | macOS only; Linux tray untouched (base.py contract unchanged) |

## Layout

```
┌────────────────────────────────────────────────────────────────┐
│ ●●●  (transparent titlebar, traffic lights over col 1)         │
│┌──────────┐┌──────────────┐┌──────────────────────────────────┐│
││ NAV      ││ LIST         ││ DETAIL                           ││
││ Connect..││ [Search    ] ││  postgres            Enabled [✓] ││
││ Domains  ││ Local        ││  ● active · local forward on work││
││ Forwards*││  explainer   ││ ┌──────────── card ────────────┐ ││
││ Shares   ││  ● postgres  ││ │ Tag        [postgres       ] │ ││
││          ││    :5432→…   ││ │ Connection [work         ▾] │ ││
││          ││ Remote       ││ │ Direction  [Local (-L)   ▾] │ ││
││          ││  explainer   ││ │ Source     [localhost][5432] │ ││
││          ││  ○ webserver ││ │ Destination[db.internal][..] │ ││
││          ││              ││ │ Protocols  [✓]TCP [ ]UDP     │ ││
││ Settings ││ [+ Add fwd]  ││ └──────────────────────────────┘ ││
││          ││              ││ [Delete…]        [Test] [Save]   ││
│└──────────┘└──────────────┘└──────────────────────────────────┘│
└────────────────────────────────────────────────────────────────┘
```

### Column 1 — nav (~180px, darkest band)

Source-list NSTableView (`NSTableViewStyleSourceList`, view-based) with SF Symbol icons +
count badges: **Connections / Domains / Forwards / Shares**, spacer, **Settings** pinned last.
Selection = accent pill. Icons (fallback: text only): `cable.connector` or `bolt.horizontal`
(connections), `globe` (domains), `arrow.left.arrow.right` (forwards),
`square.and.arrow.up` (shares), `gearshape` (settings).

### Column 2 — list (~270px, mid band)

View-based NSTableView + NSSearchField on top (client-side filter over the row model).
Rows: title (13px) + subtitle (11px secondary) + colored status dot + connection badge
(rounded pill, secondary fill). Disabled items render dimmed. Group/info rows are
non-selectable.

Per category:
- **Connections**: title=tag, subtitle=ssh_host, dot = green running / amber pending /
  gray stopped / dimmed+gray when disabled. No badge.
- **Domains**: title=host, badge=conn tag, dimmed when disabled. Dot: green when
  (connection running AND host enabled) else gray.
- **Forwards**: TWO sections with one-line explainers (non-selectable info rows):
  "Local — reach a remote service on a local port", "Remote — expose a local service on
  the SSH server". Row title = fw.tag or ":src_port", subtitle = ":src → dst_addr:dst_port",
  badge = conn tag. Dot: green when (connection running AND fw enabled) else gray; dimmed
  when disabled.
- **Shares**: title=filename, subtitle="port NNNN · N ok", badge=conn tag. Dot three-state:
  green running / gray stopped-manual / red connection-down.

Bottom of column 2: context-aware add button(s):
- Connections → `+ Add Connection`
- Domains → `+ Add Domain / IP / CIDR`
- Forwards → `+ Add Forward` (direction chosen inside the form)
- Shares → `+ Share File…` and `Fetch…`

### Column 3 — detail / editor (flexible, content band + card)

**Header** (all kinds): bold 16px title left; colored status line (11px, dot+text) under it;
**Enabled toggle top-right** (domains, forwards, connections — instant apply via existing
do_toggle_*; shares also have one — it expresses serving on/off via share.toggle and
replaces the old Stop/Start Share button). Connection header adds a one-line secondary note
under the toggle area: "Disabled connections are skipped when the proxy starts."

**Body**: a rounded layer-backed card (quaternary fill) holding the form grid
(right-aligned 11px secondary labels, editable controls). Below the card, the action row:
destructive button red + ellipsis on the LEFT, primary actions right-aligned
(`[Delete…] ……… [Test] [Save]`). Save is enabled only when dirty.

Per kind:
- **Forward (edit)**: Tag (text), Connection (popup), Direction (popup Local/-L, Remote/-R),
  Source addr+port, Destination addr+port, Protocols TCP/UDP checkboxes.
  Save = validate (validate_port, is_port_free for local src when changed, ≥1 protocol) →
  remove old + add new with ROLLBACK (re-add old on failure) → reselect by new identity.
  Actions: Delete…, Test, Save.
- **Domain (edit)**: Host (text), Connection (popup). Save = remove+add w/ rollback.
  Actions: Delete…, Test, Save.
- **Share**: File (read-only path), **URL row with Copy button** (`http://localhost:PORT`
  — the v1-missing essential), Port (text), Password (secure, "Reveal" + Copy),
  Downloads counts, Status. Save (port/password changed) = stop+delete+re-share w/ rollback.
  **Header Enabled toggle = serving on/off** (share.toggle: ON when running/connection-down,
  OFF only when manually stopped); it replaces the old Stop/Start button. Actions: Delete…,
  Copy URL, Copy Password, Save.
- **Connection (editable)**: Tag, SSH Host, SOCKS Port as editable text fields + live
  status. Save commits via facade `update_connection` (edits in place, preserving the
  connection's forwards/domains/shares; auto-restarts a running connection under the new
  config). Actions: Delete…, Test, Restart, Stop/Start, Save (Save enabled on dirty).
  Header Enabled toggle.
- **Create forms** (from the + buttons): same field sets, empty defaults, single
  `Create` primary button + `Cancel`. Connection create: Tag, SSH Host (combo seeded from
  get_ssh_hosts()), SOCKS port (optional). Share create: "Choose File…" → NSOpenPanel,
  Connection popup, Password (optional), Port (optional). Fetch form: Connection popup,
  Port, Password, Output path (+ Choose…), `Fetch` button.

**Dirty-state guard**: while a col-3 form is dirty, the periodic refresh must not re-render
column 3 (generalize the v1 `_gear_mode` suppression into per-pane dirty suppression);
columns 1–2 keep refreshing. Selection changes away from a dirty form prompt
(discard / keep editing) or simply discard — DECISION: discard silently is NOT ok;
use a small confirm ("Discard unsaved changes?") only when dirty.

### Settings pane (col-1 "Settings", spans cols 2+3 area; col 2 hidden)

Tailscale-Settings-style grid: right-aligned bold section labels, rows of
checkbox + label with indented gray description (11px secondary, wrapped) where the
setting needs explanation:

- **General:** Launch SusOps at login · Stop proxy on quit (desc: skipped when another
  frontend is attached) · Random SSH ports on start (desc) · Restore shares on start
- **Menu bar:** Show bandwidth (desc) · Desktop notifications · Logo style (segmented,
  live preview, instant persist)
- **Servers:** RPC / SSE / PAC port fields ("auto" placeholder when 0; secondary note
  "restart daemon to apply" on RPC+SSE). The single `Apply` button (left-aligned in the
  content column under the port fields) commits EVERYTHING staged at once, validating all
  three ports first (validate_port allow_zero, is_port_free) so a bad port keeps the whole
  pane without a partial apply
- **Config file:** `Open Config File…` button only (left-aligned in the content column next
  to its section label) — NO explainer text

ALL settings (toggles + logo + launch-at-login + ports) are STAGED locally on change and
persist only when the user clicks Apply. Logo gets a live icon preview that reverts if the
user leaves without Apply. Leaving the settings category (nav to another category) or closing
the window without Apply discards every staged change (reverting the logo preview). The same
dirty-tracking + discard approach the detail forms use applies here via a settings dirty flag.
`apply_all_settings(values)` runs on Apply in a worker (per-field `_apply_setting_toggle`
+ the validated `_apply_server_ports`). The single-source-of-truth rule stays: field spec +
validation live in mac.py, the pane only renders, stages, and collects.

## Identities & dispatch (v2)

Identity tuples now carry the connection tag (global lists):
`("connection", tag)`, `("domain", conn_tag, host)`, `("forward", conn_tag, direction, src_port)`,
`("share", port)`. `dispatch_window_action(action_id, identity)` reads conn_tag from the
identity (fixes the v1 current_tag race) and gains: `*.save` (with payload from the form),
`*.create`, `share.copy_url`, `share.copy_password`, `fetch.run`. Remove+re-add rollback
lives in mac.py next to the dispatch (client-side, sequential RPC with try/except re-add).

## Debug server / test migration

- `select <category> [index]` (category ∈ connections/domains/forwards/shares/settings);
  `dump-window` v2: nav categories+counts, list rows (title/subtitle/dot-color/badge/dimmed),
  selected identity, detail title, dirty flag, button states.
- NEW: `set-field <key> <value…>` (sets a col-3 form widget) and `action <action_id>` —
  enables full GUI round-trip tests of inline editing without modals (e.g. select forward →
  set-field dst_port 5433 → action forward.save → dump-window asserts new identity + config).
- GUI smoke tests rewritten to the new geometry; old tab-based tests removed. Keep:
  menu test, screenshot test, external-changes refresh test (adapted), gear→settings test
  (adapted), add: edit-round-trip test, search-filter test.

## Out of scope

- ~~Phase 3 facade update methods (update_connection etc.) — required only for editing
  connection fields; separate spec/plan.~~ DONE: `update_connection` lands inline
  connection editing (tag/host/SOCKS port) with child preservation + auto-restart.
- Linux/GTK parity.
- Window-level animations, drag-to-reorder, multi-select.

## Risks acknowledged

- Remove+re-add Save is destructive mid-failure → rollback re-add of the old item is
  mandatory; identity re-targeting after key-field edits.
- behindWindow vibrancy would break the in-process screenshot loop → explicit colors only.
- macOS 11 APIs runtime-guarded.
- The v1 spec's per-connection-tabs decision is explicitly reversed here with user consent.
