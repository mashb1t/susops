# SusOps

SSH SOCKS5 proxy manager with PAC server, Textual TUI, and system tray apps.

## Overview

SusOps manages SSH SOCKS5 proxy tunnels and serves a PAC (Proxy Auto-Config) file so browsers and other tools route traffic through your tunnels automatically. It replaces a 1600-line Bash CLI with a modern Python stack:

- **Textual TUI** — interactive dashboard, live bandwidth graphs, CRUD editor, log viewer
- **Non-interactive CLI** — scriptable `susops` command with semantic exit codes
- **Linux tray app** — GTK3 + AyatanaAppIndicator3
- **macOS tray app** — rumps + PyObjC
- **Shared Python core** — all business logic in `susops.core`, used by every frontend

### Architecture

```
susops/
  src/susops/
    core/          # Business logic (no UI)
      config.py    # Pydantic v2 models + ruamel.yaml I/O
      ssh.py       # autossh/ssh subprocess + PID tracking
      pac.py       # PAC generation + Python HTTP server
      share.py     # AES-256-CTR encrypted file sharing
      process.py   # ProcessManager: PID files, start/stop/status
      ports.py     # Free port allocation, CIDR helpers
      types.py     # Enums and result dataclasses
    facade.py      # SusOpsManager — single API for all frontends
    tui/           # Textual TUI + argparse CLI
    tray/          # GTK3 (Linux) and rumps (macOS) tray apps
```

---

## Requirements

| Component | Requirement |
|-----------|-------------|
| Python | ≥ 3.11 |
| SSH tunnels | `autossh` (recommended) or `ssh` |
| PAC server | built-in (`http.server`) |
| TUI | `textual >= 0.80` (optional extra) |
| File sharing | `cryptography >= 42` (optional extra) |
| Linux tray | `python-gobject`, `gtk3`, `libayatana-appindicator` (system packages) |
| macOS tray | `rumps >= 0.4` |

---

## Installation

### pip

```bash
# CLI only (no TUI, no tray)
pip install susops

# TUI
pip install "susops[tui]"

# TUI + encrypted file sharing
pip install "susops[tui,crypto]"

# Linux tray (system GTK3 packages must be installed separately)
pip install "susops[tui,tray-linux,crypto]"

# macOS tray
pip install "susops[tui,tray-mac,crypto]"
```

### Arch Linux (AUR)

```bash
yay -S susops
# Optional TUI: yay -S python-textual
# Optional file sharing: yay -S python-cryptography
```

### macOS (Homebrew)

```bash
brew install mashb1t/susops/susops
```

### From source

```bash
git clone https://github.com/mashb1t/susops
cd susops
pip install -e ".[tui,crypto,dev]"
```

---

## Quick Start

```bash
# Add your first SSH connection
susops add-connection work user@bastion.example.com

# Add hosts that should route through the proxy
susops add *.internal.example.com
susops add 10.0.0.0/8

# Start tunnels + PAC server
susops start

# Check status
susops ps

# Point your browser at the PAC URL
susops ps   # shows PAC port, e.g. http://localhost:51234/susops.pac
```

---

## TUI

Run `susops` (or `so`) with no arguments in a terminal to launch the interactive TUI:

```
susops
```

### Keybindings

| Key | Action |
|-----|--------|
| `d` | Dashboard |
| `c` | Connection editor |
| `l` | Log viewer |
| `f` | File share wizard |
| `e` | Config editor (YAML) |
| `s` | Start all tunnels |
| `x` | Stop all tunnels |
| `r` | Restart all tunnels |
| `Ctrl+P` | Command palette |
| `q` | Quit |

### Screens

**Dashboard** — live status for every connection (colored dot, SOCKS port, PID) plus ↓/↑ bandwidth sparklines updated every 3 seconds. PAC server and active file share status shown below.

**Connection editor** — tabbed view of Connections, PAC Hosts, Local Forwards, and Remote Forwards. Press `a` to add, `d` to delete the selected row.

**Log viewer** — scrollable real-time log buffer with per-connection filter and auto-scroll toggle (`a`). Cleared with `c`.

**Share wizard** — share an encrypted file (enter path, optional password, optional port) or fetch a shared file (enter port + password).

