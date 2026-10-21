"""Focused regression test suite for Antigravity Post-Terminal Process Lifecycle.

Addresses root causes and fixes from Stress-Testing-Framework run-003:
1. Valid terminal event followed by short delay before natural process exit.
2. Process that remains alive beyond the bounded grace period.
3. Descendant that retains stdout or stderr after the root exits.
4. Cleanup failure or ambiguous process identity (PID reuse protection).
5. Repeated cleanup and recovery without duplicate signals or false success.
6. Nonzero exit or invalid artifact fails despite valid terminal event.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.base import BaseAdapter
from forge.core.config import Config
from forge.core.context import Context
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.core.git import GitService
from forge.core.platform import (
    IS_POSIX,
    get_process_identity,
    is_pid_alive,
    request_graceful_shutdown,
    terminate_process_tree,
    verify_process_identity,
)
from forge.core.process import (
    ProcessLifecycleState,
    ProcessRecord,
    ProcessTracker,
)
from forge.core.role import Role
from forge.stages.stage import Stage
from forge.storage.run_manager import Run, RunManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(tmp_path: Path) -> tuple[Context, RunManager, Run]:
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Test post-terminal process lifecycle")
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )
    return context, run_mgr, run


def _setup_critic_prerequisites(run_mgr: RunManager, run: Run) -> None:
    """Populate prerequisite verification deliverables for closing Critic."""
    run_mgr.save_stage_artifacts(
        run=run,
        sequence_number=4,
        role_name="tester",
        markdown_content="# Tester Report\n```yaml\nROLE: TESTER\nSTATUS: PASSED\nHANDOFF: REVIEWER\n```",
        json_data={"role": "tester", "status": "PASSED", "exit_code": 0, "machine_report": {"is_valid": True, "status": "PASSED"}},
        adapter_name="antigravity",
    )
    run_mgr.save_stage_artifacts(
        run=run,
        sequence_number=5,
        role_name="reviewer",
        markdown_content="# Reviewer Report\n```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: CRITIC\n```",
        json_data={"role": "reviewer", "status": "APPROVED", "exit_code": 0, "machine_report": {"is_valid": True, "status": "APPROVED"}},
        adapter_name="antigravity",
    )


def _make_role(name: str = "critic", seq: int = 6) -> Role:
    return Role(
        name=name,
        sequence_number=seq,
        template_content=f"{name.capitalize()} template",
        protocol_content="Emit machine report.",
    )


def _create_mock_script(tmp_path: Path, script_content: str, name: str = "mock_agy.py") -> Path:
    script_path = tmp_path / name
    script_path.write_text(f"#!/usr/bin/env python3\n{script_content}\n", encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


# ---------------------------------------------------------------------------
# 1. Valid terminal event followed by short delay before natural process exit
# ---------------------------------------------------------------------------

def test_valid_terminal_event_with_short_exit_delay_succeeds(tmp_path: Path):
    """Test 1: Process emits a complete report and terminal event, but takes a short delay
    (e.g., ~0.4s to 1.2s runtime teardown) before natural exit.
    Verifies that with the bounded grace period (default 2.0s), the adapter allows
    natural completion, captures exit code 0, and records EXIT_VERIFIED.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    valid_md = (
        "# Critic Comprehensive Audit\n\n"
        "Audit findings clean.\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    code = (
        "import json, sys, time\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(valid_md)}}}}}), flush=True)\n"
        "# Simulate agy runtime teardown (cache flush, sqlite commits, socket closes) taking 0.4s\n"
        "time.sleep(0.4)\n"
        "sys.exit(0)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_critic_teardown.py")

    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_period=2.0)
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    assert result.success is True
    assert result.status == "APPROVED"
    assert result.handoff == "NONE"
    assert result.response.exit_code == 0

    # Verify process record in tracker
    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert rec.exit_code == 0
    assert rec.terminal_event_type == "COMPLETE"


def test_decode_stream_events_waits_for_natural_exit_monotonic():
    """Unit test: _decode_stream_events honors post_terminal_grace_period monotonically."""
    adapter = AntigravityAdapter()

    t_start = time.monotonic()
    delay_until = t_start + 0.2

    def get_rc(wait_timeout=None):
        if wait_timeout and time.monotonic() < delay_until:
            time.sleep(min(wait_timeout, max(0.0, delay_until - time.monotonic())))
        if time.monotonic() >= delay_until:
            return 0
        return None

    stream = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Hello world"}}),
        json.dumps({"event": "result", "status": "success"}),
    ]

    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=get_rc,
        grace_period=1.5,
    ))

    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[1].event_type == AgentEventType.COMPLETE
    assert events[1].result.exit_code == 0


