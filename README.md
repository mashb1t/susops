<p align="center">
    <img src="assets/icon.png" alt="Menu" height="200" />
</p>

# SusOps - SSH Utilities & SOCKS5 Operations

SSH SOCKS5 proxy manager with PAC server, TCP/UDP port forwarding, Textual TUI, and system tray apps.

## Overview

SusOps manages SSH SOCKS5 proxy tunnels and serves a PAC (Proxy Auto-Config) file so browsers and other tools route traffic through your tunnels automatically. It replaces a 1600-line Bash CLI with a modern Python stack:

- **Textual TUI** — interactive split-pane dashboard, live bandwidth charts, CRUD editor, integrated log viewer
- **Non-interactive CLI** — scriptable `susops` command with semantic exit codes
- **Linux tray app** — GTK3 + AyatanaAppIndicator3
- **macOS tray app** — rumps + PyObjC
- **Shared Python core** — all business logic in `susops.core`, used by every frontend
- **TCP port forwarding** — SSH `-L`/`-R` slaves multiplexed over the existing ControlMaster
- **UDP port forwarding** — socat over SSH ControlMaster; no extra SSH ports needed

### Architecture

```
susops/
  src/susops/
    core/          # Business logic (no UI)
      config.py    # Pydantic v2 models + ruamel.yaml I/O
      ssh.py       # SSH ControlMaster/slave subprocess + PID tracking + socket helpers
      socat.py     # UDP port forwarding via socat over SSH ControlMaster
      pac.py       # PAC generation + aiohttp HTTP server (shared async loop)
      share.py     # AES-256-CTR encrypted file sharing + client fetch (shared async loop)
      status.py    # SSE StatusServer — broadcasts state/share/forward events via aiohttp
      process.py   # ProcessManager: PID files, start/stop/status, zombie detection
      ports.py     # Free port allocation, CIDR helpers
      types.py     # Enums and result dataclasses (ShareInfo with three-state status)
    facade.py      # SusOpsManager — single API for all frontends
    tui/           # Textual TUI + argparse CLI
    tray/          # GTK3 (Linux) and rumps (macOS) tray apps
```

#### Component relations

```mermaid
flowchart TD
    TUI["Textual TUI\n(dashboard, share, etc.)"]
    CLI["argparse CLI"]
    Tray["GTK3 / rumps Tray App\n(AbstractTrayApp + platform subclass)"]

    Facade["SusOpsManager — facade.py\nconfig I/O · PID mgmt · bandwidth sampling\nserver lifecycle"]

    SSH["ssh.py\nControlMaster + forward slaves"]
    Socat["socat.py\nUDP forwards via socat"]
    PAC["pac.py\nPAC server (aiohttp)"]
    Share["share.py\nShareServer(s) (aiohttp)"]

    Loop["Shared async event loop\ndaemon thread — pac · share · status\nall schedule coroutines here"]

    Status["status.py — StatusServer\nSSE /events endpoint\nbroadcasts: state · share · forward"]

    Dashboard["dashboard screen\nSSE listener thread\n(reconnects on 2 s timeout)"]
    ShareScreen["share screen\nset_interval 2 s poll"]

    Master["susops-ssh-&lt;tag&gt;\nSSH ControlMaster\n(-M -N -D socks_port)"]
    Slave["susops-fwd-&lt;tag&gt;-&lt;fw&gt;\nSSH forward slave\n(-O forward -L/-R)"]
    PacProc["susops-pac\nPAC HTTP server"]
    UDPLocal["susops-udp-&lt;tag&gt;-&lt;fw&gt;-lsocat\nLocal socat\n(EXEC → SSH → remote socat)"]
    UDPRemote["susops-udp-&lt;tag&gt;-&lt;fw&gt;-ssh/rsocat/lsocat\nRemote UDP bridge\n(SSH -R + remote socat + local socat)"]

    TUI --> Facade
    CLI --> Facade
    Tray --> Facade

    Facade --> SSH
    Facade --> Socat
    Facade --> PAC
    Facade --> Share

    PAC --> Loop
    Share --> Loop
    Loop --> Status

    Status -->|Server-Sent Events| Dashboard
    Status -->|Server-Sent Events| ShareScreen

    SSH -->|spawns| Master
    Master -->|multiplexes| Slave
    PAC -->|spawns| PacProc
    Socat -->|local direction| UDPLocal
    Socat -->|remote direction| UDPRemote
    Master -.->|ControlMaster socket| UDPLocal
    Master -.->|ControlMaster socket| UDPRemote
```

