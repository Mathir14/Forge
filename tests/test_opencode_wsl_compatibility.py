"""Regression tests for WSL2 + Windows OpenCode compatibility detection."""

import os
import platform
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.opencode import (
    OpenCodeAdapter,
    is_windows_executable,
    is_wsl,
)
from forge.adapters.registry import AdapterRegistry
from forge.core.config import Config
from forge.core.events import AgentEventType


# ---------------------------------------------------------------------------
# 1. WSL Detection Tests
# ---------------------------------------------------------------------------

def test_is_wsl_via_distro_env(monkeypatch):
    """Verify is_wsl returns True when WSL_DISTRO_NAME is set on Linux."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    assert is_wsl() is True


def test_is_wsl_via_interop_env(monkeypatch):
    """Verify is_wsl returns True when WSL_INTEROP is set on Linux."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")
    assert is_wsl() is True


def test_is_wsl_via_kernel_release(monkeypatch):
    """Verify is_wsl returns True when kernel release contains 'microsoft'."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    with patch("platform.release", return_value="5.15.153.1-microsoft-standard-WSL2"), \
         patch.object(Path, "exists", return_value=False):
        assert is_wsl() is True


def test_is_wsl_non_linux(monkeypatch):
    """Verify is_wsl returns False on non-Linux platforms (e.g. darwin)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert is_wsl() is False


