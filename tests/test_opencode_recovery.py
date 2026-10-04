"""Comprehensive test suite for OpenCode session recovery mechanism."""

import json
import subprocess
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.base import AdapterResponse
from forge.adapters.opencode import OpenCodeAdapter, is_recoverable_opencode_error
from forge.core.config import Config
from forge.core.context import Context
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.core.git import GitService
from forge.core.role import Role
from forge.storage.run_manager import RunManager
from forge.stages.stage import Stage


@pytest.fixture(autouse=True)
def reset_opencode_cache():
    OpenCodeAdapter._clear_variants_cache()
    yield
    OpenCodeAdapter._clear_variants_cache()


def _create_critic_role() -> Role:
    return Role(
        name="critic",
        sequence_number=1,
        template_content="You are critic.",
        protocol_content="Emit machine report.",
    )


def _create_test_context(tmp_path: Path) -> Context:
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Run codebase audit")
    return Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )


# ===========================================================================
# 1. Error Classification Tests (is_recoverable_opencode_error)
# ===========================================================================

def test_is_recoverable_with_provider_transport_dict():
    """Verify provider.transport dict structure is classified as recoverable."""
    err_dict = {
        "type": "provider.transport",
        "message": "Connection lost while reading the response: ECONNRESET: The socket connection was closed unexpectedly.",
        "status": 200,
    }
    assert is_recoverable_opencode_error(err_dict) is True


def test_is_recoverable_with_transport_strings():
    """Verify standard network and socket transport error strings are classified as recoverable."""
    assert is_recoverable_opencode_error("ECONNRESET: connection reset by peer") is True
    assert is_recoverable_opencode_error("ECONNREFUSED 127.0.0.1:49374") is True
    assert is_recoverable_opencode_error("ETIMEDOUT: Connection timed out") is True
    assert is_recoverable_opencode_error("EPIPE: Broken pipe") is True
    assert is_recoverable_opencode_error("Connection lost while reading the response") is True
    assert is_recoverable_opencode_error("The socket connection was closed unexpectedly.") is True
    assert is_recoverable_opencode_error("Unexpected EOF before terminal event.") is True
    assert is_recoverable_opencode_error("socket hang up") is True


def test_is_recoverable_with_agent_event_and_response():
    """Verify AgentEvent and AdapterResponse objects are properly inspected."""
    event = AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="Connection lost while reading the response: ECONNRESET",
        result=ExecutionResult(
            exit_code=1,
            duration_seconds=184.3,
            stderr="ECONNRESET: The socket connection was closed unexpectedly.",
            metadata={"error_type": "provider.transport", "session_id": "ses_123"},
        ),
    )
    assert is_recoverable_opencode_error(event) is True

    resp = AdapterResponse(
        stdout="",
        stderr="provider.transport ECONNRESET",
        exit_code=1,
        duration_seconds=10.0,
        raw_output="provider.transport ECONNRESET",
    )
    assert is_recoverable_opencode_error(resp) is True


def test_non_recoverable_errors_rejected():
    """Verify authentication, quota, user interrupt, timeout, and generic errors are rejected."""
    # User cancellation (SIGINT exit 130)
    sigint_event = AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="Execution interrupted by user (SIGINT).",
        result=ExecutionResult(exit_code=130, duration_seconds=1.0, stderr="SIGINT"),
    )
    assert is_recoverable_opencode_error(sigint_event) is False

    # Forge execution timeout (exit 124)
    timeout_event = AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="Execution timed out after 300 seconds.",
        result=ExecutionResult(exit_code=124, duration_seconds=300.0, stderr="Timeout"),
    )
    assert is_recoverable_opencode_error(timeout_event) is False

    # Authentication failure
    assert is_recoverable_opencode_error("401 Unauthorized: Invalid API key") is False
    assert is_recoverable_opencode_error("auth_error: invalid_api_key") is False

    # Rate limits / quota
    assert is_recoverable_opencode_error("Rate limit exceeded: 429 Too Many Requests") is False
    assert is_recoverable_opencode_error("insufficient_quota") is False

    # Context length exceeded
    assert is_recoverable_opencode_error("context_length_exceeded: maximum context length is 128k") is False

    # Generic exit 1 without transport evidence
    assert is_recoverable_opencode_error("Process exited with code 1") is False
    assert is_recoverable_opencode_error("Tool execution failed: file not found") is False
    assert is_recoverable_opencode_error("") is False
    assert is_recoverable_opencode_error(None) is False


