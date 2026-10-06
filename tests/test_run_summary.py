"""Comprehensive test suite for Forge post-run stage summary and dashboard lifecycle."""

import io
import json
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner
import pytest
from rich.console import Console

from forge.cli import main
from forge.core.summary import RunSummary
from forge.adapters.base import AdapterResponse
from forge.dashboard.app import DashboardApp
from forge.storage.run_manager import RunManager
from tests.conftest import configure_automated_execution_environment


def test_normal_multi_stage_completion(tmp_path: Path):
    """Test 1: Normal multi-stage completion marks all executed stages as COMPLETED with success icons."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=1.5, raw_output="APPROVED",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=1.2, raw_output="READY",
        )
        exec_resp = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: COMPLETE\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=3.0, raw_output="COMPLETE",
        )
        test_resp = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="PASS",
        )
        rev_resp = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=2.0, raw_output="APPROVED",
        )
        crit_resp = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=1.8, raw_output="APPROVED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, crit_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "Build full feature"])
            assert res.exit_code == 0

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run is not None
            assert run.status == "APPROVED"

            summary = run.summary
            assert summary is not None
            assert summary.final_status == "APPROVED"
            assert summary.reason is None

            # Verify every stage
            arch_item = summary.get_stage("Architect")
            assert arch_item is not None
            assert arch_item.status == "APPROVED"
            assert arch_item.execution_state == "COMPLETED"
            assert arch_item.icon == "✓"

            plan_item = summary.get_stage("Planner")
            assert plan_item is not None
            assert plan_item.status == "READY"
            assert plan_item.execution_state == "COMPLETED"
            assert plan_item.icon == "✓"

            exec_item = summary.get_stage("Executor")
            assert exec_item is not None
            assert exec_item.status == "COMPLETE"
            assert exec_item.execution_state == "COMPLETED"
            assert exec_item.icon == "✓"

            test_item = summary.get_stage("Tester")
            assert test_item is not None
            assert test_item.status == "PASS"
            assert test_item.execution_state == "COMPLETED"
            assert test_item.icon == "✓"

            rev_item = summary.get_stage("Reviewer")
            assert rev_item is not None
            assert rev_item.status == "APPROVED"
            assert rev_item.execution_state == "COMPLETED"
            assert rev_item.icon == "✓"

            crit_item = summary.get_stage("Critic")
            assert crit_item is not None
            assert crit_item.status == "APPROVED"
            assert crit_item.execution_state == "COMPLETED"
            assert crit_item.icon == "✓"

            text = summary.format_text()
            assert f"Run: {run.run_id}" in text
            assert "✓ Architect   APPROVED          COMPLETED" in text
            assert "✓ Planner     READY             COMPLETED" in text
            assert "✓ Executor    COMPLETE          COMPLETED" in text
            assert "✓ Tester      PASS              COMPLETED" in text
            assert "✓ Reviewer    APPROVED          COMPLETED" in text
            assert "✓ Critic      APPROVED          COMPLETED" in text
            assert "Final status: APPROVED" in text


def test_halt_at_intermediate_stage(tmp_path: Path):
    """Test 2: Halt at an intermediate stage preserves statuses, marks halted stage COMPLETED (✗), and remaining as NOT REACHED (○)."""
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
        failure_reason = "T0 fix verified in a rebuilt bare venv; undisclosed harness-shaped main.py escapes the gate."
        rev_resp = AdapterResponse(
            stdout=f"```yaml\nROLE: REVIEWER\nSTATUS: CHANGES_REQUIRED\nHANDOFF: EXECUTOR\nREASON: {failure_reason}\n```",
            stderr="", exit_code=0, duration_seconds=1.5, raw_output="CHANGES_REQUIRED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            # auto with max-retries 1 so it halts on first CHANGES_REQUIRED
            res = runner.invoke(main, ["auto", "Build feature", "--max-retries", "1"])
            assert res.exit_code == 1

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "CHANGES_REQUIRED"

            summary = run.summary
            assert summary is not None
            assert summary.final_status == "CHANGES_REQUIRED"
            assert failure_reason in summary.reason

            # Architect: APPROVED, COMPLETED, ✓
            arch = summary.get_stage("Architect")
            assert arch.status == "APPROVED"
            assert arch.execution_state == "COMPLETED"
            assert arch.icon == "✓"

            # Planner: READY, COMPLETED, ✓
            plan = summary.get_stage("Planner")
            assert plan.status == "READY"
            assert plan.execution_state == "COMPLETED"
            assert plan.icon == "✓"

            # Executor: COMPLETE, COMPLETED, ✓
            exe = summary.get_stage("Executor")
            assert exe.status == "COMPLETE"
            assert exe.execution_state == "COMPLETED"
            assert exe.icon == "✓"

            # Tester: PASS, COMPLETED, ✓
            tst = summary.get_stage("Tester")
            assert tst.status == "PASS"
            assert tst.execution_state == "COMPLETED"
            assert tst.icon == "✓"

            # Reviewer: CHANGES_REQUIRED, COMPLETED, ✗
            rev = summary.get_stage("Reviewer")
            assert rev.status == "CHANGES_REQUIRED"
            assert rev.execution_state == "COMPLETED"
            assert rev.icon == "✗"

            # Critic: NOT REACHED, —, ○
            crit = summary.get_stage("Critic")
            assert crit.status == "—"
            assert crit.execution_state == "NOT REACHED"
            assert crit.icon == "○"

            formatted = summary.format_text()
            assert "✓ Architect   APPROVED          COMPLETED" in formatted
            assert "✓ Planner     READY             COMPLETED" in formatted
            assert "✓ Executor    COMPLETE          COMPLETED" in formatted
            assert "✓ Tester      PASS              COMPLETED" in formatted
            assert "✗ Reviewer    CHANGES_REQUIRED  COMPLETED" in formatted
            assert "○ Critic      —                 NOT REACHED" in formatted
            assert "Final status: CHANGES_REQUIRED" in formatted
            assert f"Reason: {failure_reason}" in formatted


def test_skipped_architect_planner_followed_by_executed_stages(tmp_path: Path):
    """Test 3: Resumed run skipping Architect and Planner marks them SKIPPED and executed stages as COMPLETED."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        rm = RunManager(Path.cwd())
        run = rm.create_run("Skip preloop feature")

        # Save pre-existing completed artifacts for Architect and Planner
        rm.save_stage_artifacts(
            run=run, sequence_number=1, role_name="architect",
            markdown_content="# Arch Spec", json_data={"status": "APPROVED"},
        )
        rm.save_stage_artifacts(
            run=run, sequence_number=2, role_name="planner",
            markdown_content="# Plan Spec", json_data={"status": "READY"},
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
            stderr="", exit_code=0, duration_seconds=1.5, raw_output="APPROVED",
        )
        crit_resp = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[test_resp, rev_resp, crit_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "--run", run.run_id])
            assert res.exit_code == 0

            run = rm.resume(run.run_id)
            summary = run.summary
            assert summary is not None

            arch = summary.get_stage("Architect")
            assert arch.status == "APPROVED"
            assert arch.execution_state == "SKIPPED"
            assert arch.icon == "✓"

            plan = summary.get_stage("Planner")
            assert plan.status == "READY"
            assert plan.execution_state == "SKIPPED"
            assert plan.icon == "✓"

            exe = summary.get_stage("Executor")
            assert exe.status == "COMPLETE"
            assert exe.execution_state == "COMPLETED"
            assert exe.icon == "✓"

            tst = summary.get_stage("Tester")
            assert tst.status == "PASS"
            assert tst.execution_state == "COMPLETED"
            assert tst.icon == "✓"

            rev = summary.get_stage("Reviewer")
            assert rev.status == "APPROVED"
            assert rev.execution_state == "COMPLETED"
            assert rev.icon == "✓"

            crit = summary.get_stage("Critic")
            assert crit.status == "APPROVED"
            assert crit.execution_state == "COMPLETED"
            assert crit.icon == "✓"

            text = summary.format_text()
            assert "✓ Architect   APPROVED          SKIPPED" in text
            assert "✓ Planner     READY             SKIPPED" in text
            assert "✓ Executor    COMPLETE          COMPLETED" in text
            assert "✓ Tester      PASS              COMPLETED" in text
            assert "✓ Reviewer    APPROVED          COMPLETED" in text
            assert "✓ Critic      APPROVED          COMPLETED" in text


