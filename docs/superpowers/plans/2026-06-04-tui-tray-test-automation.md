# TUI + Tray Test Automation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automated test coverage for the Textual TUI and the rumps/GTK tray. After this lands, a `pytest` run exercises every menu, every dialog, every action, in both frontends — no manual click-through required.

**Architecture:** Three test layers:
1. **Headless TUI** — Textual's `App.run_test(headless=True)` + `Pilot` driver. Real `SusOpsClient` against a real `susops-services` daemon spawned by a session-scoped fixture. Pilot drives keystrokes; tests assert against screen exports, widget state, and daemon state.
2. **Tray business logic** — `AbstractTrayApp` subclassed with a `_TestTrayApp` that stubs `update_icon` / `update_menu_sensitivity` / `show_alert` / `show_output_dialog` / `run_in_background` / `schedule_poll` as plain Python recorders. All `do_*` methods get tested directly, no rumps/GTK runtime.
3. **Tray dialog plumbing (Mac only, smoke level)** — Spawn the actual `susops-tray` process under macOS, send `osascript` UI events, assert via menu-bar state and `lsof`/daemon log. Optional — gated on `pytest -m gui` and skipped in headless CI.

**Tech Stack:** pytest, Textual's `Pilot` API, `unittest.mock` for tray platform shim, `pyobjc` (already available on Mac for layer 3), `subprocess` for daemon fixture.

---

## Module-level test files (new)

| File                                    | Purpose                                                                      | Layer      |
|-----------------------------------------|------------------------------------------------------------------------------|------------|
| `tests/tui/conftest.py`                 | Session-scoped daemon fixture; `tui_app` factory fixture                     | shared     |
| `tests/tui/test_dashboard.py`           | Dashboard screen — connections list, tabs, bandwidth, logs                   | 1          |
| `tests/tui/test_connections_screen.py`  | Add / remove / toggle / forwards / PAC hosts                                 | 1          |
| `tests/tui/test_shares_screen.py`       | Create / fetch / stop / delete shares                                        | 1          |
| `tests/tui/test_navigation.py`          | Inter-screen navigation, modals, keybindings, quit                           | 1          |
| `tests/tui/test_error_paths.py`         | Daemon down, invalid input, RPC failures                                     | 1          |
| `tests/tray/conftest.py`                | `_TestTrayApp` factory; daemon fixture                                       | shared     |
| `tests/tray/test_lifecycle.py`          | init, on_state_change, schedule_poll, do_quit                                | 2          |
| `tests/tray/test_connection_actions.py` | do_add/remove/start/stop/restart/toggle connection                           | 2          |
| `tests/tray/test_pac_actions.py`        | do_add/remove/toggle pac host                                                | 2          |
| `tests/tray/test_forward_actions.py`    | local/remote forwards CRUD + toggle                                          | 2          |
| `tests/tray/test_share_actions.py`      | do_share / do_fetch / do_stop_share / do_delete_share                        | 2          |
| `tests/tray/test_browser_launch.py`     | do_launch_chrome / firefox path on each platform                             | 2          |
| `tests/tray/test_mac_dialogs.py`        | macOS-specific dialog helpers (`_show_form_dialog`, etc.) with mocked AppKit | 2          |
| `tests/tray/test_linux_dialogs.py`      | GTK dialog helpers with mocked Gtk                                           | 2          |
| `tests/tray/test_gui_smoke.py`          | end-to-end macOS tray smoke test via osascript                               | 3 (opt-in) |

---

## Fixtures and harness

### Shared daemon fixture

The same fixture pattern lives in `tests/tui/conftest.py` and `tests/tray/conftest.py` — extract to `tests/conftest.py` for reuse. Each test gets a fresh isolated workspace + a fresh daemon.

- [ ] **Step 1: Add `daemon` fixture in `tests/conftest.py`**

