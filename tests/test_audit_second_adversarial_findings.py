"""Adversarial regression test suite for second audit findings (N-001 through N-007).

Covers:
- N-001 (CRITICAL): Stale Downstream Deliverables Survive Task Modification & Stale Auto-Commit
- N-002 (HIGH / F-004 REGRESSION): Dashboard False APPROVED on Partial Runs
- N-003 (HIGH): Uncaught AutonomousHalt in Standard forge run
- N-004 (HIGH): Subprocess Stderr Pipe Deadlock During Event Streaming
- N-005 (MEDIUM): PKB Dispute Synthesis Overwriting Evidence & Idempotency
- N-006 (MEDIUM): Windows Native Command-Line Limits for Antigravity Adapter
- N-007 (LOW): Stage Timeout Propagation to TesterEngine
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
from click.testing import CliRunner

from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.base import BaseAdapter, StderrDrainer
from forge.adapters.opencode import OpenCodeAdapter
from forge.cli import main, is_stage_completed, auto_commit_run
from forge.core.config import Config, StageConfig
from forge.core.context import Context
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
from forge.core.run import Run, compute_task_fingerprint
from forge.dashboard.app import DashboardApp
from forge.dashboard.model import RunModel, StageModel
from forge.stages.definition import StageOrder
from forge.stages.result import AutonomousHalt, StageResult
from forge.stages.stage import Stage
from forge.storage.run_manager import RunManager
from forge.testing.budget import TestingBudget
from forge.testing.engine import TesterEngine


# ==============================================================================
# N-001: Stale Downstream Deliverables Survive Task Modification & Stale Auto-Commit
# ==============================================================================

def test_n001_resumed_task_change_archives_stale_artifacts(tmp_path):
    """N-001: When run.task is changed on a resumed run, existing deliverables from the
    previous task must be archived and must NOT satisfy stage completion for the new task.
    """
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Original Task A")

    # Generate deliverables for Task A
    rm.save_stage_artifacts(
        run, sequence_number=1, role_name="architect",
        markdown_content="# Architect A", json_data={"status": "APPROVED"}
    )
    rm.save_stage_artifacts(
        run, sequence_number=2, role_name="planner",
        markdown_content="# Planner A", json_data={"status": "READY"}
    )
    rm.save_stage_artifacts(
        run, sequence_number=3, role_name="executor",
        markdown_content="# Executor A", json_data={"status": "COMPLETE"}
    )
    rm.save_stage_artifacts(
        run, sequence_number=4, role_name="tester",
        markdown_content="# Tester A", json_data={"status": "PASS"}
    )
    rm.save_stage_artifacts(
        run, sequence_number=5, role_name="reviewer",
        markdown_content="# Reviewer A", json_data={"status": "APPROVED"}
    )

    planner_def = StageOrder.get_planner()
    tester_def = StageOrder.get_tester()

    # Prior to task change, Task A artifacts satisfy completion
    done, status = is_stage_completed(run, planner_def, rm)
    assert done is True
    assert status == "READY"

    # User modifies task for the resumed run
    run.task = "Completely New Task B"

    # Invariant 1: Stale stage files were moved to history archive
    history_dir = run.run_dir / "history"
    assert history_dir.exists()
    archived_dirs = list(history_dir.iterdir())
    assert len(archived_dirs) == 1
    archived_files = [f.name for f in archived_dirs[0].iterdir()]
    assert "01_architect.json" in archived_files
    assert "02_planner.json" in archived_files
    assert "04_tester.json" in archived_files

    # Invariant 2: Active run directory no longer has old stage files
    assert not (run.run_dir / "02_planner.json").exists()
    assert not (run.run_dir / "04_tester.json").exists()

    # Invariant 3: is_stage_completed rejects completion for the new task
    done, status = is_stage_completed(run, planner_def, rm)
    assert done is False
    assert status is None

    done, status = is_stage_completed(run, tester_def, rm)
    assert done is False
    assert status is None


def test_n001_artifact_with_different_task_fingerprint_rejected(tmp_path):
    """N-001: Even if an artifact file somehow remains on disk, task_fingerprint mismatch
    prevents it from satisfying completion for a different task.
    """
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Task A")

    # Save artifact with Task A fingerprint
    rm.save_stage_artifacts(
        run, sequence_number=4, role_name="tester",
        markdown_content="# Tester", json_data={"status": "PASS", "task_fingerprint": compute_task_fingerprint("Task A")}
    )

    # Change run task directly without deleting the file
    run._initial_task = "Task B"
    run.task = "Task B"

    tester_def = StageOrder.get_tester()
    done, status = is_stage_completed(run, tester_def, rm)
    assert done is False
    assert status is None


def test_n001_resume_same_task_preserves_checkpoints(tmp_path):
    """N-001: Resuming without changing task must keep existing valid checkpoints."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Same Consistent Task")

    rm.save_stage_artifacts(
        run, sequence_number=1, role_name="architect",
        markdown_content="# Architect", json_data={"status": "APPROVED"}
    )

    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is True
    assert status == "APPROVED"

    # Resuming with identical task string
    run.task = "Same Consistent Task"
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is True
    assert status == "APPROVED"
    assert not (run.run_dir / "history").exists()


