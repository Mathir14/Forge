"""Comprehensive regression and verification tests for Audit Findings F-01 through F-11."""

import json
import os
import subprocess
import typing
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.base import AdapterResponse, BaseAdapter
from forge.adapters.codex import CodexAdapter
from forge.adapters.opencode import OpenCodeAdapter
from forge.adapters.registry import AdapterRegistry
from forge.cli import STAGE_SUCCESS_STATUSES, is_stage_completed, main
from forge.core.capabilities import Capability
from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.core.run import Run
from forge.prompts.builder import InstructionBuilder
from forge.stages.definition import STAGE_DEFINITIONS, StageDefinition, StageOrder
from forge.storage.run_manager import RunManager
from tests.conftest import configure_automated_execution_environment


# ===========================================================================
# F-01: Antigravity prompt transport bounds
# ===========================================================================

def test_f01_antigravity_transport_prompt_within_bounds():
    """F-01: Prompts within bounds execute normally via subprocess."""
    adapter = AntigravityAdapter()
    safe_prompt = "Hello Antigravity " * 100  # ~1.8KB, well within 130KB

    with patch("shutil.which", return_value="/mock/bin/agy"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="Output", stderr="", returncode=0)
        resp = adapter.execute(safe_prompt)
        assert resp.exit_code == 0
        assert resp.raw_output == "Output"
        mock_run.assert_called_once()


def test_f01_antigravity_transport_prompt_exceeding_bounds():
    """F-01: Prompts exceeding 130,000 bytes fail safely before subprocess invocation without E2BIG crash."""
    adapter = AntigravityAdapter()
    oversized_prompt = "X" * 135000  # Exceeds 130KB limit

    with patch("shutil.which", return_value="/mock/bin/agy"), \
         patch("subprocess.run") as mock_run:
        resp = adapter.execute(oversized_prompt)
        # Subprocess must NOT be invoked with an argument that causes OSError [Errno 7]
        mock_run.assert_not_called()
        assert resp.exit_code == 1
        assert "exceeds Antigravity CLI argv transport limit" in resp.raw_output
        assert "135000 bytes" in resp.raw_output


# ===========================================================================
# F-02: GitService protected path exclusion in diff and operations
# ===========================================================================

def test_f02_git_diff_excludes_protected_paths(tmp_path):
    """F-02: git.diff() and diff(cached=True) pass pathspec exclusions preventing .forge and .env leakage."""
    git = GitService(tmp_path)
    captured_commands = []

    def mock_run(args, **kwargs):
        captured_commands.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(git, "_run", side_effect=mock_run):
        git.diff()
        git.diff(cached=True)

    # Filter for git diff invocations
    diff_commands = [cmd for cmd in captured_commands if cmd and cmd[0] == "diff"]
    assert len(diff_commands) >= 2
    for cmd in diff_commands:
        assert "--" in cmd
        assert ":(exclude).forge" in cmd
        assert ":(exclude).forge/**" in cmd
        assert ":(exclude).env" in cmd
        assert ":(exclude).env*" in cmd
        assert ":(exclude)**/.env*" in cmd


def test_f02_is_protected_path_coverage():
    """F-02: GitService.is_protected_path() accurately classifies sensitive and runtime paths."""
    assert GitService.is_protected_path(".forge/runs/run-001/01_architect.md") is True
    assert GitService.is_protected_path(".forge/config.json") is True
    assert GitService.is_protected_path(".env") is True
    assert GitService.is_protected_path(".env.local") is True
    assert GitService.is_protected_path(".env.production") is True
    assert GitService.is_protected_path("submodule/.env") is True
    assert GitService.is_protected_path("src/app/.env.test") is True

    assert GitService.is_protected_path("src/forge/core/git.py") is False
    assert GitService.is_protected_path("README.md") is False
    assert GitService.is_protected_path("forge.yaml") is False


# ===========================================================================
# F-03: Untracked symlink dereferencing
# ===========================================================================

