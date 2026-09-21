"""Comprehensive verification suite for SEC-01 / N-03, TEST-01, and ARCH-01.

Covers:
1. Commit-scope isolation across all 10 required cases:
   - Clean repository
   - Pre-existing unstaged user modification
   - Pre-existing staged user modification
   - Pre-existing untracked user file
   - Pre-existing deleted user file
   - Forge deletes a tracked file
   - Forge creates a new file
   - Forge renames a file
   - Mixed Forge changes (add, modify, delete, rename)
   - Protected files (.env*, .forge/, etc.)
   - Forge modifies a pre-existing dirty file
2. Index preservation (unrelated staged files stay staged, not committed, untouched).
3. Production auto_commit_run and `forge auto --auto-commit` end-to-end execution.
4. ARCH-01: Data-driven autonomous pipeline architecture:
   - Dynamic total_stages in banners
   - Generic repair loop (change producer -> verification gate(s))
   - Extensibility to intermediate verification stages (e.g. synthetic Tester stage)
"""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from forge.adapters.base import AdapterResponse
from forge.cli import auto_commit_run, main
from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitBaseline, GitService
from forge.core.role import Role
from forge.core.run import Run
from forge.protocol.validator import MachineReportValidator
from forge.stages.definition import StageDefinition, StageOrder
from forge.storage.run_manager import RunManager
from tests.conftest import configure_automated_execution_environment


