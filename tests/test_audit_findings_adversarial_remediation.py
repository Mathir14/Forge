"""Adversarial regression test suite for audit findings remediation (v0.1.0b10).

Covers:
- F-001 (CRITICAL): Resumed Run Auto-Commit Stale Baseline User Data Leakage
- F-002 (HIGH): Resumed Run Task Modification Silently Lost in Metadata & Prompts
- F-003 (HIGH): Adapter Subprocess Cancellation Failure via Missing Instance Registration
- F-004 (HIGH): Crashed / Incomplete Run Falsely Reported as APPROVED / COMPLETED in Dashboard
- F-005 (HIGH): Unconditional _kill_process_group in Adapter Exit Path Incurs PID Reuse Kill Risk
- F-006 (MEDIUM): TesterEngine Uncancellable Loop, Telemetry Blackout, and Dashboard Report Loss
- F-007 (LOW): Non-Idempotent Dispute Record Accumulation in Knowledge Reconciler
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.base import BaseAdapter
from forge.adapters.opencode import OpenCodeAdapter
from forge.cli import auto_commit_run
from forge.core.config import Config
from forge.core.context import Context
from forge.core.events import AgentEvent, AgentEventType
from forge.core.git import GitService
from forge.core.knowledge import (
    FactStatus,
    FactType,
    KnowledgeFact,
    KnowledgeProposal,
    ProposalAction,
)
from forge.core.reconciler import KnowledgeReconciler
from forge.core.role import Role
from forge.core.run import Run
from forge.dashboard.app import DashboardApp
from forge.dashboard.model import RunModel
from forge.prompts.builder import InstructionBuilder
from forge.storage.knowledge import KnowledgeStore
from forge.storage.run_lock import RunLock
from forge.storage.run_manager import RunManager
from forge.testing.budget import TestingBudget
from forge.testing.engine import TesterEngine


# ==============================================================================
# F-001: Resumed Run Auto-Commit Stale Baseline User Data Leakage
# ==============================================================================

def test_f001_zero_stages_executed_does_not_commit_unrelated_changes(tmp_path):
    """F-001: If a run is resumed where all stages are already complete (0 stages executed),
    unrelated developer modifications in the working tree must NEVER be auto-committed.
    """
    # Initialize a test git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)

    initial_file = tmp_path / "README.md"
    initial_file.write_text("# Project\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True, capture_output=True)

    git_svc = GitService(tmp_path)
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Initial task")

    # Save all 5 canonical stages as complete/approved
    rm.save_stage_artifacts(run, sequence_number=1, role_name="architect", markdown_content="# Architect", json_data={"status": "APPROVED"})
    rm.save_stage_artifacts(run, sequence_number=2, role_name="planner", markdown_content="# Planner", json_data={"status": "READY"})
    rm.save_stage_artifacts(run, sequence_number=3, role_name="executor", markdown_content="# Executor", json_data={"status": "COMPLETE"})
    rm.save_stage_artifacts(run, sequence_number=4, role_name="tester", markdown_content="# Tester", json_data={"status": "PASS"})
    rm.save_stage_artifacts(run, sequence_number=5, role_name="reviewer", markdown_content="# Reviewer", json_data={"status": "APPROVED"})
    run.status = "APPROVED"
    run.save_metadata()

    # Developer creates unrelated files after the run
    unrelated_untracked = tmp_path / "secret_developer_notes.txt"
    unrelated_untracked.write_text("Confidential user data\n", encoding="utf-8")

    initial_file.write_text("# Project\nUnrelated developer manual edit\n", encoding="utf-8")

    # Invariant check: auto_commit_run with 0 stages executed in current session must return False
    committed = auto_commit_run(git=git_svc, task_summary="Initial task", run=run, stages_executed=0)
    assert not committed, "auto_commit_run must refuse to commit when 0 stages executed!"

    # Verify unrelated files remain uncommitted
    status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True, text=True)
    assert "secret_developer_notes.txt" in status_proc.stdout
    assert "README.md" in status_proc.stdout

    # Verify no commit was created
    log_proc = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, check=True, capture_output=True, text=True)
    assert "Initial commit" in log_proc.stdout
    assert "Forge" not in log_proc.stdout


def test_f001_auto_commit_defense_in_depth_already_committed_flag(tmp_path):
    """F-001: auto_commit_run marks metadata['auto_committed'] and refuses duplicate commit."""
    git_svc = MagicMock()
    git_svc.is_git_repo.return_value = True
    git_svc.has_staged_changes.return_value = False
    attribution_mock = MagicMock()
    attribution_mock.pure_forge_changes = ["forge.py"]
    attribution_mock.mixed_ownership_changes = []
    git_svc.attribute_changes.return_value = attribution_mock
    git_svc.commit.return_value = "commit123"

    run = MagicMock()
    run.run_id = "run-001"
    run.task = "Test Task"
    run.run_dir = tmp_path
    run.metadata = {}
    run._stages_executed_in_session = 1

    # First auto_commit succeeds
    assert auto_commit_run(git=git_svc, task_summary="Test Task", run=run, stages_executed=1) == "commit123"
    assert run.metadata.get("auto_committed") is True

    # Second auto_commit fails due to idempotency / already-committed guard
    assert auto_commit_run(git=git_svc, task_summary="Test Task", run=run, stages_executed=1) is False


# ==============================================================================
# F-002: Resumed Run Task Modification Silently Lost in Metadata & Prompts
# ==============================================================================

def test_f002_resumed_task_updates_metadata_and_prompts(tmp_path):
    """F-002: Updating run.task must synchronize _initial_task, persist to metadata.json,
    and be used by InstructionBuilder.build().
    """
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Original Task v1")

    assert run.task == "Original Task v1"
    assert run._initial_task == "Original Task v1"

    # User modifies task on resumed run
    run.task = "New Resumed Task v2"
    assert run.task == "New Resumed Task v2"
    assert run._initial_task == "New Resumed Task v2"

    # Save metadata and verify persistence
    run.save_metadata()
    meta = json.loads((run.run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["task"] == "New Resumed Task v2"

    # Verify InstructionBuilder uses the updated task
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )
    role = Role.load("architect", project_root=tmp_path, sequence_number=1)
    instruction = InstructionBuilder.build(context, role)
    assert "New Resumed Task v2" in instruction.task
    assert "Original Task v1" not in instruction.task


def test_f002_auto_repair_feedback_preserves_clean_task(tmp_path):
    """F-002: Recording auto-repair feedback via set_auto_repair_feedback preserves canonical task and fingerprint."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Core Feature")
    old_fp = run.task_fingerprint

    run.set_auto_repair_feedback("### Auto-Repair Feedback (Attempt 2/3)\nFix the linter errors.")
    assert run.repair_feedback is not None
    assert "Auto-Repair Feedback" in run.repair_feedback
    assert run.task == "Core Feature"
    assert run.task_fingerprint == old_fp