# ---------------------------------------------------------------------------
# 2. Process that remains alive beyond the bounded grace period
# ---------------------------------------------------------------------------

def test_failed_terminal_event_lingering_process_fails(tmp_path: Path):
    """Test 6: Agent emits an unsuccessful terminal result and remains alive.
    Verifies that:
    1. The successful-terminal escalation path is NOT used to convert stage to success.
    2. Adapter emits AgentEventType.ERROR.
    3. Safe cleanup terminates the lingering process.
    4. Stage validation marks the stage failed (success is False).
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    code = (
        "import json, sys, time\n"
        "print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': 'Failed audit run.'}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'status': 'failed', 'error': 'Agent failed audit'}), flush=True)\n"
        "while True:\n"
        "    time.sleep(0.5)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_failed_and_hang.py")

    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_period=0.3, drain_timeout=0.2)
    t0 = time.monotonic()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0
    assert result.success is False
    assert result.response.exit_code != 0

    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.terminal_event_type == "ERROR"
    assert not is_pid_alive(rec.pid)


# ---------------------------------------------------------------------------
# 3. Descendant that retains stdout or stderr after the root exits
# ---------------------------------------------------------------------------

def test_descendant_retains_pipe_after_root_exits(tmp_path: Path):
    """Test 3: Root process spawns a background descendant child that inherits stdout/stderr.
    The root process outputs terminal result and exits 0 immediately.
    Verifies that:
    1. Adapter does not deadlock on the inherited pipe; bounded drain completes promptly.
    2. Root process's clean exit code 0 is captured and COMPLETE is emitted.
    3. Adapter/Stage cleanup reaps the descendant child so no orphan survives.
    """
    context, run_mgr, run = _make_context(tmp_path)
    role = _make_role("architect", seq=2)

    child_pid_file = tmp_path / "child.pid"

    valid_md = (
        "# System Architecture\n\n"
        "```yaml\n"
        "ROLE: ARCHITECT\n"
        "STATUS: APPROVED\n"
        "HANDOFF: PLANNER\n"
        "```\n"
    )

    code = (
        "import json, os, subprocess, sys, time\n"
        "# Spawn long-running background child inheriting stdout & stderr\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=sys.stdout, stderr=sys.stderr)\n"
        f"with open({repr(str(child_pid_file))}, 'w') as f:\n"
        "    f.write(str(child.pid))\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'status': 'success'}), flush=True)\n"
        "# Root exits immediately with 0\n"
        "sys.exit(0)\n"
    )
    script = _create_mock_script(tmp_path, code, "agent_with_descendant.py")

    adapter = AntigravityAdapter(auto_approve=True, drain_timeout=0.2)
    t0 = time.monotonic()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)
    elapsed = time.monotonic() - t0

    # Ensure execution did not hang waiting 30s for the child sleeper
    assert elapsed < 2.5
    assert result.success is True
    assert result.status == "APPROVED"
    assert result.response.exit_code == 0

    # Read child PID and verify it was terminated during group cleanup
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text().strip())
    # Wait up to 1 second for OS process table to reflect termination if needed
    t_deadline = time.monotonic() + 1.0
    while time.monotonic() < t_deadline and is_pid_alive(child_pid):
        time.sleep(0.05)
    assert not is_pid_alive(child_pid), f"Descendant PID {child_pid} survived stage execution!"


# ---------------------------------------------------------------------------
# 4. Cleanup failure or ambiguous process identity (PID reuse protection)
# ---------------------------------------------------------------------------

def test_cleanup_refuses_to_signal_on_mismatched_identity(tmp_path: Path):
    """Test 4: terminate_process_tree and _safe_cleanup_subprocess refuse to signal
    a process if start_time_ticks does not match (PID reuse / ambiguous identity).
    Verifies that:
    1. terminate_process_tree returns False without sending signals.
    2. _safe_cleanup_subprocess marks CLEANUP_FAILED, never reporting false success.
    3. The innocent process is not killed.
    """
    # Spawn an innocent dummy process
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    dummy_pid = dummy.pid
    try:
        ident = get_process_identity(dummy_pid) or {}
        real_ticks = ident.get("start_time_ticks")

        # 1. Direct test of terminate_process_tree with mismatched expected_start_time_ticks
        bogus_ticks = 999999999 if real_ticks != 999999999 else 888888888
        success = terminate_process_tree(dummy, expected_start_time_ticks=bogus_ticks)
        assert success is False
        assert is_pid_alive(dummy_pid), "Innocent process was killed despite start time mismatch!"

        # 2. Test _safe_cleanup_subprocess with ProcessTracker recording mismatched ticks
        context, run_mgr, run = _make_context(tmp_path)
        tracker = ProcessTracker(
            run_id=run.run_id,
            stage_name="critic",
            sequence_number=6,
            adapter_name="antigravity",
            run_dir=run.run_dir,
        )
        dummy_pgid = os.getpgid(dummy_pid) if hasattr(os, "getpgid") else None
        tracker.record_launched(pid=dummy_pid, pgid=dummy_pgid)
        # Intentionally tamper the tracker's start_time_ticks to simulate PID reuse
        tracker.record.start_time_ticks = bogus_ticks
        tracker.record.save_atomic(tracker.record_file)

        # Run _safe_cleanup_subprocess
        BaseAdapter._safe_cleanup_subprocess(dummy, tracker=tracker)

        # Reload tracker record: state MUST be CLEANUP_FAILED, NOT TERMINATED
        rec = ProcessRecord.load(tracker.record_file)
        assert rec.state == ProcessLifecycleState.CLEANUP_FAILED.value
        assert "identity verification failed" in (rec.error_details or "") or "refusing" in (rec.error_details or "")
        # Process is still alive
        assert is_pid_alive(dummy_pid)
    finally:
        # Proper cleanup of dummy process
        terminate_process_tree(dummy)


def test_cleanup_failure_signal_error_recorded(tmp_path: Path):
    """Test 4b: If OS signal delivery raises an error (e.g., PermissionError),
    cleanup failure is captured and recorded as CLEANUP_FAILED, not false success.
    """
    context, run_mgr, run = _make_context(tmp_path)
    tracker = ProcessTracker(
        run_id=run.run_id,
        stage_name="critic",
        sequence_number=6,
        adapter_name="antigravity",
        run_dir=run.run_dir,
    )
    mock_proc = MagicMock()
    mock_proc.pid = 98765
    mock_proc.poll.return_value = None
    tracker.record_launched(pid=mock_proc.pid, pgid=mock_proc.pid)

    with patch("forge.adapters.base.BaseAdapter._kill_process_group", return_value=False):
        BaseAdapter._safe_cleanup_subprocess(mock_proc, tracker=tracker)

    rec = ProcessRecord.load(tracker.record_file)
    assert rec.state == ProcessLifecycleState.CLEANUP_FAILED.value
    assert "Failed to terminate process tree" in (rec.error_details or "")


# ---------------------------------------------------------------------------
# 5. Repeated cleanup and recovery without duplicate signals or false success
# ---------------------------------------------------------------------------

def test_repeated_cleanup_is_idempotent_no_duplicate_signals(tmp_path: Path):
    """Test 5a: Calling _safe_cleanup_subprocess multiple times on the same process/tracker
    is strictly idempotent: it does not dispatch duplicate signals and preserves state.
    """
    context, run_mgr, run = _make_context(tmp_path)
    tracker = ProcessTracker(
        run_id=run.run_id,
        stage_name="critic",
        sequence_number=6,
        adapter_name="antigravity",
        run_dir=run.run_dir,
    )

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    try:
        proc_pgid = os.getpgid(proc.pid) if hasattr(os, "getpgid") else None
        tracker.record_launched(pid=proc.pid, pgid=proc_pgid)

        with patch.object(BaseAdapter, "_kill_process_group", wraps=BaseAdapter._kill_process_group) as spy_kill:
            # First cleanup call: terminates process
            BaseAdapter._safe_cleanup_subprocess(proc, tracker=tracker)
            rec1 = ProcessRecord.load(tracker.record_file)
            assert rec1.state == ProcessLifecycleState.TERMINATED.value
            initial_call_count = spy_kill.call_count
            assert initial_call_count > 0

            # Second cleanup call: should be a no-op (process already dead / flagged)
            BaseAdapter._safe_cleanup_subprocess(proc, tracker=tracker)
            assert spy_kill.call_count == initial_call_count

            # Third cleanup call: still no-op
            BaseAdapter._safe_cleanup_subprocess(proc, tracker=tracker)
            assert spy_kill.call_count == initial_call_count

            rec2 = ProcessRecord.load(tracker.record_file)
            assert rec2.state == ProcessLifecycleState.TERMINATED.value
    finally:
        terminate_process_tree(proc)


def test_repeated_recovery_idempotent_no_false_success(tmp_path: Path):
    """Test 5b: run_mgr.recover_run_processes invoked repeatedly is idempotent
    and never reports false success if cleanup fails.
    """
    context, run_mgr, run = _make_context(tmp_path)

    # 1. Test clean recovery: terminates unresolved process and is idempotent
    proc_file = run.run_dir / "processes" / "06_critic.json"
    proc_file.parent.mkdir(parents=True, exist_ok=True)
    rec = ProcessRecord(
        run_id=run.run_id,
        stage_name="critic",
        sequence_number=6,
        adapter="antigravity",
        state=ProcessLifecycleState.CLEANUP_PENDING.value,
        pid=None,
    )
    rec.save_atomic(proc_file)

    first_recovery = run_mgr.recover_run_processes(run)
    assert len(first_recovery) == 1
    assert first_recovery[0].state == ProcessLifecycleState.TERMINATED.value
    assert first_recovery[0].status == "CONFIRMED"

    # Second recovery run finds nothing pending
    second_recovery = run_mgr.recover_run_processes(run)
    assert len(second_recovery) == 0

    # 2. Test failed cleanup: never reports false success
    rec_failed = ProcessRecord(
        run_id=run.run_id,
        stage_name="critic",
        sequence_number=6,
        adapter="antigravity",
        state=ProcessLifecycleState.RUNNING.value,
        pid=99999,
        pgid=99999,
    )
    rec_failed.save_atomic(proc_file)

    with patch("forge.core.platform.terminate_process_tree", return_value=False), \
         patch("forge.core.platform.is_pid_alive", return_value=True), \
         patch("forge.core.platform.verify_process_identity", return_value=True):

        failed_recovery = run_mgr.recover_run_processes(run)
        assert len(failed_recovery) == 1
        # MUST NOT claim TERMINATED or EXIT_VERIFIED
        assert failed_recovery[0].state == ProcessLifecycleState.CLEANUP_FAILED.value
        assert failed_recovery[0].status == "UNRESOLVED"
        assert failed_recovery[0].state != ProcessLifecycleState.EXIT_VERIFIED.value
        assert failed_recovery[0].state != ProcessLifecycleState.TERMINATED.value

        persisted = ProcessRecord.load(proc_file)
        assert persisted.state == ProcessLifecycleState.CLEANUP_FAILED.value


# ---------------------------------------------------------------------------
# 6. Nonzero exit or invalid artifact fails despite valid terminal event
# ---------------------------------------------------------------------------

def test_nonzero_exit_after_valid_terminal_event_fails(tmp_path: Path):
    """Test 6a: Process outputs a valid terminal event and complete markdown report,
    but exits with non-zero exit code (e.g. 1).
    Verifies that clean terminal event delivery does NOT bypass process exit-code verification.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    valid_md = (
        "# Critic Audit\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    code = (
        "import json, sys\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(valid_md)}}}}}), flush=True)\n"
        "# Teardown crashes or returns non-zero code\n"
        "sys.exit(1)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_crash_exit1.py")

    adapter = AntigravityAdapter(auto_approve=True)
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    # Stage MUST fail
    assert result.success is False
    assert result.response.exit_code == 1
    assert result.machine_report.exit_code == 1

    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.exit_code == 1
    # Exit verified with non-zero code marks stage as failed
    assert rec.terminal_event_type == "COMPLETE"


