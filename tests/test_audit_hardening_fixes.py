"""Adversarial regression tests for final foundation hardening pass.

Validates:
- P1-01: Protocol parser role collision & quoting
- P1-02: Git rename direction in real repository
- P2-01: --from-critic precedence (closing vs pre-run)
- P2-02: Auto-repair task immutability on disk & resume
- P2-03: Config split-brain harmonization across entry points
- P3: execution.auto_commit parity between forge run and forge auto
- Architecture observations: No mock inspection, secret path protection
"""

import inspect
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.cli import _resolve_pipeline_run, auto_pipeline, main, run_pipeline
from forge.core.config import Config, StageConfig
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.core.run import Run
from forge.prompts.builder import InstructionBuilder
from forge.prompts.compiler import PromptCompiler
from forge.prompts.instruction import Instruction
from forge.prompts.rendered_prompt import RenderedPrompt
from forge.protocol.parser import MachineReportParser
from forge.protocol.report import MachineReport
from forge.protocol.validator import MachineReportValidator
from forge.stages.result import StageResult
from forge.storage.run_manager import RunManager


def make_fake_stage_result(
    role_name: str,
    status: str,
    reason: str = "",
    issues: dict = None,
    success: bool = True,
    seq: int = None,
) -> StageResult:
    role = Role(name=role_name, template_content="", sequence_number=seq or 1)
    prompt = RenderedPrompt.from_text("")
    response = AdapterResponse(stdout="", stderr="", exit_code=0 if success else 1, duration_seconds=0.1, raw_output="")
    report = MachineReport(
        role=role_name.upper(),
        status=status,
        reason=reason,
        issues=issues or {},
        is_valid=True,
    )
    return StageResult(
        role=role,
        prompt=prompt,
        response=response,
        machine_report=report,
        raw_markdown=f"```yaml\nROLE: {role_name.upper()}\nSTATUS: {status}\n```",
        duration_seconds=0.1,
        success=success,
    )


# ==============================================================================
# P1-01: Protocol Parser Role Collision
# ==============================================================================

def test_protocol_parser_extracts_expected_role_when_quoting_other_stage():
    """Verify Reviewer quoting Executor YAML report extracts Reviewer's own report."""
    output_with_quote = """
## Reviewer Analysis

I have evaluated the Executor's implementation and machine report:

```yaml
ROLE: EXECUTOR
STATUS: SUCCESS
NOTES: "Implemented authentication middleware in src/auth.py"
```

However, security analysis reveals issues:

```yaml
ROLE: REVIEWER
STATUS: CHANGES_REQUIRED
ISSUES:
  HIGH:
    - "JWT secret is hardcoded in auth.py"
NOTES: "Rejecting implementation due to hardcoded credential."
```
"""
    # When expected_role="reviewer" is specified, must extract Reviewer block
    extracted_dict, raw_yaml = MachineReportParser.extract_yaml(output_with_quote, expected_role="reviewer")
    assert extracted_dict["ROLE"] == "REVIEWER"
    assert extracted_dict["STATUS"] == "CHANGES_REQUIRED"

    report = MachineReportValidator.validate(extracted_dict, expected_role="reviewer")
    assert report.is_valid
    assert report.status == "CHANGES_REQUIRED"
    assert "JWT secret is hardcoded" in report.issues["HIGH"][0]


def test_protocol_parser_critic_quoting_reviewer():
    """Verify Critic quoting Reviewer YAML report extracts Critic's report."""
    output = """
# Critic Closing Audit

Reviewer reported:
```yaml
ROLE: REVIEWER
STATUS: APPROVED
```

Closing critic assessment:
```yaml
ROLE: CRITIC
STATUS: APPROVED
NOTES: "Overall codebase health verified."
```
"""
    extracted_dict, raw_yaml = MachineReportParser.extract_yaml(output, expected_role="critic")
    assert extracted_dict["ROLE"] == "CRITIC"
    assert extracted_dict["STATUS"] == "APPROVED"


