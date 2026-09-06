"""Immutable Role metadata container."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Role:
    name: str
    sequence_number: int
    template_content: str
    protocol_content: str = ""

    @classmethod
    def load(cls, name: str, project_root: Optional[Path] = None) -> "Role":
        root = project_root or Path.cwd()
        ai_dir = root / ".ai"

        # Fallback template paths
        role_file = ai_dir / "roles" / f"{name}.md"
        proto_file = ai_dir / "templates" / "protocol.md"

        template_content = ""
        if role_file.exists():
            with open(role_file, "r", encoding="utf-8") as f:
                template_content = f.read()
        else:
            template_content = f"# ROLE: {name.upper()}\nExecute your responsibilities according to project standards."

        protocol_content = ""
        if proto_file.exists():
            with open(proto_file, "r", encoding="utf-8") as f:
                protocol_content = f.read()

        seq_map = {
            "critic": 0,
            "architect": 1,
            "planner": 2,
            "executor": 3,
            "reviewer": 4,
        }
        sequence_number = seq_map.get(name.lower(), 99)

        return cls(
            name=name.lower(),
            sequence_number=sequence_number,
            template_content=template_content,
            protocol_content=protocol_content,
        )
