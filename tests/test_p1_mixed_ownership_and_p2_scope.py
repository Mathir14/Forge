"""Comprehensive behavioral regression test suite for:
- P1: Same-file mixed-ownership commit attribution (fail-safe exclusion from auto-commit, user WIP preserved, warning emitted)
- P2: Run-scoped Reviewer and Critic Git context (unrelated pre-existing user files excluded, mixed files explicitly attributed)
- SEC-01 & ARCH-01 preservation
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from forge.cli import auto_commit_run, main
from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitBaseline, GitService, ChangeAttribution
from forge.core.role import Role
from forge.core.run import Run
from forge.prompts.builder import InstructionBuilder
from forge.prompts.compiler import PromptCompiler
from forge.prompts.instruction import Instruction
from forge.stages.definition import StageOrder
from forge.storage.run_manager import RunManager


@pytest.fixture
def fresh_repo(tmp_path):
    """Fixture providing an initialized Git repository with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "P1P2 Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "p1p2@forge.dev"], cwd=repo, check=True)
    (repo / "base.txt").write_text("initial base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True)
    return repo


# ==============================================================================
# PART 1: P1 Same-File Mixed Ownership Attribution & Safety Tests
# ==============================================================================


def test_mixed_ownership_unstaged_user_edit(fresh_repo, capsys):
    """P1: File has unstaged user edits at baseline; Forge modifies it further.
    
    Must be classified as mixed ownership, excluded from auto-commit, warning emitted,
    and user working-tree content preserved intact.
    """
    repo = fresh_repo
    git = GitService(repo)

    (repo / "module.py").write_text("def module(): return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add module"], cwd=repo, check=True)

    # 1. User makes pre-existing unstaged modifications
    (repo / "module.py").write_text("def module(): return 'USER_WIP'\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # 2. Forge modifies the same file further during run
    (repo / "module.py").write_text("def module(): return 'USER_WIP'\n# FORGE_ADDITION\n", encoding="utf-8")

    attr = git.attribute_changes(baseline)
    assert "module.py" in attr.mixed_ownership_changes
    assert "module.py" not in attr.pure_forge_changes

    # 3. auto_commit_run must fail safe
    success = auto_commit_run(git, "forge task", baseline=baseline)
    assert success is False

    captured = capsys.readouterr()
    assert "Skipped auto-commit for mixed-ownership file(s):" in captured.out
    assert "module.py" in captured.out

    # 4. Working tree preserved intact and NOT committed
    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "forge task" not in show
    assert (repo / "module.py").read_text(encoding="utf-8") == "def module(): return 'USER_WIP'\n# FORGE_ADDITION\n"

    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert " M module.py" in status


def test_mixed_ownership_staged_user_edit(fresh_repo, capsys):
    """P1: File has staged user edits at baseline; Forge modifies it in working tree.
    
    Must be classified as mixed ownership, excluded from auto-commit, and staged WIP preserved.
    """
    repo = fresh_repo
    git = GitService(repo)

    (repo / "staged_shared.py").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged_shared.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add staged_shared"], cwd=repo, check=True)

    # User modifies and stages
    (repo / "staged_shared.py").write_text("v2_user_staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged_shared.py"], cwd=repo, check=True)
    baseline = git.capture_baseline()

    # Forge modifies in working tree
    (repo / "staged_shared.py").write_text("v2_user_staged\nv3_forge_worktree\n", encoding="utf-8")

    attr = git.attribute_changes(baseline)
    assert "staged_shared.py" in attr.mixed_ownership_changes
    assert "staged_shared.py" not in attr.pure_forge_changes

    success = auto_commit_run(git, "forge task", baseline=baseline)
    assert success is False

    captured = capsys.readouterr()
    assert "Skipped auto-commit for mixed-ownership file(s):" in captured.out
    assert "staged_shared.py" in captured.out

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "forge task" not in show


