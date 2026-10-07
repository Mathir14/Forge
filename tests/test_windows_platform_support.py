"""Dedicated test suite for Native Windows platform abstractions and compatibility.

Validates:
1. Platform detection constants and process group creation flags
2. Windows command preparation (.cmd / .bat wrapping with cmd.exe /c)
3. Windows process-tree termination (taskkill invocation, self/system PID protection)
4. Windows PID liveness checking (ctypes OpenProcess emulation)
5. Windows terminal session and virtual key mapping (msvcrt.kbhit / msvcrt.getwch)
6. Windows filesystem paths with spaces and backslashes (C:\\Users\\User\\OneDrive\\Desktop\\Mapthon)
7. OpenCode binary resolution on native Windows (acceptance of .cmd / .exe)
8. OpenCode daemon API and run invocation with Windows prepared commands
9. RunLock locking and stale recovery semantics under Windows
10. Dashboard app execution with Windows terminal session
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.adapters.opencode import OpenCodeAdapter, is_windows_executable, is_wsl
from forge.core.platform import (
    IS_WINDOWS,
    IS_POSIX,
    get_process_group_flags,
    prepare_command,
    terminate_process_tree,
    safe_kill,
    is_pid_alive,
    get_home_dir,
    get_null_device,
    join_path_env,
    split_path_env,
    WindowsTerminalSession,
    PosixTerminalSession,
    get_terminal_session,
    is_tty,
)
from forge.dashboard.app import DashboardApp
from forge.dashboard.model import RunModel
from forge.dashboard.state import DashboardState
from forge.storage.run_lock import RunLock
from forge.storage.run_manager import RunManager


# ============================================================================
# 1. PLATFORM CONSTANTS & PROCESS GROUP FLAGS
# ============================================================================

def test_process_group_flags_windows(monkeypatch):
    """Verify get_process_group_flags returns CREATE_NEW_PROCESS_GROUP on Windows."""
    monkeypatch.setattr("forge.core.platform.IS_POSIX", False)
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)
    flags = get_process_group_flags()
    assert "creationflags" in flags
    assert flags["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def test_process_group_flags_posix(monkeypatch):
    """Verify get_process_group_flags returns start_new_session on POSIX."""
    monkeypatch.setattr("forge.core.platform.IS_POSIX", True)
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", False)
    flags = get_process_group_flags()
    assert flags == {"start_new_session": True}


# ============================================================================
# 2. WINDOWS COMMAND PREPARATION (.cmd / .bat)
# ============================================================================

def test_prepare_command_posix_unchanged(monkeypatch):
    """Verify prepare_command leaves command unmodified on POSIX."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", False)
    cmd = ["opencode", "run", "--model", "gpt-4"]
    assert prepare_command(cmd) == cmd


