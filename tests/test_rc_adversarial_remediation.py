"""Adversarial regression test suite for release-blocking audit findings (RC-01 through RC-04).

Covers:
- RC-01 (HIGH): User task modification misclassified as auto-repair feedback
- RC-03 (HIGH): Completion verification trusts metadata summary when physical artifacts are absent
- RC-02 (MEDIUM): Contradictory dashboard terminal banner after interrupted run
- RC-04 (MEDIUM): Stage provenance fields are optional in is_stage_completed
"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from rich.console import Console

from forge.cli import is_stage_completed
from forge.core.role import Role
from forge.core.run import Run, compute_task_fingerprint
from forge.dashboard.app import DashboardApp
from forge.dashboard.components.terminal_banner import render_terminal_banner
from forge.dashboard.model import RunModel, StageModel
from forge.dashboard.state import DashboardState
from forge.prompts.builder import InstructionBuilder
from forge.stages.definition import StageOrder
from forge.storage.run_manager import RunManager


# ==============================================================================
# RC-01: User Task Modification Misclassified as Auto-Repair Feedback
# ==============================================================================


def test_rc01_task_contains_exact_delimiter_as_ordinary_text(tmp_path: Path):
    """RC-01.1: A task containing the exact auto-repair delimiter as user text
    must NOT be misclassified as internal auto-repair feedback.
    """
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_text = "Implement feature X\n\n### Auto-Repair Feedback\nMust handle edge case Y properly."
    run = Run(run_id="run-1", task=task_text, run_dir=run_dir)

    assert run.task == task_text
    assert run.auto_repair_feedback is None
    assert run.task_fingerprint == compute_task_fingerprint(task_text)


def test_rc01_task_starts_with_previous_task_then_delimiter(tmp_path: Path):
    """RC-01.2: Updating a task to one that starts with the previous task and then contains
    the delimiter must be treated as a genuine task mutation, not auto-repair feedback.
    """
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    initial_task = "Build authentication system"
    run = Run(run_id="run-1", task=initial_task, run_dir=run_dir)
    initial_fp = run.task_fingerprint

    new_task = f"{initial_task}\n\n### Auto-Repair Feedback\nInclude PKCE flow support."
    run.task = new_task

    assert run.task == new_task
    assert run.auto_repair_feedback is None
    assert run.task_fingerprint != initial_fp
    assert run.task_fingerprint == compute_task_fingerprint(new_task)


def test_rc01_task_contains_multiple_delimiters(tmp_path: Path):
    """RC-01.3: User prompt containing multiple delimiter occurrences is preserved verbatim."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_text = (
        "Explain the following text:\n"
        "\n\n### Auto-Repair Feedback\nSection 1\n"
        "\n\n### Auto-Repair Feedback\nSection 2"
    )
    run = Run(run_id="run-1", task=task_text, run_dir=run_dir)

    assert run.task == task_text
    assert run.auto_repair_feedback is None
    assert run.task_fingerprint == compute_task_fingerprint(task_text)


def test_rc01_task_contains_delimiter_like_text_unrelated(tmp_path: Path):
    """RC-01.4: Prompt discussing Forge auto-repair mechanisms is treated as ordinary user text."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_text = "Write a technical document about ### Auto-Repair Feedback in autonomous agents."
    run = Run(run_id="run-1", task=task_text, run_dir=run_dir)

    assert run.task == task_text
    assert run.auto_repair_feedback is None
    assert run.task_fingerprint == compute_task_fingerprint(task_text)


def test_rc01_explicit_auto_repair_feedback_path(tmp_path: Path):
    """RC-01.5: Legitimate internal auto-repair feedback is set via explicit API
    and does NOT mutate the canonical task or fingerprint.
    """
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_text = "Fix issue #42"
    run = Run(run_id="run-1", task=task_text, run_dir=run_dir)
    orig_fp = run.task_fingerprint

    feedback_text = "Reviewer found missing test coverage in auth.py:45."
    run.set_auto_repair_feedback(feedback_text)

    assert run.task == task_text
    assert run.auto_repair_feedback == feedback_text
    assert run.task_fingerprint == orig_fp


def test_rc01_task_fingerprint_changes_on_genuine_task_mutation(tmp_path: Path):
    """RC-01.6: Fingerprint must update on every genuine task mutation."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-1", task="Task version 1", run_dir=run_dir)
    fp1 = run.task_fingerprint

    run.task = "Task version 2"
    fp2 = run.task_fingerprint

    assert fp1 != fp2
    assert fp2 == compute_task_fingerprint("Task version 2")


