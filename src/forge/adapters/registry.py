"""Adapter registry and tool discovery."""

import shutil
from typing import Dict, Type, Optional, List, Any, Set
from forge.adapters.base import BaseAdapter
from forge.adapters.opencode import OpenCodeAdapter
from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.codex import CodexAdapter


class AdapterRegistry:
    _ADAPTERS: Dict[str, Type[BaseAdapter]] = {
        "opencode": OpenCodeAdapter,
        "antigravity": AntigravityAdapter,
        "agy": AntigravityAdapter,
        "codex": CodexAdapter,
    }
    _CANONICAL: Dict[str, Type[BaseAdapter]] = {
        "opencode": OpenCodeAdapter,
        "antigravity": AntigravityAdapter,
        "codex": CodexAdapter,
    }

    @classmethod
    def register(
        cls,
        name: str,
        adapter_cls: Type[BaseAdapter],
        aliases: Optional[List[str]] = None,
    ) -> None:
        """Register an adapter class under a primary name and optional aliases."""
        clean_name = str(name).strip().lower()
        if not clean_name:
            raise ValueError("Adapter name cannot be empty.")
        cls._ADAPTERS[clean_name] = adapter_cls
        cls._CANONICAL[clean_name] = adapter_cls
        if aliases:
            for alias in aliases:
                clean_alias = str(alias).strip().lower()
                if clean_alias:
                    cls._ADAPTERS[clean_alias] = adapter_cls

    @classmethod
    def register_adapter(cls, name: str, aliases: Optional[List[str]] = None):
        """Decorator for registering an adapter class."""
        def decorator(adapter_cls: Type[BaseAdapter]):
            cls.register(name, adapter_cls, aliases=aliases)
            return adapter_cls
        return decorator

    @classmethod
    def unregister(cls, name: str) -> None:
        """Unregister an adapter by name."""
        clean_name = str(name).strip().lower()
        cls._ADAPTERS.pop(clean_name, None)
        cls._CANONICAL.pop(clean_name, None)

    @classmethod
    def is_registered(cls, name: str) -> bool:
        """Check if an adapter is registered."""
        return str(name).strip().lower() in cls._ADAPTERS

    @classmethod
    def get_class(cls, name: str) -> Type[BaseAdapter]:
        """Retrieve the adapter class without instantiating it."""
        clean_name = str(name).strip().lower()
        adapter_cls = cls._ADAPTERS.get(clean_name)
        if not adapter_cls:
            raise ValueError(f"Unknown adapter '{name}'. Available: {sorted(cls._ADAPTERS.keys())}")
        return adapter_cls

    @classmethod
    def get_capabilities(cls, name: str) -> Set[str]:
        """Get capabilities for a registered adapter name."""
        adapter_cls = cls.get_class(name)
        return adapter_cls.get_capabilities()

    @classmethod
    def list_canonical_adapters(cls) -> Dict[str, Type[BaseAdapter]]:
        """Return canonical (non-alias) registered adapters."""
        # Ensure any dynamically added adapters in _ADAPTERS are represented
        result: Dict[str, Type[BaseAdapter]] = dict(cls._CANONICAL)
        for k, v in cls._ADAPTERS.items():
            if v not in result.values():
                result[k] = v
        return result

    @classmethod
    def get(
        cls,
        name: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        auto_approve: bool = False,
        extra_flags: Optional[Dict[str, Any]] = None,
    ) -> BaseAdapter:
        clean_name = str(name).strip().lower()
        adapter_cls = cls.get_class(clean_name)
        if model is None and getattr(adapter_cls, "DEFAULT_MODEL", None):
            model = adapter_cls.DEFAULT_MODEL
        if effort is None and getattr(adapter_cls, "DEFAULT_EFFORT", None):
            effort = adapter_cls.DEFAULT_EFFORT
        instance = adapter_cls(model=model, effort=effort, auto_approve=auto_approve, extra_flags=extra_flags)
        if not getattr(instance, "name", None) or instance.name == instance.__class__.__name__.lower().replace("adapter", ""):
            instance.name = clean_name
        return instance

    @classmethod
    def check_all_tools(cls) -> List[Dict[str, Any]]:
        """Check status of all known CLI tools for forge doctor."""
        tools = [
            {"name": "OpenCode", "binary": "opencode", "desc": "OpenCode CLI Assistant"},
            {"name": "Antigravity", "binary": "agy", "alt_binary": "antigravity", "desc": "Google Antigravity CLI"},
            {"name": "Codex", "binary": "codex", "desc": "OpenAI Codex CLI"},
            {"name": "Claude Code", "binary": "claude", "desc": "Anthropic Claude Code CLI"},
            {"name": "Aider", "binary": "aider", "desc": "Aider AI Pair Programmer"},
            {"name": "Gemini CLI", "binary": "gemini", "desc": "Gemini CLI tool"},
        ]

        results = []
        for tool in tools:
            path = shutil.which(tool["binary"])
            if not path and "alt_binary" in tool:
                path = shutil.which(tool["alt_binary"])
            results.append({
                "name": tool["name"],
                "binary": tool["binary"],
                "found": path is not None,
                "path": path,
                "desc": tool["desc"],
            })
        return results
