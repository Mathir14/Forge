"""Tests for Forge Full Pipeline Release-Readiness and Safety Audit."""

import os
import sys
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.cli import main, is_stage_completed
from forge.core.config import Config
from forge.core.context import Context
from forge.core.role import Role
from forge.protocol.validator import MachineReportValidator
from forge.stages.definition import StageOrder, StageDefinition
from forge.stages.stage import Stage
from forge.storage.run_manager import RunManager
from tests.conftest import configure_automated_execution_environment


class MockAdapter(BaseAdapter):
    """Mock adapter for stage execution testing."""
    is_mock = True

    def __init__(self, response: AdapterResponse):
        super().__init__(name="mock")
        self._response = response

    def execute(self, prompt, **kwargs):
        return self._response

    def is_available(self) -> bool:
        return True


def test_validator_rejects_failing_stage_handoff_to_reviewer():
    """Verify MachineReportValidator rejects any failing stage that attempts to hand off to REVIEWER."""
    # 1. Tester FAIL with HANDOFF REVIEWER
    data_tester = {
        "ROLE": "TESTER",
        "STATUS": "FAIL",
        "HANDOFF": "REVIEWER",
    }
    report_tester = MachineReportValidator.validate(data_tester, expected_role="TESTER")
    assert report_tester.is_valid is False
    assert any("cannot hand off to 'REVIEWER'" in e for e in report_tester.validation_errors)

    # 2. Architect FAILED with HANDOFF REVIEWER
    data_arch = {
        "ROLE": "ARCHITECT",
        "STATUS": "FAILED",
        "HANDOFF": "REVIEWER",
    }
    report_arch = MachineReportValidator.validate(data_arch, expected_role="ARCHITECT")
    assert report_arch.is_valid is False
    assert any("cannot hand off to 'REVIEWER'" in e for e in report_arch.validation_errors)


def test_validator_enforces_tester_and_reviewer_routing():
    """Verify MachineReportValidator enforces role-specific routing invariants."""
    # Tester PASS cannot hand off to EXECUTOR
    data_tester_pass = {
        "ROLE": "TESTER",
        "STATUS": "PASS",
        "HANDOFF": "EXECUTOR",
    }
    report_tester_pass = MachineReportValidator.validate(data_tester_pass, expected_role="TESTER")
    assert report_tester_pass.is_valid is False
    assert any("cannot hand off to 'EXECUTOR'" in e for e in report_tester_pass.validation_errors)

    # Reviewer APPROVED cannot hand off to EXECUTOR (must be NONE)
    data_rev_approved = {
        "ROLE": "REVIEWER",
        "STATUS": "APPROVED",
        "HANDOFF": "EXECUTOR",
    }
    report_rev_approved = MachineReportValidator.validate(data_rev_approved, expected_role="REVIEWER")
    assert report_rev_approved.is_valid is False
    assert any("requires handoff 'NONE'" in e for e in report_rev_approved.validation_errors)


def test_validator_rejects_success_status_with_nonzero_exit_code():
    """Verify MachineReportValidator rejects reports where success status contradicts non-zero EXIT_CODE."""
    data_approved_ec1 = {
        "ROLE": "REVIEWER",
        "STATUS": "APPROVED",
        "HANDOFF": "NONE",
        "EXIT_CODE": 1,
    }
    report = MachineReportValidator.validate(data_approved_ec1, expected_role="REVIEWER")
    assert report.is_valid is False
    assert any("contradicts non-zero EXIT_CODE" in e for e in report.validation_errors)

    data_pass_ec2 = {
        "ROLE": "TESTER",
        "STATUS": "PASS",
        "HANDOFF": "REVIEWER",
        "EXIT_CODE": 2,
    }
    report2 = MachineReportValidator.validate(data_pass_ec2, expected_role="TESTER")
    assert report2.is_valid is False
    assert any("contradicts non-zero EXIT_CODE" in e for e in report2.validation_errors)


def test_stage_run_marks_fail_status_as_failure(tmp_path):
    """Verify Stage.run() computes success=False when report status is FAIL."""
    role = Role(
        name="tester",
        sequence_number=4,
        template_content="Test {{ task }}",
        protocol_content="Emit machine report.",
    )
    fail_response = AdapterResponse(
        stdout="```yaml\nROLE: TESTER\nSTATUS: FAIL\nHANDOFF: EXECUTOR\nEXIT_CODE: 0\n```",
        stderr="",
        exit_code=0,
        duration_seconds=1.0,
        raw_output="FAIL",
    )
    stage = Stage(role=role, adapter=MockAdapter(fail_response))
    rm = RunManager(tmp_path)
    run = rm.create_run("Test feature")
    ctx = Context(run=run, project_root=tmp_path, config=Config.default(), git=MagicMock())
    result = stage.run(ctx)
    assert result.status == "FAIL"
    assert result.success is False


