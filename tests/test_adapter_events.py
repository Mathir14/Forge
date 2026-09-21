"""Tests for the Core Event Interface, ExecutionResult, and BaseAdapter.iter_events contract."""

import pytest
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.adapters.opencode import OpenCodeAdapter
from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.codex import CodexAdapter


class DummyAdapter(BaseAdapter):
    """Minimal adapter subclass implementing execute() for testing universal BaseAdapter behavior."""

    def __init__(self, response: Optional[AdapterResponse] = None):
        super().__init__(name="dummy")
        self._response = response or AdapterResponse(
            stdout="hello world",
            stderr="",
            exit_code=0,
            duration_seconds=1.23,
            raw_output="hello world",
        )
        self.last_prompt = None
        self.last_cwd = None
        self.last_timeout = None

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        self.last_prompt = prompt
        self.last_cwd = cwd
        self.last_timeout = timeout
        return self._response


# ---------------------------------------------------------------------------
# 1. Event Model & ExecutionResult Unit Tests
# ---------------------------------------------------------------------------

def test_agent_event_type_values():
    """Verify all required AgentEventType enum members and their string values."""
    assert AgentEventType.CHUNK == "chunk"
    assert AgentEventType.TOOL_START == "tool_start"
    assert AgentEventType.TOOL_FINISH == "tool_finish"
    assert AgentEventType.HEARTBEAT == "heartbeat"
    assert AgentEventType.COMPLETE == "complete"
    assert AgentEventType.ERROR == "error"


def test_execution_result_typed_fields_and_defaults():
    """Verify ExecutionResult instantiation, typed raw_output field, and defaults."""
    res = ExecutionResult(
        exit_code=0,
        duration_seconds=2.5,
    )
    assert res.exit_code == 0
    assert res.duration_seconds == 2.5
    assert res.stdout == ""
    assert res.stderr == ""
    assert res.raw_output == ""
    assert res.token_usage == {}
    assert res.metadata == {}

    # Verify first-class raw_output domain field
    res_with_raw = ExecutionResult(
        exit_code=1,
        duration_seconds=0.5,
        stdout="out",
        stderr="err",
        raw_output="full raw output trace",
        token_usage={"prompt": 100, "completion": 50},
        metadata={"custom": "info"},
    )
    assert res_with_raw.raw_output == "full raw output trace"
    assert "raw_output" not in res_with_raw.metadata
    assert res_with_raw.token_usage == {"prompt": 100, "completion": 50}
    assert res_with_raw.metadata == {"custom": "info"}


