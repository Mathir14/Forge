"""Validator for machine protocol reports."""

from typing import Dict, Any, List, Optional, Set
from forge.protocol.report import MachineReport


class MachineReportValidator:
    ALLOWED_STATUSES = {
        "CRITIC": {"CRITIQUE_COMPLETE", "COMPLETED", "PASSED", "APPROVED", "SUCCESS", "BLOCKED", "FAILED", "REJECTED"},
        "ARCHITECT": {"APPROVED", "READY", "SUCCESS", "COMPLETED", "FAILED", "BLOCKED", "REJECTED"},
        "PLANNER": {"READY", "APPROVED", "SUCCESS", "COMPLETED", "FAILED", "BLOCKED", "REJECTED"},
        "EXECUTOR": {"SUCCESS", "APPROVED", "COMPLETED", "FAILED", "BLOCKED", "REJECTED"},
        "REVIEWER": {"APPROVED", "CHANGES_REQUIRED", "BLOCKED", "REJECTED", "FAILED"},
    }

    ALLOWED_HANDOFFS = {
        "CRITIC": {"ARCHITECT", "PLANNER", "NONE"},
        "ARCHITECT": {"PLANNER", "NONE"},
        "PLANNER": {"EXECUTOR", "ARCHITECT", "NONE"},
        "EXECUTOR": {"REVIEWER", "PLANNER", "ARCHITECT", "NONE"},
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
        role = str(normalized_data.get("ROLE", expected_role)).upper()
        if role != expected_role.upper():
            errors.append(f"Expected role '{expected_role.upper()}', got '{role}'")

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
                    issues[str(k).upper()] = [str(x) for x in v]
                elif v:
                    issues[str(k).upper()] = [str(v)]

        # Remaining role-specific fields
        custom_fields = {}
        standard_keys = {
            "ROLE", "STATUS", "HANDOFF", "EXIT_CODE", "REASON",
            "CONFIDENCE", "NEXT_ACTION", "ISSUES", "START_TIME",
            "END_TIME", "DURATION", "INPUTS", "OUTPUTS", "PROMPT_VERSION",
            "TASK_ID",
        }
        for k, v in normalized_data.items():
            if k not in standard_keys:
                custom_fields[k] = v

        is_valid = len(errors) == 0
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
            raw_yaml=raw_yaml,
            is_valid=is_valid,
            validation_errors=errors,
        )