def test_invalid_machine_report_fails_despite_clean_exit(tmp_path: Path):
    """Test 6b: Process exits with code 0 and emits valid terminal event,
    but the emitted report violates protocol schema (e.g. invalid handoff).
    Verifies that zero exit code does NOT bypass artifact and machine report validation.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    invalid_md = (
        "# Critic Audit\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: INVALID_TARGET\n"
        "```\n"
    )

    code = (
        "import json, sys\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(invalid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(invalid_md)}}}}}), flush=True)\n"
        "sys.exit(0)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_invalid_handoff.py")

    adapter = AntigravityAdapter(auto_approve=True)
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    # Process exited with 0, but stage MUST fail due to protocol validation error
    assert result.response.exit_code == 0
    assert result.success is False
    assert result.machine_report.is_valid is False
    assert any("not in allowed handoffs" in err for err in result.machine_report.validation_errors)


# ---------------------------------------------------------------------------
# 7. Post-terminal teardown regression & configurable grace period tests (v0.1.0b18)
# ---------------------------------------------------------------------------

def test_natural_process_exit_after_2_5s_succeeds_under_default_configuration(tmp_path: Path):
    """Regression test: Antigravity teardown taking 2.5s succeeds cleanly under the
    default 5.0s grace period (fixing the run-003 2.0s timeout regression).
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    valid_md = (
        "# Critic Comprehensive Audit\n\n"
        "Audit findings clean.\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    code = (
        "import json, sys, time\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(valid_md)}}}}}), flush=True)\n"
        "# Teardown delay of 2.5s (exceeds old 2.0s limit, well within new 5.0s limit)\n"
        "time.sleep(2.5)\n"
        "sys.exit(0)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_critic_teardown_2_5s.py")

    # Instantiate with default configuration (no custom grace period specified)
    adapter = AntigravityAdapter(auto_approve=True)
    assert adapter.post_terminal_grace_period == 15.0
    assert adapter.post_terminal_grace_seconds == 15.0

    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    assert result.success is True
    assert result.status == "APPROVED"
    assert result.handoff == "NONE"
    assert result.response.exit_code == 0

    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert rec.exit_code == 0
    assert rec.terminal_event_type == "COMPLETE"


