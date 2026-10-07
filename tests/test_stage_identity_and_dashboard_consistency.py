"""Regression tests for stage identity, cross-run contamination prevention, and dashboard consistency.

Verifies:
1. run-007 cannot display run-005's terminal reason when Critic emits invalid role/reason.
2. Successful Critic completion produces terminal status APPROVED rather than FAILED.
3. Selecting Critic loads Critic metadata/artifact with canonical ROLE: CRITIC, not Reviewer.
4. Critic always appears in the canonical sidebar when part of configured pipeline, even without artifacts.
5. Completion counter correctly reflects defined executable stage set (handles uppercase Tester V2 JSON).
6. Resumed run with previous stages SKIPPED and Critic newly executed has correct terminal state.
7. A run resumed from an earlier run cannot inherit that earlier run's terminal reason/status.
8. Dashboard summary and selected-stage detail refer to the same stage identity.
"""

import json
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import pytest
from rich.console import Console

from forge.cli import main
from forge.adapters.base import AdapterResponse
from forge.dashboard.app import DashboardApp
from forge.dashboard.components.artifact_view import render_artifact_view
from forge.dashboard.components.header import render_header
from forge.dashboard.model import RunModel, StageModel
from forge.dashboard.state import DashboardState
from forge.protocol.report import MachineReport
from forge.protocol.validator import MachineReportValidator
from forge.stages.definition import StageOrder
from forge.stages.result import StageResult
from forge.storage.run_manager import RunManager
from tests.conftest import configure_automated_execution_environment


def test_invalid_critic_role_does_not_leak_stale_or_invalid_reason(tmp_path: Path):
    """Criterion 1 & 2: run-007 cannot display run-005's terminal reason.

    When Critic emits an invalid report with ROLE: REVIEWER and a reason referencing run-005,
    the pipeline must halt with FAILED, record the validation error as the terminal reason,
    and update Critic's stage status to FAILED in RunSummary (never APPROVED).
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="READY",
        )
        exec_resp = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: COMPLETE\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=2.0, raw_output="COMPLETE",
        )
        test_resp = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="PASS",
        )
        rev_resp = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )
        # Critic mistakenly echoes Reviewer's header and mentions run-005
        critic_invalid = AdapterResponse(
            stdout=(
                "# Critic Audit\n\n"
                "```yaml\n"
                "ROLE: REVIEWER\n"
                "STATUS: APPROVED\n"
                "HANDOFF: NONE\n"
                "REASON: Pipeline complete; run-005 approved with no blocking issues.\n"
                "```"
            ),
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, critic_invalid]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "Fix issues from run-005"])
            assert res.exit_code == 1

        rm = RunManager(Path.cwd())
        run = rm.latest()
        assert run.status == "FAILED"
        assert run.summary is not None
        assert run.summary.final_status == "FAILED"

        # Crucial: the reason MUST NOT be run-005's text; it must report the validation error
        assert "run-005 approved" not in run.summary.reason
        assert "Expected role 'CRITIC', got 'REVIEWER'" in run.summary.reason

        # Critic stage record must be FAILED, NOT APPROVED
        critic_stage = run.summary.get_stage("Critic")
        assert critic_stage is not None
        assert critic_stage.status == "FAILED"
        assert critic_stage.execution_state == "COMPLETED"


def test_successful_critic_completion_produces_approved_status(tmp_path: Path):
    """Criterion 2: Successful Critic completion produces terminal status APPROVED."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="READY",
        )
        exec_resp = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: COMPLETE\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=2.0, raw_output="COMPLETE",
        )
        test_resp = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="PASS",
        )
        rev_resp = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )
        critic_success = AdapterResponse(
            stdout=(
                "# Critic Audit\n\n"
                "```yaml\n"
                "ROLE: CRITIC\n"
                "STATUS: APPROVED\n"
                "HANDOFF: NONE\n"
                "REASON: Codebase clean with no architectural drift.\n"
                "```"
            ),
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, critic_success]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "Standard clean run"])
            assert res.exit_code == 0

        rm = RunManager(Path.cwd())
        run = rm.latest()
        assert run.status == "APPROVED"
        assert run.summary is not None
        assert run.summary.final_status == "APPROVED"

        critic_stage = run.summary.get_stage("Critic")
        assert critic_stage is not None
        assert critic_stage.status == "APPROVED"
        assert critic_stage.execution_state == "COMPLETED"