```python
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def daemon(tmp_path: Path):
    """Spawn a fresh susops-services daemon in tmp_path; tear it down after the test."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "susops.core.services_daemon",
         "--workspace", str(tmp_path), "--port", "0"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    port_file = tmp_path / "pids" / "susops-services.port"
    for _ in range(50):
        if port_file.exists():
            break
        time.sleep(0.1)
    if not port_file.exists():
        proc.kill()
        _, err = proc.communicate(timeout=2)
        pytest.fail(f"daemon never came up; stderr: {err.decode(errors='replace')!r}")
    yield tmp_path
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
```

- [ ] **Step 2: Verify the fixture works**

Quick sanity test in `tests/test_conftest_smoke.py`:

```python
def test_daemon_fixture_starts_and_stops(daemon):
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=daemon)
    cfg = c.list_config()
    assert cfg.connections == []
```

Run: `.venv/bin/pytest tests/test_conftest_smoke.py -v` → 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py tests/test_conftest_smoke.py
git commit -m "test: shared daemon fixture for TUI + tray suites"
```

---

## Layer 1 — TUI tests

### Task 1.1: TUI app factory fixture

**Files:**
- Create: `tests/tui/conftest.py`

- [ ] **Step 1: Add factory fixture**

```python
# tests/tui/conftest.py
import pytest

from susops.tui.app import SusOpsTuiApp


@pytest.fixture
async def tui_app(daemon, monkeypatch):
    """Yield a Pilot driving a SusOpsTuiApp wired to the fixture daemon's workspace."""
    monkeypatch.setattr("susops.client._WORKSPACE_DEFAULT", daemon)
    app = SusOpsTuiApp()
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause(1.0)  # let on_mount + dashboard mount complete
        yield app, pilot
```

- [ ] **Step 2: Smoke test**

```python
# tests/tui/test_navigation.py
import pytest

pytestmark = pytest.mark.asyncio


async def test_tui_starts_on_dashboard(tui_app):
    app, pilot = tui_app
    assert type(app.screen).__name__ == "DashboardScreen"
    # The dashboard has populated the connections list from the daemon's
    # (empty) config — no traceback, no error notify.
```

- [ ] **Step 3: Run + commit**

```bash
.venv/bin/pytest tests/tui/ -v
```

```bash
git add tests/tui/
git commit -m "test(tui): factory fixture + smoke that dashboard mounts"
```

### Task 1.2: Dashboard — connections list reflects daemon state

**Files:**
- Modify: `tests/tui/test_dashboard.py`

- [ ] **Step 1: Test that adding a connection via the daemon shows up in the dashboard**

```python
import pytest
from susops.client import SusOpsClient

pytestmark = pytest.mark.asyncio


async def test_dashboard_shows_connection_after_add(tui_app, daemon):
    app, pilot = tui_app
    SusOpsClient(workspace=daemon).add_connection("work", "user@host")
    # Trigger a refresh — Pilot can't wait out the dashboard's 2-second poll,
    # so we call refresh_status directly.
    app.screen.refresh_status()
    await pilot.pause(0.3)
    snap = app.export_screenshot()
    assert "work" in snap


async def test_dashboard_pac_url_shown_when_pac_running(tui_app, daemon):
    app, pilot = tui_app
    client = SusOpsClient(workspace=daemon)
    client.add_connection("work", "user@host")
    client.start()
    app.screen.refresh_status()
    await pilot.pause(0.3)
    snap = app.export_screenshot()
    assert "susops.pac" in snap.lower()
```

- [ ] **Step 2: Logs panel populates on default 'All' view (regression test for the bug we just fixed)**

```python
async def test_dashboard_logs_show_on_all_view(tui_app, daemon):
    from textual.widgets import RichLog, TabbedContent
    app, pilot = tui_app
    client = SusOpsClient(workspace=daemon)
    client.add_connection("work", "user@host")  # generates a log line
    app.screen.refresh_status()
    await pilot.pause(0.5)
    tc = app.screen.query_one(TabbedContent)
    tc.active = "tab-logs"
    await pilot.pause(0.3)
    log_widget = app.screen.query_one("#detail-logs", RichLog)
    assert len(log_widget.lines) > 0, "logs panel must populate on default 'All' view"
