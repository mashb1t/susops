# macOS Tray Unified Config Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mac tray's ~14 scattered modal dialogs with one unified non-modal config window (per-connection tabs + grouped sidebar + detail panel per the hand-drawn mockup), slim the tray menu, and build an agent-drivable screenshot/dump feedback loop first.

**Architecture:** Three phases. Phase 0 builds self-verification infra: a workspace env override, a localhost-only TCP debug server inside the tray (commands: dump-menu, screenshot via in-process `cacheDisplayInRect_`, open/select/dump-window), and a client tool. Phase 1 builds the window: a pure-Python view-model layer (`config_window_model.py`, headlessly testable) feeding a raw-AppKit window (`mac_config_window.py`) that reuses mac.py's proven NSPanel patterns and existing `do_*`/dialog flows. Phase 2 slims the menu and deletes dead dialog code.

**Tech Stack:** PyObjC/AppKit (no new deps), rumps 0.4.0 (menu only), pytest (+ opt-in `gui` marker for real-GUI smoke).

**Spec:** `docs/superpowers/specs/2026-06-12-mac-tray-config-window-design.md`

**Branch:** create `feature/tray-config-window` off current HEAD before Task 1.

**Reference reading for the engineer (skim before starting):**
- `src/susops/tray/mac.py:100-530` — cached NSObject subclass pattern (NEVER define NSObject subclasses inside functions; PyObjC re-registers the class name and selectors go stale), `_on_main`, `_RegularPolicyScope`.
- `src/susops/tray/mac.py:1257-1511` — `_open_live_text_window`: the non-modal always-on-top window lifecycle this plan copies (held-open policy scope, delegate close, `_LIVE_WINDOWS` strong refs).
- `src/susops/tray/base.py` — `AbstractTrayApp.do_*` business methods the window calls.
- `CLAUDE.md` — facade-only rule, commit style (Conventional Commits with scope).

---

## File structure

| File | Status | Responsibility |
|---|---|---|
| `src/susops/tray/base.py` | modify | `_resolve_workspace()` env override |
| `src/susops/tray/debug_server.py` | create | platform-neutral TCP command server (parse/dispatch/JSON) |
| `tools/tray_debug.py` | create | one-shot client: send command, print JSON reply |
| `src/susops/tray/config_window_model.py` | create | pure-Python view-model builders (tabs, sidebar rows, detail specs) — no AppKit |
| `src/susops/tray/mac_config_window.py` | create | the AppKit window; consumes the model; calls tray `do_*` |
| `src/susops/tray/mac.py` | modify | debug-server wiring, open-window action, form-builder extraction, slim menu (Phase 2) |
| `tests/tray/test_workspace_override.py` | create | env override unit test |
| `tests/tray/test_debug_server.py` | create | debug server unit tests |
| `tests/tray/test_config_window_model.py` | create | model layer unit tests (SimpleNamespace fixtures, no daemon) |
| `tests/tray/conftest.py` | create | gui-marked tray-process fixture + `send()` helper |
| `tests/tray/test_gui_smoke.py` | create | opt-in `pytest -m gui` smoke (dump-menu, open-config, screenshot) |
| `pyproject.toml` | modify | register `gui` marker |

Dev feedback loop (used throughout Phase 1/2; established in Task 4):

```bash
# terminal A — isolated dev instance (never touches ~/.susops or the user's running tray)
WS=$(mktemp -d /tmp/susops-dev.XXXX)
SUSOPS_TRAY_WORKSPACE=$WS SUSOPS_TRAY_DEBUG_PORT=7799 .venv/bin/susops-tray &

# seed sample data through the same daemon the tray uses
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
c.add_local_forward("work", PortForward(src_port=5432, dst_port=5432, dst_addr="db.internal", tag="postgres"))
c.add_remote_forward("work", PortForward(src_port=8080, dst_port=8080, tag="webserver"))
EOF

# drive it
.venv/bin/python tools/tray_debug.py 7799 dump-menu
.venv/bin/python tools/tray_debug.py 7799 open-config
.venv/bin/python tools/tray_debug.py 7799 screenshot /tmp/susops-window.png
# then visually inspect /tmp/susops-window.png (agent: Read the PNG)
```

---

# Phase 0 — feedback loop

### Task 1: Workspace env override

**Files:**
- Modify: `src/susops/tray/base.py:80-83`
- Test: `tests/tray/test_workspace_override.py`

- [ ] **Step 0: Create branch**

```bash
git checkout -b feature/tray-config-window
```

- [ ] **Step 1: Write the failing test**

```python
# tests/tray/test_workspace_override.py
from pathlib import Path


def test_resolve_workspace_default(monkeypatch):
    monkeypatch.delenv("SUSOPS_TRAY_WORKSPACE", raising=False)
    from susops.tray.base import _resolve_workspace
    assert _resolve_workspace() == Path.home() / ".susops"


def test_resolve_workspace_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SUSOPS_TRAY_WORKSPACE", str(tmp_path / "ws"))
    from susops.tray.base import _resolve_workspace
    assert _resolve_workspace() == tmp_path / "ws"


def test_resolve_workspace_expands_user(monkeypatch):
    monkeypatch.setenv("SUSOPS_TRAY_WORKSPACE", "~/somewhere")
    from susops.tray.base import _resolve_workspace
    assert _resolve_workspace() == Path.home() / "somewhere"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/tray/test_workspace_override.py -v`
Expected: FAIL / ImportError — `_resolve_workspace` does not exist.

- [ ] **Step 3: Implement**

In `src/susops/tray/base.py`, add `import os` to the imports, then above `class AbstractTrayApp`:

```python
def _resolve_workspace() -> Path:
    """Workspace dir for the tray. SUSOPS_TRAY_WORKSPACE overrides ~/.susops
    so a dev/test instance can run alongside the user's real tray."""
    env = os.environ.get("SUSOPS_TRAY_WORKSPACE")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".susops"
```

And in `AbstractTrayApp.__init__`, replace `workspace = Path.home() / ".susops"` with:

```python
workspace = _resolve_workspace()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/tray/test_workspace_override.py -v` → 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/susops/tray/base.py tests/tray/test_workspace_override.py
git commit -m "feat(tray): SUSOPS_TRAY_WORKSPACE env override for dev/test instances"
```

### Task 2: Debug command server core + client tool

**Files:**
- Create: `src/susops/tray/debug_server.py`
- Create: `tools/tray_debug.py`
- Test: `tests/tray/test_debug_server.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/tray/test_debug_server.py
import json
import socket

import pytest

from susops.tray.debug_server import TrayDebugServer


@pytest.fixture
def server():
    handlers = {
        "echo": lambda args: {"args": args},
        "boom": lambda args: (_ for _ in ()).throw(RuntimeError("kaput")),
        "none": lambda args: None,
    }
    srv = TrayDebugServer(handlers, port=0)
    srv.start()
    yield srv
    srv.close()


def _send(port: int, line: str) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        f = s.makefile("rw", encoding="utf-8")
        f.write(line + "\n")
        f.flush()
        return json.loads(f.readline())


def test_dispatches_to_handler(server):
    assert _send(server.port, "echo a b") == {"args": ["a", "b"]}


def test_unknown_command_is_error(server):
    assert "error" in _send(server.port, "nope")


def test_handler_exception_is_error_not_crash(server):
    assert _send(server.port, "boom") == {"error": "kaput"}
    # server still alive afterwards
    assert _send(server.port, "echo x") == {"args": ["x"]}


def test_none_result_means_ok(server):
    assert _send(server.port, "none") == {"ok": True}


def test_binds_localhost_only(server):
    assert server._sock.getsockname()[0] == "127.0.0.1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/tray/test_debug_server.py -v`
Expected: ImportError — module does not exist.

- [ ] **Step 3: Implement the server**

```python
# src/susops/tray/debug_server.py
"""Opt-in localhost TCP command server for driving a tray app in tests/dev loops.

Platform-neutral: knows nothing about AppKit/GTK. Handlers receive the
argument list and return a JSON-able dict (None → {"ok": true}). UI-touching
handlers are responsible for marshaling onto their toolkit's main thread.

Protocol: newline-delimited commands ("cmd arg1 arg2"), one JSON object per
line in response. Only ever bound to 127.0.0.1; only started when the tray
is launched with SUSOPS_TRAY_DEBUG_PORT set.
"""
from __future__ import annotations

import json
import socket
import threading
from typing import Callable

Handler = Callable[[list[str]], dict | None]


class TrayDebugServer:
    def __init__(self, handlers: dict[str, Handler], port: int = 0) -> None:
        self._handlers = handlers
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(
            target=self._serve, daemon=True, name="susops-tray-debug",
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # socket closed
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True,
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            f = conn.makefile("rw", encoding="utf-8")
            for line in f:
                line = line.strip()
                if not line:
                    continue
                f.write(json.dumps(self._dispatch(line)) + "\n")
                f.flush()

    def _dispatch(self, line: str) -> dict:
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        handler = self._handlers.get(cmd)
        if handler is None:
            known = ", ".join(sorted(self._handlers))
            return {"error": f"unknown command: {cmd} (known: {known})"}
        try:
            return handler(args) or {"ok": True}
        except Exception as exc:
            return {"error": str(exc)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/tray/test_debug_server.py -v` → 5 passed.

