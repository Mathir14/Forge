"""Execution context for active Forge stages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from forge.core.run import Run
from forge.core.config import Config
from forge.core.git import GitBaseline, GitService
from forge.core.role import Role


@dataclass
class Context:
    run: Run
    project_root: Path
    config: Config
    git: GitService
    current_role: Optional[Role] = None
    baseline: Optional["GitBaseline"] = None
    repair_feedback: Optional[str] = None
    event_listener: Optional[Any] = None
    abort_event: Optional[Any] = None