def test_rc01_stale_artifacts_archived_on_genuine_task_mutation(tmp_path: Path):
    """RC-01.7: Stale stage deliverables are archived when run.task is mutated."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-1", task="Original task", run_dir=run_dir)
    old_fp = run.task_fingerprint

    # Create a stage deliverable
    stage_file = run_dir / "01_architect.json"
    stage_file.write_text(json.dumps({"status": "APPROVED", "task_fingerprint": old_fp}), encoding="utf-8")

    # Mutate task
    run.task = "Original task\n\n### Auto-Repair Feedback\nNew prompt requirement"

    # Stage file from old generation should be archived out of active stage files
    assert not stage_file.exists()
    archive_dir = run_dir / "history" / f"task_{old_fp[:8]}"
    assert (archive_dir / "01_architect.json").exists()


def test_rc01_metadata_roundtrip_preserves_exact_user_task(tmp_path: Path):
    """RC-01.8: RunManager metadata save and reload preserves exact user task with delimiter."""
    rm = RunManager(project_root=tmp_path)
    task_text = "User prompt with\n\n### Auto-Repair Feedback\nPreserve this exactly."
    run = rm.create_run(task=task_text)

    meta_file = run.run_dir / "metadata.json"
    meta_dict = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta_dict["task"] == task_text

    loaded_run = rm.resume(run_id=run.run_id)
    assert loaded_run is not None
    assert loaded_run.task == task_text
    assert loaded_run.task_fingerprint == compute_task_fingerprint(task_text)


def test_rc01_prompt_builder_does_not_strip_or_mutate_task(tmp_path: Path):
    """RC-01.9: InstructionBuilder treats user task as completely opaque and preserves it."""
    task_text = "Analyze why\n\n### Auto-Repair Feedback\nis an important feature."
    run = Run(run_id="run-1", task=task_text, run_dir=tmp_path / "run-1")
    run.run_dir.mkdir(parents=True)
    run.set_auto_repair_feedback("Test failure in auth module.")

    role = Role(name="executor", sequence_number=3, template_content="Task: {{ task }}")
    context = Mock()
    context.run = run
    context.project_root = tmp_path
    context.git = None
    context.repair_feedback = run.auto_repair_feedback

    instruction = InstructionBuilder.build(context=context, role=role)

    # Instruction contains the user task verbatim
    assert instruction.task == task_text
    # Instruction also contains the auto-repair feedback independently
    assert instruction.repair_feedback == "Test failure in auth module."
    # Canonical task remains unchanged
    assert run.task == task_text


# ==============================================================================
# RC-03: Completion Verification Physical Evidence Authentication
# ==============================================================================


def _setup_stage_artifacts(
    run_dir: Path,
    run_id: str,
    task_fp: str,
    stages=None,
    no_critic: bool = True,
):
    if stages is None:
        stages = [
            (1, "architect", "APPROVED"),
            (2, "planner", "READY"),
            (3, "executor", "COMPLETE"),
            (4, "tester", "PASS"),
            (5, "reviewer", "APPROVED"),
        ]
        if not no_critic:
            stages.append((6, "critic", "APPROVED"))

    for seq, role, status in stages:
        stage_file = run_dir / f"{seq:02d}_{role}.json"
        stage_file.write_text(json.dumps({
            "run_id": run_id,
            "task_fingerprint": task_fp,
            "role": role,
            "sequence_number": seq,
            "status": status,
        }), encoding="utf-8")


def test_rc03_metadata_all_completed_physical_artifacts_deleted(tmp_path: Path):
    """RC-03.1: Metadata summary claims all stages COMPLETED, but all physical artifacts deleted."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_fp = compute_task_fingerprint("Task 1")

    summary = Mock()
    summary.stages = [
        Mock(name="Architect", status="APPROVED"),
        Mock(name="Planner", status="READY"),
        Mock(name="Executor", status="COMPLETE"),
        Mock(name="Tester", status="PASS"),
        Mock(name="Reviewer", status="APPROVED"),
    ]

    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        summary=summary,
        no_critic=True,
        run_dir=run_dir,
        task_fingerprint=task_fp,
        run_id="run-1",
    )

    assert is_complete is False
    assert inc_stage == "Architect"
    assert "missing on disk" in reason or "No stages were executed" in reason