- [ ] **Step 5: Add the client tool (no test — trivial I/O wrapper, exercised constantly by the loop)**

```python
#!/usr/bin/env python3
# tools/tray_debug.py
"""Send one command to a running tray debug server, print the JSON reply.

Usage: python tools/tray_debug.py <port> <command> [args...]
"""
import socket
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    port = int(sys.argv[1])
    line = " ".join(sys.argv[2:])
    with socket.create_connection(("127.0.0.1", port), timeout=15) as s:
        f = s.makefile("rw", encoding="utf-8")
        f.write(line + "\n")
        f.flush()
        print(f.readline().strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Commit**

```bash
git add src/susops/tray/debug_server.py tools/tray_debug.py tests/tray/test_debug_server.py
git commit -m "feat(tray): localhost debug command server + client tool"
```

### Task 3: Wire debug server into the mac tray (dump-menu, screenshot, quit)

**Files:**
- Modify: `src/susops/tray/mac.py` (imports ~line 10-25; `SusOpsMacTray.__init__` ~line 1885; new module-level helpers near `_on_main` ~line 318)
- Modify: `pyproject.toml` (`[tool.pytest.ini_options]`)
- Create: `tests/tray/conftest.py`
- Test: `tests/tray/test_gui_smoke.py`

- [ ] **Step 1: Add module-level helpers to mac.py** (place after `_on_main`, ~line 334)

```python
def _run_on_main(fn, timeout: float = 5.0) -> dict:
    """Run fn on the main thread, wait for the result. For debug-server
    handlers, which run on socket threads but must touch AppKit."""
    import threading as _threading
    box: dict = {}
    done = _threading.Event()

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
```

- [ ] **Step 2: Start the server from `SusOpsMacTray.__init__`** (after `self._build_menu()`):

```python
        self._config_window = None  # set in Phase 1
        self._debug_server = None
        debug_port = os.environ.get("SUSOPS_TRAY_DEBUG_PORT")
        if debug_port:
            from susops.tray.debug_server import TrayDebugServer
            self._debug_server = TrayDebugServer(
                self._debug_handlers(), port=int(debug_port),
            )
            self._debug_server.start()
```

And add the handler table as a method on `SusOpsMacTray`:

```python
    def _debug_handlers(self) -> dict:
        """Debug-server command table. Every UI-touching handler marshals via
        _run_on_main. Extended in Phase 1 with open-config/select/dump-window."""

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

        return {
            "ping": lambda args: {"ok": True},
            "dump-menu": lambda args: _run_on_main(
                lambda: {"menu": _menu_tree(self._app.menu)}),
            "open-about": lambda args: _run_on_main(
                lambda: (_show_about_panel(), {"ok": True})[1]),
            "screenshot": _screenshot,
            "quit": _quit,
        }

    def _debug_target_window(self):
        """Window the screenshot command captures: config window when open
        (Phase 1), else any open About/live panel (Phase 0 verification)."""
        cw = getattr(self, "_config_window", None)
        if cw is not None and cw.is_open():
            return cw.window
        for store in (_ABOUT_WINDOWS, _LIVE_WINDOWS):
            for entry in store.values():
                return entry["panel"]
        return None
```

- [ ] **Step 3: Register the `gui` pytest marker** in `pyproject.toml` under `[tool.pytest.ini_options]` (add the key if the section lacks it):

```toml
markers = [
    "gui: tests that exercise a real GUI runtime (rumps / AppKit); slow + macOS-only, run with -m gui",
]
```

If `pyproject.toml` already filters by marker or has `addopts`, leave those untouched; also add `-m "not gui"` is NOT needed — gui tests self-skip via the fixture below unless `-m gui` is passed... Correction: pytest runs all tests by default regardless of marker. To keep `pytest` green without a GUI, the gui tests are additionally guarded with `pytest.mark.skipif` on `SUSOPS_RUN_GUI_TESTS` (see conftest). Run them with: `SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui`.

- [ ] **Step 4: GUI fixture**

```python
# tests/tray/conftest.py
from __future__ import annotations

import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