def test_f03_untracked_symlink_does_not_dereference_target(tmp_path):
    """F-03: Untracked symlink produces git 120000 diff without dumping target content or resolving path."""
    git = GitService(tmp_path)
    target_file = tmp_path / "big_target.txt"
    target_file.write_text("HUGE TARGET CONTENT\n" * 1000, encoding="utf-8")

    link_file = tmp_path / "symlink_test.txt"
    link_file.symlink_to(target_file)

    mock_status = "?? symlink_test.txt\n"

    def mock_run(args, **kwargs):
        if args and args[0] == "diff" and "/dev/null" not in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        elif "-c" in args and "status" in args:
            return MagicMock(returncode=0, stdout=mock_status, stderr="")
        elif "diff" in args and "/dev/null" in args:
            # git diff --no-index -- /dev/null symlink_test.txt
            symlink_arg = args[-1]
            return MagicMock(
                returncode=1,
                stdout=f"diff --git a//dev/null b/{symlink_arg}\nnew file mode 120000\n--- /dev/null\n+++ b/{symlink_arg}\n@@ -0,0 +1 @@\n+{target_file.name}\n",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(git, "_run", side_effect=mock_run):
        diff_out = git.diff(include_untracked=True)
        assert "mode 120000" in diff_out
        assert "symlink_test.txt" in diff_out
        # Target content must NOT be dumped into the diff
        assert "HUGE TARGET CONTENT" not in diff_out

        # Changed files must report the symlink path itself, not the resolved target
        changed = git.changed_files()
        assert "symlink_test.txt" in changed
        assert "big_target.txt" not in changed


# ===========================================================================
# F-04: Missing typing imports across definitions and adapters
# ===========================================================================

def test_f04_typing_get_type_hints_evaluates_without_name_errors():
    """F-04: typing.get_type_hints() succeeds on StageDefinition, StageOrder, and all adapters."""
    import forge.stages.definition
    import forge.adapters.base
    import forge.adapters.antigravity
    import forge.adapters.codex
    import forge.adapters.opencode

    hints_stage_def = typing.get_type_hints(StageDefinition)
    assert "required_capabilities" in hints_stage_def or "name" in hints_stage_def

    hints_stage_order = typing.get_type_hints(StageOrder)
    assert hints_stage_order is not None

    hints_base_adapter = typing.get_type_hints(BaseAdapter)
    assert hints_base_adapter is not None

    hints_agy_adapter = typing.get_type_hints(AntigravityAdapter)
    assert hints_agy_adapter is not None

    hints_codex_adapter = typing.get_type_hints(CodexAdapter)
    assert hints_codex_adapter is not None

    hints_opencode_adapter = typing.get_type_hints(OpenCodeAdapter)
    assert hints_opencode_adapter is not None


# ===========================================================================
# F-05: Closing Critic context includes pre-run Critic analysis
# ===========================================================================

def test_f05_closing_critic_retains_pre_run_critic_output(tmp_path):
    """F-05: Closing Critic (05_critic) retains 00_critic.md in context, dropping only its own artifact."""
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Test closing critic context", run_dir=run_dir)
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))

    # Pre-run critic artifact
    (run_dir / "00_critic.md").write_text("# Codebase Critic Report\nFound architectural debt.", encoding="utf-8")
    # Architect and Planner artifacts
    (run_dir / "01_architect.md").write_text("# Architect Design", encoding="utf-8")
    (run_dir / "02_planner.md").write_text("# Plan Breakdown", encoding="utf-8")
    (run_dir / "03_executor.md").write_text("# Execution Report", encoding="utf-8")
    (run_dir / "04_reviewer.md").write_text("# Reviewer Verdict: APPROVED", encoding="utf-8")
    # Own previous attempt artifact for closing critic
    (run_dir / "05_critic.md").write_text("# Old Closing Critic Report", encoding="utf-8")

    # When closing Critic builds instruction
    closing_critic_role = Role(
        name="critic",
        sequence_number=5,
        template_content="Audit codebase post-run.",
        phase="post_run",
    )
    instruction = InstructionBuilder.build(context, closing_critic_role)
    outputs = instruction.previous_stage_outputs

    # 00_critic output must be present for closing critic!
    assert "critic" in outputs or "00_critic" in outputs
    critic_content = outputs.get("critic") or outputs.get("00_critic")
    assert "Found architectural debt" in critic_content

    # 05_critic (its own artifact) must NOT be present
    assert "05_critic" not in outputs
    assert "Old Closing Critic Report" not in critic_content


def test_f05_pre_run_critic_excludes_own_artifact(tmp_path):
    """F-05: Pre-run Critic (00_critic) excludes 00_critic.md from its own context."""
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Test pre-run critic context", run_dir=run_dir)
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))

    (run_dir / "00_critic.md").write_text("# Old Pre-run Critic", encoding="utf-8")

    pre_critic_role = Role(
        name="critic",
        sequence_number=0,
        template_content="Audit codebase pre-run.",
        phase="pre_run",
    )
    instruction = InstructionBuilder.build(context, pre_critic_role)
    assert "critic" not in instruction.previous_stage_outputs
    assert "00_critic" not in instruction.previous_stage_outputs


# ===========================================================================
# F-06: StageOrder autonomous loop stages structure & dispatch
# ===========================================================================

