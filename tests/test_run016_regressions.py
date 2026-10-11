"""Regression tests for run-016 defects and post-remediation hardening:
1. Protocol validation end-to-end: role mismatch preservation, invalid handoffs, exit code 0 vs validity.
2. Tester execution event semantics: successful (PASS), failed (FAIL), and unhandled exceptions.
3. Terminal state application: sync on halt without regressing newer in-memory state.
4. Presentation: sub-second duration rendering (<1s and <0.1s), PASS status styling.
5. Deterministic event replay convergence: replaying persisted events yields bit-exact live state.
6. Acceptance integration: Tester passes, Critic role mismatch halts pipeline, Commit stage skipped.
"""

import json
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from forge.adapters.base import AdapterResponse, BaseAdapter
from forge.cli import main
from forge.core.events import AgentEvent, AgentEventType
from forge.core.role import Role
from forge.core.run import Run
from forge.core.summary import RunSummary
from forge.dashboard.app import DashboardApp
from forge.dashboard.components.tester_view import render_tester_view
from forge.dashboard.components.timeline import render_timeline
from forge.dashboard.model import RunModel, StageModel
from forge.dashboard.state import DashboardState
from forge.prompts.rendered_prompt import RenderedPrompt
from forge.protocol.validator import MachineReportValidator
from forge.stages.result import StageResult
from forge.stages.stage import Stage
from forge.storage.run_manager import RunManager
from forge.testing.engine import TesterEngine
from forge.testing.models import Journey, JourneyResult, ProjectArchetype
from tests.conftest import configure_automated_execution_environment

# ============================================================================
# 1. Protocol Validation End-to-End
# ============================================================================

def test_validator_preserves_emitted_role_without_silent_normalization():
    """Invariant: Protocol validator must preserve emitted role and not silently rewrite it to expected_role."""
    raw_data = {
        "ROLE": "EXECUTOR",
        "STATUS": "SUCCESS",
        "HANDOFF": "REVIEWER",
        "REASON": "MIN-1 fixed",
    }
    report = MachineReportValidator.validate(raw_data, expected_role="CRITIC")

    assert not report.is_valid
    assert report.role == "EXECUTOR", f"Expected report.role to be 'EXECUTOR', got '{report.role}'"
    assert "Expected role 'CRITIC', got 'EXECUTOR'" in report.validation_errors
    assert any("Handoff 'REVIEWER' not in allowed handoffs" in err for err in report.validation_errors)


def test_validator_defaults_role_to_expected_when_omitted():
    """When ROLE is omitted in payload, validator defaults to expected_role."""
    raw_data = {
        "STATUS": "CRITIQUE_COMPLETE",
        "HANDOFF": "NONE",
    }
    report = MachineReportValidator.validate(raw_data, expected_role="CRITIC")
    assert report.role == "CRITIC"
    assert report.is_valid


def test_invalid_handoff_remains_invalid_for_critic():
    """Critic allowed handoffs are PLANNER, ARCHITECT, NONE. Handoffs like REVIEWER or EXECUTOR must fail."""
    for bad_handoff in ("REVIEWER", "EXECUTOR", "TESTER"):
        raw_data = {
            "ROLE": "CRITIC",
            "STATUS": "CRITIQUE_COMPLETE",
            "HANDOFF": bad_handoff,
        }
        report = MachineReportValidator.validate(raw_data, expected_role="CRITIC")
        assert not report.is_valid
        assert any(f"Handoff '{bad_handoff}' not in allowed handoffs" in err for err in report.validation_errors)


def test_role_mismatch_remains_invalid_end_to_end_in_stage_result():
    """An emitted EXECUTOR role in Critic stage must propagate to StageResult as invalid and failed."""
    raw_data = {
        "ROLE": "EXECUTOR",
        "STATUS": "SUCCESS",
        "HANDOFF": "REVIEWER",
        "REASON": "All clear",
    }
    report = MachineReportValidator.validate(raw_data, expected_role="CRITIC")
    assert not report.is_valid
    assert report.role == "EXECUTOR"

    role = Role(name="critic", sequence_number=6, template_content="", protocol_content="")
    prompt = RenderedPrompt.from_text("Audit prompt")
    adapter_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=10.0, raw_output="")

    stage_res = StageResult(
        role=role,
        prompt=prompt,
        response=adapter_resp,
        machine_report=report,
        raw_markdown="```yaml\nROLE: EXECUTOR\n```",
        duration_seconds=10.0,
        success=False,
    )

    # Process exit code was 0, but protocol validation failed -> stage status must be FAILED
    assert stage_res.status == "FAILED"
    assert not stage_res.success
    serialized = stage_res.to_dict()
    assert serialized["status"] == "FAILED"
    assert serialized["machine_report"]["role"] == "EXECUTOR"
    assert not serialized["machine_report"]["is_valid"]


