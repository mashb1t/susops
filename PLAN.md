# SusOps Monorepo Rewrite: Python TUI + Shared Core

## Context

The SusOps project currently consists of a 1621-line Bash shell function (`susops.sh`) as the CLI backend, a 1499-line Python GTK3 tray app for Linux, and a ~1320-line Python rumps/PyObjC tray app for macOS. All three live in separate repos. Both tray apps shell out to `susops.sh` for every operation and duplicate ~60% of their logic. The Bash CLI uses fragile patterns: `exec -a` process renaming + `pgrep -f` for process detection (broken on Linux), `nc` loops for the PAC HTTP server, and `openssl`/`nc` for file sharing.

**Goal:** Replace the Bash CLI with a modern Python Textual TUI, build a shared Python core library that eliminates all subprocess-to-bash calls, unify all repos into a monorepo, and keep SSH (autossh) + PAC serving as the functional core.

---

## Monorepo Structure

**Root:** `susops/` (new monorepo, replaces separate repos)

```
susops/
  pyproject.toml                    # single package: pip install susops[tui,tray-linux,crypto]
  version.py                        # single version string
  src/susops/
    core/
      __init__.py
      config.py                     # Pydantic v2 models + ruamel.yaml I/O
      ssh.py                        # autossh/ssh subprocess + PID file tracking
      pac.py                        # PAC JS generation + Python HTTP server (http.server)
      share.py                      # AES-256 file share server + client (cryptography lib)
      process.py                    # ProcessManager: start/stop/status via PID files
      ports.py                      # random free port allocation, validation, CIDR utils
      ssh_config.py                 # ~/.ssh/config parser for host autocomplete
      types.py                      # ProcessState, LogoStyle, enums, result dataclasses
    facade.py                       # SusOpsManager: single public API for all frontends
    tui/
      __init__.py
      __main__.py                   # entrypoint: TUI if isatty(), else CLI dispatch
      cli.py                        # argparse non-interactive dispatch
      app.py                        # Textual App subclass
      screens/
        dashboard.py                # live status + SSH bandwidth sparklines
        connection_editor.py        # CRUD for connections, PAC hosts, forwards
        share.py                    # file share/fetch wizard
        log_viewer.py               # RichLog real-time logs
        config_editor.py            # TextArea YAML view + "open in $EDITOR"
      widgets/
        connection_card.py          # per-connection status card
        status_indicator.py         # colored dot + state label
        bandwidth_chart.py          # psutil-based traffic sparkline
        log_panel.py
    tray/
      __init__.py
      base.py                       # AbstractTrayApp: shared menu/polling/CRUD logic
      linux.py                      # GTK3 + AyatanaAppIndicator3 (~250 lines)
      mac.py                        # rumps + PyObjC (~250 lines)
  assets/
    icons/
      colored_glasses/{dark,light}/*.svg
      colored_s/{dark,light}/*.svg
      gear/{dark,light}/*.svg
    icon.png
  packaging/
    aur/
      PKGBUILD
      .SRCINFO
    homebrew/
      Formula/susops.rb
      Casks/susops.rb
  tests/
    test_config.py
    test_process.py
    test_pac.py
    test_share.py
    test_facade.py
  README.md
  LICENSE
```

**Incoming repos merged in:**
- `susops-linux/` → `src/susops/tray/linux.py` + `src/susops/tui/` + assets
- `susops-mac/` → `src/susops/tray/mac.py`
- `susops-cli/` → superseded by `src/susops/core/` (kept as reference, not used)
- `susops-aur/` → `packaging/aur/`
- `homebrew-susops/` → `packaging/homebrew/`

---

## pyproject.toml Structure

```toml
[project]
name = "susops"
version = { file = "version.py" }  # or dynamic from version.py
dependencies = [
    "pydantic>=2.0",
    "ruamel.yaml>=0.18",
    "psutil>=5.9",
]

[project.optional-dependencies]
tui    = ["textual>=0.80", "textual-plotext>=0.2"]
tray-linux = []          # PyGObject/GTK3 are system packages; documented
tray-mac   = ["rumps>=0.4"]
crypto = ["cryptography>=42"]   # file sharing feature

[project.scripts]
susops = "susops.tui.__main__:main"
so     = "susops.tui.__main__:main"

[project.gui-scripts]
susops-tray = "susops.tray:main"   # auto-detects platform at runtime
```

