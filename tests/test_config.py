import tempfile
from pathlib import Path
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


