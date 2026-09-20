from pathlib import Path
import yaml
from forge.protocol.parser import MachineReportParser
from forge.protocol.validator import MachineReportValidator


def test_extract_yaml_from_markdown():
    sample = """
# Human Report
Summary: Designed JWT system

```yaml
ROLE: ARCHITECT
STATUS: APPROVED
HANDOFF: PLANNER
ARCHITECTURE:
  MODULES:
    - auth
    - tokens
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(sample)
    assert data["ROLE"] == "ARCHITECT"
    assert data["STATUS"] == "APPROVED"
    assert data["HANDOFF"] == "PLANNER"
    assert "auth" in data["ARCHITECTURE"]["MODULES"]


def test_validate_machine_report():
    raw_data = {
        "ROLE": "ARCHITECT",
        "STATUS": "APPROVED",
        "HANDOFF": "PLANNER",
        "EXIT_CODE": 0,
        "CONFIDENCE": "HIGH",
        "ISSUES": {
            "CRITICAL": [],
            "MAJOR": ["Need secret key rotation strategy"],
        },
    }
    report = MachineReportValidator.validate(raw_data, expected_role="ARCHITECT")
    assert report.is_valid
    assert report.status == "APPROVED"
    assert report.handoff == "PLANNER"
    assert len(report.issues["MAJOR"]) == 1


def test_validate_invalid_status():
    raw_data = {
        "ROLE": "ARCHITECT",
        "STATUS": "INVALID_STATUS",
    }
    report = MachineReportValidator.validate(raw_data, expected_role="ARCHITECT")
    assert not report.is_valid
    assert len(report.validation_errors) > 0


# ===========================================================================
# Regression Tests for Role Machine Reports
# Verify:
# 1. MachineReportParser.extract_yaml()
# 2. yaml.safe_load() succeeds
# 3. ROLE, STATUS, HANDOFF extracted correctly
# 4. MachineReportValidator.validate() returns is_valid=True
# ===========================================================================

def test_regression_architect_output_parses_successfully():
    """Verify Architect output parses successfully into ONE complete valid YAML machine report."""
    output = """
# SOFTWARE ARCHITECT
## Human Report
- Summary: Microservice decomposition approved.
- Recommendation: Proceed to planning.

## Machine Report
```yaml
ROLE: ARCHITECT
PROMPT_VERSION: 1.0
TASK_ID: task-001
START_TIME: 2026-09-20T10:00:00Z
END_TIME: 2026-09-20T10:05:00Z
DURATION: 300s
STATUS: APPROVED
EXIT_CODE: 0
HANDOFF: PLANNER
REASON: Architecture is verified and sound.
INPUTS:
  - spec.md
OUTPUTS:
  - architecture.md
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: Proceed to planning
ARCHITECTURE:
  MODULES:
    - auth
    - core
  NEW_INTERFACES:
    - TokenProvider
  REFACTOR_REQUIRED: NO
  BREAKING_ARCHITECTURE_CHANGE: NO
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(output, expected_role="ARCHITECT")
    assert raw_yaml != ""
    loaded = yaml.safe_load(raw_yaml)
    assert isinstance(loaded, dict)
    assert data.get("ROLE") == "ARCHITECT"
    assert data.get("STATUS") == "APPROVED"
    assert data.get("HANDOFF") == "PLANNER"
    report = MachineReportValidator.validate(data, expected_role="ARCHITECT", raw_yaml=raw_yaml)
    assert report.is_valid is True
    assert report.role == "ARCHITECT"
    assert report.status == "APPROVED"
    assert report.handoff == "PLANNER"


def test_regression_planner_output_parses_successfully():
    """Verify Planner output parses successfully into ONE complete valid YAML machine report."""
    output = """
# PROJECT PLANNER
## Human Report
- Summary: Decomposed tasks for authentication module.
- Acceptance Criteria: All unit tests pass.

## Machine Report
```yaml
ROLE: PLANNER
PROMPT_VERSION: 1.0
TASK_ID: task-002
START_TIME: 2026-09-20T10:05:00Z
END_TIME: 2026-09-20T10:10:00Z
DURATION: 300s
STATUS: READY
EXIT_CODE: 0
HANDOFF: EXECUTOR
REASON: Implementation plan is decomposed and ready.
INPUTS:
  - architecture.md
