"""Focused tests for StageOrder, explicit Critic semantics, run/auto deduplication, and status handling."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from click.testing import CliRunner

from forge.cli import main, is_stage_completed, execute_stage
from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.core.run import Run
from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.stages.definition import StageDefinition, StageOrder
from forge.storage.run_manager import RunManager
from tests.conftest import configure_automated_execution_environment


def test_stage_order_authoritative_definitions():
    """Verify StageOrder provides exactly the 7 ordered stages with explicit pre-run and closing Critic definitions."""
    stages = StageOrder.all_stages()
    assert len(stages) == 7

    expected = [
        ("critic", 0, "pre_run", True, False),
        ("architect", 1, "pre_run", False, False),
        ("planner", 2, "pre_run", False, False),
        ("executor", 3, "pre_run", False, False),
        ("tester", 4, "pre_run", False, False),
        ("reviewer", 5, "pre_run", False, False),
        ("critic", 6, "post_run", False, True),
    ]

    for stage_def, (exp_name, exp_seq, exp_phase, exp_pre_crit, exp_close_crit) in zip(stages, expected):
        assert stage_def.name == exp_name
        assert stage_def.sequence_number == exp_seq
        assert stage_def.phase == exp_phase
        assert stage_def.is_pre_run_critic == exp_pre_crit
        assert stage_def.is_closing_critic == exp_close_crit
        assert stage_def.artifact_prefix == f"{exp_seq:02d}_{exp_name}"


def test_critic_semantics_explicit_without_magic_numbers():
    """Verify Critic semantics are explicit on both StageDefinition and Role."""
    pre_critic_def = StageOrder.get_pre_run_critic()
    assert pre_critic_def.is_pre_run_critic is True
    assert pre_critic_def.is_closing_critic is False
    assert pre_critic_def.phase == "pre_run"
    assert pre_critic_def.sequence_number == 0

    closing_critic_def = StageOrder.get_closing_critic()
    assert closing_critic_def.is_pre_run_critic is False
    assert closing_critic_def.is_closing_critic is True
    assert closing_critic_def.phase == "post_run"
    assert closing_critic_def.sequence_number == 6

    # Role.load with phase
    role_pre = Role.load("critic", phase="pre_run")
    assert role_pre.is_pre_run_critic is True
    assert role_pre.is_closing_critic is False
    assert role_pre.sequence_number == 0

    role_post = Role.load("critic", phase="post_run")
    assert role_post.is_pre_run_critic is False
    assert role_post.is_closing_critic is True
    assert role_post.sequence_number == 6

    # Non-critic roles
    role_arch = Role.load("architect")
    assert role_arch.is_pre_run_critic is False
    assert role_arch.is_closing_critic is False
    assert role_arch.sequence_number == 1

    # StageOrder helper checks
    assert StageOrder.is_closing_critic(closing_critic_def) is True
    assert StageOrder.is_closing_critic(pre_critic_def) is False
    assert StageOrder.is_pre_run_critic(pre_critic_def) is True
    assert StageOrder.is_pre_run_critic(closing_critic_def) is False


def test_stage_order_drives_both_standard_and_auto_pipelines():
    """Verify single StageOrder source of truth drives both normal and auto pipeline definitions."""
    std_stages = StageOrder.standard_pipeline_stages(no_critic=False)
    auto_stages = StageOrder.autonomous_loop_stages(no_critic=False)

    assert len(std_stages) == 6
    assert len(auto_stages) == 6

    for std_s, auto_s in zip(std_stages, auto_stages):
        assert std_s.name == auto_s.name
        assert std_s.sequence_number == auto_s.sequence_number
        assert std_s.phase == auto_s.phase
        assert std_s.is_closing_critic == auto_s.is_closing_critic

    # With no_critic=True
    std_no_crit = StageOrder.standard_pipeline_stages(no_critic=True)
    auto_no_crit = StageOrder.autonomous_loop_stages(no_critic=True)
    assert len(std_no_crit) == 5
    assert len(auto_no_crit) == 5
    assert all(not s.is_closing_critic for s in std_no_crit)
    assert all(not s.is_closing_critic for s in auto_no_crit)


def test_is_stage_completed_status_distinctions(tmp_path):
    """Verify is_stage_completed differentiates SUCCESS/APPROVED from CHANGES_REQUIRED/FAILED/UNKNOWN."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run("Status test")

    arch_def = StageOrder.get_architect()
    rev_def = StageOrder.get_reviewer()

    # No report saved yet -> not completed
    done, status = is_stage_completed(run, arch_def, run_mgr)
    assert done is False
    assert status is None

    # Save Architect with APPROVED -> completed
    run_mgr.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Arch", json_data={"status": "APPROVED"},
    )
    done, status = is_stage_completed(run, arch_def, run_mgr)
    assert done is True
    assert status == "APPROVED"

    # Save Reviewer with CHANGES_REQUIRED -> NOT completed (must not count as successful)
    run_mgr.save_stage_artifacts(
        run=run, sequence_number=5, role_name="reviewer",
        markdown_content="# Review", json_data={"status": "CHANGES_REQUIRED"},
    )
    done, status = is_stage_completed(run, rev_def, run_mgr)
    assert done is False
    assert status == "CHANGES_REQUIRED"

    # Save Reviewer with FAILED -> NOT completed
    run_mgr.save_stage_artifacts(
        run=run, sequence_number=5, role_name="reviewer",
        markdown_content="# Review", json_data={"status": "FAILED"},
    )
    done, status = is_stage_completed(run, rev_def, run_mgr)
    assert done is False
    assert status == "FAILED"

    # Save Reviewer with APPROVED -> completed
    run_mgr.save_stage_artifacts(
        run=run, sequence_number=5, role_name="reviewer",
        markdown_content="# Review", json_data={"status": "APPROVED"},
    )
    done, status = is_stage_completed(run, rev_def, run_mgr)
    assert done is True
    assert status == "APPROVED"