# ===========================================================================
# 2. Session ID Capture Tests
# ===========================================================================

def test_session_id_captured_from_events():
    """Verify session ID is captured across stream events and stored on adapter."""
    adapter = OpenCodeAdapter()
    assert adapter.session_id is None

    stream = [
        json.dumps({"type": "init", "sessionID": "ses_captured_001"}),
        json.dumps({"type": "text", "part": {"text": "Analyzing..."}}),
        json.dumps({
            "type": "error",
            "error": {
                "type": "provider.transport",
                "message": "ECONNRESET",
            },
            "sessionID": "ses_captured_001",
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert adapter.session_id == "ses_captured_001"
    assert len(events) == 2  # CHUNK and ERROR

    error_ev = events[1]
    assert error_ev.event_type == AgentEventType.ERROR
    assert error_ev.result.metadata.get("session_id") == "ses_captured_001"
    assert error_ev.result.metadata.get("error_type") == "provider.transport"


def test_session_id_reset_between_invocations():
    """Verify session_id is reset on each execute / iter_events call."""
    adapter = OpenCodeAdapter()
    adapter._session_id = "stale_session_123"

    with patch.object(adapter, "_resolve_binary", return_value=("/bin/opencode", None)), \
         patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter([
            json.dumps({"type": "init", "sessionID": "ses_fresh_456"}),
            json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
        ])
        mock_proc.stderr = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        events = list(adapter.iter_events("test prompt"))
        assert adapter.session_id == "ses_fresh_456"


# ===========================================================================
# 3. can_recover_session Capability Tests
# ===========================================================================

def test_can_recover_session_checks():
    """Verify can_recover_session conditions."""
    adapter = OpenCodeAdapter()

    # Case 1: No session ID -> False
    event_no_session = AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="provider.transport ECONNRESET",
        result=ExecutionResult(exit_code=1, duration_seconds=1.0, stderr="ECONNRESET"),
    )
    assert adapter.can_recover_session(event_no_session) is False

    # Case 2: Session ID present + recoverable error -> True
    adapter._session_id = "ses_rec_123"
    assert adapter.can_recover_session(event_no_session) is True

    # Case 3: Session ID in event metadata -> True even if adapter._session_id not set
    adapter._session_id = None
    event_with_meta = AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="provider.transport ECONNRESET",
        result=ExecutionResult(
            exit_code=1,
            duration_seconds=1.0,
            stderr="ECONNRESET",
            metadata={"session_id": "ses_rec_456"},
        ),
    )
    assert adapter.can_recover_session(event_with_meta) is True

    # Case 4: Non-recoverable error with session ID -> False
    auth_event = AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="401 Unauthorized",
        result=ExecutionResult(
            exit_code=1,
            duration_seconds=1.0,
            stderr="Unauthorized",
            metadata={"session_id": "ses_rec_456"},
        ),
    )
    assert adapter.can_recover_session(auth_event) is False

    # Case 5: COMPLETE event -> False (already complete)
    complete_event = AgentEvent(
        event_type=AgentEventType.COMPLETE,
        timestamp=time.time(),
        text="All good",
        result=ExecutionResult(exit_code=0, duration_seconds=1.0, stdout="Done", metadata={"session_id": "ses_rec_456"}),
    )
    assert adapter.can_recover_session(complete_event) is False


# ===========================================================================
# 4. Polling & Message Retrieval Tests
# ===========================================================================