OUTPUTS:
  - tasks.json
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: Proceed to execution
TASK_COUNT: 2
TASKS:
  - Implement service layer
  - Add unit tests
DEPENDENCIES: []
ACCEPTANCE_CRITERIA:
  - All unit tests pass
VALIDATION_REQUIRED:
  - pytest tests/
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(output, expected_role="PLANNER")
    assert raw_yaml != ""
    loaded = yaml.safe_load(raw_yaml)
    assert isinstance(loaded, dict)
    assert data.get("ROLE") == "PLANNER"
    assert data.get("STATUS") == "READY"
    assert data.get("HANDOFF") == "EXECUTOR"
    report = MachineReportValidator.validate(data, expected_role="PLANNER", raw_yaml=raw_yaml)
    assert report.is_valid is True
    assert report.role == "PLANNER"
    assert report.status == "READY"
    assert report.handoff == "EXECUTOR"


def test_regression_executor_output_parses_successfully():
    """Verify Executor output parses successfully into ONE complete valid YAML machine report."""
    output = """
# SOFTWARE ENGINEER
## Human Report
- Summary: Implemented service layer and unit tests.
- Commands: pytest tests/

## Machine Report
```yaml
ROLE: EXECUTOR
PROMPT_VERSION: 1.0
TASK_ID: task-003
START_TIME: 2026-09-20T10:10:00Z
END_TIME: 2026-09-20T10:25:00Z
DURATION: 900s
STATUS: SUCCESS
EXIT_CODE: 0
HANDOFF: REVIEWER
REASON: Implementation completed and validated.
INPUTS:
  - tasks.json
OUTPUTS:
  - src/
  - tests/
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: Proceed to review
VALIDATION:
  COMMANDS:
    - pytest
ARTIFACTS:
  ARCHITECTURE_CHANGED: NO
  API_CHANGED: NO
  DATABASE_SCHEMA_CHANGED: NO
  NEW_DEPENDENCIES: []
  BREAKING_CHANGE: NO
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(output, expected_role="EXECUTOR")
    assert raw_yaml != ""
    loaded = yaml.safe_load(raw_yaml)
    assert isinstance(loaded, dict)
    assert data.get("ROLE") == "EXECUTOR"
    assert data.get("STATUS") == "SUCCESS"
    assert data.get("HANDOFF") == "REVIEWER"
    report = MachineReportValidator.validate(data, expected_role="EXECUTOR", raw_yaml=raw_yaml)
    assert report.is_valid is True
    assert report.role == "EXECUTOR"
    assert report.status == "SUCCESS"
    assert report.handoff == "REVIEWER"


def test_regression_reviewer_output_parses_successfully():
    """Verify Reviewer output parses successfully into ONE complete valid YAML machine report."""
    output = """
# SENIOR REVIEWER
## Human Report
- Executive Summary: Changes verified against git diff.
- Scores: Testing 10/10, Architecture 10/10.

## Machine Report
```yaml
ROLE: REVIEWER
PROMPT_VERSION: 1.0
TASK_ID: task-004
START_TIME: 2026-09-20T10:25:00Z
END_TIME: 2026-09-20T10:30:00Z
DURATION: 300s
STATUS: APPROVED
EXIT_CODE: 0
HANDOFF: NONE
REASON: All claims verified against git diff and tests pass.
INPUTS:
  - git diff
OUTPUTS:
  - review.md
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: Complete run
SCORES:
  ARCHITECTURE: 10
  MAINTAINABILITY: 9
  READABILITY: 10
  SECURITY: 10
  PERFORMANCE: 9
  TESTING: 10
APPROVAL: YES
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(output, expected_role="REVIEWER")
    assert raw_yaml != ""
    loaded = yaml.safe_load(raw_yaml)
    assert isinstance(loaded, dict)
    assert data.get("ROLE") == "REVIEWER"
    assert data.get("STATUS") == "APPROVED"
    assert data.get("HANDOFF") == "NONE"
    report = MachineReportValidator.validate(data, expected_role="REVIEWER", raw_yaml=raw_yaml)
    assert report.is_valid is True
    assert report.role == "REVIEWER"
    assert report.status == "APPROVED"
    assert report.handoff == "NONE"


def test_regression_critic_output_parses_successfully():
    """Verify Critic output parses successfully into ONE complete valid YAML machine report."""
    output = """
# CODEBASE CRITIC & AUDITOR
## Human Report
- Executive Summary: Audit identified module boundary debt.
- Health Score: 8/10

## Machine Report
```yaml
ROLE: CRITIC
PROMPT_VERSION: 1.0
TASK_ID: task-005
START_TIME: 2026-09-20T09:50:00Z
END_TIME: 2026-09-20T09:55:00Z
DURATION: 300s
STATUS: CRITIQUE_COMPLETE
EXIT_CODE: 0
HANDOFF: ARCHITECT
REASON: Codebase audit completed, identified architectural priorities.
INPUTS:
  - codebase