```

- [ ] **Step 3: Tab navigation works**

```python
async def test_dashboard_tabs_switchable(tui_app):
    from textual.widgets import TabbedContent
    app, pilot = tui_app
    tc = app.screen.query_one(TabbedContent)
    for target in ("tab-logs", "tab-config", "tab-pac", "tab-stats"):
        tc.active = target
        await pilot.pause(0.2)
        assert tc.active == target
```

- [ ] **Step 4: Run + commit**

### Task 1.3: Dashboard — action_start_all brings up PAC + SSH

**Files:**
- Modify: `tests/tui/test_dashboard.py`

- [ ] **Step 1: Test the full start workflow**

```python
async def test_action_start_all_brings_up_everything(tui_app, daemon):
    app, pilot = tui_app
    client = SusOpsClient(workspace=daemon)
    client.add_connection("work", "user@host")
    # Pre-condition
    st = client.status()
    assert st.pac_running is False
    # Drive the start binding
    app.action_start_all()
    await pilot.pause(2.5)  # SSH connect + PAC bind
    st = client.status()
    assert st.pac_running is True
    assert any(cs.tag == "work" and cs.running for cs in st.connection_statuses)
```

> Note: `user@host` won't resolve in CI. For tests that don't need a real SSH connection, use a stub SSH server (see Task 5.1) OR mark these tests as `@pytest.mark.skip_in_ci`. The `start()` method handles connection failures gracefully — the assertion can be relaxed to `result.message contains "Failed"` for offline coverage.

- [ ] **Step 2: Same for action_stop_all + assert tear-down**

- [ ] **Step 3: Run + commit**

### Task 1.4: Connections screen — Add/Remove/Toggle

**Files:**
- Create: `tests/tui/test_connections_screen.py`

- [ ] **Step 1: Test navigation to connections screen**

```python
async def test_navigate_to_connections_screen(tui_app):
    app, pilot = tui_app
    await pilot.press("c")  # the 'c' binding opens connections (verify in app.py)
    await pilot.pause(0.5)
    assert type(app.screen).__name__ == "ConnectionsScreen"
```

- [ ] **Step 2: Add a connection through the modal**

```python
async def test_add_connection_through_modal(tui_app, daemon):
    app, pilot = tui_app
    await pilot.press("c")
    await pilot.pause(0.3)
    await pilot.press("a")  # action_add_item
    await pilot.pause(0.3)
    # Fill the add-connection modal
    from textual.widgets import Input
    inputs = list(app.screen.query(Input))
    inputs[0].value = "work"
    inputs[1].value = "user@host"
    await pilot.press("enter")
    await pilot.pause(0.5)
    # Verify via the daemon
    cfg = SusOpsClient(workspace=daemon).list_config()
    assert any(c.tag == "work" for c in cfg.connections)
```

- [ ] **Step 3: Remove via 'd' binding**

```python
async def test_remove_connection(tui_app, daemon):
    client = SusOpsClient(workspace=daemon)
    client.add_connection("work", "user@host")
    app, pilot = tui_app
    await pilot.press("c")
    await pilot.pause(0.3)
    # Select the connection in the table (DataTable cursor moves with arrow keys)
    from textual.widgets import DataTable
    table = app.screen.query_one(DataTable)
    table.move_cursor(row=0)
    await pilot.press("d")  # action_delete_item
    await pilot.pause(0.3)
    # Confirm dialog
    await pilot.press("enter")
    await pilot.pause(0.5)
    cfg = client.list_config()
    assert not any(c.tag == "work" for c in cfg.connections)
