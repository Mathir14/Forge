"""Regression test matrix for Forensic Investigation: Stage Completion Hang After Artifact Creation.

Covers Scenarios A through J specified in Phase Four:
- Scenario A: Artifact produced, process exits normally -> prompt success.
- Scenario B: In-stream terminal event received, but process keeps stdout open / process alive -> reader hang reproduction.
- Scenario C: Process exits, event delivery delayed -> buffered events drained cleanly.
- Scenario D: Child process holds stdout pipe open after parent finishes -> pipe inheritance hang.
- Scenario E: Artifact exists, protocol validation fails -> validation not bypassed by artifact presence.
- Scenario F: Artifact written, adapter fails afterward -> failure correctly surfaced.
- Scenario G: Timeout races with finalization -> exactly one coherent terminal outcome.
- Scenario H: Duplicate or out-of-order terminal events -> idempotent handling, no duplicate transition.
- Scenario I: Stale artifact from earlier attempt in run directory -> stale evidence rejected / not mistaken for success.
- Scenario J: Cancellation and process cleanup while reader is blocked -> prompt termination and clean resource release.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator, Optional, Dict, Any, List
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.base import BaseAdapter, AdapterResponse, StderrDrainer, BoundedLineReader
from forge.adapters.opencode import OpenCodeAdapter
from forge.core.config import Config
from forge.core.context import Context
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.core.git import GitService
from forge.core.role import Role
from forge.protocol.parser import MachineReportParser
from forge.protocol.validator import MachineReportValidator
from forge.stages.result import StageResult
from forge.stages.stage import Stage
from forge.storage.run_manager import RunManager


# ---------------------------------------------------------------------------
# Helpers & Mock Adapters
# ---------------------------------------------------------------------------

class QueueStreamAdapter(BaseAdapter):
    """Adapter backed by a thread-safe Queue, allowing controlled event delivery."""

    def __init__(self, name: str = "queue_adapter"):
        super().__init__(name=name, auto_approve=True)
        self.event_queue: queue.Queue[Optional[AgentEvent]] = queue.Queue()
        self.cancel_called = False

    def is_available(self) -> bool:
        return True

    def cancel(self) -> None:
        self.cancel_called = True

    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        return AdapterResponse(
            stdout="",
            stderr="",
            exit_code=0,
            duration_seconds=0.1,
            raw_output="",
        )

    def iter_events(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> Iterator[AgentEvent]:
        while True:
            ev = self.event_queue.get()
            if ev is None:
                break
            yield ev


class GeneratorMockAdapter(BaseAdapter):
    """Test adapter yielding preconfigured events."""

    def __init__(self, events: Optional[List[AgentEvent]] = None, name: str = "generator_mock"):
        super().__init__(name=name, auto_approve=True)
        self.events = events or []

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        return AdapterResponse(
            stdout="",
            stderr="",
            exit_code=0,
            duration_seconds=0.1,
            raw_output="",
        )

    def iter_events(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> Iterator[AgentEvent]:
        yield from self.events


def _make_context(tmp_path: Path, run_id: str = "run-test") -> tuple[Context, RunManager]:
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Forensic regression testing")
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )
    return context, run_mgr


# ---------------------------------------------------------------------------
# Scenario A: Artifact produced, process exits normally -> prompt success
# ---------------------------------------------------------------------------

def test_scenario_a_normal_completion_prompt_success(tmp_path):
    """Scenario A: Process produces output, in-stream result, and terminates normally (EOF + exit code 0).
    Stage must complete promptly, validate protocol, write artifacts, and return success=True.
    """
    context, run_mgr = _make_context(tmp_path)
    role = Role(
        name="architect",
        sequence_number=1,
        template_content="Architect template",
        protocol_content="Emit machine report.",
    )

    valid_markdown = (
        "# System Architecture\nProposed clean design.\n\n"
        "```yaml\n"
        "ROLE: ARCHITECT\n"
        "STATUS: APPROVED\n"
        "HANDOFF: PLANNER\n"
        "```\n"
    )

    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": valid_markdown}}),
        json.dumps({"event": "result", "status": "success"}),
    ]

    # In normal Scenario A, EOF is reached immediately after result and process returns 0
    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=lambda: 0,
        get_stderr=lambda: "",
    ))

    assert events[-1].event_type == AgentEventType.COMPLETE
    assert events[-1].result.exit_code == 0

    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
    with patch.object(stage, "_get_event_stream", return_value=iter(events)):
        result = stage.run(context)

    assert result.success is True
    assert result.status == "APPROVED"
    assert result.handoff == "PLANNER"

    # Both paired artifacts exist on disk
    md_file = context.run.run_dir / "01_architect.md"
    json_file = context.run.run_dir / "01_architect.json"
    assert md_file.exists()
    assert json_file.exists()


# ---------------------------------------------------------------------------
# Scenario B: In-stream result received, but stdout not closed -> hang reproduction
# ---------------------------------------------------------------------------

def test_scenario_b_instream_result_hangs_on_unclosed_stdout(tmp_path):
    """Scenario B (Direct Defect Reproduction):
    Subprocess emits text and terminal result event, but stdout is NOT closed (EOF never sent).
    Under ADR-017, AntigravityAdapter/OpenCodeAdapter suppresses COMPLETE in-stream and waits for EOF.
    With default idle_timeout=None, Stage._execute_stream_events blocks indefinitely until timeout/kill.
    When terminated by timeout (or SIGKILL), Stage writes the valid markdown to disk as an artifact,
    but fails the stage (exit code != 0), leaving a complete artifact on disk while Forge reports failure.
    """
    context, run_mgr = _make_context(tmp_path)
    role = Role(
        name="planner",
        sequence_number=2,
        template_content="Planner template",
        protocol_content="Emit machine report.",
    )

    valid_markdown = (
        "# Project Plan\nComplete operational plan.\n\n"
        "```yaml\n"
        "ROLE: PLANNER\n"
        "STATUS: SUCCESS\n"
        "HANDOFF: EXECUTOR\n"
        "```\n"
    )

    # We simulate a blocking stream: generator yields text and result, then blocks simulating open pipe
    unblock_stream = threading.Event()

    def blocking_stream_gen() -> Iterator[str]:
        yield json.dumps({"event": "step_update", "step_update": {"text_delta": valid_markdown}})
        yield json.dumps({"event": "result", "status": "success"})
        # Agent has completely finished generating! But pipe writer is not closed.
        unblock_stream.wait(timeout=2.0)
        # Process is eventually killed with non-zero exit code (e.g. -9 / 124)

    adapter = AntigravityAdapter()

    # Verify adapter behavior: does NOT emit COMPLETE in-stream
    # When pipe is blocked, the generator cannot reach post-mortem evaluation
    t_start = time.time()
    events_received = []

    def run_adapter():
        for ev in adapter._decode_stream_events(
            stream=blocking_stream_gen(),
            start_time=time.time(),
            get_returncode=lambda: -9 if unblock_stream.is_set() else None,
            get_stderr=lambda: "Process terminated by SIGKILL",
        ):
            events_received.append(ev)

    th = threading.Thread(target=run_adapter)
    th.start()
    time.sleep(0.3)

    # At this point, the agent has emitted valid markdown and 'result', yet NO COMPLETE event was emitted!
    assert len(events_received) == 1
    assert events_received[0].event_type == AgentEventType.CHUNK
    # The reader thread is stuck waiting for EOF
    assert th.is_alive()

    # Now unblock / kill process (simulating SIGKILL -9 after hang)
    unblock_stream.set()
    th.join(timeout=1.0)

    # After kill, the adapter yields ERROR (not COMPLETE) because exit_code == -9
    assert events_received[-1].event_type == AgentEventType.ERROR
    assert events_received[-1].result.exit_code == -9

    # Now verify Stage execution behavior with idle_timeout configured vs None:
    # When Stage executes this stream and times out (e.g. timeout=0.5s), it writes the valid markdown to disk!
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr, timeout=1)
    with patch.object(stage, "_get_event_stream", return_value=iter(events_received)):
        result = stage.run(context)

    # Stage failed because terminal event was ERROR
    assert result.success is False
    assert result.status == "FAILED"

    # But the artifact on disk contains the FULL, VALID Markdown plan!
    md_file = context.run.run_dir / "02_planner.md"
    json_file = context.run.run_dir / "02_planner.json"
    assert md_file.exists()
    assert json_file.exists()
    assert "ROLE: PLANNER" in md_file.read_text(encoding="utf-8")
    assert "STATUS: SUCCESS" in md_file.read_text(encoding="utf-8")
    # JSON records failure with exit_code -9
    json_data = json.loads(json_file.read_text(encoding="utf-8"))
    assert json_data["status"] == "FAILED"
    assert json_data["exit_code"] == -9


# ---------------------------------------------------------------------------
# Scenario C: Process exits, event delivery delayed -> buffered events drained
# ---------------------------------------------------------------------------

def test_scenario_c_process_exits_event_delivery_delayed(tmp_path):
    """Scenario C: Process terminates, but output buffer is delivered in small increments.
    Verify all buffered events are drained before post-mortem evaluation.
    """
    adapter = AntigravityAdapter()

    def delayed_stream():
        yield json.dumps({"event": "step_update", "step_update": {"text_delta": "Part 1 "}})
        time.sleep(0.05)
        yield json.dumps({"event": "step_update", "step_update": {"text_delta": "Part 2"}})
        time.sleep(0.05)
        yield json.dumps({"event": "result", "status": "success"})

    events = list(adapter._decode_stream_events(
        stream=delayed_stream(),
        start_time=time.time(),
        get_returncode=lambda: 0,
        get_stderr=lambda: "",
    ))

    assert events[-1].event_type == AgentEventType.COMPLETE
    assert events[-1].result.stdout == "Part 1 Part 2"
    assert events[-1].result.exit_code == 0


# ---------------------------------------------------------------------------
# Scenario D: Child process holds stdout pipe open after parent finishes
# ---------------------------------------------------------------------------

def test_scenario_d_child_process_holds_stdout_pipe(tmp_path):
    """Scenario D: A background child process inherits stdout and keeps the write end of the pipe open.
    In POSIX, proc.stdout.readline() blocks until ALL writers close the descriptor.
    Demonstrates the exact OS pipe descriptor inheritance deadlock under ADR-017.
    """
    # Spawn a real subprocess that spawns a background sleeper inheriting stdout
    cmd = [
        sys.executable,
        "-c",
        (
            "import subprocess, sys, time\n"
            "# Parent spawns child inheriting stdout\n"
            "subprocess.Popen(['sleep', '2.0'], stdout=sys.stdout)\n"
            "print('{\"event\": \"step_update\", \"step_update\": {\"text_delta\": \"hello\"}}', flush=True)\n"
            "print('{\"event\": \"result\", \"status\": \"success\"}', flush=True)\n"
            "sys.exit(0)\n"
        ),
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    adapter = AntigravityAdapter()
    start_t = time.time()
    events = list(adapter._decode_stream_events(
        stream=proc.stdout,
        start_time=start_t,
        get_returncode=lambda: proc.poll(),
        get_stderr=lambda: "",
    ))

    elapsed = time.time() - start_t
    # With bounded drain, the reader does NOT hang waiting for the 2.0s child sleeper!
    assert elapsed < 1.5
    assert events[-1].event_type == AgentEventType.COMPLETE
    assert events[-1].result.stdout == "hello"
    assert events[-1].result.exit_code == 0


# ---------------------------------------------------------------------------
# Scenario E: Artifact exists, protocol validation fails
# ---------------------------------------------------------------------------

def test_scenario_e_artifact_exists_protocol_validation_fails(tmp_path):
    """Scenario E: The agent produces an artifact file on disk or output, but machine report
    has invalid YAML or failing status. Artifact presence MUST NOT bypass validation.
    """
    context, run_mgr = _make_context(tmp_path)
    role = Role(
        name="architect",
        sequence_number=1,
        template_content="Architect template",
        protocol_content="Emit machine report.",
    )

    invalid_protocol_markdown = (
        "# System Architecture\nFound blocking architectural violations.\n\n"
        "```yaml\n"
        "ROLE: ARCHITECT\n"
        "STATUS: REJECTED\n"
        "HANDOFF: NONE\n"
        "```\n"
    )

    adapter = GeneratorMockAdapter(events=[
        AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text=invalid_protocol_markdown,
            result=ExecutionResult(
                exit_code=0,
                duration_seconds=1.0,
                stdout=invalid_protocol_markdown,
                raw_output=invalid_protocol_markdown,
            ),
        )
    ])

    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
    result = stage.run(context)

    # Artifact is saved on disk, but result.success is strictly False!
    assert result.success is False
    assert result.status == "REJECTED"
    md_file = context.run.run_dir / "01_architect.md"
    json_file = context.run.run_dir / "01_architect.json"
    assert md_file.exists()
    assert json_file.exists()
    saved_json = json.loads(json_file.read_text(encoding="utf-8"))
    assert saved_json["machine_report"]["status"] == "REJECTED"


# ---------------------------------------------------------------------------
# Scenario F: Artifact written, adapter fails afterward
# ---------------------------------------------------------------------------

def test_scenario_f_artifact_written_adapter_fails_afterward(tmp_path):
    """Scenario F: Adapter yields chunks and then encounters an ERROR event (e.g. transport crash).
    Forge must not falsely declare success. Failure is surfaced and recorded.
    """
    context, run_mgr = _make_context(tmp_path)
    role = Role(
        name="executor",
        sequence_number=3,
        template_content="Executor template",
        protocol_content="Emit machine report.",
    )

    partial_output = "# Executor Output\nRunning migrations..."
    error_msg = "Connection reset by peer during transport"

    adapter = GeneratorMockAdapter(events=[
        AgentEvent(
            event_type=AgentEventType.CHUNK,
            timestamp=time.time(),
            text=partial_output,
        ),
        AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text=error_msg,
            result=ExecutionResult(
                exit_code=1,
                duration_seconds=0.5,
                stdout=partial_output,
                stderr=error_msg,
                raw_output=partial_output,
            ),
        ),
    ])

    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
    result = stage.run(context)

    assert result.success is False
    assert result.status == "FAILED"
    assert result.response.exit_code == 1

    # Check that disk records failure, not success
    json_file = context.run.run_dir / "03_executor.json"
    assert json_file.exists()
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["status"] == "FAILED"
    assert data["exit_code"] == 1


# ---------------------------------------------------------------------------
# Scenario G: Timeout races with finalization
# ---------------------------------------------------------------------------

def test_scenario_g_timeout_races_with_finalization(tmp_path):
    """Scenario G: Timeout boundary occurs while event queue is processing.
    Verify exactly one coherent terminal event is recorded without duplicate transition.
    """
    context, run_mgr = _make_context(tmp_path)
    role = Role(
        name="reviewer",
        sequence_number=5,
        template_content="Reviewer template",
        protocol_content="Emit machine report.",
    )

    # Deliver events right at the timeout boundary
    adapter = QueueStreamAdapter()
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr, timeout=1)

    def worker():
        time.sleep(0.95)
        # Put COMPLETE event right around deadline
        valid_md = "# Review\nApproved.\n```yaml\nROLE: REVIEWER\nSTATUS: APPROVED\nHANDOFF: NONE\n```\n"
        adapter.event_queue.put(AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text=valid_md,
            result=ExecutionResult(exit_code=0, duration_seconds=1.0, stdout=valid_md, raw_output=valid_md),
        ))
        adapter.event_queue.put(None)

    t = threading.Thread(target=worker)
    t.start()

    result = stage.run(context)
    t.join(timeout=2.0)

    # Result must be either cleanly APPROVED or cleanly timed out (exit_code 124), never an inconsistent hybrid
    assert result.status in ("APPROVED", "FAILED")
    if result.status == "FAILED":
        assert result.response.exit_code in (1, 124)
    else:
        assert result.success is True


# ---------------------------------------------------------------------------
# Scenario H: Duplicate or out-of-order terminal events
# ---------------------------------------------------------------------------

def test_scenario_h_duplicate_or_out_of_order_terminal_events(tmp_path):
    """Scenario H: Multiple terminal events delivered in stream.
    First terminal event wins; subsequent terminal events are ignored idempotently.
    """
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": "# Final Report\n"}}),
        json.dumps({"event": "result", "status": "success"}),
        # Duplicate result event
        json.dumps({"event": "result", "status": "success"}),
        # Out-of-order step_finish stop
        json.dumps({"event": "step_finish", "part": {"reason": "stop"}}),
    ]

    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=lambda: 0,
        get_stderr=lambda: "",
    ))

    # Exactly one COMPLETE event yielded at EOF
    complete_events = [e for e in events if e.event_type == AgentEventType.COMPLETE]
    assert len(complete_events) == 1
    assert complete_events[0].result.exit_code == 0


# ---------------------------------------------------------------------------
# Scenario I: Stale artifact from earlier attempt in run directory
# ---------------------------------------------------------------------------

def test_scenario_i_stale_artifact_in_run_directory(tmp_path):
    """Scenario I: A stale artifact from an earlier sequence exists on disk.
    If current stage execution fails, the stale artifact must NOT be accepted as current success.
    """
    context, run_mgr = _make_context(tmp_path)
    role = Role(
        name="planner",
        sequence_number=2,
        template_content="Planner template",
        protocol_content="Emit machine report.",
    )

    # Seed stale artifact from previous run
    run_dir = context.run.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    stale_json = run_dir / "02_planner.json"
    stale_json.write_text(json.dumps({
        "role": "planner",
        "sequence_number": 2,
        "status": "APPROVED",
        "machine_report": {"status": "APPROVED", "exit_code": 0, "is_valid": True},
        "input_fingerprint": "stale_fingerprint_123",
    }), encoding="utf-8")

    # Current attempt fails
    adapter = GeneratorMockAdapter(events=[
        AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text="Immediate fatal failure",
            result=ExecutionResult(exit_code=1, duration_seconds=0.1, stderr="Immediate fatal failure"),
        )
    ])

    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
    result = stage.run(context)

    # Current execution overwrote or recorded failure; result is False
    assert result.success is False
    assert result.status == "FAILED"
    new_json = json.loads(stale_json.read_text(encoding="utf-8"))
    assert new_json["status"] == "FAILED"


# ---------------------------------------------------------------------------
# Scenario J: Cancellation and process cleanup while reader is blocked
# ---------------------------------------------------------------------------

def test_scenario_j_cancellation_and_process_cleanup_while_reader_blocked(tmp_path):
    """Scenario J: While stream reader is blocked, user/abort_event requests cancellation.
    Stage must promptly terminate, call adapter.cancel(), return exit code 130, and clean up.
    """
    context, run_mgr = _make_context(tmp_path)
    abort_event = threading.Event()
    context.abort_event = abort_event

    role = Role(
        name="executor",
        sequence_number=3,
        template_content="Executor template",
        protocol_content="Emit machine report.",
    )

    adapter = QueueStreamAdapter()
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr, abort_event=abort_event)

    # Put one chunk so stage starts consuming
    adapter.event_queue.put(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Starting long operation...",
    ))

    def trigger_abort():
        time.sleep(0.3)
        abort_event.set()

    t = threading.Thread(target=trigger_abort)
    t.start()

    start_time = time.time()
    result = stage.run(context)
    elapsed = time.time() - start_time
    t.join(timeout=1.0)

    # Prompt termination (< 1-2 seconds)
    assert elapsed < 2.5, f"Expected prompt cancellation, took {elapsed:.2f}s"
    assert result.success is False
    assert result.response.exit_code == 130
    assert adapter.cancel_called is True


# ---------------------------------------------------------------------------
# Scenario K: Equivalence of Antigravity and OpenCode decoders under unclosed pipe
# ---------------------------------------------------------------------------

def test_scenario_k_decoder_equivalence_under_unclosed_stdout(tmp_path):
    """Scenario K: Both AntigravityAdapter and OpenCodeAdapter must exhibit identical
    bounded-drain lifecycle behavior when an inherited child process retains stdout:
    1. Prompt completion (< 1.5s) instead of indefinite hang.
    2. Proper COMPLETE event with exit_code 0 when parent exits cleanly.
    3. Proper ERROR event with non-zero exit code when parent exits with error.
    4. Successful artifact persistence and stage finalization.
    """
    valid_markdown = (
        "# System Architecture\nEquivalence test design.\n\n"
        "```yaml\n"
        "ROLE: ARCHITECT\n"
        "STATUS: APPROVED\n"
        "HANDOFF: PLANNER\n"
        "```\n"
    )

    payload_ag = json.dumps({"event": "step_update", "step_update": {"text_delta": valid_markdown}})
    payload_oc = json.dumps({"type": "text", "part": {"text": valid_markdown}})

    # 1. Antigravity with unclosed stdout
    cmd_ag = [
        sys.executable,
        "-c",
        (
            "import subprocess, sys, time\n"
            "subprocess.Popen(['sleep', '2.0'], stdout=sys.stdout)\n"
            f"print({repr(payload_ag)}, flush=True)\n"
            "print('{\"event\": \"result\", \"status\": \"success\"}', flush=True)\n"
            "sys.exit(0)\n"
        ),
    ]
    proc_ag = subprocess.Popen(cmd_ag, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    adapter_ag = AntigravityAdapter()
    t0_ag = time.time()
    events_ag = list(adapter_ag._decode_stream_events(
        stream=proc_ag.stdout,
        start_time=t0_ag,
        get_returncode=lambda: proc_ag.poll(),
        get_stderr=lambda: "",
    ))
    elapsed_ag = time.time() - t0_ag
    proc_ag.wait()

    assert elapsed_ag < 1.5
    assert events_ag[-1].event_type == AgentEventType.COMPLETE
    assert events_ag[-1].result.exit_code == 0
    assert "ROLE: ARCHITECT" in events_ag[-1].result.stdout

    # 2. OpenCode with unclosed stdout
    cmd_oc = [
        sys.executable,
        "-c",
        (
            "import subprocess, sys, time\n"
            "subprocess.Popen(['sleep', '2.0'], stdout=sys.stdout)\n"
            f"print({repr(payload_oc)}, flush=True)\n"
            "print('{\"type\": \"step_finish\", \"part\": {\"reason\": \"stop\"}}', flush=True)\n"
            "sys.exit(0)\n"
        ),
    ]
    proc_oc = subprocess.Popen(cmd_oc, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    adapter_oc = OpenCodeAdapter()
    t0_oc = time.time()
    events_oc = list(adapter_oc._decode_stream_events(
        stream=proc_oc.stdout,
        start_time=t0_oc,
        get_returncode=lambda: proc_oc.poll(),
        get_stderr=lambda: "",
    ))
    elapsed_oc = time.time() - t0_oc
    proc_oc.wait()

    assert elapsed_oc < 1.5
    assert events_oc[-1].event_type == AgentEventType.COMPLETE
    assert events_oc[-1].result.exit_code == 0
    assert "ROLE: ARCHITECT" in events_oc[-1].result.stdout

    # 3. Nonzero exit with unclosed stdout yields ERROR in both
    cmd_err = [
        sys.executable,
        "-c",
        (
            "import subprocess, sys, time\n"
            "subprocess.Popen(['sleep', '2.0'], stdout=sys.stdout)\n"
            "print('{\"type\": \"step_finish\", \"part\": {\"reason\": \"stop\"}}', flush=True)\n"
            "sys.stderr.write('Fatal crash\\n')\n"
            "sys.exit(2)\n"
        ),
    ]
    proc_err = subprocess.Popen(cmd_err, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    events_err = list(adapter_oc._decode_stream_events(
        stream=proc_err.stdout,
        start_time=time.time(),
        get_returncode=lambda: proc_err.poll(),
        get_stderr=lambda: "Fatal crash",
    ))
    proc_err.wait()
    assert events_err[-1].event_type == AgentEventType.ERROR
    assert events_err[-1].result.exit_code == 2


# ---------------------------------------------------------------------------
# Scenario L: Platform neutrality and BoundedLineReader resilience
# ---------------------------------------------------------------------------

def test_scenario_l_platform_neutrality_and_bounded_reader():
    """Scenario L: Verify BoundedLineReader operates neutrally without POSIX-only APIs:
    - No fcntl / termios dependencies.
    - Handles bytes and string streams.
    - Non-blocking semantics via thread-safe queue.
    - Handles timeouts, EOF, and closing cleanly without thread leaks.
    """
    from forge.adapters.base import BoundedLineReader
    import io

    # 1. Ensure no POSIX-only APIs imported
    import forge.adapters.base as base_mod
    assert not hasattr(base_mod, "fcntl")
    assert not hasattr(base_mod, "termios")

    # 2. String stream
    str_stream = io.StringIO("line1\nline2\n")
    reader = BoundedLineReader(str_stream)
    assert reader.read_line(timeout=0.2) == "line1\n"
    assert reader.read_line(timeout=0.2) == "line2\n"
    assert reader.read_line(timeout=0.2) is None
    assert reader.is_eof() is True
    reader.close()

    # 3. Bytes stream
    bytes_stream = io.BytesIO(b"byte1\nbyte2\n")
    reader_bytes = BoundedLineReader(bytes_stream)
    assert reader_bytes.read_line(timeout=0.2) == "byte1\n"
    assert reader_bytes.read_line(timeout=0.2) == "byte2\n"
    assert reader_bytes.read_line(timeout=0.2) is None
    reader_bytes.close()

    # 4. Timeout on slow/empty stream
    slow_queue = queue.Queue()
    def slow_generator():
        val = slow_queue.get()
        if val is not None:
            yield val

    slow_reader = BoundedLineReader(slow_generator())
    # Should raise queue.Empty after 0.05s
    with pytest.raises(queue.Empty):
        slow_reader.read_line(timeout=0.05)

    # Deliver line, should return promptly
    slow_queue.put("delayed_line\n")
    assert slow_reader.read_line(timeout=0.2) == "delayed_line\n"

    # Close and ensure cleanup
    slow_queue.put(None)
    slow_reader.close()
    assert slow_reader._closed.is_set()


# ---------------------------------------------------------------------------
# Scenario M: Test 1 - Unknown process status yields ERROR (no false COMPLETE)
# ---------------------------------------------------------------------------

def test_scenario_m_unknown_process_status_yields_error():
    """Test 1: Subprocess emits terminal event, but remains active (proc.poll() is None).
    Adapter must NOT emit COMPLETE with exit_code 0.
    Must emit AgentEventType.ERROR indicating process remained active.
    """
    cmd = [
        sys.executable,
        "-c",
        (
            "import time\n"
            "print('{\"event\": \"result\", \"status\": \"success\", \"response\": \"Done\"}', flush=True)\n"
            "time.sleep(10)\n"
        ),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    adapter = AntigravityAdapter()
    t0 = time.time()
    events = list(adapter._decode_stream_events(
        stream=proc.stdout,
        start_time=t0,
        get_returncode=lambda wait_timeout=None: proc.poll(),
        get_stderr=lambda: "",
    ))
    proc.kill()
    proc.wait()

    assert events[-1].event_type == AgentEventType.ERROR
    assert events[-1].result.exit_code == 1
    assert "process still active" in events[-1].result.stderr.lower() or "active" in str(events[-1].result.metadata)


# ---------------------------------------------------------------------------
# Scenario N: Test 2 - Late nonzero exit captured as ERROR
# ---------------------------------------------------------------------------

def test_scenario_n_late_nonzero_exit_captured():
    """Test 2: Subprocess emits terminal event, then pauses and exits with code 2.
    Adapter must wait boundedly and capture the non-zero exit code as an ERROR.
    """
    cmd = [
        sys.executable,
        "-c",
        (
            "import time, sys\n"
            "print('{\"type\": \"text\", \"part\": {\"text\": \"Done\"}}', flush=True)\n"
            "print('{\"type\": \"step_finish\", \"part\": {\"reason\": \"stop\"}}', flush=True)\n"
            "time.sleep(0.3)\n"
            "sys.stderr.write('Fatal post-generation failure\\n')\n"
            "sys.exit(2)\n"
        ),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    adapter = OpenCodeAdapter()
    t0 = time.time()
    events = list(adapter._decode_stream_events(
        stream=proc.stdout,
        start_time=t0,
        get_returncode=lambda wait_timeout=None: proc.poll() if not wait_timeout else (proc.wait(timeout=wait_timeout) if proc.poll() is None else proc.poll()),
        get_stderr=lambda: "Fatal post-generation failure",
    ))
    proc.wait()

    assert events[-1].event_type == AgentEventType.ERROR
    assert events[-1].result.exit_code == 2
    assert "Fatal post-generation failure" in events[-1].result.stderr


# ---------------------------------------------------------------------------
# Scenario O: Test 4 - Child holds stderr open does not deadlock cleanup
# ---------------------------------------------------------------------------

def test_scenario_o_child_holds_stderr_no_deadlock():
    """Test 4: Child process inherits stderr and holds it open.
    StderrDrainer.close() must complete cleanly in < 0.5s without blocking for child duration.
    """
    cmd = [
        sys.executable,
        "-c",
        (
            "import subprocess, sys\n"
            "subprocess.Popen(['sleep', '5'], stderr=sys.stderr)\n"
            "sys.stderr.write('parent stderr\\n')\n"
            "sys.exit(0)\n"
        ),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    drainer = StderrDrainer(proc.stderr)
    proc.wait()
    t0 = time.time()
    drainer.close(timeout=0.2)
    elapsed = time.time() - t0

    assert elapsed < 0.5
    assert "parent stderr" in drainer.get_stderr()


# ---------------------------------------------------------------------------
# Scenario P: Test 5 - Terminal event with trailing required output preserved
# ---------------------------------------------------------------------------

def test_scenario_p_terminal_event_trailing_output_preserved():
    """Test 5: Trailing lines emitted during bounded drain window are preserved."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": "# Initial\n"}}),
        json.dumps({"event": "result", "status": "success"}),
        json.dumps({"event": "step_update", "step_update": {"text_delta": "```yaml\nROLE: PLANNER\nSTATUS: SUCCESS\n```\n"}}),
    ]

    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=lambda wait_timeout=None: 0,
        get_stderr=lambda: "",
    ))

    assert events[-1].event_type == AgentEventType.COMPLETE
    assert "ROLE: PLANNER" in events[-1].result.stdout


