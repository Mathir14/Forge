"""Stage capability requirements specification and registry."""

from typing import Dict, Set, Iterable, Union, Optional
from forge.core.capabilities import Capability


class StageRequirementsRegistry:
    """Registry mapping stage names to their required capabilities."""

    _REQUIREMENTS: Dict[str, Set[str]] = {}

    @classmethod
    def register(cls, stage_name: str, required_capabilities: Iterable[Union[str, Capability]]) -> None:
        """Register required capabilities for a stage."""
        name = str(stage_name).strip().lower()
        cls._REQUIREMENTS[name] = {str(c) for c in required_capabilities}

    @classmethod
    def get(cls, stage_name: str) -> Set[str]:
        """Get required capabilities for a given stage name."""
        name = str(stage_name).strip().lower()
        if name in cls._REQUIREMENTS:
            return set(cls._REQUIREMENTS[name])
        # Default fallback for unknown stages is code_read
        return {str(Capability.CODE_READ)}

    @classmethod
    def all_requirements(cls) -> Dict[str, Set[str]]:
        """Return a copy of all registered stage requirements."""
        return {k: set(v) for k, v in cls._REQUIREMENTS.items()}

    @classmethod
    def reset(cls) -> None:
        """Reset stage requirements to Forge defaults."""
        cls._REQUIREMENTS.clear()
        cls._register_defaults()

    @classmethod
    def _register_defaults(cls) -> None:
        """Register standard stage requirements."""
        defaults = {
            "critic": {Capability.CODE_READ},
            "architect": {Capability.CODE_READ},
            "planner": {Capability.CODE_READ},
            "executor": {Capability.CODE_EDIT, Capability.SHELL},
            "reviewer": {Capability.CODE_READ},
            "tester": {Capability.SHELL, Capability.PLAYWRIGHT, Capability.SCREENSHOTS},
        }
        for stage, caps in defaults.items():
            cls.register(stage, caps)


# Initialize default stage requirements
StageRequirementsRegistry._register_defaults()
