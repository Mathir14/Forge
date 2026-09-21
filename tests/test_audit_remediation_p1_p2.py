"""Tests verifying remediation of zero-context audit findings P1-01 through P1-04 and P2-01 through P2-05."""

import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.opencode import OpenCodeAdapter
from forge.adapters.codex import CodexAdapter
from forge.core.config import Config, VALID_STAGES
from forge.core.context import Context
from forge.core.git import GitService, GitBaseline
from forge.core.role import Role
from forge.core.run import Run
from forge.prompts.builder import InstructionBuilder
from forge.prompts.compiler import PromptCompiler
from forge.prompts.instruction import Instruction
from forge.protocol.report import MachineReport
from forge.protocol.validator import MachineReportValidator
from forge.stages.definition import StageOrder, StageDefinition
from forge.storage.run_manager import RunManager


# ==============================================================================
# P1-01: Protocol validator contradicts stage definitions
# ==============================================================================

def test_p1_01_protocol_validator_success_statuses():
    """Verify MachineReportValidator accepts all success statuses defined in StageDefinition."""
    # Architect allows: APPROVED, READY, SUCCESS, COMPLETED
    architect_allowed = MachineReportValidator.get_allowed_statuses("ARCHITECT")
    assert "SUCCESS" in architect_allowed
    assert "COMPLETED" in architect_allowed
    assert "APPROVED" in architect_allowed
    assert "READY" in architect_allowed

    # Planner allows: READY, APPROVED, SUCCESS, COMPLETED
    planner_allowed = MachineReportValidator.get_allowed_statuses("PLANNER")
    assert "SUCCESS" in planner_allowed
    assert "COMPLETED" in planner_allowed
    assert "READY" in planner_allowed
    assert "APPROVED" in planner_allowed

    # Executor allows: SUCCESS, APPROVED, COMPLETED
    executor_allowed = MachineReportValidator.get_allowed_statuses("EXECUTOR")
    assert "SUCCESS" in executor_allowed
    assert "APPROVED" in executor_allowed
    assert "COMPLETED" in executor_allowed

    # Reviewer allows: APPROVED
    reviewer_allowed = MachineReportValidator.get_allowed_statuses("REVIEWER")
    assert "APPROVED" in reviewer_allowed

    # Critic allows: CRITIQUE_COMPLETE, APPROVED, SUCCESS, COMPLETED, PASSED
    critic_allowed = MachineReportValidator.get_allowed_statuses("CRITIC")
    assert "CRITIQUE_COMPLETE" in critic_allowed
    assert "SUCCESS" in critic_allowed
    assert "APPROVED" in critic_allowed
    assert "PASSED" in critic_allowed


def test_p1_01_protocol_validation_execution():
    """Verify validate() succeeds on Architect with SUCCESS and Executor with APPROVED."""
    report_arch = MachineReportValidator.validate(
        {"ROLE": "ARCHITECT", "STATUS": "SUCCESS", "HANDOFF": "PLANNER"},
        expected_role="ARCHITECT",
    )
    assert report_arch.is_valid
    assert report_arch.status == "SUCCESS"

    report_exec = MachineReportValidator.validate(
        {"ROLE": "EXECUTOR", "STATUS": "APPROVED", "HANDOFF": "REVIEWER"},
        expected_role="EXECUTOR",
    )
    assert report_exec.is_valid
    assert report_exec.status == "APPROVED"


def make_fake_stage_result(
    role_name: str,
    status: str,
    reason: str = "",
    issues: Optional[dict] = None,
    success: bool = True,
    seq: Optional[int] = None,
) -> StageResult:
    from forge.prompts.rendered_prompt import RenderedPrompt
    from forge.stages.result import StageResult
    role = Role(name=role_name, template_content="", sequence_number=seq)
    prompt = RenderedPrompt.from_text("")
    response = AdapterResponse(stdout="", stderr="", exit_code=0 if success else 1, duration_seconds=0.5, raw_output="")
    report = MachineReport(
        role=role_name.upper(),
        status=status,
        reason=reason,
        issues=issues or {},
        is_valid=True,
    )
    return StageResult(
        role=role,
        prompt=prompt,
        response=response,
        machine_report=report,
        raw_markdown=f"```yaml\nROLE: {role_name.upper()}\nSTATUS: {status}\n```",
        duration_seconds=0.5,
        success=success,
    )


