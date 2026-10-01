"""Runtime Supervisor for Tester v2.

Responsible for:
- Detecting application start commands and ports
- Launching the application in an isolated process group
- Polling for HTTP / process readiness with exponential backoff
- Collecting stdout/stderr streams to disk
- Monitoring health during test execution
- Cleanly terminating the entire process tree on exit (SIGTERM -> SIGKILL)
"""

import logging
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a TCP port is actively listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def detect_runtime_target(project_root: Path) -> Tuple[Optional[str], Optional[int], str]:
    """Inspect project files to detect start command, port, and archetype.
    
    Returns:
        (start_command, default_port, detected_archetype)
    """
    pkg_json = project_root / "package.json"
    if pkg_json.exists():
        try:
            import json
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            dependencies = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            
            # Check for standard dev commands
            if "dev" in scripts:
                port = 3000
                if "vite" in dependencies:
                    port = 5173
                return "npm run dev", port, "WEB_SPA"
            elif "start" in scripts:
                return "npm start", 3000, "WEB_SPA"
        except Exception as e:
            logger.debug("Failed parsing package.json: %s", e)

    # Python projects
    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8")
        if "fastapi" in content or "uvicorn" in content:
            return "uvicorn main:app --port 8000", 8000, "API"
        if "streamlit" in content:
            return "streamlit run app.py --server.port 8501", 8501, "WEB_SPA"
        if "flask" in content:
            return "flask run --port 5000", 5000, "API"
        if "click" in content or "typer" in content:
            return None, None, "CLI"

    # Requirements.txt fallback
    req_file = project_root / "requirements.txt"
    if req_file.exists():
        content = req_file.read_text(encoding="utf-8")
        if "fastapi" in content or "uvicorn" in content:
            return "uvicorn main:app --port 8000", 8000, "API"
        if "flask" in content:
            return "flask run --port 5000", 5000, "API"

    return None, None, "UNKNOWN"


