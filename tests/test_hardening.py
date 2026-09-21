import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from click.testing import CliRunner

from forge.cli import main
from forge.core.config import Config
from forge.core.git import GitService
from forge.core.role import Role
from forge.core.run import Run
from forge.core.context import Context
from forge.adapters.base import AdapterResponse
from forge.prompts.builder import InstructionBuilder
from forge.storage.run_manager import RunManager
from tests.conftest import configure_automated_execution_environment


def test_run_1000_sorting_and_creation(tmp_path):
    """Verify that run-1000 does not cause an infinite loop and sorts chronologically."""
    mgr = RunManager(tmp_path)
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True)

    # Manually create run-999
    dir_999 = runs_dir / "run-999"
    dir_999.mkdir()
    (dir_999 / "metadata.json").write_text(
        json.dumps({"run_id": "run-999", "task": "Task 999", "created_at": "2026-09-01T00:00:00"}),
        encoding="utf-8",
    )

    # Creating next run should produce run-1000 without infinite loop
    run_1000 = mgr.create_run("Task 1000")
    assert run_1000.run_id == "run-1000"
    assert (runs_dir / "run-1000" / "metadata.json").exists()

    # Creating subsequent run should produce run-1001
    run_1001 = mgr.create_run("Task 1001")
    assert run_1001.run_id == "run-1001"

    # list_runs must sort numerically so run-1000 comes after run-999
    runs = mgr.list_runs()
    assert len(runs) == 3
    assert [r.run_id for r in runs] == ["run-999", "run-1000", "run-1001"]
    assert mgr.latest().run_id == "run-1001"


def test_run_manager_path_traversal_protection(tmp_path):
    """Verify resume and delete_run reject path traversal attempts."""
    mgr = RunManager(tmp_path)
    with pytest.raises(ValueError, match="Invalid run ID format"):
        mgr.resume("../../etc")

    with pytest.raises(ValueError, match="Invalid run ID format"):
        mgr.delete_run("../sensitive_dir")


def test_git_diff_includes_untracked_files(tmp_path):
    """Verify git.diff() includes newly created untracked files."""
    git = GitService(tmp_path)
    mock_status = "?? new_script.py\n M existing.py\n"
    mock_diff = "diff --git a/existing.py b/existing.py\n--- a/existing.py\n+++ b/existing.py\n@@ -1 +1 @@\n-old\n+new"
    mock_untracked_diff = "diff --git a//dev/null b/new_script.py\n--- /dev/null\n+++ b/new_script.py\n@@ -0,0 +1 @@\n+print('hello')"

    new_file = tmp_path / "new_script.py"
    new_file.write_text("print('hello')", encoding="utf-8")

    def mock_run(args, **kwargs):
        if args and args[0] == "diff" and "/dev/null" not in args:
            return MagicMock(returncode=0, stdout=mock_diff, stderr="")
        elif "-c" in args and "status" in args:
            return MagicMock(returncode=0, stdout=mock_status, stderr="")
        elif "diff" in args and "/dev/null" in args:
            return MagicMock(returncode=1, stdout=mock_untracked_diff, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(git, "_run", side_effect=mock_run):
        full_diff = git.diff(include_untracked=True)
        assert "diff --git a/existing.py" in full_diff
        assert "diff --git a//dev/null b/new_script.py" in full_diff


def test_git_commit_excludes_forge_and_secrets(tmp_path):
    """Verify git.commit() excludes .forge and secret files from staging."""
    git = GitService(tmp_path)
    mock_changed = [
        "src/app.py",
        ".forge/runs/run-001/01_architect.md",
        ".env",
        ".env.local",
    ]

    captured_add_args = []
    def mock_run(args, **kwargs):
        if args and args[0] == "add":
            captured_add_args.extend(args)
            return MagicMock(returncode=0, stdout="", stderr="")
        elif args and args[0] == "commit":
            return MagicMock(returncode=0, stdout="[master 12345] committed", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(git, "changed_files", return_value=mock_changed), \
         patch.object(git, "_run", side_effect=mock_run):
        assert git.commit("Add feature") is True
        assert "src/app.py" in captured_add_args
        assert ".forge/runs/run-001/01_architect.md" not in captured_add_args
        assert ".env" not in captured_add_args
        assert ".env.local" not in captured_add_args


def test_prompt_builder_budgeting_and_no_collision(tmp_path):
    """Verify stage stem collisions are resolved and huge outputs are budgeted."""
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Test budgeting", run_dir=run_dir)
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))
    role = Role(name="auditor", sequence_number=6, template_content="Auditor")

    # Create 00_critic.md and 05_critic.md to test collision resolution
    (run_dir / "00_critic.md").write_text("Pre-critic analysis", encoding="utf-8")
    (run_dir / "05_critic.md").write_text("Post-critic analysis", encoding="utf-8")

    # Create huge 03_executor.md
    huge_text = "A" * 60000
    (run_dir / "03_executor.md").write_text(huge_text, encoding="utf-8")

    instruction = InstructionBuilder.build(context, role)
    outputs = instruction.previous_stage_outputs

    # Both critic outputs should be present without overwriting
    assert "critic" in outputs or "00_critic" in outputs
    assert len(outputs) >= 3

    # Huge executor output should be truncated
    assert len(outputs["executor"]) < 50000
    assert "Truncated remaining" in outputs["executor"]


