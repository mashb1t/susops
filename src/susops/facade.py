"""SusOpsManager — the single public API for all SusOps frontends."""
from __future__ import annotations

import dataclasses
import datetime
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable

from susops.core.config import (
    Connection,
    FileShare,
    PortForward,
    SusOpsConfig,
    get_connection,
    get_default_connection,
    load_config,
    save_config,
)
from susops.core.pac import PacServer, write_pac_file
from susops.core.ports import get_random_free_port, is_port_free, validate_port
from susops.core.process import ProcessManager
from susops.core.share import ShareServer, fetch_file, generate_password
from susops.core.socat import (
    UDP_PROCESS_PREFIX,
    start_udp_forward,
    stop_udp_forward,
    stop_all_udp_forwards_for_connection,
    is_udp_forward_running as _is_udp_forward_running,
)
from susops.core.ssh import (
    FWD_PROCESS_PREFIX,
    SSH_PROCESS_PREFIX,
    cancel_forward,
    find_master_pid,
    is_socket_alive,
    is_tunnel_running,
    socket_path,
    start_forward,
    start_master,
    stop_tunnel,
    test_ssh_connectivity,
)
from susops.core.status import StatusServer
from susops.core.types import (
    ConnectionStatus,
    ProcessState,
    ShareInfo,
    StartResult,
    StatusResult,
    StopResult,
    TestResult,
)

__all__ = ["SusOpsManager"]

_WORKSPACE_DEFAULT = Path.home() / ".susops"


class _BandwidthSampler:
    """Background thread that samples per-connection bandwidth every second."""

    INTERVAL = 1.0
    _HISTORY_MAX = 60
    _HISTORY_FILE = "bandwidth_history.json"

    def __init__(
            self,
            process_mgr: ProcessManager,
            workspace: "Path | None" = None,
            on_sample: Callable[[str, float, float], None] | None = None,
    ) -> None:
        self._mgr = process_mgr
        self._workspace = workspace
        self._rates: dict[str, tuple[float, float]] = {}
        self._totals: dict[str, tuple[float, float]] = {}  # tag -> (rx_total_bytes, tx_total_bytes)
        self._prev_net: tuple[float, float, float] | None = None
        self._prev_chars: dict[str, float] = {}
        # macOS-only: cumulative bytes_in/bytes_out per pid from `nettop`.
        # Used to compute per-sample deltas → per-tag rates. None until
        # nettop is confirmed available (probe on first sample).
        self._prev_nettop: dict[int, tuple[int, int]] | None = None
        self._prev_nettop_t: float | None = None
        self._nettop_available: bool | None = None  # tri-state probe cache
        self._lock = threading.Lock()
        self._on_sample = on_sample
        # Per-tag rolling history: tag -> list of [rx_bps, tx_bps] (up to _HISTORY_MAX samples)
        self._history: dict[str, list[list[float]]] = {}
        self._load_history()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="susops-bw-sampler"
        )
        self._thread.start()

    def _history_path(self) -> "Path | None":
        if self._workspace is None:
            return None
        return self._workspace / self._HISTORY_FILE

    def _load_history(self) -> None:
        """Load persisted bandwidth history from disk if available."""
        path = self._history_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                for tag, samples in data.items():
                    if isinstance(samples, list):
                        self._history[tag] = samples[-self._HISTORY_MAX:]
        except Exception:
            pass

    def _save_history(self) -> None:
        """Persist the current bandwidth history to disk (called under self._lock)."""
        path = self._history_path()
        if path is None:
            return
        try:
            path.write_text(json.dumps(self._history))
        except Exception:
            pass

    def _run(self) -> None:
        while True:
            try:
                self._sample()
            except Exception:
                pass
            time.sleep(self.INTERVAL)

    @staticmethod
    def _parse_nettop_line(line: str) -> tuple[int, int, int] | None:
        """Parse one `nettop -P -x -J bytes_in,bytes_out` data row.

        Format (whitespace-aligned, NOT comma-separated):
            <process name>.<pid>   <bytes_in>   <bytes_out>

        Process names can contain spaces (e.g. "Brave Browser H.879"), so we
        split on whitespace and take the last two tokens as the numeric
        columns and everything before as the name+pid field.
        """
        parts = line.split()
        if len(parts) < 3:
            return None
        try:
            b_in = int(parts[-2])
            b_out = int(parts[-1])
        except ValueError:
            return None
        name_field = " ".join(parts[:-2])
        if "." not in name_field:
            return None
        try:
            pid = int(name_field.rsplit(".", 1)[1])
        except ValueError:
            return None
        return pid, b_in, b_out

    def _sample_macos_nettop(self, tag_pids: dict[str, list[int]], now: float) -> bool:
        """Sample per-tunnel bandwidth on macOS via the `nettop` CLI tool.

        Returns True if rates were published (caller skips the Linux path).
        Returns False if nettop is unavailable or the first sample (we need
        a baseline to compute deltas against); caller falls back / waits.

        Why nettop: macOS doesn't expose per-process network bytes through
        psutil or any public C API (no /proc, no proc_pidinfo network
        counters). `nettop` is shipped with macOS, runs without sudo, and
        reports cumulative bytes_in/bytes_out per process. We run it
        non-interactively (`-l 1 -s 1`) per sample tick, compute deltas
        against the previous snapshot, and divide by elapsed time.
        """
        if self._nettop_available is False:
            return False
        if not tag_pids:
            return False

        try:
            # `-t external` excludes loopback traffic. Without it nettop
            # counts every proxied byte twice — once on the Chrome ↔ ssh
            # loopback leg and once on the ssh ↔ remote external socket —
            # producing artificially symmetric bytes_in/bytes_out (e.g. a
            # 40 MB YouTube stream shows as 40 MB in + 40 MB out instead
            # of 40 MB in + ~150 KB out). External-only matches the real
            # SSH-to-remote throughput.
            result = subprocess.run(
                ["nettop", "-P", "-l", "1", "-s", "1", "-x",
                 "-t", "external", "-J", "bytes_in,bytes_out"],
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, OSError):
            self._nettop_available = False
            return False
        except subprocess.TimeoutExpired:
            # Don't disable permanently — could be transient under load.
            return False
        self._nettop_available = True

        # Parse: collect per-PID cumulative bytes for the PIDs we care about.
        wanted: set[int] = {pid for pids in tag_pids.values() for pid in pids}
        per_pid: dict[int, tuple[int, int]] = {}
        for line in result.stdout.splitlines():
            parsed = self._parse_nettop_line(line)
            if parsed is None:
                continue
            pid, b_in, b_out = parsed
            if pid in wanted:
                per_pid[pid] = (b_in, b_out)

        with self._lock:
            prev = self._prev_nettop
            prev_t = self._prev_nettop_t
            # First sample — store baseline, no rates yet.
            if prev is None or prev_t is None:
                self._prev_nettop = per_pid
                self._prev_nettop_t = now
                return True

            dt = now - prev_t
            if dt <= 0:
                dt = self.INTERVAL

            new_rates: dict[str, tuple[float, float]] = {}
            for tag, pids in tag_pids.items():
                rx_delta = 0
                tx_delta = 0
                for pid in pids:
                    cur = per_pid.get(pid)
                    if cur is None:
                        continue
                    p = prev.get(pid)
                    if p is None:
                        # New PID — no baseline, contribute 0 this sample.
                        continue
                    rx_delta += max(0, cur[0] - p[0])
                    tx_delta += max(0, cur[1] - p[1])
                rx_rate = rx_delta / dt
                tx_rate = tx_delta / dt
                new_rates[tag] = (rx_rate, tx_rate)
                if self._on_sample:
                    try:
                        self._on_sample(tag, rx_rate, tx_rate)
                    except Exception:
                        pass
                # Accumulate cumulative byte totals (actual bytes, not rate).
                prev_total_rx, prev_total_tx = self._totals.get(tag, (0.0, 0.0))
                self._totals[tag] = (
                    prev_total_rx + float(rx_delta),
                    prev_total_tx + float(tx_delta),
                )
                # Per-tag rolling rate history.
                tag_hist = self._history.setdefault(tag, [])
                tag_hist.append([rx_rate, tx_rate])
                if len(tag_hist) > self._HISTORY_MAX:
                    del tag_hist[:-self._HISTORY_MAX]

            self._rates = new_rates
            self._prev_nettop = per_pid
            self._prev_nettop_t = now
            self._save_history()
        return True

    def _sample(self) -> None:
        try:
            import psutil
        except ImportError:
            return

        now = time.monotonic()
        net = psutil.net_io_counters()
        if net is None:
            return
        sys_rx = float(net.bytes_recv)
        sys_tx = float(net.bytes_sent)

        # Build tag → list[pid] covering master + all slave processes.
        # Forward slaves are NOT OS children of the master (start_new_session=True),
        # so proc.children() misses them.
        all_entries = self._mgr.status_all()
        master_tags: dict[str, int] = {}
        for key in all_entries:
            if key.startswith(SSH_PROCESS_PREFIX + "-"):
                tag = key[len(SSH_PROCESS_PREFIX) + 1:]
                pid = self._mgr.get_pid(key)
                if pid:
                    master_tags[tag] = pid

        tag_pids: dict[str, list[int]] = {tag: [pid] for tag, pid in master_tags.items()}
        for key in all_entries:
            if key.startswith(FWD_PROCESS_PREFIX + "-"):
                remainder = key[len(FWD_PROCESS_PREFIX) + 1:]
                for tag in master_tags:
                    if remainder.startswith(tag + "-"):
                        pid = self._mgr.get_pid(key)
                        if pid:
                            tag_pids[tag].append(pid)
                        break

        # macOS: psutil.Process.io_counters() doesn't exist (Linux/Windows only),
        # so the read_chars-weighted attribution below collapses to all-zero.
        # Use `nettop` instead — it's the only macOS userspace tool that
        # exposes per-process network bytes. Values are cumulative since the
        # OS process started, stable across invocations, monotonic.
        if sys.platform == "darwin":
            macos_done = self._sample_macos_nettop(tag_pids, now)
            if macos_done:
                return

        proc_chars: dict[str, float] = {}
        for tag, pids in tag_pids.items():
            chars = 0.0
            for pid in pids:
                try:
                    proc = psutil.Process(pid)
                    chars += float(getattr(proc.io_counters(), "read_chars", 0))
                except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                    pass
            proc_chars[tag] = chars

        with self._lock:
            if self._prev_net is not None:
                prev_rx, prev_tx, prev_t = self._prev_net
                dt = now - prev_t
                if dt > 0:
                    delta_rx = max(0.0, sys_rx - prev_rx) / dt
                    delta_tx = max(0.0, sys_tx - prev_tx) / dt

                    deltas: dict[str, float] = {}
                    for tag, chars in proc_chars.items():
                        prev = self._prev_chars.get(tag, chars)
                        deltas[tag] = max(0.0, chars - prev)
                    total_delta = sum(deltas.values()) or 1.0

                    new_rates: dict[str, tuple[float, float]] = {}
                    for tag in proc_chars:
                        weight = deltas.get(tag, 0.0) / total_delta
                        rx = delta_rx * weight
                        tx = delta_tx * weight
                        new_rates[tag] = (rx, tx)
                        if self._on_sample:
                            try:
                                self._on_sample(tag, rx, tx)
                            except Exception:
                                pass
                    self._rates = new_rates

                    # Accumulate cumulative byte totals (rate × elapsed time = bytes this interval)
                    for tag, (rx, tx) in new_rates.items():
                        prev_rx, prev_tx = self._totals.get(tag, (0.0, 0.0))
                        self._totals[tag] = (prev_rx + rx * dt, prev_tx + tx * dt)

                    # Append to rolling per-tag history and persist
                    for tag, (rx, tx) in new_rates.items():
                        tag_hist = self._history.setdefault(tag, [])
                        tag_hist.append([rx, tx])
                        if len(tag_hist) > self._HISTORY_MAX:
                            del tag_hist[:-self._HISTORY_MAX]
                    self._save_history()

            self._prev_net = (sys_rx, sys_tx, now)
            self._prev_chars = dict(proc_chars)

    def get_rate(self, tag: str) -> tuple[float, float]:
        with self._lock:
            return self._rates.get(tag, (0.0, 0.0))

    def get_totals(self, tag: str) -> tuple[float, float]:
        """Return (rx_total_bytes, tx_total_bytes) accumulated since last reset."""
        with self._lock:
            return self._totals.get(tag, (0.0, 0.0))

    def get_history(self, tag: str) -> list[list[float]]:
        """Return the persisted (rx_bps, tx_bps) samples for *tag* (newest last)."""
        with self._lock:
            return list(self._history.get(tag, []))

    def reset_totals(self, tag: str | None = None) -> None:
        """Reset cumulative counters. Pass tag=None to reset all."""
        with self._lock:
            if tag is None:
                self._totals.clear()
            else:
                self._totals.pop(tag, None)


