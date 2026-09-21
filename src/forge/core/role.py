"""Immutable Role metadata container."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set


@dataclass(frozen=True)
class Role:
    name: str
    sequence_number: int
    template_content: str
    protocol_content: str = ""
    phase: str = "pre_run"

    @property
    def is_closing_critic(self) -> bool:
        return self.name == "critic" and (self.phase == "post_run" or self.sequence_number == 5)

    @property
    def is_pre_run_critic(self) -> bool:
        return self.name == "critic" and not self.is_closing_critic

    @property
    def required_capabilities(self) -> Set[str]:
        from forge.stages.requirements import StageRequirementsRegistry
        return StageRequirementsRegistry.get(self.name)

    @classmethod
    def load(
        cls,
        name: str,
        project_root: Optional[Path] = None,
        sequence_number: Optional[int] = None,
        phase: Optional[str] = None,
    ) -> "Role":
        from forge.stages.definition import StageOrder

        stage_def = StageOrder.resolve_definition(name, phase=phase, sequence_number=sequence_number)
        resolved_seq = sequence_number if sequence_number is not None else stage_def.sequence_number
        resolved_phase = phase if phase is not None else stage_def.phase

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
            from forge.core.templates import DEFAULT_ROLES
            template_content = DEFAULT_ROLES.get(
                name.lower(),
                f"# ROLE: {name.upper()}\nExecute your responsibilities according to project standards."
            )

        protocol_content = ""
        if proto_file.exists():
            with open(proto_file, "r", encoding="utf-8") as f:
                protocol_content = f.read()
        else:
            from forge.core.templates import DEFAULT_PROTOCOL
            protocol_content = DEFAULT_PROTOCOL

        return cls(
            name=name.lower(),
            sequence_number=resolved_seq,
            template_content=template_content,
            protocol_content=protocol_content,
            phase=resolved_phase,
        )