def test_recover_session_polls_and_succeeds(tmp_path):
    """Verify recovery polls daemon until succeeded, then extracts multi-block message."""
    adapter = OpenCodeAdapter(extra_flags={"recovery_timeout": 10.0, "recovery_poll_interval": 0.1})
    adapter._session_id = "ses_poll_test"

    session_query_count = 0

    def mock_query_daemon_api(endpoint, method="GET", cwd=None, timeout=10.0):
        nonlocal session_query_count
        if endpoint == "/api/session/ses_poll_test":
            session_query_count += 1
            if session_query_count == 1:
                # First poll: still running
                return {"data": {"id": "ses_poll_test", "outcome": None}}
            else:
                # Second poll: succeeded
                return {
                    "data": {
                        "id": "ses_poll_test",
                        "outcome": "succeeded",
                        "tokens": {"input": 1500, "output": 500},
                        "time": {"created": 1000000, "idle": 1025000},
                    }
                }
        elif "message" in endpoint:
            # Final message query with multiple content blocks
            return {
                "data": [
                    {
                        "id": "msg_recovered_001",
                        "content": [
                            {"type": "text", "text": "Audit part 1.\n"},
                            {"type": "text", "text": "```yaml\nROLE: CRITIC\nSTATUS: APPROVED\n```"},
                        ],
                        "tokens": {"input": 1500, "output": 500},
                    }
                ]
            }
        return None

    with patch.object(adapter, "_resolve_binary", return_value=("/bin/opencode", None)), \
         patch.object(adapter, "_query_daemon_api", side_effect=mock_query_daemon_api):

        recovered = adapter.recover_session(session_id="ses_poll_test", cwd=tmp_path)
        assert recovered is not None
        rec_event, rec_resp = recovered

        assert session_query_count == 2
        assert rec_event.event_type == AgentEventType.COMPLETE
        assert rec_event.result.exit_code == 0
        assert "Audit part 1." in rec_event.result.stdout
        assert "STATUS: APPROVED" in rec_event.result.stdout
        assert rec_event.result.metadata.get("recovered") is True
        assert rec_event.result.metadata.get("recovered_message_id") == "msg_recovered_001"
        assert rec_event.result.token_usage == {"input": 1500, "output": 500}
        assert rec_resp.exit_code == 0
        assert rec_resp.stdout == rec_event.result.stdout


def test_recover_session_daemon_reported_failure(tmp_path):
    """Verify recovery returns None when daemon reports outcome as failed."""
    adapter = OpenCodeAdapter(extra_flags={"recovery_timeout": 5.0, "recovery_poll_interval": 0.1})

    def mock_query(endpoint, method="GET", cwd=None, timeout=10.0):
        return {"data": {"id": "ses_failed", "outcome": "failed"}}

    with patch.object(adapter, "_resolve_binary", return_value=("/bin/opencode", None)), \
         patch.object(adapter, "_query_daemon_api", side_effect=mock_query):

        orig_event = AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text="ECONNRESET",
            result=ExecutionResult(exit_code=1, duration_seconds=5.0, stderr="ECONNRESET"),
        )
        recovered = adapter.recover_session(terminal_event=orig_event, session_id="ses_failed", cwd=tmp_path)
        assert recovered is None
        assert orig_event.result.metadata.get("recovery_failed") is True
        assert "failed" in orig_event.result.metadata.get("recovery_failure_reason", "").lower()


def test_recover_session_timeout(tmp_path):
    """Verify recovery returns None and marks timeout when daemon does not complete within recovery_timeout."""
    adapter = OpenCodeAdapter(extra_flags={"recovery_timeout": 0.3, "recovery_poll_interval": 0.1})

    def mock_query(endpoint, method="GET", cwd=None, timeout=10.0):
        # Daemon session keeps running
        return {"data": {"id": "ses_slow", "outcome": None}}

    with patch.object(adapter, "_resolve_binary", return_value=("/bin/opencode", None)), \
         patch.object(adapter, "_query_daemon_api", side_effect=mock_query):

        orig_event = AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text="ECONNRESET",
            result=ExecutionResult(exit_code=1, duration_seconds=5.0, stderr="ECONNRESET"),
        )
        recovered = adapter.recover_session(terminal_event=orig_event, session_id="ses_slow", cwd=tmp_path)
        assert recovered is None
        assert orig_event.result.metadata.get("recovery_failed") is True
        assert "timeout" in orig_event.result.metadata.get("recovery_failure_reason", "").lower()


