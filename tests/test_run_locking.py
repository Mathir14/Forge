"""Comprehensive test suite for Forge exclusive run ownership locking.

Covers:
1. Successful acquisition (direct, context manager, Run.lock, RunManager)
2. Lock metadata verification (all 8 required fields)
3. Duplicate acquisition rejection and diagnostic reporting
4. Stale lock detection and recovery (dead PID, corrupted JSON, empty file)
5. Cleanup (normal exit, exceptions, sys.exit, SIGTERM, SIGINT)
6. Concurrent acquisition (multiprocessing mutual exclusion race tests)
7. Regressions (delete_run protection, CLI stage and pipeline locking)
"""

import contextlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

import pytest
from click.testing import CliRunner

from forge import __version__
from forge.cli import main
from forge.core.run import Run
from forge.storage.run_lock import RunLock, RunOwnershipError
from forge.storage.run_manager import RunManager


# ============================================================================
# 1. SUCCESSFUL ACQUISITION
# ============================================================================


def test_successful_acquisition_basic(tmp_path):
    """Verify clean acquisition creates the lock file with correct state and cleanup."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Implement feature A")
    lock_file = run.run_dir / "run.lock"

    assert not lock_file.exists()

    lock = RunLock(run.run_dir, run_id=run.run_id)
    assert not lock.is_locked

    lock.acquire()
    try:
        assert lock.is_locked is True
        assert lock.is_stale_recovered is False
        assert lock_file.exists()

        # Verify metadata content on disk
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
        assert "hostname" in data and data["hostname"]
        assert "username" in data and data["username"]
        assert "working_directory" in data
        assert "start_timestamp" in data
        assert data["forge_version"] == __version__
        assert isinstance(data["command_line"], list)
    finally:
        lock.release()

    assert lock.is_locked is False
    assert not lock_file.exists()


def test_lock_metadata_all_eight_required_fields(tmp_path):
    """Verify that lock metadata contains all eight required diagnostic fields."""
    metadata = RunLock.collect_metadata()

    required_fields = [
        "schema_version",
        "pid",
        "hostname",
        "username",
        "tty",
        "command_line",
        "working_directory",
        "start_timestamp",
        "forge_version",
    ]
    for field in required_fields:
        assert field in metadata, f"Missing required metadata field: {field}"

    assert metadata["schema_version"] == 1
    assert isinstance(metadata["pid"], int)
    assert metadata["pid"] == os.getpid()
    assert isinstance(metadata["hostname"], str) and len(metadata["hostname"]) > 0
    assert isinstance(metadata["username"], str) and len(metadata["username"]) > 0
    assert metadata["tty"] is None or isinstance(metadata["tty"], str)
    assert isinstance(metadata["command_line"], list)
    assert isinstance(metadata["working_directory"], str)
    assert isinstance(metadata["start_timestamp"], str)
    # Validate ISO timestamp parse
    datetime.fromisoformat(metadata["start_timestamp"])
    assert metadata["forge_version"] == __version__


def test_context_manager_acquisition_and_cleanup(tmp_path):
    """Verify RunLock context manager handles acquisition and cleanup cleanly."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Context manager test")
    lock_file = run.run_dir / "run.lock"

    with RunLock(run.run_dir, run_id=run.run_id) as lock:
        assert lock.is_locked is True
        assert lock_file.exists()

    assert lock.is_locked is False
    assert not lock_file.exists()


def test_run_lock_convenience_method(tmp_path):
    """Verify Run.lock() helper method works cleanly."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Run lock method test")

    with run.lock() as lock:
        assert lock.is_locked is True
        assert (run.run_dir / "run.lock").exists()

    assert not (run.run_dir / "run.lock").exists()


def test_run_manager_acquire_run_lock(tmp_path):
    """Verify RunManager.acquire_run_lock works cleanly."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="RunManager lock test")

    with run_mgr.acquire_run_lock(run) as lock:
        assert lock.is_locked is True
        assert (run.run_dir / "run.lock").exists()

    assert not (run.run_dir / "run.lock").exists()


