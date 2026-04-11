# UDP Port Forward Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-forward TCP/UDP protocol flags to SusOps port forwards, using socat over the existing SSH ControlMaster to tunnel UDP traffic.

**Architecture:** Each `PortForward` gains `tcp: bool = True` and `udp: bool = False` flags. UDP local forwards use socat's EXEC address (one process, pipes through ControlMaster). UDP remote forwards use an SSH -R slave plus two socat processes (remote socat → TCP → local socat). The facade starts/stops UDP socat processes alongside existing SSH slaves.

**Tech Stack:** Python 3.11+, Pydantic v2, socat (system binary), Textual 8.x, GTK3 (Linux tray), rumps (macOS tray)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/susops/core/config.py` | Modify | Add `tcp`/`udp` fields + validation to `PortForward` |
| `src/susops/core/socat.py` | **Create** | All UDP socat process management |
| `src/susops/facade.py` | Modify | Wire socat start/stop into lifecycle hooks |
| `src/susops/tui/screens/__init__.py` | Modify | Add `proto_label()` helper |
| `src/susops/tui/screens/connection_editor.py` | Modify | Protocol checkboxes in dialog; Protocol column in tables |
| `src/susops/tui/screens/dashboard.py` | Modify | Protocol shown in forwards display |
| `src/susops/tray/linux.py` | Modify | TCP/UDP checkboxes in add-forward dialogs |
| `src/susops/tray/mac.py` | Modify | TCP/UDP prompts in `_prompt_add_forward` |
| `packaging/homebrew/Formula/susops.rb` | Modify | Add `depends_on "socat"` |
| `packaging/aur/PKGBUILD` | Modify | Add `socat` to `optdepends` |
| `pyproject.toml` | Modify | Add udp comment block |
| `tests/test_config.py` | Modify | Protocol flag tests |
| `tests/test_socat.py` | **Create** | Command builder + stop tests |
| `tests/test_facade.py` | Modify | UDP forward lifecycle tests |

---

## Task 1: Config — `tcp`/`udp` fields on `PortForward`

**Files:**
- Modify: `src/susops/core/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
from pydantic import ValidationError

def test_port_forward_defaults_include_tcp():
    fw = PortForward(src_port=8080, dst_port=80)
    assert fw.tcp is True
    assert fw.udp is False


def test_port_forward_tcp_false_udp_false_raises():
    with pytest.raises(ValidationError, match="At least one of tcp/udp must be True"):
        PortForward(src_port=8080, dst_port=80, tcp=False, udp=False)


def test_port_forward_udp_only():
    fw = PortForward(src_port=53, dst_port=53, tcp=False, udp=True)
    assert fw.tcp is False
    assert fw.udp is True


def test_port_forward_both_protocols():
    fw = PortForward(src_port=53, dst_port=53, tcp=True, udp=True)
    assert fw.tcp is True
    assert fw.udp is True


def test_port_forward_backward_compat_no_protocol_fields():
    """Old YAML entries with no tcp/udp keys still parse with correct defaults."""
    fw = PortForward.model_validate({"src_port": 5432, "dst_port": 5432})
    assert fw.tcp is True
    assert fw.udp is False
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_config.py::test_port_forward_defaults_include_tcp \
       tests/test_config.py::test_port_forward_tcp_false_udp_false_raises \
       tests/test_config.py::test_port_forward_udp_only \
       tests/test_config.py::test_port_forward_both_protocols \
       tests/test_config.py::test_port_forward_backward_compat_no_protocol_fields -v
