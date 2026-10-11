"""Cross-platform system abstractions for Forge.

Provides platform-neutral interfaces for:
- Process management and process-tree termination
- Subprocess creation flags (process groups / sessions)
- Executable discovery and Windows .cmd / .bat command preparation
- Terminal / console raw cbreak sessions across POSIX and Windows
- Process liveness verification
- Platform paths and environment variable utilities
"""

import collections
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# Platform constants
IS_WINDOWS: bool = (sys.platform == "win32" or os.name == "nt")
IS_POSIX: bool = (os.name == "posix")
IS_DARWIN: bool = (sys.platform == "darwin")
IS_LINUX: bool = (sys.platform == "linux")

# Byte offset used for Windows mandatory file locking (1 GiB).
# Offsetting locks avoids blocking diagnostic reads of metadata at offset 0.
WINDOWS_LOCK_OFFSET: int = 1073741824


# ============================================================================
# 1. PROCESS SPAWNING & LIFECYCLE
# ============================================================================

def get_process_group_flags() -> Dict[str, Any]:
    """Return platform-appropriate subprocess flags to create an isolated process group.

    On POSIX: {'start_new_session': True} (creates a new session via setsid).
    On Windows: {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} (creates a new console process group).
    """
    if IS_POSIX:
        return {"start_new_session": True}
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {}


def prepare_command(cmd: Union[List[str], str]) -> Union[List[str], str]:
    """Prepare a command for cross-platform execution via subprocess.

    On Windows:
    Direct execution of .cmd or .bat files via CreateProcess (without shell=True)
    can fail with WinError 193 (%1 is not a valid Win32 application).
    When cmd is a list whose first element resolves to a .cmd or .bat script,
    wrap it with cmd.exe /c so Windows executes it cleanly.
    """
    if not IS_WINDOWS:
        return cmd

    if isinstance(cmd, list) and cmd:
        executable = cmd[0]
        # Check if already wrapped with cmd.exe / comspec
        base_name = Path(executable).name.lower()
        if base_name in ("cmd.exe", "cmd"):
            return cmd

        # Check if target is a batch/cmd file or resolves to one
        target_path: Optional[str] = executable
        if not (executable.lower().endswith(".cmd") or executable.lower().endswith(".bat")):
            resolved = shutil.which(executable)
            if resolved and (resolved.lower().endswith(".cmd") or resolved.lower().endswith(".bat")):
                target_path = resolved
            else:
                target_path = None

        if target_path and (target_path.lower().endswith(".cmd") or target_path.lower().endswith(".bat")):
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            # Preserve original args, updating the script path
            new_cmd = [comspec, "/c", target_path] + cmd[1:]
            return new_cmd

    return cmd


def safe_kill(proc: subprocess.Popen) -> None:
    """Best-effort process termination ignoring missing or already-exited processes."""
    try:
        proc.kill()
    except Exception:
        pass


def terminate_process_tree(
    proc: subprocess.Popen,
    timeout: float = 1.0,
    force: bool = True,
    safe_kill_fn: Optional[Any] = None,
) -> None:
    """Terminate or kill a process and all its child/descendant processes cleanly.

    CRITICAL REGRESSION SAFETY REQUIREMENT:
    It must be structurally impossible for Forge to terminate itself or caller/system
    process groups on either platform.

    On POSIX:
    - Verifies child PID is a valid integer > 1.
    - Guards against caller's process group (os.getpgrp()), caller PID, and PGID <= 1.
    - Sends SIGTERM to the process group, waits 0.1s, then escalates to SIGKILL.
    - Falls back to safe_kill(proc) if process group signaling is disallowed.

    On Windows:
    - Verifies PID is a valid integer > 4 (protecting System/Idle processes) and != os.getpid().
    - Uses 'taskkill /F /T /PID <pid>' to terminate the process and all spawned child processes.
    - Falls back to safe_kill(proc).
    """
    _kill = safe_kill_fn or safe_kill
    try:
        pid = getattr(proc, "pid", None)
        if type(pid) is not int or pid <= 1:
            if type(pid) is not int:
                _kill(proc)
            return

        current_pid = os.getpid()

        if os.name == "posix":
            try:
                pgid = os.getpgid(pid)
            except (ProcessLookupError, PermissionError, OSError):
                _kill(proc)
                return

            current_pgid = os.getpgrp() if hasattr(os, "getpgrp") else None
            if (
                type(pgid) is not int
                or pgid <= 1
                or (current_pgid is not None and pgid == current_pgid)
                or pgid == current_pid
                or pid == current_pid
            ):
                _kill(proc)
                return

            try:
                os.killpg(pgid, signal.SIGTERM)
                time.sleep(0.1)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                _kill(proc)

        elif os.name == "nt" or sys.platform == "win32":
            # Guard against system processes (PID <= 4 on Windows) or caller PID
            if pid <= 4 or pid == current_pid:
                _kill(proc)
                return

            if proc.poll() is None:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                    )
                except Exception:
                    pass
            _kill(proc)
        else:
            _kill(proc)
    except Exception:
        _kill(proc)


