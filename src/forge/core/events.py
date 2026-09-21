"""Core execution event model and transport-neutral data contracts."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any


class AgentEventType(str, Enum):
    """Observable lifecycle events emitted during agent execution."""
    CHUNK = "chunk"              # Transport-neutral output, markdown, or token delta
    TOOL_START = "tool_start"    # Agent initiated a tool call (file edit, bash command, etc.)
    TOOL_FINISH = "tool_finish"  # Tool call returned output
    HEARTBEAT = "heartbeat"      # Explicit liveness pulse during long internal compute
    COMPLETE = "complete"        # Normal execution termination
    ERROR = "error"              # Fatal provider error or unrecoverable crash


@dataclass
class ExecutionResult:
    """Transport-neutral, comprehensive final execution payload."""
    exit_code: int
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    raw_output: str = ""         # First-class typed domain field
    token_usage: Dict[str, int] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentEvent:
    """Atomic observable execution event."""
    event_type: AgentEventType
    timestamp: float
    text: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    result: Optional[ExecutionResult] = None  # Populated ONLY on COMPLETE / ERROR
