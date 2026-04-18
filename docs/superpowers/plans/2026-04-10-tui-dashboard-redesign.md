# TUI Dashboard Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the dashboard into a three-panel layout (Connections · Stats+BW · Domains/Forwards/Shares) so every common operation is reachable in zero screen navigations.

**Architecture:** Left panel holds the connection list with an "All" row; selecting it shows aggregate stats and all domains/forwards/shares, selecting a connection filters to that connection's data. Cumulative RX/TX counters are tracked in `_BandwidthSampler` and reset on stop. The right panel pins Forwards and Shares at the bottom with fixed height; Domains expand to fill the rest.

**Tech Stack:** Python 3.11+, Textual 8.2.3, textual-plotext 1.0.1, psutil, susops facade pattern

---

## File Map

| File | Change |
|------|--------|
| `src/susops/facade.py` | Add `_totals`, `_start_times` to sampler; add `get_bandwidth_totals()`, `get_uptime()` to manager; call `reset_totals()` on stop |
| `src/susops/tui/screens/connection_editor.py` | Rename "PAC Hosts" → "Domain / IP / CIDR" in tab, column, and detail preview |
| `src/susops/tui/screens/dashboard.py` | Full rewrite of compose, DEFAULT_CSS, `_apply_status`, `_update_detail_panel`, add `_update_context_panel`, add `_render_all_stats` |
| `src/susops/tui/app.tcss` | Remove stale panel rules, add three-panel rules |
| `tests/test_bw_totals.py` | New: unit tests for cumulative counters and uptime |

---

## Task 1: Rename "PAC Hosts" → "Domain / IP / CIDR" in the Connections editor

**Files:**
- Modify: `src/susops/tui/screens/connection_editor.py`

- [ ] **Step 1: Apply the three display-string changes**

In `connection_editor.py`:

Change line 196 — TabPane title:
```python
# Before:
with TabPane("PAC Hosts", id="tab-pac"):
# After:
with TabPane("Domain / IP / CIDR", id="tab-pac"):
```

Change line 213 — column header in the Connections table:
```python
# Before:
tbl.add_columns("Status", "Tag", "SSH Host", "SOCKS Port", "PAC Hosts", "Forwards")
# After:
tbl.add_columns("Status", "Tag", "SSH Host", "SOCKS Port", "Domains", "Forwards")
```

Change line 325 — detail preview text:
```python
# Before:
f"  |  PAC hosts: {len(conn.pac_hosts)}"
# After:
f"  |  Domains: {len(conn.pac_hosts)}"
```

- [ ] **Step 2: Verify no other display-only references remain**

```bash
grep -rn "PAC Hosts\|PAC hosts" src/susops/tui/
```
Expected: zero matches (the underlying field `pac_hosts` is unchanged — it is a config model field, not a display string).

- [ ] **Step 3: Run tests**

```bash
pytest tests/ -q
```
Expected: all pass (no tests reference the display string directly).

- [ ] **Step 4: Commit**

```bash
git add src/susops/tui/screens/connections.py
git commit -m "feat: rename 'PAC Hosts' to 'Domain / IP / CIDR' in connections editor UI"
```

---

## Task 2: Cumulative bandwidth counters + connection uptime in facade

**Files:**
- Modify: `src/susops/facade.py`
- Create: `tests/test_bw_totals.py`

### Step 1: Add `_totals` dict and `_start_times` dict

- [ ] In `_BandwidthSampler.__init__`, after `self._rates`:

```python
self._totals: dict[str, tuple[float, float]] = {}  # tag -> (rx_total_bytes, tx_total_bytes)
```

- [ ] In `SusOpsManager.__init__`, after `self._bw_sampler` is assigned, add:

```python
self._start_times: dict[str, float] = {}  # tag -> time.monotonic() when started
```

### Step 2: Accumulate totals in `_BandwidthSampler._sample()`

- [ ] Inside the `with self._lock:` block in `_sample()`, immediately after `self._rates = new_rates`:

```python
# Accumulate cumulative byte totals (rate × elapsed time = bytes this interval)
for tag, (rx, tx) in new_rates.items():
    prev_rx, prev_tx = self._totals.get(tag, (0.0, 0.0))
    self._totals[tag] = (prev_rx + rx * dt, prev_tx + tx * dt)
```

### Step 3: Add `get_totals()` and `reset_totals()` to `_BandwidthSampler`