def test_mixed_ownership_disjoint_regional_edits(fresh_repo):
    """P1: User edits line 1; Forge edits line 50. Committing entire file would commit line 1.
    
    Must be classified as mixed ownership and excluded from auto-commit.
    """
    repo = fresh_repo
    git = GitService(repo)

    lines = [f"line_{i}\n" for i in range(1, 60)]
    (repo / "long_file.py").write_text("".join(lines), encoding="utf-8")
    subprocess.run(["git", "add", "long_file.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add long_file"], cwd=repo, check=True)

    # User edits line 1
    lines[0] = "USER_MODIFIED_LINE_1\n"
    (repo / "long_file.py").write_text("".join(lines), encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge edits line 50
    lines[49] = "FORGE_MODIFIED_LINE_50\n"
    (repo / "long_file.py").write_text("".join(lines), encoding="utf-8")

    attr = git.attribute_changes(baseline)
    assert "long_file.py" in attr.mixed_ownership_changes
    assert "long_file.py" not in attr.pure_forge_changes

    success = auto_commit_run(git, "update long file", baseline=baseline)
    assert success is False

    # Confirm HEAD does NOT contain long_file.py
    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "update long file" not in show


def test_mixed_ownership_overlapping_line_edit(fresh_repo):
    """P1: Forge overwrites the user's WIP line.
    
    Must be classified as mixed ownership and excluded from auto-commit.
    """
    repo = fresh_repo
    git = GitService(repo)

    (repo / "config.py").write_text("TIMEOUT = 10\n", encoding="utf-8")
    subprocess.run(["git", "add", "config.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add config"], cwd=repo, check=True)

    # User edits TIMEOUT
    (repo / "config.py").write_text("TIMEOUT = 'USER_EXPERIMENT'\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge overwrites TIMEOUT
    (repo / "config.py").write_text("TIMEOUT = 300\n", encoding="utf-8")

    attr = git.attribute_changes(baseline)
    assert "config.py" in attr.mixed_ownership_changes
    assert "config.py" not in attr.pure_forge_changes

    success = auto_commit_run(git, "set timeout", baseline=baseline)
    assert success is False


def test_pure_forge_ownership_commits_cleanly(fresh_repo):
    """Pure Forge changes (clean baseline, modified/added by Forge) commit successfully."""
    repo = fresh_repo
    git = GitService(repo)

    (repo / "clean.py").write_text("def clean(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "clean.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add clean"], cwd=repo, check=True)

    baseline = git.capture_baseline()

    # Forge modifies clean.py and adds new.py
    (repo / "clean.py").write_text("def clean(): return 'forge_update'\n", encoding="utf-8")
    (repo / "new.py").write_text("def new(): return True\n", encoding="utf-8")

    attr = git.attribute_changes(baseline)
    assert set(attr.pure_forge_changes) == {"clean.py", "new.py"}
    assert attr.mixed_ownership_changes == []

    success = auto_commit_run(git, "pure forge update", baseline=baseline)
    assert success is True

    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "clean.py" in show
    assert "new.py" in show


def test_mixed_and_pure_forge_combined_skips_only_mixed(fresh_repo, capsys):
    """When both pure Forge changes and mixed-ownership changes exist:
    
    Pure Forge changes commit cleanly; mixed-ownership file is excluded and preserved.
    """
    repo = fresh_repo
    git = GitService(repo)

    (repo / "shared.py").write_text("shared_v1\n", encoding="utf-8")
    (repo / "pure.py").write_text("pure_v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base files"], cwd=repo, check=True)

    # User modifies shared.py before Forge
    (repo / "shared.py").write_text("shared_user_wip\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge modifies pure.py AND shared.py
    (repo / "pure.py").write_text("pure_v2_forge\n", encoding="utf-8")
    (repo / "shared.py").write_text("shared_user_wip\nshared_forge_addition\n", encoding="utf-8")

    attr = git.attribute_changes(baseline)
    assert attr.pure_forge_changes == ["pure.py"]
    assert attr.mixed_ownership_changes == ["shared.py"]

    success = auto_commit_run(git, "implement pure feature", baseline=baseline)
    assert success is True

    captured = capsys.readouterr()
    assert "⚠️ Skipped auto-commit for mixed-ownership file(s):" in captured.out
    assert "shared.py" in captured.out

    # Verify HEAD contains ONLY pure.py
    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "pure.py" in show
    assert "shared.py" not in show

    # Verify shared.py remains uncommitted in working tree
    assert (repo / "shared.py").read_text(encoding="utf-8") == "shared_user_wip\nshared_forge_addition\n"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert " M shared.py" in status


# ==============================================================================
# PART 2: P2 Reviewer and Critic Scope Tests
# ==============================================================================


def test_reviewer_scope_test_a_single_unrelated_file(fresh_repo):
    """P2 Test A: B.py is pre-existing dirty file; Forge changes A.py.
    
    Reviewer prompt must contain A.py and must NOT contain B.py in changed_files,
    git_status, git_diff, or rendered prompt.
    """
    repo = fresh_repo
    git = GitService(repo)

    (repo / "b.py").write_text("def b(): return 'base'\n", encoding="utf-8")
    (repo / "a.py").write_text("def a(): return 'base'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init a and b"], cwd=repo, check=True)

    # User modifies b.py before Forge run
    (repo / "b.py").write_text("def b(): return 'USER_UNRELATED_SECRET'\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge modifies a.py
    (repo / "a.py").write_text("def a(): return 'FORGE_IMPLEMENTATION'\n", encoding="utf-8")

    run_dir = repo / ".forge" / "runs" / "run-p2-test-a"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-p2-test-a", task="Implement feature A", run_dir=run_dir)

    context = Context(run=run, project_root=repo, config=Config.default(), git=git, baseline=baseline)
    role = Role.load("reviewer", project_root=repo)

    instruction = InstructionBuilder.build(context, role)
    prompt = PromptCompiler.compile(instruction, role.template_content)

    # 1. Check Instruction attributes
    assert "a.py" in instruction.changed_files
    assert "b.py" not in instruction.changed_files
    assert "b.py" not in instruction.mixed_files
    assert "USER_UNRELATED_SECRET" not in (instruction.git_diff or "")
    assert "FORGE_IMPLEMENTATION" in (instruction.git_diff or "")

    # 2. Check compiled prompt text
    assert "a.py" in prompt.text
    assert "FORGE_IMPLEMENTATION" in prompt.text
    assert "b.py" not in prompt.text
    assert "USER_UNRELATED_SECRET" not in prompt.text


def test_reviewer_scope_test_b_multiple_unrelated_user_files(fresh_repo):
    """P2 Test B: Multiple unrelated pre-existing user files (staged, unstaged, untracked).
    
    All must be excluded from Reviewer context.
    """
    repo = fresh_repo
    git = GitService(repo)

    (repo / "user1.py").write_text("u1\n", encoding="utf-8")
    (repo / "user2.py").write_text("u2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init user files"], cwd=repo, check=True)

    # User edits user1 (unstaged), user2 (staged), user3 (untracked)
    (repo / "user1.py").write_text("user1_wip\n", encoding="utf-8")
    (repo / "user2.py").write_text("user2_staged_wip\n", encoding="utf-8")
    subprocess.run(["git", "add", "user2.py"], cwd=repo, check=True)
    (repo / "user3_scratch.py").write_text("user3_scratchpad\n", encoding="utf-8")

    baseline = git.capture_baseline()

    # Forge creates feature.py
    (repo / "feature.py").write_text("def forge_feature(): pass\n", encoding="utf-8")

    run_dir = repo / ".forge" / "runs" / "run-p2-test-b"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-p2-test-b", task="Add feature", run_dir=run_dir)
    context = Context(run=run, project_root=repo, config=Config.default(), git=git, baseline=baseline)
    role = Role.load("reviewer", project_root=repo)

    instruction = InstructionBuilder.build(context, role)
    prompt = PromptCompiler.compile(instruction, role.template_content)

    assert "feature.py" in prompt.text
    assert "user1.py" not in prompt.text
    assert "user2.py" not in prompt.text
    assert "user3_scratch.py" not in prompt.text


def test_reviewer_scope_test_c_prestaged_user_source_file(fresh_repo):
    """P2 Test C: Pre-existing staged user source file must not appear in Reviewer context."""
    repo = fresh_repo
    git = GitService(repo)

    (repo / "prestaged.py").write_text("# user prestaged\n", encoding="utf-8")
    subprocess.run(["git", "add", "prestaged.py"], cwd=repo, check=True)
    baseline = git.capture_baseline()

    # Forge creates forge_task.py
    (repo / "forge_task.py").write_text("# forge work\n", encoding="utf-8")

    run_dir = repo / ".forge" / "runs" / "run-p2-test-c"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-p2-test-c", task="Run task", run_dir=run_dir)
    context = Context(run=run, project_root=repo, config=Config.default(), git=git, baseline=baseline)
    role = Role.load("reviewer", project_root=repo)

    instruction = InstructionBuilder.build(context, role)
    prompt = PromptCompiler.compile(instruction, role.template_content)

    assert "forge_task.py" in prompt.text
    assert "prestaged.py" not in prompt.text


def test_reviewer_scope_test_d_preexisting_untracked_user_file(fresh_repo):
    """P2 Test D: Pre-existing untracked user file must not appear in Reviewer context."""
    repo = fresh_repo
    git = GitService(repo)

    (repo / "notes.txt").write_text("User scratch notes\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge modifies tracked file
    (repo / "base.txt").write_text("initial base\nforge update\n", encoding="utf-8")

    run_dir = repo / ".forge" / "runs" / "run-p2-test-d"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-p2-test-d", task="Update base", run_dir=run_dir)
    context = Context(run=run, project_root=repo, config=Config.default(), git=git, baseline=baseline)
    role = Role.load("reviewer", project_root=repo)

    instruction = InstructionBuilder.build(context, role)
    prompt = PromptCompiler.compile(instruction, role.template_content)

    assert "base.txt" in prompt.text
    assert "notes.txt" not in prompt.text


def test_reviewer_scope_test_e_preexisting_deleted_file(fresh_repo):
    """P2 Test E: Pre-existing deleted file must not appear in Reviewer context."""
    repo = fresh_repo
    git = GitService(repo)

    (repo / "delete_me.py").write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "add", "delete_me.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add delete_me"], cwd=repo, check=True)

    # User deletes the file before Forge run
    (repo / "delete_me.py").unlink()
    baseline = git.capture_baseline()

    # Forge creates new_service.py
    (repo / "new_service.py").write_text("class NewService: pass\n", encoding="utf-8")

    run_dir = repo / ".forge" / "runs" / "run-p2-test-e"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-p2-test-e", task="Add service", run_dir=run_dir)
    context = Context(run=run, project_root=repo, config=Config.default(), git=git, baseline=baseline)
    role = Role.load("reviewer", project_root=repo)

    instruction = InstructionBuilder.build(context, role)
    prompt = PromptCompiler.compile(instruction, role.template_content)

    assert "new_service.py" in prompt.text
    assert "delete_me.py" not in prompt.text


def test_reviewer_scope_test_f_mixed_ownership_file(fresh_repo):
    """P2 Test F: Mixed-ownership file must be explicitly identified in Reviewer prompt.
    
    Reviewer must see the ambiguity clearly and understand that the file cannot be auto-committed.
    """
    repo = fresh_repo
    git = GitService(repo)

    (repo / "shared_app.py").write_text("def app(): return 'v1'\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared_app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add shared_app"], cwd=repo, check=True)

    # User modifies before run
    (repo / "shared_app.py").write_text("def app(): return 'v2_user'\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge modifies further
    (repo / "shared_app.py").write_text("def app(): return 'v2_user'\n# forge edit\n", encoding="utf-8")

    run_dir = repo / ".forge" / "runs" / "run-p2-test-f"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-p2-test-f", task="Update app", run_dir=run_dir)
    context = Context(run=run, project_root=repo, config=Config.default(), git=git, baseline=baseline)
    role = Role.load("reviewer", project_root=repo)

    instruction = InstructionBuilder.build(context, role)
    prompt = PromptCompiler.compile(instruction, role.template_content)

    assert "shared_app.py" in instruction.mixed_files
    assert "Mixed-Ownership Files" in prompt.text
    assert "shared_app.py" in prompt.text
    assert "excluded from automatic commit" in prompt.text


def test_closing_critic_scope_uses_run_baseline(fresh_repo):
    """P2: Closing Critic prompt is run-scoped and excludes unrelated pre-existing user work."""
    repo = fresh_repo
    git = GitService(repo)

    (repo / "unrelated.py").write_text("unrelated base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add unrelated"], cwd=repo, check=True)

    # User modifies unrelated.py before run
    (repo / "unrelated.py").write_text("unrelated modified by user\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge modifies base.txt
    (repo / "base.txt").write_text("base updated by forge\n", encoding="utf-8")

    run_dir = repo / ".forge" / "runs" / "run-critic-scope"
    run_dir.mkdir(parents=True)
    baseline.save(run_dir / "git_baseline.json")

    run = Run(run_id="run-critic-scope", task="Update base", run_dir=run_dir)
    context = Context(run=run, project_root=repo, config=Config.default(), git=git, baseline=baseline)
    role = Role.load("critic", project_root=repo, sequence_number=5, phase="post_run")

    instruction = InstructionBuilder.build(context, role)
    prompt = PromptCompiler.compile(instruction, role.template_content)

    assert "base.txt" in prompt.text
    assert "unrelated.py" not in prompt.text


def test_prerun_critic_without_baseline_observes_repo_wide(fresh_repo):
    """P2: Pre-run Critic with baseline=None observes repository-wide context for codebase audit."""
    repo = fresh_repo
    git = GitService(repo)

    (repo / "dirty_code.py").write_text("def broken(): return 1\n", encoding="utf-8")

    run_dir = repo / ".forge" / "runs" / "run-prerun-critic"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-prerun-critic", task="Audit codebase", run_dir=run_dir)
    context = Context(run=run, project_root=repo, config=Config.default(), git=git, baseline=None)
    role = Role.load("critic", project_root=repo, sequence_number=0, phase="pre_run")

    instruction = InstructionBuilder.build(context, role)
    prompt = PromptCompiler.compile(instruction, role.template_content)

    # Pre-run Critic must observe repository-wide dirty code
    assert "dirty_code.py" in prompt.text


# ==============================================================================
# PART 3: End-to-End Production Path Scenarios
# ==============================================================================


def test_production_auto_commit_prestaged_source_and_secret(fresh_repo):
    """PART 14: Exercises the actual production auto_commit_run with pre-staged user code and secret."""
    repo = fresh_repo
    git = GitService(repo)

    (repo / "user_staged.py").write_text("# User WIP\n", encoding="utf-8")
    subprocess.run(["git", "add", "user_staged.py"], cwd=repo, check=True)

    (repo / ".env.production").write_text("SECRET=super_secret\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env.production"], cwd=repo, check=True)

    baseline = git.capture_baseline()

    # Forge creates new_service.py
    (repo / "new_service.py").write_text("class NewService: pass\n", encoding="utf-8")

    success = auto_commit_run(git=git, task_summary="add new service", baseline=baseline)
    assert success is True

    # 1. Only new_service.py is committed to HEAD
    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "new_service.py" in show
    assert "user_staged.py" not in show
    assert ".env.production" not in show

    # 2. Both user files remain staged and uncommitted in git index
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "A  user_staged.py" in status
    assert "A  .env.production" in status


def test_production_auto_commit_same_file_mixed_safety(fresh_repo):
    """PART 15: Production-path scenario where A.py has user WIP before Forge.
    
    Forge modifies A.py. Auto-commit must NOT commit A.py to HEAD.
    """
    repo = fresh_repo
    git = GitService(repo)

    (repo / "app.py").write_text("def app(): return 'v1'\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init app"], cwd=repo, check=True)

    # User modifies app.py before Forge
    (repo / "app.py").write_text("def app(): return 'USER_WIP'\n", encoding="utf-8")
    baseline = git.capture_baseline()

    # Forge modifies app.py further
    (repo / "app.py").write_text("def app(): return 'USER_WIP'\n# FORGE_NEW_FEATURE\n", encoding="utf-8")

    success = auto_commit_run(git=git, task_summary="update app", baseline=baseline)
    # Must be skipped because it is mixed-ownership
    assert success is False

    # HEAD commit must NOT contain "update app" or app.py
    show = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "update app" not in show

    # Working tree must be preserved
    assert (repo / "app.py").read_text(encoding="utf-8") == "def app(): return 'USER_WIP'\n# FORGE_NEW_FEATURE\n"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert " M app.py" in status
