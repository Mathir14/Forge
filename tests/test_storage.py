from pathlib import Path
from forge.storage.run_manager import RunManager


def test_create_sequential_runs(tmp_path):
    mgr = RunManager(tmp_path)
    run1 = mgr.create_run(task="Task 1")
    assert run1.run_id == "run-001"
    assert (tmp_path / ".forge" / "runs" / "run-001" / "metadata.json").exists()

    run2 = mgr.create_run(task="Task 2")
    assert run2.run_id == "run-002"

    runs = mgr.list_runs()
    assert len(runs) == 2
    assert mgr.latest().run_id == "run-002"


def test_save_and_load_stage_artifacts(tmp_path):
    mgr = RunManager(tmp_path)
    run = mgr.create_run(task="Test Task")

    md_file, json_file = mgr.save_stage_artifacts(
        run=run,
        sequence_number=1,
        role_name="architect",
        markdown_content="# Architecture Report",
        json_data={"ROLE": "ARCHITECT", "STATUS": "APPROVED"},
        prompt_hash="abc123",
        adapter_name="opencode",
    )

    assert md_file.name == "01_architect.md"
    assert json_file.name == "01_architect.json"
    assert md_file.exists()
    assert json_file.exists()

    data = mgr.load_stage_json(run, "architect")
    assert data["STATUS"] == "APPROVED"

    md = mgr.load_stage_markdown(run, "architect")
    assert md == "# Architecture Report"