# ============================================================================
# 2. DUPLICATE ACQUISITION
# ============================================================================


@contextlib.contextmanager
def locked_in_background(run):
    """Simulate another concurrent thread/process actively holding the run lock."""
    acquired_event = threading.Event()
    stop_event = threading.Event()

    def _holder():
        with run.lock():
            acquired_event.set()
            stop_event.wait()

    t = threading.Thread(target=_holder)
    t.start()
    acquired_event.wait()
    try:
        yield
    finally:
        stop_event.set()
        t.join()


def test_duplicate_acquisition_raises_ownership_error(tmp_path):
    """Verify duplicate acquisition attempts on the same run fail with RunOwnershipError."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Mutual exclusion task")

    with locked_in_background(run):
        lock2 = RunLock(run.run_dir, run_id=run.run_id)
        with pytest.raises(RunOwnershipError) as exc_info:
            lock2.acquire(timeout=0.0)

        err = exc_info.value
        assert err.run_id == run.run_id
        assert err.owner_info["pid"] == os.getpid()
        assert err.owner_info["forge_version"] == __version__
        assert err.owner_info["schema_version"] == 1

        diag = err.format_diagnostic()
        assert f"Run '{run.run_id}' is currently locked" in diag
        assert f"PID:              {os.getpid()}" in diag
        assert "Hostname:" in diag
        assert "User:" in diag
        assert "Command:" in diag
        assert "Working Dir:" in diag
        assert "Started:" in diag
        assert f"Forge Version:    {__version__}" in diag

    # Once external lock is released, lock2 can acquire successfully
    lock2.acquire()
    assert lock2.is_locked is True
    lock2.release()
    assert not (run.run_dir / "run.lock").exists()


def test_independent_runs_do_not_conflict(tmp_path):
    """Verify that different runs have independent locks and can be held concurrently."""
    run_mgr = RunManager(tmp_path)
    run1 = run_mgr.create_run(task="Task 1")
    run2 = run_mgr.create_run(task="Task 2")

    with run1.lock() as l1:
        assert l1.is_locked is True
        with run2.lock() as l2:
            assert l2.is_locked is True
            assert (run1.run_dir / "run.lock").exists()
            assert (run2.run_dir / "run.lock").exists()

    assert not (run1.run_dir / "run.lock").exists()
    assert not (run2.run_dir / "run.lock").exists()


# ============================================================================
# 3. STALE LOCK RECOVERY
# ============================================================================


def test_stale_lock_recovery_dead_pid(tmp_path):
    """Verify that an abandoned lock file from a dead process is automatically recovered."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Stale lock test")
    lock_file = run.run_dir / "run.lock"

    # Simulate a crashed process that left behind a lock file
    stale_meta = {
        "pid": 9999999,
        "hostname": "crashed-host",
        "username": "deaduser",
        "tty": None,
        "command_line": ["forge", "run", "crashed task"],
        "working_directory": str(tmp_path),
        "start_timestamp": "2026-01-01T00:00:00+00:00",
        "forge_version": "1.0.0",
    }
    lock_file.write_text(json.dumps(stale_meta), encoding="utf-8")

    # New process attempts acquisition
    lock = RunLock(run.run_dir, run_id=run.run_id)
    lock.acquire()

    try:
        assert lock.is_locked is True
        assert lock.is_stale_recovered is True
        assert lock.stale_metadata["pid"] == 9999999
        assert lock.stale_metadata["command_line"] == ["forge", "run", "crashed task"]

        # Verify on-disk file was overwritten with new metadata
        new_data = json.loads(lock_file.read_text(encoding="utf-8"))
        assert new_data["pid"] == os.getpid()
    finally:
        lock.release()

    assert not lock_file.exists()


