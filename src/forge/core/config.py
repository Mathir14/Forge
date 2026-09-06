"""Configuration management for Forge."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Any
import yaml


@dataclass
class StageConfig:
    adapter: str = "opencode"
    model: Optional[str] = None
    effort: Optional[str] = None
    auto_approve: bool = False
    extra_flags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionConfig:
    mode: str = "interactive"  # "interactive" or "autonomous"
    auto_commit: bool = False


@dataclass
class Config:
    version: str = "1.0"
    stages: Dict[str, StageConfig] = field(default_factory=dict)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    @classmethod
    def default(cls) -> "Config":
        return cls(
            version="1.0",
            stages={
                "architect": StageConfig(adapter="opencode", model=None),
                "planner": StageConfig(adapter="opencode", model=None),
                "executor": StageConfig(
                    adapter="antigravity",
                    model="gemini-3.7-flash-high",
                    effort="high",
                    auto_approve=True,
                ),
                "reviewer": StageConfig(adapter="opencode", model=None),
            },
            execution=ExecutionConfig(),
        )

    @classmethod
    def load(cls, project_root: Optional[Path] = None) -> "Config":
        """Load configuration cascading from global -> project -> local."""
        cfg = cls.default()
        root = project_root or Path.cwd()

        paths_to_check = [
            Path.home() / ".forge" / "config.yaml",
            root / "forge.yaml",
            root / ".forge" / "config.yaml",
        ]

        for p in paths_to_check:
            if p.exists() and p.is_file():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    if isinstance(data, dict):
                        cls._merge_dict(cfg, data)
                except Exception as e:
                    print(f"Warning: Failed to parse config file {p}: {e}")

        return cfg

    @classmethod
    def _merge_dict(cls, cfg: "Config", data: dict) -> None:
        if "version" in data:
            cfg.version = str(data["version"])
        if "execution" in data and isinstance(data["execution"], dict):
            exec_data = data["execution"]
            if "mode" in exec_data:
                cfg.execution.mode = str(exec_data["mode"])
            if "auto_commit" in exec_data:
                cfg.execution.auto_commit = bool(exec_data["auto_commit"])
        if "stages" in data and isinstance(data["stages"], dict):
            for stage_name, stage_data in data["stages"].items():
                if isinstance(stage_data, dict):
                    existing = cfg.stages.get(stage_name, StageConfig())
                    existing.adapter = stage_data.get("adapter", existing.adapter)
                    existing.model = stage_data.get("model", existing.model)
                    existing.effort = stage_data.get("effort", existing.effort)
                    existing.auto_approve = stage_data.get("auto_approve", existing.auto_approve)
                    if "extra_flags" in stage_data and isinstance(stage_data["extra_flags"], dict):
                        existing.extra_flags.update(stage_data["extra_flags"])
                    cfg.stages[stage_name] = existing
