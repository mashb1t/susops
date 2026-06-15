"""Process lifecycle management for SusOps via PID files."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import psutil

__all__ = ["ProcessManager", "atomic_write"]

logger = logging.getLogger(__name__)

# create_time round-trips through str/float, and psutil resolution is ~0.01 s,
# so a generous tolerance absorbs precision loss while still telling a reused
# PID apart (a recycled PID starts seconds/minutes after the one we recorded).
_CTIME_TOLERANCE_S = 0.5

# How long stop() waits for a signalled process to actually exit before it
# escalates / gives up. 20 * 0.1 s = 2 s, matching the original wait loop.
_STOP_WAIT_TRIES = 20
_STOP_WAIT_INTERVAL = 0.1


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write text to path atomically with the given mode.

    Writes to a temp file in the same dir, chmods it, then os.replace()s it
    into place so a concurrent reader never observes a half-written or empty
    file (SusOpsClient polls the port file every 100 ms).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    os.chmod(tmp, mode)  # honour mode even if the file pre-existed
    os.replace(tmp, path)


class ProcessManager:
    """Manages long-running background processes via PID files.

    PID files are stored in <workspace>/pids/<name>.pid and hold
    ``<pid>:<create_time>`` so a recycled PID can't be mistaken for the
    original process. Legacy files holding a bare ``<pid>`` are still read
    and upgraded in place on the first successful liveness check.
    """

    def __init__(self, workspace: Path) -> None:
        self._pids_dir = workspace / "pids"
        self._pids_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._pids_dir, 0o700)  # relax an existing 0755 too
        except OSError:
            pass

    def _pid_file(self, name: str) -> Path:
        return self._pids_dir / f"{name}.pid"

    # ------------------------------------------------------------------ #
    # PID file (de)serialisation — "<pid>" (legacy) or "<pid>:<ctime>".
    # ------------------------------------------------------------------ #

    def _read_entry(self, name: str) -> tuple[int | None, float | None]:
        pid_file = self._pid_file(name)
        if not pid_file.exists():
            return None, None
        try:
            raw = pid_file.read_text().strip()
        except OSError:
            return None, None
        if not raw:
            return None, None
        head, _, tail = raw.partition(":")
        try:
            pid = int(head)
        except ValueError:
            return None, None
        ctime: float | None = None
        if tail:
            try:
                ctime = float(tail)
            except ValueError:
                ctime = None
        return pid, ctime

    def _write_entry(self, name: str, pid: int, ctime: float | None) -> None:
        text = f"{pid}:{ctime!r}" if ctime is not None else str(pid)
        atomic_write(self._pid_file(name), text)

    @staticmethod
    def _create_time(pid: int) -> float | None:
        try:
            return psutil.Process(pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
            return None

    def _ctime_matches(self, pid: int, ctime: float) -> bool:
        """True if the live PID still belongs to the process we recorded.

        AccessDenied → True (can't inspect a foreign-owned PID; let the caller
        try and fail on its own perms). NoSuchProcess → False (it's gone).
        """
        try:
            return abs(psutil.Process(pid).create_time() - ctime) <= _CTIME_TOLERANCE_S
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, psutil.Error):
            return True

    # ------------------------------------------------------------------ #
    # Lifecycle.
    # ------------------------------------------------------------------ #

    def start(
            self,
            name: str,
            cmd: list[str],
            env: dict[str, str] | None = None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
    ) -> int:
        """Start a process and record its PID + create_time.

        Returns the PID. Raises RuntimeError if the process fails to start
        (e.g. exec error makes it exit before we can stamp its create_time).
        """
        proc = subprocess.Popen(
            cmd,
            env={**os.environ, **(env or {})},
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,  # detach from terminal
        )
        pid = proc.pid
        ctime = self._create_time(pid)
        if ctime is None:
            # Child exited before we could read its create_time → start failed.
            # Don't leave a PID file pointing at a dead/recyclable PID.
            logger.warning("process %s (pid %s) vanished before create_time read; "
                           "treating start as failed", name, pid)
            raise RuntimeError(f"process {name!r} exited immediately after start")
        self._write_entry(name, pid, ctime)
        return pid

    def track_existing(self, name: str, pid: int) -> None:
        """Record an already-running (recovered/adopted) PID with its
        create_time so it isn't reuse-vulnerable. Used when recovering a
        master whose PID file went stale (find_master_pid / orphan recovery).
        """
        self._write_entry(name, pid, self._create_time(pid))

    def stop(self, name: str, force: bool = False) -> bool:
        """Stop a process by name. Returns True if stopped, False if it
        wasn't running (or the PID was reused by an unrelated process)."""
        pid, ctime = self._read_entry(name)
        if pid is None:
            return False

        # Identity guard: never signal a PID that's been recycled to a
        # different process.
        if ctime is not None:
            try:
                live = psutil.Process(pid)
                if abs(live.create_time() - ctime) > _CTIME_TOLERANCE_S:
                    logger.warning("process %s pid %d was reused by another "
                                   "process; not signalling, unlinking stale file",
                                   name, pid)
                    self._pid_file(name).unlink(missing_ok=True)
                    return False
            except psutil.NoSuchProcess:
                pass  # already gone — os.kill below will confirm
            except (psutil.AccessDenied, psutil.Error):
                pass

        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            self._reap(pid)
            self._pid_file(name).unlink(missing_ok=True)
            return True

        exited = self._wait_for_exit(pid)
        if not exited and not force:
            logger.warning("process %s (pid %d) ignored SIGTERM; escalating to "
                           "SIGKILL", name, pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                exited = True
            else:
                exited = self._wait_for_exit(pid)

        self._reap(pid)
        if not exited and self._alive(pid):
            # Survived even SIGKILL (uninterruptible I/O). Keep the file so the
            # process stays tracked and the user can retry, rather than leaking
            # an untracked live process.
            logger.error("process %s (pid %d) survived SIGKILL; leaving PID file",
                         name, pid)
            return False
        self._pid_file(name).unlink(missing_ok=True)
        return True

    def _wait_for_exit(self, pid: int) -> bool:
        for _ in range(_STOP_WAIT_TRIES):
            time.sleep(_STOP_WAIT_INTERVAL)
            if not self._alive(pid):
                return True
        return False

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def _reap(pid: int) -> None:
        """Best-effort zombie reap; no-op if not our child or already reaped."""
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass

    def is_running(self, name: str) -> bool:
        """Return True if the named process is currently running (not a zombie).

        The running verdict comes from psutil process status, which is correct
        for non-child PIDs too (orphaned-but-live masters adopted after a daemon
        restart). A recycled PID (create_time mismatch) reads as not running.
        """
        pid, ctime = self._read_entry(name)
        if pid is None:
            return False

        try:
            proc = psutil.Process(pid)
            status = proc.status()
        except psutil.NoSuchProcess:
            self._pid_file(name).unlink(missing_ok=True)
            return False
        except psutil.AccessDenied:
            return True  # exists, foreign-owned — can't inspect, treat as up
        except psutil.Error:
            return self._alive(pid)  # psutil hiccup → fall back to liveness

        # Recycled PID?
        if ctime is not None:
            try:
                if abs(proc.create_time() - ctime) > _CTIME_TOLERANCE_S:
                    self._pid_file(name).unlink(missing_ok=True)
                    return False
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
                pass

        if status == psutil.STATUS_ZOMBIE:
            self._reap(pid)
            self._pid_file(name).unlink(missing_ok=True)
            return False

        # Alive and ours. Upgrade a legacy bare-PID file in place so it stops
        # being reuse-vulnerable on the next contact. Skip the daemon's own
        # pid file: it is written/parsed as a bare int by services_daemon +
        # client.py and has its own (cmdline-based) reuse defense.
        if ctime is None and name not in self._KILL_ALL_BLACKLIST:
            try:
                self._write_entry(name, pid, proc.create_time())
            except (psutil.Error, OSError):
                pass
        return True

    def get_pid(self, name: str) -> int | None:
        """Return the PID for a named process, or None if not tracked."""
        pid, _ = self._read_entry(name)
        return pid

    # Process names that kill_all() must NEVER touch — these belong to the
    # services daemon itself (which is the process *calling* kill_all). Killing
    # them would make the daemon SIGTERM itself, taking down PAC + RPC and
    # making subsequent ensure_daemon_running calls race with the dying
    # daemon's PID file.
    _KILL_ALL_BLACKLIST = frozenset({"susops-services"})

    def kill_all(self) -> None:
        """Send SIGTERM to every tracked process and remove PID files. Non-blocking.

        Skips the services daemon's own pid file — see _KILL_ALL_BLACKLIST.
        Verifies create_time before signalling so a recycled PID belonging to
        an unrelated process is never killed.
        """
        for pid_file in list(self._pids_dir.glob("*.pid")):
            if pid_file.stem in self._KILL_ALL_BLACKLIST:
                continue
            pid, ctime = self._read_entry(pid_file.stem)
            if pid is not None and (ctime is None or self._ctime_matches(pid, ctime)):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
            try:
                pid_file.unlink()
            except OSError:
                pass

    def status_all(self) -> dict[str, bool]:
        """Return a dict of {name: is_running} for all tracked processes."""
        result = {}
        for pid_file in self._pids_dir.glob("*.pid"):
            name = pid_file.stem
            result[name] = self.is_running(name)
        return result
