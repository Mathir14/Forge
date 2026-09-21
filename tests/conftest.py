"""Shared pytest fixtures and helpers for Forge test suite."""

from pathlib import Path
from typing import Optional
import yaml
import pytest

from forge.core.config import Config


def configure_automated_execution_environment(project_root: Optional[Path] = None) -> Path:
    """Configure a valid automated execution environment in forge.yaml.

    Sets stages.executor.auto_approve = True so that automated headless pipelines
    can execute without deadlocking on interactive permission confirmation.
    """
    root = project_root or Path.cwd()
    forge_yaml_path = root / "forge.yaml"
    if forge_yaml_path.exists():
        data = yaml.safe_load(forge_yaml_path.read_text(encoding="utf-8")) or {}
    else:
        data = {"version": "2.0"}

    stages = data.setdefault("stages", {})
    if not isinstance(stages, dict):
        stages = {}
        data["stages"] = stages
    executor_cfg = stages.setdefault("executor", {})
    if not isinstance(executor_cfg, dict):
        executor_cfg = {}
        stages["executor"] = executor_cfg
    executor_cfg["auto_approve"] = True

    with open(forge_yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    return forge_yaml_path


def make_automated_config() -> Config:
    """Return a Config instance representing a valid automated execution environment."""
    cfg = Config.default()
    cfg.stages["executor"].auto_approve = True
    cfg.stages["executor"]._explicit_fields.add("auto_approve")
    return cfg


@pytest.fixture
def enable_automated_env():
    """Pytest fixture providing a helper to enable executor.auto_approve in the working directory."""
    return configure_automated_execution_environment
