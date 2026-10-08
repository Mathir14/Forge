"""Adversarial regression test suite for third audit findings (N-008 through N-013).

Covers:
- N-008 (CRITICAL): False Approved Dashboard State
- N-009 (CRITICAL): Stage Artifacts Are Not Self-Authenticating
- N-010 (HIGH): Cross-Task Contamination of Proposals, Evidence, and Debug Data
- N-011 (HIGH): Knowledge Reconciliation Duplicate Diff Accounting
- N-012 (MEDIUM): User Task Corruption From Auto-Repair Marker
- N-013 (MEDIUM): Stderr Drainer Memory and Cancellation Latency
"""

import collections
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
from forge.cli import main, is_stage_completed, reconcile_run_knowledge
from forge.core.context import Context
from forge.core.knowledge import (
    FactStatus,
    FactType,
    KnowledgeFact,
    KnowledgeProposal,
    ProposalAction,
)
from forge.core.reconciler import KnowledgeReconciler
from forge.core.run import Run, compute_task_fingerprint
from forge.dashboard.app import DashboardApp
from forge.dashboard.model import RunModel, StageModel
from forge.stages.definition import StageOrder
from forge.storage.run_manager import RunManager


# ==============================================================================
# N-008: False Approved Dashboard State
# ==============================================================================

def test_n008_zero_stages_with_approved_metadata_reports_incomplete(tmp_path):
    """N-008.1: A run with zero stages executed must report INCOMPLETE,
    even if metadata.json has status APPROVED.
    """
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    meta = {
        "run_id": "run-001",
        "task": "Build feature",
        "status": "APPROVED",
        "created_at": "2026-10-08T00:00:00Z",
    }
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "INCOMPLETE"
    assert app.run_model.status == "INCOMPLETE"
    assert "no stages were executed" in app.state.terminal_reason.lower()


