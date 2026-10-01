"""Comprehensive tests for the Tester role (ADR-002).

Covers:
- Stage definition, sequence, capabilities, role properties
- Prompt compilation (_compile_tester, Reviewer behavioral evidence injection)
- Protocol report validation with score-free structured issues
- CLI standalone command 'forge test' and prerequisite validation
- Autonomous loop dual-verifier execution, repair loop on FAIL, BLOCKED halting,
  NOT_TESTABLE bypass, and attempt artifact preservation.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from click.testing import CliRunner

from forge.cli import main, execute_stage
from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.core.run import Run
from forge.adapters.base import AdapterResponse
from forge.stages.definition import StageDefinition, StageOrder
from forge.stages.requirements import StageRequirementsRegistry, Capability
from forge.storage.run_manager import RunManager
from forge.prompts.compiler import PromptCompiler
from forge.prompts.builder import InstructionBuilder
from forge.protocol.validator import MachineReportValidator
from tests.conftest import configure_automated_execution_environment, make_automated_config


# ==============================================================================
# 1. Stage Definition & Capabilities
# ==============================================================================

def test_tester_stage_definition():
    """Verify Tester StageDefinition metadata conforms strictly to ADR-002."""
    tester = StageOrder.get_tester()
    assert tester.name == "tester"
    assert tester.sequence_number == 4
    assert tester.phase == "pre_run"
    assert tester.display_title == "Tester"
    assert tester.role_type == "verifier"
    assert tester.is_verifier is True
    assert tester.is_producer is False
    assert tester.repair_target == "executor"
    assert tester.artifact_prefix == "04_tester"

    # Statuses
    assert "PASS" in tester.success_statuses
    assert "NOT_TESTABLE" in tester.success_statuses
    assert "FAIL" in tester.allowed_statuses
    assert "BLOCKED" in tester.allowed_statuses


def test_tester_required_capabilities():
    """Verify Tester requires CODE_READ and SHELL, but no browser or code edit capabilities."""
    reqs = StageRequirementsRegistry.get("tester")
    assert Capability.CODE_READ in reqs
    assert Capability.SHELL in reqs
    assert Capability.CODE_EDIT not in reqs
    assert Capability.PLAYWRIGHT not in reqs
    assert Capability.SCREENSHOTS not in reqs


def test_stage_order_sequence_with_tester():
    """Verify StageOrder positions Tester at sequence 4 between Executor (3) and Reviewer (5)."""
    stages = StageOrder.standard_pipeline_stages(no_critic=False)
    names = [s.name for s in stages]
    assert names == ["architect", "planner", "executor", "tester", "reviewer", "critic"]
    assert stages[3].name == "tester"
    assert stages[3].sequence_number == 4
    assert stages[4].name == "reviewer"
    assert stages[4].sequence_number == 5


# ==============================================================================
# 2. Prompt Compilation & Downstream Behavioral Evidence
# ==============================================================================

def test_compile_tester_prompt(tmp_path):
    """Verify PromptCompiler._compile_tester includes all required sections and guidelines."""
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Build User Authentication")

    rm.save_stage_artifacts(
        run=run, sequence_number=1, role_name="architect",
        markdown_content="# Architect Spec\nRoutes: /login, /register", json_data={"status": "APPROVED"},
    )
    rm.save_stage_artifacts(
        run=run, sequence_number=2, role_name="planner",
        markdown_content="# Task Plan\n1. Add auth views", json_data={"status": "READY"},
    )
    rm.save_stage_artifacts(
        run=run, sequence_number=3, role_name="executor",
        markdown_content="# Executor Report\nImplemented views and tests.", json_data={"status": "SUCCESS"},
    )

    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))
    tester_role = Role.load("tester", project_root=tmp_path)
    instruction = InstructionBuilder.build(context, tester_role)

    rendered = PromptCompiler.compile(instruction, tester_role)
    prompt = rendered.text

    assert "## User Requirements & Acceptance Criteria" in prompt
    assert "Build User Authentication" in prompt
    assert "## Architect Specification" in prompt
    assert "## Planner Plan" in prompt
    assert "## Testing Instructions" in prompt
    assert "Critique internal architecture" in prompt
    assert "Critique code style" in prompt
    assert "Do NOT emit numeric scores or critique code style/architecture" in prompt
    assert "PASS" in prompt


def test_reviewer_receives_complete_tester_report_across_statuses(tmp_path):
    """Verify Reviewer receives complete Tester report (human and machine) for PASS, FAIL, BLOCKED, and NOT_TESTABLE."""
    rm = RunManager(tmp_path)
    git = GitService(tmp_path)
    rev_role = Role.load("reviewer", project_root=tmp_path)

    for status in ["PASS", "FAIL", "BLOCKED", "NOT_TESTABLE"]:
        run = rm.create_run(f"Test run for {status}")
        rm.save_stage_artifacts(
            run=run, sequence_number=3, role_name="executor",
            markdown_content="# Executor Impl", json_data={"status": "SUCCESS"},
        )
        tester_md = f"# Tester Evaluation\nVerdict: {status}\nAll journeys tested thoroughly."
        tester_json = {"ROLE": "TESTER", "STATUS": status, "HANDOFF": "REVIEWER"}
        rm.save_stage_artifacts(
            run=run, sequence_number=4, role_name="tester",
            markdown_content=tester_md, json_data=tester_json,
        )

        context = Context(run=run, project_root=tmp_path, config=Config.default(), git=git)
        instruction = InstructionBuilder.build(context, rev_role)

        assert instruction.tester_report is not None
        assert "Verdict: " + status in instruction.tester_report
        assert json.loads(instruction.tester_machine_report) == tester_json

        rendered = PromptCompiler.compile(instruction, rev_role)
        prompt = rendered.text
        assert "## Tester Report & Behavioral Evidence" in prompt
        assert f"Verdict: {status}" in prompt
        assert "### Tester Machine Protocol (JSON)" in prompt
        assert f'"STATUS": "{status}"' in prompt


# ==============================================================================
# 3. Protocol Validation with Score-Free Structured Findings
# ==============================================================================

def test_machine_report_validator_tester_pass():
    """Verify MachineReportValidator validates valid TESTER PASS report."""
    data = {
        "ROLE": "TESTER",
        "STATUS": "PASS",
        "HANDOFF": "REVIEWER",
        "SUMMARY": "All critical workflows passed.",
    }
    report = MachineReportValidator.validate(data, expected_role="TESTER")
    assert report.is_valid is True
    assert report.status == "PASS"
    assert report.handoff == "REVIEWER"


def test_machine_report_validator_tester_structured_issues():
    """Verify MachineReportValidator accepts and preserves score-free structured issue dicts."""
    data = {
        "ROLE": "TESTER",
        "STATUS": "FAIL",
        "HANDOFF": "EXECUTOR",
        "ISSUES": {
            "CRITICAL": [
                {
                    "ID": "BUG-001",
                    "DESCRIPTION": "Login button unresponsive on click",
                    "STEPS_TO_REPRODUCE": "1. Go to /login\n2. Click Submit",
                    "EXPECTED": "Redirect to dashboard",
                    "ACTUAL": "Console error: submitAuth is not defined",
                    "EVIDENCE": "TypeError: submitAuth is not defined at login.js:42",
                }
            ],
            "MINOR": [
                {
                    "ID": "UI-002",
                    "DESCRIPTION": "Missing contrast on secondary text",
                    "STEPS_TO_REPRODUCE": "Inspect footer text",
                    "EXPECTED": "Contrast ratio >= 4.5:1",
                    "ACTUAL": "Contrast ratio 2.1:1",
                    "EVIDENCE": "color: #999 on background #aaa",
                }
            ],
        },
    }
    report = MachineReportValidator.validate(data, expected_role="TESTER")
    assert report.is_valid is True
    assert report.status == "FAIL"
    assert "CRITICAL" in report.issues
    crit_issue = report.issues["CRITICAL"][0]
    assert isinstance(crit_issue, dict)
    assert crit_issue["ID"] == "BUG-001"
    assert crit_issue["DESCRIPTION"] == "Login button unresponsive on click"
    assert crit_issue["STEPS_TO_REPRODUCE"] == "1. Go to /login\n2. Click Submit"


def test_machine_report_validator_rejects_tester_invalid_status():
    """Verify MachineReportValidator rejects unrecognized status for TESTER."""
    data = {
        "ROLE": "TESTER",
        "STATUS": "MAYBE_PASSED",
        "HANDOFF": "REVIEWER",
    }
    report = MachineReportValidator.validate(data, expected_role="TESTER")
    assert report.is_valid is False
    assert any("MAYBE_PASSED" in e for e in report.validation_errors)


# ==============================================================================
# 4. CLI Standalone Command 'forge test'
# ==============================================================================

def test_cli_test_command_requires_prerequisite(tmp_path):
    """Verify 'forge test' errors out when Executor output is absent."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        res1 = runner.invoke(main, ["test"])
        assert res1.exit_code == 1
        assert "Run 'forge architect', 'forge planner', and 'forge execute' first" in res1.output

        rm = RunManager(Path.cwd())
        run = rm.create_run("Task without executor")
        res2 = runner.invoke(main, ["test", "--run", run.run_id])
        assert res2.exit_code == 1
        assert "Tester requires prior Executor output" in res2.output