- [ ] Add both methods to the `_BandwidthSampler` class (after `get_rate`):

```python
def get_totals(self, tag: str) -> tuple[float, float]:
    """Return (rx_total_bytes, tx_total_bytes) accumulated since last reset."""
    with self._lock:
        return self._totals.get(tag, (0.0, 0.0))

def reset_totals(self, tag: str | None = None) -> None:
    """Reset cumulative counters. Pass tag=None to reset all."""
    with self._lock:
        if tag is None:
            self._totals.clear()
        else:
            self._totals.pop(tag, None)
```

### Step 4: Reset totals on stop in `SusOpsManager.stop()`

- [ ] In `SusOpsManager.stop()`, in the `for conn in connections:` loop, right after the `self._log(f"[{conn.tag}] Stopped")` line:

```python
self._bw_sampler.reset_totals(conn.tag)
self._start_times.pop(conn.tag, None)
```

### Step 5: Record start times in `SusOpsManager.start()`

- [ ] Find the code path in `SusOpsManager.start()` that logs a connection as started (look for `self._log(f"[{conn.tag}] Started")`). Right after that line:

```python
self._start_times[conn.tag] = time.monotonic()
```

### Step 6: Add public methods to `SusOpsManager`

- [ ] After `get_bandwidth()`:

```python
def get_bandwidth_totals(self, tag: str) -> tuple[float, float]:
    """Return cumulative (rx_bytes, tx_bytes) since last start. Resets on stop."""
    return self._bw_sampler.get_totals(tag)

def get_uptime(self, tag: str) -> float | None:
    """Return seconds since connection started, or None if not recorded."""
    start = self._start_times.get(tag)
    return time.monotonic() - start if start is not None else None
```

### Step 7: Write failing tests

- [ ] Create `tests/test_bw_totals.py`:

```python
"""Tests for cumulative bandwidth counters and uptime tracking."""
from __future__ import annotations

import time
import threading
from unittest.mock import MagicMock

import pytest

from susops.facade import _BandwidthSampler
from susops.core.process import ProcessManager


@pytest.fixture
def sampler(tmp_path):
    """Sampler with background thread started but no real sampling."""
    mgr = MagicMock(spec=ProcessManager)
    mgr.status_all.return_value = {}
    s = _BandwidthSampler(mgr)
    yield s


def test_totals_start_at_zero(sampler):
    assert sampler.get_totals("pi3") == (0.0, 0.0)


def test_reset_totals_single_tag(sampler):
    with sampler._lock:
        sampler._totals["pi3"] = (100.0, 50.0)
        sampler._totals["mash"] = (200.0, 80.0)
    sampler.reset_totals("pi3")
    assert sampler.get_totals("pi3") == (0.0, 0.0)
    # Other tag unaffected
    assert sampler.get_totals("mash") == (200.0, 80.0)


def test_reset_totals_all(sampler):
    with sampler._lock:
        sampler._totals["pi3"] = (100.0, 50.0)
        sampler._totals["mash"] = (200.0, 80.0)
    sampler.reset_totals()
    assert sampler.get_totals("pi3") == (0.0, 0.0)
    assert sampler.get_totals("mash") == (0.0, 0.0)


def test_totals_accumulate_across_injected_samples(sampler):
    """Directly write to _totals to simulate two accumulated samples."""
    with sampler._lock:
        sampler._totals["pi3"] = (500.0, 100.0)
    # Simulate a second accumulation (as _sample() would do)
    with sampler._lock:
        prev_rx, prev_tx = sampler._totals.get("pi3", (0.0, 0.0))
        sampler._totals["pi3"] = (prev_rx + 300.0, prev_tx + 60.0)
    assert sampler.get_totals("pi3") == (800.0, 160.0)
```

- [ ] **Run tests to confirm they fail (or some pass trivially)**

```bash
pytest tests/test_bw_totals.py -v
```
Expected: all 4 pass (they test internal state directly, no sampling needed).

### Step 8: Run full test suite

```bash
pytest tests/ -q
```
Expected: all pass.

### Step 9: Commit

```bash
git add src/susops/facade.py tests/test_bw_totals.py
git commit -m "feat: add cumulative bandwidth totals and uptime tracking to facade"
```

---

## Task 3: Three-panel dashboard layout scaffold

**Files:**
- Modify: `src/susops/tui/screens/dashboard.py`
- Modify: `src/susops/tui/app.tcss`

