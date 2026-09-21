"""Tests for phase-aware adapter resolution in Forge CLI."""

from unittest.mock import patch, MagicMock
from forge.cli import _get_adapter
from forge.core.config import Config, StageConfig
from forge.adapters.opencode import OpenCodeAdapter
from forge.adapters.antigravity import AntigravityAdapter
from tests.conftest import configure_automated_execution_environment


def test_phase_resolution_override_set():
    """Verify pre_run (00_critic) and post_run (05_critic) resolve to different adapters/models when override is set."""
    cfg = Config.default()
    cfg.stages["critic"] = StageConfig(
        adapter="opencode",
        model="big-pickle",
        effort="low",
        auto_approve=False,
        post_run_override=StageConfig(
            adapter="antigravity",
            model="gemini-2.5-pro",
            effort="high",
            auto_approve=True,
        ),
    )

    with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
         patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True):
        adapter_pre = _get_adapter(cfg, "critic", phase="pre_run")
        assert isinstance(adapter_pre, OpenCodeAdapter)
        assert adapter_pre.model == "big-pickle"
        assert adapter_pre.effort == "low"
        assert adapter_pre.auto_approve is False

        adapter_post = _get_adapter(cfg, "critic", phase="post_run")
        assert isinstance(adapter_post, AntigravityAdapter)
        assert adapter_post.model == "gemini-2.5-pro"
        assert adapter_post.effort == "high"
        assert adapter_post.auto_approve is True


def test_phase_resolution_override_unset_backward_compat():
    """Verify pre_run and post_run resolve identically when post_run_override is unset (backward compatible)."""
    cfg = Config.default()
    cfg.stages["critic"] = StageConfig(
        adapter="opencode",
        model="big-pickle",
        effort=None,
        auto_approve=False,
        post_run_override=None,
    )

    with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True):
        adapter_pre = _get_adapter(cfg, "critic", phase="pre_run")
        adapter_post = _get_adapter(cfg, "critic", phase="post_run")
        adapter_default = _get_adapter(cfg, "critic")

        assert isinstance(adapter_pre, OpenCodeAdapter)
        assert isinstance(adapter_post, OpenCodeAdapter)
        assert isinstance(adapter_default, OpenCodeAdapter)

        assert adapter_pre.model == "big-pickle"
        assert adapter_post.model == "big-pickle"
        assert adapter_default.model == "big-pickle"


def test_post_run_override_field_fallback_resolution(tmp_path):
    """Verify partial post_run_override falls back field-by-field to base critic config during resolution."""
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "1.0"
stages:
  critic:
    adapter: opencode
    model: big-pickle
    effort: high
    auto_approve: true
    post_run_override:
      model: gemini-2.5-pro
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)

    with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True):
        adapter_pre = _get_adapter(cfg, "critic", phase="pre_run")
        assert isinstance(adapter_pre, OpenCodeAdapter)
        assert adapter_pre.model == "big-pickle"
        assert adapter_pre.effort == "high"
        assert adapter_pre.auto_approve is True

        adapter_post = _get_adapter(cfg, "critic", phase="post_run")
        assert isinstance(adapter_post, OpenCodeAdapter)
        assert adapter_post.model == "gemini-2.5-pro"
        assert adapter_post.effort == "high"
        assert adapter_post.auto_approve is True


def test_executor_phase_resolution_unaffected():
    """Verify executor resolution and default fallback branch are completely independent of phase parameter."""
    cfg = Config.default()

    with patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True):
        exec_pre = _get_adapter(cfg, "executor", phase="pre_run")
        exec_post = _get_adapter(cfg, "executor", phase="post_run")
        exec_default = _get_adapter(cfg, "executor")

        assert isinstance(exec_pre, AntigravityAdapter)
        assert isinstance(exec_post, AntigravityAdapter)
        assert isinstance(exec_default, AntigravityAdapter)

        assert exec_pre.model == AntigravityAdapter.DEFAULT_MODEL
        assert exec_post.model == AntigravityAdapter.DEFAULT_MODEL
        assert exec_default.model == AntigravityAdapter.DEFAULT_MODEL


