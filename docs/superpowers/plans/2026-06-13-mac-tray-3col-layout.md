# macOS Tray 3-Column Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the mac tray config window as a 3-column Tailscale-style editor (nav / list / detail-editor) per `docs/superpowers/specs/2026-06-13-mac-tray-3col-redesign-design.md`, with inline editing, colored run-state dots, layered dark look, and an instant-apply settings pane.

**Architecture:** The pure-Python view-model (`config_window_model.py`) is rewritten first (TDD, headless) to emit nav/list/form specs with conn-tag-carrying identities. The AppKit layer (`mac_config_window.py`) is rebuilt around three columns (two view-based NSTableViews + a form-rendering detail pane) reusing the v1 lifecycle scaffolding (policy scope, cached NSObject subclasses, `_schedule_render`, handler trimming, background refresh + generation counter). Editing uses remove+re-add with rollback in mac.py. The debug server gains `set-field` so GUI tests can drive full edit round-trips. Every UI task is acceptance-tested by in-process screenshots read by the agent.

**Tech Stack:** PyObjC/AppKit (macOS 11+ APIs runtime-guarded), rumps (menu only, unchanged), pytest + `SUSOPS_RUN_GUI_TESTS=1 -m gui`.

**Spec:** `docs/superpowers/specs/2026-06-13-mac-tray-3col-redesign-design.md` — the contract. Read it fully before any task.

**Branch:** continue on `feature/tray-config-window`. Rollback tag: `before-3col-layout`.

**Baseline:** full suite green except 2 known pre-existing failures (`test_version_matches_pyproject`, `test_cmd_guide_contains_tool_sections`); 7 GUI smoke tests pass.

**Dev feedback loop** (unchanged from v1; MANDATORY acceptance for every UI task):

```bash
WS=$(mktemp -d /tmp/susops-dev.XXXX)
.venv/bin/python - "$WS" <<'EOF'
import sys
from pathlib import Path
from susops.client import SusOpsClient
from susops.core.config import PortForward
c = SusOpsClient(workspace=Path(sys.argv[1]))
c.add_connection("work", "user@bastion")
c.add_connection("home", "pi@home.lan")
c.add_pac_host("blabla.de", conn_tag="work")
c.add_pac_host("10.0.0.0/8", conn_tag="work")
c.add_pac_host("intra.home", conn_tag="home")
c.add_local_forward("work", PortForward(src_port=5432, dst_port=5432, dst_addr="db.internal", tag="postgres"))
c.add_remote_forward("work", PortForward(src_port=8080, dst_port=8080, tag="webserver"))
EOF
SUSOPS_TRAY_WORKSPACE=$WS SUSOPS_TRAY_DEBUG_PORT=7799 .venv/bin/susops-tray &
sleep 4
.venv/bin/python tools/tray_debug.py 7799 open-config
.venv/bin/python tools/tray_debug.py 7799 dump-window
.venv/bin/python tools/tray_debug.py 7799 screenshot /tmp/v2.png
# … iterate, then:
.venv/bin/python tools/tray_debug.py 7799 quit
[ -f "$WS/pids/susops-services.pid" ] && kill $(cat "$WS/pids/susops-services.pid") 2>/dev/null
rm -rf "$WS"
```

NEVER touch the user's running tray or `~/.susops`.

---

## File structure

| File | Status | Responsibility |
|---|---|---|
| `src/susops/tray/config_window_model.py` | rewrite | v2 view-model: NavItem/ListRow/FormField specs, identities with conn_tag, colored dot semantics, counts |
| `tests/tray/test_config_window_model.py` | rewrite | headless contract for the v2 model |
| `src/susops/tray/mac_config_window.py` | rebuild | 3-column window: nav table, list table + search, detail/form renderer, dirty tracking |
| `src/susops/tray/mac.py` | modify | dispatch v2 (identity-carried conn_tag, save/create with rollback), settings split (instant-apply vs ports), debug `set-field`, delete dead modal adds at the end |
| `tests/tray/test_gui_smoke.py` | rewrite | new-geometry GUI tests incl. inline-edit round-trip |
| `README.md`, `CLAUDE.md` | modify | final docs |

Identity tuples v2 (used EVERYWHERE — model, window, dispatch, debug):
`("connection", tag)` · `("domain", conn_tag, host)` · `("forward", conn_tag, direction, src_port)` · `("share", port)`.