# ---------------------------------------------------------------------------
# Scenario Q: Test 6 - No valid terminal event yields ERROR
# ---------------------------------------------------------------------------

def test_scenario_q_no_valid_terminal_event_yields_error():
    """Test 6: Process emits text and exits 0 but never produces a terminal event.
    Must emit AgentEventType.ERROR indicating unexpected EOF before terminal event.
    """
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "text", "part": {"text": "Just talking without finishing..."}}),
    ]

    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=lambda wait_timeout=None: 0,
        get_stderr=lambda: "",
    ))

    assert events[-1].event_type == AgentEventType.ERROR
    assert events[-1].result.exit_code == 1
    assert "Unexpected EOF before terminal event" in events[-1].text


# ---------------------------------------------------------------------------
# Scenario R: Test 7 - Blocked reader cleanup bounded
# ---------------------------------------------------------------------------

def test_scenario_r_blocked_reader_cleanup_bounded():
    """Test 7: Background thread blocked on pipe reader terminates within bounded timeout."""
    slow_q = queue.Queue()
    def slow_stream():
        item = slow_q.get()
        if item:
            yield item

    reader = BoundedLineReader(slow_stream())
    t0 = time.time()
    reader.close(timeout=0.2)
    elapsed = time.time() - t0

    assert elapsed < 0.5
    slow_q.put(None)


