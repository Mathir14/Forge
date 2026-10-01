from pathlib import Path
import pytest
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
  LINT:
    status: PASSED
    details: "next lint: 0 warnings, 0 errors"
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
REASON: "All claims verified against git diff and tests pass."
INPUTS:
  - "git diff"
OUTPUTS:
  - "review.md"
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: "Complete run"
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


def test_regression_executor_unquoted_colon_failure_and_remediation():
    """Reproduce exact Executor unquoted colon failure in VALIDATION, and verify structured remediation."""
    # 1. Reproduce unquoted colon in VALIDATION scalar causing ScannerError
    unquoted_validation = "PASSED (next lint: 0 warnings, 0 errors)"
    failing_report = f"""
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
  LINT: {unquoted_validation}
ARTIFACTS:
  ARCHITECTURE_CHANGED: NO
  API_CHANGED: NO
  DATABASE_SCHEMA_CHANGED: NO
  NEW_DEPENDENCIES: []
  BREAKING_CHANGE: NO
```
"""
    # yaml.safe_load() must fail on the unquoted colon scalar
    import pytest
    with pytest.raises(yaml.YAMLError) as exc_info:
        yaml.safe_load(f"""
VALIDATION:
  LINT: {unquoted_validation}
""")
    assert "mapping values are not allowed here" in str(exc_info.value)

    # MachineReportParser must record diagnostic and return empty dict
    data_failing, raw_failing = MachineReportParser.extract_yaml(failing_report, expected_role="EXECUTOR")
    assert data_failing == {}
    assert MachineReportParser.last_error is not None
    assert "mapping values are not allowed here" in str(MachineReportParser.last_error)

    # MachineReportValidator must fail invalid report
    report_failing = MachineReportValidator.validate(data_failing, expected_role="EXECUTOR")
    assert report_failing.is_valid is False

    # 2. Hardened structured remediation verifies:
    # - yaml.safe_load() succeeds
    # - MachineReportParser.extract_yaml() succeeds
    # - MachineReportValidator.validate() succeeds
    role_file = Path(__file__).resolve().parent.parent / ".ai" / "roles" / "executor.md"
    content = role_file.read_text(encoding="utf-8")
    data_structured, raw_yaml_structured = MachineReportParser.extract_yaml(content, expected_role="EXECUTOR")
    assert raw_yaml_structured != ""
    loaded = yaml.safe_load(raw_yaml_structured)
    assert isinstance(loaded, dict)
    assert loaded["VALIDATION"]["LINT"]["status"] == "PASSED"
    assert loaded["VALIDATION"]["LINT"]["details"] == "next lint: 0 warnings, 0 errors"
    assert loaded["VALIDATION"]["COMMANDS"] == ["pytest"]

    report_structured = MachineReportValidator.validate(data_structured, expected_role="EXECUTOR", raw_yaml=raw_yaml_structured)
    assert report_structured.is_valid is True
    assert report_structured.role == "EXECUTOR"
    assert report_structured.status == "SUCCESS"
    assert report_structured.handoff == "REVIEWER"


def test_protocol_structured_validation_format_is_valid_yaml():
    """Verify preferred structured VALIDATION format parses independently of prompt templates."""
    valid = yaml.safe_load("""
VALIDATION:
  LINT:
    status: PASSED
    details: "next lint: 0 warnings, 0 errors"
""")
    assert valid["VALIDATION"]["LINT"]["status"] == "PASSED"
    assert valid["VALIDATION"]["LINT"]["details"] == "next lint: 0 warnings, 0 errors"


def test_regression_reviewer_failure_a_bracket_syntax_error():
    """Verify Reviewer unquoted bracket syntax fails YAML parsing and quoted remediation succeeds."""
    # 1. Unquoted bracket list item causes yaml.parser.ParserError
    failing_report = """```yaml
ROLE: REVIEWER
PROMPT_VERSION: 1.0
TASK_ID: task-001
START_TIME: 2026-09-20T10:25:00Z
END_TIME: 2026-09-20T10:30:00Z
DURATION: 300s
STATUS: CHANGES_REQUIRED
EXIT_CODE: 1
HANDOFF: EXECUTOR
REASON: "Issues identified during review."
INPUTS:
  - "git diff"
OUTPUTS:
  - "review.md"
ISSUES:
  CRITICAL:
    - [C1] register route accepts client-supplied role
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: "Remediate findings"
SCORES:
  ARCHITECTURE: 5
  MAINTAINABILITY: 6
  READABILITY: 7
  SECURITY: 3
  PERFORMANCE: 8
  TESTING: 6
APPROVAL: NO
```"""
    with pytest.raises(yaml.YAMLError) as exc_info:
        yaml.safe_load("""
ISSUES:
  CRITICAL:
    - [C1] register route accepts client-supplied role
""")
    assert "expected ',' or ']'" in str(exc_info.value) or isinstance(exc_info.value, yaml.parser.ParserError)

    # MachineReportParser must record diagnostic and return empty dict
    data_failing, raw_failing = MachineReportParser.extract_yaml(failing_report, expected_role="REVIEWER")
    assert data_failing == {}
    assert MachineReportParser.last_error is not None

    # MachineReportValidator must fail invalid report
    report_failing = MachineReportValidator.validate(data_failing, expected_role="REVIEWER")
    assert report_failing.is_valid is False

    # 2. Hardened quoted string remediation succeeds across parser and validator
    remediated_yaml = """
ROLE: REVIEWER
PROMPT_VERSION: 1.0
TASK_ID: task-001
START_TIME: 2026-09-20T10:25:00Z
END_TIME: 2026-09-20T10:30:00Z
DURATION: 300s
STATUS: CHANGES_REQUIRED
EXIT_CODE: 1
HANDOFF: EXECUTOR
REASON: "Issues identified during review."
INPUTS:
  - "git diff"
OUTPUTS:
  - "review.md"
ISSUES:
  CRITICAL:
    - "[C1] Register route accepts client-supplied role."
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: "Remediate findings"
SCORES:
  ARCHITECTURE: 5
  MAINTAINABILITY: 6
  READABILITY: 7
  SECURITY: 3
  PERFORMANCE: 8
  TESTING: 6
APPROVAL: NO
"""
    loaded = yaml.safe_load(remediated_yaml)
    assert isinstance(loaded, dict)
    assert loaded["ISSUES"]["CRITICAL"] == ["[C1] Register route accepts client-supplied role."]

    report_remediated = MachineReportValidator.validate(loaded, expected_role="REVIEWER", raw_yaml=remediated_yaml)
    assert report_remediated.is_valid is True
    assert report_remediated.role == "REVIEWER"
    assert report_remediated.status == "CHANGES_REQUIRED"
    assert report_remediated.handoff == "EXECUTOR"