```

Expected: `FAILED` — `PortForward` has no `tcp`/`udp` fields yet.

- [ ] **Step 3: Implement the fields**

In `src/susops/core/config.py`, replace the `PortForward` class with:

```python
class PortForward(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tag: str = ""
    src_addr: str = "localhost"
    src_port: int
    dst_addr: str = "localhost"
    dst_port: int
    tcp: bool = True
    udp: bool = False

    @model_validator(mode="before")
    @classmethod
    def handle_legacy_schema(cls, data: Any) -> Any:
        """Handle old schema where 'src'/'dst' were plain port numbers."""
        if isinstance(data, dict) and "src" in data and "src_port" not in data:
            data = dict(data)
            data["src_port"] = int(data.pop("src"))
            data["dst_port"] = int(data.pop("dst", data["src_port"]))
        return data

    @model_validator(mode="after")
    def require_at_least_one_protocol(self) -> "PortForward":
        if not self.tcp and not self.udp:
            raise ValueError("At least one of tcp/udp must be True")
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_config.py -v
```

Expected: all config tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/susops/core/config.py tests/test_config.py
git commit -m "feat: add tcp/udp protocol flags to PortForward"
```

---

## Task 2: New module — `core/socat.py`

**Files:**
- Create: `src/susops/core/socat.py`
- Create: `tests/test_socat.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_socat.py`:

```python
"""Tests for susops.core.socat — UDP socat command building and process management."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from susops.core.config import Connection, PortForward
from susops.core.socat import (
    UDP_PROCESS_PREFIX,
    _fw_tag,
    _udp_process_name,
    stop_udp_forward,
    stop_all_udp_forwards_for_connection,
)


@pytest.fixture
def conn():
    return Connection(tag="work", ssh_host="user@host.example.com", socks_proxy_port=1080)


@pytest.fixture
def sock(tmp_path):
    return tmp_path / "sockets" / "work.sock"


@pytest.fixture
def fw_local():
    return PortForward(src_port=53, dst_port=53, dst_addr="dns.internal", tcp=False, udp=True)


@pytest.fixture
def fw_remote():
    return PortForward(src_port=51820, dst_port=51820, tcp=False, udp=True)


def test_fw_tag_uses_tag_field():
    fw = PortForward(src_port=53, dst_port=53, tag="dns", udp=True, tcp=False)
    assert _fw_tag(fw, "local") == "dns"


def test_fw_tag_falls_back_to_direction_port():
    fw = PortForward(src_port=53, dst_port=53, udp=True, tcp=False)
    assert _fw_tag(fw, "local") == "local-53"
    assert _fw_tag(fw, "remote") == "remote-53"


def test_udp_process_name():
    name = _udp_process_name("work", "local-53", "lsocat")
    assert name == "susops-udp-work-local-53-lsocat"


def test_stop_udp_forward_stops_matching_processes():
    mgr = MagicMock()
    mgr.status_all.return_value = {
        "susops-udp-work-local-53-lsocat": "running",
        "susops-udp-work-local-80-lsocat": "running",  # different forward
        "susops-fwd-work-local-53": "running",          # not a UDP process
    }
    mgr.stop.return_value = True
    result = stop_udp_forward("work", "local-53", mgr)
    assert result is True
    mgr.stop.assert_called_once_with("susops-udp-work-local-53-lsocat")


def test_stop_udp_forward_returns_false_when_nothing_running():
    mgr = MagicMock()
    mgr.status_all.return_value = {}
    result = stop_udp_forward("work", "local-53", mgr)
    assert result is False


def test_stop_all_udp_forwards_for_connection():
    mgr = MagicMock()
    mgr.status_all.return_value = {
        "susops-udp-work-local-53-lsocat": "running",
        "susops-udp-work-remote-51820-ssh": "running",
        "susops-udp-work-remote-51820-rsocat": "running",
        "susops-udp-work-remote-51820-lsocat": "running",
        "susops-udp-other-local-53-lsocat": "running",  # different connection
    }
    stop_all_udp_forwards_for_connection("work", mgr)
    stopped = {call.args[0] for call in mgr.stop.call_args_list}
    assert "susops-udp-work-local-53-lsocat" in stopped
    assert "susops-udp-work-remote-51820-ssh" in stopped
    assert "susops-udp-work-remote-51820-rsocat" in stopped
    assert "susops-udp-work-remote-51820-lsocat" in stopped
    assert "susops-udp-other-local-53-lsocat" not in stopped
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_socat.py -v
```

Expected: `ModuleNotFoundError` — `susops.core.socat` doesn't exist yet.

- [ ] **Step 3: Create `src/susops/core/socat.py`**

```python
"""UDP port forwarding via socat over SSH ControlMaster.

Architecture:
  - Local UDP forward: socat EXEC approach — no SSH port forward slave needed.
    One process: local socat pipes each UDP conversation through ControlMaster
    to a remote socat instance (spawned per conversation via EXEC).
  - Remote UDP forward: SSH -R slave + remote socat + local socat.
    Three processes: an intermediate TCP port bridges the two socat instances.

Error handling: FileNotFoundError when socat is not installed locally;
subprocess exit errors when the remote host blocks command execution or
lacks socat — both surface through the process manager log file.
"""
from __future__ import annotations

from pathlib import Path

from susops.core.config import Connection, PortForward
from susops.core.ports import get_random_free_port
from susops.core.process import ProcessManager

__all__ = [
    "UDP_PROCESS_PREFIX",
    "_fw_tag",
    "_udp_process_name",
    "start_udp_forward",
    "stop_udp_forward",
    "stop_all_udp_forwards_for_connection",
]

UDP_PROCESS_PREFIX = "susops-udp"


def _fw_tag(fw: PortForward, direction: str) -> str:
    """Return the identifying tag for a forward (tag field or direction-port)."""
    return fw.tag or f"{direction}-{fw.src_port}"


def _udp_process_name(conn_tag: str, fw_tag: str, suffix: str) -> str:
    """Build a process name like susops-udp-<conn>-<fw_tag>-<suffix>."""
    return f"{UDP_PROCESS_PREFIX}-{conn_tag}-{fw_tag}-{suffix}"


def start_udp_forward(
    conn: Connection,
    fw: PortForward,
    direction: str,
    process_mgr: ProcessManager,
    workspace: Path,
) -> None:
    """Start socat process(es) for a UDP port forward.

    direction="local":  one local socat process using EXEC through ControlMaster.
    direction="remote": SSH -R slave + remote socat (via SSH) + local socat.

    Raises FileNotFoundError if socat is not installed locally.
    Remote errors (socat missing, shell access blocked) surface as immediate
    process exit — check the log file at workspace/logs/<name>.log.
    """
    from susops.core.ssh import socket_path
    sock = socket_path(conn.tag, workspace)
    tag = _fw_tag(fw, direction)
    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if direction == "local":
        _start_local_udp(conn, fw, sock, tag, process_mgr, log_dir)
    else:
        _start_remote_udp(conn, fw, sock, tag, process_mgr, log_dir)


def _start_local_udp(
    conn: Connection,
    fw: PortForward,
    sock: Path,
    tag: str,
    process_mgr: ProcessManager,
    log_dir: Path,
) -> None:
    """Local UDP forward: socat EXEC piped through SSH ControlMaster.

    Each UDP conversation forks one SSH session (multiplexed via ControlMaster).
    -T15 closes idle forked children after 15 seconds.
    """
    name = _udp_process_name(conn.tag, tag, "lsocat")
    ssh_exec = (
        f"ssh -S {sock} -T {conn.ssh_host} "
        f"socat - UDP4-SENDTO:{fw.dst_addr}:{fw.dst_port}"
    )
    cmd = [
        "socat",
        "-T15",
        f"UDP4-RECVFROM:{fw.src_port},reuseaddr,fork",
        f"EXEC:{ssh_exec}",
    ]
    log_file = log_dir / f"{name}.log"
    with open(log_file, "a") as log:
        process_mgr.start(name, cmd, stdout=log, stderr=log)


def _start_remote_udp(
    conn: Connection,
    fw: PortForward,
    sock: Path,
    tag: str,
    process_mgr: ProcessManager,
    log_dir: Path,
) -> None:
    """Remote UDP forward: SSH -R + remote socat (via SSH) + local socat.

    Allocates a random intermediate TCP port for bridging the two socat instances.
    """
    intermediate = get_random_free_port()

    # 1. SSH -R slave: binds intermediate port on remote, forwards to local
    ssh_name = _udp_process_name(conn.tag, tag, "ssh")
    ssh_cmd = [
        "ssh", "-N", "-T",
        "-o", f"ControlPath={sock}",
        "-R", f"{intermediate}:localhost:{intermediate}",
        conn.ssh_host,
    ]
    log_file = log_dir / f"{ssh_name}.log"
    with open(log_file, "a") as log:
        process_mgr.start(ssh_name, ssh_cmd, stdout=log, stderr=log)

    # 2. Remote socat (runs on remote host via SSH): UDP → TCP intermediate
    rsocat_name = _udp_process_name(conn.tag, tag, "rsocat")
    rsocat_cmd = [
        "ssh", "-T",
        "-o", f"ControlPath={sock}",
        conn.ssh_host,
        f"socat -T15 UDP4-RECVFROM:{fw.src_port},reuseaddr,fork TCP4:localhost:{intermediate}",
    ]
    log_file = log_dir / f"{rsocat_name}.log"
    with open(log_file, "a") as log:
        process_mgr.start(rsocat_name, rsocat_cmd, stdout=log, stderr=log)

    # 3. Local socat: TCP intermediate → UDP local service
    lsocat_name = _udp_process_name(conn.tag, tag, "lsocat")
    lsocat_cmd = [
        "socat",
        f"TCP4-LISTEN:{intermediate},reuseaddr,fork",
        f"UDP4-SENDTO:{fw.dst_addr}:{fw.dst_port}",
    ]
    log_file = log_dir / f"{lsocat_name}.log"
    with open(log_file, "a") as log:
        process_mgr.start(lsocat_name, lsocat_cmd, stdout=log, stderr=log)


def stop_udp_forward(
    conn_tag: str,
    fw_tag: str,
    process_mgr: ProcessManager,
) -> bool:
    """Stop all socat/SSH processes for a single UDP forward.

    Returns True if at least one process was stopped.
    """
    prefix = f"{UDP_PROCESS_PREFIX}-{conn_tag}-{fw_tag}-"
    stopped_any = False
    for name in list(process_mgr.status_all().keys()):
        if name.startswith(prefix):
            if process_mgr.stop(name):
                stopped_any = True
    return stopped_any


def stop_all_udp_forwards_for_connection(
    conn_tag: str,
    process_mgr: ProcessManager,
) -> None:
    """Stop all UDP socat processes for every forward on a connection."""
    prefix = f"{UDP_PROCESS_PREFIX}-{conn_tag}-"
    for name in list(process_mgr.status_all().keys()):
        if name.startswith(prefix):
            process_mgr.stop(name)
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_socat.py -v
```

Expected: all socat tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/susops/core/socat.py tests/test_socat.py
git commit -m "feat: add core/socat.py for UDP forwarding via socat"
```

---

## Task 3: Facade — wire UDP into lifecycle

**Files:**
- Modify: `src/susops/facade.py`
- Modify: `tests/test_facade.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_facade.py` (find an appropriate location, after existing forward tests):

```python
def test_add_local_udp_forward_persisted(tmp_path):
    """UDP forward is saved to config with correct flags."""
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host", 1080)
    fw = PortForward(src_port=53, dst_port=53, dst_addr="dns.internal", tcp=False, udp=True)
    mgr.add_local_forward("work", fw)
    config = mgr.list_config()
    saved = config.connections[0].forwards.local[0]
    assert saved.tcp is False
    assert saved.udp is True
    assert saved.dst_addr == "dns.internal"


def test_add_local_forward_both_protocols_persisted(tmp_path):
    """Forward with tcp=True and udp=True is saved with both flags."""
    mgr = SusOpsManager(workspace=tmp_path)
    mgr.add_connection("work", "user@host", 1080)
    fw = PortForward(src_port=53, dst_port=53, tcp=True, udp=True)
    mgr.add_local_forward("work", fw)
    saved = mgr.list_config().connections[0].forwards.local[0]
    assert saved.tcp is True
    assert saved.udp is True
```

- [ ] **Step 2: Run to verify they pass already** (config persistence doesn't require facade changes)

```
pytest tests/test_facade.py::test_add_local_udp_forward_persisted \
       tests/test_facade.py::test_add_local_forward_both_protocols_persisted -v
```

Expected: PASS (config serialization already handles new fields).

- [ ] **Step 3: Add imports and wire socat into facade**

At the top of `src/susops/facade.py`, add the import after the existing `from susops.core.ssh import ...` block:

```python
from susops.core.socat import (
    UDP_PROCESS_PREFIX,
    _fw_tag,
    start_udp_forward,
    stop_udp_forward,
    stop_all_udp_forwards_for_connection,
)
```

- [ ] **Step 4: Wire UDP start into `start()` method**

In `src/susops/facade.py`, locate the `start()` method. Find the two loops that call `start_forward` (around line 547–557). Replace them with:

```python
                # Start configured local/remote forwards as slaves
                for fw in conn.forwards.local:
                    try:
                        if fw.tcp:
                            start_forward(conn, fw, "local", self._process_mgr, self.workspace)
                        if fw.udp:
                            start_udp_forward(conn, fw, "local", self._process_mgr, self.workspace)
                    except Exception as exc:
                        self._log(f"[{conn.tag}] Forward {fw.src_port} failed: {exc}")

                for fw in conn.forwards.remote:
                    try:
                        if fw.tcp:
                            start_forward(conn, fw, "remote", self._process_mgr, self.workspace)
                        if fw.udp:
                            start_udp_forward(conn, fw, "remote", self._process_mgr, self.workspace)
                    except Exception as exc:
                        self._log(f"[{conn.tag}] Forward {fw.src_port} failed: {exc}")
```

- [ ] **Step 5: Wire UDP stop into `stop()` method**

In the `stop()` method, locate the `stop_tunnel(...)` call (around line 722). Replace it with:

```python
                if stop_tunnel(conn.tag, self._process_mgr, self.workspace, conn.ssh_host):
                    stop_all_udp_forwards_for_connection(conn.tag, self._process_mgr)
                    self._log(f"[{conn.tag}] Stopped")
                    self._bw_sampler.reset_totals(conn.tag)
                    self._start_times.pop(conn.tag, None)
                    self._emit("state", {"tag": conn.tag, "running": False, "pid": None})
```

- [ ] **Step 6: Wire UDP into `add_local_forward` and `add_remote_forward`**

In `add_local_forward` (around line 946), replace the try/except block that calls `start_forward` with:

```python
        conn = get_connection(self.config, conn_tag)
        if conn and is_tunnel_running(conn_tag, self._process_mgr):
            try:
                if fw.tcp:
                    start_forward(conn, fw, "local", self._process_mgr, self.workspace)
                if fw.udp:
                    start_udp_forward(conn, fw, "local", self._process_mgr, self.workspace)
                self._emit("forward", {
                    "tag": conn_tag, "fw_tag": fw.tag or f"local-{fw.src_port}",
                    "direction": "local", "running": True,
                })
            except Exception as exc:
                self._log(f"[{conn_tag}] Could not start forward: {exc}")
```

In `add_remote_forward` (around line 905), apply the same pattern with `"remote"`:

```python
        conn = get_connection(self.config, conn_tag)
        if conn and is_tunnel_running(conn_tag, self._process_mgr):
            try:
                if fw.tcp:
                    start_forward(conn, fw, "remote", self._process_mgr, self.workspace)
                if fw.udp:
                    start_udp_forward(conn, fw, "remote", self._process_mgr, self.workspace)
                self._emit("forward", {
                    "tag": conn_tag, "fw_tag": fw.tag or f"remote-{fw.src_port}",
                    "direction": "remote", "running": True,
                })
            except Exception as exc:
                self._log(f"[{conn_tag}] Could not start forward: {exc}")
```

- [ ] **Step 7: Wire UDP stop into `_remove_forward`**

In `_remove_forward` (around line 922), find the loop that calls `stop_forward`. After the `stop_forward(...)` call, add:

```python
                stop_udp_forward(conn.tag, _fw_tag(fw, direction), self._process_mgr)
```

The full updated loop body (within the `for conn in self.config.connections:` loop) looks like:

```python
        found = False
        new_conns = []
        for conn in self.config.connections:
            fwds = conn.forwards.local if direction == "local" else conn.forwards.remote
            new_fwds_list = []
            for fw in fwds:
                if fw.src_port == src_port and not found:
                    found = True
                    stop_forward(conn.tag, fw.tag or f"{direction}-{fw.src_port}", self._process_mgr)
                    stop_udp_forward(conn.tag, _fw_tag(fw, direction), self._process_mgr)
                    self._emit("forward", {
                        "tag": conn.tag,
                        "fw_tag": fw.tag or f"{direction}-{fw.src_port}",
                        "direction": direction, "running": False,
                    })
                else:
                    new_fwds_list.append(fw)
            if direction == "local":
                new_fwds = conn.forwards.model_copy(update={"local": new_fwds_list})
            else:
                new_fwds = conn.forwards.model_copy(update={"remote": new_fwds_list})
            new_conns.append(conn.model_copy(update={"forwards": new_fwds}))
        if not found:
            raise ValueError(f"{direction.capitalize()} forward on port {src_port} not found")
        self.config = self.config.model_copy(update={"connections": new_conns})
        self._save()
        self._log(f"Removed {direction} forward on port {src_port}")
```

Note: read the existing `_remove_forward` implementation carefully before applying — preserve its exact structure and only add the `stop_udp_forward` call.

- [ ] **Step 8: Run all tests**

```
pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 9: Commit**

```bash
git add src/susops/facade.py tests/test_facade.py
git commit -m "feat: wire socat UDP start/stop into facade lifecycle"
```

---

## Task 4: TUI — Protocol helper + connection editor checkboxes + Protocol column

**Files:**
- Modify: `src/susops/tui/screens/__init__.py`
- Modify: `src/susops/tui/screens/connection_editor.py`

- [ ] **Step 1: Add `proto_label` to TUI screens helpers**

In `src/susops/tui/screens/__init__.py`, add at the end:

```python
from susops.core.config import PortForward as _PortForward


def proto_label(fw: _PortForward) -> str:
    """Return display string for a forward's protocol(s): TCP, UDP, or TCP+UDP."""
    if fw.tcp and fw.udp:
        return "TCP+UDP"
    if fw.udp:
        return "UDP"
    return "TCP"
```

- [ ] **Step 2: Add Protocol checkboxes to `_AddForwardDialog`**

In `src/susops/tui/screens/connection_editor.py`, add `Checkbox` to the widget imports:

```python
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
)
```

Replace the `_AddForwardDialog.compose` method with:

```python
    def compose(self) -> ComposeResult:
        d = self._direction
        conn_options = [(tag, tag) for tag in self._connections]
        bind_options = [("localhost", "localhost"), ("172.17.0.1", "172.17.0.1"), ("0.0.0.0", "0.0.0.0")]
        with Static(classes="modal-dialog"):
            yield Label(f"[bold]Add {d.capitalize()} Forward[/bold]")
            yield Label("Connection:")
            yield Select(conn_options, allow_blank=False, id="conn")
            yield Label("Label (optional):")
            yield Input(placeholder="", id="tag")
            yield Label("Forward Local Port *:" if d == "local" else "Forward Remote Port *:")
            yield Input(placeholder="8080", id="src-port")
            yield Label("To Remote Port *:" if d == "local" else "To Local Port *:")
            yield Input(placeholder="8080", id="dst-port")
            yield Label("Local Bind:" if d == "local" else "Remote Bind:")
            yield Select(bind_options, allow_blank=False, id="src-addr")
            yield Label("Remote Bind:" if d == "local" else "Local Bind:")
            yield Select(bind_options, allow_blank=False, id="dst-addr")
            yield Label("Protocol:")
            yield Checkbox("TCP", value=True, id="proto-tcp")
            yield Checkbox("UDP", value=False, id="proto-udp")
            yield Label("", id="error", classes="modal-error")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Add", id="btn-ok", variant="success")
                yield Button("Cancel", id="btn-cancel")
```

Replace `_AddForwardDialog.on_button_pressed` with:

```python
    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        conn_val = self.query_one("#conn", Select).value
        conn = conn_val if isinstance(conn_val, str) else ""
        src_addr_val = self.query_one("#src-addr", Select).value
        src_addr = src_addr_val if isinstance(src_addr_val, str) else "localhost"
        dst_addr_val = self.query_one("#dst-addr", Select).value
        dst_addr = dst_addr_val if isinstance(dst_addr_val, str) else "localhost"
        tag = self.query_one("#tag", Input).value.strip()
        tcp = self.query_one("#proto-tcp", Checkbox).value
        udp = self.query_one("#proto-udp", Checkbox).value
        error_label = self.query_one(".modal-error", Label)
        if not tcp and not udp:
            error_label.update("Select at least one protocol (TCP or UDP).")
            return
        try:
            src = int(self.query_one("#src-port", Input).value.strip())
            dst = int(self.query_one("#dst-port", Input).value.strip())
        except ValueError:
            error_label.update("Ports must be valid numbers.")
            return
        if not validate_port(src) or not validate_port(dst):
            error_label.update("Ports must be between 1 and 65535.")
            return
        if self._direction == "local" and not is_port_free(src):
            error_label.update(f"Local port {src} is already in use.")
            return
        if self._direction == "remote" and not is_port_free(dst):
            error_label.update(f"Local port {dst} is already in use.")
            return
        self.dismiss({
            "conn": conn, "src": src, "dst": dst,
            "src_addr": src_addr, "dst_addr": dst_addr,
            "tag": tag, "dir": self._direction,
            "tcp": tcp, "udp": udp,
        })
```

- [ ] **Step 3: Pass `tcp`/`udp` to `PortForward` in `_do_add_forward`**

In `ConnectionEditorScreen._do_add_forward`, replace the `PortForward(...)` construction:

```python
    def _do_add_forward(self, direction: str) -> None:
        def _on_result(data) -> None:
            if not data:
                return
            fw = PortForward(
                src_addr=data["src_addr"],
                src_port=data["src"],
                dst_addr=data["dst_addr"],
                dst_port=data["dst"],
                tag=data["tag"],
                tcp=data["tcp"],
                udp=data["udp"],
            )
            try:
                if direction == "local":
                    self.app.manager.add_local_forward(data["conn"], fw)  # type: ignore[attr-defined]
                else:
                    self.app.manager.add_remote_forward(data["conn"], fw)  # type: ignore[attr-defined]
                self._bg_reload()
            except ValueError as e:
                self.app.notify(str(e), severity="error")

        self.app.push_screen(_AddForwardDialog(direction, self._conn_tags()), _on_result)
```

- [ ] **Step 4: Add Protocol column to DataTables**

In `ConnectionEditorScreen._setup_tables`, replace the local/remote forward column definitions:

```python
        tbl = self.query_one("#tbl-local", DataTable)
        tbl.add_columns("Connection", "Local Port", "Local Bind", "Remote Port", "Remote Bind", "Protocol", "Label")

        tbl = self.query_one("#tbl-remote", DataTable)
        tbl.add_columns("Connection", "Remote Port", "Remote Bind", "Local Port", "Local Bind", "Protocol", "Label")
```

In `_reload`, replace the local forward table row population:

```python
        tbl = self.query_one("#tbl-local", DataTable)
        cur = tbl.cursor_row
        tbl.clear()
        for conn in config.connections:
            for fw in conn.forwards.local:
                tbl.add_row(
                    conn.tag, str(fw.src_port), fw.src_addr,
                    str(fw.dst_port), fw.dst_addr, proto_label(fw), fw.tag or "",
                    key=f"{conn.tag}:L:{fw.src_port}",
                )
        if tbl.row_count:
            tbl.move_cursor(row=min(cur, tbl.row_count - 1))
```

And the remote forward table row population:

```python
        tbl = self.query_one("#tbl-remote", DataTable)
        cur = tbl.cursor_row
        tbl.clear()
        for conn in config.connections:
            for fw in conn.forwards.remote:
                tbl.add_row(
                    conn.tag, str(fw.src_port), fw.src_addr,
                    str(fw.dst_port), fw.dst_addr, proto_label(fw), fw.tag or "",
                    key=f"{conn.tag}:R:{fw.src_port}",
                )
        if tbl.row_count:
            tbl.move_cursor(row=min(cur, tbl.row_count - 1))
```

Add the import at the top of `connection_editor.py`:

```python
from susops.tui.screens import compose_footer, proto_label
```

- [ ] **Step 5: Run tests**

```
pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/susops/tui/screens/__init__.py src/susops/tui/screens/connection_editor.py
git commit -m "feat: add Protocol checkboxes and column to connection editor TUI"
```

---

## Task 5: TUI dashboard — Protocol in forwards display

**Files:**
- Modify: `src/susops/tui/screens/dashboard.py`

- [ ] **Step 1: Update `_fmt_forward_local` and `_fmt_forward_remote`**

In `src/susops/tui/screens/dashboard.py`, add the import at the top:

```python
from susops.tui.screens import open_in_explorer, open_path, proto_label, share_name_markup
```

Replace `_fmt_forward_local`:

```python
def _fmt_forward_local(fw, prefix: str = "") -> str:
    pre = f"[dim]{prefix}[/dim] " if prefix else ""
    label = f"\n  [dim]{fw.tag}[/dim]" if fw.tag else ""
    proto = f" [dim]{proto_label(fw)}[/dim]"
    return f"{pre}[green]L[/green] {fw.src_addr}:{fw.src_port} [green]→[/green] {fw.dst_addr}:{fw.dst_port}{proto}{label}"
```

Replace `_fmt_forward_remote`:

```python
def _fmt_forward_remote(fw, prefix: str = "") -> str:
    pre = f"[dim]{prefix}[/dim] " if prefix else ""
    label = f"\n  [dim]{fw.tag}[/dim]" if fw.tag else ""
    proto = f" [dim]{proto_label(fw)}[/dim]"
    return f"{pre}[yellow]R[/yellow] {fw.src_addr}:{fw.src_port} [yellow]←[/yellow] {fw.dst_addr}:{fw.dst_port}{proto}{label}"
```

- [ ] **Step 2: Run tests**

```
pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add src/susops/tui/screens/dashboard.py
git commit -m "feat: show Protocol in dashboard forwards display"
```

---

## Task 6: Tray Linux — TCP/UDP checkboxes in add-forward dialogs

**Files:**
- Modify: `src/susops/tray/linux.py`

- [ ] **Step 1: Add protocol checkboxes to `_show_add_local_dialog`**

In `src/susops/tray/linux.py`, locate `_show_add_local_dialog`. After the existing grid construction (after `dst_addr_combo`), add two `CheckButton` widgets and include them in the grid.

Find the `_labeled_grid` call and replace it with:

```python
        tcp_check = Gtk.CheckButton(label="TCP (SSH -L forward)")
        tcp_check.set_active(True)
        udp_check = Gtk.CheckButton(label="UDP (socat relay)")
        udp_check.set_active(False)

        grid, _ = _labeled_grid(Gtk, [
            ("conn", "Connection *:", conn_combo),
            ("tag", "Tag (optional):", tag_entry),
            ("src", "Forward Local Port *:", src_port_entry),
            ("dst", "To Remote Port *:", dst_port_entry),
            ("src_addr", "Local Bind (optional):", src_addr_combo),
            ("dst_addr", "Remote Bind (optional):", dst_addr_combo),
            ("tcp", "Protocol:", tcp_check),
            ("udp", "", udp_check),
        ])
```

Inside the `while True:` validation loop, after reading `dst_addr`, add:

```python
            tcp = tcp_check.get_active()
            udp = udp_check.get_active()
            if not tcp and not udp:
                _alert(Gtk, dlg, "Protocol Required", "Select at least one protocol (TCP or UDP).", Gtk.MessageType.ERROR)
                continue
```

Replace the `PortForward(...)` construction:

```python
            fw = PortForward(src_addr=src_addr, src_port=int(src), dst_addr=dst_addr, dst_port=int(dst), tag=tag or None, tcp=tcp, udp=udp)
```

- [ ] **Step 2: Add protocol checkboxes to `_show_add_remote_dialog`**

Apply identical changes to `_show_add_remote_dialog`. After reading `dst_addr` in the validation loop:

```python
        tcp_check = Gtk.CheckButton(label="TCP (SSH -R forward)")
        tcp_check.set_active(True)
        udp_check = Gtk.CheckButton(label="UDP (socat relay)")
        udp_check.set_active(False)

        grid, _ = _labeled_grid(Gtk, [
            ("conn", "Connection *:", conn_combo),
            ("tag", "Tag (optional):", tag_entry),
            ("rport", "Forward Remote Port *:", remote_port_entry),
            ("lport", "To Local Port *:", local_port_entry),
            ("src_addr", "Remote Bind (optional):", src_addr_combo),
            ("dst_addr", "Local Bind (optional):", dst_addr_combo),
            ("tcp", "Protocol:", tcp_check),
            ("udp", "", udp_check),
        ])
```

Inside the `while True:` loop, after reading `dst_addr`:

```python
            tcp = tcp_check.get_active()
            udp = udp_check.get_active()
            if not tcp and not udp:
                _alert(Gtk, dlg, "Protocol Required", "Select at least one protocol (TCP or UDP).", Gtk.MessageType.ERROR)
                continue
```

Replace the `PortForward(...)` construction:

```python
            fw = PortForward(src_addr=src_addr, src_port=int(rport), dst_addr=dst_addr, dst_port=int(lport), tag=tag or None, tcp=tcp, udp=udp)
```

- [ ] **Step 3: Run tests**

```
pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/susops/tray/linux.py
git commit -m "feat: add TCP/UDP protocol checkboxes to Linux tray add-forward dialogs"
```

---

## Task 7: Tray macOS — TCP/UDP prompts in `_prompt_add_forward`

**Files:**
- Modify: `src/susops/tray/mac.py`

- [ ] **Step 1: Add protocol prompts to `_prompt_add_forward`**

In `src/susops/tray/mac.py`, locate `_prompt_add_forward`. After the `r_dst_bind` result is read (before the `try: src_int = int(src)` block), add two new `rumps.Window` prompts:

```python
        win_tcp = rumps.Window(
            message="Enable TCP forwarding? (yes/no)",
            title=title,
            default_text="yes",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r_tcp = win_tcp.run()
        if r_tcp.clicked == 0:
            return
        tcp = r_tcp.text.strip().lower() in ("yes", "y", "true", "1")

        win_udp = rumps.Window(
            message="Enable UDP forwarding? (yes/no)",
            title=title,
            default_text="no",
            ok="Add",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r_udp = win_udp.run()
        if r_udp.clicked == 0:
            return
        udp = r_udp.text.strip().lower() in ("yes", "y", "true", "1")

        if not tcp and not udp:
            self.show_alert("Protocol Required", "At least one protocol (TCP or UDP) must be selected.")
            return
```

Replace the `PortForward(...)` construction (currently line ~743):

```python
        fw = PortForward(src_addr=src_addr, src_port=src_int, dst_addr=dst_addr, dst_port=dst_int, tcp=tcp, udp=udp)
```

- [ ] **Step 2: Run tests**

```
pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add src/susops/tray/mac.py
git commit -m "feat: add TCP/UDP prompts to macOS tray add-forward flow"
```

---

## Task 8: Packaging — document socat system dependency

**Files:**
- Modify: `packaging/homebrew/Formula/susops.rb`
- Modify: `packaging/aur/PKGBUILD`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `depends_on "socat"` to Homebrew formula**

In `packaging/homebrew/Formula/susops.rb`, add after `depends_on "autossh"`:

```ruby
  depends_on "socat"
```

- [ ] **Step 2: Add `socat` to AUR optdepends**

In `packaging/aur/PKGBUILD`, add to the `optdepends` array:

```bash
optdepends=(
    'python-textual: interactive TUI interface'
    'python-cryptography: encrypted file sharing'
    'python-aiohttp: file sharing and SSE status server'
    'socat: UDP port forwarding support'
)
```

- [ ] **Step 3: Add udp comment block to `pyproject.toml`**

In `pyproject.toml`, add after the `tray-linux = []` section:

```toml
udp = []
# socat must be installed via system package manager for UDP port forwarding:
#   macOS:  brew install socat
#   Arch:   sudo pacman -S socat
#   Ubuntu: sudo apt install socat
```

- [ ] **Step 4: Run tests**

```
pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add packaging/homebrew/Formula/susops.rb packaging/aur/PKGBUILD pyproject.toml
git commit -m "chore: document socat as system dependency for UDP forwarding"
```

---

## Self-Review

### Spec coverage

| Spec requirement | Covered by task |
|---|---|
| `tcp: bool = True`, `udp: bool = False` on `PortForward` | Task 1 |
| Backward compat (old YAML defaults correctly) | Task 1 test |
| `model_validator` rejects `tcp=False, udp=False` | Task 1 |
| `core/socat.py` module with `start_udp_forward` / `stop_udp_forward` | Task 2 |
| Local UDP: socat EXEC (1 process, no SSH -L) | Task 2 |
| Remote UDP: SSH -R + rsocat + lsocat (3 processes) | Task 2 |
| `stop_all_udp_forwards_for_connection` | Task 2 |
| Facade `start()` starts UDP forwards | Task 3 |
| Facade `stop()` stops UDP forwards | Task 3 |
| Facade `add_local/remote_forward` starts UDP if tunnel running | Task 3 |
| Facade `_remove_forward` stops UDP | Task 3 |
| `proto_label()` helper in `tui/screens/__init__.py` | Task 4 |
| TUI connection editor: TCP/UDP checkboxes in `_AddForwardDialog` | Task 4 |
| TUI connection editor: Protocol column in local/remote tables | Task 4 |
| Dashboard: Protocol shown in forwards display | Task 5 |
| Linux tray: TCP/UDP checkboxes in add-forward dialogs | Task 6 |
| macOS tray: TCP/UDP prompts in `_prompt_add_forward` | Task 7 |
| Homebrew: `depends_on "socat"` | Task 8 |
| AUR: `socat` in `optdepends` | Task 8 |
| `pyproject.toml`: udp comment with install instructions | Task 8 |
| Runtime error: socat not found locally | Handled in Task 2 (FileNotFoundError surfaces from `process_mgr.start`) |
| Runtime error: remote shell blocked | Handled in Task 2 (rsocat/ssh process exits immediately, logged) |

### Type consistency check

- `_fw_tag(fw, direction)` defined in Task 2, used in Task 3 (`_fw_tag` import) ✓
- `proto_label(fw)` defined in Task 4 Step 1, used in Tasks 4 and 5 ✓
- `start_udp_forward(conn, fw, direction, process_mgr, workspace)` — signature consistent across Tasks 2 and 3 ✓
- `stop_udp_forward(conn_tag, fw_tag, process_mgr)` — consistent ✓
- `stop_all_udp_forwards_for_connection(conn_tag, process_mgr)` — consistent ✓
- `PortForward.tcp`, `PortForward.udp` — defined Task 1, used Tasks 2–7 ✓
