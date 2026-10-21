import tempfile
from pathlib import Path
import pytest
from forge.core.config import Config


def test_default_config():
    cfg = Config.default()
    assert "architect" in cfg.stages
    assert "executor" in cfg.stages
    assert cfg.stages["executor"].adapter == "antigravity"
    assert cfg.stages["architect"].adapter == "opencode"


def test_load_cascading_config(tmp_path):
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "2.0"
stages:
  architect:
    adapter: custom_adapter
    model: test-model
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)
    assert cfg.version == "2.0"
    assert cfg.stages["architect"].adapter == "custom_adapter"
    assert cfg.stages["architect"].model == "test-model"


def test_stage_config_post_run_override_default():
    cfg = Config.default()
    assert cfg.stages["critic"].post_run_override is None


def test_post_run_override_full_config(tmp_path):
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "1.0"
stages:
  critic:
    adapter: opencode
    model: big-pickle
    effort: low
    auto_approve: false
    extra_flags:
      foo: bar
    post_run_override:
      adapter: antigravity
      model: gemini-3.7-flash-high
      effort: high
      auto_approve: true
      extra_flags:
        baz: qux
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)
    critic = cfg.stages["critic"]
    assert critic.adapter == "opencode"
    assert critic.model == "big-pickle"
    assert critic.effort == "low"
    assert critic.auto_approve is False
    assert critic.extra_flags == {"foo": "bar"}

    override = critic.post_run_override
    assert override is not None
    assert override.adapter == "antigravity"
    assert override.model == "gemini-3.7-flash-high"
    assert override.effort == "high"
    assert override.auto_approve is True
    assert override.extra_flags == {"baz": "qux"}


def test_post_run_override_partial_fallback(tmp_path):
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
    extra_flags:
      base_flag: 123
    post_run_override:
      model: gemini-2.5-pro
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)
    critic = cfg.stages["critic"]
    assert critic.model == "big-pickle"

    override = critic.post_run_override
    assert override is not None
    assert override.adapter == "opencode"
    assert override.model == "gemini-2.5-pro"
    assert override.effort == "high"
    assert override.auto_approve is True
    assert override.extra_flags == {"base_flag": 123}


def test_post_run_override_unset_remains_none(tmp_path):
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "1.0"
stages:
  critic:
    adapter: opencode
    model: big-pickle
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)
    assert cfg.stages["critic"].post_run_override is None
    assert cfg.stages["critic"].model == "big-pickle"


def test_post_run_override_non_dict_ignored(tmp_path):
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "1.0"
stages:
  critic:
    adapter: opencode
    post_run_override: null
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)
    assert cfg.stages["critic"].post_run_override is None