def test_selecting_critic_loads_critic_metadata_not_reviewer(tmp_path: Path):
    """Criterion 3 & 8: Selecting Critic loads Critic metadata with canonical ROLE: CRITIC."""
    run_dir = tmp_path / "run-007"
    run_dir.mkdir(parents=True)

    # Corrupted 06_critic.json containing ROLE: REVIEWER emitted by model
    critic_json = {
        "role": "critic",
        "sequence_number": 6,
        "status": "APPROVED",
        "duration_seconds": 12.5,
        "exit_code": 0,
        "machine_report": {
            "role": "REVIEWER",
            "status": "APPROVED",
            "reason": "Pipeline complete; run-005 approved with no blocking issues.",
            "is_valid": False,
            "validation_errors": ["Expected role 'CRITIC', got 'REVIEWER'"],
        },
    }
    with open(run_dir / "06_critic.json", "w", encoding="utf-8") as f:
        json.dump(critic_json, f)

    with open(run_dir / "06_critic.md", "w", encoding="utf-8") as f:
        f.write("# Critic Deliverable\n\n```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\n```")

    model = RunModel.from_dir(run_dir)
    critic_stage = model.get_stage("06_critic")
    assert critic_stage is not None
    assert critic_stage.role_name == "critic"
    assert critic_stage.machine_report is not None

    # Canonical role enforcement
    assert critic_stage.machine_report.role == "CRITIC"
    assert not critic_stage.machine_report.is_valid
    assert critic_stage.machine_report.status == "FAILED"

    # Render artifact view in machine mode
    state = DashboardState(run=model, active_tab=2)
    state.selected_stage_index = model.stages.index(critic_stage)
    state.active_content_tab = "machine"

    rendered_panel = render_artifact_view(state)
    console = Console(width=100)
    with console.capture() as capture:
        console.print(rendered_panel)
    output = capture.get()

    assert "CRITIC" in output
    assert "REVIEWER" not in output.split("ROLE:")[1].splitlines()[0]
    assert "VALIDATION ERRORS:" in output
    assert "Expected role 'CRITIC', got 'REVIEWER'" in output


def test_critic_always_appears_in_canonical_sidebar_regardless_of_artifacts(tmp_path: Path):
    """Criterion 4: Critic always appears in the canonical sidebar for pipeline runs even if it never ran."""
    run_dir = tmp_path / "run-early-fail"
    run_dir.mkdir(parents=True)

    meta = {
        "run_id": "run-early-fail",
        "task": "Test task halting early",
        "created_at": "2026-10-07T10:00:00Z",
        "status": "FAILED",
        "pipeline_type": "auto",
        "summary": {
            "run_id": "run-early-fail",
            "final_status": "FAILED",
            "stages": [
                {"name": "Architect", "status": "APPROVED", "execution_state": "COMPLETED", "role_name": "architect", "sequence_number": 1},
                {"name": "Planner", "status": "READY", "execution_state": "COMPLETED", "role_name": "planner", "sequence_number": 2},
                {"name": "Executor", "status": "SUCCESS", "execution_state": "COMPLETED", "role_name": "executor", "sequence_number": 3},
                {"name": "Tester", "status": "FAILED", "execution_state": "COMPLETED", "role_name": "tester", "sequence_number": 4},
                {"name": "Reviewer", "status": "—", "execution_state": "NOT REACHED", "role_name": "reviewer", "sequence_number": 5},
                {"name": "Critic", "status": "—", "execution_state": "NOT REACHED", "role_name": "critic", "sequence_number": 6},
            ],
        },
    }
    with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)

    # Only stages up to Tester have artifacts on disk; Critic has NO file on disk
    with open(run_dir / "01_architect.json", "w", encoding="utf-8") as f:
        json.dump({"status": "APPROVED"}, f)
    with open(run_dir / "02_planner.json", "w", encoding="utf-8") as f:
        json.dump({"status": "READY"}, f)
    with open(run_dir / "03_executor.json", "w", encoding="utf-8") as f:
        json.dump({"status": "SUCCESS"}, f)
    with open(run_dir / "04_tester.json", "w", encoding="utf-8") as f:
        json.dump({"status": "FAILED"}, f)

    model = RunModel.from_dir(run_dir)
    stage_names = [s.stage_name for s in model.stages]

    # Critic MUST be present in stages and sidebar despite no 06_critic files existing
    assert "06_critic" in stage_names
    critic_stage = model.get_stage("06_critic")
    assert critic_stage is not None
    assert critic_stage.sequence_number == 6
    assert critic_stage.role_name == "critic"


