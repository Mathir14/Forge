import pytest
import logging
from pathlib import Path
from typing import Optional
from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.opencode import OpenCodeAdapter
from forge.adapters.registry import AdapterRegistry
from forge.core.config import Config
from forge.core.git import GitService
from forge.core.context import Context
from forge.core.role import Role
from forge.core.run import Run
from forge.storage.run_manager import RunManager
from forge.prompts.builder import InstructionBuilder
from tests.conftest import configure_automated_execution_environment


def test_base_adapter_default_timeout():
    assert BaseAdapter.DEFAULT_TIMEOUT == 300


def test_antigravity_adapter_defaults():
    adapter = AntigravityAdapter()
    assert adapter.auto_approve is False


def test_config_executor_default_auto_approve():
    cfg = Config.default()
    assert cfg.stages["executor"].auto_approve is False
    assert cfg.execution.timeout == 300


def test_config_load_timeout(tmp_path):
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """version: "1.0"
stages:
  executor:
    adapter: antigravity
    auto_approve: false
execution:
  mode: autonomous
  auto_commit: true
  timeout: 600
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)
    assert cfg.stages["executor"].auto_approve is False
    assert cfg.execution.mode == "autonomous"
    assert cfg.execution.auto_commit is True
    assert cfg.execution.timeout == 600


def test_git_commit_no_op_on_clean_tree(tmp_path):
    git = GitService(tmp_path)
    assert git.commit("Empty commit") is False


def test_registry_check_all_tools_annotations():
    tools = AdapterRegistry.check_all_tools()
    assert isinstance(tools, list)
    assert len(tools) > 0
    assert "name" in tools[0]


def test_subpackages_importable():
    import forge.adapters
    import forge.core
    import forge.prompts
    import forge.protocol
    import forge.stages
    import forge.storage

    assert forge.adapters is not None
    assert forge.core is not None
    assert forge.prompts is not None
    assert forge.protocol is not None
    assert forge.stages is not None
    assert forge.storage is not None


def test_builder_handles_unreadable_file(tmp_path, caplog):
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run = Run(run_id="run-001", task="Test", run_dir=run_dir)
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )
    role = Role(name="critic", sequence_number=0, template_content="", protocol_content="")

    project_dir = tmp_path / ".ai" / "project"
    project_dir.mkdir(parents=True)
    bad_file = project_dir / "architecture.md"
    bad_file.mkdir()

    with caplog.at_level(logging.WARNING):
        instruction = InstructionBuilder.build(context, role)
    assert "Failed to read project doc" in caplog.text


def test_run_manager_atomic_creation(tmp_path):
    mgr = RunManager(tmp_path)
    run1 = mgr.create_run(task="Run 1")
    run2 = mgr.create_run(task="Run 2")
    assert run1.run_id == "run-001"
    assert run2.run_id == "run-002"
    assert (tmp_path / ".forge" / "runs" / "run-001" / "metadata.json").exists()
    assert (tmp_path / ".forge" / "runs" / "run-002" / "metadata.json").exists()


def test_stage_forwards_execution_timeout(tmp_path):
    from forge.stages.stage import Stage

    class RecordingAdapter(BaseAdapter):
        def __init__(self):
            super().__init__(name="recording")
            self.last_timeout = None

        def is_available(self) -> bool:
            return True

        def execute(
            self,
            prompt: str,
            cwd: Optional[Path] = None,
            timeout: Optional[int] = None,
        ) -> AdapterResponse:
            self.last_timeout = timeout
            return AdapterResponse(
                stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
                stderr="",
                exit_code=0,
                duration_seconds=0.1,
                raw_output="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            )

    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Test timeout forwarding")
    cfg = Config.default()
    cfg.execution.timeout = 450
    context = Context(run=run, project_root=tmp_path, config=cfg, git=GitService(tmp_path))
    role = Role(name="architect", sequence_number=1, template_content="Test", protocol_content="")

    adapter = RecordingAdapter()
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
    result = stage.run(context)
    assert result.success
    assert adapter.last_timeout == 450


def test_antigravity_adapter_timeout_handling():
    import subprocess
    from unittest.mock import patch

    adapter = AntigravityAdapter()
    with patch("shutil.which", return_value="/usr/bin/agy"):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=10)):
            res = adapter.execute(prompt="test prompt", timeout=10)
            assert res.exit_code == 124
            assert "timed out after 10 seconds" in res.stderr


def test_opencode_adapter_timeout_handling():
    import subprocess
    from unittest.mock import patch

    adapter = OpenCodeAdapter()
    with patch("shutil.which", return_value="/usr/bin/opencode"):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["opencode"], timeout=15)):
            res = adapter.execute(prompt="test prompt", timeout=15)
            assert res.exit_code == 124
            assert "timed out after 15 seconds" in res.stderr


def test_git_service_timeout_handling(tmp_path):
    import subprocess
    from unittest.mock import patch

    git = GitService(tmp_path)
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=5)):
        res = git._run(["status"], timeout=5)
        assert res.returncode == 124
        assert res.stderr == "Git command timed out"


def test_antigravity_auto_approve_flags():
    from unittest.mock import patch, MagicMock

    with patch("shutil.which", return_value="/usr/bin/agy"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="done", stderr="", returncode=0)

            # Default auto_approve=False
            adapter_default = AntigravityAdapter(auto_approve=False)
            adapter_default.execute("test prompt")
            cmd_default = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" not in cmd_default

            # Opt-in auto_approve=True
            adapter_opt_in = AntigravityAdapter(auto_approve=True)
            adapter_opt_in.execute("test prompt")
            cmd_opt_in = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" in cmd_opt_in


def test_run_manager_preserves_fresh_temp_dirs(tmp_path):
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True)
    active = runs_dir / ".tmp_run_active123"
    active.mkdir()
    (active / "metadata.json").write_text("{}", encoding="utf-8")
    assert active.exists()

    mgr = RunManager(tmp_path)
    mgr.list_runs()
    assert active.exists()


def test_run_manager_cleans_stale_orphaned_temp_dirs(tmp_path):
    import time
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True)
    orphan = runs_dir / ".tmp_run_orphan123"
    orphan.mkdir()
    (orphan / "metadata.json").write_text("{}", encoding="utf-8")
    old = time.time() - 600
    for p in orphan.iterdir():
        import os
        os.utime(p, (old, old))
    os.utime(orphan, (old, old))
    assert orphan.exists()

    mgr = RunManager(tmp_path)
    mgr.create_run(task="Trigger cleanup")
    assert not orphan.exists()


def test_git_changed_files_parsing(tmp_path):
    from unittest.mock import patch, MagicMock

    git = GitService(tmp_path)
    mock_porcelain = (
        " M src/main.py\n"
        '?? "path with spaces/file.txt"\n'
        "R  old_name.py -> new_name.py\n"
    )
    with patch.object(git, "_run", return_value=MagicMock(returncode=0, stdout=mock_porcelain)):
        files = git.changed_files()
        assert "src/main.py" in files
        assert "path with spaces/file.txt" in files
        assert "new_name.py" in files


def test_config_merge_immutability(tmp_path):
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """version: "1.0"
stages:
  executor:
    effort: low
