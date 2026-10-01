"""Exclusive run ownership locking mechanism for Forge runs.

Guarantees that a run directory is owned by exactly one active Forge OS process at a time.
Provides atomic non-blocking acquisition, thread/process-safe re-entrancy, stale-lock detection
and recovery, and comprehensive diagnostic reporting.
"""

import atexit
import getpass
import json
import logging
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

from forge import __version__

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

logger = logging.getLogger(__name__)

# Thread-local storage for re-entrant lock tracking
_local_state = threading.local()


def _get_active_locks() -> Dict[str, "RunLock"]:
    if not hasattr(_local_state, "active_locks"):
        _local_state.active_locks = {}
    return _local_state.active_locks


class RunOwnershipError(RuntimeError):
    """Raised when an attempt to acquire run ownership fails because another process holds the lock."""

    def __init__(
        self,
        message: str,
        run_id: str,
        owner_info: Optional[Dict[str, Any]] = None,
        lock_file: Optional[Path] = None,
    ):
        super().__init__(message)
        self.run_id = run_id
        self.owner_info = owner_info or {}
        self.lock_file = lock_file

    def format_diagnostic(self) -> str:
        pid = self.owner_info.get("pid", "(unknown)")
        hostname = self.owner_info.get("hostname", "(unknown)")
        username = self.owner_info.get("username", "(unknown)")
        tty = self.owner_info.get("tty") or "(not a tty / headless)"
        cmd = self.owner_info.get("command_line")
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
        elif cmd:
            cmd_str = str(cmd)
        else:
            cmd_str = "(unknown command)"
        cwd = self.owner_info.get("working_directory", "(unknown)")
        started = self.owner_info.get("start_timestamp", "(unknown)")
        version = self.owner_info.get("forge_version", "(unknown)")

        return (
            f"\n❌ Run Ownership Conflict: Run '{self.run_id}' is currently locked by an active Forge process.\n\n"
            f"Owner Details:\n"
            f"  • PID:              {pid}\n"
            f"  • Hostname:         {hostname}\n"
            f"  • User:             {username}\n"
            f"  • TTY:              {tty}\n"
            f"  • Command:          {cmd_str}\n"
            f"  • Working Dir:      {cwd}\n"
            f"  • Started:          {started}\n"
            f"  • Forge Version:    {version}\n\n"
            f"Aborting execution to prevent concurrent corruption of run artifacts.\n"
        )