gui_guard = [
    pytest.mark.gui,
    pytest.mark.skipif(platform.system() != "Darwin", reason="macOS only"),
    pytest.mark.skipif(
        not os.environ.get("SUSOPS_RUN_GUI_TESTS"),
        reason="set SUSOPS_RUN_GUI_TESTS=1 to run GUI smoke tests",
    ),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TrayProc:
    def __init__(self, proc: subprocess.Popen, port: int, workspace: Path):
        self.proc = proc
        self.port = port
        self.workspace = workspace

    def send(self, line: str, timeout: float = 15.0) -> dict:
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as s:
            f = s.makefile("rw", encoding="utf-8")
            f.write(line + "\n")
            f.flush()
            return json.loads(f.readline())


@pytest.fixture
def tray_proc(tmp_path: Path):
    """Spawn a real susops-tray with isolated workspace + debug server."""
    port = _free_port()
    env = os.environ.copy()
    env["SUSOPS_TRAY_WORKSPACE"] = str(tmp_path)
    env["SUSOPS_TRAY_DEBUG_PORT"] = str(port)
    tray_bin = Path(sys.executable).parent / "susops-tray"
    proc = subprocess.Popen(
        [str(tray_bin)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    tp = TrayProc(proc, port, tmp_path)
    # Wait for the debug server to come up.
    deadline = time.time() + 20
    last_err: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"tray died on startup: {proc.stderr.read().decode(errors='replace')!r}")
        try:
            assert tp.send("ping") == {"ok": True}
            break
        except Exception as exc:  # noqa: BLE001 — retry until deadline
            last_err = exc
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail(f"debug server never came up: {last_err!r}")
    yield tp
    # Teardown: ask politely, then force; also reap the workspace daemon.
    try:
        tp.send("quit", timeout=5)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    pid_file = tmp_path / "pids" / "susops-services.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
```

- [ ] **Step 5: GUI smoke test**

```python
# tests/tray/test_gui_smoke.py
import os
from pathlib import Path

import pytest

from tests.tray.conftest import gui_guard

pytestmark = gui_guard


def test_ping_and_dump_menu(tray_proc):
    menu = tray_proc.send("dump-menu")["menu"]
    titles = [n.get("title") for n in menu if "title" in n]
    assert "Start Proxy" in titles
    assert "Quit" in titles


def test_screenshot_of_about_panel(tray_proc, tmp_path):
    assert tray_proc.send("open-about").get("ok")
    out = tmp_path / "about.png"
    result = tray_proc.send(f"screenshot {out}")
    assert result.get("ok"), result
    assert out.stat().st_size > 5_000  # a real PNG, not a stub
    assert result["width"] > 100 and result["height"] > 100
```

If `from tests.tray.conftest import gui_guard` fails because `tests/` isn't a package, add empty `tests/tray/__init__.py` and `tests/__init__.py` ONLY if the repo doesn't already configure rootdir-relative imports — check how existing tests import first; otherwise inline the marker list in the test file.

- [ ] **Step 6: Run + verify**

```bash
.venv/bin/pytest tests/tray/ -v                       # non-gui tests still green, gui skipped
SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui -v     # 2 passed (real window opens briefly)
```

Then verify the loop end-to-end manually (commands from the "Dev feedback loop" block at the top): `dump-menu` returns the current menu tree, `open-about` + `screenshot` produces a readable PNG. **Agent: Read the PNG and confirm the About panel is visible.**

- [ ] **Step 7: Commit**

```bash
git add src/susops/tray/mac.py pyproject.toml tests/tray/conftest.py tests/tray/test_gui_smoke.py
git commit -m "feat(tray): debug server wiring on mac — dump-menu, in-process screenshot"
```

---

# Phase 1 — window

### Task 4: View-model layer

**Files:**
- Create: `src/susops/tray/config_window_model.py`
- Test: `tests/tray/test_config_window_model.py`

The model layer is pure Python (no AppKit) so it tests headlessly and a future GTK port can reuse it. It reads attributes duck-typed off the pydantic config / status / ShareInfo objects.

- [ ] **Step 1: Write the failing tests**

```python
# tests/tray/test_config_window_model.py
from types import SimpleNamespace as NS

from susops.tray.config_window_model import (
    Action,
    DetailSpec,
    SidebarRow,
    TabSpec,
    build_connection_detail,
    build_domain_detail,
    build_forward_detail,
    build_share_detail,
    build_sidebar_rows,
    build_tab_specs,
)


def _conn(tag="work", enabled=True, **kw):
    return NS(
        tag=tag, ssh_host="user@bastion", socks_port=1080, enabled=enabled,
        pac_hosts=kw.get("pac_hosts", ["blabla.de", "10.0.0.0/8"]),
        pac_hosts_disabled=kw.get("pac_hosts_disabled", ["10.0.0.0/8"]),
        forwards=NS(
            local=kw.get("local", [NS(src_port=5432, src_addr="localhost",
                                      dst_port=5432, dst_addr="db.internal",
                                      tag="postgres", tcp=True, udp=False, enabled=True)]),
            remote=kw.get("remote", [NS(src_port=8080, src_addr="localhost",
                                        dst_port=8080, dst_addr="localhost",
                                        tag=None, tcp=True, udp=True, enabled=False)]),
        ),
    )


def _status(tag="work", running=True, pid=4711):
    return NS(tag=tag, running=running, pid=pid, socks_port=1080,
              enabled=True, pending=False)


def _share(port=44001, running=True, stopped=False):
    return NS(file_path="/tmp/file.bin", port=port, running=running,
              stopped=stopped, password="pw", access_count=3, failed_count=0,
              conn_tag="work")


# ---- tabs ----

def test_tab_specs_running_dot_and_synthetic_tabs():
    cfg = NS(connections=[_conn("work"), _conn("home", enabled=True)])
    tabs = build_tab_specs(cfg, [_status("work", running=True),
                                 _status("home", running=False)])
    assert tabs[0] == TabSpec(tag="work", title="● work", kind="connection")
    assert tabs[1] == TabSpec(tag="home", title="○ home", kind="connection")
    assert tabs[-2].kind == "add" and tabs[-2].title == "+"
    assert tabs[-1].kind == "gear"


def test_tab_specs_disabled_connection_dash():
    cfg = NS(connections=[_conn("work", enabled=False)])
    tabs = build_tab_specs(cfg, [])
    assert tabs[0].title == "– work"


# ---- sidebar ----

def test_sidebar_rows_groups_and_items():
    rows = build_sidebar_rows(_conn(), [_share()])
    kinds = [r.kind for r in rows]
    # all four headers present, in order
    headers = [r.label for r in rows if r.kind == "header"]
    assert headers == ["DOMAINS", "FORWARDS", "SHARES", "CONNECTION"]
    # enabled domain ●, disabled domain ○
    domain_rows = [r for r in rows if r.kind == "domain"]
    assert domain_rows[0].label == "● blabla.de"
    assert domain_rows[1].label == "○ 10.0.0.0/8"
    assert domain_rows[0].identity == ("domain", "blabla.de")
    # forwards: local enabled, remote disabled
    fwd_rows = [r for r in rows if r.kind == "forward"]
    assert fwd_rows[0].label == "● L :5432→db.internal:5432"
    assert fwd_rows[1].label == "○ R :8080→localhost:8080"
    assert fwd_rows[0].identity == ("forward", "local", 5432)
    # share running ●
    share_rows = [r for r in rows if r.kind == "share"]
    assert share_rows[0].label == "● file.bin (44001)"
    assert share_rows[0].identity == ("share", 44001)
    # fixed connection row
    assert rows[-1] == SidebarRow(kind="connection", label="Settings",
                                  identity=("connection",))


def test_sidebar_share_three_state_dots():
    running = build_sidebar_rows(_conn(), [_share(running=True)])
    stopped = build_sidebar_rows(_conn(), [_share(running=False, stopped=True)])
    down = build_sidebar_rows(_conn(), [_share(running=False, stopped=False)])
    get = lambda rows: [r for r in rows if r.kind == "share"][0].label[0]
    assert get(running) == "●"
    assert get(stopped) == "○"   # manually stopped (renderer dims it)
    assert get(down) == "◌"      # connection down (renderer colors it red)


# ---- details ----

def test_connection_detail_running():
    spec = build_connection_detail(_conn(), _status(running=True))
    assert spec.title == "work"
    assert ("SSH Host", "user@bastion") in spec.rows
    assert ("Status", "● running · pid 4711") in spec.rows
    assert spec.toggle == ("Enabled", True, "conn.toggle")
    by_id = {a.action_id: a for a in spec.actions}
    assert by_id["conn.start"].enabled is False
    assert by_id["conn.stop"].enabled is True
    assert by_id["conn.test"].enabled is True
    assert by_id["conn.remove"].destructive is True


def test_connection_detail_stopped():
    spec = build_connection_detail(_conn(), _status(running=False, pid=None))
    by_id = {a.action_id: a for a in spec.actions}
    assert by_id["conn.start"].enabled is True
    assert by_id["conn.stop"].enabled is False
    assert by_id["conn.test"].enabled is False


def test_domain_detail():
    spec = build_domain_detail(_conn(), "10.0.0.0/8")
    assert spec.title == "10.0.0.0/8"
    assert ("Connection", "work") in spec.rows
    assert spec.toggle == ("Enabled", False, "domain.toggle")
    assert {a.action_id for a in spec.actions} == {"domain.test", "domain.remove"}


def test_forward_detail():
    fw = _conn().forwards.remote[0]
    spec = build_forward_detail(_conn(), fw, "remote")
    assert ("Direction", "remote (-R)") in spec.rows
    assert ("Forward", "localhost:8080 → localhost:8080") in spec.rows
    assert ("Protocols", "TCP + UDP") in spec.rows
    assert spec.toggle == ("Enabled", False, "forward.toggle")
    assert {a.action_id for a in spec.actions} == {"forward.test", "forward.remove"}


def test_share_detail_running():
    spec = build_share_detail(_share())
    assert spec.title == "file.bin"
    assert ("Port", "44001") in spec.rows
    assert ("Downloads", "3 ok · 0 failed") in spec.rows
    by_id = {a.action_id: a for a in spec.actions}
    assert "share.stop" in by_id and "share.delete" in by_id
    assert "share.reveal" in by_id


def test_share_detail_stopped_offers_restart():
    spec = build_share_detail(_share(running=False, stopped=True))
    assert "share.start" in {a.action_id for a in spec.actions}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/tray/test_config_window_model.py -v`
Expected: ImportError — module does not exist.

- [ ] **Step 3: Implement the model**

```python
# src/susops/tray/config_window_model.py
"""Pure-Python view-model builders for the tray config window.

No AppKit imports — testable headlessly, reusable by a future GTK port.
All builders read duck-typed attributes off the facade's pydantic/dataclass
objects (Connection, ConnectionStatus, ShareInfo)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DOT_ON = "●"
DOT_OFF = "○"
DOT_DOWN = "◌"   # share whose connection is down (renderer colors it red)
DOT_DISABLED = "–"


@dataclass(frozen=True)
class TabSpec:
    tag: str | None        # None for synthetic tabs
    title: str
    kind: str              # "connection" | "add" | "gear"


@dataclass(frozen=True)
class SidebarRow:
    kind: str              # "header" | "domain" | "forward" | "share" | "connection"
    label: str
    identity: tuple        # stable identity for selection restore


@dataclass(frozen=True)
class Action:
    action_id: str
    title: str
    enabled: bool = True
    destructive: bool = False


@dataclass(frozen=True)
class DetailSpec:
    title: str
    rows: list = field(default_factory=list)       # list[tuple[label, value]]
    toggle: tuple | None = None                    # (label, value, action_id)
    actions: list = field(default_factory=list)    # list[Action]


# ---------------------------------------------------------------- tabs ----

def build_tab_specs(cfg, statuses) -> list[TabSpec]:
    by_tag = {s.tag: s for s in statuses}
    tabs: list[TabSpec] = []
    for conn in cfg.connections:
        st = by_tag.get(conn.tag)
        if not conn.enabled:
            dot = DOT_DISABLED
        elif st is not None and getattr(st, "running", False):
            dot = DOT_ON
        else:
            dot = DOT_OFF
        tabs.append(TabSpec(tag=conn.tag, title=f"{dot} {conn.tag}", kind="connection"))
    tabs.append(TabSpec(tag=None, title="+", kind="add"))
    tabs.append(TabSpec(tag=None, title="⚙", kind="gear"))
    return tabs


# ------------------------------------------------------------- sidebar ----

def _forward_label(fw, direction: str) -> str:
    dot = DOT_ON if fw.enabled else DOT_OFF
    prefix = "L" if direction == "local" else "R"
    return f"{dot} {prefix} :{fw.src_port}→{fw.dst_addr}:{fw.dst_port}"


def _share_dot(info) -> str:
    if info.running:
        return DOT_ON
    return DOT_OFF if info.stopped else DOT_DOWN


def build_sidebar_rows(conn, shares) -> list[SidebarRow]:
    """Flattened sidebar rows for one connection: group headers + items.
    `shares` must already be filtered to this connection's tag."""
    rows: list[SidebarRow] = [SidebarRow("header", "DOMAINS", ("header", "domains"))]
    disabled = set(getattr(conn, "pac_hosts_disabled", []) or [])
    for host in conn.pac_hosts:
        dot = DOT_OFF if host in disabled else DOT_ON
        rows.append(SidebarRow("domain", f"{dot} {host}", ("domain", host)))

    rows.append(SidebarRow("header", "FORWARDS", ("header", "forwards")))
    for fw in conn.forwards.local:
        rows.append(SidebarRow("forward", _forward_label(fw, "local"),
                               ("forward", "local", fw.src_port)))
    for fw in conn.forwards.remote:
        rows.append(SidebarRow("forward", _forward_label(fw, "remote"),
                               ("forward", "remote", fw.src_port)))

    rows.append(SidebarRow("header", "SHARES", ("header", "shares")))
    for info in shares:
        name = Path(info.file_path).name
        rows.append(SidebarRow("share", f"{_share_dot(info)} {name} ({info.port})",
                               ("share", info.port)))

    rows.append(SidebarRow("header", "CONNECTION", ("header", "connection")))
    rows.append(SidebarRow("connection", "Settings", ("connection",)))
    return rows


# ------------------------------------------------------------- details ----

def build_connection_detail(conn, status) -> DetailSpec:
    running = bool(status is not None and getattr(status, "running", False))
    pid = getattr(status, "pid", None) if status is not None else None
    if running:
        status_text = f"{DOT_ON} running" + (f" · pid {pid}" if pid else "")
    else:
        status_text = f"{DOT_OFF} stopped"
    rows = [
        ("Tag", conn.tag),
        ("SSH Host", conn.ssh_host),
        ("SOCKS Port", str(conn.socks_port or "auto")),
        ("Status", status_text),
    ]
    actions = [
        Action("conn.start", "Start", enabled=not running),
        Action("conn.stop", "Stop", enabled=running),
        Action("conn.restart", "Restart", enabled=running),
        Action("conn.test", "Test", enabled=running),
        Action("conn.remove", "Remove Connection…", destructive=True),
    ]
    return DetailSpec(title=conn.tag, rows=rows,
                      toggle=("Enabled", bool(conn.enabled), "conn.toggle"),
                      actions=actions)


def build_domain_detail(conn, host: str) -> DetailSpec:
    disabled = set(getattr(conn, "pac_hosts_disabled", []) or [])
    rows = [("Host", host), ("Connection", conn.tag)]
    actions = [
        Action("domain.test", "Test"),
        Action("domain.remove", "Remove", destructive=True),
    ]
    return DetailSpec(title=host, rows=rows,
                      toggle=("Enabled", host not in disabled, "domain.toggle"),
                      actions=actions)


def build_forward_detail(conn, fw, direction: str) -> DetailSpec:
    protos = [p for p, on in (("TCP", fw.tcp), ("UDP", fw.udp)) if on]
    rows = [
        ("Direction", f"{direction} (-L)" if direction == "local" else f"{direction} (-R)"),
        ("Forward", f"{fw.src_addr}:{fw.src_port} → {fw.dst_addr}:{fw.dst_port}"),
        ("Protocols", " + ".join(protos)),
        ("Tag", fw.tag or "—"),
        ("Connection", conn.tag),
    ]
    actions = [
        Action("forward.test", "Test"),
        Action("forward.remove", "Remove", destructive=True),
    ]
    return DetailSpec(title=f":{fw.src_port}", rows=rows,
                      toggle=("Enabled", bool(fw.enabled), "forward.toggle"),
                      actions=actions)


def build_share_detail(info) -> DetailSpec:
    name = Path(info.file_path).name
    if info.running:
        status_text = f"{DOT_ON} running"
    elif info.stopped:
        status_text = f"{DOT_OFF} stopped (manual)"
    else:
        status_text = f"{DOT_DOWN} connection down"
    rows = [
        ("File", str(info.file_path)),
        ("Port", str(info.port)),
        ("Status", status_text),
        ("Downloads", f"{info.access_count} ok · {info.failed_count} failed"),
        ("Connection", info.conn_tag),
    ]
    actions = [Action("share.reveal", "Reveal Password")]
    if info.running:
        actions.append(Action("share.stop", "Stop Share"))
    else:
        actions.append(Action("share.start", "Start Share"))
    actions.append(Action("share.delete", "Delete", destructive=True))
    return DetailSpec(title=name, rows=rows, toggle=None, actions=actions)
```

- [ ] **Step 4: Run tests** → `.venv/bin/pytest tests/tray/test_config_window_model.py -v` → all pass. Iterate on mismatches (the tests are the contract).

- [ ] **Step 5: Commit**

```bash
git add src/susops/tray/config_window_model.py tests/tray/test_config_window_model.py
git commit -m "feat(tray): pure-python view-model for the config window"
```

### Task 5: Window skeleton — tabs, sidebar, placeholder detail, debug commands

**Files:**
- Create: `src/susops/tray/mac_config_window.py`
- Modify: `src/susops/tray/mac.py` (`_debug_handlers`, `_build_menu` — temporary "Config…" item, `do_poll`)

No headless test possible for raw AppKit — verification is via the feedback loop + the gui smoke extended in Step 5. Keep `pytest` green throughout.

- [ ] **Step 1: Create the window module**

```python
# src/susops/tray/mac_config_window.py
"""Unified config window — raw AppKit (rumps has no window classes).

Layout per docs/superpowers/specs/2026-06-12-mac-tray-config-window-design.md:
tab strip (one segment per connection + "+" + gear), grouped sidebar
(DOMAINS/FORWARDS/SHARES/CONNECTION), detail panel for the selection.

Lifecycle copies _open_live_text_window in mac.py: non-modal NSWindow,
held-open _RegularPolicyScope, close via delegate, module-level cached
NSObject subclasses (PyObjC re-registration bug — see mac.py).
"""
from __future__ import annotations

import threading
from typing import Callable

from susops.tray.config_window_model import (
    DetailSpec,
    SidebarRow,
    TabSpec,
    build_connection_detail,
    build_domain_detail,
    build_forward_detail,
    build_share_detail,
    build_sidebar_rows,
    build_tab_specs,
)

# Cached NSObject subclasses (built once per process).
_sidebar_ds_cls = None
_window_delegate_cls = None
_action_handler_cls = None


def _get_sidebar_ds_cls():
    global _sidebar_ds_cls
    if _sidebar_ds_cls is not None:
        return _sidebar_ds_cls
    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _SusOpsSidebarDS(NSObject):
        """Data source + delegate for the sidebar NSTableView."""

        def initWithOwner_(self, owner):
            self = objc.super(_SusOpsSidebarDS, self).init()
            if self is None:
                return None
            self._owner = owner
            return self

        def numberOfRowsInTableView_(self, _tv):
            return len(self._owner.sidebar_rows)

        def tableView_objectValueForTableColumn_row_(self, _tv, _col, row):
            return self._owner.sidebar_rows[row].label

        def tableView_shouldSelectRow_(self, _tv, row):
            return self._owner.sidebar_rows[row].kind != "header"

        def tableView_isGroupRow_(self, _tv, row):
            return self._owner.sidebar_rows[row].kind == "header"

        def tableViewSelectionDidChange_(self, _note):
            self._owner._on_sidebar_selection()

    _sidebar_ds_cls = _SusOpsSidebarDS
    return _SusOpsSidebarDS


def _get_window_delegate_cls():
    global _window_delegate_cls
    if _window_delegate_cls is not None:
        return _window_delegate_cls
    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _SusOpsConfigWindowDelegate(NSObject):
        def initWithCallback_(self, cb):
            self = objc.super(_SusOpsConfigWindowDelegate, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        def windowShouldClose_(self, _sender):
            try:
                self._cb()
            except Exception:
                pass
            return True

    _window_delegate_cls = _SusOpsConfigWindowDelegate
    return _SusOpsConfigWindowDelegate


def _get_action_handler_cls():
    global _action_handler_cls
    if _action_handler_cls is not None:
        return _action_handler_cls
    import objc  # type: ignore[import]
    from Cocoa import NSObject  # type: ignore[import]

    class _SusOpsActionHandler(NSObject):
        """Generic target for buttons/controls; calls back with the sender."""

        def initWithCallback_(self, cb):
            self = objc.super(_SusOpsActionHandler, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        def fire_(self, sender):
            try:
                self._cb(sender)
            except Exception:
                pass

    _action_handler_cls = _SusOpsActionHandler
    return _SusOpsActionHandler


SIDEBAR_W = 220
TAB_H = 28
ADD_BTN_H = 26
WIN_W = 900
WIN_H = 560


class ConfigWindow:
    """Controller for the unified config window. All methods MUST be called
    on the main thread (callers marshal via mac._on_main)."""

    def __init__(self, tray) -> None:
        self.tray = tray                      # SusOpsMacTray
        self.window = None
        self.tabs: list[TabSpec] = []
        self.sidebar_rows: list[SidebarRow] = []
        self.current_tag: str | None = None   # selected connection tag
        self._policy_scope = None
        self._handlers: list = []             # strong refs to ObjC helpers
        self._cfg = None
        self._statuses: list = []
        self._shares: list = []

    # ------------------------------------------------------------ public

    def is_open(self) -> bool:
        return self.window is not None and bool(self.window.isVisible())

    def open(self, tab: str | None = None) -> None:
        if self.window is None:
            self._build()
        self.refresh()
        if tab:
            self._select_tab_by_tag(tab)
        from susops.tray.mac import _RegularPolicyScope
        if self._policy_scope is None:
            self._policy_scope = _RegularPolicyScope()
            self._policy_scope.__enter__()
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        try:
            self.window.orderFrontRegardless()
        except Exception:
            pass

    def close(self) -> None:
        if self.window is not None:
            self.window.orderOut_(None)
        self._on_closed()

    def refresh(self) -> None:
        """Re-pull state from the manager and update all views in place,
        preserving tab + sidebar selection."""
        mgr = self.tray.manager
        self._cfg = mgr.list_config()
        try:
            self._statuses = list(mgr.status().connection_statuses)
        except Exception:
            self._statuses = []
        try:
            self._shares = list(mgr.list_shares())
        except Exception:
            self._shares = []
        self._reload_tabs()
        self._reload_sidebar(preserve=True)
        self._render_current_detail()

    def dump(self) -> dict:
        """JSON-able window state for the debug server."""
        return {
            "open": self.is_open(),
            "tabs": [t.title for t in self.tabs],
            "current_tag": self.current_tag,
            "sidebar": [
                {"kind": r.kind, "label": r.label} for r in self.sidebar_rows
            ],
            "selected": self._selected_identity(),
            "detail_title": self._current_detail_title,
        }

    def select(self, tag: str, group: str | None = None, index: int = 0) -> dict:
        self._select_tab_by_tag(tag)
        if group in (None, "", "connection"):
            target_kinds = {"connection"}
        else:
            target_kinds = {{"domains": "domain", "forwards": "forward",
                             "shares": "share"}.get(group, group)}
        matches = [i for i, r in enumerate(self.sidebar_rows)
                   if r.kind in target_kinds]
        if not matches or index >= len(matches):
            return {"error": f"no row for group={group} index={index}"}
        self._select_sidebar_row(matches[index])
        return {"ok": True, "selected": self._selected_identity()}

    # ------------------------------------------------------------- build

    def _build(self) -> None:
        from AppKit import (  # type: ignore[import]
            NSBackingStoreBuffered,
            NSFloatingWindowLevel,
            NSSegmentSwitchTrackingSelectOne,
            NSWindow,
            NSWindowStyleMaskClosable,
            NSWindowStyleMaskResizable,
            NSWindowStyleMaskTitled,
        )
        from Cocoa import (  # type: ignore[import]
            NSMakeRect,
            NSScrollView,
            NSSegmentedControl,
            NSPopUpButton,
            NSTableColumn,
            NSTableView,
            NSView,
        )

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIN_W, WIN_H), style, NSBackingStoreBuffered, False,
        )
        win.setTitle_("SusOps Settings")
        win.setReleasedWhenClosed_(False)
        win.setHidesOnDeactivate_(False)
        win.setLevel_(NSFloatingWindowLevel)
        content = win.contentView()

        # Tab strip (segments set in _reload_tabs)
        seg = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(12, WIN_H - TAB_H - 10, WIN_W - 24, TAB_H))
        seg.setTrackingMode_(NSSegmentSwitchTrackingSelectOne)
        seg.setAutoresizingMask_(2 | 8)  # WidthSizable | MinYMargin
        seg_handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda sender: self._on_segment(int(sender.selectedSegment())))
        self._handlers.append(seg_handler)
        seg.setTarget_(seg_handler)
        seg.setAction_("fire:")
        content.addSubview_(seg)
        self._seg = seg

        body_h = WIN_H - TAB_H - 28

        # Sidebar table inside a scroll view
        tv = NSTableView.alloc().initWithFrame_(
            NSMakeRect(0, 0, SIDEBAR_W, body_h - ADD_BTN_H - 16))
        col = NSTableColumn.alloc().initWithIdentifier_("item")
        col.setWidth_(SIDEBAR_W - 20)
        tv.addTableColumn_(col)
        tv.setHeaderView_(None)
        ds = _get_sidebar_ds_cls().alloc().initWithOwner_(self)
        self._handlers.append(ds)
        tv.setDataSource_(ds)
        tv.setDelegate_(ds)
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(12, ADD_BTN_H + 20, SIDEBAR_W, body_h - ADD_BTN_H - 20))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDocumentView_(tv)
        scroll.setAutoresizingMask_(16)  # HeightSizable
        content.addSubview_(scroll)
        self._sidebar_tv = tv

        # Add… pull-down (populated in Task 8; placeholder title until then)
        add_btn = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(12, 12, SIDEBAR_W, ADD_BTN_H), True)
        add_btn.addItemWithTitle_("Add…")
        content.addSubview_(add_btn)
        self._add_btn = add_btn

        # Detail container
        detail = NSView.alloc().initWithFrame_(
            NSMakeRect(SIDEBAR_W + 24, 12, WIN_W - SIDEBAR_W - 36, body_h))
        detail.setAutoresizingMask_(2 | 16)  # Width+HeightSizable
        content.addSubview_(detail)
        self._detail = detail
        self._current_detail_title = None

        delegate = _get_window_delegate_cls().alloc().initWithCallback_(self._on_closed)
        self._handlers.append(delegate)
        win.setDelegate_(delegate)
        self.window = win

    def _on_closed(self) -> None:
        if self._policy_scope is not None:
            try:
                self._policy_scope.__exit__(None, None, None)
            except Exception:
                pass
            self._policy_scope = None

    # -------------------------------------------------------------- tabs

    def _reload_tabs(self) -> None:
        self.tabs = build_tab_specs(self._cfg, self._statuses)
        conn_tags = [t.tag for t in self.tabs if t.kind == "connection"]
        if self.current_tag not in conn_tags:
            self.current_tag = conn_tags[0] if conn_tags else None
        seg = self._seg
        seg.setSegmentCount_(len(self.tabs))
        for i, t in enumerate(self.tabs):
            seg.setLabel_forSegment_(t.title, i)
            seg.setWidth_forSegment_(0, i)  # autosize
        if self.current_tag is not None:
            seg.setSelectedSegment_(conn_tags.index(self.current_tag))

    def _select_tab_by_tag(self, tag: str) -> None:
        if tag == "gear":
            self._on_segment(len(self.tabs) - 1)
            return
        for i, t in enumerate(self.tabs):
            if t.kind == "connection" and t.tag == tag:
                self._seg.setSelectedSegment_(i)
                self._on_segment(i)
                return

    def _on_segment(self, idx: int) -> None:
        if not (0 <= idx < len(self.tabs)):
            return
        spec = self.tabs[idx]
        if spec.kind == "connection":
            self.current_tag = spec.tag
            self._reload_sidebar(preserve=False)
            self._render_current_detail()
        elif spec.kind == "add":
            # Revert visual selection, then run the add-connection flow.
            self._restore_segment_selection()
            self.tray.run_add_connection_from_window()
        elif spec.kind == "gear":
            # Phase 1: placeholder; Task 9 renders the settings pane here.
            self._restore_segment_selection()
            self._render_placeholder("App settings move here in Task 9.")

    def _restore_segment_selection(self) -> None:
        conn_tags = [t.tag for t in self.tabs if t.kind == "connection"]
        if self.current_tag in conn_tags:
            self._seg.setSelectedSegment_(conn_tags.index(self.current_tag))

    # ----------------------------------------------------------- sidebar

    def _current_conn(self):
        if self._cfg is None or self.current_tag is None:
            return None
        return next((c for c in self._cfg.connections
                     if c.tag == self.current_tag), None)

    def _reload_sidebar(self, *, preserve: bool) -> None:
        prev = self._selected_identity() if preserve else None
        conn = self._current_conn()
        if conn is None:
            self.sidebar_rows = []
        else:
            conn_shares = [s for s in self._shares
                           if getattr(s, "conn_tag", None) == conn.tag]
            self.sidebar_rows = build_sidebar_rows(conn, conn_shares)
        self._sidebar_tv.reloadData()
        # Restore previous selection by identity, else default to Settings.
        target = None
        if prev is not None:
            target = next((i for i, r in enumerate(self.sidebar_rows)
                           if r.identity == prev), None)
        if target is None:
            target = next((i for i, r in enumerate(self.sidebar_rows)
                           if r.kind == "connection"), None)
        if target is not None:
            self._select_sidebar_row(target)

    def _select_sidebar_row(self, row: int) -> None:
        from Foundation import NSIndexSet  # type: ignore[import]
        self._sidebar_tv.selectRowIndexes_byExtendingSelection_(
            NSIndexSet.indexSetWithIndex_(row), False)

    def _selected_identity(self) -> tuple | None:
        row = int(self._sidebar_tv.selectedRow()) if self.window else -1
        if 0 <= row < len(self.sidebar_rows):
            r = self.sidebar_rows[row]
            if r.kind != "header":
                return r.identity
        return None

    def _on_sidebar_selection(self) -> None:
        self._render_current_detail()

    # ------------------------------------------------------------ detail

    def _render_current_detail(self) -> None:
        identity = self._selected_identity()
        conn = self._current_conn()
        if conn is None or identity is None:
            self._render_placeholder("No selection.")
            return
        spec = self._detail_spec_for(conn, identity)
        if spec is None:
            self._render_placeholder("Item no longer exists.")
            return
        self._render_detail(spec, identity)

    def _detail_spec_for(self, conn, identity: tuple) -> DetailSpec | None:
        kind = identity[0]
        if kind == "connection":
            st = next((s for s in self._statuses if s.tag == conn.tag), None)
            return build_connection_detail(conn, st)
        if kind == "domain":
            host = identity[1]
            return build_domain_detail(conn, host) if host in conn.pac_hosts else None
        if kind == "forward":
            _, direction, src_port = identity
            fws = (conn.forwards.local if direction == "local"
                   else conn.forwards.remote)
            fw = next((f for f in fws if f.src_port == src_port), None)
            return build_forward_detail(conn, fw, direction) if fw else None
        if kind == "share":
            info = next((s for s in self._shares if s.port == identity[1]), None)
            return build_share_detail(info) if info else None
        return None

    def _clear_detail(self) -> None:
        for v in list(self._detail.subviews()):
            v.removeFromSuperview()

    def _render_placeholder(self, text: str) -> None:
        from Cocoa import NSMakeRect, NSTextField  # type: ignore[import]
        self._clear_detail()
        self._current_detail_title = None
        h = self._detail.frame().size.height
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(8, h - 40, 400, 24))
        lbl.setStringValue_(text)
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        self._detail.addSubview_(lbl)

    def _render_detail(self, spec: DetailSpec, identity: tuple) -> None:
        """Generic renderer: title, label/value rows, optional Enabled
        switch, action button row. Actions dispatch via the tray."""
        from AppKit import (  # type: ignore[import]
            NSFont,
            NSOffState,
            NSOnState,
            NSSwitchButton,
        )
        from Cocoa import NSButton, NSMakeRect, NSTextField  # type: ignore[import]

        self._clear_detail()
        self._current_detail_title = spec.title
        w = self._detail.frame().size.width
        h = self._detail.frame().size.height
        y = h - 36

        title = NSTextField.alloc().initWithFrame_(NSMakeRect(8, y, w - 16, 24))
        title.setStringValue_(f"Config for {spec.title}")
        title.setFont_(NSFont.boldSystemFontOfSize_(16))
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        self._detail.addSubview_(title)
        y -= 40

        def _row(label: str, value: str) -> None:
            nonlocal y
            lab = NSTextField.alloc().initWithFrame_(NSMakeRect(8, y, 130, 20))
            lab.setStringValue_(label)
            lab.setAlignment_(2)
            for fld in (lab,):
                fld.setBezeled_(False)
                fld.setDrawsBackground_(False)
                fld.setEditable_(False)
            val = NSTextField.alloc().initWithFrame_(NSMakeRect(148, y, w - 160, 20))
            val.setStringValue_(value)
            val.setBezeled_(False)
            val.setDrawsBackground_(False)
            val.setEditable_(False)
            val.setSelectable_(True)
            self._detail.addSubview_(lab)
            self._detail.addSubview_(val)
            y -= 26

        for label, value in spec.rows:
            _row(label, str(value))

        if spec.toggle is not None:
            t_label, t_value, t_action = spec.toggle
            lab = NSTextField.alloc().initWithFrame_(NSMakeRect(8, y, 130, 20))
            lab.setStringValue_(t_label)
            lab.setAlignment_(2)
            lab.setBezeled_(False)
            lab.setDrawsBackground_(False)
            lab.setEditable_(False)
            self._detail.addSubview_(lab)
            sw = NSButton.alloc().initWithFrame_(NSMakeRect(148, y, 60, 20))
            sw.setButtonType_(NSSwitchButton)
            sw.setTitle_("")
            sw.setState_(NSOnState if t_value else NSOffState)
            handler = _get_action_handler_cls().alloc().initWithCallback_(
                lambda _s, aid=t_action, ident=identity: self.tray.dispatch_window_action(aid, ident))
            self._handlers.append(handler)
            sw.setTarget_(handler)
            sw.setAction_("fire:")
            self._detail.addSubview_(sw)
            y -= 32

        # Action buttons, left-to-right
        x = 8
        for action in spec.actions:
            bw = max(70, 16 + 8 * len(action.title))
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y - 6, bw, 28))
            btn.setTitle_(action.title)
            btn.setBezelStyle_(1)
            btn.setEnabled_(action.enabled)
            handler = _get_action_handler_cls().alloc().initWithCallback_(
                lambda _s, aid=action.action_id, ident=identity: self.tray.dispatch_window_action(aid, ident))
            self._handlers.append(handler)
            btn.setTarget_(handler)
            btn.setAction_("fire:")
            self._detail.addSubview_(btn)
            x += bw + 8
