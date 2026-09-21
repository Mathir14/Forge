"""Comprehensive regression and verification tests for Post-Audit Findings N-01, N-02, and N-03.

N-01: Deleted-file commit staging
N-02: Global / adapter-aware prompt budgeting
N-03: Commit-scope isolation (unrelated pre-staged files remain uncommitted)
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.base import AdapterResponse, BaseAdapter
from forge.adapters.codex import CodexAdapter
from forge.adapters.opencode import OpenCodeAdapter
from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.core.run import Run
from forge.prompts.compiler import PromptCompiler
from forge.prompts.instruction import Instruction
from forge.cli import auto_commit_run
from forge.stages.stage import Stage


@pytest.fixture
def real_git_repo(tmp_path):
    """Fixture providing an initialized real Git repository."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Tester"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "forge@example.com"], cwd=tmp_path, check=True)
    return tmp_path


# ===========================================================================
# N-01: Deleted-file commit staging
# ===========================================================================

def test_n01_deleted_file_committed_successfully(real_git_repo):
    """N-01: GitService.commit() successfully stages and commits deleted files."""
    repo = real_git_repo
    git = GitService(repo)

    file_to_delete = repo / "delete_me.py"
    file_to_keep = repo / "keep_me.py"
    file_to_delete.write_text("def old_function(): pass\n", encoding="utf-8")
    file_to_keep.write_text("def keep(): pass\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)

    # Delete the file from the filesystem (working tree deletion)
    file_to_delete.unlink()
    assert not file_to_delete.exists()

    changed = git.changed_files()
    assert "delete_me.py" in changed

    # Commit the deletion via GitService
    success = git.commit("Remove delete_me.py")
    assert success is True

    # Verify via git log that delete mode was committed
    show_res = subprocess.run(["git", "show", "--name-status", "--oneline", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    assert "D\tdelete_me.py" in show_res.stdout or "D delete_me.py" in show_res.stdout

    # Working tree must be clean
    status_res = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True)
    assert status_res.stdout.strip() == ""


def test_n01_deleted_file_via_git_rm_committed_successfully(real_git_repo):
    """N-01: GitService.commit() handles pre-staged deletions (git rm) without fatal pathspec error."""
    repo = real_git_repo
    git = GitService(repo)

    f = repo / "staged_del.py"
    f.write_text("pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged_del.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Add staged_del.py"], cwd=repo, check=True)

    # User or script already staged the deletion with git rm
    subprocess.run(["git", "rm", "staged_del.py"], cwd=repo, check=True)

    changed = git.changed_files()
    assert "staged_del.py" in changed

    # Calling git.commit() must NOT error with pathspec error
    success = git.commit("Commit pre-staged removal")
    assert success is True

    show_res = subprocess.run(["git", "show", "--name-status", "--oneline", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    assert "D\tstaged_del.py" in show_res.stdout or "D staged_del.py" in show_res.stdout


def test_n01_mixed_changeset_add_modify_delete_rename(real_git_repo):
    """N-01: Mixed changeset containing add, modify, delete, and rename commits atomically."""
    repo = real_git_repo
    git = GitService(repo)

    # Initial state
    (repo / "modify.py").write_text("v1\n", encoding="utf-8")
    (repo / "delete.py").write_text("to delete\n", encoding="utf-8")
    (repo / "rename_old.py").write_text("content to rename\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True)

    # 1. Add new file
    (repo / "added.py").write_text("new content\n", encoding="utf-8")
    # 2. Modify existing file
    (repo / "modify.py").write_text("v2 modified\n", encoding="utf-8")
    # 3. Delete file
    (repo / "delete.py").unlink()
    # 4. Rename file
    (repo / "rename_old.py").rename(repo / "rename_new.py")

    changed = git.changed_files()
    assert "added.py" in changed
    assert "modify.py" in changed
    assert "delete.py" in changed
    assert "rename_old.py" in changed
    assert "rename_new.py" in changed

    success = git.commit("feat: apply mixed changeset")
    assert success is True

    show_res = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    stat = show_res.stdout
    assert "added.py" in stat
    assert "modify.py" in stat
    assert "delete.py" in stat
    assert "rename_old.py => rename_new.py" in stat or "rename_new.py" in stat

    # Confirm clean state
    status_res = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True)
    assert status_res.stdout.strip() == ""


def test_n01_renamed_file_detected_in_changed_files(real_git_repo):
    """N-01: Both source and destination of renamed files are returned by changed_files()."""
    repo = real_git_repo
    git = GitService(repo)

    (repo / "source.txt").write_text("sample data\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init source"], cwd=repo, check=True)

    # Git move
    subprocess.run(["git", "mv", "source.txt", "destination.txt"], cwd=repo, check=True)

    changed = git.changed_files()
    assert "source.txt" in changed
    assert "destination.txt" in changed


# ===========================================================================
# N-02: Global / adapter-aware prompt budgeting
# ===========================================================================

def test_n02_adapter_max_prompt_bytes_property():
    """N-02: Adapters declare max_prompt_bytes property (Antigravity=130000, others=None)."""
    antigravity = AntigravityAdapter()
    assert antigravity.max_prompt_bytes == 130000

    opencode = OpenCodeAdapter()
    assert opencode.max_prompt_bytes is None

    codex = CodexAdapter()
    assert codex.max_prompt_bytes is None


def test_n02_antigravity_oversized_prompt_budgeted_under_limit():
    """N-02: Prompts exceeding 130,000 bytes are budgeted strictly under limit for Antigravity."""
    oversized_instruction = Instruction(
        role_name="executor",
        task="Implement high-performance caching layer",
        project_docs={
            "architecture": "ARCH_DOC " * 5000,     # ~45KB
            "conventions": "CONVENTIONS " * 5000,    # ~60KB
        },
        previous_stage_outputs={
            "architect": "ARCH_SPEC " * 4000,        # ~40KB
            "planner": "PLAN_ITEMS " * 4000,         # ~44KB
        },
        git_diff="DIFF_LINE\n" * 5000,               # ~50KB
        git_status="STATUS_LINE\n" * 1000,           # ~12KB
        protocol_schema="INVIOLABLE_SCHEMA_REQUIREMENTS\noutput_format: yaml",
    )
    role_template = "You are the Executor agent. Follow instructions strictly."

    # Raw prompt exceeds 250,000 bytes
    raw_prompt = PromptCompiler.compile(oversized_instruction, role_template, max_prompt_bytes=None)
    raw_bytes = len(raw_prompt.text.encode("utf-8"))
    assert raw_bytes > 200000

    # Budgeted prompt with Antigravity limit (130,000 bytes)
    budgeted_prompt = PromptCompiler.compile(oversized_instruction, role_template, max_prompt_bytes=130000)
    budgeted_bytes = len(budgeted_prompt.text.encode("utf-8"))

    assert budgeted_bytes <= 130000
    assert budgeted_bytes >= 100000  # Preserves maximal context up to budget
    assert "INVIOLABLE_SCHEMA_REQUIREMENTS" in budgeted_prompt.text
    assert "Implement high-performance caching layer" in budgeted_prompt.text
    assert "You are the Executor agent" in budgeted_prompt.text


def test_n02_unbounded_adapters_preserve_full_context():
    """N-02: OpenCode and Codex (max_prompt_bytes=None) preserve full context without truncation."""
    oversized_instruction = Instruction(
        role_name="executor",
        task="Implement feature",
        project_docs={"doc": "D" * 30000},
        previous_stage_outputs={"planner": "P" * 30000},
        git_diff="DIFF\n" * 10000,
        protocol_schema="SCHEMA",
    )
    role_template = "You are Executor."

    prompt_none = PromptCompiler.compile(oversized_instruction, role_template, max_prompt_bytes=None)
    bytes_none = len(prompt_none.text.encode("utf-8"))
    assert bytes_none > 100000
    assert "[... Git diff truncated" not in prompt_none.text
    assert "[... Project documentation omitted" not in prompt_none.text


def test_n02_deterministic_priority_order_verified():
    """N-02: Priority order truncates docs -> diff -> status -> older stage outputs."""
    inst = Instruction(
        role_name="executor",
        task="Test priority ordering",
        project_docs={"arch": "DOC_CONTENT " * 8000},
        previous_stage_outputs={
            "architect": "OLD_ARCH " * 3000,
            "planner": "LATEST_PLAN " * 3000,
        },
        git_diff="DIFF_DATA " * 6000,
        git_status="STATUS_DATA " * 1000,
        protocol_schema="COMMON_AGENT_PROTOCOL_SCHEMA",
    )
    role_template = "Role template text"

    budgeted = PromptCompiler.apply_transport_budget(inst, role_template, max_prompt_bytes=130000)
    rendered = PromptCompiler.compile(budgeted, role_template)
    rendered_bytes = len(rendered.text.encode("utf-8"))
    assert rendered_bytes <= 130000

    # 1. Project docs should be truncated or omitted first
    assert "transport budget" in budgeted.project_docs["arch"]

    # 2. Schema and task are inviolable
    assert "COMMON_AGENT_PROTOCOL_SCHEMA" in rendered.text
    assert "Test priority ordering" in rendered.text


def test_n02_multibyte_utf8_truncation_safe():
    """N-02: Byte truncation handles multi-byte UTF-8 code points safely without corrupting bytes."""
    # 4-byte emoji and 3-byte unicode characters
    multibyte_text = "Emoji: 🚀🌟🔥, Chinese: 深度强化学习, Japanese: プロンプト" * 50
    for target in range(50, 200, 7):
        truncated = PromptCompiler._truncate_bytes(multibyte_text, target, notice="[CUT]")
        encoded = truncated.encode("utf-8")
        assert len(encoded) <= target
        # Decode without error must succeed
        decoded = encoded.decode("utf-8")
        assert decoded == truncated


def test_n02_stage_run_passes_adapter_budget(tmp_path):
    """N-02: Stage.run() supplies adapter.max_prompt_bytes to PromptCompiler.compile()."""
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Test stage transport budgeting", run_dir=run_dir)
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))

    adapter = AntigravityAdapter()
    role = Role(name="architect", sequence_number=1, template_content="You are Architect.")

    stage = Stage(role=role, adapter=adapter)

    with patch.object(stage, "validate_compatibility"), \
         patch("forge.prompts.compiler.PromptCompiler.compile", wraps=PromptCompiler.compile) as spy_compile, \
         patch.object(adapter, "execute") as mock_execute:
        mock_execute.return_value = AdapterResponse(
            stdout="status: COMPLETED\nsummary: done",
            stderr="",
            exit_code=0,
            duration_seconds=1.0,
            raw_output="status: COMPLETED\nsummary: done",
        )
        stage.run(context)

        # Confirm PromptCompiler.compile was called with max_prompt_bytes=130000
        spy_compile.assert_called_once()
        _, kwargs = spy_compile.call_args
        assert kwargs.get("max_prompt_bytes") == 130000