def test_regression_reviewer_failure_b_unquoted_colon_scalar_error():
    """Verify Reviewer unquoted scalar containing colon fails YAML parsing and quoted remediation succeeds."""
    # 1. Unquoted colon in plain scalar causes yaml.scanner.ScannerError
    failing_report = """```yaml
ROLE: REVIEWER
PROMPT_VERSION: 1.0
TASK_ID: task-001
START_TIME: 2026-09-20T10:25:00Z
END_TIME: 2026-09-20T10:30:00Z
DURATION: 300s
STATUS: CHANGES_REQUIRED
EXIT_CODE: 1
HANDOFF: EXECUTOR
REASON: REQUIRED solely for convention debt: Zod mandated but unused in auth schema
INPUTS:
  - "git diff"
OUTPUTS:
  - "review.md"
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: "Refactor auth schema"
SCORES:
  ARCHITECTURE: 7
  MAINTAINABILITY: 7
  READABILITY: 8
  SECURITY: 9
  PERFORMANCE: 8
  TESTING: 8
APPROVAL: NO
```"""
    with pytest.raises(yaml.YAMLError) as exc_info:
        yaml.safe_load("""
REASON: REQUIRED solely for convention debt: Zod mandated but unused in auth schema
""")
    assert "mapping values are not allowed here" in str(exc_info.value) or isinstance(exc_info.value, yaml.scanner.ScannerError)

    # MachineReportParser must record diagnostic and return empty dict
    data_failing, raw_failing = MachineReportParser.extract_yaml(failing_report, expected_role="REVIEWER")
    assert data_failing == {}
    assert MachineReportParser.last_error is not None

    # MachineReportValidator must fail invalid report
    report_failing = MachineReportValidator.validate(data_failing, expected_role="REVIEWER")
    assert report_failing.is_valid is False

    # 2. Hardened quoted string remediation succeeds across parser and validator
    remediated_yaml = """
ROLE: REVIEWER
PROMPT_VERSION: 1.0
TASK_ID: task-001
START_TIME: 2026-09-20T10:25:00Z
END_TIME: 2026-09-20T10:30:00Z
DURATION: 300s
STATUS: CHANGES_REQUIRED
EXIT_CODE: 1
HANDOFF: EXECUTOR
REASON: "REQUIRED solely for convention debt: Zod mandated but unused in auth schema"
INPUTS:
  - "git diff"
OUTPUTS:
  - "review.md"
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: "Refactor auth schema"
SCORES:
  ARCHITECTURE: 7
  MAINTAINABILITY: 7
  READABILITY: 8
  SECURITY: 9
  PERFORMANCE: 8
  TESTING: 8
APPROVAL: NO
"""
    loaded = yaml.safe_load(remediated_yaml)
    assert isinstance(loaded, dict)
    assert loaded["REASON"] == "REQUIRED solely for convention debt: Zod mandated but unused in auth schema"

    report_remediated = MachineReportValidator.validate(loaded, expected_role="REVIEWER", raw_yaml=remediated_yaml)
    assert report_remediated.is_valid is True
    assert report_remediated.role == "REVIEWER"
    assert report_remediated.status == "CHANGES_REQUIRED"
    assert report_remediated.handoff == "EXECUTOR"


