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
        pid_file = self._pid_file(name)
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(pid))
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
            # Reap the zombie if this process is our direct child.
            # Popen objects are not stored, so Python never calls wait()
            # automatically, leaving the exited process as a zombie until
            # the parent exits.
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass  # not our child or already reaped
            except OSError:
                pass
        return True

    def is_running(self, name: str) -> bool:
        """Return True if the named process is currently running (not a zombie)."""
        pid = self.get_pid(name)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, not our process, but it's running
        # Process exists in the table — check whether it's a zombie.
        # Zombies respond to kill(0) but can't do any real work.
        # Reading /proc is Linux-only; on other platforms we trust kill(0).
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            for line in status.splitlines():
                if line.startswith("State:") and "Z" in line:
                    # Reap it (only works if we're the parent)
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except (ChildProcessError, OSError):
                        pass
                    self._pid_file(name).unlink(missing_ok=True)
                    return False
        except OSError:
            pass  # /proc not available or pid already gone
        return True

    def get_pid(self, name: str) -> int | None:
        """Return the PID for a named process, or None if not tracked."""
        pid_file = self._pid_file(name)
        if not pid_file.exists():
            return None
        try:
            return int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return None

    def kill_all(self) -> None:
        """Send SIGTERM to every tracked process and remove PID files. Non-blocking."""
        for pid_file in list(self._pids_dir.glob("*.pid")):
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
            except (ValueError, OSError, ProcessLookupError):
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
