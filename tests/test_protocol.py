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