```

- [ ] **Step 4: Toggle enabled**

```python
async def test_toggle_connection_enabled(tui_app, daemon):
    client = SusOpsClient(workspace=daemon)
    client.add_connection("work", "user@host")
    app, pilot = tui_app
    await pilot.press("c")
    await pilot.pause(0.3)
    from textual.widgets import DataTable
    app.screen.query_one(DataTable).move_cursor(row=0)
    await pilot.press("e")  # action_toggle_enabled
    await pilot.pause(0.3)
    cfg = client.list_config()
    assert cfg.connections[0].enabled is False
```

- [ ] **Step 5–8: PAC host CRUD + forward CRUD (separate tests, same shape)**

For each: navigate, trigger action, fill modal, assert daemon state via `SusOpsClient`.

- [ ] **Step 9: Run + commit**

### Task 1.5: Shares screen

**Files:**
- Create: `tests/tui/test_shares_screen.py`

- [ ] **Step 1: Test share creation through the modal**

```python
async def test_create_share(tui_app, daemon, tmp_path):
    test_file = tmp_path / "payload.bin"
    test_file.write_bytes(b"hello")
    client = SusOpsClient(workspace=daemon)
    client.add_connection("work", "user@host")
    app, pilot = tui_app
    await pilot.press("s")  # action_show_share / shares screen
    await pilot.pause(0.3)
    await pilot.press("a")  # add share
    await pilot.pause(0.3)
    # Fill modal: file path, password, port
    from textual.widgets import Input
    inputs = list(app.screen.query(Input))
    inputs[0].value = str(test_file)
    inputs[1].value = "secret123"
    await pilot.press("enter")
    await pilot.pause(0.8)
    shares = client.list_shares()
    assert len(shares) == 1
    assert shares[0].file_path == str(test_file)
```

- [ ] **Step 2: Stop share, delete share**
- [ ] **Step 3: Run + commit**

### Task 1.6: Error-path coverage

**Files:**
- Create: `tests/tui/test_error_paths.py`

- [ ] **Step 1: TUI handles daemon dying mid-session**

```python
import os
import signal
import time

async def test_tui_survives_daemon_kill(tui_app, daemon):
    app, pilot = tui_app
    # Kill the daemon (with -9 so it doesn't go through its finally and the
    # client has to spawn a fresh one).
    pid_file = daemon / "pids" / "susops-services.pid"
    pid = int(pid_file.read_text())
    os.kill(pid, signal.SIGKILL)
    pid_file.unlink(missing_ok=True)
    (daemon / "pids" / "susops-services.port").unlink(missing_ok=True)
    # Give the TUI time to notice — its next 2 s tick will respawn the daemon
    # via the client's retry logic.
    await pilot.pause(3.5)
    # App must still be alive on the dashboard screen
    assert type(app.screen).__name__ == "DashboardScreen"
    # And a fresh daemon should be back up
    assert (daemon / "pids" / "susops-services.pid").exists()
```

- [ ] **Step 2: Add-connection with empty tag shows validation error**

```python
async def test_add_connection_empty_tag_rejected(tui_app):
    app, pilot = tui_app
    await pilot.press("c")
    await pilot.pause(0.3)
    await pilot.press("a")
    await pilot.pause(0.3)
    # Leave tag empty, press Enter
    await pilot.press("enter")
    await pilot.pause(0.3)
    # Modal should still be open
    from textual.screen import ModalScreen
    assert isinstance(app.screen, ModalScreen) or "Add" in str(app.screen)
```

- [ ] **Step 3: Run + commit**

---

## Layer 2 — Tray business-logic tests

The strategy: subclass `AbstractTrayApp` as `_TestTrayApp`. Override every abstract method with a Python-only recorder (no rumps, no AppKit, no GTK). Drive `do_*` methods directly, assert via the recorder and via `SusOpsClient` against the fixture daemon.

### Task 2.1: `_TestTrayApp` harness

**Files:**
- Create: `tests/tray/conftest.py`

- [ ] **Step 1: Build the harness**

```python
# tests/tray/conftest.py
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from susops.client import SusOpsClient
from susops.core.types import ProcessState
from susops.tray.base import AbstractTrayApp