def test_forge_init_populates_full_templates(tmp_path):
    """Verify forge init installs all default role templates and creates .gitignore protection."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        res = runner.invoke(main, ["init"])
        assert res.exit_code == 0

        ai_dir = Path.cwd() / ".ai"
        assert (ai_dir / "roles" / "architect.md").exists()
        assert (ai_dir / "roles" / "planner.md").exists()
        assert (ai_dir / "roles" / "executor.md").exists()
        assert (ai_dir / "roles" / "reviewer.md").exists()
        assert (ai_dir / "roles" / "critic.md").exists()
        assert (ai_dir / "templates" / "protocol.md").exists()
        assert (ai_dir / "project" / "architecture.md").exists()
        assert (ai_dir / "project" / "conventions.md").exists()

        gitignore = Path.cwd() / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text(encoding="utf-8")
        assert ".forge/runs/" in content


def test_role_load_fallback_to_bundled_templates(tmp_path):
    """Verify Role.load falls back to bundled templates if files are missing."""
    empty_root = tmp_path / "empty_project"
    empty_root.mkdir()
    role = Role.load("architect", project_root=empty_root)
    assert role.name == "architect"
    assert "SOFTWARE ARCHITECT" in role.template_content
    assert "Common Agent Protocol" in role.protocol_content


def test_forge_runs_command(tmp_path):
    """Verify forge runs command lists runs and supports JSON export."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        # Empty runs
        res_empty = runner.invoke(main, ["runs"])
        assert res_empty.exit_code == 0
        assert "No runs found" in res_empty.output

        # Create a run
        mgr = RunManager(Path.cwd())
        mgr.create_run("Implement OAuth2")

        res_table = runner.invoke(main, ["runs"])
        assert res_table.exit_code == 0
        assert "run-001" in res_table.output
        assert "Implement OAuth2" in res_table.output

        res_json = runner.invoke(main, ["runs", "-j"])
        assert res_json.exit_code == 0
        data = json.loads(res_json.output)
        assert isinstance(data, list)
        assert data[0]["run_id"] == "run-001"