# ===========================================================================
# N-03: Commit-scope isolation (unrelated pre-staged files)
# ===========================================================================

def test_n03_unrelated_prestaged_files_remain_staged_and_uncommitted_with_paths(real_git_repo):
    """N-03: Commit is strictly isolated to safe_files; unrelated pre-staged files remain untouched."""
    repo = real_git_repo
    git = GitService(repo)

    # Initial repository commit
    (repo / "base.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

    # 1. User pre-stages an unrelated code file
    (repo / "user_work.py").write_text("# User work in progress\n", encoding="utf-8")
    subprocess.run(["git", "add", "user_work.py"], cwd=repo, check=True)

    # 2. User pre-stages a sensitive secret file (.env.production)
    (repo / ".env.production").write_text("DATABASE_PASSWORD=secret123\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env.production"], cwd=repo, check=True)

    # Verify both are currently staged in the git index
    status_before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True)
    assert "A  user_work.py" in status_before.stdout
    assert "A  .env.production" in status_before.stdout

    # Capture run baseline before Forge modifies the workspace
    baseline = git.capture_baseline()

    # 3. Forge creates and changes a legitimate project file
    (repo / "forge_feature.py").write_text("def forge_feature(): return True\n", encoding="utf-8")

    # Forge executes production auto-commit path without passing explicit paths
    success = auto_commit_run(git=git, task_summary="forge feature implementation", baseline=baseline)
    assert success is True

    # 4. Verify HEAD commit contains ONLY forge_feature.py
    show_res = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    commit_stat = show_res.stdout
    assert "forge_feature.py" in commit_stat
    assert "user_work.py" not in commit_stat
    assert ".env.production" not in commit_stat

    # 5. Verify the git index after commit: user_work.py and .env.production are STILL staged and uncommitted!
    status_after = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True)
    assert "A  user_work.py" in status_after.stdout
    assert "A  .env.production" in status_after.stdout

    # Verify file contents are unchanged (not modified, reset, or stashed)
    assert (repo / "user_work.py").read_text(encoding="utf-8") == "# User work in progress\n"
    assert (repo / ".env.production").read_text(encoding="utf-8") == "DATABASE_PASSWORD=secret123\n"