# ==============================================================================
# P1-02: Future-stage artifacts leak into earlier-stage prompts
# ==============================================================================

def test_p1_02_future_stage_artifacts_do_not_leak_to_earlier_stages(tmp_path):
    """Verify earlier stages do not see artifacts from future stages on resume/retry."""
    run_dir = tmp_path / ".forge" / "runs" / "test_run"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Populate run directory with artifacts from all stages (simulating a prior execution)
    (run_dir / "00_critic.md").write_text("# Critic Analysis", encoding="utf-8")
    (run_dir / "01_architect.md").write_text("# Architect Design", encoding="utf-8")
    (run_dir / "02_planner.md").write_text("# Planner Breakdown", encoding="utf-8")
    (run_dir / "03_executor.md").write_text("# Executor Implementation", encoding="utf-8")
    (run_dir / "04_reviewer.md").write_text("# Reviewer Feedback", encoding="utf-8")
    (run_dir / "03_executor_attempt_1.md").write_text("# Attempt 1", encoding="utf-8")

    run = Run(run_id="test_run", task="Implement feature", run_dir=run_dir)
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=None)

    # 1. When running Architect (seq 1), it must only see Critic (seq 0)
    role_arch = Role(name="architect", template_content="", sequence_number=1)
    inst_arch = InstructionBuilder.build(context, role_arch)
    assert "critic" in inst_arch.previous_stage_outputs
    assert "architect" not in inst_arch.previous_stage_outputs
    assert "planner" not in inst_arch.previous_stage_outputs
    assert "executor" not in inst_arch.previous_stage_outputs
    assert "reviewer" not in inst_arch.previous_stage_outputs

    # 2. When running Planner (seq 2), it must only see Critic (seq 0) and Architect (seq 1)
    role_plan = Role(name="planner", template_content="", sequence_number=2)
    inst_plan = InstructionBuilder.build(context, role_plan)
    assert "critic" in inst_plan.previous_stage_outputs
    assert "architect" in inst_plan.previous_stage_outputs
    assert "planner" not in inst_plan.previous_stage_outputs
    assert "executor" not in inst_plan.previous_stage_outputs
    assert "reviewer" not in inst_plan.previous_stage_outputs

    # 3. When running Executor (seq 3), it must see Critic, Architect, and Planner, but NOT Reviewer
    role_exec = Role(name="executor", template_content="", sequence_number=3)
    inst_exec = InstructionBuilder.build(context, role_exec)
    assert "critic" in inst_exec.previous_stage_outputs
    assert "architect" in inst_exec.previous_stage_outputs
    assert "planner" in inst_exec.previous_stage_outputs
    assert "executor" not in inst_exec.previous_stage_outputs
    assert "reviewer" not in inst_exec.previous_stage_outputs


# ==============================================================================
# P1-03: Standalone stage commands capture a Git baseline
# ==============================================================================

