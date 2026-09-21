"""Focused tests for Diff-First Reviewer context, Executor claim verification, and diff budgets."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.core.run import Run
from forge.prompts.builder import InstructionBuilder
from forge.prompts.compiler import PromptCompiler
from forge.prompts.instruction import Instruction
from forge.storage.run_manager import RunManager


def test_reviewer_diff_first_context_structure(tmp_path):
    """Verify Reviewer prompt context follows the change-centric, diff-first structure."""
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Add rate limiting to auth endpoint", run_dir=run_dir)

    # Save executor report
    (run_dir / "03_executor.md").write_text(
        "# Executor Implementation Report\n\n"
        "Claims:\n"
        "- Implemented token bucket rate limiter\n"
        "- Added 429 Too Many Requests response handling\n"
        "- Added unit tests in tests/test_rate_limiter.py\n"
        "- All tests pass without regressions\n",
        encoding="utf-8",
    )

    git = GitService(tmp_path)
    mock_status = " M src/auth.py\n?? tests/test_rate_limiter.py\n"
    mock_diff = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -10,3 +10,8 @@\n"
        "+def check_rate_limit(ip: str):\n"
        "+    pass  # rate limit check\n"
    )

    with patch.object(git, "is_git_repo", return_value=True), \
         patch.object(git, "status", return_value=mock_status), \
         patch.object(git, "diff", return_value=mock_diff), \
         patch.object(git, "changed_files", return_value=["src/auth.py", "tests/test_rate_limiter.py"]), \
         patch.object(git, "changed_files_summary", return_value=["M src/auth.py", "A tests/test_rate_limiter.py"]):

        context = Context(run=run, project_root=tmp_path, config=Config.default(), git=git)
        role = Role.load("reviewer", project_root=tmp_path)
        instruction = InstructionBuilder.build(context, role)
        prompt = PromptCompiler.compile(instruction, role.template_content)

    # Check that the Reviewer prompt contains all key sections in change-centric order
    text = prompt.text

    assert "## Original Requirements" in text
    assert "Add rate limiting to auth endpoint" in text

    assert "## Executor Report" in text
    assert "Implemented token bucket rate limiter" in text
    assert "Added unit tests in tests/test_rate_limiter.py" in text

    assert "## Git Status" in text
    assert "M src/auth.py" in text
    assert "?? tests/test_rate_limiter.py" in text

    assert "## Changed Files" in text
    assert "M src/auth.py" in text
    assert "A tests/test_rate_limiter.py" in text

    assert "## Git Diff" in text
    assert "```diff" in text
    assert "diff --git a/src/auth.py" in text

    assert "## Review Instructions" in text
    assert "Treat the Executor as a change producer and verify all claims against reality" in text

    assert "## Repository Access" in text
    assert "The repository is available for inspection" in text

    # Verify section ordering: Original Requirements -> Executor Report -> Git Status -> Changed Files -> Git Diff -> Review Instructions -> Repository Access
    req_pos = text.index("## Original Requirements")
    exec_pos = text.index("## Executor Report")
    status_pos = text.index("## Git Status")
    changed_pos = text.index("## Changed Files")
    diff_pos = text.index("## Git Diff")
    instr_pos = text.index("## Review Instructions")
    repo_pos = text.index("## Repository Access")

    assert req_pos < exec_pos < status_pos < changed_pos < diff_pos < instr_pos < repo_pos


def test_reviewer_receives_both_executor_report_and_git_diff(tmp_path):
    """Verify Reviewer receives BOTH Executor claims and actual Git diff to detect discrepancies."""
    inst = Instruction(
        role_name="reviewer",
        task="Implement timeout argument in HTTP client",
        executor_report="I have implemented timeout=30 in all HTTP client calls and added timeout tests.",
        git_diff="diff --git a/client.py b/client.py\n+def get(url, timeout=30):\n+    return requests.get(url)",  # timeout argument is not passed to requests.get
        git_status=" M client.py",
        changed_file_summary="M client.py",
        changed_files=["client.py"],
        protocol_schema="ROLE: REVIEWER\nSTATUS: APPROVED | CHANGES_REQUIRED",
    )
    role_template = "You are a senior reviewer."
    prompt = PromptCompiler.compile(inst, role_template)

    # Both must be present in the prompt
    assert "I have implemented timeout=30" in prompt.text
    assert "diff --git a/client.py" in prompt.text

    # Review instructions must explicitly command comparing claims against diff
    assert "Compare each claim in the Executor Report directly against the Git diff and repository state" in prompt.text
    assert "Flag claims that are unsubstantiated, partially implemented, or contradicted by the diff" in prompt.text


def test_untracked_files_represented_in_reviewer_context(tmp_path):
    """Verify an Executor-created untracked file is represented in Git status, changed files, and unified diff."""
    git = GitService(tmp_path)

    # Create an existing file and a newly added untracked file
    existing_file = tmp_path / "existing.py"
    existing_file.write_text("def existing(): pass\n", encoding="utf-8")

    untracked_file = tmp_path / "new_module.py"
    untracked_file.write_text("def new_feature():\n    return True\n", encoding="utf-8")

    mock_status = "?? new_module.py\n"
    mock_untracked_diff = (
        "diff --git a//dev/null b/new_module.py\n"
        "--- /dev/null\n"
        "+++ b/new_module.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def new_feature():\n"
        "+    return True\n"
    )

    def mock_run(args, **kwargs):
        if "-c" in args and "status" in args:
            return MagicMock(returncode=0, stdout=mock_status, stderr="")
        elif args == ["status", "--short"]:
            return MagicMock(returncode=0, stdout="?? new_module.py\n", stderr="")
        elif "diff" in args and "/dev/null" in args:
            return MagicMock(returncode=1, stdout=mock_untracked_diff, stderr="")
        elif args == ["diff"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        elif args == ["rev-parse", "--is-inside-work-tree"]:
            return MagicMock(returncode=0, stdout="true\n", stderr="")
        elif args == ["rev-parse", "--show-toplevel"]:
            return MagicMock(returncode=0, stdout=str(tmp_path) + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Create new module", run_dir=run_dir)

    with patch.object(git, "_run", side_effect=mock_run):
        # 1. Git service includes untracked files in diff
        full_diff = git.diff(include_untracked=True)
        assert "diff --git a//dev/null b/new_module.py" in full_diff
        assert "+def new_feature():" in full_diff

        # 2. Changed files summary classifies untracked file as A
        summary = git.changed_files_summary()
        assert any("new_module.py" in s for s in summary)

        # 3. InstructionBuilder and PromptCompiler include untracked file in Reviewer prompt
        context = Context(run=run, project_root=tmp_path, config=Config.default(), git=git)
        role = Role.load("reviewer", project_root=tmp_path)
        instruction = InstructionBuilder.build(context, role)
        prompt = PromptCompiler.compile(instruction, role.template_content)

        assert "new_module.py" in prompt.text
        assert "diff --git a//dev/null b/new_module.py" in prompt.text
        assert "+def new_feature():" in prompt.text


def test_reviewer_diff_budget_bounds_large_diff(tmp_path):
    """Verify that huge diffs remain bounded by the existing budget without prompt explosion."""
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Bulk file refactor", run_dir=run_dir)
    git = GitService(tmp_path)

    # 150,000 chars of diff
    massive_diff = "diff --git a/large.py b/large.py\n" + ("+" + ("x" * 100) + "\n") * 1500
    assert len(massive_diff) > 100000

    mock_summary = ["M large.py", "M other.py"]

    with patch.object(git, "is_git_repo", return_value=True), \
         patch.object(git, "diff", return_value=massive_diff), \
         patch.object(git, "status", return_value=" M large.py\n M other.py\n"), \
         patch.object(git, "changed_files", return_value=["large.py", "other.py"]), \
         patch.object(git, "changed_files_summary", return_value=mock_summary):

        context = Context(run=run, project_root=tmp_path, config=Config.default(), git=git)
        role = Role.load("reviewer", project_root=tmp_path)
        instruction = InstructionBuilder.build(context, role)
        prompt = PromptCompiler.compile(instruction, role.template_content)

    # Diff is safely truncated to MAX_DIFF_CHARS (60000)
    assert instruction.git_diff is not None
    assert len(instruction.git_diff) < 65000
    assert "Diff truncated" in instruction.git_diff
    assert "chars omitted to fit context window" in instruction.git_diff

    # Changed-file summary is still intact and visible despite large diff
    assert "## Changed Files" in prompt.text
    assert "M large.py" in prompt.text
    assert "M other.py" in prompt.text

    # Repository access instruction is present so reviewer knows it can inspect files directly
    assert "The repository is available for inspection" in prompt.text


def test_reviewer_clean_working_tree_fallbacks(tmp_path):
    """Verify Reviewer prompt provides clear clean-state indicators when no diff or executor report exists."""
    inst = Instruction(
        role_name="reviewer",
        task="Audit uncommitted code",
        executor_report=None,
        git_diff=None,
        git_status=None,
        changed_file_summary=None,
        changed_files=[],
    )
    prompt = PromptCompiler.compile(inst, "Role template")

    assert "(No Executor report found.)" in prompt.text
    assert "(Clean working tree / no uncommitted status changes detected.)" in prompt.text
    assert "(No changed files detected.)" in prompt.text
    assert "(No git diff detected.)" in prompt.text