def test_protocol_parser_multiple_yaml_blocks_no_expected_role_falls_back_to_last():
    """Verify when no expected_role is supplied, parser picks the LAST valid report block."""
    output = """
```yaml
ROLE: EXECUTOR
STATUS: SUCCESS
```

Some intermediate text.

```yaml
ROLE: REVIEWER
STATUS: APPROVED
```
"""
    extracted_dict, raw_yaml = MachineReportParser.extract_yaml(output)
    # Must be the last block (REVIEWER), not the first
    assert extracted_dict["ROLE"] == "REVIEWER"


def test_protocol_parser_nested_markdown_and_formatting():
    """Verify parser extracts report within complex nested markdown."""
    output = """
> Note: Quoted block here
> ```yaml
> other: config
> ```

### Verification Report

```yaml
ROLE: REVIEWER
STATUS: APPROVED
NOTES: "All tests pass"
```

Final remarks.
"""
    extracted_dict, raw_yaml = MachineReportParser.extract_yaml(output, expected_role="reviewer")
    assert extracted_dict["ROLE"] == "REVIEWER"
    assert extracted_dict["STATUS"] == "APPROVED"


def test_protocol_parser_single_block_backwards_compatible():
    """Verify standard single YAML machine report continues to parse unchanged."""
    output = """
Implementation completed.

```yaml
ROLE: EXECUTOR
STATUS: SUCCESS
NOTES: "Done"
```
"""
    extracted_dict, raw_yaml = MachineReportParser.extract_yaml(output)
    assert extracted_dict["ROLE"] == "EXECUTOR"
    assert extracted_dict["STATUS"] == "SUCCESS"


# ==============================================================================
# P1-02: Git Rename Direction
# ==============================================================================

def test_git_status_rename_direction_in_real_repo(tmp_path):
    """Verify Git porcelain parsing correctly reports old_name -> new_name in real repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "forge@test.com"], cwd=repo, check=True)

    # Create and commit initial file
    initial_file = repo / "source_module.py"
    initial_file.write_text("print('hello')", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)

    # Perform git mv: source_module.py -> renamed_module.py
    subprocess.run(["git", "mv", "source_module.py", "renamed_module.py"], cwd=repo, check=True)

    # Also add a new file to verify additions are untouched
    (repo / "new_module.py").write_text("print('new')", encoding="utf-8")
    subprocess.run(["git", "add", "new_module.py"], cwd=repo, check=True)

    git = GitService(repo)
    status_str = git.status()
    # git.status() outputs: "R  source_module.py -> renamed_module.py"
    assert "source_module.py -> renamed_module.py" in status_str
    assert "new_module.py" in status_str

    # Verify underlying _parse_status_z token positions: (staged, unstaged, path, orig_path)
    entries = git._parse_status_z()
    rename_entry = next((e for e in entries if e[0] == "R"), None)
    assert rename_entry is not None
    staged, unstaged, path, orig_path = rename_entry
    assert path == "renamed_module.py"
    assert orig_path == "source_module.py"


# ==============================================================================
# P2-01: --from-critic Precedence
# ==============================================================================

def test_from_critic_prefers_closing_critic_over_pre_critic(tmp_path):
    """Verify --from-critic selects closing critic (06_critic.md) over pre-run critic (00_critic.md)."""
    rm = RunManager(tmp_path)
    run = rm.create_run("From critic test")

    # Create both 00_critic.md (pre-run audit) and 06_critic.md (closing audit)
    pre_critic = run.run_dir / "00_critic.md"
    pre_critic.write_text("# 00 Pre-run Critic Audit\nPreliminary findings before execution.", encoding="utf-8")
    (run.run_dir / "00_critic.json").write_text('{"ROLE": "CRITIC", "STATUS": "APPROVED"}', encoding="utf-8")

    closing_critic = run.run_dir / "06_critic.md"
    closing_critic.write_text("# 06 Closing Critic Audit\nPost-execution codebase health audit.", encoding="utf-8")
    (run.run_dir / "06_critic.json").write_text('{"ROLE": "CRITIC", "STATUS": "APPROVED"}', encoding="utf-8")

    new_run, is_resumed = _resolve_pipeline_run(
        run_mgr=rm,
        task=None,
        from_critic=True,
        run_id=run.run_id,
    )

    # Must link to closing critic (06_critic)
    assert "06_critic" in new_run.adapters_used.get("critic", "")
    new_run_critic_md = rm.load_stage_markdown(new_run, "critic")
    assert "Post-execution codebase health audit" in new_run_critic_md


def test_from_critic_falls_back_to_pre_critic_if_no_closing_critic(tmp_path):
    """Verify --from-critic falls back to 00_critic.md when only pre-run critic exists."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Pre critic fallback test")

    pre_critic = run.run_dir / "00_critic.md"
    pre_critic.write_text("# 00 Pre-run Critic Audit\nOnly pre-critic exists.", encoding="utf-8")
    (run.run_dir / "00_critic.json").write_text('{"ROLE": "CRITIC", "STATUS": "APPROVED"}', encoding="utf-8")

    new_run, is_resumed = _resolve_pipeline_run(
        run_mgr=rm,
        task=None,
        from_critic=True,
        run_id=run.run_id,
    )

    assert "00_critic" in new_run.adapters_used.get("critic", "")
    new_run_critic_md = rm.load_stage_markdown(new_run, "critic")
    assert "Only pre-critic exists" in new_run_critic_md