def test_resume_preserves_completed_stages_without_reexecution(tmp_path):
    """Verify resuming a run skips completed stages and starts execution from the first pending stage."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        rm = RunManager(Path.cwd())
        run = rm.create_run("Add feature")

        # Mark Architect and Planner as completed
        rm.save_stage_artifacts(
            run=run, sequence_number=1, role_name="architect",
            markdown_content="# Arch", json_data={"status": "APPROVED"},
        )
        rm.save_stage_artifacts(
            run=run, sequence_number=2, role_name="planner",
            markdown_content="# Plan", json_data={"status": "READY"},
        )

        exec_resp = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS",
        )
        test_resp = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS",
        )
        rev_resp = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )
        critic_resp = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="CRITIQUE_COMPLETE",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[test_resp, rev_resp, critic_resp]) as mock_oc, \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp) as mock_agy:

            res = runner.invoke(main, ["run", "--run", run.run_id], input="y\ny\ny\ny\n")
            assert res.exit_code == 0

            # Architect and Planner skipped
            assert "Skipping Stage: Architect" in res.output
            assert "Skipping Stage: Planner" in res.output

            # Executor, Tester, Reviewer, and closing Critic executed
            assert "Executing Stage: 03_EXECUTOR" in res.output
            assert "Executing Stage: 04_TESTER" in res.output
            assert "Executing Stage: 05_REVIEWER" in res.output
            assert "Executing Stage: 06_CRITIC" in res.output


def test_run_pipeline_halts_on_reviewer_changes_required(tmp_path):
    """Verify run_pipeline halts with exit code 1 when Reviewer returns CHANGES_REQUIRED."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY",
        )
        exec_resp = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS",
        )
        test_resp = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS",
        )
        rev_changes = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: CHANGES_REQUIRED\nHANDOFF: EXECUTOR\nISSUES:\n  CRITICAL:\n    - Security vulnerability in auth check\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="CHANGES_REQUIRED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_changes]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["run", "Implement auth"], input="y\ny\ny\ny\n")
            assert res.exit_code == 1
            assert "Pipeline halted at stage 'reviewer'" in res.output

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "CHANGES_REQUIRED"


