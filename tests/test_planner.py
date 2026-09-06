from pathlib import Path
from typing import Optional
from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.core.context import Context
from forge.core.config import Config
from forge.core.git import GitService
from forge.core.role import Role
from forge.storage.run_manager import RunManager
from forge.stages.stage import Stage


class MockPlannerAdapter(BaseAdapter):
    def __init__(self, response_text: str):
        super().__init__(name="mock_planner")
        self.response_text = response_text

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        # Verify that prompt includes the previous architect output
        assert "JWT auth system" in prompt or "PREVIOUS STAGE ARTIFACTS" in prompt
        return AdapterResponse(
            stdout=self.response_text,
            stderr="",
            exit_code=0,
            duration_seconds=0.4,
            raw_output=self.response_text,
        )


def test_planner_stage_lifecycle(tmp_path):
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Implement JWT Authentication")

    # Simulate prior architect step
    run_mgr.save_stage_artifacts(
        run=run,
        sequence_number=1,
        role_name="architect",
        markdown_content="# Architecture\nDesigned JWT auth system with token expiration.",
        json_data={"ROLE": "ARCHITECT", "STATUS": "APPROVED", "HANDOFF": "PLANNER"},
        prompt_hash="arch_hash_123",
        adapter_name="opencode",
    )

    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    role = Role.load("planner", project_root=tmp_path)

    sample_planner_output = """
# Planner Report
Tasks defined for JWT implementation.

```yaml
ROLE: PLANNER
STATUS: READY
HANDOFF: EXECUTOR
TASK_COUNT: 3
TASKS:
  - Create token generator
  - Add auth middleware
  - Add unit tests
ACCEPTANCE_CRITERIA:
  - 200 OK on valid token
  - 401 Unauthorized on expired token
VALIDATION_REQUIRED:
  - pytest tests/test_auth.py
```
"""
    adapter = MockPlannerAdapter(sample_planner_output)
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    result = stage.run(context)
    assert result.status == "READY"
    assert result.handoff == "EXECUTOR"
    assert result.success

    # Check that 02_planner.md and 02_planner.json were created
    md_file = tmp_path / ".forge" / "runs" / "run-001" / "02_planner.md"
    json_file = tmp_path / ".forge" / "runs" / "run-001" / "02_planner.json"
    assert md_file.exists()
    assert json_file.exists()

    data = run_mgr.load_stage_json(run, "planner")
    assert data["machine_report"]["status"] == "READY"
    assert data["machine_report"]["handoff"] == "EXECUTOR"