def test_recover_session_cancellation(tmp_path):
    """Verify recovery aborts immediately when cancel() is requested."""
    adapter = OpenCodeAdapter(extra_flags={"recovery_timeout": 10.0, "recovery_poll_interval": 0.5})

    def mock_query(endpoint, method="GET", cwd=None, timeout=10.0):
        # Simulate cancellation during query
        adapter.cancel()
        return {"data": {"id": "ses_cancel", "outcome": None}}

    with patch.object(adapter, "_resolve_binary", return_value=("/bin/opencode", None)), \
         patch.object(adapter, "_query_daemon_api", side_effect=mock_query):

        orig_event = AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text="ECONNRESET",
            result=ExecutionResult(exit_code=1, duration_seconds=5.0, stderr="ECONNRESET"),
        )
        recovered = adapter.recover_session(terminal_event=orig_event, session_id="ses_cancel", cwd=tmp_path)
        assert recovered is None
        assert orig_event.result.metadata.get("recovery_failed") is True
        assert "cancelled" in orig_event.result.metadata.get("recovery_failure_reason", "").lower()


# ===========================================================================
# 5. Full Stage End-to-End Recovery Integration Tests
# ===========================================================================

def test_stage_recovers_from_transport_failure(tmp_path):
    """End-to-end integration test: OpenCode stream hits ECONNRESET, daemon succeeds, Stage marks COMPLETE."""
    context = _create_test_context(tmp_path)
    role = _create_critic_role()

    critic_yaml_report = """# Critic Security Audit
Found 2 critical issues.

```yaml
ROLE: CRITIC
STATUS: APPROVED
HANDOFF: ARCHITECT
```
"""

    adapter = OpenCodeAdapter(auto_approve=True, extra_flags={"recovery_timeout": 5.0, "recovery_poll_interval": 0.05})

    # 1. Stream emits partial CHUNK, then ECONNRESET error event with sessionID
    stream_events = [
        json.dumps({
            "type": "text",
            "sessionID": "ses_stage_e2e",
            "part": {"text": "Starting critic audit...\n"},
        }),
        json.dumps({
            "type": "error",
            "sessionID": "ses_stage_e2e",
            "error": {
                "type": "provider.transport",
                "message": "Connection lost while reading the response: ECONNRESET: The socket connection was closed unexpectedly.",
                "status": 200,
            },
        }),
    ]

    def mock_query_daemon_api(endpoint, method="GET", cwd=None, timeout=10.0):
        if endpoint == "/api/session/ses_stage_e2e":
            return {
                "data": {
                    "id": "ses_stage_e2e",
                    "outcome": "succeeded",
                    "tokens": {"input": 2000, "output": 800},
                }
            }
        elif "message" in endpoint:
            return {
                "data": [
                    {
                        "id": "msg_critic_final",
                        "content": [{"type": "text", "text": critic_yaml_report}],
                        "tokens": {"input": 2000, "output": 800},
                    }
                ]
            }
        return None

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter(stream_events)
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = ""
    mock_proc.poll.return_value = 1
    mock_proc.wait.return_value = 1
    captured_events: List[AgentEvent] = []
    context.event_listener = captured_events.append

    with patch.object(adapter, "_resolve_binary", return_value=("/bin/opencode", None)), \
         patch.object(adapter, "_query_daemon_api", side_effect=mock_query_daemon_api), \
         patch("forge.adapters.opencode.subprocess.Popen", return_value=mock_proc):

        stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))
        result = stage.run(context)

        # Stage should succeed despite CLI exiting 1 because daemon completed the session
        assert result.success is True
        assert result.response.exit_code == 0
        assert result.machine_report is not None
        assert result.machine_report.is_valid is True
        assert result.machine_report.status == "APPROVED"
        assert result.raw_markdown == critic_yaml_report
        assert len(captured_events) >= 1
        assert captured_events[-1].event_type == AgentEventType.COMPLETE
        assert captured_events[-1].result.metadata.get("recovered") is True
        # Verify downstream semantic equivalence: NO error events were dispatched to listener
        assert AgentEventType.ERROR not in [e.event_type for e in captured_events]