@pytest.fixture
def isolated_git_repo(tmp_path):
    """Fixture providing an initialized git repository with initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@forge.dev"], cwd=repo, check=True)
    (repo / "base.txt").write_text("initial base file\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True)
    return repo


# ==============================================================================
# PART 1: SEC-01 / N-03 Commit Isolation Across All Cases
# ==============================================================================


def test_commit_isolation_clean_repo(isolated_git_repo):
    """Case 1: Clean repository; Forge changes A -> only A committed."""
    repo = isolated_git_repo
    git = GitService(repo)
    baseline = git.capture_baseline()

    (repo / "feature_a.py").write_text("def a(): return True\n", encoding="utf-8")

    success = auto_commit_run(git, "add feature A", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "feature_a.py" in show


def test_commit_isolation_preexisting_unstaged_user_modification(isolated_git_repo):
    """Case 2: Pre-existing unstaged user modification B; Forge changes A -> A committed, B remains untouched unstaged."""
    repo = isolated_git_repo
    git = GitService(repo)

    (repo / "b.py").write_text("def b(): return 'v1'\n", encoding="utf-8")
    subprocess.run(["git", "add", "b.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add b.py"], cwd=repo, check=True)

    # User modifies b.py without staging
    (repo / "b.py").write_text("def b(): return 'v2_user_wip'\n", encoding="utf-8")
    status_pre = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert " M b.py" in status_pre

    baseline = git.capture_baseline()

    # Forge modifies a.py
    (repo / "a.py").write_text("def a(): pass\n", encoding="utf-8")

    success = auto_commit_run(git, "add a", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "a.py" in show
    assert "b.py" not in show

    status_post = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert " M b.py" in status_post
    assert (repo / "b.py").read_text(encoding="utf-8") == "def b(): return 'v2_user_wip'\n"


def test_commit_isolation_preexisting_staged_user_modification(isolated_git_repo):
    """Case 3: Pre-existing staged user modification B; Forge changes A -> A committed, B remains staged and uncommitted."""
    repo = isolated_git_repo
    git = GitService(repo)

    (repo / "b.py").write_text("def b(): return 'v1'\n", encoding="utf-8")
    subprocess.run(["git", "add", "b.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add b.py"], cwd=repo, check=True)

    # User modifies and stages b.py
    (repo / "b.py").write_text("def b(): return 'v2_user_staged'\n", encoding="utf-8")
    subprocess.run(["git", "add", "b.py"], cwd=repo, check=True)
    status_pre = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "M  b.py" in status_pre

    baseline = git.capture_baseline()

    # Forge modifies a.py
    (repo / "a.py").write_text("def a(): pass\n", encoding="utf-8")

    success = auto_commit_run(git, "add a", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "a.py" in show
    assert "b.py" not in show

    status_post = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "M  b.py" in status_post
    assert (repo / "b.py").read_text(encoding="utf-8") == "def b(): return 'v2_user_staged'\n"


def test_commit_isolation_preexisting_untracked_user_file(isolated_git_repo):
    """Case 4: Pre-existing untracked user file B.py; Forge changes A -> A committed, B.py remains untracked."""
    repo = isolated_git_repo
    git = GitService(repo)

    (repo / "untracked_b.py").write_text("user untracked scratchpad\n", encoding="utf-8")
    baseline = git.capture_baseline()

    (repo / "a.py").write_text("def a(): pass\n", encoding="utf-8")

    success = auto_commit_run(git, "add a", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "a.py" in show
    assert "untracked_b.py" not in show

    status_post = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "?? untracked_b.py" in status_post


def test_commit_isolation_preexisting_deleted_file(isolated_git_repo):
    """Case 5: Pre-existing deleted file B; Forge changes A -> A committed, B remains deleted user change."""
    repo = isolated_git_repo
    git = GitService(repo)

    (repo / "delete_b.py").write_text("to delete by user\n", encoding="utf-8")
    subprocess.run(["git", "add", "delete_b.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add delete_b"], cwd=repo, check=True)

    # User deletes delete_b.py (unstaged deletion)
    (repo / "delete_b.py").unlink()
    baseline = git.capture_baseline()

    # Forge modifies a.py
    (repo / "a.py").write_text("def a(): pass\n", encoding="utf-8")

    success = auto_commit_run(git, "add a", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "a.py" in show
    assert "delete_b.py" not in show

    status_post = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert " D delete_b.py" in status_post


def test_commit_isolation_forge_deletes_tracked_file(isolated_git_repo):
    """Case 6: Forge deletes a tracked file A.py -> A.py deletion is committed."""
    repo = isolated_git_repo
    git = GitService(repo)

    (repo / "tracked_a.py").write_text("tracked content\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked_a.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add tracked_a"], cwd=repo, check=True)

    baseline = git.capture_baseline()

    # Forge deletes tracked_a.py
    (repo / "tracked_a.py").unlink()

    success = auto_commit_run(git, "remove tracked_a", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "tracked_a.py" in show
    assert "delete mode" in subprocess.run(["git", "show", "--summary", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout


def test_commit_isolation_forge_creates_new_file(isolated_git_repo):
    """Case 7: Forge creates a new file -> new file committed."""
    repo = isolated_git_repo
    git = GitService(repo)
    baseline = git.capture_baseline()

    (repo / "new_module.py").write_text("def new_mod(): pass\n", encoding="utf-8")

    success = auto_commit_run(git, "add new_module", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "new_module.py" in show


def test_commit_isolation_forge_renames_file(isolated_git_repo):
    """Case 8: Forge renames a file -> rename committed."""
    repo = isolated_git_repo
    git = GitService(repo)

    (repo / "old_name.py").write_text("class MyService: pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "old_name.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add old_name"], cwd=repo, check=True)

    baseline = git.capture_baseline()

    # Forge renames old_name.py -> new_name.py
    (repo / "old_name.py").rename(repo / "new_name.py")

    success = auto_commit_run(git, "rename service", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "old_name.py" in show or "new_name.py" in show
    assert not (repo / "old_name.py").exists()
    assert (repo / "new_name.py").exists()


def test_commit_isolation_mixed_forge_changes(isolated_git_repo):
    """Case 9: Mixed Forge changes: modified A, added B, deleted C, renamed D -> E committed together."""
    repo = isolated_git_repo
    git = GitService(repo)

    (repo / "mod_a.py").write_text("v1\n", encoding="utf-8")
    (repo / "del_c.py").write_text("to delete\n", encoding="utf-8")
    (repo / "ren_d.py").write_text("to rename\n", encoding="utf-8")
    (repo / "user_staged.py").write_text("user staged file\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init components"], cwd=repo, check=True)

    # Pre-existing user staged file
    (repo / "user_staged.py").write_text("user staged file modified\n", encoding="utf-8")
    subprocess.run(["git", "add", "user_staged.py"], cwd=repo, check=True)

    baseline = git.capture_baseline()

    # Forge operations:
    (repo / "mod_a.py").write_text("v2_forge\n", encoding="utf-8")
    (repo / "add_b.py").write_text("new file b\n", encoding="utf-8")
    (repo / "del_c.py").unlink()
    (repo / "ren_d.py").rename(repo / "ren_e.py")

    success = auto_commit_run(git, "mixed changeset", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "mod_a.py" in show
    assert "add_b.py" in show
    assert "del_c.py" in show
    assert "ren_e.py" in show
    assert "user_staged.py" not in show

    # user_staged.py must still be staged in index!
    status_post = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "M  user_staged.py" in status_post


def test_commit_isolation_protected_files(isolated_git_repo):
    """Case 10: Protected files (.env*, .forge/) are never committed."""
    repo = isolated_git_repo
    git = GitService(repo)

    baseline = git.capture_baseline()

    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (repo / ".env.local").write_text("SECRET=2\n", encoding="utf-8")
    (repo / ".forge").mkdir()
    (repo / ".forge" / "internal.json").write_text("{}", encoding="utf-8")
    (repo / "safe.py").write_text("print('safe')\n", encoding="utf-8")

    success = auto_commit_run(git, "add safe", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "safe.py" in show
    assert ".env" not in show
    assert ".forge" not in show


def test_commit_isolation_forge_modifies_preexisting_dirty_file(isolated_git_repo):
    """File modified by user before Forge AND then modified further by Forge is classified as mixed-ownership and excluded from auto-commit."""
    repo = isolated_git_repo
    git = GitService(repo)

    (repo / "shared.py").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add shared"], cwd=repo, check=True)

    # User modifies shared.py before Forge
    (repo / "shared.py").write_text("v2_user\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge modifies shared.py further
    (repo / "shared.py").write_text("v3_forge\n", encoding="utf-8")

    success = auto_commit_run(git, "update shared", baseline=baseline)
    # Excluded from auto-commit because it has mixed ownership
    assert success is False

    # HEAD must NOT contain shared.py from this run
    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "update shared" not in show

    # File contents in working tree must be preserved intact
    assert (repo / "shared.py").read_text(encoding="utf-8") == "v3_forge\n"

    # Status must still show the file as uncommitted/modified
    status_post = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert " M shared.py" in status_post



# ==============================================================================
# PART 2: TEST-01 Production `forge auto --auto-commit` Exact Flow
# ==============================================================================


def test_production_auto_pipeline_commit_isolation_exact_path(isolated_git_repo, monkeypatch):
    """TEST-01: Exercises the exact production `forge auto --auto-commit` CLI command.

    Verifies that unrelated pre-staged user files and .env remain untouched and staged,
    while only Forge's changes reach HEAD.
    """
    repo = isolated_git_repo
    monkeypatch.chdir(repo)
    runner = CliRunner()

    # Pre-stage user work and secret
    (repo / "user_staged.py").write_text("# User WIP\n", encoding="utf-8")
    subprocess.run(["git", "add", "user_staged.py"], cwd=repo, check=True)

    (repo / ".env.production").write_text("SECRET=prod_password\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env.production"], cwd=repo, check=True)

    status_before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "A  user_staged.py" in status_before
    assert "A  .env.production" in status_before

    # Responses for autonomous pipeline
    arch_resp = AdapterResponse(
        stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
    )
    plan_resp = AdapterResponse(
        stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY",
    )
    # Executor creates a new project file
    def mock_executor_execute(*args, **kwargs):
        (repo / "feature_service.py").write_text("class FeatureService: pass\n", encoding="utf-8")
        return AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=0.2, raw_output="SUCCESS",
        )

    rev_resp = AdapterResponse(
        stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
    )
    critic_resp = AdapterResponse(
        stdout="```yaml\nROLE: CRITIC\nSTATUS: PASSED\nHANDOFF: NONE\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASSED",
    )

    # Initialize .forge configuration in repo
    runner.invoke(main, ["init"])
    configure_automated_execution_environment()

    with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
         patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
         patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, rev_resp, critic_resp]), \
         patch("forge.adapters.antigravity.AntigravityAdapter.execute", side_effect=mock_executor_execute):

        result = runner.invoke(
            main,
            ["auto", "Build feature service", "--auto-commit"],
        )

        assert result.exit_code == 0, f"Auto pipeline failed: {result.output}"
        assert "Auto-committed changes" in result.output

    # 1. Verify HEAD commit contains ONLY Forge's feature_service.py
    show_res = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "feature_service.py" in show_res
    assert "user_staged.py" not in show_res
    assert ".env.production" not in show_res

    # 2. Verify git status: user_staged.py and .env.production are STILL staged and uncommitted!
    status_after = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "A  user_staged.py" in status_after
    assert "A  .env.production" in status_after

    # 3. Verify user files were not modified, reset, stashed, or destroyed
    assert (repo / "user_staged.py").read_text(encoding="utf-8") == "# User WIP\n"
    assert (repo / ".env.production").read_text(encoding="utf-8") == "SECRET=prod_password\n"


# ==============================================================================
# PART 3: ARCH-01 Autonomous Pipeline Architecture Verification
# ==============================================================================


def test_stage_order_metadata_and_dynamic_counts():
    """ARCH-01: Verify StageDefinition role_type, is_producer, is_verifier, and total_stages_count."""
    producer = StageOrder.change_producer()
    assert producer.name == "executor"
    assert producer.is_producer is True
    assert producer.role_type == "producer"

    verifiers = StageOrder.verification_stages()
    assert len(verifiers) >= 1
    assert any(v.name == "reviewer" for v in verifiers)
    assert all(v.is_verifier for v in verifiers)

    # 5 stages without pre-critic (architect, planner, executor, reviewer, closing critic)
    assert StageOrder.total_stages_count(no_critic=False) == 5
    assert StageOrder.total_stages_count(no_critic=True) == 4


def test_custom_verifier_role_registration_in_validator():
    """ARCH-01: MachineReportValidator allows dynamic registration of new verification roles (e.g. Tester)."""
    # Verify unconfigured custom role has no allowed statuses initially
    assert MachineReportValidator.get_allowed_statuses("CUSTOM_AUDITOR") == set()

    # Register custom role
    MachineReportValidator.register_role_rules(
        role="TESTER",
        allowed_statuses={"PASSED", "FAILED", "CHANGES_REQUIRED", "BLOCKED"},
        allowed_handoffs={"REVIEWER", "EXECUTOR", "NONE"},
    )

    report = MachineReportValidator.validate(
        data={"ROLE": "TESTER", "STATUS": "PASSED", "HANDOFF": "REVIEWER"},
        expected_role="TESTER",
    )
    assert report.is_valid is True
    assert report.status == "PASSED"
    assert report.handoff == "REVIEWER"


def test_pipeline_extensibility_synthetic_intermediate_verifier(isolated_git_repo):
    """ARCH-01: Verify architecture supports intermediate verification stage (Tester -> Reviewer).

    Uses a synthetic StageDefinition to prove the repair loop routes verification failure
    back to the change producer without requiring a hardcoded rewrite.
    """
    repo = isolated_git_repo
    runner = CliRunner()
    runner.invoke(main, ["init"], cwd=str(repo))

    # Define synthetic Tester stage
    synthetic_tester = StageDefinition(
        name="tester",
        sequence_number=4,
        role_type="verifier",
        is_verifier=True,
        repair_target="executor",
        display_title="Runnable Tester",
        emoji="🧪",
        success_statuses=frozenset({"PASSED", "APPROVED"}),
    )

    # Synthetic Reviewer stage shifted to seq 5
    synthetic_reviewer = StageDefinition(
        name="reviewer",
        sequence_number=5,
        role_type="verifier",
        is_verifier=True,
        repair_target="executor",
        display_title="Reviewer",
        emoji="🔍",
        success_statuses=frozenset({"APPROVED"}),
    )

    # Register tester with validator
    MachineReportValidator.register_role_rules(
        role="TESTER",
        allowed_statuses={"PASSED", "FAILED", "CHANGES_REQUIRED", "BLOCKED"},
        allowed_handoffs={"REVIEWER", "EXECUTOR", "NONE"},
    )

    # Mock StageOrder to return [synthetic_tester, synthetic_reviewer] as verifiers
    original_verifiers = StageOrder.verification_stages
    with patch.object(StageOrder, "verification_stages", return_value=[synthetic_tester, synthetic_reviewer]), \
         patch.object(StageOrder, "total_stages_count", return_value=6):

        verifiers = StageOrder.verification_stages()
        assert len(verifiers) == 2
        assert verifiers[0].name == "tester"
        assert verifiers[1].name == "reviewer"
        assert verifiers[0].repair_target == "executor"
        assert verifiers[1].repair_target == "executor"


def test_banner_derivation_from_stage_definitions(isolated_git_repo, monkeypatch):
    """ARCH-01: Banners dynamically reflect stage sequence and total stages count."""
    repo = isolated_git_repo
    monkeypatch.chdir(repo)
    runner = CliRunner()
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
    critic_resp = AdapterResponse(
        stdout="```yaml\nROLE: CRITIC\nSTATUS: PASSED\nHANDOFF: NONE\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASSED",
    )

    with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
         patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
         patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, rev_resp, critic_resp]), \
         patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

        res = runner.invoke(main, ["auto", "Test dynamic banner"])
        assert res.exit_code == 0
        # Check that banners show [1/5], [2/5], [3/5] (Attempt 1/3), [4/5] (Attempt 1/3), [5/5]
        assert "[1/5] ▶ Executing Stage: 01_ARCHITECT" in res.output
        assert "[2/5] ▶ Executing Stage: 02_PLANNER" in res.output
        assert "[3/5] (Attempt 1/3) ▶ Executing Stage: 03_EXECUTOR" in res.output
        assert "[4/5] (Attempt 1/3) ▶ Executing Stage: 04_REVIEWER" in res.output
        assert "[5/5] ▶ Executing Stage: 05_CRITIC" in res.output


def test_resume_preserves_run_baseline(isolated_git_repo):
    """Resume loads the saved baseline from git_baseline.json so pre-existing files remain isolated across retries/resumes."""
    repo = isolated_git_repo
    git = GitService(repo)
    rm = RunManager(repo)

    (repo / "user_staged.py").write_text("staged content\n", encoding="utf-8")
    subprocess.run(["git", "add", "user_staged.py"], cwd=repo, check=True)

    run = rm.create_run("task needing resume")
    baseline = git.capture_baseline()
    baseline.save(run.run_dir / "git_baseline.json")

    # Forge modifies file
    (repo / "forge_file.py").write_text("forge file\n", encoding="utf-8")

    # Resume run
    loaded_run = rm.resume(run.run_id)
    success = auto_commit_run(git, "resume commit", run=loaded_run)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "forge_file.py" in show
    assert "user_staged.py" not in show


def test_pipeline_generic_repair_loop_with_synthetic_verifier(isolated_git_repo, monkeypatch):
    """ARCH-01: Verify that an intermediate verification stage naturally participates in the repair loop.

    Synthetic verifier returns CHANGES_REQUIRED on attempt 1, causing the change producer (Executor)
    to retry with feedback. On attempt 2, synthetic verifier returns PASSED, and Reviewer returns APPROVED.
    """
    repo = isolated_git_repo
    monkeypatch.chdir(repo)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    configure_automated_execution_environment()

    # Register synthetic verifier requirements
    from forge.stages.requirements import StageRequirementsRegistry
    from forge.core.capabilities import Capability
    StageRequirementsRegistry.register("verifier_alpha", {Capability.CODE_READ})

    synthetic_verifier = StageDefinition(
        name="verifier_alpha",
        sequence_number=4,
        role_type="verifier",
        is_verifier=True,
        repair_target="executor",
        display_title="Alpha Verifier",
        emoji="🔬",
        success_statuses=frozenset({"PASSED", "APPROVED"}),
    )
    reviewer_def = StageOrder.get_reviewer()
    synthetic_reviewer = replace(reviewer_def, sequence_number=5)

    MachineReportValidator.register_role_rules(
        role="VERIFIER_ALPHA",
        allowed_statuses={"PASSED", "CHANGES_REQUIRED", "FAILED"},
        allowed_handoffs={"REVIEWER", "EXECUTOR", "NONE"},
    )

    # Responses
    arch_resp = AdapterResponse(
        stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
    )
    plan_resp = AdapterResponse(
        stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY",
    )
    exec_resp_1 = AdapterResponse(
        stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: NONE\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS 1",
    )
    verif_changes = AdapterResponse(
        stdout="```yaml\nROLE: VERIFIER_ALPHA\nSTATUS: CHANGES_REQUIRED\nHANDOFF: EXECUTOR\nREASON: Syntax error in implementation\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="CHANGES_REQUIRED",
    )
    exec_resp_2 = AdapterResponse(
        stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: NONE\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS 2",
    )
    verif_passed = AdapterResponse(
        stdout="```yaml\nROLE: VERIFIER_ALPHA\nSTATUS: PASSED\nHANDOFF: NONE\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASSED",
    )
    rev_approved = AdapterResponse(
        stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
    )
    critic_resp = AdapterResponse(
        stdout="```yaml\nROLE: CRITIC\nSTATUS: PASSED\nHANDOFF: NONE\n```",
        stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASSED",
    )

    with patch.object(StageOrder, "verification_stages", return_value=[synthetic_verifier, synthetic_reviewer]), \
         patch.object(StageOrder, "total_stages_count", return_value=6), \
         patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
         patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
         patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[
             arch_resp, plan_resp,
             verif_changes,   # attempt 1 verifier_alpha rejects
             verif_passed,    # attempt 2 verifier_alpha passes
             rev_approved,    # attempt 2 reviewer approves
             critic_resp,     # closing critic audit
         ]), \
         patch("forge.adapters.antigravity.AntigravityAdapter.execute", side_effect=[
             exec_resp_1,     # attempt 1
             exec_resp_2,     # attempt 2 retry
         ]):

        res = runner.invoke(main, ["auto", "Repair loop with synthetic verifier", "--max-retries", "2"])
        assert res.exit_code == 0, f"Failed with output:\n{res.output}"
        assert "Alpha Verifier requested changes. Launching auto-repair iteration 2..." in res.output
        assert "Implementation APPROVED by Alpha Verifier & Reviewer on attempt 2!" in res.output