def test_process_exiting_before_deadline_returns_early_without_waiting_full_grace(tmp_path: Path):
    """Regression test: A process exiting naturally before its deadline is not forced
    to wait for the full grace period (monotonic early return).
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    valid_md = (
        "# Critic Comprehensive Audit\n\n"
        "Audit findings clean.\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    code = (
        "import json, sys, time\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(valid_md)}}}}}), flush=True)\n"
        "# Rapid exit after 0.2s\n"
        "time.sleep(0.2)\n"
        "sys.exit(0)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_critic_fast_exit.py")

    # Set generous 6.0s grace period
    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_seconds=6.0)
    assert adapter.post_terminal_grace_seconds == 6.0

    t0 = time.monotonic()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)
    elapsed = time.monotonic() - t0

    assert result.success is True
    assert result.response.exit_code == 0
    # Must return promptly upon natural exit, not wait anywhere near 6.0s
    assert elapsed < 2.0, f"Process waited too long ({elapsed:.2f}s) instead of returning early!"


def test_forced_termination_unverified_or_failed_fails_stage(tmp_path: Path):
    """Test 4: Forced termination cannot be verified or cleanup fails.
    When forced termination fails (cleanup function returns failure or identity mismatch),
    the stage fails, error identifies cleanup failure, and no false success occurs.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    valid_md = (
        "# Critic Audit\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    code = (
        "import json, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'status': 'success'}), flush=True)\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_forced_term_fail.py")

    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_seconds=0.3, drain_timeout=0.1)
    with patch.object(adapter, "_get_binary", return_value=str(script)), \
         patch.object(BaseAdapter, "_safe_cleanup_subprocess", return_value=False):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    # Must fail because forced termination could not be verified/performed
    assert result.success is False
    assert result.status == "FAILED"
    assert "cleanup verification failed" in result.response.stderr.lower() or "cleanup" in result.response.stderr.lower()