def test_stale_lock_recovery_corrupt_metadata(tmp_path):
    """Verify recovery when a prior crashed process left corrupted or truncated metadata."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Corrupt stale lock test")
    lock_file = run.run_dir / "run.lock"

    # Malformed JSON
    lock_file.write_text("{\"pid\": 12345, \"incomplete\": ", encoding="utf-8")

    lock = RunLock(run.run_dir, run_id=run.run_id)
    lock.acquire()

    try:
        assert lock.is_locked is True
        assert lock.is_stale_recovered is True
        assert "raw" in lock.stale_metadata

        new_data = json.loads(lock_file.read_text(encoding="utf-8"))
        assert new_data["pid"] == os.getpid()
    finally:
        lock.release()

    assert not lock_file.exists()


def test_stale_lock_recovery_empty_file(tmp_path):
    """Verify recovery when a 0-byte lock file was left behind."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Empty lock file test")
    lock_file = run.run_dir / "run.lock"
    lock_file.touch()

    lock = RunLock(run.run_dir, run_id=run.run_id)
    lock.acquire()

    try:
        assert lock.is_locked is True
        new_data = json.loads(lock_file.read_text(encoding="utf-8"))
        assert new_data["pid"] == os.getpid()
    finally:
        lock.release()

    assert not lock_file.exists()


# ============================================================================
# 4. CLEANUP (EXCEPTIONS, SYS.EXIT, SIGTERM, SIGINT)
# ============================================================================


def test_cleanup_on_exception(tmp_path):
    """Verify lock is released and unlinked when an exception occurs inside context."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Exception cleanup test")
    lock_file = run.run_dir / "run.lock"

    with pytest.raises(ZeroDivisionError):
        with RunLock(run.run_dir, run_id=run.run_id):
            assert lock_file.exists()
            _ = 1 / 0

    assert not lock_file.exists()


def test_cleanup_on_system_exit(tmp_path):
    """Verify lock is released and unlinked when sys.exit is invoked."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="SystemExit cleanup test")
    lock_file = run.run_dir / "run.lock"

    with pytest.raises(SystemExit):
        with RunLock(run.run_dir, run_id=run.run_id):
            assert lock_file.exists()
            sys.exit(2)

    assert not lock_file.exists()


def test_cleanup_on_sigterm_cli_subprocess(tmp_path):
    """Verify that receiving SIGTERM at the CLI boundary cleans up the lock file before process terminates."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="SIGTERM cleanup test")
    lock_file = run.run_dir / "run.lock"

    code = f"""
import time, sys
from pathlib import Path
from forge.cli import _setup_cli_sigterm_handler
from forge.storage.run_lock import RunLock

_setup_cli_sigterm_handler()
lock = RunLock(Path({repr(str(run.run_dir))}), run_id="{run.run_id}")
lock.acquire()
print("ACQUIRED", flush=True)
time.sleep(30)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Wait until lock is acquired
        line = proc.stdout.readline().strip()
        assert line == "ACQUIRED"
        assert lock_file.exists()
        assert RunLock.is_run_locked(run.run_dir) is True

        # Send SIGTERM
        proc.terminate()
        proc.wait(timeout=5)

        # File must be cleaned up gracefully!
        assert not lock_file.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_unclean_termination_kernel_flock_release_and_stale_recovery(tmp_path):
    """Verify that when a process terminates uncleanly (SIGKILL), kernel lock is freed and next run recovers stale lock."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Unclean kill test")
    lock_file = run.run_dir / "run.lock"

    code = f"""
import time, sys
from pathlib import Path
from forge.storage.run_lock import RunLock

