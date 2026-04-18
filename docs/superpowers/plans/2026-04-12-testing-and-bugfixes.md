# Testing & Bug Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add missing `start_udp_forward` command-construction tests to `test_socat.py` and fix the remote UDP three-process startup race condition in `socat.py`.

**Architecture:** The test gap is that `test_socat.py` covers helper functions but not the socat/SSH command strings built by `_start_local_udp` and `_start_remote_udp`. The race condition is that `rsocat` (remote socat) starts before the SSH -R slave has finished binding the intermediate TCP port on the remote host; fixing it requires reordering the three processes and wrapping the rsocat command in a retry loop.

**Tech Stack:** pytest, unittest.mock, Python subprocess (no real processes spawned in tests)

---

## File Map

- Modify: `tests/test_socat.py` — add tests for `start_udp_forward` command construction
- Modify: `src/susops/core/socat.py:100–153` — reorder `_start_remote_udp` and add retry to rsocat command

---

### Task 1: Test `_start_local_udp` command construction

The EXEC argument in the local UDP socat command must wrap the SSH sub-command in single quotes. Without them socat errors "wrong number of parameters". This was a real bug caught during manual testing; the test locks it in as a regression guard.

**Files:**
- Modify: `tests/test_socat.py`

- [ ] **Step 1: Add `start_udp_forward` to the existing import block**

Open `tests/test_socat.py`. The current import block is:

```python
from susops.core.socat import (
    UDP_PROCESS_PREFIX,
    _fw_tag,
    _udp_process_name,
    stop_udp_forward,
    stop_all_udp_forwards_for_connection,
)
```

Add `start_udp_forward`:

```python
from susops.core.socat import (
    UDP_PROCESS_PREFIX,
    _fw_tag,
    _udp_process_name,
    start_udp_forward,
    stop_udp_forward,
    stop_all_udp_forwards_for_connection,
)
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_socat.py`:

```python
# ------------------------------------------------------------------ #
# start_udp_forward — local direction command construction
# ------------------------------------------------------------------ #

def test_start_local_udp_process_name(conn, fw_local, tmp_path):
    pm = MagicMock()
    start_udp_forward(conn, fw_local, "local", pm, tmp_path)
    pm.start.assert_called_once()
    name = pm.start.call_args[0][0]
    assert name == "susops-udp-work-local-53-lsocat"


def test_start_local_udp_exec_single_quoted(conn, fw_local, tmp_path):
    """EXEC argument must single-quote the SSH sub-command.

    socat splits EXEC on spaces; without single quotes it sees 'ssh', '-o',
    '...' as separate arguments and errors 'wrong number of parameters (3 instead of 1)'.
    """
    pm = MagicMock()
    start_udp_forward(conn, fw_local, "local", pm, tmp_path)
    cmd = pm.start.call_args[0][1]
    exec_arg = next(a for a in cmd if a.startswith("EXEC:"))
    assert exec_arg.startswith("EXEC:'ssh "), f"EXEC not single-quoted: {exec_arg!r}"
    assert exec_arg.endswith("'"), f"EXEC not closed with single quote: {exec_arg!r}"


def test_start_local_udp_destination_in_exec(conn, fw_local, tmp_path):
    pm = MagicMock()
    start_udp_forward(conn, fw_local, "local", pm, tmp_path)
    cmd = pm.start.call_args[0][1]
    exec_arg = next(a for a in cmd if a.startswith("EXEC:"))
    assert f"UDP4-SENDTO:{fw_local.dst_addr}:{fw_local.dst_port}" in exec_arg


def test_start_local_udp_listens_on_src_port(conn, fw_local, tmp_path):
    pm = MagicMock()
    start_udp_forward(conn, fw_local, "local", pm, tmp_path)
    cmd = pm.start.call_args[0][1]
    recvfrom_arg = next(a for a in cmd if "UDP4-RECVFROM" in a)
    assert f"UDP4-RECVFROM:{fw_local.src_port}" in recvfrom_arg
    assert "fork" in recvfrom_arg
```

- [ ] **Step 3: Run tests to verify they pass**

```bash
pytest tests/test_socat.py::test_start_local_udp_process_name \
       tests/test_socat.py::test_start_local_udp_exec_single_quoted \
       tests/test_socat.py::test_start_local_udp_destination_in_exec \
       tests/test_socat.py::test_start_local_udp_listens_on_src_port -v
```

Expected: 4 x PASSED (the implementation already has EXEC quoting correct)

- [ ] **Step 4: Run full suite**

```bash
pytest -x
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_socat.py
git commit -m "test: add start_local_udp command construction and EXEC quoting tests"
```

---

### Task 2: Test `_start_remote_udp` — process count and ordering

Write tests that assert process count, names, and startup order. The ordering tests will initially **fail** because the current code starts ssh first, then rsocat, then lsocat. The fix comes in Task 3.

**Files:**
- Modify: `tests/test_socat.py`

- [ ] **Step 1: Append remote UDP tests**

