"""Regression and unit test suite for Forge Terminal Dashboard (Phase 1: Read-Only Post-Mortem Inspector).

Guards against:
- Corrupted or missing metadata.json
- Partially written stage artifacts or missing markdown/json files
- Navigation bounds errors (timeline stage selection, scrolling, paging)
- Machine report extraction and property table formatting
- CLI invocation and non-interactive / headless fallback
"""

import json
from pathlib import Path
from click.testing import CliRunner
import pytest

from forge.cli import main
from forge.dashboard.model import RunModel, StageModel, MachineReportModel
from forge.dashboard.state import DashboardState
from forge.dashboard.app import DashboardApp
from forge.dashboard.components.header import render_header
from forge.dashboard.components.timeline import render_timeline
from forge.dashboard.components.artifact_view import render_artifact_view
from forge.dashboard.components.footer import render_footer


@pytest.fixture
def mock_run_dir(tmp_path: Path) -> Path:
    """Create a populated mock run directory with metadata and multiple stages."""
    run_dir = tmp_path / "run-101"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. metadata.json
    meta = {
        "run_id": "run-101",
        "task": "Refactor user authentication service to support OAuth2 tokens.",
        "created_at": "2026-09-27T10:00:00Z",
        "status": "APPROVED",
        "adapters_used": {
            "architect": "opencode",
            "planner": "opencode",
            "executor": "antigravity",
        },
    }
    with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)

    # 2. Stage 1: Architect (Approved)
    s1_json = {
        "role": "architect",
        "sequence_number": 1,
        "status": "APPROVED",
        "handoff": "PLANNER",
        "duration_seconds": 45.2,
        "exit_code": 0,
        "machine_report": {
            "role": "ARCHITECT",
            "status": "APPROVED",
            "handoff": "PLANNER",
            "reason": "OAuth2 architectural contracts defined.",
            "confidence": "HIGH",
            "issues": {"MINOR": ["Token expiry config is not yet validated."]},
            "next_action": "Planner: sequence implementation steps.",
        },
    }
    with open(run_dir / "01_architect.json", "w", encoding="utf-8") as f:
        json.dump(s1_json, f)

    s1_md = """# Human Report — OAuth2 Spec

We have selected OAuth2 with JWT bearer tokens.

## Architecture
- Client initiates authorization flow
- Middleware validates bearer token

## Machine Report
```yaml
ROLE: ARCHITECT
STATUS: APPROVED
HANDOFF: PLANNER
CONFIDENCE: HIGH
REASON: "OAuth2 architectural contracts defined."
NEXT_ACTION: "Planner: sequence implementation steps."
ISSUES:
  MINOR:
    - "Token expiry config is not yet validated."
```
"""
    with open(run_dir / "01_architect.md", "w", encoding="utf-8") as f:
        f.write(s1_md)

    # 3. Stage 2: Planner (with an attempt)
    s2_json = {
        "role": "planner",
        "sequence_number": 2,
        "status": "READY",
        "handoff": "EXECUTOR",
        "duration_seconds": 30.0,
        "exit_code": 0,
    }
    with open(run_dir / "02_planner.json", "w", encoding="utf-8") as f:
        json.dump(s2_json, f)

    s2_md = """# Human Report — Implementation Plan

Task 1: Add token verification helper
Task 2: Update middleware

## Machine Report
```yaml
ROLE: PLANNER
STATUS: READY
HANDOFF: EXECUTOR
```
"""
    with open(run_dir / "02_planner.md", "w", encoding="utf-8") as f:
        f.write(s2_md)

    return run_dir


def test_run_model_loading_populated_run(mock_run_dir: Path):
    """Verify RunModel parses metadata, stages, attempts, and reports cleanly."""
    model = RunModel.from_dir(mock_run_dir)

    assert model.run_id == "run-101"
    assert "OAuth2 tokens" in model.task
    assert model.status == "APPROVED"
    assert len(model.stages) == 2

    # Check stage 1
    s1 = model.stages[0]
    assert s1.stage_name == "01_architect"
    assert s1.role_name == "architect"
    assert s1.status == "APPROVED"
    assert s1.duration_seconds == 45.2
    assert s1.exit_code == 0
    assert "We have selected OAuth2" in s1.human_report
    assert "## Machine Report" not in s1.human_report  # Must be sliced out
    assert s1.machine_report is not None
    assert s1.machine_report.role == "ARCHITECT"
    assert s1.machine_report.status == "APPROVED"
    assert s1.machine_report.handoff == "PLANNER"
    assert s1.machine_report.confidence == "HIGH"
    assert "Token expiry config" in s1.machine_report.issues.get("MINOR", [])[0]

    # Check stage 2
    s2 = model.stages[1]
    assert s2.stage_name == "02_planner"
    assert s2.status == "READY"
    assert s2.machine_report is not None
    assert s2.machine_report.status == "READY"