Dot colors: `green` (active/running) · `amber` (pending) · `gray` (stopped/inactive) · `red` (error/connection-down). "Active" for domains/forwards = connection running AND item enabled. Disabled items additionally `dimmed=True`.

---

### Task 1: View-model v2 (pure Python, TDD)

**Files:** rewrite `src/susops/tray/config_window_model.py`, rewrite `tests/tray/test_config_window_model.py`.

Dataclasses (frozen):

```python
@dataclass(frozen=True)
class NavItem:      # column 1
    key: str        # "connections" | "domains" | "forwards" | "shares" | "settings"
    title: str
    icon: str       # SF Symbol name ("" = none)
    count: int | None

@dataclass(frozen=True)
class ListRow:      # column 2
    kind: str       # "item" | "section" | "info"
    title: str
    subtitle: str = ""
    dot: str = ""        # "" | "green" | "amber" | "gray" | "red"
    badge: str = ""      # connection tag pill, "" = none
    dimmed: bool = False
    identity: tuple = ()

@dataclass(frozen=True)
class FormField:
    key: str
    label: str
    kind: str        # "text" | "secure" | "popup" | "combo" | "check_pair" | "static" | "path"
    value: object = ""
    options: list = field(default_factory=list)
    placeholder: str = ""
    note: str = ""   # secondary inline note

@dataclass(frozen=True)
class DetailSpec:
    title: str
    status_text: str       # "active · local forward on work"
    status_dot: str        # color word
    toggle: tuple | None   # ("Enabled", bool, action_id) — rendered top-right
    toggle_note: str = ""  # connection pane explainer
    fields: list = field(default_factory=list)   # list[FormField]
    actions: list = field(default_factory=list)  # list[Action] (Action unchanged from v1: action_id/title/enabled/destructive)
    editable: bool = False # False = static value rows, True = form controls + Save
```

Builders (signatures are the contract; statuses = ConnectionStatus list, cfg = config):

```python
build_nav(cfg, shares) -> list[NavItem]                 # 4 categories with counts + settings(count=None)
build_connection_rows(cfg, statuses) -> list[ListRow]
build_domain_rows(cfg, statuses) -> list[ListRow]
build_forward_rows(cfg, statuses) -> list[ListRow]      # Local section+info row, then Remote
build_share_rows(cfg, shares, statuses) -> list[ListRow]
filter_rows(rows, query) -> list[ListRow]               # case-insensitive over title+subtitle+badge; sections/info kept only if their section has matches
build_connection_detail(conn, status) -> DetailSpec     # editable=False, toggle top-right, toggle_note set
build_domain_form(conn_tags, *, conn_tag=None, host=None, status=None, conn=None) -> DetailSpec   # edit when host given, else create
build_forward_form(conn_tags, *, fw=None, direction=None, conn_tag=None, statuses=()) -> DetailSpec
build_share_detail(info, status) -> DetailSpec          # includes URL static row; port+password editable
build_fetch_form(conn_tags) -> DetailSpec
```

Pin behavior with tests (SimpleNamespace fixtures, realistic disjoint pac_hosts as in v1 tests): dot colors per the matrix above (incl. amber pending, red share-connection-down, dimmed disabled), forward sections + explainer info rows in order, badges, counts, filter_rows behavior, edit-vs-create field sets, action sets (`forward.save/test/remove`, `domain.save/test/remove`, `share.save/stop|start/delete/copy_url/copy_password`, `conn.start/stop/restart/test/remove`, `*.create`, `fetch.run`), Save absent on connection detail (editable=False), toggle present top-right for domain/forward/connection only.

- [ ] Write the full test file first; run (ImportError) → implement → all pass.
- [ ] `.venv/bin/pytest tests/tray/ -q` green (old gui tests still skip; they break only when the window changes in Task 2+ — if any non-gui test imports removed v1 builders, update it here).
- [ ] Commit: `feat(tray): view-model v2 for 3-column layout`

### Task 2: Window shell v2 — chrome, 3 columns, nav + list

**Files:** rebuild `src/susops/tray/mac_config_window.py` (keep: lifecycle scaffolding, `_get_*_cls` cached classes pattern, `_schedule_render`, `_apply_data` plumbing, handler trimming, `_screenshot`-compat). Modify mac.py debug handlers.