def test_prepare_command_windows_bat_cmd(monkeypatch, tmp_path):
    """Verify prepare_command wraps .cmd and .bat files with cmd.exe /c on Windows."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    # 1. Direct .cmd extension
    cmd_cmd = [r"C:\Users\User\AppData\Roaming\npm\opencode.cmd", "run", "--auto"]
    prepared = prepare_command(cmd_cmd)
    assert prepared == [r"C:\Windows\System32\cmd.exe", "/c", r"C:\Users\User\AppData\Roaming\npm\opencode.cmd", "run", "--auto"]

    # 2. Direct .bat extension
    cmd_bat = [r"C:\Tools\agent.bat", "exec"]
    prepared_bat = prepare_command(cmd_bat)
    assert prepared_bat == [r"C:\Windows\System32\cmd.exe", "/c", r"C:\Tools\agent.bat", "exec"]

    # 3. Already wrapped with cmd.exe
    cmd_wrapped = [r"C:\Windows\System32\cmd.exe", "/c", "opencode.cmd", "run"]
    assert prepare_command(cmd_wrapped) == cmd_wrapped

    # 4. Binary that resolves to .cmd via PATH
    fake_cmd_file = tmp_path / "opencode.cmd"
    fake_cmd_file.write_text("@echo off", encoding="utf-8")
    with patch("shutil.which", return_value=str(fake_cmd_file)):
        cmd_resolved = ["opencode", "run"]
        res = prepare_command(cmd_resolved)
        assert res == [r"C:\Windows\System32\cmd.exe", "/c", str(fake_cmd_file), "run"]

    # 5. Direct .exe binary does NOT get wrapped with cmd.exe
    cmd_exe = [r"C:\Program Files\OpenCode\opencode.exe", "run"]
    with patch("shutil.which", return_value=cmd_exe[0]):
        assert prepare_command(cmd_exe) == cmd_exe


# ============================================================================
# 3. WINDOWS PROCESS-TREE TERMINATION
# ============================================================================

def test_terminate_process_tree_windows_invokes_taskkill(monkeypatch):
    """Verify terminate_process_tree runs taskkill /F /T /PID on Windows."""
    proc = MagicMock()
    proc.pid = 8888
    proc.poll.return_value = None

    with patch("os.name", "nt"), \
         patch("sys.platform", "win32"), \
         patch("subprocess.run") as mock_run, \
         patch("forge.core.platform.safe_kill") as mock_safe_kill:
        terminate_process_tree(proc, timeout=2.0)

        mock_run.assert_called_once_with(
            ["taskkill", "/F", "/T", "/PID", "8888"],
            capture_output=True,
            timeout=2.0,
            check=False,
        )
        mock_safe_kill.assert_called_once_with(proc)


def test_terminate_process_tree_windows_protects_system_and_self_pids():
    """Verify terminate_process_tree never calls taskkill on self or system PIDs (<= 4)."""
    current_pid = os.getpid()

    # 1. Forge's own PID
    proc_self = MagicMock()
    proc_self.pid = current_pid
    with patch("os.name", "nt"), \
         patch("sys.platform", "win32"), \
         patch("subprocess.run") as mock_run, \
         patch("forge.core.platform.safe_kill") as mock_safe_kill:
        terminate_process_tree(proc_self)
        mock_run.assert_not_called()
        mock_safe_kill.assert_called_once_with(proc_self)

    # 2. System Idle process (PID 0)
    proc_idle = MagicMock()
    proc_idle.pid = 0
    with patch("os.name", "nt"), \
         patch("sys.platform", "win32"), \
         patch("subprocess.run") as mock_run:
        terminate_process_tree(proc_idle)
        mock_run.assert_not_called()

    # 3. System process (PID 4 on Windows)
    proc_sys = MagicMock()
    proc_sys.pid = 4
    with patch("os.name", "nt"), \
         patch("sys.platform", "win32"), \
         patch("subprocess.run") as mock_run, \
         patch("forge.core.platform.safe_kill") as mock_safe_kill:
        terminate_process_tree(proc_sys)
        mock_run.assert_not_called()
        mock_safe_kill.assert_called_once_with(proc_sys)


def test_terminate_process_tree_windows_already_exited():
    """Verify terminate_process_tree skips taskkill if process already exited."""
    proc_dead = MagicMock()
    proc_dead.pid = 9999
    proc_dead.poll.return_value = 0  # Process already exited

    with patch("os.name", "nt"), \
         patch("sys.platform", "win32"), \
         patch("subprocess.run") as mock_run, \
         patch("forge.core.platform.safe_kill") as mock_safe_kill:
        terminate_process_tree(proc_dead)
        mock_run.assert_not_called()
        mock_safe_kill.assert_called_once_with(proc_dead)


# ============================================================================
# 4. WINDOWS PID LIVENESS CHECKING
# ============================================================================

def test_is_pid_alive_windows_emulation(monkeypatch):
    """Verify is_pid_alive uses kernel32 OpenProcess on Windows."""
    monkeypatch.setattr("forge.core.platform.IS_POSIX", False)
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_kernel32 = MagicMock()
    # Emulate active process: OpenProcess returns non-zero handle, GetExitCodeProcess returns STILL_ACTIVE (259)
    mock_kernel32.OpenProcess.return_value = 12345
    def fake_get_exit_code(handle, byref_var):
        byref_var._obj.value = 259  # STILL_ACTIVE
        return True
    mock_kernel32.GetExitCodeProcess.side_effect = fake_get_exit_code

    with patch("ctypes.windll", MagicMock(kernel32=mock_kernel32), create=True):
        assert is_pid_alive(5555) is True
        mock_kernel32.CloseHandle.assert_called_once_with(12345)


def test_is_pid_alive_windows_terminated_process(monkeypatch):
    """Verify is_pid_alive returns False when process has exited."""
    monkeypatch.setattr("forge.core.platform.IS_POSIX", False)
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_kernel32 = MagicMock()
    mock_kernel32.OpenProcess.return_value = 12345
    def fake_get_exit_code(handle, byref_var):
        byref_var._obj.value = 0  # Exited with code 0
        return True
    mock_kernel32.GetExitCodeProcess.side_effect = fake_get_exit_code

    with patch("ctypes.windll", MagicMock(kernel32=mock_kernel32), create=True):
        assert is_pid_alive(5555) is False


def test_is_pid_alive_windows_invalid_process(monkeypatch):
    """Verify is_pid_alive returns False when OpenProcess returns null."""
    monkeypatch.setattr("forge.core.platform.IS_POSIX", False)
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_kernel32 = MagicMock()
    mock_kernel32.OpenProcess.return_value = 0  # Process not found

    with patch("ctypes.windll", MagicMock(kernel32=mock_kernel32), create=True):
        assert is_pid_alive(99999) is False


# ============================================================================
# 5. WINDOWS TERMINAL SESSION & KEY MAPPING
# ============================================================================

def test_windows_terminal_session_key_mapping():
    """Verify WindowsTerminalSession.read_key correctly translates Windows virtual keys."""
    mock_msvcrt = MagicMock()
    # Test arrow keys (prefixed by \xe0)
    mock_msvcrt.getwch.side_effect = ["\xe0", "H"]  # Up
    with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        assert WindowsTerminalSession.read_key() == "up"

    mock_msvcrt.getwch.side_effect = ["\xe0", "P"]  # Down
    with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        assert WindowsTerminalSession.read_key() == "down"

    mock_msvcrt.getwch.side_effect = ["\xe0", "K"]  # Left
    with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        assert WindowsTerminalSession.read_key() == "left"

    mock_msvcrt.getwch.side_effect = ["\xe0", "M"]  # Right
    with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        assert WindowsTerminalSession.read_key() == "right"

    mock_msvcrt.getwch.side_effect = ["\xe0", "I"]  # Page Up
    with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        assert WindowsTerminalSession.read_key() == "page_up"

    mock_msvcrt.getwch.side_effect = ["\xe0", "Q"]  # Page Down
    with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        assert WindowsTerminalSession.read_key() == "page_down"

    # Test standard keys
    mock_msvcrt.getwch.side_effect = ["\x1b"]  # Escape
    with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        assert WindowsTerminalSession.read_key() == "esc"

    mock_msvcrt.getwch.side_effect = ["\r"]  # Enter
    with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        assert WindowsTerminalSession.read_key() == "enter"

    mock_msvcrt.getwch.side_effect = ["q"]  # 'q'
    with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        assert WindowsTerminalSession.read_key() == "q"


def test_windows_terminal_session_poll_key_timeout():
    """Verify WindowsTerminalSession.poll_key returns None when no key is hit."""
    mock_msvcrt = MagicMock()
    mock_msvcrt.kbhit.return_value = False
    session = WindowsTerminalSession()
    with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        start = time.time()
        key = session.poll_key(timeout=0.03)
        elapsed = time.time() - start
        assert key is None
        assert elapsed >= 0.02


def test_get_terminal_session_factory(monkeypatch):
    """Verify get_terminal_session selects WindowsTerminalSession on Windows."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)
    session = get_terminal_session()
    assert isinstance(session, WindowsTerminalSession)

    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", False)
    session_posix = get_terminal_session()
    assert isinstance(session_posix, PosixTerminalSession)


