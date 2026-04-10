# TUI Dashboard Redesign

**Date:** 2026-04-10
**Goal:** Minimise clicks — make the dashboard the one-stop shop for all common operations.

---

## Problem Statement

The current TUI requires unnecessary navigation:

- Starting/stopping a connection from the Connections screen is not possible — you must go to the dashboard.
- Shares and PAC hosts are visible in the dashboard sidebar but without sufficient detail.
- The stats panel shows basic info; there are no cumulative bandwidth counters.
- Users frequently switch between Dashboard, Connections, Share, and Config screens for operations that should all be reachable in one place.

---

## Design

### Layout: Three-Panel Dashboard

Replace the current two-pane dashboard (sidebar + tabbed detail) with a **three-panel layout**:

```
┌─ Connections ──────┬──── Stats / Bandwidth ─────────────────┬─ [context] ─────────┐
│  All               │                                        │ Domain / IP / CIDR   │
│ ● pi3    64726 ↓0B/s│                                        │ ...                  │
│ ● mash   63731 ↓0B/s│                                        ├─ Forwards ───────────┤
│                    │                                        │ ...                  │
│                    │                                        ├─ Shares ─────────────┤
│                    │                                        │ ...                  │
└────────────────────┴────────────────────────────────────────┴──────────────────────┘
  s Start  x Stop  r Restart  a Add  d Delete  ...                             v3.0.0
```

**Left panel (~20 cols):** Connection list with live status dots and current bandwidth rate.
**Centre panel (1fr):** Stats and bandwidth chart for the selected context.
**Right panel (~22 cols):** Domain/IP/CIDR entries, Forwards, and Shares — filtered by selection.

No action hints inside panels. All commands live exclusively in the footer bar.

---

### Left Panel — Connection List

- Permanent **`All`** row at the top of the list (always the first item).
- Below it: one row per connection showing `● tag   port  ↓rate`.
- Navigating to a connection filters the centre and right panels to that connection.
- Navigating to `All` resets both panels to the global/aggregated view.
- The footer adapts:
  - `All` selected: `S` Start all, `X` Stop all, `R` Restart all, `a` Add connection. `d` Delete is hidden (no target).
  - Connection selected: `s` Start, `x` Stop, `r` Restart, `a` Add connection, `d` Delete selected connection.

---

### Centre Panel — Stats

**When `All` is selected:**

```
All Connections   2 running / 2 total

CPU total     0.4%
Memory total  5.8 MB

↓ RX  rate  142 kB/s   total  1.2 GB
↑ TX  rate   18 kB/s   total  84 MB

── per connection ───────────────────────────
● pi3   ↓ 102kB/s  900MB  ↑ 12kB/s  60MB
● mash  ↓  40kB/s  300MB  ↑  6kB/s  24MB
```

- Aggregate CPU and memory (sum of all running connections).
- Aggregate RX/TX current rate and cumulative totals.
- Per-connection breakdown rows below the aggregate section.
- No bandwidth sparkline chart in All view (not meaningful when aggregated).

**When a specific connection is selected:**

```
pi3  ● running

SSH host    pi3          SOCKS   64726
PID         12481        Uptime  2h 14m
CPU         0.1%         Memory  2.1 MB
Connections 3            Fwds    2L 1R

↓ RX  rate  102 kB/s   total  900 MB
↑ TX  rate   12 kB/s   total   60 MB
resets on stop

RX ▁▂▃▄▅▆▄▃▂▁▂▃▅▆▇▆▅▄▃▂▁▂▃▄▅▆▅▄▃▂▁
TX ▁▁▁▂▁▁▁▂▁▁▁▂▁▁▁▂▁▁▁▂▁▁▁▂▁▁▁▂▁▁▁
```

- Full individual stats: SSH host, SOCKS port, PID, uptime, CPU, memory, active connections, forward counts.
- RX/TX current rate and cumulative total since connection started.
- Bandwidth sparkline chart below (existing PlotextPlot charts, stacked vertically).
- Note "resets on stop" beneath the cumulative counters.

The bandwidth charts share available vertical space. If the terminal is short, they may be compact — this is acceptable and will be evaluated during implementation.

---

### Right Panel — Context Panel

Title updates to the selected connection name, or `All` when in global view.

Three stacked sections with deliberate height allocation, designed for realistic data volumes (10+ domains, 2-3 forwards, 1-2 shares):

**Domain / IP / CIDR** (renamed from "PAC Hosts") — `height: 1fr`, scrollable
- Takes all remaining vertical space after Forwards and Shares are allocated.
- Scrollable with a `↓ N more` indicator when content overflows.
- In All view: each entry prefixed with `[conn]` tag.
- In connection view: entries for that connection only, no prefix.

**Forwards** — fixed height (~5 rows), always visible, pinned above Shares
- In All view: each forward prefixed with `[conn]` tag, direction arrow `→` (local) or `←` (remote).
- In connection view: forwards for that connection only.

**Shares** — fixed height (~3 rows), always visible, pinned at bottom
- Status dot, filename, port for each share.
- In All view: all shares across connections.
- In connection view: shares belonging to that connection only.

Forwards and Shares are always visible regardless of domain count — they are never pushed off screen. All three sections are read-only in this panel; add/delete/manage via dedicated screens or modals.

---

### Cumulative Bandwidth Counters

New feature in `facade.py` / `_BandwidthSampler`:

- Track `rx_total` and `tx_total` (bytes) per connection tag, accumulating deltas from each 2-second sample.
- Both counters reset to 0 when `stop_tunnel()` is called for that connection (or `stop()` for all).
- `get_bandwidth(tag)` returns the existing rate dict extended with `rx_total` and `tx_total`.
- Aggregate totals in the All view are computed by summing across all running connections.

---

### Connections Screen — No Change to Core Behaviour

The Connections screen (CRUD editor) remains the place for adding, editing, and deleting connections, PAC hosts, and forwards. The dashboard does not replace this — it adds visibility and start/stop access without navigation.

The rename "PAC Hosts" → "Domain / IP / CIDR" applies everywhere: Connections screen tab label, right panel title, config editor display.

---

### Other Screens

Share, Config, and the command palette are unchanged. The dashboard becomes the primary operational view; the other screens remain accessible via footer bindings.

---

## Constraints & Notes

- Textual 8.2.3 layout: three `Horizontal` children with fixed/1fr widths.
- Right panel is a `Vertical` container. Domain section is a `VerticalScroll` with `height: 1fr`. Forwards and Shares are fixed-height `VerticalScroll` containers (~5 and ~3 rows respectively) always pinned at the bottom.
- The `All` row is a special `ListItem` rendered differently (no status dot, italic or dimmed style).
- SSE events continue to drive instant refresh on state changes.
- Bandwidth chart vertical space concern noted — evaluate during implementation and adjust panel heights if needed.
- No new screens are added. The dashboard replaces the need to navigate away for common read operations.

---

## Success Criteria

- Starting, stopping, or restarting any connection requires zero screen navigation — it's always one keypress from the dashboard.
- All domains, forwards, and shares for a given connection are visible without leaving the dashboard.
- Cumulative bandwidth totals are visible per-connection and in aggregate.
- No action hints appear inside panel bodies — footer only.
