"""OpenAI Codex CLI adapter."""

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Set

from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.core.capabilities import Capability


class CodexAdapter(BaseAdapter):
    CAPABILITIES: Set[str] = {
        Capability.CODE_READ,
        Capability.CODE_EDIT,
        Capability.SHELL,
        Capability.GIT,
        Capability.STRUCTURED_OUTPUT,
        Capability.TOOL_CALLING,
        Capability.LONG_RUNNING,
        Capability.CUSTOM_FLAGS,
    }
    DEFAULT_MODEL = "gpt-5.6-terra"
    DEFAULT_EFFORT = "medium"

    def __init__(
        self,
        model: Optional[str] = "gpt-5.6-terra",
        effort: Optional[str] = "medium",
        auto_approve: bool = False,
        extra_flags: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name="codex",
            model=model,
            effort=effort,
            auto_approve=auto_approve,
            extra_flags=extra_flags,
        )

    def _get_binary(self) -> Optional[str]:
        return shutil.which("codex")

    def is_available(self) -> bool:
        return self._get_binary() is not None

    def build_command(
        self,
        work_dir: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> List[str]:
        """Construct the CLI argv for codex exec invocation."""
        bin_path = self._get_binary() or "codex"
        cmd: List[str] = [bin_path, "exec", "--color", "never"]

        if work_dir:
            cmd.extend(["-C", str(work_dir)])
        if self.model:
            cmd.extend(["-m", self.model])
        if self.effort:
            cmd.extend(["-c", f'model_reasoning_effort="{self.effort}"'])
        if self.auto_approve:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        if self.extra_flags:
            cmd.extend(self._render_extra_flags())

        # The prompt is fed via stdin through '-'
        cmd.append("-")
        return cmd

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
                stderr="Executable 'codex' was not found in PATH.",
                exit_code=1,
                duration_seconds=0.0,
                raw_output="Executable 'codex' was not found in PATH.",
            )

        work_dir = cwd or Path.cwd()
        timeout_val = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        cmd = self.build_command(work_dir=work_dir, timeout=timeout_val)

        start_time = time.time()
        try:
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
            logging.error("OS/Encoding error executing codex: %s", e)
            return AdapterResponse(
                stdout="",
                stderr=str(e),
                exit_code=1,
                duration_seconds=duration,
                raw_output=f"OS/Encoding error executing codex: {e}",
            )
        except Exception as e:
            duration = time.time() - start_time
            logging.error("Unexpected error executing codex: %s", e)
            return AdapterResponse(
                stdout="",
                stderr=str(e),
                exit_code=1,
                duration_seconds=duration,
                raw_output=f"Error executing codex: {e}",
            )
