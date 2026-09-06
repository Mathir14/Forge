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
