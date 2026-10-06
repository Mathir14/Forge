"""Regression tests for v0.1.0b7 dashboard and transport-recovery fixes.

Covers:
1. Model/tool/web output containing "authentication" does NOT block transport recovery.
2. Unexpected EOF + valid OpenCode session ID enters daemon recovery and succeeds when daemon session succeeds.
3. RunModel.from_dir() with only 00_critic.json seeds the complete canonical pipeline.
4. CRITIQUE_COMPLETE (and READY/PASSED/COMPLETED) renders as completed/successful in timeline.
5. Tab navigation remains visible across all five tabs.
6. Tester/PKB/Compare empty states retain the full navigation bar.
7. Compare view correctly discovers historical Run B from the project root.
8. Dashboard selection follows Critic -> Architect -> Planner as active stages change and respects manual navigation.
9. Narrow terminal widths degrade gracefully in centralized navigation.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from rich.console import Console

from forge.adapters.base import AdapterResponse
from forge.adapters.opencode import OpenCodeAdapter, is_recoverable_opencode_error
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.dashboard.app import DashboardApp
from forge.dashboard.components.compare_view import render_compare_view
from forge.dashboard.components.navigation import render_tab_navigation
from forge.dashboard.components.pkb_view import render_pkb_view
from forge.dashboard.components.tester_view import render_tester_view
from forge.dashboard.components.timeline import render_timeline
from forge.dashboard.model import RunModel, StageModel
from forge.dashboard.state import DashboardState
from forge.stages.definition import StageOrder
from forge.storage.run_manager import RunManager


# ===========================================================================
# 1. Transport Recovery Classification Regression Tests
# ===========================================================================


def test_output_containing_authentication_does_not_block_transport_recovery():
    """Verify raw model/tool output containing 'authentication' does NOT block transport recovery."""
    # Simulates Planner generating Streamlit docs containing 'authentication' before premature EOF
    raw_tool_output = (
        "Here is the Streamlit documentation on user authentication and session state:\n"
        "st.login() handles authentication with OAuth2 providers.\n"
        "Ensure your authentication tokens are stored securely in st.session_state.\n"
        * 100
    )

    eof_event = AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="Unexpected EOF before terminal event.",
        result=ExecutionResult(
            exit_code=1,
            duration_seconds=120.5,
            stdout=raw_tool_output,
            stderr="",
            raw_output=raw_tool_output,
            metadata={
                "session_id": "ses_ef87f9f04ffefIQg1k7qMVOzUl",
                "diagnostic": "Provider stream terminated prematurely without emitting COMPLETE or ERROR.",
            },
        ),
    )

    # Classification must recognise transport failure and NOT be rejected by 'authentication' in raw output
    assert is_recoverable_opencode_error(eof_event) is True

    adapter = OpenCodeAdapter()
    assert adapter.can_recover_session(eof_event) is True


def test_genuine_authentication_error_in_stderr_remains_non_recoverable():
    """Verify genuine authentication errors in stderr or error metadata ARE classified as non-recoverable."""
    auth_event = AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="OpenCode error event",
        result=ExecutionResult(
            exit_code=1,
            duration_seconds=1.2,
            stdout="",
            stderr="401 Unauthorized: Invalid API key or authentication credentials expired.",
            metadata={
                "session_id": "ses_auth_fail",
                "error_type": "authentication_error",
            },
        ),
    )

    assert is_recoverable_opencode_error(auth_event) is False

    adapter = OpenCodeAdapter()
    assert adapter.can_recover_session(auth_event) is False


# ===========================================================================
# 2. Unexpected EOF + Daemon Session Recovery Test
# ===========================================================================


def test_unexpected_eof_with_valid_session_enters_recovery_and_succeeds(tmp_path: Path):
    """Verify unexpected EOF with valid session ID enters recovery and recovers full plan on success."""
    adapter = OpenCodeAdapter(extra_flags={"recovery_timeout": 5.0, "recovery_poll_interval": 0.05})
    adapter._session_id = "ses_planner_recovery"

    plan_output = (
        "# Implementation Plan\n\n"
        "## Wave 1: Core Framework\n"
        "- Task 1: Setup test scaffolding\n"
        "- Task 2: Implement protocol parser\n\n"
        "## Machine Report\n"
        "```yaml\n"
        "ROLE: PLANNER\n"
        "STATUS: READY\n"
        "HANDOFF: EXECUTOR\n"
        "CONFIDENCE: HIGH\n"
        "REASON: Complete 36-task plan generated.\n"
        "```\n"
    )

    def mock_query_daemon(endpoint, method="GET", cwd=None, timeout=10.0):
        if endpoint == "/api/session/ses_planner_recovery":
            return {
                "data": {
                    "id": "ses_planner_recovery",
                    "outcome": "succeeded",
                    "tokens": {"input": 4500, "output": 8200},
                    "time": {"created": 1000000, "idle": 1045000},
                }
            }
        elif "message" in endpoint:
            return {
                "data": [
                    {
                        "id": "msg_plan_final",
                        "role": "assistant",
                        "content": [{"type": "text", "text": plan_output}],
                    }
                ]
            }
        return None

    eof_event = AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="Unexpected EOF before terminal event.",
        result=ExecutionResult(
            exit_code=1,
            duration_seconds=30.0,
            stdout="partial stream...",
            stderr="",
            raw_output="partial stream...",
            metadata={"session_id": "ses_planner_recovery"},
        ),
    )

    with patch.object(adapter, "_resolve_binary", return_value=("/bin/opencode", None)), \
         patch.object(adapter, "_query_daemon_api", side_effect=mock_query_daemon):

        assert adapter.can_recover_session(eof_event) is True
        recovered = adapter.recover_session(terminal_event=eof_event, cwd=tmp_path)
        assert recovered is not None
        rec_event, rec_resp = recovered

        assert rec_event.event_type == AgentEventType.COMPLETE
        assert rec_event.result.exit_code == 0
        assert "## Wave 1: Core Framework" in rec_event.result.stdout
        assert "ROLE: PLANNER" in rec_event.result.stdout
        assert rec_event.result.metadata.get("recovered") is True
        assert rec_resp.exit_code == 0
        assert rec_resp.stdout == rec_event.result.stdout


# ===========================================================================
# 3. RunModel.from_dir() with only 00_critic.json Seeds Canonical Pipeline
# ===========================================================================


def test_run_model_from_dir_with_only_critic_seeds_complete_canonical_pipeline(tmp_path: Path):
    """Verify RunModel.from_dir() with only 00_critic.json seeds all canonical autonomous stages."""
    run_dir = tmp_path / "run-003"
    run_dir.mkdir(parents=True)

    critic_meta = {
        "status": "CRITIQUE_COMPLETE",
        "duration_seconds": 418.5,
        "exit_code": 0,
        "machine_report": {
            "role": "CRITIC",
            "status": "CRITIQUE_COMPLETE",
            "handoff": "ARCHITECT",
            "reason": "Critic audit completed successfully with tech debt items.",
        },
    }
    with open(run_dir / "00_critic.json", "w", encoding="utf-8") as f:
        json.dump(critic_meta, f)

    with open(run_dir / "00_critic.md", "w", encoding="utf-8") as f:
        f.write("# Critic Audit\n\n```yaml\nROLE: CRITIC\nSTATUS: CRITIQUE_COMPLETE\n```")

    model = RunModel.from_dir(run_dir)

    # 1. Critic must be preserved with actual status and duration
    critic_stage = model.get_stage("00_critic")
    assert critic_stage is not None
    assert critic_stage.status == "CRITIQUE_COMPLETE"
    assert critic_stage.duration_seconds == 418.5
    assert critic_stage.sequence_number == 0

    # 2. Every subsequent canonical stage must exist and be represented as PENDING
    expected_pending = [
        ("01_architect", "architect", 1),
        ("02_planner", "planner", 2),
        ("03_executor", "executor", 3),
        ("04_tester", "tester", 4),
        ("05_reviewer", "reviewer", 5),
    ]
    for s_name, role, seq in expected_pending:
        st = model.get_stage(s_name)
        assert st is not None, f"Stage {s_name} missing from dashboard model on resumed run"
        assert st.role_name == role
        assert st.sequence_number == seq
        assert st.status == "PENDING"
        assert st.duration_seconds == 0.0

    # 3. Overall stage count includes historical Critic + canonical pipeline
    assert len(model.stages) >= 6
    assert model.stages[0].stage_name == "00_critic"
    assert model.stages[1].stage_name == "01_architect"
    assert model.stages[2].stage_name == "02_planner"


# ===========================================================================
# 4. CRITIQUE_COMPLETE / READY / PASSED in Timeline
# ===========================================================================


def test_critique_complete_and_protocol_successes_render_as_successful_in_timeline():
    """Verify CRITIQUE_COMPLETE, READY, PASSED, COMPLETED render as green success in timeline."""
    assert StageOrder.is_success_status("CRITIQUE_COMPLETE") is True
    assert StageOrder.is_success_status("READY") is True
    assert StageOrder.is_success_status("PASSED") is True
    assert StageOrder.is_success_status("COMPLETED") is True
    assert StageOrder.is_success_status("APPROVED") is True
    assert StageOrder.is_success_status("DONE") is True

    stages = [
        StageModel(stage_name="00_critic", role_name="critic", sequence_number=0, status="CRITIQUE_COMPLETE", duration_seconds=12.0),
        StageModel(stage_name="01_architect", role_name="architect", sequence_number=1, status="READY", duration_seconds=15.0),
        StageModel(stage_name="02_planner", role_name="planner", sequence_number=2, status="APPROVED", duration_seconds=20.0),
        StageModel(stage_name="03_executor", role_name="executor", sequence_number=3, status="SUCCESS", duration_seconds=30.0),
        StageModel(stage_name="04_tester", role_name="tester", sequence_number=4, status="PASSED", duration_seconds=10.0),
        StageModel(stage_name="05_reviewer", role_name="reviewer", sequence_number=5, status="COMPLETED", duration_seconds=5.0),
    ]

    run = RunModel(
        run_id="run-test",
        task="Test run",
        status="RUNNING",
        created_at="",
        run_dir=Path("/tmp/run-test"),
        stages=stages,
    )
    state = DashboardState(run=run)

    panel = render_timeline(state)
    console = Console(width=80)
    with console.capture() as cap:
        console.print(panel)
    rendered = cap.get()

    # All completed successful stages should display checkmark icon ✓
    assert "✓" in rendered
    assert "00_critic" in rendered
    assert "01_architect" in rendered


# ===========================================================================
# 5. Tab Navigation Visible Across All 5 Tabs
# ===========================================================================


def test_tab_navigation_remains_visible_across_all_five_tabs():
    """Verify centralized tab navigation retains all 5 tab indicators across all active tabs."""
    run = RunModel(run_id="run-tabs", task="Tabs", status="RUNNING", created_at="", run_dir=Path("/tmp/run"), stages=[])
    state = DashboardState(run=run)

    expected_tab_names = ["1: Console", "2: Artifact", "3: Tester", "4: PKB", "5: Compare"]

    for tab_num in range(1, 6):
        state.set_tab(tab_num)
        nav = render_tab_navigation(state)
        for expected in expected_tab_names:
            assert expected in nav, f"Tab '{expected}' missing when active_tab is {tab_num}"


# ===========================================================================
# 6. Empty States Retain Full Navigation Bar
# ===========================================================================


def test_empty_states_retain_full_navigation_bar(tmp_path: Path):
    """Verify Tester, PKB, and Compare empty states retain full 5-tab navigation bar."""
    run = RunModel(
        run_id="run-empty-test",
        task="Empty states test",
        status="RUNNING",
        created_at="",
        run_dir=tmp_path / "run-empty-test",
        stages=[],
    )
    state = DashboardState(run=run)

    expected_tabs = ["[1: Console]", "[2: Artifact]", "[4: PKB]"]

    # 1. Tester empty state
    state.set_tab(3)
    tester_panel = render_tester_view(state)
    assert tester_panel.title is not None
    title_str = str(tester_panel.title)
    for tab in expected_tabs:
        assert tab in title_str, f"Tester empty state missing tab '{tab}'"
    assert "3: Tester" in title_str

    # 2. PKB empty state
    state.set_tab(4)
    pkb_panel = render_pkb_view(state, project_root=tmp_path)
    assert pkb_panel.title is not None
    title_str = str(pkb_panel.title)
    for tab in ["[1: Console]", "[2: Artifact]", "[3: Tester]"]:
        assert tab in title_str, f"PKB empty state missing tab '{tab}'"
    assert "4: PKB" in title_str

    # 3. Compare empty state
    state.set_tab(5)
    compare_panel = render_compare_view(state, project_root=tmp_path)
    assert compare_panel.title is not None
    title_str = str(compare_panel.title)
    for tab in ["[1: Console]", "[2: Artifact]", "[3: Tester]", "[4: PKB]"]:
        assert tab in title_str, f"Compare empty state missing tab '{tab}'"
    assert "5: Compare" in title_str


# ===========================================================================
# 7. Compare View Project Root Resolution
# ===========================================================================


def test_compare_view_discovers_run_b_from_project_root(tmp_path: Path):
    """Verify Compare view resolves RunManager from actual project root, discovering historical Run B."""
    project_root = tmp_path / "MyProject"
    runs_dir = project_root / ".forge" / "runs"
    runs_dir.mkdir(parents=True)

    # Run 1 (historical)
    run_1_dir = runs_dir / "run-001"
    run_1_dir.mkdir()
    with open(run_1_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump({"run_id": "run-001", "task": "Initial implementation", "status": "APPROVED"}, f)
    with open(run_1_dir / "01_architect.json", "w", encoding="utf-8") as f:
        json.dump({"status": "APPROVED", "duration_seconds": 15.0}, f)

    # Run 2 (current)
    run_2_dir = runs_dir / "run-002"
    run_2_dir.mkdir()
    with open(run_2_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump({"run_id": "run-002", "task": "Bug fixes", "status": "APPROVED"}, f)
    with open(run_2_dir / "01_architect.json", "w", encoding="utf-8") as f:
        json.dump({"status": "APPROVED", "duration_seconds": 25.0}, f)

    run_a = RunModel.from_dir(run_2_dir)
    state = DashboardState(run=run_a)
    state.set_tab(5)

    # Call render_compare_view without passing project_root directly to test path recovery
    panel = render_compare_view(state)

    assert state.compare_run_model is not None, "Compare view failed to discover Run B"
    assert state.compare_run_model.run_id == "run-001"

    console = Console(width=100)
    with console.capture() as cap:
        console.print(panel)
    rendered = cap.get()

    assert "run-002" in rendered
    assert "run-001" in rendered
    assert "01_architect" in rendered


# ===========================================================================
# 8. Dashboard Selection Cursor Follows Execution
# ===========================================================================


def test_dashboard_selection_follows_active_stage_and_preserves_manual():
    """Verify selection cursor follows Critic -> Architect -> Planner, and preserves manual navigation."""
    stages = [
        StageModel(stage_name="00_critic", role_name="critic", sequence_number=0, status="CRITIQUE_COMPLETE"),
        StageModel(stage_name="01_architect", role_name="architect", sequence_number=1, status="PENDING"),
        StageModel(stage_name="02_planner", role_name="planner", sequence_number=2, status="PENDING"),
        StageModel(stage_name="03_executor", role_name="executor", sequence_number=3, status="PENDING"),
    ]
    run = RunModel(
        run_id="run-follow",
        task="Follow test",
        status="RUNNING",
        created_at="",
        run_dir=Path("/tmp/run-follow"),
        stages=stages,
    )
    state = DashboardState(run=run)

    # 1. Critic active
    run.active_stage_name = "00_critic"
    state.follow_active_stage()
    assert state.selected_stage_index == 0
    assert state.current_stage.stage_name == "00_critic"

    # 2. Architect becomes active
    run.active_stage_name = "01_architect"
    state.follow_active_stage()
    assert state.selected_stage_index == 1
    assert state.current_stage.stage_name == "01_architect"

    # 3. Planner becomes active
    run.active_stage_name = "02_planner"
    state.follow_active_stage()
    assert state.selected_stage_index == 2
    assert state.current_stage.stage_name == "02_planner"

    # 4. User manually navigates back to Architect
    state.select_prev_stage()
    assert state.selected_stage_index == 1
    assert state.user_has_selected_stage is True

    # 5. Subsequent stage transitions do NOT overwrite intentional manual navigation
    run.active_stage_name = "03_executor"
    state.follow_active_stage()
    assert state.selected_stage_index == 1
    assert state.current_stage.stage_name == "01_architect"


# ===========================================================================
# 9. Narrow Width Scalability
# ===========================================================================


def test_narrow_terminal_width_navigation_degradation():
    """Verify tab navigation degrades gracefully on narrow terminal widths."""
    run = RunModel(run_id="run-w", task="W", status="RUNNING", created_at="", run_dir=Path("/tmp/w"), stages=[])
    state = DashboardState(run=run, active_tab=2)

    # Standard width
    full_nav = render_tab_navigation(state, width=100)
    assert "1: Console" in full_nav
    assert "2: Artifact" in full_nav

    # Narrow width (< 60)
    narrow_nav = render_tab_navigation(state, width=50)
    assert "1:Con" in narrow_nav
    assert "2:Art" in narrow_nav

    # Very narrow width (< 42)
    tiny_nav = render_tab_navigation(state, width=30)
    assert "[1]" in tiny_nav or "[bold cyan][2][/]" in tiny_nav


def test_stream_exit_with_code_one_and_session_id_enters_recovery():
    """Verify premature CLI process termination with exit code 1 and valid session ID is eligible for daemon recovery."""
    adapter = OpenCodeAdapter()
    
    # Stream with session ID but premature EOF where proc exits with code 1
    stream = ['{"type": "init", "sessionID": "ses_abc123"}\n']
    events = list(
        adapter._decode_stream_events(
            stream,
            start_time=time.time(),
            get_returncode=lambda: 1,
            get_stderr=lambda: "",
        )
    )
    assert len(events) == 1
    err_ev = events[0]
    assert err_ev.event_type == AgentEventType.ERROR
    assert adapter.can_recover_session(terminal_event=err_ev)


# ===========================================================================
# 10. OpenCode Daemon Session Cancellation & Timeout Lifecycle
# ===========================================================================


def test_opencode_cancel_with_valid_session_id_interrupts_daemon_and_cleans_local_procs():
    """Verify cancel() with a valid session ID sends POST interrupt to daemon AND calls super().cancel()."""
    adapter = OpenCodeAdapter()
    adapter._session_id = "ses_active_123"

    mock_proc = MagicMock()
    mock_proc.pid = 9991
    mock_proc.poll.return_value = None
    adapter._register_proc(mock_proc, instance=adapter)

    with patch.object(adapter, "_query_daemon_api", return_value={"interrupted": True}) as mock_api, \
         patch.object(OpenCodeAdapter, "_kill_process_group") as mock_kill_pg:

        adapter.cancel()

        mock_api.assert_called_once_with(
            "/api/session/ses_active_123/interrupt",
            method="POST",
            cwd=None,
            timeout=5.0,
        )
        mock_kill_pg.assert_called_once_with(mock_proc)

    assert mock_proc not in adapter._active_procs
    assert adapter._cancel_requested is True


def test_opencode_cancel_without_session_id_skips_daemon_and_cleans_local_procs():
    """Verify cancel() without session ID skips daemon API query and cleans local procs."""
    adapter = OpenCodeAdapter()
    adapter._session_id = None

    mock_proc = MagicMock()
    mock_proc.pid = 9992
    mock_proc.poll.return_value = None
    adapter._register_proc(mock_proc, instance=adapter)

    with patch.object(adapter, "_query_daemon_api") as mock_api, \
         patch.object(OpenCodeAdapter, "_kill_process_group") as mock_kill_pg:

        adapter.cancel()

        mock_api.assert_not_called()
        mock_kill_pg.assert_called_once_with(mock_proc)

    assert mock_proc not in adapter._active_procs
    assert adapter._cancel_requested is True


def test_opencode_cancel_handles_daemon_api_errors_and_still_cleans_local_procs():
    """Verify daemon API errors/exceptions do not prevent local process cleanup."""
    adapter = OpenCodeAdapter()
    adapter._session_id = "ses_error_123"

    mock_proc = MagicMock()
    mock_proc.pid = 9993
    mock_proc.poll.return_value = None
    adapter._register_proc(mock_proc, instance=adapter)

    with patch.object(adapter, "_query_daemon_api", side_effect=RuntimeError("Daemon unreachable")) as mock_api, \
         patch.object(OpenCodeAdapter, "_kill_process_group") as mock_kill_pg:

        # Should not raise exception
        adapter.cancel()

        mock_api.assert_called_once()
        mock_kill_pg.assert_called_once_with(mock_proc)

    assert mock_proc not in adapter._active_procs
    assert adapter._cancel_requested is True


def test_opencode_cancel_is_idempotent():
    """Verify calling cancel() multiple times issues daemon interrupt only once."""
    adapter = OpenCodeAdapter()
    adapter._session_id = "ses_idem_123"

    mock_proc = MagicMock()
    mock_proc.pid = 9994
    mock_proc.poll.return_value = None
    adapter._register_proc(mock_proc, instance=adapter)

    with patch.object(adapter, "_query_daemon_api", return_value={"interrupted": True}) as mock_api, \
         patch.object(OpenCodeAdapter, "_kill_process_group"):

        adapter.cancel()
        adapter.cancel()

        assert mock_api.call_count == 1

    assert adapter._cancel_requested is True


def test_stage_timeout_triggers_both_daemon_interrupt_and_local_process_cleanup(tmp_path):
    """Verify Stage timeout invokes OpenCodeAdapter.cancel(), interrupting daemon session and killing local proc."""
    from forge.stages.stage import Stage
    from forge.core.role import Role
    from forge.core.context import Context
    from forge.core.config import Config
    from forge.core.git import GitService
    from forge.storage.run_manager import RunManager
    from forge.adapters.base import AdapterResponse

    fake_bin = tmp_path / "opencode"
    fake_bin.write_text("#!/bin/sh\nsleep 30\n")
    fake_bin.chmod(0o755)

    adapter = OpenCodeAdapter(auto_approve=True)
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run("test stage timeout cancel")
    role = Role(name="executor", sequence_number=3, template_content="Prompt", phase="execution")
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr, timeout=0.3)

    # Simulate adapter capturing session ID from OpenCode during startup
    def fake_iter_events(prompt, cwd=None, timeout=None):
        adapter._session_id = "ses_stage_timeout_456"
        adapter._current_cwd = cwd or tmp_path
        import subprocess
        proc = subprocess.Popen([str(fake_bin)], cwd=cwd or tmp_path, start_new_session=True)
        adapter._register_proc(proc, instance=adapter)
        try:
            while not getattr(adapter, "_cancel_requested", False):
                time.sleep(0.02)
            yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="working")
        finally:
            adapter._unregister_proc(proc, instance=adapter)
            proc.kill()

    with patch.object(adapter, "iter_events", side_effect=fake_iter_events), \
         patch.object(adapter, "_query_daemon_api", return_value={"interrupted": True}) as mock_api:

        start = time.time()
        result = stage.run(context)
        elapsed = time.time() - start

    assert result.response.exit_code == 124
    assert result.success is False
    assert elapsed < 3.0
    mock_api.assert_called_once_with(
        "/api/session/ses_stage_timeout_456/interrupt",
        method="POST",
        cwd=tmp_path,
        timeout=5.0,
    )
    assert len(adapter._active_procs) == 0


def test_stage_premature_eof_does_not_interrupt_daemon_session_allowing_recovery(tmp_path):
    """Verify unexpected EOF before terminal event does NOT cancel daemon session, allowing recovery."""
    from forge.stages.stage import Stage
    from forge.core.role import Role
    from forge.core.context import Context
    from forge.core.config import Config
    from forge.core.git import GitService
    from forge.storage.run_manager import RunManager

    adapter = OpenCodeAdapter()
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run("test eof recovery preservation")
    role = Role(name="planner", sequence_number=2, template_content="Prompt", phase="planning")
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr, timeout=10.0)

    # Generator simulates premature EOF: yields one chunk, sets session ID, then generator returns cleanly
    def fake_iter_events(prompt, cwd=None, timeout=None):
        adapter._session_id = "ses_recover_eof_789"
        adapter._current_cwd = cwd or tmp_path
        yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Plan step 1")

    recovered_text = "```yaml\nstatus: READY\nhandoff: EXECUTOR\nnext_action: EXECUTE\n```\nPlan complete"
    recovered_event = AgentEvent(
        event_type=AgentEventType.COMPLETE,
        timestamp=time.time(),
        text=recovered_text,
        result=ExecutionResult(exit_code=0, duration_seconds=5.0, stdout=recovered_text, stderr=""),
    )
    recovered_response = AdapterResponse(stdout=recovered_text, stderr="", exit_code=0, duration_seconds=5.0, raw_output=recovered_text)

    with patch.object(adapter, "iter_events", side_effect=fake_iter_events), \
         patch.object(adapter, "_query_daemon_api") as mock_api, \
         patch.object(adapter, "can_recover_session", return_value=True), \
         patch.object(adapter, "recover_session", return_value=(recovered_event, recovered_response)) as mock_recover:

        result = stage.run(context)

    # Interrupt MUST NOT have been called on premature EOF
    mock_api.assert_not_called()
    # Recovery was executed successfully
    mock_recover.assert_called_once()
    assert result.success is True