def test_f06_stage_order_pipeline_stages_structure():
    """F-06: StageOrder defines pre-loop, loop, and post-loop stages accurately."""
    pre = StageOrder.pre_loop_stages()
    assert len(pre) == 2
    assert pre[0].name == "architect"
    assert pre[1].name == "planner"

    loop = StageOrder.implementation_loop_stages()
    assert len(loop) == 2
    assert loop[0].name == "executor"
    assert loop[1].name == "reviewer"

    post_with_critic = StageOrder.post_loop_stages(no_critic=False)
    assert len(post_with_critic) == 1
    assert post_with_critic[0].name == "critic"
    assert post_with_critic[0].sequence_number == 5

    post_no_critic = StageOrder.post_loop_stages(no_critic=True)
    assert len(post_no_critic) == 0

    all_auto = StageOrder.autonomous_loop_stages(no_critic=False)
    assert len(all_auto) == 5
    assert [s.name for s in all_auto] == ["architect", "planner", "executor", "reviewer", "critic"]


# ===========================================================================
# F-07: Auto resume skips completed closing Critic (Stage 5)
# ===========================================================================

def test_f07_auto_resume_skips_completed_closing_critic(tmp_path):
    """F-07: Resuming an auto run with an approved/complete closing Critic skips re-running Critic."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        mgr = RunManager(Path.cwd())
        run = mgr.create_run("Implement full pipeline")

        # Set up completed artifacts for ALL stages 1 to 5
        mgr.save_stage_artifacts(
            run=run, sequence_number=1, role_name="architect",
            markdown_content="# Arch", json_data={"status": "APPROVED", "is_valid": True},
        )
        mgr.save_stage_artifacts(
            run=run, sequence_number=2, role_name="planner",
            markdown_content="# Plan", json_data={"status": "READY", "is_valid": True},
        )
        mgr.save_stage_artifacts(
            run=run, sequence_number=3, role_name="executor",
            markdown_content="# Exec", json_data={"status": "SUCCESS", "is_valid": True},
        )
        mgr.save_stage_artifacts(
            run=run, sequence_number=4, role_name="reviewer",
            markdown_content="# Rev", json_data={"status": "APPROVED", "is_valid": True},
        )
        mgr.save_stage_artifacts(
            run=run, sequence_number=5, role_name="critic",
            markdown_content="# Critic", json_data={"status": "CRITIQUE_COMPLETE", "is_valid": True},
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute") as mock_opencode, \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute") as mock_agy:

            res = runner.invoke(main, ["auto", "--run", run.run_id])
            assert res.exit_code == 0
            # Critic should be skipped because it was already completed!
            assert "Skipping Post-Execution Critic" in res.output
            assert mock_opencode.call_count == 0
            assert mock_agy.call_count == 0


def test_f07_auto_resume_reruns_failed_closing_critic(tmp_path):
    """F-07: Resuming an auto run where closing Critic previously failed will re-run Critic."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        mgr = RunManager(Path.cwd())
        run = mgr.create_run("Implement full pipeline")

        mgr.save_stage_artifacts(
            run=run, sequence_number=1, role_name="architect",
            markdown_content="# Arch", json_data={"status": "APPROVED", "is_valid": True},
        )
        mgr.save_stage_artifacts(
            run=run, sequence_number=2, role_name="planner",
            markdown_content="# Plan", json_data={"status": "READY", "is_valid": True},
        )
        mgr.save_stage_artifacts(
            run=run, sequence_number=3, role_name="executor",
            markdown_content="# Exec", json_data={"status": "SUCCESS", "is_valid": True},
        )
        mgr.save_stage_artifacts(
            run=run, sequence_number=4, role_name="reviewer",
            markdown_content="# Rev", json_data={"status": "APPROVED", "is_valid": True},
        )
        # Critic previously had FAILED status
        mgr.save_stage_artifacts(
            run=run, sequence_number=5, role_name="critic",
            markdown_content="# Critic Failed", json_data={"status": "FAILED", "is_valid": False},
        )

        critic_success = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="CRITIQUE_COMPLETE",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=critic_success) as mock_opencode:

            res = runner.invoke(main, ["auto", "--run", run.run_id])
            assert res.exit_code == 0
            # Critic re-ran and succeeded
            assert mock_opencode.call_count == 1
            assert "Post-Execution Critic audit completed | Status: CRITIQUE_COMPLETE" in res.output


# ===========================================================================
# F-08: Standalone stage commands validate prerequisite success
# ===========================================================================