OUTPUTS:
  - critic_report.md
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: Proceed to architecture
HEALTH_SCORE: 8
RECOMMENDED_ACTIONS:
  - Review modular boundaries
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(output, expected_role="CRITIC")
    assert raw_yaml != ""
    loaded = yaml.safe_load(raw_yaml)
    assert isinstance(loaded, dict)
    assert data.get("ROLE") == "CRITIC"
    assert data.get("STATUS") == "CRITIQUE_COMPLETE"
    assert data.get("HANDOFF") == "ARCHITECT"
    report = MachineReportValidator.validate(data, expected_role="CRITIC", raw_yaml=raw_yaml)
    assert report.is_valid is True
    assert report.role == "CRITIC"
    assert report.status == "CRITIQUE_COMPLETE"
    assert report.handoff == "ARCHITECT"


def test_regression_all_role_prompt_template_examples_parse_and_validate():
    """Verify that each role prompt markdown file in .ai/roles/ contains an example that parses and validates."""
    roles_dir = Path(__file__).resolve().parent.parent / ".ai" / "roles"
    expected_roles = {
        "architect": ("ARCHITECT", "APPROVED", "PLANNER"),
        "planner": ("PLANNER", "READY", "EXECUTOR"),
        "executor": ("EXECUTOR", "SUCCESS", "REVIEWER"),
        "reviewer": ("REVIEWER", "APPROVED", "NONE"),
        "critic": ("CRITIC", "CRITIQUE_COMPLETE", "ARCHITECT"),
    }

    for role_name, (expected_role, expected_status, expected_handoff) in expected_roles.items():
        role_file = roles_dir / f"{role_name}.md"
        assert role_file.exists(), f"Missing role file: {role_file}"
        content = role_file.read_text(encoding="utf-8")

        data, raw_yaml = MachineReportParser.extract_yaml(content, expected_role=expected_role)
        assert raw_yaml != "", f"No YAML extracted from {role_file}"
        loaded = yaml.safe_load(raw_yaml)
        assert isinstance(loaded, dict)
        assert loaded["ROLE"] == expected_role
        assert loaded["STATUS"] == expected_status
        assert loaded["HANDOFF"] == expected_handoff

        report = MachineReportValidator.validate(data, expected_role=expected_role, raw_yaml=raw_yaml)
        assert report.is_valid is True, f"Validation failed for {role_file}: {report.validation_errors}"
        assert report.role == expected_role
        assert report.status == expected_status
        assert report.handoff == expected_handoff


def test_parser_diagnostics_on_malformed_yaml(capsys):
    """Verify parser includes exception in diagnostic output and preserves strict validation."""
    malformed_output = """
```yaml
ROLE: ARCHITECT
STATUS: [unclosed list
HANDOFF: PLANNER
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(malformed_output, expected_role="ARCHITECT")
    # Must not silently accept malformed YAML
    assert data == {}
    # Diagnostic error recorded
    assert MachineReportParser.last_error is not None
    assert MachineReportParser.last_diagnostic is not None
    assert "YAML parse error" in MachineReportParser.last_diagnostic
    # Check that diagnostic was emitted to stderr
    captured = capsys.readouterr()
    assert "YAML parse error" in captured.err or "MachineReportParser diagnostic" in captured.err

    # Strict validation preserved: empty/malformed data fails validation
    report = MachineReportValidator.validate(data, expected_role="ARCHITECT", raw_yaml=raw_yaml)
    assert report.is_valid is False


def test_regression_planner_unescaped_colon_failure_and_remediation():
    """Reproduce exact Planner YAML failure with unquoted colon task, and verify quoted and structured remediation."""
    # 1. Unquoted colon causes YAML ScannerError (mapping values are not allowed here)
    unquoted_task_str = "T6: flag.vote/revoke: DB unique enforcement, soft-revoke..."
    unquoted_output = f"""