def test_pipeline_validation_bypass_and_exit_code(tmp_path):
    """Verify run_pipeline halts with exit code 1 when stage produces invalid/unknown status."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])

        # Corrupt response without protocol block -> status UNKNOWN, is_valid False
        corrupt_resp = AdapterResponse(
            stdout="I am an LLM but forgot the YAML machine report.",
            stderr="",
            exit_code=0,
            duration_seconds=0.1,
            raw_output="I am an LLM but forgot the YAML machine report.",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=corrupt_resp), \
             patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True):
            res = runner.invoke(main, ["run", "Some task"])
            assert res.exit_code == 1
            assert "Pipeline halted" in res.output


def test_auto_pipeline_retry_artifact_preservation_and_exit_code(tmp_path):
    """Verify auto_pipeline preserves attempt artifacts and exits with 1 if reviewer rejects."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=0.1,
            raw_output="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=0.1,
            raw_output="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
        )
        exec_resp = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=0.1,
            raw_output="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```",
        )
        rev_reject = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: CHANGES_REQUIRED\nHANDOFF: EXECUTOR\nISSUES:\n  MAJOR:\n    - Missing unit tests\n```",
            stderr="", exit_code=0, duration_seconds=0.1,
            raw_output="```yaml\nROLE: REVIEWER\nSTATUS: CHANGES_REQUIRED\nHANDOFF: EXECUTOR\nISSUES:\n  MAJOR:\n    - Missing unit tests\n```",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, rev_reject, rev_reject]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):
            res = runner.invoke(main, ["auto", "Build feature", "--max-retries", "2"])
            assert res.exit_code == 1
            assert "Autonomous Loop finished without approval" in res.output

            # Verify attempt artifacts exist
            run_dir = Path.cwd() / ".forge" / "runs" / "run-001"
            assert (run_dir / "03_executor_attempt_1.md").exists()
            assert (run_dir / "03_executor_attempt_2.md").exists()
            assert (run_dir / "04_reviewer_attempt_1.md").exists()
            assert (run_dir / "04_reviewer_attempt_2.md").exists()


def test_changes_required_marks_stage_failed_and_halts_cli(tmp_path):
    """Verify CHANGES_REQUIRED results in success=False and halts pipeline/review with exit code 1."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])

        rev_changes_resp = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: CHANGES_REQUIRED\nHANDOFF: EXECUTOR\nISSUES:\n  CRITICAL:\n    - Security vulnerability\n```",
            stderr="",
            exit_code=0,
            duration_seconds=0.1,
            raw_output="```yaml\nROLE: REVIEWER\nSTATUS: CHANGES_REQUIRED\nHANDOFF: EXECUTOR\n```",
        )

        # Standalone stage invocation: forge review / forge stage reviewer
        mgr = RunManager(Path.cwd())
        run = mgr.create_run("Review task")
        # Save executor prerequisite
        mgr.save_stage_artifacts(
            run=run,
            sequence_number=3,
            role_name="executor",
            markdown_content="# Impl",
            json_data={"ROLE": "EXECUTOR", "STATUS": "SUCCESS"},
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=rev_changes_resp):
            res = runner.invoke(main, ["review", "--run", run.run_id])
            assert res.exit_code == 1
            assert "Stage 'reviewer' finished with non-success status 'CHANGES_REQUIRED'" in res.output


def test_instruction_builder_excludes_attempt_files(tmp_path):
    """Verify InstructionBuilder excludes historical _attempt_ files from previous stage outputs."""
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Test attempt isolation", run_dir=run_dir)
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))
    role = Role(name="executor", sequence_number=3, template_content="Executor")

    # Canonical outputs
    (run_dir / "01_architect.md").write_text("# Architecture spec", encoding="utf-8")
    (run_dir / "02_planner.md").write_text("# Implementation plan", encoding="utf-8")

    # Historical attempt outputs from auto-repair iterations
    (run_dir / "03_executor_attempt_1.md").write_text("# Old executor attempt 1", encoding="utf-8")
    (run_dir / "04_reviewer_attempt_1.md").write_text("# Old reviewer attempt 1", encoding="utf-8")

    instruction = InstructionBuilder.build(context, role)
    outputs = instruction.previous_stage_outputs

    assert "architect" in outputs
    assert "planner" in outputs
    assert "executor_attempt_1" not in outputs
    assert "reviewer_attempt_1" not in outputs
    assert not any("_attempt_" in k for k in outputs.keys())


