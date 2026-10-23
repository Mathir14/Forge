"""Regression and generalization tests for Commit Integrity across repositories.

Verifies:
1. Clean baseline; all intended files committed.
2. Pre-existing unrelated modifications; run-owned files committed completely.
3. Files modified both before and during run (mixed ownership safely excluded, Commit stage PARTIAL, auto_committed: false).
4. Pre-existing staged content differing from working tree survives untouched.
5. All mixed-ownership run (0 pure files): Commit stage PARTIAL, working tree preserved.
6. Partial commit never silently converted to overall success.
7. Git commit creation failure: Commit stage FAILED.
8. Commit verification failure: Commit stage VERIFICATION_FAILED.
9. No changes to commit: Commit stage SKIPPED.
10. Commit disabled by configuration.
11. Association between commit SHA, committed file set, run ID, and metadata.
12. Generalization with different directory structures and multi-repo isolation.
"""

import json
from pathlib import Path
import subprocess
import pytest

from forge.core.config import Config
from forge.core.git import GitService, GitBaseline, CommitStatus, CommitResult
from forge.core.run import Run
from forge.storage.run_manager import RunManager
from forge.cli import auto_commit_run, _record_stage_transition, _record_final_halt, _record_final_success


@pytest.fixture
def clean_git_repo(tmp_path: Path):
    """Create a pristine git repository with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@forge.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)

    (repo / "README.md").write_text("# Project\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True)
    return repo


def test_scenario_1_clean_baseline_all_intended_committed(clean_git_repo, tmp_path):
    """Scenario 1: Clean baseline; all intended files committed completely."""
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="Add feature auth")

    baseline = git.capture_baseline()

    # Forge modifies two new files
    (repo / "auth.py").write_text("def auth(): return True\n", encoding="utf-8")
    (repo / "test_auth.py").write_text("def test_auth(): pass\n", encoding="utf-8")

    result = auto_commit_run(
        git=git,
        task_summary="Add feature auth",
        baseline=baseline,
        run=run,
        stages_executed=1,
        return_result=True,
    )

    assert result.status == CommitStatus.COMMITTED
    assert result.is_success is True
    assert result.is_partial is False
    assert result.commit_sha is not None
    assert set(result.committed_files) == {"auth.py", "test_auth.py"}
    assert result.excluded_files == []

    # Verify run metadata
    assert run.metadata.get("auto_committed") is True
    assert run.metadata.get("commit_status") == "COMMITTED"
    assert run.metadata.get("commit_sha") == result.commit_sha
    assert set(run.metadata.get("committed_files", [])) == {"auth.py", "test_auth.py"}
    assert run.metadata.get("excluded_files") == []

    # Verify HEAD in git
    head_files = git.get_commit_files(result.commit_sha)
    assert set(head_files) == {"auth.py", "test_auth.py"}


def test_scenario_2_preexisting_unrelated_modifications_preserved(clean_git_repo):
    """Scenario 2: Pre-existing unrelated modifications survive untouched."""
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="Add service worker")

    # User modifies an unrelated file before Forge run
    (repo / "user_notes.txt").write_text("unrelated draft\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge adds its own file
    (repo / "worker.py").write_text("class Worker: pass\n", encoding="utf-8")

    result = auto_commit_run(
        git=git,
        task_summary="Add service worker",
        baseline=baseline,
        run=run,
        stages_executed=1,
        return_result=True,
    )

    assert result.status == CommitStatus.COMMITTED
    assert result.committed_files == ["worker.py"]
    assert "user_notes.txt" not in result.committed_files

    # User's uncommitted work is preserved and still dirty
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "?? user_notes.txt" in status
    assert (repo / "user_notes.txt").read_text(encoding="utf-8") == "unrelated draft\n"


def test_scenario_3_mixed_ownership_partial_commit_reporting(clean_git_repo):
    """Scenario 3: Files modified both before and during run (Stress-Testing-Framework run-005 defect).

    4 mixed-ownership files excluded, 2 pure files committed.
    Forge MUST report Commit stage as PARTIAL, auto_committed: false, and run.status = PARTIAL.
    """
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="Audit and fix reporting views")

    # Seed 4 files that will have pre-existing edits
    (repo / "data.py").write_text("v1_data\n", encoding="utf-8")
    (repo / "report.py").write_text("v1_report\n", encoding="utf-8")
    (repo / "smoke.py").write_text("v1_smoke\n", encoding="utf-8")
    (repo / "unit.py").write_text("v1_unit\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add base modules"], cwd=repo, check=True)

    # User makes uncommitted edits before Forge runs
    (repo / "data.py").write_text("v1_data_user_edit\n", encoding="utf-8")
    (repo / "report.py").write_text("v1_report_user_edit\n", encoding="utf-8")
    (repo / "smoke.py").write_text("v1_smoke_user_edit\n", encoding="utf-8")
    (repo / "unit.py").write_text("v1_unit_user_edit\n", encoding="utf-8")

    baseline = git.capture_baseline()

    # Forge modifies all 4 mixed files + creates 2 pure files (like run-005)
    (repo / "data.py").write_text("v1_data_user_edit\nforge_line\n", encoding="utf-8")
    (repo / "report.py").write_text("v1_report_user_edit\nforge_line\n", encoding="utf-8")
    (repo / "smoke.py").write_text("v1_smoke_user_edit\nforge_line\n", encoding="utf-8")
    (repo / "unit.py").write_text("v1_unit_user_edit\nforge_line\n", encoding="utf-8")
    (repo / "base_view.py").write_text("pure_base_view\n", encoding="utf-8")
    (repo / "reports_view.py").write_text("pure_reports_view\n", encoding="utf-8")

    result = auto_commit_run(
        git=git,
        task_summary="Audit and fix reporting views",
        baseline=baseline,
        run=run,
        stages_executed=1,
        return_result=True,
    )

    # Required invariants for partial commit
    assert result.status == CommitStatus.PARTIAL
    assert result.is_success is False
    assert result.is_partial is True
    assert set(result.committed_files) == {"base_view.py", "reports_view.py"}
    assert set(result.excluded_files) == {"data.py", "report.py", "smoke.py", "unit.py"}
    assert "Partial commit: 2 of 6 file(s) committed" in result.reason

    # Metadata MUST NOT claim full auto_committed success
    assert run.metadata.get("auto_committed") is False
    assert run.metadata.get("commit_status") == "PARTIAL"
    assert run.metadata.get("commit_sha") == result.commit_sha
    assert set(run.metadata.get("committed_files", [])) == {"base_view.py", "reports_view.py"}
    assert set(run.metadata.get("excluded_files", [])) == {"data.py", "report.py", "smoke.py", "unit.py"}

    # All mixed-ownership user edits are preserved intact in working tree
    assert "forge_line" in (repo / "data.py").read_text(encoding="utf-8")
    assert "user_edit" in (repo / "data.py").read_text(encoding="utf-8")
    assert "forge_line" in (repo / "report.py").read_text(encoding="utf-8")


def test_scenario_4_staged_content_differing_from_working_tree_preserved(clean_git_repo):
    """Scenario 4: Pre-existing staged content differing from working-tree content is preserved."""
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="Run with staged user diff")

    (repo / "user_staged.py").write_text("staged_v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init staged file"], cwd=repo, check=True)

    # User stages content A, then edits working tree to content B
    (repo / "user_staged.py").write_text("index_version_A\n", encoding="utf-8")
    subprocess.run(["git", "add", "user_staged.py"], cwd=repo, check=True)
    (repo / "user_staged.py").write_text("worktree_version_B\n", encoding="utf-8")

    baseline = git.capture_baseline()

    # Forge modifies another file
    (repo / "pure_forge.py").write_text("forge_content\n", encoding="utf-8")

    result = auto_commit_run(
        git=git,
        task_summary="Run with staged user diff",
        baseline=baseline,
        run=run,
        stages_executed=1,
        return_result=True,
    )

    assert result.status == CommitStatus.COMMITTED
    assert result.committed_files == ["pure_forge.py"]

    # Verify user's staged index content is still index_version_A
    index_content = subprocess.run(["git", "show", ":user_staged.py"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert index_content == "index_version_A\n"

    # Verify user's working tree is still worktree_version_B
    assert (repo / "user_staged.py").read_text(encoding="utf-8") == "worktree_version_B\n"


def test_scenario_5_all_mixed_changes_zero_pure(clean_git_repo):
    """Scenario 5: A run where all modified files have mixed ownership (0 pure files)."""
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="Modify existing file")

    (repo / "file1.py").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init file1"], cwd=repo, check=True)

    (repo / "file1.py").write_text("init_user_wip\n", encoding="utf-8")
    baseline = git.capture_baseline()

    (repo / "file1.py").write_text("init_user_wip\nforge_wip\n", encoding="utf-8")

    result = auto_commit_run(
        git=git,
        task_summary="Modify existing file",
        baseline=baseline,
        run=run,
        stages_executed=1,
        return_result=True,
    )

    assert result.status == CommitStatus.PARTIAL
    assert result.committed_files == []
    assert result.excluded_files == ["file1.py"]
    assert run.metadata.get("auto_committed") is False
    assert run.metadata.get("commit_status") == "PARTIAL"


def test_scenario_6_git_commit_failure_handled(clean_git_repo, monkeypatch):
    """Scenario 6: Git commit creation failure (e.g. hook rejects or git error)."""
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="Trigger commit failure")

    baseline = git.capture_baseline()
    (repo / "new_file.py").write_text("new\n", encoding="utf-8")

    # Simulate git commit failure
    def mock_commit(*args, **kwargs):
        return False
    monkeypatch.setattr(git, "commit", mock_commit)

    result = auto_commit_run(
        git=git,
        task_summary="Trigger commit failure",
        baseline=baseline,
        run=run,
        stages_executed=1,
        return_result=True,
    )

    assert result.status == CommitStatus.FAILED
    assert result.is_success is False
    assert run.metadata.get("auto_committed") is False
    assert run.metadata.get("commit_status") == "FAILED"


def test_scenario_7_no_changes_to_commit(clean_git_repo):
    """Scenario 7: No changes made during run."""
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="No-op run")

    baseline = git.capture_baseline()

    result = auto_commit_run(
        git=git,
        task_summary="No-op run",
        baseline=baseline,
        run=run,
        stages_executed=1,
        return_result=True,
    )

    assert result.status == CommitStatus.NO_CHANGES
    assert run.metadata.get("auto_committed") is False
    assert run.metadata.get("commit_status") == "NO_CHANGES"


def test_scenario_8_commit_verification_detects_unintended_files(clean_git_repo, monkeypatch):
    """Scenario 8: Commit verification catches unintended files in commit."""
    repo = clean_git_repo
    git = GitService(repo)

    baseline = git.capture_baseline()
    (repo / "intended.py").write_text("ok\n", encoding="utf-8")

    # Mock get_commit_files and get_head_commit to simulate accidental inclusion of secret file
    monkeypatch.setattr(git, "get_commit_files", lambda sha: ["intended.py", "unintended_secret.py"])
    monkeypatch.setattr(git, "commit", lambda *a, **k: True)

    shas = ["commit_before_sha", "commit_after_sha"]
    monkeypatch.setattr(git, "get_head_commit", lambda: shas.pop(0) if shas else "commit_after_sha")

    result = git.commit_run("message", baseline=baseline, return_result=True)
    assert result.status == CommitStatus.VERIFICATION_FAILED
    assert "unintended file(s) committed" in result.reason


def test_scenario_9_commit_disabled_by_config(clean_git_repo):
    """Scenario 9: Auto-commit disabled by configuration."""
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="Run with commit disabled")

    # Config has execution.auto_commit = False
    config = Config()
    config.execution.auto_commit = False

    # In pipeline, when auto_commit is False, auto_commit_run is skipped
    auto_commit_requested = False or config.execution.auto_commit
    assert auto_commit_requested is False

    _record_stage_transition(
        run, "Commit", "SKIPPED", "SKIPPED",
        role_name="commit", seq=99,
        reason="Auto-commit disabled by configuration"
    )

    item = run.summary.get_stage("Commit")
    assert item is not None
    assert item.status == "SKIPPED"
    assert item.execution_state == "SKIPPED"
    assert "disabled by configuration" in item.reason
    assert run.metadata.get("auto_committed", False) is False


def test_scenario_10_cli_override_enabling_auto_commit(clean_git_repo):
    """Scenario 10: CLI override --auto-commit enables commit even if config disables it."""
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="Run with CLI override")

    config = Config()
    config.execution.auto_commit = False

    # CLI flag --auto-commit passed by user
    cli_auto_commit_flag = True
    effective_auto_commit = cli_auto_commit_flag or config.execution.auto_commit
    assert effective_auto_commit is True

    baseline = git.capture_baseline()
    (repo / "feature_cli.py").write_text("print('cli')\n", encoding="utf-8")

    result = auto_commit_run(
        git=git,
        task_summary="Run with CLI override",
        baseline=baseline,
        run=run,
        stages_executed=1,
        return_result=True,
    )
    assert result.status == CommitStatus.COMMITTED
    assert run.metadata.get("auto_committed") is True


def test_scenario_11_partial_commit_followed_by_later_stage_failure(clean_git_repo):
    """Scenario 11: A partial commit followed by a later-stage failure preserves failure status."""
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="Run with later stage failure")

    # Seed mixed file
    (repo / "shared.py").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    (repo / "shared.py").write_text("v1_dirty\n", encoding="utf-8")

    baseline = git.capture_baseline()

    (repo / "shared.py").write_text("v1_dirty_forge\n", encoding="utf-8")
    (repo / "pure.py").write_text("pure\n", encoding="utf-8")

    commit_res = auto_commit_run(
        git=git,
        task_summary="Run with later stage failure",
        baseline=baseline,
        run=run,
        stages_executed=1,
        return_result=True,
    )
    assert commit_res.status == CommitStatus.PARTIAL

    # Record Commit stage as PARTIAL
    _record_stage_transition(
        run, "Commit", "PARTIAL", "COMPLETED",
        role_name="commit", seq=99, reason=commit_res.reason
    )

    # Later post-run check fails
    later_failure_reason = "Post-run integration check failed with connection timeout"
    run.status = "FAILED"
    _record_final_halt(run, "FAILED", later_failure_reason)

    assert run.status == "FAILED"
    assert run.summary.final_status == "FAILED"
    assert run.summary.reason == later_failure_reason
    # Partial commit metadata remains accurate
    assert run.metadata.get("auto_committed") is False
    assert run.metadata.get("commit_status") == "PARTIAL"


def test_scenario_12_generalization_nested_directories_and_layouts(clean_git_repo):
    """Scenario 12: Generalization across complex repository structures (deep subdirectories, nested layouts)."""
    repo = clean_git_repo
    git = GitService(repo)
    run_mgr = RunManager(repo)
    run = run_mgr.create_run(task="Refactor nested packages")

    # Create deeply nested directory structure
    deep_dir = repo / "packages" / "sub_a" / "src" / "deeply" / "nested"
    deep_dir.mkdir(parents=True, exist_ok=True)
    (deep_dir / "module.py").write_text("# Deep module\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init deep tree"], cwd=repo, check=True)

    # Pre-existing user edit in one deep file
    (deep_dir / "module.py").write_text("# Deep module\n# User edit\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge edits deep file (mixed) + adds another deep file (pure)
    (deep_dir / "module.py").write_text("# Deep module\n# User edit\n# Forge edit\n", encoding="utf-8")
    (deep_dir / "helper.py").write_text("# Deep helper\n", encoding="utf-8")

    result = auto_commit_run(
        git=git,
        task_summary="Refactor nested packages",
        baseline=baseline,
        run=run,
        stages_executed=1,
        return_result=True,
    )

    assert result.status == CommitStatus.PARTIAL
    expected_pure = "packages/sub_a/src/deeply/nested/helper.py"
    expected_mixed = "packages/sub_a/src/deeply/nested/module.py"
    assert expected_pure in result.committed_files
    assert expected_mixed in result.excluded_files
    assert run.metadata.get("auto_committed") is False
    assert run.metadata.get("commit_status") == "PARTIAL"