def test_is_wsl_native_linux(monkeypatch):
    """Verify is_wsl returns False on standard native Linux systems."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    with patch("platform.release", return_value="6.8.0-45-generic"), \
         patch.object(Path, "exists", return_value=False):
        assert is_wsl() is False


# ---------------------------------------------------------------------------
# 2. Windows Executable and Shim Detection Tests
# ---------------------------------------------------------------------------

def test_is_windows_executable_by_extension():
    """Verify Windows executable extensions (.exe, .cmd, .bat, .ps1) are detected."""
    assert is_windows_executable(r"C:\Users\test\AppData\Roaming\npm\opencode.cmd") is True
    assert is_windows_executable("/mnt/c/Program Files/OpenCode/opencode.exe") is True
    assert is_windows_executable("/mnt/c/bin/opencode.bat") is True
    assert is_windows_executable("/mnt/c/bin/opencode.ps1") is True


def test_is_windows_executable_by_pe_header(tmp_path):
    """Verify DOS/PE 'MZ' header identifies Windows binaries even without .exe suffix."""
    pe_bin = tmp_path / "opencode_pe"
    pe_bin.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00")
    assert is_windows_executable(str(pe_bin)) is True


def test_is_windows_executable_npm_shim_with_sibling_cmd(tmp_path):
    """Verify Windows npm shims with sibling .cmd scripts are identified as Windows."""
    shim = tmp_path / "opencode"
    shim.write_text("#!/bin/sh\nexec opencode.exe \"$@\"\n", encoding="utf-8")
    sibling_cmd = tmp_path / "opencode.cmd"
    sibling_cmd.write_text("@ECHO off\n", encoding="utf-8")

    assert is_windows_executable(str(shim)) is True


def test_is_windows_executable_by_windows_mount_and_appdata():
    """Verify paths under /mnt/c/.../AppData/Roaming/npm are detected even as mock paths."""
    mock_path = "/mnt/c/Users/Pravin/AppData/Roaming/npm/opencode"
    assert is_windows_executable(mock_path) is True


def test_is_windows_executable_script_referencing_exe(tmp_path):
    """Verify wrapper scripts explicitly executing .exe are identified as Windows."""
    script = tmp_path / "opencode"
    script.write_text("#!/bin/sh\nexec \"$basedir/opencode.exe\" \"$@\"\n", encoding="utf-8")
    assert is_windows_executable(str(script)) is True


def test_linux_elf_on_mounted_volume_accepted(tmp_path):
    """Verify requirement 4: ELF executables on /mnt volumes are NOT blindly rejected."""
    elf_bin = tmp_path / "opencode"
    elf_bin.write_bytes(b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00")
    # Even if path is mocked as a /mnt path, ELF header must take precedence
    assert is_windows_executable(str(elf_bin)) is False


def test_standard_linux_shell_script_accepted(tmp_path):
    """Verify native Linux shell script wrappers are accepted."""
    script = tmp_path / "opencode"
    script.write_text("#!/bin/sh\nexec /usr/lib/node_modules/opencode/bin/cli.js \"$@\"\n", encoding="utf-8")
    assert is_windows_executable(str(script)) is False


# ---------------------------------------------------------------------------
# 3. Adapter Compatibility & Rejection Behavior (WSL + Windows Binary)
# ---------------------------------------------------------------------------

def test_wsl_windows_mounted_opencode_rejected(monkeypatch):
    """WSL + Windows-mounted OpenCode is rejected before execution with clear diagnostic."""
    adapter = OpenCodeAdapter()
    win_bin = "/mnt/c/Users/Pravin/AppData/Roaming/npm/opencode"

    with patch("forge.adapters.opencode.is_wsl", return_value=True), \
         patch("shutil.which", return_value=win_bin), \
         patch.object(adapter, "_find_candidates", return_value=[win_bin]):

        # 1. is_available() must return False
        assert adapter.is_available() is False

        # 2. availability_error must contain clear explanation and guidance
        err = adapter.availability_error
        assert err is not None
        assert "OpenCode resolved to a Windows installation while Forge is running inside WSL2" in err
        assert win_bin in err
        assert "Forge currently requires a native Linux OpenCode installation inside WSL2" in err
        assert "npm install -g opencode" in err

        # 3. _get_binary() must return None
        assert adapter._get_binary() is None

        # 4. validate_availability() must raise RuntimeError with the diagnostic
        with pytest.raises(RuntimeError) as exc_info:
            adapter.validate_availability()
        assert win_bin in str(exc_info.value)

        # 5. execute() must immediately return exit_code=1 with diagnostic in stderr
        resp = adapter.execute(prompt="Audit codebase")
        assert resp.exit_code == 1
        assert "OpenCode resolved to a Windows installation while Forge is running inside WSL2" in resp.stderr

        # 6. iter_events() must yield an AgentEventType.ERROR event with diagnostic
        events = list(adapter.iter_events(prompt="Audit codebase"))
        assert len(events) == 1
        assert events[0].event_type == AgentEventType.ERROR
        assert "OpenCode resolved to a Windows installation while Forge is running inside WSL2" in events[0].text
        assert events[0].result.exit_code == 1


def test_wsl_native_linux_opencode_accepted():
    """WSL + native Linux OpenCode is accepted normally."""
    adapter = OpenCodeAdapter()
    linux_bin = "/usr/local/bin/opencode"

    with patch("forge.adapters.opencode.is_wsl", return_value=True), \
         patch("shutil.which", return_value=linux_bin), \
         patch.object(adapter, "_find_candidates", return_value=[linux_bin]):

        assert adapter.is_available() is True
        assert adapter.availability_error is None
        assert adapter._get_binary() == linux_bin


def test_non_wsl_linux_normal_opencode_accepted():
    """Non-WSL Linux + standard OpenCode binary is accepted normally."""
    adapter = OpenCodeAdapter()
    linux_bin = "/usr/bin/opencode"

    with patch("forge.adapters.opencode.is_wsl", return_value=False), \
         patch("shutil.which", return_value=linux_bin):

        assert adapter.is_available() is True
        assert adapter.availability_error is None
        assert adapter._get_binary() == linux_bin


def test_non_wsl_linux_preserves_mocked_windows_test_path():
    """Verify non-WSL Linux does not interfere with Windows mock tests (e.g. test_fixes.py)."""
    adapter = OpenCodeAdapter()
    win_cmd = r"C:\Users\test\AppData\Roaming\npm\opencode.cmd"

    with patch("forge.adapters.opencode.is_wsl", return_value=False), \
         patch("shutil.which", return_value=win_cmd):

        assert adapter.is_available() is True
        assert adapter.availability_error is None
        assert adapter._get_binary() == win_cmd


def test_wsl_prefers_native_linux_when_windows_in_path_earlier():
    """When both Windows and Linux binaries appear in PATH under WSL, native Linux is preferred."""
    adapter = OpenCodeAdapter()
    win_bin = "/mnt/c/Users/Pravin/AppData/Roaming/npm/opencode"
    linux_bin = "/home/mathir/.local/bin/opencode"

    # Even if Windows binary is first in candidates / shutil.which
    with patch("forge.adapters.opencode.is_wsl", return_value=True), \
         patch("shutil.which", return_value=win_bin), \
         patch.object(adapter, "_find_candidates", return_value=[win_bin, linux_bin]):

        assert adapter.is_available() is True
        assert adapter.availability_error is None
        assert adapter._get_binary() == linux_bin


# ---------------------------------------------------------------------------
# 4. CLI & Doctor Integration Tests
# ---------------------------------------------------------------------------

def test_cli_get_adapter_surfaces_wsl_diagnostic(tmp_path):
    """Verify forge CLI _get_adapter halts with explicit diagnostic on WSL + Windows OpenCode."""
    from forge.cli import _get_adapter

    config = Config.default()
    win_bin = "/mnt/c/Users/Pravin/AppData/Roaming/npm/opencode"

    adapter = OpenCodeAdapter()
    with patch("forge.adapters.opencode.is_wsl", return_value=True), \
         patch("shutil.which", return_value=win_bin), \
         patch.object(OpenCodeAdapter, "_find_candidates", return_value=[win_bin]):

        # exit_on_error=False raises RuntimeError with the exact diagnostic
        with pytest.raises(RuntimeError) as exc_info:
            _get_adapter(config, "critic", exit_on_error=False)

        assert "OpenCode resolved to a Windows installation while Forge is running inside WSL2" in str(exc_info.value)
        assert win_bin in str(exc_info.value)


def test_registry_check_all_tools_reports_wsl_incompatible():
    """Verify AdapterRegistry.check_all_tools marks OpenCode as incompatible under WSL."""
    win_bin = "/mnt/c/Users/Pravin/AppData/Roaming/npm/opencode"

    with patch("forge.adapters.opencode.is_wsl", return_value=True), \
         patch("shutil.which", return_value=win_bin), \
         patch.object(OpenCodeAdapter, "_find_candidates", return_value=[win_bin]):

        tools = AdapterRegistry.check_all_tools()
        opencode_entry = next((t for t in tools if t["name"] == "OpenCode"), None)
        assert opencode_entry is not None
        assert opencode_entry["found"] is False
        assert opencode_entry.get("incompatible") is True
        assert "OpenCode resolved to a Windows installation while Forge is running inside WSL2" in opencode_entry["error"]
