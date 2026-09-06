"""OpenCode CLI adapter."""

import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
from forge.adapters.base import BaseAdapter, AdapterResponse


class OpenCodeAdapter(BaseAdapter):
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

    def is_available(self) -> bool:
        return shutil.which("opencode") is not None

    def execute(self, prompt: str, cwd: Optional[Path] = None) -> AdapterResponse:
        work_dir = cwd or Path.cwd()
        cmd: List[str] = ["opencode", "run"]

        if self.model:
            cmd.extend(["-m", self.model])
        if self.auto_approve:
            cmd.append("--auto")
        if self.effort:
            cmd.extend(["--variant", self.effort])

        # Add prompt as argument
        cmd.append(prompt)

        start_time = time.time()
        try:
            res = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
            )
            duration = time.time() - start_time
            raw = res.stdout if res.stdout else res.stderr
            return AdapterResponse(
                stdout=res.stdout,
                stderr=res.stderr,
                exit_code=res.returncode,
                duration_seconds=duration,
                raw_output=raw,
            )
        except Exception as e:
            duration = time.time() - start_time
            return AdapterResponse(
                stdout="",
                stderr=str(e),
                exit_code=1,
                duration_seconds=duration,
                raw_output=f"Error executing opencode: {e}",
            )