def test_f08_standalone_planner_rejects_unapproved_architect(tmp_path):
    """F-08: forge planner --run rejects run where Architect status is REJECTED or BLOCKED."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        mgr = RunManager(Path.cwd())
        run = mgr.create_run("Build system")

        # Save rejected architect output
        mgr.save_stage_artifacts(
            run=run, sequence_number=1, role_name="architect",
            markdown_content="# Bad Arch", json_data={"status": "REJECTED", "is_valid": True},
        )

        res = runner.invoke(main, ["planner", "--run", run.run_id])
        assert res.exit_code == 1
        assert "Prerequisite stage 'architect' did not succeed" in res.output
        assert "Status: 'REJECTED'" in res.output


def test_f08_standalone_execute_rejects_unapproved_planner(tmp_path):
    """F-08: forge execute --run rejects run where Planner status is BLOCKED or FAILED."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        mgr = RunManager(Path.cwd())
        run = mgr.create_run("Build system")

        mgr.save_stage_artifacts(
            run=run, sequence_number=2, role_name="planner",
            markdown_content="# Blocked Plan", json_data={"status": "BLOCKED", "is_valid": True},
        )

        res = runner.invoke(main, ["execute", "--run", run.run_id])
        assert res.exit_code == 1
        assert "Prerequisite stage 'planner' did not succeed" in res.output
        assert "Status: 'BLOCKED'" in res.output


def test_f08_standalone_review_rejects_failed_executor(tmp_path):
    """F-08: forge review --run rejects run where Executor status is FAILED."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        mgr = RunManager(Path.cwd())
        run = mgr.create_run("Build system")

        mgr.save_stage_artifacts(
            run=run, sequence_number=3, role_name="executor",
            markdown_content="# Failed Exec", json_data={"status": "FAILED", "is_valid": False},
        )

        res = runner.invoke(main, ["review", "--run", run.run_id])
        assert res.exit_code == 1
        assert "Prerequisite stage 'executor' did not succeed" in res.output
        assert "Status: 'FAILED'" in res.output


# ===========================================================================
# F-09: Canonical Run status is APPROVED on successful run_pipeline
# ===========================================================================

def test_f09_forge_run_sets_approved_status_on_success(tmp_path):
    """F-09: forge run pipeline completes with status APPROVED instead of COMPLETED."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        rev_resp = AdapterResponse(stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        critic_resp = AdapterResponse(stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="CRITIQUE_COMPLETE")

        # Provide answers 'y' to continue prompts between stages
        user_inputs = "y\ny\ny\ny\n"

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, rev_resp, critic_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["run", "Pipeline test"], input=user_inputs)
            assert res.exit_code == 0
            assert "Forge Pipeline completed successfully" in res.output

            mgr = RunManager(Path.cwd())
            latest_run = mgr.latest()
            assert latest_run is not None
            assert latest_run.status == "APPROVED"


# ===========================================================================
# F-10: Capability consistency for custom_flags
# ===========================================================================

def test_f10_custom_flags_declared_in_all_adapters():
    """F-10: custom_flags is declared in OpenCodeAdapter, AntigravityAdapter, and CodexAdapter."""
    opencode = OpenCodeAdapter()
    assert opencode.has_capability("custom_flags")
    assert opencode.has_capability(Capability.CUSTOM_FLAGS)
    assert "custom_flags" in opencode.capabilities()

    antigravity = AntigravityAdapter()
    assert antigravity.has_capability("custom_flags")
    assert antigravity.has_capability(Capability.CUSTOM_FLAGS)
    assert "custom_flags" in antigravity.capabilities()

    codex = CodexAdapter()
    assert codex.has_capability("custom_flags")
    assert codex.has_capability(Capability.CUSTOM_FLAGS)
    assert "custom_flags" in codex.capabilities()


def test_f10_forge_adapters_cli_reports_custom_flags():
    """F-10: forge adapters and forge adapters --json list custom_flags for supported adapters."""
    runner = CliRunner()
    res = runner.invoke(main, ["adapters", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    for adapter_info in data:
        assert "custom_flags" in adapter_info["capabilities"]


# ===========================================================================
# F-11: Magic string 03_executor.md replaced by StageOrder reference
# ===========================================================================

def test_f11_executor_report_resolved_via_stage_order(tmp_path):
    """F-11: InstructionBuilder dynamically resolves executor report artifact path using StageOrder."""
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Test executor report resolution", run_dir=run_dir)
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))

    exec_def = StageOrder.get_executor()
    exec_artifact = run_dir / f"{exec_def.artifact_prefix}.md"
    exec_artifact.write_text("# Dynamic Executor Report Content", encoding="utf-8")

    reviewer_role = Role(
        name="reviewer",
        sequence_number=4,
        template_content="Review implementation.",
    )

    instruction = InstructionBuilder.build(context, reviewer_role)
    assert instruction.executor_report is not None
    assert "Dynamic Executor Report Content" in instruction.executor_report
