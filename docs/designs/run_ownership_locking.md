# Forge Architecture Design: Exclusive Run Ownership Locking

**Document Version:** 1.0.0  
**Status:** Approved  
**Author:** Forge Team  
**Date:** 2026-09-23  

---

## 1. Executive Summary

A real-world investigation established that two independent Forge OS processes simultaneously executed against the same run directory (`run-009`). While the stage orchestration logic itself functioned properly, the concurrent execution caused state clobbering, race conditions in artifact generation, and metadata corruption.

This design document specifies the exclusive run ownership locking architecture for Forge to guarantee that a run ID is owned by **exactly one live Forge process** at a time.

---

## 2. Ownership Model

- **Mutual Exclusion:** Exactly one live Forge operating system process may own a run ID (`run-XXX`) at any moment.
- **Scope:** Bound to the specific run directory (`.forge/runs/<run_id>`). Each run ID has an independent lock.
- **Kernel-Enforced Primitive:** Ownership is backed by an OS-level file lock descriptor (`fcntl.flock` on POSIX systems, `msvcrt.locking` on Windows).
  - Locks are managed by the kernel file table and tied to open file descriptions.
  - In normal operation, locks are released and cleaned up upon exit.
  - If a process terminates abruptly (`SIGKILL`, segmentation fault, out-of-memory killer, power failure), the OS kernel automatically releases the kernel lock while preserving the file on disk. This enables deterministic stale-lock detection without manual file deletion.
- **Zero Race Conditions:** Lock acquisition is atomic; no "check-then-create" window exists.

---

## 3. Lock Location

- **File Path:** `<project_root>/.forge/runs/<run_id>/run.lock`
- **Design Rationale:**
  - **Colocation with Run Artifacts:** The lock is directly contained in the directory whose integrity it guards.
  - **No Global Leakage:** Deleting, archiving, or transferring a run directory automatically includes or removes its lock file without leaving orphaned locks in a global directory.
  - **Observability:** Operators and diagnostics can inspect `.forge/runs/<run_id>/run.lock` using standard tools (`cat`, `jq`, `lsof`, `fuser`).

---

## 4. Lock Metadata Schema

The lock file contains a JSON payload populated atomically at acquisition:

```json
{
  "pid": 12345,
  "hostname": "forge-host",
  "username": "developer",
  "tty": "/dev/pts/1",
  "command_line": ["forge", "run", "Add OAuth2 authentication"],
  "working_directory": "/home/developer/project",
  "start_timestamp": "2026-09-23T06:15:00.123456+00:00",
  "forge_version": "1.0.0"
}
```

### Metadata Fields
| Field | Type | Description |
|---|---|---|
| `pid` | `int` | Process ID of the owning Forge process (`os.getpid()`). |
| `hostname` | `str` | Hostname where the process is running (`socket.gethostname()`). |
| `username` | `str` | Operating system user (`getpass.getuser()`). |
| `tty` | `str \| null` | Controlling terminal device if interactive, or `null` if headless/daemonized. |
| `command_line` | `List[str]` | Exact invocation arguments (`sys.argv`). |
| `working_directory` | `str` | Absolute path of working directory at startup (`os.getcwd()`). |
| `start_timestamp` | `str` | ISO 8601 UTC timestamp when lock was acquired. |
| `forge_version` | `str` | Forge version (`forge.__version__`). |

---

## 5. Acquisition Algorithm

The acquisition algorithm is atomic, non-blocking, and race-free:

