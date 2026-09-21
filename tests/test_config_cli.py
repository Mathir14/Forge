"""Tests for forge config CLI commands."""

import json
from pathlib import Path
from unittest.mock import patch
import yaml
from click.testing import CliRunner

from forge.cli import main
from forge.core.templates import DEFAULT_FORGE_YAML


def test_cli_config_show_default(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["config", "show"])
        assert result.exit_code == 0
        assert "version: '2.0'" in result.output or 'version: "2.0"' in result.output
        assert "defaults:" in result.output
        assert "executor:" in result.output


def test_cli_config_show_json(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["config", "show", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["version"] == "2.0"
        assert "defaults" in data
        assert data["stages"]["executor"]["adapter"] == "antigravity"


def test_cli_config_show_raw_missing(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["config", "show", "--raw"])
        assert result.exit_code == 0
        assert "No project configuration file found" in result.output


def test_cli_config_show_raw_existing(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("forge.yaml").write_text("version: '2.0'\ndefaults:\n  effort: low\n", encoding="utf-8")
        result = runner.invoke(main, ["config", "show", "--raw"])
        assert result.exit_code == 0
        assert "effort: low" in result.output


def test_cli_config_get_existing(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["config", "get", "defaults.adapter"])
        assert result.exit_code == 0
        assert result.output.strip() == "opencode"

        result2 = runner.invoke(main, ["config", "get", "stages.executor.timeout"])
        assert result2.exit_code == 0
        assert result2.output.strip() == "1200"


def test_cli_config_get_missing_key(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["config", "get", "stages.nonexistent.key"])
        assert result.exit_code == 1
        assert "Error: Configuration key 'stages.nonexistent.key' not found" in result.output


def test_cli_config_set_valid_scalar(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("forge.yaml").write_text(DEFAULT_FORGE_YAML, encoding="utf-8")
        result = runner.invoke(main, ["config", "set", "stages.executor.timeout", "1500"])
        assert result.exit_code == 0
        assert "Updated stages.executor.timeout = 1500" in result.output

        # Verify persisted
        with open("forge.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["stages"]["executor"]["timeout"] == 1500


def test_cli_config_set_creates_file_if_missing(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        assert not Path("forge.yaml").exists()
        result = runner.invoke(main, ["config", "set", "defaults.adapter", "opencode"])
        assert result.exit_code == 0
        assert Path("forge.yaml").exists()


def test_cli_config_set_validation_rejects_and_preserves_config(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        original_content = "version: '2.0'\ndefaults:\n  adapter: opencode\n"
        Path("forge.yaml").write_text(original_content, encoding="utf-8")

        # Attempt invalid adapter
        res_bad_adapter = runner.invoke(main, ["config", "set", "defaults.adapter", "bad_provider"])
        assert res_bad_adapter.exit_code == 1
        assert "Validation failed" in res_bad_adapter.output
        assert "Config file was NOT modified" in res_bad_adapter.output

        # Attempt negative timeout
        res_bad_timeout = runner.invoke(main, ["config", "set", "defaults.timeout", "-50"])
        assert res_bad_timeout.exit_code == 1
        assert "Negative timeout" in res_bad_timeout.output

        # Attempt invalid effort
        res_bad_effort = runner.invoke(main, ["config", "set", "defaults.effort", "super_fast"])
        assert res_bad_effort.exit_code == 1
        assert "Invalid effort 'super_fast'" in res_bad_effort.output

        # Verify file untouched (never corrupted)
        assert Path("forge.yaml").read_text(encoding="utf-8") == original_content


def test_cli_config_validate_command(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("forge.yaml").write_text(DEFAULT_FORGE_YAML, encoding="utf-8")
        res_valid = runner.invoke(main, ["config", "validate"])
        assert res_valid.exit_code == 0
        assert "valid" in res_valid.output

        # Make file invalid
        Path("forge.yaml").write_text("stages:\n  executor:\n    timeout: -10\n", encoding="utf-8")
        res_invalid = runner.invoke(main, ["config", "validate"])
        assert res_invalid.exit_code == 1
        assert "Negative timeout" in res_invalid.output


def test_cli_config_validate_specific_path(tmp_path):
    runner = CliRunner()
    custom_cfg = tmp_path / "custom.yaml"
    custom_cfg.write_text("defaults:\n  adapter: unknown_tool\n", encoding="utf-8")

    result = runner.invoke(main, ["config", "validate", "-p", str(custom_cfg)])
    assert result.exit_code == 1
    assert "Unknown adapter 'unknown_tool'" in result.output


def test_cli_config_reset_with_force(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("forge.yaml").write_text("custom: content\n", encoding="utf-8")
        result = runner.invoke(main, ["config", "reset", "--force"])
        assert result.exit_code == 0
        assert "Reset configuration to defaults" in result.output

        with open("forge.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["version"] == "2.0"
        assert "defaults" in data


def test_cli_config_reset_prompt_cancel(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        original = "version: '2.0'\n"
        Path("forge.yaml").write_text(original, encoding="utf-8")
        result = runner.invoke(main, ["config", "reset"], input="n\n")
        assert result.exit_code == 0
        assert "Reset cancelled" in result.output
        assert Path("forge.yaml").read_text(encoding="utf-8") == original


def test_cli_config_edit_valid(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("forge.yaml").write_text(DEFAULT_FORGE_YAML, encoding="utf-8")
        edited = DEFAULT_FORGE_YAML.replace("effort: medium", "effort: low")
        with patch("click.edit", return_value=edited):
            result = runner.invoke(main, ["config", "edit"])
            assert result.exit_code == 0
            assert "updated and validated successfully" in result.output
            assert "effort: low" in Path("forge.yaml").read_text(encoding="utf-8")


def test_cli_config_edit_invalid_syntax_preserves_file(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        original = DEFAULT_FORGE_YAML
        Path("forge.yaml").write_text(original, encoding="utf-8")
        bad_syntax = "stages:\n  critic: [unclosed"
        with patch("click.edit", return_value=bad_syntax):
            result = runner.invoke(main, ["config", "edit"])
            assert result.exit_code == 1
            assert "YAML Syntax Error" in result.output
            assert "NOT saved" in result.output
            assert Path("forge.yaml").read_text(encoding="utf-8") == original


def test_cli_config_edit_validation_failure_preserves_file(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        original = DEFAULT_FORGE_YAML
        Path("forge.yaml").write_text(original, encoding="utf-8")
        invalid_cfg = DEFAULT_FORGE_YAML.replace("timeout: 300", "timeout: -999")
        with patch("click.edit", return_value=invalid_cfg):
            result = runner.invoke(main, ["config", "edit"])
            assert result.exit_code == 1
            assert "Validation failed" in result.output
            assert "NOT saved" in result.output
            assert Path("forge.yaml").read_text(encoding="utf-8") == original