def is_pid_alive(pid: int) -> bool:
    """Check whether a given PID is currently active and running."""
    if not isinstance(pid, int) or pid <= 0:
        return False

    if IS_POSIX:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h_proc:
                ERROR_ACCESS_DENIED = 5
                err = kernel32.GetLastError()
                if err == ERROR_ACCESS_DENIED:
                    return True
                return False
            try:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
                    STILL_ACTIVE = 259
                    return exit_code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(h_proc)
        except Exception:
            return False

    return False


# ============================================================================
# 2. PATHS & ENVIRONMENT UTILITIES
# ============================================================================

def get_home_dir() -> Path:
    """Return the user home directory, respecting USERPROFILE on Windows and HOME on POSIX."""
    if IS_WINDOWS:
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            return Path(userprofile)
    return Path.home()


def get_null_device() -> str:
    """Return platform-appropriate null device ('nul' on Windows, '/dev/null' on POSIX)."""
    return "nul" if IS_WINDOWS else "/dev/null"


def join_path_env(paths: List[str]) -> str:
    """Join path strings using platform-appropriate path separator (';' on Windows, ':' on POSIX)."""
    return os.pathsep.join(paths)


def split_path_env(path_str: str) -> List[str]:
    """Split path string using platform-appropriate path separator."""
    if not path_str:
        return []
    return [p.strip() for p in path_str.split(os.pathsep) if p.strip()]


def find_executable(name: str, path: Optional[str] = None) -> Optional[str]:
    """Find an executable in PATH, accounting for Windows PATHEXT (.exe, .cmd, .bat)."""
    return shutil.which(name, path=path)


# ============================================================================
# 3. TERMINAL / CONSOLE SESSIONS
# ============================================================================

def is_tty(stream: Any = sys.stdin) -> bool:
    """Return True if stream is an interactive terminal/TTY."""
    return hasattr(stream, "isatty") and stream.isatty()


def parse_key_buffer(buf: str) -> List[str]:
    """Parse a string of raw keystrokes and ANSI escape sequences into token names."""
    tokens: List[str] = []
    i = 0
    n = len(buf)
    while i < n:
        ch = buf[i]
        if ch == "\x1b":
            # Check for escape sequence
            if i + 1 >= n:
                tokens.append("esc")
                i += 1
                continue

            next_ch = buf[i + 1]
            if next_ch == "[":
                # Check for SGR mouse: \x1b[<(\d+);(\d+);(\d+)([Mm])
                if i + 2 < n and buf[i + 2] == "<":
                    match = re.match(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])", buf[i:])
                    if match:
                        btn = int(match.group(1))
                        x = int(match.group(2))
                        y = int(match.group(3))
                        action = match.group(4)
                        if btn == 64:
                            tokens.append(f"wheel_up:{x}:{y}")
                        elif btn == 65:
                            tokens.append(f"wheel_down:{x}:{y}")
                        elif btn == 0 and action == "M":
                            tokens.append(f"click:{x}:{y}")
                        elif btn == 2 and action == "M":
                            tokens.append(f"right_click:{x}:{y}")
                        i += match.end()
                        continue

                # Check for legacy X10/Normal mouse: \x1b[M followed by 3 bytes
                if i + 2 < n and buf[i + 2] == "M":
                    if i + 5 <= n:
                        b = ord(buf[i + 3]) - 32
                        x = ord(buf[i + 4]) - 32
                        y = ord(buf[i + 5]) - 32
                        if b in (64, 96):
                            tokens.append(f"wheel_up:{x}:{y}")
                        elif b in (65, 97):
                            tokens.append(f"wheel_down:{x}:{y}")
                        elif b == 0:
                            tokens.append(f"click:{x}:{y}")
                        i += 6
                        continue

                # Standard CSI sequences: \x1b[ [0-9;]* [A-Za-z~]
                match = re.match(r"\x1b\[[0-9;]*[A-Za-z~]", buf[i:])
                if match:
                    seq = match.group(0)
                    if seq == "\x1b[A":
                        tokens.append("up")
                    elif seq == "\x1b[B":
                        tokens.append("down")
                    elif seq == "\x1b[C":
                        tokens.append("right")
                    elif seq == "\x1b[D":
                        tokens.append("left")
                    elif seq in ("\x1b[H", "\x1b[1~", "\x1b[7~"):
                        tokens.append("home")
                    elif seq in ("\x1b[F", "\x1b[4~", "\x1b[8~"):
                        tokens.append("end")
                    elif seq in ("\x1b[5~", "\x1b[V"):
                        tokens.append("page_up")
                    elif seq in ("\x1b[6~", "\x1b[U"):
                        tokens.append("page_down")
                    elif seq == "\x1b[Z":
                        tokens.append("backtab")
                    else:
                        tokens.append(seq)
                    i += match.end()
                    continue

                tokens.append("esc")
                i += 1
                continue

            elif next_ch == "O":
                # SS3 sequences: \x1bOA, \x1bOB, \x1bOC, \x1bOD, \x1bOH, \x1bOF
                if i + 2 < n:
                    third = buf[i + 2]
                    if third == "A":
                        tokens.append("up")
                    elif third == "B":
                        tokens.append("down")
                    elif third == "C":
                        tokens.append("right")
                    elif third == "D":
                        tokens.append("left")
                    elif third == "H":
                        tokens.append("home")
                    elif third == "F":
                        tokens.append("end")
                    else:
                        tokens.append(f"\x1bO{third}")
                    i += 3
                    continue
                else:
                    tokens.append("esc")
                    i += 1
                    continue
            else:
                tokens.append("esc")
                i += 1
                continue
        elif ch == "\t":
            tokens.append("tab")
            i += 1
        elif ch in ("\r", "\n"):
            tokens.append("enter")
            i += 1
        elif ch in ("\x7f", "\x08"):
            tokens.append("backspace")
            i += 1
        else:
            tokens.append(ch)
            i += 1

    return tokens


