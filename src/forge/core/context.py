"""Execution context for active Forge stages."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from forge.core.run import Run
from forge.core.config import Config
from forge.core.git import GitService
from forge.core.role import Role

if TYPE_CHECKING:
    from forge.core.git import GitBaseline


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

