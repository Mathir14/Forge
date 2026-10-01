from pathlib import Path
from typing import Optional
from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.core.context import Context
from forge.core.config import Config
from forge.core.git import GitService
from forge.core.role import Role
from forge.storage.run_manager import RunManager
from forge.stages.stage import Stage


class MockStageAdapter(BaseAdapter):
    def __init__(self, name: str, output_yaml_block: str):
        super().__init__(name=name)
        self.output_yaml_block = output_yaml_block

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        full_text = f"# Stage Report for {self.name}\n\n```yaml\n{self.output_yaml_block}\n```"
        return AdapterResponse(
            stdout=full_text,
            stderr="",
            exit_code=0,
            duration_seconds=0.2,
            raw_output=full_text,
        )


from tests.conftest import make_automated_config


def test_full_four_stage_pipeline(tmp_path):
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Add OAuth2 Login")
    context = Context(
        run=run,
        project_root=tmp_path,
        config=make_automated_config(),
        git=GitService(tmp_path),
    )

    # 1. Architect
    arch_role = Role.load("architect", project_root=tmp_path)
    arch_stage = Stage(
        role=arch_role,
        adapter=MockStageAdapter("opencode", "ROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER"),
        run_manager=run_mgr,
    )
    res1 = arch_stage.run(context)
    assert res1.status == "APPROVED"
    assert (tmp_path / ".forge" / "runs" / "run-001" / "01_architect.json").exists()

    # 2. Planner
    plan_role = Role.load("planner", project_root=tmp_path)
    plan_stage = Stage(
        role=plan_role,
        adapter=MockStageAdapter("opencode", "ROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR"),
        run_manager=run_mgr,
    )
    res2 = plan_stage.run(context)
    assert res2.status == "READY"
    assert (tmp_path / ".forge" / "runs" / "run-001" / "02_planner.json").exists()

    # 3. Executor
    exec_role = Role.load("executor", project_root=tmp_path)
    exec_stage = Stage(
        role=exec_role,
        adapter=MockStageAdapter("antigravity", "ROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER"),
        run_manager=run_mgr,
    )
    res3 = exec_stage.run(context)
    assert res3.status == "SUCCESS"
    assert (tmp_path / ".forge" / "runs" / "run-001" / "03_executor.json").exists()

    # 4. Tester
    tester_role = Role.load("tester", project_root=tmp_path)
    tester_stage = Stage(
        role=tester_role,
        adapter=MockStageAdapter("opencode", "ROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER"),
        run_manager=run_mgr,
    )
    res4 = tester_stage.run(context)
    assert res4.status == "PASS"
    assert (tmp_path / ".forge" / "runs" / "run-001" / "04_tester.json").exists()

    # 5. Reviewer
    rev_role = Role.load("reviewer", project_root=tmp_path)
    rev_stage = Stage(
        role=rev_role,
        adapter=MockStageAdapter("opencode", "ROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE"),
        run_manager=run_mgr,
    )
    res5 = rev_stage.run(context)
    assert res5.status == "APPROVED"
    assert (tmp_path / ".forge" / "runs" / "run-001" / "05_reviewer.json").exists()

    # Verify all stage artifacts exist in sequence
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    assert (run_dir / "01_architect.md").exists()
    assert (run_dir / "01_architect.json").exists()
    assert (run_dir / "02_planner.md").exists()
    assert (run_dir / "02_planner.json").exists()
    assert (run_dir / "03_executor.md").exists()
    assert (run_dir / "03_executor.json").exists()
    assert (run_dir / "04_tester.md").exists()
    assert (run_dir / "04_tester.json").exists()
    assert (run_dir / "05_reviewer.md").exists()
    assert (run_dir / "05_reviewer.json").exists()
    assert (run_dir / "metadata.json").exists()


def test_pipeline_resuming_from_critic_report(tmp_path):
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Critique: Code smells in adapters")

    # Save critic output
    run_mgr.save_stage_artifacts(
        run=run,
        sequence_number=0,
        role_name="critic",
        markdown_content="# Critic Report\nFound subprocess deadlock risk in adapter execution.",
        json_data={"ROLE": "CRITIC", "STATUS": "CRITIQUE_COMPLETE", "HANDOFF": "ARCHITECT"},
        prompt_hash="critic_hash_999",
        adapter_name="opencode",
    )

    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    # Architect stage consuming critic report
    arch_role = Role.load("architect", project_root=tmp_path)
    arch_stage = Stage(
        role=arch_role,
        adapter=MockStageAdapter("opencode", "ROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER"),
        run_manager=run_mgr,
    )
    res = arch_stage.run(context)
    assert res.status == "APPROVED"

    # Verify both 00_critic and 01_architect exist in the same run folder
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    assert (run_dir / "00_critic.md").exists()
    assert (run_dir / "00_critic.json").exists()
    assert (run_dir / "01_architect.md").exists()
    assert (run_dir / "01_architect.json").exists()