class PosixTerminalSession:
    """Terminal session manager for POSIX terminals using termios and tty."""

    def __init__(self, stream: Any = sys.stdin, read_key_fn: Optional[Any] = None):
        self.stream = stream
        try:
            self.fd = stream.fileno() if hasattr(stream, "fileno") else 0
        except Exception:
            self.fd = 0
        self.old_settings = None
        self._custom_read_key = read_key_fn
        self._key_queue: collections.deque[str] = collections.deque()

    def has_queued_keys(self) -> bool:
        return len(self._key_queue) > 0

    def __enter__(self) -> "PosixTerminalSession":
        import termios
        import tty
        try:
            self.old_settings = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        except Exception:
            self.old_settings = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.old_settings is not None:
            import termios
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass

    def poll_key(self, timeout: float = 0.05) -> Optional[str]:
        if self._key_queue:
            return self._key_queue.popleft()
        import select
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
            if r:
                if self._custom_read_key:
                    return self._custom_read_key(self.fd)
                raw = os.read(self.fd, 1024).decode("utf-8", errors="ignore")
                if raw.endswith(("\x1b", "\x1b[", "\x1bO", "\x1b[<")):
                    r2, _, _ = select.select([self.fd], [], [], 0.05)
                    if r2:
                        raw += os.read(self.fd, 1024).decode("utf-8", errors="ignore")
                tokens = parse_key_buffer(raw)
                if tokens:
                    self._key_queue.extend(tokens)
                    return self._key_queue.popleft()
        except Exception:
            pass
        return None

    @staticmethod
    def read_key(fd: int) -> str:
        import select
        try:
            raw = os.read(fd, 1024).decode("utf-8", errors="ignore")
            if raw.endswith(("\x1b", "\x1b[", "\x1bO", "\x1b[<")):
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    raw += os.read(fd, 1024).decode("utf-8", errors="ignore")
            tokens = parse_key_buffer(raw)
            return tokens[0] if tokens else ""
        except Exception:
            return ""


class WindowsTerminalSession:
    """Terminal session manager for native Windows console using msvcrt."""

    def __init__(self, stream: Any = sys.stdin, read_key_fn: Optional[Any] = None):
        self.stream = stream
        self._custom_read_key = read_key_fn

    def __enter__(self) -> "WindowsTerminalSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    def poll_key(self, timeout: float = 0.05) -> Optional[str]:
        try:
            import msvcrt
        except ImportError:
            time.sleep(timeout)
            return None

        deadline = time.time() + timeout
        while True:
            try:
                if msvcrt.kbhit():
                    return self.read_key()
            except Exception:
                return None

            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            time.sleep(min(0.01, remaining))

    @staticmethod
    def read_key(fd: int = 0) -> str:
        try:
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch2 = msvcrt.getwch()
                special_map = {
                    "H": "up",
                    "P": "down",
                    "K": "left",
                    "M": "right",
                    "I": "page_up",
                    "Q": "page_down",
                }
                return special_map.get(ch2, ch2)
            elif ch == "\x1b":
                return "esc"
            elif ch == "\t":
                return "tab"
            elif ch in ("\r", "\n"):
                return "enter"
            return ch
        except Exception:
            return ""


def get_terminal_session(stream: Any = sys.stdin, read_key_fn: Optional[Any] = None) -> Any:
    """Return platform-appropriate terminal session manager."""
    if IS_WINDOWS:
        return WindowsTerminalSession(stream=stream, read_key_fn=read_key_fn)
    return PosixTerminalSession(stream=stream, read_key_fn=read_key_fn)
