"""Configuration management for Forge (Configuration v2)."""

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Any, List, Set, Tuple
import yaml

DEFAULT_TIMEOUT: int = 300


def _get_valid_stages() -> Set[str]:
    try:
        from forge.stages.definition import StageOrder
        from forge.stages.requirements import StageRequirementsRegistry
        return StageOrder.all_stage_names() | set(StageRequirementsRegistry.all_requirements().keys())
    except Exception:
        return {"critic", "architect", "planner", "executor", "reviewer", "tester"}


VALID_STAGES: Set[str] = _get_valid_stages()
VALID_EFFORTS: Set[str] = {"low", "medium", "high", "max", "none"}


class ConfigValidationError(ValueError):
    """Raised when configuration validation fails."""

    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or [message]


@dataclass
class DefaultsConfig:
    adapter: str = "opencode"
    model: Optional[str] = None
    effort: Optional[str] = "medium"
    timeout: int = DEFAULT_TIMEOUT
    auto_approve: bool = False
    auth_method: str = "api_key"
    auth_provider: Optional[str] = None
    auth_scopes: str = ""
    auth_token_ttl: int = 3600
    extra_flags: Dict[str, Any] = field(default_factory=dict)
    _explicit_fields: Set[str] = field(default_factory=set, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        res: Dict[str, Any] = {
            "adapter": self.adapter,
            "model": self.model,
            "effort": self.effort,
            "timeout": self.timeout,
            "auto_approve": self.auto_approve,
        }
        if self.extra_flags:
            res["extra_flags"] = dict(self.extra_flags)
        return res


@dataclass
class StageConfig:
    adapter: str = "opencode"
    model: Optional[str] = None
    effort: Optional[str] = None
    timeout: Optional[int] = None
    auto_approve: bool = False
    extra_flags: Dict[str, Any] = field(default_factory=dict)
    post_run_override: Optional["StageConfig"] = None
    _explicit_fields: Set[str] = field(default_factory=set, repr=False)

    def to_dict(self, explicit_only: bool = False) -> Dict[str, Any]:
        if explicit_only and self._explicit_fields:
            res: Dict[str, Any] = {}
            if "adapter" in self._explicit_fields:
                res["adapter"] = self.adapter
            if "model" in self._explicit_fields:
                res["model"] = self.model
            if "effort" in self._explicit_fields:
                res["effort"] = self.effort
            if "timeout" in self._explicit_fields:
                res["timeout"] = self.timeout
            if "auto_approve" in self._explicit_fields:
                res["auto_approve"] = self.auto_approve
            if "extra_flags" in self._explicit_fields and self.extra_flags:
                res["extra_flags"] = dict(self.extra_flags)
            if self.post_run_override:
                res["post_run_override"] = self.post_run_override.to_dict(explicit_only=explicit_only)
            return res

        res = {
            "adapter": self.adapter,
            "model": self.model,
            "effort": self.effort,
            "timeout": self.timeout,
            "auto_approve": self.auto_approve,
        }
        if self.extra_flags:
            res["extra_flags"] = dict(self.extra_flags)
        if self.post_run_override:
            res["post_run_override"] = self.post_run_override.to_dict(explicit_only=explicit_only)
        return res


@dataclass
class ExecutionConfig:
    mode: str = "interactive"  # "interactive" or "autonomous"
    auto_commit: bool = False
    default_timeout: int = DEFAULT_TIMEOUT
    _timeout: Optional[int] = field(default=None, repr=False)

    @property
    def timeout(self) -> int:
        """Backward compatibility alias for execution.timeout."""
        if self._timeout is not None:
            return self._timeout
        return self.default_timeout

    @timeout.setter
    def timeout(self, value: Any) -> None:
        int_val = int(value)
        self._timeout = int_val
        self.default_timeout = int_val

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "auto_commit": self.auto_commit,
            "default_timeout": self.default_timeout,
        }


