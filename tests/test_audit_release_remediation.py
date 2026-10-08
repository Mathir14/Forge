"""Regression tests verifying all P2/P3 audit findings:
- FINDING-PRM-01: Auto-repair feedback deduplication and user task immutability.
- FINDING-CFG-01: Configuration precedence parity between CLI and runtime loading.
- FINDING-STG-01: Stage.run() respects role phase (pre_run vs post_run).
- FINDING-PRM-02: Reviewer prompt includes Architect and Planner outputs.
- FINDING-ADP-01: Process group cleanup on adapter timeout.
- FINDING-CLI-01: Standalone execution of closing critic.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.cli import main
from forge.core.config import Config, StageConfig
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.core.run import Run
from forge.prompts.builder import InstructionBuilder
from forge.prompts.compiler import PromptCompiler
from forge.prompts.instruction import Instruction
from forge.prompts.rendered_prompt import RenderedPrompt
from forge.protocol.report import MachineReport
from forge.stages.definition import StageDefinition, StageOrder
from forge.stages.result import StageResult
from forge.stages.stage import Stage
from forge.storage.run_manager import RunManager


def make_fake_stage_result(
    role_name: str,
    status: str,
    reason: str = "",
    issues: Optional[Dict[str, Any]] = None,
    success: bool = True,
    seq: Optional[int] = None,
) -> StageResult:
    role = Role(name=role_name, template_content="", sequence_number=seq)
    prompt = RenderedPrompt.from_text("")
    response = AdapterResponse(stdout="", stderr="", exit_code=0 if success else 1, duration_seconds=0.5, raw_output="")
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
        duration_seconds=0.5,
        success=success,
    )


# ==============================================================================
# FINDING-PRM-01: Auto-Repair Feedback Deduplication & Task Immutability
# ==============================================================================

def test_prm01_prompt_contains_repair_feedback_exactly_once():
    """Verify that PromptCompiler includes repair feedback exactly once, even if already present in task."""
    original_task = "Implement user authentication with JWT."
    repair_feedback = "### Auto-Repair Feedback (Attempt 1):\n- [HIGH] Insecure token signature validation."

    # Case 1: Standard compilation with independent task and repair_feedback
    inst1 = Instruction(
        role_name="executor",
        task=original_task,
        project_docs={},
        previous_stage_outputs={},
        repair_feedback=repair_feedback,
    )
    rendered1 = PromptCompiler.compile(inst1, "You are an executor.")
    assert rendered1.text.count("Insecure token signature validation.") == 1
    assert "## USER TASK REQUEST" in rendered1.text

    # Case 2: Even if task string already contains repair_feedback, it is never rendered twice
    inst2 = Instruction(
        role_name="executor",
        task=f"{original_task}\n\n{repair_feedback}",
        project_docs={},
        previous_stage_outputs={},
        repair_feedback=repair_feedback,
    )
    rendered2 = PromptCompiler.compile(inst2, "You are an executor.")
    assert rendered2.text.count("Insecure token signature validation.") == 1


def test_prm01_reviewer_prompt_contains_repair_feedback_exactly_once():
    """Verify Reviewer prompt compiler renders repair feedback exactly once."""
    original_task = "Implement OAuth2 login."
    repair_feedback = "### Auto-Repair Feedback from Reviewer (Attempt 1):\nStatus: CHANGES_REQUIRED\n- [HIGH] Missing CSRF state check"

    inst = Instruction(
        role_name="reviewer",
        task=original_task,
        project_docs={},
        previous_stage_outputs={},
        repair_feedback=repair_feedback,
    )
    rendered = PromptCompiler.compile(inst, "You are a reviewer.")
    assert rendered.text.count("Missing CSRF state check") == 1


def test_prm01_instruction_builder_preserves_task_immutability(tmp_path):
    """Verify InstructionBuilder extracts clean base task when context.repair_feedback is present."""
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    original_task = "Build payment gateway"
    repair_fb = "### Auto-Repair Feedback from Reviewer (Attempt 1):\n- Fix webhook signature"

    run = Run(run_id="run-001", task=original_task, run_dir=run_dir)
    run.set_auto_repair_feedback(repair_fb)
    git = GitService(tmp_path)
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config(),
        git=git,
        repair_feedback=repair_fb,
    )
    role = Role(name="executor", sequence_number=3, template_content="Exec", protocol_content="")

    inst = InstructionBuilder.build(context, role)
    # Instruction task must be the clean original task, not polluted with repair feedback
    assert inst.task == original_task
    assert inst.repair_feedback == repair_fb

    # Run metadata must save the clean initial task
    run.save_metadata()
    meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["task"] == original_task


def test_prm01_multiple_repair_retries_and_resume(tmp_path, monkeypatch):
    """Verify multi-iteration auto-repair loop keeps prompt feedback to one block and maintains immutability."""
    from forge.cli import auto_pipeline

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    runner = CliRunner()

    exec_prompts = []
    iteration_count = {"executor": 0, "reviewer": 0}

    def fake_execute(stage_def, context, run_mgr, display_task=None, banner_prefix=""):
        if stage_def.name == "executor":
            iteration_count["executor"] += 1
            # Compile prompt as the real Stage would
            role = Role.load(stage_def.name, project_root=context.project_root)
            inst = InstructionBuilder.build(context, role)
            rendered = PromptCompiler.compile(inst, role.template_content)
            exec_prompts.append(rendered.text)
            return make_fake_stage_result("executor", "SUCCESS", seq=stage_def.sequence_number)
        elif stage_def.name == "reviewer":
            iteration_count["reviewer"] += 1
            if iteration_count["reviewer"] == 1:
                return make_fake_stage_result(
                    "reviewer",
                    "CHANGES_REQUIRED",
                    issues={"HIGH": ["Flaw 1 detected"]},
                    success=False,
                    seq=stage_def.sequence_number,
                )
            elif iteration_count["reviewer"] == 2:
                return make_fake_stage_result(
                    "reviewer",
                    "CHANGES_REQUIRED",
                    issues={"MEDIUM": ["Flaw 2 detected"]},
                    success=False,
                    seq=stage_def.sequence_number,
                )
            else:
                return make_fake_stage_result("reviewer", "APPROVED", seq=stage_def.sequence_number)
        return make_fake_stage_result(stage_def.name, "APPROVED", seq=stage_def.sequence_number)

    with patch("forge.cli.execute_stage", side_effect=fake_execute):
        auto_pipeline.callback(
            task="Implement feature X",
            spec_file=None,
            from_critic=False,
            run_id=None,
            max_retries=3,
            auto_commit=False,
            no_critic=True,
        )

    assert iteration_count["executor"] == 3
    assert iteration_count["reviewer"] == 3

    # Attempt 1: No repair feedback
    assert "Auto-Repair Feedback" not in exec_prompts[0]

    # Attempt 2: Contains Flaw 1 feedback exactly once
    assert exec_prompts[1].count("Flaw 1 detected") == 1
    assert "Flaw 2 detected" not in exec_prompts[1]

    # Attempt 3: Contains Flaw 2 feedback exactly once
    assert exec_prompts[2].count("Flaw 2 detected") == 1


# ==============================================================================
# FINDING-CFG-01: Parity Across All CLI Config Commands & Runtime
# ==============================================================================

def test_cfg01_precedence_parity_both_configs_exist(tmp_path, monkeypatch):
    """Verify that when both forge.yaml and .forge/config.yaml exist, all config entry points use .forge/config.yaml."""
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    runner = CliRunner()

    # Create forge.yaml (lower project precedence)
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        yaml.safe_dump({"stages": {"executor": {"adapter": "opencode", "model": "from-forge-yaml"}}}),
        encoding="utf-8",
    )

    # Create .forge/config.yaml (highest project precedence)
    dot_forge_dir = tmp_path / ".forge"
    dot_forge_dir.mkdir(parents=True)
    dot_config = dot_forge_dir / "config.yaml"
    dot_config.write_text(
        yaml.safe_dump({"stages": {"executor": {"adapter": "antigravity", "model": "from-dot-forge"}}}),
        encoding="utf-8",
    )

    # 1. Runtime Config.load()
    cfg = Config.load(tmp_path)
    assert cfg.stages["executor"].adapter == "antigravity"
    assert cfg.stages["executor"].model == "from-dot-forge"

    # 2. Config helper resolution
    active_target = Config.resolve_active_project_config_file(tmp_path)
    write_target = Config.resolve_write_target(tmp_path)
    assert active_target == dot_config
    assert write_target == dot_config

    # 3. CLI: config get
    res_get = runner.invoke(main, ["config", "get", "stages.executor.model"])
    assert res_get.exit_code == 0
    assert "from-dot-forge" in res_get.output

    # 4. CLI: config show --raw
    res_show_raw = runner.invoke(main, ["config", "show", "--raw"])
    assert res_show_raw.exit_code == 0
    assert "from-dot-forge" in res_show_raw.output

    # 5. CLI: config set modifies the authoritative write target (.forge/config.yaml)
    res_set = runner.invoke(main, ["config", "set", "stages.executor.timeout", "999"])
    assert res_set.exit_code == 0
    updated_dot_cfg = yaml.safe_load(dot_config.read_text(encoding="utf-8"))
    assert updated_dot_cfg["stages"]["executor"]["timeout"] == 999
    # forge.yaml must remain untouched
    forge_yaml_content = yaml.safe_load(forge_yaml.read_text(encoding="utf-8"))
    assert "timeout" not in forge_yaml_content.get("stages", {}).get("executor", {})


# ==============================================================================
# FINDING-STG-01: Stage.run() Respects Role Phase
# ==============================================================================

def test_stg01_stage_run_respects_role_phase(tmp_path):
    """Verify Stage.run() passes the role's phase (pre_run vs post_run) when fetching stage configuration."""
    cfg = Config()
    # Configure distinct models/timeouts for pre_run critic vs post_run critic
    cfg.stages["critic"] = StageConfig(adapter="opencode", model="pre-model", timeout=100)
    cfg.stages["critic_post_run"] = StageConfig(adapter="opencode", model="post-model", timeout=500)

    role_pre = Role(name="critic", sequence_number=0, template_content="", protocol_content="", phase="pre_run")
    role_post = Role(name="critic", sequence_number=5, template_content="", protocol_content="", phase="post_run")

    adapter = MagicMock()
    adapter.name = "opencode"
    adapter.DEFAULT_TIMEOUT = 300
    adapter.max_prompt_bytes = None
    adapter.capabilities.return_value = {"code_read", "shell", "git", "code_edit", "structured_output"}
    adapter.execute.return_value = MagicMock(
        stdout="```yaml\nROLE: CRITIC\nSTATUS: APPROVED\n```",
        stderr="",
        exit_code=0,
        duration_seconds=1.0,
        raw_output="Approved",
    )

    run = Run(run_id="run-stg-01", task="Audit", run_dir=tmp_path / "run-stg-01")
    git = GitService(tmp_path)
    context = Context(run=run, project_root=tmp_path, config=cfg, git=git)
    run_mgr = RunManager(tmp_path)

    # Pre-run stage run
    stage_pre = Stage(role=role_pre, adapter=adapter, run_manager=run_mgr)
    with patch.object(cfg, "get_stage_config", wraps=cfg.get_stage_config) as mock_get_cfg:
        stage_pre.run(context)
        mock_get_cfg.assert_called_with("critic", phase="pre_run")

    # Post-run stage run
    stage_post = Stage(role=role_post, adapter=adapter, run_manager=run_mgr)
    with patch.object(cfg, "get_stage_config", wraps=cfg.get_stage_config) as mock_get_cfg:
        stage_post.run(context)
        mock_get_cfg.assert_called_with("critic", phase="post_run")