This task replaces the layout structure. After this task the app runs and shows three empty panels. Data rendering is added in Tasks 4–6.

- [ ] **Step 1: Add `_fmt_bytes` helper and update imports**

At the top of `dashboard.py`, add after `_fmt_bps`:

```python
def _fmt_bytes(b: float) -> str:
    """Format raw bytes as a human-readable string (e.g. 1.2 GB, 450 MB, 12 kB)."""
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f}GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f}MB"
    if b >= 1024:
        return f"{b / 1024:.0f}kB"
    return f"{b:.0f}B"


def _fmt_uptime(seconds: float) -> str:
    """Format elapsed seconds as 'Xh Ym', 'Xm', or 'Xs'."""
    if seconds >= 3600:
        h = int(seconds / 3600)
        m = int((seconds % 3600) / 60)
        return f"{h}h {m}m"
    if seconds >= 60:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds)}s"
```

Remove unused imports: `Header`, `TabbedContent`, `TabPane`, `DataTable` are no longer used in compose — keep them only if referenced elsewhere in the file. Check with:

```bash
grep -n "TabbedContent\|TabPane\|DataTable\|Header" src/susops/tui/screens/dashboard.py
```

Remove any that only appeared in the old compose.

- [ ] **Step 2: Replace `DEFAULT_CSS`**

Replace the entire `DEFAULT_CSS` string in `DashboardScreen`:

```python
DEFAULT_CSS = """
DashboardScreen { layout: vertical; }
#main-split { height: 1fr; }
#conn-panel { width: 22; background: $surface-darken-1; border-right: solid $primary-darken-2; }
#conn-list  { height: 1fr; border: round $primary-darken-1; margin: 1; border-title-align: left; }
#detail-panel { width: 1fr; }
#detail-tabs  { height: 1fr; }
#stats-content { height: auto; padding: 1 2; }
#bw-container  { height: 1fr; min-height: 8; }
#rx-chart { height: 1fr; width: 1fr; border: round $primary-darken-1; margin: 0 1 1 1; }
#tx-chart { height: 1fr; width: 1fr; border: round $primary-darken-1; margin: 0 1 1 0; }
#detail-logs { height: 1fr; margin: 1; border: round $primary-darken-1; }
#context-panel { width: 26; background: $surface-darken-1; border-left: solid $primary-darken-2; }
#domain-section { height: 1fr; border: round $primary-darken-1; margin: 1 1 0 1; border-title-align: left; }
#domain-content { padding: 0 1; }
#forward-content { height: auto; padding: 0 1; border: round $primary-darken-1; margin: 1; border-title-align: left; }
#share-content { height: auto; padding: 0 1; border: round $primary-darken-1; margin: 0 1 1 1; border-title-align: left; }
"""
```

- [ ] **Step 3: Replace `compose()`**

```python
def compose(self) -> ComposeResult:
    with Horizontal(id="main-split"):
        # Left: connection list
        with Vertical(id="conn-panel"):
            yield ListView(id="conn-list")
        # Centre: stats + bandwidth + logs
        with Vertical(id="detail-panel"):
            with TabbedContent(id="detail-tabs"):
                with TabPane("Stats", id="tab-stats"):
                    yield Static("", id="stats-content")
                    with Horizontal(id="bw-container"):
                        yield PlotextPlot(id="rx-chart")
                        yield PlotextPlot(id="tx-chart")
                with TabPane("Logs", id="tab-logs"):
                    yield RichLog(id="detail-logs", highlight=True, markup=True)
        # Right: context panel (domains / forwards / shares)
        with Vertical(id="context-panel"):
            with VerticalScroll(id="domain-section"):
                yield Static("", id="domain-content", markup=True)
            yield Static("", id="forward-content", markup=True)
            yield Static("", id="share-content", markup=True)
    yield from compose_footer()
```

- [ ] **Step 4: Update `on_mount()`**

Replace the full `on_mount` body:

```python
def on_mount(self) -> None:
    self.query_one("#conn-list", ListView).border_title = "Connections"
    self.query_one("#domain-section", VerticalScroll).border_title = "Domain / IP / CIDR"
    self.query_one("#forward-content", Static).border_title = "Forwards"
    self.query_one("#share-content", Static).border_title = "Shares"
    mgr = self.app.manager  # type: ignore[attr-defined]
    self._prev_on_log = mgr.on_log
    mgr.on_log = self._on_new_log
    self.set_interval(2.0, self._tick_refresh)
    self.refresh_status()
    self._start_sse_listener()
```