Contract:
- Window: 1080×640 (min 980×560), `NSWindowStyleMaskFullSizeContentView`, `setTitlebarAppearsTransparent_(True)`, `setTitleVisibility_(NSWindowTitleHidden)`, **normal level** (remove NSFloatingWindowLevel), system appearance (no pin).
- Three bands with explicit dynamic layer colors (no behindWindow blur): col1 darkest (try `NSVisualEffectView` material sidebar with `blendingModeWithinWindow`; fallback plain layer color), col2 `underPageBackgroundColor`, col3 `windowBackgroundColor`; card boxes later.
- Col 1: view-based source-list NSTableView (`setStyle_` guarded: `hasattr(tv, 'setStyle_')`), rows from `build_nav` (icon via `NSImage.imageWithSystemSymbolName_accessibilityDescription_` guarded, count right-aligned secondary), Settings pinned last (separate row after spacer row or simply last row). Selection drives col 2.
- Col 2: view-based NSTableView with custom `NSTableCellView`s (title+subtitle+dot circle via small layer-backed view or colored attributed `●`, badge pill = rounded NSTextField with quaternary fill), section/info rows non-selectable (`tableView_shouldSelectRow_`), NSSearchField on top driving `filter_rows`, context-aware add button(s) at the bottom (titles per spec; actions wired in Tasks 5/6 — for now they may no-op with a placeholder col-3 message).
- Col 3: placeholder label (real panes in Task 3/4).
- Selection plumbing: nav category → rebuild col-2 rows (preserve col-2 selection by identity when refreshing); list row → store selected identity; `refresh()`/`_apply_data` keep all three columns in place without dropping selection (v1 in-place pattern).
- Debug: `select <category> [index]` (index over selectable item rows), `dump-window` v2 = `{nav:[{key,title,count}], category, search, rows:[{kind,title,subtitle,dot,badge,dimmed}], selected, detail_title, dirty}`. Keep `screenshot`, `open-config [category]` (accepts category key or omits).
- Old tab-based gui tests will fail — REWRITE in this task: keep+adapt `test_config_window_opens_and_dumps` (asserts nav categories + domain row present after seeding), `test_window_reflects_external_changes` (poll refresh adds row), `test_unified_menu_structure`, `test_ping_and_dump_menu`, `test_screenshot_of_about_panel` unchanged. Drop tab/gear-specific asserts until Task 7.

