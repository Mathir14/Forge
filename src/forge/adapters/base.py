"""Base adapter interface for CLI tools."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any


@dataclass
class AdapterResponse:
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    raw_output: str


class BaseAdapter(ABC):
    DEFAULT_TIMEOUT: int = 300

    def __init__(
        self,
        name: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        auto_approve: bool = False,
        extra_flags: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.model = model
        self.effort = effort
        self.auto_approve = auto_approve
        self.extra_flags = extra_flags or {}

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the CLI binary is available in PATH."""
        pass

    @abstractmethod
    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        """Run the prompt against the CLI tool and return response."""
        pass