# ==============================================================================
# FINDING-PRM-02: Reviewer Prompt Includes Architect & Planner Outputs
# ==============================================================================

def test_prm02_reviewer_prompt_includes_architect_and_planner():
    """Verify Reviewer prompt renders Architect Specification and Planner Plan before Executor Report."""
    inst = Instruction(
        role_name="reviewer",
        task="Implement feature Y",
        project_docs={},
        previous_stage_outputs={
            "architect": "# System Architecture\nUse layered service architecture.",
            "planner": "# Implementation Plan\nStep 1: DB schema.\nStep 2: API endpoints.",
            "executor": "# Executor Report\nImplemented DB and endpoints.",
        },
        git_diff="diff --git a/app.py b/app.py",
        git_status="M app.py",
        changed_files=["app.py"],
    )

    rendered = PromptCompiler.compile(inst, "You are a reviewer.")
    text = rendered.text

    assert "## Original Requirements" in text
    assert "## Architect Specification" in text
    assert "Use layered service architecture." in text
    assert "## Planner Plan" in text
    assert "Step 1: DB schema." in text
    assert "## Executor Report" in text
    assert "## Git Status" in text
    assert "## Changed Files" in text
    assert "## Git Diff" in text

    # Verify structural order
    arch_pos = text.find("## Architect Specification")
    plan_pos = text.find("## Planner Plan")
    exec_pos = text.find("## Executor Report")
    diff_pos = text.find("## Git Diff")

    assert arch_pos < plan_pos < exec_pos < diff_pos