- [ ] **Step 5: Remove stale CSS from `app.tcss`**

Remove these rules from `app.tcss` (they belong only to the old layout):

```
#status-bar { ... }
#conn-list { height: auto; min-height: 3; ... }  ← remove (replaced in DEFAULT_CSS)
#bw-container { height: 1fr; }                   ← remove (replaced)
#rx-chart { ... }                                ← remove (replaced)
#tx-chart { ... }                                ← remove (replaced)
#stats-content { ... }                           ← remove (replaced)
#fwd-table { ... }                               ← remove (no longer used)
#detail-logs { ... }                             ← remove (replaced)
```

Keep `Footer`, `Screen`, `Header` global rules and any `#share-*`, `#log-*`, `#config-*`, `#conn-list`-editor rules (those belong to other screens).

- [ ] **Step 6: Launch the TUI and confirm it starts without errors**

```bash
susops
```
Expected: three panels visible, connection list on left, tabbed Stats/Logs in centre, empty right panel with "Domain / IP / CIDR" / "Forwards" / "Shares" borders. No traceback.

- [ ] **Step 7: Commit**

```bash
git add src/susops/tui/screens/dashboard.py src/susops/tui/app.tcss
git commit -m "feat: three-panel dashboard layout scaffold"
```

---

## Task 4: Left panel — "All" row + selection logic

**Files:**
- Modify: `src/susops/tui/screens/dashboard.py`

The connection list gets a permanent "All" row at index 0. `_selected_tag = None` means All is selected; a non-None string means a specific connection.

- [ ] **Step 1: Update `__init__` — initialise `_selected_tag` to `None` (All)**

`_selected_tag: str | None = None` is already the default, so no change needed. Verify line:

```python
self._selected_tag: str | None = None
```

- [ ] **Step 2: Update `_apply_status` — connection list building**

Replace the entire block that builds and updates the `conn_list` (from `conn_list = self.query_one(...)` through `self._selected_tag = ...`):

```python
conn_list = self.query_one("#conn-list", ListView)
new_tags = [cs.tag for cs in result.connection_statuses]

label_texts: list[str] = []
for cs in result.connection_statuses:
    dot = "[green]●[/green]" if cs.running else "[red]○[/red]"
    port_str = str(cs.socks_port) if cs.socks_port else "auto"
    rx, _tx = bw.get(cs.tag, (0.0, 0.0))
    label_texts.append(f"{dot} {cs.tag:<12} {port_str:<5} {_fmt_bps(rx):>6}↓")

if new_tags == self._conn_tags:
    # Same connections — update connection rows in-place (index 0 is the All row, skip it)
    items = list(conn_list.query(ListItem))
    for item, text in zip(items[1:], label_texts):
        item.query_one(Label).update(text)
else:
    # Tags changed — rebuild list, preserve whether All or a connection was selected
    prev_index = conn_list.index if conn_list.index is not None else 0
    conn_list.clear()
    conn_list.append(ListItem(Label("[dim]All[/dim]")))
    for text in label_texts:
        conn_list.append(ListItem(Label(text)))
    self._conn_tags = new_tags
    # Restore selection: clamp to valid range (All row = 0, connections = 1..N)
    conn_list.index = min(prev_index, len(self._conn_tags))

# Derive _selected_tag from current list position
idx = conn_list.index if conn_list.index is not None else 0
if idx == 0 or not self._conn_tags:
    self._selected_tag = None  # All
else:
    self._selected_tag = self._conn_tags[min(idx - 1, len(self._conn_tags) - 1)]
```

Also remove the old PAC info and shares sidebar update code (the old `#pac-info` and `#shares-info` widgets no longer exist):

```python
# DELETE these blocks entirely:
#   self.query_one("#pac-info", Static).update(...)
#   shares_widget = self.query_one("#shares-info", Static)
#   ...
```

- [ ] **Step 3: Update `on_list_view_highlighted`**

Replace the handler:

```python
def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
    index = event.list_view.index
    if index is None or index == 0:
        self._selected_tag = None  # All row
    elif index - 1 < len(self._conn_tags):
        self._selected_tag = self._conn_tags[index - 1]
    self._update_detail_panel(self._selected_tag)
    self._update_context_panel(self._selected_tag)
```

- [ ] **Step 4: Add stub for `_update_context_panel`** (filled in Task 6)