**Config editor** — read-only YAML view of `~/.susops/config.yaml`. Press `e` to open in `$EDITOR`.

---

## CLI Reference

When a subcommand is given (or stdout is not a TTY), `susops` runs in non-interactive mode:

```
susops [-c TAG] COMMAND [args]

  -c, --connection TAG   Target a specific connection by tag
```

### Commands

#### Connection management

```bash
susops add-connection <tag> <user@host> [socks_port]
# Add a new SSH connection. Port 0 = auto-assign on start.

susops rm-connection <tag>
# Remove a connection (stops it first if running).
```

#### Lifecycle

```bash
susops start [-c TAG]     # Start tunnel(s) + PAC server
susops stop [--keep-ports] [--force]
susops restart [-c TAG]
```

`--keep-ports` preserves assigned port numbers across restarts.
`--force` sends SIGKILL instead of SIGTERM.

#### Status

```bash
susops ps    # Show running state; exit 0=all running, 2=partial, 3=stopped
susops ls    # List full config (connections, PAC hosts, forwards)
```

#### PAC hosts and port forwards

```bash
# PAC host (routes matching traffic through SOCKS proxy)
susops add <host>              # e.g. *.example.com, 10.0.0.0/8, host.example.com
susops rm  <host>

# Local port forward (-L equivalent)
susops add -l <local_port> <remote_port> [label] [-c tag]
susops rm  -l <local_port>

# Remote port forward (-R equivalent)
susops add -r <remote_port> <local_port> [label] [-c tag]
susops rm  -r <remote_port>
```

#### Testing

```bash
susops test <hostname>     # Test one host through SOCKS proxy
susops test --all          # Test all PAC hosts; exit 0 if all pass
```

#### File sharing

```bash
# Share a file (AES-256-CTR encrypted over HTTP)
susops share <file> [password] [port]
# Prints URL + password + fetch command

# Fetch a shared file
susops fetch <port> <password> [outfile]
# Saves to ~/Downloads/<original_filename> if outfile omitted
```

#### Browser launch

```bash
susops chrome     # Launch Chrome/Chromium with --proxy-pac-url
susops firefox    # Launch Firefox with a temporary PAC profile
```

#### Reset

```bash
susops reset [--force]    # Kill all processes, wipe ~/.susops workspace
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success / all running |
| `1` | Error |
| `2` | Partial (some services stopped) |
| `3` | All stopped |

---

## System Tray

### Linux

```bash
susops-tray
```

Requires (system packages):
- Arch: `sudo pacman -S python-gobject gtk3 libayatana-appindicator`
- Ubuntu/Debian: `sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatana-appindicator3-0.1`

### macOS

```bash
susops-tray
```

Requires `rumps`: `pip install "susops[tray-mac]"`

The tray icon reflects the current state (running/partial/stopped). The menu provides Start, Stop, Restart, Test connections, Show status, browser launch, and Quit. State is polled every 5 seconds.

---

## Configuration

Config is stored at `~/.susops/config.yaml`. It is created automatically on first use.

```yaml
pac_server_port: 51234
connections:
  - tag: work
    ssh_host: user@bastion.example.com
    socks_proxy_port: 51235
    pac_hosts:
      - "*.internal.example.com"
      - "10.0.0.0/8"
    forwards:
      local:
        - src_port: 5432
          dst_port: 5432
          src_addr: localhost
          dst_addr: db.internal.example.com
          tag: postgres
      remote: []
susops_app:
  stop_on_quit: true
  ephemeral_ports: false
  logo_style: COLORED_GLASSES
```

### PAC host syntax

| Pattern | Matches |
|---------|---------|
| `*.example.com` | any subdomain of example.com |
| `10.0.0.0/8` | any IP in 10.0.0.0/8 CIDR |
| `host.example.com` | that exact hostname |

### Port assignment

Ports default to `0` (auto-assign). SusOps picks a random free port from the ephemeral range (49152–65535) at start time and saves it back to `config.yaml`. Pass a specific port to `add-connection` or set it in the config to pin it.

### Workspace

All runtime data lives in `~/.susops/`:

```
~/.susops/
  config.yaml       # persistent config
  susops.pac        # generated PAC file (regenerated on start/change)
  pids/             # PID files for each managed process
    susops-ssh-<tag>.pid
    susops-pac.pid
  firefox_profile/  # temporary Firefox profile for PAC launch