```

- [ ] **Step 2: Wire into mac.py.** Add to `SusOpsMacTray`:

```python
    def _show_config_window(self, tab: str | None = None) -> None:
        def _open():
            if self._config_window is None:
                from susops.tray.mac_config_window import ConfigWindow
                self._config_window = ConfigWindow(self)
            self._config_window.open(tab)
        _on_main(_open)

    def run_add_connection_from_window(self) -> None:
        """Called by the window's '+' tab. Same flow as the menu item; the
        window refreshes via the action's normal completion path."""
        self._show_add_connection_dialog()
        self._refresh_config_window()

    def dispatch_window_action(self, action_id: str, identity: tuple) -> None:
        """Map a detail-pane action onto the existing do_* methods.
        Extended for shares in Task 7."""
        conn_tag = self._config_window.current_tag if self._config_window else None
        if conn_tag is None:
            return
        kind = identity[0]
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
            if _show_confirm("Remove Connection",
                             f"Remove connection '{conn_tag}' and all its "
                             f"domains, forwards and shares?", ok="Remove"):
                self.do_remove_connection(conn_tag)
        elif kind == "domain":
            host = identity[1]
            if action_id == "domain.test":
                self.do_test_domain(host, conn_tag)
            elif action_id == "domain.toggle":
                self.do_toggle_pac_host_enabled(host)
            elif action_id == "domain.remove":
                if _show_confirm("Remove Domain", f"Remove '{host}'?", ok="Remove"):
                    self.do_remove_pac_host(host)
        elif kind == "forward":
            _, direction, src_port = identity
            if action_id == "forward.test":
                self.do_test_forward(conn_tag, src_port, direction)
            elif action_id == "forward.toggle":
                self.do_toggle_forward_enabled(conn_tag, src_port, direction)
            elif action_id == "forward.remove":
                if _show_confirm("Remove Forward", f"Remove :{src_port} ({direction})?",
                                 ok="Remove"):
                    if direction == "local":
                        self.do_remove_local_forward(src_port)
                    else:
                        self.do_remove_remote_forward(src_port)
        self._refresh_config_window()

    def _refresh_config_window(self) -> None:
        cw = self._config_window
        if cw is not None and cw.is_open():
            _on_main(cw.refresh)