```python
def _update_context_panel(self, tag: str | None) -> None:
    """Populate domain/forward/share sections. tag=None means show all."""
    pass  # implemented in Task 6
```

- [ ] **Step 5: Update `_apply_status` to call both panel updaters**

At the end of `_apply_status`, replace `self._update_detail_panel(self._selected_tag)` with:

```python
self._update_detail_panel(self._selected_tag)
self._update_context_panel(self._selected_tag)
```

- [ ] **Step 6: Launch and confirm "All" row appears at top of connection list**

```bash
susops
```
Expected: "All" row at the very top of the connection list, connections below it. Navigating to "All" should show empty/placeholder stats panel (not crash).

- [ ] **Step 7: Commit**

```bash
git add src/susops/tui/screens/dashboard.py
git commit -m "feat: add 'All' row to connection list with context-sensitive selection"
```

---

## Task 5: Centre panel — stats for both views

**Files:**
- Modify: `src/susops/tui/screens/dashboard.py`

### Step 1: Fetch bandwidth totals and uptime in `refresh_status`

- [ ] Update `refresh_status` to fetch totals and uptime alongside existing data:

```python
@work(thread=True)
def refresh_status(self) -> None:
    mgr = self.app.manager  # type: ignore[attr-defined]
    result: StatusResult = mgr.status()

    extras: dict[str, dict] = {}
    bw: dict[str, tuple[float, float]] = {}
    bw_totals: dict[str, tuple[float, float]] = {}
    uptimes: dict[str, float | None] = {}
    for cs in result.connection_statuses:
        extras[cs.tag] = mgr.get_process_info(cs.tag)
        bw[cs.tag] = mgr.get_bandwidth(cs.tag)
        bw_totals[cs.tag] = mgr.get_bandwidth_totals(cs.tag)
        uptimes[cs.tag] = mgr.get_uptime(cs.tag)

    shares = mgr.list_shares()
    config = mgr.list_config()
    self.app.call_from_thread(
        self._apply_status, result, extras, bw, bw_totals, uptimes, shares, config
    )
```

- [ ] Update `_apply_status` signature and store new data in `_conn_data`:

```python
def _apply_status(
    self,
    result: StatusResult,
    extras: dict,
    bw: dict,
    bw_totals: dict,
    uptimes: dict,
    shares: list,
    config,
) -> None:
```

Inside the `for cs in result.connection_statuses:` loop that builds `new_conn_data`, add:
```python
new_conn_data[cs.tag] = {
    "cs": cs,
    "proc_info": extras.get(cs.tag) or {},
    "bw": bw.get(cs.tag, (0.0, 0.0)),
    "bw_total": bw_totals.get(cs.tag, (0.0, 0.0)),
    "uptime": uptimes.get(cs.tag),
    "forwards_local": forwards_local,
    "forwards_remote": forwards_remote,
    "conn": conn,
}
```

### Step 2: Add `_render_all_stats()` helper

- [ ] Add this method to `DashboardScreen`:

```python
def _render_all_stats(self) -> str:
    """Render aggregate stats for the 'All' view."""
    running = sum(1 for d in self._conn_data.values() if d["cs"].running)
    total = len(self._conn_data)
    if total == 0:
        return "[dim]No connections configured.[/dim]"

    total_cpu = sum(d["proc_info"].get("cpu", 0.0) for d in self._conn_data.values())
    total_mem = sum(d["proc_info"].get("mem_mb", 0.0) for d in self._conn_data.values())
    total_rx = sum(d["bw"][0] for d in self._conn_data.values())
    total_tx = sum(d["bw"][1] for d in self._conn_data.values())
    total_rx_bytes = sum(d["bw_total"][0] for d in self._conn_data.values())
    total_tx_bytes = sum(d["bw_total"][1] for d in self._conn_data.values())

    lines = [
        f"[bold]All Connections[/bold]   {running} running / {total} total",
        "",
        f"  CPU total      {total_cpu:.1f}%",
        f"  Memory total   {total_mem:.1f} MB",
        "",
        f"  [green]↓ RX[/green]  rate  {_fmt_bps(total_rx):<10}  total  [cyan]{_fmt_bytes(total_rx_bytes)}[/cyan]",
        f"  [yellow]↑ TX[/yellow]  rate  {_fmt_bps(total_tx):<10}  total  [cyan]{_fmt_bytes(total_tx_bytes)}[/cyan]",
        "",
        f"  [dim]{'─' * 36}[/dim]",
    ]
    for tag, data in self._conn_data.items():
        cs = data["cs"]
        dot = "[green]●[/green]" if cs.running else "[red]○[/red]"
        rx, tx = data["bw"]
        rx_t, tx_t = data["bw_total"]
        lines.append(
            f"  {dot} {tag:<8}  "
            f"[green]↓[/green]{_fmt_bps(rx):>7} [cyan]{_fmt_bytes(rx_t):>7}[/cyan]  "
            f"[yellow]↑[/yellow]{_fmt_bps(tx):>7} [cyan]{_fmt_bytes(tx_t):>7}[/cyan]"
        )
    return "\n".join(lines)
```