---

## Requirements

| Component         | Requirement                                                           |
|-------------------|-----------------------------------------------------------------------|
| Python            | ≥ 3.11                                                                |
| SSH tunnels       | `ssh` (OpenSSH, for ControlMaster support)                            |
| PAC server        | `aiohttp >= 3.9` (shared async loop)                                  |
| TUI               | `textual >= 8.2`, `textual-plotext >= 1.0` (optional extra)           |
| File sharing      | `cryptography >= 42`, `aiohttp >= 3.9` (optional extra)               |
| UDP forwarding    | `socat` (system package — see [UDP Port Forwarding](#udp-port-forwarding)) |
| Linux tray        | `python-gobject`, `gtk3`, `libayatana-appindicator` (system packages) |
| macOS tray        | `rumps >= 0.4`                                                        |

---

## Installation

### pip

```bash
# CLI only (no TUI, no tray)
pip install susops

# TUI
pip install "susops[tui]"

# TUI + encrypted file sharing
pip install "susops[tui,share]"

# Linux tray (system GTK3 packages must be installed separately)
pip install "susops[tui,share,tray-linux]"

# macOS tray
pip install "susops[tui,share,tray-mac]"
```

UDP port forwarding has no Python dependencies — install `socat` via your system package manager:

```bash
# macOS
brew install socat

# Arch Linux
sudo pacman -S socat

# Ubuntu / Debian
sudo apt install socat
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
pip install -e ".[tui,share,dev]"
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

#### Dashboard — selected connection

| Key | Action                          |
|-----|---------------------------------|
| `s` | Start selected connection       |
| `x` | Stop selected connection        |
| `r` | Restart selected connection     |

#### Dashboard — all connections

| Key | Action               |
|-----|----------------------|
| `S` | Start all tunnels    |
| `X` | Stop all tunnels     |
| `R` | Restart all tunnels  |

#### Global

| Key      | Action               |
|----------|----------------------|
| `c`      | Connection editor    |
| `f`      | File share screen    |
| `e`      | Config editor (YAML) |
| `Ctrl+P` | Command palette      |
| `q`      | Quit                 |

#### Connection editor

| Key | Action                                                      |
|-----|-------------------------------------------------------------|
| `a` | Add item (connection, PAC host, forward)                    |
| `d` | Delete selected item                                        |
| `t` | Toggle enabled/disabled for selected connection, domain, or forward |
| `e` | Test selected item (SSH ping, SOCKS curl, or port liveness) |
| `s` | Start selected connection or forward                        |
| `x` | Stop selected connection or forward                         |
| `r` | Restart selected connection                                 |

#### Share screen

| Key | Action                           |
|-----|----------------------------------|
| `a` | Share a new file                 |
| `f` | Fetch a remote shared file       |
| `d` | Stop selected share              |
| `s` | Restart a stopped share          |
| `x` | Delete selected share            |

#### Global per-screen

| Key      | Action            |
|----------|-------------------|
| `Escape` | Back to dashboard |

### Screens

**Dashboard** (default) — split-pane view. Left sidebar shows all connections (status dot, SOCKS port, live throughput), PAC server status, and active file shares. Right panel is tabbed:
- **Stats** — CPU%, memory, active connections, PID for the selected connection
- **Bandwidth** — live RX and TX line charts (PlotextPlot, 60-sample rolling window, auto-scaled units)
- **Forwards** — DataTable of all port forwards (direction, local port, local bind, remote port, remote bind, label)
- **Logs** — RichLog of all tunnel output, auto-refreshed every 3 seconds

**Connection editor** — tabbed CRUD editor for Connections, PAC Hosts, Local Forwards, and Remote Forwards. Press `a` to add, `d` to delete, `t` to toggle enabled/disabled, `e` to run a connectivity test for the selected item, `s`/`x`/`r` to start/stop/restart. All add dialogs are modal overlays (dimmed background). A detail preview panel at the bottom shows expanded info for the selected row.

**Share screen** — split-pane: left list of shares with three-state indicators (green = running, dim = manually stopped, red = offline/connection down), right panel with file details, URL, password, access counts, and fetch commands. Press `a` to share a new file, `f` to fetch a remote share, `d` to stop a share, `s` to restart a stopped share, `x` to delete. Refreshes every 2 seconds via `set_interval` to reflect connection state changes.

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
susops start [-c TAG]     # Start tunnel(s) + PAC server (omit -c for all)
susops stop  [-c TAG] [--keep-ports] [--force]
susops restart [-c TAG]
```

`-c TAG` targets a single connection; omit to operate on all connections.
When stopping a single connection its associated file shares are also stopped.
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
susops add -l <local_port> <remote_port> [label] [local_bind] [remote_bind]
susops rm  -l <local_port>

# Remote port forward (-R equivalent)
susops add -r <remote_port> <local_port> [label] [remote_bind] [local_bind]
susops rm  -r <remote_port>
```

Bind addresses default to `localhost`. Use `0.0.0.0` to listen on all interfaces or `172.17.0.1` for Docker bridge access.

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

# Fetch a shared file through an SSH tunnel
susops fetch <port> <password> [outfile]
# Auto-starts the connection if not running; stops it again after download
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

The tray icon reflects the current state (running/partial/stopped). State is polled every 5 seconds. Both tray implementations support the same feature set via native dialogs:

- **Manage** — toggle connection/PAC host/forward enabled state; start, stop, or restart a specific connection
- **Start / Stop / Restart All** — bulk lifecycle operations across all connections
- **Test** — test SSH reachability for a specific connection, curl a domain through its SOCKS proxy, or check port liveness for a specific forward; "Test All PAC Hosts" bulk-tests every configured domain
- **CRUD** — add/remove connections, PAC hosts, and port forwards (with bind address selection)
- **Settings** — configure PAC port, stop-on-quit, and ephemeral ports
- **Browser launch** — open Chrome or Firefox with the PAC URL pre-configured
- **Quit**

---

## Configuration

Config is stored at `~/.susops/config.yaml`. It is created automatically on first use.

```yaml
pac_server_port: 51234
connections:
  - tag: work
    ssh_host: user@bastion.example.com
    socks_proxy_port: 51235
    enabled: true                       # false = skip this connection on start-all
    pac_hosts:
      - "*.internal.example.com"
      - "10.0.0.0/8"
    pac_hosts_disabled:                 # hosts temporarily disabled without removal
      - "*.staging.example.com"
    forwards:
      local:
        - src_port: 5432
          src_addr: localhost
          dst_port: 5432
          dst_addr: db.internal.example.com
          tag: postgres
          tcp: true
          udp: false
          enabled: true                 # false = forward skipped on connection start
        - src_port: 5353
          src_addr: localhost
          dst_port: 53
          dst_addr: dns.internal.example.com
          tag: dns
          tcp: false
          udp: true
          enabled: true
        - src_port: 8080
          src_addr: localhost
          dst_port: 80
          dst_addr: web.internal.example.com
          tag: webui
          tcp: true
          udp: false
          enabled: false
      remote: []
susops_app:
  stop_on_quit: true
  ephemeral_ports: false
  logo_style: COLORED_GLASSES
```

### Port forward bind addresses

| Field      | Description                                                                |
|------------|----------------------------------------------------------------------------|
| `src_addr` | Local bind address for local forwards; remote bind for remote forwards     |
| `dst_addr` | Remote destination host for local forwards; local bind for remote forwards |

Common values: `localhost` (default, loopback only), `0.0.0.0` (all interfaces), `172.17.0.1` (Docker bridge).

### Port forward protocol flags

Each forward has two boolean flags that control which transport protocols are enabled:

| Field | Default | Description                                         |
|-------|---------|-----------------------------------------------------|
| `tcp` | `true`  | Enable TCP forwarding via SSH `-L`/`-R` slave       |
| `udp` | `false` | Enable UDP forwarding via socat (requires `socat`)  |

Both can be `true` simultaneously — susops will start an SSH slave for TCP and socat process(es) for UDP independently. Requires at least one of `tcp` or `udp` to be `true`.

### PAC host syntax

| Pattern            | Matches                      |
|--------------------|------------------------------|
| `*.example.com`    | any subdomain of example.com |
| `10.0.0.0/8`       | any IP in 10.0.0.0/8 CIDR    |
| `host.example.com` | that exact hostname          |

### Port assignment

Ports default to `0` (auto-assign). SusOps picks a random free port from the ephemeral range (49152–65535) at start time and saves it back to `config.yaml`. Pass a specific port to `add-connection` or set it in the config to pin it.

### Workspace

All runtime data lives in `~/.susops/`:

```
~/.susops/
  config.yaml             # persistent config
  susops.pac              # generated PAC file (regenerated on start/change)
  pids/                   # PID files + socket files for each managed process
    susops-ssh-<tag>.pid  # ControlMaster PID
    susops-fwd-<tag>-<fw>.pid  # forward slave PIDs
    susops-pac.pid
    susops-<tag>.sock     # SSH ControlMaster Unix socket
  logs/                   # per-process log files
    susops-ssh-<tag>.log
  firefox_profile/        # temporary Firefox profile for PAC launch
```

### SSH Server: Forward-Only Access

To restrict the SSH user on the server to only perform port forwarding (no shell, no file transfer):

**1. Create a dedicated system user:**

```bash
sudo useradd -r -m -s /usr/sbin/nologin susops-tunnel
sudo mkdir -p /home/susops-tunnel/.ssh
sudo chmod 700 /home/susops-tunnel/.ssh
sudo chown -R susops-tunnel:susops-tunnel /home/susops-tunnel/.ssh
```

**2. Add your public key to `authorized_keys` with restrictions:**

```
# /home/susops-tunnel/.ssh/authorized_keys
restrict,port-forwarding,permitopen="any" ssh-ed25519 AAAA... your-key
```

The `restrict` keyword disables shell, PTY, agent forwarding, and X11 forwarding. `port-forwarding` re-enables only TCP forwarding. `permitopen="any"` allows forwarding to any destination — restrict with `permitopen="host:port"` for tighter control.

**3. Configure `sshd_config`:**

```
# /etc/ssh/sshd_config.d/susops.conf
Match User susops-tunnel
    AllowTcpForwarding yes
    AllowStreamLocalForwarding no
    GatewayPorts no
    X11Forwarding no
    PermitTTY no
    ForceCommand /bin/false
```

**4. Reload sshd:**

```bash
sudo systemctl reload sshd
```

The connection stays alive for SOCKS proxying and port forwards but cannot execute commands or transfer files.

---

## File Sharing

SusOps can share files over an encrypted HTTP server, useful for transferring files through an SSH tunnel. Multiple files can be shared simultaneously on different ports.

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

**Protocol:** HTTP Basic auth (`:password`) + gzip compression + AES-256-CTR encryption (PBKDF2-HMAC-SHA256 key derivation, 600,000 iterations). The original filename is also encrypted and stored in `Content-Disposition`. Requires `pip install "susops[share]"`.

**Fetch behaviour:** `fetch` auto-starts the SSH ControlMaster if the connection is not running, using a minimal start (no configured forwards, PAC server, or file shares are touched). The connection is stopped again after the download completes. If the connection was already running it is left untouched.

**Share status** in the TUI uses three states:
- `●` green — server running and accessible
- `○` dim — manually stopped (will not auto-restart)
- `○` red — offline because its connection went down (auto-resumes when connection restarts)

Each share tracks successful and failed access counts in memory (shown in the detail panel; not persisted across restarts).

---

## UDP Port Forwarding

SusOps can forward UDP traffic through an SSH tunnel using `socat`. This works without any additional ports on the SSH server beyond the existing ControlMaster connection.

### Requirements

`socat` must be installed on **both the local machine and the remote SSH host** for either direction:

- **Local UDP**: local socat listens and forks; remote socat relays each datagram to the destination (via SSH EXEC).
- **Remote UDP**: remote socat relays UDP → TCP (via SSH exec); local socat relays TCP → UDP.

```bash
brew install socat          # macOS
sudo pacman -S socat        # Arch Linux
sudo apt install socat      # Ubuntu / Debian
```

### Enabling UDP on a forward

Set `udp: true` in the forward config, or check "UDP" in the TUI/tray add-forward dialog:

```yaml
forwards:
  local:
    - src_port: 5353
      dst_port: 53
      dst_addr: dns.internal.example.com
      tag: dns
      tcp: false
      udp: true
```

### Architecture: local UDP forward

A single `socat` process listens locally on the UDP port. For each incoming datagram it forks a child that opens an SSH channel through the existing ControlMaster socket and runs `socat` on the remote host to relay the packet to the destination service. Responses travel back through the same channel.

```mermaid
flowchart LR
    subgraph local["Local machine"]
        Client["UDP client\ne.g. dig, game, VoIP"]
        LSOcat["lsocat\nUDP4-RECVFROM:src_port,fork\nEXEC:'ssh -o ControlPath=sock...'"]
    end

    subgraph tunnel["SSH ControlMaster (one channel per datagram)"]
        Channel["SSH channel"]
    end

    subgraph remote["Remote SSH host"]
        RSOcat["remote socat\nsocat - UDP4-SENDTO:\ndst_addr:dst_port"]
        Service["UDP service"]
    end

    Client -->|"UDP datagram"| LSOcat
    LSOcat -->|"fork child"| Channel
    Channel -->|"stdin/stdout"| RSOcat
    RSOcat -->|"UDP"| Service
    Service -. "response" .-> RSOcat
    RSOcat -. "stdout" .-> Channel
    Channel -. "response" .-> LSOcat
    LSOcat -. "UDP response" .-> Client
```

Process name: `susops-udp-<conn>-<fw>-lsocat`

**Latency note:** each datagram opens one SSH channel (~25–40 ms overhead on a typical WAN link). This is suitable for request/response protocols (DNS, database ping, STUN) but not for high-frequency streaming (real-time audio/video).

### Architecture: remote UDP forward

Three processes bridge the remote UDP listener to a local UDP service via an intermediate TCP port:

```mermaid
flowchart LR
    subgraph remote["Remote SSH host"]
        RClient["UDP client\non remote"]
        RSOcat["rsocat\nUDP4-RECVFROM:src_port,fork\nTCP4:localhost:intermediate"]
    end

    subgraph local["Local machine"]
        SSHSlave["SSH -R slave\n-R intermediate:localhost:intermediate"]
        LSOcat["lsocat\nTCP4-LISTEN:intermediate,fork\nUDP4-SENDTO:dst_addr:dst_port"]
        Service["UDP service\ndst_addr:dst_port"]
    end

    RClient -->|"UDP datagram"| RSOcat
    RSOcat -->|"TCP to remote:intermediate\n(bound by SSH -R)"| SSHSlave
    SSHSlave -->|"TCP to localhost:intermediate"| LSOcat
    LSOcat -->|"UDP"| Service
    Service -. "response" .-> LSOcat
    LSOcat -. "TCP" .-> SSHSlave
    SSHSlave -. "TCP" .-> RSOcat
    RSOcat -. "UDP response" .-> RClient
```

Three managed processes per remote UDP forward:

| Name | Runs on | Role |
|------|---------|------|
| `susops-udp-<conn>-<fw>-ssh`    | local  | SSH `-R` slave — requests remote SSH server to bind `intermediate` port and tunnel it back locally |
| `susops-udp-<conn>-<fw>-rsocat` | remote | socat UDP → TCP (via SSH exec); connects to the `intermediate` port bound by the SSH -R slave      |
| `susops-udp-<conn>-<fw>-lsocat` | local  | socat TCP → UDP; listens on `intermediate`, forwards UDP to the destination service                |

### Use cases

| Protocol | Direction | Example                                                |
|----------|-----------|--------------------------------------------------------|
| DNS      | local     | Route `dig` queries to an internal resolver (bypasses split-DNS) |
| DNS      | local     | Use an external resolver (8.8.8.8) bypassing corporate DNS monitoring |
| SNMP     | local     | Query internal SNMP agents (UDP 161) through the tunnel |
| Syslog   | remote    | Receive syslog UDP 514 from remote host to a local collector |
| VoIP/SIP | local     | Reach an internal SIP registrar over UDP 5060          |
| NTP      | local     | Sync to an internal NTP server (UDP 123)               |
| Gaming   | local     | Tunnel UDP game traffic to an internal game server     |

### SSH server restrictions and UDP

The EXEC-based local UDP approach runs a command on the remote SSH host for each datagram. This requires the SSH user to have command execution permission. A `ForceCommand /bin/false` or `restrict` without `command="..."` override will block UDP forwarding. If you need both security restrictions and UDP, use the remote UDP direction (which only requires TCP port forwarding on the server side) or relax `ForceCommand` for the specific socat command.

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

# Add a local port forward (with optional bind addresses)
mgr.add_local_forward("work", PortForward(
    src_port=5432, src_addr="localhost",
    dst_port=5432, dst_addr="db.internal.example.com",
    tag="postgres",
))

# Add a remote port forward
mgr.add_remote_forward("work", PortForward(
    src_port=8080, src_addr="localhost",
    dst_port=8080, dst_addr="localhost",
    tag="webserver",
))

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

# Test SSH reachability for a connection
ok, msg = mgr.test_connection("work")
print(ok, msg)   # True, "SSH OK (42 ms)"

# Test a domain through a connection's SOCKS proxy
ok, msg = mgr.test_domain("internal.example.com", conn_tag="work")
print(ok, msg)   # True, "HTTP 200 (91 ms)"

# Test liveness of a specific port forward
results = mgr.test_forward("work", src_port=5432, direction="local")
# {"tcp": (True, "port bound (PID 1234)"), "udp": (True, "socat running (PID 5678)")}

# Share a file (multiple concurrent shares supported)
from pathlib import Path
info = mgr.share(Path("/tmp/file.txt"))
print(info.url, info.password)

# Fetch a shared file
path = mgr.fetch(port=info.port, password=info.password, host="localhost")
print(path)  # ~/Downloads/file.txt

# Stop
mgr.stop()
```

---

## Development

```bash
git clone https://github.com/mashb1t/susops
cd susops
python -m venv .venv && source .venv/bin/activate
pip install -e ".[tui,share,dev]"

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
      config.py         # Pydantic v2 + ruamel.yaml
      ssh.py            # SSH ControlMaster/slave management + socket helpers
      socat.py          # UDP forwarding via socat EXEC + SSH ControlMaster
      pac.py            # PAC generation + aiohttp HTTP server
      share.py          # AES-256-CTR file sharing + shared async event loop
      status.py         # aiohttp SSE StatusServer (/events endpoint)
      process.py        # PID file-based process manager + zombie detection
      ports.py          # Port utilities (validate_port, is_port_free, CIDR)
      ssh_config.py     # ~/.ssh/config parser
      types.py          # Enums + result dataclasses (ShareInfo three-state)
    facade.py           # SusOpsManager public API
    tui/
      __main__.py       # Dual-mode entrypoint
      cli.py            # argparse non-interactive CLI
      app.py            # Textual App (SusOpsTuiApp)
      app.tcss          # Global CSS theme
      screens/
        dashboard.py        # Split-pane dashboard (sidebar + tabbed detail)
        connections.py      # CRUD editor with modal dialogs
        share.py            # File share + fetch screen
        config_editor.py    # Read-only YAML viewer
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

### Building Packages

#### PyPI / pip (all platforms)

```bash
pip install build
python -m build
# Produces dist/susops-<version>.tar.gz and dist/susops-<version>-py3-none-any.whl
pip install dist/susops-*.whl
```

#### Arch Linux (AUR)

The `packaging/aur/PKGBUILD` builds a wheel and installs it system-wide:

```bash
cd packaging/aur
makepkg -si
```

**Prerequisites:** `python-build`, `python-installer`, `python-wheel`, `python-setuptools`. The `ruamel.yaml` dependency is not in the official Arch repos — install `python-ruamel-yaml` from the AUR or use pip.

The PKGBUILD also installs `susops-tray.desktop` for the system tray launcher.

#### macOS (Homebrew)

```bash
brew tap mashb1t/susops
brew install susops
```

The formula at `packaging/homebrew/Formula/susops.rb` uses `virtualenv_install_with_resources` to create an isolated Python environment with all dependencies. The cask at `packaging/homebrew/Casks/susops.rb` is for a future `.dmg` distribution of `SusOps.app`.

**Note:** Resource sha256 checksums in the formula must be updated for each release. Generate them with:

```bash
shasum -a 256 <downloaded-tarball>
```

#### Version bumping

Update the version in `src/susops/version.py`, then update `pkgver` in `packaging/aur/PKGBUILD` and the `url` version in `packaging/homebrew/Formula/susops.rb` to match.

---

## Migration from susops.sh

If you used the previous Bash-based `susops.sh`:

1. Your `~/.susops/config.yaml` is **fully compatible** — no migration needed.
2. The `go-yq` / `yq` dependency is **removed** (replaced by Python + pydantic + ruamel.yaml).
3. Process detection no longer uses `pgrep -f` / `exec -a` hacks — PID files in `~/.susops/pids/` are used instead.
4. The PAC HTTP server is now a Python `http.server` daemon thread instead of a `nc` loop.
5. `autossh` is no longer used — replaced by plain `ssh` with ControlMaster mode for stable PID tracking and multiplexed forwards.
6. All CLI commands have the same names and behavior.

---

## License

[GNU General Public License v3.0](https://github.com/mashb1t/susops/blob/main/LICENSE)
