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
from typing import Dict, List, Optional, Tuple, Union, Any, Set

from forge.core.platform import (
    terminate_process_tree,
    get_process_group_flags,
    is_pid_alive,
    find_descendant_pids,
    launch_contained_process,
)

logger = logging.getLogger(__name__)

# Standard environment variables permitted in clean subprocess execution
STANDARD_ENV_WHITELIST = {
    "PATH", "PYTHONPATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "HOME", "USER", "LOGNAME", "USERPROFILE", "HOMEPATH", "HOMEDRIVE",
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TERM", "SHELL", "TZ",
    "TMPDIR", "TEMP", "TMP",
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
}


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a TCP port is actively listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def find_free_port(host: str = "127.0.0.1") -> int:
    """Find an available TCP port bound to 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def detect_runtime_target(project_root: Path) -> Tuple[Optional[str], Optional[int], str]:
    """Inspect project files to detect start command, port, and archetype.
    
    Returns:
        (start_command, default_port, detected_archetype)
    """
    # 1. Check explicit configuration in forge.yaml (authoritative override)
    forge_yaml = project_root / "forge.yaml"
    if forge_yaml.exists():
        try:
            import yaml
            cfg_data = yaml.safe_load(forge_yaml.read_text(encoding="utf-8"))
            if isinstance(cfg_data, dict):
                stages = cfg_data.get("stages", {})
                tester_cfg = stages.get("tester", {}) if isinstance(stages, dict) else {}
                extra = tester_cfg.get("extra_flags", {}) if isinstance(tester_cfg, dict) else {}
                cmd = extra.get("start_command") or extra.get("command")
                port = extra.get("port")
                arch = extra.get("archetype")
                if cmd:
                    return cmd, int(port) if port else None, str(arch).upper() if arch else "WEB_SPA"
        except Exception as e:
            logger.debug("Failed reading forge.yaml: %s", e)

    # 2. Check Python project files (pyproject.toml, requirements.txt)
    pyproject = project_root / "pyproject.toml"
    req_file = project_root / "requirements.txt"
    py_content = ""
    if pyproject.exists():
        try:
            py_content += " " + pyproject.read_text(encoding="utf-8").lower()
        except Exception:
            pass
    if req_file.exists():
        try:
            py_content += " " + req_file.read_text(encoding="utf-8").lower()
        except Exception:
            pass

    if py_content:
        has_streamlit = "streamlit" in py_content
        has_fastapi = "fastapi" in py_content or "uvicorn" in py_content
        has_flask = "flask" in py_content
        has_api = has_fastapi or has_flask
        has_cli = "project.scripts" in py_content or "console_scripts" in py_content or (project_root / "cli.py").exists()

        # Ambiguity check: conflicting web/api backend frameworks without entrypoint disambiguation
        if has_streamlit and has_api:
            has_streamlit_file = (project_root / "streamlit_app.py").exists()
            has_api_file = (project_root / "api.py").exists()
            if has_streamlit_file and not has_api_file:
                entry = "streamlit_app.py"
                return f"streamlit run {entry} --server.port 8501 --server.headless true --browser.gatherUsageStats false", 8501, "WEB_SPA"
            elif has_api_file and not has_streamlit_file:
                return "uvicorn api:app --port 8000", 8000, "API"
            else:
                logger.warning("Ambiguous runtime target: conflicting backend frameworks detected in %s", project_root)
                return None, None, "UNKNOWN"

        if has_streamlit:
            entry = "app.py" if (project_root / "app.py").exists() else ("streamlit_app.py" if (project_root / "streamlit_app.py").exists() else "main.py")
            return f"streamlit run {entry} --server.port 8501 --server.headless true --browser.gatherUsageStats false", 8501, "WEB_SPA"
        if has_fastapi:
            entry = "app" if (project_root / "app.py").exists() else ("api" if (project_root / "api.py").exists() else "main")
            return f"uvicorn {entry}:app --port 8000", 8000, "API"
        if has_flask:
            entry = "app.py" if (project_root / "app.py").exists() else ("main.py" if (project_root / "main.py").exists() else "")
            cmd = f"flask --app {entry} run --port 5000" if entry else "flask run --port 5000"
            return cmd, 5000, "API"
        if has_cli:
            return None, None, "CLI"

    # 3. Check Node.js project (package.json)
    pkg_json = project_root / "package.json"
    if pkg_json.exists():
        try:
            import json
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            dependencies = {**data.get("dependencies", {}), **data.get("devDependencies", {})}

            web_markers = [
                "react", "next", "vue", "nuxt", "svelte", "sveltekit",
                "vite", "@angular/core", "astro", "solid-js", "remix",
                "gatsby", "tailwindcss",
            ]
            api_markers = ["express", "koa", "fastify", "nest", "@nestjs/core", "hapi"]
            is_api = any(m in dependencies for m in api_markers) and not any(m in dependencies for m in web_markers)
            archetype = "API" if is_api else "WEB_SPA"

            if "dev" in scripts:
                port = 3000
                if "vite" in dependencies:
                    port = 5173
                return "npm run dev", port, archetype
            elif "start" in scripts:
                return "npm start", 3000, archetype
        except Exception as e:
            logger.debug("Failed parsing package.json: %s", e)

    # 4. Check candidate entry files
    if (project_root / "cli.py").exists():
        return None, None, "CLI"

    for candidate in ("app.py", "streamlit_app.py"):
        fpath = project_root / candidate
        if fpath.exists():
            try:
                head = fpath.read_text(encoding="utf-8", errors="ignore")[:2000]
                if "import streamlit" in head or "from streamlit" in head:
                    return f"streamlit run {candidate} --server.port 8501 --server.headless true --browser.gatherUsageStats false", 8501, "WEB_SPA"
            except Exception:
                pass

    # 5. Check Conventional Static HTML only if no backend framework was found
    if (project_root / "index.html").exists() or (project_root / "public" / "index.html").exists():
        return "python3 -m http.server 8080", 8080, "WEB_SPA"

    return None, None, "UNKNOWN"


class RuntimeSupervisor:
    """Supervises an application runtime process tree."""

    def __init__(
        self,
        project_root: Path,
        log_dir: Optional[Path] = None,
        grace_period_seconds: float = 3.0,
        venv_path: Optional[Path] = None,
        allow_user_site: Optional[bool] = None,
        attach_if_running: bool = False,
        sanitize_env: bool = True,
    ):
        self.project_root = project_root
        self.log_dir = log_dir or (project_root / ".forge" / "runtime_logs")
        self.grace_period_seconds = grace_period_seconds
        self.venv_path = venv_path
        self.allow_user_site = allow_user_site
        self.attach_if_running = attach_if_running
        self.sanitize_env = sanitize_env
        self.process: Optional[subprocess.Popen] = None
        self.stdout_file: Optional[Path] = None
        self.stderr_file: Optional[Path] = None
        self._stdout_handle = None
        self._stderr_handle = None
        self.attached_to_existing: bool = False
        self.port_conflict: bool = False
        self.bind_conflict: bool = False
        self.missing_dependency: bool = False
        self.startup_timed_out: bool = False
        self.exit_code: Optional[int] = None
        self.failure_reason: Optional[str] = None
        self.cleanup_failed: bool = False
        self.uncleaned_pids: List[int] = []
        self.port_still_occupied: bool = False
        self.tracked_pids: Set[int] = set()
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
        final_port = port if port is not None else detected_port

        # Dynamic port support: port 0 allocates an available port
        if final_port == 0:
            final_port = find_free_port()
            if final_cmd and isinstance(final_cmd, str):
                for def_p in ("8000", "8080", "8501", "5000", "3000"):
                    if def_p in final_cmd:
                        final_cmd = final_cmd.replace(def_p, str(final_port))
                        break

        self.port = final_port
        if final_port:
            self.base_url = f"http://127.0.0.1:{final_port}"

        # 2. Port conflict safety: check if port is already listening
        if final_port and is_port_in_use(final_port):
            if self.attach_if_running:
                logger.info("Target port %d is already in use; attaching to existing instance.", final_port)
                self.attached_to_existing = True
                return True
            else:
                logger.error("Port conflict: port %d is already in use by an existing process.", final_port)
                self.port_conflict = True
                self.failure_reason = f"Port conflict: port {final_port} is already in use by an existing process."
                return False

        if not final_cmd:
            logger.info("No start command specified or detected for runtime supervisor.")
            self.failure_reason = "No start command specified or detected."
            return False

        # 3. Setup log redirection
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.stdout_file = self.log_dir / "process_stdout.log"
        self.stderr_file = self.log_dir / "process_stderr.log"
        self._stdout_handle = open(self.stdout_file, "w", encoding="utf-8")
        self._stderr_handle = open(self.stderr_file, "w", encoding="utf-8")

        # 4. Prepare environment with clean isolation and sanitization
        if self.sanitize_env:
            process_env = {k: v for k, v in os.environ.items() if k in STANDARD_ENV_WHITELIST}
        else:
            process_env = os.environ.copy()

        if env:
            process_env.update(env)
        process_env["PYTHONUNBUFFERED"] = "1"
        process_env["NODE_ENV"] = "development"

        # Resolve target virtual environment if present
        active_venv = self.venv_path
        if not active_venv:
            for venv_name in (".venv", "venv", "env"):
                cand = self.project_root / venv_name
                if cand.is_dir() and (cand / ("Scripts" if os.name == "nt" else "bin")).exists():
                    active_venv = cand
                    break

        if active_venv:
            venv_bin = active_venv / ("Scripts" if os.name == "nt" else "bin")
            process_env["PATH"] = f"{venv_bin}{os.pathsep}{process_env.get('PATH', '')}"
            process_env["VIRTUAL_ENV"] = str(active_venv.resolve())
            process_env.pop("PYTHONHOME", None)
            # In an active virtual environment, do NOT inject host user-site
        else:
            # No virtual environment: check if user-site is explicitly permitted
            should_allow_user_site = self.allow_user_site
            if should_allow_user_site is None:
                should_allow_user_site = os.environ.get("FORGE_ALLOW_USER_SITE", "0") == "1"

            if should_allow_user_site:
                import site
                user_site = site.getusersitepackages()
                if user_site and Path(user_site).exists():
                    curr_pypath = process_env.get("PYTHONPATH", "")
                    if user_site not in curr_pypath:
                        process_env["PYTHONPATH"] = f"{user_site}{os.pathsep}{curr_pypath}" if curr_pypath else user_site
                user_bin = str(Path.home() / ".local" / "bin")
                if Path(user_bin).exists():
                    curr_path = process_env.get("PATH", "")
                    if user_bin not in curr_path:
                        process_env["PATH"] = f"{user_bin}{os.pathsep}{curr_path}"

        # 5. Spawn child process in a new session / process group
        cmd_args = final_cmd if isinstance(final_cmd, str) else " ".join(final_cmd)
        logger.info("Starting runtime command: %s (cwd=%s)", cmd_args, self.project_root)

        popen_kwargs: Dict[str, Any] = {
            "shell": True,
            "cwd": str(self.project_root),
            "stdout": self._stdout_handle,
            "stderr": self._stderr_handle,
            "env": process_env,
        }
        popen_kwargs.update(get_process_group_flags())

        self.process = launch_contained_process(cmd_args, popen_kwargs=popen_kwargs, popen_fn=subprocess.Popen)
        if self.process and self.process.pid:
            self.tracked_pids.add(self.process.pid)

        # 6. Wait for readiness
        target_url = readiness_url or (f"http://127.0.0.1:{final_port}" if final_port else None)
        if target_url:
            ready = self._wait_for_url_readiness(target_url, timeout=timeout)
            if not ready:
                logger.error("Application failed to become ready at %s within %ss (%s)", target_url, timeout, self.failure_reason)
                self.stop()
                return False
        else:
            time.sleep(1.0)
            if self.process.poll() is not None:
                self.exit_code = self.process.poll()
                self._inspect_failure()
                logger.error("Process exited immediately: %s", self.failure_reason)
                self.stop()
                return False

        return True

    def _inspect_failure(self) -> None:
        """Inspect stderr and stdout to determine the exact failure cause."""
        stdout_txt, stderr_txt = self.get_logs()
        combined = f"{stderr_txt}\n{stdout_txt}".lower()

        # 1. Bind failure / address already in use (TOCTOU race detection)
        bind_markers = (
            "address already in use",
            "errno 98",
            "eaddrinuse",
            "port is already in use",
            "could not bind",
            "failed to bind",
            "binding to address",
        )
        if any(m in combined for m in bind_markers):
            self.bind_conflict = True
            self.port_conflict = True
            self.failure_reason = f"Port bind conflict on port {self.port}: Address already in use"
            return

        # 2. Missing dependency or command
        dep_markers = (
            "modulenotfounderror",
            "importerror",
            "command not found",
            "no module named",
            "is not recognized as an internal or external command",
            "cannot import name",
        )
        if any(m in combined for m in dep_markers):
            self.missing_dependency = True
            err_line = stderr_txt.strip().splitlines()[-1] if stderr_txt.strip() else "dependency missing"
            self.failure_reason = f"Missing prerequisite or dependency: {err_line[:200]}"
            return

        # 3. General exit code
        err_msg = (stderr_txt.strip() or stdout_txt.strip())
        err_line = err_msg.splitlines()[-1] if err_msg else f"Exit code {self.exit_code}"
        self.failure_reason = f"Process exited with code {self.exit_code}: {err_line[:200]}"

    def _wait_for_url_readiness(self, url: str, timeout: float = 30.0) -> bool:
        """Poll URL with exponential backoff until it responds with non-fatal HTTP status."""
        deadline = time.time() + timeout
        delay = 0.2
        while time.time() < deadline:
            # Check if process died
            if self.process and self.process.poll() is not None:
                self.exit_code = self.process.poll()
                self._inspect_failure()
                logger.error("Process died during readiness wait: %s", self.failure_reason)
                return False

            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status < 500:
                        # Re-verify process didn't die while/after connection
                        if self.process and self.process.poll() is not None:
                            self.exit_code = self.process.poll()
                            self._inspect_failure()
                            return False
                        logger.info("Application ready at %s (HTTP %d)", url, resp.status)
                        return True
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    if self.process and self.process.poll() is not None:
                        self.exit_code = self.process.poll()
                        self._inspect_failure()
                        return False
                    logger.info("Application ready at %s (HTTP %d)", url, e.code)
                    return True
            except (urllib.error.URLError, ConnectionRefusedError, socket.timeout):
                pass
            except Exception as e:
                logger.debug("Readiness probe exception: %s", e)

            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)

        # Timed out while process is still running
        if self.process and self.process.poll() is None:
            self.startup_timed_out = True
            self.failure_reason = f"Application failed to become ready at {url} within {timeout}s (timed out) while process pid={self.process.pid} was still running"
        elif self.process and self.process.poll() is not None:
            self.exit_code = self.process.poll()
            self._inspect_failure()

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

    def stop(self) -> bool:
        """Cleanly terminate the entire process tree. Returns True if all owned processes were reaped."""
        # Always check if port remains occupied
        if self.port and is_port_in_use(self.port):
            self.port_still_occupied = True
        else:
            self.port_still_occupied = False

        if self.attached_to_existing:
            logger.info("Supervisor attached to existing instance; leaving process running.")
            return True

        if not self.process:
            return True

        pid = getattr(self.process, "pid", None)
        if not pid or not isinstance(pid, int):
            return True

        # Discover all descendant PIDs before terminating
        descendants = find_descendant_pids(pid)
        all_owned_pids = {pid} | descendants | set(self.tracked_pids)

        terminate_process_tree(self.process, timeout=self.grace_period_seconds)
        try:
            self.process.wait(timeout=0.5)
        except Exception:
            try:
                self.process.poll()
            except Exception:
                pass

        # Verify whether any owned PID is still alive
        surviving = [p for p in all_owned_pids if is_pid_alive(p)]
        if surviving:
            self.cleanup_failed = True
            self.uncleaned_pids = surviving
            logger.error("Cleanup failure: owned processes failed to terminate: %s", surviving)
        else:
            self.cleanup_failed = False
            self.uncleaned_pids = []

        # Check if port remains occupied after process termination
        if self.port and is_port_in_use(self.port):
            self.port_still_occupied = True
            logger.warning("Port %d remains occupied after process termination", self.port)
        else:
            self.port_still_occupied = False

        # Close log handles
        if self._stdout_handle and not self._stdout_handle.closed:
            self._stdout_handle.close()
        if self._stderr_handle and not self._stderr_handle.closed:
            self._stderr_handle.close()
        self.process = None

        return not self.cleanup_failed

    def __enter__(self) -> "RuntimeSupervisor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