def test_antigravity_stderr_pollution_does_not_mask_post_terminal_timeout(tmp_path: Path):
    """Regression test: Stderr pollution (such as 'root agent idle; waiting...')
    must NOT mask cleanup failures or timeouts.
    When a process fails to exit and cleanup verification fails,
    the primary error message MUST be the cleanup failure diagnostic,
    with captured stderr included as supplementary context rather than replacing the error.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    valid_md = (
        "# Critic Audit\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    code = (
        "import json, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "# Emit informational stderr message early in execution\n"
        "sys.stderr.write('root agent idle; waiting up to 30m0s for 1 background task(s)\\n')\n"
        "sys.stderr.flush()\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'status': 'success'}), flush=True)\n"
        "# Hang past grace period\n"
        "while True:\n"
        "    time.sleep(0.5)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_stderr_pollution.py")

    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_seconds=0.3, drain_timeout=0.2)
    with patch.object(adapter, "_get_binary", return_value=str(script)), \
         patch.object(BaseAdapter, "_safe_cleanup_subprocess", return_value=False):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    assert result.success is False
    # Primary lifecycle failure MUST NOT be masked
    assert "cleanup verification failed" in result.response.stderr.lower() or "process failed to exit" in result.response.stderr.lower()
    # Captured stderr preserved as supplementary context
    assert "Captured process stderr:" in result.response.stderr
    assert "root agent idle; waiting up to 30m0s for 1 background task(s)" in result.response.stderr
    assert result.response.metadata.get("captured_stderr") is not None
    assert "root agent idle" in result.response.metadata.get("captured_stderr")


def test_antigravity_heavy_teardown_succeeds_under_default_15s_grace(tmp_path: Path):
    """Regression test: Heavy Antigravity teardown (~5.5s-6.0s) succeeds cleanly under the
    default 15.0s grace period without premature SIGTERM, and returns early upon natural exit.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    valid_md = (
        "# Critic Comprehensive Audit\n\n"
        "Audit findings clean.\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    code = (
        "import json, sys, time\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(valid_md)}}}}}), flush=True)\n"
        "# Heavy teardown taking 5.5s (exceeds previous 5.0s default, well within 15.0s default)\n"
        "time.sleep(5.5)\n"
        "sys.exit(0)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_critic_heavy_teardown.py")

    # Default configuration (15.0s grace period)
    adapter = AntigravityAdapter(auto_approve=True)
    assert adapter.post_terminal_grace_period == 15.0
    assert adapter.post_terminal_grace_seconds == 15.0

    t0 = time.monotonic()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)
    elapsed = time.monotonic() - t0

    assert result.success is True
    assert result.status == "APPROVED"
    assert result.handoff == "NONE"
    assert result.response.exit_code == 0
    # Must return promptly upon natural exit, well under the 15.0s grace period
    assert elapsed < 10.0, f"Process took {elapsed:.2f}s, expected prompt return after ~5.5s natural exit"

    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert rec.exit_code == 0
    assert rec.terminal_event_type == "COMPLETE"


