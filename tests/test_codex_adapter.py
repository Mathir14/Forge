"""Focused test suite for OpenAI Codex CLI adapter."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.adapters.codex import CodexAdapter
from forge.adapters.registry import AdapterRegistry
from forge.core.capabilities import Capability, CapabilityValidationError
from forge.core.config import Config, StageConfig
from forge.cli import _get_adapter


# ---------------------------------------------------------------------------
# 1. Registration & Discovery
# ---------------------------------------------------------------------------

def test_codex_registry_registration():
    """Verify Codex is discoverable through AdapterRegistry with canonical name 'codex'."""
    assert AdapterRegistry.is_registered("codex")
    assert AdapterRegistry.get_class("codex") is CodexAdapter
    assert "codex" in AdapterRegistry.list_canonical_adapters()
    assert AdapterRegistry.list_canonical_adapters()["codex"] is CodexAdapter

    instance = AdapterRegistry.get("codex")
    assert isinstance(instance, CodexAdapter)
    assert instance.name == "codex"
    assert instance.model == "gpt-5.6-terra"
    assert instance.effort == "medium"


# ---------------------------------------------------------------------------
# 2. Detection
# ---------------------------------------------------------------------------

def test_codex_detection_available():
    """Verify is_available returns True when codex binary is in PATH."""
    adapter = CodexAdapter()
    with patch("shutil.which", return_value="/usr/local/bin/codex"):
        assert adapter.is_available() is True


def test_codex_detection_missing():
    """Verify is_available returns False and execute returns exit_code=1 when codex binary is missing."""
    adapter = CodexAdapter()
    with patch("shutil.which", return_value=None):
        assert adapter.is_available() is False
        res = adapter.execute(prompt="Hello")
        assert res.exit_code == 1
        assert "Executable 'codex' was not found in PATH." in res.stderr
        assert res.duration_seconds == 0.0


# ---------------------------------------------------------------------------
# 3. Command Construction
# ---------------------------------------------------------------------------

def test_codex_command_construction_defaults():
    """Verify build_command produces expected arguments under default settings."""
    adapter = CodexAdapter()
    with patch("shutil.which", return_value="/mock/codex"):
        cmd = adapter.build_command()
        assert cmd[0] == "/mock/codex"
        assert cmd[1:4] == ["exec", "--color", "never"]
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "gpt-5.6-terra"
        assert "-c" in cmd
        assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="medium"'
        # Prompt argument '-' must be the final argument
        assert cmd[-1] == "-"


def test_codex_command_construction_custom_model_and_effort():
    """Verify build_command respects explicit model, effort, and work_dir."""
    adapter = CodexAdapter(model="o3-mini", effort="high")
    work_dir = Path("/home/test/project")
    with patch("shutil.which", return_value="/mock/codex"):
        cmd = adapter.build_command(work_dir=work_dir)
        assert cmd[cmd.index("-m") + 1] == "o3-mini"
        assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="high"'
        assert cmd[cmd.index("-C") + 1] == str(work_dir)
        assert cmd[-1] == "-"


def test_codex_command_construction_none_model_and_effort():
    """Verify -m and -c model_reasoning_effort are omitted when model/effort are None."""
    adapter = CodexAdapter(model=None, effort=None)
    with patch("shutil.which", return_value="/mock/codex"):
        cmd = adapter.build_command()
        assert "-m" not in cmd
        assert "-c" not in cmd
        assert cmd[-1] == "-"


def test_codex_command_construction_auto_approve():
    """Verify auto_approve maps to --dangerously-bypass-approvals-and-sandbox."""
    adapter_normal = CodexAdapter(auto_approve=False)
    adapter_approved = CodexAdapter(auto_approve=True)
    with patch("shutil.which", return_value="/mock/codex"):
        cmd_normal = adapter_normal.build_command()
        cmd_approved = adapter_approved.build_command()
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd_normal
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd_approved
        assert cmd_approved[-1] == "-"


def test_codex_command_construction_extra_flags():
    """Verify extra flags are safely rendered before the trailing '-' prompt."""
    adapter = CodexAdapter(extra_flags={
        "sandbox": "workspace-write",
        "ephemeral": True,
        "disabled_flag": False,
        "unsafe;flag": "inject",
    })
    with patch("shutil.which", return_value="/mock/codex"):
        cmd = adapter.build_command()
        assert "--sandbox" in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
        assert "--ephemeral" in cmd
        assert "--disabled_flag" not in cmd
        assert "--unsafe;flag" not in cmd
        assert cmd[-1] == "-"


# ---------------------------------------------------------------------------
# 4. Execution Lifecycle
# ---------------------------------------------------------------------------

def test_codex_execution_success(tmp_path):
    """Verify successful execution returns AdapterResponse with stdout, stderr, and code 0."""
    adapter = CodexAdapter()
    expected_stdout = "```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\n```"
    expected_stderr = "OpenAI Codex header..."

    with patch("shutil.which", return_value="/mock/codex"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["/mock/codex"],
            returncode=0,
            stdout=expected_stdout,
            stderr=expected_stderr,
        )
        res = adapter.execute(prompt="Design system", cwd=tmp_path, timeout=120)

        assert res.exit_code == 0
        assert res.stdout == expected_stdout
        assert res.stderr == expected_stderr
        assert res.raw_output == expected_stdout
        assert res.duration_seconds >= 0.0

        # Verify subprocess.run arguments
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input"] == "Design system"
        assert call_kwargs["cwd"] == tmp_path
        assert call_kwargs["timeout"] == 120
        assert call_kwargs["capture_output"] is True
        assert call_kwargs["text"] is True


def test_codex_execution_nonzero_exit():
    """Verify non-zero return code preserves stderr in raw_output when stdout is empty."""
    adapter = CodexAdapter()
    with patch("shutil.which", return_value="/mock/codex"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["/mock/codex"],
            returncode=1,
            stdout="",
            stderr="Error: Model authentication token expired.",
        )
        res = adapter.execute(prompt="Test")
        assert res.exit_code == 1
        assert res.stdout == ""
        assert res.stderr == "Error: Model authentication token expired."
        assert res.raw_output == "Error: Model authentication token expired."


def test_codex_execution_timeout():
    """Verify timeout produces exit_code 124 and clear diagnostic message."""
    adapter = CodexAdapter()
    with patch("shutil.which", return_value="/mock/codex"), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=45)):
        res = adapter.execute(prompt="Long run", timeout=45)
        assert res.exit_code == 124
        assert "Execution timed out after 45 seconds." in res.stderr
        assert "Execution timed out after 45 seconds." in res.raw_output


def test_codex_execution_keyboard_interrupt():
    """Verify keyboard interrupt produces exit_code 130 without leaking exception."""
    adapter = CodexAdapter()
    with patch("shutil.which", return_value="/mock/codex"), \
         patch("subprocess.run", side_effect=KeyboardInterrupt()):
        res = adapter.execute(prompt="Test")
        assert res.exit_code == 130
        assert "Execution interrupted by user (SIGINT)." in res.stderr


def test_codex_execution_os_error():
    """Verify OSError is caught cleanly without leaking internal exceptions."""
    adapter = CodexAdapter()
    with patch("shutil.which", return_value="/mock/codex"), \
         patch("subprocess.run", side_effect=OSError("Exec format error")):
        res = adapter.execute(prompt="Test")
        assert res.exit_code == 1
        assert "Exec format error" in res.stderr
        assert "OS/Encoding error executing codex" in res.raw_output


# ---------------------------------------------------------------------------
# 5. Capabilities
# ---------------------------------------------------------------------------

def test_codex_capabilities():
    """Verify CodexAdapter declares exact supported capabilities."""
    adapter = CodexAdapter()
    caps = adapter.capabilities()
    expected = {
        "code_read",
        "code_edit",
        "shell",
        "git",
        "structured_output",
        "tool_calling",
        "long_running",
        "custom_flags",
    }
    assert caps == expected

    # Feature properties
    assert adapter.supports_session_resume is False
    assert adapter.has_capability("session_resume") is False
    assert adapter.has_capability(Capability.SESSION_RESUME) is False
    assert adapter.supports_structured_output is True
    assert adapter.has_capability("structured_output") is True
    assert adapter.has_capability(Capability.STRUCTURED_OUTPUT) is True
    assert adapter.supports_browser is False
    assert adapter.supports_playwright is False
    assert adapter.supports_streaming is False


def test_codex_session_resume_is_truthfully_absent():
    """Verify session_resume is absent because CodexAdapter has no operational resume contract."""
    adapter = CodexAdapter()
    assert "session_resume" not in adapter.capabilities()
    assert adapter.supports_session_resume is False

    # Verify adapter exposes no resume session method or CLI argument
    with patch("shutil.which", return_value="/mock/codex"):
        cmd = adapter.build_command()
        assert "resume" not in cmd


def test_codex_structured_output_contract(tmp_path):
    """Verify Forge Stage engine reliably requests, executes, and consumes structured output from Codex."""
    from forge.core.context import Context
    from forge.core.git import GitService
    from forge.core.role import Role
    from forge.stages.stage import Stage
    from forge.storage.run_manager import RunManager

    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Design cache architecture")
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    role = Role(
        name="architect",
        sequence_number=1,
        template_content="You are the Architect.",
        protocol_content="Emit machine report.",
    )

    sample_stdout = """# Architecture Plan