def test_rc03_metadata_all_completed_one_artifact_missing(tmp_path: Path):
    """RC-03.2: Metadata claims all stages COMPLETED, but one physical artifact is missing."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_fp = compute_task_fingerprint("Task 1")
    run_id = "run-1"

    # Create 01, 02, 03, 05 (04_tester.json missing)
    stages = [
        (1, "architect", "APPROVED"),
        (2, "planner", "READY"),
        (3, "executor", "COMPLETE"),
        (5, "reviewer", "APPROVED"),
    ]
    _setup_stage_artifacts(run_dir, run_id, task_fp, stages=stages, no_critic=True)

    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        no_critic=True,
        run_dir=run_dir,
        task_fingerprint=task_fp,
        run_id=run_id,
    )

    assert is_complete is False
    assert inc_stage == "Tester"
    assert "04_tester.json" in reason
    assert "missing on disk" in reason


def test_rc03_metadata_all_completed_one_artifact_malformed(tmp_path: Path):
    """RC-03.3: Metadata claims all stages COMPLETED, but an artifact contains malformed JSON."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_fp = compute_task_fingerprint("Task 1")
    run_id = "run-1"

    _setup_stage_artifacts(run_dir, run_id, task_fp, no_critic=True)
    # Corrupt 03_executor.json
    (run_dir / "03_executor.json").write_text("{invalid json", encoding="utf-8")

    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        no_critic=True,
        run_dir=run_dir,
        task_fingerprint=task_fp,
        run_id=run_id,
    )

    assert is_complete is False
    assert inc_stage == "Executor"
    assert "malformed" in reason.lower()


def test_rc03_metadata_all_completed_artifact_wrong_fingerprint(tmp_path: Path):
    """RC-03.4: Metadata claims all stages COMPLETED, but an artifact has mismatched task fingerprint."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_fp = compute_task_fingerprint("Task 1")
    wrong_fp = "0" * 64 if task_fp != "0" * 64 else "1" * 64
    run_id = "run-1"

    _setup_stage_artifacts(run_dir, run_id, task_fp, no_critic=True)
    # Corrupt fingerprint in 02_planner.json
    (run_dir / "02_planner.json").write_text(json.dumps({
        "run_id": run_id,
        "task_fingerprint": wrong_fp,
        "role": "planner",
        "sequence_number": 2,
        "status": "READY",
    }), encoding="utf-8")

    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        no_critic=True,
        run_dir=run_dir,
        task_fingerprint=task_fp,
        run_id=run_id,
    )

    assert is_complete is False
    assert inc_stage == "Planner"
    assert "mismatched task fingerprint" in reason


def test_rc03_metadata_all_completed_artifact_wrong_run_id(tmp_path: Path):
    """RC-03.5: Metadata claims all stages COMPLETED, but an artifact has mismatched run identity."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_fp = compute_task_fingerprint("Task 1")
    run_id = "run-1"

    _setup_stage_artifacts(run_dir, run_id, task_fp, no_critic=True)
    # Corrupt run_id in 03_executor.json
    (run_dir / "03_executor.json").write_text(json.dumps({
        "run_id": "different-run-999",
        "task_fingerprint": task_fp,
        "role": "executor",
        "sequence_number": 3,
        "status": "COMPLETE",
    }), encoding="utf-8")

    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        no_critic=True,
        run_dir=run_dir,
        task_fingerprint=task_fp,
        run_id=run_id,
    )

    assert is_complete is False
    assert inc_stage == "Executor"
    assert "mismatched run identity" in reason


