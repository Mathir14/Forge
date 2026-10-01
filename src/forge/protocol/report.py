"""Machine report dataclass emitted by agents according to protocol.md."""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from forge.core.knowledge import KnowledgeProposal


@dataclass
class MachineReport:
    role: str
    status: str                         # e.g., APPROVED, REJECTED, SUCCESS, FAILED, READY, BLOCKED
    handoff: str = "NONE"               # e.g., PLANNER, EXECUTOR, REVIEWER, ARCHITECT, NONE
    exit_code: int = 0
    reason: Optional[str] = None
    confidence: Optional[str] = None
    next_action: Optional[str] = None
    issues: Dict[str, List[Any]] = field(default_factory=dict)
    data: Dict[str, Any] = field(default_factory=dict)
    proposals: List[KnowledgeProposal] = field(default_factory=list)
    raw_yaml: str = ""
    is_valid: bool = True
    validation_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "status": self.status,
            "handoff": self.handoff,
            "exit_code": self.exit_code,
            "reason": self.reason,
            "confidence": self.confidence,
            "next_action": self.next_action,
            "issues": self.issues,
            "data": self.data,
            "proposals": [p.to_dict() for p in self.proposals],
            "is_valid": self.is_valid,
            "validation_errors": self.validation_errors,
        }
