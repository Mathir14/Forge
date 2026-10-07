"""Validator for machine protocol reports."""

from typing import Dict, Any, List, Optional, Set
from forge.protocol.report import MachineReport
from forge.core.knowledge import KnowledgeProposal


class MachineReportValidator:
    ALLOWED_STATUSES = {
        "CRITIC": {"CRITIQUE_COMPLETE", "COMPLETED", "PASSED", "APPROVED", "SUCCESS", "BLOCKED", "FAILED", "REJECTED"},
        "ARCHITECT": {"APPROVED", "READY", "SUCCESS", "COMPLETED", "FAILED", "BLOCKED", "REJECTED"},
        "PLANNER": {"READY", "APPROVED", "SUCCESS", "COMPLETED", "FAILED", "BLOCKED", "REJECTED"},
        "EXECUTOR": {"SUCCESS", "APPROVED", "COMPLETED", "FAILED", "BLOCKED", "REJECTED"},
        "TESTER": {"PASS", "FAIL", "BLOCKED", "NOT_TESTABLE", "APPROVED", "CHANGES_REQUIRED", "REJECTED", "PASSED", "FAILED"},
        "REVIEWER": {"APPROVED", "CHANGES_REQUIRED", "BLOCKED", "REJECTED", "FAILED"},
    }

    ALLOWED_HANDOFFS = {
        "CRITIC": {"ARCHITECT", "PLANNER", "NONE"},
        "ARCHITECT": {"PLANNER", "NONE"},
        "PLANNER": {"EXECUTOR", "ARCHITECT", "NONE"},
        "EXECUTOR": {"TESTER", "REVIEWER", "PLANNER", "ARCHITECT", "NONE"},
        "TESTER": {"REVIEWER", "EXECUTOR", "NONE"},
        "REVIEWER": {"NONE", "EXECUTOR", "ARCHITECT"},
    }

    _CUSTOM_ALLOWED_STATUSES: Dict[str, Set[str]] = {}
    _CUSTOM_ALLOWED_HANDOFFS: Dict[str, Set[str]] = {}

    @classmethod
    def register_role_rules(
        cls,
        role: str,
        allowed_statuses: Set[str],
        allowed_handoffs: Optional[Set[str]] = None,
    ) -> None:
        """Register allowed statuses and handoffs for a new or custom pipeline stage/role."""
        r = role.upper()
        cls._CUSTOM_ALLOWED_STATUSES[r] = set(allowed_statuses)
        if allowed_handoffs is not None:
            cls._CUSTOM_ALLOWED_HANDOFFS[r] = set(allowed_handoffs)

    @classmethod
    def reset_custom_rules(cls) -> None:
        """Reset all registered custom role rules."""
        cls._CUSTOM_ALLOWED_STATUSES.clear()
        cls._CUSTOM_ALLOWED_HANDOFFS.clear()

    @classmethod
    def get_allowed_statuses(cls, role: str) -> Set[str]:
        """Get allowed statuses for role, falling back to registered rules or stage definitions."""
        r = role.upper()
        if r in cls._CUSTOM_ALLOWED_STATUSES:
            return cls._CUSTOM_ALLOWED_STATUSES[r]
        # Check StageOrder for predefined stage definitions
        try:
            from forge.stages.definition import StageOrder
            order_statuses = StageOrder.get_allowed_statuses(r)
            if order_statuses:
                return set(order_statuses)
        except Exception:
            pass
        if r in cls.ALLOWED_STATUSES:
            return cls.ALLOWED_STATUSES[r]
        return set()

    @classmethod
    def get_allowed_handoffs(cls, role: str) -> Set[str]:
        """Get allowed handoffs for role, falling back to registered rules."""
        r = role.upper()
        if r in cls._CUSTOM_ALLOWED_HANDOFFS:
            return cls._CUSTOM_ALLOWED_HANDOFFS[r]
        return cls.ALLOWED_HANDOFFS.get(r, set())

    @classmethod
    def validate(
        cls,
        data: Dict[str, Any],
        expected_role: str,
        raw_yaml: str = "",
    ) -> MachineReport:
        errors: List[str] = []
        normalized_data = {str(k).upper(): v for k, v in data.items()}

        # 1. Role validation
        emitted_role = str(normalized_data.get("ROLE", expected_role)).upper()
        if emitted_role != expected_role.upper():
            errors.append(f"Expected role '{expected_role.upper()}', got '{emitted_role}'")
        role = expected_role.upper()

        # 1.1 Enforce ADR-002 Score-Free policy for TESTER
        if expected_role.upper() == "TESTER" or emitted_role == "TESTER":
            if "SCORES" in normalized_data or "SCORE" in normalized_data:
                errors.append(
                    "Tester machine report strictly disallows 'SCORES' or subjective numeric ratings. "
                    "Use empirical defect findings under 'ISSUES' instead."
                )

        # 2. Status validation
        status = str(normalized_data.get("STATUS", "UNKNOWN")).upper()
        allowed_status_set = cls.get_allowed_statuses(expected_role)
        if allowed_status_set and status not in allowed_status_set:
            errors.append(
                f"Status '{status}' not in allowed statuses {list(allowed_status_set)}"
            )

        # 3. Handoff validation
        handoff = str(normalized_data.get("HANDOFF", "NONE")).upper()
        allowed_handoff_set = cls.get_allowed_handoffs(expected_role)
        if allowed_handoff_set and handoff not in allowed_handoff_set:
            errors.append(
                f"Handoff '{handoff}' not in allowed handoffs {list(allowed_handoff_set)}"
            )

        # 3.1 ADR-014 Protocol Invariants
        # Invariant 1: STATUS == BLOCKED <=> HANDOFF == NONE
        if status == "BLOCKED" and handoff != "NONE":
            errors.append(
                f"Protocol invariant violation: STATUS 'BLOCKED' requires HANDOFF 'NONE', got '{handoff}'. "
                "BLOCKED is reserved exclusively for external operational blockers halting the pipeline."
            )

        # Invariant 2: STATUS == REJECTED <=> HANDOFF == NONE
        if status == "REJECTED" and handoff != "NONE":
            errors.append(
                f"Protocol invariant violation: STATUS 'REJECTED' requires HANDOFF 'NONE', got '{handoff}'. "
                "REJECTED indicates an inadmissible task halting the pipeline."
            )

        # Invariant 3: Intermediate stages on success require active downstream routing
        if role == "ARCHITECT" and status in ("APPROVED", "READY", "SUCCESS", "COMPLETED") and handoff == "NONE":
            errors.append(
                f"Protocol invariant violation: Architect success status '{status}' requires active handoff (e.g. 'PLANNER'), got 'NONE'."
            )
        elif role == "PLANNER" and status in ("READY", "APPROVED", "SUCCESS", "COMPLETED") and handoff == "NONE":
            errors.append(
                f"Protocol invariant violation: Planner success status '{status}' requires active handoff (e.g. 'EXECUTOR'), got 'NONE'."
            )

        # 3.2 ADR-015 PKB Protocol Invariants & Proposal Validation
        proposals_raw = normalized_data.get("KNOWLEDGE_PROPOSALS", [])
        proposals: List[KnowledgeProposal] = []
        if isinstance(proposals_raw, list):
            for idx, p_item in enumerate(proposals_raw):
                if isinstance(p_item, dict):
                    try:
                        p_obj = KnowledgeProposal.from_dict(p_item, default_role=role.lower())
                        p_errs = p_obj.validate()
                        for pe in p_errs:
                            errors.append(f"Knowledge proposal [{idx}] '{p_obj.id}' error: {pe}")

                        # INV-PKB-08: Feature Boundary Invariant
                        if p_obj.type == "feature":
                            bad_components = ("service", "repository", "controller", "handler", "dao", "manager")
                            id_lower = p_obj.id.lower()
                            title_lower = p_obj.title.lower()
                            if any(id_lower.endswith(f"-{bc}") or id_lower == bc for bc in bad_components) or \
                               any(f" {bc}" in title_lower or title_lower.endswith(bc) for bc in bad_components):
                                errors.append(
                                    f"Protocol invariant violation (INV-PKB-08): Proposal '{p_obj.id}' defines an internal code component ('{p_obj.title}') as a feature. "
                                    "Features must represent observable user/consumer capabilities (e.g. 'User Login'). Internal components belong in architecture."
                                )
                        proposals.append(p_obj)
                    except Exception as e:
                        errors.append(f"Invalid knowledge proposal format at index {idx}: {e}")
                elif p_item:
                    errors.append(f"Knowledge proposal at index {idx} must be a dictionary, got {type(p_item).__name__}")
        elif proposals_raw:
            errors.append(f"KNOWLEDGE_PROPOSALS must be a list of proposals, got {type(proposals_raw).__name__}")

        # Invariant INV-PKB-01: Executor must never emit KNOWLEDGE_PROPOSALS
        if role == "EXECUTOR" and proposals:
            errors.append(
                "Protocol invariant violation (INV-PKB-01): Executor is strictly prohibited from emitting KNOWLEDGE_PROPOSALS. "
                "Executor produces implementation code only."
            )

        # Invariant INV-PKB-02: Planner must never emit KNOWLEDGE_PROPOSALS
        if role == "PLANNER" and proposals:
            errors.append(
                "Protocol invariant violation (INV-PKB-02): Planner is strictly prohibited from emitting KNOWLEDGE_PROPOSALS. "
                "Planner is a read-only consumer for task decomposition."
            )

        # 4. Extract standard fields
        exit_code = 0
        try:
            exit_code = int(normalized_data.get("EXIT_CODE", 0))
        except (ValueError, TypeError):
            pass

        reason = normalized_data.get("REASON")
        confidence = normalized_data.get("CONFIDENCE")
        next_action = normalized_data.get("NEXT_ACTION")

        issues_raw = normalized_data.get("ISSUES", {})
        issues = {}
        if isinstance(issues_raw, dict):
            for k, v in issues_raw.items():
                if isinstance(v, list):
                    issues[str(k).upper()] = [x if isinstance(x, dict) else str(x) for x in v]
                elif v:
                    issues[str(k).upper()] = [v if isinstance(v, dict) else str(v)]

        # Remaining role-specific fields
        custom_fields = {}
        standard_keys = {
            "ROLE", "STATUS", "HANDOFF", "EXIT_CODE", "REASON",
            "CONFIDENCE", "NEXT_ACTION", "ISSUES", "START_TIME",
            "END_TIME", "DURATION", "INPUTS", "OUTPUTS", "PROMPT_VERSION",
            "TASK_ID", "KNOWLEDGE_PROPOSALS",
        }
        for k, v in normalized_data.items():
            if k not in standard_keys:
                custom_fields[k] = v

        is_valid = len(errors) == 0
        if not is_valid:
            from forge.stages.definition import StageOrder
            if StageOrder.is_success_status(status, expected_role.lower()):
                status = "FAILED"
        return MachineReport(
            role=role,
            status=status,
            handoff=handoff,
            exit_code=exit_code,
            reason=reason,
            confidence=str(confidence) if confidence is not None else None,
            next_action=str(next_action) if next_action is not None else None,
            issues=issues,
            data=custom_fields,
            proposals=proposals,
            raw_yaml=raw_yaml,
            is_valid=is_valid,
            validation_errors=errors,
        )
