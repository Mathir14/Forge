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
        if not self.machine_report.is_valid:
            from forge.stages.definition import StageOrder
            if StageOrder.is_success_status(self.machine_report.status, self.role.name):
                return "FAILED"
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


class AutonomousHalt(Exception):
    """Structured halt signal for the autonomous pipeline.

    Carries status, exit code, human-readable reason, stage name, and stage result
    without forcing worker threads to invoke sys.exit().
    """

    def __init__(
        self,
        status: str,
        exit_code: int = 1,
        reason: Optional[str] = None,
        stage_name: Optional[str] = None,
        stage_result: Optional[StageResult] = None,
    ):
        msg = f"Autonomous loop halted: {stage_name or 'stage'} finished with status '{status}' (exit code {exit_code})."
        if reason:
            msg += f" Reason: {reason}"
        super().__init__(msg)
        self.status = status
        self.exit_code = exit_code
        self.reason = reason
        self.stage_name = stage_name
        self.stage_result = stage_result