# ---------------------------------------------------------------------------
# Scenario S: Test 8 - Continuous output does not extend drain deadline
# ---------------------------------------------------------------------------

def test_scenario_s_continuous_output_does_not_extend_deadline():
    """Test 8: Endless output emitted after terminal event must not extend drain deadline indefinitely."""
    adapter = AntigravityAdapter()

    def endless_stream():
        yield json.dumps({"event": "step_update", "step_update": {"text_delta": "Start"}})
        yield json.dumps({"event": "result", "status": "success"})
        while True:
            time.sleep(0.02)
            yield json.dumps({"event": "step_update", "step_update": {"text_delta": "more"}})

    t0 = time.time()
    events = list(adapter._decode_stream_events(
        stream=endless_stream(),
        start_time=t0,
        get_returncode=lambda wait_timeout=None: 0,
        get_stderr=lambda: "",
    ))
    elapsed = time.time() - t0

    # Must cut off after drain timeout (~1.0s), not run forever
    assert elapsed < 2.0
    assert events[-1].event_type == AgentEventType.COMPLETE


# ---------------------------------------------------------------------------
# Scenario T: Test 9 - Malformed and split output resilience
# ---------------------------------------------------------------------------

def test_scenario_t_malformed_and_split_output_resilience():
    """Test 9: Malformed JSON, empty lines, and non-dict payloads are safely ignored."""
    adapter = AntigravityAdapter()
    stream = [
        "not json at all\n",
        "\n",
        "   \n",
        json.dumps(["a", "list", "not", "dict"]),
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Valid content\n"}}),
        json.dumps({"event": "result", "status": "success"}),
    ]

    events = list(adapter._decode_stream_events(
        stream=stream,
        start_time=time.time(),
        get_returncode=lambda wait_timeout=None: 0,
        get_stderr=lambda: "",
    ))

    assert events[-1].event_type == AgentEventType.COMPLETE
    assert "Valid content" in events[-1].result.stdout