```

Add a refresh hook to the existing `do_poll` override (mac.py already overrides it for `_refresh_share_submenu`):

```python
    def do_poll(self) -> None:
        super().do_poll()
        self._refresh_share_submenu()
        self._refresh_config_window()
```

Add a temporary menu item in `_build_menu` (right under "Settings…"; replaced in Task 9/10):

```python
            rumps.MenuItem("Config Window…", callback=lambda _: self._show_config_window()),
```

Extend `_debug_handlers` return dict with:

```python
            "open-config": lambda args: _run_on_main(
                lambda: (self._ensure_config_window().open(args[0] if args else None),
                         {"ok": True})[1]),
            "select": lambda args: _run_on_main(
                lambda: self._ensure_config_window().select(
                    args[0],
                    args[1] if len(args) > 1 else None,
                    int(args[2]) if len(args) > 2 else 0)),
            "dump-window": lambda args: _run_on_main(
                lambda: self._ensure_config_window().dump()),
```

with the helper on `SusOpsMacTray`:

```python
    def _ensure_config_window(self):
        if self._config_window is None:
            from susops.tray.mac_config_window import ConfigWindow
            self._config_window = ConfigWindow(self)
        return self._config_window
```

- [ ] **Step 3: Feedback-loop verification (iterate until right)**

Run the dev-loop commands from the top of this plan (fresh workspace, seed data), then:

```bash
.venv/bin/python tools/tray_debug.py 7799 open-config
.venv/bin/python tools/tray_debug.py 7799 dump-window
.venv/bin/python tools/tray_debug.py 7799 screenshot /tmp/cw-1.png
.venv/bin/python tools/tray_debug.py 7799 select work domains 0
.venv/bin/python tools/tray_debug.py 7799 screenshot /tmp/cw-2.png
.venv/bin/python tools/tray_debug.py 7799 select work forwards 1
.venv/bin/python tools/tray_debug.py 7799 screenshot /tmp/cw-3.png
```

Check (agent: Read each PNG): tab strip shows `● work / ○ home / + / ⚙` (dots correct), sidebar groups in order with correct dot labels, detail shows "Config for …" with rows, selection survives a `refresh` (run `dump-window` after a poll interval and confirm `selected` unchanged). Iterate on layout glitches before moving on — this skeleton is the foundation every later task renders into.

- [ ] **Step 4: Extend the gui smoke test**

Append to `tests/tray/test_gui_smoke.py`:

```python
def test_config_window_opens_and_dumps(tray_proc):
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    c.add_pac_host("blabla.de", conn_tag="work")
    assert tray_proc.send("open-config").get("ok")
    dump = tray_proc.send("dump-window")
    assert dump["open"] is True
    assert any("work" in t for t in dump["tabs"])
    labels = [r["label"] for r in dump["sidebar"]]
    assert "DOMAINS" in labels
    assert any("blabla.de" in l for l in labels)
    sel = tray_proc.send("select work domains 0")
    assert sel.get("ok"), sel