---

## Core Modules

### `core/types.py`
- `ProcessState` enum: `INITIAL`, `RUNNING`, `STOPPED_PARTIALLY`, `STOPPED`, `ERROR`
- `LogoStyle` enum: `COLORED_GLASSES`, `COLORED_S`, `GEAR`
- Result dataclasses: `StartResult`, `StopResult`, `StatusResult`, `TestResult`, `ShareInfo`

### `core/config.py` — Pydantic v2 + ruamel.yaml

```python
class PortForward(BaseModel):
    tag: str = ""
    src_addr: str = "localhost"
    src_port: int
    dst_addr: str = "localhost"
    dst_port: int

class Forwards(BaseModel):
    local: list[PortForward] = []
    remote: list[PortForward] = []

class Connection(BaseModel):
    tag: str
    ssh_host: str
    socks_proxy_port: int = 0   # 0 = auto-assign on start
    forwards: Forwards = Forwards()
    pac_hosts: list[str] = []

class AppConfig(BaseModel):
    stop_on_quit: bool = True
    ephemeral_ports: bool = False
    logo_style: LogoStyle = LogoStyle.COLORED_GLASSES

class SusOpsConfig(BaseModel):
    pac_server_port: int = 0
    connections: list[Connection] = []
    susops_app: AppConfig = AppConfig()

def load_config(workspace: Path) -> SusOpsConfig: ...   # ruamel.yaml load
def save_config(config: SusOpsConfig, workspace: Path): ...  # ruamel.yaml dump
```

**Migration:** `@model_validator(mode='before')` handles legacy schema (plain `src`/`dst` port-only forward fields from old YAML).

### `core/process.py` — ProcessManager

- PID files at `~/.susops/pids/<name>.pid`
- `start(name, cmd: list[str]) -> int` — `subprocess.Popen`, stores PID
- `stop(name, force=False)` — SIGTERM (or SIGKILL with `force`), removes PID file
- `is_running(name) -> bool` — reads PID file, checks `os.kill(pid, 0)` (cross-platform)
- `status_all() -> dict[str, bool]`
- `get_pid(name) -> int | None`
- No more `exec -a` renaming, no `pgrep -f` hacks

### `core/ports.py`
- `get_random_free_port(start=49152, end=65535) -> int` — uses `socket.bind(('', 0))` instead of `lsof`
- `is_port_free(port, host='localhost') -> bool`
- `validate_port(port) -> bool` — 1–65535
- `cidr_to_netmask(cidr_bits) -> str` — for PAC CIDR rules
- `check_port_forward_conflicts(config, src_port, direction) -> str | None`

### `core/ssh.py`

```python
def build_ssh_cmd(conn: Connection, workspace: Path) -> list[str]:
    """Builds: [autossh|-M 0|-N|-T|-D <port>|-L ...|-R ...| ssh_host]"""

def start_tunnel(conn: Connection, workspace: Path, process_mgr: ProcessManager) -> int:
    """Starts autossh (or ssh fallback), returns PID."""

def stop_tunnel(tag: str, process_mgr: ProcessManager): ...

def test_ssh_connectivity(ssh_host: str) -> bool:
    """Quick SSH connectivity check (replaces add-connection's temp L-forward test)."""
```

- autossh detection via `shutil.which('autossh')`
- SSH options: `-N -T -D <socks_port> -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3`
- Local/remote forwards from `conn.forwards.local` and `conn.forwards.remote`
- Process name: `susops-ssh-<tag>` stored in PID file key (no exec -a)

### `core/pac.py`

```python
def generate_pac(config: SusOpsConfig) -> str:
    """Generates FindProxyForURL JS for all connections."""

def write_pac_file(config: SusOpsConfig, workspace: Path) -> Path: ...

class PacServer:
    def start(self, port: int, pac_path: Path): ...   # HTTPServer in daemon thread
    def stop(self): ...
    def is_running(self) -> bool: ...
    def get_port(self) -> int: ...
```

- PAC rules: wildcard → `shExpMatch`, CIDR → `isInNet`, plain → `host ==` / `dnsDomainIs`
- HTTP server: `http.server.HTTPServer` in a `threading.Thread(daemon=True)`, serves `application/x-ns-proxy-autoconfig`

