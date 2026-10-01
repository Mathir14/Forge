"""Core domain model and data structures for the Project Knowledge Base (PKB).

Conforms to ADR-015 and PKB v1 Implementation Roadmap.
"""

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Dict, List, Optional


class FactType(str, Enum):
    ARCHITECTURE = "architecture"
    FEATURE = "feature"
    DECISION = "decision"
    UNRESOLVED = "unresolved"

    @classmethod
    def values(cls) -> List[str]:
        return [m.value for m in cls]


class FactStatus(str, Enum):
    PROVISIONAL = "PROVISIONAL"
    VERIFIED = "VERIFIED"
    DISPUTED = "DISPUTED"
    HUMAN_LOCKED = "HUMAN_LOCKED"
    DEPRECATED = "DEPRECATED"

    @classmethod
    def values(cls) -> List[str]:
        return [m.value for m in cls]


class FactSource(str, Enum):
    INFERRED = "inferred"
    OBSERVED = "observed"
    VERIFIED = "verified"
    HUMAN = "human"

    @classmethod
    def values(cls) -> List[str]:
        return [m.value for m in cls]


class ProposalAction(str, Enum):
    ASSERT = "ASSERT"
    VERIFY = "VERIFY"
    DISPUTE = "DISPUTE"
    DEPRECATE = "DEPRECATE"

    @classmethod
    def values(cls) -> List[str]:
        return [m.value for m in cls]


_SLUG_REGEX = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class KnowledgeFact:
    """A single atomic unit of project knowledge."""

    id: str
    type: str
    title: str
    status: str = FactStatus.PROVISIONAL.value
    summary: str = ""
    evidence: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    disputes: List[Dict[str, Any]] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.type = str(self.type).lower().strip()
        self.status = str(self.status).upper().strip()
        if "source" not in self.provenance:
            self.provenance["source"] = FactSource.INFERRED.value
        else:
            self.provenance["source"] = str(self.provenance["source"]).lower().strip()

    @property
    def is_locked(self) -> bool:
        return self.status == FactStatus.HUMAN_LOCKED.value

    @property
    def is_disputed(self) -> bool:
        return self.status == FactStatus.DISPUTED.value

    @property
    def is_verified(self) -> bool:
        return self.status == FactStatus.VERIFIED.value

    @property
    def source(self) -> str:
        return str(self.provenance.get("source", FactSource.INFERRED.value))

    def validate(self) -> List[str]:
        """Validate fact attributes according to PKB v1 invariants."""
        errors: List[str] = []
        if not self.id or not isinstance(self.id, str):
            errors.append("Fact ID must be a non-empty string.")
        elif not _SLUG_REGEX.match(self.id):
            errors.append(f"Fact ID '{self.id}' must be a valid kebab-case slug (e.g. 'auth-rate-limiter').")

        if not self.title or not isinstance(self.title, str) or not self.title.strip():
            errors.append("Fact title must be a non-empty string.")

        if self.type not in FactType.values():
            errors.append(f"Fact type '{self.type}' invalid; must be one of {FactType.values()}.")

        if self.status not in FactStatus.values():
            errors.append(f"Fact status '{self.status}' invalid; must be one of {FactStatus.values()}.")

        source = self.provenance.get("source")
        if source not in FactSource.values():
            errors.append(f"Provenance source '{source}' invalid; must be one of {FactSource.values()}.")

        return errors

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary representation for YAML persistence."""
        res: Dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "provenance": dict(self.provenance),
        }
        if self.disputes:
            res["disputes"] = [dict(d) for d in self.disputes]
        if self.payload:
            res["payload"] = dict(self.payload)
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeFact":
        """Deserialize from dictionary representation."""
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for KnowledgeFact, got {type(data).__name__}")
        return cls(
            id=str(data.get("id", "")),
            type=str(data.get("type", FactType.ARCHITECTURE.value)),
            title=str(data.get("title", "")),
            status=str(data.get("status", FactStatus.PROVISIONAL.value)),
            summary=str(data.get("summary", "")),
            evidence=list(data.get("evidence", [])),
            provenance=dict(data.get("provenance", {})),
            disputes=list(data.get("disputes", [])),
            payload=dict(data.get("payload", {})),
        )


@dataclass
class KnowledgeProposal:
    """A structured knowledge update proposal emitted by a pipeline stage."""

    action: str
    id: str
    type: str = FactType.ARCHITECTURE.value
    title: str = ""
    summary: str = ""
    evidence: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    note: Optional[str] = None
    role: Optional[str] = None

    def __post_init__(self) -> None:
        self.action = str(self.action).upper().strip()
        self.type = str(self.type).lower().strip()
        if self.role:
            self.role = str(self.role).lower().strip()

    def validate(self) -> List[str]:
        """Validate proposal according to PKB v1 invariants."""
        errors: List[str] = []
        if self.action not in ProposalAction.values():
            errors.append(f"Proposal action '{self.action}' invalid; must be one of {ProposalAction.values()}.")

        if not self.id or not isinstance(self.id, str):
            errors.append("Proposal ID must be a non-empty string.")
        elif not _SLUG_REGEX.match(self.id):
            errors.append(f"Proposal ID '{self.id}' must be a valid kebab-case slug.")

        if self.action == ProposalAction.ASSERT.value:
            if not self.title or not isinstance(self.title, str) or not self.title.strip():
                errors.append("ASSERT proposal requires a non-empty title.")
            if self.type not in FactType.values():
                errors.append(f"ASSERT proposal type '{self.type}' invalid; must be one of {FactType.values()}.")

        return errors

    def to_dict(self) -> Dict[str, Any]:
        res: Dict[str, Any] = {
            "action": self.action,
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "summary": self.summary,
            "evidence": list(self.evidence),
        }
        if self.payload:
            res["payload"] = dict(self.payload)
        if self.note:
            res["note"] = self.note
        if self.role:
            res["role"] = self.role
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any], default_role: Optional[str] = None) -> "KnowledgeProposal":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for KnowledgeProposal, got {type(data).__name__}")
        role = data.get("role") or default_role
        return cls(
            action=str(data.get("action", ProposalAction.ASSERT.value)),
            id=str(data.get("id", "")),
            type=str(data.get("type", FactType.ARCHITECTURE.value)),
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            evidence=list(data.get("evidence", [])),
            payload=dict(data.get("payload", {})),
            note=data.get("note"),
            role=str(role) if role else None,
        )