def test_regression_reviewer_failure_c_multiline_prose_reason_error():
    """Verify Reviewer unquoted multiline prose REASON with colons fails YAML parsing and short quoted remediation succeeds."""
    # 1. Unquoted multiline prose containing colons causes yaml.scanner.ScannerError
    failing_report = """```yaml
ROLE: REVIEWER
PROMPT_VERSION: 1.0
TASK_ID: run-001
START_TIME: 2026-09-20T19:05:00Z
END_TIME: 2026-09-20T19:58:00Z
DURATION: ~53m
STATUS: CHANGES_REQUIRED
EXIT_CODE: 1
HANDOFF: EXECUTOR
REASON: Re-verified all executor claims independently (tsc 0 errors, lint clean, build 18/18, 33/33 tests vs live Postgres, migrate up-to-date, seed idempotent x2, zero cross-imports, no scope violations). C1, M3, M4, M5 and minors substantively confirmed. However previous reviews missed a genuine stored XSS: user-controlled website and sourceLink rendered verbatim into anchor href on companies/[id]/page.tsx lines 44, 61, 236 with only a length>=3 check upstream; React 18.3.1 does not block javascript hrefs, and submissions are auto-APPROVED so payloads are immediately public. Also the prior reviewer CHANGES_REQUIRED items were not fully closed: Zod mandated but unused, as-any still present in 3 files, plus minor robustness gaps.
INPUTS:
  - "src/app/companies/[id]/page.tsx"
OUTPUTS:
  - "review.md"
ISSUES:
  CRITICAL: []
  MAJOR:
    - "[XSS] Stored javascript URL in href: company.website and company.sourceLink rendered verbatim into anchor href on src/app/companies/[id]/page.tsx."
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: "Remediate findings"
SCORES:
  ARCHITECTURE: 5
  MAINTAINABILITY: 6
  READABILITY: 7
  SECURITY: 5
  PERFORMANCE: 8
  TESTING: 9
APPROVAL: NO
```"""
    data_failing, raw_failing = MachineReportParser.extract_yaml(failing_report, expected_role="REVIEWER")
    assert data_failing == {}
    assert MachineReportParser.last_error is not None
    assert "mapping values are not allowed here" in str(MachineReportParser.last_error)

    report_failing = MachineReportValidator.validate(data_failing, expected_role="REVIEWER")
    assert report_failing.is_valid is False

    # 2. Remediated: short concise quoted REASON for machine signaling, detailed prose in Human Report
    remediated_report = """# SENIOR REVIEWER
## Human Report
- Executive Summary: Re-verified all executor claims independently. Found stored XSS on company detail page.
- Recommendation: CHANGES_REQUIRED to sanitize URLs with http/https allowlist.

## Machine Report
```yaml
ROLE: REVIEWER
PROMPT_VERSION: 1.0
TASK_ID: run-001
START_TIME: 2026-09-20T19:05:00Z
END_TIME: 2026-09-20T19:58:00Z
DURATION: 300s
STATUS: CHANGES_REQUIRED
EXIT_CODE: 1
HANDOFF: EXECUTOR
REASON: "Stored XSS detected on company page and convention debt unclosed."
INPUTS:
  - "src/app/companies/[id]/page.tsx"
OUTPUTS:
  - "review.md"
ISSUES:
  CRITICAL: []
  MAJOR:
    - "[XSS] Stored javascript URL in href: company.website and company.sourceLink rendered verbatim into anchor href on src/app/companies/[id]/page.tsx."
  MINOR: []
CONFIDENCE: HIGH
NEXT_ACTION: "Remediate findings"
SCORES:
  ARCHITECTURE: 5
  MAINTAINABILITY: 6
  READABILITY: 7
  SECURITY: 5
  PERFORMANCE: 8
  TESTING: 9
APPROVAL: NO
```"""
    data_remediated, raw_remediated = MachineReportParser.extract_yaml(remediated_report, expected_role="REVIEWER")
    assert data_remediated != {}
    assert data_remediated["REASON"] == "Stored XSS detected on company page and convention debt unclosed."
    assert len(data_remediated["REASON"]) <= 100

    report_remediated = MachineReportValidator.validate(data_remediated, expected_role="REVIEWER", raw_yaml=raw_remediated)
    assert report_remediated.is_valid is True
    assert report_remediated.status == "CHANGES_REQUIRED"
    assert report_remediated.reason == "Stored XSS detected on company page and convention debt unclosed."