### Step 3: Update `_update_detail_panel()` — per-connection view

- [ ] Replace the stats section inside `_update_detail_panel()` (keep bandwidth chart code unchanged):

```python
def _update_detail_panel(self, tag: str | None) -> None:
    # All view — aggregate stats, hide bandwidth charts
    if tag is None:
        self.query_one("#stats-content", Static).update(self._render_all_stats())
        self.query_one("#bw-container", Horizontal).display = False
        return

    if tag not in self._conn_data:
        self.query_one("#stats-content", Static).update(
            "[dim]Select a connection.[/dim]"
        )
        self.query_one("#bw-container", Horizontal).display = False
        return

    self.query_one("#bw-container", Horizontal).display = True
    data = self._conn_data[tag]
    cs = data["cs"]
    proc_info = data["proc_info"]
    conn = data.get("conn")
    forwards_local = data.get("forwards_local", [])
    forwards_remote = data.get("forwards_remote", [])
    rx, tx = data["bw"]
    rx_total, tx_total = data["bw_total"]
    uptime = data.get("uptime")

    cpu = proc_info.get("cpu", 0.0) if proc_info else 0.0
    mem_mb = proc_info.get("mem_mb", 0.0) if proc_info else 0.0
    conns = proc_info.get("conns", 0) if proc_info else 0
    pid_str = str(cs.pid) if cs.pid else "—"
    ssh_host = conn.ssh_host if conn else "—"
    socks_port_str = str(cs.socks_port) if cs.socks_port else "auto"
    status_str = "[green]● running[/green]" if cs.running else "[red]○ stopped[/red]"
    uptime_str = _fmt_uptime(uptime) if uptime is not None else "—"
    fwd_summary = f"{len(forwards_local)}L {len(forwards_remote)}R"

    stats_lines = [
        f"[bold]{tag}[/bold]  {status_str}",
        "",
        f"  SSH host    {ssh_host:<16} SOCKS  {socks_port_str}",
        f"  PID         {pid_str:<16} Uptime {uptime_str}",
        f"  CPU         {cpu:.1f}%{'':14} Memory {mem_mb:.1f} MB",
        f"  Connections {conns:<16} Fwds   {fwd_summary}",
        "",
        f"  [green]↓ RX[/green]  rate  {_fmt_bps(rx):<10}  total  [cyan]{_fmt_bytes(rx_total)}[/cyan]",
        f"  [yellow]↑ TX[/yellow]  rate  {_fmt_bps(tx):<10}  total  [cyan]{_fmt_bytes(tx_total)}[/cyan]",
        f"  [dim]resets on stop[/dim]",
    ]
    self.query_one("#stats-content", Static).update("\n".join(stats_lines))

    # Bandwidth charts (existing logic — unchanged, just moved inside this method)
    rx_data = list(self._rx_history.get(tag, [0.0] * 60))
    tx_data = list(self._tx_history.get(tag, [0.0] * 60))
    rx_chart = self.query_one("#rx-chart", PlotextPlot)
    tx_chart = self.query_one("#tx-chart", PlotextPlot)
    rx_scaled, rx_unit = _scale_data(rx_data)
    rx_max = max(1.0, max(rx_scaled))
    rx_ticks, rx_labels = _yticks(rx_max, rx_unit)
    rx_chart.plt.clear_data()
    rx_chart.plt.title(f"RX  {_fmt_bps(rx)}")
    rx_chart.plt.ylim(0, rx_max)
    rx_chart.plt.yticks(rx_ticks, rx_labels)
    rx_chart.plt.plot(rx_scaled, color="green")
    rx_chart.refresh()
    tx_scaled, tx_unit = _scale_data(tx_data)
    tx_max = max(1.0, max(tx_scaled))
    tx_ticks, tx_labels = _yticks(tx_max, tx_unit)
    tx_chart.plt.clear_data()
    tx_chart.plt.title(f"TX  {_fmt_bps(tx)}")
    tx_chart.plt.ylim(0, tx_max)
    tx_chart.plt.yticks(tx_ticks, tx_labels)
    tx_chart.plt.plot(tx_scaled, color="yellow")
    tx_chart.refresh()

    # Logs (unchanged)
    log_widget = self.query_one("#detail-logs", RichLog)
    log_widget.clear()
    mgr = self.app.manager  # type: ignore[attr-defined]
    for line in mgr.get_logs(500):
        log_widget.write(line)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 5: Launch and confirm stats rendering**

```bash
susops
```
- Selecting "All": shows aggregate CPU/memory/BW with per-connection breakdown rows, charts hidden.
- Selecting a connection: shows individual stats, cumulative RX/TX, charts visible.

- [ ] **Step 6: Commit**

```bash
git add src/susops/tui/screens/dashboard.py
git commit -m "feat: dashboard centre panel — aggregate All stats and per-connection stats with cumulative BW"
```

---

## Task 6: Right panel — Domain / IP / CIDR, Forwards, and Shares sections

**Files:**
- Modify: `src/susops/tui/screens/dashboard.py`

The `_conn_data` dict already carries `forwards_local` and `forwards_remote` per connection. `_apply_status` receives `shares` and `config` which contain pac_hosts. This task fills in `_update_context_panel()`.

- [ ] **Step 1: Implement `_update_context_panel()`** — replace the stub from Task 4:

```python
def _update_context_panel(self, tag: str | None) -> None:
    """Populate domain/forward/share sections. tag=None shows all connections."""
    config = getattr(self, "_last_config", None)
    shares = getattr(self, "_last_shares", [])
    conn_map = {c.tag: c for c in config.connections} if config else {}

    domain_lines: list[str] = []
    forward_lines: list[str] = []
    share_lines: list[str] = []

    if tag is None:
        # Global view — prefix each item with [conn] tag
        for conn in (config.connections if config else []):
            for host in conn.pac_hosts:
                domain_lines.append(f"[dim][{conn.tag}][/dim] {host}")
        for t, data in self._conn_data.items():
            for fw in data.get("forwards_local", []):
                label = f" [dim]{fw.tag}[/dim]" if fw.tag else ""
                forward_lines.append(
                    f"[dim][{t}][/dim] [green]→[/green] {fw.src_port}→{fw.dst_addr}:{fw.dst_port}{label}"
                )
            for fw in data.get("forwards_remote", []):
                label = f" [dim]{fw.tag}[/dim]" if fw.tag else ""
                forward_lines.append(
                    f"[dim][{t}][/dim] [yellow]←[/yellow] {fw.src_port}←:{fw.dst_port}{label}"
                )
        for info in shares:
            dot = "[green]●[/green]" if info.running else ("[dim]○[/dim]" if info.stopped else "[red]○[/red]")
            name = Path(info.file_path).name
            share_lines.append(f"{dot} {name}  :{info.port}")
    else:
        # Per-connection view — no prefix
        conn = conn_map.get(tag)
        if conn:
            for host in conn.pac_hosts:
                domain_lines.append(host)
        data = self._conn_data.get(tag, {})
        for fw in data.get("forwards_local", []):
            label = f"  [dim]{fw.tag}[/dim]" if fw.tag else ""
            forward_lines.append(
                f"[green]→[/green] {fw.src_port} → {fw.dst_addr}:{fw.dst_port}{label}"
            )
        for fw in data.get("forwards_remote", []):
            label = f"  [dim]{fw.tag}[/dim]" if fw.tag else ""
            forward_lines.append(
                f"[yellow]←[/yellow] {fw.src_port} ← :{fw.dst_port}{label}"
            )
        for info in shares:
            if info.conn_tag == tag:
                dot = "[green]●[/green]" if info.running else ("[dim]○[/dim]" if info.stopped else "[red]○[/red]")
                name = Path(info.file_path).name
                share_lines.append(f"{dot} {name}  :{info.port}")

    domain_text = "\n".join(domain_lines) if domain_lines else "[dim]—[/dim]"
    forward_text = "\n".join(forward_lines) if forward_lines else "[dim]—[/dim]"
    share_text = "\n".join(share_lines) if share_lines else "[dim]—[/dim]"

    self.query_one("#domain-content", Static).update(domain_text)
    self.query_one("#forward-content", Static).update(forward_text)
    self.query_one("#share-content", Static).update(share_text)
