"""Regression tests for Forge v1.0 Final Remediation Pass."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from click.testing import CliRunner

from forge import __version__
from forge.cli import main, runs_cmd
from forge.adapters.base import AdapterResponse
from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.opencode import OpenCodeAdapter
from forge.core.git import GitService
from forge.core.run import Run
from forge.protocol.parser import MachineReportParser
from forge.storage.run_manager import RunManager


def test_monorepo_subdirectory_git_path_resolution(tmp_path):
    """Finding 1 (P0): GitService must resolve paths relative to repo root in subdirectories."""
    # 1. Initialize git repo at tmp_path
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=tmp_path, check=True)

    (tmp_path / "README.md").write_text("# Root Project\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, check=True)

    # 2. Create subproject directory
    subproject = tmp_path / "services" / "payment"
    subproject.mkdir(parents=True)
    worker_file = subproject / "worker.py"
    worker_file.write_text("print('payment worker active')\n", encoding="utf-8")

    # 3. Instantiate GitService pointing to subproject
    git_sub = GitService(subproject)

    # 4. Verify changed_files returns worker.py relative to subproject
    changed = git_sub.changed_files()
    assert "worker.py" in changed

    # 5. Verify diff includes worker.py content
    diff_output = git_sub.diff(include_untracked=True)
    assert "print('payment worker active')" in diff_output

    # 6. Verify commit works from subproject
    committed = git_sub.commit("feat: add payment worker")
    assert committed is True

    # 7. Verify git log shows committed file
    show_res = subprocess.run(
        ["git", "show", "--name-only", "--oneline"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "services/payment/worker.py" in show_res.stdout


def test_staged_changes_captured_in_diff(tmp_path):
    """Finding 2 (P0): Staged changes must be captured in git.diff() for Reviewer/Critic."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=tmp_path, check=True)

    app_file = tmp_path / "app.py"
    app_file.write_text("def hello():\n    return 'v1'\n", encoding="utf-8")
    utils_file = tmp_path / "utils.py"
    utils_file.write_text("def helper():\n    return 'u1'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)

    # 1. Stage a modification in app.py
    app_file.write_text("def hello():\n    return 'v2-staged'\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)

    git = GitService(tmp_path)
    # Default diff() must capture the staged modification
    diff_output = git.diff()
    assert "v2-staged" in diff_output

    # 2. Add an unstaged modification in utils.py without staging or committing
    utils_file.write_text("def helper():\n    return 'u2-unstaged'\n", encoding="utf-8")

    diff_both = git.diff()
    assert "v2-staged" in diff_both
    assert "u2-unstaged" in diff_both


def test_adapter_failure_does_not_persist_approved_status(tmp_path):
    """Finding 3 (P1): Adapter failure with stdout approval must NOT write APPROVED to run metadata."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])

        hallucinated_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="Subprocess segfaulted or exited with error",
            exit_code=1,
            duration_seconds=0.5,
            raw_output="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=hallucinated_resp):
            res = runner.invoke(main, ["architect", "Build new engine"])
            assert res.exit_code == 1

        rm = RunManager(Path.cwd())
        latest_run = rm.latest()
        assert latest_run is not None
        assert latest_run.status == "FAILED"
        assert latest_run.status != "APPROVED"


def test_auto_pipeline_resume_skips_approved_executor_and_reviewer(tmp_path):
    """Finding 4 (P1): forge auto --run must skip Executor & Reviewer if already APPROVED."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        rm = RunManager(Path.cwd())
        run = rm.create_run(task="Implement Feature X")

        # Save approved artifacts for architect, planner, executor, and reviewer
        rm.save_stage_artifacts(
            run=run, sequence_number=1, role_name="architect",
            markdown_content="# Architect Report", json_data={"role": "architect", "status": "APPROVED"},
        )
        rm.save_stage_artifacts(
            run=run, sequence_number=2, role_name="planner",
            markdown_content="# Planner Report", json_data={"role": "planner", "status": "READY"},
        )
        rm.save_stage_artifacts(
            run=run, sequence_number=3, role_name="executor",
            markdown_content="# Executor Report", json_data={"role": "executor", "status": "SUCCESS"},
        )
        rm.save_stage_artifacts(
            run=run, sequence_number=4, role_name="reviewer",
            markdown_content="# Reviewer Report", json_data={"role": "reviewer", "status": "APPROVED"},
        )

        critic_resp = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="CRITIQUE_COMPLETE",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute") as mock_exec, \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=critic_resp) as mock_critic:
            res = runner.invoke(main, ["auto", "--run", run.run_id])
            assert res.exit_code == 0
            assert "Skipping Executor & Reviewer" in res.output
            # Executor should NOT have been called because reviewer was already approved
            assert mock_exec.call_count == 0
            # Critic should have been called for stage 5
            assert mock_critic.call_count == 1


def test_windows_path_separator_commit_filter(tmp_path):
    """Finding 5 (P1): Windows backslash paths (.forge\\...) must be excluded from commit staging."""
    git = GitService(tmp_path)
    captured_add = []

    def mock_run(args, **kwargs):
        if args and args[0] == "add":
            captured_add.extend(args)
            return MagicMock(returncode=0, stdout="", stderr="")
        elif args and args[0] == "commit":
            return MagicMock(returncode=0, stdout="[main 123] committed", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    windows_paths = [
        r".forge\runs\run-001\01_architect.json",
        r".forge\cache\index.db",
        r"src\services\auth.py",
        r".env",
        r"config\.env.local",
    ]

    with patch.object(git, "_run", side_effect=mock_run):
        committed = git.commit("feat: windows paths", paths=windows_paths)
        assert committed is True
        # Only src\services\auth.py should be in git add
        assert r"src\services\auth.py" in captured_add
        for bad in [r".forge\runs\run-001\01_architect.json", r".forge\cache\index.db", r".env", r"config\.env.local"]:
            assert bad not in captured_add


def test_fallback_protocol_parser_with_blank_lines():
    """Finding 6 (P1): Fallback parser must not truncate protocol report on section blank lines."""
    raw_output = """
Here is my evaluation of the architecture:

ROLE: ARCHITECT
PROMPT_VERSION: 1.0
TASK_ID: task-42

START_TIME: 2026-09-15T12:00:00
END_TIME: 2026-09-15T12:05:00
DURATION: 300s

STATUS: APPROVED
EXIT_CODE: 0
HANDOFF: PLANNER
REASON: Architecture is sound and verified.

ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []

CONFIDENCE: HIGH
NEXT_ACTION: Proceed to planning

## Human Summary
The architecture looks solid.
"""
    data, raw_yaml = MachineReportParser.extract_yaml(raw_output)
    assert data != {}
    assert data.get("ROLE") == "ARCHITECT"
    assert data.get("STATUS") == "APPROVED"
    assert data.get("HANDOFF") == "PLANNER"


def test_successful_stage_command_persists_status(tmp_path):
    """Finding 7 (P2): Running forge architect/planner must persist status in metadata.json."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=arch_resp):
            res = runner.invoke(main, ["architect", "Design system"])
            assert res.exit_code == 0

        rm = RunManager(Path.cwd())
        latest_run = rm.latest()
        assert latest_run is not None
        assert latest_run.status == "APPROVED"

        # Verify forge runs outputs APPROVED
        runs_res = runner.invoke(main, ["runs"])
        assert runs_res.exit_code == 0
        assert "APPROVED" in runs_res.output
        assert "IN_PROGRESS" not in runs_res.output


def test_adapter_handles_keyboard_interrupt():
    """Finding 8 (P2): Adapters must catch KeyboardInterrupt and return exit_code 130."""
    with patch("subprocess.run", side_effect=KeyboardInterrupt):
        agy = AntigravityAdapter()
        with patch.object(agy, "_get_binary", return_value="/fake/agy"):
            resp_agy = agy.execute("test prompt")
            assert resp_agy.exit_code == 130
            assert "SIGINT" in resp_agy.stderr

        opencode = OpenCodeAdapter()
        resp_oc = opencode.execute("test prompt")
        assert resp_oc.exit_code == 130
        assert "SIGINT" in resp_oc.stderr


def test_save_stage_artifacts_is_atomic(tmp_path):
    """Finding 9 (P2): save_stage_artifacts must write files atomically."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Atomic test")

    md_file, json_file = rm.save_stage_artifacts(
        run=run,
        sequence_number=1,
        role_name="architect",
        markdown_content="# Complete Markdown",
        json_data={"ROLE": "ARCHITECT", "STATUS": "APPROVED"},
    )

    assert md_file.exists()
    assert json_file.exists()
    assert md_file.read_text(encoding="utf-8") == "# Complete Markdown"
    loaded_json = json.loads(json_file.read_text(encoding="utf-8"))
    assert loaded_json["STATUS"] == "APPROVED"


def test_runs_cmd_avoids_loading_all_metadata(tmp_path, monkeypatch):
    """Finding 10 (P2): forge runs -n 5 must only load metadata for the requested slice."""
    rm = RunManager(tmp_path)
    for i in range(1, 26):
        r_dir = rm.runs_dir / f"run-{i:03d}"
        r_dir.mkdir(parents=True)
        (r_dir / "metadata.json").write_text(
            json.dumps({"run_id": f"run-{i:03d}", "task": f"Task {i}", "created_at": "2026-09-15T00:00:00", "status": "APPROVED"}),
            encoding="utf-8",
        )

    assert rm.count_runs() == 25
    monkeypatch.chdir(tmp_path)

    with patch("forge.core.run.Run.load", wraps=Run.load) as spy:
        runner = CliRunner()
        res = runner.invoke(main, ["runs", "-n", "5"])
        assert res.exit_code == 0
        assert "Total runs: 25 (showing 5)" in res.output
        # Should only load the 5 requested runs, not all 25
        assert spy.call_count == 5


def test_version_metadata_consistency():
    """Finding 11 (Release): __version__ must be 1.0.0 matching pyproject.toml and CLI."""
    assert __version__ == "1.0.0"

    runner = CliRunner()
    res = runner.invoke(main, ["--version"])
    assert res.exit_code == 0
    assert "1.0.0" in res.output
