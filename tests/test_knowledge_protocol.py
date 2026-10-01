"""Unit tests for PKB Protocol validation and invariant enforcement."""

import pytest
from forge.protocol.validator import MachineReportValidator
from forge.protocol.parser import MachineReportParser
from forge.core.knowledge import KnowledgeProposal


def test_valid_proposals_for_architect():
    yaml_text = """
```yaml
ROLE: ARCHITECT
STATUS: APPROVED
HANDOFF: PLANNER
KNOWLEDGE_PROPOSALS:
  - action: ASSERT
    id: "module-auth-limiter"
    type: "architecture"
    title: "Auth Limiter"
    summary: "Token bucket limiter."
    evidence: ["src/auth/limiter.py"]
```
"""
    data, _ = MachineReportParser.extract_yaml(yaml_text, expected_role="ARCHITECT")
    report = MachineReportValidator.validate(data, expected_role="ARCHITECT")
    assert report.is_valid, f"Validation errors: {report.validation_errors}"
    assert len(report.proposals) == 1
    p = report.proposals[0]
    assert p.id == "module-auth-limiter"
    assert p.action == "ASSERT"
    assert p.role == "architect"


def test_invariant_inv_pkb_01_executor_prohibition():
    data = {
        "ROLE": "EXECUTOR",
        "STATUS": "SUCCESS",
        "HANDOFF": "TESTER",
        "KNOWLEDGE_PROPOSALS": [
            {
                "action": "ASSERT",
                "id": "module-shortcut",
                "type": "architecture",
                "title": "Shortcut",
            }
        ],
    }
    report = MachineReportValidator.validate(data, expected_role="EXECUTOR")
    assert not report.is_valid
    assert any("INV-PKB-01" in err for err in report.validation_errors)
    assert any("Executor is strictly prohibited from emitting KNOWLEDGE_PROPOSALS" in err for err in report.validation_errors)


def test_invariant_inv_pkb_02_planner_read_only():
    data = {
        "ROLE": "PLANNER",
        "STATUS": "READY",
        "HANDOFF": "EXECUTOR",
        "KNOWLEDGE_PROPOSALS": [
            {
                "action": "ASSERT",
                "id": "feature-cart",
                "type": "feature",
                "title": "Cart Feature",
            }
        ],
    }
    report = MachineReportValidator.validate(data, expected_role="PLANNER")
    assert not report.is_valid
    assert any("INV-PKB-02" in err for err in report.validation_errors)
    assert any("Planner is strictly prohibited from emitting KNOWLEDGE_PROPOSALS" in err for err in report.validation_errors)


def test_invariant_inv_pkb_08_feature_boundary_validation():
    # Attempting to declare an internal Service as a feature
    data = {
        "ROLE": "ARCHITECT",
        "STATUS": "APPROVED",
        "HANDOFF": "PLANNER",
        "KNOWLEDGE_PROPOSALS": [
            {
                "action": "ASSERT",
                "id": "payment-service",
                "type": "feature",
                "title": "Payment Service",
            }
        ],
    }
    report = MachineReportValidator.validate(data, expected_role="ARCHITECT")
    assert not report.is_valid
    assert any("INV-PKB-08" in err for err in report.validation_errors)
    assert any("defines an internal code component ('Payment Service') as a feature" in err for err in report.validation_errors)


def test_valid_feature_proposal_for_architect():
    # User-observable capability
    data = {
        "ROLE": "ARCHITECT",
        "STATUS": "APPROVED",
        "HANDOFF": "PLANNER",
        "KNOWLEDGE_PROPOSALS": [
            {
                "action": "ASSERT",
                "id": "user-login",
                "type": "feature",
                "title": "User Login",
                "summary": "Allows users to sign in with email and password.",
            }
        ],
    }
    report = MachineReportValidator.validate(data, expected_role="ARCHITECT")
    assert report.is_valid, f"Validation errors: {report.validation_errors}"
    assert len(report.proposals) == 1
    assert report.proposals[0].type == "feature"


def test_proposal_validation_errors():
    data = {
        "ROLE": "TESTER",
        "STATUS": "PASS",
        "HANDOFF": "REVIEWER",
        "KNOWLEDGE_PROPOSALS": [
            {
                "action": "INVALID_ACTION",
                "id": "bad-id!",
                "type": "feature",
            }
        ],
    }
    report = MachineReportValidator.validate(data, expected_role="TESTER")
    assert not report.is_valid
    assert any("Proposal action 'INVALID_ACTION' invalid" in err for err in report.validation_errors)
    assert any("must be a valid kebab-case slug" in err for err in report.validation_errors)
