import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Set, Iterator, Iterable, Callable, Tuple

from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.core.capabilities import Capability
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult

logger = logging.getLogger(__name__)


def is_wsl() -> bool:
    """Return True if Forge is running under Windows Subsystem for Linux (WSL)."""
    if sys.platform != "linux":
        return False
    if "WSL_DISTRO_NAME" in os.environ or "WSL_INTEROP" in os.environ:
        return True
    if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists():
        return True
    try:
        release = platform.release().lower()
        if "microsoft" in release or "wsl" in release:
            return True
    except Exception:
        pass
    try:
        proc_version = Path("/proc/version")
        if proc_version.exists():
            text = proc_version.read_text(encoding="utf-8", errors="replace").lower()
            if "microsoft" in text or "wsl" in text:
                return True
    except Exception:
        pass
    return False


def is_windows_executable(path_str: str) -> bool:
    """Determine whether a given executable path is a Windows executable or wrapper shim.

    Distinguishes:
    - Direct Windows executables (.exe, .cmd, .bat, .ps1).
    - Windows PE binaries (MZ magic bytes).
    - Windows wrapper shims (npm/scoop/chocolatey scripts that execute .exe or have sibling .cmd/.bat/.exe files).
    - Windows filesystem paths (/mnt/c/Users/.../AppData/Roaming/npm).

    Crucially, genuine Linux ELF executables (starting with \\x7fELF) or standard
    Linux shell scripts (without Windows shim indicators) are NOT classified as Windows
    binaries even if located on a mounted volume (e.g., /mnt/data/bin/opencode).
    """
    if not path_str:
        return False

    try:
        p = Path(path_str)
        resolved = p.resolve()
    except Exception:
        p = Path(path_str)
        resolved = p

    # 1. Direct extension check on original or resolved path
    windows_exts = {".exe", ".cmd", ".bat", ".ps1"}
    if p.suffix.lower() in windows_exts or resolved.suffix.lower() in windows_exts:
        return True

    # 2. Check binary header for PE (MZ) or ELF magic bytes
    if resolved.is_file():
        try:
            with open(resolved, "rb") as f:
                header = f.read(4)
                if header.startswith(b"MZ"):
                    return True
                if header.startswith(b"\x7fELF"):
                    # Definite Linux ELF executable - safe, not a Windows binary
                    return False
        except (OSError, PermissionError):
            pass

    # 3. Check for sibling Windows shims in the same directory (e.g., npm generates opencode.cmd alongside opencode)
    for ext in windows_exts:
        try:
            if p.with_suffix(ext).is_file() or resolved.with_suffix(ext).is_file():
                return True
        except (OSError, PermissionError):
            pass

    # 4. Check for paths located under Windows mounts (/mnt/[a-z]/...) containing Windows directory structures
    parts_lower = [part.lower() for part in resolved.parts]
    if len(parts_lower) >= 3 and parts_lower[1] == "mnt" and len(parts_lower[2]) == 1:
        if any(w_dir in parts_lower for w_dir in ("appdata", "program files", "program files (x86)", "windows", "users")):
            return True

    # 5. Inspect script content if it's a text/wrapper script
    if resolved.is_file():
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")[:4096]
            lower = content.lower()
            if any(marker in lower for marker in ("opencode.exe", "cmd.exe", "%~dp0", "appdata\\roaming\\npm", "appdata/roaming/npm")):
                return True
            if re.search(r'\b[a-zA-Z0-9_\-]+\.exe\b', lower):
                return True
        except (OSError, PermissionError, UnicodeDecodeError):
            pass

    return False