def test_rc03_metadata_all_completed_artifact_wrong_role(tmp_path: Path):
    """RC-03.6: Metadata claims all stages COMPLETED, but an artifact has mismatched role."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_fp = compute_task_fingerprint("Task 1")
    run_id = "run-1"

    _setup_stage_artifacts(run_dir, run_id, task_fp, no_critic=True)
    # Corrupt role in 04_tester.json
    (run_dir / "04_tester.json").write_text(json.dumps({
        "run_id": run_id,
        "task_fingerprint": task_fp,
        "role": "reviewer",
        "sequence_number": 4,
        "status": "PASS",
    }), encoding="utf-8")

    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        no_critic=True,
        run_dir=run_dir,
        task_fingerprint=task_fp,
        run_id=run_id,
    )

    assert is_complete is False
    assert inc_stage == "Tester"
    assert "mismatched role" in reason


def test_rc03_metadata_all_completed_artifact_wrong_sequence(tmp_path: Path):
    """RC-03.7: Metadata claims all stages COMPLETED, but an artifact has mismatched sequence number."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_fp = compute_task_fingerprint("Task 1")
    run_id = "run-1"

    _setup_stage_artifacts(run_dir, run_id, task_fp, no_critic=True)
    # Corrupt sequence_number in 05_reviewer.json
    (run_dir / "05_reviewer.json").write_text(json.dumps({
        "run_id": run_id,
        "task_fingerprint": task_fp,
        "role": "reviewer",
        "sequence_number": 99,
        "status": "APPROVED",
    }), encoding="utf-8")

    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        no_critic=True,
        run_dir=run_dir,
        task_fingerprint=task_fp,
        run_id=run_id,
    )

    assert is_complete is False
    assert inc_stage == "Reviewer"
    assert "mismatched sequence number" in reason


def test_rc03_metadata_summary_success_while_artifact_says_failure(tmp_path: Path):
    """RC-03.8: Metadata summary claims success, but artifact on disk has failure status."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_fp = compute_task_fingerprint("Task 1")
    run_id = "run-1"

    _setup_stage_artifacts(run_dir, run_id, task_fp, no_critic=True)
    # Corrupt status in 03_executor.json
    (run_dir / "03_executor.json").write_text(json.dumps({
        "run_id": run_id,
        "task_fingerprint": task_fp,
        "role": "executor",
        "sequence_number": 3,
        "status": "FAILED",
    }), encoding="utf-8")

    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        no_critic=True,
        run_dir=run_dir,
        task_fingerprint=task_fp,
        run_id=run_id,
    )

    assert is_complete is False
    assert inc_stage == "Executor"
    assert "non-success status 'FAILED'" in reason


def test_rc03_metadata_summary_stale_from_earlier_task_generation(tmp_path: Path):
    """RC-03.9: Metadata summary claims COMPLETED, but artifacts on disk are from an older generation."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    old_fp = compute_task_fingerprint("Old task")
    new_fp = compute_task_fingerprint("New mutated task")
    run_id = "run-1"

    # Artifacts belong to old generation
    _setup_stage_artifacts(run_dir, run_id, old_fp, no_critic=True)

    # Verification must check against current task fingerprint
    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        no_critic=True,
        run_dir=run_dir,
        task_fingerprint=new_fp,
        run_id=run_id,
    )

    assert is_complete is False
    assert "mismatched task fingerprint" in reason