def test_cli_test_command_executes_successfully(tmp_path):
    """Verify 'forge test' executes Tester and writes 04_tester artifacts."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        rm = RunManager(Path.cwd())
        run = rm.create_run("Implement feature")
        rm.save_stage_artifacts(
            run=run, sequence_number=3, role_name="executor",
            markdown_content="# Executor Done", json_data={"ROLE": "EXECUTOR", "STATUS": "SUCCESS"},
        )

        test_resp = AdapterResponse(
            stdout="""# Tester Evaluation Report
All behavioral tests passed cleanly.

```yaml
ROLE: TESTER
STATUS: PASS
HANDOFF: REVIEWER
```""",
            stderr="", exit_code=0, duration_seconds=0.15, raw_output="PASS",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=test_resp):

            res = runner.invoke(main, ["test", "--run", run.run_id])
            assert res.exit_code == 0
            assert "Invoking Tester (opencode)" in res.output
            assert (run.run_dir / "04_tester.md").exists()
            assert (run.run_dir / "04_tester.json").exists()

            saved_json = json.loads((run.run_dir / "04_tester.json").read_text(encoding="utf-8"))
            assert saved_json["status"] == "PASS"
            assert saved_json["role"] == "tester"


# ==============================================================================
# 5. Autonomous Loop Dual-Verifier Execution & Edge Cases
# ==============================================================================

def test_auto_pipeline_tester_fail_triggers_repair_loop_skips_reviewer(tmp_path):
    """Verify auto_pipeline: Tester FAIL triggers auto-repair to Executor, skipping Reviewer in that attempt."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")

        exec_attempt1 = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        exec_attempt2 = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")

        test_fail = AdapterResponse(
            stdout="""# Tester Report
Discovered broken submit handler.

```yaml
ROLE: TESTER
STATUS: FAIL
HANDOFF: EXECUTOR
ISSUES:
  CRITICAL:
    - ID: BUG-101
      DESCRIPTION: Button click does nothing
      STEPS_TO_REPRODUCE: Click submit button
      EXPECTED: Form submits
      ACTUAL: Nothing happens
      EVIDENCE: No event listener attached
```""",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="FAIL",
        )
        test_pass = AdapterResponse(stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS")
        rev_approve = AdapterResponse(stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        critic_resp = AdapterResponse(stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="CRITIQUE_COMPLETE")

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_fail, test_pass, rev_approve, critic_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", side_effect=[exec_attempt1, exec_attempt2]) as mock_exec:

            res = runner.invoke(main, ["auto", "Build responsive form", "--max-retries", "3"])
            assert res.exit_code == 0
            # Tester requested changes / failed on attempt 1 -> launched repair iteration 2
            assert "Tester resulted in 'FAIL'. Launching auto-repair iteration 2" in res.output
            # Both attempts ran
            assert mock_exec.call_count == 2
            # Feedback from tester was passed into Executor prompt on attempt 2
            call_2 = mock_exec.call_args_list[1]
            exec_repair_prompt = call_2.kwargs.get("prompt") or (call_2.args[0] if call_2.args else "")
            assert "Reproduction: Click submit button" in exec_repair_prompt
            assert "Button click does nothing" in exec_repair_prompt
            assert "Implementation APPROVED by Tester & Reviewer on attempt 2" in res.output

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "APPROVED"
            assert (run.run_dir / "04_tester_attempt_1.md").exists()
            assert (run.run_dir / "04_tester_attempt_2.md").exists()


def test_auto_pipeline_tester_blocked_halts_immediately(tmp_path):
    """Verify auto_pipeline halts immediately without further retries when Tester returns BLOCKED."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        test_blocked = AdapterResponse(
            stdout="""```yaml
ROLE: TESTER
STATUS: BLOCKED
HANDOFF: NONE
REASON: Database daemon not reachable on localhost:5432
```""",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="BLOCKED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_blocked]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp) as mock_exec:

            res = runner.invoke(main, ["auto", "Build feature", "--max-retries", "3"])
            assert res.exit_code == 1
            assert "Tester blocked: Database daemon not reachable" in res.output
            # Should have halted immediately on attempt 1
            assert mock_exec.call_count == 1

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "BLOCKED"


def test_auto_pipeline_tester_not_testable_bypasses_to_reviewer(tmp_path):
    """Verify auto_pipeline: Tester NOT_TESTABLE status cleanly bypasses to Reviewer and succeeds."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        test_not_testable = AdapterResponse(
            stdout="""# Tester Evaluation
Documentation update only; no runnable code or server entry point to test.

```yaml
ROLE: TESTER
STATUS: NOT_TESTABLE
HANDOFF: REVIEWER
```""",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="NOT_TESTABLE",
        )
        rev_approve = AdapterResponse(stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        critic_resp = AdapterResponse(stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="CRITIQUE_COMPLETE")

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_not_testable, rev_approve, critic_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "Update documentation"])
            assert res.exit_code == 0
            assert "Tester finished | Status: NOT_TESTABLE" in res.output
            assert "Reviewer finished | Status: APPROVED" in res.output
            assert "Implementation APPROVED by Tester & Reviewer" in res.output

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "APPROVED"
