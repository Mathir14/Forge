"""Regression test suite for TUI Console streaming text fragmentation,
multi-line preservation, stream buffer boundaries, and natural word wrapping.
"""

from pathlib import Path
import time
from rich.console import Console

from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.dashboard.app import DashboardApp
from forge.dashboard.components.artifact_view import render_artifact_view
from forge.dashboard.components.console_view import render_console_view
from forge.dashboard.model import RunModel, StageModel
from forge.dashboard.state import DashboardState


def test_token_fragmentation_produces_coalesced_lines(tmp_path: Path):
    """Verify streaming token deltas are coalesced into complete lines, not 1-word lines."""
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=tmp_path)
    stage = StageModel(stage_name="01_architect", role_name="architect", sequence_number=1, status="RUNNING")
    model.stages.append(stage)
    model.active_stage_name = "01_architect"

    chunks = ["The ", "quick ", "brown ", "fox\n", "jumps ", "over\n"]
    for c in chunks:
        model.apply_event(AgentEvent(
            event_type=AgentEventType.CHUNK,
            timestamp=time.time(),
            text=c,
            data={"stage_name": "01_architect"},
        ))

    assert model.console_logs == [
        "  The quick brown fox",
        "  jumps over",
    ]


def test_multi_line_chunks_preserves_all_lines(tmp_path: Path):
    """Verify multi-line chunks preserve every line and do not discard lines with splitlines()[0]."""
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=tmp_path)
    stage = StageModel(stage_name="01_architect", role_name="architect", sequence_number=1, status="RUNNING")
    model.stages.append(stage)
    model.active_stage_name = "01_architect"

    # Multi-line chunk ending with newline
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Line 1\nLine 2\nLine 3\n",
        data={"stage_name": "01_architect"},
    ))

    assert model.console_logs == [
        "  Line 1",
        "  Line 2",
        "  Line 3",
    ]

    # Multi-line chunk with incomplete trailing fragment
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Alpha\nBeta\nGamma incomplete",
        data={"stage_name": "01_architect"},
    ))

    assert model.console_logs == [
        "  Line 1",
        "  Line 2",
        "  Line 3",
        "  Alpha",
        "  Beta",
    ]

    # Subsequent chunk completes the trailing fragment
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text=" completed successfully\n",
        data={"stage_name": "01_architect"},
    ))

    assert model.console_logs == [
        "  Line 1",
        "  Line 2",
        "  Line 3",
        "  Alpha",
        "  Beta",
        "  Gamma incomplete completed successfully",
    ]


def test_incomplete_fragments_retained_until_newline(tmp_path: Path):
    """Verify incomplete fragments are retained in stream buffer until a newline arrives."""
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=tmp_path)
    stage = StageModel(stage_name="01_architect", role_name="architect", sequence_number=1, status="RUNNING")
    model.stages.append(stage)
    model.active_stage_name = "01_architect"

    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Designing ",
        data={"stage_name": "01_architect"},
    ))
    assert len(model.console_logs) == 0

    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="database schema ",
        data={"stage_name": "01_architect"},
    ))
    assert len(model.console_logs) == 0

    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="for auth service.\n",
        data={"stage_name": "01_architect"},
    ))
    assert model.console_logs == ["  Designing database schema for auth service."]


def test_interleaved_events_flush_pending_narration_chronologically(tmp_path: Path):
    """Verify pending stream text is flushed BEFORE interleaved tool events to maintain order."""
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=tmp_path)
    stage = StageModel(stage_name="01_architect", role_name="architect", sequence_number=1, status="RUNNING")
    model.stages.append(stage)
    model.active_stage_name = "01_architect"

    # 1. Narration without newline followed by TOOL_START
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Inspecting current directory contents",
        data={"stage_name": "01_architect"},
    ))
    assert len(model.console_logs) == 0  # not emitted yet

    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_START,
        timestamp=time.time(),
        text="shell",
        data={"stage_name": "01_architect", "tool": "shell", "input": {"command": "ls -la"}},
    ))

    # Narration MUST appear before TOOL_START
    assert len(model.console_logs) == 2
    assert model.console_logs[0] == "  Inspecting current directory contents"
    assert "TOOL_START: shell" in model.console_logs[1]

    # 2. Narration without newline followed by TOOL_FINISH
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Analyzing output files",
        data={"stage_name": "01_architect"},
    ))
    assert len(model.console_logs) == 2  # pending in buffer

    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        text="shell",
        data={"stage_name": "01_architect", "tool": "shell"},
    ))

    # Narration MUST appear before TOOL_FINISH
    assert len(model.console_logs) == 4
    assert model.console_logs[2] == "  Analyzing output files"
    assert "TOOL_FINISH: shell" in model.console_logs[3]