def test_rc03_successful_physical_artifacts_evaluated_from_physical_evidence(tmp_path: Path):
    """RC-03.10: Valid physical artifacts certify completion regardless of missing or stale metadata summary."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task_fp = compute_task_fingerprint("Valid Task")
    run_id = "run-1"

    _setup_stage_artifacts(run_dir, run_id, task_fp, no_critic=True)

    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        summary=None,
        no_critic=True,
        run_dir=run_dir,
        task_fingerprint=task_fp,
        run_id=run_id,
    )

    assert is_complete is True
    assert inc_stage is None
    assert reason == "All stages completed successfully."


# ==============================================================================
# RC-02: Dashboard Terminal Banner State Semantics
# ==============================================================================


def test_rc02_interrupted_run_with_complete_artifacts_reconciles_to_approved(tmp_path: Path):
    """RC-02.1: An interrupted run whose physical deliverables are complete
    reconciles to APPROVED and never renders PIPELINE FAILED.
    """
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task = "Build feature"
    task_fp = compute_task_fingerprint(task)
    run_id = "run-1"

    _setup_stage_artifacts(run_dir, run_id, task_fp, no_critic=True)

    meta = {
        "run_id": run_id,
        "task": task,
        "status": "IN_PROGRESS",
        "metadata": {"no_critic": True},
    }
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    app = DashboardApp(run_dir=run_dir)
    app._apply_terminal_state()

    assert app.state.terminal_status == "APPROVED"
    assert app.run_model.status == "APPROVED"

    output = app.render_once()
    assert "PIPELINE COMPLETED" in output
    assert "PIPELINE FAILED" not in output

    # Verify metadata.json was updated to APPROVED
    updated_meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert updated_meta["status"] == "APPROVED"


def test_rc02_interrupted_incomplete_run_renders_interrupted_never_failed(tmp_path: Path):
    """RC-02.2: An in-progress/interrupted run with incomplete stages renders
    PIPELINE INTERRUPTED, never PIPELINE FAILED.
    """
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    task = "Build feature"
    task_fp = compute_task_fingerprint(task)
    run_id = "run-1"

    # Only Architect exists
    (run_dir / "01_architect.json").write_text(json.dumps({
        "run_id": run_id,
        "task_fingerprint": task_fp,
        "role": "architect",
        "sequence_number": 1,
        "status": "APPROVED",
    }), encoding="utf-8")

    meta = {
        "run_id": run_id,
        "task": task,
        "status": "IN_PROGRESS",
        "metadata": {"no_critic": True},
    }
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    app = DashboardApp(run_dir=run_dir)
    app._apply_terminal_state()

    assert app.state.terminal_status == "INCOMPLETE"

    # Verify that render_once or banner never renders PIPELINE FAILED for interrupted/incomplete run
    output = app.render_once()
    assert "PIPELINE FAILED" not in output
    assert "PIPELINE INCOMPLETE" in output or "PIPELINE INTERRUPTED" in output


def _render_state_banner(status: str, stage: str = None, reason: str = None) -> str:
    run_model = RunModel(
        run_id="run-test",
        task="Test",
        status=status,
        created_at="2026-10-08T00:00:00Z",
        run_dir=Path("."),
    )
    state = DashboardState(run=run_model)
    state.terminal_status = status
    state.terminal_stage = stage
    state.terminal_reason = reason
    panel = render_terminal_banner(state)
    console = Console()
    with console.capture() as cap:
        console.print(panel)
    return cap.get()


def test_rc02_terminal_banner_render_branches():
    """RC-02.3: TerminalBanner explicitly and truthfully handles all terminal states."""
    # APPROVED
    approved_out = _render_state_banner("APPROVED", None, "All stages completed successfully.")
    assert "PIPELINE COMPLETED" in approved_out

    # CANCELLED
    cancelled_out = _render_state_banner("CANCELLED", "Executor", "User aborted")
    assert "PIPELINE CANCELLED" in cancelled_out

    # INTERRUPTED / IN_PROGRESS
    interrupted_out = _render_state_banner("IN_PROGRESS", "Executor", "Process terminated")
    assert "PIPELINE INTERRUPTED" in interrupted_out

    # INCOMPLETE
    incomplete_out = _render_state_banner("INCOMPLETE", "Planner", "Stage artifact missing")
    assert "PIPELINE INCOMPLETE" in incomplete_out

    # Genuine FAILED
    failed_out = _render_state_banner("FAILED", "Tester", "Tests failed")
    assert "PIPELINE FAILED" in failed_out


# ==============================================================================
# RC-04: Mandatory Stage Provenance Fields in is_stage_completed
# ==============================================================================


def test_rc04_missing_task_fingerprint_fails_verification(tmp_path: Path):
    """RC-04.1: Missing task_fingerprint in stage artifact causes is_stage_completed to return False."""
    run_mgr = RunManager(project_root=tmp_path)
    run = run_mgr.create_run(task="Task 1")
    stage_def = StageOrder.get_architect()
    run.metadata["stage_fingerprints"] = {stage_def.name: run.task_fingerprint}

    art = run.run_dir / "01_architect.json"
    art.write_text(json.dumps({
        "run_id": run.run_id,
        "role": "architect",
        "sequence_number": 1,
        "status": "APPROVED",
    }), encoding="utf-8")

    completed, prior_status = is_stage_completed(run, stage_def, run_mgr)
    assert completed is False
    assert prior_status is None


def test_rc04_missing_run_id_fails_verification(tmp_path: Path):
    """RC-04.2: Missing run_id in stage artifact causes is_stage_completed to return False."""
    run_mgr = RunManager(project_root=tmp_path)
    run = run_mgr.create_run(task="Task 1")
    stage_def = StageOrder.get_architect()
    run.metadata["stage_fingerprints"] = {stage_def.name: run.task_fingerprint}

    art = run.run_dir / "01_architect.json"
    art.write_text(json.dumps({
        "task_fingerprint": run.task_fingerprint,
        "role": "architect",
        "sequence_number": 1,
        "status": "APPROVED",
    }), encoding="utf-8")

    completed, prior_status = is_stage_completed(run, stage_def, run_mgr)
    assert completed is False
    assert prior_status is None


def test_rc04_missing_role_fails_verification(tmp_path: Path):
    """RC-04.3: Missing role in stage artifact causes is_stage_completed to return False."""
    run_mgr = RunManager(project_root=tmp_path)
    run = run_mgr.create_run(task="Task 1")
    stage_def = StageOrder.get_architect()
    run.metadata["stage_fingerprints"] = {stage_def.name: run.task_fingerprint}

    art = run.run_dir / "01_architect.json"
    art.write_text(json.dumps({
        "run_id": run.run_id,
        "task_fingerprint": run.task_fingerprint,
        "sequence_number": 1,
        "status": "APPROVED",
    }), encoding="utf-8")

    completed, prior_status = is_stage_completed(run, stage_def, run_mgr)
    assert completed is False
    assert prior_status is None


def test_rc04_missing_sequence_number_fails_verification(tmp_path: Path):
    """RC-04.4: Missing sequence_number in stage artifact causes is_stage_completed to return False."""
    run_mgr = RunManager(project_root=tmp_path)
    run = run_mgr.create_run(task="Task 1")
    stage_def = StageOrder.get_architect()
    run.metadata["stage_fingerprints"] = {stage_def.name: run.task_fingerprint}

    art = run.run_dir / "01_architect.json"
    art.write_text(json.dumps({
        "run_id": run.run_id,
        "task_fingerprint": run.task_fingerprint,
        "role": "architect",
        "status": "APPROVED",
    }), encoding="utf-8")

    completed, prior_status = is_stage_completed(run, stage_def, run_mgr)
    assert completed is False
    assert prior_status is None


def test_rc04_all_provenance_present_and_valid_passes(tmp_path: Path):
    """RC-04.5: When all mandatory provenance fields match and status is success, is_stage_completed returns True."""
    run_mgr = RunManager(project_root=tmp_path)
    run = run_mgr.create_run(task="Task 1")
    stage_def = StageOrder.get_architect()
    run.metadata["stage_fingerprints"] = {stage_def.name: run.task_fingerprint}

    art = run.run_dir / "01_architect.json"
    art.write_text(json.dumps({
        "run_id": run.run_id,
        "task_fingerprint": run.task_fingerprint,
        "role": "architect",
        "sequence_number": 1,
        "status": "APPROVED",
    }), encoding="utf-8")

    completed, prior_status = is_stage_completed(run, stage_def, run_mgr)
    assert completed is True
    assert prior_status == "APPROVED"


def test_rc04_save_stage_artifacts_guarantees_all_provenance_keys(tmp_path: Path):
    """RC-04.6: RunManager.save_stage_artifacts always writes all 4 mandatory provenance fields."""
    rm = RunManager(project_root=tmp_path)
    run = rm.create_run(task="Test provenance writing")

    rm.save_stage_artifacts(
        run=run,
        sequence_number=1,
        role_name="architect",
        markdown_content="# Architect Deliverable",
        json_data={"status": "APPROVED"},
    )

    art_file = run.run_dir / "01_architect.json"
    assert art_file.exists()
    payload = json.loads(art_file.read_text(encoding="utf-8"))

    # Verify all four provenance keys exist and are non-empty
    assert payload.get("run_id") == run.run_id
    assert payload.get("task_fingerprint") == run.task_fingerprint
    assert payload.get("role") == "architect"
    assert payload.get("sequence_number") == 1