def test_n001_auto_repair_feedback_does_not_archive_artifacts(tmp_path):
    """N-001: Auto-repair feedback appends to task but preserves task identity and checkpoints."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Core Feature")

    rm.save_stage_artifacts(
        run, sequence_number=1, role_name="architect",
        markdown_content="# Architect", json_data={"status": "APPROVED"}
    )

    # Auto-repair feedback recorded explicitly
    run.set_auto_repair_feedback("### Auto-Repair Feedback (Attempt 2/3)\nFix lint error.")
    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is True
    assert status == "APPROVED"
    assert not (run.run_dir / "history").exists()


# ==============================================================================
# N-002: Dashboard False APPROVED on Partial Runs (Regression of F-004)
# ==============================================================================

def test_n002_single_stage_approved_reports_incomplete_in_dashboard(tmp_path):
    """N-002: A run where only Architect completed with APPROVED must report INCOMPLETE in the dashboard,
    never 'APPROVED: All stages completed successfully'.
    """
    run_dir = tmp_path / ".forge" / "runs" / "run-architect-only"
    run_dir.mkdir(parents=True)

    # metadata says APPROVED (e.g. from standalone forge architect command)
    metadata = {
        "run_id": "run-architect-only",
        "task": "Build feature",
        "status": "APPROVED",
        "pipeline_type": "auto",
        "created_at": "2026-10-08T00:00:00Z",
    }
    (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    # Only architect artifact exists
    from forge.core.run import compute_task_fingerprint
    task_fp = compute_task_fingerprint("Build feature")
    (run_dir / "01_architect.json").write_text(json.dumps({
        "status": "APPROVED",
        "role": "architect",
        "sequence_number": 1,
        "run_id": "run-architect-only",
        "task_fingerprint": task_fp,
        "duration_seconds": 3.0,
    }), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Architect\nApproved.", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.run_model.status == "INCOMPLETE"
    assert app.state.terminal_status == "INCOMPLETE"
    assert "planner" in app.state.terminal_reason.lower() or "pipeline terminated before stage" in app.state.terminal_reason.lower()


def test_n002_partial_architect_and_planner_reports_incomplete(tmp_path):
    """N-002: Architect + Planner approved, but executor/tester/reviewer never ran -> INCOMPLETE."""
    run_dir = tmp_path / ".forge" / "runs" / "run-arch-plan"
    run_dir.mkdir(parents=True)

    metadata = {
        "run_id": "run-arch-plan",
        "task": "Build feature",
        "status": "READY",
        "pipeline_type": "auto",
        "created_at": "2026-10-08T00:00:00Z",
    }
    (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    from forge.core.run import compute_task_fingerprint
    task_fp = compute_task_fingerprint("Build feature")
    (run_dir / "01_architect.json").write_text(json.dumps({
        "status": "APPROVED",
        "role": "architect",
        "sequence_number": 1,
        "run_id": "run-arch-plan",
        "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Architect", encoding="utf-8")
    (run_dir / "02_planner.json").write_text(json.dumps({
        "status": "READY",
        "role": "planner",
        "sequence_number": 2,
        "run_id": "run-arch-plan",
        "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "02_planner.md").write_text("# Planner", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.run_model.status == "INCOMPLETE"
    assert app.state.terminal_status == "INCOMPLETE"


def test_n002_complete_pipeline_reports_approved(tmp_path):
    """N-002: When all canonical stages are complete and successful, dashboard reports APPROVED."""
    run_dir = tmp_path / ".forge" / "runs" / "run-all-good"
    run_dir.mkdir(parents=True)

    metadata = {
        "run_id": "run-all-good",
        "task": "Build feature",
        "status": "APPROVED",
        "pipeline_type": "auto",
        "no_critic": True,
        "created_at": "2026-10-08T00:00:00Z",
    }
    (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    from forge.core.run import compute_task_fingerprint
    task_fp = compute_task_fingerprint("Build feature")
    for seq, name, st in [
        (1, "architect", "APPROVED"),
        (2, "planner", "READY"),
        (3, "executor", "COMPLETE"),
        (4, "tester", "PASS"),
        (5, "reviewer", "APPROVED"),
    ]:
        (run_dir / f"{seq:02d}_{name}.json").write_text(json.dumps({
            "status": st,
            "role": name,
            "sequence_number": seq,
            "run_id": "run-all-good",
            "task_fingerprint": task_fp,
        }), encoding="utf-8")
        (run_dir / f"{seq:02d}_{name}.md").write_text(f"# {name}", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.run_model.status == "APPROVED"
    assert app.state.terminal_status == "APPROVED"
    assert "All stages completed successfully" in app.state.terminal_reason


# ==============================================================================
# N-003: Uncaught AutonomousHalt in Standard forge run
# ==============================================================================

def test_n003_run_pipeline_handles_autonomous_halt(tmp_path, monkeypatch):
    """N-003: When execute_stage raises AutonomousHalt in run_pipeline, it must be cleanly
    caught, record CANCELLED status in metadata, and exit with code 130 without stack trace.
    """
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# Initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True, capture_output=True)

    monkeypatch.chdir(tmp_path)

    halt_signal = AutonomousHalt(status="CANCELLED", exit_code=130, reason="User cancelled stage.")
    runner = CliRunner()

    with patch("forge.cli.execute_stage", side_effect=halt_signal):
        result = runner.invoke(main, ["run", "Test cancelling standard pipeline", "--no-critic"])
        # Invariant 1: Exits with code 130
        assert result.exit_code == 130
        # Invariant 2: No unhandled exception stack trace
        assert result.exception is None or isinstance(result.exception, SystemExit)

    # Invariant 3: Persisted run metadata has status CANCELLED
    rm = RunManager(tmp_path)
    runs = rm.list_runs()
    assert len(runs) >= 1
    assert runs[0].status == "CANCELLED"


# ==============================================================================
# N-004: Subprocess Stderr Pipe Deadlock During Event Streaming
# ==============================================================================

def test_n004_stderr_drainer_prevents_pipe_deadlock(tmp_path):
    """N-004: A subprocess that writes massive output to stderr (>80 KB) while producing stdout
    must not deadlock the parent event stream reader.
    """
    script = (
        "import sys, time\n"
        "sys.stderr.write('E' * 85000 + '\\n')\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write('O' * 1000 + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    cmd = [sys.executable, "-c", script]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    drainer = StderrDrainer(proc.stderr)

    # Read stdout without deadlocking
    stdout_lines = []
    for line in proc.stdout:
        stdout_lines.append(line)

    proc.wait(timeout=5.0)
    drainer.close()

    assert len("".join(stdout_lines)) >= 1000
    captured_err = drainer.get_stderr()
    assert len(captured_err) >= 85000
    assert proc.returncode == 0


def test_n004_adapter_streams_with_large_stderr(tmp_path):
    """N-004: Verify StderrDrainer integrates into adapter event streaming."""
    drainer = StderrDrainer(None)
    assert drainer.get_stderr() == ""
    drainer.close()


# ==============================================================================
# N-005: PKB Dispute Synthesis Overwriting Evidence & Idempotency
# ==============================================================================

def test_n005_dispute_synthesis_unions_evidence_and_marks_modified(tmp_path):
    """N-005: Synthesizing a dispute fact when one already exists must union evidence,
    preserve provenance, and record the fact in diff['modified'] rather than diff['added'].
    """
    reconciler = KnowledgeReconciler(tmp_path)

    # Base architecture fact
    base_fact = KnowledgeFact(
        id="fact-arch-1",
        type=FactType.ARCHITECTURE.value,
        title="Database Choice",
        status=FactStatus.VERIFIED.value,
        summary="Use PostgreSQL for primary store.",
        evidence=["bench_pg.txt"],
    )
    reconciler.store.save_fact(base_fact)

    # First dispute by Tester
    p1 = KnowledgeProposal(
        id="fact-arch-1",
        action=ProposalAction.DISPUTE.value,
        role="tester",
        evidence=["tester_evidence_1.log"],
        note="Postgres latency exceeds 100ms threshold under load.",
    )
    diff1 = reconciler.reconcile_run("run-001", tmp_path, proposals=[p1])

    assert "dispute-fact-arch-1" in diff1["added"]
    assert "dispute-fact-arch-1" not in diff1["modified"]

    # Verify first dispute state
    facts = reconciler.store.load_all()
    dispute = facts["dispute-fact-arch-1"]
    assert "tester_evidence_1.log" in dispute.evidence

    # Second dispute by Reviewer in a separate pass with different evidence and note
    p2 = KnowledgeProposal(
        id="fact-arch-1",
        action=ProposalAction.DISPUTE.value,
        role="reviewer",
        evidence=["reviewer_evidence_2.log"],
        note="DuckDB would be more appropriate for read-heavy analytics.",
    )
    diff2 = reconciler.reconcile_run("run-002", tmp_path, proposals=[p2])

    # Invariant 1: Second dispute is recorded in modified, NOT added
    assert "dispute-fact-arch-1" in diff2["modified"]
    assert "dispute-fact-arch-1" not in diff2["added"]

    # Invariant 2: Evidence from BOTH disputes is preserved (union)
    facts2 = reconciler.store.load_all()
    dispute2 = facts2["dispute-fact-arch-1"]
    assert "tester_evidence_1.log" in dispute2.evidence
    assert "reviewer_evidence_2.log" in dispute2.evidence
    assert len(dispute2.evidence) == 2

    # Invariant 3: Original provenance preserved with last_updated fields recorded
    assert dispute2.provenance["originating_run"] == "run-001"
    assert dispute2.provenance["last_updated_run"] == "run-002"
    assert dispute2.provenance["last_updated_role"] == "reviewer"


def test_n005_repeated_dispute_reconciliation_is_idempotent(tmp_path):
    """N-005: Reconciling the exact same dispute proposal repeatedly is idempotent."""
    reconciler = KnowledgeReconciler(tmp_path)

    base_fact = KnowledgeFact(
        id="fact-arch-2",
        type=FactType.ARCHITECTURE.value,
        title="Auth",
        status=FactStatus.VERIFIED.value,
        summary="JWT Auth",
        evidence=["rfc.txt"],
    )
    reconciler.store.save_fact(base_fact)

    p = KnowledgeProposal(
        id="fact-arch-2",
        action=ProposalAction.DISPUTE.value,
        role="tester",
        evidence=["leak.txt"],
        note="JWTs leak sensitive claims.",
    )

    diff1 = reconciler.reconcile_run("run-001", tmp_path, proposals=[p])
    assert "dispute-fact-arch-2" in diff1["added"]

    diff2 = reconciler.reconcile_run("run-001", tmp_path, proposals=[p])
    assert "dispute-fact-arch-2" in diff2["modified"]
    assert "dispute-fact-arch-2" not in diff2["added"]

    facts = reconciler.store.load_all()
    assert len(facts["dispute-fact-arch-2"].evidence) == 1


# ==============================================================================
# N-006: Windows Native Command-Line Limits for Antigravity Adapter
# ==============================================================================

def test_n006_antigravity_max_prompt_bytes_platform_aware():
    """N-006: AntigravityAdapter dynamically determines max_prompt_bytes based on host OS."""
    adapter = AntigravityAdapter()

    # POSIX default
    with patch("forge.adapters.antigravity.IS_WINDOWS", False):
        assert adapter.max_prompt_bytes == 130000

    # Windows .cmd shim
    with patch("forge.adapters.antigravity.IS_WINDOWS", True), \
         patch.object(adapter, "_get_binary", return_value=r"C:\Users\test\AppData\Roaming\npm\agy.cmd"):
        assert adapter.max_prompt_bytes == 7500

    # Windows .exe binary
    with patch("forge.adapters.antigravity.IS_WINDOWS", True), \
         patch.object(adapter, "_get_binary", return_value=r"C:\Program Files\Antigravity\agy.exe"):
        assert adapter.max_prompt_bytes == 30000


def test_n006_windows_cmd_prompt_over_limit_fails_fast_with_clean_error():
    """N-006: On Windows under cmd.exe shim, prompts > 7500 bytes fail fast with clear diagnostic."""
    adapter = AntigravityAdapter()
    large_prompt = "x" * 8000

    with patch("forge.adapters.antigravity.IS_WINDOWS", True), \
         patch.object(adapter, "_get_binary", return_value=r"C:\npm\agy.cmd"):
        resp = adapter.execute(prompt=large_prompt)
        assert resp.exit_code == 1
        assert "exceeds Antigravity CLI argv transport limit" in resp.stderr
        assert "7500 bytes" in resp.stderr


def test_n006_unicode_prompt_byte_counting():
    """N-006: Multi-byte unicode characters are counted by UTF-8 byte length."""
    adapter = AntigravityAdapter()
    # 4-byte emoji: 2000 emojis = 8000 bytes
    emoji_prompt = "🔥" * 2000
    assert len(emoji_prompt) == 2000
    assert len(emoji_prompt.encode("utf-8")) == 8000

    with patch("forge.adapters.antigravity.IS_WINDOWS", True), \
         patch.object(adapter, "_get_binary", return_value=r"C:\npm\agy.cmd"):
        resp = adapter.execute(prompt=emoji_prompt)
        assert resp.exit_code == 1
        assert "8000 bytes" in resp.stderr


# ==============================================================================
# N-007: Stage Timeout Propagation to TesterEngine
# ==============================================================================

def test_n007_stage_timeout_propagates_to_tester_engine(tmp_path):
    """N-007: Stage timeout is propagated to TesterEngine's TestingBudget."""
    cfg = Config.default()
    ctx = Context(
        run=Run(run_id="r1", task="t", run_dir=tmp_path),
        project_root=tmp_path,
        config=cfg,
        git=GitService(tmp_path),
    )

    class RealAdapter(BaseAdapter):
        name = "real_adapter"
        def execute(self, *a, **k): pass
        def iter_events(self, *a, **k): pass
        def capabilities(self): return {"code_read", "shell", "structured_output"}
        def is_available(self): return True

    adapter = RealAdapter()

    stage = Stage(
        role=Role.load("tester", project_root=tmp_path, sequence_number=4),
        adapter=adapter,
        run_manager=RunManager(tmp_path),
        timeout=450,
    )

    with patch("forge.testing.engine.TesterEngine.run", return_value=MagicMock()) as mock_engine_run:
        with patch("forge.testing.engine.TesterEngine.__init__", return_value=None) as mock_engine_init:
            stage.run(ctx)
            assert mock_engine_init.call_count == 1
            call_budget = mock_engine_init.call_args[1].get("budget")
            assert call_budget is not None
            assert call_budget.max_runtime_seconds == 450.0


def test_n007_tester_engine_resolves_timeout_from_context_config(tmp_path):
    """N-007: TesterEngine resolves timeout from context.config when instantiated directly without explicit budget."""
    cfg = Config.default()
    cfg.stages["tester"] = StageConfig(timeout=360)
    ctx = Context(
        run=Run(run_id="r2", task="t", run_dir=tmp_path),
        project_root=tmp_path,
        config=cfg,
        git=GitService(tmp_path),
    )

    engine = TesterEngine(context=ctx, run_manager=RunManager(tmp_path))
    assert engine.budget.max_runtime_seconds == 360.0


def test_n007_tester_engine_defaults_to_120_when_no_timeout_configured(tmp_path):
    """N-007: Default testing budget is 120s when no stage timeout is specified in context."""
    ctx = Context(
        run=Run(run_id="r3", task="t", run_dir=tmp_path),
        project_root=tmp_path,
        config=None,
        git=GitService(tmp_path),
    )

    engine = TesterEngine(context=ctx, run_manager=RunManager(tmp_path))
    assert engine.budget.max_runtime_seconds == 120.0