def test_post_run_override_cascade_freshness(tmp_path, monkeypatch):
    """Verify post_run_override does not go stale across cascade levels when a higher level updates base critic."""
    home_dir = tmp_path / "home"
    home_forge = home_dir / ".forge"
    home_forge.mkdir(parents=True)
    (home_forge / "config.yaml").write_text(
        """
stages:
  critic:
    model: model-A
    post_run_override:
      adapter: opencode
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home_dir)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "forge.yaml").write_text(
        """
stages:
  critic:
    model: model-C
""",
        encoding="utf-8",
    )

    cfg = Config.load(project_dir)
    critic = cfg.stages["critic"]
    assert critic.model == "model-C"
    assert critic.post_run_override is not None
    assert critic.post_run_override.adapter == "opencode"
    assert critic.post_run_override.model == "model-C"


def test_defaults_inheritance():
    """Verify stages inherit adapter, model, effort, and timeout from defaults block."""
    cfg = Config.default()
    cfg.defaults.adapter = "opencode"
    cfg.defaults.model = "gpt-5"
    cfg.defaults.effort = "medium"
    cfg.defaults.timeout = 500

    # architect only overrides effort in default
    arch_cfg = cfg.get_stage_config("architect")
    assert arch_cfg.adapter == "opencode"
    assert arch_cfg.model == "gpt-5"
    assert arch_cfg.effort == "high"  # stage override
    assert arch_cfg.timeout == 500  # inherited from defaults


def test_stage_timeout_overrides_default():
    """Verify explicit stage timeout takes precedence over default timeout."""
    cfg = Config.default()
    cfg.defaults.timeout = 300
    cfg.execution.default_timeout = 300

    # executor has timeout: 1200 explicitly set
    exec_cfg = cfg.get_stage_config("executor")
    assert exec_cfg.timeout == 1200

    # critic has timeout: 900 explicitly set
    critic_cfg = cfg.get_stage_config("critic")
    assert critic_cfg.timeout == 900

    # architect inherits default timeout
    arch_cfg = cfg.get_stage_config("architect")
    assert arch_cfg.timeout == 300


def test_config_precedence_deep_merge(tmp_path, monkeypatch):
    """Verify precedence: built-in -> ~/.forge/config.yaml -> ./forge.yaml -> ./.forge/config.yaml."""
    home_dir = tmp_path / "home"
    home_forge = home_dir / ".forge"
    home_forge.mkdir(parents=True)
    (home_forge / "config.yaml").write_text(
        """
defaults:
  model: global-default-model
  effort: low
stages:
  planner:
    model: global-planner-model
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home_dir)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "forge.yaml").write_text(
        """
defaults:
  model: project-default-model
stages:
  architect:
    effort: medium
""",
        encoding="utf-8",
    )

    local_forge = project_dir / ".forge"
    local_forge.mkdir()
    (local_forge / "config.yaml").write_text(
        """
stages:
  architect:
    effort: high
    timeout: 750
""",
        encoding="utf-8",
    )

    cfg = Config.load(project_dir)

    # defaults.model overridden by forge.yaml
    assert cfg.defaults.model == "project-default-model"
    # defaults.effort inherited from ~/.forge/config.yaml
    assert cfg.defaults.effort == "low"

    # planner stage model from ~/.forge/config.yaml
    planner_cfg = cfg.get_stage_config("planner")
    assert planner_cfg.model == "global-planner-model"

    # architect effort and timeout overridden by .forge/config.yaml
    arch_cfg = cfg.get_stage_config("architect")
    assert arch_cfg.effort == "high"
    assert arch_cfg.timeout == 750
    assert arch_cfg.model == "project-default-model"  # inherited from project defaults!


def test_validation_rejects_unknown_adapter():
    """Verify validation rejects unknown adapters."""
    data = {
        "stages": {
            "executor": {
                "adapter": "unknown_ai_provider",
            }
        }
    }
    errors = Config.validate_dict(data)
    assert any("Unknown adapter 'unknown_ai_provider'" in e for e in errors)


def test_validation_rejects_negative_timeout():
    """Verify validation rejects negative timeouts."""
    data = {
        "defaults": {
            "timeout": -30,
        },
        "stages": {
            "critic": {
                "timeout": -500,
            }
        },
        "execution": {
            "default_timeout": -100,
        }
    }
    errors = Config.validate_dict(data)
    assert any("Negative timeout (-30) in defaults" in e for e in errors)
    assert any("Negative timeout (-500) in stage 'critic'" in e for e in errors)
    assert any("Negative timeout (-100) in execution" in e for e in errors)


def test_validation_rejects_invalid_effort():
    """Verify validation rejects invalid effort levels."""
    data = {
        "defaults": {
            "effort": "ultra_maximum",
        },
        "stages": {
            "planner": {
                "effort": "turbo",
            }
        }
    }
    errors = Config.validate_dict(data)
    assert any("Invalid effort 'ultra_maximum' in defaults" in e for e in errors)
    assert any("Invalid effort 'turbo' in stage 'planner'" in e for e in errors)