def test_regression_exact_planner_artifact_unspaced_fenced_yaml():
    """Regression: Verify exact 02_planner.md artifact from run-009 parses successfully despite unspaced ```yaml fence."""
    exact_planner_md = """## Human Report

### Summary
Decomposed the Architect-approved run-009 scope (11 refactors from the run-008 Critic audit) into 13 executable tasks. One task is the hard critical gate (fail-closed tagged-thread visibility in profile contracts); the rest are sprint debt. Verified current tree: 125/125 tests, clean tsc/lint with the `vh-pg` Postgres container up. No schema changes, no new deps, no architectural reversal.

### Assumptions
1. Postgres container `vh-pg` (port 5432, migrations applied) remains available for executor verification.
2. `getThreadById` fail-closed behavior is authoritative and untouched; only the two profile contracts need the shared predicate.
3. `ThreadCommentsSection` (threadId-based poster) remains the canonical comment UI after `DiscussionThreadSection` (threadRef-based) is removed — executor must verify before deletion.
4. ADR-012 wording/demo amendments are documentation-only edits to `decisions.md`; no src changes required for documentation.

### Dependencies
- T1 → T3 (same file, sequence), and T1 blocks verification of fail-closed leak fix.
- T3 (remove `DiscussionThreadSection`) must precede T2 removal of `threadRef` path to avoid orphaned consumers.
- T13 (bounded `getRejectedEntityIds`) informs how `buildVisibleThreadWhere` resolves rejected IDs; do T13's cap as part of T1 predicate work.
- T4/T5/T10/T11 are independent of the file-heavy refactors; can run in parallel.

### Tasks
- **T1 (CRITICAL):** Extract `buildVisibleThreadWhere()` in `shared-kernel/contracts`; apply to `getCompanyProfileData`/`getJobProfileData` tagged-thread queries to exclude threads with rejected experience, rejected experience-company, or any rejected ThreadTag/legacy entity reference. Align with `listInterviewThreads` predicate (single source of truth). *(docs: ADR-012.3 cap note in T13)*
- **T2 (MAJOR):** Delete `getOrCreateDiscussionThread` + `inFlightThreads`; `createComment` requires an existing APPROVED `threadId`; remove `threadRef` from `createCommentSchema`, `CreateCommentInput` (contracts/index.ts), and any poster components; update affected tests (thread-lifecycle, security-audit, auth-rbac).
- **T3 (MAJOR):** Remove dead `discussion`/`threads`-alias fields from `CompanyProfileResult`/`JobProfileResult`, orphaned `DiscussionThreadProfileDTO`/`CommentProfileDTO` in profile-contracts, the legacy `findFirst`+comments assembly, and delete `DiscussionThreadSection.tsx` + its test import.
- **T4 (MAJOR):** Unify GET `/api/threads` with the service boundary — invalid params return 200 empty list per ADR-012.5 (drop `ValidationError` 400).
- **T5 (MAJOR):** Retire `/questions` page + home link; redirect `/companies/[id]/interviews/new` → `/threads/new?companyId=...`; repoint all 4 links in `companies/[id]/page.tsx`; remove dead links.
- **T6 (MIN):** Bound `db.jobPosting.findMany` to `.take(100)` in `threads/new/page.tsx` (M4).
- **T7 (MIN):** Cap `interviewThreadQuerySchema.page` upper bound (bounded offset) in `shared-kernel/validation.ts`.
- **T8 (MIN):** Add typed `TaggedEntityDTO { entityType, entityId, entityName }`; replace the 4 `(tag as any).entityName` escapes in `threads/page.tsx` + `threads/[id]/page.tsx`.
- **T9 (MIN):** `threads/[id]/page.tsx` catch only `EntityNotFoundError → notFound()`; log/rethrow others.
- **T10 (MIN):** `auth-options.ts` jwt callback fails loudly if `db.session.create` fails (rethrow) instead of minting a doomed token; keep background pruner as-is.
- **T11 (MIN):** `reportAbuse` asserts target existence + visibility per `targetType` before queueing; single-source targetType→moderation-entityType mapping (replace direct passthrough).
- **T12 (MIN):** `/threads` pagination links preserve `q/companyId/role/outcome/difficulty` (+ page).
- **T13 (MIN):** Bound `getRejectedEntityIds` with a `take` cap + fail-closed empty-page fallback at service boundary; amend ADR-012.3 wording and ratify ADR-012.5 (200-empty) in `decisions.md`.

### Acceptance Criteria
1. Thread tagged to a REJECTED entity never appears on any APPROVED entity profile or listing (kernel test).
2. `getOrCreateDiscussionThread`, `threadRef`, `DiscussionThreadSection`, `discussion`/`threads` DTO fields all absent from src.
3. GET `/api/threads` and the page return identical shaped results on invalid params (empty list, 200).
4. No `(tag as any)` casts remain; tags are typed with `entityName`.
5. jwt callback propagates session-create failure; `reportAbuse` 404s on unknown/rejected targets.
6. All 125 existing tests updated-and-passing plus new kernel fail-closed test; `tsc --noEmit`, `next lint` clean.

### Validation
- `npx vitest run` (needs Postgres `vh-pg` up; expect ≥126 tests green)
- `npx tsc --noEmit`
- `npx next lint`
- Manual/grep: no `threadRef`, `getOrCreateDiscussionThread`, `DiscussionThreadSection`, `(tag as any)`, `/companies/[id]/interviews/new`, `/questions` home link remain.

### Risks
- T2 removes a public API used by legacy threads + tests; any missed consumer surfaces as a build break — mitigated by tsc + targeted grep in T2 AC.
- `buildVisibleThreadWhere` must reproduce the exact fail-closed conditions already in `listInterviewThreads` (tags none + legacy entityType/entityId NOT in rejected IDs) or listing/profile behavior diverges — mitigate by having T1 share the single predicate and add the cross-check kernel test.
- Removing `discussion` DTO could break the hardening-run005 assertions on `profile.discussion.comments`; executor must update those assertions in lockstep with T3.
- Postgres must be up; earlier red was purely environmental.

### Recommendation
**Proceed to execution.** Execute sequentially: T1 → T3 → T2 (file-conflict order), then T4-T13 in any order in parallel. Run full suite + tsc + lint after each major task; confirm DB up first.```yaml
ROLE: PLANNER
PROMPT_VERSION: 1.0
TASK_ID: run-009-plan
START_TIME: 2026-09-22T20:20:00Z
END_TIME: 2026-09-22T20:38:00Z
DURATION: 1080s
STATUS: READY
EXIT_CODE: 0
HANDOFF: EXECUTOR
REASON: "Critic run-008 fixes scoped into 13 ordered tasks; ready to execute."
INPUTS:
  - .forge/runs/run-009/01_architect.md
  - src/shared-kernel/contracts/profile-contracts.ts
  - src/shared-kernel/contracts/visibility.ts
  - src/shared-kernel/validation.ts
  - src/features/community/services/community-service.ts
  - src/features/interview-intelligence/services/interview-service.ts
  - src/shared-kernel/auth/auth-options.ts
  - src/app/threads/*, src/app/api/threads/route.ts, src/app/page.tsx
  - src/app/companies/[id]/page.tsx, src/components/DiscussionThreadSection.tsx
OUTPUTS:
  - run-009 execution plan (13 tasks)
ISSUES:
  CRITICAL:
    - summary: "profile-contracts tagged-thread queries lack M3/ADR-012 fail-closed filter causing rejected-entity content leak"
      location: "src/shared-kernel/contracts/profile-contracts.ts"
  MAJOR:
    - summary: "getOrCreateDiscussionThread in-memory lock races across instances; lazy thread creation obsolete under ADR-012"
      location: "src/features/community/services/community-service.ts"
    - summary: "Dead published discussion/threads interfaces and DiscussionThreadSection component"
      location: "src/shared-kernel/contracts/profile-contracts.ts, src/components/DiscussionThreadSection.tsx"
    - summary: "GET /api/threads returns 400 while page/service return empty for same invalid query; contradicts ADR-012.5"
      location: "src/app/api/threads/route.ts"
    - summary: "Respec wiring incomplete: /questions linked from home; /companies/[id]/interviews/new duplicate submission path"
      location: "src/app/page.tsx, src/app/companies/[id]/page.tsx"
  MINOR:
    - summary: "Unbounded jobPosting.findMany on thread creation page (M4)"
      location: "src/app/threads/new/page.tsx"
    - summary: "interviewThreadQuerySchema.page has no upper bound"
      location: "src/shared-kernel/validation.ts"
    - summary: "Four (tag as any).entityName type escapes"
      location: "src/app/threads/page.tsx, src/app/threads/[id]/page.tsx"
    - summary: "threads/[id] catches all errors into notFound()"
      location: "src/app/threads/[id]/page.tsx"
    - summary: "jwt callback persists sessionToken even when session create fails"
      location: "src/shared-kernel/auth/auth-options.ts"
    - summary: "reportAbuse accepts arbitrary target ids with no visibility assertion; broken entityType mapping"
      location: "src/features/community/services/community-service.ts"
    - summary: "Threads pagination drops role/outcome/difficulty filters"
      location: "src/app/threads/page.tsx"
    - summary: "getRejectedEntityIds deviates from ADR-012.3 single-bounded-query wording"
      location: "src/shared-kernel/contracts/visibility.ts"
CONFIDENCE: HIGH
NEXT_ACTION: "Executor implements T1..T13 in dependency order with DB vh-pg up; verify vitest/tsc/lint"
TASK_COUNT: 13
TASKS:
  - id: T1
    component: shared-kernel/contracts
    description: "Extract buildVisibleThreadWhere() and apply fail-closed filter to getCompanyProfileData/getJobProfileData tagged-thread queries (exclude rejected experience, rejected experience-company, rejected tags/legacy refs); single predicate shared with listInterviewThreads; add kernel fail-closed leak test."
  - id: T2
    component: features/community
    description: "Delete getOrCreateDiscussionThread and inFlightThreads; createComment requires existing APPROVED threadId; remove threadRef from createCommentSchema, CreateCommentInput, and posters; update dependent tests."
  - id: T3
    component: shared-kernel/contracts
    description: "Remove discussion/threads alias fields from Company/JobProfileResult, orphaned ThreadProfileDTOs, legacy findFirst+comments assembly; delete DiscussionThreadSection.tsx and its test import; update hardening-run005 assertions."
  - id: T4
    component: app/api/threads
    description: "Unify GET /api/threads with service boundary: invalid params return 200 empty-list result instead of ValidationError 400 per ADR-012.5."
  - id: T5
    component: app/wiring
    description: "Retire /questions page and home link; redirect /companies/[id]/interviews/new to /threads/new?companyId; repoint links in companies/[id]/page.tsx."
  - id: T6
    component: app/threads/new
    description: "Bound db.jobPosting.findMany with take 100 in threads/new/page.tsx (M4 query bounding)."
  - id: T7
    component: shared-kernel/validation
    description: "Cap interviewThreadQuerySchema.page with an upper bound to bound offset-based pagination."
  - id: T8
    component: shared-kernel/contracts
    description: "Add typed TaggedEntityDTO with entityName; replace the four (tag as any).entityName escapes in threads list/detail pages."
  - id: T9
    component: app/threads/[id]
    description: "Catch only EntityNotFoundError to notFound() in threads/[id] page; log and rethrow all other errors."
  - id: T10
    component: shared-kernel/auth
    description: "jwt callback propagates db.session.create failure instead of minting a doomed token with sessionToken; keep background pruner."
  - id: T11
    component: features/community
    description: "reportAbuse asserts target existence and visibility per targetType before queueing; single-source targetType-to-moderation entityType mapping."
  - id: T12
    component: app/threads/page
    description: "Preserve q/companyId/role/outcome/difficulty filters in pagination links on threads listing."
  - id: T13
    component: shared-kernel/contracts
    description: "Bound getRejectedEntityIds with take cap and fail-closed empty-page fallback at service boundary; amend ADR-012.3 and ratify ADR-012.5 in decisions.md."
DEPENDENCIES:
  - "T1 before T3; T3 before T2; T13 cap design incorporated in T1 predicate"
ACCEPTANCE_CRITERIA:
  - "Thread tagged to a REJECTED entity never appears on any APPROVED entity profile, detail, or listing (kernel fail-closed test present)."
  - "getOrCreateDiscussionThread, threadRef, DiscussionThreadSection, and discussion/threads DTO fields absent from src."
  - "GET /api/threads and threads page return identical empty-list 200 result on invalid params."
  - "No (tag as any) casts remain; tags typed with entityName via TaggedEntityDTO."
  - "jwt callback propagates session-create failure; reportAbuse 404s on unknown or rejected targets."
  - "All existing tests updated and passing plus new fail-closed test; tsc --noEmit and next lint clean."
VALIDATION_REQUIRED:
  - "docker ps --filter name=vh-pg (Postgres must be up)"
  - "npx vitest run"
  - "npx tsc --noEmit"
  - "npx next lint"
  - "grep for threadRef, getOrCreateDiscussionThread, DiscussionThreadSection, (tag as any), /companies/[id]/interviews/new, /questions home link"
EXIT_CRITERIA: "Postgres up; vitest suite green (>= 126 tests); tsc and lint clean; greps return no stale references."
```"""

    data, raw_yaml = MachineReportParser.extract_yaml(exact_planner_md, expected_role="PLANNER")

    # Must extract cleanly without returning empty dict or error
    assert data != {}
    assert raw_yaml != ""
    assert data["ROLE"] == "PLANNER"
    assert data["STATUS"] == "READY"
    assert data["HANDOFF"] == "EXECUTOR"
    assert data["TASK_COUNT"] == 13
    assert len(data["TASKS"]) == 13

    # Verification: neither opening ```yaml nor closing ``` code fence markers remain attached to raw_yaml
    assert not raw_yaml.startswith("```")
    assert not raw_yaml.endswith("```")
    assert not raw_yaml.startswith("first.```")

    # Verification: MachineReportValidator validates successfully
    report = MachineReportValidator.validate(data, expected_role="PLANNER", raw_yaml=raw_yaml)
    assert report.is_valid is True
    assert report.role == "PLANNER"
    assert report.status == "READY"
    assert report.handoff == "EXECUTOR"