""",
        encoding="utf-8",
    )
    cfg1 = Config.default()
    cfg2 = Config.load(tmp_path)
    assert cfg1.stages["executor"].effort == "high"
    assert cfg2.stages["executor"].effort == "low"


def test_cli_import_and_commands():
    from click.testing import CliRunner
    from forge.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Forge: CLI-First Multi-Agent Orchestration Framework." in result.output

    doctor_result = runner.invoke(main, ["doctor"])
    assert doctor_result.exit_code == 0
    assert "Forge Doctor" in doctor_result.output


def test_antigravity_adapter_prompt_argument():
    from unittest.mock import patch, MagicMock

    with patch("shutil.which", return_value="/usr/bin/agy"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
            adapter = AntigravityAdapter()
            adapter.execute("hello world prompt")
            
            cmd = mock_run.call_args[0][0]
            kwargs = mock_run.call_args[1]
            assert "-p" in cmd or "--print" in cmd
            assert "hello world prompt" in cmd
            flag_idx = cmd.index("-p") if "-p" in cmd else cmd.index("--print")
            assert cmd[flag_idx + 1] == "hello world prompt"
            # Verify -p does NOT take --output-format as its prompt value
            assert cmd[flag_idx + 1] != "--output-format"
            assert kwargs.get("input") is None


def test_cli_run_stage_and_commands(tmp_path):
    from unittest.mock import patch, MagicMock
    from click.testing import CliRunner
    from forge.cli import main

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        # Init
        init_res = runner.invoke(main, ["init"])
        assert init_res.exit_code == 0
        assert (Path.cwd() / "forge.yaml").exists()
        configure_automated_execution_environment()

        mock_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="",
            exit_code=0,
            duration_seconds=0.1,
            raw_output="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=mock_resp), \
             patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True):
            
            # Architect command
            arch_res = runner.invoke(main, ["architect", "Design auth system"])
            assert arch_res.exit_code == 0
            assert "Invoking Architect (opencode) for task:" in arch_res.output
            assert (Path.cwd() / ".forge" / "runs" / "run-001" / "01_architect.json").exists()

            # Planner command with prerequisite satisfied (testing default to latest run without --run)
            plan_mock = AdapterResponse(
                stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
                stderr="",
                exit_code=0,
                duration_seconds=0.1,
                raw_output="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            )
            with patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=plan_mock):
                plan_res = runner.invoke(main, ["planner"])
                assert plan_res.exit_code == 0
                assert "Invoking Planner (opencode) for task:" in plan_res.output
                assert (Path.cwd() / ".forge" / "runs" / "run-001" / "02_planner.json").exists()

            # Executor command with prerequisite satisfied (testing default to latest run without --run)
            exec_mock = AdapterResponse(
                stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```",
                stderr="",
                exit_code=0,
                duration_seconds=0.1,
                raw_output="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```",
            )
            with patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_mock), \
                 patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True):
                exec_res = runner.invoke(main, ["execute"])
                assert exec_res.exit_code == 0
                assert "Invoking Executor (antigravity) for task:" in exec_res.output
                assert (Path.cwd() / ".forge" / "runs" / "run-001" / "03_executor.json").exists()

            # Reviewer command with prerequisite satisfied (testing default to latest run without --run)
            rev_mock = AdapterResponse(
                stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
                stderr="",
                exit_code=0,
                duration_seconds=0.1,
                raw_output="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            )
            with patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=rev_mock):
                rev_res = runner.invoke(main, ["review"])
                assert rev_res.exit_code == 0
                assert "Invoking Reviewer (opencode) for task:" in rev_res.output
                assert (Path.cwd() / ".forge" / "runs" / "run-001" / "05_reviewer.json").exists()

            # Critic command
            critic_mock = AdapterResponse(
                stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: ARCHITECT\n```",
                stderr="",
                exit_code=0,
                duration_seconds=0.1,
                raw_output="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: ARCHITECT\n```",
            )
            with patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=critic_mock):
                critic_res = runner.invoke(main, ["critic", "Check codebase"])
                assert critic_res.exit_code == 0
                assert "Invoking Codebase Critic (opencode) on:" in critic_res.output
                assert (Path.cwd() / ".forge" / "runs" / "run-002" / "00_critic.json").exists()


def test_cli_prerequisite_failures(tmp_path):
    from click.testing import CliRunner
    from forge.cli import main

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        
        # Planner fails if run doesn't exist (explicit --run)
        plan_res = runner.invoke(main, ["planner", "--run", "run-999"])
        assert plan_res.exit_code == 1
        assert "Error loading run" in plan_res.output

        # Planner fails if no runs exist at all (default to latest)
        plan_no_runs = runner.invoke(main, ["planner"])
        assert plan_no_runs.exit_code == 1
        assert "Error loading run" in plan_no_runs.output
        assert "Run 'forge architect" in plan_no_runs.output

        # Executor fails if no runs exist at all (default to latest)
        exec_no_runs = runner.invoke(main, ["execute"])
        assert exec_no_runs.exit_code == 1
        assert "Error loading run" in exec_no_runs.output
        assert "Run 'forge architect' and 'forge planner' first." in exec_no_runs.output

        # Reviewer fails if no runs exist at all (default to latest)
        rev_no_runs = runner.invoke(main, ["review"])
        assert rev_no_runs.exit_code == 1
        assert "Error loading run" in rev_no_runs.output
        assert "Run 'forge architect', 'forge planner', 'forge execute', and 'forge test' first." in rev_no_runs.output

        # Create run-001 without architect output
        from forge.storage.run_manager import RunManager
        mgr = RunManager(Path.cwd())
        mgr.create_run(task="Empty run")

        plan_res2 = runner.invoke(main, ["planner", "--run", "run-001"])
        assert plan_res2.exit_code == 1
        assert "No Architect artifacts found" in plan_res2.output

        plan_res3 = runner.invoke(main, ["planner"])
        assert plan_res3.exit_code == 1
        assert "No Architect artifacts found" in plan_res3.output


def test_forge_runs_whitespace_task(tmp_path):
    """Regression Finding 1: 'forge runs' must not crash with IndexError when run has whitespace-only task."""
    from click.testing import CliRunner
    from forge.cli import main
    from forge.storage.run_manager import RunManager

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        mgr = RunManager(Path.cwd())
        mgr.create_run(task="   \n\n\t  \n")

        res = runner.invoke(main, ["runs"])
        assert res.exit_code == 0
        assert "run-001" in res.output


def test_forge_runs_empty_task(tmp_path):
    """Regression Finding 1: 'forge runs' must not crash when run has empty or None task."""
    from click.testing import CliRunner
    from forge.cli import main
    from forge.storage.run_manager import RunManager

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        mgr = RunManager(Path.cwd())
        mgr.create_run(task="")
        r2 = mgr.create_run(task="placeholder")
        r2.task = None
        r2.save_metadata()

        res = runner.invoke(main, ["runs"])
        assert res.exit_code == 0
        assert "run-001" in res.output
        assert "run-002" in res.output


def test_forge_auto_whitespace_spec(tmp_path):
    """Regression Finding 2: 'forge auto --file' must not crash with IndexError when spec file is whitespace-only."""
    from click.testing import CliRunner
    from unittest.mock import patch
    from forge.cli import main
    from forge.adapters.base import AdapterResponse

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        spec_file = Path.cwd() / "spec.md"
        spec_file.write_text("   \n\n  \t\n", encoding="utf-8")

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: REJECTED\nHANDOFF: NONE\n```",
            stderr="",
            exit_code=0,
            duration_seconds=0.1,
            raw_output="```yaml\nROLE: ARCHITECT\nSTATUS: REJECTED\nHANDOFF: NONE\n```",
        )
        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=arch_resp):
            res = runner.invoke(main, ["auto", "--file", str(spec_file)])
            assert res.exception is None or not isinstance(res.exception, IndexError)
            assert "Starting Fully Autonomous Forge Loop:" in res.output


def test_executor_configured_opencode_model_none():
    """Regression Finding 3: executor configured as OpenCode with model=None must not inherit Antigravity default model."""
    from unittest.mock import patch
    from forge.cli import _get_adapter
    from forge.core.config import Config, StageConfig
    from forge.adapters.registry import AdapterRegistry

    cfg = Config.default()
    cfg.stages["executor"] = StageConfig(adapter="opencode", model=None)

    with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True):
        adapter = _get_adapter(cfg, "executor")
        assert isinstance(adapter, OpenCodeAdapter)
        assert adapter.model is None

    cfg.stages["executor"] = StageConfig(adapter="antigravity", model=None)
    with patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True):
        adapter_agy = _get_adapter(cfg, "executor")
        assert isinstance(adapter_agy, AntigravityAdapter)
        assert adapter_agy.model == AntigravityAdapter.DEFAULT_MODEL

    # Future adapter should not inherit Antigravity defaults
    class CustomFutureAdapter(OpenCodeAdapter):
        pass

    with patch.dict(AdapterRegistry._ADAPTERS, {"future_agent": CustomFutureAdapter}):
        cfg.stages["executor"] = StageConfig(adapter="future_agent", model=None)
        with patch.object(CustomFutureAdapter, "is_available", return_value=True):
            adapter_future = _get_adapter(cfg, "executor")
            assert isinstance(adapter_future, CustomFutureAdapter)
            assert adapter_future.model is None


def test_opencode_windows_executable_resolution():
    """Regression Finding 4: OpenCodeAdapter resolves executable via shutil.which() with fallback to 'opencode'."""
    from unittest.mock import patch, MagicMock
    import sys

    adapter = OpenCodeAdapter()

    # When shutil.which finds Windows .cmd file
    windows_cmd_path = r"C:\Users\test\AppData\Roaming\npm\opencode.cmd"
    with patch("shutil.which", return_value=windows_cmd_path):
        assert adapter.is_available() is True
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            res = adapter.execute(prompt="hello")
            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            if sys.platform == "win32":
                assert windows_cmd_path in called_cmd
                assert "run" in called_cmd
            else:
                assert called_cmd[0] == windows_cmd_path
                assert called_cmd[1] == "run"
            assert res.exit_code == 0

    # When shutil.which returns None (lookup fails), fall back to "opencode"
    with patch("shutil.which", return_value=None):
        assert adapter.is_available() is False
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            res = adapter.execute(prompt="hello")
            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert called_cmd[0] == "opencode"
            assert called_cmd[1] == "run"
            assert res.exit_code == 0


def test_antigravity_adapter_passes_print_timeout():
    """Regression: AntigravityAdapter must pass --print-timeout to synchronize agy's timeout with Forge."""
    from unittest.mock import patch, MagicMock

    with patch("shutil.which", return_value="/usr/bin/agy"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            adapter = AntigravityAdapter()
            res = adapter.execute(prompt="hello", timeout=600)
            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--print-timeout" in called_cmd
            timeout_idx = called_cmd.index("--print-timeout")
            assert called_cmd[timeout_idx + 1] == "600s"
            assert res.exit_code == 0