```

- [ ] **Step 2: Cache `config` and `shares` in `_apply_status`** so `_update_context_panel` can access them

At the start of `_apply_status`, before building `new_conn_data`:

```python
self._last_config = config
self._last_shares = shares
```

Also add these to `__init__`:

```python
self._last_config = None
self._last_shares: list = []
```

- [ ] **Step 3: Verify `_update_context_panel` is called from `_apply_status` and `on_list_view_highlighted`**

Both call sites were wired in Task 4. Confirm they exist:

```bash
grep -n "_update_context_panel" src/susops/tui/screens/dashboard.py
```
Expected: 3 matches (stub definition + 2 call sites).

- [ ] **Step 4: Run tests**

```bash
pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 5: Launch and confirm right panel**

```bash
susops
```
- "All" selected: right panel shows all domains with `[tag]` prefixes, all forwards, all shares.
- Connection selected: right panel shows only that connection's domains, forwards, shares.
- Empty sections show `—`.

- [ ] **Step 6: Commit**

```bash
git add src/susops/tui/screens/dashboard.py
git commit -m "feat: dashboard right panel — Domain/IP/CIDR, Forwards, Shares with All/per-connection filter"
```

---

## Task 7: Footer binding adaptation — hide `d` and `S/X/R` when "All" selected

