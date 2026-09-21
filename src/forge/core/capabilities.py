"""Capability model, registry, and validation for Forge adapters and stages."""

from enum import Enum
from typing import Dict, Set, Optional, Any, Iterable, Union


class Capability(str, Enum):
    """Standard capabilities supported across Forge adapters and required by stages."""

    CODE_READ = "code_read"
    CODE_EDIT = "code_edit"
    SHELL = "shell"
    GIT = "git"
    SESSION_RESUME = "session_resume"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"
    IMAGE_INPUT = "image_input"
    BROWSER = "browser"
    PLAYWRIGHT = "playwright"
    SCREENSHOTS = "screenshots"
    TRACE_COLLECTION = "trace_collection"
    TOOL_CALLING = "tool_calling"
    LONG_RUNNING = "long_running"
    CUSTOM_FLAGS = "custom_flags"
    AUTHENTICATE = "authenticate"
    AUTHORIZE = "authorize"

    def __str__(self) -> str:
        return self.value


class CapabilityDefinition:
    """Metadata describing a capability."""

    def __init__(self, name: str, description: str = ""):
        self.name = str(name)
        self.description = description

    def __repr__(self) -> str:
        return f"CapabilityDefinition(name={self.name!r}, description={self.description!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, CapabilityDefinition):
            return self.name == other.name
        if isinstance(other, (str, Capability)):
            return self.name == str(other)
        return False

    def __hash__(self) -> int:
        return hash(self.name)


class CapabilityValidationError(ValueError):
    """Raised when an adapter does not satisfy the capabilities required by a stage."""

    def __init__(
        self,
        stage_name: str,
        adapter_name: str,
        required_capabilities: Iterable[Union[str, Capability]],
        provided_capabilities: Iterable[Union[str, Capability]],
        missing_capabilities: Optional[Iterable[Union[str, Capability]]] = None,
        custom_message: Optional[str] = None,
    ):
        self.stage_name = str(stage_name).lower()
        self.adapter_name = str(adapter_name).lower()
        self.required_capabilities: Set[str] = {str(c) for c in required_capabilities}
        self.provided_capabilities: Set[str] = {str(c) for c in provided_capabilities}

        if missing_capabilities is not None:
            self.missing_capabilities: Set[str] = {str(c) for c in missing_capabilities}
        else:
            self.missing_capabilities = self.required_capabilities - self.provided_capabilities

        if custom_message:
            message = custom_message
        else:
            req_list = "\n".join(f"  • {c}" for c in sorted(self.required_capabilities))
            missing_list = "\n".join(f"  • {c}" for c in sorted(self.missing_capabilities))
            message = (
                f"Stage '{self.stage_name}' requires capabilities:\n{req_list}\n"
                f"Configured adapter '{self.adapter_name}' is missing:\n{missing_list}\n"
                f"Abort before execution."
            )

        super().__init__(message)


class CapabilityRegistry:
    """Registry of known capabilities and their descriptions."""

    _CAPABILITIES: Dict[str, CapabilityDefinition] = {}

    @classmethod
    def register(cls, name: Union[str, Capability], description: str = "") -> CapabilityDefinition:
        """Register a new capability in the registry."""
        cap_name = str(name).strip().lower()
        if not cap_name:
            raise ValueError("Capability name cannot be empty.")
        defn = CapabilityDefinition(name=cap_name, description=description)
        cls._CAPABILITIES[cap_name] = defn
        return defn

    @classmethod
    def get(cls, name: Union[str, Capability]) -> Optional[CapabilityDefinition]:
        """Look up a capability definition by name."""
        return cls._CAPABILITIES.get(str(name).strip().lower())

    @classmethod
    def all_capabilities(cls) -> Dict[str, CapabilityDefinition]:
        """Return all registered capability definitions."""
        return dict(cls._CAPABILITIES)

    @classmethod
    def all_names(cls) -> Set[str]:
        """Return all registered capability names as a set of strings."""
        return set(cls._CAPABILITIES.keys())

    @classmethod
    def is_valid(cls, name: Union[str, Capability]) -> bool:
        """Check if a capability name is registered."""
        return str(name).strip().lower() in cls._CAPABILITIES

    @classmethod
    def reset(cls) -> None:
        """Reset the registry to standard capabilities."""
        cls._CAPABILITIES.clear()
        cls._register_defaults()

    @classmethod
    def _register_defaults(cls) -> None:
        """Populate registry with standard Forge capabilities."""
        defaults = [
            (Capability.CODE_READ, "Read and inspect source code and files in repository"),
            (Capability.CODE_EDIT, "Modify, create, and write code files in repository"),
            (Capability.SHELL, "Execute arbitrary shell and terminal commands"),
            (Capability.GIT, "Perform Git version control operations"),
            (Capability.SESSION_RESUME, "Resume and continue existing agent sessions"),
            (Capability.STRUCTURED_OUTPUT, "Emit structured YAML/JSON machine-readable reports"),
            (Capability.STREAMING, "Stream real-time tokens or output events"),
            (Capability.IMAGE_INPUT, "Accept and process multimodal images as input"),
            (Capability.BROWSER, "Drive headless or headful web browser sessions"),
            (Capability.PLAYWRIGHT, "Automate end-to-end browser workflows with Playwright"),
            (Capability.SCREENSHOTS, "Capture visual page screenshots and UI states"),
            (Capability.TRACE_COLLECTION, "Record and export execution traces and telemetry"),
            (Capability.TOOL_CALLING, "Invoke native functions or external tools"),
            (Capability.LONG_RUNNING, "Sustain long-running autonomous execution without timeouts"),
            (Capability.CUSTOM_FLAGS, "Pass custom CLI arguments to underlying tool"),
            (Capability.AUTHENTICATE, "Authenticate agent identities via tokens or credentials"),
            (Capability.AUTHORIZE, "Authorize stage actions based on role-based permissions"),
        ]
        for cap, desc in defaults:
            cls.register(cap, desc)


# Initialize default capabilities
CapabilityRegistry._register_defaults()