lock = RunLock(Path({repr(str(run.run_dir))}), run_id="{run.run_id}")
lock.acquire()
print("ACQUIRED", flush=True)
time.sleep(30)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        line = proc.stdout.readline().strip()
        assert line == "ACQUIRED"
        assert lock_file.exists()
        assert RunLock.is_run_locked(run.run_dir) is True
        crashed_pid = proc.pid

        # Hard kill (SIGKILL) - simulates crash / out-of-memory / sudden termination
        proc.kill()
        proc.wait(timeout=5)

        # 1. Lock file remains on disk because process died abruptly
        assert lock_file.exists()

        # 2. Kernel automatically released the OS lock!
        assert RunLock.is_run_locked(run.run_dir) is False

        # 3. Next process cleanly acquires and recovers the stale lock
        new_lock = RunLock(run.run_dir, run_id=run.run_id)
        new_lock.acquire()
        try:
            assert new_lock.is_locked is True
            assert new_lock.is_stale_recovered is True
            assert new_lock.stale_metadata["pid"] == crashed_pid
            assert new_lock.stale_metadata["schema_version"] == 1
        finally:
            new_lock.release()

        # Clean release removes lock file
        assert not lock_file.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_reentrant_lock_acquisition_same_thread(tmp_path):
    """Verify RunLock is safely re-entrant within the same thread using depth counter."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Re-entrancy test")
    lock_file = run.run_dir / "run.lock"

    lock1 = RunLock(run.run_dir, run_id=run.run_id)
    lock1.acquire()
    assert lock1.is_locked is True
    assert lock1._depth == 1
    assert lock_file.exists()

    # Nested acquisition on the same run
    lock2 = RunLock(run.run_dir, run_id=run.run_id)
    lock2.acquire()
    assert lock2.is_locked is True
    assert lock2._depth == 2

    # Release inner - depth drops to 1, lock remains active
    lock2.release()
    assert lock1.is_locked is True
    assert lock1._depth == 1
    assert lock_file.exists()
    assert RunLock.is_run_locked(run.run_dir) is True

    # Release outer - depth drops to 0, underlying OS lock and file released
    lock1.release()
    assert lock1.is_locked is False
    assert lock1._depth == 0
    assert not lock_file.exists()


def test_cleanup_on_sigint_subprocess(tmp_path):
    """Verify that receiving SIGINT (Ctrl+C) cleans up the lock file."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="SIGINT cleanup test")
    lock_file = run.run_dir / "run.lock"

    code = f"""
import time, sys
from pathlib import Path
from forge.storage.run_lock import RunLock

try:
    with RunLock(Path({repr(str(run.run_dir))}), run_id="{run.run_id}"):
        print("ACQUIRED", flush=True)
        time.sleep(30)
except KeyboardInterrupt:
    sys.exit(130)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        line = proc.stdout.readline().strip()
        assert line == "ACQUIRED"
        assert lock_file.exists()

        # Send SIGINT (Ctrl+C)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)

        assert not lock_file.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ============================================================================
# 5. CONCURRENT ACQUISITION
# ============================================================================


def _worker_attempt_lock(run_dir_str: str, run_id: str, success_list, fail_list):
    """Worker function for multiprocessing concurrent race test."""
    try:
        lock = RunLock(Path(run_dir_str), run_id=run_id)
        lock.acquire(timeout=0.0)
        success_list.append(os.getpid())
        # Hold lock briefly to force collision with concurrent workers
        time.sleep(0.6)
        lock.release()
    except RunOwnershipError:
        fail_list.append(os.getpid())
    except Exception as e:
        fail_list.append(f"ERR: {e}")


def test_concurrent_acquisition_multiprocessing_race(tmp_path):
    """Verify that among multiple concurrent OS processes, exactly ONE process wins ownership at a time."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Concurrent race test")

    manager = multiprocessing.Manager()
    successes = manager.list()
    failures = manager.list()

    num_workers = 8
    processes = []
    for _ in range(num_workers):
        p = multiprocessing.Process(
            target=_worker_attempt_lock,
            args=(str(run.run_dir), run.run_id, successes, failures),
        )
        processes.append(p)

    for p in processes:
        p.start()

    for p in processes:
        p.join(timeout=10)

    # Exactly 1 process should have acquired the non-blocking lock; the rest 7 should have failed
    assert len(successes) == 1, f"Expected 1 winner, got {len(successes)}: {list(successes)}"
    assert len(failures) == num_workers - 1
    assert not (run.run_dir / "run.lock").exists()