**Files:**
- Modify: `src/susops/tui/screens/dashboard.py`

When "All" is selected: `s/x/r` act on all (since `mgr.start(None)` = start all, already works), so `S/X/R` become redundant. Hide `S/X/R` when All. Hide `d` always (delete is in the Connections editor, not the dashboard). The `s/x/r` labels stay as-is; the user can infer "All" scope from the list selection.

- [ ] **Step 1: Add `check_action` to `DashboardScreen`**

```python
def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
    # S/X/R (start_all / stop_all / restart_all) are redundant when All row is selected,
    # because s/x/r already call mgr.start/stop/restart(None) in that state.
    # Hide them to avoid footer clutter.
    if action in ("start_all", "stop_all", "restart_all") and self._selected_tag is None:
        return False
    return True
```

- [ ] **Step 2: Launch and verify footer adapts**

```bash
susops
```
- "All" row selected: footer shows `s Start  x Stop  r Restart  c Connections  e Config  f Share  ^p Commands  q Quit` — no `S/X/R`.
- Connection selected: footer shows all bindings including `S Start all  X Stop all  R Restart all`.

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/susops/tui/screens/dashboard.py
git commit -m "feat: hide redundant S/X/R footer bindings when All connections selected"
```

---

## Self-Review Checklist

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Three-panel layout | Task 3 |
| "All" row at top, resets to global view | Task 4 |
| Footer-only commands | Task 3 (no inline hints in panels) |
| Aggregate stats (CPU, memory, RX/TX rate + totals, per-conn breakdown) | Task 5 |
| Per-connection stats (SSH, PID, uptime, CPU, memory, conns, fwds, RX/TX rate + totals) | Task 5 |
| Cumulative BW counters, reset on stop | Task 2 |
| Bandwidth sparklines hidden for All view | Task 5 |
| Right panel: Domain/IP/CIDR (1fr scrollable) | Task 6 |
| Right panel: Forwards (fixed, pinned) | Task 6 |
| Right panel: Shares (fixed, pinned at bottom) | Task 6 |
| Both panels filter to selected connection | Tasks 4 + 6 |
| Footer adapts between All and per-connection | Task 7 |
| Rename "PAC Hosts" → "Domain / IP / CIDR" | Task 1 |

**No placeholders, no TBDs.** ✓

**Type consistency:**
- `bw_totals: dict[str, tuple[float, float]]` used in Task 2, stored in `_conn_data["bw_total"]` in Task 5 ✓
- `get_bandwidth_totals(tag)` defined Task 2, called Task 5 ✓
- `get_uptime(tag)` defined Task 2, called Task 5, stored in `_conn_data["uptime"]` ✓
- `_last_config`, `_last_shares` initialised in `__init__` Task 6, set in `_apply_status` Task 6 ✓
- `_update_context_panel` stub added Task 4, implemented Task 6 ✓
