# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install for development (venv at .venv/)
pip install -e ".[tui,crypto,dev]"

# Run all tests
pytest

# Run a single test file
pytest tests/test_facade.py

# Run a single test by name
pytest tests/test_pac.py::test_pac_server_reload -v

# Run tests with coverage
pytest --cov=susops --cov-report=term-missing

# Launch the TUI
susops        # or: python -m susops.tui

# Launch as non-interactive CLI
susops ps
susops ls
susops start
susops stop

# Launch tray app (Linux; requires system GTK3 packages)
susops-tray
```

## Architecture

Three frontends share a **single `SusOpsManager` facade** — changes to the facade or core must be reflected in all three:

| Frontend    | Entry point                | Notes                                         |
|-------------|----------------------------|-----------------------------------------------|
| TUI         | `src/susops/tui/`          | Textual 8.2.3 + textual-plotext 1.0.1         |
| Tray Linux  | `src/susops/tray/linux.py` | GTK3 + AyatanaAppIndicator3 (system packages) |
| Tray macOS  | `src/susops/tray/mac.py`   | rumps + PyObjC                                |

```
src/susops/
  facade.py          # SusOpsManager — only public API any frontend should use
  core/
    config.py        # Pydantic v2 models + ruamel.yaml I/O
    ssh.py           # autossh/ssh subprocess + PID tracking
    pac.py           # PAC JS generation + Python HTTP server (daemon thread)
    share.py         # AES-256-CTR HTTP file sharing + client fetch
    process.py       # PID-file-based process manager (~/.susops/pids/)
    ports.py         # free port allocation, CIDR helpers
    types.py         # ProcessState enum, result dataclasses (StartResult etc.)
  tui/
    __main__.py      # dual-mode: TUI if isatty() + no subcommand, else CLI dispatch
    cli.py           # argparse non-interactive CLI
    app.py           # SusOpsTuiApp (Textual App subclass), CSS_PATH=app.tcss
    app.tcss         # global CSS theme for all screens
    screens/         # one file per screen (dashboard, connection_editor, share, log_viewer, config_editor)
    widgets/         # connection_card.py (only active custom widget)
  tray/
    base.py          # AbstractTrayApp — all shared business logic
    linux.py         # GTK3 implementation of abstract methods
    mac.py           # rumps implementation of abstract methods
```

## Key Design Patterns

**Facade is the only entry point.** Never import from `susops.core.*` in a frontend — always go through `SusOpsManager`. The facade owns config I/O, PID file management, bandwidth sampling, and the PAC/share server lifecycle.

**Thread safety in the TUI.** All blocking calls (start/stop/restart, share, fetch, editor launch) use `@work(thread=True)`. Results are pushed back with `self.app.call_from_thread(...)`, never `self.call_from_thread(...)` (which doesn't exist on `Screen`).

**Bandwidth sampling.** `_BandwidthSampler` (inside `facade.py`) is a daemon thread that reads `read_chars`/`write_chars` from `/proc/pid/io` every 2 s — these include socket I/O, unlike `read_bytes`/`write_bytes` which only count disk. `get_bandwidth(tag)` is a non-blocking dict lookup.

**Multi-share.** `_share_servers: dict[int, tuple[ShareServer, ShareInfo]]` in the facade is keyed by port, allowing concurrent shares on different ports.

**TUI dashboard.** Split-pane: 32-col `VerticalScroll` sidebar (Connections `ListView`, PAC Static, Shares Static) + `TabbedContent` detail panel (Stats / Bandwidth with `PlotextPlot` / Forwards `DataTable` / Logs `RichLog`). Selection in the `ListView` drives the right panel via `on_list_view_highlighted`.

**Modal dialogs.** All dialogs subclass `ModalScreen` (not `Screen`) so Textual dims the background automatically. They use `self.dismiss(data_dict)` to return results to the caller via the push_screen callback.

**Tray abstraction.** `AbstractTrayApp.do_*` methods contain all business logic. Platform subclasses implement `update_icon`, `update_menu_sensitivity`, `show_alert`, `show_output_dialog`, `run_in_background`, and `schedule_poll`.

## Config & Runtime State

- Config file: `~/.susops/config.yaml` (Pydantic model, ruamel.yaml preserves comments)
- PID files: `~/.susops/pids/susops-ssh-<tag>.pid`, `susops-pac.pid`
- Port `0` in config means auto-assign at start; the chosen port is written back to config
- `ephemeral_ports: true` in `susops_app:` section skips the write-back (ports stay 0)

## Adding a New Feature Checklist

1. Add method to `SusOpsManager` in `facade.py`
2. Update **TUI** screen(s) that expose the feature
3. Update **`AbstractTrayApp`** (`tray/base.py`) with a `do_*` method
4. Update **`linux.py`** and **`mac.py`** to wire the new menu item / dialog
5. Add tests in `tests/`