class RunLock:
    """Exclusive lock for a Forge run directory with re-entrancy and stale-lock recovery."""

    LOCK_FILE_NAME = "run.lock"
    SCHEMA_VERSION = 1

    def __init__(self, run_dir: Path, run_id: Optional[str] = None):
        self.run_dir = Path(run_dir)
        self.run_id = run_id or self.run_dir.name
        self.lock_file = self.run_dir / self.LOCK_FILE_NAME
        self._fd: Optional[int] = None
        self._depth: int = 0
        self.is_locked: bool = False
        self.is_stale_recovered: bool = False
        self.stale_metadata: Optional[Dict[str, Any]] = None
        self.metadata: Optional[Dict[str, Any]] = None

    @classmethod
    def collect_metadata(cls) -> Dict[str, Any]:
        """Gather process metadata for diagnostic reporting."""
        tty = None
        try:
            if sys.stdin.isatty():
                tty = os.ttyname(sys.stdin.fileno())
            elif sys.stdout.isatty():
                tty = os.ttyname(sys.stdout.fileno())
        except Exception:
            tty = None

        try:
            username = getpass.getuser()
        except Exception:
            username = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"

        return {
            "schema_version": cls.SCHEMA_VERSION,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "username": username,
            "tty": tty,
            "command_line": list(sys.argv),
            "working_directory": str(Path.cwd().resolve()),
            "start_timestamp": datetime.now(timezone.utc).isoformat(),
            "forge_version": __version__,
        }

    @staticmethod
    def read_owner_metadata(lock_file: Path, max_attempts: int = 5) -> Dict[str, Any]:
        """Safely read metadata JSON from a lock file, with retries for concurrently written files."""
        for _ in range(max_attempts):
            try:
                if lock_file.exists():
                    content = lock_file.read_text(encoding="utf-8").strip()
                    if content:
                        return json.loads(content)
            except Exception:
                pass
            time.sleep(0.02)
        return {}

    @classmethod
    def is_run_locked(cls, run_dir: Path) -> bool:
        """Check whether a run directory is currently locked by a live process."""
        lock_file = run_dir / cls.LOCK_FILE_NAME
        if not lock_file.exists():
            return False
        try:
            fd = os.open(lock_file, os.O_RDWR, 0o644)
        except OSError:
            return False

        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    return False
                except (BlockingIOError, OSError):
                    return True
            elif msvcrt is not None:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    return False
                except OSError:
                    return True
            return False
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def acquire(self, timeout: float = 0.0) -> "RunLock":
        """Acquire exclusive ownership of the run directory atomically.

        Safely re-entrant within the same thread: if this thread already holds the lock
        on the run, increments depth counter and returns itself.

        Args:
            timeout: Maximum seconds to wait if currently locked. Default 0.0 (non-blocking).

        Raises:
            RunOwnershipError: If run is already locked by an active Forge process.
        """
        active_locks = _get_active_locks()
        canonical_key = str(self.lock_file.resolve())

        # Re-entrancy check: if current thread already holds this lock
        if canonical_key in active_locks:
            held = active_locks[canonical_key]
            held._depth += 1
            self._depth = held._depth
            self._fd = held._fd
            self.is_locked = True
            self.metadata = held.metadata
            self.is_stale_recovered = held.is_stale_recovered
            self.stale_metadata = held.stale_metadata
            return self

        if self.is_locked:
            self._depth += 1
            return self

        self.run_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(0.0, timeout)

        while True:
            try:
                fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o644)
            except OSError as e:
                raise RunOwnershipError(
                    f"Failed to open lock file {self.lock_file}: {e}",
                    run_id=self.run_id,
                    lock_file=self.lock_file,
                ) from e

            locked = False
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except (BlockingIOError, OSError):
                    locked = False
            elif msvcrt is not None:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError:
                    locked = False
            else:
                locked = True

            if not locked:
                try:
                    os.close(fd)
                except OSError:
                    pass

                if time.time() < deadline:
                    time.sleep(0.05)
                    continue

                owner_info = self.read_owner_metadata(self.lock_file)
                raise RunOwnershipError(
                    f"Run '{self.run_id}' is owned by an active Forge process.",
                    run_id=self.run_id,
                    owner_info=owner_info,
                    lock_file=self.lock_file,
                )

            # Inode verification for POSIX to guard against unlink race
            if fcntl is not None:
                try:
                    stat_fd = os.fstat(fd)
                    stat_path = os.stat(self.lock_file)
                    if stat_fd.st_ino != stat_path.st_ino or stat_fd.st_dev != stat_path.st_dev:
                        # File was unlinked before we locked it
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        os.close(fd)
                        continue
                except FileNotFoundError:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(fd)
                    continue

            # Lock acquired! Check if file previously had content (stale lock recovery)
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                existing_bytes = os.read(fd, 65536)
                existing_text = existing_bytes.decode("utf-8", errors="replace").strip()
                if existing_text:
                    try:
                        prev_meta = json.loads(existing_text)
                        self.is_stale_recovered = True
                        self.stale_metadata = prev_meta
                        prev_pid = prev_meta.get("pid", "(unknown)")
                        prev_host = prev_meta.get("hostname", "(unknown)")
                        prev_cmd = prev_meta.get("command_line", "")
                        logger.warning(
                            "Recovered stale lock on run '%s' (previous owner PID %s on %s, cmd: %s)",
                            self.run_id,
                            prev_pid,
                            prev_host,
                            prev_cmd,
                        )
                    except Exception:
                        self.is_stale_recovered = True
                        self.stale_metadata = {"raw": existing_text}
                        logger.warning("Recovered stale lock on run '%s' with corrupt metadata", self.run_id)

                # Write new process metadata
                self.metadata = self.collect_metadata()
                payload = json.dumps(self.metadata, indent=2).encode("utf-8")
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, payload)
                os.fsync(fd)

                self._fd = fd
                self._depth = 1
                self.is_locked = True
                active_locks[canonical_key] = self
                atexit.register(self.release)
                return self
            except Exception:
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

    def release(self) -> None:
        """Release exclusive lock and clean up the lock file when depth reaches 0."""
        if not self.is_locked:
            return

        active_locks = _get_active_locks()
        canonical_key = str(self.lock_file.resolve())

        # If re-entrancy depth > 1, decrement depth and return without unlocking OS primitive
        if canonical_key in active_locks:
            held = active_locks[canonical_key]
            held._depth -= 1
            self._depth = held._depth
            if held._depth > 0:
                return
            del active_locks[canonical_key]
        else:
            self._depth -= 1
            if self._depth > 0:
                return

        if self._fd is None:
            self.is_locked = False
            return

        try:
            atexit.unregister(self.release)
        except Exception:
            pass

        fd = self._fd
        self._fd = None
        self.is_locked = False

        if sys.platform == "win32":
            if msvcrt is not None:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                if self.lock_file.exists():
                    os.unlink(self.lock_file)
            except OSError:
                pass
        else:
            try:
                if self.lock_file.exists():
                    stat_path = os.stat(self.lock_file)
                    stat_fd = os.fstat(fd)
                    if stat_path.st_ino == stat_fd.st_ino and stat_path.st_dev == stat_fd.st_dev:
                        os.unlink(self.lock_file)
            except OSError:
                pass
            finally:
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