# ---------------------------------------------------------------------------
# Scenario U: Test 12 - Full end-to-end stage lifecycle with unclosed child pipe
# ---------------------------------------------------------------------------

def test_scenario_u_full_lifecycle_stage_success(tmp_path):
    """Test 12: Controlled end-to-end Stage.run() execution with real subprocess
    spawning a background sleeper inheriting stdout, verifying prompt completion,
    protocol validation, and paired artifact persistence.
    """
    context, run_mgr = _make_context(tmp_path)
    role = Role(
        name="planner",
        sequence_number=2,
        template_content="Planner template",
        protocol_content="Emit machine report.",
    )

    valid_markdown = (
        "# Implementation Plan\nAll steps defined.\n\n"
        "```yaml\n"
        "ROLE: PLANNER\n"
        "STATUS: APPROVED\n"
        "HANDOFF: ARCHITECT\n"
        "```\n"
    )

    cmd = [
        sys.executable,
        "-c",
        (
            "import subprocess, sys, time, json\n"
            "subprocess.Popen(['sleep', '5.0'], stdout=sys.stdout)\n"
            f"payload = json.dumps({{'event': 'step_update', 'step_update': {{'text_delta': {repr(valid_markdown)}}}}})\n"
            "print(payload, flush=True)\n"
            "print('{\"event\": \"result\", \"status\": \"success\"}', flush=True)\n"
            "sys.exit(0)\n"
        ),
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    adapter = AntigravityAdapter()

    t0 = time.time()
    events = list(adapter._decode_stream_events(
        stream=proc.stdout,
        start_time=t0,
        get_returncode=lambda wait_timeout=None: proc.poll(),
        get_stderr=lambda: "",
    ))
    elapsed = time.time() - t0
    proc.wait()

    assert elapsed < 1.5
    assert events[-1].event_type == AgentEventType.COMPLETE
    assert events[-1].result.exit_code == 0

    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)
    with patch.object(stage, "_get_event_stream", return_value=iter(events)):
        result = stage.run(context)

    assert result.success is True
    assert result.status == "APPROVED"
    assert result.handoff == "ARCHITECT"

    md_file = context.run.run_dir / "02_planner.md"
    json_file = context.run.run_dir / "02_planner.json"
    assert md_file.exists()
    assert json_file.exists()
