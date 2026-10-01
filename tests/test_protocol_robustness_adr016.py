"""Regression tests for ADR-016: Deterministic Protocol Parsing and Section Anchoring.

Guards against:
- TypeScript interfaces, Python code, JSON schemas, shell scripts in Human Report.
- stderr pollution from PyYAML exceptions on non-protocol code blocks.
- Candidate hijacking by example YAML/JSON in the Human Report.
- Language tag discipline (rejecting non-YAML blocks).
- Section-first anchoring (ignoring blocks before ## Machine Report).
"""

import sys
import pytest
from forge.protocol.parser import MachineReportParser


def test_parser_ignores_typescript_code_fence_and_avoids_stderr_pollution(capsys):
    """Production Incident A reproduction:
    Architect Human Report contains TypeScript interface with curly braces and colons.
    Parser must extract the valid YAML Machine Report with ZERO output written to stderr.
    """
    raw_output = """# Human Report — Architecture Spec

## Interfaces
```typescript
interface AuthService {
  resolveAuthSecret(): string;
  validateToken(token: string): boolean;
}
```

## Machine Report
```yaml
ROLE: ARCHITECT
STATUS: APPROVED
HANDOFF: PLANNER
REASON: "Architecture complete"
```
"""
    capsys.readouterr()  # Clear existing buffer
    data, raw_yaml = MachineReportParser.extract_yaml(raw_output, expected_role="ARCHITECT")
    captured = capsys.readouterr()

    # 1. Successful extraction
    assert data.get("ROLE") == "ARCHITECT"
    assert data.get("STATUS") == "APPROVED"
    assert data.get("HANDOFF") == "PLANNER"

    # 2. Strict stderr discipline: No YAML parse errors or diagnostic prints
    assert "YAML parse error" not in captured.err
    assert "MachineReportParser diagnostic" not in captured.err
    assert "mapping values are not allowed here" not in captured.err
    assert captured.err == ""


def test_parser_ignores_multiple_diverse_code_fences_in_human_report(capsys):
    """Verify that Python, JSON, Shell, and diff snippets in Human Report are ignored
    without triggering parse diagnostics or candidate pollution.
    """
    raw_output = """# Human Report

### Implementation Snippet
```python
def configure_auth():
    status = "active"
    role = "admin"
    return {"status": status, "role": role}
```

### Config Example
```json
{
  "role": "SYSTEM",
  "status": "INITIALIZED",
  "handoff": "NONE"
}
```

### Deployment Script
```bash
export ROLE=OPERATOR
export STATUS=DEPLOYED
./deploy.sh --status check
```

## Machine Report
```yaml
ROLE: PLANNER
STATUS: READY
HANDOFF: EXECUTOR
REASON: "Plan complete and sequenced"
```
"""
    capsys.readouterr()
    data, raw_yaml = MachineReportParser.extract_yaml(raw_output, expected_role="PLANNER")
    captured = capsys.readouterr()

    assert data.get("ROLE") == "PLANNER"
    assert data.get("STATUS") == "READY"
    assert data.get("HANDOFF") == "EXECUTOR"
    assert captured.err == ""
    assert MachineReportParser.last_error is None


def test_parser_does_not_hijack_candidate_from_human_report_json():
    """Ensure a JSON block in the Human Report declaring ROLE/STATUS is NOT selected
    over the actual Machine Report.
    """
    raw_output = """# Human Report

Here is the user payload schema to be processed:
```json
{
  "role": "PLANNER",
  "status": "REJECTED",
  "handoff": "NONE",
  "notes": "Fake mock report in human report"
}
```

## Machine Report
```yaml
ROLE: PLANNER
STATUS: READY
HANDOFF: EXECUTOR
REASON: "True report"
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(raw_output, expected_role="PLANNER")
    assert data.get("STATUS") == "READY"
    assert data.get("HANDOFF") == "EXECUTOR"
    assert data.get("REASON") == "True report"


def test_parser_ignores_yaml_examples_in_human_report():
    """Ensure YAML configuration examples in Human Report are not captured as candidates."""
    raw_output = """# Human Report

Example Kubernetes Pod configuration:
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: test-pod
status:
  phase: Running
  role: worker
```

## Machine Report
```yaml
ROLE: EXECUTOR
STATUS: SUCCESS
HANDOFF: TESTER
REASON: "Implementation verified"
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(raw_output, expected_role="EXECUTOR")
    assert data.get("ROLE") == "EXECUTOR"
    assert data.get("STATUS") == "SUCCESS"
    assert data.get("HANDOFF") == "TESTER"


def test_parser_strict_language_fence_requirement():
    """ADR-016: Non-YAML code fences must not be evaluated as machine reports.
    If a block is fenced as ```json, even if it contains valid protocol keys,
    it must NOT be parsed as the Machine Report.
    """
    raw_output = """## Machine Report
```json
{
  "ROLE": "REVIEWER",
  "STATUS": "APPROVED",
  "HANDOFF": "NONE"
}
```
"""
    # Because language tag is json, strict parser must ignore it
    data, raw_yaml = MachineReportParser.extract_yaml(raw_output, expected_role="REVIEWER")
    assert data == {}
    assert raw_yaml == ""


def test_parser_anchored_to_machine_report_heading():
    """ADR-016: Extraction must be anchored to '## Machine Report'.
    Any YAML block before the heading must be ignored.
    """
    raw_output = """# Human Report

Here was the previous stage report:
```yaml
ROLE: ARCHITECT
STATUS: BLOCKED
HANDOFF: NONE
REASON: "Old blocked report from previous run"
```

## Machine Report
```yaml
ROLE: ARCHITECT
STATUS: APPROVED
HANDOFF: PLANNER
REASON: "Current approved report"
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(raw_output, expected_role="ARCHITECT")
    assert data.get("STATUS") == "APPROVED"
    assert data.get("HANDOFF") == "PLANNER"
    assert data.get("REASON") == "Current approved report"


def test_parser_legacy_fallback_when_machine_report_heading_missing():
    """ADR-016 Backwards Compatibility:
    If an agent output completely omits '## Machine Report' heading,
    the parser falls back to scanning ```yaml blocks starting with ROLE:.
    """
    raw_output = """Architectural design completed.

```yaml
ROLE: ARCHITECT
STATUS: APPROVED
HANDOFF: PLANNER
REASON: "Unanchored fallback report"
```
"""
    data, raw_yaml = MachineReportParser.extract_yaml(raw_output, expected_role="ARCHITECT")
    assert data.get("ROLE") == "ARCHITECT"
    assert data.get("STATUS") == "APPROVED"
    assert data.get("HANDOFF") == "PLANNER"