# ==============================================================================
# P2-02: Auto-Repair Task Mutation & Immutability
# ==============================================================================

def test_auto_repair_task_immutability_and_resume(tmp_path):
    """Verify auto-repair retries do NOT pollute metadata.json on disk or across run resume."""
    rm = RunManager(tmp_path)
    original_task = "Implement user authentication service"
    run = rm.create_run(original_task)

    assert run.task == original_task

    # Simulate attempt 1 retry setting context.repair_feedback and context.run.task
    attempt_feedback = "### Auto-Repair Feedback from Reviewer (Attempt 1):\nFix token hashing"
    run.task = f"{original_task}\n\n{attempt_feedback}"

    # During stage execution, save_stage_artifacts is called which calls run.save_metadata()
    rm.save_stage_artifacts(
        run=run,
        sequence_number=3,
        role_name="executor",
        markdown_content="Executed attempt 2",
        json_data={"ROLE": "EXECUTOR", "STATUS": "SUCCESS"},
    )

    # 1. Verify metadata.json on disk contains pristine original task
    meta_path = run.run_dir / "metadata.json"
    with open(meta_path, "r", encoding="utf-8") as f:
        meta_data = json.load(f)
    assert meta_data["task"] == original_task
    assert "Auto-Repair Feedback" not in meta_data["task"]

    # 2. Simulate process exit and resume
    resumed = rm.resume(run.run_id)
    assert resumed.task == original_task
    assert "Auto-Repair Feedback" not in resumed.task


def test_prompt_compiler_includes_repair_feedback():
    """Verify prompt compiler appends context.repair_feedback to the task instruction."""
    inst = Instruction(
        role_name="executor",
        task="Original implementation task",
        repair_feedback="### Auto-Repair Feedback from Reviewer (Attempt 1):\nFix SQL injection",
    )
    compiler = PromptCompiler()
    rendered = compiler.compile(inst, role_template="You are the Executor.")

    assert "Original implementation task" in rendered.text
    assert "### Auto-Repair Feedback from Reviewer (Attempt 1):" in rendered.text
    assert "Fix SQL injection" in rendered.text


# ==============================================================================
# P2-03: Config Split-Brain Harmonization
# ==============================================================================

def test_config_split_brain_harmonization(tmp_path):
    """Verify all public configuration entry points agree when defaults.adapter is customized."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "forge.yaml").write_text(
        """
version: "2.0"
defaults:
  adapter: claude
stages:
  planner:
    effort: medium