def test_exit_code_zero_does_not_override_protocol_validation_failure():
    """Process exit code 0 does not imply protocol validity."""
    raw_data = {
        "ROLE": "CRITIC",
        "STATUS": "SUCCESS",
        "HANDOFF": "REVIEWER",  # Invalid handoff for CRITIC
    }
    report = MachineReportValidator.validate(raw_data, expected_role="CRITIC")
    assert not report.is_valid
    assert report.status == "FAILED"  # Success status coerced to FAILED when validation errors occur

    role = Role(name="critic", sequence_number=6, template_content="", protocol_content="")
    prompt = RenderedPrompt.from_text("Prompt")
    resp = AdapterResponse(stdout="output", stderr="", exit_code=0, duration_seconds=5.0, raw_output="output")

    stage_res = StageResult(
        role=role,
        prompt=prompt,
        response=resp,
        machine_report=report,
        raw_markdown="output",
        duration_seconds=5.0,
        success=False,
    )
    # Success definition strictly requires report.is_valid
    success = (resp.exit_code == 0 and report.is_valid and report.status not in ("REJECTED", "FAILED", "BLOCKED"))
    assert not success
    assert resp.exit_code == 0
    assert stage_res.status == "FAILED"
    assert not stage_res.success


# ============================================================================
# 2. Tester Completion and Event Semantics
# ============================================================================

def test_successful_tester_execution_emits_complete_event(tmp_path: Path):
    """Successful Tester execution emits AgentEventType.COMPLETE with PASS, exit_code 0, duration."""
    events = []
    def listener(event):
        events.append(event)

    context = Mock()
    context.project_root = tmp_path
    run_dir = tmp_path / "run-001"
    run_dir.mkdir(parents=True)
    context.run = Run(run_id="test-run", task="Verification task", run_dir=run_dir)
    context.event_listener = listener
    context.abort_event = None
    context.git = None
    context.config = None

    engine = TesterEngine(context=context)

    # Mock archetype detection and driver execution to produce 2 passing journeys
    j1 = Journey(id="J-01", title="Journey 1", description="desc 1", priority=1, steps=[])
    j2 = Journey(id="J-02", title="Journey 2", description="desc 2", priority=2, steps=[])
    res1 = JourneyResult(journey=j1, status="PASS", duration_seconds=0.06, steps_completed=1)
    res2 = JourneyResult(journey=j2, status="PASS", duration_seconds=0.07, steps_completed=1)

    with patch("forge.testing.archetypes.ArchetypeDetector.detect", return_value=ProjectArchetype.CLI), \
         patch("forge.testing.planner.JourneyPlanner.plan_journeys", return_value=[j1, j2]), \
         patch("forge.testing.drivers.cli.CliInteractionDriver.execute_journey", side_effect=[res1, res2]), \
         patch("forge.testing.drivers.cli.CliInteractionDriver.close"):

        result = engine.run()

    assert result.status == "PASS"
    assert result.success is True

    # Verify AgentEventType.COMPLETE was emitted
    complete_events = [e for e in events if e.event_type == AgentEventType.COMPLETE]
    assert len(complete_events) == 1
    complete_ev = complete_events[0]
    assert complete_ev.data["status"] == "PASS"
    assert complete_ev.data["exit_code"] == 0
    assert complete_ev.data["duration_seconds"] > 0

    # Apply to RunModel and verify in-memory update
    stage = StageModel(stage_name="04_tester", role_name="tester", sequence_number=4, status="RUNNING")
    run_model = RunModel(
        run_id="test-run", task="task", status="RUNNING", created_at="2026-10-11T00:00:00Z",
        run_dir=tmp_path, stages=[stage],
    )
    run_model.apply_event(complete_ev)
    assert stage.status == "PASS"
    assert stage.exit_code == 0
    assert stage.duration_seconds > 0


def test_failed_tester_execution_cannot_be_represented_as_passing(tmp_path: Path):
    """Failed Tester execution emits COMPLETE with status FAIL, exit_code 1, and applies as FAIL."""
    from forge.testing.models import Defect, DefectCategory, DefectSeverity

    events = []
    context = Mock()
    context.project_root = tmp_path
    run_dir = tmp_path / "run-fail"
    run_dir.mkdir(parents=True)
    context.run = Run(run_id="test-run-fail", task="Verification task", run_dir=run_dir)
    context.event_listener = lambda e: events.append(e)
    context.abort_event = None
    context.git = None
    context.config = None

    engine = TesterEngine(context=context)

    j1 = Journey(id="J-01", title="Failing Journey", description="desc", priority=1, steps=[])
    defect = Defect(
        id="DEF-01",
        title="Null pointer crash",
        category=DefectCategory.CLI_CRASH,
        severity=DefectSeverity.CRITICAL,
        journey_id="J-01",
        steps_to_reproduce=["step 1"],
        expected="200 OK",
        actual="Crash",
    )
    res1 = JourneyResult(
        journey=j1,
        status="FAIL",
        duration_seconds=0.1,
        steps_completed=0,
        defects=[defect],
    )

    with patch("forge.testing.archetypes.ArchetypeDetector.detect", return_value=ProjectArchetype.CLI), \
         patch("forge.testing.planner.JourneyPlanner.plan_journeys", return_value=[j1]), \
         patch("forge.testing.drivers.cli.CliInteractionDriver.execute_journey", return_value=res1), \
         patch("forge.testing.drivers.cli.CliInteractionDriver.close"):

        result = engine.run()

    assert result.status == "FAIL"
    assert result.success is False

    complete_events = [e for e in events if e.event_type == AgentEventType.COMPLETE]
    assert len(complete_events) == 1
    assert complete_events[0].data["status"] == "FAIL"
    assert complete_events[0].data["exit_code"] == 1

    stage = StageModel(stage_name="04_tester", role_name="tester", sequence_number=4, status="RUNNING")
    run_model = RunModel(
        run_id="test-run-fail", task="task", status="RUNNING", created_at="2026-10-11T00:00:00Z",
        run_dir=tmp_path, stages=[stage],
    )
    run_model.apply_event(complete_events[0])
    assert stage.status == "FAIL"
    assert stage.status != "APPROVED"
    assert stage.status != "PASS"


