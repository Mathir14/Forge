"""Shared pytest fixtures and helpers for Forge test suite."""

from pathlib import Path
from typing import Optional
import os
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


@pytest.fixture(autouse=True)
def clean_validator_rules():
    """Ensure custom validator rules do not leak across test boundaries."""
    from forge.protocol.validator import MachineReportValidator
    MachineReportValidator.reset_custom_rules()
    yield
    MachineReportValidator.reset_custom_rules()


@pytest.fixture(autouse=True)
def isolate_test_environment(tmp_path_factory, monkeypatch):
    """Ensure tests run in a completely isolated and deterministic environment."""
    test_home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(test_home))
    monkeypatch.setenv("USERPROFILE", str(test_home))
    monkeypatch.setattr(Path, "home", lambda: test_home)

    # Deterministic Git identity and configuration isolation
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Forge Tester")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "tester@forge.dev")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Forge Tester")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "tester@forge.dev")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(test_home / ".gitconfig"))
    from forge.core.platform import get_null_device
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", get_null_device())

    # Deterministic timezone and UTF-8 encoding
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("PYTHONUTF8", "1")

    # Deterministic PYTHONPATH ensuring subprocesses can resolve forge package from src
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    new_pythonpath = f"{src_dir}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else src_dir
    monkeypatch.setenv("PYTHONPATH", new_pythonpath)
