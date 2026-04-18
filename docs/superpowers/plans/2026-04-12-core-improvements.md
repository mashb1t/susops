# Core Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four independent improvements to core behaviour: (1) auto-reconnect when a ControlMaster dies unexpectedly, (2) surface SSH error log lines in the TUI on connection failure, (3) populate the add-connection dialog with hosts from `~/.ssh/config`, (4) show a useful error modal when `config.yaml` fails Pydantic validation on startup instead of crashing.

**Architecture:** Auto-reconnect is a daemon thread in `facade.py` modelled after `_BandwidthSampler`. SSH error surfacing reads the tail of the per-connection log file in the existing exception handler. SSH config import uses the existing `core/ssh_config.py::get_ssh_hosts()` in `_AddConnectionDialog`. Config validation catches `ValidationError` in `app.py` startup and shows a `ModalScreen`.

**Tech Stack:** Python threading, pathlib, Textual (Select, ModalScreen), pydantic ValidationError

---

## File Map

- Modify: `src/susops/facade.py` — add `_ReconnectMonitor` class; call it from `start()`/`stop()`; add `_notify()` helper; read log tail on SSH failure
- Modify: `src/susops/tui/screens/connection_editor.py` — populate ssh-host Select from `get_ssh_hosts()`
- Modify: `src/susops/tui/app.py` — catch `ValidationError` on startup, show error modal
- Test: `tests/test_facade.py` — test that `_ReconnectMonitor.mark_running/stopped` updates intended set

---

### Task 1: SSH error surfacing — read log tail on connection failure

Currently when `start_master()` raises, the facade logs only the exception string. The actual SSH error (wrong key, host unreachable, banner mismatch) is in `~/.susops/logs/susops-ssh-<tag>.log` and the user must find it manually.

**Files:**
- Modify: `src/susops/facade.py`

- [ ] **Step 1: Write a failing test**

In `tests/test_facade.py`, add:

```python
def test_start_logs_ssh_tail_on_failure(tmp_path):
    """When SSH fails to start, the facade logs the last lines of the SSH log."""
    from susops.facade import SusOpsManager
    from susops.core.config import Connection

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("demo", "user@nonexistent.invalid")

    # Pre-write a fake SSH log that would normally be written by the SSH process
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "susops-ssh-demo.log").write_text(
        "OpenSSH_9.0\nConnection refused (port 22)\n"
    )

    log_lines = []
    mgr.on_log = log_lines.append

    mgr.start(tag="demo")  # will fail — host does not exist

    combined = "\n".join(log_lines)
    assert "Connection refused" in combined, (
        f"SSH log tail not surfaced in log output:\n{combined}"
    )
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_facade.py::test_start_logs_ssh_tail_on_failure -v
```

Expected: FAILED — log tail not in output

- [ ] **Step 3: Find the exception handler in `src/susops/facade.py`**

Search for the block (around line 620):

```python
            except Exception as exc:
                msg = f"[{conn.tag}] Failed: {exc}"
                self._log(msg)
                errors.append(msg)
                statuses.append(ConnectionStatus(tag=conn.tag, running=False))
                self._emit("state", {"tag": conn.tag, "running": False, "pid": None})
```

- [ ] **Step 4: Replace that block with**

```python
            except Exception as exc:
                log_path = self.workspace / "logs" / f"susops-ssh-{conn.tag}.log"
                tail = ""
                if log_path.exists():
                    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
                    if lines:
                        tail = "\n  " + "\n  ".join(lines[-5:])
                msg = f"[{conn.tag}] Failed: {exc}{tail}"
                self._log(msg)
                errors.append(msg)
                statuses.append(ConnectionStatus(tag=conn.tag, running=False))
                self._emit("state", {"tag": conn.tag, "running": False, "pid": None})
```

- [ ] **Step 5: Run the test**

```bash
pytest tests/test_facade.py::test_start_logs_ssh_tail_on_failure -v
```