def test_validation_rejects_invalid_stage_name():
    """Verify validation rejects invalid stage names."""
    data = {
        "stages": {
            "invalid_stage": {
                "adapter": "opencode",
            }
        }
    }
    errors = Config.validate_dict(data)
    assert any("Invalid stage name 'invalid_stage'" in e for e in errors)


def test_validation_rejects_wrong_data_types():
    """Verify validation rejects wrong data types."""
    data = {
        "defaults": "not a dict",
        "stages": ["critic", "architect"],
        "execution": "not a dict",
    }
    errors = Config.validate_dict(data)
    assert any("'defaults' must be a mapping/dictionary" in e for e in errors)
    assert any("'stages' must be a mapping/dictionary" in e for e in errors)
    assert any("'execution' must be a mapping/dictionary" in e for e in errors)


def test_validation_wrong_field_types():
    """Verify validation rejects wrong field types (string for timeout, list for flags)."""
    data = {
        "defaults": {
            "timeout": "not-an-int",
            "extra_flags": "not-a-dict",
            "auto_approve": [1, 2],
        }
    }
    errors = Config.validate_dict(data)
    assert any("Invalid timeout in defaults" in e for e in errors)
    assert any("Invalid extra_flags in defaults" in e for e in errors)
    assert any("Invalid auto_approve in defaults" in e for e in errors)


def test_v1_backwards_compatibility(tmp_path):
    """Verify existing v1 forge.yaml works without modification."""
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "1.0"
stages:
  critic:
    adapter: opencode
    model: null
  executor:
    adapter: antigravity
    model: gemini-3.7-flash-high
    effort: high
    auto_approve: false

execution:
  mode: autonomous
  auto_commit: true
  timeout: 600
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)
    assert cfg.version == "1.0"
    assert cfg.execution.timeout == 600
    assert cfg.execution.default_timeout == 600
    assert cfg.execution.mode == "autonomous"
    assert cfg.execution.auto_commit is True
    assert cfg.stages["executor"].adapter == "antigravity"
    assert cfg.stages["executor"].model == "gemini-3.7-flash-high"
    assert cfg.stages["executor"].effort == "high"


def test_migration_dict():
    """Verify Config.migrate_dict upgrades v1 schema to v2."""
    v1_data = {
        "version": "1.0",
        "stages": {
            "executor": {"adapter": "antigravity"}
        },
        "execution": {
            "timeout": 800,
        }
    }
    migrated = Config.migrate_dict(v1_data)
    assert migrated["version"] == "2.0"
    assert "defaults" in migrated
    assert migrated["defaults"]["adapter"] == "opencode"
    assert migrated["execution"]["default_timeout"] == 800


def test_dot_notation_get_set():
    """Verify get_key_from_dict and set_key_in_dict."""
    data = {
        "defaults": {"adapter": "opencode"},
        "stages": {"executor": {"timeout": 300}},
    }
    assert Config.get_key_from_dict(data, "defaults.adapter") == "opencode"
    assert Config.get_key_from_dict(data, "stages.executor.timeout") == 300

    Config.set_key_in_dict(data, "stages.executor.timeout", 1500)
    assert Config.get_key_from_dict(data, "stages.executor.timeout") == 1500

    Config.set_key_in_dict(data, "stages.critic.effort", "high")
    assert Config.get_key_from_dict(data, "stages.critic.effort") == "high"


# ---------------------------------------------------------------------------
# Post-terminal grace period configuration tests (v0.1.0b18)
# ---------------------------------------------------------------------------