### `core/share.py`

```python
class ShareServer:
    """Python HTTP server with AES-256-CTR + PBKDF2 encryption (cryptography lib)."""
    def start(self, file: Path, password: str, port: int): ...
    def stop(self): ...

def fetch_file(host: str, port: int, password: str, outfile: Path | None) -> Path:
    """Downloads and decrypts shared file using urllib."""
```

- Encryption: `cryptography.hazmat.primitives.ciphers.algorithms.AES` (CTR mode) + `PBKDF2HMAC`
- HTTP Basic auth (`:password` scheme, same as current)
- Filename encryption: same AES key, base64 encoded in `Content-Disposition`
- File stored encrypted in `~/.susops/shared-files/`

### `core/ssh_config.py`
- `get_ssh_hosts() -> list[str]` — parses `~/.ssh/config` for `Host` entries (strips wildcards)

---

## `facade.py` — SusOpsManager

The single public API used by TUI, tray apps, and CLI mode:

```python
class SusOpsManager:
    def __init__(self, workspace: Path = Path.home() / ".susops"):
        self.config: SusOpsConfig
        self._process_mgr: ProcessManager
        self._pac_server: PacServer
        self._share_server: ShareServer | None
        self._log_buffer: deque[str]  # ring buffer, maxlen=500

        # Event callbacks (set by frontend)
        self.on_state_change: Callable[[ProcessState], None] | None = None
        self.on_log: Callable[[str], None] | None = None

    # Lifecycle
    def start(self, tag: str | None = None) -> StartResult
    def stop(self, keep_ports=False, force=False) -> StopResult
    def restart(self, tag: str | None = None) -> RestartResult
    def status(self) -> StatusResult    # returns ProcessState + per-connection states

    # Connection CRUD
    def add_connection(self, tag, ssh_host, socks_port=0) -> Connection
    def remove_connection(self, tag) -> None
    def test_ssh(self, ssh_host: str) -> bool

    # PAC hosts
    def add_pac_host(self, host: str, conn_tag: str | None = None) -> None
    def remove_pac_host(self, host: str) -> None

    # Port forwards
    def add_local_forward(self, conn_tag: str, fw: PortForward) -> None
    def add_remote_forward(self, conn_tag: str, fw: PortForward) -> None
    def remove_local_forward(self, src_port: int) -> None
    def remove_remote_forward(self, src_port: int) -> None

    # File sharing
    def share(self, file: Path, password: str | None = None, port: int | None = None) -> ShareInfo
    def fetch(self, port: int, password: str, outfile: Path | None = None) -> Path

    # Testing
    def test(self, target: str) -> TestResult
    def test_all(self) -> list[TestResult]

    # Utilities
    def list_config(self) -> SusOpsConfig
    def reset(self) -> None
    def get_logs(self, n: int = 100) -> list[str]
    def get_bandwidth(self, tag: str) -> tuple[float, float]  # (rx_bps, tx_bps) via psutil
```

---

## TUI Design (`src/susops/tui/`)

### Dual-mode entrypoint (`__main__.py`)

```python
def main():
    args = parse_args()  # argparse
    if sys.stdout.isatty() and args.command is None:
        SusOpsTuiApp().run()    # Textual TUI
    else:
        cli_dispatch(args)       # non-interactive CLI output
```

### Non-interactive CLI (`cli.py`)

Commands: `start [tag]`, `stop [--keep-ports] [--force]`, `restart`, `ps`, `ls`, `add-connection <tag> <host> [port]`, `rm-connection <tag>`, `add <host> [-c tag]`, `rm <host>`, `add -l/-r ...`, `rm -l/-r ...`, `test <target>`, `test --all`, `share <file> [password] [port]`, `fetch <port> <password> [outfile]`, `reset [--force]`, `chrome`, `firefox`.

All call `SusOpsManager` methods directly, print results to stdout, exit with semantic codes (0=ok, 2=partial, 3=stopped, 1=error).

### Textual App (`app.py`)