def test_regression_unspaced_fenced_yaml_all_stages():
    """Verify fenced YAML without preceding newline parses successfully across all stages."""
    stages = [
        ("ARCHITECT", "APPROVED", "PLANNER", "Architecture verified.```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```"),
        ("PLANNER", "READY", "EXECUTOR", "Plan ready.```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```"),
        ("EXECUTOR", "SUCCESS", "REVIEWER", "Tests passed.```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```"),
        ("REVIEWER", "APPROVED", "NONE", "Diff clean.```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```"),
        ("CRITIC", "CRITIQUE_COMPLETE", "ARCHITECT", "Audit complete.```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: ARCHITECT\n```"),
    ]

    for role, status, handoff, report_text in stages:
        data, raw_yaml = MachineReportParser.extract_yaml(report_text, expected_role=role)
        assert data != {}, f"Failed to extract unspaced fenced YAML for {role}"
        assert data["ROLE"] == role
        assert data["STATUS"] == status
        assert data["HANDOFF"] == handoff
        assert not raw_yaml.startswith("```")
        assert not raw_yaml.endswith("```")

        val_report = MachineReportValidator.validate(data, expected_role=role, raw_yaml=raw_yaml)
        assert val_report.is_valid is True
        assert val_report.status == status
        assert val_report.handoff == handoff


