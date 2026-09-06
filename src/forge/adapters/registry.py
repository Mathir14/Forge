"""Adapter registry and tool discovery."""

import shutil
from typing import Dict, Type, Optional, List, Any
from forge.adapters.base import BaseAdapter
from forge.adapters.opencode import OpenCodeAdapter
from forge.adapters.antigravity import AntigravityAdapter


class AdapterRegistry:
    _ADAPTERS: Dict[str, Type[BaseAdapter]] = {
        "opencode": OpenCodeAdapter,
        "antigravity": AntigravityAdapter,
        "agy": AntigravityAdapter,
    }

    @classmethod
    def get(
        cls,
        name: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        auto_approve: bool = False,
    ) -> BaseAdapter:
        adapter_cls = cls._ADAPTERS.get(name.lower())
        if not adapter_cls:
            raise ValueError(f"Unknown adapter '{name}'. Available: {list(cls._ADAPTERS.keys())}")
        return adapter_cls(model=model, effort=effort, auto_approve=auto_approve)

    @classmethod
    def check_all_tools(cls) -> List[Dict[str, Any]]:
        """Check status of all known CLI tools for forge doctor."""
        tools = [
            {"name": "OpenCode", "binary": "opencode", "desc": "OpenCode CLI Assistant"},
            {"name": "Antigravity", "binary": "agy", "alt_binary": "antigravity", "desc": "Google Antigravity CLI"},
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