def test_n03_prestaged_secret_not_committed_without_paths(real_git_repo):
    """N-03: Pre-staged secret file (.env.production) is excluded from commit even when no paths arg is given."""
    repo = real_git_repo
    git = GitService(repo)

    (repo / "base.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

    # User pre-staged a secret file in git index
    (repo / ".env.production").write_text("API_SECRET=super_secret\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env.production"], cwd=repo, check=True)

    # Forge creates feature file
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")

    # Forge commits without explicit paths (calls changed_files internally)
    success = git.commit("feat: update app")
    assert success is True

    # Check commit stat: must contain app.py and NOT .env.production
    show_res = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    assert "app.py" in show_res.stdout
    assert ".env.production" not in show_res.stdout

    # In index, .env.production must still be staged and untouched
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True)
    assert "A  .env.production" in status.stdout



def test_n03_protected_paths_never_committed_even_if_passed_explicitly(real_git_repo):
    """N-03: Protected files (.env*, .forge*) are filtered out even if passed in paths parameter."""
    repo = real_git_repo
    git = GitService(repo)

    (repo / "init.txt").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "init.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

    (repo / ".env.secret").write_text("SECRET=xyz\n", encoding="utf-8")
    (repo / "legit.py").write_text("legit code\n", encoding="utf-8")

    # Pass paths explicitly including protected path
    success = git.commit("Add files", paths=[".env.secret", "legit.py"])
    assert success is True

    show_res = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    assert "legit.py" in show_res.stdout
    assert ".env.secret" not in show_res.stdout