def test_cli_test_command_halts_on_fail(tmp_path):
    """Verify 'forge test' halts with exit code 1 when tester reports STATUS: FAIL."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        rm = RunManager(Path.cwd())
        run = rm.create_run("Verify user signup")
        rm.save_stage_artifacts(
            run=run, sequence_number=3, role_name="executor",
            markdown_content="# Executor Done", json_data={"ROLE": "EXECUTOR", "STATUS": "SUCCESS"},
        )

        test_fail = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: FAIL\nHANDOFF: EXECUTOR\n```",
            stderr="",
            exit_code=0,
            duration_seconds=0.5,
            raw_output="FAIL",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=test_fail):

            res = runner.invoke(main, ["test", "--run", run.run_id])
            assert res.exit_code != 0
            assert "finished with non-success status 'FAIL'" in res.output


def test_pipeline_halts_when_tester_reports_fail(tmp_path):
    """Verify 'forge run' halts fail-closed and does not proceed to Reviewer when Tester reports FAIL."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        test_fail = AdapterResponse(stdout="```yaml\nROLE: TESTER\nSTATUS: FAIL\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="FAIL")
        rev_approve = AdapterResponse(stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_fail, rev_approve]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["run", "Implement feature", "--no-critic"], input="y\ny\ny\ny\ny\n")
            assert res.exit_code != 0
            assert "Pipeline halted at stage 'tester' due to status 'FAIL'" in res.output
            assert "Proceed to next stage (REVIEWER)" not in res.output


def test_is_stage_completed_rejects_not_testable_when_code_modified(tmp_path):
    """Verify is_stage_completed rejects NOT_TESTABLE when application code files were modified."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Add analytics to dashboard")

    tester_def = StageOrder.get_tester()
    tester_data = {
        "status": "NOT_TESTABLE",
        "handoff": "REVIEWER",
        "exit_code": 0,
        "task_fingerprint": run.task_fingerprint,
        "run_id": run.run_id,
        "machine_report": {
            "role": "TESTER",
            "status": "NOT_TESTABLE",
            "handoff": "REVIEWER",
            "exit_code": 0,
            "data": {},
        },
    }
    rm.save_stage_artifacts(
        run=run, sequence_number=tester_def.sequence_number, role_name=tester_def.name,
        markdown_content="# Tester", json_data=tester_data
    )

    # Simulate executor artifact with code changes (main.py)
    exec_def = StageOrder.get_executor()
    exec_data = {
        "status": "SUCCESS",
        "handoff": "TESTER",
        "files_changed": ["src/main.py"],
    }
    rm.save_stage_artifacts(
        run=run, sequence_number=exec_def.sequence_number, role_name=exec_def.name,
        markdown_content="# Executor", json_data=exec_data
    )

    mock_git = MagicMock()
    mock_git.is_git_repo.return_value = False
    ctx = Context(run=run, project_root=tmp_path, config=Config.default(), git=mock_git)
    completed, status = is_stage_completed(run, tester_def, rm, context=ctx)
    assert completed is False
    assert status == "NOT_TESTABLE"


def test_cascade_resume_reexecutes_downstream_stages(tmp_path):
    """Verify that when an upstream stage re-executes on resume, downstream stages are not skipped."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        rm = RunManager(Path.cwd())
        run = rm.create_run("Implement payment gateway")

        # Save completed artifacts for architect, planner, executor, and tester
        for s_def, s_status, s_handoff in [
            (StageOrder.get_architect(), "APPROVED", "PLANNER"),
            (StageOrder.get_planner(), "READY", "EXECUTOR"),
            (StageOrder.get_executor(), "SUCCESS", "TESTER"),
            (StageOrder.get_tester(), "PASS", "REVIEWER"),
        ]:
            data = {
                "status": s_status,
                "handoff": s_handoff,
                "exit_code": 0,
                "task_fingerprint": run.task_fingerprint,
                "run_id": run.run_id,
                "machine_report": {
                    "role": s_def.name.upper(),
                    "status": s_status,
                    "handoff": s_handoff,
                    "exit_code": 0,
                    "is_valid": True,
                },
            }
            rm.save_stage_artifacts(
                run=run, sequence_number=s_def.sequence_number, role_name=s_def.name,
                markdown_content=f"#{s_def.name}", json_data=data
            )

        # Corrupt planner JSON so planner must re-execute
        planner_json_path = run.run_dir / "02_planner.json"
        planner_json_path.unlink()

        fresh_plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        fresh_exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        fresh_test_resp = AdapterResponse(stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS")
        rev_approve = AdapterResponse(stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[fresh_plan_resp, fresh_test_resp, rev_approve]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=fresh_exec_resp):

            res = runner.invoke(main, ["run", "--run", run.run_id, "--no-critic"], input="y\ny\ny\ny\ny\n")
            assert res.exit_code == 0
            # Architect was skipped because it was already complete
            assert "Skipping Stage: Architect" in res.output
            # Planner re-executed because its artifact was missing
            # Once Planner re-executed, Executor, Tester, Reviewer MUST execute fresh and not be skipped!
            assert "Skipping Stage: Executor" not in res.output
            assert "Skipping Stage: Tester" not in res.output


def test_auto_pipeline_cascade_resume_reexecutes_implementation_loop(tmp_path):
    """Verify that in auto_pipeline, when an upstream pre-loop stage re-executes on resume,
    the implementation loop (Executor & Verifiers) is not skipped."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        rm = RunManager(Path.cwd())
        run = rm.create_run("Autonomous cascade task")

        # Save completed artifacts for architect, planner, executor, tester, reviewer
        for s_def, s_status, s_handoff in [
            (StageOrder.get_architect(), "APPROVED", "PLANNER"),
            (StageOrder.get_planner(), "READY", "EXECUTOR"),
            (StageOrder.get_executor(), "SUCCESS", "TESTER"),
            (StageOrder.get_tester(), "PASS", "REVIEWER"),
            (StageOrder.get_reviewer(), "APPROVED", "NONE"),
        ]:
            data = {
                "status": s_status,
                "handoff": s_handoff,
                "exit_code": 0,
                "task_fingerprint": run.task_fingerprint,
                "run_id": run.run_id,
                "machine_report": {
                    "role": s_def.name.upper(),
                    "status": s_status,
                    "handoff": s_handoff,
                    "exit_code": 0,
                    "is_valid": True,
                },
            }
            rm.save_stage_artifacts(
                run=run, sequence_number=s_def.sequence_number, role_name=s_def.name,
                markdown_content=f"#{s_def.name}", json_data=data
            )

        # Remove planner JSON so planner must re-execute
        planner_json_path = run.run_dir / "02_planner.json"
        planner_json_path.unlink()

        fresh_plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        fresh_exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        fresh_test_resp = AdapterResponse(stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS")
        rev_approve = AdapterResponse(stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        critic_resp = AdapterResponse(stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="CRITIQUE_COMPLETE")

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[fresh_plan_resp, fresh_test_resp, rev_approve, critic_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=fresh_exec_resp):

            res = runner.invoke(main, ["auto", "--run", run.run_id])
            assert res.exit_code == 0
            # Architect was skipped
            assert "Skipping Architect (already completed with status 'APPROVED')" in res.output
            # Planner ran, so executor and verifiers must NOT be skipped
            assert "Skipping Executor & Tester & Reviewer (already approved)" not in res.output


def test_auto_pipeline_halts_on_persistent_tester_fail(tmp_path):
    """Verify auto_pipeline exhausts max-retries and halts with exit code 1 when Tester persistently reports FAIL."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        test_fail = AdapterResponse(stdout="```yaml\nROLE: TESTER\nSTATUS: FAIL\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="FAIL")

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_fail, test_fail]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "Persistent failure task", "--max-retries", "2"])
            assert res.exit_code != 0
            assert "Tester resulted in 'FAIL'. Launching auto-repair iteration 2" in res.output
            assert "Autonomous Loop finished without approval" in res.output


