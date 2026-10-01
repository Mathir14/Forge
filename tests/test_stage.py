from pathlib import Path
from typing import Optional
from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.core.context import Context
from forge.core.config import Config
from forge.core.git import GitService
from forge.core.role import Role
from forge.storage.run_manager import RunManager
from forge.stages.stage import Stage


class MockAdapter(BaseAdapter):
    def __init__(self, response_text: str):
        super().__init__(name="mock")
        self.response_text = response_text

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        return AdapterResponse(
            stdout=self.response_text,
            stderr="",
            exit_code=0,
            duration_seconds=0.5,
            raw_output=self.response_text,
        )


def test_stage_lifecycle(tmp_path):
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Design database")
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    role = Role(
        name="architect",
        sequence_number=1,
        template_content="You are architect.",
        protocol_content="Emit machine report.",
    )

    sample_output = """
# Architecture Design
Proposed PostgreSQL schema.

```yaml
ROLE: ARCHITECT
STATUS: APPROVED
HANDOFF: PLANNER
```
"""
    adapter = MockAdapter(sample_output)
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    result = stage.run(context)
    assert result.status == "APPROVED"
    assert result.handoff == "PLANNER"
    assert result.success

    # Verify artifacts were persisted
    md_file = tmp_path / ".forge" / "runs" / "run-001" / "01_architect.md"
    json_file = tmp_path / ".forge" / "runs" / "run-001" / "01_architect.json"
    assert md_file.exists()
    assert json_file.exists()


def test_stage_is_ndjson_detection():
    assert Stage._is_ndjson('{"type": "step_start"}\n{"type": "text"}') is True
    assert Stage._is_ndjson('   {"event": "init"}') is True
    assert Stage._is_ndjson("# Markdown heading\n```yaml\nROLE: PLANNER\n```") is False
    assert Stage._is_ndjson("") is False
    assert Stage._is_ndjson("plain text") is False
    assert Stage._is_ndjson("{invalid json") is False


def test_stage_skips_raw_ndjson_fallback_when_primary_parse_fails(tmp_path):
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Test planner")
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    role = Role(
        name="planner",
        sequence_number=2,
        template_content="You are planner.",
        protocol_content="Emit machine report.",
    )

    # Malformed YAML in stdout (unquoted colon)
    failing_stdout = """
# Plan
```yaml
ROLE: PLANNER
STATUS: READY
HANDOFF: EXECUTOR
TASKS:
  - description: Fix bug: bad colon
```
"""
    raw_ndjson = '{"type": "step_start"}\n{"type": "text", "part": {"text": "something"}}\n{"type": "step_finish"}'

    class NdjsonMockAdapter(BaseAdapter):
        def __init__(self):
            super().__init__(name="mock_ndjson")

        def is_available(self) -> bool:
            return True

        def execute(self, prompt: str, cwd=None, timeout=None):
            return AdapterResponse(
                stdout=failing_stdout,
                stderr="",
                exit_code=0,
                duration_seconds=0.5,
                raw_output=raw_ndjson,
            )

    stage = Stage(role=role, adapter=NdjsonMockAdapter(), run_manager=run_mgr)

    from unittest.mock import patch
    from forge.protocol.parser import MachineReportParser

    calls = []
    original_extract_yaml = MachineReportParser.extract_yaml

    def spy_extract(text, expected_role=None):
        calls.append(text)
        return original_extract_yaml(text, expected_role=expected_role)

    with patch.object(MachineReportParser, "extract_yaml", side_effect=spy_extract):
        result = stage.run(context)

    # extract_yaml must have been called ONLY on failing_stdout, NEVER on raw_ndjson
    assert len(calls) == 1
    assert calls[0] == failing_stdout
    assert raw_ndjson not in calls
    assert result.status == "UNKNOWN"