# ============================================================================
# 3. Terminal-State Synchronization & Race Protection
# ============================================================================

def test_dashboard_terminal_state_syncs_stages_on_halt(tmp_path: Path):
    """Bug fix: _apply_terminal_state must sync disk stages to run_model even when halted via halt_holder."""
    run_dir = tmp_path / "run-halt"
    run_dir.mkdir(parents=True)

    meta = {
        "run_id": "run-halt",
        "task": "Test halt stage sync",
        "created_at": "2026-10-11T00:00:00Z",
        "status": "FAILED",
        "pipeline_type": "auto",
        "summary": {
            "run_id": "run-halt",
            "final_status": "FAILED",
            "stages": [
                {"name": "Architect", "status": "APPROVED", "execution_state": "COMPLETED", "role_name": "architect", "sequence_number": 1, "duration_seconds": 10.0},
                {"name": "Planner", "status": "READY", "execution_state": "COMPLETED", "role_name": "planner", "sequence_number": 2, "duration_seconds": 15.0},
                {"name": "Executor", "status": "SUCCESS", "execution_state": "COMPLETED", "role_name": "executor", "sequence_number": 3, "duration_seconds": 50.0},
                {"name": "Tester", "status": "PASS", "execution_state": "COMPLETED", "role_name": "tester", "sequence_number": 4, "duration_seconds": 0.13},
                {"name": "Reviewer", "status": "APPROVED", "execution_state": "COMPLETED", "role_name": "reviewer", "sequence_number": 5, "duration_seconds": 20.0},
                {"name": "Critic", "status": "FAILED", "execution_state": "COMPLETED", "role_name": "critic", "sequence_number": 6, "duration_seconds": 5.0},
            ],
        },
    }
    (run_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

    (run_dir / "01_architect.json").write_text(json.dumps({"status": "APPROVED", "duration_seconds": 10.0}))
    (run_dir / "02_planner.json").write_text(json.dumps({"status": "READY", "duration_seconds": 15.0}))
    (run_dir / "03_executor.json").write_text(json.dumps({"status": "SUCCESS", "duration_seconds": 50.0}))
    (run_dir / "04_tester.json").write_text(json.dumps({"status": "PASS", "duration_seconds": 0.13}))
    (run_dir / "05_reviewer.json").write_text(json.dumps({"status": "APPROVED", "duration_seconds": 20.0}))
    (run_dir / "06_critic.json").write_text(json.dumps({"status": "FAILED", "duration_seconds": 5.0}))

    mock_halt = Mock()
    mock_halt.status = "FAILED"
    mock_halt.stage_name = "Critic"
    mock_halt.reason = "Closing Critic validation error: Expected role 'CRITIC', got 'EXECUTOR'"
    halt_holder = [mock_halt]

    app = DashboardApp(run_dir=run_dir, halt_holder=halt_holder)
    tester_stage = app.run_model.get_stage("04_tester")
    assert tester_stage is not None
    tester_stage.status = "PENDING"
    tester_stage.duration_seconds = 0.0

    app._apply_terminal_state()

    synced_tester = app.run_model.get_stage("04_tester")
    assert synced_tester.status == "PASS"
    assert abs(synced_tester.duration_seconds - 0.13) < 0.001


def test_terminal_state_sync_does_not_overwrite_newer_in_memory_state(tmp_path: Path):
    """Synchronization must NOT regress completed in-memory state with stale pending disk state."""
    run_dir = tmp_path / "run-no-regress"
    run_dir.mkdir(parents=True)

    meta = {
        "run_id": "run-no-regress", "task": "task", "status": "RUNNING", "created_at": "2026-10-11T00:00:00Z",
    }
    (run_dir / "metadata.json").write_text(json.dumps(meta))

    mock_halt = Mock(status="FAILED", stage_name="Pipeline", reason="Aborted")
    app = DashboardApp(run_dir=run_dir, halt_holder=[mock_halt])

    # In-memory stage has already finished as PASS
    tester_stage = app.run_model.get_stage("04_tester")
    assert tester_stage is not None
    tester_stage.status = "PASS"
    tester_stage.duration_seconds = 0.13

    # Disk model has no 04_tester.json yet (PENDING)
    app._apply_terminal_state()

    # In-memory stage must NOT be regressed back to PENDING
    assert tester_stage.status == "PASS"
    assert abs(tester_stage.duration_seconds - 0.13) < 0.001


# ============================================================================
# 4. Rendering & Presentation Invariants
# ============================================================================

def test_timeline_duration_formatting_subsecond_and_pass_status():
    """Timeline must format sub-second duration cleanly (e.g. 0.1s and 0.05s) and display checkmark for PASS."""
    stage_sub = StageModel(
        stage_name="04_tester", role_name="tester", sequence_number=4, status="PASS", duration_seconds=0.13,
    )
    stage_tiny = StageModel(
        stage_name="05_fast", role_name="tester", sequence_number=5, status="PASS", duration_seconds=0.04,
    )
    run_model = RunModel(
        run_id="run-subsecond", task="Test duration", status="RUNNING", created_at="2026-10-11T00:00:00Z",
        run_dir=Path("/tmp"), stages=[stage_sub, stage_tiny],
    )
    state = DashboardState(run=run_model)

    console = Console(width=60)
    with console.capture() as capture:
        console.print(render_timeline(state))
    rendered = capture.get()

    assert "✓" in rendered
    assert "0.1s" in rendered
    assert "0.04s" in rendered
    assert "--" not in rendered


def test_timeline_renders_dash_only_for_zero_or_pending():
    """Timeline renders '--' for zero duration or unexecuted stage."""
    stage = StageModel(
        stage_name="04_tester", role_name="tester", sequence_number=4, status="PENDING", duration_seconds=0.0,
    )
    run_model = RunModel(
        run_id="run-pending", task="Task", status="PENDING", created_at="2026-10-11T00:00:00Z",
        run_dir=Path("/tmp"), stages=[stage],
    )
    state = DashboardState(run=run_model)

    console = Console(width=60)
    with console.capture() as capture:
        console.print(render_timeline(state))
    rendered = capture.get()

    assert "--" in rendered
    assert "○" in rendered


def test_tester_view_renders_pass_status_as_success():
    """Tester view must style status 'PASS' as green, not failed/red."""
    stage = StageModel(
        stage_name="04_tester", role_name="tester", sequence_number=4, status="PASS", duration_seconds=0.13,
        raw_content="# QA Report\nAll passed", human_report="# QA Report\nAll passed",
    )
    run_model = RunModel(
        run_id="run-tester-view", task="Test styling", status="COMPLETED", created_at="2026-10-11T00:00:00Z",
        run_dir=Path("/tmp"), stages=[stage],
    )
    state = DashboardState(run=run_model, active_tab=3)

    console = Console(width=80)
    with console.capture() as capture:
        console.print(render_tester_view(state))
    rendered = capture.get()

    assert "STATUS: PASS" in rendered
    assert "No tester evidence available" not in rendered


def test_apply_event_stage_record_update_mutates_stage_state():
    """apply_event lifecycle stage_record_update must update stage status and duration in stages collection."""
    stage = StageModel(
        stage_name="04_tester", role_name="tester", sequence_number=4, status="RUNNING", duration_seconds=0.0,
    )
    run_model = RunModel(
        run_id="run-update", task="Test update", status="RUNNING", created_at="2026-10-11T00:00:00Z",
        run_dir=Path("/tmp"), stages=[stage], summary=RunSummary(run_id="run-update"),
    )

    ev = AgentEvent(
        event_type=AgentEventType.HEARTBEAT, timestamp=time.time(),
        data={
            "lifecycle": "stage_record_update", "name": "Tester", "stage_name": "04_tester",
            "role_name": "tester", "sequence_number": 4, "status": "PASS", "duration_seconds": 0.13,
            "execution_state": "COMPLETED",
        },
    )
    run_model.apply_event(ev)

    updated_stage = run_model.get_stage("04_tester")
    assert updated_stage.status == "PASS"
    assert abs(updated_stage.duration_seconds - 0.13) < 0.001


# ============================================================================
# 5. Deterministic Event Replay Convergence
# ============================================================================

def test_event_replay_converges_with_live_execution():
    """Replaying recorded lifecycle events produces the exact same stage status and duration as live execution."""
    stage_names = ["01_architect", "02_planner", "03_executor", "04_tester", "05_reviewer", "06_critic"]

    def create_model():
        stages = [
            StageModel(stage_name=name, role_name=name.split("_")[1], sequence_number=idx + 1, status="PENDING")
            for idx, name in enumerate(stage_names)
        ]
        return RunModel(
            run_id="replay-run", task="Task", status="RUNNING", created_at="2026-10-11T00:00:00Z",
            run_dir=Path("/tmp"), stages=stages, summary=RunSummary(run_id="replay-run"),
        )

    live_model = create_model()
    recorded_events = []

    # Sequence of lifecycle events simulating run-016
    event_specs = [
        ("stage_start", "01_architect", "architect", 1, "RUNNING", 0.0),
        ("stage_finish", "01_architect", "architect", 1, "APPROVED", 10.5),
        ("stage_record_update", "01_architect", "architect", 1, "APPROVED", 10.5),

        ("stage_start", "02_planner", "planner", 2, "RUNNING", 0.0),
        ("stage_finish", "02_planner", "planner", 2, "READY", 8.2),
        ("stage_record_update", "02_planner", "planner", 2, "READY", 8.2),

        ("stage_start", "03_executor", "executor", 3, "RUNNING", 0.0),
        ("stage_finish", "03_executor", "executor", 3, "SUCCESS", 25.0),
        ("stage_record_update", "03_executor", "executor", 3, "SUCCESS", 25.0),

        ("stage_start", "04_tester", "tester", 4, "RUNNING", 0.0),
        ("stage_finish", "04_tester", "tester", 4, "PASS", 0.13),
        ("stage_record_update", "04_tester", "tester", 4, "PASS", 0.13),

        ("stage_start", "05_reviewer", "reviewer", 5, "RUNNING", 0.0),
        ("stage_finish", "05_reviewer", "reviewer", 5, "APPROVED", 14.1),
        ("stage_record_update", "05_reviewer", "reviewer", 5, "APPROVED", 14.1),

        ("stage_start", "06_critic", "critic", 6, "RUNNING", 0.0),
        ("stage_finish", "06_critic", "critic", 6, "FAILED", 4.3),
        ("stage_record_update", "06_critic", "critic", 6, "FAILED", 4.3),
    ]

    for lifecycle, s_name, r_name, seq, st, dur in event_specs:
        ev = AgentEvent(
            event_type=AgentEventType.HEARTBEAT, timestamp=time.time(),
            data={
                "lifecycle": lifecycle, "stage_name": s_name, "role_name": r_name, "sequence_number": seq,
                "status": st, "duration_seconds": dur, "execution_state": "COMPLETED",
            },
        )
        live_model.apply_event(ev)
        recorded_events.append(ev)

    # Now replay all recorded events on a fresh model
    replay_model = create_model()
    for ev in recorded_events:
        replay_model.apply_event(ev)

    # Compare all stages between live and replay
    for s_name in stage_names:
        live_s = live_model.get_stage(s_name)
        replay_s = replay_model.get_stage(s_name)
        assert live_s.status == replay_s.status
        assert abs(live_s.duration_seconds - replay_s.duration_seconds) < 0.001


def test_tester_exception_path_emits_error_and_does_not_report_success(tmp_path: Path):
    """When TesterEngine raises an unhandled exception, Stage.execute emits ERROR and re-raises."""
    events = []
    def listener(event):
        events.append(event)

    context = Mock()
    context.project_root = tmp_path
    run_dir = tmp_path / "run-exc"
    run_dir.mkdir(parents=True)
    context.run = Run(run_id="test-run-exc", task="Task", run_dir=run_dir)
    context.event_listener = listener
    context.config = None
    context.abort_event = None

    class NonMockAdapter(BaseAdapter):
        def is_available(self):
            return True
        def execute(self, *args, **kwargs):
            return None

    adapter = NonMockAdapter(name="non_mock")
    role = Role(name="tester", sequence_number=4, template_content="", protocol_content="")
    stage = Stage(role=role, adapter=adapter, event_listener=listener)

    with patch("forge.testing.engine.TesterEngine.run", side_effect=RuntimeError("Docker engine crashed")), \
         pytest.raises(RuntimeError, match="Docker engine crashed"):
        stage.run(context)

    error_events = [e for e in events if e.event_type == AgentEventType.ERROR]
    assert len(error_events) == 1
    assert error_events[0].data["status"] == "FAILED"
    assert error_events[0].data["exit_code"] == 1
    assert "Docker engine crashed" in error_events[0].text


def test_stage_attempt_tracking_recognizes_pass():
    """StageModel.attempts archives prior attempt when a new attempt starts after PASS."""
    stage = StageModel(
        stage_name="04_tester",
        role_name="tester",
        sequence_number=4,
        status="PASS",
        duration_seconds=0.13,
    )
    run_model = RunModel(
        run_id="test-attempts",
        task="Test attempts",
        status="RUNNING",
        created_at="2026-10-11T00:00:00Z",
        run_dir=Path("/tmp"),
        stages=[stage],
    )
    # Simulate stage_start lifecycle event for a second attempt
    ev = AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={
            "lifecycle": "stage_start",
            "stage_name": "04_tester",
            "role_name": "tester",
            "sequence_number": 4,
        },
    )
    run_model.apply_event(ev)

    assert len(stage.attempts) == 1
    assert stage.attempts[0].attempt_number == 1
    assert stage.attempts[0].status == "PASS"
    assert abs(stage.attempts[0].duration_seconds - 0.13) < 0.001
    assert stage.status == "RUNNING"