# ---------------------------------------------------------------------------
# 8. Verified graceful shutdown for blocked post-terminal idle wait (run-003 fix)
# ---------------------------------------------------------------------------

def test_natural_process_exit_succeeds_without_graceful_shutdown_signal(tmp_path: Path):
    """Natural process exit after a successful terminal result succeeds without signaling.
    Verifies that when a process exits cleanly on its own within the bounded drain period,
    request_graceful_shutdown is never invoked.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    valid_md = (
        "# Critic Comprehensive Audit\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    code = (
        "import json, sys\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(valid_md)}}}}}), flush=True)\n"
        "sys.exit(0)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_natural_exit.py")

    adapter = AntigravityAdapter(auto_approve=True)
    with patch("forge.adapters.base.request_graceful_shutdown", wraps=request_graceful_shutdown) as spy_shutdown, \
         patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    assert result.success is True
    assert result.status == "APPROVED"
    assert result.response.exit_code == 0
    # Natural exit: graceful shutdown was NOT needed and never requested
    spy_shutdown.assert_not_called()

    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert rec.exit_code == 0


def test_blocked_process_after_terminal_success_shuts_down_gracefully_and_succeeds(tmp_path: Path):
    """Process blocked in idle wait after a successful terminal result:
    Graceful shutdown (SIGTERM) is requested and confirmed; valid completion succeeds.
    Simulates Antigravity CLI blocking in printmode.waitForIdle (1,800s inactivity wait).
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    valid_md = (
        "# Critic Comprehensive Audit\n\n"
        "Audit findings clean.\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    # Script simulates agy behavior:
    # Emits SUCCESS event, then enters a signal-waiting loop simulating printmode.waitForIdle.
    # Upon receiving SIGTERM, it cleanly exits (as agy does when unblocked from waitForIdle).
    code = (
        "import json, os, signal, sys, time\n"
        "def on_sigterm(signum, frame):\n"
        "    # agy runs Server.Shutdown(), flushes migrations, and exits\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, on_sigterm)\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(valid_md)}}}}}), flush=True)\n"
        "# Block in idle wait until SIGTERM arrives\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_blocked_in_wait_for_idle.py")

    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_seconds=5.0, drain_timeout=0.2)
    t0 = time.monotonic()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)
    elapsed = time.monotonic() - t0

    # Unblocked promptly via graceful shutdown (~0.3-0.8s), far under 5.0s
    assert elapsed < 3.0
    assert result.success is True
    assert result.status == "APPROVED"
    assert result.handoff == "NONE"
    assert result.response.exit_code == 0

    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert rec.exit_code == 0
    assert rec.terminal_event_type == "COMPLETE"
    assert not is_pid_alive(rec.pid)


