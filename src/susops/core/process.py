"""Process lifecycle management for SusOps via PID files."""
from __future__ import annotations
import os
import signal
import subprocess
import time
from pathlib import Path

__all__ = ["ProcessManager"]

class ProcessManager:
    """Manages long-running background processes via PID files.

    PID files are stored in <workspace>/pids/<name>.pid.
    This replaces the exec -a / pgrep -f approach used in the Bash CLI.
    """

    def __init__(self, workspace: Path) -> None:
        self._pids_dir = workspace / "pids"
        self._pids_dir.mkdir(parents=True, exist_ok=True)

    def _pid_file(self, name: str) -> Path:
        return self._pids_dir / f"{name}.pid"

    def start(
        self,
        name: str,
        cmd: list[str],
        env: dict[str, str] | None = None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) -> int:
        """Start a process and record its PID.

        Returns the PID. Raises RuntimeError if process fails to start.
        """
        import os as _os
        proc = subprocess.Popen(
            cmd,
            env={**_os.environ, **(env or {})},
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,  # detach from terminal
        )
        pid = proc.pid
        self._pid_file(name).write_text(str(pid))
        return pid

    def stop(self, name: str, force: bool = False) -> bool:
        """Stop a process by name. Returns True if stopped, False if not running."""
        pid = self.get_pid(name)
        if pid is None:
            return False
        try:
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.kill(pid, sig)
            # Wait briefly for process to exit
            for _ in range(20):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)  # check if still alive
                except ProcessLookupError:
                    break
        except ProcessLookupError:
            pass  # already gone
        finally:
            pid_file = self._pid_file(name)
            if pid_file.exists():
                pid_file.unlink()
        return True

    def is_running(self, name: str) -> bool:
        """Return True if the named process is currently running."""
        pid = self.get_pid(name)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, not our process, but it's running

    def get_pid(self, name: str) -> int | None:
        """Return the PID for a named process, or None if not tracked."""
        pid_file = self._pid_file(name)
        if not pid_file.exists():
            return None
        try:
            return int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return None

    def status_all(self) -> dict[str, bool]:
        """Return a dict of {name: is_running} for all tracked processes."""
        result = {}
        for pid_file in self._pids_dir.glob("*.pid"):
            name = pid_file.stem
            result[name] = self.is_running(name)
        return result

    def cleanup_stale(self) -> list[str]:
        """Remove PID files for processes that are no longer running.

        Returns list of cleaned-up names.
        """
        cleaned = []
        for pid_file in self._pids_dir.glob("*.pid"):
            name = pid_file.stem
            if not self.is_running(name):
                pid_file.unlink()
                cleaned.append(name)
        return cleaned