def test_post_terminal_grace_seconds_validation():
    """Verify validation strictly rejects nonnumeric, nonfinite, zero, negative, and bool values."""
    # 1. Negative values
    for bad_val in (-1, -0.5, -100):
        data = {"defaults": {"post_terminal_grace_seconds": bad_val}}
        errors = Config.validate_dict(data)
        assert any("strictly positive (> 0)" in e for e in errors), f"Failed to reject {bad_val}"

    # 2. Zero
    for bad_val in (0, 0.0):
        data = {"defaults": {"post_terminal_grace_seconds": bad_val}}
        errors = Config.validate_dict(data)
        assert any("strictly positive (> 0)" in e for e in errors), f"Failed to reject {bad_val}"

    # 3. Nonnumeric strings and collections
    for bad_val in ("invalid", "abc", [1, 2], {"a": 1}):
        data = {"stages": {"critic": {"post_terminal_grace_seconds": bad_val}}}
        errors = Config.validate_dict(data)
        assert any("expected numeric value" in e for e in errors), f"Failed to reject {bad_val}"

    # 4. Booleans (bool is a subclass of int in Python)
    for bad_val in (True, False):
        data = {"defaults": {"post_terminal_grace_seconds": bad_val}}
        errors = Config.validate_dict(data)
        assert any("expected positive number" in e for e in errors), f"Failed to reject bool {bad_val}"

    # 5. Nonfinite (inf, -inf, nan)
    for bad_val in (float("inf"), float("-inf"), float("nan"), "inf", "-inf", "nan"):
        data = {"stages": {"critic": {"post_run_override": {"post_terminal_grace_seconds": bad_val}}}}
        errors = Config.validate_dict(data)
        assert any("must be finite" in e or "strictly positive" in e for e in errors), f"Failed to reject {bad_val}"

    # 6. Explicit None / null
    data = {"defaults": {"post_terminal_grace_seconds": None}}
    errors = Config.validate_dict(data)
    assert any("expected positive number, got None" in e for e in errors)

    # 7. Valid values pass validation
    for good_val in (5.0, 2.5, 1, 10, "3.5"):
        data = {
            "defaults": {"post_terminal_grace_seconds": good_val},
            "stages": {
                "critic": {
                    "post_terminal_grace_seconds": good_val,
                    "post_run_override": {"post_terminal_grace_seconds": good_val},
                }
            },
        }
        errors = Config.validate_dict(data)
        assert not errors, f"Unexpected validation errors for valid value {good_val}: {errors}"


def test_forge_yaml_override_reaches_selected_adapter(tmp_path: Path):
    """Verify forge.yaml default post_terminal_grace_seconds overrides built-in and reaches adapter."""
    from forge.cli import _get_adapter

    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "2.0"
defaults:
  adapter: "antigravity"
  post_terminal_grace_seconds: 7.5
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)
    assert cfg.defaults.post_terminal_grace_seconds == 7.5

    adapter = _get_adapter(cfg, "critic", exit_on_error=False)
    assert adapter.name == "antigravity"
    assert adapter.post_terminal_grace_seconds == 7.5
    assert adapter.post_terminal_grace_period == 7.5


def test_stage_specific_override_precedence(tmp_path: Path):
    """Verify configuration precedence: stage override > defaults > built-in default (5.0s)."""
    from forge.cli import _get_adapter

    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "2.0"
defaults:
  adapter: "antigravity"
  post_terminal_grace_seconds: 4.0