```

Run: `SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui -v` → all pass.
Run: `.venv/bin/pytest` → full suite still green.

- [ ] **Step 5: Commit**

```bash
git add src/susops/tray/mac_config_window.py src/susops/tray/mac.py tests/tray/test_gui_smoke.py
git commit -m "feat(tray): config window skeleton — tabs, sidebar, detail, debug hooks"
```

### Task 6: Detail-pane actions verified end-to-end (connection / domain / forward)

The rendering and dispatch code already landed in Task 5. This task verifies every action id against a live daemon and fixes what breaks.

**Files:**
- Modify: `src/susops/tray/mac.py` / `mac_config_window.py` (fixes found during verification)
- Test: `tests/tray/test_gui_smoke.py`

- [ ] **Step 1: Add gui test for toggle + remove flows driven through the daemon**

Append to `tests/tray/test_gui_smoke.py`:

```python
def test_window_reflects_external_changes(tray_proc):
    """The poll-driven refresh must pick up daemon-side changes."""
    import time
    from susops.client import SusOpsClient
    c = SusOpsClient(workspace=tray_proc.workspace)
    c.add_connection("work", "user@bastion")
    assert tray_proc.send("open-config").get("ok")
    c.add_pac_host("added-later.de", conn_tag="work")
    time.sleep(4)  # > one poll interval
    labels = [r["label"] for r in tray_proc.send("dump-window")["sidebar"]]
    assert any("added-later.de" in l for l in labels)