def test_run_model_graceful_handling_missing_metadata(tmp_path: Path):
    """Verify RunModel gracefully handles directories lacking metadata.json."""
    empty_run = tmp_path / "run-empty"
    empty_run.mkdir()

    model = RunModel.from_dir(empty_run)
    assert model.run_id == "run-empty"
    assert model.status == "PENDING"
    # Should seed default canonical stages
    assert len(model.stages) == 6
    assert model.stages[0].stage_name == "01_architect"
    assert model.stages[0].status == "PENDING"


def test_run_model_corrupted_metadata_and_json(tmp_path: Path):
    """Verify RunModel does not crash on corrupt json files."""
    corrupt_run = tmp_path / "run-corrupt"
    corrupt_run.mkdir()

    with open(corrupt_run / "metadata.json", "w", encoding="utf-8") as f:
        f.write("{invalid json syntax,,")

    with open(corrupt_run / "01_architect.json", "w", encoding="utf-8") as f:
        f.write("not json at all")

    with open(corrupt_run / "01_architect.md", "w", encoding="utf-8") as f:
        f.write("# Partial Architect Report\n\n```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\n```")

    model = RunModel.from_dir(corrupt_run)
    assert model.run_id == "run-corrupt"
    assert model.status == "CORRUPT_METADATA"
    assert len(model.stages) == 1
    assert model.stages[0].stage_name == "01_architect"
    assert model.stages[0].status in ("CORRUPT_STAGE_JSON", "APPROVED")
    assert model.stages[0].machine_report is not None
    assert model.stages[0].machine_report.status == "APPROVED"


def test_dashboard_state_navigation_and_scrolling(mock_run_dir: Path):
    """Verify DashboardState stage selection, tab toggling, and scroll bounds."""
    model = RunModel.from_dir(mock_run_dir)
    state = DashboardState(run=model)

    assert state.selected_stage_index == 0
    assert state.current_stage.stage_name == "01_architect"
    assert state.focused_pane == "timeline"
    assert state.active_content_tab == "human"

    # Select next stage
    state.select_next_stage()
    assert state.selected_stage_index == 1
    assert state.current_stage.stage_name == "02_planner"

    # Bounds: Cannot exceed max stages
    state.select_next_stage()
    assert state.selected_stage_index == 1

    # Select prev stage
    state.select_prev_stage()
    assert state.selected_stage_index == 0

    # Bounds: Cannot drop below 0
    state.select_prev_stage()
    assert state.selected_stage_index == 0

    # Focus toggle
    state.toggle_focus()
    assert state.focused_pane == "content"
    state.toggle_focus()
    assert state.focused_pane == "timeline"

    # Content tab toggle
    state.toggle_content_tab()
    assert state.active_content_tab == "machine"
    state.toggle_content_tab()
    assert state.active_content_tab == "human"

    # Scroll bounds
    assert state.scroll_offset == 0
    state.scroll_down(5)
    assert state.scroll_offset == 5
    state.scroll_up(2)
    assert state.scroll_offset == 3
    state.scroll_up(10)
    assert state.scroll_offset == 0  # Clamped to 0


def test_components_rendering(mock_run_dir: Path):
    """Verify component rendering functions return valid Renderable Panels."""
    model = RunModel.from_dir(mock_run_dir)
    state = DashboardState(run=model)

    header = render_header(state)
    assert header is not None

    timeline = render_timeline(state)
    assert timeline is not None

    # Test human report render
    artifact_human = render_artifact_view(state)
    assert artifact_human is not None

    # Test machine report render
    state.set_content_tab("machine")
    artifact_machine = render_artifact_view(state)
    assert artifact_machine is not None

    footer = render_footer(state)
    assert footer is not None


def test_dashboard_app_key_handling(mock_run_dir: Path):
    """Verify DashboardApp processes navigation and control keys."""
    app = DashboardApp(mock_run_dir)

    # Initial state
    assert app.state.selected_stage_index == 0
    assert app.state.focused_pane == "timeline"
    assert app.state.active_content_tab == "human"
    assert app.state.should_exit is False

    # Key 'down' moves selection
    app.handle_key("down")
    assert app.state.selected_stage_index == 1

    # Key 'up' moves back
    app.handle_key("up")
    assert app.state.selected_stage_index == 0

    # Key 'tab' switches focus to content
    app.handle_key("tab")
    assert app.state.focused_pane == "content"

    # In content focus, 'down' scrolls instead of changing stage
    app.handle_key("down")
    assert app.state.selected_stage_index == 0
    assert app.state.scroll_offset == 1

    # Key 'm' switches to machine report
    app.handle_key("m")
    assert app.state.active_content_tab == "machine"

    # Key 'h' switches back to human report
    app.handle_key("h")
    assert app.state.active_content_tab == "human"

    # Key 'q' triggers exit
    app.handle_key("q")
    assert app.state.should_exit is True


def test_dashboard_app_render_once(mock_run_dir: Path):
    """Verify DashboardApp.render_once produces formatted snapshot string."""
    app = DashboardApp(mock_run_dir)
    output = app.render_once()

    assert "FORGE" in output
    assert "run-101" in output
    assert "01_architect" in output
    assert "02_planner" in output
    assert "OAuth2" in output