def test_completion_counter_handles_uppercase_tester_json(tmp_path: Path):
    """Criterion 5: Completion counter correctly reflects defined executable stage set.

    Tester V2 writes uppercase keys (STATUS: PASS, DURATION, EXIT_CODE).
    The header must parse Tester's status and duration properly and report 6/6 completed.
    """
    run_dir = tmp_path / "run-all-done"
    run_dir.mkdir(parents=True)

    meta = {
        "run_id": "run-all-done",
        "task": "Full run verification",
        "created_at": "2026-10-07T10:00:00Z",
        "status": "APPROVED",
        "pipeline_type": "auto",
    }
    with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)

    # Stages 1-3 lowercase
    with open(run_dir / "01_architect.json", "w", encoding="utf-8") as f:
        json.dump({"status": "APPROVED", "duration_seconds": 10.0}, f)
    with open(run_dir / "02_planner.json", "w", encoding="utf-8") as f:
        json.dump({"status": "READY", "duration_seconds": 15.0}, f)
    with open(run_dir / "03_executor.json", "w", encoding="utf-8") as f:
        json.dump({"status": "SUCCESS", "duration_seconds": 30.0}, f)

    # Stage 4: Tester V2 uppercase JSON
    with open(run_dir / "04_tester.json", "w", encoding="utf-8") as f:
        json.dump({"STATUS": "PASS", "DURATION": 12.5, "EXIT_CODE": 0}, f)

    # Stages 5-6 lowercase
    with open(run_dir / "05_reviewer.json", "w", encoding="utf-8") as f:
        json.dump({"status": "APPROVED", "duration_seconds": 20.0}, f)
    with open(run_dir / "06_critic.json", "w", encoding="utf-8") as f:
        json.dump({"status": "CRITIQUE_COMPLETE", "duration_seconds": 25.0}, f)

    model = RunModel.from_dir(run_dir)

    # Verify Tester stage parsing
    tester = model.get_stage("04_tester")
    assert tester is not None
    assert tester.status == "PASS"
    assert tester.duration_seconds == 12.5
    assert tester.exit_code == 0

    state = DashboardState(run=model)
    panel = render_header(state)
    console = Console(width=120)
    with console.capture() as capture:
        console.print(panel)
    header_output = capture.get()

    # The header must say 6/6 completed, NOT 5/6 completed
    assert "6/6 completed" in header_output


def test_resumed_run_resets_stale_terminal_state(tmp_path: Path):
    """Criterion 6 & 7: A run resumed from an earlier run cannot inherit that earlier run's terminal reason/status."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        # Step 1: Create a run that halts at Reviewer
        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="READY",
        )
        exec_resp = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: COMPLETE\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=2.0, raw_output="COMPLETE",
        )
        test_resp = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="PASS",
        )
        rev_fail = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: CHANGES_REQUIRED\nHANDOFF: EXECUTOR\nREASON: Earlier run failure reason\n```",
            stderr="", exit_code=0, duration_seconds=1.5, raw_output="CHANGES_REQUIRED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_fail]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res1 = runner.invoke(main, ["auto", "Resumed run test", "--max-retries", "1"])
            assert res1.exit_code == 1

        rm = RunManager(Path.cwd())
        run1 = rm.latest()
        assert run1.status == "CHANGES_REQUIRED"
        assert run1.summary.reason == "Earlier run failure reason"

        # Step 2: Resume the run with successful Reviewer & Critic
        # On resume, Architect, Planner, Executor are already done. Loop will run Executor, Tester, Reviewer, then Critic.
        exec_pass = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: COMPLETE\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="COMPLETE",
        )
        test_pass = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="PASS",
        )
        rev_pass = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )
        critic_pass = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: APPROVED\nHANDOFF: NONE\nREASON: All clean.\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[test_pass, rev_pass, critic_pass]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_pass):

            res2 = runner.invoke(main, ["auto", "--run", run1.run_id])
            assert res2.exit_code == 0

        run2 = rm.resume(run1.run_id)
        assert run2.status == "APPROVED"
        assert run2.summary is not None
        assert run2.summary.final_status == "APPROVED"
        # The earlier failure reason must NOT be present
        assert run2.summary.reason is None or "Earlier run failure reason" not in run2.summary.reason


def test_resumed_run_skips_prior_stages_and_executes_critic_successfully(tmp_path: Path):
    """Criterion 6: Resumed run with previous stages SKIPPED and Critic newly executed has correct terminal state."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        # Pre-seed run where Architect, Planner, Executor, Tester, Reviewer completed successfully
        rm = RunManager(Path.cwd())
        run = rm.create_run(task="Resume Critic run")
        rm.save_stage_artifacts(run, sequence_number=1, role_name="architect", markdown_content="# Architect\n\n```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", json_data={"status": "APPROVED"})
        rm.save_stage_artifacts(run, sequence_number=2, role_name="planner", markdown_content="# Planner\n\n```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", json_data={"status": "READY"})
        rm.save_stage_artifacts(run, sequence_number=3, role_name="executor", markdown_content="# Executor\n\n```yaml\nROLE: EXECUTOR\nSTATUS: COMPLETE\nHANDOFF: TESTER\n```", json_data={"status": "COMPLETE"})
        rm.save_stage_artifacts(run, sequence_number=4, role_name="tester", markdown_content="# Tester\n\n```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```", json_data={"status": "PASS"})
        rm.save_stage_artifacts(run, sequence_number=5, role_name="reviewer", markdown_content="# Reviewer\n\n```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", json_data={"status": "APPROVED"})

        critic_resp = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: APPROVED\nHANDOFF: NONE\nREASON: All clear.\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=critic_resp):

            res = runner.invoke(main, ["auto", "--run", run.run_id])
            assert res.exit_code == 0

        resumed_run = rm.resume(run.run_id)
        assert resumed_run.status == "APPROVED"
        summary = resumed_run.summary
        assert summary is not None
        assert summary.final_status == "APPROVED"

        # Stages 1-5 must be SKIPPED
        assert summary.get_stage("Architect").execution_state == "SKIPPED"
        assert summary.get_stage("Planner").execution_state == "SKIPPED"
        assert summary.get_stage("Executor").execution_state == "SKIPPED"
        assert summary.get_stage("Tester").execution_state == "SKIPPED"
        assert summary.get_stage("Reviewer").execution_state == "SKIPPED"

        # Critic must be COMPLETED and APPROVED
        critic_item = summary.get_stage("Critic")
        assert critic_item is not None
        assert critic_item.execution_state == "COMPLETED"
        assert critic_item.status == "APPROVED"