def test_stage_preserves_genuine_failure_when_recovery_fails(tmp_path):
    """End-to-end integration test: OpenCode stream hits ECONNRESET, but daemon also failed; Stage reports FAILED."""
    context = _create_test_context(tmp_path)
    role = _create_critic_role()

    adapter = OpenCodeAdapter(auto_approve=True, extra_flags={"recovery_timeout": 5.0, "recovery_poll_interval": 0.05})

    stream_events = [
        json.dumps({
            "type": "error",
            "sessionID": "ses_failed_daemon",
            "error": {
                "type": "provider.transport",
                "message": "Connection lost while reading the response: ECONNRESET",
                "status": 200,
            },
        }),
    ]

    def mock_query_daemon_api(endpoint, method="GET", cwd=None, timeout=10.0):
        if endpoint == "/api/session/ses_failed_daemon":
            return {"data": {"id": "ses_failed_daemon", "outcome": "failed"}}
        return None

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter(stream_events)
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = ""
    mock_proc.poll.return_value = 1
    mock_proc.wait.return_value = 1
    captured_events: List[AgentEvent] = []
    context.event_listener = captured_events.append

    with patch.object(adapter, "_resolve_binary", return_value=("/bin/opencode", None)), \
         patch.object(adapter, "_query_daemon_api", side_effect=mock_query_daemon_api), \
         patch("forge.adapters.opencode.subprocess.Popen", return_value=mock_proc):

        stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))
        result = stage.run(context)

        # Stage MUST fail and preserve failure
        assert result.success is False
        assert result.response.exit_code == 1
        assert result.machine_report.status == "FAILED"
        assert len(captured_events) >= 1
        assert captured_events[-1].event_type == AgentEventType.ERROR
        assert captured_events[-1].result.metadata.get("recovery_failed") is True


# ===========================================================================
# 6. Audit & Edge Case Tests
# ===========================================================================

def test_opencode_extra_flags_filtering():
    """Verify internal recovery flags are never passed to the OpenCode CLI argv."""
    adapter = OpenCodeAdapter(
        extra_flags={
            "recovery_timeout": 60.0,
            "recovery-poll-interval": 1.5,
            "--dangerously-skip-permissions": True,
            "custom_flag": "custom_val",
        }
    )
    rendered = adapter._render_extra_flags()
    assert "--recovery-timeout" not in rendered
    assert "--recovery_timeout" not in rendered
    assert "--recovery-poll-interval" not in rendered
    assert "--recovery_poll_interval" not in rendered
    assert "--dangerously-skip-permissions" in rendered
    assert "--custom_flag" in rendered
    assert "custom_val" in rendered


def test_opencode_configuration_bounds_and_fallbacks():
    """Verify invalid or out-of-range configuration parameters fall back safely."""
    # Non-numeric string fallbacks
    a1 = OpenCodeAdapter(extra_flags={"recovery_timeout": "invalid", "recovery_poll_interval": "bad"})
    assert a1.recovery_timeout == OpenCodeAdapter.DEFAULT_RECOVERY_TIMEOUT
    assert a1.recovery_poll_interval == OpenCodeAdapter.DEFAULT_RECOVERY_POLL_INTERVAL

    # None values fallback
    a2 = OpenCodeAdapter(extra_flags={"recovery_timeout": None, "recovery_poll_interval": None})
    assert a2.recovery_timeout == OpenCodeAdapter.DEFAULT_RECOVERY_TIMEOUT
    assert a2.recovery_poll_interval == OpenCodeAdapter.DEFAULT_RECOVERY_POLL_INTERVAL

    # Clamping negative / out-of-bounds values
    a3 = OpenCodeAdapter(extra_flags={"recovery_timeout": -10.0, "recovery_poll_interval": 0.01})
    assert a3.recovery_timeout == 1.0  # clamped to min 1.0s
    assert a3.recovery_poll_interval == 0.5  # clamped to min 0.5s

    # Poll interval bounded by recovery timeout
    a4 = OpenCodeAdapter(extra_flags={"recovery_timeout": 5.0, "recovery_poll_interval": 20.0})
    assert a4.recovery_timeout == 5.0
    assert a4.recovery_poll_interval == 5.0  # clamped to recovery_timeout