```

Run: `SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui -v` — fix refresh bugs until green.

- [ ] **Step 2: Manual action sweep via feedback loop**

With the dev loop running and seeded: select each item kind, screenshot, then click-test each action **by invoking the dispatch directly** through a temporary debug command — add to `_debug_handlers`:

```python
            "action": lambda args: _run_on_main(
                lambda: (self.dispatch_window_action(
                    args[0], tuple(self._config_window._selected_identity() or ())),
                    {"ok": True})[1]),
```

Then for each: `select work domains 0` → `action domain.toggle` → `dump-window` (label dot flips ● → ○); `action domain.test` (alert appears — screenshot it); `select work forwards 0` → `action forward.toggle`; connection pane: `action conn.toggle` twice. Confirmation dialogs (`*.remove`) are exercised by hand once — they block on user input by design.

Expected wrinkle: `_show_confirm` runs modal on the main thread — the `action` debug command will time out for `*.remove` while the dialog is up. That's fine; verify removals interactively.

- [ ] **Step 3: Commit fixes**

```bash
git add -A src/susops/tray tests/tray
git commit -m "feat(tray): verify + fix config-window action dispatch (domain/forward/conn)"
```

### Task 7: Shares in the window (detail actions + share/fetch flows)

**Files:**
- Modify: `src/susops/tray/mac.py` (`dispatch_window_action` share branch, share/fetch dialog reuse with preset connection)

- [ ] **Step 1: Extend `dispatch_window_action`** — add before the final `self._refresh_config_window()`:

```python
        elif kind == "share":
            port = identity[1]
            info = next((s for s in self.manager.list_shares() if s.port == port), None)
            if info is None:
                pass  # vanished; refresh below handles it
            elif action_id == "share.reveal":
                _show_message("Share Password",
                              f"{Path(info.file_path).name}\nPassword: {info.password}")
            elif action_id == "share.stop":
                self.do_stop_share(port)
            elif action_id == "share.start":
                self.do_share(info.conn_tag, info.file_path,
                              password=info.password, port=info.port)
            elif action_id == "share.delete":
                if _show_confirm("Delete Share",
                                 f"Delete share on port {port}?", ok="Delete"):
                    self.do_delete_share(port)
```

(`Path` is already imported in mac.py.)

- [ ] **Step 2: Preset-connection share/fetch.** Locate `_show_share_file_dialog` and `_show_fetch_file_dialog` in mac.py (~line 2749+). Change their signatures to accept a preset:

```python
    def _show_share_file_dialog(self, conn_tag: str | None = None) -> None:
```

and inside, where the connection popup field is built, use `"default": conn_tag or tags[0]`. Same change for `_show_fetch_file_dialog`. Menu callbacks keep calling them with no argument — behavior unchanged.

- [ ] **Step 3: Verify via feedback loop**

```bash
echo hello > /tmp/payload.bin
# share via client against the dev workspace, conn 'work' must be running…
# OR exercise the dialog interactively from the window in Task 8's Add… menu.
.venv/bin/python tools/tray_debug.py 7799 select work shares 0
.venv/bin/python tools/tray_debug.py 7799 screenshot /tmp/cw-share.png
.venv/bin/python tools/tray_debug.py 7799 action share.reveal   # password alert; screenshot it
```

Note: a share requires its connection to be running; with the fake `user@bastion` host the share may sit in "connection down" state — that is itself the three-state render to verify (◌ red dot).

- [ ] **Step 4: Run full suite + commit**

```bash
.venv/bin/pytest && SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui
git add src/susops/tray/mac.py
git commit -m "feat(tray): share detail actions + preset-connection share/fetch dialogs"
```

### Task 8: Add… pull-down + "+" tab flows

**Files:**
- Modify: `src/susops/tray/mac.py` (`_show_add_host_dialog`, `_show_add_forward_dialog` get preset param)
- Modify: `src/susops/tray/mac_config_window.py` (populate Add… menu)

- [ ] **Step 1: Preset-connection add dialogs.** In mac.py change:

```python
    def _show_add_host_dialog(self, conn_tag: str | None = None) -> None:
```

and where fields are built, `"default": conn_tag or tags[0]` for the `conn` popup. Same for:

```python
    def _show_add_forward_dialog(self, *, remote: bool, conn_tag: str | None = None) -> None:
```

Menu callbacks unchanged (no argument → old behavior).

- [ ] **Step 2: Populate the Add… pull-down** in `ConfigWindow._build` (replace the placeholder single item):

```python
        # Pull-down: first item is the title, the rest are commands.
        add_btn.removeAllItems()
        add_btn.addItemsWithTitles_([
            "Add…",
            "Add Domain / IP / CIDR…",
            "Add Local Forward…",
            "Add Remote Forward…",
            "Share File…",
            "Fetch File…",
        ])
        add_handler = _get_action_handler_cls().alloc().initWithCallback_(
            lambda sender: self._on_add_command(str(sender.titleOfSelectedItem() or "")))
        self._handlers.append(add_handler)
        add_btn.setTarget_(add_handler)
        add_btn.setAction_("fire:")
```

and add the method:

```python
    def _on_add_command(self, title: str) -> None:
        tag = self.current_tag
        if tag is None:
            return
        if title.startswith("Add Domain"):
            self.tray._show_add_host_dialog(conn_tag=tag)
        elif title.startswith("Add Local"):
            self.tray._show_add_forward_dialog(remote=False, conn_tag=tag)
        elif title.startswith("Add Remote"):
            self.tray._show_add_forward_dialog(remote=True, conn_tag=tag)
        elif title.startswith("Share File"):
            self.tray._show_share_file_dialog(conn_tag=tag)
        elif title.startswith("Fetch File"):
            self.tray._show_fetch_file_dialog(conn_tag=tag)
        self.tray._refresh_config_window()
```

- [ ] **Step 3: Verify interactively** (modal dialogs need a human or stay open under the agent's screenshot — both acceptable): open window → Add… → Add Domain → dialog appears with connection preset to the current tab → add `test.example.com` → sidebar shows it after refresh. Repeat once for a local forward. Screenshot the dialog over the window for the record. Verify the `+` tab opens Add Connection and a new tab appears after adding tag `third` (host can be fake).

- [ ] **Step 4: Run suites + commit**

```bash
.venv/bin/pytest && SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui
git add src/susops/tray/mac.py src/susops/tray/mac_config_window.py
git commit -m "feat(tray): Add… pull-down and + tab wired to preset add flows"
```

### Task 9: Gear tab — embedded app settings

**Files:**
- Modify: `src/susops/tray/mac.py` (extract `_settings_fields()` + `_apply_settings()` from `_show_settings_dialog`)
- Modify: `src/susops/tray/mac_config_window.py` (render gear pane)

- [ ] **Step 1: Extract reusable pieces from `_show_settings_dialog`** (mac.py ~line 2324). Split it into:

```python
    def _settings_fields(self) -> tuple[list[dict], dict]:
        """(field specs, context) shared by the legacy dialog and the gear pane.
        Context carries current port values for _apply_settings validation."""
```

— move the `defaults` construction and the `fields = [...]` list there verbatim (including the logo segmented options + `_preview` and `self._preview_bandwidth_visibility` hooks), returning `(fields, {"rpc_port": rpc_port, "sse_port": sse_port, "pac_port": pac_port, "logo_styles": logo_styles, "saved_logo": saved_logo})`.

```python
    def _apply_settings(self, result: dict, ctx: dict) -> str | None:
        """Validate + persist a settings form result. Returns an error
        message (caller re-shows) or None on success."""