Expected: PASSED

- [ ] **Step 6: Run full suite**

```bash
pytest -x
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/susops/facade.py tests/test_facade.py
git commit -m "feat: surface SSH log tail in facade log on connection failure"
```

---

### Task 2: Auto-reconnect — restart dead ControlMasters

Add a `_ReconnectMonitor` daemon thread that checks every 15 seconds whether connections that were started intentionally are still alive. If a ControlMaster socket has died, it calls `start(tag=)` to restart.

**Files:**
- Modify: `src/susops/facade.py`

- [ ] **Step 1: Write a unit test for `_ReconnectMonitor`**

In `tests/test_facade.py`, add:

```python
def test_reconnect_monitor_tracks_intended_tags(tmp_path):
    """mark_running and mark_stopped maintain the intended set correctly."""
    from susops.facade import _ReconnectMonitor

    class _FakeMgr:
        pass

    monitor = _ReconnectMonitor(_FakeMgr())
    assert "work" not in monitor._intended

    monitor.mark_running("work")
    assert "work" in monitor._intended

    monitor.mark_running("home")
    assert "home" in monitor._intended

    monitor.mark_stopped("work")
    assert "work" not in monitor._intended
    assert "home" in monitor._intended
```

- [ ] **Step 2: Run to confirm failure (class doesn't exist yet)**

```bash
pytest tests/test_facade.py::test_reconnect_monitor_tracks_intended_tags -v
```

Expected: FAILED with ImportError

- [ ] **Step 3: Add `_ReconnectMonitor` to `src/susops/facade.py`**

Add the class after `_BandwidthSampler` (before the `SusOpsManager` class):

```python
class _ReconnectMonitor:
    """Background thread that restarts dead SSH ControlMasters.

    Tracks which connection tags were intentionally started. Every 15 seconds
    it checks socket liveness for each tracked tag and calls SusOpsManager.start()
    to reconnect any that have died.
    """

    INTERVAL = 15.0

    def __init__(self, mgr: "SusOpsManager") -> None:
        self._mgr = mgr
        self._intended: set[str] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="susops-reconnect"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def mark_running(self, tag: str) -> None:
        with self._lock:
            self._intended.add(tag)

    def mark_stopped(self, tag: str) -> None:
        with self._lock:
            self._intended.discard(tag)

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self.INTERVAL):
            with self._lock:
                tags = list(self._intended)
            for tag in tags:
                try:
                    self._check(tag)
                except Exception:
                    pass

    def _check(self, tag: str) -> None:
        if not is_socket_alive(tag, self._mgr.workspace):
            self._mgr._log(f"[{tag}] Connection lost — reconnecting...")
            self._mgr._emit("state", {"tag": tag, "running": False, "pid": None, "reconnecting": True})
            self._mgr._notify(f"SusOps [{tag}]", "Connection lost — reconnecting...")
            self._mgr.start(tag=tag)
```

- [ ] **Step 4: Add `_notify` helper to `SusOpsManager` (needed by `_check`)**

Add this method to `SusOpsManager` after `_debug`:

```python
def _notify(self, title: str, body: str) -> None:
    """Send a desktop notification. Best-effort — fails silently."""
    import platform
    import subprocess
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{body}" with title "{title}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif platform.system() == "Linux":
            subprocess.Popen(
                ["notify-send", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass
```

- [ ] **Step 5: Wire `_ReconnectMonitor` into `SusOpsManager.__init__`**

In `SusOpsManager.__init__`, after the `_bw_sampler` line, add:

```python
        self._reconnect_monitor = _ReconnectMonitor(self)
        self._reconnect_monitor.start()
```

- [ ] **Step 6: Mark connections in `start()` and `stop()`**

In `start()`, after `self._emit("state", {"tag": conn.tag, "running": True, ...})` (the success path), add:

```python
                self._reconnect_monitor.mark_running(conn.tag)
```

In `stop()`, after `self._emit("state", {"tag": conn.tag, "running": False, ...})` for each stopped connection, add:

```python
                    self._reconnect_monitor.mark_stopped(conn.tag)
```

- [ ] **Step 7: Run the unit test**

```bash
pytest tests/test_facade.py::test_reconnect_monitor_tracks_intended_tags -v
```

Expected: PASSED

- [ ] **Step 8: Run full suite**

```bash
pytest -x
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/susops/facade.py tests/test_facade.py
git commit -m "feat: add auto-reconnect monitor — restarts dead ControlMasters every 15 s

Adds _ReconnectMonitor daemon thread that tracks intentionally-started
connections and calls start(tag=) when a socket dies. Includes desktop
notification (notify-send / osascript) on connection loss."
```

---

### Task 3: SSH config host autocomplete in add-connection dialog

`core/ssh_config.py::get_ssh_hosts()` already parses `~/.ssh/config` and returns a list of non-wildcard host strings. Surface these in `_AddConnectionDialog` as a `Select` widget that pre-fills the SSH host input.

**Files:**
- Modify: `src/susops/tui/screens/connection_editor.py`

- [ ] **Step 1: Add `Select` to the import from textual.widgets**

At the top of `connection_editor.py`, `Select` is likely already imported (check first). If not, add it:

```python
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select
```

- [ ] **Step 2: Add `get_ssh_hosts` import**

```python
from susops.core.ssh_config import get_ssh_hosts
```

- [ ] **Step 3: Replace `_AddConnectionDialog.compose` with**

```python
    def compose(self) -> ComposeResult:
        ssh_hosts = get_ssh_hosts()
        with Static(classes="modal-dialog"):
            yield Label("[bold]Add Connection[/bold]")
            yield Label("Tag:")
            yield Input(placeholder="e.g. work", id="tag")
            if ssh_hosts:
                yield Label("SSH host (pick from ~/.ssh/config or type below):")
                options = [(h, h) for h in ssh_hosts]
                yield Select(options, prompt="— pick from SSH config —", id="ssh-host-select", allow_blank=True)
            else:
                yield Label("SSH host (user@host):")
            yield Input(placeholder="user@hostname", id="ssh-host")
            yield Label("SOCKS port (0 = auto):")
            yield Input(placeholder="0", id="socks-port", value="0")
            yield Label("", id="error", classes="modal-error")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Add", id="btn-ok", variant="success")
                yield Button("Cancel", id="btn-cancel")
```

- [ ] **Step 4: Add a `Select.Changed` handler to pre-fill the Input**

Add this method to `_AddConnectionDialog`:

```python
    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "ssh-host-select" and isinstance(event.value, str):
            self.query_one("#ssh-host", Input).value = event.value
```

- [ ] **Step 5: Update `on_button_pressed` to handle the case where no Select exists**

The `host` line is already reading from `#ssh-host` Input — no change needed.

- [ ] **Step 6: Manual smoke test**

```bash
susops  # launch TUI
# Press c → connection editor → a (add connection)
# If ~/.ssh/config has host entries: verify Select appears and selecting one fills the Input
# If ~/.ssh/config is empty: verify only Input appears
```

- [ ] **Step 7: Run test suite**

```bash
pytest -x
```

Expected: all pass (no tests touch _AddConnectionDialog internals directly).

- [ ] **Step 8: Commit**

```bash
git add src/susops/tui/screens/connections.py
git commit -m "feat: populate add-connection dialog with hosts from ~/.ssh/config"
```

---

### Task 4: Config validation feedback on startup

If `~/.susops/config.yaml` contains invalid YAML or fails Pydantic validation, `SusOpsManager.__init__` raises `ValidationError`. Currently this crashes the TUI with a traceback. Show a readable error modal instead.

**Files:**
- Modify: `src/susops/tui/app.py`

- [ ] **Step 1: Read `src/susops/tui/app.py` to find the `on_mount` or startup block**

Look for where `SusOpsManager` is instantiated (likely in `__init__` or `on_mount`).

- [ ] **Step 2: Add a `_ConfigErrorScreen` modal class to `app.py`**

```python
from pydantic import ValidationError

class _ConfigErrorScreen(ModalScreen):
    """Shown when config.yaml fails to load. Allows the user to open $EDITOR to fix it."""

    def __init__(self, error: str, config_path: str) -> None:
        super().__init__()
        self._error = error
        self._config_path = config_path

    def compose(self) -> ComposeResult:
        with Static(classes="modal-dialog"):
            yield Label("[bold red]Config file error[/bold red]")
            yield Label(f"[dim]{self._config_path}[/dim]")
            yield Label(self._error)
            yield Label("\nFix the file and restart susops, or press [bold]e[/bold] to open in $EDITOR.")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Open in $EDITOR", id="btn-edit", variant="warning")
                yield Button("Quit", id="btn-quit", variant="error")

    def on_button_pressed(self, event) -> None:
        import os
        import subprocess
        if event.button.id == "btn-edit":
            editor = os.environ.get("EDITOR", "nano")
            subprocess.run([editor, self._config_path])
            self.dismiss(None)
        else:
            self.app.exit(1)
```

- [ ] **Step 3: Wrap `SusOpsManager` construction in `on_mount`**

In `app.py`, find where `self.mgr = SusOpsManager(...)` is called. Wrap it:

```python
    async def on_mount(self) -> None:
        from pydantic import ValidationError
        try:
            self.mgr = SusOpsManager(workspace=self._workspace, verbose=self._verbose)
        except (ValidationError, Exception) as exc:
            config_path = str(self._workspace / "config.yaml")
            await self.push_screen(
                _ConfigErrorScreen(str(exc), config_path)
            )
            return
        # ... rest of on_mount
```

- [ ] **Step 4: Run the test suite**

```bash
pytest -x
```

Expected: all pass.

- [ ] **Step 5: Smoke test (write a broken config)**

```bash
cp ~/.susops/config.yaml ~/.susops/config.yaml.bak
echo "invalid: [yaml: {broken" > ~/.susops/config.yaml
susops
# Should show error modal, not traceback
cp ~/.susops/config.yaml.bak ~/.susops/config.yaml
```

- [ ] **Step 6: Commit**

```bash
git add src/susops/tui/app.py
git commit -m "feat: show error modal on config.yaml validation failure instead of crashing"
```

---

### Task 5: TUI error notifications via `on_error` callback

Errors logged via `_log()` go to the Logs tab — invisible unless the user happens to be on that tab. Add an `on_error` callback to `SusOpsManager` that the TUI wires to `self.notify()` (Textual toast), so connection failures, forward failures, and share failures surface immediately regardless of which tab is active.

**Files:**
- Modify: `src/susops/facade.py` — add `on_error` public callback and `_error()` private method; replace critical `_log()` calls with `_error()`
- Modify: `src/susops/tui/screens/dashboard.py` — wire `mgr.on_error` to `self.notify()` on mount/unmount

- [ ] **Step 1: Write a test for `_error()`**

In `tests/test_facade.py`, add:

```python
def test_error_calls_both_on_log_and_on_error(tmp_path):
    """_error() must invoke both on_log and on_error callbacks."""
    from susops.facade import SusOpsManager

    mgr = SusOpsManager(workspace=tmp_path)

    log_msgs = []
    error_msgs = []
    mgr.on_log = log_msgs.append
    mgr.on_error = error_msgs.append

    mgr._error("something went wrong")

    assert any("something went wrong" in m for m in log_msgs), "on_log not called"
    assert any("something went wrong" in m for m in error_msgs), "on_error not called"


def test_error_tolerates_missing_on_error(tmp_path):
    """_error() must not raise when on_error is None."""
    from susops.facade import SusOpsManager

    mgr = SusOpsManager(workspace=tmp_path)
    mgr.on_error = None
    mgr._error("oops")  # must not raise
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_facade.py::test_error_calls_both_on_log_and_on_error \
       tests/test_facade.py::test_error_tolerates_missing_on_error -v
```

Expected: both FAILED (`_error` not defined)

- [ ] **Step 3: Add `on_error` and `_error()` to `SusOpsManager`**

In `SusOpsManager.__init__`, after the `on_log` line:

```python
        self.on_error: Callable[[str], None] | None = None
```

Add the `_error()` method directly after `_log()`:

```python
    def _error(self, msg: str) -> None:
        """Log an error to the log buffer and fire the on_error callback.

        Use this instead of _log() for failures that the user must see
        immediately (connection failures, forward failures, share errors).
        on_error is wired to the TUI's notify() toast in dashboard.py.
        """
        self._log(msg)
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception:
                pass
```

- [ ] **Step 4: Run the two new tests**

```bash
pytest tests/test_facade.py::test_error_calls_both_on_log_and_on_error \
       tests/test_facade.py::test_error_tolerates_missing_on_error -v
```

Expected: both PASSED

- [ ] **Step 5: Replace `_log()` with `_error()` for critical failures in `facade.py`**

Search for the following patterns and replace `self._log(` with `self._error(` for each:

1. SSH master start failure (the `except Exception as exc:` block in `start()` that logs `f"[{conn.tag}] Failed: {exc}"`):

```python
                self._error(msg)   # was: self._log(msg)
```

2. Per-forward failure in `start()` — `f"[{conn.tag}] Forward {fw.src_port} failed: {exc}"`:

```python
                        self._error(f"[{conn.tag}] Forward {fw.src_port} failed: {exc}")
```

3. Share start failure in `start()` — `f"[{conn.tag}] Failed to start share ...`:

```python
                        self._error(f"[{conn.tag}] Failed to start share '{fs.file_path}': {exc}")
```

4. Share forward failure in `start()` — `f"[{conn.tag}] Share forward {share_port} failed: {exc}"`:

```python
                        self._error(f"[{conn.tag}] Share forward {share_port} failed: {exc}")
```

5. PAC server failure in `start()` — `f"PAC server failed: {exc}"`:

```python
                    errors.append(f"PAC server failed: {exc}")
                    self._error(f"PAC server failed: {exc}")  # add this line
```

Do **not** change informational `_log()` calls like "Master started (PID …)" or "Already running" — only failures.

- [ ] **Step 6: Run full suite**

```bash
pytest -x
```

Expected: all pass.

- [ ] **Step 7: Wire `on_error` in `dashboard.py`**

In `DashboardScreen.on_mount`, after the `mgr.on_log = self._on_new_log` line, add:

```python
        self._prev_on_error = mgr.on_error
        mgr.on_error = self._on_new_error
```

In `DashboardScreen.on_unmount`, after `mgr.on_log = self._prev_on_log`, add:

```python
        mgr.on_error = self._prev_on_error
```

Add the handler method alongside `_on_new_log`:

```python
    def _on_new_error(self, msg: str) -> None:
        try:
            self.app.call_from_thread(
                self.notify,
                msg,
                severity="error",
                timeout=6,
            )
        except Exception:
            pass
```

- [ ] **Step 8: Manual smoke test**

```bash
susops  # launch TUI
# Try to start a connection to a nonexistent host
# Verify: error appears in Logs tab AND as a red toast notification at top of screen
```

- [ ] **Step 9: Run full suite**

```bash
pytest -x
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add src/susops/facade.py src/susops/tui/screens/dashboard.py tests/test_facade.py
git commit -m "feat: surface critical errors as TUI notify toasts via on_error callback

Adds on_error callback alongside on_log. _error() fires both.
SSH failures, forward failures, share failures, and PAC server errors
now show as red toast notifications in the dashboard regardless of
which tab is active."
```