# ==============================================================================
# F-003: Adapter Subprocess Cancellation Failure via Missing Instance Registration
# ==============================================================================

def test_f003_adapter_subprocess_instance_registration_and_cancellation():
    """F-003: Subprocess spawned via adapter._run_subprocess must be registered in
    adapter._active_procs and terminated when adapter.cancel() is invoked.
    """
    adapter = AntigravityAdapter()

    # Launch a long-running subprocess in a worker thread
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    thread_result = []

    def run_worker():
        try:
            out, err, code = adapter._run_subprocess(cmd)
            thread_result.append(code)
        except Exception as e:
            thread_result.append(e)

    t = threading.Thread(target=run_worker, daemon=True)
    t.start()

    # Wait for process to spawn and be registered
    max_wait = time.time() + 5.0
    while not adapter._active_procs and time.time() < max_wait:
        time.sleep(0.05)

    try:
        assert len(adapter._active_procs) == 1, "Subprocess must be tracked in adapter._active_procs"
        proc = list(adapter._active_procs)[0]
        assert proc.poll() is None, "Subprocess must be actively running"

        # Invoke adapter cancellation
        adapter.cancel()

        # Thread must terminate promptly
        t.join(timeout=3.0)
        assert not t.is_alive(), "Worker thread must exit after adapter.cancel()"
        assert proc.poll() is not None, "Subprocess must be terminated"
        assert len(adapter._active_procs) == 0, "Subprocess must be unregistered"
    finally:
        adapter.cancel()


def test_f003_class_level_run_subprocess_fallback():
    """F-003: BaseAdapter._run_subprocess can still be called at class level."""
    cmd = [sys.executable, "-c", "print('hello')"]
    stdout, stderr, code = BaseAdapter._run_subprocess(cmd)
    assert code == 0
    assert "hello" in stdout


# ==============================================================================
# F-004: Crashed / Incomplete Run Falsely Reported as APPROVED / COMPLETED
# ==============================================================================