class _ReconnectMonitor:
    """Background thread that monitors and restarts dropped SSH connections.

    Tracks which connection tags were intentionally started. Every 5 seconds
    it checks socket liveness per tag. When a socket goes down it attempts to
    restart the ControlMaster immediately and on every subsequent poll until it
    succeeds. Once the master is back, all enabled forwards are re-registered.
    """

    INTERVAL = 5.0

    def __init__(self, mgr: "SusOpsManager") -> None:
        self._mgr = mgr
        self._intended: set[str] = set()
        self._socket_was_alive: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start (or restart) the monitor thread. Idempotent.

        Race-safe across stop()/start() cycles: each thread receives its own
        Event reference (passed as an argument) so a freshly-stopped thread on
        its way out can't be confused with a running one — and the new thread
        gets a fresh Event, not a cleared-after-set one.
        """
        # If a thread is actually running (event exists and not set), keep it.
        if (
                self._thread is not None
                and self._thread.is_alive()
                and self._stop_event is not None
                and not self._stop_event.is_set()
        ):
            return
        # Spin up a fresh thread with its own fresh stop event. Any prior
        # thread is still draining its own (already-set) event and will exit
        # naturally — it can't be confused with this new one.
        new_event = threading.Event()
        self._stop_event = new_event
        self._thread = threading.Thread(
            target=self._run, args=(new_event,), daemon=True, name="susops-reconnect"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the monitor thread to exit on its next poll."""
        if self._stop_event is not None:
            self._stop_event.set()

    def mark_running(self, tag: str) -> None:
        with self._lock:
            self._intended.add(tag)
            self._socket_was_alive[tag] = True  # assume alive at start

    def mark_stopped(self, tag: str) -> None:
        with self._lock:
            self._intended.discard(tag)
            self._socket_was_alive.pop(tag, None)

    def _run(self, stop_event: threading.Event) -> None:
        # Bound to *this* thread's event so a subsequent rebind on the
        # manager doesn't accidentally keep us alive after stop().
        while not stop_event.wait(timeout=self.INTERVAL):
            with self._lock:
                tags = list(self._intended)
            for tag in tags:
                try:
                    self._check(tag)
                except Exception:
                    pass

    def _check(self, tag: str) -> None:
        alive = is_socket_alive(tag, self._mgr.workspace)
        with self._lock:
            was_alive = self._socket_was_alive.get(tag, True)
            self._socket_was_alive[tag] = alive

        if alive:
            if not was_alive:
                # Socket came back (reconnect succeeded on a previous poll).
                self._mgr._log(f"[{tag}] Connection restored — re-registering forwards...")
                self._mgr._reregister_forwards(tag)
            return

        # Socket is down. If a stopped-marker file exists, the user
        # intentionally stopped this tag from THIS or ANOTHER process — do
        # not reconnect. The marker is shared across processes so a TUI stop
        # is honoured by the tray's monitor (and vice versa).
        if _stopped_marker_path(self._mgr.workspace, tag).exists():
            # Treat the tag as stopped for our local intended set so we stop
            # emitting reconnect notifications on every poll.
            with self._lock:
                self._intended.discard(tag)
                self._socket_was_alive.pop(tag, None)
            return

        if was_alive:
            self._mgr._log(f"[{tag}] Connection lost — reconnecting...")
            self._mgr._emit("state", {"tag": tag, "running": False, "pid": None, "reconnecting": True})
            self._mgr._notify(f"{self._mgr._process_name} [{tag}]", "Connection lost — reconnecting...")
        # Attempt to restart the master on every poll while the socket is down.
        if self._mgr._try_reconnect(tag):
            with self._lock:
                self._socket_was_alive[tag] = True
            self._mgr._reregister_forwards(tag)


_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validate_tag(tag: str) -> None:
    """Raise ValueError if tag is unsafe for filesystem + display use.

    Bars empty, control chars, slashes, '..', leading dot/dash, and overlong
    inputs. Tags appear in marker filenames, PID files, socket paths, log
    lines — so they must be a strict safe-identifier subset.
    """
    if not isinstance(tag, str):
        raise ValueError(f"Tag must be a string, got {type(tag).__name__}")
    if not _TAG_PATTERN.fullmatch(tag) or ".." in tag:
        raise ValueError(
            f"Invalid tag {tag!r}: must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}} "
            "(no '..', no leading punctuation, no slashes)"
        )


def _stopped_marker_path(workspace: Path, tag: str) -> Path:
    """File whose existence means a tag was *intentionally* stopped.

    Written by any process that calls stop() for a tag; checked by every
    in-process _ReconnectMonitor before
    attempting a reconnect. Cleared by start().

    Without this, a tray and a TUI running side-by-side each have their own
    in-process monitor with its own _intended set — when the user stops a
    tag in one, the other's monitor sees a dead socket and silently brings
    the tunnel back up.
    """
    # Defense in depth: reject traversal even if a caller skipped tag validation.
    if "/" in tag or "\\" in tag or tag in ("", ".", ".."):
        raise ValueError(f"Unsafe tag for marker path: {tag!r}")
    return workspace / "pids" / f"susops-stopped-{tag}.marker"


def _write_stopped_marker(workspace: Path, tag: str) -> None:
    p = _stopped_marker_path(workspace, tag)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except Exception:
        pass


def _clear_stopped_marker(workspace: Path, tag: str) -> None:
    try:
        _stopped_marker_path(workspace, tag).unlink(missing_ok=True)
    except Exception:
        pass