def test_git_diff_untracked_filters_env_and_caps_size(tmp_path):
    """Verify git.diff ignores .env untracked files and caps file count/size."""
    git = GitService(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=supersecret123\n", encoding="utf-8")
    large_file = tmp_path / "huge_data.bin"
    large_file.write_text("X" * 100, encoding="utf-8")
    safe_file = tmp_path / "main.py"
    safe_file.write_text("print('safe')\n", encoding="utf-8")

    mock_status = "?? .env\n?? huge_data.bin\n?? main.py\n"

    def mock_run(args, **kwargs):
        if args == ["diff"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        elif "-c" in args and "status" in args:
            return MagicMock(returncode=0, stdout=mock_status, stderr="")
        elif "diff" in args and "/dev/null" in args:
            if "main.py" in args:
                return MagicMock(returncode=1, stdout="diff --git a//dev/null b/main.py\n+print('safe')", stderr="")
            elif ".env" in args:
                return MagicMock(returncode=1, stdout="diff --git a//dev/null b/.env\n+SECRET_KEY", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(git, "_run", side_effect=mock_run):
        diff_out = git.diff(include_untracked=True)
        assert "SECRET_KEY" not in diff_out
        assert ".env" not in diff_out
        assert "main.py" in diff_out


def test_pipeline_run_resumption_without_critic(tmp_path):
    """Verify forge run --run resumes existing run without requiring critic report."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])

        # Create a paused or existing run
        mgr = RunManager(Path.cwd())
        run = mgr.create_run(task="Original Task to Resume")

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=0.1,
            raw_output="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=arch_resp):
            # Invoking run --run without task or critic flag and answering "n" to proceed past architect
            res = runner.invoke(main, ["run", "--run", run.run_id], input="n\n")
            assert res.exit_code == 0
            assert "Pipeline paused by user." in res.output
            assert (Path.cwd() / ".forge" / "runs" / run.run_id / "01_architect.md").exists()


def test_git_service_handles_missing_binary():
    """Verify GitService handles missing git binary without crashing."""
    git = GitService()
    with patch("subprocess.run", side_effect=FileNotFoundError("No git binary")):
        assert git.is_git_repo() is False
        assert git.status() == ""
        assert git.diff() == ""
        assert git.changed_files() == []
        assert git.current_branch() == ""
        assert git.commit("Initial") is False


def test_instruction_builder_budgets_massive_git_diff(tmp_path):
    """Verify InstructionBuilder truncates massive git diffs to prevent context overflow."""
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Test diff budgeting", run_dir=run_dir)
    git = GitService(tmp_path)
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=git)
    role = Role(name="reviewer", sequence_number=4, template_content="Reviewer")

    huge_diff = "diff --git a/big.txt b/big.txt\n" + ("+line\n" * 20000)

    with patch.object(git, "is_git_repo", return_value=True), \
         patch.object(git, "diff", return_value=huge_diff), \
         patch.object(git, "changed_files", return_value=["big.txt"]):
        instruction = InstructionBuilder.build(context, role)
        assert instruction.git_diff is not None
        assert len(instruction.git_diff) < 70000
        assert "Diff truncated" in instruction.git_diff


def test_auto_pipeline_halts_on_executor_blocked_without_calling_reviewer(tmp_path):
    """Verify auto_pipeline halts when executor reports BLOCKED and does not invoke reviewer."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=0.1,
            raw_output="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=0.1,
            raw_output="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
        )
        exec_blocked = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: BLOCKED\nHANDOFF: NONE\nREASON: Database unavailable\n```",
            stderr="", exit_code=0, duration_seconds=0.1,
            raw_output="```yaml\nROLE: EXECUTOR\nSTATUS: BLOCKED\nHANDOFF: NONE\nREASON: Database unavailable\n```",
        )

        reviewer_mock = MagicMock()

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_blocked) as exec_mock:
            res = runner.invoke(main, ["auto", "Build feature", "--max-retries", "2"])
            assert res.exit_code == 1
            assert "Executor blocked" in res.output
            # Reviewer should never have been invoked
            assert reviewer_mock.call_count == 0


def test_auto_pipeline_critic_runs_before_commit(tmp_path):
    """Verify closing Critic audit runs before auto-commit on uncommitted changes."""
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
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS",
        )
        rev_resp = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )
        critic_blocked = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: BLOCKED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="BLOCKED",
        )

        commit_mock = MagicMock(return_value=True)

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, rev_resp, critic_blocked]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp), \
             patch("forge.core.git.GitService.is_git_repo", return_value=True), \
             patch("forge.core.git.GitService.commit", commit_mock):
            res = runner.invoke(main, ["auto", "Build feature", "--auto-commit"])
            assert res.exit_code == 1
            assert "Closing Critic audit reported non-success status" in res.output
            # Because Critic blocked, auto-commit must NOT be called
            assert commit_mock.call_count == 0