def test_closing_critic_rejection_prevents_approval(tmp_path):
    """Verify that a non-success closing Critic audit halts auto loop and prevents commit even if Reviewer approved."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY",
        )
        exec_resp = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS",
        )
        test_resp = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS",
        )
        rev_resp = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )
        critic_blocked = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: BLOCKED\nHANDOFF: NONE\nREASON: Severe regression detected in repository health\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="BLOCKED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, critic_blocked]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp), \
             patch("forge.core.git.GitService.commit") as mock_commit:

            res = runner.invoke(main, ["auto", "Implement feature", "--auto-commit"])
            assert res.exit_code == 1
            assert "Closing Critic audit reported non-success status 'BLOCKED'" in res.output

            # Commit must NOT have been called
            assert mock_commit.call_count == 0

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "BLOCKED"


def test_auto_pipeline_retry_on_changes_required_then_approved(tmp_path):
    """Verify auto_pipeline feeds Reviewer CHANGES_REQUIRED feedback into next Executor attempt and succeeds upon APPROVED."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY",
        )
        exec_resp1 = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="First attempt code",
        )
        exec_resp2 = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="Repaired code with tests",
        )
        test_resp1 = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS",
        )
        test_resp2 = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS",
        )
        rev_reject = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: CHANGES_REQUIRED\nHANDOFF: EXECUTOR\nISSUES:\n  MAJOR:\n    - Missing timeout handling\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="CHANGES_REQUIRED",
        )
        rev_approve = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )
        critic_resp = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="CRITIQUE_COMPLETE",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp1, rev_reject, test_resp2, rev_approve, critic_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", side_effect=[exec_resp1, exec_resp2]) as mock_exec:

            res = runner.invoke(main, ["auto", "Implement robust client", "--max-retries", "3"])
            assert res.exit_code == 0
            assert "Reviewer requested changes. Launching auto-repair iteration 2" in res.output
            assert "Implementation APPROVED by Tester & Reviewer on attempt 2" in res.output
            assert mock_exec.call_count == 2

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "APPROVED"
            # Attempt artifacts preserved
            assert (run.run_dir / "03_executor_attempt_1.md").exists()
            assert (run.run_dir / "03_executor_attempt_2.md").exists()
            assert (run.run_dir / "04_tester_attempt_1.md").exists()
            assert (run.run_dir / "04_tester_attempt_2.md").exists()
            assert (run.run_dir / "05_reviewer_attempt_1.md").exists()
            assert (run.run_dir / "05_reviewer_attempt_2.md").exists()


def test_run_pipeline_halts_on_unknown_status(tmp_path):
    """Verify run_pipeline halts with exit code 1 when a stage report produces status UNKNOWN."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])

        # Output with invalid protocol YAML -> UNKNOWN status
        corrupt_resp = AdapterResponse(
            stdout="I finished the work but forgot the YAML machine protocol block completely.",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="Missing protocol",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=corrupt_resp):

            res = runner.invoke(main, ["run", "Some task"])
            assert res.exit_code == 1
            assert "Pipeline halted at stage 'architect' due to status 'UNKNOWN'" in res.output

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "FAILED"


def test_stage_order_deduplication_consistency():
    """Verify that StageOrder definitions guarantee exact stage metadata parity across commands."""
    all_stages = StageOrder.all_stages()
    std_stages = StageOrder.standard_pipeline_stages()
    auto_stages = StageOrder.autonomous_loop_stages()

    # Pre-run critic
    pre_crit = StageOrder.get_pre_run_critic()
    assert pre_crit == all_stages[0]
    assert pre_crit.phase == "pre_run"

    # Closing critic
    closing_crit = StageOrder.get_closing_critic()
    assert closing_crit == all_stages[6]
    assert closing_crit.phase == "post_run"
    assert closing_crit.is_closing_critic is True

    # Standard pipeline ends with closing critic
    assert std_stages[-1] == closing_crit
    assert auto_stages[-1] == closing_crit

    # Middle stages are identical
    for i in range(5):
        assert std_stages[i] == auto_stages[i]
        assert std_stages[i] == all_stages[i + 1]