```text
Algorithm AcquireLock(run_dir, run_id):
  1. Ensure run_dir exists.
  2. Set lock_path = run_dir / "run.lock".
  3. Loop (attempt atomic acquisition):
      a. Open lock_path with flags (O_RDWR | O_CREAT, mode 0o644). Obtain file descriptor fd.
      b. Request non-blocking exclusive OS lock:
           POSIX: fcntl.flock(fd, LOCK_EX | LOCK_NB)
           Windows: msvcrt.locking(fd, LK_NBLCK, 1)
         If lock fails with EWOULDBLOCK / EAGAIN / EACCES:
           Close fd.
           Read existing metadata from lock_path (with safe retry).
           Raise RunOwnershipError(active_owner_metadata).
      c. [POSIX Inode Invariant Check]
           stat_fd = fstat(fd)
           stat_path = stat(lock_path)
           If stat_path does not exist OR stat_fd.st_ino != stat_path.st_ino:
             // File was unlinked by previous owner before flock completed
             flock(fd, LOCK_UN)
             Close fd
             Repeat loop
      d. Read existing content from fd.
         If content is present:
           Parse previous owner metadata.
           Mark is_stale_recovered = True.
           Record stale_metadata.
           Log warning: "Recovered stale lock for run '<run_id>' from dead PID <prev_pid>."
      e. Rewind fd to offset 0 and truncate fd to 0 bytes.
      f. Collect current process metadata JSON.
      g. Write metadata payload to fd, flush, and fsync(fd).
      h. Register atexit cleanup hook.
      i. Set is_locked = True and return.
```

---

## 6. Release Algorithm

The release algorithm guarantees clean unlinking under mutual exclusion:

```text
Algorithm ReleaseLock(lock_path, fd):
  1. If not is_locked or fd is None: Return.
  2. Unregister atexit cleanup hook.
  3. On POSIX:
       If lock_path exists and stat(lock_path).st_ino == fstat(fd).st_ino:
         Unlink lock_path.
       Unlock fcntl.flock(fd, LOCK_UN).
       Close fd.
  4. On Windows:
       Unlock msvcrt.locking(fd, LK_UNLCK, 1).
       Close fd.
       If lock_path exists: Unlink lock_path.
  5. Set is_locked = False.
```

---

## 7. Stale-Lock Recovery

1. **Detection Mechanism:**
   - When a process dies abnormally (`SIGKILL`, crash), the OS kernel closes all open file descriptors and frees the kernel file lock.
   - The lock file `run.lock` remains on disk with the dead process's metadata.
   - A subsequent Forge process attempts `flock(fd, LOCK_EX | LOCK_NB)`.
   - The OS lock succeeds because no live process holds the kernel lock.
   - The process inspects the file contents. Because the file contains metadata from a prior process, it detects that the lock was abandoned.
2. **Recovery Action:**
   - The acquiring process recognizes the stale state.
   - It records the previous owner's metadata for diagnostics and tests.
   - It logs a diagnostic notification (`logger.warning` / CLI warning).
   - It overwrites the file with its own metadata and assumes ownership seamlessly without manual intervention.

---

## 8. Failure Behavior & Diagnostics

If another live process owns the run:
- Acquisition fails immediately (fail-fast, non-blocking).
- A formatted diagnostic is emitted to `stderr`:

```
❌ Run Ownership Conflict: Run 'run-009' is currently locked by an active Forge process.

Owner Details:
  • PID:              12345
  • Hostname:         forge-host
  • User:             developer
  • TTY:              /dev/pts/1
  • Command:          forge run Add OAuth2 authentication
  • Working Dir:      /home/developer/project
  • Started:          2026-09-23T06:15:00.123456+00:00
  • Forge Version:    1.0.0

Aborting execution to prevent concurrent corruption of run artifacts.
```
- Process exits immediately with status code `1`.

---

## 9. Platform-Specific Considerations

| Platform | Primitive | Behavior / Considerations |
|---|---|---|
| **Linux** | `fcntl.flock(fd, LOCK_EX \| LOCK_NB)` | Advisory locking supported across all modern Linux kernels and filesystems. Inode checking prevents unlink races. |
| **macOS** | `fcntl.flock(fd, LOCK_EX \| LOCK_NB)` | Fully supported BSD-style lock semantics on APFS and HFS+. |
| **Windows** | `msvcrt.locking(fd, LK_NBLCK, 1)` | Standard Windows byte-range locking. File must be closed prior to deletion on NTFS. |