def test_not_reached_stages(tmp_path: Path):
    """Test 4: Autonomous loop halting early leaves all unreached stages as NOT REACHED (○ and —)."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_fail = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: REJECTED\nHANDOFF: NONE\nREASON: Architecture unsound and infeasible\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="REJECTED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=arch_fail):

            res = runner.invoke(main, ["auto", "Invalid task"])
            assert res.exit_code == 1

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "REJECTED"

            summary = run.summary
            assert summary is not None
            assert summary.final_status == "REJECTED"
            assert "Architecture unsound" in (summary.reason or "")

            arch = summary.get_stage("Architect")
            assert arch.status == "REJECTED"
            assert arch.execution_state == "COMPLETED"
            assert arch.icon == "✗"

            # All remaining stages must be NOT REACHED
            for stage_name in ["Planner", "Executor", "Tester", "Reviewer", "Critic"]:
                item = summary.get_stage(stage_name)
                assert item is not None, f"Stage {stage_name} missing from summary"
                assert item.status == "—"
                assert item.execution_state == "NOT REACHED"
                assert item.icon == "○"

            formatted = summary.format_text()
            assert "✗ Architect   REJECTED          COMPLETED" in formatted
            assert "○ Planner     —                 NOT REACHED" in formatted
            assert "○ Executor    —                 NOT REACHED" in formatted
            assert "○ Tester      —                 NOT REACHED" in formatted
            assert "○ Reviewer    —                 NOT REACHED" in formatted
            assert "○ Critic      —                 NOT REACHED" in formatted


def test_resumed_runs(tmp_path: Path):
    """Test 5: Resumed runs correctly retain previously completed/skipped stages while newly executed stages update the same run history."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        # Run 1: Halts at Reviewer with CHANGES_REQUIRED
        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="READY",
        )
        exec_resp1 = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: COMPLETE\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=2.0, raw_output="COMPLETE",
        )
        test_resp1 = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="PASS",
        )
        rev_fail = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: CHANGES_REQUIRED\nHANDOFF: EXECUTOR\nREASON: Missing unit tests\n```",
            stderr="", exit_code=0, duration_seconds=1.5, raw_output="CHANGES_REQUIRED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp1, rev_fail]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp1):

            res1 = runner.invoke(main, ["auto", "Two phase task", "--max-retries", "1"])
            assert res1.exit_code == 1

        rm = RunManager(Path.cwd())
        run1 = rm.latest()
        run_id = run1.run_id
        assert run1.status == "CHANGES_REQUIRED"
        assert run1.summary.get_stage("Reviewer").status == "CHANGES_REQUIRED"
        assert run1.summary.get_stage("Critic").execution_state == "NOT REACHED"

        # Run 2: Resume run-id. Skips Architect and Planner, re-executes Executor and Reviewer to approval, then runs Critic.
        exec_resp2 = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: COMPLETE\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=2.5, raw_output="COMPLETE",
        )
        test_resp2 = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=1.0, raw_output="PASS",
        )
        rev_pass = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=1.2, raw_output="APPROVED",
        )
        crit_pass = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=1.1, raw_output="APPROVED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[test_resp2, rev_pass, crit_pass]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp2):

            res2 = runner.invoke(main, ["auto", "--run", run_id])
            assert res2.exit_code == 0

        # Load run and verify the updated run history
        resumed_run = rm.resume(run_id)
        assert resumed_run.status == "APPROVED"
        summary2 = resumed_run.summary
        assert summary2 is not None
        assert summary2.final_status == "APPROVED"

        # Architect and Planner must be SKIPPED
        assert summary2.get_stage("Architect").execution_state == "SKIPPED"
        assert summary2.get_stage("Architect").status == "APPROVED"
        assert summary2.get_stage("Planner").execution_state == "SKIPPED"
        assert summary2.get_stage("Planner").status == "READY"

        # Executor, Tester, and Reviewer must be updated to COMPLETED with success
        assert summary2.get_stage("Executor").execution_state == "COMPLETED"
        assert summary2.get_stage("Executor").status == "COMPLETE"
        assert summary2.get_stage("Tester").execution_state == "COMPLETED"
        assert summary2.get_stage("Tester").status == "PASS"
        assert summary2.get_stage("Reviewer").execution_state == "COMPLETED"
        assert summary2.get_stage("Reviewer").status == "APPROVED"

        # Critic now executed and COMPLETED
        assert summary2.get_stage("Critic").execution_state == "COMPLETED"
        assert summary2.get_stage("Critic").status == "APPROVED"


def test_terminal_summary_matching_dashboard_summary(tmp_path: Path):
    """Test 6: Terminal summary matches dashboard summary using identical source of truth and formatting."""
    run_dir = tmp_path / ".forge" / "runs" / "run-003"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Construct canonical RunSummary matching the user example exactly
    example_summary = RunSummary(
        run_id="run-003",
        final_status="CHANGES_REQUIRED",
        reason="T0 fix verified in a rebuilt bare venv; undisclosed harness-shaped main.py escapes the gate.",
    )
    example_summary.record_stage("Architect", "APPROVED", "SKIPPED", role_name="architect", sequence_number=1)
    example_summary.record_stage("Planner", "READY", "SKIPPED", role_name="planner", sequence_number=2)
    example_summary.record_stage("Executor", "COMPLETE", "COMPLETED", duration_seconds=4.2, role_name="executor", sequence_number=3)
    example_summary.record_stage("Reviewer", "CHANGES_REQUIRED", "COMPLETED", duration_seconds=2.1, role_name="reviewer", sequence_number=4)
    example_summary.record_stage("Critic", "—", "NOT REACHED", role_name="critic", sequence_number=5)
    example_summary.record_stage("Commit", "—", "NOT REACHED", role_name="commit", sequence_number=99)

    meta_file = run_dir / "metadata.json"
    meta_data = {
        "run_id": "run-003",
        "task": "Fix security vulnerability",
        "status": "CHANGES_REQUIRED",
        "metadata": {
            "summary": example_summary.to_dict(),
        },
    }
    meta_file.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")

    # 1. Initialize DashboardApp with wide console
    test_console = Console(width=120, height=40)
    app = DashboardApp(run_dir, console=test_console, project_root=tmp_path)

    # 2. Source of truth check
    summary_text = app.get_summary_text()
    expected_text = example_summary.format_text()
    assert summary_text == expected_text

    # 3. Verify format matches exact expected specification
    lines = [line.rstrip() for line in summary_text.splitlines()]
    assert "Forge Run Summary" in lines
    assert "Run: run-003" in lines
    assert any("✓ Architect   APPROVED          SKIPPED" in l for l in lines)
    assert any("✓ Planner     READY             SKIPPED" in l for l in lines)
    assert any("✓ Executor    COMPLETE          COMPLETED" in l for l in lines)
    assert any("✗ Reviewer    CHANGES_REQUIRED  COMPLETED" in l for l in lines)
    assert any("○ Critic      —                 NOT REACHED" in l for l in lines)
    assert any("○ Commit      —                 NOT REACHED" in l for l in lines)
    assert "Final status: CHANGES_REQUIRED" in lines
    assert "Reason: T0 fix verified in a rebuilt bare venv; undisclosed harness-shaped main.py escapes the gate." in lines

    # 4. Verify dashboard screen rendering contains the same summary
    rendered_screen = app.render_once()
    assert "Forge Run Summary" in rendered_screen
    assert "run-003" in rendered_screen
    assert "Architect" in rendered_screen
    assert "Planner" in rendered_screen
    assert "Executor" in rendered_screen
    assert "Reviewer" in rendered_screen
    assert "CHANGES_REQUIRED" in rendered_screen
    assert "T0 fix verified in a rebuilt bare venv" in rendered_screen

    # 5. Verify app.run() outputs the identical summary on exit
    output_capture = io.StringIO()
    run_console = Console(file=output_capture, width=120, height=40)
    app.console = run_console
    with patch("sys.stdin.isatty", return_value=False):
        exit_code = app.run()
        assert exit_code == 0
        cli_output = output_capture.getvalue()
        assert expected_text in cli_output


def test_interactive_dashboard_exit_emits_summary(tmp_path: Path):
    """Verify interactive dashboard exits on 'q' and outputs concise Forge Run Summary to target stdout."""
    run_dir = tmp_path / ".forge" / "runs" / "run-005"
    run_dir.mkdir(parents=True, exist_ok=True)

    summary = RunSummary(run_id="run-005", final_status="APPROVED")
    summary.record_stage("Architect", "APPROVED", "COMPLETED", duration_seconds=1.0)
    summary.record_stage("Planner", "READY", "COMPLETED", duration_seconds=1.0)
    summary.record_stage("Executor", "COMPLETE", "COMPLETED", duration_seconds=2.0)
    summary.record_stage("Reviewer", "APPROVED", "COMPLETED", duration_seconds=1.5)

    meta_file = run_dir / "metadata.json"
    meta_file.write_text(json.dumps({
        "run_id": "run-005",
        "task": "Test task",
        "status": "APPROVED",
        "metadata": {"summary": summary.to_dict()},
    }), encoding="utf-8")

    app = DashboardApp(run_dir, project_root=tmp_path)
    expected_text = summary.format_text()

    fake_out = io.StringIO()
    with patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0), \
         patch("termios.tcgetattr", return_value=[]), \
         patch("termios.tcsetattr"), \
         patch("tty.setcbreak"), \
         patch("sys.__stdout__", fake_out), \
         patch("select.select", side_effect=[([0], [], []), ([], [], [])]), \
         patch.object(app, "_read_key", side_effect=["q"]):
        exit_code = app.run()
        assert exit_code == 0
        output = fake_out.getvalue()
        assert expected_text in output


def test_standard_pipeline_run_summary(tmp_path: Path):
    """Verify run_pipeline (forge run) initializes planned stages and updates summary on completion."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: COMPLETE\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=2.0, raw_output="COMPLETE")
        test_resp = AdapterResponse(stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="PASS")
        rev_resp = AdapterResponse(stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED")
        crit_resp = AdapterResponse(stdout="```yaml\nROLE: CRITIC\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED")

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, crit_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["run", "Step by step task"], input="y\ny\ny\ny\ny\n")
            assert res.exit_code == 0

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "APPROVED"
            assert run.summary is not None
            assert run.summary.final_status == "APPROVED"
            assert run.summary.get_stage("Architect").execution_state == "COMPLETED"
            assert run.summary.get_stage("Reviewer").execution_state == "COMPLETED"
            assert run.summary.get_stage("Critic").execution_state == "COMPLETED"

