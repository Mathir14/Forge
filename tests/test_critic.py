from pathlib import Path
from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.core.context import Context
from forge.core.config import Config
from forge.core.git import GitService
from forge.core.role import Role
from forge.storage.run_manager import RunManager
from forge.stages.stage import Stage


class MockCriticAdapter(BaseAdapter):
    def __init__(self, response_text: str):
        super().__init__(name="mock_critic")
        self.response_text = response_text

    def is_available(self) -> bool:
        return True

    def execute(self, prompt: str, cwd: Path = None) -> AdapterResponse:
        assert "CODEBASE CRITIC" in prompt or "CRITIC" in prompt
        return AdapterResponse(
            stdout=self.response_text,
            stderr="",
            exit_code=0,
            duration_seconds=0.3,
            raw_output=self.response_text,
        )


def test_critic_stage_lifecycle(tmp_path):
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Audit error handling in adapters")
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    role = Role.load("critic", project_root=tmp_path)
    assert role.sequence_number == 0

    sample_critic_output = """
# Codebase Health Audit
Score: 7/10

### Critical Issues
- None

### Major Issues
- Missing timeout on subprocess calls in base adapter.

```yaml
ROLE: CRITIC
STATUS: CRITIQUE_COMPLETE
HANDOFF: ARCHITECT
HEALTH_SCORE: 7
ISSUES:
  MAJOR:
    - Missing subprocess timeout handling
RECOMMENDED_ACTIONS:
  - Add timeout parameter to BaseAdapter.execute
```
"""
    adapter = MockCriticAdapter(sample_critic_output)
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    result = stage.run(context)
    assert result.status == "CRITIQUE_COMPLETE"
    assert result.handoff == "ARCHITECT"
    assert result.success

    # Verify 00_critic.md and 00_critic.json created
    md_file = tmp_path / ".forge" / "runs" / "run-001" / "00_critic.md"
    json_file = tmp_path / ".forge" / "runs" / "run-001" / "00_critic.json"
    assert md_file.exists()
    assert json_file.exists()

    data = run_mgr.load_stage_json(run, "critic")
    assert data["machine_report"]["status"] == "CRITIQUE_COMPLETE"
    assert "Missing subprocess timeout handling" in data["machine_report"]["issues"]["MAJOR"]