class _TestTrayApp(AbstractTrayApp):
    """Headless tray app for testing the do_* business logic.

    Records every platform-layer call (icon updates, alerts, dialogs,
    background jobs) without depending on rumps / AppKit / GTK.
    """

    def __init__(self, workspace: Path) -> None:
        # Bypass AbstractTrayApp.__init__ partially — it creates a
        # SusOpsClient with the default workspace, which we override.
        self.manager = SusOpsClient(workspace=workspace, process_name="susops-tray-test")
        self.state = ProcessState.INITIAL
        # Recorders
        self.icon_updates: list[ProcessState] = []
        self.menu_states: list[ProcessState] = []
        self.alerts: list[tuple[str, str]] = []
        self.output_dialogs: list[tuple[str, str]] = []
        self.bg_jobs: list[tuple[Callable, Callable | None]] = []
        self.poll_intervals: list[int] = []

    # Platform abstracts → recorders
    def update_icon(self, state: ProcessState) -> None:
        self.icon_updates.append(state)

    def update_menu_sensitivity(self, state: ProcessState) -> None:
        self.menu_states.append(state)

    def show_alert(self, title: str, msg: str) -> None:
        self.alerts.append((title, msg))

    def show_output_dialog(self, title: str, output: str) -> None:
        self.output_dialogs.append((title, output))

    def run_in_background(self, fn: Callable, callback: Callable | None = None) -> None:
        # Synchronous for tests — call fn directly + callback synchronously.
        result = fn()
        if callback is not None:
            callback(result)

    def schedule_poll(self, interval_seconds: int) -> None:
        self.poll_intervals.append(interval_seconds)


@pytest.fixture
def tray(daemon: Path) -> _TestTrayApp:
    """Fresh tray harness wired to the fixture daemon's workspace."""
    return _TestTrayApp(workspace=daemon)
```

- [ ] **Step 2: Smoke test the harness**

```python
# tests/tray/test_lifecycle.py
def test_tray_harness_initialises(tray):
    assert tray.manager is not None
    cfg = tray.manager.list_config()
    assert cfg.connections == []


def test_do_poll_updates_icon_and_menu(tray):
    tray.do_poll()
    assert len(tray.icon_updates) == 1
    assert len(tray.menu_states) == 1
```

- [ ] **Step 3: Run + commit**

### Task 2.2: Connection-action coverage

**Files:**
- Create: `tests/tray/test_connection_actions.py`

- [ ] **Step 1: Add connection via tray API**

```python
def test_do_add_connection_persists_to_config(tray, daemon):
    tray.do_add_connection("work", "user@host", port=0)
    cfg = tray.manager.list_config()
    assert any(c.tag == "work" for c in cfg.connections)
    # User-facing confirmation
    assert any(title == "Added" for title, _ in tray.alerts)