```python
class SusOpsTuiApp(App):
    TITLE = "SusOps"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("ctrl+p", "command_palette", "Commands"),
        ("d", "push_screen('dashboard')", "Dashboard"),
        ("c", "push_screen('connections')", "Connections"),
        ("l", "push_screen('logs')", "Logs"),
        ("s", "push_screen('share')", "Share"),
        ("q", "quit", "Quit"),
    ]
    SCREENS = {
        "dashboard": DashboardScreen,
        "connections": ConnectionEditorScreen,
        "logs": LogViewerScreen,
        "share": ShareScreen,
        "config": ConfigEditorScreen,
    }
```

### Dashboard Screen (`screens/dashboard.py`)

- Top bar: SusOps version + global start/stop/restart buttons
- Per-connection cards (via `ConnectionCard` widget):
  - Status dot (colored by `ProcessState`)
  - Tag, SSH host, SOCKS port
  - SSH bandwidth sparkline (↑↓ kB/s) using `psutil` + `Sparkline` widget
  - PAC host count, forward count
- PAC server status card: port, running state
- File share status card (if active)
- Footer: keybindings
- Auto-refresh via `set_interval(2.0, self.refresh_status)`

### Connection Editor Screen (`screens/connection_editor.py`)

- `TabbedContent`: Connections | PAC Hosts | Local Forwards | Remote Forwards
- Each tab: `DataTable` with add/remove actions
- Add dialogs use `Input`, `Select`, `Switch` widgets
- Validates ports, duplicates, CIDR syntax

### Log Viewer Screen (`screens/log_viewer.py`)

- `RichLog` widget showing `manager.get_logs()`
- Filter by connection tag via `Select`
- Auto-scroll toggle
- Live updates via `on_log` callback

### Bandwidth Chart Widget (`widgets/bandwidth_chart.py`)

- Polls `manager.get_bandwidth(tag)` every 2s
- Maintains rolling 30-sample deque per connection
- Renders via Textual `Sparkline` (or `textual-plotext` line chart)
- Shows ↑ rx_bps and ↓ tx_bps in human-readable kB/s or MB/s

### Command Palette

Textual's built-in `CommandPalette` (Ctrl+P) with providers:
- `SusOpsCommandProvider`: exposes start/stop/restart/test-all/add-connection/share/reset

---

## Tray App Design (`src/susops/tray/`)

### `base.py` — AbstractTrayApp

```python
class AbstractTrayApp(ABC):
    def __init__(self):
        self.manager = SusOpsManager()
        self.manager.on_state_change = self._on_state_change_safe
        self.state = ProcessState.INITIAL

    # Shared logic (identical between Linux + Mac)
    def do_start(self): ...
    def do_stop(self): ...
    def do_restart(self): ...
    def do_add_connection(self, tag, host, port): ...
    def do_remove_connection(self, tag): ...
    def do_add_pac_host(self, host, conn_tag): ...
    def do_remove_pac_host(self, host): ...
    def do_add_local_forward(self, conn_tag, fw): ...
    def do_add_remote_forward(self, conn_tag, fw): ...
    def do_remove_local_forward(self, port): ...
    def do_remove_remote_forward(self, port): ...
    def do_poll(self): ...
    def do_test(self, target): ...
    def do_quit(self): ...
    def do_reset(self): ...
    def _build_browser_launch_cmd(self, browser, pac_port) -> list[str]: ...
    def _should_restart_after_change(self) -> bool: ...

    # Abstract (platform-specific)
    @abstractmethod
    def update_icon(self, state: ProcessState): ...
    @abstractmethod
    def update_menu_sensitivity(self, state: ProcessState): ...
    @abstractmethod
    def show_alert(self, title: str, msg: str): ...
    @abstractmethod
    def show_output_dialog(self, title: str, output: str): ...
    @abstractmethod
    def run_in_background(self, fn: Callable, callback: Callable): ...
    @abstractmethod
    def schedule_poll(self, interval: int): ...
```

### `linux.py` — GTK3 tray (~250 lines)
- Inherits `AbstractTrayApp`
- `run_in_background` → `threading.Thread` + `GLib.idle_add`
- `schedule_poll` → `GLib.timeout_add_seconds`
- All dialogs: `Gtk.Dialog` subclasses (keep existing dialog code, slim down)
- Icon: SVG→PNG via GdkPixbuf, cached in `~/.cache/susops/icons/`

### `mac.py` — rumps tray (~250 lines)
- Inherits `AbstractTrayApp`
- `run_in_background` → `threading.Thread` + `performSelectorOnMainThread_`
- `schedule_poll` → `rumps.Timer`
- All panels: `NSPanel` subclasses (keep existing panel code, slim down)

