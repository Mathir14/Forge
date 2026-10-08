"""Antigravity (agy) CLI adapter."""

import errno
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Set, Iterator, Iterable, Callable, Tuple
from forge.adapters.base import BaseAdapter, AdapterResponse, StderrDrainer
from forge.core.capabilities import Capability
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.core.platform import get_process_group_flags, prepare_command, IS_WINDOWS

logger = logging.getLogger(__name__)


class AntigravityAdapter(BaseAdapter):
    CAPABILITIES: Set[str] = {
        Capability.CODE_READ,
        Capability.CODE_EDIT,
        Capability.SHELL,
        Capability.GIT,
        Capability.STRUCTURED_OUTPUT,
        Capability.LONG_RUNNING,
        Capability.CUSTOM_FLAGS,
    }
    DEFAULT_MODEL = "gemini-3.7-flash-high"
    DEFAULT_EFFORT = "high"
    MAX_PROMPT_BYTES: int = 130000  # Provider CLI argument limit bounded by POSIX OS MAX_ARG_STRLEN (131,072 bytes)
    WINDOWS_CMD_MAX_PROMPT_BYTES: int = 7500  # Windows cmd.exe 8,191 char limit minus flags
    WINDOWS_EXE_MAX_PROMPT_BYTES: int = 30000  # Windows CreateProcessW 32,767 char limit minus flags

    @property
    def max_prompt_bytes(self) -> int:
        """Platform-aware maximum prompt size in bytes supported by argv transport."""
        if IS_WINDOWS:
            bin_path = self._get_binary()
            if bin_path and (bin_path.lower().endswith(".cmd") or bin_path.lower().endswith(".bat")):
                return self.WINDOWS_CMD_MAX_PROMPT_BYTES
            return self.WINDOWS_EXE_MAX_PROMPT_BYTES
        return self.MAX_PROMPT_BYTES

    def __init__(
        self,
        model: Optional[str] = "gemini-3.7-flash-high",
        effort: Optional[str] = "high",
        auto_approve: bool = False,
        extra_flags: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name="antigravity",
            model=model,
            effort=effort,
            auto_approve=auto_approve,
            extra_flags=extra_flags,
        )

    def _get_binary(self) -> Optional[str]:
        return shutil.which("agy") or shutil.which("antigravity")

    def is_available(self) -> bool:
        return self._get_binary() is not None

    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        bin_path = self._get_binary()
        if not bin_path:
            return AdapterResponse(
                stdout="",
                stderr="Neither 'agy' nor 'antigravity' was found in PATH.",
                exit_code=1,
                duration_seconds=0.0,
                raw_output="Neither 'agy' nor 'antigravity' was found in PATH.",
            )

        prompt_bytes = len(prompt.encode("utf-8"))
        effective_limit = self.max_prompt_bytes
        if prompt_bytes > effective_limit:
            err_msg = (
                f"Prompt size ({prompt_bytes} bytes) exceeds Antigravity CLI argv transport limit "
                f"({effective_limit} bytes). Provider requires command-line argument transport limited by host OS constraints."
            )
            logging.error(err_msg)
            return AdapterResponse(
                stdout="",
                stderr=err_msg,
                exit_code=1,
                duration_seconds=0.0,
                raw_output=err_msg,
            )

        timeout_val = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        work_dir = cwd or Path.cwd()
        cmd: List[str] = [bin_path, "-p", prompt, "--output-format", "text"]

        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort:
            cmd.extend(["--effort", self.effort])
        if self.auto_approve:
            cmd.append("--dangerously-skip-permissions")
        if timeout_val:
            cmd.extend(["--print-timeout", f"{timeout_val}s"])
        if self.extra_flags:
            cmd.extend(self._render_extra_flags())
        start_time = time.time()
        try:
            stdout, stderr, returncode = self._run_subprocess(
                cmd,
                input_data=None,
                cwd=work_dir,
                timeout=timeout_val,
            )
            duration = time.time() - start_time
            raw = stdout if stdout else stderr
            return AdapterResponse(
                stdout=stdout,
                stderr=stderr,
                exit_code=returncode,
                duration_seconds=duration,
                raw_output=raw,
            )
        except subprocess.TimeoutExpired as e:
            duration = time.time() - start_time
            return self._create_timeout_response(e, timeout_val, duration)
        except KeyboardInterrupt:
            duration = time.time() - start_time
            logging.warning("Execution interrupted by user.")
            return AdapterResponse(
                stdout="",
                stderr="Execution interrupted by user (SIGINT).",
                exit_code=130,
                duration_seconds=duration,
                raw_output="Execution interrupted by user (SIGINT).",
            )
        except (OSError, UnicodeDecodeError) as e:
            duration = time.time() - start_time
            import errno
            if isinstance(e, OSError) and e.errno == errno.E2BIG:
                err_msg = f"Prompt exceeds maximum OS command-line argument length (E2BIG): {e}"
            else:
                err_msg = f"OS/Encoding error executing antigravity: {e}"
            logging.error(err_msg)
            return AdapterResponse(
                stdout="",
                stderr=err_msg,
                exit_code=1,
                duration_seconds=duration,
                raw_output=err_msg,
            )
        except Exception as e:
            duration = time.time() - start_time
            logging.error("Unexpected error executing antigravity: %s", e)
            return AdapterResponse(
                stdout="",
                stderr=str(e),
                exit_code=1,
                duration_seconds=duration,
                raw_output=f"Error executing antigravity: {e}",
            )

    @staticmethod
    def _decode_text_chunk(
        payload: Dict[str, Any],
        ts: float,
        session_id: Optional[str],
    ) -> Optional[Tuple[AgentEvent, str]]:
        """Decode text delta payloads from Antigravity stream."""
        step_update = payload.get("step_update") if isinstance(payload.get("step_update"), dict) else {}
        part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
        text_chunk = (
            step_update.get("text_delta")
            or step_update.get("text")
            or payload.get("text_delta")
            or payload.get("text")
            or part.get("text")
            or part.get("text_delta")
            or ""
        )
        if not text_chunk:
            return None
        event = AgentEvent(
            event_type=AgentEventType.CHUNK,
            timestamp=ts,
            text=text_chunk,
            data={"session_id": session_id} if session_id else {},
        )
        return event, text_chunk

    @staticmethod
    def _decode_tool_event(
        event_type: str,
        payload: Dict[str, Any],
        ts: float,
        session_id: Optional[str],
    ) -> Optional[AgentEvent]:
        """Decode tool invocation payloads from Antigravity stream."""
        step_update = payload.get("step_update") if isinstance(payload.get("step_update"), dict) else {}
        tool_info = (
            step_update.get("tool_info")
            if isinstance(step_update.get("tool_info"), dict)
            else (payload.get("tool_info") if isinstance(payload.get("tool_info"), dict) else {})
        )
        part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
        state = part.get("state") if isinstance(part.get("state"), dict) else {}

        tool_name = (
            step_update.get("tool_name")
            or tool_info.get("tool_name")
            or tool_info.get("tool")
            or tool_info.get("name")
            or payload.get("tool_name")
            or payload.get("tool")
            or payload.get("name")
            or part.get("tool")
            or part.get("name")
            or "tool"
        )
        call_id = (
            step_update.get("call_id")
            or step_update.get("callID")
            or step_update.get("id")
            or tool_info.get("call_id")
            or tool_info.get("callID")
            or tool_info.get("id")
            or payload.get("call_id")
            or payload.get("callID")
            or payload.get("id")
            or part.get("callID")
            or part.get("call_id")
            or (str(step_update.get("step_index")) if step_update.get("step_index") is not None else "")
        )
        tool_input = (
            tool_info.get("parameters")
            or tool_info.get("input")
            or tool_info.get("args")
            or step_update.get("parameters")
            or step_update.get("input")
            or step_update.get("args")
            or payload.get("input")
            or payload.get("parameters")
            or payload.get("args")
            or state.get("input")
            or {}
        )
        tool_output = (
            tool_info.get("output")
            or tool_info.get("result")
            or step_update.get("output")
            or step_update.get("result")
            or payload.get("output")
            or payload.get("result")
            or state.get("output")
        )
        raw_status = (
            tool_info.get("status")
            or step_update.get("state")
            or step_update.get("status")
            or payload.get("status")
            or state.get("status")
        )
        status_str = str(raw_status).lower() if raw_status is not None else ""
        is_err = bool(
            tool_info.get("error")
            or step_update.get("error")
            or payload.get("error")
            or part.get("error")
            or status_str in ("error", "failed")
        )

        is_start = (
            event_type in ("tool_start", "tool_call")
            or status_str in ("running", "active", "start")
            or (raw_status is None and tool_output is None and event_type not in ("tool_finish", "tool_result"))
        )

        if is_start:
            return AgentEvent(
                event_type=AgentEventType.TOOL_START,
                timestamp=ts,
                text=tool_name,
                data={
                    "tool": tool_name,
                    "call_id": call_id,
                    "input": tool_input,
                    "session_id": session_id,
                },
            )
        else:
            norm_status = "completed" if status_str in ("done", "completed") else (raw_status or ("error" if is_err else "completed"))
            if is_err:
                norm_status = "error"
            return AgentEvent(
                event_type=AgentEventType.TOOL_FINISH,
                timestamp=ts,
                text=tool_name,
                data={
                    "tool": tool_name,
                    "call_id": call_id,
                    "status": norm_status,
                    "output": tool_output,
                    "input": tool_input,
                    "error": is_err,
                    "session_id": session_id,
                },
            )

    @staticmethod
    def _build_complete_event(
        ts: float,
        duration: float,
        stdout: str,
        stderr: str,
        raw_output: str,
        token_usage: Dict[str, int],
        metadata: Dict[str, Any],
        exit_code: int = 0,
    ) -> AgentEvent:
        """Construct a COMPLETE AgentEvent with its ExecutionResult."""
        exec_res = ExecutionResult(
            exit_code=exit_code,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr,
            raw_output=raw_output,
            token_usage=dict(token_usage),
            metadata=metadata,
        )
        return AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=ts,
            text=stdout,
            result=exec_res,
        )

    @staticmethod
    def _build_error_event(
        ts: float,
        duration: float,
        error_msg: str,
        stdout: str,
        stderr: str,
        raw_output: str,
        token_usage: Dict[str, int],
        metadata: Dict[str, Any],
        exit_code: int = 1,
    ) -> AgentEvent:
        """Construct an ERROR AgentEvent with its ExecutionResult."""
        exec_res = ExecutionResult(
            exit_code=exit_code,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr or error_msg,
            raw_output=raw_output,
            token_usage=dict(token_usage),
            metadata=metadata,
        )
        return AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=ts,
            text=error_msg,
            result=exec_res,
        )

    def _decode_stream_events(
        self,
        stream: Iterable[str],
        start_time: float,
        get_returncode: Optional[Callable[[], Optional[int]]] = None,
        get_stderr: Optional[Callable[[], str]] = None,
    ) -> Iterator[AgentEvent]:
        """Decode Antigravity NDJSON stdout lines into canonical AgentEvents.

        Handles:
            - event: "step_update" (text_delta) -> AgentEventType.CHUNK
            - event: "step_update" (tool_info running) / "tool_call" -> AgentEventType.TOOL_START
            - event: "step_update" (tool_info completed) / "tool_finish" -> AgentEventType.TOOL_FINISH
            - event: "result" / "complete" (status=success) -> AgentEventType.COMPLETE
            - event: "result" (status=failed) / "error" -> AgentEventType.ERROR
            - event: "heartbeat" / "ping" -> AgentEventType.HEARTBEAT
            - event: "init" / "step_start" -> logged at debug, ignored
            - Unknown provider events -> logged as warning, ignored
            - Malformed JSON payloads -> logged as warning, ignored
            - Duplicate terminal events -> logged as warning, dropped
            - Unexpected EOF -> AgentEventType.ERROR with diagnostic
        """
        accumulated_stdout = ""
        accumulated_raw = ""
        token_usage: Dict[str, int] = {}
        session_id: Optional[str] = None
        has_stopped_step = False
        terminal_status: Optional[str] = None
        terminal_error_msg: Optional[str] = None
        terminal_event_emitted = False

        for raw_line in stream:
            accumulated_raw += raw_line
            line = raw_line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Malformed JSON payload from Antigravity stream: %s", line)
                continue

            if not isinstance(payload, dict):
                logger.warning("Unexpected non-dictionary payload from Antigravity stream: %s", line)
                continue

            event_type = payload.get("event") or payload.get("type")
            step_update_data = payload.get("step_update") if isinstance(payload.get("step_update"), dict) else {}
            part_data = payload.get("part") if isinstance(payload.get("part"), dict) else {}

            if not session_id:
                session_id = (
                    payload.get("session_id")
                    or payload.get("sessionID")
                    or payload.get("conversation_id")
                    or step_update_data.get("conversation_id")
                )

            raw_ts = payload.get("timestamp") or step_update_data.get("timestamp")
            if isinstance(raw_ts, (int, float)):
                ts = raw_ts / 1000.0 if raw_ts > 1e11 else float(raw_ts)
            else:
                ts = time.time()

            # Track intermediate token usage if provided in step metadata
            step_usage = step_update_data.get("usage") or payload.get("usage")
            if isinstance(step_usage, dict):
                for k, v in step_usage.items():
                    if isinstance(v, int):
                        token_usage[k] = v

            if terminal_event_emitted:
                if event_type in ("result", "complete", "error", "fatal", "step_finish"):
                    logger.warning(
                        "Duplicate terminal event received from Antigravity stream; ignoring",
                        extra={
                            "adapter": self.name,
                            "event_type": str(event_type),
                            "raw_payload": line,
                        },
                    )
                    continue

            # 1. Text chunks
            has_text = bool(
                payload.get("text_delta")
                or payload.get("text")
                or step_update_data.get("text_delta")
                or step_update_data.get("text")
                or part_data.get("text")
                or part_data.get("text_delta")
            )
            if event_type in ("step_update", "text", "text_delta") and has_text:
                if has_stopped_step:
                    accumulated_stdout = ""
                    has_stopped_step = False
                res = self._decode_text_chunk(payload, ts, session_id)
                if res:
                    chunk_event, chunk_text = res
                    accumulated_stdout += chunk_text
                    yield chunk_event
                continue

            # 2. Tool events (either tool_info in step_update or explicit tool event types)
            has_tool_info = (
                "tool_info" in payload
                or "tool_info" in step_update_data
                or step_update_data.get("step_type") == "tool"
            )
            if (
                event_type in ("tool_call", "tool_start", "tool_finish", "tool_result", "tool_use")
                or (event_type == "step_update" and has_tool_info)
            ):
                tool_evt = self._decode_tool_event(str(event_type), payload, ts, session_id)
                if tool_evt:
                    if tool_evt.event_type == AgentEventType.TOOL_START:
                        if accumulated_stdout and not has_stopped_step:
                            # Pre-tool conversational preface; purge
                            accumulated_stdout = ""
                        has_stopped_step = False
                    yield tool_evt
                continue

            # 3. Heartbeat
            if event_type in ("heartbeat", "ping"):
                yield AgentEvent(
                    event_type=AgentEventType.HEARTBEAT,
                    timestamp=ts,
                    data=payload,
                )
                continue

            # 4. Result / Complete (informational only under ADR-017)
            if event_type in ("result", "complete"):
                terminal_event_emitted = True
                has_stopped_step = True

                res_dict = payload.get("result") if isinstance(payload.get("result"), dict) else {}
                raw_tokens = payload.get("usage") or payload.get("token_usage") or res_dict.get("usage") or res_dict.get("token_usage")
                if isinstance(raw_tokens, dict):
                    for k, v in raw_tokens.items():
                        if isinstance(v, int):
                            token_usage[k] = v

                # Extract canonical full response if emitted in result event
                final_response = res_dict.get("response") or payload.get("response")
                if final_response and isinstance(final_response, str) and not accumulated_stdout:
                    accumulated_stdout = final_response

                status = payload.get("status") or res_dict.get("status")
                status_lower = str(status).lower() if status is not None else ""
                terminal_status = status_lower
                if not session_id:
                    session_id = res_dict.get("conversation_id") or res_dict.get("session_id")

                if status_lower in ("failed", "error"):
                    err_msg = (
                        payload.get("error")
                        or payload.get("message")
                        or res_dict.get("error")
                        or res_dict.get("message")
                        or f"Execution failed with status: {status}"
                    )
                    if isinstance(err_msg, dict):
                        err_msg = err_msg.get("message") or str(err_msg)
                    terminal_error_msg = str(err_msg)
                continue

            # 5. Step finish (compatibility with general NDJSON streams)
            if event_type == "step_finish":
                part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
                reason = part.get("reason") or payload.get("reason")
                raw_tokens = part.get("tokens") or payload.get("tokens") or payload.get("usage")
                if isinstance(raw_tokens, dict):
                    for k, v in raw_tokens.items():
                        if isinstance(v, int):
                            token_usage[k] = v

                if reason in ("tool-calls", "tool_calls", "tool_use", "continue"):
                    continue
                elif reason in ("error", "failed", "aborted", "cancelled"):
                    terminal_event_emitted = True
                    terminal_status = "error"
                    err_msg = part.get("error") or payload.get("error") or f"Step finished with error: {reason}"
                    terminal_error_msg = str(err_msg)
                    continue
                else:
                    if has_stopped_step:
                        logger.warning(
                            "Duplicate terminal event received from Antigravity stream; ignoring",
                            extra={"adapter": self.name, "raw_event": payload},
                        )
                        continue
                    has_stopped_step = True
                    continue

            # 6. Error / Fatal
            if event_type in ("error", "fatal"):
                terminal_event_emitted = True
                terminal_status = "error"
                err_data = payload.get("error") or payload.get("message") or "Antigravity error event"
                err_msg = err_data.get("message") if isinstance(err_data, dict) else str(err_data)
                terminal_error_msg = str(err_msg)
                continue

            # 7. Normal lifecycle chatter
            if event_type in ("init", "step_start") or event_type == "step_update":
                logger.debug("Antigravity lifecycle event received: %s", event_type)
                continue

            # 8. Unknown event
            logger.warning(
                "Unrecognized provider event ignored: %s",
                event_type,
                extra={"adapter": self.name, "raw_event": payload},
            )
            continue

        # Post-mortem completion evaluation (ADR-017)
        duration = time.time() - start_time
        proc_code = get_returncode() if get_returncode else None
        stderr_out = get_stderr() if get_stderr else ""

        if proc_code is not None and proc_code != 0:
            err_msg = stderr_out or terminal_error_msg or f"Antigravity process exited with non-zero exit code: {proc_code}"
            yield self._build_error_event(
                ts=time.time(),
                duration=duration,
                error_msg=err_msg,
                stdout=accumulated_stdout,
                stderr=stderr_out or err_msg,
                raw_output=accumulated_raw,
                token_usage=token_usage,
                metadata={"diagnostic": f"Process exited with code {proc_code}"},
                exit_code=proc_code,
            )
        elif terminal_status in ("failed", "error") or terminal_error_msg is not None:
            err_msg = terminal_error_msg or f"Execution failed with status: {terminal_status}"
            yield self._build_error_event(
                ts=time.time(),
                duration=duration,
                error_msg=err_msg,
                stdout=accumulated_stdout,
                stderr=stderr_out or err_msg,
                raw_output=accumulated_raw,
                token_usage=token_usage,
                metadata={"error": err_msg, "status": terminal_status},
                exit_code=1,
            )
        elif (has_stopped_step or terminal_status in ("success", "completed", "done")) and (proc_code is None or proc_code == 0):
            yield self._build_complete_event(
                ts=time.time(),
                duration=duration,
                stdout=accumulated_stdout,
                stderr=stderr_out,
                raw_output=accumulated_raw,
                token_usage=token_usage,
                metadata={"session_id": session_id} if session_id else {},
                exit_code=0,
            )
        else:
            exit_code = proc_code if (proc_code is not None and proc_code != 0) else 1
            diag_msg = "Unexpected EOF before terminal event."
            yield self._build_error_event(
                ts=time.time(),
                duration=duration,
                error_msg=diag_msg,
                stdout=accumulated_stdout,
                stderr=stderr_out or diag_msg,
                raw_output=accumulated_raw,
                token_usage=token_usage,
                metadata={"diagnostic": "Provider stream terminated prematurely without emitting COMPLETE or ERROR."},
                exit_code=exit_code,
            )

    def iter_events(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> Iterator[AgentEvent]:
        """Progressive event generator consuming Antigravity's native stream-json event stream.

        Spawns agy with `--output-format stream-json` and translates NDJSON stream events into
        canonical Forge AgentEvents (CHUNK, TOOL_START, TOOL_FINISH, COMPLETE, ERROR).
        """
        bin_path = self._get_binary()
        if not bin_path:
            err_msg = "Neither 'agy' nor 'antigravity' was found in PATH."
            yield AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text=err_msg,
                result=ExecutionResult(
                    exit_code=1,
                    duration_seconds=0.0,
                    stderr=err_msg,
                    raw_output=err_msg,
                ),
            )
            return

        prompt_bytes = len(prompt.encode("utf-8"))
        effective_limit = self.max_prompt_bytes
        if prompt_bytes > effective_limit:
            err_msg = (
                f"Prompt size ({prompt_bytes} bytes) exceeds Antigravity CLI argv transport limit "
                f"({effective_limit} bytes). Provider requires command-line argument transport limited by host OS constraints."
            )
            logger.error(err_msg)
            yield AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text=err_msg,
                result=ExecutionResult(
                    exit_code=1,
                    duration_seconds=0.0,
                    stderr=err_msg,
                    raw_output=err_msg,
                ),
            )
            return

        timeout_val = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        work_dir = cwd or Path.cwd()
        cmd: List[str] = [bin_path, "-p", prompt, "--output-format", "stream-json"]

        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort:
            cmd.extend(["--effort", self.effort])
        if self.auto_approve:
            cmd.append("--dangerously-skip-permissions")
        if timeout_val:
            cmd.extend(["--print-timeout", f"{timeout_val}s"])
        if self.extra_flags:
            cmd.extend(self._render_extra_flags())

        start_time = time.time()

        if logger.isEnabledFor(logging.DEBUG):
            try:
                debug_dir = work_dir / ".forge" / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / "antigravity_command.txt").write_text(
                    " ".join(cmd),
                    encoding="utf-8",
                )
            except Exception:
                pass

        popen_kwargs: Dict[str, Any] = {
            "cwd": work_dir,
            "stdin": None,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        popen_kwargs.update(get_process_group_flags())

        proc: Optional[subprocess.Popen] = None
        stderr_drainer: Optional[StderrDrainer] = None
        try:
            prepared_cmd = prepare_command(cmd)
            proc = subprocess.Popen(prepared_cmd, **popen_kwargs)
            self._register_proc(proc, instance=self)
            stderr_drainer = StderrDrainer(proc.stderr)

            def _get_returncode() -> Optional[int]:
                if proc is not None:
                    try:
                        return proc.wait(timeout=1.0)
                    except Exception:
                        return proc.poll()
                return None

            def _get_stderr() -> str:
                return stderr_drainer.get_stderr() if stderr_drainer is not None else ""

            if proc.stdout is not None:
                yield from self._decode_stream_events(
                    stream=proc.stdout,
                    start_time=start_time,
                    get_returncode=_get_returncode,
                    get_stderr=_get_stderr,
                )

        except KeyboardInterrupt:
            duration = time.time() - start_time
            logger.warning("Execution interrupted by user.")
            yield AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text="Execution interrupted by user (SIGINT).",
                result=ExecutionResult(
                    exit_code=130,
                    duration_seconds=duration,
                    stderr="Execution interrupted by user (SIGINT).",
                    raw_output="Execution interrupted by user (SIGINT).",
                ),
            )
        except (OSError, UnicodeDecodeError) as e:
            duration = time.time() - start_time
            if isinstance(e, OSError) and e.errno == errno.E2BIG:
                err_msg = f"Prompt exceeds maximum OS command-line argument length (E2BIG): {e}"
            else:
                err_msg = f"OS/Encoding error executing antigravity: {e}"
            logger.error(err_msg)
            yield AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text=err_msg,
                result=ExecutionResult(
                    exit_code=1,
                    duration_seconds=duration,
                    stderr=err_msg,
                    raw_output=err_msg,
                ),
            )
        except Exception as e:
            duration = time.time() - start_time
            logger.error("Unexpected error executing antigravity: %s", e)
            yield AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text=str(e),
                result=ExecutionResult(
                    exit_code=1,
                    duration_seconds=duration,
                    stderr=str(e),
                    raw_output=f"Error executing antigravity: {e}",
                ),
            )
        finally:
            if proc is not None:
                self._safe_cleanup_subprocess(proc)
            if stderr_drainer is not None:
                stderr_drainer.close()
            if proc is not None:
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
                self._unregister_proc(proc, instance=self)

    execute_events = iter_events