def test_regression_universal_scalar_quoting_fixtures():
    """Verify machine reports following Universal Scalar Quoting parse and validate cleanly.

    Regression fixtures specified:
    - description: "Apply N-series fixes: z.infer inputs"
    - description: "foo: bar: baz"
    - reason: "Fix parser: preserve quoting"
    - summary: "Supports arrays: maps: and punctuation"
    """
    planner_output = """# Human Report
Planning complete.

## Machine Report
```yaml
ROLE: PLANNER
PROMPT_VERSION: 1.0
TASK_ID: "task-001"
START_TIME: "2026-09-20T10:05:00Z"
END_TIME: "2026-09-20T10:10:00Z"
DURATION: "300s"
STATUS: READY
EXIT_CODE: 0
HANDOFF: EXECUTOR
REASON: "Fix parser: preserve quoting"
INPUTS:
  - "architecture.md"
OUTPUTS:
  - "tasks.json"
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR:
    - summary: "Supports arrays: maps: and punctuation"
      location: "src/parser.py"
CONFIDENCE: HIGH
NEXT_ACTION: "Proceed to execution"
TASK_COUNT: 2
TASKS:
  - id: "T1"
    component: "shared-kernel/fixes"
    description: "Apply N-series fixes: z.infer inputs"
  - id: "T2"
    component: "core/routing"
    description: "foo: bar: baz"
DEPENDENCIES: []
ACCEPTANCE_CRITERIA:
  - "All unit tests pass"
VALIDATION_REQUIRED:
  - "pytest tests/"
```"""

    data, raw_yaml = MachineReportParser.extract_yaml(planner_output, expected_role="PLANNER")
    assert data != {}, "Expected successful YAML extraction for properly quoted scalars"
    assert data["ROLE"] == "PLANNER"
    assert data["STATUS"] == "READY"
    assert data["HANDOFF"] == "EXECUTOR"
    assert data["REASON"] == "Fix parser: preserve quoting"
    assert data["TASKS"][0]["description"] == "Apply N-series fixes: z.infer inputs"
    assert data["TASKS"][1]["description"] == "foo: bar: baz"
    assert data["ISSUES"]["MINOR"][0]["summary"] == "Supports arrays: maps: and punctuation"

    report = MachineReportValidator.validate(data, expected_role="PLANNER", raw_yaml=raw_yaml)
    assert report.is_valid is True
    assert report.status == "READY"
    assert report.handoff == "EXECUTOR"


