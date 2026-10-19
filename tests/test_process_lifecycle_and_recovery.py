"""Comprehensive test suite for Forge Process Lifecycle, Orphan Prevention, and Safe Recovery.

Covers:
1. Process Lifecycle:
   - Zero-exit successful completion
   - Clean terminal event followed by delayed exit
   - Stage hang where process survives terminal event
   - Missing terminal event with process hang
   - Adapter timeout
   - User cancellation
   - Idempotent repeated cleanup invocation
2. Descendants & Inherited Descriptors:
   - Descendant child process holding stdout pipe open after parent exits
   - Descendant child ignoring SIGTERM requiring SIGKILL escalation
   - Reader threads unblocking without deadlock when pipes close
   - Cleanup does not touch unrelated processes
3. State & Recovery:
   - Atomic persistence of process lifecycle records
   - State transition tracking
   - Process crash between launch and state persistence
   - Unresolved process discovery
   - Identity verification before cleanup
   - PID reuse protection (refusing unsafe kill)
   - Stage reconciliation with truth-grounded completion semantics
   - Idempotent repeated recovery
   - Working tree lease conflict when active agent process exists
4. Platform Compatibility:
   - Graceful fallback when /proc or process groups are unavailable
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.base import BaseAdapter, AdapterResponse, BoundedLineReader
from forge.core.config import Config
from forge.core.context import Context
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.core.git import GitService
from forge.core.platform import (
    IS_POSIX,
    get_process_identity,
    verify_process_identity,
    is_pid_alive,
    find_process_group_pids,
    terminate_process_tree,
)
from forge.core.process import (
    ProcessRecord,
    ProcessLifecycleState,
    ProcessTracker,
)
from forge.core.role import Role
from forge.stages.result import StageResult
from forge.stages.stage import Stage
from forge.storage.run_lock import WorkingTreeLease, WorkingTreeConflictError
from forge.storage.run_manager import RunManager, Run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(tmp_path: Path) -> tuple[Context, RunManager, Run]:
    """Helper to construct Context, RunManager, and Run in tmp_path."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Test process lifecycle and recovery")
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )
    return context, run_mgr, run


def _make_role(name: str = "planner", seq: int = 1) -> Role:
    return Role(
        name=name,
        sequence_number=seq,
        template_content=f"{name.capitalize()} template",
        protocol_content="Emit machine report.",
    )


