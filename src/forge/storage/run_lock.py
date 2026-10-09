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
from forge.core.platform import WINDOWS_LOCK_OFFSET, is_pid_alive

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

logger = logging.getLogger(__name__)

# Byte offset used for Windows mandatory file locking.
# On Windows NT, byte-range locks applied via msvcrt.locking are kernel-enforced
# and mandatory for the file handle. If byte 0 is locked, any external read of
# the lock file (e.g. diagnostic inspection of owner metadata via Path.read_text())
# fails with PermissionError (ERROR_LOCK_VIOLATION).
# By locking a single byte at a high offset beyond the file content (1 GiB),
# the lock file metadata in bytes [0, file_size) remains freely readable by
# diagnostic readers while mutual exclusion is fully preserved across processes.
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
                    os.lseek(fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    os.lseek(fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
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
                    os.lseek(fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
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
                        os.lseek(fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
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
                    os.lseek(fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
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


class WorkingTreeConflictError(RuntimeError):
    """Raised when an attempt to acquire working-tree writer lease fails due to an active writer."""

    def __init__(
        self,
        message: str,
        working_tree: Path,
        lease_info: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.working_tree = working_tree
        self.lease_info = lease_info or {}

    def format_diagnostic(self) -> str:
        session_id = self.lease_info.get("session_id", "(unknown)")
        stage_name = self.lease_info.get("stage_name", "(unknown)")
        run_id = self.lease_info.get("run_id", "(unknown)")
        pid = self.lease_info.get("pid", "(unknown)")
        adapter = self.lease_info.get("adapter", "(unknown)")

        return (
            f"\n❌ Working Tree Conflict: Working tree '{self.working_tree}' is currently "
            f"occupied by active writer session '{session_id}' in stage '{stage_name}' (Run '{run_id}').\n\n"
            f"Owner Details:\n"
            f"  • PID:              {pid}\n"
            f"  • Adapter:          {adapter}\n"
            f"  • Session ID:       {session_id}\n"
            f"  • Stage:            {stage_name}\n"
            f"  • Run ID:           {run_id}\n\n"
            f"Aborting execution to prevent concurrent corruption of repository files.\n"
        )


class _WorkingTreeMutex:
    """Advisory filesystem mutex protecting WorkingTreeLease metadata modifications.

    Uses kernel file locking (fcntl.flock on POSIX, msvcrt.locking at WINDOWS_LOCK_OFFSET on Windows)
    to serialize inspection and mutation of the lease metadata file across processes.
    """

    def __init__(self, mutex_path: Path):
        self.mutex_path = Path(mutex_path)
        self._fd: Optional[int] = None

    def acquire(self, timeout: float = 10.0, poll_interval: float = 0.02) -> bool:
        self.mutex_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(0.1, timeout)
        while True:
            try:
                self._fd = os.open(self.mutex_path, os.O_RDWR | os.O_CREAT, 0o644)
            except OSError:
                if time.time() >= deadline:
                    return False
                time.sleep(poll_interval)
                continue

            locked = False
            if fcntl is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except (BlockingIOError, OSError):
                    locked = False
            elif msvcrt is not None:
                try:
                    os.lseek(self._fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError:
                    locked = False
            else:
                locked = True

            if locked:
                return True

            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

            if time.time() >= deadline:
                return False
            time.sleep(poll_interval)

    def release(self) -> None:
        if self._fd is not None:
            try:
                if fcntl is not None:
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                elif msvcrt is not None:
                    try:
                        os.lseek(self._fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None

    def __enter__(self) -> "_WorkingTreeMutex":
        if not self.acquire():
            raise TimeoutError(f"Timed out acquiring working tree mutex at {self.mutex_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def _write_lease_metadata_atomic(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write lease metadata to disk via temporary file and atomic replace."""
    content = json.dumps(data, indent=2)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_file = parent / f".{path.name}.tmp.{os.getpid()}_{time.time_ns()}"
    try:
        temp_file.write_text(content, encoding="utf-8")
        os.replace(temp_file, path)
    finally:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass


class WorkingTreeLease:
    """Exclusive writer lease for the repository working tree across stages and retries.

    Guarantees that at any point in time, at most one active adapter session or Forge
    execution stage can modify files in the working tree.
    """

    LEASE_FILE_NAME = "working_tree.lock"
    SCHEMA_VERSION = 1

    def __init__(
        self,
        project_root: Path,
        run_id: str,
        stage_name: str,
        iteration: int = 1,
        adapter_name: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.forge_dir = self.project_root / ".forge"
        self.lease_file = self.forge_dir / self.LEASE_FILE_NAME
        self.mutex_file = self.forge_dir / (self.LEASE_FILE_NAME + ".mutex")
        self.mutex = _WorkingTreeMutex(self.mutex_file)
        self.run_id = run_id
        self.stage_name = stage_name
        self.iteration = iteration
        self.adapter_name = adapter_name or "unknown"
        self.session_id = session_id
        self.is_acquired: bool = False
        self._depth: int = 0

    def acquire(self, timeout: float = 0.0, adapter_instance: Optional[Any] = None) -> "WorkingTreeLease":
        """Acquire exclusive writer ownership of the working tree.

        If a prior writer session is recorded, verifies whether that session has stopped.
        If the prior session is still active and cannot be proven stopped, raises
        WorkingTreeConflictError (fails closed).
        """
        self.forge_dir.mkdir(parents=True, exist_ok=True)
        canonical_key = f"wt_{self.project_root}"
        active_locks = _get_active_locks()
        if canonical_key in active_locks:
            held = active_locks[canonical_key]
            if (
                held.session_id == self.session_id
                and held.run_id == self.run_id
                and held.stage_name == self.stage_name
                and held.iteration == self.iteration
            ):
                held._depth += 1
                self._depth = held._depth
                self.is_acquired = True
                return self

        # Serialize acquisition across processes using advisory kernel file lock
        mutex_timeout = max(5.0, timeout)
        if not self.mutex.acquire(timeout=mutex_timeout):
            raise WorkingTreeConflictError(
                f"Working tree '{self.project_root}' is currently being arbitrated by another process. "
                "Timed out waiting for acquisition mutex.",
                working_tree=self.project_root,
            )

        try:
            if self.lease_file.exists():
                existing = RunLock.read_owner_metadata(self.lease_file)
                if existing and existing.get("status") == "ACTIVE":
                    existing_sid = existing.get("session_id")
                    existing_pid = existing.get("pid")
                    existing_stage = existing.get("stage_name")
                    existing_run = existing.get("run_id")

                    is_different_session = (
                        (existing_sid and existing_sid != self.session_id)
                        or (existing_run != self.run_id or existing_stage != self.stage_name)
                    )

                    if is_different_session:
                        pid_alive = False
                        if existing_pid:
                            pid_alive = is_pid_alive(existing_pid)

                        session_active = False
                        if adapter_instance and existing_sid and hasattr(adapter_instance, "is_session_active"):
                            session_active = adapter_instance.is_session_active(existing_sid, cwd=self.project_root)

                        if session_active or (pid_alive and existing_pid != os.getpid()):
                            stopped = False
                            if adapter_instance and existing_sid and hasattr(adapter_instance, "cancel_session"):
                                stopped = adapter_instance.cancel_session(existing_sid, verify=True, timeout=5.0, cwd=self.project_root)

                            if not stopped:
                                raise WorkingTreeConflictError(
                                    f"Working tree '{self.project_root}' is actively held by writer "
                                    f"session '{existing_sid}' (PID {existing_pid}, Stage '{existing_stage}'). "
                                    "Cannot start competing execution.",
                                    working_tree=self.project_root,
                                    lease_info=existing,
                                )
                        else:
                            logger.info(
                                "Reclaimed stale working-tree lease from dead process/session (PID %s, Stage '%s', Session '%s')",
                                existing_pid,
                                existing_stage,
                                existing_sid,
                            )

            metadata = {
                "schema_version": self.SCHEMA_VERSION,
                "pid": os.getpid(),
                "run_id": self.run_id,
                "stage_name": self.stage_name,
                "iteration": self.iteration,
                "adapter": self.adapter_name,
                "session_id": self.session_id,
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "status": "ACTIVE",
            }
            try:
                _write_lease_metadata_atomic(self.lease_file, metadata)
            except Exception as e:
                logger.debug("Failed to write lease metadata: %s", e)

            active_locks[canonical_key] = self
            self._depth = 1
            self.is_acquired = True
            return self
        finally:
            self.mutex.release()

    def update_session_id(self, session_id: str) -> None:
        """Update lease with the captured session ID once known."""
        self.session_id = session_id
        with self.mutex:
            if self.lease_file.exists():
                try:
                    data = RunLock.read_owner_metadata(self.lease_file)
                    if data:
                        data["session_id"] = session_id
                        _write_lease_metadata_atomic(self.lease_file, data)
                except Exception:
                    pass

    def release(self, force: bool = False, adapter_instance: Optional[Any] = None) -> None:
        """Release the working-tree writer lease."""
        canonical_key = f"wt_{self.project_root}"
        active_locks = _get_active_locks()
        if canonical_key in active_locks:
            held = active_locks[canonical_key]
            held._depth -= 1
            if held._depth > 0:
                return
            del active_locks[canonical_key]

        with self.mutex:
            if self.lease_file.exists():
                try:
                    if not force and self.session_id and adapter_instance and hasattr(adapter_instance, "is_session_active"):
                        if adapter_instance.is_session_active(self.session_id, cwd=self.project_root):
                            logger.warning(
                                "Working tree lease retained: session %s is still active in background.",
                                self.session_id,
                            )
                            self.is_acquired = False
                            return

                    data = RunLock.read_owner_metadata(self.lease_file)
                    if data:
                        data["status"] = "RELEASED"
                        data["released_at"] = datetime.now(timezone.utc).isoformat()
                        _write_lease_metadata_atomic(self.lease_file, data)
                except Exception:
                    pass
        self.is_acquired = False

    @classmethod
    def check_no_active_writers(cls, project_root: Path, adapter_instance: Optional[Any] = None) -> bool:
        """Verify that no active writer session or process holds the working tree lease."""
        lease_path = Path(project_root) / ".forge" / cls.LEASE_FILE_NAME
        if not lease_path.exists():
            return True
        data = RunLock.read_owner_metadata(lease_path)
        if not data or data.get("status") != "ACTIVE":
            return True
        canonical_key = f"wt_{Path(project_root).resolve()}"
        active_locks = _get_active_locks()
        if canonical_key in active_locks:
            return False
        pid = data.get("pid")
        if pid and pid != os.getpid():
            if is_pid_alive(pid):
                return False
        sid = data.get("session_id")
        if sid:
            adapter = adapter_instance
            if adapter is None and sid.startswith("ses_"):
                try:
                    from forge.adapters.opencode import OpenCodeAdapter
                    adapter = OpenCodeAdapter()
                except Exception:
                    pass
            if adapter and hasattr(adapter, "is_session_active"):
                if adapter.is_session_active(sid, cwd=project_root):
                    return False
        if pid == os.getpid():
            return False
        return True

    def __enter__(self) -> "WorkingTreeLease":
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