# ============================================================================
# 6. REGRESSIONS
# ============================================================================


def test_delete_run_fails_when_locked(tmp_path):
    """Verify RunManager.delete_run refuses to delete a run that is actively locked."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Delete protection test")

    with locked_in_background(run):
        with pytest.raises(RunOwnershipError) as exc_info:
            run_mgr.delete_run(run.run_id)
        assert f"Cannot delete run '{run.run_id}'" in str(exc_info.value)
        # Directory must still exist!
        assert run.run_dir.exists()

    # Once unlocked, delete_run succeeds
    assert run_mgr.delete_run(run.run_id) is True
    assert not run.run_dir.exists()


def test_cli_duplicate_run_execution_aborts_with_diagnostic(tmp_path, monkeypatch):
    """Verify CLI aborts cleanly with diagnostic when another process owns the targeted run."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    # Initialize Forge project
    runner.invoke(main, ["init"])

    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Architect system")

    # Lock the run in an external lock handle
    with locked_in_background(run):
        # Attempt to run planner on this locked run
        result = runner.invoke(main, ["planner", "--run", run.run_id])

        assert result.exit_code != 0
        output = result.output
        assert "Run Ownership Conflict" in output
        assert run.run_id in output
        assert "Owner Details:" in output
        assert str(os.getpid()) in output
        assert "Aborting execution" in output


def test_cli_pipeline_run_duplicate_aborts_with_diagnostic(tmp_path, monkeypatch):
    """Verify forge run --run <id> aborts when run is currently owned."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    runner.invoke(main, ["init"])

    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Pipeline task")

    with locked_in_background(run):
        result = runner.invoke(main, ["run", "--run", run.run_id])
        assert result.exit_code != 0
        assert "Run Ownership Conflict" in result.output
        assert run.run_id in result.output
        assert str(os.getpid()) in result.output


def test_cli_auto_pipeline_duplicate_aborts_with_diagnostic(tmp_path, monkeypatch):
    """Verify forge auto --run <id> aborts when run is currently owned."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    runner.invoke(main, ["init"])

    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Autonomous task")

    with locked_in_background(run):
        result = runner.invoke(main, ["auto", "--run", run.run_id])
        assert result.exit_code != 0
        assert "Run Ownership Conflict" in result.output
        assert run.run_id in result.output
        assert str(os.getpid()) in result.output


def test_release_is_idempotent(tmp_path):
    """Verify calling release multiple times is safe and idempotent."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Idempotence test")

    lock = RunLock(run.run_dir, run_id=run.run_id)
    lock.acquire()
    assert lock.is_locked is True

    lock.release()
    assert lock.is_locked is False

    # Second release call must not raise any error
    lock.release()
    assert lock.is_locked is False


def test_acquire_with_timeout_succeeds_after_release(tmp_path):
    """Verify acquire(timeout=...) succeeds if prior lock is released before timeout expires."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Timeout test")

    lock1 = RunLock(run.run_dir, run_id=run.run_id)
    lock1.acquire()

    def release_delayed():
        time.sleep(0.1)
        lock1.release()

    import threading
    t = threading.Thread(target=release_delayed)
    t.start()

    lock2 = RunLock(run.run_dir, run_id=run.run_id)
    lock2.acquire(timeout=2.0)
    t.join()

    try:
        assert lock2.is_locked is True
    finally:
        lock2.release()


def test_acquire_creates_directory_if_not_existing(tmp_path):
    """Verify RunLock creates parent directory if not already created."""
    new_dir = tmp_path / "subdir" / "run-999"
    assert not new_dir.exists()

    with RunLock(new_dir, run_id="run-999") as lock:
        assert lock.is_locked is True
        assert new_dir.exists()
        assert (new_dir / "run.lock").exists()

    assert not (new_dir / "run.lock").exists()