def test_dashboard_summary_and_selected_stage_identity_alignment(tmp_path: Path):
    """Criterion 8: Dashboard summary and selected-stage detail refer to the same stage identity."""
    run_dir = tmp_path / "run-alignment"
    run_dir.mkdir(parents=True)

    meta = {
        "run_id": "run-alignment",
        "task": "Identity alignment test",
        "status": "APPROVED",
        "pipeline_type": "auto",
        "summary": {
            "run_id": "run-alignment",
            "final_status": "APPROVED",
            "stages": [
                {"name": "Architect", "status": "APPROVED", "execution_state": "COMPLETED", "role_name": "architect", "sequence_number": 1},
                {"name": "Planner", "status": "READY", "execution_state": "COMPLETED", "role_name": "planner", "sequence_number": 2},
                {"name": "Executor", "status": "SUCCESS", "execution_state": "COMPLETED", "role_name": "executor", "sequence_number": 3},
                {"name": "Tester", "status": "PASS", "execution_state": "COMPLETED", "role_name": "tester", "sequence_number": 4},
                {"name": "Reviewer", "status": "APPROVED", "execution_state": "COMPLETED", "role_name": "reviewer", "sequence_number": 5},
                {"name": "Critic", "status": "APPROVED", "execution_state": "COMPLETED", "role_name": "critic", "sequence_number": 6},
            ],
        },
    }
    with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)

    # 05_reviewer.json and 06_critic.json
    with open(run_dir / "05_reviewer.json", "w", encoding="utf-8") as f:
        json.dump({"status": "APPROVED", "machine_report": {"role": "REVIEWER", "status": "APPROVED"}}, f)
    with open(run_dir / "06_critic.json", "w", encoding="utf-8") as f:
        json.dump({"status": "APPROVED", "machine_report": {"role": "CRITIC", "status": "APPROVED"}}, f)

    model = RunModel.from_dir(run_dir)
    state = DashboardState(run=model)

    # Select Reviewer (index 4)
    rev_stage = model.get_stage("05_reviewer")
    state.selected_stage_index = model.stages.index(rev_stage)
    assert state.current_stage.stage_name == "05_reviewer"
    assert state.current_stage.role_name == "reviewer"
    summary_rev = state.get_run_summary().get_stage("Reviewer")
    assert summary_rev is not None
    assert summary_rev.role_name == "reviewer"

    # Select Critic (index 5)
    critic_stage = model.get_stage("06_critic")
    state.selected_stage_index = model.stages.index(critic_stage)
    assert state.current_stage.stage_name == "06_critic"
    assert state.current_stage.role_name == "critic"
    summary_critic = state.get_run_summary().get_stage("Critic")
    assert summary_critic is not None
    assert summary_critic.role_name == "critic"

    # Reviewer and Critic must NEVER collide
    assert state.current_stage.role_name != rev_stage.role_name
    assert summary_critic.role_name != summary_rev.role_name