stages:
  critic:
    post_terminal_grace_seconds: 8.5
    post_run_override:
      post_terminal_grace_seconds: 12.0
  tester:
    adapter: "antigravity"
    # unconfigured grace period on stage -> inherits default 4.0
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)

    # 1. Stage-specific override (critic pre_run)
    critic_pre = cfg.get_stage_config("critic", phase="pre_run")
    assert critic_pre.post_terminal_grace_seconds == 8.5
    critic_pre_adapter = _get_adapter(cfg, "critic", phase="pre_run", exit_on_error=False)
    assert critic_pre_adapter.post_terminal_grace_seconds == 8.5
    assert critic_pre_adapter.post_terminal_grace_period == 8.5

    # 2. Stage-specific post_run_override (critic post_run)
    critic_post = cfg.get_stage_config("critic", phase="post_run")
    assert critic_post.post_terminal_grace_seconds == 12.0
    critic_post_adapter = _get_adapter(cfg, "critic", phase="post_run", exit_on_error=False)
    assert critic_post_adapter.post_terminal_grace_seconds == 12.0
    assert critic_post_adapter.post_terminal_grace_period == 12.0

    # 3. Defaults inheritance when stage does not specify override (tester)
    tester_stage = cfg.get_stage_config("tester", phase="pre_run")
    assert tester_stage.post_terminal_grace_seconds == 4.0
    tester_adapter = _get_adapter(cfg, "tester", phase="pre_run", exit_on_error=False)
    assert tester_adapter.post_terminal_grace_seconds == 4.0
    assert tester_adapter.post_terminal_grace_period == 4.0

    # 4. Built-in default (15.0s) when neither stage nor defaults configure grace period
    clean_cfg = Config.default()
    clean_critic = clean_cfg.get_stage_config("critic", phase="pre_run")
    assert clean_critic.post_terminal_grace_seconds is None
    clean_adapter = _get_adapter(clean_cfg, "critic", phase="pre_run", exit_on_error=False)
    assert clean_adapter.post_terminal_grace_seconds == 15.0
    assert clean_adapter.post_terminal_grace_period == 15.0


def test_both_antigravity_and_opencode_receive_grace_period(tmp_path: Path):
    """Verify both Antigravity and OpenCode adapters receive and configure grace period."""
    from forge.adapters.registry import AdapterRegistry
    from forge.cli import _get_adapter

    # Direct registry resolution
    agy = AdapterRegistry.get("antigravity", post_terminal_grace_seconds=6.5)
    assert agy.post_terminal_grace_seconds == 6.5
    assert agy.post_terminal_grace_period == 6.5

    oc = AdapterRegistry.get("opencode", post_terminal_grace_seconds=6.5)
    assert oc.post_terminal_grace_seconds == 6.5
    assert oc.post_terminal_grace_period == 6.5

    # Via configuration and _get_adapter
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text(
        """
version: "2.0"
defaults:
  post_terminal_grace_seconds: 9.0
stages:
  architect:
    adapter: "opencode"
  critic:
    adapter: "antigravity"
""",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)

    oc_inst = _get_adapter(cfg, "architect", exit_on_error=False)
    assert oc_inst.post_terminal_grace_seconds == 9.0
    assert oc_inst.post_terminal_grace_period == 9.0

    agy_inst = _get_adapter(cfg, "critic", exit_on_error=False)
    assert agy_inst.post_terminal_grace_seconds == 9.0
    assert agy_inst.post_terminal_grace_period == 9.0


def test_adapter_direct_validation():
    """Verify adapter constructors and AdapterRegistry reject invalid grace periods."""
    from forge.adapters.antigravity import AntigravityAdapter
    from forge.adapters.opencode import OpenCodeAdapter
    from forge.adapters.registry import AdapterRegistry

    invalid_values = (-1, 0, float("nan"), float("inf"), "bad", True)
    for bad in invalid_values:
        with pytest.raises(ValueError):
            AntigravityAdapter(post_terminal_grace_seconds=bad)
        with pytest.raises(ValueError):
            AntigravityAdapter(post_terminal_grace_period=bad)
        with pytest.raises(ValueError):
            OpenCodeAdapter(post_terminal_grace_seconds=bad)
        with pytest.raises(ValueError):
            OpenCodeAdapter(post_terminal_grace_period=bad)
        with pytest.raises(ValueError):
            AdapterRegistry.get("antigravity", post_terminal_grace_seconds=bad)
        with pytest.raises(ValueError):
            AdapterRegistry.get("opencode", post_terminal_grace_seconds=bad)

    # Explicit None for post_terminal_grace_period is rejected
    with pytest.raises(ValueError):
        AntigravityAdapter(post_terminal_grace_period=None)
    with pytest.raises(ValueError):
        OpenCodeAdapter(post_terminal_grace_period=None)