# ============================================================================
# 6. WINDOWS PATHS & ENVIRONMENT UTILITIES
# ============================================================================

def test_windows_home_dir_uses_userprofile(monkeypatch):
    """Verify get_home_dir prioritizes USERPROFILE on Windows."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)
    monkeypatch.setenv("USERPROFILE", r"C:\Users\ForgeUser")
    assert str(get_home_dir()) == r"C:\Users\ForgeUser"


def test_get_null_device_windows(monkeypatch):
    """Verify get_null_device returns 'nul' on Windows."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)
    assert get_null_device() == "nul"


def test_split_and_join_path_env(monkeypatch):
    """Verify split_path_env and join_path_env work with platform separators."""
    # Test current platform
    if os.name == "nt":
        paths = [r"C:\Windows", r"C:\Tools\Bin", r"C:\Users\User\AppData\Local\Programs"]
    else:
        paths = ["/usr/bin", "/usr/local/bin", "/home/user/bin"]
    joined = os.pathsep.join(paths)
    split = split_path_env(joined)
    assert split == paths
    assert join_path_env(paths) == joined

    # Explicitly test Windows semicolon separator
    monkeypatch.setattr(os, "pathsep", ";")
    win_paths = [r"C:\Windows", r"C:\Tools\Bin", r"C:\Users\User\AppData\Local\Programs"]
    win_joined = ";".join(win_paths)
    assert split_path_env(win_joined) == win_paths
    assert join_path_env(win_paths) == win_joined