def test_dashboard_cli_render_once(mock_run_dir: Path, monkeypatch):
    """Verify 'forge dashboard [RUN_ID] --render-once' works via Click CLI."""
    runner = CliRunner()
    from forge.storage.run_manager import RunManager

    orig_init = RunManager.__init__
    def custom_init(self, project_root=None):
        orig_init(self, project_root=project_root)
        self.runs_dir = mock_run_dir.parent
    monkeypatch.setattr(RunManager, "__init__", custom_init)

    res = runner.invoke(main, ["dashboard", "run-101", "--render-once"])
    assert res.exit_code == 0
    assert "FORGE" in res.output
    assert "run-101" in res.output
    assert "01_architect" in res.output


def test_dashboard_cli_nonexistent_run(mock_run_dir: Path, monkeypatch):
    """Verify 'forge dashboard' with invalid run ID fails gracefully."""
    runner = CliRunner()
    from forge.storage.run_manager import RunManager

    orig_init = RunManager.__init__
    def custom_init(self, project_root=None):
        orig_init(self, project_root=project_root)
        self.runs_dir = mock_run_dir.parent
    monkeypatch.setattr(RunManager, "__init__", custom_init)

    res = runner.invoke(main, ["dashboard", "non-existent-run", "--render-once"])
    assert res.exit_code != 0
    assert "not found" in res.output


def test_run_model_loading_production_run_005():
    """Verify loading real historical production run-005 if present on disk."""
    prod_run_dir = Path(".forge/runs/run-005")
    if not prod_run_dir.exists():
        pytest.skip("Production run-005 not present in repo")

    model = RunModel.from_dir(prod_run_dir)
    assert model.run_id == "run-005"
    assert len(model.stages) >= 4

    # Check stage 03_executor has 3 attempts
    executor_stage = model.get_stage("03_executor")
    assert executor_stage is not None
    assert len(executor_stage.attempts) == 3

    # Check stage 04_reviewer has 2 attempts
    reviewer_stage = model.get_stage("04_reviewer")
    assert reviewer_stage is not None
    assert len(reviewer_stage.attempts) == 2


def test_header_status_variations(tmp_path):
    """Verify header component renders correct styling for various run statuses."""
    for st in ("APPROVED", "FAILED", "IN_PROGRESS", "BLOCKED", "UNKNOWN"):
        model = RunModel(
            run_id="run-test",
            task="Test task",
            status=st,
            created_at="",
            run_dir=tmp_path,
            stages=[],
            total_duration_seconds=125.0,
        )
        state = DashboardState(run=model)
        header = render_header(state)
        assert header is not None


def test_artifact_view_scrolling_and_empty_stages(tmp_path):
    """Verify artifact viewport handles empty, pending, and scrolled stages."""
    empty_stage = StageModel(
        stage_name="05_tester",
        role_name="tester",
        sequence_number=5,
        status="PENDING",
    )
    long_content = "\n".join([f"Line {i} of detailed report" for i in range(100)])
    populated_stage = StageModel(
        stage_name="01_architect",
        role_name="architect",
        sequence_number=1,
        status="APPROVED",
        human_report=long_content,
    )
    model = RunModel(
        run_id="run-scroll",
        task="Scroll test",
        status="IN_PROGRESS",
        created_at="",
        run_dir=tmp_path,
        stages=[empty_stage, populated_stage],
    )
    state = DashboardState(run=model, selected_stage_index=0)

    # Empty stage rendering
    view_empty = render_artifact_view(state)
    assert view_empty is not None

    # Populated stage with scroll offset
    state.selected_stage_index = 1
    state.scroll_offset = 20
    view_scrolled = render_artifact_view(state, max_lines=15)
    assert view_scrolled is not None


def test_dashboard_app_vim_and_paging_keys(mock_run_dir: Path):
    """Verify vim-style keys (j, k, 1, 2) and paging (pgup, pgdn) behave correctly."""
    app = DashboardApp(mock_run_dir)

    # Vim 'j' (down) in timeline moves to next stage
    app.handle_key("j")
    assert app.state.selected_stage_index == 1

    # Vim 'k' (up) in timeline moves to prev stage
    app.handle_key("k")
    assert app.state.selected_stage_index == 0

    # Numeric '2' switches to machine report
    app.handle_key("2")
    assert app.state.active_content_tab == "machine"

    # Numeric '1' switches to human report
    app.handle_key("1")
    assert app.state.active_content_tab == "human"

    # Tab to content pane and test scroll keys
    app.handle_key("tab")
    assert app.state.focused_pane == "content"
    app.handle_key("page_down")
    assert app.state.scroll_offset == 15
    app.handle_key("page_up")
    assert app.state.scroll_offset == 0


def test_dashboard_app_run_non_tty_fallback(mock_run_dir: Path):
    """Verify app.run() returns code 0 and renders snapshot when stdin is not a tty."""
    app = DashboardApp(mock_run_dir)
    ret = app.run()
    assert ret == 0