def test_blocked_process_exiting_with_sigterm_code_is_normalized_to_success(tmp_path: Path):
    """Process blocked in idle wait that terminates with -SIGTERM (-15) or 143:
    When graceful shutdown was intentionally requested after a verified SUCCESS terminal event,
    the exit code is normalized to 0 and verified completion succeeds.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    valid_md = (
        "# Critic Comprehensive Audit\n\n"
        "Audit findings clean.\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    # Standard default SIGTERM behavior (process terminates with signal 15 -> exit code -15)
    code = (
        "import json, sys, time\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(valid_md)}}}}}), flush=True)\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_sigterm_default.py")

    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_seconds=5.0, drain_timeout=0.2)
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    assert result.success is True
    assert result.response.exit_code == 0
    assert result.status == "APPROVED"

    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    rec = ProcessRecord.load(proc_files[0])
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert rec.exit_code == 0
    assert rec.terminal_event_type == "COMPLETE"


def test_antigravity_idle_wait_escalates_to_verified_forced_kill_and_succeeds(tmp_path: Path):
    """Test 3: Antigravity-style idle wait.
    Create a controlled process that:
    - Emits a valid successful terminal result.
    - Installs a SIGTERM handler.
    - Records that SIGTERM was received.
    - Remains alive after handling SIGTERM, emulating server shutdown that does not terminate main goroutine.
    Expected:
    - Forge dispatches graceful shutdown.
    - The process remains alive through the configured grace deadline.
    - Forge escalates to forced termination.
    - Forge verifies termination of the intended process.
    - The forced-cleanup metadata is accurate.
    - The adapter emits successful completion only if all success criteria are met.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    sigterm_marker = tmp_path / "sigterm_received.txt"

    valid_md = (
        "# Critic Comprehensive Audit\n\n"
        "Audit findings clean.\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    code = (
        "import json, os, signal, sys, time\n"
        "def on_sigterm(signum, frame):\n"
        f"    with open({repr(str(sigterm_marker))}, 'w') as f:\n"
        "        f.write('SIGTERM received - server shutdown initiated\\n')\n"
        "    # Emulate agy blocking in printmode.waitForIdle: do NOT exit!\n"
        "signal.signal(signal.SIGTERM, on_sigterm)\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(valid_md)}}}}}), flush=True)\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_blocked_idle_wait.py")

    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_seconds=0.4, drain_timeout=0.1)
    t0 = time.monotonic()
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)
    elapsed = time.monotonic() - t0

    # Wait was bounded: must have terminated promptly (~0.4 - 2.0s)
    assert elapsed < 2.5
    # SIGTERM was received by the process before forced termination
    assert sigterm_marker.exists()
    assert "SIGTERM received" in sigterm_marker.read_text()

    # Stage succeeded: exit normalized to 0 and report validated
    assert result.success is True
    assert result.status == "APPROVED"
    assert result.handoff == "NONE"
    assert result.response.exit_code == 0
    assert result.response.metadata.get("graceful_shutdown") is True
    assert result.response.metadata.get("forced_cleanup") is True

    # Process record in tracker: verified exit and confirmed dead
    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert rec.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert rec.exit_code == 0
    assert rec.status == "CONFIRMED"
    assert not is_pid_alive(rec.pid)
    assert len(rec.cleanup_attempts) > 0
    assert any(attempt.get("status") == "SUCCESS" for attempt in rec.cleanup_attempts)


def test_failed_terminal_event_does_not_request_graceful_success_shutdown(tmp_path: Path):
    """Invalid or unsuccessful terminal result:
    Graceful success shutdown is NOT requested. Stage fails truthfully.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    code = (
        "import json, sys, time\n"
        "print(json.dumps({'event': 'result', 'status': 'failed', 'error': 'Agent failed to complete task'}), flush=True)\n"
        "time.sleep(0.1)\n"
        "sys.exit(1)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_failed_result.py")

    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_seconds=1.0, drain_timeout=0.2)
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    assert result.success is False
    assert result.response.exit_code != 0

    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    rec = ProcessRecord.load(proc_files[0])
    assert rec.terminal_event_type == "ERROR"


def test_request_graceful_shutdown_refuses_to_signal_on_identity_mismatch(tmp_path: Path):
    """Process identity mismatch: request_graceful_shutdown verifies identity and refuses
    to dispatch signals when start_time_ticks mismatch (PID reuse protection).
    """
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    try:
        ident = get_process_identity(dummy.pid) or {}
        real_ticks = ident.get("start_time_ticks")
        bogus_ticks = 999999999 if real_ticks != 999999999 else 888888888

        # Identity mismatch -> refuses to signal, returns False
        success = request_graceful_shutdown(dummy, expected_start_time_ticks=bogus_ticks)
        assert success is False
        assert is_pid_alive(dummy.pid), "Process was signaled despite identity mismatch!"
    finally:
        terminate_process_tree(dummy)

def test_request_graceful_shutdown_safe_guards():
    """request_graceful_shutdown safety guards: refuses to signal PID <= 1 or current PID."""
    assert request_graceful_shutdown(0) is True
    assert request_graceful_shutdown(1) is True
    assert request_graceful_shutdown(os.getpid()) is False


def test_decode_stream_events_graceful_shutdown_failed_emits_error():
    """If graceful shutdown request fails due to identity check failure,
    _decode_stream_events emits AgentEventType.ERROR.
    """
    adapter = AntigravityAdapter()

    stream = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Test"}}),
        json.dumps({"event": "result", "status": "success"}),
    ]

    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=lambda wait_timeout=None: None,  # Always alive
        request_shutdown_fn=lambda: False,  # Identity mismatch -> returns False
        grace_period=0.1,
        drain_timeout=0.05,
    ))

    error_evts = [e for e in events if e.event_type == AgentEventType.ERROR]
    assert len(error_evts) == 1
    assert "identity verification failed" in error_evts[0].text


def test_decode_stream_events_unexpected_sigterm_without_request_fails():
    """If a process exits with -15 without Forge requesting graceful shutdown,
    it is treated as non-zero error exit, not converted to success.
    """
    adapter = AntigravityAdapter()

    stream = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Test"}}),
        # Unexpected EOF before result
    ]

    # Process died with -15 unexpectedly
    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=lambda wait_timeout=None: -15,
        request_shutdown_fn=lambda: True,
        grace_period=0.1,
    ))

    error_evts = [e for e in events if e.event_type == AgentEventType.ERROR]
    assert len(error_evts) == 1
    assert error_evts[0].result.exit_code == -15


def test_invalid_machine_report_lingering_process_fails_despite_cleanup(tmp_path: Path):
    """Test 7: The terminal result is successful but required machine report validation fails.
    Process cleanup still occurs safely, but the stage remains failed.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    invalid_md = (
        "# Critic Comprehensive Audit\n\n"
        "Audit findings clean.\n\n"
        "```yaml\n"
        "ROLE: CRITIC\n"
        "STATUS: APPROVED\n"
        "HANDOFF: INVALID_TARGET\n"
        "```\n"
    )

    code = (
        "import json, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(invalid_md)}}}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'result', 'status': 'success', 'result': {{'status': 'SUCCESS', 'response': {repr(invalid_md)}}}}}), flush=True)\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_invalid_report_hang.py")

    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_seconds=0.3, drain_timeout=0.1)
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    # Machine report validation failed, so stage MUST fail
    assert result.success is False
    assert result.machine_report.is_valid is False
    assert any("not in allowed handoffs" in err for err in result.machine_report.validation_errors)

    # Process cleanup occurred safely
    proc_files = list((run.run_dir / "processes").glob("06_critic.json"))
    assert len(proc_files) == 1
    rec = ProcessRecord.load(proc_files[0])
    assert not is_pid_alive(rec.pid)