def test_f004_crashed_run_with_earlier_success_is_reported_incomplete(tmp_path):
    """F-004: A run where Architect succeeded but Engineer/Planner crashed (without failure artifact)
    must NOT be marked APPROVED or COMPLETED by the dashboard. It must be INCOMPLETE.
    """
    run_dir = tmp_path / ".forge" / "runs" / "run-crash-test"
    run_dir.mkdir(parents=True)

    # Metadata says IN_PROGRESS
    metadata = {
        "run_id": "run-crash-test",
        "task": "Build feature",
        "status": "IN_PROGRESS",
        "pipeline_type": "auto",
        "created_at": "2026-10-08T00:00:00Z",
    }
    (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    # Only 01_architect succeeded
    from forge.core.run import compute_task_fingerprint
    (run_dir / "01_architect.json").write_text(json.dumps({
        "status": "APPROVED",
        "role": "architect",
        "sequence_number": 1,
        "run_id": "run-crash-test",
        "task_fingerprint": compute_task_fingerprint("Build feature"),
        "duration_seconds": 5.0,
    }), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Architect\nApproved architecture.", encoding="utf-8")

    # Reconstruct run model
    run_model = RunModel.from_dir(run_dir)
    assert run_model.status == "IN_PROGRESS"

    # Instantiate DashboardApp
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model

    # Apply terminal state post-mortem
    app._apply_terminal_state()

    assert app.run_model.status == "INCOMPLETE", f"Expected INCOMPLETE but got {app.run_model.status}"
    assert "planner" in app.terminal_reason.lower() or "did not reach a terminal state" in app.terminal_reason.lower()


# ==============================================================================
# F-005: Unconditional _kill_process_group in Adapter Exit Path Incurs PID Reuse Kill Risk
# ==============================================================================

def test_f005_normal_exit_does_not_invoke_kill_process_group():
    """F-005: When a subprocess exits cleanly (exit code 0), _kill_process_group must NOT be called."""
    adapter = AntigravityAdapter()

    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0
    mock_proc.pid = 12345
    mock_proc.stdout = None
    mock_proc.stderr = None

    with patch.object(adapter, "_kill_process_group") as mock_kill, \
         patch.object(adapter, "_run_subprocess", return_value=("", "", 0)):

        events = list(adapter.iter_events(prompt="test prompt", cwd=Path.cwd()))

        # _kill_process_group must NOT be called for a cleanly exited process
        mock_kill.assert_not_called()


# ==============================================================================
# F-006: TesterEngine Uncancellable Loop, Telemetry Blackout, and Dashboard Report Loss
# ==============================================================================

def test_f006_tester_engine_aborts_immediately_on_abort_event(tmp_path):
    """F-006: Setting abort_event causes TesterEngine to exit cleanly with code 130 and BLOCKED."""
    (tmp_path / "app.py").write_text("print('App')", encoding="utf-8")
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Test cancellation")

    abort_ev = threading.Event()
    abort_ev.set()  # Pre-abort

    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
        abort_event=abort_ev,
    )

    engine = TesterEngine(context=context, run_manager=rm)
    res = engine.run()

    assert res.response.exit_code == 130
    assert res.machine_report.status == "BLOCKED"
    assert not res.success


def test_f006_tester_engine_emits_telemetry_events(tmp_path):
    """F-006: TesterEngine dispatches AgentEvents to context.event_listener."""
    (tmp_path / "main.py").write_text("import sys\nprint('Running')", encoding="utf-8")
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Test telemetry emission")

    captured_events = []

    def listener(event: AgentEvent):
        captured_events.append(event)

    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
        event_listener=listener,
    )

    engine = TesterEngine(context=context, run_manager=rm)
    stage_res = engine.run()

    assert len(captured_events) > 0
    event_types = [e.event_type for e in captured_events]
    assert AgentEventType.CHUNK in event_types


def test_f006_tester_engine_persists_machine_report_for_dashboard(tmp_path):
    """F-006: TesterEngine saves machine_report into json_data and Dashboard model parses it."""
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Validate calculator")

    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    engine = TesterEngine(context=context, run_manager=rm)
    stage_res = engine.run()

    assert (run.run_dir / "04_tester.json").exists()
    json_data = json.loads((run.run_dir / "04_tester.json").read_text(encoding="utf-8"))
    assert "machine_report" in json_data
    assert json_data["machine_report"]["role"] == "TESTER"

    # Dashboard parses the stage
    model = RunModel.from_dir(run.run_dir)
    tester_stage = model.get_stage("tester")
    assert tester_stage is not None
    assert tester_stage.machine_report is not None
    assert tester_stage.machine_report.role == "TESTER"


# ==============================================================================
# F-007: Non-Idempotent Dispute Record Accumulation in Knowledge Reconciler
# ==============================================================================

def test_f007_reconciler_dispute_accumulation_is_idempotent(tmp_path):
    """F-007: Reconciling duplicate dispute proposals must not accumulate duplicate dispute records."""
    store = KnowledgeStore(tmp_path)
    initial_fact = KnowledgeFact(
        id="fact-arch-01",
        type=FactType.ARCHITECTURE.value,
        title="Microservice Boundary",
        status=FactStatus.VERIFIED.value,
        summary="Initial boundary",
    )
    store.save_fact(initial_fact)

    reconciler = KnowledgeReconciler(project_root=tmp_path, store=store)

    proposal = KnowledgeProposal(
        id="fact-arch-01",
        action=ProposalAction.DISPUTE.value,
        role="tester",
        note="Monolith tightly coupled",
        evidence=["logs/test.log"],
    )

    run_dir = tmp_path / "run_test"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Reconcile multiple times
    reconciler.reconcile_run("run-101", run_dir, proposals=[proposal])
    reconciler.reconcile_run("run-101", run_dir, proposals=[proposal])
    reconciler.reconcile_run("run-101", run_dir, proposals=[proposal])

    fact_after = store.get_fact("fact-arch-01")
    assert fact_after is not None
    assert fact_after.status == FactStatus.DISPUTED.value
    # Invariant: exactly 1 dispute record exists, not 3
    assert len(fact_after.disputes) == 1
    assert fact_after.disputes[0]["claim"] == "Monolith tightly coupled"
    assert fact_after.disputes[0]["evidence"] == ["logs/test.log"]