# ============================================================================
# 6. End-to-End Mocked Pipeline Acceptance Test
# ============================================================================

def test_acceptance_mocked_pipeline_halts_on_critic_role_mismatch(tmp_path: Path):
    """End-to-End Acceptance Test (Section 7):
    - Full autonomous pipeline execution with mocked stages
    - Tester produces PASS (0.13s)
    - Reviewer produces APPROVED
    - Critic emits ROLE: EXECUTOR and HANDOFF: REVIEWER
    - Pipeline halts with AutonomousHalt(exit_code=1)
    - Commit stage (99) is NOT REACHED / skipped
    - Terminal halt synchronizes state: Tester is PASS (0.13s) in both CLI RunSummary and TUI RunModel
    - Closing Critic failure message is persisted without stale contamination
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(
            stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```",
            stderr="", exit_code=0, duration_seconds=1.2, raw_output="APPROVED",
        )
        plan_resp = AdapterResponse(
            stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```",
            stderr="", exit_code=0, duration_seconds=0.8, raw_output="READY",
        )
        exec_resp = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```",
            stderr="", exit_code=0, duration_seconds=2.5, raw_output="SUCCESS",
        )
        test_resp = AdapterResponse(
            stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=0.13, raw_output="PASS",
        )
        rev_resp = AdapterResponse(
            stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```",
            stderr="", exit_code=0, duration_seconds=1.1, raw_output="APPROVED",
        )
        critic_mismatch = AdapterResponse(
            stdout=(
                "# Closing Critic Audit\n\n"
                "```yaml\n"
                "ROLE: EXECUTOR\n"
                "STATUS: FAILED\n"
                "HANDOFF: REVIEWER\n"
                "REASON: MIN-1 and non-finite/missing inconsistency fixed and gated; 978 tests pass.\n"
                "```"
            ),
            stderr="", exit_code=0, duration_seconds=92.45, raw_output="EXECUTOR",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, critic_mismatch]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "--auto-commit", "Audit run-016 task"])
            assert res.exit_code == 1

        # Assert CLI output
        assert "Expected role 'CRITIC', got 'EXECUTOR'" in res.output
        assert "Handoff 'REVIEWER' not in allowed handoffs" in res.output

        rm = RunManager(Path.cwd())
        run = rm.latest()
        assert run.status == "FAILED"
        assert run.summary is not None
        assert run.summary.final_status == "FAILED"

        # Check commit stage was NOT reached
        commit_stage = next((s for s in run.summary.stages if s.role_name == "commit"), None)
        assert commit_stage is not None
        assert commit_stage.execution_state == "NOT REACHED"

        # Check Tester stage in CLI summary
        tester_stages = [s for s in run.summary.stages if s.role_name == "tester"]
        assert len(tester_stages) == 1
        assert tester_stages[0].status == "PASS"
        assert abs(tester_stages[0].duration_seconds - 0.13) < 0.001
        assert tester_stages[0].execution_state == "COMPLETED"

        # Check Critic stage in CLI summary
        critic_stages = [s for s in run.summary.stages if s.role_name == "critic"]
        assert len(critic_stages) == 1
        assert critic_stages[0].status == "FAILED"

        # Check TUI Dashboard terminal-state synchronization
        app = DashboardApp(run_dir=run.run_dir)
        app._apply_terminal_state()
        tui_tester = app.run_model.get_stage("04_tester")
        assert tui_tester is not None
        assert tui_tester.status == "PASS"
        assert abs(tui_tester.duration_seconds - 0.13) < 0.001
        assert tui_tester.status != "PENDING"


# ============================================================================
# 7. Closing Critic Prompt Hardening, Provenance, and Bounded Retry
# ============================================================================

def test_critic_prompt_delimiters_isolate_executor_evidence():
    """Verify that prior stage outputs (specifically Executor's YAML report) are:
    1. Delimited inside <audit_evidence stage="executor">...</audit_evidence> tags.
    2. Sanitized so nested ```yaml fences become ```yaml-evidence to prevent LLM echoing.
    3. The bottom protocol schema mandates ROLE: CRITIC and HANDOFF: NONE derived from stage config.
    4. The prompt requires an independently generated Critic evaluation.
    """
    from forge.prompts.compiler import PromptCompiler
    from forge.prompts.instruction import Instruction
    from forge.core.templates import DEFAULT_ROLES

    executor_output = (
        "# Human Report\n"
        "Implemented MIN-1 boundary fix.\n\n"
        "```yaml\n"
        "ROLE: EXECUTOR\n"
        "STATUS: SUCCESS\n"
        "HANDOFF: REVIEWER\n"
        "REASON: \"MIN-1 and non-finite/missing inconsistency fixed and gated; 978 tests pass.\"\n"
        "```\n"
    )

    instruction = Instruction(
        role_name="critic",
        task="Audit the ADR-027 remediation",
        previous_stage_outputs={"executor": executor_output},
        git_diff="diff --git a/test.py b/test.py\n+assert True",
    )

    rendered = PromptCompiler.compile(instruction, DEFAULT_ROLES["critic"])
    text = rendered.text

    # 1. Delimited inside audit_evidence tags
    assert '<audit_evidence stage="executor">' in text
    assert "</audit_evidence>" in text

    # 2. Nested ```yaml block inside evidence is sanitized to ```yaml-evidence
    assert "```yaml-evidence\nROLE: EXECUTOR" in text
    # There should NOT be an unescaped ```yaml\nROLE: EXECUTOR in the evidence section
    assert "```yaml\nROLE: EXECUTOR" not in text

    # 3. Bottom protocol schema mandates ROLE: CRITIC and HANDOFF: NONE
    assert "## MANDATORY PROTOCOL REQUIREMENT FOR CRITIC" in text
    assert "ROLE: CRITIC" in text
    assert "STATUS: CRITIQUE_COMPLETE" in text
    assert "HANDOFF: NONE" in text

    # 4. Independent report mandate
    assert "independently analyze the codebase and git diff" in text
    assert "Do NOT copy the machine report of the Executor" in text


def test_critic_originates_expected_role_and_handoff_from_trusted_stage_config():
    """Verify that Critic expected role and allowed statuses/handoffs originate strictly
    from trusted stage configuration / MachineReportValidator, and not from prior stage content.
    """
    from forge.prompts.compiler import PromptCompiler
    from forge.prompts.instruction import Instruction

    try:
        MachineReportValidator.register_role_rules(
            "CRITIC",
            allowed_statuses={"CUSTOM_CRITIQUE_PASS", "CRITIQUE_COMPLETE"},
            allowed_handoffs={"CUSTOM_NEXT_STAGE", "NONE"},
        )

        instruction = Instruction(
            role_name="critic",
            task="Audit code",
            previous_stage_outputs={"executor": "```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```"},
        )
        rendered = PromptCompiler.compile(instruction, "Template")
        text = rendered.text

        # Allowed values reflect trusted stage configuration
        assert "CUSTOM_CRITIQUE_PASS" in text
        assert "CUSTOM_NEXT_STAGE" in text
        # Prior stage values are NOT adopted into allowed statuses/handoffs
        assert "REVIEWER" not in text.split("Allowed HANDOFF values for CRITIC:")[-1]
    finally:
        MachineReportValidator.reset_custom_rules()


def test_critic_valid_and_malformed_protocol_validation():
    """Verify strict validation for Critic:
    - Valid CRITIC report with CRITIQUE_COMPLETE and NONE passes.
    - Contaminated EXECUTOR report fails, preserves EXECUTOR role, and records error.
    - Malformed YAML fails validation.
    """
    valid_data = {
        "ROLE": "CRITIC",
        "STATUS": "CRITIQUE_COMPLETE",
        "HANDOFF": "NONE",
        "REASON": "Codebase audit completed.",
    }
    report_valid = MachineReportValidator.validate(valid_data, expected_role="CRITIC")
    assert report_valid.is_valid
    assert report_valid.role == "CRITIC"
    assert report_valid.status == "CRITIQUE_COMPLETE"
    assert report_valid.handoff == "NONE"

    # Contaminated with Executor
    executor_data = {
        "ROLE": "EXECUTOR",
        "STATUS": "SUCCESS",
        "HANDOFF": "REVIEWER",
        "REASON": "MIN-1 fixed",
    }
    report_exec = MachineReportValidator.validate(executor_data, expected_role="CRITIC")
    assert not report_exec.is_valid
    assert report_exec.role == "EXECUTOR"
    assert "Expected role 'CRITIC', got 'EXECUTOR'" in report_exec.validation_errors
    assert any("Handoff 'REVIEWER' not in allowed handoffs" in e for e in report_exec.validation_errors)

    from forge.protocol.parser import MachineReportParser
    parsed_dict, raw_yaml = MachineReportParser.extract_yaml("Invalid yaml block: [unclosed", expected_role="CRITIC")
    report_malformed = MachineReportValidator.validate(parsed_dict, expected_role="CRITIC", raw_yaml=raw_yaml)
    assert not report_malformed.is_valid


def test_critic_protocol_retry_succeeds_on_second_attempt(tmp_path):
    """Verify that when Critic fails protocol validation on attempt 1 (e.g. emits EXECUTOR),
    Forge:
    1. Preserves attempt 1 artifacts (06_critic_attempt_1.md and 06_critic_attempt_1.json).
    2. Retries Critic with targeted repair feedback (Attempt 2/2).
    3. When attempt 2 emits valid ROLE: CRITIC report, pipeline succeeds.
    4. Preserves attempt 2 artifacts (06_critic_attempt_2.md and 06_critic_attempt_2.json).
    5. Auto-commit runs (if requested) and run finishes with APPROVED / COMPLETED.
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="SUCCESS")
        test_resp = AdapterResponse(stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS")
        rev_resp = AdapterResponse(stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED")

        # Attempt 1 emits contaminated Executor report
        critic_attempt_1 = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\nREASON: 'MIN-1 fixed'\n```",
            stderr="", exit_code=0, duration_seconds=1.5, raw_output="EXECUTOR",
        )
        # Attempt 2 emits valid Critic report
        critic_attempt_2 = AdapterResponse(
            stdout="```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\nHANDOFF: NONE\nREASON: 'Audit completed successfully'\n```",
            stderr="", exit_code=0, duration_seconds=1.2, raw_output="CRITIQUE_COMPLETE",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, critic_attempt_1, critic_attempt_2]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "--auto-commit", "Test retry success"])
            assert res.exit_code == 0, f"Expected 0, got {res.exit_code}. Output:\n{res.output}"

        # Assert CLI output mentions retry and success
        assert "Retrying (Attempt 2/2)" in res.output
        assert "Critic audit retry completed" in res.output

        rm = RunManager(Path.cwd())
        run = rm.latest()
        assert run.status in ("COMPLETED", "APPROVED")

        # Verify artifacts on disk
        run_dir = run.run_dir
        assert (run_dir / "06_critic_attempt_1.md").exists()
        assert (run_dir / "06_critic_attempt_1.json").exists()
        assert (run_dir / "06_critic_attempt_2.md").exists()
        assert (run_dir / "06_critic_attempt_2.json").exists()

        # Check attempt 1 json recorded EXECUTOR
        att1_json = json.loads((run_dir / "06_critic_attempt_1.json").read_text(encoding="utf-8"))
        assert att1_json["machine_report"]["role"] == "EXECUTOR"
        assert not att1_json["machine_report"]["is_valid"]

        # Check attempt 2 json recorded CRITIC
        att2_json = json.loads((run_dir / "06_critic_attempt_2.json").read_text(encoding="utf-8"))
        assert att2_json["machine_report"]["role"] == "CRITIC"
        assert att2_json["machine_report"]["is_valid"]


def test_critic_protocol_retry_exhaustion_halts(tmp_path):
    """Verify that when Critic fails protocol validation on BOTH attempt 1 and attempt 2:
    1. Preserves attempt 1 artifacts (06_critic_attempt_1.md/json).
    2. Preserves attempt 2 artifacts (06_critic_attempt_2.md/json).
    3. Halts with exit code 1.
    4. Auto-commit is skipped / NOT REACHED.
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="SUCCESS")
        test_resp = AdapterResponse(stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS")
        rev_resp = AdapterResponse(stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED")

        # Attempt 1 emits wrong role
        critic_attempt_1 = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=1.5, raw_output="EXECUTOR",
        )
        # Attempt 2 ALSO emits wrong role
        critic_attempt_2 = AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```",
            stderr="", exit_code=0, duration_seconds=1.5, raw_output="EXECUTOR",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, critic_attempt_1, critic_attempt_2]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "--auto-commit", "Test retry exhaustion"])
            assert res.exit_code == 1

        assert "Retrying (Attempt 2/2)" in res.output
        assert "Expected role 'CRITIC', got 'EXECUTOR'" in res.output

        rm = RunManager(Path.cwd())
        run = rm.latest()
        assert run.status == "FAILED"

        run_dir = run.run_dir
        assert (run_dir / "06_critic_attempt_1.md").exists()
        assert (run_dir / "06_critic_attempt_1.json").exists()
        assert (run_dir / "06_critic_attempt_2.md").exists()
        assert (run_dir / "06_critic_attempt_2.json").exists()

        commit_stage = next((s for s in run.summary.stages if s.role_name == "commit"), None)
        assert commit_stage is not None
        assert commit_stage.execution_state == "NOT REACHED"


def test_critic_non_protocol_failures_do_not_retry(tmp_path):
    """Verify that process exit code 1 or status BLOCKED in Critic halts immediately without retrying."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="SUCCESS")
        test_resp = AdapterResponse(stdout="```yaml\nROLE: TESTER\nSTATUS: PASS\nHANDOFF: REVIEWER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="PASS")
        rev_resp = AdapterResponse(stdout="```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```", stderr="", exit_code=0, duration_seconds=1.0, raw_output="APPROVED")

        # Critic exits with process code 1 (process failure)
        critic_crash = AdapterResponse(
            stdout="", stderr="Out of memory", exit_code=1, duration_seconds=0.5, raw_output="",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_resp, rev_resp, critic_crash]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "--auto-commit", "Test crash no retry"])
            assert res.exit_code == 1

        # No retry attempted
        assert "Retrying (Attempt 2/2)" not in res.output

        rm = RunManager(Path.cwd())
        run = rm.latest()
        run_dir = run.run_dir
        assert not (run_dir / "06_critic_attempt_1.md").exists()
        assert not (run_dir / "06_critic_attempt_2.md").exists()
