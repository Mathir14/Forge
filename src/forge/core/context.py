"""Execution context for active Forge stages."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from forge.core.run import Run
from forge.core.config import Config
from forge.core.git import GitService
from forge.core.role import Role


@dataclass
class Context:
    run: Run
    project_root: Path
    config: Config
    git: GitService
    current_role: Optional[Role] = None