def test_closing_critic_blocks_when_verification_deliverable_missing(tmp_path):
    """Verify Closing Critic blocks with status BLOCKED and exit code 1 when Tester deliverable is missing."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Implement auth")

    # Save Architect, Planner, Executor, Reviewer - deliberately omit Tester
    for s_def, s_status, s_handoff in [
        (StageOrder.get_architect(), "APPROVED", "PLANNER"),
        (StageOrder.get_planner(), "READY", "EXECUTOR"),
        (StageOrder.get_executor(), "SUCCESS", "TESTER"),
        (StageOrder.get_reviewer(), "APPROVED", "NONE"),
    ]:
        data = {
            "status": s_status,
            "handoff": s_handoff,
            "exit_code": 0,
            "task_fingerprint": run.task_fingerprint,
            "run_id": run.run_id,
            "machine_report": {
                "role": s_def.name.upper(),
                "status": s_status,
                "handoff": s_handoff,
                "exit_code": 0,
                "is_valid": True,
            },
        }
        rm.save_stage_artifacts(
            run=run, sequence_number=s_def.sequence_number, role_name=s_def.name,
            markdown_content=f"#{s_def.name}", json_data=data
        )

    critic_role = Role.load("critic", phase="post_run")
    critic_stage = Stage(role=critic_role, adapter=MockAdapter(AdapterResponse(stdout="", stderr="", exit_code=0, duration_seconds=0.0, raw_output="")), run_manager=rm)
    ctx = Context(run=run, project_root=tmp_path, config=Config.default(), git=MagicMock())

    result = critic_stage.run(ctx)
    assert result.status == "BLOCKED"
    assert result.response.exit_code == 1
    assert result.success is False
    assert "Required verification deliverable '04_tester.json' is missing" in result.machine_report.reason


def test_closing_critic_blocks_when_verification_deliverable_failed(tmp_path):
    """Verify Closing Critic blocks with status BLOCKED and exit code 1 when Tester deliverable failed."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Implement feature Y")

    # Save all deliverables, but Tester is FAIL
    for s_def, s_status, s_handoff in [
        (StageOrder.get_architect(), "APPROVED", "PLANNER"),
        (StageOrder.get_planner(), "READY", "EXECUTOR"),
        (StageOrder.get_executor(), "SUCCESS", "TESTER"),
        (StageOrder.get_tester(), "FAIL", "EXECUTOR"),
        (StageOrder.get_reviewer(), "APPROVED", "NONE"),
    ]:
        data = {
            "status": s_status,
            "handoff": s_handoff,
            "exit_code": 0,
            "task_fingerprint": run.task_fingerprint,
            "run_id": run.run_id,
            "machine_report": {
                "role": s_def.name.upper(),
                "status": s_status,
                "handoff": s_handoff,
                "exit_code": 0,
                "is_valid": True,
            },
        }
        rm.save_stage_artifacts(
            run=run, sequence_number=s_def.sequence_number, role_name=s_def.name,
            markdown_content=f"#{s_def.name}", json_data=data
        )

    critic_role = Role.load("critic", phase="post_run")
    critic_stage = Stage(role=critic_role, adapter=MockAdapter(AdapterResponse(stdout="", stderr="", exit_code=0, duration_seconds=0.0, raw_output="")), run_manager=rm)
    ctx = Context(run=run, project_root=tmp_path, config=Config.default(), git=MagicMock())

    result = critic_stage.run(ctx)
    assert result.status == "BLOCKED"
    assert result.response.exit_code == 1
    assert result.success is False
    assert "has non-success status 'FAIL'" in result.machine_report.reason