```

— move the port validation loop + `update_app_config` + `update_config` + launch-at-login thread there verbatim, returning the `_show_message`-style error text instead of calling `_show_message` directly. `_show_settings_dialog` becomes a thin loop: build fields → `_show_form_dialog` → `_apply_settings` → on error `_show_message` + retry. **Behavior must be identical — verify by opening Settings… from the menu and saving once.**

- [ ] **Step 2: Render the gear pane.** In `ConfigWindow._on_segment`, replace the gear placeholder with `self._render_gear_pane()`:

```python
    def _render_gear_pane(self) -> None:
        from Cocoa import NSButton, NSMakeRect  # type: ignore[import]
        from susops.tray.mac import _show_message

        self._clear_detail()
        self._current_detail_title = "App Settings"
        fields, ctx = self.tray._settings_fields()
        # Reuse the form-field builder style: render each field with the same
        # widget kinds _show_form_dialog uses, but into self._detail.
        # Implementation detail: extract mac._build_form_fields(content, fields,
        # origin_y, input_x) from _show_form_dialog's field loop (mechanical
        # move of the per-kind widget construction, returning the widgets dict
        # + handler refs) and call it here AND from _show_form_dialog so there
        # is exactly one widget-construction path.
        from susops.tray.mac import _build_form_fields, _read_form_values
        h = self._detail.frame().size.height
        widgets, handlers = _build_form_fields(self._detail, fields, top_y=h - 16)
        self._handlers.extend(handlers)

        def _save(_sender):
            result = _read_form_values(fields, widgets)
            err = self.tray._apply_settings(result, ctx)
            if err:
                _show_message("Invalid Settings", err)
            self.tray._refresh_config_window()

        save = NSButton.alloc().initWithFrame_(NSMakeRect(8, 12, 90, 28))
        save.setTitle_("Save")
        save.setBezelStyle_(1)
        sh = _get_action_handler_cls().alloc().initWithCallback_(_save)
        self._handlers.append(sh)
        save.setTarget_(sh)
        save.setAction_("fire:")
        self._detail.addSubview_(save)

        open_cfg = NSButton.alloc().initWithFrame_(NSMakeRect(106, 12, 150, 28))
        open_cfg.setTitle_("Open Config File")
        open_cfg.setBezelStyle_(1)
        oh = _get_action_handler_cls().alloc().initWithCallback_(
            lambda _s: self.tray.do_open_config_file())
        self._handlers.append(oh)
        open_cfg.setTarget_(oh)
        open_cfg.setAction_("fire:")
        self._detail.addSubview_(open_cfg)
```

This requires the third extraction in mac.py: `_build_form_fields(content, fields, top_y)` and `_read_form_values(fields, widgets)` — mechanical moves of `_show_form_dialog`'s widget-construction loop (lines ~640-812) and result-reading loop (lines ~902-922) into module functions that `_show_form_dialog` now calls. The gear pane hides the sidebar/Add… (gear is app-level): in `_on_segment` gear branch also call `self._sidebar_tv.enclosingScrollView().setHidden_(True)` / `self._add_btn.setHidden_(True)`, and un-hide both in the connection branch.

- [ ] **Step 3: Verify** — legacy dialog still works (open Settings…, save, no behavior change); gear tab renders all fields; toggling logo style live-previews the menu-bar icon; saving an invalid port shows the error and keeps edits; `screenshot` the gear pane.

- [ ] **Step 4: Run suites + commit**

```bash
.venv/bin/pytest && SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui
git add src/susops/tray/mac.py src/susops/tray/mac_config_window.py
git commit -m "feat(tray): gear tab with embedded app settings (shared form builder)"
```

---

# Phase 2 — unification

### Task 10: Slim menu + delete dead dialogs

**Files:**
- Modify: `src/susops/tray/mac.py` (`_build_menu`, `update_menu_sensitivity`, deletions)
- Test: `tests/tray/test_gui_smoke.py`

- [ ] **Step 1: Write the failing gui test for the final menu**

Append to `tests/tray/test_gui_smoke.py`:

```python
EXPECTED_MENU = [
    "SusOps:",        # status (prefix match)
    "Settings…",
    "Start Proxy",
    "Stop Proxy",
    "Restart Proxy",
    "Show Status",
    "Show Logs",
    "Launch Browser",
    "Reset All",
    "About SusOps",
    "Quit",
]

REMOVED_MENU = ["Add", "Remove", "Manage", "Test", "File Transfer",
                "Open Config File", "Config Window…"]


def test_unified_menu_structure(tray_proc):
    menu = tray_proc.send("dump-menu")["menu"]
    titles = [n["title"] for n in menu if "title" in n]
    for expected in EXPECTED_MENU:
        assert any(t.startswith(expected) for t in titles), f"missing {expected}"
    for removed in REMOVED_MENU:
        assert not any(t == removed for t in titles), f"should be gone: {removed}"
```

Run: `SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui -k unified -v` → FAILS (old menu).

- [ ] **Step 2: Rewrite `_build_menu`** to exactly:

```python
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
```

Delete from `_build_menu`: the add/rm/manage/test submenu construction blocks, `self._item_test_all` (and its references in `update_menu_sensitivity` — remove the `_item_test_all` block there), `self._ft_menu` construction, the temporary "Config Window…" item.

- [ ] **Step 3: Delete dead methods** from mac.py — each must have NO remaining callers (grep before deleting):

`_show_settings_dialog`, `_show_rm_connection_dialog`, `_show_rm_host_dialog`, `_show_rm_local_dialog`, `_show_rm_remote_dialog`, `_show_toggle_connection_dialog`, `_show_toggle_domain_dialog`, `_show_toggle_forward_dialog`, `_show_start_connection_dialog`, `_show_stop_connection_dialog`, `_show_restart_connection_dialog`, `_show_test_connection_dialog`, `_show_test_domain_dialog`, `_show_test_forward_dialog`, `_show_pick_dialog`, `_refresh_share_submenu` (and its call in `do_poll`, keeping `_refresh_config_window`), `self._active_shares` init.

KEEP (window reuses them): `_show_add_connection_dialog`, `_show_add_host_dialog`, `_show_add_forward_dialog`, `_show_share_file_dialog`, `_show_fetch_file_dialog`, `_make_share_info_handler` → check: only used by `_refresh_share_submenu` → if so, delete it too. Also delete now-unused `do_*` methods in base.py? NO — Linux tray still uses every `do_*`; base.py is untouched.

```bash
grep -n "_show_pick_dialog\|_refresh_share_submenu\|_item_test_all" src/susops/tray/mac.py
# must return only definition lines about to be deleted
```

- [ ] **Step 4: Run everything**

```bash
.venv/bin/pytest
SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui -v   # incl. test_unified_menu_structure → PASS
```

Feedback loop: `dump-menu` matches the spec menu; screenshot the window once more; interactively confirm ⌘, opens the window.

- [ ] **Step 5: Commit**

```bash
git add src/susops/tray/mac.py tests/tray/test_gui_smoke.py
git commit -m "feat(tray): unified slim menu — config window absorbs add/remove/manage/test/share dialogs"
```

### Task 11: Docs + final verification sweep

**Files:**
- Modify: `README.md` (tray section — menu items, new Settings window, screenshots if the README embeds any)

- [ ] **Step 1: Update README** — replace descriptions of Add/Remove/Manage/Test/File-Transfer menu items with the unified Settings window (per-connection tabs, sidebar, Add… menu, gear tab). Mention `SUSOPS_TRAY_WORKSPACE` and `SUSOPS_TRAY_DEBUG_PORT` in a development section, plus `SUSOPS_RUN_GUI_TESTS=1 pytest -m gui`.

- [ ] **Step 2: Full verification**

```bash
.venv/bin/pytest                                   # all green
SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui -v  # all green
python tools/gen_openapi.py --check                # unchanged (no facade changes in this plan)
```

Final feedback-loop screenshot set (fresh seeded workspace): window on each item kind + gear tab + dialog-over-window; agent reviews each PNG against the mockup.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): unified tray settings window + dev debug loop"
```

---

## Self-review

**Spec coverage:** workspace override → Task 1; debug server + commands (ping/dump-menu/screenshot/open-config/select/dump-window/quit) → Tasks 2–3, 5; in-process screenshot (no TCC) → Task 3; window lifecycle/tab strip/sidebar/detail → Task 5; detail panes + actions for connection/domain/forward → Tasks 4–6; shares (three-state, stop/start/delete/reveal, share/fetch flows) → Tasks 4, 7; Add… pull-down + "+" tab incl. all four add types → Task 8 (+ Task 5 for "+"); gear tab (settings fields, validation, Open Config File) → Task 9; slim menu + dead-code deletion → Task 10; refresh model (poll hook, selection preservation, post-action refresh) → Task 5 Steps 1–2, gui test Task 6 Step 1; Layer-2 headless tests → Tasks 1, 2, 4; Layer-3 gui tests → Tasks 3, 5, 6, 10; README → Task 11. Phase 3 (inline editing) is explicitly out of scope per spec.

**Known risks (accepted):** AppKit layout code in Tasks 5/9 will need visual iteration — that's what the Phase-0 loop is for; the plan's code is the starting point, the screenshots are the acceptance test. `tableView_isGroupRow_` styling varies by macOS version; if group rows look wrong, fall back to bold-font headers via cell font. rumps `Menu.values()` quirks around separators are handled defensively in `_menu_tree`.

**Type consistency check:** `TabSpec/SidebarRow/Action/DetailSpec` names match between model (Task 4), window (Task 5), and tests; `dispatch_window_action(action_id, identity)` signature consistent across Tasks 5–7; debug command names consistent between spec, handlers (Tasks 3/5/6), and gui tests; `_build_form_fields`/`_read_form_values`/`_settings_fields`/`_apply_settings` introduced and consumed only in Task 9.