def test_pseudo_string_session_id_rejected():
    """Verify pseudo-string session IDs ('null', 'undefined', 'None', whitespace) are rejected."""
    adapter = OpenCodeAdapter()

    for pseudo_sid in ("null", "undefined", "None", "NONE", "   ", ""):
        adapter._session_id = pseudo_sid
        err_event = AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text="provider.transport ECONNRESET",
            result=ExecutionResult(
                exit_code=1,
                duration_seconds=1.0,
                stderr="ECONNRESET",
                metadata={"session_id": pseudo_sid},
            ),
        )
        assert adapter.can_recover_session(terminal_event=err_event) is False
        assert adapter.recover_session(terminal_event=err_event, session_id=pseudo_sid) is None


def test_recover_session_content_block_filtering(tmp_path):
    """Verify non-text blocks (tool_use, tool_result) are ignored and multi-block text is concatenated in order."""
    adapter = OpenCodeAdapter(extra_flags={"recovery_timeout": 5.0, "recovery_poll_interval": 0.1})
    adapter._session_id = "ses_blocks_test"

    complex_content = [
        {"type": "text", "text": "Heading: Security Audit\n"},
        {"type": "tool_use", "name": "view_file", "input": {"path": "main.py"}, "text": "should be ignored"},
        {"type": "tool_result", "content": "file contents", "text": "also ignored"},
        {"type": "text", "text": "Findings: 0 critical issues.\n"},
        {"type": "text", "text": "```yaml\nROLE: CRITIC\nSTATUS: APPROVED\n```"},
    ]

    def mock_query(endpoint, method="GET", cwd=None, timeout=10.0):
        if "message" in endpoint:
            return {
                "data": [
                    {
                        "id": "msg_multi_block",
                        "role": "assistant",
                        "finish": "stop",
                        "content": complex_content,
                    }
                ]
            }
        return {"data": {"id": "ses_blocks_test", "outcome": "succeeded"}}

    with patch.object(adapter, "_resolve_binary", return_value=("/bin/opencode", None)), \
         patch.object(adapter, "_query_daemon_api", side_effect=mock_query):

        recovered = adapter.recover_session(session_id="ses_blocks_test", cwd=tmp_path)
        assert recovered is not None
        rec_event, rec_resp = recovered
        stdout = rec_event.result.stdout
        assert "Heading: Security Audit" in stdout
        assert "Findings: 0 critical issues." in stdout
        assert "```yaml\nROLE: CRITIC\nSTATUS: APPROVED\n```" in stdout
        assert "should be ignored" not in stdout
        assert "also ignored" not in stdout


def test_recover_session_aborted_finish_reason_rejected(tmp_path):
    """Verify an assistant message with error/aborted finish status causes recovery to fail."""
    adapter = OpenCodeAdapter(extra_flags={"recovery_timeout": 5.0, "recovery_poll_interval": 0.1})
    adapter._session_id = "ses_aborted_test"

    def mock_query(endpoint, method="GET", cwd=None, timeout=10.0):
        if "message" in endpoint:
            return {
                "data": [
                    {
                        "id": "msg_aborted",
                        "role": "assistant",
                        "finish": "aborted",
                        "content": [{"type": "text", "text": "Partial text before abort"}],
                    }
                ]
            }
        return {"data": {"id": "ses_aborted_test", "outcome": "succeeded"}}

    with patch.object(adapter, "_resolve_binary", return_value=("/bin/opencode", None)), \
         patch.object(adapter, "_query_daemon_api", side_effect=mock_query):

        orig_event = AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text="ECONNRESET",
            result=ExecutionResult(exit_code=1, duration_seconds=5.0, stderr="ECONNRESET"),
        )
        recovered = adapter.recover_session(terminal_event=orig_event, session_id="ses_aborted_test", cwd=tmp_path)
        assert recovered is None
        assert orig_event.result.metadata.get("recovery_failed") is True
        assert "finish status was 'aborted'" in orig_event.result.metadata.get("recovery_failure_reason", "")