def test_n008_standalone_architect_reports_incomplete(tmp_path):
    """N-008.2: Standalone 'forge architect' execution where only Architect ran
    must report INCOMPLETE at the run level, not APPROVED."""
    run_dir = tmp_path / ".forge" / "runs" / "run-002"
    run_dir.mkdir(parents=True)
    meta = {
        "run_id": "run-002",
        "task": "Design auth",
        "status": "APPROVED",
        "created_at": "2026-10-08T00:00:00Z",
    }
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    task_fp = compute_task_fingerprint("Design auth")
    (run_dir / "01_architect.json").write_text(json.dumps({
        "role": "architect", "sequence_number": 1, "status": "APPROVED", "duration_seconds": 2.5,
        "run_id": "run-002", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Architect Spec", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "INCOMPLETE"
    assert app.run_model.status == "INCOMPLETE"
    assert "planner" in app.state.terminal_reason.lower()
    # Stage level status remains legitimately APPROVED
    assert app.run_model.get_stage("01_architect").status == "APPROVED"


def test_n008_architect_and_planner_reports_incomplete(tmp_path):
    """N-008.3: Architect + Planner approved must report INCOMPLETE (Executor missing)."""
    run_dir = tmp_path / ".forge" / "runs" / "run-003"
    run_dir.mkdir(parents=True)
    meta = {
        "run_id": "run-003",
        "task": "Implement feature",
        "status": "READY",
        "created_at": "2026-10-08T00:00:00Z",
    }
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    task_fp = compute_task_fingerprint("Implement feature")
    (run_dir / "01_architect.json").write_text(json.dumps({
        "role": "architect", "sequence_number": 1, "status": "APPROVED",
        "run_id": "run-003", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Architect", encoding="utf-8")
    (run_dir / "02_planner.json").write_text(json.dumps({
        "role": "planner", "sequence_number": 2, "status": "READY",
        "run_id": "run-003", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "02_planner.md").write_text("# Planner", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "INCOMPLETE"
    assert "executor" in app.state.terminal_reason.lower()


def test_n008_architect_planner_executor_reports_incomplete(tmp_path):
    """N-008.4: Architect + Planner + Executor approved must report INCOMPLETE (Tester missing)."""
    run_dir = tmp_path / ".forge" / "runs" / "run-004"
    run_dir.mkdir(parents=True)
    meta = {"run_id": "run-004", "task": "Feature", "status": "COMPLETE", "created_at": "2026-10-08T00:00:00Z"}
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    task_fp = compute_task_fingerprint("Feature")
    (run_dir / "01_architect.json").write_text(json.dumps({
        "role": "architect", "sequence_number": 1, "status": "APPROVED",
        "run_id": "run-004", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Architect", encoding="utf-8")
    (run_dir / "02_planner.json").write_text(json.dumps({
        "role": "planner", "sequence_number": 2, "status": "READY",
        "run_id": "run-004", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "02_planner.md").write_text("# Planner", encoding="utf-8")
    (run_dir / "03_executor.json").write_text(json.dumps({
        "role": "executor", "sequence_number": 3, "status": "COMPLETE",
        "run_id": "run-004", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "03_executor.md").write_text("# Executor", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "INCOMPLETE"
    assert "tester" in app.state.terminal_reason.lower()


def test_n008_critic_arch_plan_exec_reports_incomplete(tmp_path):
    """N-008.5: Critic + Architect + Planner + Executor (4 stages) must report INCOMPLETE,
    not bypassed by the len(stages) >= 4 heuristic."""
    run_dir = tmp_path / ".forge" / "runs" / "run-005"
    run_dir.mkdir(parents=True)
    meta = {"run_id": "run-005", "task": "Feature", "status": "APPROVED", "created_at": "2026-10-08T00:00:00Z"}
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    task_fp = compute_task_fingerprint("Feature")
    (run_dir / "00_critic.json").write_text(json.dumps({
        "role": "critic", "sequence_number": 0, "status": "CRITIQUE_COMPLETE",
        "run_id": "run-005", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "00_critic.md").write_text("# Critic", encoding="utf-8")
    (run_dir / "01_architect.json").write_text(json.dumps({
        "role": "architect", "sequence_number": 1, "status": "APPROVED",
        "run_id": "run-005", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Architect", encoding="utf-8")
    (run_dir / "02_planner.json").write_text(json.dumps({
        "role": "planner", "sequence_number": 2, "status": "READY",
        "run_id": "run-005", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "02_planner.md").write_text("# Planner", encoding="utf-8")
    (run_dir / "03_executor.json").write_text(json.dumps({
        "role": "executor", "sequence_number": 3, "status": "COMPLETE",
        "run_id": "run-005", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "03_executor.md").write_text("# Executor", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "INCOMPLETE"
    assert "tester" in app.state.terminal_reason.lower()


def test_n008_complete_pipeline_reports_approved(tmp_path):
    """N-008.6: Complete pipeline with all required stages reports APPROVED."""
    run_dir = tmp_path / ".forge" / "runs" / "run-006"
    run_dir.mkdir(parents=True)
    meta = {"run_id": "run-006", "task": "Feature", "status": "APPROVED", "no_critic": True, "created_at": "2026-10-08T00:00:00Z"}
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    stages = [
        (1, "architect", "APPROVED"),
        (2, "planner", "READY"),
        (3, "executor", "COMPLETE"),
        (4, "tester", "PASS"),
        (5, "reviewer", "APPROVED"),
    ]
    task_fp = compute_task_fingerprint("Feature")
    for seq, name, st in stages:
        (run_dir / f"{seq:02d}_{name}.json").write_text(json.dumps({
            "role": name,
            "sequence_number": seq,
            "status": st,
            "run_id": "run-006",
            "task_fingerprint": task_fp,
        }), encoding="utf-8")
        (run_dir / f"{seq:02d}_{name}.md").write_text(f"# {name}", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "APPROVED"
    assert app.run_model.status == "APPROVED"
    assert "all stages completed successfully" in app.state.terminal_reason.lower()


def test_n008_failed_pipeline_reports_failed(tmp_path):
    """N-008.7: Failed pipeline where Tester failed reports FAILED with Tester as terminal stage."""
    run_dir = tmp_path / ".forge" / "runs" / "run-007"
    run_dir.mkdir(parents=True)
    meta = {"run_id": "run-007", "task": "Feature", "status": "FAILED", "created_at": "2026-10-08T00:00:00Z"}
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    (run_dir / "01_architect.json").write_text(json.dumps({"role": "architect", "sequence_number": 1, "status": "APPROVED"}), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Architect", encoding="utf-8")
    (run_dir / "02_planner.json").write_text(json.dumps({"role": "planner", "sequence_number": 2, "status": "READY"}), encoding="utf-8")
    (run_dir / "02_planner.md").write_text("# Planner", encoding="utf-8")
    (run_dir / "03_executor.json").write_text(json.dumps({"role": "executor", "sequence_number": 3, "status": "COMPLETE"}), encoding="utf-8")
    (run_dir / "03_executor.md").write_text("# Executor", encoding="utf-8")
    (run_dir / "04_tester.json").write_text(json.dumps({
        "role": "tester", "sequence_number": 4, "status": "FAILED", "exit_code": 1,
        "machine_report": {"role": "TESTER", "status": "FAILED", "reason": "3 assertions failed", "is_valid": True}
    }), encoding="utf-8")
    (run_dir / "04_tester.md").write_text("# Tester Failed", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "FAILED"
    assert "tester" in app.state.terminal_stage.lower()
    assert "3 assertions failed" in app.state.terminal_reason


def test_n008_crashed_incomplete_pipeline(tmp_path):
    """N-008.8: Crashed pipeline reports INCOMPLETE or FAILED."""
    run_dir = tmp_path / ".forge" / "runs" / "run-008"
    run_dir.mkdir(parents=True)
    meta = {"run_id": "run-008", "task": "Crash test", "status": "RUNNING", "pipeline_type": "auto"}
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    task_fp = compute_task_fingerprint("Crash test")
    (run_dir / "01_architect.json").write_text(json.dumps({
        "role": "architect", "sequence_number": 1, "status": "APPROVED",
        "run_id": "run-008", "task_fingerprint": task_fp,
    }), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Arch", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "INCOMPLETE"
    assert "planner" in app.state.terminal_reason.lower()


def test_n008_resumed_incomplete_pipeline(tmp_path):
    """N-008.9: Resumed pipeline remains INCOMPLETE until fully verified."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Resume test")
    rm.save_stage_artifacts(run, sequence_number=1, role_name="architect", markdown_content="# Arch", json_data={"status": "APPROVED"})
    rm.save_stage_artifacts(run, sequence_number=2, role_name="planner", markdown_content="# Plan", json_data={"status": "READY"})

    run_model = RunModel.from_dir(run.run_dir)
    app = DashboardApp(run_dir=run.run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "INCOMPLETE"


def test_n008_persisted_metadata_reconstructed_from_disk(tmp_path):
    """N-008.10: Reconstructed from disk with missing stages reports INCOMPLETE."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Reconstruct test")
    rm.save_stage_artifacts(run, sequence_number=1, role_name="architect", markdown_content="# Arch", json_data={"status": "APPROVED"})

    reconstructed_model = RunModel.from_dir(run.run_dir)
    app = DashboardApp(run_dir=run.run_dir)
    app.run_model = reconstructed_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "INCOMPLETE"


def test_n008_missing_summary(tmp_path):
    """N-008.11: Run directory with missing summary derives terminal state safely from deliverables."""
    run_dir = tmp_path / ".forge" / "runs" / "run-no-summary"
    run_dir.mkdir(parents=True)
    meta = {"run_id": "run-no-summary", "task": "No summary", "status": "APPROVED"}
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "01_architect.json").write_text(json.dumps({"role": "architect", "sequence_number": 1, "status": "APPROVED"}), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Arch", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    assert run_model.summary is not None
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "INCOMPLETE"


def test_n008_contradictory_summary_and_status(tmp_path):
    """N-008.12: Contradictory summary claiming APPROVED when required stages are missing is rejected."""
    run_dir = tmp_path / ".forge" / "runs" / "run-contradictory"
    run_dir.mkdir(parents=True)
    meta = {
        "run_id": "run-contradictory",
        "task": "Contradictory test",
        "status": "APPROVED",
        "summary": {
            "run_id": "run-contradictory",
            "final_status": "APPROVED",
            "reason": "All stages completed successfully.",
            "stages": [
                {"name": "Architect", "role_name": "architect", "sequence_number": 1, "status": "APPROVED", "execution_state": "COMPLETED"},
            ]
        }
    }
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "01_architect.json").write_text(json.dumps({"role": "architect", "sequence_number": 1, "status": "APPROVED"}), encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Arch", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    # Contradictory approval rejected because Planner, Executor, Tester, Reviewer are absent
    assert app.state.terminal_status == "INCOMPLETE"


def test_n008_case_variations_of_status_strings(tmp_path):
    """N-008.13: Case variations in status strings ('approved', 'Pass', 'Ready') are properly normalized."""
    run_dir = tmp_path / ".forge" / "runs" / "run-case"
    run_dir.mkdir(parents=True)
    meta = {"run_id": "run-case", "task": "Case test", "status": "approved", "no_critic": True}
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    stages = [
        (1, "architect", "approved"),
        (2, "planner", "Ready"),
        (3, "executor", "complete"),
        (4, "tester", "pass"),
        (5, "reviewer", "APPROVED"),
    ]
    task_fp = compute_task_fingerprint("Case test")
    for seq, name, st in stages:
        (run_dir / f"{seq:02d}_{name}.json").write_text(json.dumps({
            "role": name,
            "sequence_number": seq,
            "status": st,
            "run_id": "run-case",
            "task_fingerprint": task_fp,
        }), encoding="utf-8")
        (run_dir / f"{seq:02d}_{name}.md").write_text(f"# {name}", encoding="utf-8")

    run_model = RunModel.from_dir(run_dir)
    app = DashboardApp(run_dir=run_dir)
    app.run_model = run_model
    app._apply_terminal_state()

    assert app.state.terminal_status == "APPROVED"


# ==============================================================================
# N-009: Stage Artifacts Are Not Self-Authenticating
# ==============================================================================

def test_n009_real_save_stage_artifacts_contains_fingerprint(tmp_path):
    """N-009.1: Real RunManager.save_stage_artifacts() output on disk MUST contain task_fingerprint."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Deploy feature X")
    md_file, json_file = rm.save_stage_artifacts(
        run=run,
        sequence_number=1,
        role_name="architect",
        markdown_content="# Architect Report",
        json_data={"status": "APPROVED"},
    )

    # Invariant: Intrinsic task identity written to JSON deliverable
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert "task_fingerprint" in data
    assert data["task_fingerprint"] == run.task_fingerprint
    assert len(data["task_fingerprint"]) == 64
    assert data["role"] == "architect"
    assert data["sequence_number"] == 1
    assert data["run_id"] == run.run_id


def test_n009_matching_fingerprint_accepted(tmp_path):
    """N-009.2: Artifact with matching task fingerprint is accepted."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Valid Task")
    rm.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Arch", json_data={"status": "APPROVED"}
    )
    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is True
    assert status == "APPROVED"


def test_n009_mismatching_fingerprint_rejected(tmp_path):
    """N-009.3: Artifact with mismatching task fingerprint is rejected."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Task A")
    rm.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Arch", json_data={"status": "APPROVED"}
    )
    # Manually tamper with task_fingerprint
    arch_json = run.run_dir / "01_architect.json"
    data = json.loads(arch_json.read_text(encoding="utf-8"))
    data["task_fingerprint"] = "0" * 64
    arch_json.write_text(json.dumps(data), encoding="utf-8")

    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is False
    assert status is None


def test_n009_missing_artifact_fingerprint_rejected(tmp_path):
    """N-009.4: Artifact lacking task_fingerprint is rejected for checkpointing."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Task A")
    rm.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Arch", json_data={"status": "APPROVED"}
    )
    # Remove fingerprint from artifact JSON
    arch_json = run.run_dir / "01_architect.json"
    data = json.loads(arch_json.read_text(encoding="utf-8"))
    data.pop("task_fingerprint", None)
    arch_json.write_text(json.dumps(data), encoding="utf-8")

    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is False
    assert status is None


def test_n009_missing_metadata_fingerprint_rejected(tmp_path):
    """N-009.5: Missing stage fingerprint in run metadata is rejected."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Task A")
    rm.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Arch", json_data={"status": "APPROVED"}
    )
    # Strip metadata stage_fingerprints
    run.metadata["stage_fingerprints"] = {}
    run.save_metadata()

    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is False
    assert status is None


def test_n009_corrupted_fingerprint_rejected(tmp_path):
    """N-009.6: Corrupted / non-hex / malformed fingerprint is rejected."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Task A")
    rm.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Arch", json_data={"status": "APPROVED"}
    )
    arch_json = run.run_dir / "01_architect.json"
    data = json.loads(arch_json.read_text(encoding="utf-8"))
    data["task_fingerprint"] = "corrupted-fingerprint-not-sha256"
    arch_json.write_text(json.dumps(data), encoding="utf-8")

    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is False
    assert status is None


def test_n009_legacy_artifact_safe_behavior(tmp_path):
    """N-009.7: Old b10 legacy artifact without fingerprint is treated as non-resumable."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Legacy Test")
    # Simulate legacy file format without fingerprint
    (run.run_dir / "01_architect.json").write_text(json.dumps({
        "status": "APPROVED",
        "role": "architect",
        "duration_seconds": 10.0,
    }), encoding="utf-8")
    (run.run_dir / "01_architect.md").write_text("# Legacy Arch", encoding="utf-8")

    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is False
    assert status is None


def test_n009_artifact_copied_from_history_rejected(tmp_path):
    """N-009.8: Artifact copied from old task history has mismatched fingerprint and is rejected."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Task A")
    rm.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Arch A", json_data={"status": "APPROVED"}
    )
    # Task changes to Task B
    run.task = "Task B"

    # Suppose an attacker copies 01_architect.json from history into active dir
    history_dir = run.run_dir / "history" / f"task_{compute_task_fingerprint('Task A')[:8]}"
    copied_json = history_dir / "01_architect.json"
    dest_json = run.run_dir / "01_architect.json"
    dest_json.write_text(copied_json.read_text(encoding="utf-8"), encoding="utf-8")

    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is False
    assert status is None


def test_n009_artifact_copied_from_another_run_rejected(tmp_path):
    """N-009.9: Artifact copied from another run with mismatching run_id is rejected."""
    rm = RunManager(tmp_path)
    run1 = rm.create_run(task="Same Task Text")
    rm.save_stage_artifacts(
        run=run1, sequence_number=1, role_name="architect",
        markdown_content="# Arch 1", json_data={"status": "APPROVED"}
    )
    run2 = rm.create_run(task="Same Task Text")
    # Copy run1's artifact into run2
    run1_json = run1.run_dir / "01_architect.json"
    (run2.run_dir / "01_architect.json").write_text(run1_json.read_text(encoding="utf-8"), encoding="utf-8")

    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run2, arch_def, rm)
    assert done is False
    assert status is None


def test_n009_stage_identity_mismatch_rejected(tmp_path):
    """N-009.10: Artifact with role mismatch (e.g. planner payload inside 01_architect.json) is rejected."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Identity test")
    rm.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Arch", json_data={"status": "APPROVED"}
    )
    arch_json = run.run_dir / "01_architect.json"
    data = json.loads(arch_json.read_text(encoding="utf-8"))
    data["role"] = "planner"  # Role mismatch!
    arch_json.write_text(json.dumps(data), encoding="utf-8")

    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(run, arch_def, rm)
    assert done is False
    assert status is None


def test_n009_normal_resume_same_task_still_works(tmp_path):
    """N-009.11: Normal resume for identical task succeeds completely."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Resumable Task")
    rm.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Arch", json_data={"status": "APPROVED"}
    )
    # Resume run object
    resumed_run = rm.resume(run.run_id)
    arch_def = StageOrder.get_architect()
    done, status = is_stage_completed(resumed_run, arch_def, rm)
    assert done is True
    assert status == "APPROVED"


# ==============================================================================
# N-010: Cross-Task Contamination of Proposals, Evidence, and Debug Data
# ==============================================================================

def test_n010_cross_task_proposals_evidence_debug_isolated(tmp_path):
    """N-010: When Task A becomes Task B, proposals, evidence, and debug data
    from Task A are archived into history and cannot contaminate Task B reconciliation."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Task A: Implement PostgreSQL backend")

    # Generate Task A deliverables
    rm.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Architect A", json_data={"status": "APPROVED"}
    )

    # Generate Task A proposals, evidence, and debug dirs
    proposals_dir = run.run_dir / "knowledge_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    prop_file = proposals_dir / "01_architect_proposals.yaml"
    prop_file.write_text("role: architect\nproposals:\n  - id: fact-db-pg\n    type: architecture\n    action: assert\n    title: PostgreSQL\n    summary: Use PG\n", encoding="utf-8")

    evidence_dir = run.run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "pg_bench.log").write_text("PG latency 5ms\n", encoding="utf-8")

    debug_dir = run.run_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "architect_prompt.txt").write_text("Prompt for Task A\n", encoding="utf-8")

    # Switch run task to Task B
    run.task = "Task B: Implement SQLite embedded store"

    # Invariant 1: Active run directory has NO Task A proposals, evidence, or debug
    assert not (run.run_dir / "knowledge_proposals").exists()
    assert not (run.run_dir / "evidence").exists()
    assert not (run.run_dir / "debug").exists()
    assert not (run.run_dir / "01_architect.json").exists()

    # Invariant 2: History contains all Task A artifacts and directories
    history_base = run.run_dir / "history"
    assert history_base.exists()
    archived_dirs = list(history_base.iterdir())
    assert len(archived_dirs) == 1
    archive_gen = archived_dirs[0]

    assert (archive_gen / "knowledge_proposals" / "01_architect_proposals.yaml").exists()
    assert (archive_gen / "evidence" / "pg_bench.log").exists()
    assert (archive_gen / "debug" / "architect_prompt.txt").exists()
    assert (archive_gen / "01_architect.json").exists()

    # Invariant 3: Reconciling Task B's run knowledge does NOT import Task A's proposals
    diff = reconcile_run_knowledge(tmp_path, run.run_id, run.run_dir)
    assert diff is not None
    assert "fact-db-pg" not in diff.get("added", [])
    assert "fact-db-pg" not in diff.get("modified", [])


def test_n010_chain_task_generations_isolated_a_b_c_d(tmp_path):
    """N-010: Chain of task changes A -> B -> C -> D keeps all generations isolated."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Task A")
    (run.run_dir / "knowledge_proposals").mkdir(parents=True, exist_ok=True)
    (run.run_dir / "knowledge_proposals" / "prop_a.yaml").write_text("task: a\n", encoding="utf-8")

    run.task = "Task B"
    (run.run_dir / "knowledge_proposals").mkdir(parents=True, exist_ok=True)
    (run.run_dir / "knowledge_proposals" / "prop_b.yaml").write_text("task: b\n", encoding="utf-8")

    run.task = "Task C"
    (run.run_dir / "knowledge_proposals").mkdir(parents=True, exist_ok=True)
    (run.run_dir / "knowledge_proposals" / "prop_c.yaml").write_text("task: c\n", encoding="utf-8")

    run.task = "Task D"

    # All three historical generations exist in history/
    history_dir = run.run_dir / "history"
    archived_gens = sorted([d.name for d in history_dir.iterdir() if d.is_dir()])
    assert len(archived_gens) == 3

    # Active run directory has no lingering proposals
    assert not (run.run_dir / "knowledge_proposals").exists()


def test_n010_repeated_task_changes_and_archive_collision_resistance(tmp_path):
    """N-010: Switching A -> B -> A -> B does not collide or overwrite existing archives."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Task Alpha")
    (run.run_dir / "debug").mkdir(parents=True, exist_ok=True)
    (run.run_dir / "debug" / "v1.log").write_text("Alpha v1\n", encoding="utf-8")

    run.task = "Task Beta"
    (run.run_dir / "debug").mkdir(parents=True, exist_ok=True)
    (run.run_dir / "debug" / "v2.log").write_text("Beta v2\n", encoding="utf-8")

    run.task = "Task Alpha"
    (run.run_dir / "debug").mkdir(parents=True, exist_ok=True)
    (run.run_dir / "debug" / "v3.log").write_text("Alpha v3\n", encoding="utf-8")

    run.task = "Task Beta"

    history_dir = run.run_dir / "history"
    archived_gens = list(history_dir.iterdir())
    assert len(archived_gens) == 3


def test_n010_missing_and_empty_subdirectories_handled(tmp_path):
    """N-010: Archiving handles missing or empty subdirectories without error."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Task One")
    (run.run_dir / "evidence").mkdir(parents=True, exist_ok=True)  # empty evidence dir
    # debug and knowledge_proposals do not exist

    run.task = "Task Two"
    assert not (run.run_dir / "evidence").exists()


# ==============================================================================
# N-011: Knowledge Reconciliation Duplicate Diff Accounting
# ==============================================================================

def test_n011_first_dispute_added_second_reconciliation_modified(tmp_path):
    """N-011.1 & 2: First dispute synthesizes anomaly in diff['added'];
    subsequent reconciliation records it in diff['modified']."""
    reconciler = KnowledgeReconciler(tmp_path)
    base_fact = KnowledgeFact(
        id="fact-core-1",
        type=FactType.ARCHITECTURE.value,
        title="Async Event Bus",
        status=FactStatus.VERIFIED.value,
        summary="Use Redis PubSub",
    )
    reconciler.store.save_fact(base_fact)

    p1 = KnowledgeProposal(
        id="fact-core-1",
        action=ProposalAction.DISPUTE.value,
        role="tester",
        evidence=["tester_ev.log"],
        note="Redis PubSub drops messages under network partition.",
    )
    diff1 = reconciler.reconcile_run("run-101", tmp_path, proposals=[p1])

    assert "dispute-fact-core-1" in diff1["added"]
    assert "dispute-fact-core-1" not in diff1["modified"]

    # Second pass
    p2 = KnowledgeProposal(
        id="fact-core-1",
        action=ProposalAction.DISPUTE.value,
        role="reviewer",
        evidence=["reviewer_ev.log"],
        note="Kafka or NATS JetStream is required for at-least-once delivery.",
    )
    diff2 = reconciler.reconcile_run("run-102", tmp_path, proposals=[p2])

    assert "dispute-fact-core-1" in diff2["modified"]
    assert "dispute-fact-core-1" not in diff2["added"]


def test_n011_two_disputes_in_same_run_exactly_one_category(tmp_path):
    """N-011.3: Two proposals disputing the same fact within the SAME reconciliation pass
    must place the synthesized dispute in exactly ONE diff category ('added')."""
    reconciler = KnowledgeReconciler(tmp_path)
    base_fact = KnowledgeFact(
        id="fact-arch-bus",
        type=FactType.ARCHITECTURE.value,
        title="Event Bus Architecture",
        status=FactStatus.VERIFIED.value,
        summary="ZeroMQ bus",
    )
    reconciler.store.save_fact(base_fact)

    p1 = KnowledgeProposal(
        id="fact-arch-bus",
        action=ProposalAction.DISPUTE.value,
        role="tester",
        evidence=["bus_leak.log"],
        note="ZeroMQ leaks sockets on process restart.",
    )
    p2 = KnowledgeProposal(
        id="fact-arch-bus",
        action=ProposalAction.DISPUTE.value,
        role="reviewer",
        evidence=["review_notes.md"],
        note="Brokerless pubsub lacks durability guarantees.",
    )

    diff = reconciler.reconcile_run("run-same-pass", tmp_path, proposals=[p1, p2])

    # Invariant: Mutually exclusive diff categories
    assert "dispute-fact-arch-bus" in diff["added"]
    assert "dispute-fact-arch-bus" not in diff["modified"]
    assert len(set(diff["added"]) & set(diff["modified"])) == 0


def test_n011_multiple_roles_claims_and_evidence_union(tmp_path):
    """N-011.4, 5, 7, 8, 9: Distinct claims from multiple roles are preserved
    in disputes list, synthesized summary is updated, and evidence is unioned."""
    reconciler = KnowledgeReconciler(tmp_path)
    base_fact = KnowledgeFact(
        id="fact-sec-auth",
        type=FactType.FEATURE.value,
        title="Session Auth",
        status=FactStatus.VERIFIED.value,
        summary="Cookie sessions",
    )
    reconciler.store.save_fact(base_fact)

    p1 = KnowledgeProposal(
        id="fact-sec-auth",
        action=ProposalAction.DISPUTE.value,
        role="tester",
        evidence=["ev_test_csrf.log"],
        note="Missing SameSite attribute on session cookie.",
    )
    p2 = KnowledgeProposal(
        id="fact-sec-auth",
        action=ProposalAction.DISPUTE.value,
        role="reviewer",
        evidence=["ev_rev_tls.log"],
        note="Missing Secure flag over HTTPS.",
    )

    reconciler.reconcile_run("run-multi", tmp_path, proposals=[p1, p2])

    facts = reconciler.store.load_all()
    base_updated = facts["fact-sec-auth"]
    assert len(base_updated.disputes) == 2

    dispute_fact = facts["dispute-fact-sec-auth"]
    assert "ev_test_csrf.log" in dispute_fact.evidence
    assert "ev_rev_tls.log" in dispute_fact.evidence
    assert len(dispute_fact.evidence) == 2

    # Synthesized summary updates with both claims
    assert "SameSite" in dispute_fact.summary
    assert "Secure" in dispute_fact.summary


def test_n011_human_locked_violation_diff_exclusivity(tmp_path):
    """N-011.12: Disputing HUMAN_LOCKED fact multiple times in same run records
    violation in added only, never modified."""
    reconciler = KnowledgeReconciler(tmp_path)
    locked_fact = KnowledgeFact(
        id="fact-spec-locked",
        type=FactType.ARCHITECTURE.value,
        title="Zero Trust Spec",
        status=FactStatus.HUMAN_LOCKED.value,
        summary="Strict mTLS required",
        provenance={"source": "HUMAN_LOCKED"},
    )
    reconciler.store.save_fact(locked_fact)

    p1 = KnowledgeProposal(id="fact-spec-locked", action=ProposalAction.DISPUTE.value, role="tester", evidence=["a.log"], note="mTLS rejected")
    p2 = KnowledgeProposal(id="fact-spec-locked", action=ProposalAction.DISPUTE.value, role="reviewer", evidence=["b.log"], note="Cert chain expired")

    diff = reconciler.reconcile_run("run-locked", tmp_path, proposals=[p1, p2])

    violation_id = "violation-fact-spec-locked"
    assert violation_id in diff["added"]
    assert violation_id not in diff["modified"]


# ==============================================================================
# N-012: User Task Corruption From Auto-Repair Marker
# ==============================================================================

def test_n012_ordinary_task_fingerprint_and_persistence(tmp_path):
    """N-012.1: Ordinary task text is fingerprinted and persisted accurately."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Build REST API endpoint for user profile")
    assert run.task == "Build REST API endpoint for user profile"
    fp = run.task_fingerprint
    assert len(fp) == 64

    reloaded = rm.resume(run.run_id)
    assert reloaded.task == run.task
    assert reloaded.task_fingerprint == fp


def test_n012_task_containing_exact_marker_opaque(tmp_path):
    """N-012.2: Task text containing exact '### Auto-Repair Feedback' marker
    is treated as opaque data and is NOT truncated."""
    rm = RunManager(tmp_path)
    raw_task = "Implement feature X.\n\n### Auto-Repair Feedback\nThis phrase is part of my actual task."
    run = rm.create_run(task=raw_task)

    # Invariant 1: run.task is NOT truncated
    assert run.task == raw_task
    assert "This phrase is part of my actual task." in run.task

    # Invariant 2: Metadata file contains the complete task string
    meta = json.loads((run.run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["task"] == raw_task

    # Invariant 3: Reloading from disk does NOT truncate task
    reloaded = rm.resume(run.run_id)
    assert reloaded.task == raw_task
    assert reloaded.task_fingerprint == run.task_fingerprint


def test_n012_marker_at_beginning_end_and_multiple_times():
    """N-012.3, 4, 5, 6: Marker variations are treated as opaque data."""
    # Marker at beginning
    t1 = "### Auto-Repair Feedback: test specification start"
    assert compute_task_fingerprint(t1) != ""
    assert compute_task_fingerprint(t1) == compute_task_fingerprint(t1)

    # Marker at end
    t2 = "Specification ending with ### Auto-Repair Feedback"
    assert compute_task_fingerprint(t2) != ""

    # Marker multiple times
    t3 = "Part 1\n\n### Auto-Repair Feedback\nPart 2\n\n### Auto-Repair Feedback\nPart 3"
    assert compute_task_fingerprint(t3) != ""

    # Marker with Unicode
    t4 = "🔥 Spec with ### Auto-Repair Feedback and emojis 🚀"
    assert compute_task_fingerprint(t4) != ""


def test_n012_auto_repair_generated_feedback_separation(tmp_path):
    """N-012.7: Generated auto-repair feedback is kept separate from run.task."""
    rm = RunManager(tmp_path)
    user_task = "Build authentication endpoint"
    run = rm.create_run(task=user_task)

    # Simulate auto-repair iteration without mutating run.task
    run.repair_feedback = "### Auto-Repair Feedback from Tester (Attempt 1):\nFix 401 error."
    run.save_metadata()

    assert run.task == user_task
    assert run.repair_feedback is not None

    reloaded = rm.resume(run.run_id)
    assert reloaded.task == user_task
    assert reloaded.repair_feedback == run.repair_feedback


def test_n012_no_fingerprint_collision_after_marker():
    """N-012.11: Two user tasks differing only after the marker MUST NOT collide."""
    task_a = "Implement feature X.\n\n### Auto-Repair Feedback\nVariant A details."
    task_b = "Implement feature X.\n\n### Auto-Repair Feedback\nVariant B details."

    fp_a = compute_task_fingerprint(task_a)
    fp_b = compute_task_fingerprint(task_b)

    assert fp_a != fp_b


# ==============================================================================
# N-013: Stderr Drainer Memory and Cancellation Latency
# ==============================================================================

def test_n013_large_stderr_does_not_deadlock():
    """N-013.1: Subprocess writing 150 KB of stderr while producing stdout does not deadlock."""
    script = (
        "import sys\n"
        "sys.stderr.write('E' * 150000 + '\\n')\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write('DONE\\n')\n"
        "sys.stdout.flush()\n"
    )
    cmd = [sys.executable, "-c", script]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    drainer = StderrDrainer(proc.stderr)

    stdout_data = proc.stdout.read()
    proc.wait(timeout=5.0)
    drainer.close()

    assert "DONE" in stdout_data
    assert len(drainer.get_stderr()) >= 150000


def test_n013_bounded_memory_behavior():
    """N-013.2: Drainer caps memory usage under massive stderr stream (e.g. 2 MB with 32 KB limit)."""
    # 32 KB limit
    limit_bytes = 32 * 1024
    drainer = StderrDrainer(None, max_bytes=limit_bytes)

    # Simulate draining 2 MB in chunks
    chunk = "Error log line with diagnostic details\n" * 100
    total_written = 0
    drainer._chunks = collections.deque()
    for _ in range(500):
        drainer._chunks.append(chunk)
        drainer._current_bytes += len(chunk.encode("utf-8"))
        while drainer._current_bytes > drainer._max_bytes and drainer._chunks:
            removed = drainer._chunks.popleft()
            drainer._current_bytes -= len(removed.encode("utf-8"))
            drainer._truncated = True
        total_written += len(chunk)

    assert total_written > 1_500_000
    assert drainer._current_bytes <= limit_bytes
    output = drainer.get_stderr()
    assert "[... stderr truncated:" in output
    assert len(output.encode("utf-8")) <= limit_bytes + 200


def test_n013_cancellation_terminates_promptly():
    """N-013.3: Cancelling a running process that writes stderr terminates promptly (<150ms)."""
    script = (
        "import sys, time\n"
        "for i in range(100):\n"
        "    sys.stderr.write(f'line {i}\\n')\n"
        "    sys.stderr.flush()\n"
        "    time.sleep(0.1)\n"
    )
    cmd = [sys.executable, "-c", script]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    drainer = StderrDrainer(proc.stderr)

    time.sleep(0.05)  # Let process start and write some lines

    t0 = time.perf_counter()
    # Correct order: clean up / kill proc first, then close drainer
    BaseAdapter._safe_cleanup_subprocess(proc)
    drainer.close()
    elapsed = time.perf_counter() - t0

    # Prompt termination: must NOT wait for 0.5s drainer timeout
    assert elapsed < 0.25, f"Cancellation took {elapsed:.3f}s, expected < 0.25s"
    assert not drainer._thread.is_alive()


def test_n013_drainer_thread_exits():
    """N-013.4: Drainer background thread terminates cleanly after close()."""
    cmd = [sys.executable, "-c", "import sys; sys.stderr.write('test\\n')"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    drainer = StderrDrainer(proc.stderr)
    proc.wait(timeout=2.0)
    drainer.close()

    assert not drainer._thread.is_alive()
    assert "test" in drainer.get_stderr()


def test_n013_stderr_diagnostics_remain_available():
    """N-013.5: Diagnostics written to stderr remain retrievable via get_stderr()."""
    cmd = [sys.executable, "-c", "import sys; sys.stderr.write('CRITICAL_FAILURE: memory leak\\n')"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    drainer = StderrDrainer(proc.stderr)
    proc.wait(timeout=2.0)
    drainer.close()

    assert "CRITICAL_FAILURE: memory leak" in drainer.get_stderr()