class SusOpsManager:
    """Unified manager for SSH tunnels, PAC server, and file sharing."""

    def __init__(
            self,
            workspace: Path = _WORKSPACE_DEFAULT,
            verbose: bool = False,
            _enable_background_threads: bool = True,
            _skip_restore: bool = False,
            process_name: str = "SusOps",
    ) -> None:
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._process_name = process_name
        self._verbose = verbose

        self.config: SusOpsConfig = load_config(workspace)
        # RLock around every reload→mutate→save window. Two concurrent RPC
        # handlers (each on its own executor thread) would otherwise race:
        # both read, both mutate, last writer wins — losing updates. RLock
        # because some mutators delegate to other mutators (e.g. via
        # set_connection_enabled → _update_pac path).
        self._config_lock = threading.RLock()
        # Separate lock around the PAC server check-then-start. Otherwise
        # parallel start() calls all see is_running() == False, each tries
        # to bind the same port, one wins and the rest log spurious EADDRINUSE
        # / "PAC server is already running" errors.
        self._pac_start_lock = threading.Lock()
        # Per-connection lock around the master check-then-start. Without it,
        # concurrent start(tag=X) calls (RPC runs in executor threads) or a
        # start racing the reconnect monitor both see is_tunnel_running()==False
        # and each spawn a master, overwriting the PID file — see the comment
        # below. Held ONLY around the check + start_master, never across config
        # I/O or downloads (so fetch can't block stop), so it stays a leaf.
        self._start_locks: dict[str, threading.Lock] = {}
        self._start_locks_guard = threading.Lock()
        self._process_mgr = ProcessManager(workspace)
        self._pac_server = PacServer()
        self._status_server = StatusServer()
        self._share_servers: dict[int, tuple[ShareServer, ShareInfo]] = {}
        # Guards _share_servers. Held ONLY around dict snapshot/pop/insert,
        # never across I/O (server.stop(), forwards) or _config_lock — see the
        # _share_* helpers. The dict is touched from RPC executor threads, the
        # auth watcher, and the reconnect monitor concurrently with frontend
        # list_shares polls; an unguarded iteration there raised
        # "dictionary changed size during iteration".
        self._share_lock = threading.Lock()
        self._log_buffer: deque[str] = deque(maxlen=500)
        self._bw_sampler = _BandwidthSampler(
            self._process_mgr, workspace=workspace, on_sample=self._on_bandwidth
        )
        self._start_times: dict[str, float] = {}  # tag -> time.monotonic() when started
        self._reconnect_monitor = _ReconnectMonitor(self)
        if _enable_background_threads:
            self._reconnect_monitor.start()

        self.on_state_change: Callable[[ProcessState], None] | None = None
        self.on_log: Callable[[str], None] | None = None
        self.on_error: Callable[[str], None] | None = None

        if not _skip_restore:
            # Auto-restart PAC server when tunnels are running but this is a
            # fresh process (e.g. TUI restarted without stop_on_quit).
            self._restore_pac()

            if self.config.susops_app.restore_shares_on_start:
                self._restore_shares()

            # Mark already-running connections so the reconnect monitor watches them.
            # Needed when the TUI restarts with connections still live (stop_on_quit=False).
            self._restore_reconnect_monitor()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _log(self, msg: str) -> None:
        # Store the raw, human-readable message. Markup-escaping (so Rich-based
        # frontends like the TUI's RichLog don't interpret "[pi3]" as a tag) is
        # the consumer's job — the tray + plain-text consumers want the raw
        # text so users can copy it and read it normally.
        #
        # Every line gets a `[HH:MM:SS]` prefix so logs are temporally
        # navigable in the TUI + tray viewers, where the user can't easily
        # tell when an entry landed. The shared log_style parser has a
        # dedicated rule that colours the timestamp dim so it doesn't
        # crowd the message.
        stamped = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
        self._log_buffer.append(stamped)
        if self.on_log:
            self.on_log(stamped)

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

    def _debug(self, msg: str) -> None:
        """Log a debug message. Only active when verbose=True.

        In TUI/tray mode the message goes to the Logs tab via on_log.
        In CLI mode (no on_log handler) it is printed to stderr.
        """
        if not self._verbose:
            return
        full = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [debug] {msg}"
        self._log_buffer.append(full)
        if self.on_log:
            self.on_log(full)
        else:
            print(full, file=sys.stderr)

    def _notify(self, title: str, body: str) -> None:
        """Send a desktop notification with the SusOps icon. Best-effort.

        Suppressed entirely when susops_app.notifications_enabled is False
        (tray Settings → Notifications toggle).
        """
        if not self.config.susops_app.notifications_enabled:
            return
        import platform
        from pathlib import Path
        icon = Path(__file__).parent / "assets" / "icon.png"
        try:
            if platform.system() == "Darwin":
                self._notify_macos(title, body, icon)
            elif platform.system() == "Linux":
                self._notify_linux(title, body, icon)
        except Exception:
            pass

    @staticmethod
    def _notify_macos(title: str, body: str, icon: "Path") -> None:
        # NSUserNotification's setContentImage_ puts the SusOps icon on the
        # right side of the banner. The source-app slot (left side) reflects
        # the running process — bundled .app shows SusOps, raw pip install
        # shows Python. osascript can't set either, so it loses both.
        try:
            from Foundation import NSUserNotification, NSUserNotificationCenter  # type: ignore
            from AppKit import NSImage  # type: ignore
            n = NSUserNotification.alloc().init()
            n.setTitle_(title)
            n.setInformativeText_(body)
            if icon.exists():
                img = NSImage.alloc().initWithContentsOfFile_(str(icon))
                if img is not None:
                    n.setContentImage_(img)
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(n)
            return
        except Exception:
            pass
        # Fallback for non-PyObjC environments
        import subprocess
        subprocess.Popen(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _notify_linux(title: str, body: str, icon: "Path") -> None:
        import subprocess
        cmd = ["notify-send"]
        if icon.exists():
            cmd += ["-i", str(icon)]
        cmd += [title, body]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _emit(self, event: str, data: dict) -> None:
        """Emit an SSE event and log it when verbose (bandwidth excluded — too noisy)."""
        self._status_server.emit(event, data)
        if event != "bandwidth":
            self._debug(f"event:{event} {data}")

    def _emit_state(self, state: ProcessState) -> None:
        if self.on_state_change:
            self.on_state_change(state)

    def _reload_config(self) -> None:
        with self._config_lock:
            self.config = load_config(self.workspace)

    def _save(self) -> None:
        with self._config_lock:
            save_config(self.config, self.workspace)

    def _replace_connection(self, updated: Connection) -> None:
        """Swap the connection with the same tag in self.config (does not save)."""
        self.config = self.config.model_copy(
            update={"connections": [updated if c.tag == updated.tag else c for c in self.config.connections]}
        )

    # Auth-pending watcher timeout. 60s gives the user plenty of time to
    # find / unlock the agent UI and approve. After that, the orphan ssh
    # is killed so the connection doesn't stay stuck in a half-state.
    _AUTH_WATCHER_TIMEOUT = 60.0

    def _spawn_auth_watcher(self, conn: "Connection", pid: int) -> None:
        """Wait for the ControlMaster socket asynchronously, then register
        forwards + shares. If the socket never appears (e.g. user dismisses
        the agent prompt) kill the orphan ssh so the connection doesn't
        stay pinned in a "PID alive but never authenticated" state."""

        def _watch() -> None:
            sock = socket_path(conn.tag, self.workspace)
            deadline = time.monotonic() + self._AUTH_WATCHER_TIMEOUT
            while time.monotonic() < deadline:
                if not is_tunnel_running(conn.tag, self._process_mgr):
                    # User cancelled the agent prompt → ssh exited.
                    self._error(
                        f"[{conn.tag}] SSH master exited before authenticating "
                        "(agent prompt cancelled?)"
                    )
                    self._emit("state", {"tag": conn.tag, "running": False, "pid": None})
                    return
                # start_master() unlinks any stale socket before spawning,
                # so socket file existence by itself means ssh authenticated
                # and entered multiplex mode. Skip the ssh -O check
                # subprocess here — it was hammering the daemon's executor
                # pool 5x/sec while in pending state.
                if sock.exists():
                    try:
                        self._finalize_connection_after_auth(conn, pid)
                    except Exception as exc:
                        self._error(f"[{conn.tag}] Post-auth setup failed: {exc}")
                    return
                time.sleep(0.2)
            # Timed out — ssh master is alive but never authenticated.
            self._error(
                f"[{conn.tag}] SSH did not authenticate within "
                f"{int(self._AUTH_WATCHER_TIMEOUT)}s — killing orphan master. "
                "Approve the agent prompt next time."
            )
            try:
                stop_tunnel(conn.tag, self._process_mgr, self.workspace, conn.ssh_host)
            except Exception:
                pass
            self._emit("state", {"tag": conn.tag, "running": False, "pid": None})

        threading.Thread(
            target=_watch, daemon=True, name=f"susops-auth-{conn.tag}",
        ).start()

    def _finalize_connection_after_auth(self, conn: "Connection", pid: int) -> None:
        """Register forwards + start file-share servers + emit "running".
        Called from _spawn_auth_watcher once the ControlMaster socket is up."""
        self._register_forwards(conn)

        # Start HTTP servers for config-only (stopped) shares, then forward
        # slaves for all running share servers belonging to this connection.
        for fs in conn.file_shares:
            if fs.stopped:
                continue
            if self._share_contains(fs.port):
                continue
            fp = Path(fs.file_path)
            if not fp.exists():
                self._log(f"[{conn.tag}] Share '{fs.file_path}' not found — skipping")
                continue
            try:
                srv = ShareServer()
                raw = srv.start(file_path=fp, password=fs.password,
                                port=fs.port, workspace=self.workspace)
                si = ShareInfo(
                    file_path=str(fp), port=raw.port, password=fs.password,
                    url=f"http://localhost:{raw.port}", conn_tag=conn.tag, running=True,
                )
                self._share_put(raw.port, srv, si)
                if raw.port != fs.port:
                    self._update_file_share_port(conn.tag, fs, raw.port)
                self._log(f"[{conn.tag}] Started share '{fp.name}' on port {raw.port}")
                self._emit("share", {
                    "port": raw.port, "file": fp.name, "running": True, "conn_tag": conn.tag,
                })
            except Exception as exc:
                self._error(f"[{conn.tag}] Failed to start share '{fs.file_path}': {exc}")

        for share_port, _server, share_info in self._share_snapshot():
            if share_info.conn_tag == conn.tag:
                fw = PortForward(
                    src_port=share_port, dst_port=share_port,
                    src_addr="localhost", dst_addr="localhost",
                    tag=f"share-{share_port}",
                )
                try:
                    start_forward(conn, fw, "remote", self.workspace)
                except Exception as exc:
                    self._log(f"[{conn.tag}] Share forward {share_port} failed: {exc}")

        self._log(f"[{conn.tag}] Authenticated and ready")
        self._emit("state", {"tag": conn.tag, "running": True, "pid": pid})

    def _register_forwards(self, conn: Connection, error_suffix: str = "") -> None:
        """Register all enabled forwards for conn through its ControlMaster.

        Iterates local then remote. TCP forwards use ``ssh -O forward``; UDP
        forwards start socat processes. Errors are user-visible but do not
        abort registration of other forwards.
        """
        for direction, fwds in (("local", conn.forwards.local), ("remote", conn.forwards.remote)):
            for fw in fwds:
                if not fw.enabled:
                    continue
                try:
                    if fw.tcp:
                        start_forward(conn, fw, direction, self.workspace)
                    if fw.udp:
                        start_udp_forward(conn, fw, direction, self._process_mgr, self.workspace)
                except Exception as exc:
                    suffix = f" {error_suffix}" if error_suffix else ""
                    self._error(f"[{conn.tag}] Forward {fw.src_port} failed{suffix}: {exc}")

    def _maybe_start_forward_live(self, conn_tag: str, fw: PortForward, direction: str) -> None:
        """Start a forward immediately if the connection master is currently running."""
        conn = get_connection(self.config, conn_tag)
        if not conn:
            return
        if not (is_tunnel_running(conn_tag, self._process_mgr) or is_socket_alive(conn_tag, self.workspace)):
            return
        try:
            if fw.tcp:
                start_forward(conn, fw, direction, self.workspace)
            if fw.udp:
                start_udp_forward(conn, fw, direction, self._process_mgr, self.workspace)
            self._emit("forward", {
                "tag": conn_tag,
                "fw_tag": fw.tag or f"{direction}-{fw.src_port}",
                "direction": direction,
                "running": True,
            })
        except Exception as exc:
            self._log(f"[{conn_tag}] Could not start forward: {exc}")

    def _on_bandwidth(self, tag: str, rx: float, tx: float) -> None:
        self._status_server.emit("bandwidth", {"tag": tag, "rx_bps": rx, "tx_bps": tx})

    def _connection_status(self, conn: Connection) -> ConnectionStatus:
        running = is_tunnel_running(conn.tag, self._process_mgr)
        # Fall back to socket liveness when PID file is stale (zombie reaped,
        # or master restarted outside our control).
        if not running and is_socket_alive(conn.tag, self.workspace):
            running = True
            # Recover the PID so the dashboard can show it; track_existing
            # stamps create_time so the recovered PID isn't reuse-vulnerable.
            recovered = find_master_pid(conn.tag, self.workspace)
            if recovered:
                name = f"{SSH_PROCESS_PREFIX}-{conn.tag}"
                self._process_mgr.track_existing(name, recovered)
        # Pending covers two scenarios:
        #   1. ssh master alive but socket not yet up → waiting on agent prompt
        #   2. ssh master not alive but the reconnect monitor still intends
        #      this tag → previous attempt failed (bad key, host down, ...)
        #      and we're between retry ticks. Without this the UI flips to
        #      "stopped" between retries even though the monitor is actively
        #      reconnecting every 5s.
        pending = False
        if running:
            sock = socket_path(conn.tag, self.workspace)
            pending = not sock.exists()
        else:
            try:
                with self._reconnect_monitor._lock:
                    if conn.tag in self._reconnect_monitor._intended:
                        pending = True
            except Exception:
                pass
        pid = self._process_mgr.get_pid(f"{SSH_PROCESS_PREFIX}-{conn.tag}")
        return ConnectionStatus(
            tag=conn.tag,
            running=running,
            pid=pid,
            socks_port=conn.socks_proxy_port,
            enabled=conn.enabled,
            pending=pending,
        )

    def _ensure_socks_port(self, conn: Connection) -> Connection:
        if conn.socks_proxy_port != 0:
            return conn
        port = get_random_free_port()
        updated = conn.model_copy(update={"socks_proxy_port": port})
        self._replace_connection(updated)
        self._save()
        self._log(f"[{conn.tag}] Assigned SOCKS port {port}")
        return updated

    # ------------------------------------------------------------------ #
    # PAC port-file helpers (cross-process PAC status detection)
    # ------------------------------------------------------------------ #

    @property
    def _pac_port_file(self) -> "Path":
        return self.workspace / "pids" / "susops-pac.port"

    def _write_pac_port_file(self, port: int) -> None:
        self._pac_port_file.parent.mkdir(parents=True, exist_ok=True)
        self._pac_port_file.write_text(str(port))

    def _remove_pac_port_file(self) -> None:
        self._pac_port_file.unlink(missing_ok=True)

    def _read_pac_port_file(self) -> int:
        try:
            return int(self._pac_port_file.read_text().strip())
        except Exception:
            return 0

    @staticmethod
    def _probe_port(port: int) -> bool:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False

    @staticmethod
    def _fire_http(url: str, timeout: int = 2) -> None:
        """POST to url, ignoring all errors (fire-and-forget)."""
        try:
            urllib.request.urlopen(url, data=b"", timeout=timeout)
        except Exception:
            pass

    def _ensure_pac_port(self) -> int:
        if self.config.pac_server_port != 0:
            return self.config.pac_server_port
        port = get_random_free_port()
        self.config = self.config.model_copy(update={"pac_server_port": port})
        self._save()
        return port

    def _compute_state(
            self,
            statuses: tuple[ConnectionStatus, ...] | None = None,
            pac_running: bool | None = None,
    ) -> ProcessState:
        if statuses is None:
            statuses = tuple(self._connection_status(c) for c in self.config.connections)
        if pac_running is None:
            pac_running = self._pac_server.is_running()
        if not self.config.connections:
            return ProcessState.STOPPED
        # Disabled connections shouldn't count toward "partial" — the user
        # explicitly took them out of rotation. State is computed only over
        # connections the user wants up.
        enabled_statuses = [s for s in statuses if s.enabled]
        if not enabled_statuses:
            # All connections are disabled — there's nothing to run, so we're
            # stopped (PAC is irrelevant in this case but we don't actively
            # demote RUNNING → STOPPED if it happens to be up).
            return ProcessState.STOPPED if not pac_running else ProcessState.STOPPED_PARTIALLY
        # Any pending connection (waiting on agent prompt, or between
        # reconnect retries) means we're not fully up, even if the ssh PID
        # is alive. Show STOPPED_PARTIALLY so the tray icon flips to orange.
        if any(getattr(s, "pending", False) for s in enabled_statuses):
            return ProcessState.STOPPED_PARTIALLY
        running_count = sum(1 for s in enabled_statuses if s.running)
        total = len(enabled_statuses)
        if running_count == total and pac_running:
            return ProcessState.RUNNING
        if running_count == 0 and not pac_running:
            return ProcessState.STOPPED
        return ProcessState.STOPPED_PARTIALLY

    # ------------------------------------------------------------------ #
    # Share persistence helpers
    # ------------------------------------------------------------------ #

    def _restore_pac(self) -> None:
        """Restart the PAC server if SSH tunnels are running but PAC is dead.

        Called on __init__ so the PAC server is recovered after a TUI restart
        without stop_on_quit (the daemon thread died with the previous process).
        Uses both PID-file and socket-liveness checks so a stale PID file
        (daemon thread deleted it mid-shutdown) doesn't prevent PAC restore.
        """
        any_tunnel = False
        for conn in self.config.connections:
            if is_tunnel_running(conn.tag, self._process_mgr):
                any_tunnel = True
            elif is_socket_alive(conn.tag, self.workspace):
                any_tunnel = True
                # PID file is stale — recover PID so future checks don't re-enter here
                recovered = find_master_pid(conn.tag, self.workspace)
                if recovered:
                    name = f"{SSH_PROCESS_PREFIX}-{conn.tag}"
                    self._process_mgr.track_existing(name, recovered)
        if not any_tunnel:
            return
        port = self.config.pac_server_port
        if not port:
            # Port unknown — let start() allocate one when user next calls start
            return
        if self._probe_port(port):
            # A cross-process PAC server is still serving (e.g. tray app)
            self._log(f"PAC server already running (cross-process) on port {port}")
            return
        try:
            pac_path = write_pac_file(self.config, self.workspace, active_tags=self._active_tags())
            self._pac_server.start(port, pac_path)
            self._write_pac_port_file(port)
            self._log(f"PAC server restored on port {port}")
        except Exception as exc:
            self._log(f"PAC restore failed: {exc}")

    def _restore_reconnect_monitor(self) -> None:
        """Mark already-live connections so _ReconnectMonitor watches them on startup.

        Called after __init__ when connections may already be running from a
        previous session (stop_on_quit=False).  Without this, mark_running() is
        never called for adopted connections and the monitor's _intended set
        stays empty — watching nothing until the next explicit start().
        """
        for conn in self.config.connections:
            if not conn.enabled:
                continue
            if is_tunnel_running(conn.tag, self._process_mgr) or is_socket_alive(conn.tag, self.workspace):
                self._reconnect_monitor.mark_running(conn.tag)

    def _restore_shares(self) -> None:
        """Restart share servers for persisted FileShare entries whose connection is running.

        Skips entries the user manually stopped (stopped=True).
        """
        for conn in self.config.connections:
            if not is_tunnel_running(conn.tag, self._process_mgr):
                continue  # shares are meaningless without a live tunnel
            for fs in conn.file_shares:
                if fs.stopped:
                    continue  # user manually stopped this share — don't auto-restart
                file_path = Path(fs.file_path)
                if not file_path.exists():
                    self._log(
                        f"[{conn.tag}] Share '{fs.file_path}' not found on disk — skipping restore"
                    )
                    continue
                try:
                    server = ShareServer()
                    info_raw = server.start(
                        file_path=file_path,
                        password=fs.password,
                        port=fs.port,
                        workspace=self.workspace,
                    )
                    # Write back the actual port if it changed
                    actual_port = info_raw.port
                    info = ShareInfo(
                        file_path=str(file_path),
                        port=actual_port,
                        password=fs.password,
                        url=f"http://localhost:{actual_port}",
                        conn_tag=conn.tag,
                        running=True,
                    )
                    self._share_put(actual_port, server, info)
                    if actual_port != fs.port:
                        self._update_file_share_port(conn.tag, fs, actual_port)
                    self._log(f"[{conn.tag}] Restored share '{file_path.name}' on port {actual_port}")
                except Exception as exc:
                    self._log(f"[{conn.tag}] Failed to restore share '{fs.file_path}': {exc}")

    def _update_file_share_port(
            self, conn_tag: str, fs: FileShare, new_port: int
    ) -> None:
        """Update the stored port for a FileShare entry in config."""
        conn = get_connection(self.config, conn_tag)
        if conn is None:
            return
        updated_shares = [
            fs.model_copy(update={"port": new_port}) if f.file_path == fs.file_path else f
            for f in conn.file_shares
        ]
        self._replace_connection(conn.model_copy(update={"file_shares": updated_shares}))
        self._save()

    def _add_file_share_to_config(
            self, conn_tag: str, file_path: str, password: str, port: int
    ) -> None:
        with self._config_lock:
            self._reload_config()
            conn = get_connection(self.config, conn_tag)
            if conn is None:
                return
            # Update existing entry (clear stopped flag on re-share) or append new
            existing = [f for f in conn.file_shares if f.file_path == file_path]
            if existing:
                new_shares = [
                    fs.model_copy(update={"password": password, "port": port, "stopped": False})
                    if fs.file_path == file_path else fs
                    for fs in conn.file_shares
                ]
            else:
                new_shares = list(conn.file_shares) + [
                    FileShare(file_path=file_path, password=password, port=port)
                ]
            self._replace_connection(conn.model_copy(update={"file_shares": new_shares}))
            self._save()

    def _remove_file_share_from_config(self, port: int) -> None:
        with self._config_lock:
            self._reload_config()
            new_conns = []
            for conn in self.config.connections:
                updated_shares = [f for f in conn.file_shares if f.port != port]
                if len(updated_shares) != len(conn.file_shares):
                    new_conns.append(conn.model_copy(update={"file_shares": updated_shares}))
                else:
                    new_conns.append(conn)
            self.config = self.config.model_copy(update={"connections": new_conns})
            self._save()

    def _set_file_share_stopped(self, port: int, stopped: bool) -> None:
        """Update the stopped flag on a persisted FileShare entry.

        Reads fresh under _config_lock so a concurrent config mutation (the
        tray poll's list_shares, another frontend) cannot be clobbered by a
        stale in-memory write. Matches the read-modify-write pattern used by
        the other config mutators.
        """
        with self._config_lock:
            self._reload_config()
            new_conns = []
            for conn in self.config.connections:
                updated = [
                    fs.model_copy(update={"stopped": stopped}) if fs.port == port else fs
                    for fs in conn.file_shares
                ]
                new_conns.append(conn.model_copy(update={"file_shares": updated}))
            self.config = self.config.model_copy(update={"connections": new_conns})
            self._save()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self, tag: str | None = None) -> StartResult:
        # Re-spin the in-process monitor in case a prior stop() halted it.
        self._reconnect_monitor.start()
        self._reload_config()
        if tag is not None:
            conn = get_connection(self.config, tag)
            if conn is None:
                raise ValueError(f"Connection '{tag}' not found")
            connections = [conn]
        else:
            connections = list(self.config.connections)

        if not connections:
            return StartResult(success=False, message="No connections configured")

        statuses = []
        errors = []

        for conn in connections:
            # User asked to start this — clear any "intentionally stopped"
            # marker so reconnect monitors (this process and any other) will
            # watch it again.
            _clear_stopped_marker(self.workspace, conn.tag)
            if not conn.enabled:
                self._log(f"[{conn.tag}] Disabled — skipping")
                statuses.append(self._connection_status(conn))
                continue
            # Serialise the check-then-start so two concurrent start()s (or a
            # start racing the reconnect monitor) can't both spawn a master.
            with self._tag_start_lock(conn.tag):
              if is_tunnel_running(conn.tag, self._process_mgr):
                self._log(f"[{conn.tag}] Already running")
                statuses.append(self._connection_status(conn))
                continue
              if is_socket_alive(conn.tag, self.workspace):
                # ControlMaster is alive but our PID file is stale (e.g.
                # process was a zombie that was reaped). Don't start a
                # second master — re-adopt by re-tracking the socket owner.
                self._log(f"[{conn.tag}] Socket alive but PID stale — skipping new master")
                statuses.append(self._connection_status(conn))
                continue
              try:
                conn = self._ensure_socks_port(conn)
                pid = start_master(conn, self._process_mgr, self.workspace)
                self._log(f"[{conn.tag}] Master started (PID {pid})")

                # Forwards and share-forwards can only register once the
                # ControlMaster socket exists — i.e. after ssh has finished
                # authenticating. With a 1Password / Bitwarden prompt that
                # can take 30s+. Watch in the background so start() returns
                # immediately; the watcher emits state events as the SSH
                # session moves through pending → running → (or stopped on
                # timeout / cancelled prompt).
                self._spawn_auth_watcher(conn, pid)

                statuses.append(ConnectionStatus(
                    tag=conn.tag, running=True, pid=pid,
                    socks_port=conn.socks_proxy_port,
                ))
                self._start_times[conn.tag] = time.monotonic()
                self._emit("state", {"tag": conn.tag, "running": True, "pid": pid})
                self._reconnect_monitor.mark_running(conn.tag)
              except Exception as exc:
                log_path = self.workspace / "logs" / f"susops-ssh-{conn.tag}.log"
                tail = ""
                if log_path.exists():
                    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
                    if lines:
                        tail = "\n  " + "\n  ".join(lines[-5:])
                msg = f"[{conn.tag}] Failed: {exc}{tail}"
                self._error(msg)
                errors.append(msg)
                statuses.append(ConnectionStatus(tag=conn.tag, running=False))
                self._emit("state", {"tag": conn.tag, "running": False, "pid": None})

        with self._pac_start_lock:
            if not self._pac_server.is_running():
                cross_port = self._read_pac_port_file()
                if cross_port and self._probe_port(cross_port):
                    self._log(f"PAC server already running (cross-process) on port {cross_port}")
                    self._update_pac()
                else:
                    if cross_port:
                        self._remove_pac_port_file()
                    try:
                        self._reload_config()
                        pac_port = self._ensure_pac_port()
                        pac_path = write_pac_file(self.config, self.workspace, active_tags=self._active_tags())
                        self._pac_server.start(pac_port, pac_path)
                        self._write_pac_port_file(self._pac_server.get_port())
                        self._log(f"PAC server started on port {pac_port}")
                    except Exception as exc:
                        self._error(f"PAC server failed: {exc}")
                        errors.append(f"PAC server failed: {exc}")
            else:
                self._update_pac()

        # Start status server if not already running
        self.ensure_sse_status_server()

        self._emit_state(self._compute_state())
        return StartResult(
            success=not errors,
            message="; ".join(errors) if errors else "Started",
            connection_statuses=tuple(statuses),
        )

    def stop_quick(self) -> None:
        """Non-blocking stop for TUI quit: signal all external processes immediately.

        Sends SIGTERM to every tracked PID at once (no per-process waiting),
        then shuts down in-process async servers. Used by the TUI so that all
        connections are signaled before the Python process exits, regardless
        of how many connections are configured.
        """
        self._reconnect_monitor.stop()
        self._process_mgr.kill_all()
        for _p, server, _info in self._share_pop_all():
            try:
                server.stop()
            except Exception:
                pass
        if self._pac_server.is_running():
            try:
                self._pac_server.stop()
            except Exception:
                pass

    def ensure_sse_status_server(self) -> int | None:
        """Start the SSE status server if not already running.

        Returns the bound port on success, or None if startup failed. Persists
        an auto-allocated port back to config so subsequent daemon spawns
        reuse it (which matters when frontends cache the URL).
        """
        if self._status_server.is_running():
            return self._status_server.get_port()
        try:
            status_port = self.config.status_server_port
            actual_port = self._status_server.start(port=status_port)
            if actual_port != status_port and status_port == 0:
                self.config = self.config.model_copy(
                    update={"status_server_port": actual_port}
                )
                self._save()
            return actual_port
        except Exception as exc:
            self._log(f"SSE status server failed: {exc}")
            return None

    def is_idle(self) -> bool:
        """Return True when the daemon has no work to do.

        Used to drive the services daemon's idle-shutdown logic: when the last
        SSE client disconnects AND the daemon is idle, the process exits and
        the next frontend respawns it (<1 s).

        Idle means:
          - no SSH masters or forwards tracked under our pids/ dir (anything
            except the daemon's own susops-services.pid)
          - no share servers running
          - PAC server not running
          - reconnect monitor not watching any connection
        """
        from susops.core.process import ProcessManager

        blacklist = ProcessManager._KILL_ALL_BLACKLIST  # type: ignore[attr-defined]
        try:
            tracked = [
                p for p in self._process_mgr._pids_dir.glob("*.pid")
                if p.stem not in blacklist
            ]
        except Exception:
            tracked = []
        if tracked:
            return False
        if self._share_any():
            return False
        if self._pac_server.is_running():
            return False
        try:
            with self._reconnect_monitor._lock:
                if self._reconnect_monitor._intended:
                    return False
        except Exception:
            pass
        return True

    def reconnect_monitor_info(self) -> dict:
        """Return display info about the current reconnect monitor state.

        Returns a dict with:
          thread_alive   – in-process _ReconnectMonitor thread is running
          daemon_running – always False (background reconnect daemon removed)
          watching       – set of connection tags currently being monitored
        """
        t = self._reconnect_monitor._thread
        thread_alive = t is not None and t.is_alive()
        with self._reconnect_monitor._lock:
            watching = set(self._reconnect_monitor._intended)
        return {
            "thread_alive": thread_alive,
            "daemon_running": False,
            "watching": watching,
        }

    def process_info(self) -> dict:
        """Return subprocess info grouped by connection for ``susops ps`` display.

        TCP forwards have no separate processes — they are port bindings on the
        ControlMaster.  They are sourced from config so they always appear.
        UDP forwards have socat processes; their ``lsocat`` PID is shown.

        Returns a dict with:
          conn_children  – {tag: [{display, pid, running}, ...]}
          reconnect      – {pid, running, thread_alive, daemon_running}
        """
        # Index UDP lsocat processes by (conn_tag, fw_tag) for fast lookup.
        # Only lsocat is shown — it is the entry-point process for both local
        # and remote UDP forwards and best represents "is this forward active".
        udp_lsocat: dict[tuple, dict] = {}
        for name, proc_running in self._process_mgr.status_all().items():
            if not name.startswith(UDP_PROCESS_PREFIX + "-"):
                continue
            remainder = name[len(UDP_PROCESS_PREFIX) + 1:]
            if not remainder.endswith("-lsocat"):
                continue
            for conn in self.config.connections:
                if remainder.startswith(conn.tag + "-"):
                    fw_tag = remainder[len(conn.tag) + 1: -len("-lsocat")]
                    pid = self._process_mgr.get_pid(name)
                    udp_lsocat[(conn.tag, fw_tag)] = {"pid": pid, "running": proc_running}
                    break

        conn_children: dict[str, list[dict]] = {}
        for conn in self.config.connections:
            # TCP forwards are bound to the ControlMaster and never have their
            # own PID files.  Mark them running whenever the master is up.
            master_up = is_tunnel_running(conn.tag, self._process_mgr)
            children: list[dict] = []
            for direction, fwds in (("local", conn.forwards.local), ("remote", conn.forwards.remote)):
                for fw in fwds:
                    if not fw.enabled:
                        continue
                    src = f"{fw.src_addr}:{fw.src_port}" if fw.src_addr != "localhost" else str(fw.src_port)
                    dst = f"{fw.dst_addr}:{fw.dst_port}"
                    label = f" [{fw.tag}]" if fw.tag else ""
                    fw_tag = fw.tag if fw.tag else f"{direction[0]}-{fw.src_port}"
                    if fw.tcp:
                        children.append({
                            "display": f"fwd {direction}  {src} → {dst}{label}",
                            "pid": None,
                            "running": master_up,
                        })
                    if fw.udp:
                        proc = udp_lsocat.get((conn.tag, fw_tag))
                        children.append({
                            "display": f"udp {direction}  {src} → {dst}{label}",
                            "pid": proc["pid"] if proc else None,
                            "running": proc["running"] if proc else False,
                        })
            if children:
                conn_children[conn.tag] = children

        reconnect_info = self.reconnect_monitor_info()
        return {
            "conn_children": conn_children,
            "reconnect": {
                "pid": None,
                "running": False,
                "thread_alive": reconnect_info["thread_alive"],
                "daemon_running": False,
            },
        }

    def _active_tags(self) -> set[str]:
        """Return the set of connection tags that are currently running."""
        return {
            conn.tag for conn in self.config.connections
            if is_tunnel_running(conn.tag, self._process_mgr) or is_socket_alive(conn.tag, self.workspace)
        }

    def _tag_start_lock(self, tag: str) -> threading.Lock:
        """Get-or-create the per-connection start lock for tag."""
        with self._start_locks_guard:
            lock = self._start_locks.get(tag)
            if lock is None:
                lock = threading.Lock()
                self._start_locks[tag] = lock
            return lock

    def _start_master_only(self, conn_tag: str) -> None:
        """Start only the SSH ControlMaster for conn_tag — no forwards, PAC, or shares.

        Used by fetch() to establish connectivity without touching any other
        configured services.  Returns immediately once the socket appears.
        """
        conn = get_connection(self.config, conn_tag)
        if conn is None:
            raise ValueError(f"Connection '{conn_tag}' not found")
        # Lock only the check-then-start, not the wait-for-socket loop below,
        # so fetch never holds the lock across its (slow) download.
        with self._tag_start_lock(conn_tag):
            if is_tunnel_running(conn_tag, self._process_mgr) or is_socket_alive(conn_tag, self.workspace):
                return  # already up
            conn = self._ensure_socks_port(conn)
            pid = start_master(conn, self._process_mgr, self.workspace)
            self._log(f"[{conn_tag}] Master started for fetch (PID {pid})")
        sock = socket_path(conn_tag, self.workspace)
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.1)

    def _try_reconnect(self, tag: str) -> bool:
        """Attempt to restart the ControlMaster for a connection that went down.

        Returns True only once the socket is confirmed alive — not merely when
        the SSH process has spawned. This prevents the monitor from calling
        _reregister_forwards (and sending "Connection restored") for a master
        that started but immediately failed (e.g. WiFi off, host unreachable).

        Called by _ReconnectMonitor on every poll while the socket is down.
        """
        # Serialise the check-then-start under the per-tag lock so the monitor
        # and a concurrent start() can't both spawn a master. Release it before
        # the (slow) wait-for-socket loop so a manual start isn't blocked.
        with self._tag_start_lock(tag):
            if is_tunnel_running(tag, self._process_mgr) or is_socket_alive(tag, self.workspace):
                return True
            self._reload_config()
            conn = get_connection(self.config, tag)
            if conn is None or not conn.enabled:
                return False
            try:
                conn = self._ensure_socks_port(conn)
                pid = start_master(conn, self._process_mgr, self.workspace)
            except Exception as exc:
                self._log(f"[{tag}] Reconnect attempt failed: {exc}")
                return False
        self._log(f"[{tag}] Reconnect started (PID {pid}), waiting for socket…")
        # Wait up to 10 s for the socket file to appear, then verify it is
        # actually responding.  If SSH fails (host unreachable, auth error)
        # the process exits and the socket never becomes alive — return False
        # so the caller does not prematurely declare success.
        sock = socket_path(tag, self.workspace)
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.1)
        if not is_socket_alive(tag, self.workspace):
            self._log(f"[{tag}] Reconnect started but socket not ready — will retry")
            return False
        self._log(f"[{tag}] Reconnected (PID {pid})")
        return True

    def _reregister_forwards(self, tag: str) -> None:
        """Re-register all enabled forwards

        Called by _ReconnectMonitor when the ControlMaster socket comes back
        alive. The fresh master starts with no forwards registered — all enabled
        TCP forwards are re-registered via ``ssh -O forward``, stale UDP
        processes are restarted, and active share forwards are re-registered.
        """
        self._reload_config()
        conn = get_connection(self.config, tag)
        if conn is None:
            return

        # Clean up stale UDP processes — they died with the previous master.
        stop_all_udp_forwards_for_connection(tag, self._process_mgr)

        self._register_forwards(conn, error_suffix="to re-register")

        for share_port, _server, share_info in self._share_snapshot():
            if share_info.conn_tag != tag:
                continue
            fw = PortForward(
                src_port=share_port, dst_port=share_port,
                src_addr="localhost", dst_addr="localhost",
                tag=f"share-{share_port}",
            )
            try:
                start_forward(conn, fw, "remote", self.workspace)
            except Exception as exc:
                self._log(f"[{tag}] Share forward {share_port} failed to re-register: {exc}")

        pid = self._process_mgr.get_pid(f"{SSH_PROCESS_PREFIX}-{tag}")
        self._emit("state", {"tag": tag, "running": True, "pid": pid})
        self._notify(f"{self._process_name} [{tag}]", "Connection restored")

    def stop(self, keep_ports: bool = False, tag: str | None = None) -> StopResult:
        self._reload_config()
        errors = []

        ephemeral = self.config.susops_app.ephemeral_ports
        if tag is not None:
            conn = get_connection(self.config, tag)
            if conn is None:
                raise ValueError(f"Connection '{tag}' not found")
            connections = [conn]
        else:
            connections = list(self.config.connections)
        for conn in connections:
            try:
                # Write the stopped marker BEFORE killing the tunnel — that
                # way a parallel process's monitor (e.g. tray watching while
                # TUI is stopping) sees the marker the moment the socket dies
                # and won't try to reconnect.
                _write_stopped_marker(self.workspace, conn.tag)
                # Always tell the reconnect monitor we don't want this tag
                # watched anymore — even if stop_tunnel returns False because
                # the socket was already down (e.g. the monitor was
                # mid-reconnect-attempt when the user clicked Stop). Without
                # this, _intended still contains the tag and the monitor
                # keeps reviving the connection after stop.
                self._reconnect_monitor.mark_stopped(conn.tag)
                if stop_tunnel(conn.tag, self._process_mgr, self.workspace, conn.ssh_host):
                    self._log(f"[{conn.tag}] Stopped")
                    self._bw_sampler.reset_totals(conn.tag)
                    self._start_times.pop(conn.tag, None)
                    self._emit("state", {"tag": conn.tag, "running": False, "pid": None})
                stop_all_udp_forwards_for_connection(conn.tag, self._process_mgr)
                if not keep_ports and ephemeral and conn.socks_proxy_port != 0:
                    self._replace_connection(conn.model_copy(update={"socks_proxy_port": 0}))
            except Exception as exc:
                errors.append(f"[{conn.tag}] {exc}")

        # Stop share servers for the affected connections
        stopped_tags = {c.tag for c in connections}
        for p, server, info in self._share_snapshot():
            if info.conn_tag in stopped_tags:
                try:
                    server.stop()
                    self._log(f"File share on port {p} stopped")
                    self._emit("share", {
                        "port": p,
                        "file": Path(info.file_path).name,
                        "running": False,
                        "conn_tag": info.conn_tag,
                    })
                    self._share_pop(p)
                except Exception as exc:
                    errors.append(f"Share {p}: {exc}")

        if tag is None:
            self._stop_pac_server(errors, keep_ports, ephemeral)

        # Regenerate PAC (or stop it) when stopping a single connection
        if tag is not None:
            remaining = self._active_tags()
            if not remaining:
                # Last connection stopped — write empty PAC first for consistency, then shut down
                write_pac_file(self.config, self.workspace, active_tags=set())
                self._stop_pac_server(errors, keep_ports, ephemeral, context="no active connections")
            else:
                self._update_pac()

        # Halt the in-process monitor if there's nothing left to watch.
        # Covers two cases: a full stop (tag=None), and a per-tag stop that
        # happened to be the last live tag. Without this, the daemon's
        # status would keep showing "● Reconnect" with an empty intended
        # set, polling every 5 s for nothing.
        with self._reconnect_monitor._lock:
            still_watching = bool(self._reconnect_monitor._intended)
        if not still_watching:
            self._reconnect_monitor.stop()

        self._save()
        final_state = self._compute_state()
        self._emit_state(final_state)
        # SSE event so cross-process frontends recompute their icon. The
        # per-connection emit at stop_tunnel() time fired BEFORE PAC was
        # stopped, so any status() racing with it would see PAC still
        # running and return STOPPED_PARTIALLY. This second emit fires
        # after PAC has been torn down so the recompute settles on the
        # final aggregate.
        self._emit("state", {"aggregate": final_state.value})
        return StopResult(
            success=not errors,
            message="; ".join(errors) if errors else "Stopped",
        )

    def restart(self, tag: str | None = None) -> StartResult:
        self.stop(keep_ports=True, tag=tag)
        time.sleep(0.5)
        return self.start(tag)

    def status(self) -> StatusResult:
        self._reload_config()
        statuses = tuple(self._connection_status(c) for c in self.config.connections)
        pac_running = self._pac_server.is_running()
        pac_port = self._pac_server.get_port()
        if not pac_running:
            pac_port = pac_port or self._read_pac_port_file()
            if pac_port:
                pac_running = self._probe_port(pac_port)
        pac_port = pac_port or self.config.pac_server_port
        return StatusResult(
            state=self._compute_state(statuses, pac_running),
            connection_statuses=statuses,
            pac_running=pac_running,
            pac_port=pac_port,
        )

    # ------------------------------------------------------------------ #
    # Connection CRUD
    # ------------------------------------------------------------------ #

    def add_connection(self, tag: str, ssh_host: str, socks_port: int = 0) -> Connection:
        _validate_tag(tag)
        if not (isinstance(ssh_host, str) and ssh_host.strip()):
            raise ValueError("ssh_host must be a non-empty string")
        if socks_port != 0 and not validate_port(socks_port, allow_zero=True):
            raise ValueError(f"Invalid socks_port {socks_port}: must be 0 or 1-65535")
        with self._config_lock:
            self._reload_config()
            if get_connection(self.config, tag) is not None:
                raise ValueError(f"Connection '{tag}' already exists")
            conn = Connection(tag=tag, ssh_host=ssh_host, socks_proxy_port=socks_port)
            self.config = self.config.model_copy(
                update={"connections": list(self.config.connections) + [conn]}
            )
            self._save()
        _clear_stopped_marker(self.workspace, tag)
        self._log(f"Added connection '{tag}' → {ssh_host}")
        return conn

    def remove_connection(self, tag: str) -> None:
        with self._config_lock:
            self._reload_config()
            conn = get_connection(self.config, tag)
            if conn is None:
                raise ValueError(f"Connection '{tag}' not found")
            _write_stopped_marker(self.workspace, tag)
            stop_tunnel(tag, self._process_mgr, self.workspace, conn.ssh_host)
            stop_all_udp_forwards_for_connection(tag, self._process_mgr)
            self._reconnect_monitor.mark_stopped(tag)
            self.config = self.config.model_copy(
                update={"connections": [c for c in self.config.connections if c.tag != tag]}
            )
            self._save()
        self._log(f"Removed connection '{tag}'")

    def set_connection_enabled(self, tag: str, enabled: bool) -> None:
        with self._config_lock:
            self._reload_config()
            conn = get_connection(self.config, tag)
            if conn is None:
                raise ValueError(f"Connection '{tag}' not found")
            self._replace_connection(conn.model_copy(update={"enabled": enabled}))
            self._save()
        self._log(f"[{tag}] {'enabled' if enabled else 'disabled'}")
        if enabled:
            # Re-enabling clears any leftover "stopped" marker so reconnect
            # monitors will watch this tag again.
            _clear_stopped_marker(self.workspace, tag)
        else:
            # Disabling = intentional stop. Write the marker BEFORE killing the
            # tunnel so a parallel process's monitor doesn't try to revive it.
            _write_stopped_marker(self.workspace, tag)
            if is_tunnel_running(tag, self._process_mgr) or is_socket_alive(tag, self.workspace):
                stop_tunnel(tag, self._process_mgr, self.workspace, conn.ssh_host)
                stop_all_udp_forwards_for_connection(tag, self._process_mgr)
                self._bw_sampler.reset_totals(tag)
                self._start_times.pop(tag, None)
                self._reconnect_monitor.mark_stopped(tag)
                if not self._active_tags():
                    write_pac_file(self.config, self.workspace, active_tags=set())
                    self._stop_pac_server(
                        errors=[], keep_ports=True, ephemeral=False,
                        context="last enabled connection disabled",
                    )
                else:
                    self._update_pac()
        # Always emit a state event so frontends recompute their aggregate
        # icon — the enabled-set drives _compute_state's "partial vs running"
        # decision even when the per-connection running status didn't change.
        running = is_tunnel_running(tag, self._process_mgr)
        pid = self._process_mgr.get_pid(f"{SSH_PROCESS_PREFIX}-{tag}") if running else None
        self._emit("state", {"tag": tag, "running": running, "pid": pid})

    def update_connection(
            self,
            tag: str,
            *,
            new_tag: str | None = None,
            ssh_host: str | None = None,
            socks_proxy_port: int | None = None,
            restart: bool = True,
    ) -> Connection:
        """Edit a connection's tag / ssh_host / socks_proxy_port in place.

        Unlike remove + re-add, this preserves the connection's children
        (forwards, pac_hosts, pac_hosts_disabled, file_shares, enabled) via
        model_copy — remove_connection cascades and would drop them.

        If the connection was running and restart=True, the tunnel is torn
        down under the OLD tag and brought back up under the NEW config so a
        renamed/re-pointed connection picks up the change immediately.
        """
        with self._config_lock:
            self._reload_config()
            conn = get_connection(self.config, tag)
            if conn is None:
                raise ValueError(f"Connection '{tag}' not found")

            target_tag = (new_tag if new_tag is not None else tag).strip()
            if not target_tag:
                raise ValueError("Tag must be a non-empty string")
            if target_tag != tag:
                _validate_tag(target_tag)
                if get_connection(self.config, target_tag) is not None:
                    raise ValueError(f"Connection '{target_tag}' already exists")

            new_host = (ssh_host if ssh_host is not None else conn.ssh_host).strip()
            if not new_host:
                raise ValueError("ssh_host must be a non-empty string")

            new_port = conn.socks_proxy_port if socks_proxy_port is None else int(socks_proxy_port)
            if not validate_port(new_port, allow_zero=True):
                raise ValueError(f"Invalid socks_proxy_port {new_port}: must be 0 or 1-65535")
            if new_port != 0 and new_port != conn.socks_proxy_port and not is_port_free(new_port):
                raise ValueError(f"SOCKS port {new_port} is already in use")

            was_running = (
                    is_tunnel_running(tag, self._process_mgr)
                    or is_socket_alive(tag, self.workspace)
            )

            updated = conn.model_copy(update={
                "tag": target_tag,
                "ssh_host": new_host,
                "socks_proxy_port": new_port,
            })
            self.config = self.config.model_copy(update={
                "connections": [updated if c.tag == tag else c for c in self.config.connections]
            })
            self._save()

        self._update_pac()
        if was_running and restart:
            # Tear down under the OLD tag (its PID/socket/shares/forwards), then
            # bring it back up under the NEW config. Outside the config lock —
            # stop/start acquire their own locks.
            self.stop(tag=tag)
            self.start(tag=target_tag)
        self._log(f"[{tag}] Updated → tag={target_tag} host={new_host} socks={new_port}")
        self._emit_state(self._compute_state())
        return updated

    def _update_pac(self) -> None:
        """Write the PAC file and reload the in-process server if running."""
        pac_path = write_pac_file(self.config, self.workspace, active_tags=self._active_tags())
        if self._pac_server.is_running():
            self._pac_server.reload(pac_path)

    def _stop_pac_server(self, errors: list[str], keep_ports: bool, ephemeral: bool, context: str = "") -> None:
        """Stop the PAC server (in-process or cross-process) and clean up.

        context is appended to the log message in parentheses when non-empty,
        e.g. ``context="no active connections"`` → ``"PAC server stopped (no active connections)"``.
        """
        suffix = f" ({context})" if context else ""
        if self._pac_server.is_running():
            try:
                self._pac_server.stop()
                self._remove_pac_port_file()
                self._log(f"PAC server stopped{suffix}")
                if not keep_ports and ephemeral:
                    self.config = self.config.model_copy(update={"pac_server_port": 0})
            except Exception as exc:
                errors.append(f"PAC: {exc}")
        else:
            cross_port = self._read_pac_port_file()
            if cross_port:
                self._fire_http(f"http://127.0.0.1:{cross_port}/stop")
                self._remove_pac_port_file()
                self._log(f"PAC server stopped (remote{suffix})")

    def test_ssh(self, ssh_host: str) -> bool:
        return test_ssh_connectivity(ssh_host)

    # ------------------------------------------------------------------ #
    # PAC hosts
    # ------------------------------------------------------------------ #

    def add_pac_host(self, host: str, conn_tag: str | None = None) -> None:
        with self._config_lock:
            self._reload_config()
            default = get_default_connection(self.config)
            tag = conn_tag or (default.tag if default else None)
            if tag is None:
                raise ValueError("No connections configured")
            conn = get_connection(self.config, tag)
            if conn is None:
                raise ValueError(f"Connection '{tag}' not found")
            if host in conn.pac_hosts:
                raise ValueError(f"Host '{host}' already in PAC list for '{tag}'")
            self._replace_connection(conn.model_copy(update={"pac_hosts": list(conn.pac_hosts) + [host]}))
            self._save()
        self._update_pac()
        self._log(f"[{tag}] Added PAC host '{host}'")
        self._emit_state(self._compute_state())

    def remove_pac_host(self, host: str, conn_tag: str | None = None) -> None:
        with self._config_lock:
            self._reload_config()
            found = False
            new_conns = []
            for conn in self.config.connections:
                if host in conn.pac_hosts and (conn_tag is None or conn.tag == conn_tag):
                    found = True
                    new_conns.append(
                        conn.model_copy(update={"pac_hosts": [h for h in conn.pac_hosts if h != host]})
                    )
                else:
                    new_conns.append(conn)
            if not found:
                scope = f" in connection '{conn_tag}'" if conn_tag else " in any PAC list"
                raise ValueError(f"Host '{host}' not found{scope}")
            self.config = self.config.model_copy(update={"connections": new_conns})
            self._save()
        self._update_pac()
        self._log(f"Removed PAC host '{host}'")
        self._emit_state(self._compute_state())

    def set_pac_host_enabled(self, host: str, enabled: bool, conn_tag: str | None = None) -> None:
        with self._config_lock:
            self._reload_config()
            found = False
            new_conns = []
            conn_tag_label = f"[{conn_tag}] " if conn_tag else ""
            for conn in self.config.connections:
                if conn_tag and conn.tag != conn_tag:
                    new_conns.append(conn)
                    continue
                if enabled and host in conn.pac_hosts_disabled:
                    found = True
                    new_conns.append(conn.model_copy(update={
                        "pac_hosts": list(conn.pac_hosts) + [host],
                        "pac_hosts_disabled": [h for h in conn.pac_hosts_disabled if h != host],
                    }))
                elif not enabled and host in conn.pac_hosts:
                    found = True
                    new_conns.append(conn.model_copy(update={
                        "pac_hosts": [h for h in conn.pac_hosts if h != host],
                        "pac_hosts_disabled": list(conn.pac_hosts_disabled) + [host],
                    }))
                else:
                    new_conns.append(conn)
            if not found:
                raise ValueError(f"{conn_tag_label}PAC host '{host}' not found")
            self.config = self.config.model_copy(update={"connections": new_conns})
            self._save()
        self._update_pac()
        self._log(f"{conn_tag_label}PAC host '{host}' {'enabled' if enabled else 'disabled'}")
        self._emit_state(self._compute_state())

    # ------------------------------------------------------------------ #
    # Port forwards
    # ------------------------------------------------------------------ #

    def _add_forward(self, conn_tag: str, fw: PortForward, direction: str) -> None:
        if not validate_port(fw.src_port):
            raise ValueError(f"Invalid src_port {fw.src_port}: must be 1-65535")
        if not validate_port(fw.dst_port):
            raise ValueError(f"Invalid dst_port {fw.dst_port}: must be 1-65535")
        with self._config_lock:
            self._reload_config()
            conn = get_connection(self.config, conn_tag)
            if conn is None:
                raise ValueError(f"Connection '{conn_tag}' not found")
            if direction == "local":
                if any(f.src_port == fw.src_port for f in conn.forwards.local):
                    raise ValueError(f"Local forward on port {fw.src_port} already exists")
                new_fwds = conn.forwards.model_copy(
                    update={"local": list(conn.forwards.local) + [fw]}
                )
            else:
                if any(f.src_port == fw.src_port for f in conn.forwards.remote):
                    raise ValueError(f"Remote forward on port {fw.src_port} already exists")
                new_fwds = conn.forwards.model_copy(
                    update={"remote": list(conn.forwards.remote) + [fw]}
                )
            self._replace_connection(conn.model_copy(update={"forwards": new_fwds}))
            self._save()
        self._log(f"[{conn_tag}] Added {direction} forward {fw.src_port}→{fw.dst_port}")

    def add_local_forward(self, conn_tag: str, fw: PortForward) -> None:
        self._add_forward(conn_tag, fw, "local")
        self._maybe_start_forward_live(conn_tag, fw, "local")

    def add_remote_forward(self, conn_tag: str, fw: PortForward) -> None:
        self._add_forward(conn_tag, fw, "remote")
        self._maybe_start_forward_live(conn_tag, fw, "remote")

    def _remove_forward(self, src_port: int, direction: str) -> None:
        with self._config_lock:
            self._reload_config()
            found = False
            new_conns = []
            for conn in self.config.connections:
                fwds = conn.forwards.local if direction == "local" else conn.forwards.remote
                updated_fwds = [f for f in fwds if f.src_port != src_port]
                if len(updated_fwds) != len(fwds):
                    found = True
                    removed_fw = next(f for f in fwds if f.src_port == src_port)
                    key = "local" if direction == "local" else "remote"
                    new_fwds = conn.forwards.model_copy(update={key: updated_fwds})
                    new_conns.append(conn.model_copy(update={"forwards": new_fwds}))
                    fw_tag = removed_fw.tag or f"{direction}-{src_port}"
                    if removed_fw.tcp:
                        cancel_forward(conn, removed_fw, direction, self.workspace)
                    stop_udp_forward(conn.tag, fw_tag, self._process_mgr)
                    self._emit("forward", {
                        "tag": conn.tag, "fw_tag": fw_tag,
                        "direction": direction, "running": False,
                    })
                else:
                    new_conns.append(conn)
            if not found:
                raise ValueError(f"{direction.capitalize()} forward on port {src_port} not found")
            self.config = self.config.model_copy(update={"connections": new_conns})
            self._save()
        self._log(f"Removed {direction} forward on port {src_port}")

    def remove_local_forward(self, src_port: int) -> None:
        self._remove_forward(src_port, "local")

    def remove_remote_forward(self, src_port: int) -> None:
        self._remove_forward(src_port, "remote")

    def set_forward_enabled(self, conn_tag: str, src_port: int, direction: str, enabled: bool) -> None:
        """Set the enabled flag on a forward and start/stop the live process accordingly.

        If enabling and the connection is running, the forward slave is started immediately.
        If disabling, the forward slave is stopped (if running).
        """
        with self._config_lock:
            self._reload_config()
            conn = get_connection(self.config, conn_tag)
            if conn is None:
                raise ValueError(f"Connection {conn_tag!r} not found")
            forwards = conn.forwards.local if direction == "local" else conn.forwards.remote
            for fw in forwards:
                if fw.src_port == src_port:
                    fw.enabled = enabled
                    self._save()
                    break
            else:
                raise ValueError(f"{direction.capitalize()} forward on port {src_port} not found in '{conn_tag}'")
        # Re-bind conn/fw for the live-start path below (they're now stale references to
        # objects under self.config which may have been replaced by reload).
        conn = get_connection(self.config, conn_tag)
        forwards = conn.forwards.local if direction == "local" else conn.forwards.remote
        for fw in forwards:
            if fw.src_port == src_port:
                fw_tag = fw.tag or f"{direction}-{src_port}"
                self._log(f"[{conn_tag}] Forward {fw_tag} {'enabled' if enabled else 'disabled'}")
                if enabled and (
                        is_tunnel_running(conn_tag, self._process_mgr) or is_socket_alive(conn_tag, self.workspace)):
                    try:
                        if fw.tcp:
                            start_forward(conn, fw, direction, self.workspace)
                        if fw.udp:
                            start_udp_forward(conn, fw, direction, self._process_mgr, self.workspace)
                    except Exception as exc:
                        self._error(f"[{conn_tag}] Forward {src_port} failed to start: {exc}")
                elif not enabled:
                    if fw.tcp:
                        cancel_forward(conn, fw, direction, self.workspace)
                    stop_udp_forward(conn_tag, fw_tag, self._process_mgr)
                self._emit("forward", {
                    "conn_tag": conn_tag,
                    "src_port": src_port,
                    "direction": direction,
                    "enabled": enabled,
                })
                return
        raise ValueError(f"Forward {src_port} not found in {conn_tag} {direction}")

    def toggle_forward_enabled(self, conn_tag: str, src_port: int, direction: str) -> bool:
        """Toggle enabled on a forward. Returns the new enabled state."""
        conn = get_connection(self.config, conn_tag)
        if conn is None:
            raise ValueError(f"Connection {conn_tag!r} not found")
        forwards = conn.forwards.local if direction == "local" else conn.forwards.remote
        for fw in forwards:
            if fw.src_port == src_port:
                new_enabled = not fw.enabled
                self.set_forward_enabled(conn_tag, src_port, direction, new_enabled)
                return new_enabled
        raise ValueError(f"Forward {src_port} not found in {conn_tag} {direction}")

    def is_udp_forward_running(self, conn_tag: str, src_port: int, direction: str) -> bool:
        """Return True if the UDP socat process for this forward is alive."""
        self._reload_config()
        conn = get_connection(self.config, conn_tag)
        if conn is None:
            return False
        forwards = conn.forwards.local if direction == "local" else conn.forwards.remote
        fw = next((f for f in forwards if f.src_port == src_port), None)
        if fw is None or not fw.udp:
            return False
        return _is_udp_forward_running(conn_tag, fw, direction, self._process_mgr)

    # ------------------------------------------------------------------ #
    # File sharing
    # ------------------------------------------------------------------ #

    def share(
            self,
            file: Path,
            conn_tag: str,
            password: str | None = None,
            port: int | None = None,
    ) -> ShareInfo:
        """Start serving an encrypted file share and persist it to config.

        If the connection's SSH tunnel is not running it is started automatically
        so the remote forward slave can be established immediately.
        """
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file}")
        self._reload_config()
        if get_connection(self.config, conn_tag) is None:
            raise ValueError(f"Connection '{conn_tag}' not found")

        pw = password or generate_password()
        server = ShareServer()
        _raw = server.start(file_path=file, password=pw, port=port or 0, workspace=self.workspace)

        info = ShareInfo(
            file_path=_raw.file_path,
            port=_raw.port,
            password=_raw.password,
            url=_raw.url,
            conn_tag=conn_tag,
            running=True,
        )
        # Register in memory and config BEFORE checking tunnel state so that
        # self.start() (below) can pick up this share when iterating _share_servers.
        self._share_put(info.port, server, info)
        self._log(f"Sharing '{file.name}' on port {info.port}")
        self._add_file_share_to_config(conn_tag, str(file), pw, info.port)

        conn = get_connection(self.config, conn_tag)
        _tunnel_up = conn and (
                    is_tunnel_running(conn_tag, self._process_mgr) or is_socket_alive(conn_tag, self.workspace))
        if conn and not _tunnel_up:
            # Tunnel not running — start it; start() will also launch the remote forward slave.
            self.start(conn_tag)
        elif _tunnel_up:
            # Tunnel already running — start the slave directly.
            fw = PortForward(
                src_port=info.port,
                dst_port=info.port,
                src_addr="localhost",
                dst_addr="localhost",
                tag=f"share-{info.port}",
            )
            try:
                start_forward(conn, fw, "remote", self.workspace)
            except Exception as exc:
                self._log(f"[{conn_tag}] Share forward {info.port} failed: {exc}")

        self._emit("share", {
            "port": info.port,
            "file": file.name,
            "running": True,
            "conn_tag": conn_tag,
        })
        return info

    # ---- _share_servers access — all guarded by _share_lock --------------- #
    # These hold the lock only for the dict operation itself and never do I/O
    # or touch _config_lock, so the lock stays a deadlock-free leaf.

    def _share_contains(self, port: int) -> bool:
        with self._share_lock:
            return port in self._share_servers

    def _share_any(self) -> bool:
        with self._share_lock:
            return bool(self._share_servers)

    def _share_put(self, port: int, server: "ShareServer", info: ShareInfo) -> None:
        with self._share_lock:
            self._share_servers[port] = (server, info)

    def _share_pop(self, port: int):
        with self._share_lock:
            return self._share_servers.pop(port, None)

    def _share_snapshot(self) -> list[tuple[int, "ShareServer", ShareInfo]]:
        """Consistent snapshot of (port, server, info) tuples."""
        with self._share_lock:
            return [(p, s, i) for p, (s, i) in self._share_servers.items()]

    def _share_pop_all(self) -> list[tuple[int, "ShareServer", ShareInfo]]:
        """Snapshot then clear atomically — for stop-everything paths."""
        with self._share_lock:
            items = [(p, s, i) for p, (s, i) in self._share_servers.items()]
            self._share_servers.clear()
            return items

    def stop_share(self, port: int | None = None) -> None:
        """Stop share server(s) without removing from config (entry shows as stopped).

        Sets stopped=True on the config entry so the share is not auto-restarted
        on the next start() or restore cycle.
        """
        if port is not None:
            entry = self._share_pop(port)
            if entry:
                entry[0].stop()
                info = entry[1]
                self._log(f"File share on port {port} stopped")
                if info.conn_tag:
                    conn = get_connection(self.config, info.conn_tag)
                    if conn:
                        fw = PortForward(src_port=port, dst_port=port, src_addr="localhost", dst_addr="localhost")
                        cancel_forward(conn, fw, "remote", self.workspace)
                self._set_file_share_stopped(port, True)
                self._emit("share", {
                    "port": port,
                    "file": Path(info.file_path).name,
                    "running": False,
                    "conn_tag": info.conn_tag,
                })
            else:
                # Offline share (not in _share_servers): mark as manually stopped in config
                self._set_file_share_stopped(port, True)
        else:
            # Snapshot+clear under the lock, then do the stop()/cancel I/O.
            for p, server, info in self._share_pop_all():
                server.stop()
                self._log(f"File share on port {p} stopped")
                if info.conn_tag:
                    conn = get_connection(self.config, info.conn_tag)
                    if conn:
                        fw = PortForward(src_port=p, dst_port=p, src_addr="localhost", dst_addr="localhost")
                        cancel_forward(conn, fw, "remote", self.workspace)
                self._emit("share", {
                    "port": p,
                    "file": Path(info.file_path).name,
                    "running": False,
                    "conn_tag": info.conn_tag,
                })

    def delete_share(self, port: int) -> None:
        """Stop and permanently remove a share from config."""
        self.stop_share(port)
        self._remove_file_share_from_config(port)
        self._emit("share", {
            "port": port,
            "file": "",
            "running": False,
            "conn_tag": None,
        })

    def list_shares(self) -> list[ShareInfo]:
        """Return info for all shares: running (in-memory) and stopped (config-only)."""
        # Snapshot under the lock; is_running()/counters are in-memory so we
        # process the snapshot outside it, then pop any dead servers.
        running_ports: set[int] = set()
        result: list[ShareInfo] = []
        dead: list[int] = []
        for p, server, info in self._share_snapshot():
            if not server.is_running():
                dead.append(p)
                continue
            running_ports.add(p)
            result.append(dataclasses.replace(
                info,
                access_count=server.access_count,
                failed_count=server.failed_count,
            ))
        for p in dead:
            self._share_pop(p)

        # Config-only stopped shares (persisted but server not running in this
        # process). Reload under the lock so a concurrent share/stop_share
        # config write is not interleaved or clobbered.
        with self._config_lock:
            self._reload_config()
            for conn in self.config.connections:
                for fs in conn.file_shares:
                    if fs.port not in running_ports:
                        result.append(ShareInfo(
                            file_path=fs.file_path,
                            port=fs.port,
                            password=fs.password,
                            url=f"http://localhost:{fs.port}",
                            conn_tag=conn.tag,
                            running=False,
                            stopped=fs.stopped,
                        ))

        return result

    def share_is_running(self) -> bool:
        return self._share_any()

    def fetch(
            self,
            port: int,
            password: str,
            conn_tag: str,
            outfile: Path | None = None,
    ) -> Path:
        """Download and decrypt a shared file via a transient local forward slave.

        The local forward slave is started, the file is downloaded through
        localhost, then the slave is stopped. No tunnel restart required.
        """
        self._reload_config()
        if get_connection(self.config, conn_tag) is None:
            raise ValueError(f"Connection '{conn_tag}' not found")

        local_port = get_random_free_port()
        fw = PortForward(
            src_port=local_port,
            dst_port=port,
            src_addr="localhost",
            dst_addr="localhost",
            tag=f"fetch-{port}",
        )

        forward_started = False

        # Record whether the tunnel was already running so we know whether to
        # tear it down after the fetch.  Then always call _start_master_only:
        # it is a no-op when the master is already up, but crucially it waits
        # for the socket to appear — which the running-connection path previously
        # skipped, causing the forward to be silently omitted.
        tunnel_was_running = is_tunnel_running(conn_tag, self._process_mgr) or is_socket_alive(conn_tag, self.workspace)
        self._start_master_only(conn_tag)
        conn = get_connection(self.config, conn_tag)  # refresh after potential port assignment

        # Use a transient forward slave if ControlMaster socket is alive
        sock = socket_path(conn_tag, self.workspace) if conn else None
        if conn and sock is not None and sock.exists():
            try:
                start_forward(conn, fw, "local", self.workspace)
                # Poll until local port is accessible (up to 5 s)
                for _ in range(50):
                    if self._probe_port(local_port):
                        break
                    time.sleep(0.1)
                forward_started = True
            except Exception as exc:
                self._log(f"[{conn_tag}] Fetch forward {port} failed: {exc}")

        try:
            # If forward was started, fetch from local_port; otherwise fall back to original port
            # (useful in test/dev scenarios where share server is running locally)
            fetch_port = local_port if forward_started else port
            result = fetch_file(host="localhost", port=fetch_port, password=password, outfile=outfile)
        finally:
            if forward_started:
                cancel_forward(conn, fw, "local", self.workspace)
            if not tunnel_was_running:
                stop_tunnel(conn_tag, self._process_mgr, self.workspace, conn.ssh_host if conn else None)
                self._emit("state", {"tag": conn_tag, "running": False, "pid": None})

        self._log(f"Fetched file to {result}")
        return result

    # ------------------------------------------------------------------ #
    # Testing
    # ------------------------------------------------------------------ #

    def test(self, target: str) -> TestResult:
        conn = get_default_connection(self.config)
        if conn is None or conn.socks_proxy_port == 0:
            return TestResult(target=target, success=False, message="No active SOCKS proxy")
        proxy = f"socks5h://127.0.0.1:{conn.socks_proxy_port}"
        start = time.monotonic()
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--proxy", proxy, "--max-time", "10", f"http://{target}"],
                capture_output=True, timeout=15, text=True,
            )
            latency = (time.monotonic() - start) * 1000
            success = result.returncode == 0
            return TestResult(
                target=target, success=success,
                message=f"HTTP {result.stdout.strip()}" if success else result.stderr.strip(),
                latency_ms=latency if success else None,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return TestResult(target=target, success=False, message=str(exc))

    def test_all(self) -> list[TestResult]:
        return [self.test(host) for conn in self.config.connections for host in conn.pac_hosts]

    def test_connection(self, conn_tag: str) -> TestResult:
        """Test SSH reachability for a specific connection."""
        self._reload_config()
        conn = get_connection(self.config, conn_tag)
        if conn is None:
            return TestResult(target=conn_tag, success=False, message="Connection not found")
        start = time.monotonic()
        ok = test_ssh_connectivity(conn.ssh_host)
        latency = (time.monotonic() - start) * 1000
        return TestResult(
            target=conn.ssh_host,
            success=ok,
            message="SSH reachable" if ok else "SSH unreachable",
            latency_ms=latency if ok else None,
        )

    def test_domain(self, host: str, conn_tag: str) -> TestResult:
        """Test domain reachability via the specified connection's SOCKS proxy."""
        self._reload_config()
        conn = get_connection(self.config, conn_tag)
        if conn is None or conn.socks_proxy_port == 0:
            return TestResult(target=host, success=False, message="No active SOCKS proxy")
        proxy = f"socks5h://127.0.0.1:{conn.socks_proxy_port}"
        clean_host = host.lstrip("*.")
        start = time.monotonic()
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--proxy", proxy, "--max-time", "10", f"http://{clean_host}"],
                capture_output=True, timeout=15, text=True,
            )
            latency = (time.monotonic() - start) * 1000
            success = result.returncode == 0
            return TestResult(
                target=host, success=success,
                message=f"HTTP {result.stdout.strip()}" if success else result.stderr.strip(),
                latency_ms=latency if success else None,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return TestResult(target=host, success=False, message=str(exc))

    def test_forward(self, conn_tag: str, src_port: int, direction: str) -> dict[str, bool]:
        """Check if a port forward is active.

        Returns a dict with keys "tcp" and/or "udp" mapped to True/False.
        For local TCP: checks whether src_port is bound (not free).
        For remote TCP: checks whether the ControlMaster socket is alive.
        For UDP (either direction): checks whether the socat lsocat process is alive.
        """
        self._reload_config()
        conn = get_connection(self.config, conn_tag)
        if conn is None:
            raise ValueError(f"Connection '{conn_tag}' not found")
        forwards = conn.forwards.local if direction == "local" else conn.forwards.remote
        fw = next((f for f in forwards if f.src_port == src_port), None)
        if fw is None:
            raise ValueError(f"Forward {src_port} not found in {conn_tag} {direction}")
        results: dict[str, bool] = {}
        if fw.tcp:
            if direction == "local":
                results["tcp"] = not is_port_free(src_port)
            else:
                results["tcp"] = is_socket_alive(conn_tag, self.workspace)
        if fw.udp:
            results["udp"] = _is_udp_forward_running(conn_tag, fw, direction, self._process_mgr)
        return results

    # ------------------------------------------------------------------ #
    # Utilities
    # ------------------------------------------------------------------ #

    def list_config(self) -> SusOpsConfig:
        self._reload_config()
        return self.config

    def reset(self) -> None:
        self.stop()
        self.stop_share()
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.config = SusOpsConfig()
        self._save()
        self._log("Workspace reset")

    def get_logs(self, n: int = 100) -> list[str]:
        return list(self._log_buffer)[-n:]

    def log_message(self, msg: str) -> None:
        """Push an arbitrary line into the daemon's in-memory log buffer.

        Exposed over RPC so frontends (TUI, tray) can route their own
        operational notes (e.g. browser launches) to the same place the
        daemon's own logs land — visible in the TUI Logs tab and the
        tray Logs window.
        """
        self._log(msg)

    def get_bandwidth(self, tag: str) -> tuple[float, float]:
        return self._bw_sampler.get_rate(tag)

    def get_bandwidth_totals(self, tag: str) -> tuple[float, float]:
        """Return cumulative (rx_bytes, tx_bytes) since last start. Resets on stop."""
        return self._bw_sampler.get_totals(tag)

    def get_bandwidth_history(self, tag: str) -> list[list[float]]:
        """Return persisted [rx_bps, tx_bps] samples for *tag* (oldest → newest)."""
        return self._bw_sampler.get_history(tag)

    def sse_client_count(self) -> int:
        """Live SSE subscriber count. Used by frontends to decide whether
        a stop-on-quit should actually stop, or skip because another
        frontend is still attached to the same daemon."""
        try:
            return int(self._status_server.client_count())
        except Exception:
            return 0

    def get_bandwidth_global(self) -> tuple[float, float]:
        """Return (rx_bps, tx_bps) summed across every connection."""
        rx_total = 0.0
        tx_total = 0.0
        for conn in self.config.connections:
            rx, tx = self._bw_sampler.get_rate(conn.tag)
            rx_total += rx
            tx_total += tx
        return rx_total, tx_total

    def get_uptime(self, tag: str) -> float | None:
        """Return seconds since connection started, or None if not recorded."""
        start = self._start_times.get(tag)
        return time.monotonic() - start if start is not None else None

    def get_process_info(self, tag: str) -> dict:
        try:
            import psutil
        except ImportError:
            return {}

        pid = self._process_mgr.get_pid(f"{SSH_PROCESS_PREFIX}-{tag}")
        if pid is None:
            return {}

        self._reload_config()
        conn = get_connection(self.config, tag)
        socks_port = conn.socks_proxy_port if conn else 0

        # Collect master PID + all forward slave PIDs for this tag.
        # Slaves are not OS children (start_new_session=True), so children() misses them.
        all_pids = [pid]
        prefix = f"{FWD_PROCESS_PREFIX}-{tag}-"
        for key in self._process_mgr.status_all():
            if key.startswith(prefix):
                slave_pid = self._process_mgr.get_pid(key)
                if slave_pid:
                    all_pids.append(slave_pid)

        cpu = 0.0
        mem_mb = 0.0
        for p_pid in all_pids:
            try:
                proc = psutil.Process(p_pid)
                cpu += proc.cpu_percent(interval=None)
                mem_mb += proc.memory_info().rss / 1_048_576
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        active_conns = 0
        if socks_port:
            try:
                active_conns = sum(
                    1 for c in psutil.net_connections("tcp")
                    if c.laddr.port == socks_port and c.status == "ESTABLISHED"
                )
            except (psutil.AccessDenied, OSError):
                pass
        return {"cpu": cpu, "mem_mb": mem_mb, "conns": active_conns}

    def get_pac_url(self) -> str:
        port = self._pac_server.get_port() or self.config.pac_server_port
        return f"http://localhost:{port}/susops.pac" if port else ""

    def get_status_url(self) -> str:
        port = self._status_server.get_port()
        return f"http://localhost:{port}/events" if port else ""

    @property
    def app_config(self):
        return self.config.susops_app

    def update_app_config(self, **kwargs) -> None:
        with self._config_lock:
            self._reload_config()
            self.config = self.config.model_copy(
                update={"susops_app": self.config.susops_app.model_copy(update=kwargs)}
            )
            self._save()

    def update_config(self, **kwargs) -> None:
        """Update top-level SusOpsConfig fields (e.g. pac_server_port).

        Public counterpart to the in-process pattern
            mgr._reload_config()
            mgr.config = mgr.config.model_copy(update={...})
            mgr._save()
        — needed by RPC clients that can't touch private methods or rebind
        the config attribute directly.
        """
        with self._config_lock:
            self._reload_config()
            self.config = self.config.model_copy(update=kwargs)
            self._save()