def test_unsolicited_termination_fails_stage(tmp_path: Path):
    """Test 8: Unsolicited termination.
    A process receives external SIGTERM without Forge having intentionally requested shutdown.
    Exit code is not normalized to success; stage remains failed.
    """
    context, run_mgr, run = _make_context(tmp_path)
    _setup_critic_prerequisites(run_mgr, run)
    role = _make_role("critic", seq=6)

    # Process dies from external SIGTERM before emitting any terminal result
    code = (
        "import os, signal, sys, time\n"
        "sys.stdout.write('Starting work...\\n')\n"
        "sys.stdout.flush()\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    script = _create_mock_script(tmp_path, code, "agy_unsolicited_sigterm.py")

    adapter = AntigravityAdapter(auto_approve=True, post_terminal_grace_seconds=1.0)
    with patch.object(adapter, "_get_binary", return_value=str(script)):
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
        result = stage.run(context)

    # Must fail with non-zero exit code
    assert result.success is False
    assert result.response.exit_code != 0


def test_cross_platform_process_management_invariants():
    """Test 10: Cross-platform process management.
    Verify that:
    - POSIX process-group signaling guards caller PGID, caller PID, and PID <= 1.
    - Windows process-tree termination uses supported platform abstraction.
    - Identity mismatch and cleanup failure remain correctly handled on each platform.
    """
    from forge.core.platform import (
        terminate_process_tree,
        request_graceful_shutdown,
        verify_process_identity,
    )
    # 1. Guards against caller PID and PID <= 1
    assert terminate_process_tree(0) is True
    assert terminate_process_tree(1) is True
    assert terminate_process_tree(os.getpid()) is True

    assert request_graceful_shutdown(0) is True
    assert request_graceful_shutdown(1) is True
    assert request_graceful_shutdown(os.getpid()) is False

    # 2. Identity mismatch on target process refuses termination and graceful shutdown
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    try:
        ident = get_process_identity(dummy.pid) or {}
        real_ticks = ident.get("start_time_ticks")
        bogus_ticks = 999999999 if real_ticks != 999999999 else 888888888

        assert verify_process_identity(dummy.pid, expected_start_time_ticks=bogus_ticks) is False
        assert terminate_process_tree(dummy.pid, expected_start_time_ticks=bogus_ticks) is False
        assert request_graceful_shutdown(dummy.pid, expected_start_time_ticks=bogus_ticks) is False
        assert is_pid_alive(dummy.pid)
    finally:
        terminate_process_tree(dummy)