class RuntimeSupervisor:
    """Supervises an application runtime process tree."""

    def __init__(
        self,
        project_root: Path,
        log_dir: Optional[Path] = None,
        grace_period_seconds: float = 3.0,
    ):
        self.project_root = project_root
        self.log_dir = log_dir or (project_root / ".forge" / "runtime_logs")
        self.grace_period_seconds = grace_period_seconds
        self.process: Optional[subprocess.Popen] = None
        self.stdout_file: Optional[Path] = None
        self.stderr_file: Optional[Path] = None
        self._stdout_handle = None
        self._stderr_handle = None
        self.attached_to_existing: bool = False
        self.port: Optional[int] = None
        self.base_url: Optional[str] = None

    def start(
        self,
        command: Optional[Union[str, List[str]]] = None,
        port: Optional[int] = None,
        readiness_url: Optional[str] = None,
        timeout: float = 30.0,
        env: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Launch the application or attach to an existing listening instance."""
        # 1. Resolve command and port if not specified
        detected_cmd, detected_port, _ = detect_runtime_target(self.project_root)
        final_cmd = command or detected_cmd
        final_port = port or detected_port

        self.port = final_port
        if final_port:
            self.base_url = f"http://127.0.0.1:{final_port}"

        # 2. Check if port is already listening (e.g. developer started it or prior run)
        if final_port and is_port_in_use(final_port):
            logger.info("Target port %d is already in use; attaching to existing instance.", final_port)
            self.attached_to_existing = True
            return True

        if not final_cmd:
            logger.info("No start command specified or detected for runtime supervisor.")
            return False

        # 3. Setup log redirection
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.stdout_file = self.log_dir / "process_stdout.log"
        self.stderr_file = self.log_dir / "process_stderr.log"
        self._stdout_handle = open(self.stdout_file, "w", encoding="utf-8")
        self._stderr_handle = open(self.stderr_file, "w", encoding="utf-8")

        # 4. Prepare environment
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        # Avoid buffering in child processes
        process_env["PYTHONUNBUFFERED"] = "1"
        process_env["NODE_ENV"] = "development"

        # 5. Spawn child process in a new session group (os.setsid)
        cmd_args = final_cmd if isinstance(final_cmd, str) else " ".join(final_cmd)
        logger.info("Starting runtime command: %s (cwd=%s)", cmd_args, self.project_root)

        self.process = subprocess.Popen(
            cmd_args,
            shell=True,
            cwd=str(self.project_root),
            stdout=self._stdout_handle,
            stderr=self._stderr_handle,
            env=process_env,
            preexec_fn=os.setsid,  # Create new process group for clean tree kill
        )

        # 6. Wait for readiness
        target_url = readiness_url or (f"http://127.0.0.1:{final_port}" if final_port else None)
        if target_url:
            ready = self._wait_for_url_readiness(target_url, timeout=timeout)
            if not ready:
                logger.error("Application failed to become ready at %s within %ss", target_url, timeout)
                self.stop()
                return False
        else:
            # If no URL, verify process did not exit immediately
            time.sleep(1.0)
            if self.process.poll() is not None:
                logger.error("Process exited immediately with code %d", self.process.poll())
                self.stop()
                return False

        return True

    def _wait_for_url_readiness(self, url: str, timeout: float = 30.0) -> bool:
        """Poll URL with exponential backoff until it responds with non-fatal HTTP status."""
        deadline = time.time() + timeout
        delay = 0.2
        while time.time() < deadline:
            # Check if process died
            if self.process and self.process.poll() is not None:
                logger.error("Process died during readiness wait (exit=%d)", self.process.poll())
                return False

            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status < 500:
                        logger.info("Application ready at %s (HTTP %d)", url, resp.status)
                        return True
            except urllib.error.HTTPError as e:
                # 401, 403, 404 still mean the server is online and responding!
                if e.code < 500:
                    logger.info("Application ready at %s (HTTP %d)", url, e.code)
                    return True
            except (urllib.error.URLError, ConnectionRefusedError, socket.timeout):
                pass
            except Exception as e:
                logger.debug("Readiness probe exception: %s", e)

            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)

        return False

    def is_running(self) -> bool:
        """Check if supervised process is currently running."""
        if self.attached_to_existing:
            return bool(self.port and is_port_in_use(self.port))
        if not self.process:
            return False
        return self.process.poll() is None

    def get_logs(self) -> Tuple[str, str]:
        """Retrieve collected stdout and stderr logs."""
        stdout_txt = ""
        stderr_txt = ""
        if self.stdout_file and self.stdout_file.exists():
            try:
                stdout_txt = self.stdout_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        if self.stderr_file and self.stderr_file.exists():
            try:
                stderr_txt = self.stderr_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        return stdout_txt, stderr_txt

    def stop(self) -> None:
        """Cleanly terminate the entire process group (SIGTERM -> wait -> SIGKILL)."""
        if self.attached_to_existing:
            logger.info("Supervisor attached to existing instance; leaving process running.")
            return

        if not self.process:
            return

        pid = self.process.pid
        if self.process.poll() is None:
            try:
                pgid = os.getpgid(pid)
                logger.info("Sending SIGTERM to process group %d (pid %d)", pgid, pid)
                os.killpg(pgid, signal.SIGTERM)
                
                # Wait up to grace period
                deadline = time.time() + self.grace_period_seconds
                while time.time() < deadline:
                    if self.process.poll() is not None:
                        break
                    time.sleep(0.1)

                # Escalate to SIGKILL if still alive
                if self.process.poll() is None:
                    logger.warning("Process group %d did not terminate; escalating to SIGKILL", pgid)
                    os.killpg(pgid, signal.SIGKILL)
                    self.process.wait(timeout=2.0)
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.error("Error shutting down process tree for pid %d: %s", pid, e)

        # Close log handles
        if self._stdout_handle and not self._stdout_handle.closed:
            self._stdout_handle.close()
        if self._stderr_handle and not self._stderr_handle.closed:
            self._stderr_handle.close()

    def __enter__(self) -> "RuntimeSupervisor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