def test_regression_unquoted_scalars_with_colons_fail_cleanly_without_silent_repair():
    """Verify intentionally malformed YAML with unquoted colons in scalars fails validation.

    The parser MUST remain strict standards-compliant YAML and NOT attempt brittle
    field-specific regex auto-repair. When scalars containing ': ' are emitted without
    quotes, yaml.safe_load() rejects the syntax and validation fails (is_valid=False).
    """
    malformed_planner_output = """# Human Report
Planning complete.

## Machine Report
```yaml
ROLE: PLANNER
PROMPT_VERSION: 1.0
STATUS: READY
HANDOFF: EXECUTOR
REASON: Fix parser: preserve quoting
TASK_COUNT: 2
TASKS:
  - id: T1
    component: shared-kernel/fixes
    description: Apply N-series fixes: z.infer inputs
  - id: T2
    component: core/routing
    description: foo: bar: baz
```"""

    data, raw_yaml = MachineReportParser.extract_yaml(malformed_planner_output, expected_role="PLANNER")
    # Strict parser behavior: yaml.safe_load fails with ScannerError ("mapping values are not allowed here")
    # and no regex auto-repair alters the document to silently succeed.
    assert data == {}, "Parser must not silently auto-repair unquoted scalars with colons"
    assert MachineReportParser.last_error is not None
    assert "mapping values are not allowed here" in str(MachineReportParser.last_error)

    report = MachineReportValidator.validate(data, expected_role="PLANNER", raw_yaml=raw_yaml)
    assert report.is_valid is False
    assert len(report.validation_errors) > 0


def test_regression_doubled_quotes_inside_yaml_scalars_fail_without_silent_repair():
    """Verify malformed YAML with CSV/SQL-style doubled quotes inside strings fails validation."""
    malformed_yaml = """```yaml
ROLE: PLANNER
STATUS: READY
HANDOFF: EXECUTOR
REASON: "Fix parser"
TASKS:
  - id: "T1"
    description: "Acceptance: ""seal/open round-trip"" test passes"
```"""
    data, raw_yaml = MachineReportParser.extract_yaml(malformed_yaml, expected_role="PLANNER")
    assert data == {}, "Doubled quotes are invalid YAML and must not be silently repaired"
    assert MachineReportParser.last_error is not None
    report = MachineReportValidator.validate(data, expected_role="PLANNER", raw_yaml=raw_yaml)
    assert report.is_valid is False


# ===========================================================================
# ADR-014 Protocol Invariant Tests
# ===========================================================================

def test_adr014_blocked_with_non_none_handoff_fails_validation():
    """Verify (STATUS: BLOCKED, HANDOFF: PLANNER) — the run-012 failure mode — is rejected."""
    raw_data = {
        "ROLE": "ARCHITECT",
        "STATUS": "BLOCKED",
        "HANDOFF": "PLANNER",
        "REASON": "Repository pre-existing issues require remediation",
    }
    report = MachineReportValidator.validate(raw_data, expected_role="ARCHITECT")
    assert report.is_valid is False
    assert any("STATUS 'BLOCKED' requires HANDOFF 'NONE'" in err for err in report.validation_errors)


def test_adr014_run012_remediated_scenario():
    """Verify run-012 scenario where Architect finds repo debt emits APPROVED + PLANNER."""
    data = {
        "ROLE": "ARCHITECT",
        "STATUS": "APPROVED",
        "HANDOFF": "PLANNER",
        "EXIT_CODE": 0,
        "REASON": "Architecture specification complete; includes remediation of CI and auth debt.",
        "ISSUES": {
            "CRITICAL": [
                "npm run typecheck fails (5 TS2741) — CI gate red.",
                "Unconditional client demo sign-in enables one-click takeover.",
                "No per-IP sign-in rate limit enables bcrypt CPU exhaustion.",
            ],
            "MAJOR": [
                "JWT fail-open: tokens missing sessionToken bypass DB revocation.",
            ],
        },
        "ARCHITECTURE": {
            "MODULES": ["shared-kernel/auth", "features/trust-safety"],
            "NEW_INTERFACES": ["VisibilityHelper", "IdempotentModerationResolver"],
        },
    }
    report = MachineReportValidator.validate(data, expected_role="ARCHITECT")
    assert report.is_valid is True
    assert report.status == "APPROVED"
    assert report.handoff == "PLANNER"
