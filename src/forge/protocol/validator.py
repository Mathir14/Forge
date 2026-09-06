"""Validator for machine protocol reports."""

from typing import Dict, Any, List
from forge.protocol.report import MachineReport


class MachineReportValidator:
    ALLOWED_STATUSES = {
        "CRITIC": {"CRITIQUE_COMPLETE", "APPROVED", "BLOCKED", "READY"},
        "ARCHITECT": {"APPROVED", "REJECTED", "BLOCKED", "READY"},
        "PLANNER": {"READY", "BLOCKED", "APPROVED", "REJECTED"},
        "EXECUTOR": {"SUCCESS", "FAILED", "BLOCKED"},
        "REVIEWER": {"APPROVED", "CHANGES_REQUIRED", "BLOCKED", "REJECTED"},
    }

    ALLOWED_HANDOFFS = {
        "CRITIC": {"ARCHITECT", "PLANNER", "NONE"},
        "ARCHITECT": {"PLANNER", "NONE"},
        "PLANNER": {"EXECUTOR", "ARCHITECT", "NONE"},
        "EXECUTOR": {"REVIEWER", "PLANNER", "ARCHITECT", "NONE"},
        "REVIEWER": {"NONE", "EXECUTOR", "ARCHITECT"},
    }

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
        allowed_status_set = cls.ALLOWED_STATUSES.get(expected_role.upper(), set())
        if allowed_status_set and status not in allowed_status_set:
            errors.append(
                f"Status '{status}' not in allowed statuses {list(allowed_status_set)}"
            )

        # 3. Handoff validation
        handoff = str(normalized_data.get("HANDOFF", "NONE")).upper()
        allowed_handoff_set = cls.ALLOWED_HANDOFFS.get(expected_role.upper(), set())
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