```

---

## File Sharing

SusOps can share a single file over an encrypted HTTP server, useful for transferring files through an SSH tunnel:

```bash
# On sender
susops share /path/to/secret.tar.gz
# Output:
#   Sharing: /path/to/secret.tar.gz
#   URL:      http://localhost:52100
#   Password: Xk7mN2qR...
#   Port:     52100
#   Fetch with: susops fetch 52100 Xk7mN2qR...

# On receiver (through the SOCKS tunnel or on the same LAN)
susops fetch 52100 Xk7mN2qR...
# Downloaded to: ~/Downloads/secret.tar.gz
```

**Protocol:** HTTP Basic auth (`:password`) + gzip compression + AES-256-CTR encryption (PBKDF2-HMAC-SHA256 key derivation, 600,000 iterations). The original filename is also encrypted and stored in `Content-Disposition`. Requires `pip install "susops[crypto]"`.

---

## Python API

```python
from susops.facade import SusOpsManager
from susops.core.config import PortForward

mgr = SusOpsManager()

# Add a connection
mgr.add_connection("work", "user@bastion.example.com", socks_port=0)

# Add PAC hosts
mgr.add_pac_host("*.internal.example.com", conn_tag="work")
mgr.add_pac_host("10.0.0.0/8", conn_tag="work")

# Add a local port forward
mgr.add_local_forward("work", PortForward(src_port=5432, dst_port=5432))

# Start
result = mgr.start()
print(result.message)  # "Started"

# Status
status = mgr.status()
print(status.state)    # ProcessState.RUNNING

# React to state changes
mgr.on_state_change = lambda state: print(f"State changed: {state}")
mgr.on_log = lambda msg: print(f"[LOG] {msg}")

# Test connectivity
result = mgr.test("internal.example.com")
print(result.success, result.latency_ms)

# Share a file
from pathlib import Path
info = mgr.share(Path("/tmp/file.txt"))
print(info.url, info.password)

# Stop
mgr.stop()
```

---

## Development

```bash
git clone https://github.com/mashb1t/susops
cd susops
python -m venv .venv && source .venv/bin/activate
pip install -e ".[tui,crypto,dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=susops --cov-report=term-missing

# Run the TUI
susops

# Run the CLI
susops ps
susops ls
```

### Project layout

```
susops/
  pyproject.toml
  src/susops/
    core/
      __init__.py
      config.py         # Pydantic v2 + ruamel.yaml
      ssh.py            # autossh subprocess management
      pac.py            # PAC generation + HTTP server
      share.py          # AES-256-CTR file sharing
      process.py        # PID file-based process manager
      ports.py          # Port utilities
      ssh_config.py     # ~/.ssh/config parser
      types.py          # Enums + result dataclasses
    facade.py           # SusOpsManager public API
    tui/
      __main__.py       # Dual-mode entrypoint
      cli.py            # argparse non-interactive CLI
      app.py            # Textual App
      screens/
        dashboard.py
        connection_editor.py
        share.py
        log_viewer.py
        config_editor.py
      widgets/
        connection_card.py
        status_indicator.py
        bandwidth_chart.py
        log_panel.py
    tray/
      base.py           # AbstractTrayApp
      linux.py          # GTK3 + AyatanaAppIndicator3
      mac.py            # rumps + PyObjC
  tests/
    test_config.py
    test_process.py
    test_pac.py
    test_share.py
    test_facade.py
  packaging/
    aur/PKGBUILD
    homebrew/Formula/susops.rb
    homebrew/Casks/susops.rb
```

---

## Migration from susops.sh

If you used the previous Bash-based `susops.sh`:

1. Your `~/.susops/config.yaml` is **fully compatible** — no migration needed.
2. The `go-yq` / `yq` dependency is **removed** (replaced by Python + pydantic + ruamel.yaml).
3. Process detection no longer uses `pgrep -f` / `exec -a` hacks — PID files in `~/.susops/pids/` are used instead.
4. The PAC HTTP server is now a Python `http.server` daemon thread instead of a `nc` loop.
5. `autossh` is still used when available; falls back to `ssh` if not found.
6. All CLI commands have the same names and behavior.

---

## License

MIT