def test_canonical_run012_regression_fixture_blocked_rejected_approved_accepted():
    """Permanent canonical regression for run-012 incident:
    
    Old behavior:
      STATUS: BLOCKED + HANDOFF: PLANNER (repo has debt, but routing to planner)
      -> Rejected by MachineReportValidator with invariant violation.
      
    New behavior (ADR-014):
      STATUS: APPROVED + HANDOFF: PLANNER (architecture is complete; debt is under ISSUES)
      -> Accepted by MachineReportValidator as valid.
    """
    import json
    fixture_path = Path(__file__).parent / "fixtures" / "run_012_incident_report.json"
    with open(fixture_path) as f:
        incident_data = json.load(f)

    # 1. Old incident report MUST fail validation
    old_report = MachineReportValidator.validate(incident_data, expected_role="ARCHITECT")
    assert old_report.is_valid is False
    assert any("STATUS 'BLOCKED' requires HANDOFF 'NONE'" in err for err in old_report.validation_errors)

    # 2. Same architectural situation under ADR-014 (STATUS: APPROVED) MUST pass validation
    remediated_data = dict(incident_data)
    remediated_data["STATUS"] = "APPROVED"
    remediated_data["REASON"] = "Architecture complete and verified; findings recorded for planning."
    new_report = MachineReportValidator.validate(remediated_data, expected_role="ARCHITECT")
    assert new_report.is_valid is True
    assert new_report.status == "APPROVED"
    assert new_report.handoff == "PLANNER"
    assert len(new_report.issues["CRITICAL"]) == 3
    assert len(new_report.data["ARCHITECTURE"]["MODULES"]) == 5


def test_adr014_blocked_with_none_handoff_passes_validation():
    """Verify STATUS: BLOCKED with HANDOFF: NONE is valid."""
    raw_data = {
        "ROLE": "ARCHITECT",
        "STATUS": "BLOCKED",
        "HANDOFF": "NONE",
        "REASON": "External database unavailable",
    }
    report = MachineReportValidator.validate(raw_data, expected_role="ARCHITECT")
    assert report.is_valid is True
    assert report.status == "BLOCKED"
    assert report.handoff == "NONE"


def test_adr014_rejected_with_non_none_handoff_fails_validation():
    """Verify STATUS: REJECTED with non-NONE handoff is rejected."""
    raw_data = {
        "ROLE": "ARCHITECT",
        "STATUS": "REJECTED",
        "HANDOFF": "PLANNER",
        "REASON": "Task violates repository ADRs",
    }
    report = MachineReportValidator.validate(raw_data, expected_role="ARCHITECT")
    assert report.is_valid is False
    assert any("STATUS 'REJECTED' requires HANDOFF 'NONE'" in err for err in report.validation_errors)


def test_adr014_rejected_with_none_handoff_passes_validation():
    """Verify STATUS: REJECTED with HANDOFF: NONE is valid."""
    raw_data = {
        "ROLE": "ARCHITECT",
        "STATUS": "REJECTED",
        "HANDOFF": "NONE",
        "REASON": "Task violates repository ADRs",
    }
    report = MachineReportValidator.validate(raw_data, expected_role="ARCHITECT")
    assert report.is_valid is True
    assert report.status == "REJECTED"
    assert report.handoff == "NONE"


def test_adr014_intermediate_success_requires_active_routing():
    """Verify non-terminal planning stages require active downstream routing when reporting success."""
    # Architect APPROVED with NONE must fail
    arch_none = {
        "ROLE": "ARCHITECT",
        "STATUS": "APPROVED",
        "HANDOFF": "NONE",
    }
    rep_arch_none = MachineReportValidator.validate(arch_none, expected_role="ARCHITECT")
    assert rep_arch_none.is_valid is False
    assert any("Architect success status 'APPROVED' requires active handoff" in err for err in rep_arch_none.validation_errors)

    # Architect APPROVED with PLANNER must pass
    arch_planner = {
        "ROLE": "ARCHITECT",
        "STATUS": "APPROVED",
        "HANDOFF": "PLANNER",
    }
    rep_arch_planner = MachineReportValidator.validate(arch_planner, expected_role="ARCHITECT")
    assert rep_arch_planner.is_valid is True

    # Planner READY with NONE must fail
    plan_none = {
        "ROLE": "PLANNER",
        "STATUS": "READY",
        "HANDOFF": "NONE",
    }
    rep_plan_none = MachineReportValidator.validate(plan_none, expected_role="PLANNER")
    assert rep_plan_none.is_valid is False
    assert any("Planner success status 'READY' requires active handoff" in err for err in rep_plan_none.validation_errors)

    # Planner READY with EXECUTOR must pass
    plan_exec = {
        "ROLE": "PLANNER",
        "STATUS": "READY",
        "HANDOFF": "EXECUTOR",
    }
    rep_plan_exec = MachineReportValidator.validate(plan_exec, expected_role="PLANNER")
    assert rep_plan_exec.is_valid is True