---

## Packaging

### AUR PKGBUILD
- `pkgdesc`: updated to include TUI
- `depends`: `python python-gobject gtk3 libayatana-appindicator autossh python-pydantic python-psutil openbsd-netcat`
- `optdepends`: `python-textual: TUI interface`, `python-cryptography: file sharing`
- Install: `pip install --no-deps .[tray-linux]` into package root, install launcher to `/usr/bin/susops`
- Remove `go-yq` dependency (replaced by pure Python)

### Homebrew Formula (CLI)
- `depends_on "python"`, `depends_on "autossh"`
- `pip install susops[tui,crypto]` into prefix
- Removes yq formula dependency

### Homebrew Cask (macOS GUI)
- py2app bundle with `susops[tray-mac,crypto]`
- Build step in CI: `python -m py2app`

---

## Migration Notes

1. **Config format**: fully backward compatible — existing `~/.susops/config.yaml` loads without changes
2. **Legacy schema** (old `src`/`dst` port-only forwards): handled by Pydantic `@model_validator`
3. **PID files**: new `~/.susops/pids/` directory — old process detection via `pgrep -f` is dropped
4. **PAC server**: replaces nc loop — same `Content-Type`, same PAC JS output format, different server
5. **File sharing**: same HTTP Basic auth, same encryption scheme, pure Python implementation
6. **Workspace**: still `~/.susops/` — no migration needed

---

## Implementation Order

1. **`core/types.py`** — enums and result dataclasses
2. **`core/config.py`** — Pydantic models + ruamel.yaml I/O + migration validator
3. **`core/ports.py`** — port utilities + CIDR helpers
4. **`core/process.py`** — ProcessManager with PID files
5. **`core/ssh_config.py`** — SSH host parser
6. **`core/ssh.py`** — tunnel start/stop/test
7. **`core/pac.py`** — PAC generation + HTTP server
8. **`core/share.py`** — encrypted file share server + fetch
9. **`facade.py`** — SusOpsManager wiring all core modules
10. **`tui/cli.py`** — non-interactive CLI dispatch
11. **`tui/screens/dashboard.py`** + widgets — live dashboard
12. **`tui/screens/connection_editor.py`** — CRUD screens
13. **`tui/screens/share.py`**, `log_viewer.py`, `config_editor.py`
14. **`tui/app.py`** — Textual App + command palette
15. **`tui/__main__.py`** — dual-mode entrypoint
16. **`tray/base.py`** — AbstractTrayApp
17. **`tray/linux.py`** — GTK3 implementation
18. **`tray/mac.py`** — rumps implementation
19. **`pyproject.toml`** — package definition
20. **`packaging/`** — updated AUR + Homebrew
21. **`tests/`** — core module unit tests
22. **Monorepo setup** — migrate git history, update remotes

---

## Critical Files to Read During Implementation

- `/home/mashb1t/development/susops/susops-cli/susops.sh` — definitive source for all business logic
- `/home/mashb1t/development/susops/susops-linux/susops.py` — Linux tray app to refactor
- `/home/mashb1t/development/susops/susops-mac/app.py` — Mac tray app to refactor
- `/home/mashb1t/development/susops/susops-aur/PKGBUILD` — AUR packaging to update
- `/home/mashb1t/development/susops/homebrew-susops/Formula/susops.rb` — Homebrew formula
- `/home/mashb1t/.susops/config.yaml` — live config for testing compatibility

---

## Verification

1. `pip install -e ".[tui,crypto]"` — installs cleanly
2. `susops ls` — non-interactive CLI reads existing config, prints connections
3. `susops ps` — returns correct state code (0/2/3)
4. `susops start` — autossh tunnel starts, PID file created in `~/.susops/pids/`
5. `curl http://localhost:<pac_port>/susops.pac` — returns valid PAC file
6. `susops` (no args, in terminal) — Textual TUI launches
7. TUI dashboard shows live connection state + bandwidth sparklines
8. TUI connection editor: add/remove connection, add PAC host, add forward
9. `susops-tray` — GTK3 tray app launches and polls state via core
10. Existing `~/.susops/config.yaml` loads without error (backward compat)
11. `pytest tests/` — all unit tests pass
