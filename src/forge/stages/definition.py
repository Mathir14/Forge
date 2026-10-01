"""Authoritative stage definitions and ordering for Forge pipelines."""

from dataclasses import dataclass
from typing import List, Optional, Set, Tuple, Union, Any, FrozenSet
from pathlib import Path


@dataclass(frozen=True)
class StageDefinition:
    name: str
    sequence_number: int
    phase: str = "pre_run"  # "pre_run" or "post_run"
    display_title: str = ""
    emoji: str = "🔄"
    preposition: str = "for task:"
    is_pre_run_critic: bool = False
    is_closing_critic: bool = False
    role_type: str = "general"  # "analysis", "planning", "producer", "verifier", "audit"
    is_producer: bool = False
    is_verifier: bool = False
    repair_target: Optional[str] = None
    success_statuses: FrozenSet[str] = frozenset({"APPROVED", "SUCCESS", "READY", "CRITIQUE_COMPLETE", "COMPLETED", "PASSED"})

    @property
    def artifact_prefix(self) -> str:
        return f"{self.sequence_number:02d}_{self.name}"

    @property
    def display_name(self) -> str:
        if self.display_title:
            return self.display_title
        if self.is_closing_critic:
            return "Post-Execution Critic"
        if self.is_pre_run_critic:
            return "Codebase Critic"
        return self.name.capitalize()

    @property
    def allowed_statuses(self) -> FrozenSet[str]:
        """All allowed protocol statuses for this stage (success + non-success)."""
        base = set(self.success_statuses)
        base.update({"FAILED", "BLOCKED"})
        if self.is_verifier or self.role_type == "verifier":
            base.update({"CHANGES_REQUIRED", "REJECTED", "FAIL"})
        elif self.role_type in ("planning", "producer", "analysis", "audit", "general"):
            base.update({"REJECTED"})
        return frozenset(base)

    @property
    def required_capabilities(self) -> Set[str]:
        from forge.stages.requirements import StageRequirementsRegistry
        return StageRequirementsRegistry.get(self.name)


# Single authoritative definition of pipeline stages and ordering
STAGE_DEFINITIONS: Tuple[StageDefinition, ...] = (
    StageDefinition(
        name="critic",
        sequence_number=0,
        phase="pre_run",
        display_title="Codebase Critic",
        emoji="🧐",
        preposition="on:",
        is_pre_run_critic=True,
        role_type="analysis",
        success_statuses=frozenset({"CRITIQUE_COMPLETE", "APPROVED", "SUCCESS", "COMPLETED", "PASSED"}),
    ),
    StageDefinition(
        name="architect",
        sequence_number=1,
        phase="pre_run",
        display_title="Architect",
        emoji="🔨",
        preposition="for task:",
        role_type="planning",
        success_statuses=frozenset({"APPROVED", "READY", "SUCCESS", "COMPLETED"}),
    ),
    StageDefinition(
        name="planner",
        sequence_number=2,
        phase="pre_run",
        display_title="Planner",
        emoji="📋",
        preposition="for task:",
        role_type="planning",
        success_statuses=frozenset({"READY", "APPROVED", "SUCCESS", "COMPLETED"}),
    ),
    StageDefinition(
        name="executor",
        sequence_number=3,
        phase="pre_run",
        display_title="Executor",
        emoji="⚡",
        preposition="for task:",
        role_type="producer",
        is_producer=True,
        success_statuses=frozenset({"SUCCESS", "APPROVED", "COMPLETED"}),
    ),
    StageDefinition(
        name="tester",
        sequence_number=4,
        phase="pre_run",
        display_title="Tester",
        emoji="🧪",
        preposition="for task:",
        role_type="verifier",
        is_verifier=True,
        repair_target="executor",
        success_statuses=frozenset({"PASS", "APPROVED", "SUCCESS", "COMPLETED", "NOT_TESTABLE"}),
    ),
    StageDefinition(
        name="reviewer",
        sequence_number=5,
        phase="pre_run",
        display_title="Reviewer",
        emoji="🔍",
        preposition="for task:",
        role_type="verifier",
        is_verifier=True,
        repair_target="executor",
        success_statuses=frozenset({"APPROVED"}),
    ),
    StageDefinition(
        name="critic",
        sequence_number=6,
        phase="post_run",
        display_title="Post-Execution Critic",
        emoji="🧐",
        preposition="on:",
        is_closing_critic=True,
        role_type="audit",
        success_statuses=frozenset({"CRITIQUE_COMPLETE", "APPROVED", "SUCCESS", "COMPLETED", "PASSED"}),
    ),
)