class OpenCodeAdapter(BaseAdapter):
    CAPABILITIES: Set[str] = {
        Capability.CODE_READ,
        Capability.CODE_EDIT,
        Capability.SHELL,
        Capability.GIT,
        Capability.STRUCTURED_OUTPUT,
        Capability.CUSTOM_FLAGS,
    }
    DEFAULT_MODEL: Optional[str] = None
    DEFAULT_EFFORT: Optional[str] = None

    def __init__(
        self,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        auto_approve: bool = False,
        extra_flags: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name="opencode",
            model=model,
            effort=effort,
            auto_approve=auto_approve,
            extra_flags=extra_flags,
        )
        self._availability_error: Optional[str] = None

    @property
    def availability_error(self) -> Optional[str]:
        """Return diagnostic explanation if binary was rejected during resolution."""
        return self._availability_error

    def _find_candidates(self) -> List[str]:
        """Find all executable candidates matching 'opencode' in PATH."""
        candidates: List[str] = []
        path_env = os.environ.get("PATH", "")
        for dir_path in path_env.split(os.pathsep):
            cleaned = dir_path.strip()
            if not cleaned:
                continue
            candidate = Path(cleaned) / "opencode"
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    cand_str = str(candidate)
                    if cand_str not in candidates:
                        candidates.append(cand_str)
            except (OSError, PermissionError):
                continue

        which_result = shutil.which("opencode")
        if which_result and which_result not in candidates:
            candidates.insert(0, which_result)

        return candidates

    def _resolve_binary(self) -> Tuple[Optional[str], Optional[str]]:
        """Resolve OpenCode binary, validating WSL compatibility.

        Returns:
            Tuple of (valid_binary_path, rejection_error_message).
        """
        # If not running under WSL, standard resolution applies (native Linux, macOS, etc.)
        if not is_wsl():
            bin_path = shutil.which("opencode")
            return bin_path, None

        # Under WSL: inspect all PATH candidates to prefer a native Linux OpenCode binary
        candidates = self._find_candidates()

        valid_linux_candidates: List[str] = []
        windows_candidates: List[str] = []

        for cand in candidates:
            if is_windows_executable(cand):
                windows_candidates.append(cand)
            else:
                valid_linux_candidates.append(cand)

        # 1. If a valid native Linux binary was found in PATH, use it
        if valid_linux_candidates:
            return valid_linux_candidates[0], None

        # 2. If no valid Linux binary was found, but a Windows binary was found in WSL PATH, reject it
        if windows_candidates:
            win_bin = windows_candidates[0]
            err = (
                f"OpenCode resolved to a Windows installation while Forge is running inside WSL2:\n\n"
                f"    {win_bin}\n\n"
                f"Forge currently requires a native Linux OpenCode installation inside WSL2.\n\n"
                f"Please install OpenCode natively inside your WSL environment (e.g., via 'npm install -g opencode' "
                f"or the Linux installer) and ensure it appears before Windows PATH entries."
            )
            return None, err

        # 3. No OpenCode binary found in PATH
        return None, None

    def _get_binary(self) -> Optional[str]:
        bin_path, _ = self._resolve_binary()
        return bin_path

    def is_available(self) -> bool:
        bin_path, err = self._resolve_binary()
        if err:
            self._availability_error = err
            return False
        self._availability_error = None
        return bin_path is not None

    def validate_availability(self) -> None:
        """Validate availability and raise an explicit error if rejected or missing."""
        bin_path, err = self._resolve_binary()
        if err:
            raise RuntimeError(err)
        if not bin_path:
            raise RuntimeError(f"Adapter tool '{self.name}' is not installed or not in PATH.")

    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        work_dir = cwd or Path.cwd()
        bin_path, err = self._resolve_binary()
        if err:
            logger.error("OpenCode WSL compatibility error: %s", err)
            return AdapterResponse(
                stdout="",
                stderr=err,
                exit_code=1,
                duration_seconds=0.0,
                raw_output=err,
            )
        bin_path = bin_path or "opencode"
        cmd: List[str] = [bin_path, "run"]

        if self.model:
            cmd.extend(["-m", self.model])
        if self.auto_approve:
            cmd.append("--auto")
        if self.effort:
            cmd.extend(["--variant", self.effort])
        if self.extra_flags:
            cmd.extend(self._render_extra_flags())

        timeout_val = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        start_time = time.time()

        try:
            if logger.isEnabledFor(logging.DEBUG):
                # --------------------------------------------------------------
                # DEBUG: Save the exact command Forge executes.
                # --------------------------------------------------------------
                debug_dir = work_dir / ".forge" / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)

                (debug_dir / "opencode_command.txt").write_text(
                    " ".join(cmd),
                    encoding="utf-8",
                )

            stdout, stderr, returncode = self._run_subprocess(
                cmd,
                input_data=prompt,
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
            logging.error("OS/Encoding error executing opencode: %s", e)
            return AdapterResponse(
                stdout="",
                stderr=str(e),
                exit_code=1,
                duration_seconds=duration,
                raw_output=f"OS/Encoding error executing opencode: {e}",
            )

        except Exception as e:
            duration = time.time() - start_time
            logging.error("Unexpected error executing opencode: %s", e)
            return AdapterResponse(
                stdout="",
                stderr=str(e),
                exit_code=1,
                duration_seconds=duration,
                raw_output=f"Error executing opencode: {e}",
            )

    @staticmethod
    def _decode_text_chunk(
        payload: Dict[str, Any],
        ts: float,
        session_id: Optional[str],
    ) -> Optional[Tuple[AgentEvent, str]]:
        """Decode a text chunk payload into an AgentEvent and raw text string."""
        part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
        text_chunk = (
            part.get("text")
            or part.get("text_delta")
            or payload.get("text")
            or payload.get("text_delta")
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
    ) -> AgentEvent:
        """Decode a tool event payload into a TOOL_START or TOOL_FINISH AgentEvent."""
        part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
        tool_name = part.get("tool") or payload.get("tool") or part.get("name") or payload.get("name") or "tool"
        call_id = part.get("callID") or payload.get("callID") or part.get("call_id") or payload.get("call_id") or ""
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status") or payload.get("status")
        tool_input = state.get("input") or payload.get("input") or {}
        tool_output = state.get("output") or payload.get("output")

        if (
            event_type == "tool_start"
            or status == "running"
            or (status is None and tool_output is None and event_type != "tool_finish")
        ):
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
            return AgentEvent(
                event_type=AgentEventType.TOOL_FINISH,
                timestamp=ts,
                text=tool_name,
                data={
                    "tool": tool_name,
                    "call_id": call_id,
                    "status": status or "completed",
                    "output": tool_output,
                    "input": tool_input,
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
        """Decode OpenCode NDJSON stdout lines into canonical AgentEvents.

        Handles:
            - type: "text" -> AgentEventType.CHUNK
            - type: "tool_use" / "tool_start" -> AgentEventType.TOOL_START
            - type: "tool_use" / "tool_finish" -> AgentEventType.TOOL_FINISH
            - type: "step_finish" (reason=stop) -> AgentEventType.COMPLETE
            - type: "step_finish" (reason=error) / type: "error" -> AgentEventType.ERROR
            - type: "step_start" / "init" -> logged at debug, ignored
            - type: "heartbeat" -> AgentEventType.HEARTBEAT
            - Unknown provider events -> logged as warning, ignored
            - Malformed JSON payloads -> logged as warning, ignored
            - Duplicate terminal events -> logged as warning, dropped
            - Unexpected EOF -> AgentEventType.ERROR with diagnostic
        """
        accumulated_stdout = ""
        accumulated_raw = ""
        token_usage: Dict[str, int] = {}
        terminal_event_emitted = False
        session_id: Optional[str] = None
        has_stopped_step = False

        for raw_line in stream:
            accumulated_raw += raw_line
            line = raw_line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Malformed JSON payload from OpenCode stream: %s", line)
                continue

            if not isinstance(payload, dict):
                logger.warning("Unexpected non-dictionary payload from OpenCode stream: %s", line)
                continue

            event_type = payload.get("type") or payload.get("event")
            if not session_id:
                session_id = payload.get("sessionID") or payload.get("session_id")

            raw_ts = payload.get("timestamp")
            if isinstance(raw_ts, (int, float)):
                ts = raw_ts / 1000.0 if raw_ts > 1e11 else float(raw_ts)
            else:
                ts = time.time()

            if terminal_event_emitted:
                logger.warning(
                    "Duplicate terminal event received from OpenCode stream; ignoring",
                    extra={
                        "adapter": self.name,
                        "event_type": str(event_type),
                        "raw_payload": line,
                    },
                )
                continue

            # If a new step begins after a previous step finished with 'stop',
            # it indicates the previous stop was an intermediate step (e.g. context compaction)
            # and that execution is continuing in a subsequent step.
            # Reset accumulated_stdout so intermediate artifacts do not contaminate the final deliverable.
            if has_stopped_step and event_type in ("step_start", "init"):
                accumulated_stdout = ""
                has_stopped_step = False

            if event_type == "text":
                res = self._decode_text_chunk(payload, ts, session_id)
                if res:
                    chunk_event, chunk_text = res
                    accumulated_stdout += chunk_text
                    yield chunk_event
                continue

            elif event_type in ("tool_use", "tool_start", "tool_finish", "tool_call"):
                yield self._decode_tool_event(str(event_type), payload, ts, session_id)
                continue

            elif event_type == "step_finish":
                part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
                reason = part.get("reason") or payload.get("reason")
                raw_tokens = part.get("tokens") or payload.get("tokens")
                if isinstance(raw_tokens, dict):
                    for k, v in raw_tokens.items():
                        if isinstance(v, int):
                            token_usage[k] = v

                if reason in ("tool-calls", "tool_calls"):
                    continue
                elif reason in ("stop", "complete", "completed", "end"):
                    # Record that an LLM step completed text generation.
                    # Under ADR-017: Do NOT emit COMPLETE in-stream; wait for stdout EOF.
                    if has_stopped_step:
                        logger.warning(
                            "Duplicate terminal event received from OpenCode stream; ignoring",
                            extra={"adapter": self.name, "raw_event": payload},
                        )
                        continue
                    has_stopped_step = True
                    continue
                elif reason in ("error", "failed", "aborted", "cancelled"):
                    terminal_event_emitted = True
                    duration = time.time() - start_time
                    stderr_out = get_stderr() if get_stderr else ""
                    err_msg = part.get("error") or payload.get("error") or f"Step finished with error: {reason}"
                    yield self._build_error_event(
                        ts=ts,
                        duration=duration,
                        error_msg=str(err_msg),
                        stdout=accumulated_stdout,
                        stderr=stderr_out,
                        raw_output=accumulated_raw,
                        token_usage=token_usage,
                        metadata={"error": str(err_msg)},
                    )
                else:
                    logger.warning(
                        "Unrecognized step_finish reason ignored: %s",
                        reason,
                        extra={"adapter": self.name, "raw_event": payload},
                    )
                continue

            elif event_type in ("complete", "result"):
                # Under ADR-017: in-stream completion events record state but do NOT
                # emit COMPLETE or abort stream iteration. Completion is emitted post-mortem at EOF.
                if has_stopped_step or terminal_event_emitted:
                    logger.warning(
                        "Duplicate terminal event received from OpenCode stream; ignoring",
                        extra={
                            "adapter": self.name,
                            "event_type": str(event_type),
                            "raw_payload": line,
                        },
                    )
                    continue
                has_stopped_step = True
                continue

            elif event_type in ("error", "fatal"):
                if has_stopped_step or terminal_event_emitted:
                    logger.warning(
                        "Duplicate terminal event received from OpenCode stream; ignoring",
                        extra={
                            "adapter": self.name,
                            "event_type": str(event_type),
                            "raw_payload": line,
                        },
                    )
                    continue
                terminal_event_emitted = True
                duration = time.time() - start_time
                stderr_out = get_stderr() if get_stderr else ""
                err_data = payload.get("error") or payload.get("message") or "OpenCode error event"
                err_msg = err_data.get("message") if isinstance(err_data, dict) else str(err_data)
                yield self._build_error_event(
                    ts=ts,
                    duration=duration,
                    error_msg=str(err_msg),
                    stdout=accumulated_stdout,
                    stderr=stderr_out,
                    raw_output=accumulated_raw,
                    token_usage=token_usage,
                    metadata={"error": str(err_msg)},
                )
                continue

            elif event_type == "heartbeat":
                yield AgentEvent(
                    event_type=AgentEventType.HEARTBEAT,
                    timestamp=ts,
                    data=payload,
                )
                continue

            elif event_type in ("step_start", "init"):
                logger.debug("OpenCode lifecycle event received: %s", event_type)
                continue

            else:
                logger.warning(
                    "Unrecognized provider event ignored: %s",
                    event_type,
                    extra={"adapter": self.name, "raw_event": payload},
                )
                continue

        # Post-mortem completion evaluation (ADR-017)
        if not terminal_event_emitted:
            duration = time.time() - start_time
            proc_code = get_returncode() if get_returncode else None
            stderr_out = get_stderr() if get_stderr else ""

            if proc_code is not None and proc_code != 0:
                terminal_event_emitted = True
                err_msg = stderr_out or f"OpenCode process exited with non-zero exit code: {proc_code}"
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
            elif has_stopped_step and (proc_code is None or proc_code == 0):
                terminal_event_emitted = True
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
                terminal_event_emitted = True
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
        """Progressive event generator consuming OpenCode's native JSON event stream.

        Spawns opencode with `--format json` and translates NDJSON stream events into
        canonical Forge AgentEvents (CHUNK, TOOL_START, TOOL_FINISH, COMPLETE, ERROR).
        """
        work_dir = cwd or Path.cwd()
        bin_path, err = self._resolve_binary()
        if err:
            logger.error("OpenCode WSL compatibility error: %s", err)
            yield self._build_error_event(
                ts=time.time(),
                duration=0.0,
                error_msg=err,
                stdout="",
                stderr=err,
                raw_output=err,
                token_usage={},
                metadata={"error": "WSLWindowsBinaryCompatibilityError"},
                exit_code=1,
            )
            return
        bin_path = bin_path or "opencode"
        cmd: List[str] = [bin_path, "run"]

        if self.model:
            cmd.extend(["-m", self.model])
        if self.auto_approve:
            cmd.append("--auto")
        if self.effort:
            cmd.extend(["--variant", self.effort])
        if self.extra_flags:
            cmd.extend(self._render_extra_flags())
        if "--format" not in cmd:
            cmd.extend(["--format", "json"])

        start_time = time.time()

        if logger.isEnabledFor(logging.DEBUG):
            try:
                debug_dir = work_dir / ".forge" / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / "opencode_command.txt").write_text(
                    " ".join(cmd),
                    encoding="utf-8",
                )
            except Exception:
                pass

        popen_kwargs: Dict[str, Any] = {
            "cwd": work_dir,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True

        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
            if proc.stdin:
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

            def _get_returncode() -> Optional[int]:
                if proc is not None:
                    try:
                        return proc.wait(timeout=1.0)
                    except Exception:
                        return proc.poll()
                return None

            _captured_stderr: Optional[str] = None

            def _get_stderr() -> str:
                nonlocal _captured_stderr
                if _captured_stderr is not None:
                    return _captured_stderr
                if proc is not None and proc.stderr:
                    try:
                        # Only read if the child process has terminated to avoid blocking on an open pipe
                        if proc.poll() is None:
                            try:
                                proc.wait(timeout=0.5)
                            except Exception:
                                pass
                        if proc.poll() is not None:
                            _captured_stderr = proc.stderr.read() or ""
                            return _captured_stderr
                    except Exception:
                        return ""
                return ""

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
            logger.error("OS/Encoding error executing opencode: %s", e)
            yield AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text=str(e),
                result=ExecutionResult(
                    exit_code=1,
                    duration_seconds=duration,
                    stderr=str(e),
                    raw_output=f"OS/Encoding error executing opencode: {e}",
                ),
            )
        except Exception as e:
            duration = time.time() - start_time
            logger.error("Unexpected error executing opencode: %s", e)
            yield AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text=str(e),
                result=ExecutionResult(
                    exit_code=1,
                    duration_seconds=duration,
                    stderr=str(e),
                    raw_output=f"Error executing opencode: {e}",
                ),
            )
        finally:
            if proc is not None:
                self._safe_cleanup_subprocess(proc)
                self._kill_process_group(proc)
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass

    execute_events = iter_events
