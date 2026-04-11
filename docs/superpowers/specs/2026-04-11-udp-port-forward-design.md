# UDP Port Forward Support — Design Spec

**Date:** 2026-04-11  
**Branch:** feature/udp  
**Status:** Approved

---

## Overview

Add UDP port forwarding support to SusOps via `socat`. Each `PortForward` entry gains two boolean flags (`tcp`, `udp`) allowing TCP-only, UDP-only, or both simultaneously. The implementation uses socat to bridge UDP↔TCP over the existing SSH ControlMaster tunnel.

---

## Config Model Change

```python
class PortForward(BaseModel):
    tag: str = ""
    src_addr: str = "localhost"
    src_port: int
    dst_addr: str = "localhost"
    dst_port: int
    tcp: bool = True   # start SSH -L/-R slave (existing behaviour)
    udp: bool = False  # start socat chain
```

- **Backward compatible**: existing entries default to `tcp=True, udp=False` — no migration needed.
- **Validation**: at least one of `tcp`/`udp` must be `True` (enforced via `model_validator`).

---

## socat Architecture

### Local UDP Forward (client → remote service)

Uses socat's `EXEC:` address type to pipe UDP directly through the SSH ControlMaster — **no SSH port forward slave needed**.

```
Local UDP client
    ↓  UDP :src_port
[local socat]  udp4-recvfrom:src_port,fork → EXEC:'ssh -S <sock> host socat - udp4-sendto:dst_addr:dst_port'
    ↓  SSH ControlMaster (multiplexed, one session per UDP conversation)
Remote socat (stdio ↔ UDP)
    ↓  UDP
Remote UDP service
```

**Process:** one local socat process (`susops-udp-<conn>-<fw_tag>-lsocat`).

**Use cases:** DNS, SNMP, NTP, VoIP/SIP, RADIUS — any UDP service on the remote network.

### Remote UDP Forward (remote client → local service)

Requires an SSH `-R` slave (reverse port forward) as a TCP bridge, plus socat on both ends.

```
Remote UDP client
    ↓  UDP :src_port (on remote host)
[remote socat via SSH]  udp4-recvfrom:src_port,fork → tcp:localhost:intermediate
    ↓  TCP :intermediate (remote side)
[SSH -R slave]  -R intermediate:localhost:intermediate
    ↓  TCP :intermediate (local side)
[local socat]  tcp4-listen:intermediate,fork → udp4-sendto:dst_addr:dst_port
    ↓  UDP
Local UDP service
```

**Processes:**
- `susops-udp-<conn>-<fw_tag>-ssh` — SSH -R slave (intermediate TCP)
- `susops-udp-<conn>-<fw_tag>-rsocat` — remote socat (via SSH, tracked locally by its SSH session PID)
- `susops-udp-<conn>-<fw_tag>-lsocat` — local socat

**Use cases:** WireGuard endpoint, local Pi-hole DNS, local game server, syslog collector, SNMP trap receiver.

---

## New Module: `core/socat.py`

```
UDP_PROCESS_PREFIX = "susops-udp"

start_udp_forward(conn, fw, direction, process_mgr, workspace) → None
stop_udp_forward(conn_tag, fw_tag, process_mgr) → bool
```

Internally:
- `_start_local_udp_forward(conn, fw, sock, process_mgr, workspace)` — EXEC approach, 1 process
- `_start_remote_udp_forward(conn, fw, sock, process_mgr, workspace)` — SSH -R + 2 socat, 3 processes
- `_stop_all_udp_processes(conn_tag, fw_tag, process_mgr)` — kills all `susops-udp-<conn>-<fw_tag>-*`

---

## Facade Changes

- `start_tunnel`: for each forward with `udp=True`, also call `start_udp_forward`
- `stop_tunnel`: stop all `susops-udp-<conn>-*` processes
- `add_local_forward` / `add_remote_forward`: when `fw.udp=True` and tunnel running, also call `start_udp_forward`
- `_add_forward`: validates `tcp or udp` is True
- `remove_local_forward` / `remove_remote_forward`: also stop udp processes for that forward

---

## TUI Changes

### `_AddForwardDialog` (connection_editor.py)
- Add `Checkbox("TCP", value=True, id="proto-tcp")` and `Checkbox("UDP", value=False, id="proto-udp")`
- Validation: at least one must be checked
- Dismissed data gains `tcp: bool` and `udp: bool` fields

### DataTable columns
- Local Forwards table: add **Protocol** column (values: `TCP`, `UDP`, `TCP+UDP`)
- Remote Forwards table: same

### Dashboard Forwards tab
- Add **Protocol** column to the forwards DataTable

---

## Tray Changes

### Linux (`linux.py`)
- Add two `Gtk.CheckButton` widgets in the add-forward dialog: "TCP" (default on) and "UDP" (default off)
- Validate at least one is checked before dismissing

### macOS (`mac.py`)
- After existing port prompts: `rumps.Window` yes/no for "Enable TCP forwarding?" then "Enable UDP forwarding?"
- Validate at least one is True

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| `socat` not found locally | `FileNotFoundError` → log `"socat not found locally — install socat to use UDP forwards"` |
| Remote shell commands blocked (ForceCommand) | Remote socat SSH process exits immediately with non-zero code → log `"Remote does not allow command execution — UDP forwarding requires a shell-enabled SSH account"` |
| `socat` not found remotely | Remote socat SSH process exits with "command not found" → log `"socat not found on remote host — install socat on the SSH server"` |
| `tcp=False, udp=False` | `ValueError` at model validation: `"At least one of tcp/udp must be True"` |
| Intermediate port taken (remote UDP only) | Port allocation retries via `get_random_free_port()` |

---

## Packaging

### Homebrew (`packaging/homebrew/Formula/susops.rb`)
Add `depends_on "socat"` — Homebrew manages system binaries.

### AUR (`packaging/aur/PKGBUILD`)
Add `socat` to `optdepends` with description `"socat: UDP port forwarding support"`.

### `pyproject.toml`
Add comment in `[project.optional-dependencies]`:
```toml
udp = []
# socat must be installed via system package manager for UDP forwarding:
#   macOS: brew install socat
#   Arch: sudo pacman -S socat
#   Ubuntu: sudo apt install socat
```

---

## Tests

- `tests/test_socat.py`: unit tests for command builders (local/remote, various `fw` configs)
- `tests/test_facade.py`: extend existing forward tests with `tcp=False, udp=True` cases
- `tests/test_config.py`: validate `tcp=False, udp=False` raises, backward-compat defaults