We will introduce a Redis-backed tiered cache.

```yaml
ROLE: ARCHITECT
STATUS: APPROVED
HANDOFF: PLANNER
ARCHITECTURE:
  TIERS:
    - L1_LOCAL_MEMORY
    - L2_REDIS
REFACTOR_REQUIRED: NO
BREAKING_ARCHITECTURE_CHANGE: NO
```
"""

    adapter = CodexAdapter()
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    with patch("shutil.which", return_value="/mock/codex"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["/mock/codex"],
            returncode=0,
            stdout=sample_stdout,
            stderr="",
        )

        result = stage.run(context)

        # 1. Verify stage execution succeeded
        assert result.success is True
        assert result.status == "APPROVED"
        assert result.handoff == "PLANNER"

        # 2. Verify structured machine report was consumed and validated
        report = result.machine_report
        assert report is not None
        assert report.is_valid is True
        assert report.role == "ARCHITECT"
        assert report.status == "APPROVED"
        assert report.handoff == "PLANNER"
        assert report.data["ARCHITECTURE"]["TIERS"] == ["L1_LOCAL_MEMORY", "L2_REDIS"]

        # 3. Verify structured JSON artifact was saved
        json_file = tmp_path / ".forge" / "runs" / "run-001" / "01_architect.json"
        assert json_file.exists()
        import json
        saved_data = json.loads(json_file.read_text(encoding="utf-8"))
        assert saved_data["status"] == "APPROVED"
        assert saved_data["machine_report"]["role"] == "ARCHITECT"


# ---------------------------------------------------------------------------
# 6. Configuration Integration
# ---------------------------------------------------------------------------

def test_codex_config_as_defaults():
    """Verify Config.validate_dict accepts codex as defaults.adapter."""
    data = {
        "version": "2.0",
        "defaults": {
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "effort": "high",
        },
    }
    errors = Config.validate_dict(data)
    assert errors == []


def test_codex_config_as_stage_adapter():
    """Verify Config.validate_dict accepts codex for stages it satisfies."""
    data = {
        "version": "2.0",
        "stages": {
            "architect": {
                "adapter": "codex",
                "model": "o3-mini",
                "effort": "high",
                "timeout": 600,
            },
            "executor": {
                "adapter": "codex",
                "auto_approve": True,
            },
        },
    }
    errors = Config.validate_dict(data)
    assert errors == []


def test_codex_config_satisfies_all_standard_lifecycle_stages():
    """Verify Codex satisfies critic, architect, planner, executor, and reviewer stages."""
    data = {
        "version": "2.0",
        "stages": {
            "critic": {"adapter": "codex"},
            "architect": {"adapter": "codex"},
            "planner": {"adapter": "codex"},
            "executor": {"adapter": "codex"},
            "reviewer": {"adapter": "codex"},
        },
    }
    errors = Config.validate_dict(data)
    assert errors == []


def test_codex_config_rejected_for_synthetic_browser_stage():
    """Verify Config.validate_dict rejects codex for synthetic_browser_stage (lacks playwright/screenshots)."""
    data = {
        "version": "2.0",
        "stages": {
            "synthetic_browser_stage": {
                "adapter": "codex",
            },
        },
    }
    errors = Config.validate_dict(data)
    assert len(errors) == 1
    assert "Configured adapter 'codex' does not satisfy stage 'synthetic_browser_stage'" in errors[0]
    assert "playwright" in errors[0]
    assert "screenshots" in errors[0]


def test_codex_config_accepts_tester_stage():
    """Verify Config.validate_dict accepts codex for real tester stage (provides code_read and shell)."""
    data = {
        "version": "2.0",
        "stages": {
            "tester": {
                "adapter": "codex",
            },
        },
    }
    errors = Config.validate_dict(data)
    assert errors == []



def test_get_adapter_resolves_codex_defaults():
    """Verify _get_adapter resolves Codex default model and effort when unconfigured."""
    cfg = Config.default()
    cfg.stages["architect"] = StageConfig(adapter="codex", model=None, effort=None)

    with patch("forge.adapters.codex.CodexAdapter.is_available", return_value=True):
        adapter = _get_adapter(cfg, "architect", exit_on_error=False)
        assert isinstance(adapter, CodexAdapter)
        assert adapter.name == "codex"
        assert adapter.model == CodexAdapter.DEFAULT_MODEL
        assert adapter.effort == CodexAdapter.DEFAULT_EFFORT


# ---------------------------------------------------------------------------
# 7. Doctor Integration
# ---------------------------------------------------------------------------

def test_doctor_includes_codex():
    """Verify AdapterRegistry.check_all_tools includes Codex."""
    tools = AdapterRegistry.check_all_tools()
    codex_entries = [t for t in tools if t["name"] == "Codex"]
    assert len(codex_entries) == 1
    entry = codex_entries[0]
    assert entry["binary"] == "codex"
    assert entry["desc"] == "OpenAI Codex CLI"
    assert "found" in entry
    assert "path" in entry