def _create_mock_script(tmp_path: Path, script_content: str, name: str = "mock_agent.py") -> Path:
    script_path = tmp_path / name
    script_path.write_text(f"#!/usr/bin/env python3\n{script_content}\n", encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


# ---------------------------------------------------------------------------
# 1. Process Lifecycle Tests
# ---------------------------------------------------------------------------

def test_process_lifecycle_zero_exit_success(tmp_path: Path):
    """Test 1.1: Successful zero-exit completion records all lifecycle transitions."""
    context, run_mgr, run = _make_context(tmp_path)
    role = _make_role("planner", seq=1)

    valid_md = (
        "# Implementation Plan\n\n"
        "```yaml\n"
        "ROLE: PLANNER\n"
        "STATUS: APPROVED\n"
        "HANDOFF: ARCHITECT\n"
        "```\n"
    )

    code = (
        "import json, sys\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'status': 'success'}), flush=True)\n"
        "sys.exit(0)\n"
    )
    script = _create_mock_script(tmp_path, code, "agent_success.py")

    adapter = AntigravityAdapter()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    assert result.success is True
    assert result.status == "APPROVED"

    # Verify lifecycle records on disk
    proc_files = list((run.run_dir / "processes").glob("*.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec is not None
    assert rec.stage_name == "planner"
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert rec.exit_code == 0
    assert rec.terminal_event_type == "COMPLETE"
    assert rec.pid is not None
    assert rec.start_time_ticks is not None


def test_process_lifecycle_delayed_exit_bounded_drain(tmp_path: Path):
    """Test 1.2: Terminal event received, followed by delayed clean process exit."""
    context, run_mgr, run = _make_context(tmp_path)
    role = _make_role("architect", seq=2)

    valid_md = (
        "# System Architecture\n\n"
        "```yaml\n"
        "ROLE: ARCHITECT\n"
        "STATUS: APPROVED\n"
        "HANDOFF: PLANNER\n"
        "```\n"
    )

    code = (
        "import json, sys, time\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'status': 'success'}), flush=True)\n"
        "time.sleep(0.3)\n"
        "sys.exit(0)\n"
    )
    script = _create_mock_script(tmp_path, code, "agent_delayed.py")

    adapter = AntigravityAdapter()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    assert result.success is True
    assert result.status == "APPROVED"
    assert result.response.exit_code == 0


def test_process_lifecycle_hang_survives_terminal_event(tmp_path: Path):
    """Test 1.3: Process emits COMPLETE but hangs indefinitely.
    Bounded cleanup terminates it and stage is NOT marked successful.
    """
    context, run_mgr, run = _make_context(tmp_path)
    role = _make_role("architect", seq=2)

    valid_md = (
        "# System Architecture\n\n"
        "```yaml\n"
        "ROLE: ARCHITECT\n"
        "STATUS: APPROVED\n"
        "HANDOFF: EXECUTOR\n"
        "```\n"
    )

    code = (
        "import json, sys, time\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'status': 'success'}), flush=True)\n"
        "while True:\n"
        "    time.sleep(0.5)\n"
    )
    script = _create_mock_script(tmp_path, code, "agent_hang.py")

    adapter = AntigravityAdapter()
    t0 = time.time()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)
    elapsed = time.time() - t0

    # Cleanup must be bounded (< 3.5 seconds)
    assert elapsed < 3.5
    # Process had to be terminated by force; it did not exit cleanly with 0
    assert result.success is False

    proc_files = list((run.run_dir / "processes").glob("*.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.state in (ProcessLifecycleState.TERMINATED.value, ProcessLifecycleState.CLEANUP_FAILED.value)
    # The process PID must no longer be alive
    assert not is_pid_alive(rec.pid)


def test_process_lifecycle_missing_terminal_event_hang(tmp_path: Path):
    """Test 1.4: Process hangs without emitting any terminal event; timeout terminates it."""
    context, run_mgr, run = _make_context(tmp_path)
    role = _make_role("executor", seq=3)

    code = (
        "import json, sys, time\n"
        "print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': 'Working...'}}), flush=True)\n"
        "while True:\n"
        "    time.sleep(0.5)\n"
    )
    script = _create_mock_script(tmp_path, code, "agent_no_terminal.py")

    adapter = AntigravityAdapter(auto_approve=True)
    t0 = time.time()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        # Enforce a 1.0s timeout
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr, timeout=1.0)
        result = stage.run(context)
    elapsed = time.time() - t0

    assert elapsed < 3.5
    assert result.success is False
    assert result.status == "FAILED"
    assert result.response.exit_code == 124

    proc_files = list((run.run_dir / "processes").glob("*.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert not is_pid_alive(rec.pid)


def test_process_lifecycle_user_cancellation(tmp_path: Path):
    """Test 1.5: User cancellation via abort_event promptly halts execution and terminates process."""
    context, run_mgr, run = _make_context(tmp_path)
    abort_event = threading.Event()
    context.abort_event = abort_event
    role = _make_role("executor", seq=3)

    code = (
        "import json, sys, time\n"
        "print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': 'Starting task...'}}), flush=True)\n"
        "while True:\n"
        "    time.sleep(0.5)\n"
    )
    script = _create_mock_script(tmp_path, code, "agent_cancel.py")

    adapter = AntigravityAdapter(auto_approve=True)

    def trigger_abort():
        time.sleep(0.3)
        abort_event.set()

    t = threading.Thread(target=trigger_abort)
    t.start()

    t0 = time.time()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr, abort_event=abort_event)
        result = stage.run(context)
    elapsed = time.time() - t0
    t.join(timeout=1.0)

    assert elapsed < 3.0
    assert result.success is False
    assert result.response.exit_code == 130

    proc_files = list((run.run_dir / "processes").glob("*.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert not is_pid_alive(rec.pid)


def test_process_lifecycle_repeated_cleanup_idempotent():
    """Test 1.6: Repeated invocation of terminate_process_tree is safe and idempotent."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    pid = proc.pid
    assert is_pid_alive(pid)

    # First termination
    stopped1 = terminate_process_tree(proc, timeout=0.5)
    assert stopped1 is True
    assert not is_pid_alive(pid)

    # Second termination on already terminated process
    stopped2 = terminate_process_tree(proc, timeout=0.5)
    assert stopped2 is True

    # Third termination with just PID
    stopped3 = terminate_process_tree(pid, timeout=0.5)
    assert stopped3 is True


# ---------------------------------------------------------------------------
# 2. Descendants & Inherited Descriptors Tests
# ---------------------------------------------------------------------------

def test_descendant_child_keeps_pipes_open_parent_exits(tmp_path: Path):
    """Test 2.1: Agent process spawns a child that inherits stdout pipe, parent exits.
    Cleanup must terminate the child in the process group, releasing the pipe without hang.
    """
    context, run_mgr, run = _make_context(tmp_path)
    role = _make_role("executor", seq=3)

    valid_md = (
        "# Execution Summary\n\n"
        "```yaml\n"
        "ROLE: EXECUTOR\n"
        "STATUS: SUCCESS\n"
        "HANDOFF: TESTER\n"
        "```\n"
    )

    code = (
        "import subprocess, sys, time, json\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'], stdout=sys.stdout)\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'status': 'success'}), flush=True)\n"
        "sys.exit(0)\n"
    )
    script = _create_mock_script(tmp_path, code, "agent_descendant.py")

    adapter = AntigravityAdapter(auto_approve=True)
    t0 = time.time()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)
    elapsed = time.time() - t0

    # Must complete promptly (< 3.0s), not hang for the 15s child sleeper
    assert elapsed < 3.5

    # Ensure no lingering child in the process group
    proc_files = list((run.run_dir / "processes").glob("*.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    if rec.pgid:
        lingering = find_process_group_pids(rec.pgid)
        assert len(lingering) == 0


def test_descendant_child_ignores_sigterm_requires_sigkill():
    """Test 2.2: Child process ignores SIGTERM; terminate_process_tree escalates to SIGKILL."""
    cmd = [
        sys.executable,
        "-c",
        (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True:\n"
            "    time.sleep(0.5)\n"
        ),
    ]
    proc = subprocess.Popen(cmd, start_new_session=True)
    pid = proc.pid
    pgid = os.getpgid(pid) if hasattr(os, "getpgid") else pid
    assert is_pid_alive(pid)

    t0 = time.time()
    stopped = terminate_process_tree(proc, pgid=pgid, timeout=0.6)
    elapsed = time.time() - t0

    assert stopped is True
    assert not is_pid_alive(pid)
    assert elapsed < 2.0


def test_reader_threads_unblock_without_deadlock():
    """Test 2.3: BoundedLineReader reading from a blocked pipe cleanly unblocks when writer closes."""
    r_fd, w_fd = os.pipe()
    r_pipe = open(r_fd, "r", encoding="utf-8")
    w_pipe = open(w_fd, "w", encoding="utf-8")

    reader = BoundedLineReader(r_pipe)

    # Write one line, verify read
    w_pipe.write("Line 1\n")
    w_pipe.flush()
    line = reader.read_line(timeout=1.0)
    assert line == "Line 1\n"

    # Close write side (simulating process termination)
    w_pipe.close()

    # Reader must promptly detect EOF and unblock
    eof_line = reader.read_line(timeout=1.0)
    assert eof_line is None
    assert reader.is_eof()
    reader.close()
    r_pipe.close()


def test_cleanup_does_not_terminate_unrelated_processes():
    """Test 2.4: Process tree cleanup does not kill unrelated sibling processes."""
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    sibling_pid = sibling.pid
    sibling_pgid = os.getpgid(sibling_pid) if hasattr(os, "getpgid") else sibling_pid

    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    target_pid = target.pid
    target_pgid = os.getpgid(target_pid) if hasattr(os, "getpgid") else target_pid

    assert is_pid_alive(sibling_pid)
    assert is_pid_alive(target_pid)
    assert target_pgid != sibling_pgid

    # Clean up ONLY the target
    terminate_process_tree(target, pgid=target_pgid, timeout=0.5)

    assert not is_pid_alive(target_pid)
    # Sibling MUST still be alive!
    assert is_pid_alive(sibling_pid)

    # Clean up sibling
    terminate_process_tree(sibling, pgid=sibling_pgid, timeout=0.5)


# ---------------------------------------------------------------------------
# 3. State & Recovery Tests
# ---------------------------------------------------------------------------

def test_process_record_atomic_persistence(tmp_path: Path):
    """Test 3.1: ProcessRecord writes atomically using temporary files."""
    rec = ProcessRecord(
        run_id="run-001",
        stage_name="planner",
        sequence_number=1,
        adapter="opencode",
        pid=12345,
    )
    dest = tmp_path / "processes" / "01_planner.json"
    rec.save_atomic(dest)

    assert dest.exists()
    loaded = ProcessRecord.load(dest)
    assert loaded is not None
    assert loaded.pid == 12345
    assert loaded.run_id == "run-001"

    # Verify no dangling .tmp files
    tmp_files = list(dest.parent.glob("*.tmp*"))
    assert len(tmp_files) == 0


def test_process_tracker_state_transitions(tmp_path: Path):
    """Test 3.2: ProcessTracker correctly records every state transition."""
    tracker = ProcessTracker(
        run_id="run-002",
        stage_name="architect",
        sequence_number=2,
        adapter_name="antigravity",
        run_dir=tmp_path,
    )

    tracker.record_starting()
    rec = ProcessRecord.load(tracker.record_file)
    assert rec.state == ProcessLifecycleState.STARTING.value

    tracker.record_launched(pid=os.getpid(), pgid=os.getpid())
    rec = ProcessRecord.load(tracker.record_file)
    assert rec.state == ProcessLifecycleState.RUNNING.value
    assert rec.pid == os.getpid()

    tracker.record_terminal_event("COMPLETE")
    rec = ProcessRecord.load(tracker.record_file)
    assert rec.state == ProcessLifecycleState.TERMINAL_EVENT_RECEIVED.value
    assert rec.terminal_event_type == "COMPLETE"

    tracker.record_exit(exit_code=0)
    rec = ProcessRecord.load(tracker.record_file)
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert rec.exit_code == 0

    tracker.record_stage_completed(success=True)
    rec = ProcessRecord.load(tracker.record_file)
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value


def test_process_crash_between_launch_and_persistence(tmp_path: Path):
    """Test 3.3: Crash before PID is persisted (STARTING state) is recovered safely."""
    context, run_mgr, run = _make_context(tmp_path)
    proc_file = run.run_dir / "processes" / "01_planner.json"
    proc_file.parent.mkdir(parents=True, exist_ok=True)

    rec = ProcessRecord(
        run_id=run.run_id,
        stage_name="planner",
        sequence_number=1,
        adapter="antigravity",
        state=ProcessLifecycleState.STARTING.value,
        pid=None,
    )
    rec.save_atomic(proc_file)

    unresolved = run_mgr.discover_unresolved_processes(run)
    assert len(unresolved) == 1
    assert unresolved[0].state == ProcessLifecycleState.STARTING.value

    reconciled = run_mgr.recover_run_processes(run)
    assert len(reconciled) == 1
    assert reconciled[0].state == ProcessLifecycleState.TERMINATED.value
    assert reconciled[0].status == "CONFIRMED"


def test_recovery_verifies_identity_before_cleanup(tmp_path: Path):
    """Test 3.4: Recovery verifies creation timestamp and terminates true owned process."""
    context, run_mgr, run = _make_context(tmp_path)

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"], start_new_session=True)
    pid = proc.pid
    pgid = os.getpgid(pid) if hasattr(os, "getpgid") else pid
    ident = get_process_identity(pid) or {}

    proc_file = run.run_dir / "processes" / "02_architect.json"
    rec = ProcessRecord(
        run_id=run.run_id,
        stage_name="architect",
        sequence_number=2,
        adapter="antigravity",
        state=ProcessLifecycleState.RUNNING.value,
        pid=pid,
        pgid=pgid,
        start_time_ticks=ident.get("start_time_ticks"),
    )
    rec.save_atomic(proc_file)

    assert is_pid_alive(pid)

    reconciled = run_mgr.recover_run_processes(run)
    assert len(reconciled) == 1
    assert reconciled[0].state == ProcessLifecycleState.TERMINATED.value
    assert reconciled[0].status == "CONFIRMED"
    assert not is_pid_alive(pid)


def test_recovery_refuses_to_kill_on_pid_reuse(tmp_path: Path):
    """Test 3.5: Recovery refuses to kill a process if its start time does not match (PID reuse)."""
    context, run_mgr, run = _make_context(tmp_path)

    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    pid = dummy.pid

    proc_file = run.run_dir / "processes" / "03_executor.json"
    rec = ProcessRecord(
        run_id=run.run_id,
        stage_name="executor",
        sequence_number=3,
        adapter="antigravity",
        state=ProcessLifecycleState.RUNNING.value,
        pid=pid,
        # Intentionally mismatched start_time_ticks to simulate PID reuse
        start_time_ticks=999999999,
    )
    rec.save_atomic(proc_file)

    try:
        reconciled = run_mgr.recover_run_processes(run)
        assert len(reconciled) == 1
        assert reconciled[0].state == ProcessLifecycleState.UNRESOLVED.value
        assert reconciled[0].status == "UNRESOLVED"
        assert "refusing automated termination" in reconciled[0].error_details

        # The innocent process MUST NOT have been killed!
        assert is_pid_alive(pid)
    finally:
        terminate_process_tree(dummy)


def test_recovery_reconciles_stage_artifacts_truth(tmp_path: Path):
    """Test 3.6: Stage reconciliation does NOT claim success if exit code != 0 despite artifact presence."""
    context, run_mgr, run = _make_context(tmp_path)

    valid_md = "# Plan\n```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: ARCHITECT\n```"
    run_mgr.save_stage_artifacts(
        run=run,
        sequence_number=1,
        role_name="planner",
        markdown_content=valid_md,
        json_data={"role": "planner", "status": "APPROVED", "machine_report": {"is_valid": True}},
        adapter_name="opencode",
    )

    proc_file = run.run_dir / "processes" / "01_planner.json"
    rec = ProcessRecord(
        run_id=run.run_id,
        stage_name="planner",
        sequence_number=1,
        adapter="opencode",
        state=ProcessLifecycleState.CLEANUP_FAILED.value,
        exit_code=137,
        pid=None,
    )
    rec.save_atomic(proc_file)

    reconciled = run_mgr.recover_run_processes(run)
    assert len(reconciled) == 1
    assert reconciled[0].state != ProcessLifecycleState.EXIT_VERIFIED.value


def test_repeated_recovery_is_idempotent(tmp_path: Path):
    """Test 3.7: Running recovery multiple times produces identical state and does not crash."""
    context, run_mgr, run = _make_context(tmp_path)

    proc_file = run.run_dir / "processes" / "01_planner.json"
    rec = ProcessRecord(
        run_id=run.run_id,
        stage_name="planner",
        sequence_number=1,
        adapter="opencode",
        state=ProcessLifecycleState.CLEANUP_PENDING.value,
        pid=None,
    )
    rec.save_atomic(proc_file)

    rec1 = run_mgr.recover_run_processes(run)
    assert len(rec1) == 1
    state1 = rec1[0].state

    # Second recovery run
    rec2 = run_mgr.recover_run_processes(run)
    assert len(rec2) == 0

    reloaded = ProcessRecord.load(proc_file)
    assert reloaded.state == state1


def test_working_tree_lease_blocks_when_agent_process_active(tmp_path: Path):
    """Test 3.8: WorkingTreeLease blocks acquisition when any verified agent process is alive."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    pid = proc.pid
    pgid = os.getpgid(pid) if hasattr(os, "getpgid") else pid
    ident = get_process_identity(pid) or {}

    runs_dir = tmp_path / ".forge" / "runs" / "run-active" / "processes"
    runs_dir.mkdir(parents=True, exist_ok=True)
    rec = ProcessRecord(
        run_id="run-active",
        stage_name="executor",
        sequence_number=3,
        adapter="antigravity",
        state=ProcessLifecycleState.RUNNING.value,
        pid=pid,
        pgid=pgid,
        start_time_ticks=ident.get("start_time_ticks"),
    )
    rec.save_atomic(runs_dir / "03_executor.json")

    lease = WorkingTreeLease(
        project_root=tmp_path,
        run_id="run-competing",
        stage_name="executor",
        adapter_name="antigravity",
    )

    try:
        with pytest.raises(WorkingTreeConflictError) as exc_info:
            lease.acquire()
        assert "actively held by agent process" in str(exc_info.value)
    finally:
        terminate_process_tree(proc, pgid=pgid)


# ---------------------------------------------------------------------------
# 4. Platform Compatibility Tests
# ---------------------------------------------------------------------------

def test_platform_get_process_identity_fallback():
    """Test 4.1: get_process_identity gracefully falls back when /proc is missing or non-existent PID."""
    ident = get_process_identity(99999999)
    assert ident is None


def test_platform_verify_process_identity_safe_fallback():
    """Test 4.2: verify_process_identity gracefully handles missing metadata."""
    res = verify_process_identity(os.getpid(), expected_start_time_ticks=None, expected_pgid=None)
    assert res is True


# ---------------------------------------------------------------------------
# 5. Targeted Process Lifecycle & Recovery Safety Regressions
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not IS_POSIX, reason="Process groups (PGID) and os.getpgid are POSIX-only")
def test_recovery_dead_leader_lingering_pgid_refuses_signal_and_marks_unresolved(tmp_path: Path):
    """Regression 5.1: Dead process-group leader with lingering members marks UNRESOLVED and sends NO signal."""
    _context, run_mgr, run = _make_context(tmp_path)

    # Spawn an active lingering process in a new process group/session
    lingering = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"], start_new_session=True)
    lingering_pid = lingering.pid
    lingering_pgid = os.getpgid(lingering_pid) if hasattr(os, "getpgid") else lingering_pid

    # Ensure leader PID is dead/non-existent
    dead_leader_pid = 99999998
    assert not is_pid_alive(dead_leader_pid)

    proc_file = run.run_dir / "processes" / "01_planner.json"
    rec = ProcessRecord(
        run_id=run.run_id,
        stage_name="planner",
        sequence_number=1,
        adapter="antigravity",
        state=ProcessLifecycleState.RUNNING.value,
        pid=dead_leader_pid,
        pgid=lingering_pgid,
    )
    rec.save_atomic(proc_file)

    try:
        # First recovery attempt: must detect dead leader + lingering PGID, mark UNRESOLVED, send NO signal
        reconciled = run_mgr.recover_run_processes(run)
        assert len(reconciled) == 1
        assert reconciled[0].state == ProcessLifecycleState.UNRESOLVED.value
        assert reconciled[0].status == "UNRESOLVED"
        assert "refusing automated termination" in reconciled[0].error_details
        assert "Process group leader" in reconciled[0].error_details

        # CRITICAL ADVERSARIAL INVARIANT: Lingering process must NOT have been signaled!
        assert is_pid_alive(lingering_pid)

        # Second recovery attempt: idempotent, does not repeat signaling, stays UNRESOLVED
        reconciled2 = run_mgr.recover_run_processes(run)
        assert len(reconciled2) == 1
        assert reconciled2[0].state == ProcessLifecycleState.UNRESOLVED.value
        assert is_pid_alive(lingering_pid)
    finally:
        terminate_process_tree(lingering, pgid=lingering_pgid)


def test_concurrent_recovery_mutual_exclusion(tmp_path: Path):
    """Regression 5.2: Concurrent recovery attempts cannot race on process records due to RunLock serialization."""
    from forge.storage.run_lock import RunLock, RunOwnershipError

    _context, run_mgr, run = _make_context(tmp_path)

    # Acquire lock in an external thread simulating a concurrent running process or recovery
    hold_lock = threading.Event()
    lock_acquired = threading.Event()
    error_captured: list[Exception] = []

    def _holder_thread():
        try:
            with RunLock(run.run_dir, run_id=run.run_id):
                lock_acquired.set()
                hold_lock.wait(timeout=5.0)
        except Exception as e:
            error_captured.append(e)

    th = threading.Thread(target=_holder_thread, daemon=True)
    th.start()
    assert lock_acquired.wait(timeout=2.0)

    # Attempting recovery while lock is held by another thread must raise RunOwnershipError
    try:
        def _second_caller():
            try:
                run_mgr.recover_run_processes(run)
            except Exception as e:
                error_captured.append(e)

        th2 = threading.Thread(target=_second_caller, daemon=True)
        th2.start()
        th2.join(timeout=2.0)

        assert any(isinstance(e, RunOwnershipError) for e in error_captured)
    finally:
        hold_lock.set()
        th.join(timeout=2.0)

    # After lock is released, recovery acquires lock cleanly
    reconciled = run_mgr.recover_run_processes(run)
    assert isinstance(reconciled, list)


def test_forced_termination_fails_stage_run_even_if_machine_report_valid(tmp_path: Path):
    """Regression 5.3: Forced termination sets TERMINATED and cannot satisfy Stage.run success."""
    context, run_mgr, run = _make_context(tmp_path)
    role = _make_role("architect", seq=2)

    valid_md = (
        "# System Architecture\n\n"
        "```yaml\n"
        "ROLE: ARCHITECT\n"
        "STATUS: APPROVED\n"
        "HANDOFF: EXECUTOR\n"
        "```\n"
    )
    code = (
        "import json, sys, time\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'status': 'success'}), flush=True)\n"
        "while True:\n"
        "    time.sleep(0.5)\n"
    )
    script = _create_mock_script(tmp_path, code, "agent_hang_forced.py")

    adapter = AntigravityAdapter()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    # Forced termination MUST result in failure
    assert result.success is False
    assert result.status == "FAILED"

    proc_files = list((run.run_dir / "processes").glob("*.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.state == ProcessLifecycleState.TERMINATED.value
    assert any("failed to exit" in err or "Process lifecycle verification failed" in err for err in result.machine_report.validation_errors)


def test_complete_event_followed_by_delayed_nonzero_exit_fails_stage(tmp_path: Path):
    """Regression 5.4: COMPLETE followed by delayed nonzero exit is verified as non-successful."""
    context, run_mgr, run = _make_context(tmp_path)
    role = _make_role("planner", seq=1)

    valid_md = (
        "# Implementation Plan\n\n"
        "```yaml\n"
        "ROLE: PLANNER\n"
        "STATUS: APPROVED\n"
        "HANDOFF: ARCHITECT\n"
        "```\n"
    )
    code = (
        "import json, sys, time\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'status': 'success'}), flush=True)\n"
        "time.sleep(0.1)\n"
        "sys.exit(42)\n"
    )
    script = _create_mock_script(tmp_path, code, "agent_nonzero.py")

    adapter = AntigravityAdapter()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    # Nonzero exit MUST result in failure
    assert result.success is False
    assert result.status == "FAILED"

    proc_files = list((run.run_dir / "processes").glob("*.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert rec.exit_code == 42


def test_crash_after_complete_before_exit_verification_remains_unresolved(tmp_path: Path):
    """Regression 5.5: Interruption after COMPLETE before exit verification leaves recoverable non-success state."""
    _context, run_mgr, run = _make_context(tmp_path)

    # Simulate state when COMPLETE parsed but process crashed/killed before exit verification
    dead_pid = 99999997
    proc_file = run.run_dir / "processes" / "01_planner.json"
    rec = ProcessRecord(
        run_id=run.run_id,
        stage_name="planner",
        sequence_number=1,
        adapter="antigravity",
        state=ProcessLifecycleState.TERMINAL_EVENT_RECEIVED.value,
        terminal_event_type="COMPLETE",
        pid=dead_pid,
        exit_code=None,
    )
    rec.save_atomic(proc_file)

    reconciled = run_mgr.recover_run_processes(run)
    assert len(reconciled) == 1
    # Without verified 0 returncode, it must NOT transition to EXIT_VERIFIED
    assert reconciled[0].state != ProcessLifecycleState.EXIT_VERIFIED.value
    assert reconciled[0].state == ProcessLifecycleState.TERMINATED.value


def test_antigravity_and_opencode_exit_recording_contract(tmp_path: Path):
    """Regression 5.6: Antigravity and OpenCode enforce consistent terminal event and exit recording."""
    # Antigravity contract
    tracker_anti = ProcessTracker(run_id="run-contract", stage_name="planner", sequence_number=1, adapter_name="antigravity")
    tracker_anti.record_starting()
    tracker_anti.record_launched(pid=os.getpid())
    assert tracker_anti.record.state == ProcessLifecycleState.RUNNING.value

    # Parsing COMPLETE in-stream transitions to TERMINAL_EVENT_RECEIVED, not EXIT_VERIFIED
    tracker_anti.record_terminal_event("COMPLETE")
    assert tracker_anti.record.state == ProcessLifecycleState.TERMINAL_EVENT_RECEIVED.value
    assert tracker_anti.record.exit_code is None

    # Post-mortem verified exit transitions to EXIT_VERIFIED
    tracker_anti.record_exit(0)
    assert tracker_anti.record.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert tracker_anti.record.exit_code == 0

    # Cleanup preserving EXIT_VERIFIED
    tracker_anti.record_cleanup_start("terminate_process_tree")
    assert tracker_anti.record.state == ProcessLifecycleState.EXIT_VERIFIED.value
    tracker_anti.record_cleanup_result(success=True, method="terminate_process_tree")
    assert tracker_anti.record.state == ProcessLifecycleState.EXIT_VERIFIED.value

    # OpenCode contract
    tracker_open = ProcessTracker(run_id="run-contract", stage_name="executor", sequence_number=3, adapter_name="opencode")
    tracker_open.record_starting()
    tracker_open.record_launched(pid=os.getpid())
    assert tracker_open.record.state == ProcessLifecycleState.RUNNING.value

    tracker_open.record_terminal_event("COMPLETE")
    assert tracker_open.record.state == ProcessLifecycleState.TERMINAL_EVENT_RECEIVED.value
    assert tracker_open.record.exit_code is None

    tracker_open.record_exit(0)
    assert tracker_open.record.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert tracker_open.record.exit_code == 0

    tracker_open.record_cleanup_start("terminate_process_tree")
    assert tracker_open.record.state == ProcessLifecycleState.EXIT_VERIFIED.value
    tracker_open.record_cleanup_result(success=True, method="terminate_process_tree")
    assert tracker_open.record.state == ProcessLifecycleState.EXIT_VERIFIED.value