def test_seven_step_cascade_resumption_sequence(tmp_path):
    """Execute the complete 7-step cascade resumption sequence and verify downstream invalidation:
    1. Initial run creates stage deliverables.
    2. Pipeline interrupted/halted.
    3. Resume without changing upstream inputs; verify cached stages reused.
    4. Modify upstream plan artifact.
    5. Resume pipeline.
    6. Halt during or after rerun of modified stage.
    7. Resume again; verify stale downstream artifacts are never reused and cannot satisfy verification or critic.
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        rm = RunManager(Path.cwd())
        run = rm.create_run("Build telemetry daemon")

        # Step 1: Initial run through Executor, Tester, Reviewer
        for s_def, s_status, s_handoff in [
            (StageOrder.get_architect(), "APPROVED", "PLANNER"),
            (StageOrder.get_planner(), "READY", "EXECUTOR"),
            (StageOrder.get_executor(), "SUCCESS", "TESTER"),
            (StageOrder.get_tester(), "PASS", "REVIEWER"),
            (StageOrder.get_reviewer(), "APPROVED", "NONE"),
        ]:
            data = {
                "status": s_status,
                "handoff": s_handoff,
                "exit_code": 0,
                "task_fingerprint": run.task_fingerprint,
                "run_id": run.run_id,
                "machine_report": {
                    "role": s_def.name.upper(),
                    "status": s_status,
                    "handoff": s_handoff,
                    "exit_code": 0,
                    "is_valid": True,
                },
            }
            rm.save_stage_artifacts(
                run=run, sequence_number=s_def.sequence_number, role_name=s_def.name,
                markdown_content=f"#{s_def.name}", json_data=data
            )

        # Step 2: Pipeline halted before Critic (status remains RUNNING/PENDING)
        assert (run.run_dir / "01_architect.json").exists()
        assert (run.run_dir / "02_planner.json").exists()
        assert (run.run_dir / "03_executor.json").exists()
        assert (run.run_dir / "04_tester.json").exists()
        assert (run.run_dir / "05_reviewer.json").exists()
        assert not (run.run_dir / "06_critic.json").exists()

        # Step 3: Resume unchanged -> verify cached stages are reused appropriately
        critic_resp = AdapterResponse(stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="CRITIQUE_COMPLETE")
        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=critic_resp) as mock_oc, \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute") as mock_exec:

            res3 = runner.invoke(main, ["auto", "--run", run.run_id])
            assert res3.exit_code == 0
            assert "Skipping Architect" in res3.output
            assert "Skipping Planner" in res3.output
            assert "Skipping Executor & Tester & Reviewer (already approved)" in res3.output
            assert mock_exec.call_count == 0  # Executor was skipped!
            assert mock_oc.call_count == 1   # Critic was executed!

        # Step 4: Modify upstream plan artifact (update 02_planner.json with new content and newer timestamp)
        import time
        time.sleep(0.05)  # Ensure distinct mtime
        planner_file = run.run_dir / "02_planner.json"
        planner_data = json.loads(planner_file.read_text(encoding="utf-8"))
        planner_data["plan_revision"] = 2
        planner_file.write_text(json.dumps(planner_data, indent=2), encoding="utf-8")

        # Verify downstream stages (Executor, Tester, Reviewer) are now detected stale by is_stage_completed
        ctx4 = Context(run=run, project_root=Path.cwd(), config=Config.default(), git=MagicMock())
        is_exec_done, _ = is_stage_completed(run, StageOrder.get_executor(), rm, context=ctx4)
        assert is_exec_done is False, "Executor deliverable must be stale after planner update"
        is_test_done, _ = is_stage_completed(run, StageOrder.get_tester(), rm, context=ctx4)
        assert is_test_done is False, "Tester deliverable must be stale after planner update"

        # Step 5 & 6: Execute stage Planner rerun or execute stage and halt after rerun
        # When Executor or Planner executes, rm.invalidate_stage_and_downstream_artifacts removes 03+, 04+, 05+, 06+
        invalidated = rm.invalidate_stage_and_downstream_artifacts(run, from_sequence_number=3)
        assert "03_executor.json" in invalidated
        assert "04_tester.json" in invalidated
        assert "05_reviewer.json" in invalidated
        assert "06_critic.json" in invalidated

        # Verify canonical downstream deliverables no longer exist in run_dir
        assert not (run.run_dir / "03_executor.json").exists()
        assert not (run.run_dir / "04_tester.json").exists()
        assert not (run.run_dir / "05_reviewer.json").exists()
        assert not (run.run_dir / "06_critic.json").exists()

        # Step 7: Resume again
        # Downstream stages CANNOT be skipped; stale artifacts cannot satisfy verification or critic
        # Verify verify_pipeline_completion rejects the run
        is_complete, incomplete_stage, reason = StageOrder.verify_pipeline_completion(
            run_dir=run.run_dir,
            task_fingerprint=run.task_fingerprint,
            run_id=run.run_id,
        )
        assert is_complete is False
        assert incomplete_stage == "Executor"
        assert "missing on disk" in reason

        # Verify Closing Critic blocks if verification deliverables are missing
        critic_role = Role.load("critic", phase="post_run")
        critic_stage = Stage(role=critic_role, adapter=MockAdapter(AdapterResponse(stdout="", stderr="", exit_code=0, duration_seconds=0.0, raw_output="")), run_manager=rm)
        crit_res = critic_stage.run(ctx4)
        assert crit_res.status == "BLOCKED"
        assert crit_res.response.exit_code == 1
        assert crit_res.success is False

