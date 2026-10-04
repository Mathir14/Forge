"""Regression tests for Forge v1.0.0 Final Release Gate Blockers."""

import concurrent.futures
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
from click.testing import CliRunner

from forge.cli import main
from forge.adapters.base import AdapterResponse
from forge.core.git import GitService
from forge.core.run import Run
from forge.storage.run_manager import RunManager


def test_untracked_dir_with_nested_env_not_leaked_and_diff_visible(tmp_path):
    """P0 Regression: Ensure untracked dirs show code diffs to reviewer and filter nested .env secrets."""
    git = GitService(tmp_path)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=tmp_path, check=True)

    # Initial commit
    (tmp_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, check=True)

    # Create untracked directory containing code and a sensitive .env file
    nested_dir = tmp_path / "services" / "auth"
    nested_dir.mkdir(parents=True)
    (nested_dir / ".env").write_text("API_SECRET_KEY=supersecret123\n", encoding="utf-8")
    (nested_dir / "service.py").write_text("print('auth service ready')\n", encoding="utf-8")

    # 1. Verify diff captures service.py but ignores .env
    diff_output = git.diff(include_untracked=True)
    assert "print('auth service ready')" in diff_output
    assert "supersecret123" not in diff_output
    assert ".env" not in diff_output

    # 2. Verify changed_files reports individual files, not just directory
    changed = git.changed_files()
    assert any("service.py" in f for f in changed)

    # 3. Verify commit commits service.py but excludes nested .env
    committed = git.commit("feat: add auth service")
    assert committed is True

    # 4. Inspect git commit log for committed files
    show_res = subprocess.run(["git", "show", "--name-only", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
    assert "services/auth/service.py" in show_res.stdout
    assert ".env" not in show_res.stdout


def test_unicode_binary_git_diff_handling(tmp_path):
    """P0 Regression: GitService._run must not crash on non-UTF8 bytes in diff."""
    git = GitService(tmp_path)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=tmp_path, check=True)
    (tmp_path / "data.bin").write_text("ascii line\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)

    # Write non-UTF-8 bytes to the tracked file
    with open(tmp_path / "data.bin", "wb") as f:
        f.write(b"ascii line\n" + bytes([255, 254, 128, 100]) + b"\n")

    diff = git.diff()
    assert isinstance(diff, str)


from tests.conftest import configure_automated_execution_environment


def test_closing_critic_failure_halts_auto_pipeline(tmp_path):
    """P0 Regression: auto_pipeline must halt and exit 1 if closing Critic audit fails."""
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
        # Critic reports FAILED
        critic_failed = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: FAILED\nHANDOFF: NONE\n```",
            stderr="Critical tech debt detected", exit_code=1, duration_seconds=0.1, raw_output="FAILED",
        )

        commit_mock = MagicMock(return_value=True)

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, critic_failed]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp), \
             patch("forge.core.git.GitService.is_git_repo", return_value=True), \
             patch("forge.core.git.GitService.commit", commit_mock):
            
            res = runner.invoke(main, ["auto", "Build critical feature", "--auto-commit"])
            assert res.exit_code == 1
            assert "Closing Critic audit reported non-success status" in res.output
            assert commit_mock.call_count == 0


def test_auto_pipeline_resume_preserves_task_in_repair_loop(tmp_path):
    """P0 Regression: Resuming auto_pipeline must preserve task in repair feedback and metadata."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()
        mgr = RunManager(Path.cwd())
        run = mgr.create_run(task="Build User Authentication System")

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY",
        )
        exec_fail = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: FAILED\nHANDOFF: NONE\n```",
            stderr="", exit_code=1, duration_seconds=0.1, raw_output="Syntax error in auth.py",
        )
        exec_ok = AdapterResponse(
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
            stdout="```yaml\nROLE: CRITIC\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, critic_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", side_effect=[exec_fail, exec_ok]):
            
            res = runner.invoke(main, ["auto", "--run", run.run_id, "--max-retries", "2"])
            assert res.exit_code == 0
            assert "Build User Authentication System" in res.output

            reloaded = mgr.resume(run.run_id)
            assert reloaded.task == "Build User Authentication System"
            assert not reloaded.task.startswith("None")


def _concurrent_write_worker(run_dir, worker_id):
    run = Run(run_id="run-001", task="Concurrent test", run_dir=run_dir)
    for i in range(25):
        run.metadata = {"worker": worker_id, "iter": i}
        run.save_metadata()


def test_concurrent_metadata_writes(tmp_path):
    """P1 Regression: Concurrent Run.save_metadata calls must not crash with FileNotFoundError."""
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()

    with concurrent.futures.ProcessPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(_concurrent_write_worker, run_dir, i) for i in range(6)]
        for f in futures:
            f.result()  # Must complete without exception

    meta_file = run_dir / "metadata.json"
    assert meta_file.exists()
    data = json.loads(meta_file.read_text(encoding="utf-8"))
    assert data["run_id"] == "run-001"


def test_pipeline_resumption_skips_completed_stages(tmp_path):
    """P1 Regression: forge run --run must skip already completed stages."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        mgr = RunManager(Path.cwd())
        run = mgr.create_run("Resume Stage Test")

        # Save stage 1 (architect) as already completed
        mgr.save_stage_artifacts(
            run=run,
            sequence_number=1,
            role_name="architect",
            markdown_content="# Arch Completed",
            json_data={"status": "APPROVED", "is_valid": True},
        )

        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=plan_resp) as mock_exec:
            # Answer 'n' at Planner confirmation
            res = runner.invoke(main, ["run", "--run", run.run_id], input="n\n")
            assert res.exit_code == 0
            assert "Skipping Stage: Architect" in res.output
            assert "Executing Stage: 02_PLANNER" in res.output
            # Architect was skipped, so execute was only called for planner
            assert mock_exec.call_count == 1


def test_version_metadata_is_beta():
    """P1 Regression: Version must match CLI and pyproject.toml."""
    runner = CliRunner()
    res = runner.invoke(main, ["--version"])
    assert res.exit_code == 0
    assert "0.1.0b5" in res.output

    pyproject_file = Path(__file__).resolve().parent.parent / "pyproject.toml"
    content = pyproject_file.read_text(encoding="utf-8")
    assert 'version = "0.1.0b5"' in content


def test_run_manager_latest_scales_without_loading_all_runs(tmp_path):
    """P2 Regression: RunManager.latest() must load only the newest run instead of scanning all files."""
    rm = RunManager(tmp_path)
    for i in range(1, 15):
        r_dir = rm.runs_dir / f"run-{i:03d}"
        r_dir.mkdir(parents=True)
        (r_dir / "metadata.json").write_text(f'{{"run_id": "run-{i:03d}"}}', encoding="utf-8")

    with patch("forge.core.run.Run.load", wraps=Run.load) as spy:
        latest_run = rm.latest()
        assert latest_run is not None
        assert latest_run.run_id == "run-014"
        # Must load only 1 run, not all 14 runs
        assert spy.call_count == 1
