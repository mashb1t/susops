"""Per-connection bandwidth sampling.

Extracted from facade.py so the macOS `nettop` parsing can be unit-tested in
isolation. The sampler is a self-contained daemon thread: it depends only on a
ProcessManager (to map tags → PIDs) and an optional on_sample callback — never
on SusOpsManager — so it moves cleanly.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from susops.core.process import ProcessManager
from susops.core.ssh import FWD_PROCESS_PREFIX, SSH_PROCESS_PREFIX

__all__ = ["BandwidthSampler"]


class BandwidthSampler:
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
