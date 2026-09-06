"""Stage result entity holding prompt, response, report, and metrics."""

from dataclasses import dataclass
from typing import Dict, Any, Optional
from forge.core.role import Role
from forge.prompts.rendered_prompt import RenderedPrompt
from forge.adapters.base import AdapterResponse
from forge.protocol.report import MachineReport


@dataclass
class StageResult:
    role: Role
    prompt: RenderedPrompt
    response: AdapterResponse
    machine_report: MachineReport
    raw_markdown: str
    duration_seconds: float
    success: bool

    @property
    def status(self) -> str:
        return self.machine_report.status

    @property
    def handoff(self) -> str:
        return self.machine_report.handoff

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role.name,
            "sequence_number": self.role.sequence_number,
            "status": self.status,
            "handoff": self.handoff,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.response.exit_code,
            "prompt_hash": self.prompt.prompt_hash,
            "machine_report": self.machine_report.to_dict(),
        }