```yaml
ROLE: PLANNER
PROMPT_VERSION: 1.0
TASK_ID: task-042
STATUS: READY
EXIT_CODE: 0
HANDOFF: EXECUTOR
TASK_COUNT: 1
TASKS:
  - {unquoted_task_str}
```
"""
    # Verify yaml.safe_load() fails on unquoted offending line
    import pytest
    with pytest.raises(yaml.YAMLError) as exc_info:
        yaml.safe_load(f"""
ROLE: PLANNER
STATUS: READY
HANDOFF: EXECUTOR
TASKS:
  - {unquoted_task_str}
""")
    assert "mapping values are not allowed here" in str(exc_info.value)

    # MachineReportParser must not silently accept malformed YAML and records diagnostic error
    data_unquoted, _ = MachineReportParser.extract_yaml(unquoted_output, expected_role="PLANNER")
    assert data_unquoted == {}
    assert MachineReportParser.last_error is not None
    assert "mapping values are not allowed here" in str(MachineReportParser.last_error)
    report_unquoted = MachineReportValidator.validate(data_unquoted, expected_role="PLANNER")
    assert report_unquoted.is_valid is False

    # 2. Corrected quoted output parses successfully through:
    # - yaml.safe_load()
    # - MachineReportParser.extract_yaml()
    # - MachineReportValidator.validate()
    quoted_output = f"""
# PROJECT PLANNER
## Machine Report
```yaml
ROLE: PLANNER
PROMPT_VERSION: 1.0
TASK_ID: task-042
START_TIME: 2026-09-20T10:05:00Z
END_TIME: 2026-09-20T10:10:00Z
DURATION: 300s
STATUS: READY
EXIT_CODE: 0
HANDOFF: EXECUTOR
REASON: Plan decomposed with database enforcement tasks.
TASK_COUNT: 1
TASKS:
  - "{unquoted_task_str}"
DEPENDENCIES: []
ACCEPTANCE_CRITERIA:
  - "DB unique constraint enforced"
VALIDATION_REQUIRED:
  - pytest tests/
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: Proceed to execution
```
"""
    # Test MachineReportParser.extract_yaml()
    data_quoted, raw_yaml_quoted = MachineReportParser.extract_yaml(quoted_output, expected_role="PLANNER")
    assert raw_yaml_quoted != ""

    # Test yaml.safe_load()
    loaded = yaml.safe_load(raw_yaml_quoted)
    assert isinstance(loaded, dict)
    assert loaded["TASKS"] == [unquoted_task_str]
    assert data_quoted.get("ROLE") == "PLANNER"
    assert data_quoted.get("STATUS") == "READY"
    assert data_quoted.get("HANDOFF") == "EXECUTOR"

    # Test MachineReportValidator.validate()
    report_quoted = MachineReportValidator.validate(data_quoted, expected_role="PLANNER", raw_yaml=raw_yaml_quoted)
    assert report_quoted.is_valid is True
    assert report_quoted.role == "PLANNER"
    assert report_quoted.status == "READY"
    assert report_quoted.handoff == "EXECUTOR"

    # 3. Also verify preferred structured YAML representation (id, component, description)
    nested_output = """
```yaml
ROLE: PLANNER
PROMPT_VERSION: 1.0
TASK_ID: task-042
STATUS: READY
EXIT_CODE: 0
HANDOFF: EXECUTOR
TASK_COUNT: 1
TASKS:
  - id: T6
    component: flag.vote/revoke
    description: "DB unique enforcement, soft-revoke..."
```
"""
    data_nested, raw_yaml_nested = MachineReportParser.extract_yaml(nested_output, expected_role="PLANNER")
    assert raw_yaml_nested != ""
    loaded_nested = yaml.safe_load(raw_yaml_nested)
    assert isinstance(loaded_nested, dict)
    assert loaded_nested["TASKS"][0]["id"] == "T6"
    assert loaded_nested["TASKS"][0]["component"] == "flag.vote/revoke"
    assert loaded_nested["TASKS"][0]["description"] == "DB unique enforcement, soft-revoke..."
    report_nested = MachineReportValidator.validate(data_nested, expected_role="PLANNER", raw_yaml=raw_yaml_nested)
    assert report_nested.is_valid is True


