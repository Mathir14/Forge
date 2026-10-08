"""Authoritative stage definitions and ordering for Forge pipelines."""

from dataclasses import dataclass
from typing import List, Optional, Set, Tuple, Union, Any, FrozenSet
from pathlib import Path
import json
import re


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
        success_statuses=frozenset({"SUCCESS", "APPROVED", "COMPLETED", "COMPLETE"}),
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

    ALL_SUCCESS_STATUSES: FrozenSet[str] = frozenset({
        "APPROVED",
        "VERIFIED",
        "SUCCESS",
        "DONE",
        "CRITIQUE_COMPLETE",
        "READY",
        "PASSED",
        "PASS",
        "COMPLETED",
        "COMPLETE",
        "COMMITTED",
        "NOT_TESTABLE",
    })

    @classmethod
    def is_success_status(cls, status: str, role_name: Optional[str] = None) -> bool:
        """Check if a status represents successful completion."""
        if not status:
            return False
        clean = status.upper().strip()
        if role_name:
            role_successes = cls.get_success_statuses(role_name)
            if clean in role_successes:
                return True
        return clean in cls.ALL_SUCCESS_STATUSES

    @classmethod
    def autonomous_loop_stages(cls, no_critic: bool = False) -> List[StageDefinition]:
        """Return stage definitions for autonomous loop (pre-loop + loop + post-loop)."""
        stages = list(cls.pre_loop_stages()) + [cls.change_producer()] + cls.verification_stages()
        stages.extend(cls.post_loop_stages(no_critic=no_critic))
        return sorted(stages, key=lambda s: s.sequence_number)

    @classmethod
    def full_autonomous_stages(cls, no_critic: bool = False, has_pre_critic: bool = True) -> List[StageDefinition]:
        """Return full canonical autonomous stages in execution sequence order:
        [critic (0)], architect (1), planner (2), executor (3), tester (4), reviewer (5), [closing critic (6)].
        """
        stages = []
        if has_pre_critic:
            stages.append(cls.get_pre_run_critic())
        stages.extend([
            cls.get_architect(),
            cls.get_planner(),
            cls.change_producer(),
            *cls.verification_stages(),
        ])
        if not no_critic:
            stages.append(cls.get_closing_critic())
        return stages

    @classmethod
    def required_pipeline_stages(
        cls,
        no_critic: bool = False,
        has_pre_critic: bool = False,
    ) -> List[StageDefinition]:
        """Return the canonical sequence of stages that MUST execute and succeed
        for a full pipeline to be approved."""
        stages: List[StageDefinition] = []
        if has_pre_critic:
            stages.append(cls.get_pre_run_critic())
        stages.extend([
            cls.get_architect(),
            cls.get_planner(),
            cls.get_executor(),
            cls.get_tester(),
            cls.get_reviewer(),
        ])
        if not no_critic:
            stages.append(cls.get_closing_critic())
        return stages

    @classmethod
    def verify_pipeline_completion(
        cls,
        stages: Any = None,
        summary: Optional[Any] = None,
        no_critic: bool = False,
        has_pre_critic: bool = False,
        run_dir: Optional[Union[str, Path]] = None,
        task_fingerprint: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Centrally verify if a pipeline execution positively establishes full successful completion
        based exclusively on physical, valid, correctly provenanced stage deliverables.

        Metadata summary is informational state only and is NEVER used to substitute for missing
        or invalid stage deliverables.

        Returns: (is_completed, incomplete_stage_name, reason)
        """
        import re

        # Resolve run directory and stage list
        if isinstance(stages, (str, Path)):
            run_dir = Path(stages)
            stage_list = []
        elif hasattr(stages, "run_dir"):
            run_dir = run_dir or getattr(stages, "run_dir", None)
            run_id = run_id or getattr(stages, "run_id", None)
            task_fingerprint = task_fingerprint or getattr(stages, "task_fingerprint", None)
            stage_list = list(getattr(stages, "stages", []))
        else:
            stage_list = list(stages or [])

        if run_dir is not None:
            run_dir = Path(run_dir)
            meta_path = run_dir / "metadata.json"
            if meta_path.exists():
                try:
                    meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                    if not run_id:
                        run_id = meta_data.get("run_id")
                    if not task_fingerprint:
                        task_fingerprint = meta_data.get("task_fingerprint")
                        if not task_fingerprint and meta_data.get("task"):
                            from forge.core.run import compute_task_fingerprint
                            task_fingerprint = compute_task_fingerprint(meta_data.get("task"))
                    if not no_critic:
                        if meta_data.get("no_critic") or (isinstance(meta_data.get("config"), dict) and meta_data["config"].get("execution", {}).get("no_critic")):
                            no_critic = True
                except Exception:
                    pass

        if run_dir is None:
            for s in stage_list:
                p = getattr(s, "artifact_json_path", None) or getattr(s, "run_dir", None)
                if p:
                    run_dir = Path(p).parent if getattr(s, "artifact_json_path", None) else Path(p)
                    break

        # Map in-memory executed stages by normalized (role, seq) and role alone
        executed_by_key: Dict[Tuple[str, int], Any] = {}
        for s in stage_list:
            role = str(getattr(s, "role_name", "") or getattr(s, "name", "") or "").lower().strip()
            seq = getattr(s, "sequence_number", None)
            if not role and hasattr(s, "stage_name"):
                m = re.match(r"^(\d+)_([a-zA-Z0-9_\-]+)$", str(s.stage_name))
                if m:
                    seq = int(m.group(1))
                    role = m.group(2).lower().strip()
            if role:
                key = (role, seq if seq is not None else -1)
                executed_by_key[key] = s
                if (role, -1) not in executed_by_key:
                    executed_by_key[(role, -1)] = s

        # Canonical required pipeline stages
        include_closing_critic = not no_critic
        if not has_pre_critic:
            if run_dir and run_dir.exists() and (run_dir / "00_critic.json").exists():
                has_pre_critic = True
            elif ("critic", 0) in executed_by_key:
                has_pre_critic = True

        required_defs = cls.required_pipeline_stages(no_critic=not include_closing_critic, has_pre_critic=has_pre_critic)

        # Check if literally nothing was executed
        has_any_execution = False
        if run_dir and run_dir.exists():
            for p in run_dir.iterdir():
                if p.is_file() and p.suffix in (".json", ".md") and not p.name.startswith((".tmp", "metadata", "git_baseline", "run_lock")):
                    has_any_execution = True
                    break
        if not has_any_execution:
            for s in stage_list:
                st = str(getattr(s, "status", "") or "").upper().strip()
                dur = float(getattr(s, "duration_seconds", 0.0) or 0.0)
                if (st and st not in ("PENDING", "UNKNOWN", "NOT REACHED", "—", "")) or dur > 0:
                    has_any_execution = True
                    break

        if not has_any_execution:
            return False, required_defs[0].display_name, "No stages were executed."

        # Check for any explicit failure in in-memory stages
        for s in stage_list:
            st = str(getattr(s, "status", "") or "").upper().strip()
            ec = getattr(s, "exit_code", None)
            if st in ("FAILED", "BLOCKED", "REJECTED", "CHANGES_REQUIRED") or (ec is not None and ec != 0):
                s_name = getattr(s, "display_name", None) or getattr(s, "stage_name", None) or getattr(s, "name", "Stage")
                return False, str(s_name), f"Stage '{s_name}' failed with status '{st or ec}'."

        # Verify EVERY required stage has a physical, valid, correctly provenanced deliverable
        for req_def in required_defs:
            stage_inst = executed_by_key.get((req_def.name.lower(), req_def.sequence_number))
            if stage_inst is None:
                stage_inst = executed_by_key.get((req_def.name.lower(), -1))

            if run_dir and run_dir.exists():
                expected_json = run_dir / f"{req_def.sequence_number:02d}_{req_def.name.lower()}.json"
                if not expected_json.exists() or not expected_json.is_file():
                    return False, req_def.display_name, f"Stage artifact '{expected_json.name}' is missing on disk."

                try:
                    art_data = json.loads(expected_json.read_text(encoding="utf-8"))
                    if not isinstance(art_data, dict):
                        return False, req_def.display_name, f"Stage artifact '{expected_json.name}' contains invalid JSON data."
                except Exception as e:
                    return False, req_def.display_name, f"Stage artifact '{expected_json.name}' is malformed or corrupted JSON: {e}"

                # Provenance checks (RC-03, RC-04):
                # 1. task_fingerprint
                art_fp = art_data.get("task_fingerprint")
                if not art_fp or not isinstance(art_fp, str) or len(art_fp) != 64 or not all(c in "0123456789abcdefABCDEF" for c in art_fp):
                    return False, req_def.display_name, f"Stage artifact '{expected_json.name}' lacks valid task fingerprint."
                if task_fingerprint and art_fp != task_fingerprint:
                    return False, req_def.display_name, f"Stage artifact '{expected_json.name}' has mismatched task fingerprint (generation mismatch)."

                # 2. run_id
                art_rid = art_data.get("run_id")
                if not art_rid or not str(art_rid).strip():
                    return False, req_def.display_name, f"Stage artifact '{expected_json.name}' lacks mandatory run identity."
                if run_id and str(art_rid) != str(run_id):
                    return False, req_def.display_name, f"Stage artifact '{expected_json.name}' has mismatched run identity (expected '{run_id}', got '{art_rid}')."

                # 3. canonical role
                art_role = art_data.get("role") or art_data.get("ROLE")
                if not art_role or str(art_role).lower().strip() != req_def.name.lower().strip():
                    return False, req_def.display_name, f"Stage artifact '{expected_json.name}' has mismatched role '{art_role}' (expected '{req_def.name}')."

                # 4. sequence number
                art_seq = art_data.get("sequence_number")
                if art_seq is None:
                    return False, req_def.display_name, f"Stage artifact '{expected_json.name}' lacks sequence number."
                try:
                    if int(art_seq) != req_def.sequence_number:
                        return False, req_def.display_name, f"Stage artifact '{expected_json.name}' has mismatched sequence number '{art_seq}' (expected '{req_def.sequence_number}')."
                except (ValueError, TypeError):
                    return False, req_def.display_name, f"Stage artifact '{expected_json.name}' has invalid sequence number '{art_seq}'."

                # 5. successful status
                art_status = str(art_data.get("status") or art_data.get("STATUS") or "").upper().strip()
                if not art_status or art_status in ("PENDING", "UNKNOWN", "RUNNING", "IN_PROGRESS", "NOT REACHED", "—", ""):
                    return False, req_def.display_name, f"Stage '{req_def.display_name}' did not complete (status '{art_status}')."
                if art_status in ("FAILED", "BLOCKED", "REJECTED", "CHANGES_REQUIRED"):
                    return False, req_def.display_name, f"Stage '{req_def.display_name}' finished with non-success status '{art_status}'."
                if not cls.is_success_status(art_status, req_def.name):
                    return False, req_def.display_name, f"Stage '{req_def.display_name}' finished with non-success status '{art_status}'."
                art_ec = art_data.get("exit_code")
                if art_ec is not None and art_ec != 0:
                    return False, req_def.display_name, f"Stage '{req_def.display_name}' failed with exit code {art_ec}."

            else:
                # In-memory execution without run_dir
                if stage_inst is None:
                    return False, req_def.display_name, f"Pipeline terminated before stage '{req_def.display_name}' completed."

                status_str = str(getattr(stage_inst, "status", "") or "").upper().strip()
                if not status_str or status_str in ("PENDING", "UNKNOWN", "RUNNING", "IN_PROGRESS", "NOT REACHED", "—", ""):
                    return False, req_def.display_name, f"Pipeline terminated before stage '{req_def.display_name}' completed."

                if not cls.is_success_status(status_str, req_def.name) and status_str not in ("SKIPPED", "COMPLETE"):
                    return False, req_def.display_name, f"Stage '{req_def.display_name}' finished with non-success status '{status_str}'."

                inst_meta = getattr(stage_inst, "metadata", None)
                if isinstance(inst_meta, dict) and inst_meta:
                    art_fp = inst_meta.get("task_fingerprint")
                    if task_fingerprint and art_fp and art_fp != task_fingerprint:
                        return False, req_def.display_name, f"Stage '{req_def.display_name}' has mismatched task fingerprint."
                    art_rid = inst_meta.get("run_id")
                    if run_id and art_rid and str(art_rid) != str(run_id):
                        return False, req_def.display_name, f"Stage '{req_def.display_name}' has mismatched run identity."
                    art_role = inst_meta.get("role")
                    if art_role and str(art_role).lower().strip() != req_def.name.lower().strip():
                        return False, req_def.display_name, f"Stage '{req_def.display_name}' has mismatched role."
                    art_seq = inst_meta.get("sequence_number")
                    if art_seq is not None and int(art_seq) != req_def.sequence_number:
                        return False, req_def.display_name, f"Stage '{req_def.display_name}' has mismatched sequence number."

        return True, None, "All stages completed successfully."

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