# ============================================================================
# 7. OPENCODE ON NATIVE WINDOWS
# ============================================================================

def test_opencode_native_windows_resolution(monkeypatch):
    """Verify OpenCode resolves Windows binaries when running on native Windows."""
    adapter = OpenCodeAdapter()
    win_bin = r"C:\Users\User\AppData\Roaming\npm\opencode.cmd"

    # On native Windows: sys.platform is 'win32', is_wsl() returns False
    monkeypatch.setattr(sys, "platform", "win32")
    with patch("forge.adapters.opencode.is_wsl", return_value=False), \
         patch("shutil.which", return_value=win_bin):
        assert is_wsl() is False
        bin_path, err = adapter._resolve_binary()
        assert bin_path == win_bin
        assert err is None
        assert adapter.is_available() is True
        assert adapter.availability_error is None


def test_opencode_windows_prepared_daemon_api(monkeypatch, tmp_path):
    """Verify _query_daemon_api prepares Windows command with cmd.exe /c."""
    adapter = OpenCodeAdapter()
    win_bin = r"C:\Users\User\AppData\Roaming\npm\opencode.cmd"

    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    with patch.object(adapter, "_resolve_binary", return_value=(win_bin, None)), \
         patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"status": "ok", "interrupted": true}'
        mock_run.return_value = mock_proc

        res = adapter._query_daemon_api("/api/session/ses-123/interrupt", method="POST")
        assert res == {"status": "ok", "interrupted": True}

        # Verify command executed was wrapped with cmd.exe /c
        executed_cmd = mock_run.call_args[0][0]
        assert executed_cmd == [
            r"C:\Windows\System32\cmd.exe",
            "/c",
            win_bin,
            "api",
            "POST",
            "/api/session/ses-123/interrupt",
        ]


# ============================================================================
# 8. WINDOWS PATHS WITH SPACES AND DEEP HIERARCHIES
# ============================================================================

def test_run_manager_and_lock_with_spaces_in_path(tmp_path):
    """Verify RunManager and RunLock operate reliably in paths containing spaces."""
    spaced_project = tmp_path / "My Documents" / "OneDrive" / "Desktop" / "Mapthon Project"
    spaced_project.mkdir(parents=True, exist_ok=True)

    run_mgr = RunManager(spaced_project)
    run = run_mgr.create_run(task="Test task with spaced paths")
    assert "My Documents" in str(run.run_dir)
    assert run.run_dir.exists()

    # Verify RunLock acquires and releases in spaced path
    lock = RunLock(run.run_dir, run_id=run.run_id)
    with lock:
        assert lock.is_locked is True
        assert (run.run_dir / "run.lock").exists()

    assert lock.is_locked is False
    assert not (run.run_dir / "run.lock").exists()


# ============================================================================
# 9. DASHBOARD APP EXECUTION WITH WINDOWS TERMINAL SESSION
# ============================================================================

def test_dashboard_app_windows_session_quit_key(tmp_path, monkeypatch):
    """Verify DashboardApp runs and exits cleanly on 'q' under WindowsTerminalSession."""
    run_dir = tmp_path / ".forge" / "runs" / "run-win-01"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metadata.json").write_text(
        '{"run_id": "run-win-01", "task": "Win test", "status": "APPROVED"}',
        encoding="utf-8",
    )

    app = DashboardApp(run_dir, project_root=tmp_path)
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    fake_out = MagicMock()
    mock_msvcrt = MagicMock()
    mock_msvcrt.kbhit.side_effect = [True, False]
    mock_msvcrt.getwch.side_effect = ["q"]

    with patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0), \
         patch("sys.__stdout__", fake_out), \
         patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
        exit_code = app.run()
        assert exit_code == 0