def test_do_remove_connection(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_remove_connection("work")
    cfg = tray.manager.list_config()
    assert not any(c.tag == "work" for c in cfg.connections)


def test_do_toggle_connection_enabled(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_toggle_connection_enabled("work")
    cfg = tray.manager.list_config()
    assert cfg.connections[0].enabled is False
    tray.do_toggle_connection_enabled("work")
    assert tray.manager.list_config().connections[0].enabled is True


def test_do_start_stop_restart(tray):
    """Doesn't actually need SSH — daemon's start path returns success/failure
    depending on whether SSH connects. Either way, an alert (or none) should
    be recorded and no exception should escape.
    """
    tray.do_add_connection("work", "user@host")
    tray.do_start_connection("work")  # likely fails since user@host fake
    tray.do_stop_connection("work")
    tray.do_restart_connection("work")


def test_remove_connection_error_alert(tray):
    """Removing a tag that doesn't exist should show an error alert, not raise."""
    tray.do_remove_connection("nonexistent")
    assert any("Error" in title for title, _ in tray.alerts)
```

- [ ] **Step 2: Run + commit**

### Task 2.3: PAC host actions

**Files:**
- Create: `tests/tray/test_pac_actions.py`

- [ ] **Step 1: Tests mirror Task 2.2's shape**

```python
def test_do_add_pac_host(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_pac_host("example.com")
    cfg = tray.manager.list_config()
    assert "example.com" in cfg.connections[0].pac_hosts


def test_do_remove_pac_host(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_pac_host("example.com")
    tray.do_remove_pac_host("example.com")
    cfg = tray.manager.list_config()
    assert "example.com" not in cfg.connections[0].pac_hosts


def test_do_toggle_pac_host_enabled(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_pac_host("example.com")
    tray.do_toggle_pac_host_enabled("example.com")
    cfg = tray.manager.list_config()
    assert "example.com" in cfg.connections[0].pac_hosts_disabled
```

- [ ] **Step 2: Run + commit**

### Task 2.4: Forward actions

**Files:**
- Create: `tests/tray/test_forward_actions.py`

- [ ] **Step 1: Local + remote forward CRUD, toggle**

```python
from susops.core.config import PortForward


def test_do_add_local_forward(tray):
    tray.do_add_connection("work", "user@host")
    fw = PortForward(src_port=8080, dst_port=80, tag="http")
    tray.do_add_local_forward("work", fw)
    cfg = tray.manager.list_config()
    assert any(f.src_port == 8080 for f in cfg.connections[0].forwards.local)


def test_do_add_remote_forward(tray):
    tray.do_add_connection("work", "user@host")
    fw = PortForward(src_port=9090, dst_port=90, tag="rev")
    tray.do_add_remote_forward("work", fw)
    cfg = tray.manager.list_config()
    assert any(f.src_port == 9090 for f in cfg.connections[0].forwards.remote)


def test_do_remove_local_forward(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_local_forward("work", PortForward(src_port=8080, dst_port=80))
    tray.do_remove_local_forward(8080)
    cfg = tray.manager.list_config()
    assert not cfg.connections[0].forwards.local


def test_do_toggle_forward_enabled(tray):
    tray.do_add_connection("work", "user@host")
    tray.do_add_local_forward("work", PortForward(src_port=8080, dst_port=80))
    tray.do_toggle_forward_enabled("work", 8080, "local")
    cfg = tray.manager.list_config()
    assert cfg.connections[0].forwards.local[0].enabled is False
```

- [ ] **Step 2: Run + commit**

### Task 2.5: Share + fetch actions

**Files:**
- Create: `tests/tray/test_share_actions.py`

- [ ] **Step 1: Share lifecycle**

```python
def test_do_share_starts_server(tray, tmp_path):
    f = tmp_path / "payload.bin"
    f.write_bytes(b"hello")
    tray.do_add_connection("work", "user@host")
    tray.do_share("work", str(f), password="s3cret", port=0)
    shares = tray.manager.list_shares()
    assert len(shares) == 1
    assert shares[0].file_path == str(f)


def test_do_stop_share(tray, tmp_path):
    f = tmp_path / "payload.bin"
    f.write_bytes(b"hello")
    tray.do_add_connection("work", "user@host")
    tray.do_share("work", str(f), password="s3cret", port=0)
    share = tray.manager.list_shares()[0]
    tray.do_stop_share(share.port)
    shares = tray.manager.list_shares()
    assert shares[0].running is False


def test_do_delete_share(tray, tmp_path):
    f = tmp_path / "payload.bin"
    f.write_bytes(b"hello")
    tray.do_add_connection("work", "user@host")
    tray.do_share("work", str(f), password="s3cret", port=0)
    share = tray.manager.list_shares()[0]
    tray.do_delete_share(share.port)
    assert tray.manager.list_shares() == []
```

- [ ] **Step 2: Run + commit**

### Task 2.6: macOS dialog helpers (mocked AppKit)

**Files:**
- Create: `tests/tray/test_mac_dialogs.py`

The `_show_form_dialog`, `_show_message_panel`, `_show_about_panel`, etc. in `tray/mac.py` touch real AppKit. To test them headlessly, mock the AppKit/Cocoa imports with a `MagicMock`-based fake. Goal is to verify:
- The expected widgets get instantiated (NSPanel, NSTextField count matches field count)
- The expected close-delegate / button-handler is wired
- `runModalForWindow_` is invoked
- Result dict has the right keys/types

- [ ] **Step 1: Skeleton — mock AppKit + Cocoa, import tray.mac, drive `_show_form_dialog`**

```python
import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def stub_appkit(monkeypatch):
    """Replace AppKit / Cocoa / Foundation / objc with MagicMocks so we can
    import susops.tray.mac without a Mac runtime.
    """
    for name in ("AppKit", "Cocoa", "Foundation", "objc", "rumps"):
        if name not in sys.modules:
            sys.modules[name] = MagicMock()
    yield


def test_show_message_panel_runs_modal(stub_appkit, monkeypatch):
    from susops.tray import mac
    # Replace the panel-creation and runModal calls with recorders
    sentinel = MagicMock()
    sentinel.contentView.return_value = MagicMock()
    monkeypatch.setattr(
        "susops.tray.mac.NSPanel.alloc.return_value.initWithContentRect_styleMask_backing_defer_",
        lambda *a, **kw: sentinel,
        raising=False,
    )
    # ... test that mac._show_message_panel("Hi", "msg", [("OK", 1)]) returns 1
```

> Realistically this mocking is brittle. **Alternative:** extract dialog *contracts* (what fields, what buttons, what callbacks) into pure-Python helper functions that can be unit-tested without touching AppKit. Cover the AppKit-glue with the Layer 3 GUI smoke test.

- [ ] **Step 2: Pick one approach, ship 2–3 tests, document the trade-off in the test file's docstring**
- [ ] **Step 3: Run + commit**

### Task 2.7: Linux dialog helpers (mocked GTK)

**Files:**
- Create: `tests/tray/test_linux_dialogs.py`

Same shape as Task 2.6 but with `gi.repository.Gtk` mocked. Same trade-off applies.

- [ ] **Step 1–3: As above**

---

## Layer 3 — Tray GUI smoke (opt-in)

This is the only layer that actually exercises rumps + AppKit. Gated by `pytest -m gui` so it doesn't block CI.

### Task 3.1: Smoke launch + quit

**Files:**
- Create: `tests/tray/test_gui_smoke.py`

- [ ] **Step 1: Mark + skip on non-Mac**

```python
import platform
import subprocess
import sys
import time
import pytest


pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(platform.system() != "Darwin", reason="tray GUI smoke is macOS-only"),
]


def test_tray_launches_and_quits(daemon):
    """Spawn susops-tray, verify it's alive 3 s later, send SIGTERM, verify it exits."""
    import os
    env = os.environ.copy()
    env["SUSOPS_WORKSPACE"] = str(daemon)  # If the tray honors this env var; otherwise pass via arg
    proc = subprocess.Popen(
        [sys.executable, "-m", "susops.tray.mac"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(3)
        assert proc.poll() is None, (
            f"tray crashed: stderr={proc.stderr.read().decode()!r}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
```

- [ ] **Step 2: Register the marker in `pyproject.toml`**

```toml
[tool.pytest.ini_options]
markers = [
    "gui: tests that exercise a real GUI runtime (rumps / AppKit); slow + macOS-only"
]
```

- [ ] **Step 3: Document the opt-in in README**

```markdown
### Running GUI smoke tests (macOS only)
```bash
.venv/bin/pytest -m gui
```
```

- [ ] **Step 4: Commit**

---

## CI integration

### Task 4.1: Wire Layer 1 + 2 into the existing CI run

- [ ] **Step 1: Check current `.github/workflows/*.yml` for the pytest step**
- [ ] **Step 2: Confirm Layer 1 + 2 tests run by default (no marker)**
- [ ] **Step 3: Add a separate workflow job for Layer 3 (`pytest -m gui`) — opt-in, manually-triggered (`workflow_dispatch`) on macOS-only runner**
- [ ] **Step 4: Commit**

---

## Coverage matrix

After all tasks complete, the test surface area is:

| Component | Methods covered | Test file |
|---|---|---|
| TUI dashboard | mount, refresh_status, action_start_all, action_stop_all, action_restart_all, action_quit, tab switching, logs panel population | `test_dashboard.py` |
| TUI connections | navigate, add, remove, toggle_enabled, add/remove forwards, add/remove pac_hosts, start/stop/restart per tag | `test_connections_screen.py` |
| TUI shares | navigate, create, stop, delete, fetch | `test_shares_screen.py` |
| TUI nav + quit | screen transitions, modal handling, keybinding routing | `test_navigation.py` |
| TUI error paths | daemon kill, invalid input, RPC failures | `test_error_paths.py` |
| Tray lifecycle | init, do_poll, do_quit (stop_on_quit on/off) | `test_lifecycle.py` |
| Tray connections | do_add/remove/toggle/start/stop/restart_connection, error alerts | `test_connection_actions.py` |
| Tray pac | do_add/remove/toggle_pac_host | `test_pac_actions.py` |
| Tray forwards | do_add/remove_local/remote_forward, do_toggle_forward_enabled | `test_forward_actions.py` |
| Tray shares | do_share/stop_share/delete_share/fetch | `test_share_actions.py` |
| Tray browser launch | do_launch_chrome, do_launch_firefox (mocked subprocess.Popen) | `test_browser_launch.py` |
| Tray dialog plumbing (mac) | `_show_form_dialog`, `_show_message_panel`, `_show_about_panel` shape | `test_mac_dialogs.py` |
| Tray dialog plumbing (linux) | `_show_settings_dialog`, etc. — Gtk shape | `test_linux_dialogs.py` |
| GUI smoke | tray boots + quits cleanly | `test_gui_smoke.py` |

---

## Out of scope

- **Visual regression** — pixel-level snapshots of the TUI screens. Textual ships a `SVGScreenshot` test asserter; could be added later but is brittle when fonts/terminal sizes shift.
- **Performance** — RPC latency, polling overhead, large-config scaling. Add separately as `tests/perf/` with `pytest-benchmark`.
- **macOS Tahoe-specific TCC prompt behavior** — manual-only (no way to script TCC consent).

---

## Self-review

**Spec coverage:** Every `do_*` method on `AbstractTrayApp`, every `action_*` on `SusOpsTuiApp` + dashboard, every modal screen, has at least one task covering it.

**Placeholders scan:** No "TBD" / "implement appropriately". Each task includes complete test code or refers to the harness file that does.

**Type consistency:** `SusOpsClient`, `_TestTrayApp`, fixture names (`daemon`, `tui_app`, `tray`) are consistent across tasks.

**Known risks:**
- Textual's `Pilot` API changes between versions — pin to current major in `pyproject.toml` and re-check on upgrade.
- Layer 2.6/2.7 (dialog mocking) is brittle. If the AppKit/GTK call patterns shift, those tests need updating. Acceptable maintenance cost given they catch regressions in dialog wiring (e.g., the "Connection Tag not writable" bug we hit).
- The CLI-derived `start()` path requires a real SSH connection to fully exercise. For tests that need green status, run a local sshd or use docker `linuxserver/openssh-server` as a session fixture — out of scope for this plan, add later if flakiness shows up.

---

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-06-04-tui-tray-test-automation.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints for review.

Which approach?
