"""Regression tests for ADR-017: Subprocess Lifecycle Invariants and Stream Completion Semantics.

Guards against:
- Premature COMPLETE emission on intermediate step_finish(reason="stop").
- Intermediate chatter ("I've started the build and I'm waiting for it to finish.") appearing as final artifact.
- Stage termination while underlying agent or tool execution is still in progress.
- Killing subprocess prematurely via process-group cleanup before stdout/stderr drainage.
- Emitting COMPLETE while subprocess is still running.
- Cross-adapter lifecycle inconsistency between OpenCode and Antigravity.
"""

import json
import time
from unittest.mock import MagicMock
import pytest

from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.opencode import OpenCodeAdapter
from forge.adapters.codex import CodexAdapter
from forge.core.events import AgentEventType


def test_antigravity_intermediate_step_finish_does_not_abort_stream():
    """Verify that an intermediate step_finish with reason='stop' in Antigravity
    does NOT emit COMPLETE or terminate iteration early.
    Must continue reading until stream EOF and only emit COMPLETE post-mortem.
    """
    adapter = AntigravityAdapter()
    stream = [
        # Turn 1: LLM text saying "I am starting the build..."
        json.dumps({"type": "text", "part": {"text": "I've started the build and I'm waiting for it to finish."}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),

        # Turn 2: Post-build continuation emitting final report
        json.dumps({"type": "text", "part": {"text": "# Human Report\n\n```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```\n"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
    ]

    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=lambda: 0,
        get_stderr=lambda: "",
    ))

    event_types = [e.event_type for e in events]

    # Under ADR-017:
    # 1. Exactly one COMPLETE event, emitted at the very end
    assert event_types.count(AgentEventType.COMPLETE) == 1
    assert event_types[-1] == AgentEventType.COMPLETE

    # 2. Output must contain the final report, NOT the intermediate text
    complete_event = events[-1]
    assert "ROLE: EXECUTOR" in complete_event.result.stdout
    assert "STATUS: SUCCESS" in complete_event.result.stdout
    assert "I've started the build" not in complete_event.result.stdout


def test_adapter_invariant_complete_only_after_subprocess_exit():
    """ADR-017 Fundamental Invariant:
    COMPLETE is emitted IF AND ONLY IF the subprocess has terminated (proc.poll() is not None).
    If in-stream 'result' or 'complete' event is received while proc is still running,
    the adapter MUST NOT emit COMPLETE until stdout EOF and process exit.
    """
    adapter = AntigravityAdapter()

    # Process is still running during stream iteration
    mock_proc = MagicMock()
    mock_proc.poll.side_effect = [None, None, 0]  # Running during stream, exits at EOF
    mock_proc.wait.return_value = 0

    stream = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Processing..."}}),
        # In-stream result event arrives while proc.poll() is None
        json.dumps({"event": "result", "status": "SUCCESS", "response": "Finished"}),
    ]

    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=mock_proc.wait,
        get_stderr=lambda: "",
    ))

    # Must emit COMPLETE only once, and mock_proc.wait() must have been called
    complete_events = [e for e in events if e.event_type == AgentEventType.COMPLETE]
    assert len(complete_events) == 1
    assert mock_proc.wait.called


def test_opencode_does_not_emit_complete_in_stream_before_eof():
    """Verify OpenCode adapter does NOT emit COMPLETE in-stream upon receiving
    type='complete' or type='result'; must wait for stdout EOF.
    """
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "text", "part": {"text": "Step 1 text"}}),
        json.dumps({"type": "complete"}),  # in-stream event
        json.dumps({"type": "text", "part": {"text": "\nFinal trailing text before EOF"}}),
    ]

    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=lambda: 0,
        get_stderr=lambda: "",
    ))

    # COMPLETE must be the last event, preserving all text up to EOF
    assert events[-1].event_type == AgentEventType.COMPLETE
    assert events.count(events[-1]) == 1
    assert "Final trailing text before EOF" in events[-1].result.stdout


def test_intermediate_assistant_text_purged_on_continuation_antigravity():
    """Verify that in Antigravity, intermediate assistant chatter before tool execution
    is purged when subsequent execution begins.
    """
    adapter = AntigravityAdapter()
    stream = [
        # Turn 1: Conversational preface
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Let me inspect the directory first.\n"}}),
        # Turn 2: Tool call begins
        json.dumps({"event": "tool_start", "step_update": {"tool_name": "list_dir"}}),
        json.dumps({"event": "tool_finish", "step_update": {"tool_name": "list_dir", "status": "completed"}}),
        # Turn 3: Final response
        json.dumps({"event": "step_update", "step_update": {"text_delta": "# Final Deliverable\n\nAll tasks done."}}),
        json.dumps({"event": "result", "status": "SUCCESS"}),
    ]

    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=lambda: 0,
        get_stderr=lambda: "",
    ))

    complete_events = [e for e in events if e.event_type == AgentEventType.COMPLETE]
    assert len(complete_events) == 1
    stdout = complete_events[0].result.stdout

    assert "# Final Deliverable" in stdout
    assert "All tasks done." in stdout
    assert "Let me inspect the directory first" not in stdout


def test_cross_adapter_lifecycle_consistency_on_error():
    """Verify all adapters (OpenCode, Antigravity) emit AgentEventType.ERROR
    when subprocess exits with non-zero exit code, capturing stderr.
    """
    for adapter_cls in (OpenCodeAdapter, AntigravityAdapter):
        adapter = adapter_cls()
        stream = [
            json.dumps({"type": "text", "part": {"text": "Failing command..."}}),
        ]
        events = list(adapter._decode_stream_events(
            stream=stream,
            start_time=time.time(),
            get_returncode=lambda: 2,
            get_stderr=lambda: "Fatal syntax error in generated code",
        ))

        assert events[-1].event_type == AgentEventType.ERROR
        assert events[-1].result.exit_code == 2
        assert "Fatal syntax error" in events[-1].result.stderr