def test_lifecycle_boundaries_flush_and_prevent_leakage(tmp_path: Path):
    """Verify pending text is flushed on stage completion and error, and does not leak to other stages."""
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=tmp_path)
    stage1 = StageModel(stage_name="01_architect", role_name="architect", sequence_number=1, status="RUNNING")
    stage2 = StageModel(stage_name="02_planner", role_name="planner", sequence_number=2, status="PENDING")
    model.stages.extend([stage1, stage2])
    model.active_stage_name = "01_architect"

    # Stage 1 emits narration without trailing newline
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Final conclusion: architecture approved",
        data={"stage_name": "01_architect"},
    ))

    # Stage 1 finishes with COMPLETE
    model.apply_event(AgentEvent(
        event_type=AgentEventType.COMPLETE,
        timestamp=time.time(),
        result=ExecutionResult(exit_code=0, duration_seconds=5.0, stdout="Done"),
        data={"stage_name": "01_architect"},
    ))

    # Pending narration must be flushed before COMPLETE log
    assert len(model.console_logs) == 2
    assert model.console_logs[0] == "  Final conclusion: architecture approved"
    assert "COMPLETE" in model.console_logs[1]

    # Stage 2 starts
    model.apply_event(AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={"lifecycle": "stage_start", "stage_name": "02_planner", "sequence_number": 2},
    ))
    assert "Stage execution started" in model.console_logs[2]

    # Stage 2 streams its own text
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Planner step 1\n",
        data={"stage_name": "02_planner"},
    ))
    assert model.console_logs[3] == "  Planner step 1"

    # Terminal ERROR event also flushes pending buffer
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Encountered unrecoverable syntax issue",
        data={"stage_name": "02_planner"},
    ))
    model.apply_event(AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="Compilation failed",
        data={"stage_name": "02_planner"},
    ))

    assert model.console_logs[-2] == "  Encountered unrecoverable syntax issue"
    assert "ERROR: Compilation failed" in model.console_logs[-1]


def test_rendering_width_wraps_long_narration(tmp_path: Path):
    """Verify long narration lines wrap naturally across the available Console pane width."""
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=tmp_path)
    stage = StageModel(stage_name="01_architect", role_name="architect", sequence_number=1, status="RUNNING")
    model.stages.append(stage)
    model.active_stage_name = "01_architect"

    # Stream a paragraph of ~300 characters across multiple chunks
    long_sentence = (
        "The authentication subsystem has been audited and validated against the ADR-016 protocol specifications. "
        "All candidate token schemas were verified, and unit test suites confirmed backward compatibility across all "
        "supported operating system environments including Linux, macOS, and Windows under PowerShell and WSL."
    )
    tokens = [word + " " for word in long_sentence.split()]
    tokens[-1] = tokens[-1].rstrip() + "\n"

    for tok in tokens:
        model.apply_event(AgentEvent(
            event_type=AgentEventType.CHUNK,
            timestamp=time.time(),
            text=tok,
            data={"stage_name": "01_architect"},
        ))

    # The entire paragraph is coalesced into 1 log entry in console_logs
    assert len(model.console_logs) == 1
    assert model.console_logs[0] == f"  {long_sentence}"

    state = DashboardState(run=model, active_tab=1, focused_pane="content")
    panel = render_console_view(state)

    # Render at width 80
    console_80 = Console(width=80)
    with console_80.capture() as cap_80:
        console_80.print(panel)
    output_80 = cap_80.get()

    # Verify output utilizes full width (> 60 chars per line in box content) and wraps across multiple lines
    content_lines_80 = [line for line in output_80.splitlines() if "│" in line and "Console" not in line and "─" not in line]
    assert len(content_lines_80) >= 4
    for line in content_lines_80[:-1]:  # all lines except the last line of the paragraph
        assert len(line.strip()) >= 70

    # Render at width 120
    console_120 = Console(width=120)
    with console_120.capture() as cap_120:
        console_120.print(panel)
    output_120 = cap_120.get()
    content_lines_120 = [line for line in output_120.splitlines() if "│" in line and "Console" not in line and "─" not in line]
    # Wider console wraps into fewer lines than narrower console
    assert len(content_lines_120) < len(content_lines_80)


def test_whitespace_and_consecutive_newlines_handling(tmp_path: Path):
    """Verify empty chunks, whitespace chunks, and multiple newlines do not create duplicate blank lines."""
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=tmp_path)
    stage = StageModel(stage_name="01_architect", role_name="architect", sequence_number=1, status="RUNNING")
    model.stages.append(stage)
    model.active_stage_name = "01_architect"

    # Empty chunk
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="",
        data={"stage_name": "01_architect"},
    ))
    assert len(model.console_logs) == 0

    # Text followed by 4 consecutive newlines
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Section 1\n\n\n\nSection 2\n",
        data={"stage_name": "01_architect"},
    ))

    # At most one blank line between Section 1 and Section 2
    assert model.console_logs == [
        "  Section 1",
        "",
        "  Section 2",
    ]


def test_existing_tabs_and_operational_logs_unaffected(tmp_path: Path):
    """Verify operational logs, tool events, and Artifact tab markdown rendering remain unaffected."""
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=tmp_path)
    stage = StageModel(
        stage_name="01_architect",
        role_name="architect",
        sequence_number=1,
        status="RUNNING",
        raw_content="# Architecture\nDetailed plan content.",
        human_report="# Architecture\nDetailed plan content.",
    )
    model.stages.append(stage)
    model.active_stage_name = "01_architect"

    # Operational events
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_START,
        timestamp=time.time(),
        text="read",
        data={"stage_name": "01_architect", "tool": "read", "input": {"file": "app.py"}},
    ))
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        text="read",
        data={"stage_name": "01_architect", "tool": "read"},
    ))

    assert any("⚡ TOOL_START: read" in log for log in model.console_logs)
    assert any("✔ TOOL_FINISH: read" in log for log in model.console_logs)

    # Artifact tab rendering
    state = DashboardState(run=model, active_tab=2, focused_pane="content")
    panel_art = render_artifact_view(state)
    assert panel_art is not None

    console = Console(width=80)
    with console.capture() as cap:
        console.print(panel_art)
    art_output = cap.get()
    assert "Architecture" in art_output
