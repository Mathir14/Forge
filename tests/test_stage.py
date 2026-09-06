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