def test_non_critic_stage_phase_immunity():
    """Verify non-critic stages (architect, planner, reviewer) are immune to phase parameter."""
    cfg = Config.default()

    with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True):
        for stage in ("architect", "planner", "reviewer"):
            pre = _get_adapter(cfg, stage, phase="pre_run")
            post = _get_adapter(cfg, stage, phase="post_run")
            assert pre.name == post.name
            assert pre.model == post.model
            assert pre.effort == post.effort
            assert pre.auto_approve == post.auto_approve


def test_call_site_phase_threading_cli(tmp_path):
    """Verify call sites in cli.py pass pre_run for standalone critic and post_run for closing critic in pipeline and auto."""
    from pathlib import Path
    from click.testing import CliRunner
    from forge.cli import main
    from forge.adapters.base import AdapterResponse

    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "1.0"
stages:
  critic:
    adapter: opencode
    model: pre-run-critic-model
    post_run_override:
      adapter: opencode
      model: post-run-critic-model
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        Path("forge.yaml").write_text(forge_yaml.read_text(encoding="utf-8"), encoding="utf-8")
        configure_automated_execution_environment()

        mock_resp = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )
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

        with patch("forge.cli._get_adapter", wraps=_get_adapter) as spy_adapter, \
             patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=mock_resp):

            # 1. Standalone critic: phase="pre_run"
            res_critic = runner.invoke(main, ["critic", "Test Audit"])
            assert res_critic.exit_code == 0
            critic_calls = [call for call in spy_adapter.call_args_list if call.args[1] == "critic"]
            assert len(critic_calls) == 1
            assert critic_calls[0].kwargs.get("phase") == "pre_run"

        with patch("forge.cli._get_adapter", wraps=_get_adapter) as spy_adapter, \
             patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, rev_resp, mock_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            # 2. Auto pipeline closing critic: phase="post_run"
            res_auto = runner.invoke(main, ["auto", "Test Auto Task"])
            assert res_auto.exit_code == 0
            critic_calls = [call for call in spy_adapter.call_args_list if call.args[1] == "critic"]
            assert len(critic_calls) == 1
            assert critic_calls[0].kwargs.get("phase") == "post_run"

        with patch("forge.cli._get_adapter", wraps=_get_adapter) as spy_adapter, \
             patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, rev_resp, mock_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            # 3. Standard pipeline closing critic: phase="post_run"
            res_run = runner.invoke(main, ["run", "Test Pipeline Task"], input="y\ny\ny\ny\n")
            assert res_run.exit_code == 0
            critic_calls = [call for call in spy_adapter.call_args_list if call.args[1] == "critic"]
            assert len(critic_calls) == 1
            assert critic_calls[0].kwargs.get("phase") == "post_run"


def test_00_critic_and_05_critic_different_adapters_end_to_end(tmp_path):
    """Verify 00_critic uses base adapter and 05_critic uses post_run_override adapter in artifacts."""
    from pathlib import Path
    import json
    from click.testing import CliRunner
    from forge.cli import main
    from forge.adapters.base import AdapterResponse
    from forge.storage.run_manager import RunManager

    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "1.0"
stages:
  critic:
    adapter: opencode
    model: big-pickle
    post_run_override:
      adapter: antigravity
      model: gemini-2.5-pro
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        Path("forge.yaml").write_text(forge_yaml.read_text(encoding="utf-8"), encoding="utf-8")
        configure_automated_execution_environment()

        critic_resp = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED",
        )
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

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", return_value=critic_resp) as mock_oc:

            # Standalone critic (00_critic)
            res1 = runner.invoke(main, ["critic", "Initial pre-run audit"])
            assert res1.exit_code == 0
            assert mock_oc.call_count == 1

        rm = RunManager(Path.cwd())
        run1 = rm.latest()
        assert run1.adapters_used["critic"] == "opencode"

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, rev_resp]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", side_effect=[exec_resp, critic_resp]) as mock_agy:

            # Auto pipeline with post-run closing critic (05_critic)
            res2 = runner.invoke(main, ["auto", "Full loop with closing critic"])
            assert res2.exit_code == 0
            # Executor called once, closing Critic called once on antigravity
            assert mock_agy.call_count == 2

        run2 = rm.latest()
        assert run2.adapters_used["critic"] == "antigravity"