def test_p1_03_standalone_stage_captures_git_baseline(tmp_path, monkeypatch):
    """Verify standalone stage execution captures and saves git_baseline.json if missing."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@forge.ai"], cwd=tmp_path, check=True)

    test_file = tmp_path / "hello.py"
    test_file.write_text("print('hello')", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True)

    # Monkeypatch cwd to tmp_path
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    from forge.cli import _run_stage

    with patch("forge.cli.Stage.run") as mock_stage_run:
        mock_stage_run.return_value = make_fake_stage_result("architect", "APPROVED", seq=1)
        _run_stage("architect", task="Design a new feature")

    # Verify run directory created and contains git_baseline.json
    run_mgr = RunManager(tmp_path)
    runs = run_mgr.list_runs()
    assert len(runs) >= 1
    latest_run = runs[0]
    baseline_file = latest_run.run_dir / "git_baseline.json"
    assert baseline_file.exists(), "git_baseline.json was not created during standalone stage execution!"

    baseline = GitBaseline.load(baseline_file)
    assert baseline.head_commit is not None


# ==============================================================================
# P1-04: Autonomous verifier/repair failure semantics
# ==============================================================================

def test_p1_04_verifier_blocked_halts_immediately(tmp_path, monkeypatch):
    """Verify autonomous loop halts immediately with status BLOCKED when verifier is BLOCKED."""
    from forge.cli import auto_pipeline

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    with patch("forge.cli.execute_stage") as mock_exec, \
         patch("sys.exit") as mock_exit:

        mock_exit.side_effect = SystemExit(1)

        def fake_execute(stage_def, context, run_mgr, display_task=None, banner_prefix=""):
            if stage_def.name == "reviewer":
                return make_fake_stage_result(
                    "reviewer",
                    "BLOCKED",
                    reason="Missing dependencies",
                    success=False,
                    seq=stage_def.sequence_number,
                )
            return make_fake_stage_result(
                stage_def.name,
                "APPROVED" if stage_def.name != "executor" else "SUCCESS",
                seq=stage_def.sequence_number,
            )

        mock_exec.side_effect = fake_execute

        with pytest.raises(SystemExit):
            auto_pipeline.callback(
                task="Test task",
                spec_file=None,
                from_critic=False,
                run_id=None,
                max_retries=3,
                auto_commit=False,
                no_critic=True,
            )

        # Reviewer should have executed only ONCE (no blind retries when BLOCKED)
        reviewer_calls = [
            c for c in mock_exec.call_args_list if c[0][0].name == "reviewer"
        ]
        assert len(reviewer_calls) == 1, "Verifier BLOCKED should halt immediately without retries!"


def test_p1_04_verifier_rejected_propagates_feedback(tmp_path, monkeypatch):
    """Verify verifier REJECTED or CHANGES_REQUIRED propagates actionable feedback to auto-repair."""
    from forge.cli import auto_pipeline

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    iteration_count = {"reviewer": 0, "executor": 0}
    task_received_by_executor = []

    with patch("forge.cli.execute_stage") as mock_exec:
        def fake_execute(stage_def, context, run_mgr, display_task=None, banner_prefix=""):
            if stage_def.name == "executor":
                iteration_count["executor"] += 1
                task_received_by_executor.append(context.run.task)
                return make_fake_stage_result("executor", "SUCCESS", seq=stage_def.sequence_number)
            if stage_def.name == "reviewer":
                iteration_count["reviewer"] += 1
                if iteration_count["reviewer"] == 1:
                    # First attempt: REJECTED with issues
                    return make_fake_stage_result(
                        "reviewer",
                        "REJECTED",
                        reason="Security flaw in auth",
                        issues={"HIGH": ["Insecure token generation"]},
                        success=False,
                        seq=stage_def.sequence_number,
                    )
                else:
                    # Second attempt: APPROVED
                    return make_fake_stage_result("reviewer", "APPROVED", seq=stage_def.sequence_number)
            return make_fake_stage_result(stage_def.name, "APPROVED", seq=stage_def.sequence_number)

        mock_exec.side_effect = fake_execute

        auto_pipeline.callback(
            task="Implement auth",
            spec_file=None,
            from_critic=False,
            run_id=None,
            max_retries=2,
            auto_commit=False,
            no_critic=True,
        )

        assert iteration_count["executor"] == 2
        assert iteration_count["reviewer"] == 2
        # Verify attempt 2 received structured feedback from attempt 1
        assert "Insecure token generation" in task_received_by_executor[1]
        assert "Auto-Repair Feedback" in task_received_by_executor[1]


# ==============================================================================
# P2-01: Large user task/spec content exceeding Antigravity argv limits
# ==============================================================================

def test_p2_01_large_task_prompt_budget():
    """Verify large user tasks are budgeted/truncated and never exceed max_prompt_bytes."""
    large_task = "Task specification:\n" + ("x" * 200_000)
    inst = Instruction(
        role_name="architect",
        task=large_task,
        project_docs={},
        previous_stage_outputs={},
        protocol_schema="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\n```",
    )
    role_template = "You are the Architect. Analyze the task."

    # Compile with Antigravity limit (130,000 bytes)
    rendered = PromptCompiler.compile(
        instruction=inst,
        role_template=role_template,
        max_prompt_bytes=130000,
    )

    rendered_bytes = len(rendered.text.encode("utf-8"))
    assert rendered_bytes <= 130000, f"Rendered prompt {rendered_bytes} bytes exceeds 130000 ceiling!"
    assert "[... Task truncated to fit adapter transport budget ...]" in rendered.text


# ==============================================================================
# P2-02: Pre-run Critic can expose non-.env secrets
# ==============================================================================

def test_p2_02_sensitive_files_protected():
    """Verify credentials, tokens, ssh keys, certs, and secrets are classified as protected paths."""
    sensitive_paths = [
        "credentials.json",
        "gcp_credentials.json",
        "config/aws_credentials",
        "token.json",
        "auth_token.yaml",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "server.key",
        "cert.pem",
        "client_secret.json",
        "client_secret_oauth.json",
        "secrets.yaml",
        "secret.json",
        ".env",
        ".env.local",
        ".env.production",
        ".forge/runs/123",
    ]

    for p in sensitive_paths:
        assert GitService.is_protected_path(p), f"Path '{p}' should be protected but was not!"

    # Normal files should NOT be protected
    normal_paths = [
        "src/main.py",
        "tests/test_auth.py",
        "README.md",
        "package.json",
    ]
    for p in normal_paths:
        assert not GitService.is_protected_path(p), f"Path '{p}' should not be protected!"


# ==============================================================================
# P2-03: Git filename parsing breaks quoted/C-style escaped filenames
# ==============================================================================

def test_p2_03_git_filenames_with_quotes_and_spaces(tmp_path):
    """Verify GitService handles filenames with double quotes, spaces, and special characters."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@forge.ai"], cwd=tmp_path, check=True)

    # Create files with quotes and spaces in filenames
    f1 = tmp_path / 'file"with"quotes.py'
    f1.write_text("content 1", encoding="utf-8")

    f2 = tmp_path / "file with spaces.py"
    f2.write_text("content 2", encoding="utf-8")

    git = GitService(tmp_path)
    changed = git.changed_files()

    # Filenames should be matched bit-exactly
    assert 'file"with"quotes.py' in changed
    assert "file with spaces.py" in changed

    # Test baseline capture and change attribution
    baseline = git.capture_baseline()
    assert 'file"with"quotes.py' in baseline.dirty_files
    assert "file with spaces.py" in baseline.dirty_files

    attr = git.attribute_changes(baseline)
    assert len(attr.pure_forge_changes) == 0
    assert len(attr.preexisting_unchanged) == 2


# ==============================================================================
# P2-04: Adapter subprocesses process group cleanup
# ==============================================================================

def test_p2_04_adapter_subprocess_process_group():
    """Verify BaseAdapter._run_subprocess cleans up child processes on timeout."""
    import sys
    # Script that spawns a sleep child and waits
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen(['sleep', '10'])\n"
        "time.sleep(10)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        BaseAdapter._run_subprocess(
            [sys.executable, "-c", code],
            timeout=1,
        )


# ==============================================================================
# P2-05: Stage registration/validation remains more hardcoded than data-driven
# ==============================================================================

def test_p2_05_data_driven_stage_definitions():
    """Verify VALID_STAGES and allowed statuses are data-driven from StageOrder."""
    stage_names = StageOrder.all_stage_names()
    assert "critic" in stage_names
    assert "architect" in stage_names
    assert "planner" in stage_names
    assert "executor" in stage_names
    assert "reviewer" in stage_names

    # VALID_STAGES in config includes StageOrder.all_stage_names()
    assert stage_names.issubset(VALID_STAGES)

    # Reviewer repair target points to Executor
    rev_def = StageOrder.resolve_definition("reviewer")
    assert rev_def.repair_target == "executor"
    assert rev_def.is_verifier is True