""",
        encoding="utf-8",
    )

    cfg = Config.load(project_dir)

    # 1. defaults
    assert cfg.defaults.adapter == "claude"

    # 2. get_stage_config for inherited stage
    critic_stage_cfg = cfg.get_stage_config("critic")
    assert critic_stage_cfg.adapter == "claude"

    # 3. direct stages dict
    assert cfg.stages["critic"].adapter == "claude"
    assert cfg.stages["architect"].adapter == "claude"
    assert cfg.stages["planner"].adapter == "claude"
    assert cfg.stages["reviewer"].adapter == "claude"

    # 4. to_dict() serialization
    cfg_dict = cfg.to_dict()
    assert cfg_dict["defaults"]["adapter"] == "claude"
    assert cfg_dict["stages"]["critic"]["adapter"] == "claude"
    assert cfg_dict["stages"]["architect"]["adapter"] == "claude"
    assert cfg_dict["stages"]["reviewer"]["adapter"] == "claude"

    # 5. Config.get_key_from_dict
    assert Config.get_key_from_dict(cfg_dict, "stages.critic.adapter") == "claude"
    assert Config.get_key_from_dict(cfg_dict, "stages.reviewer.adapter") == "claude"

    # 6. Explicitly pinned stage (executor) remains unchanged
    assert cfg.get_stage_config("executor").adapter == "antigravity"
    assert cfg.stages["executor"].adapter == "antigravity"
    assert cfg_dict["stages"]["executor"]["adapter"] == "antigravity"


# ==============================================================================
# P3: execution.auto_commit Parity
# ==============================================================================

def test_auto_commit_parity_forge_run_and_forge_auto(tmp_path, monkeypatch):
    """Verify execution.auto_commit triggers commit in both forge run and forge auto."""
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    # Mock git, auto_commit_run, and click.confirm
    with patch("forge.cli.auto_commit_run") as mock_commit, \
         patch("forge.core.git.GitService.is_git_repo", return_value=True), \
         patch("click.confirm", return_value=True), \
         patch("forge.cli.execute_stage") as mock_exec:

        def fake_stage_result(stage_def, *args, **kwargs):
            status = "APPROVED" if stage_def.name in ("architect", "planner", "reviewer", "critic") else "SUCCESS"
            return make_fake_stage_result(
                role_name=stage_def.name,
                status=status,
                seq=stage_def.sequence_number,
            )

        mock_exec.side_effect = fake_stage_result
        mock_commit.return_value = True

        # Test 1: forge run with --auto-commit triggers auto_commit_run
        run_pipeline.callback(
            task="Test task run",
            from_critic=False,
            run_id=None,
            auto_commit=True,
            no_critic=True,
        )
        assert mock_commit.call_count == 1

        # Test 2: forge auto with --auto-commit triggers auto_commit_run
        auto_pipeline.callback(
            task="Test task auto",
            spec_file=None,
            from_critic=False,
            run_id=None,
            max_retries=1,
            auto_commit=True,
            no_critic=True,
        )
        assert mock_commit.call_count == 2


# ==============================================================================
# Architecture Observations
# ==============================================================================

def test_base_adapter_no_mock_internals_inspection():
    """Verify BaseAdapter._run_subprocess contains no mock-detection code."""
    source = inspect.getsource(BaseAdapter._run_subprocess)
    assert "mock_calls" not in source, "Found 'mock_calls' inspection in production code"
    assert "assert_called_once" not in source, "Found 'assert_called_once' inspection in production code"
    assert "unittest.mock" not in source


def test_secret_path_protection_expansion():
    """Verify GitService.is_protected_path blocks secrets/ directories and secret_* files."""
    # Secrets directory
    assert GitService.is_protected_path("secrets/api_key.txt")
    assert GitService.is_protected_path("secrets/pass")
    assert GitService.is_protected_path("config/secrets/token.json")
    assert GitService.is_protected_path(".secrets/credentials")

    # Secret filenames
    assert GitService.is_protected_path("secret_key.txt")
    assert GitService.is_protected_path("secret-token.json")
    assert GitService.is_protected_path("submodule/secret_key.txt")

    # Ordinary project files must NOT be falsely blocked
    assert not GitService.is_protected_path("src/app.py")
    assert not GitService.is_protected_path("tests/test_secret.py")
    assert not GitService.is_protected_path("docs/user_guide.md")
    assert not GitService.is_protected_path("config.yaml")