```python
# ------------------------------------------------------------------ #
# start_udp_forward — remote direction command construction
# ------------------------------------------------------------------ #

def test_start_remote_udp_spawns_three_processes(conn, fw_remote, tmp_path):
    pm = MagicMock()
    start_udp_forward(conn, fw_remote, "remote", pm, tmp_path)
    assert pm.start.call_count == 3


def test_start_remote_udp_process_names(conn, fw_remote, tmp_path):
    pm = MagicMock()
    start_udp_forward(conn, fw_remote, "remote", pm, tmp_path)
    names = [c[0][0] for c in pm.start.call_args_list]
    assert "susops-udp-work-remote-51820-ssh" in names
    assert "susops-udp-work-remote-51820-rsocat" in names
    assert "susops-udp-work-remote-51820-lsocat" in names


def test_start_remote_udp_lsocat_before_rsocat(conn, fw_remote, tmp_path):
    """lsocat (local TCP listener) must start before rsocat (remote socat).

    rsocat connects to the intermediate TCP port via the SSH -R tunnel.
    Starting lsocat first ensures the local end is ready before the remote
    end tries to connect.
    """
    pm = MagicMock()
    start_udp_forward(conn, fw_remote, "remote", pm, tmp_path)
    names = [c[0][0] for c in pm.start.call_args_list]
    lsocat_idx = names.index("susops-udp-work-remote-51820-lsocat")
    rsocat_idx = names.index("susops-udp-work-remote-51820-rsocat")
    assert lsocat_idx < rsocat_idx, f"lsocat must start before rsocat, got order: {names}"


def test_start_remote_udp_rsocat_has_retry(conn, fw_remote, tmp_path):
    """rsocat command must include a shell retry loop.

    The SSH -R slave may not finish binding the remote intermediate port
    before rsocat executes. A retry loop with sleep handles this gracefully.
    """
    pm = MagicMock()
    start_udp_forward(conn, fw_remote, "remote", pm, tmp_path)
    rsocat_call = next(c for c in pm.start.call_args_list if c[0][0].endswith("-rsocat"))
    remote_cmd = rsocat_call[0][1][-1]
    assert "sleep" in remote_cmd, f"rsocat must retry with sleep, got: {remote_cmd!r}"
    assert f"UDP4-RECVFROM:{fw_remote.src_port},reuseaddr,fork" in remote_cmd
```

- [ ] **Step 2: Run ordering tests to confirm they fail**

```bash
pytest tests/test_socat.py::test_start_remote_udp_lsocat_before_rsocat \
       tests/test_socat.py::test_start_remote_udp_rsocat_has_retry -v
```

Expected: both FAILED

- [ ] **Step 3: Do not fix yet — proceed to Task 3**

---

### Task 3: Fix `_start_remote_udp` — reorder and add retry

**Files:**
- Modify: `src/susops/core/socat.py:100–153`

- [ ] **Step 1: Replace `_start_remote_udp` entirely**

The current function starts ssh → rsocat → lsocat. Replace it with lsocat → ssh → rsocat (with retry):

```python
def _start_remote_udp(
    conn: Connection,
    fw: PortForward,
    sock: Path,
    tag: str,
    process_mgr: ProcessManager,
    log_dir: Path,
) -> None:
    """Remote UDP forward: local socat + SSH -R slave + remote socat (via SSH).

    Startup order matters:
      1. lsocat  — bind local:intermediate so it is ready when rsocat connects.
      2. SSH -R  — request remote SSH server to bind remote:intermediate.
      3. rsocat  — connect to remote:intermediate (with retry for binding lag).

    The rsocat shell command retries up to 5 times (1 s apart) because the
    SSH server may not finish binding the remote port before rsocat executes.
    """
    intermediate = get_random_free_port()

    # 1. Local socat: TCP intermediate → UDP local service (start first)
    lsocat_name = _udp_process_name(conn.tag, tag, "lsocat")
    lsocat_cmd = [
        "socat",
        f"TCP4-LISTEN:{intermediate},reuseaddr,fork",
        f"UDP4-SENDTO:{fw.dst_addr}:{fw.dst_port}",
    ]
    log_file = log_dir / f"{lsocat_name}.log"
    with open(log_file, "a") as log:
        process_mgr.start(lsocat_name, lsocat_cmd, stdout=log, stderr=log)

    # 2. SSH -R slave: bind intermediate port on remote, forward to local
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

    # 3. Remote socat: UDP → TCP intermediate (retry loop for port binding lag)
    rsocat_name = _udp_process_name(conn.tag, tag, "rsocat")
    rsocat_shell = (
        f"for _i in 1 2 3 4 5; do "
        f"socat -T15 UDP4-RECVFROM:{fw.src_port},reuseaddr,fork "
        f"TCP4:localhost:{intermediate} && break; sleep 1; done"
    )
    rsocat_cmd = [
        "ssh", "-T",
        "-o", f"ControlPath={sock}",
        conn.ssh_host,
        rsocat_shell,
    ]
    log_file = log_dir / f"{rsocat_name}.log"
    with open(log_file, "a") as log:
        process_mgr.start(rsocat_name, rsocat_cmd, stdout=log, stderr=log)
```

- [ ] **Step 2: Run all socat tests**

```bash
pytest tests/test_socat.py -v
```

Expected: all PASSED

- [ ] **Step 3: Run full suite**

```bash
pytest -x
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/susops/core/socat.py tests/test_socat.py
git commit -m "fix: reorder remote UDP processes and add rsocat retry loop

lsocat now starts before ssh-slave and rsocat to ensure the local TCP
listener is ready before the remote end connects. rsocat command wraps
socat in a 5-attempt retry with 1 s sleep to handle SSH -R port-binding
lag on high-latency links. Adds command-construction tests for both
local and remote start_udp_forward paths."
```