- [ ] Implement; iterate via the feedback loop until the screenshots show: 3 layered bands, nav with counts + selection pill, forwards list with Local/Remote sections + explainers + dots + badges, search field filtering (screenshot before/after `select` + a search via `set-search` debug cmd OR verify filter_rows headlessly only — implementer's choice; if no debug command, cover filter in model tests only).
- [ ] `SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui -v` → adapted tests pass; full suite green except known 2.
- [ ] Commit: `feat(tray): 3-column shell - nav, list, layered chrome`

### Task 3: Detail header + read panes + colored states

**Files:** `mac_config_window.py`, `mac.py` (dispatch identity v2).

- Header renderer (all kinds): bold 16px title; status line under it (11px) with COLORED dot (systemGreen/systemOrange/systemGray/systemRed via attributed string or tiny layer view) + text; **Enabled switch top-right** (NSSwitch if available, else checkbox; small secondary "Enabled" label to its left) wired to `*.toggle` instant-apply; connection pane: `toggle_note` one-liner under the toggle.
- Connection detail: static value rows in a rounded layer-backed card (`NSBox` custom or layer-backed NSView, quaternary fill, corner radius 8), action row: `[Remove Connection…]` red LEFT … right: `[Test] [Restart] [Stop|Start]`. Red = `setBezelColor_`/attributed red title (guarded), ellipsis on destructive titles.
- Domain/forward/share panes in this task render as STATIC cards too (editable forms arrive Task 4/6) — fields from the DetailSpec, actions per spec (Save hidden when not editable-rendered yet).
- `dispatch_window_action` v2 in mac.py: conn_tag comes FROM the identity (no `current_tag` — it no longer exists); update all branches + confirm dialogs.
- Verify via loop: each category's detail screenshot (connection/domain/forward) — header toggle top-right, colored dots, card, button row styling. Toggle via `action domain.toggle` flips dim/dot in col 2 and the header switch.
- [ ] GUI tests: adapt/add `test_detail_header_toggle` (select domain → `action domain.toggle` → dump shows dimmed flip + config change via SusOpsClient).
- [ ] Suites green; commit: `feat(tray): detail panes v2 - colored states, header toggle, card layout`

### Task 4: Form engine + inline editing (forwards, domains)

**Files:** `mac_config_window.py` (form renderer + dirty tracking), `mac.py` (save handlers with rollback, debug `set-field`).

- Form renderer: FormField list → controls (text=NSTextField bezeled, secure=NSSecureTextField, popup=NSPopUpButton, combo=NSComboBox, check_pair=two NSButtons (TCP/UDP), path=static+Choose… button, static=selectable label). Right-aligned 11px labels; inline `note` as secondary text. Reuses the per-render handler trim. Text-change → dirty via control target/notification (`controlTextDidChange_` delegate on the cached DS class or NSNotificationCenter for NSControlTextDidChangeNotification).
- Dirty semantics: dirty=true enables Save, suppresses col-3 re-render on `_apply_data` (cols 1–2 still refresh), selection change away while dirty → `_show_confirm("Discard unsaved changes?")`; revert on No.
- `forward.save`: read widgets → validate (ports 1–65535; ≥1 protocol; if local and src_port changed: is_port_free) → mac.py `_save_forward(old_identity, new_fw, new_conn_tag)`: remove old (`do`-level direct manager calls, NOT do_* alerts), add new; on add failure re-add old and alert; reselect new identity; refresh. Same shape `domain.save` (`remove_pac_host` + `add_pac_host(host, conn_tag)` + restore enabled-disabled state if the old one was disabled).
- Debug: `set-field <key> <value…>` writes into the live form widgets (popup: select title; check_pair: `tcp on/off` style); `dump-window` adds `fields:{key:value}` + `dirty`.
- [ ] GUI round-trip test (the headline test of the redesign):

```python
def test_inline_edit_forward_round_trip(tray_proc):
    from susops.client import SusOpsClient
    from susops.core.config import PortForward
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_local_forward("work", PortForward(src_port=5432, dst_port=5432, dst_addr="db.internal", tag="postgres"))
    assert tray_proc.send("open-config forwards").get("ok")
    assert tray_proc.send("select forwards 0").get("ok")
    tray_proc.send("set-field dst_port 5433")
    assert tray_proc.send("dump-window")["dirty"] is True
    tray_proc.send("action forward.save")
    cfg = c.list_config()
    assert cfg.connections[0].forwards.local[0].dst_port == 5433
    assert tray_proc.send("dump-window")["dirty"] is False
```

- [ ] Also test rollback headlessly where possible (unit-test `_save_forward` logic if extractable, else gui: save with colliding src_port → alert path → old forward still present).
- [ ] Visual: screenshot the editable forward form (matches mockup: card, Save right-accent, Delete… red left).
- [ ] Commit: `feat(tray): inline editing for forwards and domains with rollback`

### Task 5: Create flows (+ buttons → inline create forms)

**Files:** `mac_config_window.py`, `mac.py`.

- Col-2 bottom buttons render per category (Task 2 placeholders become real): selecting them shows the CREATE form in col 3 (`build_*_form` with no item): Connection (tag, ssh host combo from `get_ssh_hosts()`, socks port optional), Domain (host, connection popup defaulted to selected row's conn or first), Forward (direction popup + all fields). `Create` primary + `Cancel` (returns to selection). Validation identical to the old modal dialogs (empty tag/host, validate_port, is_port_free for local src). `conn.create`/`domain.create`/`forward.create` dispatch to existing `manager.add_*` through mac.py handlers; success selects the new row.
- [ ] GUI test: `test_inline_create_domain` (open domains → trigger add via new debug `action domain.new` or `add` command → set-field host test.example.com → action domain.create → assert in config + listed).
- [ ] Visual screenshots of each create form.
- [ ] Commit: `feat(tray): inline create forms replace modal add dialogs`

### Task 6: Shares + fetch

**Files:** `mac_config_window.py`, `mac.py`.

- Share detail per spec: URL static row `http://localhost:<port>` + Copy button (`NSPasteboard.generalPasteboard` clearContents+setString), Password secure-style display with Reveal toggle + Copy, Port editable, Save = stop+delete+re-share(old file/conn, new port/password) with rollback; Stop/Start; Delete…; three-state colored status.
- Share create: `+ Share File…` → form with Choose File… (NSOpenPanel via existing `_pick_file`), connection popup, optional password/port → `share.create`.
- Fetch: `Fetch…` button → fetch form (connection popup, port, password, output path + Choose…) → `fetch.run` → `do_fetch` (existing) with result alert.
- [ ] GUI test: share create round-trip (file in tmp, action share.create, list_shares shows it, URL row present in dump fields).
- [ ] Visual screenshots (share detail with URL+copy buttons, fetch form).
- [ ] Commit: `feat(tray): shares pane with copyable URL/password, inline share + fetch`

### Task 7: Settings pane v2 (instant apply)

**Files:** `mac_config_window.py`, `mac.py` (refactor `_settings_fields`/`_apply_settings`).

- Refactor mac.py: `_settings_fields()` gains per-field `section` ("General:"/"Menu bar:"/"Servers:") + `description` strings (texts per spec); split `_apply_settings` into `_apply_setting_toggle(key, value) -> str|None` (single update_app_config kwarg; launch-at-login keeps the background thread) and `_apply_server_ports(rpc, sse, pac, ctx) -> str|None` (existing validation verbatim). The OLD all-at-once path is deleted with the pane that used it.
- Pane render: grid with right-aligned bold section labels; checkbox+label rows with wrapped indented gray descriptions (measure text height; or NSStackView for this pane only — implementer's choice per spec); logo segmented instant preview+persist; Servers rows with "auto" placeholder + notes + single `Apply` button; **Config file: row = right-aligned `Open Config File…` button ONLY (no sentence)**. Toggle change → apply immediately → on error alert + flip the control back.
- Col 2 hidden while Settings selected (reuse/generalize the v1 `_gear_mode` + `_set_sidebar_visible` approach for the new geometry); nav re-shows it.
- [ ] GUI test: adapt `test_gear_tab_shows_settings_and_hides_sidebar` → `test_settings_pane` (select settings → dump shows settings mode + col2 hidden; `action settings.toggle.notifications` style optional — minimum: pane renders, toggling one checkbox via set-field/action persists to app_config).
- [ ] Visual screenshot vs the settings mockup (sections, descriptions, button-only config row).
- [ ] Commit: `feat(tray): Tailscale-style settings pane with instant apply`

### Task 8: Cleanup, docs, final sweep

**Files:** `mac.py`, `README.md`, `CLAUDE.md`, tests.

- Delete now-dead mac.py modal flows: `_show_add_connection_dialog`, `_show_add_host_dialog`, `_show_add_forward_dialog`, `_show_share_file_dialog`, `_show_fetch_file_dialog`, and `_show_form_dialog` + its helper classes IF (grep) nothing else calls them (`_settings_fields` no longer routes through it after Task 7; `_show_message`/`_show_confirm`/`_pick_file`/about/live-logs panels STAY). Every deletion grep-verified caller-free; Linux untouched.
- README: update the macOS tray section to the 3-column window (nav categories, inline editing, settings). CLAUDE.md: update the tray divergence paragraph + file descriptions.
- Final verification: full suite; all gui tests; `python tools/gen_openapi.py --check` (must still pass — no facade changes allowed in this plan); final screenshot set (one per category detail + a create form + settings) read and compared against the two approved mockups; `dump-menu` unchanged.
- [ ] Commit: `feat(tray): finalize 3-column window - remove dead modal dialogs` + `docs(readme,claude): 3-column tray window`

---

## Self-review

**Spec coverage:** layout/columns → T2; colored dot vocabulary → T1+T3; header toggle top-right + connection note → T3; inline edit w/ rollback (forwards/domains) → T4; create flows → T5; shares URL/copy + fetch → T6; settings instant-apply + button-only config row → T7; identities v2 + dispatch → T1/T3; dirty guard vs refresh → T4; debug select/dump/set-field → T2/T4; window chrome/level/min-size → T2; macOS-11 guards → T2/T3; test migration → T2–T7; dead modal deletion + docs → T8. Connection editing excluded (spec: Phase 3).

**Known risks:** AppKit layout iteration expected (loop is the acceptance test); NSSwitch availability (fallback checkbox); text-change dirty notifications in PyObjC (use NSNotificationCenter if delegate wiring fights); remove+re-add rollback ordering (add-new-first-then-remove-old is NOT possible for same-port edits — remove first, re-add old on failure).

**Type consistency:** identity tuples, FormField/ListRow/NavItem names, action ids (`forward.save`, `domain.create`, `share.copy_url`, `fetch.run`) consistent across tasks.
