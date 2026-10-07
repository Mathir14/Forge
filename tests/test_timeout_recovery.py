"""Comprehensive unit and integration tests for timeout recovery and partial output preservation (Phase 2)."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.base import AdapterResponse, BaseAdapter
from forge.adapters.codex import CodexAdapter
from forge.adapters.opencode import OpenCodeAdapter


class DummyTimeoutAdapter(BaseAdapter):
    """Test adapter exposing _run_subprocess for timeout testing."""

    def is_available(self) -> bool:
        return True

    def execute(self, prompt: str, cwd=None, timeout=None) -> AdapterResponse:
        return self._create_timeout_response(
            exc=subprocess.TimeoutExpired(cmd=["dummy"], timeout=timeout, output="test stdout", stderr="test stderr"),
            timeout_val=timeout,
            duration=1.5,
        )


def test_timeout_preserves_stdout_string():
    """Verify partial stdout string is preserved in stdout and raw_output upon timeout."""
    exc = subprocess.TimeoutExpired(
        cmd=["dummy"],
        timeout=10,
        output="```yaml\nROLE: EXECUTOR\nSTATUS: PARTIAL\n```",
    )
    resp = BaseAdapter._create_timeout_response(exc, timeout_val=10, duration=2.5)

    assert resp.exit_code == 124
    assert resp.duration_seconds == 2.5
    assert resp.stdout == "```yaml\nROLE: EXECUTOR\nSTATUS: PARTIAL\n```"
    assert "timed out after 10 seconds" in resp.stderr
    assert resp.raw_output == "```yaml\nROLE: EXECUTOR\nSTATUS: PARTIAL\n```"


def test_timeout_preserves_stdout_bytes():
    """Verify partial stdout raw bytes are safely decoded to text in stdout and raw_output."""
    exc = subprocess.TimeoutExpired(
        cmd=["dummy"],
        timeout=15,
        output=b"PARTIAL_TOKEN_DATA\nLine 2",
    )
    resp = BaseAdapter._create_timeout_response(exc, timeout_val=15, duration=3.0)

    assert resp.exit_code == 124
    assert resp.stdout == "PARTIAL_TOKEN_DATA\nLine 2"
    assert resp.raw_output == "PARTIAL_TOKEN_DATA\nLine 2"
    assert "timed out after 15 seconds" in resp.stderr


def test_timeout_preserves_stderr_string():
    """Verify partial stderr string is preserved and included in stderr and raw_output."""
    exc = subprocess.TimeoutExpired(
        cmd=["dummy"],
        timeout=5,
        output=None,
        stderr="Diagnostic banner: compilation failed before turn end",
    )
    resp = BaseAdapter._create_timeout_response(exc, timeout_val=5, duration=5.0)

    assert resp.exit_code == 124
    assert "Diagnostic banner: compilation failed before turn end" in resp.stderr
    assert "timed out after 5 seconds" in resp.stderr
    assert resp.stdout == ""
    assert resp.raw_output == "Diagnostic banner: compilation failed before turn end"


def test_timeout_preserves_stderr_bytes():
    """Verify partial stderr bytes are safely decoded into stderr and raw_output."""
    exc = subprocess.TimeoutExpired(
        cmd=["dummy"],
        timeout=8,
        output=None,
        stderr=b"STDERR_RAW_BYTES_WARNING",
    )
    resp = BaseAdapter._create_timeout_response(exc, timeout_val=8, duration=8.0)

    assert resp.exit_code == 124
    assert "STDERR_RAW_BYTES_WARNING" in resp.stderr
    assert resp.raw_output == "STDERR_RAW_BYTES_WARNING"


def test_timeout_preserves_both_stdout_and_stderr():
    """Verify when both stdout and stderr exist on timeout, both are preserved and combined in raw_output."""
    exc = subprocess.TimeoutExpired(
        cmd=["dummy"],
        timeout=20,
        output="stdout text stream",
        stderr="stderr diagnostic stream",
    )
    resp = BaseAdapter._create_timeout_response(exc, timeout_val=20, duration=4.2)

    assert resp.exit_code == 124
    assert resp.stdout == "stdout text stream"
    assert "stderr diagnostic stream" in resp.stderr
    assert "timed out after 20 seconds" in resp.stderr
    assert "stdout text stream" in resp.raw_output
    assert "stderr diagnostic stream" in resp.raw_output


def test_timeout_empty_output_fallback_preserves_caller_expectations():
    """Verify when no stdout or stderr was produced, timeout fallback message is retained for compatibility."""
    exc = subprocess.TimeoutExpired(cmd=["dummy"], timeout=30, output=None, stderr=None)
    resp = BaseAdapter._create_timeout_response(exc, timeout_val=30, duration=30.0)

    assert resp.exit_code == 124
    assert resp.stdout == ""
    assert resp.stderr == "Execution timed out after 30 seconds."
    assert resp.raw_output == "Execution timed out after 30 seconds."


def test_antigravity_adapter_timeout_preserves_partial_output():
    """Verify AntigravityAdapter preserves partial output on TimeoutExpired."""
    adapter = AntigravityAdapter()
    partial_yaml = "```yaml\nROLE: EXECUTOR\nSTATUS: RUNNING\n```"
    partial_err = "agy: print timeout after 1200s with turn in progress"

    timeout_exc = subprocess.TimeoutExpired(
        cmd=["agy"],
        timeout=1200,
        output=partial_yaml,
        stderr=partial_err,
    )

    with patch.object(adapter, "_get_binary", return_value="/usr/bin/agy"), \
         patch.object(adapter, "_run_subprocess", side_effect=timeout_exc):
        res = adapter.execute(prompt="Implement feature", timeout=1200)

    assert res.exit_code == 124
    assert res.stdout == partial_yaml
    assert partial_err in res.stderr
    assert "Execution timed out after 1200 seconds." in res.stderr
    assert partial_yaml in res.raw_output
    assert partial_err in res.raw_output


def test_opencode_adapter_timeout_preserves_partial_output():
    """Verify OpenCodeAdapter preserves partial output on TimeoutExpired."""
    adapter = OpenCodeAdapter()
    partial_text = "Analyzing repository files..."
    timeout_exc = subprocess.TimeoutExpired(
        cmd=["opencode"],
        timeout=300,
        output=partial_text.encode("utf-8"),
        stderr=b"opencode: process killed after timeout",
    )

    with patch.object(adapter, "_get_binary", return_value="/usr/bin/opencode"), \
         patch.object(adapter, "_run_subprocess", side_effect=timeout_exc):
        res = adapter.execute(prompt="Plan project", timeout=300)

    assert res.exit_code == 124
    assert res.stdout == partial_text
    assert "opencode: process killed after timeout" in res.stderr
    assert "Execution timed out after 300 seconds." in res.stderr
    assert partial_text in res.raw_output


def test_codex_adapter_timeout_preserves_partial_output():
    """Verify CodexAdapter preserves partial output on TimeoutExpired."""
    adapter = CodexAdapter()
    partial_text = "Processing code edits..."
    timeout_exc = subprocess.TimeoutExpired(
        cmd=["codex"],
        timeout=600,
        output=partial_text,
        stderr="codex: connection timeout",
    )

    with patch.object(adapter, "_get_binary", return_value="/usr/bin/codex"), \
         patch.object(adapter, "_run_subprocess", side_effect=timeout_exc):
        res = adapter.execute(prompt="Refactor auth", timeout=600)

    assert res.exit_code == 124
    assert res.stdout == partial_text
    assert "codex: connection timeout" in res.stderr
    assert "Execution timed out after 600 seconds." in res.stderr
    assert partial_text in res.raw_output


@pytest.mark.skipif(sys.platform == "win32", reason="Windows subprocess pipe reader threads do not guarantee partial buffer drain on immediate timeout")
def test_real_subprocess_timeout_captures_partial_output(tmp_path):
    """Verify a real OS child process timing out has its pre-timeout output captured non-blockingly."""
    # Subprocess writes a unique token, flushes immediately, then sleeps longer than timeout
    token = "UNIQUE_PIPELINE_SALVAGE_TOKEN_12345"
    child_code = f"import sys, time; sys.stdout.write('{token}\\n'); sys.stdout.flush(); time.sleep(5)"

    cmd = [sys.executable, "-c", child_code]
    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        BaseAdapter._run_subprocess(cmd=cmd, cwd=tmp_path, timeout=0.2)

    exc = exc_info.value
    resp = BaseAdapter._create_timeout_response(exc, timeout_val=0.2, duration=0.2)

    assert resp.exit_code == 124
    assert token in resp.stdout
    assert token in resp.raw_output
    assert "timed out after 0.2 seconds" in resp.stderr
