<p align="center">
    <img src="src/susops/assets/icon.png" alt="Menu" height="200" />
</p>

# SusOps - SSH Utilities & SOCKS5 Operations

SSH SOCKS5 proxy manager with PAC server, TCP/UDP port forwarding, Textual TUI, and system tray apps.

aka. **"VPN for single websites"**

It unifies and supersedes the now archived projects [susops-mac](https://github.com/mashb1t/susops-mac), [susops-linux](https://github.com/mashb1t/susops-linux)  and [susops-cli](https://github.com/mashb1t/susops-cli).

## Overview

SusOps manages SSH SOCKS5 proxy tunnels and serves a PAC file, so browsers and other tools route traffic through your tunnels automatically. V3 replaces a 1600-line Bash CLI with a modern Python stack.
- **[macOS tray app](#macos-tray)** — rumps + PyObjC (sounds kinda hacky but works surprisingly well)
- **[Linux tray app](#linux-tray)** — GTK3 + AyatanaAppIndicator3
- **[TUI](#tui)** — interactive split-pane dashboard, live bandwidth charts, CRUD editor, integrated log viewer
- **[Non-interactive CLI](#cli)** — scriptable `susops` command with semantic exit codes
- **Shared Python core** — all business logic in `susops.core`, used by every frontend
- **TCP port forwarding** — SSH `-L`/`-R` slaves multiplexed over the existing ControlMaster
- **[UDP Port Forwarding](#udp-port-forwarding)** — socat over SSH ControlMaster; no extra SSH ports needed

## Use Cases

| Scenario                         | Feature           | How SusOps helps                                                                                             |
|----------------------------------|-------------------|--------------------------------------------------------------------------------------------------------------|
| Bypass web filters               | SOCKS5 Proxy      | Route only selected domains through SSH. The rest of your browsing remains local.                            |
| Circumvent hotel network blocks  | SOCKS5 Proxy      | SSH on 22/443, then use any TCP port (DB shells, RDP, Git) inside the SOCKS tunnel.                          |
| Secure browsing on hostile Wi‑Fi | SOCKS5 Proxy      | Funnel chosen domains through your VPS, encrypting sensitive traffic end‑to‑end.                             |
| Access remote databases          | Local Forwarding  | Forward a remote database port (e.g. MySQL `3306`) to `localhost:3306` for local querying and tooling.       |
| Develop against remote services  | Local Forwarding  | Map a remote web service port (e.g. `:8080`) to your machine so you can use local debuggers and live-reload. |
| Secure remote desktop            | Local Forwarding  | Tunnel RDP/VNC (`3389`) or SSH to `localhost:3389` for encrypted access to your remote workstation.          |
| Geo‑testing APIs                 | Remote Forwarding | Map `api.example.com` to a server in another region via reverse tunnel—no full VPN required.                 |
| Remote IoT / NAS management      | Remote Forwarding | Expose your local device’s UI at `remote_host:<port>` without opening extra firewall holes.                  |
| Reverse proxying to localhost    | Remote Forwarding | Make ports of local services in development available for a reverse proxy on the remote server (proxy pass). |
| Share local dev server           | Remote Forwarding | Expose your local development site (e.g. `localhost:3000`) on `remote_host:3000` for others to access.       |
| Receive external webhooks        | Remote Forwarding | Open a public endpoint on your SSH host for testing services like N8n or GitHub webhooks without deploying.  |

## What can be forwarded?

- ✅ **TCP traffic**: Any TCP socket opened by a SOCKS‑aware client is forwarded through the tunnel.
- ✅ **DNS**: Domains in the PAC file are resolved on the SSH host.
- ✅ **Ports**: Any port on localhost and the SSH host can be used for forwarding (both ways).
- ✅ **UDP traffic**: By using `socat`, UDP can be forwarded in either direction (see [UDP Port Forwarding](#udp-port-forwarding)). This roughly adds ~25–40 ms of latency, so use wisely.

## What can not be forwarded?

- ❌ **Non-TCP/UDP protocols**: ICMP (ping), GRE, IP-in-IP, etc. are not supported.
- ❌ **Broadcast/multicast**: Forwarding is unicast only. Broadcast and multicast packets are not forwarded.
- ❌ **Tun/Tap interfaces**: SusOps does not create virtual network interfaces and cannot route all traffic like a VPN.
- ❌ **Non-SOCKS-aware applications**: Apps that don't support SOCKS proxies (or can't be configured to) won't route through the tunnel unless you use additional tools like `proxychains4` or system-wide proxy settings.

> [!IMPORTANT]
> **Disclaimer:**
> SusOps uses socks5**h** proxies, which resolve DNS on the remote side. This means that when you access `internal.example.com` through the tunnel, the DNS query is made from the SSH server, not your local machine. This is crucial for accessing internal resources that aren't in public DNS and prevents DNS leaks.
> **SusOps is not a VPN replacement.** It only proxies TCP & UDP traffic through SSH tunnels, and only for configured domains/hosts. It doesn't capture all network traffic or support tun/tap interfaces.

## Requirements

| Component         | Requirement                                                           |
|-------------------|-----------------------------------------------------------------------|
| Python            | ≥ 3.11                                                                |
| SSH tunnels       | `ssh` (OpenSSH, for ControlMaster support)                            |
| PAC server        | `aiohttp >= 3.9` (shared async loop)                                  |
| TUI               | `textual >= 8.2`, `textual-plotext >= 1.0` (optional extra)           |
| File sharing      | `cryptography >= 42`, `aiohttp >= 3.9` (optional extra)               |
| UDP forwarding    | `socat` (system package, see [UDP Port Forwarding](#udp-port-forwarding)) |
| Linux tray        | `python-gobject`, `gtk3`, `libayatana-appindicator` (system packages) |
| macOS tray        | `rumps >= 0.4`                                                        |

---

## Installation

### macOS (Homebrew)

```bash
brew tap mashb1t/susops
brew install susops         # CLI + TUI
brew install --cask susops  # SusOps App (tray)
```

### pip

```bash
# CLI + daemon (no TUI, no tray)
pip install susops

# TUI
pip install "susops[tui]"

# macOS tray
pip install "susops[tui,tray-mac]"
```

The Linux tray needs system packages (`python3-gi`, `gtk3`, `libayatana-appindicator`); see below.

UDP port forwarding also needs a system package (`socat`).

```bash
# Arch Linux — Linux tray + socat
sudo pacman -S python-gobject gtk3 libayatana-appindicator socat

# Ubuntu / Debian — Linux tray + socat
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatana-appindicator3-0.1 socat
```

### Arch Linux (AUR)

```bash
yay -S susops
yay -S python-textual
yay -S python-cryptography
```

### From source

```bash
git clone https://github.com/mashb1t/susops
cd susops
pip install -e ".[tui,dev]"
```

---

## Quick Start

```bash
# Add your first SSH connection
susops add-connection work user@example.com

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

## Service Registration (optional)

Feel free to register `susops-services` as a background service so it's always running and ready to manage your tunnels.

### macOS (launchd)

```bash
cp packaging/macos/org.susops.services.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/org.susops.services.plist
```

Adjust the `ProgramArguments` path in the plist if `susops-services` isn't at `/usr/local/bin/susops-services` (run `which susops-services` to find yours).

### Linux (systemd-user)

```bash
mkdir -p ~/.config/systemd/user
cp packaging/linux/susops-services.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now susops-services
```

The unit uses `%h/.local/bin/susops-services`, adjust if your install path differs.

---

## System Tray App

### macOS tray

Simply open the SusOps app in Applications or run the following command: 

```bash
susops-tray
```

Requires `rumps` (if installed via pip): `pip install "susops[tray-mac]"`

> [!IMPORTANT]
> **The macOS and Linux tray apps diverge in their UI structure.** The 3-column config window described below is macOS-only for now. The Linux tray uses the older v2 submenu structure and might not 100% be compatible with v3, use with caution.

The tray icon reflects the current state (running / partial-or-pending / stopped). State changes arrive over the daemon's SSE `/events` stream. If the stream drops, the listener reconnects within at most 5 seconds. When TUI + tray are both attached to the same daemon, quitting one keeps the other running — `stop_on_quit` is skipped if any other frontend is still connected.

<p align="center">
    <img src="docs/tray/macos/tray-menu.png" alt="Menu" height="300" />
</p>

<p align="center">
    <img src="docs/tray/macos/tray-connections.png" alt="Menu" height="500" />
</p>

<p align="center">
    <img src="docs/tray/macos/tray-settings.png" alt="Menu" height="500" />
</p>

When activating the benu bar bandwidth option, the total bandwidth of all connections is outout next to the SusOps status indicator. 

### Linux

```bash
susops-tray
```

Requires (system packages):
- Arch: `sudo pacman -S python-gobject gtk3 libayatana-appindicator`
- Ubuntu/Debian: `sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatana-appindicator3-0.1`

### Linux tray

The Linux tray keeps the classic submenu menu structure:

- **Manage** — toggle connection/PAC host/forward enabled state; start, stop, or restart a specific connection
- **Start / Stop / Restart All** — bulk lifecycle operations across all connections
- **Test** — test SSH reachability for a specific connection, curl a domain through its SOCKS proxy, or check port liveness for a specific forward; "Test All PAC Hosts" bulk-tests every configured domain
- **CRUD** — add/remove connections, PAC hosts, and port forwards (with bind address selection)
- **Settings** — configure PAC port, stop-on-quit, and ephemeral ports
- **Browser launch** — open Chrome or Firefox with the PAC URL pre-configured
- **Quit**


---

## TUI

Run `susops` (or `so`) with no arguments in a terminal to launch the interactive TUI:

```
susops
```

### Screens

- **Dashboard** (default): shows stats (CPU%, memory, etc.), bandwidth charts (also per connection), forwards and logs.

<p align="center">
    <img src="docs/tui/tui-dashboard.png" alt="Menu" height="500" />
</p>

<p align="center">
    <img src="docs/tui/tui-logs.png" alt="Menu" height="500" />
</p>

- **Connection editor**: tabbed CRUD editor for Connections, PAC Hosts, Local Forwards, and Remote Forwards.

<p align="center">
    <img src="docs/tui/tui-add-domain.png" alt="Menu" height="500" />
</p>

- **Share screen**: add file shares or fetch shared fiels

---

## CLI

When a subcommand is given (or stdout is not a TTY), `susops` runs in non-interactive mode:

```
susops [-c TAG] COMMAND [args]

SusOps — SSH SOCKS5 proxy manager

positional arguments:
  COMMAND
    start                  Start SSH tunnel(s) and PAC server
    stop                   Stop SSH tunnel(s) and PAC server
    restart                Restart tunnel(s)
    ps                     Show process status
    ls                     List all config
    add-connection         Add a new SSH connection
    rm-connection          Remove an SSH connection
    add                    Add PAC host or port forward
    rm                     Remove PAC host or port forward
    test                   Test connectivity
    share                  Share an encrypted file
    fetch                  Fetch an encrypted shared file
    reset                  Reset workspace (destructive)
    config                 Open config file in $EDITOR
    chrome                 Launch Chrome with PAC proxy
    chrome-proxy-settings  Open Chrome proxy settings
    firefox                Launch Firefox with PAC proxy
    guide                  Print proxy setup guide for common tools

options:
  -h, --help               show this help message and exit
  -c, --connection TAG     Target a specific connection by tag
  --version                show program's version number and exit
  -v, --verbose            Enable debug logging (events, state changes)
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

#### Proxy setup guide

```bash
susops [-c TAG] guide    # Print copy-paste proxy config for common tools
```

Shows `socks5h://127.0.0.1:<port>` snippets for shell, Homebrew, pip, npm/yarn/pnpm, git, curl, wget, apt, Docker, and proxychains4. Uses the live port if the tunnel is running, falls back to the saved config port, or shows a `<port>` placeholder with a warning if the port is unknown.

#### Reset

```bash
susops reset [--force]    # Kill all processes, wipe ~/.susops workspace
```

### Exit codes

| Code | Meaning                                                    |
|------|------------------------------------------------------------|
| `0`  | Success / all running                                      |
| `1`  | Error                                                      |
| `2`  | Partial (some services stopped, or any connection pending) |
| `3`  | All stopped                                                |

A connection is **pending** when its SSH master is alive but the ControlMaster socket isn't up yet — typically while ssh-agent is waiting on a key unlock, or between reconnect attempts. `start` returns as soon as the master spawns; auth then completes asynchronously up to 60 s later. Slow agent prompts (Vaultwarden, 1Password, hardware keys) no longer freeze the UI.

---

## Configuration

Config is stored at `~/.susops/config.yaml`. It is created automatically on first use.

Example file with all currently added features:

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

### Deep Dive

<details>
<summary>Port forward bind addresses</summary>


| Field      | Description                                                                |
|------------|----------------------------------------------------------------------------|
| `src_addr` | Local bind address for local forwards, remote bind for remote forwards     |
| `dst_addr` | Remote destination host for local forwards, local bind for remote forwards |

Common values: `localhost` (default, loopback only), `0.0.0.0` (all interfaces), `172.17.0.1` (Docker bridge).

</details>

<details>
<summary>Port forward protocol flags</summary>

Each forward has two boolean flags that control which transport protocols are enabled:

| Field | Default | Description                                         |
|-------|---------|-----------------------------------------------------|
| `tcp` | `true`  | Enable TCP forwarding via SSH `-L`/`-R` slave       |
| `udp` | `false` | Enable UDP forwarding via socat (requires `socat`)  |

Both can be `true` simultaneously. SusOps will start an SSH process for TCP and socat process(es) for UDP independently. Requires at least one of `tcp` or `udp` to be `true`.


</details>

<details>
<summary>PAC host syntax</summary>

| Pattern            | Matches                      |
|--------------------|------------------------------|
| `*.example.com`    | any subdomain of example.com |
| `10.0.0.0/8`       | any IP in 10.0.0.0/8 CIDR    |
| `host.example.com` | that exact hostname          |

</details>

<details>
<summary>Port assignment</summary>

Ports default to `0` (auto-assign). SusOps picks a random free port from the ephemeral range (49152–65535) at start time and saves it back to `config.yaml`. Pass a specific port to `add-connection` or set it in the config.

</details>

<details>
<summary>Tag format</summary>

Connection tags must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` (no slashes, no `..`, no leading punctuation). Tags appear in PID-file names, socket paths, log lines, and PAC keys, so the format is intentionally strict.

</details>

<details>
<summary>Workspace</summary>

All runtime data is stored in `~/.susops/`:

```
~/.susops/
  config.yaml                  # persistent config
  susops.pac                   # generated PAC file (regenerated on start/change)
  pids/                        # PID files + socket files for each managed process
    susops-ssh-<tag>.pid       # ControlMaster PID
    susops-fwd-<tag>-<fw>.pid  # forward slave PIDs
    susops-pac.pid
    susops-<tag>.sock          # SSH ControlMaster Unix socket
  logs/                        # per-process log files
    susops-ssh-<tag>.log     
  firefox_profile/             # temporary Firefox profile for PAC launch
```

</details>

<details>
<summary>SSH Server: Forward-Only Access</summary>

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

</details>

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

**Protocol:** HTTP Basic auth (`:password`) + gzip compression + AES-256-CTR encryption (PBKDF2-HMAC-SHA256 key derivation, 600,000 iterations). The original filename is also encrypted and stored in `Content-Disposition`.

**Fetch behaviour:** `fetch` auto-starts the SSH ControlMaster if the connection is not running, using a minimal start (no configured forwards, PAC server, or file shares are touched). The connection is stopped again after the download completes. If the connection was already running it is left untouched.

Each share tracks successful and failed access counts in memory (shown in the detail panel; not persisted across restarts).

---


### Architecture

SusOps is split into a services daemon (`susops-services`) and thin frontends (CLI, TUI, tray apps) that talk to it over a local JSON-over-HTTP RPC channel plus a Server-Sent Events stream.

More details:

<details>
<summary>Daemon ↔ Frontend protocol</summary>

There are two channels between each frontend and the daemon, both bound to `127.0.0.1`:

| Channel | Endpoint              | Direction         | Use                                                                                                                                                           |
|---------|-----------------------|-------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------|
| RPC     | `POST /rpc`           | frontend → daemon | One method invocation per request. Allowlisted methods from `_ALLOWED_METHODS` in `rpc_server.py` — anything else is rejected. Synchronous, request/response. |
| SSE     | `GET /events`         | daemon → frontend | Long-lived Server-Sent-Events stream. Pushes `state`, `share`, `forward`, and `bandwidth` events so frontends update instantly without polling.               |

Ports are auto-allocated and written to `~/.susops/pids/susops-services.port`. The PID file (`susops-services.pid`) is written atomically with `O_CREAT | O_EXCL`.
The daemon exits as soon as (a) the last SSE client disconnects AND (b) it has nothing tracked: no SSH masters, no shares, PAC down, reconnect monitor watching nothing.

</details>

<details>
<summary>OpenAPI schema</summary>

The full RPC interface is documented in OpenAPI 3.1 schema at [`docs/openapi.yaml`](docs/openapi.yaml). The spec is auto-generated from `SusOpsManager` + `_ALLOWED_METHODS` via `tools/gen_openapi.py`:

```bash
python tools/gen_openapi.py             # regenerate docs/openapi.yaml
python tools/gen_openapi.py --check     # CI: fail if stale
```

The schema is prevented from drifting and always up to date using tests. When contributing, feel free to set up the pre-commit hook in `.githooks/pre-commit`.
You can use this spec as basis for Swagger UI / Redoc / Stoplight for browsable docs, or feed it into a client generator (`openapi-generator`, `oapi-codegen`, etc.) to build alternative frontends.

</details>

<details>
<summary>Access & authentication</summary>

The daemon is built for **single-user local-only access**. There is no authentication (yet), no shared secret (yet), and no per-method permission model. The protections in place are:

| Layer            | Mechanism                                                                                                                                       | What it prevents                                                                                                                                                  |
|------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Bind address     | Both `/rpc` and `/events` listen on `127.0.0.1` only                                                                                            | Remote hosts on the LAN/internet cannot reach the daemon.                                                                                                         |
| Method allowlist | `_ALLOWED_METHODS` in `rpc_server.py` — any method not in the set returns `404 method not allowed`                                              | Private helpers (`mgr._whatever`) can't be invoked. Defence against *accidental* exposure, not malicious callers.                                                 |
| Port discovery   | Port + PID are written to `~/.susops/pids/susops-services.{port,pid}`                                                                           | Filesystem perms (default `0644`) gate read access. A process running as a different local user that can't read your home directory can't find the port.         |

If you need stricter control, feel free to contribute or implement a **Per-daemon bearer token** (create random secret on startup, require it as an `Authorization: Bearer ...` header.

</details>

#### Component relations

```mermaid
flowchart TD
    subgraph Frontends["Frontends"]
        direction LR
        MacOSApp["MacOS App"]
        LinuxApp["Linux App"]
        TUI["Textual TUI"]
        CLI["CLI"]
    end

    Client["SusOpsClient — client.py\nensure_daemon_running\nRPC proxy + retry"]

    MacOSApp --> Client
    LinuxApp --> Client
    TUI --> Client
    CLI --> Client

    Client -->|POST /rpc\nJSON-over-HTTP| RPC
    Client -.->|GET /events\nServer-Sent Events stream — events pushed back| SSE

    subgraph Daemon["susops-services daemon"]
        RPC["rpc_server.py\n/rpc (POST)\nAllowlist-gated dispatch"]
        Facade["facade.py — SusOpsManager\nconfig I/O · PID mgmt · bandwidth sampling\nis_idle() drives daemon shutdown"]
        SSE["status.py — StatusServer\n/events (SSE)\nstate · share · forward · bandwidth"]
        Reconnect["_ReconnectMonitor\nwatches ControlMaster sockets\nre-registers forwards on reconnect"]
        BW["_BandwidthSampler\nreads /proc/<pid>/io (Linux) or nettop (macOS) every 1s"]

        SSH["ssh.py\nControlMaster + -O forward"]
        Socat["socat.py\nUDP forwards via socat"]
        PAC["pac.py\nPAC server (aiohttp)"]
        Share["share.py\nShareServer(s) (aiohttp)"]

        Loop["Shared async event loop\n(daemon thread — pac · share · status)"]
    end

    RPC --> Facade
    Facade --> SSE
    Facade --> Reconnect
    Facade --> BW
    Facade --> SSH
    Facade --> Socat
    Facade --> PAC
    Facade --> Share

    Share --> Loop
    PAC --> Loop
    Loop --> SSE
    
    subgraph Processes["Managed processes"]
        Master["susops-ssh-&lt;tag&gt;\nSSH ControlMaster\n(-M -N -D socks_port)"]
        PacProc["susops-pac\nPAC HTTP server"]
        UDPLocal["susops-udp-&lt;tag&gt;-&lt;fw&gt;-lsocat\nLocal UDP socat\n(EXEC → SSH → remote socat)"]
        UDPRemote["susops-udp-&lt;tag&gt;-&lt;fw&gt;-ssh/rsocat/lsocat\nRemote UDP bridge\n(SSH -R + remote/local socat)"]
    end

    SSH ---->|spawns| Master
    PAC -->|spawns| PacProc
    Socat -->|local direction| UDPLocal
    Socat -->|remote direction| UDPRemote
    Master -.->|ControlMaster socket| UDPLocal
    Master -.->|ControlMaster socket| UDPRemote
```

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

Set `udp: true` in the forward config, or check "UDP" in the TUI/tray forward form:

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

> [!NOTE]
> **Latency:** each datagram opens one SSH channel (~25–40 ms overhead on a typical WAN link). This is suitable for request/response protocols (DNS, database ping, STUN) but not for high-frequency streaming (real-time audio/video).

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

Example implementation:

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
pip install -e ".[tui,dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=susops --cov-report=term-missing

# Run the TUI
susops

# Run the CLI
susops ps
susops ls

# Build local pypi + brew artifacts (mirrors CI, no upload)
scripts/build-local.sh pypi           # wheel + sdist
scripts/build-local.sh brew           # SusOps.app + .dmg (macOS only)
scripts/build-local.sh install-pypi   # install wheel into a throwaway venv
scripts/build-local.sh install-brew   # copy SusOps.app to /Applications
```

### Tray development (macOS)

Run a dev tray instance against an isolated workspace without touching your live `~/.susops` setup:

```bash
# Isolated workspace (avoids any conflict with a running user tray)
WS=$(mktemp -d /tmp/susops-dev.XXXX)
SUSOPS_TRAY_WORKSPACE=$WS SUSOPS_TRAY_DEBUG_PORT=7799 .venv/bin/susops-tray &

# Drive the tray via the debug command server
.venv/bin/python tools/tray_debug.py 7799 ping
.venv/bin/python tools/tray_debug.py 7799 dump-menu        # full menu JSON
.venv/bin/python tools/tray_debug.py 7799 open-config       # open the Settings window
.venv/bin/python tools/tray_debug.py 7799 screenshot /tmp/tray.png
.venv/bin/python tools/tray_debug.py 7799 quit
```

`SUSOPS_TRAY_WORKSPACE` points the tray at a different workspace directory (separate config, PID files, sockets) so it never touches `~/.susops`. `SUSOPS_TRAY_DEBUG_PORT` starts a localhost command server on that port; `tools/tray_debug.py` is the client. Supported commands: `ping`, `dump-menu`, `open-config [gear]`, `select <conn> <section> <index>`, `dump-window`, `screenshot <path>`, `action <name>`, `open-about`, `quit`.

### GUI smoke tests (macOS)

```bash
SUSOPS_RUN_GUI_TESTS=1 .venv/bin/pytest -m gui -v
```

Seven smoke tests launch a real tray instance (using `SUSOPS_TRAY_WORKSPACE` + `SUSOPS_TRAY_DEBUG_PORT` internally), drive it through the debug server, and assert menu structure and window content. Skipped automatically on Linux and when `SUSOPS_RUN_GUI_TESTS` is unset.



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

The formula at `packaging/homebrew/Formula/susops.rb` uses `virtualenv_install_with_resources` to create an isolated Python environment for the CLI + TUI. The cask at `packaging/homebrew/Casks/susops.rb` installs `SusOps.app` (PyInstaller bundle) for the macOS tray — `brew install --cask susops`.

**Note:** Resource sha256 checksums in the formula must be updated for each release. Generate them with:

```bash
shasum -a 256 <downloaded-tarball>
```

---

## Migration from susops.sh

If you used the previous Bash-based `susops.sh`:

1. Your `~/.susops/config.yaml` is **fully compatible**, no migration needed.
2. The `go-yq` / `yq` dependency is **removed** (replaced by Python + pydantic + ruamel.yaml).
3. Process detection no longer uses `pgrep -f` / `exec -a` hacks. PID files in `~/.susops/pids/` are used instead.
4. The PAC HTTP server is now a Python `http.server` daemon thread instead of a `nc` loop.
5. `autossh` is no longer used, but were replaced by plain `ssh` with ControlMaster mode for stable PID tracking and multiplexed forwards.
6. All CLI commands have the same names and behavior.

---

## License

[AGPL v3.0](LICENSE)