# ==============================================================================
# FINDING-ADP-01: Process-Group Cleanup Kills Descendants
# ==============================================================================

def test_adp01_process_group_cleanup_kills_descendants():
    """Verify that when BaseAdapter._run_subprocess times out, descendants in the process group are terminated."""
    # Spawn a Python process that starts a background sleep process and writes its PID to a file
    import tempfile
    pid_file = Path(tempfile.gettempdir()) / f"forge_test_child_{int(time.time()*1000)}.pid"

    code = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"with open(r'{pid_file}', 'w') as f: f.write(str(p.pid))\n"
        "time.sleep(60)\n"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        BaseAdapter._run_subprocess([sys.executable, "-c", code], timeout=1)

    # Wait a short moment for OS signal handling
    time.sleep(0.3)

    assert pid_file.exists(), "Child PID file was not created by test script"
    child_pid = int(pid_file.read_text().strip())
    pid_file.unlink(missing_ok=True)

    # Verify child_pid is dead
    from forge.core.platform import is_pid_alive
    is_alive = is_pid_alive(child_pid)

    assert not is_alive, f"Descendant process {child_pid} was not terminated on timeout!"


# ==============================================================================
# FINDING-CLI-01: Standalone Execution of Closing Critic
# ==============================================================================

def test_cli01_standalone_closing_critic(tmp_path, monkeypatch):
    """Verify 'forge critic --post-run --run run-XXX' executes 05_critic in post_run phase."""
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    runner = CliRunner()

    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Test standalone critic")
    # Simulate prior Reviewer output to satisfy prerequisite
    (run.run_dir / "04_reviewer.md").write_text("# Reviewer Report\nApproved", encoding="utf-8")
    (run.run_dir / "04_reviewer.json").write_text(json.dumps({"status": "APPROVED"}), encoding="utf-8")

    with patch("forge.stages.stage.Stage.run") as mock_stage_run, \
         patch("forge.cli._get_adapter") as mock_get_adapter:
        mock_adapter = MagicMock()
        mock_adapter.name = "opencode"
        mock_get_adapter.return_value = mock_adapter
        mock_stage_run.return_value = make_fake_stage_result("critic", "APPROVED", seq=5)

        res = runner.invoke(main, ["critic", "--post-run", "--run", run.run_id])
        assert res.exit_code == 0
        mock_stage_run.assert_called_once()
        assert "05_CRITIC" in res.output or "Invoking" in res.output
