"""Tests for forge adapters command and capability-aware config validation."""

import json
from pathlib import Path
from click.testing import CliRunner
from unittest.mock import patch

from forge.cli import main
from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.adapters.registry import AdapterRegistry
from forge.core.config import Config


# ---------------------------------------------------------------------------
# 1. forge adapters CLI Command Tests
# ---------------------------------------------------------------------------

def test_forge_adapters_cli_human_readable():
    """Verify forge adapters displays all required adapter details and capability checks."""
    runner = CliRunner()
    res = runner.invoke(main, ["adapters"])

    assert res.exit_code == 0
    output = res.output

    # Adapters listed
    assert "Opencode" in output or "OpenCode" in output
    assert "Antigravity" in output

    # Default models
    assert "Default model:" in output
    assert "gemini-3.7-flash-high" in output

    # Feature questions
    assert "Supports session resume?" in output
    assert "Supports browser?" in output
    assert "Supports Playwright?" in output
    assert "Supports streaming?" in output
    assert "Supports structured output?" in output

    # Checkmarks & Crosses
    assert "✓" in output
    assert "✗" in output

    # Capabilities listed
    assert "code_read" in output
    assert "code_edit" in output
    assert "shell" in output


def test_forge_adapters_cli_json():
    """Verify forge adapters --json produces valid JSON with all required keys."""
    runner = CliRunner()
    res = runner.invoke(main, ["adapters", "--json"])

    assert res.exit_code == 0
    data = json.loads(res.output)
    assert isinstance(data, list)
    assert len(data) >= 2

    adapters_map = {item["adapter"]: item for item in data}
    assert "opencode" in adapters_map
    assert "antigravity" in adapters_map

    opencode = adapters_map["opencode"]
    assert opencode["supports_session_resume"] is False
    assert opencode["supports_streaming"] is False
    assert opencode["supports_browser"] is False
    assert opencode["supports_playwright"] is False
    assert opencode["supports_structured_output"] is True
    assert "session_resume" not in opencode["capabilities"]
    assert "streaming" not in opencode["capabilities"]
    assert "shell" in opencode["capabilities"]
    assert "code_edit" in opencode["capabilities"]
    assert "code_read" in opencode["capabilities"]
    assert "git" in opencode["capabilities"]
    assert "structured_output" in opencode["capabilities"]

    antigravity = adapters_map["antigravity"]
    assert antigravity["default_model"] == "gemini-3.7-flash-high"
    assert antigravity["supports_session_resume"] is False
    assert antigravity["supports_browser"] is False
    assert antigravity["supports_streaming"] is False
    assert antigravity["supports_structured_output"] is True
    assert "long_running" in antigravity["capabilities"]

    codex = adapters_map["codex"]
    assert codex["default_model"] == "gpt-5.6-terra"
    assert codex["supports_session_resume"] is False
    assert codex["supports_browser"] is False
    assert codex["supports_streaming"] is False
    assert codex["supports_structured_output"] is True
    assert "session_resume" not in codex["capabilities"]
    assert "structured_output" in codex["capabilities"]
    assert "custom_flags" in codex["capabilities"]


# ---------------------------------------------------------------------------
# 2. Config Validation Integration Tests
# ---------------------------------------------------------------------------

def test_config_validation_passes_valid_standard_config(tmp_path):
    """Verify forge config validate passes when adapters satisfy stage requirements."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        res = runner.invoke(main, ["config", "validate"])
        assert res.exit_code == 0
        assert "All configurations and resolved pipeline settings are valid." in res.output


def test_config_validation_rejects_missing_capabilities(tmp_path):
    """Verify forge config validate fails when an adapter lacks required stage capabilities."""
    class ReadOnlyMockAdapter(BaseAdapter):
        CAPABILITIES = {"code_read"}
        def is_available(self): return True
        def execute(self, *args, **kwargs): pass

    AdapterRegistry.register("readonly", ReadOnlyMockAdapter)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        cfg_file = Path("forge.yaml")
        cfg_file.write_text(
            """
version: "2.0"
stages:
  executor:
    adapter: readonly
""",
            encoding="utf-8",
        )

        try:
            res = runner.invoke(main, ["config", "validate"])
            assert res.exit_code == 1
            assert "does not satisfy stage 'executor'" in res.output
            assert "code_edit" in res.output or "shell" in res.output
        finally:
            AdapterRegistry.unregister("readonly")


def test_config_validation_rejects_tester_stage_with_opencode(tmp_path):
    """Verify forge config validate rejects configuring tester stage with an adapter lacking playwright/screenshots."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        cfg_file = Path("forge.yaml")
        cfg_file.write_text(
            """
version: "2.0"
stages:
  tester:
    adapter: opencode
""",
            encoding="utf-8",
        )

        res = runner.invoke(main, ["config", "validate"])
        assert res.exit_code == 1
        assert "does not satisfy stage 'tester'" in res.output
        assert "playwright" in res.output
        assert "screenshots" in res.output


def test_config_validation_post_run_override_capabilities(tmp_path):
    """Verify forge config validate validates capabilities for post_run_override."""
    class CriticOnlyAdapter(BaseAdapter):
        CAPABILITIES = {"code_read"}
        def is_available(self): return True
        def execute(self, *args, **kwargs): pass

    class NoShellAdapter(BaseAdapter):
        CAPABILITIES = {"code_edit"}  # lacks shell
        def is_available(self): return True
        def execute(self, *args, **kwargs): pass

    AdapterRegistry.register("critic_only", CriticOnlyAdapter)
    AdapterRegistry.register("no_shell", NoShellAdapter)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        cfg_file = Path("forge.yaml")
        cfg_file.write_text(
            """
version: "2.0"
stages:
  executor:
    adapter: opencode
    post_run_override:
      adapter: no_shell
""",
            encoding="utf-8",
        )

        try:
            res = runner.invoke(main, ["config", "validate"])
            assert res.exit_code == 1
            assert "post_run_override adapter 'no_shell' does not satisfy stage 'executor'" in res.output
        finally:
            AdapterRegistry.unregister("critic_only")
            AdapterRegistry.unregister("no_shell")


# ---------------------------------------------------------------------------
# 3. CLI Stage Execution Abort Tests
# ---------------------------------------------------------------------------

def test_cli_aborts_stage_execution_before_running_incompatible_adapter(tmp_path):
    """Verify forge execute and critic abort with clear error before running an adapter missing capabilities."""
    from forge.storage.run_manager import RunManager

    called = False

    class BrokenAdapter(BaseAdapter):
        CAPABILITIES = {"code_read"}  # missing code_edit and shell
        def is_available(self): return True
        def execute(self, *args, **kwargs):
            nonlocal called
            called = True
            return AdapterResponse(stdout="", stderr="", exit_code=0, duration_seconds=0.1, raw_output="")

    AdapterRegistry.register("broken", BrokenAdapter)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])

        # Create prerequisite run with planner artifact so forge execute proceeds to adapter resolution
        rm = RunManager(Path.cwd())
        run = rm.create_run("Implement feature")
        rm.save_stage_artifacts(run, 2, "planner", "# Plan", {"status": "READY"})

        cfg_file = Path("forge.yaml")
        cfg_file.write_text(
            """
version: "2.0"
stages:
  executor:
    adapter: broken
""",
            encoding="utf-8",
        )

        try:
            res = runner.invoke(main, ["execute"])
            assert res.exit_code == 1
            assert "Capability validation error for stage 'executor'" in res.output
            assert "Executor requires:" in res.output
            assert "Missing:" in res.output
            assert "Abort before execution" in res.output
            assert not called
        finally:
            AdapterRegistry.unregister("broken")