@dataclass
class Config:
    version: str = "2.0"
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    stages: Dict[str, StageConfig] = field(default_factory=dict)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    @classmethod
    def default(cls) -> "Config":
        """Generate built-in configuration defaults."""
        defaults = DefaultsConfig(
            adapter="opencode",
            model=None,
            effort="medium",
            timeout=DEFAULT_TIMEOUT,
            auto_approve=False,
            extra_flags={},
        )
        stages = {
            "critic": StageConfig(
                adapter="opencode",
                model=None,
                effort="high",
                timeout=900,
                auto_approve=False,
                extra_flags={},
                _explicit_fields={"effort", "timeout"},
            ),
            "architect": StageConfig(
                adapter="opencode",
                model=None,
                effort="high",
                timeout=None,
                auto_approve=False,
                extra_flags={},
                _explicit_fields={"effort"},
            ),
            "planner": StageConfig(
                adapter="opencode",
                model=None,
                effort="high",
                timeout=None,
                auto_approve=False,
                extra_flags={},
                _explicit_fields={"effort"},
            ),
            "executor": StageConfig(
                adapter="antigravity",
                model="gemini-3.7-flash-high",
                effort="high",
                timeout=1200,
                auto_approve=False,
                extra_flags={},
                _explicit_fields={"adapter", "model", "effort", "timeout", "auto_approve"},
            ),
            "reviewer": StageConfig(
                adapter="opencode",
                model=None,
                effort="high",
                timeout=None,
                auto_approve=False,
                extra_flags={},
                _explicit_fields={"effort"},
            ),
        }
        execution = ExecutionConfig(
            mode="interactive",
            auto_commit=False,
            default_timeout=DEFAULT_TIMEOUT,
        )
        cfg = cls(version="2.0", defaults=defaults, stages=stages, execution=execution)
        cls._apply_inheritance(cfg)
        return cfg

    @classmethod
    def _coerce_bool(cls, value: Any) -> bool:
        """Strictly coerce a YAML value to bool, rejecting ambiguous inputs."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "yes", "1", "on"):
                return True
            if normalized in ("false", "no", "0", "off", ""):
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        raise ValueError(
            f"Cannot interpret {value!r} as a boolean; "
            "expected one of true/false/yes/no/1/0/on/off."
        )

    def get_stage_config(self, stage_name: str, phase: str = "pre_run") -> StageConfig:
        """Resolve full stage configuration with defaults inheritance and phase awareness."""
        base_stage = self.stages.get(stage_name)
        if base_stage is None:
            # Fallback for unconfigured stage: fully inherit from defaults
            base_stage = StageConfig(
                adapter=self.defaults.adapter,
                model=self.defaults.model,
                effort=self.defaults.effort,
                timeout=self.defaults.timeout or self.execution.default_timeout,
                auto_approve=self.defaults.auto_approve,
                extra_flags=dict(self.defaults.extra_flags),
            )

        # Base resolution with inheritance from defaults
        explicit = getattr(base_stage, "_explicit_fields", set())

        if "adapter" in explicit or (not explicit and base_stage.adapter is not None):
            adapter = base_stage.adapter
        else:
            adapter = self.defaults.adapter

        if "model" in explicit or (not explicit and base_stage.model is not None):
            model = base_stage.model
        else:
            model = self.defaults.model

        if "effort" in explicit or (not explicit and base_stage.effort is not None):
            effort = base_stage.effort
        else:
            effort = self.defaults.effort

        if "timeout" in explicit or (not explicit and base_stage.timeout is not None):
            timeout = base_stage.timeout
        else:
            if self.execution._timeout is not None or self.execution.default_timeout != DEFAULT_TIMEOUT:
                timeout = self.execution.default_timeout
            else:
                timeout = self.defaults.timeout or self.execution.default_timeout

        if "auto_approve" in explicit or (not explicit and base_stage.auto_approve is not None):
            auto_approve = base_stage.auto_approve
        else:
            auto_approve = self.defaults.auto_approve

        extra_flags = dict(self.defaults.extra_flags)
        if base_stage.extra_flags:
            extra_flags.update(base_stage.extra_flags)

        resolved = StageConfig(
            adapter=adapter,
            model=model,
            effort=effort,
            timeout=timeout,
            auto_approve=auto_approve,
            extra_flags=extra_flags,
            post_run_override=base_stage.post_run_override,
            _explicit_fields=set(explicit),
        )

        # Apply post_run_override if requested
        if phase == "post_run" and base_stage.post_run_override:
            ovr = base_stage.post_run_override
            ovr_explicit = getattr(ovr, "_explicit_fields", set())

            if "adapter" in ovr_explicit or (not ovr_explicit and ovr.adapter is not None):
                override_adapter = ovr.adapter
            else:
                override_adapter = resolved.adapter

            if "model" in ovr_explicit or (not ovr_explicit and ovr.model is not None):
                override_model = ovr.model
            else:
                override_model = resolved.model

            if "effort" in ovr_explicit or (not ovr_explicit and ovr.effort is not None):
                override_effort = ovr.effort
            else:
                override_effort = resolved.effort

            if "timeout" in ovr_explicit or (not ovr_explicit and ovr.timeout is not None):
                override_timeout = ovr.timeout
            else:
                override_timeout = resolved.timeout

            if "auto_approve" in ovr_explicit or (not ovr_explicit and ovr.auto_approve is not None):
                override_auto_approve = ovr.auto_approve
            else:
                override_auto_approve = resolved.auto_approve

            override_flags = dict(resolved.extra_flags)
            if ovr.extra_flags:
                override_flags.update(ovr.extra_flags)

            return StageConfig(
                adapter=override_adapter,
                model=override_model,
                effort=override_effort,
                timeout=override_timeout,
                auto_approve=override_auto_approve,
                extra_flags=override_flags,
                _explicit_fields=ovr_explicit,
            )

        return resolved

    @classmethod
    def get_project_config_paths(cls, project_root: Optional[Path] = None) -> List[Path]:
        """Return project config file paths in ascending order of precedence: [forge.yaml, .forge/config.yaml]."""
        root = project_root or Path.cwd()
        return [root / "forge.yaml", root / ".forge" / "config.yaml"]

    @classmethod
    def get_all_config_paths(cls, project_root: Optional[Path] = None) -> List[Path]:
        """Return all config paths in ascending order of precedence: [~/.forge/config.yaml, forge.yaml, .forge/config.yaml]."""
        root = project_root or Path.cwd()
        return [
            Path.home() / ".forge" / "config.yaml",
            root / "forge.yaml",
            root / ".forge" / "config.yaml",
        ]

    @classmethod
    def resolve_active_project_config_file(cls, project_root: Optional[Path] = None) -> Optional[Path]:
        """Return the highest-precedence existing project config file (.forge/config.yaml > forge.yaml)."""
        root = project_root or Path.cwd()
        for p in [root / ".forge" / "config.yaml", root / "forge.yaml"]:
            if p.exists() and p.is_file():
                return p
        return None

    @classmethod
    def resolve_write_target(cls, project_root: Optional[Path] = None, is_global: bool = False) -> Path:
        """Resolve the target config file for write operations (set, edit, reset).

        Precedence: global (~/.forge/config.yaml) if is_global,
        else .forge/config.yaml if it exists, else forge.yaml.
        """
        if is_global:
            return Path.home() / ".forge" / "config.yaml"
        root = project_root or Path.cwd()
        if (root / ".forge" / "config.yaml").exists():
            return root / ".forge" / "config.yaml"
        return root / "forge.yaml"

    @classmethod
    def load(cls, project_root: Optional[Path] = None) -> "Config":
        """Load configuration cascading: Built-in -> ~/.forge/config.yaml -> ./forge.yaml -> ./.forge/config.yaml."""
        cfg = cls.default()
        root = project_root or Path.cwd()

        paths_to_check = cls.get_all_config_paths(root)

        for p in paths_to_check:
            if p.exists() and p.is_file():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    if data is None:
                        continue
                    if not isinstance(data, dict):
                        logging.warning(
                            "Config file %s must contain a YAML mapping at top level, got %s",
                            p,
                            type(data).__name__,
                        )
                        continue
                    cls._merge_dict(cfg, data)
                except Exception as e:
                    logging.warning("Failed to parse config file %s: %s", p, e)

        # Apply inheritance to ensure consistency across stages
        cls._apply_inheritance(cfg)
        return cfg

    @classmethod
    def _apply_inheritance(cls, cfg: "Config") -> None:
        """Propagate defaults down to stages where fields were not explicitly overridden."""
        for stage_name, stage_cfg in cfg.stages.items():
            if "adapter" not in stage_cfg._explicit_fields:
                stage_cfg.adapter = cfg.defaults.adapter
            if "model" not in stage_cfg._explicit_fields:
                stage_cfg.model = cfg.defaults.model
            if "effort" not in stage_cfg._explicit_fields:
                stage_cfg.effort = cfg.defaults.effort
            if "timeout" not in stage_cfg._explicit_fields:
                stage_cfg.timeout = cfg.defaults.timeout or cfg.execution.default_timeout
            if "auto_approve" not in stage_cfg._explicit_fields:
                stage_cfg.auto_approve = cfg.defaults.auto_approve
            if "auth_method" not in stage_cfg._explicit_fields:
                stage_cfg.auth_method = cfg.defaults.auth_method
            if "auth_provider" not in stage_cfg._explicit_fields:
                stage_cfg.auth_provider = cfg.defaults.auth_provider
            if "auth_scopes" not in stage_cfg._explicit_fields:
                stage_cfg.auth_scopes = cfg.defaults.auth_scopes
            if "auth_token_ttl" not in stage_cfg._explicit_fields:
                stage_cfg.auth_token_ttl = cfg.defaults.auth_token_ttl

            # Deep merge extra_flags: defaults.extra_flags updated with stage_cfg.extra_flags
            if cfg.defaults.extra_flags:
                merged_flags = dict(cfg.defaults.extra_flags)
                merged_flags.update(stage_cfg.extra_flags)
                stage_cfg.extra_flags = merged_flags

            # Inherit into post_run_override if present
            if stage_cfg.post_run_override:
                ovr = stage_cfg.post_run_override
                ovr_explicit = getattr(ovr, "_explicit_fields", set())
                if "adapter" not in ovr_explicit:
                    ovr.adapter = stage_cfg.adapter
                if "model" not in ovr_explicit:
                    ovr.model = stage_cfg.model
                if "effort" not in ovr_explicit:
                    ovr.effort = stage_cfg.effort
                if "timeout" not in ovr_explicit:
                    ovr.timeout = stage_cfg.timeout
                if "auto_approve" not in ovr_explicit:
                    ovr.auto_approve = stage_cfg.auto_approve
                if "extra_flags" not in ovr_explicit and stage_cfg.extra_flags:
                    merged_ovr_flags = dict(stage_cfg.extra_flags)
                    merged_ovr_flags.update(ovr.extra_flags)
                    ovr.extra_flags = merged_ovr_flags

    @classmethod
    def _merge_dict(cls, cfg: "Config", data: dict) -> None:
        """Deep merge raw dictionary configuration into a Config instance."""
        if "version" in data:
            cfg.version = str(data["version"])

        # 1. Defaults block
        if "defaults" in data and isinstance(data["defaults"], dict):
            d_data = data["defaults"]
            if "adapter" in d_data and d_data["adapter"] is not None:
                cfg.defaults.adapter = str(d_data["adapter"])
                cfg.defaults._explicit_fields.add("adapter")
            if "model" in d_data:
                cfg.defaults.model = str(d_data["model"]) if d_data["model"] is not None else None
                cfg.defaults._explicit_fields.add("model")
            if "effort" in d_data:
                cfg.defaults.effort = str(d_data["effort"]) if d_data["effort"] is not None else None
                cfg.defaults._explicit_fields.add("effort")
            if "timeout" in d_data and d_data["timeout"] is not None:
                try:
                    cfg.defaults.timeout = int(d_data["timeout"])
                    cfg.defaults._explicit_fields.add("timeout")
                except (ValueError, TypeError) as e:
                    logging.warning("Invalid defaults.timeout: %s", e)
            if "auto_approve" in d_data:
                try:
                    cfg.defaults.auto_approve = cls._coerce_bool(d_data["auto_approve"])
                    cfg.defaults._explicit_fields.add("auto_approve")
                except ValueError as e:
                    logging.warning("Invalid defaults.auto_approve: %s", e)
            if "auth_method" in d_data:
                cfg.defaults.auth_method = str(d_data["auth_method"]) if d_data["auth_method"] is not None else "api_key"
                cfg.defaults._explicit_fields.add("auth_method")
            if "auth_provider" in d_data:
                cfg.defaults.auth_provider = str(d_data["auth_provider"]) if d_data["auth_provider"] is not None else None
                cfg.defaults._explicit_fields.add("auth_provider")
            if "auth_scopes" in d_data:
                cfg.defaults.auth_scopes = str(d_data["auth_scopes"]) if d_data["auth_scopes"] is not None else ""
                cfg.defaults._explicit_fields.add("auth_scopes")
            if "auth_token_ttl" in d_data:
                try:
                    cfg.defaults.auth_token_ttl = int(d_data["auth_token_ttl"])
                    cfg.defaults._explicit_fields.add("auth_token_ttl")
                except (ValueError, TypeError) as e:
                    logging.warning("Invalid defaults.auth_token_ttl: %s", e)
            if "extra_flags" in d_data and isinstance(d_data["extra_flags"], dict):
                cfg.defaults.extra_flags.update(d_data["extra_flags"])
                cfg.defaults._explicit_fields.add("extra_flags")

        # 2. Execution block
        if "execution" in data and isinstance(data["execution"], dict):
            exec_data = data["execution"]
            if "mode" in exec_data:
                cfg.execution.mode = str(exec_data["mode"])
            if "auto_commit" in exec_data:
                try:
                    cfg.execution.auto_commit = cls._coerce_bool(exec_data["auto_commit"])
                except ValueError as e:
                    logging.warning("Invalid auto_commit value: %s", e)
            if "default_timeout" in exec_data and exec_data["default_timeout"] is not None:
                try:
                    val = int(exec_data["default_timeout"])
                    cfg.execution.default_timeout = val
                    cfg.execution.timeout = val
                except (ValueError, TypeError) as e:
                    logging.warning("Invalid default_timeout value: %s", e)
            elif "timeout" in exec_data and exec_data["timeout"] is not None:
                try:
                    val = int(exec_data["timeout"])
                    cfg.execution.default_timeout = val
                    cfg.execution.timeout = val
                except (ValueError, TypeError) as e:
                    logging.warning("Invalid timeout value: %s", e)

        # 3. Stages block
        if "stages" in data and isinstance(data["stages"], dict):
            for stage_name, stage_data in data["stages"].items():
                if not isinstance(stage_data, dict):
                    continue

                existing = cfg.stages.get(stage_name)
                explicit = set(existing._explicit_fields) if existing else set()
                explicit.update(stage_data.keys())

                # Determine extra_flags
                extra_flags = dict(existing.extra_flags) if existing else {}
                if "extra_flags" in stage_data and isinstance(stage_data["extra_flags"], dict):
                    extra_flags.update(stage_data["extra_flags"])

                # Determine auto_approve
                if "auto_approve" in stage_data:
                    try:
                        auto_approve = cls._coerce_bool(stage_data["auto_approve"])
                    except ValueError as e:
                        logging.warning("Invalid auto_approve for stage %s: %s", stage_name, e)
                        auto_approve = existing.auto_approve if existing else False
                else:
                    auto_approve = existing.auto_approve if existing else False

                # Determine timeout
                timeout_val = existing.timeout if existing else None
                if "timeout" in stage_data:
                    try:
                        timeout_val = int(stage_data["timeout"]) if stage_data["timeout"] is not None else None
                    except (ValueError, TypeError) as e:
                        logging.warning("Invalid timeout for stage %s: %s", stage_name, e)

                # Determine adapter, model, effort
                adapter = stage_data.get("adapter", existing.adapter if existing else None)
                model = stage_data["model"] if "model" in stage_data else (existing.model if existing else None)
                effort = stage_data["effort"] if "effort" in stage_data else (existing.effort if existing else None)

                new_stage = StageConfig(
                    adapter=adapter,
                    model=model,
                    effort=effort,
                    timeout=timeout_val,
                    auto_approve=auto_approve,
                    extra_flags=extra_flags,
                    _explicit_fields=explicit,
                )

                # Handle post_run_override
                if "post_run_override" in stage_data:
                    override_data = stage_data["post_run_override"]
                    if isinstance(override_data, dict):
                        ovr_explicit = set(override_data.keys())
                        if existing and existing.post_run_override:
                            ovr_explicit.update(getattr(existing.post_run_override, "_explicit_fields", set()))

                        prev_ovr = existing.post_run_override if existing else None
                        ovr_adapter = override_data.get("adapter", prev_ovr.adapter if prev_ovr else new_stage.adapter)
                        ovr_model = (
                            override_data["model"]
                            if "model" in override_data
                            else (prev_ovr.model if prev_ovr else new_stage.model)
                        )
                        ovr_effort = (
                            override_data["effort"]
                            if "effort" in override_data
                            else (prev_ovr.effort if prev_ovr else new_stage.effort)
                        )

                        ovr_timeout = prev_ovr.timeout if prev_ovr else new_stage.timeout
                        if "timeout" in override_data:
                            try:
                                ovr_timeout = (
                                    int(override_data["timeout"])
                                    if override_data["timeout"] is not None
                                    else None
                                )
                            except (ValueError, TypeError) as e:
                                logging.warning("Invalid timeout for post_run_override: %s", e)

                        ovr_auto_approve = prev_ovr.auto_approve if prev_ovr else new_stage.auto_approve
                        if "auto_approve" in override_data:
                            try:
                                ovr_auto_approve = cls._coerce_bool(override_data["auto_approve"])
                            except ValueError as e:
                                logging.warning("Invalid auto_approve for post_run_override: %s", e)

                        if "extra_flags" in override_data and isinstance(override_data["extra_flags"], dict):
                            if existing and existing.post_run_override and "extra_flags" in getattr(existing.post_run_override, "_explicit_fields", set()):
                                ovr_flags = dict(existing.post_run_override.extra_flags)
                                ovr_flags.update(override_data["extra_flags"])
                            else:
                                ovr_flags = dict(override_data["extra_flags"])
                        elif existing and existing.post_run_override and "extra_flags" in getattr(existing.post_run_override, "_explicit_fields", set()):
                            ovr_flags = dict(existing.post_run_override.extra_flags)
                        else:
                            ovr_flags = dict(new_stage.extra_flags)

                        new_stage.post_run_override = StageConfig(
                            adapter=ovr_adapter,
                            model=ovr_model,
                            effort=ovr_effort,
                            timeout=ovr_timeout,
                            auto_approve=ovr_auto_approve,
                            extra_flags=ovr_flags,
                            _explicit_fields=ovr_explicit,
                        )
                elif existing and existing.post_run_override:
                    prev_ovr = existing.post_run_override
                    ovr_explicit = getattr(prev_ovr, "_explicit_fields", set())

                    ovr_adapter = prev_ovr.adapter if "adapter" in ovr_explicit else new_stage.adapter
                    ovr_model = prev_ovr.model if "model" in ovr_explicit else new_stage.model
                    ovr_effort = prev_ovr.effort if "effort" in ovr_explicit else new_stage.effort
                    ovr_timeout = prev_ovr.timeout if "timeout" in ovr_explicit else new_stage.timeout
                    ovr_auto_approve = prev_ovr.auto_approve if "auto_approve" in ovr_explicit else new_stage.auto_approve
                    ovr_flags = dict(prev_ovr.extra_flags) if "extra_flags" in ovr_explicit else dict(new_stage.extra_flags)

                    new_stage.post_run_override = StageConfig(
                        adapter=ovr_adapter,
                        model=ovr_model,
                        effort=ovr_effort,
                        timeout=ovr_timeout,
                        auto_approve=ovr_auto_approve,
                        extra_flags=ovr_flags,
                        _explicit_fields=ovr_explicit,
                    )

                cfg.stages[stage_name] = new_stage

    @classmethod
    def validate_dict(cls, data: Any, known_adapters: Optional[Set[str]] = None) -> List[str]:
        """Validate a configuration dictionary against schema, types, and constraints."""
        errors: List[str] = []

        if not isinstance(data, dict):
            return [f"Root configuration must be a mapping/dictionary, got {type(data).__name__}"]

        # Resolve known adapters
        if known_adapters is None:
            try:
                from forge.adapters.registry import AdapterRegistry

                known_adapters = {a.lower() for a in AdapterRegistry._ADAPTERS.keys()}
            except Exception:
                known_adapters = {"opencode", "antigravity", "agy"}

        # 1. Defaults block validation
        if "defaults" in data:
            defaults = data["defaults"]
            if not isinstance(defaults, dict):
                errors.append(f"'defaults' must be a mapping/dictionary, got {type(defaults).__name__}")
            else:
                if "adapter" in defaults and defaults["adapter"] is not None:
                    if not isinstance(defaults["adapter"], str) or not defaults["adapter"].strip():
                        errors.append(f"Invalid adapter type in defaults: expected non-empty string, got {defaults['adapter']!r}")
                    elif defaults["adapter"].strip().lower() not in known_adapters:
                        errors.append(f"Unknown adapter '{defaults['adapter']}' in defaults. Valid adapters: {sorted(known_adapters)}")

                if "effort" in defaults and defaults["effort"] is not None:
                    if not isinstance(defaults["effort"], str) or defaults["effort"].strip().lower() not in VALID_EFFORTS:
                        errors.append(f"Invalid effort '{defaults['effort']}' in defaults. Valid effort levels: {sorted(VALID_EFFORTS)}")

                if "timeout" in defaults and defaults["timeout"] is not None:
                    t = defaults["timeout"]
                    if not isinstance(t, int) or isinstance(t, bool):
                        errors.append(f"Invalid timeout in defaults: expected integer, got {type(t).__name__} ({t!r})")
                    elif t < 0:
                        errors.append(f"Negative timeout ({t}) in defaults: timeout must be non-negative")

                if "auto_approve" in defaults and defaults["auto_approve"] is not None:
                    try:
                        cls._coerce_bool(defaults["auto_approve"])
                    except ValueError:
                        errors.append(f"Invalid auto_approve in defaults: cannot coerce {defaults['auto_approve']!r} to boolean")

                if "extra_flags" in defaults and defaults["extra_flags"] is not None:
                    if not isinstance(defaults["extra_flags"], dict):
                        errors.append(f"Invalid extra_flags in defaults: expected mapping/dictionary, got {type(defaults['extra_flags']).__name__}")

        # 2. Stages block validation
        if "stages" in data:
            stages = data["stages"]
            if not isinstance(stages, dict):
                errors.append(f"'stages' must be a mapping/dictionary, got {type(stages).__name__}")
            else:
                for stage_name, stage_data in stages.items():
                    if stage_name.lower() not in VALID_STAGES:
                        errors.append(f"Invalid stage name '{stage_name}' in stages. Valid stages: {sorted(VALID_STAGES)}")
                        continue

                    if not isinstance(stage_data, dict):
                        errors.append(f"Stage '{stage_name}' must be a mapping/dictionary, got {type(stage_data).__name__}")
                        continue

                    # Validate stage fields
                    cls._validate_stage_dict(f"stage '{stage_name}'", stage_data, known_adapters, errors)

                    # Validate post_run_override if present
                    if "post_run_override" in stage_data and stage_data["post_run_override"] is not None:
                        ovr = stage_data["post_run_override"]
                        if not isinstance(ovr, dict):
                            errors.append(f"post_run_override in stage '{stage_name}' must be a mapping/dictionary, got {type(ovr).__name__}")
                        else:
                            cls._validate_stage_dict(f"stage '{stage_name}' post_run_override", ovr, known_adapters, errors)

                    # Validate that configured adapter satisfies stage capability requirements
                    cls._validate_stage_capabilities(stage_name, stage_data, data, known_adapters, errors)

        # 3. Execution block validation
        if "execution" in data:
            execution = data["execution"]
            if not isinstance(execution, dict):
                errors.append(f"'execution' must be a mapping/dictionary, got {type(execution).__name__}")
            else:
                if "mode" in execution and execution["mode"] is not None:
                    if not isinstance(execution["mode"], str) or execution["mode"] not in ("interactive", "autonomous"):
                        errors.append(f"Invalid execution mode '{execution['mode']}': expected 'interactive' or 'autonomous'")

                if "auto_commit" in execution and execution["auto_commit"] is not None:
                    try:
                        cls._coerce_bool(execution["auto_commit"])
                    except ValueError:
                        errors.append(f"Invalid auto_commit in execution: cannot coerce {execution['auto_commit']!r} to boolean")

                for t_key in ("default_timeout", "timeout"):
                    if t_key in execution and execution[t_key] is not None:
                        t = execution[t_key]
                        if not isinstance(t, int) or isinstance(t, bool):
                            errors.append(f"Invalid {t_key} in execution: expected integer, got {type(t).__name__} ({t!r})")
                        elif t < 0:
                            errors.append(f"Negative timeout ({t}) in execution: timeout must be non-negative")

        return errors

    @classmethod
    def _validate_stage_dict(
        cls,
        location: str,
        data: Dict[str, Any],
        known_adapters: Set[str],
        errors: List[str],
    ) -> None:
        """Validate fields within a stage or post_run_override dictionary."""
        if "adapter" in data and data["adapter"] is not None:
            if not isinstance(data["adapter"], str) or not data["adapter"].strip():
                errors.append(f"Invalid adapter in {location}: expected non-empty string, got {data['adapter']!r}")
            elif data["adapter"].strip().lower() not in known_adapters:
                errors.append(f"Unknown adapter '{data['adapter']}' in {location}. Valid adapters: {sorted(known_adapters)}")

        if "effort" in data and data["effort"] is not None:
            if not isinstance(data["effort"], str) or data["effort"].strip().lower() not in VALID_EFFORTS:
                errors.append(f"Invalid effort '{data['effort']}' in {location}. Valid effort levels: {sorted(VALID_EFFORTS)}")

        if "timeout" in data and data["timeout"] is not None:
            t = data["timeout"]
            if not isinstance(t, int) or isinstance(t, bool):
                errors.append(f"Invalid timeout in {location}: expected integer, got {type(t).__name__} ({t!r})")
            elif t < 0:
                errors.append(f"Negative timeout ({t}) in {location}: timeout must be non-negative")

        if "auto_approve" in data and data["auto_approve"] is not None:
            try:
                cls._coerce_bool(data["auto_approve"])
            except ValueError:
                errors.append(f"Invalid auto_approve in {location}: cannot coerce {data['auto_approve']!r} to boolean")

        if "extra_flags" in data and data["extra_flags"] is not None:
            if not isinstance(data["extra_flags"], dict):
                errors.append(f"Invalid extra_flags in {location}: expected mapping/dictionary, got {type(data['extra_flags']).__name__}")

    @classmethod
    def _validate_stage_capabilities(
        cls,
        stage_name: str,
        stage_data: Dict[str, Any],
        root_data: Dict[str, Any],
        known_adapters: Set[str],
        errors: List[str],
    ) -> None:
        """Validate that configured adapter(s) satisfy stage capability requirements."""
        try:
            from forge.adapters.registry import AdapterRegistry
            from forge.stages.requirements import StageRequirementsRegistry
        except ImportError:
            return

        required_caps = StageRequirementsRegistry.get(stage_name)
        if not required_caps:
            return

        # 1. Resolve base adapter for stage
        adapter_name = stage_data.get("adapter")
        if not adapter_name and isinstance(root_data.get("defaults"), dict):
            adapter_name = root_data["defaults"].get("adapter")
        if not adapter_name:
            adapter_name = "opencode"

        clean_adapter = str(adapter_name).strip().lower()
        if clean_adapter in known_adapters and AdapterRegistry.is_registered(clean_adapter):
            try:
                provided_caps = AdapterRegistry.get_capabilities(clean_adapter)
                missing = required_caps - provided_caps
                if missing:
                    errors.append(
                        f"Configured adapter '{clean_adapter}' does not satisfy stage '{stage_name}': "
                        f"missing required capabilities {sorted(missing)}"
                    )
            except Exception as e:
                logging.debug("Error checking adapter capabilities during validation: %s", e)

        # 2. Check post_run_override if present
        if "post_run_override" in stage_data and isinstance(stage_data["post_run_override"], dict):
            ovr = stage_data["post_run_override"]
            ovr_adapter = ovr.get("adapter") or adapter_name
            if ovr_adapter:
                clean_ovr_adapter = str(ovr_adapter).strip().lower()
                if clean_ovr_adapter in known_adapters and AdapterRegistry.is_registered(clean_ovr_adapter):
                    try:
                        provided_ovr_caps = AdapterRegistry.get_capabilities(clean_ovr_adapter)
                        missing_ovr = required_caps - provided_ovr_caps
                        if missing_ovr:
                            errors.append(
                                f"Configured post_run_override adapter '{clean_ovr_adapter}' does not satisfy stage '{stage_name}': "
                                f"missing required capabilities {sorted(missing_ovr)}"
                            )
                    except Exception as e:
                        logging.debug("Error checking post_run_override capabilities during validation: %s", e)

    def validate(self) -> List[str]:
        """Validate self against schema and constraints."""
        return self.validate_dict(self.to_dict())

    def validate_or_raise(self) -> None:
        """Raise ConfigValidationError if self is invalid."""
        errors = self.validate()
        if errors:
            raise ConfigValidationError(
                f"Configuration validation failed:\n" + "\n".join(f"  • {e}" for e in errors),
                errors=errors,
            )

    def to_dict(self, explicit_only: bool = False) -> Dict[str, Any]:
        """Convert Config instance to standard dictionary."""
        self._apply_inheritance(self)
        stages_dict: Dict[str, Any] = {}
        for name, stage in self.stages.items():
            stages_dict[name] = stage.to_dict(explicit_only=explicit_only)

        return {
            "version": self.version,
            "defaults": self.defaults.to_dict(),
            "stages": stages_dict,
            "execution": self.execution.to_dict(),
        }

    def to_yaml(self, explicit_only: bool = False) -> str:
        """Serialize configuration to clean YAML string."""
        return yaml.safe_dump(self.to_dict(explicit_only=explicit_only), sort_keys=False, default_flow_style=False, indent=2)

    @classmethod
    def migrate_dict(cls, data: dict) -> dict:
        """Automatically migrate v1 configuration dictionary to v2 structure."""
        if not isinstance(data, dict):
            return data

        migrated = dict(data)
        migrated["version"] = "2.0"

        # Ensure defaults block exists
        if "defaults" not in migrated or not isinstance(migrated["defaults"], dict):
            migrated["defaults"] = {
                "adapter": "opencode",
                "model": None,
                "effort": "medium",
                "timeout": DEFAULT_TIMEOUT,
                "auto_approve": False,
            }

        # Migrate execution block
        if "execution" in migrated and isinstance(migrated["execution"], dict):
            exec_dict = dict(migrated["execution"])
            if "timeout" in exec_dict and "default_timeout" not in exec_dict:
                exec_dict["default_timeout"] = exec_dict["timeout"]
            migrated["execution"] = exec_dict

        return migrated

    @staticmethod
    def get_key_from_dict(data: dict, key_path: str) -> Any:
        """Retrieve a value by dot-notation key path from a dictionary."""
        parts = key_path.strip().split(".")
        current = data
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                raise KeyError(f"Configuration key '{key_path}' not found.")
            current = current[part]
        return current

    @classmethod
    def set_key_in_dict(cls, data: dict, key_path: str, value: Any) -> None:
        """Set a value by dot-notation key path in a dictionary with in-place path creation."""
        parts = key_path.strip().split(".")
        current = data
        for i, part in enumerate(parts[:-1]):
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]

        leaf = parts[-1]
        current[leaf] = value

    @classmethod
    def save_file_safely(cls, target_path: Path, data: dict) -> None:
        """Safely and atomically write configuration YAML after validation to prevent corruption."""
        errors = cls.validate_dict(data)
        if errors:
            raise ConfigValidationError(
                f"Cannot save invalid configuration:\n" + "\n".join(f"  • {e}" for e in errors),
                errors=errors,
            )

        target_path = Path(target_path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)

        yaml_content = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, indent=2)

        # Atomic write via temporary file in same directory
        temp_fd, temp_file_path = tempfile.mkstemp(dir=target_path.parent, prefix=".forge_cfg_", suffix=".tmp")
        try:
            with open(temp_fd, "w", encoding="utf-8") as f:
                f.write(yaml_content)
                f.flush()
                os.fsync(f.fileno())
            shutil.move(temp_file_path, str(target_path))
        except Exception:
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except OSError:
                    pass
            raise