def test_agent_event_creation_and_payload():
    """Verify AgentEvent structure, timestamps, text payload, and optional result."""
    now = time.time()
    event = AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=now,
        text="partial token chunk",
    )
    assert event.event_type == AgentEventType.CHUNK
    assert event.timestamp == now
    assert event.text == "partial token chunk"
    assert event.data == {}
    assert event.result is None

    res = ExecutionResult(exit_code=0, duration_seconds=1.0, raw_output="done")
    comp_event = AgentEvent(
        event_type=AgentEventType.COMPLETE,
        timestamp=now + 1.0,
        text="done",
        data={"finish_reason": "stop"},
        result=res,
    )
    assert comp_event.event_type == AgentEventType.COMPLETE
    assert comp_event.result is res
    assert comp_event.data["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# 2. AdapterResponse <-> ExecutionResult Conversion Bridge Tests
# ---------------------------------------------------------------------------

def test_adapter_response_to_result():
    """Verify AdapterResponse converts cleanly to ExecutionResult via direct field mapping."""
    resp = AdapterResponse(
        stdout="standard output",
        stderr="standard error",
        exit_code=0,
        duration_seconds=3.14,
        raw_output="standard output\nstandard error",
    )
    res = resp.to_result()
    assert isinstance(res, ExecutionResult)
    assert res.stdout == resp.stdout
    assert res.stderr == resp.stderr
    assert res.exit_code == resp.exit_code
    assert res.duration_seconds == resp.duration_seconds
    assert res.raw_output == resp.raw_output
    assert res.token_usage == {}
    assert res.metadata == {}


def test_adapter_response_from_result():
    """Verify AdapterResponse constructs directly from ExecutionResult."""
    res = ExecutionResult(
        exit_code=124,
        duration_seconds=5.0,
        stdout="timeout output",
        stderr="timed out",
        raw_output="timeout output\ntimed out",
    )
    resp = AdapterResponse.from_result(res)
    assert isinstance(resp, AdapterResponse)
    assert resp.stdout == res.stdout
    assert resp.stderr == res.stderr
    assert resp.exit_code == res.exit_code
    assert resp.duration_seconds == res.duration_seconds
    assert resp.raw_output == res.raw_output


def test_adapter_response_conversion_roundtrip():
    """Verify bidirectional roundtrip preserves all execution properties."""
    original = AdapterResponse(
        stdout="out",
        stderr="err",
        exit_code=42,
        duration_seconds=10.5,
        raw_output="raw",
    )
    roundtripped = AdapterResponse.from_result(original.to_result())
    assert roundtripped.stdout == original.stdout
    assert roundtripped.stderr == original.stderr
    assert roundtripped.exit_code == original.exit_code
    assert roundtripped.duration_seconds == original.duration_seconds
    assert roundtripped.raw_output == original.raw_output


# ---------------------------------------------------------------------------
# 3. BaseAdapter.iter_events() Compatibility Wrapper Tests
# ---------------------------------------------------------------------------

def test_iter_events_success_with_stdout():
    """Verify iter_events yields CHUNK and COMPLETE on success with non-empty stdout."""
    adapter = DummyAdapter(
        AdapterResponse(
            stdout="Success output",
            stderr="",
            exit_code=0,
            duration_seconds=1.5,
            raw_output="Success output",
        )
    )

    events = list(adapter.iter_events("test prompt"))
    assert len(events) == 2

    # 1. CHUNK
    chunk = events[0]
    assert chunk.event_type == AgentEventType.CHUNK
    assert chunk.text == "Success output"
    assert chunk.result is None

    # 2. COMPLETE
    complete = events[1]
    assert complete.event_type == AgentEventType.COMPLETE
    assert complete.text == "Success output"
    assert complete.result is not None
    assert complete.result.exit_code == 0
    assert complete.result.duration_seconds == 1.5
    assert complete.result.stdout == "Success output"
    assert complete.result.raw_output == "Success output"


def test_iter_events_success_without_stdout():
    """Verify iter_events yields only COMPLETE when execute() produces empty stdout."""
    adapter = DummyAdapter(
        AdapterResponse(
            stdout="",
            stderr="",
            exit_code=0,
            duration_seconds=0.8,
            raw_output="",
        )
    )

    events = list(adapter.iter_events("test prompt"))
    assert len(events) == 1

    complete = events[0]
    assert complete.event_type == AgentEventType.COMPLETE
    assert complete.text == ""
    assert complete.result is not None
    assert complete.result.exit_code == 0
    assert complete.result.duration_seconds == 0.8


def test_iter_events_failure_with_stderr():
    """Verify iter_events yields ERROR on non-zero exit code carrying ExecutionResult."""
    adapter = DummyAdapter(
        AdapterResponse(
            stdout="",
            stderr="fatal: branch not found",
            exit_code=1,
            duration_seconds=0.3,
            raw_output="fatal: branch not found",
        )
    )

    events = list(adapter.iter_events("failing prompt"))
    assert len(events) == 1

    err = events[0]
    assert err.event_type == AgentEventType.ERROR
    assert err.text == "fatal: branch not found"
    assert err.result is not None
    assert err.result.exit_code == 1
    assert err.result.stderr == "fatal: branch not found"
    assert err.result.raw_output == "fatal: branch not found"


def test_iter_events_failure_with_stdout_and_stderr():
    """Verify iter_events yields CHUNK then ERROR when execution fails but has partial stdout."""
    adapter = DummyAdapter(
        AdapterResponse(
            stdout="partial progress before crash",
            stderr="segmentation fault",
            exit_code=139,
            duration_seconds=2.0,
            raw_output="partial progress before crash\nsegmentation fault",
        )
    )

    events = list(adapter.iter_events("crash prompt"))
    assert len(events) == 2

    # 1. CHUNK for partial progress
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[0].text == "partial progress before crash"

    # 2. ERROR terminal event
    assert events[1].event_type == AgentEventType.ERROR
    assert events[1].text == "segmentation fault"
    assert events[1].result is not None
    assert events[1].result.exit_code == 139
    assert events[1].result.stdout == "partial progress before crash"
    assert events[1].result.stderr == "segmentation fault"
    assert events[1].result.raw_output == "partial progress before crash\nsegmentation fault"


def test_iter_events_failure_fallback_to_raw_output():
    """Verify error text falls back to raw_output when stderr is empty."""
    adapter = DummyAdapter(
        AdapterResponse(
            stdout="",
            stderr="",
            exit_code=124,
            duration_seconds=300.0,
            raw_output="Execution timed out after 300 seconds.",
        )
    )

    events = list(adapter.iter_events("timeout prompt"))
    assert len(events) == 1
    assert events[0].event_type == AgentEventType.ERROR
    assert events[0].text == "Execution timed out after 300 seconds."
    assert events[0].result.exit_code == 124


def test_iter_events_computes_duration_if_missing():
    """Verify iter_events populates duration_seconds if adapter response had duration 0."""
    adapter = DummyAdapter(
        AdapterResponse(
            stdout="quick",
            stderr="",
            exit_code=0,
            duration_seconds=0.0,
            raw_output="quick",
        )
    )

    events = list(adapter.iter_events("prompt"))
    complete = events[-1]
    assert complete.event_type == AgentEventType.COMPLETE
    assert complete.result.duration_seconds > 0.0


def test_execute_events_alias():
    """Verify execute_events is an alias to iter_events and works identically."""
    adapter = DummyAdapter()
    assert adapter.execute_events == adapter.iter_events

    events = list(adapter.execute_events("alias test"))
    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[1].event_type == AgentEventType.COMPLETE


def test_iter_events_passes_all_arguments():
    """Verify iter_events forwards prompt, cwd, and timeout to execute()."""
    adapter = DummyAdapter()
    test_path = Path("/custom/workdir")
    events = list(adapter.iter_events(prompt="do something", cwd=test_path, timeout=60))

    assert adapter.last_prompt == "do something"
    assert adapter.last_cwd == test_path
    assert adapter.last_timeout == 60
    assert len(events) == 2


# ---------------------------------------------------------------------------
# 4. Existing Provider Adapters Invariance
# ---------------------------------------------------------------------------

def test_concrete_adapters_inherit_iter_events():
    """Verify OpenCodeAdapter, AntigravityAdapter, and CodexAdapter inherit iter_events."""
    opencode = OpenCodeAdapter()
    antigravity = AntigravityAdapter()
    codex = CodexAdapter()

    for adp in (opencode, antigravity, codex):
        assert hasattr(adp, "iter_events")
        assert callable(adp.iter_events)
        assert hasattr(adp, "execute_events")
        assert adp.execute_events == adp.iter_events