class StageOrder:
    """Authoritative stage order registry for Forge standard and autonomous pipelines."""

    ALL_STAGES: Tuple[StageDefinition, ...] = STAGE_DEFINITIONS

    @classmethod
    def all_stages(cls) -> Tuple[StageDefinition, ...]:
        return cls.ALL_STAGES

    @classmethod
    def all_stage_names(cls) -> Set[str]:
        """Return unique names of all defined pipeline stages."""
        return {s.name for s in cls.ALL_STAGES}

    @classmethod
    def get_allowed_statuses(cls, role_name: str, phase: Optional[str] = None) -> Set[str]:
        """Return all allowed protocol statuses for a role name and optional phase."""
        clean_name = str(role_name).lower().strip()
        candidates = [s for s in cls.ALL_STAGES if s.name == clean_name]
        if not candidates:
            return set()
        if phase:
            for s in candidates:
                if s.phase == phase:
                    return set(s.allowed_statuses)
        res: Set[str] = set()
        for s in candidates:
            res.update(s.allowed_statuses)
        return res

    @classmethod
    def get_success_statuses(cls, role_name: str, phase: Optional[str] = None) -> Set[str]:
        """Return success statuses for a role name and optional phase."""
        clean_name = str(role_name).lower().strip()
        candidates = [s for s in cls.ALL_STAGES if s.name == clean_name]
        if not candidates:
            return set()
        if phase:
            for s in candidates:
                if s.phase == phase:
                    return set(s.success_statuses)
        res: Set[str] = set()
        for s in candidates:
            res.update(s.success_statuses)
        return res

    @classmethod
    def change_producer(cls) -> StageDefinition:
        """Return the change producer stage definition (e.g. Executor)."""
        for s in cls.ALL_STAGES:
            if s.is_producer:
                return s
        return cls.get_executor()

    @classmethod
    def verification_stages(cls) -> List[StageDefinition]:
        """Return list of verification gate stage definitions in sequence order (e.g. Reviewer)."""
        return sorted([s for s in cls.ALL_STAGES if s.is_verifier], key=lambda s: s.sequence_number)

    @classmethod
    def total_stages_count(cls, no_critic: bool = False) -> int:
        """Return total number of stages in the pipeline sequence."""
        return len(cls.autonomous_loop_stages(no_critic=no_critic))

    @classmethod
    def get_pre_run_critic(cls) -> StageDefinition:
        for s in cls.ALL_STAGES:
            if s.is_pre_run_critic:
                return s
        return cls.resolve_definition("critic", phase="pre_run")

    @classmethod
    def get_architect(cls) -> StageDefinition:
        return cls.resolve_definition("architect")

    @classmethod
    def get_planner(cls) -> StageDefinition:
        return cls.resolve_definition("planner")

    @classmethod
    def get_executor(cls) -> StageDefinition:
        return cls.resolve_definition("executor")

    @classmethod
    def get_tester(cls) -> StageDefinition:
        return cls.resolve_definition("tester")

    @classmethod
    def get_reviewer(cls) -> StageDefinition:
        return cls.resolve_definition("reviewer")

    @classmethod
    def get_closing_critic(cls) -> StageDefinition:
        for s in cls.ALL_STAGES:
            if s.is_closing_critic:
                return s
        return cls.resolve_definition("critic", phase="post_run")

    @classmethod
    def standard_pipeline_stages(cls, no_critic: bool = False) -> List[StageDefinition]:
        """Return stage definitions for standard pipeline (architect -> planner -> executor -> tester -> reviewer -> [closing_critic])."""
        stages = [
            cls.get_architect(),
            cls.get_planner(),
            cls.get_executor(),
            cls.get_tester(),
            cls.get_reviewer(),
        ]
        if not no_critic:
            stages.append(cls.get_closing_critic())
        return stages

    @classmethod
    def pre_loop_stages(cls) -> List[StageDefinition]:
        """Return stage definitions that run prior to the implementation/verification loop."""
        return [
            cls.get_architect(),
            cls.get_planner(),
        ]

    @classmethod
    def implementation_loop_stages(cls) -> Tuple[StageDefinition, ...]:
        """Return the implementation change producer and verification gate stage definitions."""
        return (cls.change_producer(), *cls.verification_stages())

    @classmethod
    def post_loop_stages(cls, no_critic: bool = False) -> List[StageDefinition]:
        """Return stage definitions that run following the implementation/verification loop."""
        if not no_critic:
            return [cls.get_closing_critic()]
        return []

    @classmethod
    def autonomous_loop_stages(cls, no_critic: bool = False) -> List[StageDefinition]:
        """Return stage definitions for autonomous loop (pre-loop + loop + post-loop)."""
        stages = list(cls.pre_loop_stages()) + [cls.change_producer()] + cls.verification_stages()
        stages.extend(cls.post_loop_stages(no_critic=no_critic))
        return sorted(stages, key=lambda s: s.sequence_number)

    @classmethod
    def resolve_definition(
        cls,
        name: str,
        phase: Optional[str] = None,
        sequence_number: Optional[int] = None,
    ) -> StageDefinition:
        """Resolve a StageDefinition unambiguously by name, phase, and/or sequence_number."""
        clean_name = name.lower().strip()
        candidates = [s for s in cls.ALL_STAGES if s.name == clean_name]
        if not candidates:
            seq = sequence_number if sequence_number is not None else 99
            p = phase or "pre_run"
            return StageDefinition(name=clean_name, sequence_number=seq, phase=p)

        if sequence_number is not None:
            for s in candidates:
                if s.sequence_number == sequence_number:
                    return s

        if phase is not None:
            for s in candidates:
                if s.phase == phase:
                    return s

        # Default fallback: return first candidate
        return candidates[0]

    @classmethod
    def is_closing_critic(
        cls,
        stage: Union[StageDefinition, Any, str],
        phase: Optional[str] = None,
    ) -> bool:
        """Check if a stage object, definition, role, or name corresponds to closing Critic."""
        if isinstance(stage, StageDefinition):
            return stage.is_closing_critic
        if hasattr(stage, "is_closing_critic"):
            return bool(stage.is_closing_critic)
        name = getattr(stage, "name", str(stage)).lower().strip()
        if name != "critic":
            return False
        stage_phase = phase or getattr(stage, "phase", None)
        seq = getattr(stage, "sequence_number", None)
        return stage_phase == "post_run" or seq in (5, 6)

    @classmethod
    def is_pre_run_critic(
        cls,
        stage: Union[StageDefinition, Any, str],
        phase: Optional[str] = None,
    ) -> bool:
        """Check if a stage object, definition, role, or name corresponds to pre-run Critic."""
        if isinstance(stage, StageDefinition):
            return stage.is_pre_run_critic
        if hasattr(stage, "is_pre_run_critic"):
            return bool(stage.is_pre_run_critic)
        name = getattr(stage, "name", str(stage)).lower().strip()
        if name != "critic":
            return False
        return not cls.is_closing_critic(stage, phase=phase)
