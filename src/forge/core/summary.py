"""RunSummary: Structured record and reporting for pipeline stages across execution lifecycle."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class StageSummaryItem:
    """Run-level record of a single pipeline stage encountered during a run."""
    name: str
    status: str
    execution_state: str  # "COMPLETED", "SKIPPED", "NOT REACHED"
    duration_seconds: float = 0.0
    role_name: Optional[str] = None
    sequence_number: Optional[int] = None
    reason: Optional[str] = None

    @property
    def icon(self) -> str:
        if self.execution_state == "NOT REACHED":
            return "○"
        if self.execution_state == "SKIPPED":
            return "✓"
        clean = (self.status or "").upper().strip()
        from forge.stages.definition import StageOrder
        if StageOrder.is_success_status(clean, self.role_name):
            return "✓"
        return "✗"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "execution_state": self.execution_state,
            "duration_seconds": self.duration_seconds,
            "role_name": self.role_name,
            "sequence_number": self.sequence_number,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StageSummaryItem":
        return cls(
            name=data["name"],
            status=data.get("status", "—"),
            execution_state=data.get("execution_state", "NOT REACHED"),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            role_name=data.get("role_name"),
            sequence_number=data.get("sequence_number"),
            reason=data.get("reason"),
        )


@dataclass
class RunSummary:
    """Canonical run-level record of all pipeline stages and terminal state."""
    run_id: str
    stages: List[StageSummaryItem] = field(default_factory=list)
    final_status: str = "UNKNOWN"
    reason: Optional[str] = None

    def get_stage(self, name: str) -> Optional[StageSummaryItem]:
        target = name.lower().strip()
        for s in self.stages:
            if s.name.lower() == target:
                return s
            if s.role_name and s.role_name.lower() == target:
                return s
        return None

    def record_stage(
        self,
        name: str,
        status: str,
        execution_state: str,
        duration_seconds: float = 0.0,
        role_name: Optional[str] = None,
        sequence_number: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> StageSummaryItem:
        existing = self.get_stage(name)
        if existing:
            existing.name = name
            existing.status = status
            existing.execution_state = execution_state
            if duration_seconds > 0:
                existing.duration_seconds = duration_seconds
            if role_name:
                existing.role_name = role_name
            if sequence_number is not None:
                existing.sequence_number = sequence_number
            if reason:
                existing.reason = reason
            return existing
        else:
            item = StageSummaryItem(
                name=name,
                status=status,
                execution_state=execution_state,
                duration_seconds=duration_seconds,
                role_name=role_name,
                sequence_number=sequence_number,
                reason=reason,
            )
            self.stages.append(item)
            return item

    def format_text(self) -> str:
        """Format the concise, canonical Forge Run Summary plain text."""
        lines = [
            "Forge Run Summary",
            "",
            f"Run: {self.run_id}",
            "",
        ]

        max_name_len = max([len(s.name) for s in self.stages] or [10])
        name_width = max(12, max_name_len + 2)

        max_status_len = max([len(s.status) for s in self.stages] or [10])
        status_width = max(18, max_status_len + 2)

        for s in self.stages:
            icon = s.icon
            lines.append(f"{icon} {s.name:<{name_width}}{s.status:<{status_width}}{s.execution_state}")

        lines.append("")
        lines.append(f"Final status: {self.final_status}")
        if self.reason:
            clean_reason = self.reason.strip()
            lines.append(f"Reason: {clean_reason}")

        return "\n".join(lines)

    def format_rich(self) -> Any:
        """Render formatted Rich renderable for dashboard panels."""
        from rich.table import Table
        from rich.text import Text
        from rich.console import Group

        content = []
        title_text = Text("Forge Run Summary", style="bold white")
        content.append(title_text)
        content.append(Text(f"Run: {self.run_id}", style="dim"))
        content.append(Text(""))

        max_name_len = max([len(s.name) for s in self.stages] or [10])
        name_width = max(12, max_name_len + 2)
        max_status_len = max([len(s.status) for s in self.stages] or [10])
        status_width = max(18, max_status_len + 2)

        table = Table.grid(padding=(0, 2))
        table.add_column(justify="left", width=2)
        table.add_column(justify="left", width=name_width)
        table.add_column(justify="left", width=status_width)
        table.add_column(justify="left", width=14)

        for s in self.stages:
            icon = s.icon
            if icon == "✓":
                icon_text = Text("✓", style="bold green")
            elif icon == "✗":
                icon_text = Text("✗", style="bold red")
            else:
                icon_text = Text("○", style="dim")

            name_style = "bold white"
            if s.execution_state == "NOT REACHED":
                status_style = "dim"
                state_style = "dim yellow"
            elif s.execution_state == "SKIPPED":
                status_style = "green"
                state_style = "cyan"
            else:
                from forge.stages.definition import StageOrder
                clean = (s.status or "").upper().strip()
                if StageOrder.is_success_status(clean, s.role_name):
                    status_style = "green"
                else:
                    status_style = "red"
                state_style = "white"

            table.add_row(
                icon_text,
                Text(s.name, style=name_style),
                Text(s.status, style=status_style),
                Text(s.execution_state, style=state_style),
            )

        content.append(table)
        content.append(Text(""))

        from forge.stages.definition import StageOrder
        is_success = StageOrder.is_success_status(self.final_status)
        final_style = "bold green" if is_success else "bold red"
        content.append(Text.assemble(("Final status: ", "bold"), (self.final_status, final_style)))
        if self.reason:
            content.append(Text.assemble(("Reason: ", "bold"), (self.reason.strip(), "yellow")))

        return Group(*content)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "stages": [s.to_dict() for s in self.stages],
            "final_status": self.final_status,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunSummary":
        return cls(
            run_id=data.get("run_id", ""),
            stages=[StageSummaryItem.from_dict(s) for s in data.get("stages", [])],
            final_status=data.get("final_status", "UNKNOWN"),
            reason=data.get("reason"),
        )
