"""Comprehensive test suite for Windows Job Object Containment and Lifecycle Guarantees.

Validates:
1. WindowsJobContainmentError hierarchy and non-Windows refusal.
2. WindowsJobObject creation and JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE configuration.
3. Suspended process launch (CREATE_SUSPENDED), pre-execution Job Object assignment,
   containment verification via IsProcessInJob, and main thread resumption.
4. Refusal of uncontained execution: if Job Object creation or assignment fails,
   the suspended process is immediately killed and WindowsJobContainmentError is raised
   with actionable diagnostics (e.g. parent job / sandbox restrictions).
5. Continuous Job Object handle retention on proc._job_object.
6. Termination verification: TerminateJobObject invocation and ActiveProcesses == 0 verification.
7. ProcessRecord and ProcessTracker containment tracking without persisting raw OS handles.
8. Native Windows integration tests (executes on native Windows OS / CI windows-latest runner).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from forge.adapters.base import BaseAdapter
from forge.core.platform import (
    IS_WINDOWS,
    IS_POSIX,
    WindowsJobContainmentError,
    WindowsJobObject,
    resume_process_threads,
    launch_contained_process,
    terminate_process_tree,
    get_process_group_flags,
    is_pid_alive,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JobObjectBasicAccountingInformation,
    JobObjectExtendedLimitInformation,
    CREATE_SUSPENDED,
    CREATE_NEW_PROCESS_GROUP,
    THREADENTRY32,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
)
from forge.core.process import ProcessRecord, ProcessTracker, ProcessLifecycleState


class DummyAdapter(BaseAdapter):
    """Concrete adapter for unit/integration testing of BaseAdapter subprocess routines."""

    def execute(self, prompt, **kwargs):
        pass

    def is_available(self) -> bool:
        return True


# ============================================================================
# 1. ERROR HIERARCHY & PLATFORM GUARDS
# ============================================================================

def test_windows_job_containment_error_hierarchy():
    """Verify WindowsJobContainmentError inherits from RuntimeError."""
    err = WindowsJobContainmentError("Test error")
    assert isinstance(err, RuntimeError)
    assert str(err) == "Test error"


def test_windows_job_object_refuses_non_windows(monkeypatch):
    """Verify WindowsJobObject refuses initialization on non-Windows platforms."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", False)
    with pytest.raises(WindowsJobContainmentError, match="WindowsJobObject is only supported on Windows"):
        WindowsJobObject()


def test_windows_job_object_fails_when_kernel32_missing(monkeypatch):
    """Verify WindowsJobObject raises when kernel32 is not available."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)
    with patch("forge.core.platform._get_kernel32", return_value=None):
        with pytest.raises(WindowsJobContainmentError, match="Win32 kernel32 library is not available"):
            WindowsJobObject()


# ============================================================================
# 2. JOB OBJECT CREATION & CONFIGURATION
# ============================================================================

def test_windows_job_object_creation_and_limits_configuration(monkeypatch):
    """Verify WindowsJobObject creates job and sets JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_k32 = MagicMock()
    mock_k32.CreateJobObjectW.return_value = 0x12345678  # Fake handle
    mock_k32.SetInformationJobObject.return_value = 1    # Success

    with patch("forge.core.platform._get_kernel32", return_value=mock_k32):
        job = WindowsJobObject()
        assert job.handle == 0x12345678

        # Verify CreateJobObjectW was called with None name (anonymous)
        mock_k32.CreateJobObjectW.assert_called_once_with(None, None)

        # Verify SetInformationJobObject was called with JobObjectExtendedLimitInformation
        assert mock_k32.SetInformationJobObject.called
        call_args = mock_k32.SetInformationJobObject.call_args[0]
        assert call_args[0] == 0x12345678
        assert call_args[1] == JobObjectExtendedLimitInformation

        # Verify CloseHandle called on close()
        job.close()
        mock_k32.CloseHandle.assert_called_once_with(0x12345678)
        assert job.handle is None


def test_windows_job_object_creation_failure(monkeypatch):
    """Verify WindowsJobContainmentError raised when CreateJobObjectW returns NULL."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_k32 = MagicMock()
    mock_k32.CreateJobObjectW.return_value = 0

    with patch("forge.core.platform._get_kernel32", return_value=mock_k32), \
         patch("forge.core.platform._get_last_error", return_value=5):
        with pytest.raises(WindowsJobContainmentError, match="CreateJobObjectW failed with error code 5"):
            WindowsJobObject()


def test_windows_job_object_limit_configuration_failure(monkeypatch):
    """Verify WindowsJobContainmentError raised when SetInformationJobObject fails."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_k32 = MagicMock()
    mock_k32.CreateJobObjectW.return_value = 0x9999
    mock_k32.SetInformationJobObject.return_value = 0  # Failed

    with patch("forge.core.platform._get_kernel32", return_value=mock_k32), \
         patch("forge.core.platform._get_last_error", return_value=87):
        with pytest.raises(WindowsJobContainmentError, match="SetInformationJobObject.*failed with error code 87"):
            WindowsJobObject()
        # Verify handle was cleaned up on failure
        mock_k32.CloseHandle.assert_called_once_with(0x9999)


# ============================================================================
# 3. PROCESS ASSIGNMENT & VERIFICATION
# ============================================================================

def test_windows_job_object_assign_and_verify_success(monkeypatch):
    """Verify AssignProcessToJobObject assigns process and IsProcessInJob verifies containment."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_k32 = MagicMock()
    mock_k32.CreateJobObjectW.return_value = 0x1111
    mock_k32.SetInformationJobObject.return_value = 1
    mock_k32.AssignProcessToJobObject.return_value = 1

    def fake_is_process_in_job(proc_h, job_h, byref_bool):
        byref_bool._obj.value = 1  # True
        return 1

    mock_k32.IsProcessInJob.side_effect = fake_is_process_in_job

    with patch("forge.core.platform._get_kernel32", return_value=mock_k32):
        job = WindowsJobObject()
        mock_proc = MagicMock()
        mock_proc.pid = 4321
        mock_proc._handle = 0x8888

        job.assign_process(mock_proc)
        mock_k32.AssignProcessToJobObject.assert_called_once()
        call_args = mock_k32.AssignProcessToJobObject.call_args[0]
        assert call_args[0] == 0x1111
        assert getattr(call_args[1], "value", call_args[1]) == 0x8888
        assert mock_k32.IsProcessInJob.called


def test_windows_job_object_assign_failure_access_denied(monkeypatch):
    """Verify AssignProcessToJobObject failure surfaces clear parent-job/sandbox diagnostic."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_k32 = MagicMock()
    mock_k32.CreateJobObjectW.return_value = 0x1111
    mock_k32.SetInformationJobObject.return_value = 1
    mock_k32.AssignProcessToJobObject.return_value = 0  # Failed

    with patch("forge.core.platform._get_kernel32", return_value=mock_k32), \
         patch("forge.core.platform._get_last_error", return_value=5):  # ERROR_ACCESS_DENIED
        job = WindowsJobObject()
        mock_proc = MagicMock(pid=5555, _handle=0x7777)

        with pytest.raises(WindowsJobContainmentError) as exc_info:
            job.assign_process(mock_proc)

        err_msg = str(exc_info.value)
        assert "AssignProcessToJobObject failed for PID=5555 with error 5" in err_msg
        assert "parent job or sandbox" in err_msg


def test_windows_job_object_is_process_in_job_verification_failure(monkeypatch):
    """Verify WindowsJobContainmentError raised if IsProcessInJob returns False."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_k32 = MagicMock()
    mock_k32.CreateJobObjectW.return_value = 0x1111
    mock_k32.SetInformationJobObject.return_value = 1
    mock_k32.AssignProcessToJobObject.return_value = 1

    def fake_is_process_in_job(proc_h, job_h, byref_bool):
        byref_bool._obj.value = 0  # Process NOT in job
        return 1

    mock_k32.IsProcessInJob.side_effect = fake_is_process_in_job

    with patch("forge.core.platform._get_kernel32", return_value=mock_k32):
        job = WindowsJobObject()
        mock_proc = MagicMock(pid=6666, _handle=0x5555)

        with pytest.raises(WindowsJobContainmentError, match="IsProcessInJob verification failed for PID=6666"):
            job.assign_process(mock_proc)


# ============================================================================
# 4. ACTIVE PROCESSES & TERMINATION
# ============================================================================

def test_windows_job_object_active_processes_count(monkeypatch):
    """Verify active_processes_count queries JobObjectBasicAccountingInformation."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_k32 = MagicMock()
    mock_k32.CreateJobObjectW.return_value = 0x1111
    mock_k32.SetInformationJobObject.return_value = 1

    def fake_query(job_h, info_class, byref_buf, buf_size, ret_len):
        assert info_class == JobObjectBasicAccountingInformation
        byref_buf._obj.ActiveProcesses = 3
        return 1

    mock_k32.QueryInformationJobObject.side_effect = fake_query

    with patch("forge.core.platform._get_kernel32", return_value=mock_k32):
        job = WindowsJobObject()
        assert job.active_processes_count() == 3


def test_windows_job_object_terminate_and_close(monkeypatch):
    """Verify terminate calls TerminateJobObject and close closes handle."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_k32 = MagicMock()
    mock_k32.CreateJobObjectW.return_value = 0x1111
    mock_k32.SetInformationJobObject.return_value = 1
    mock_k32.TerminateJobObject.return_value = 1

    with patch("forge.core.platform._get_kernel32", return_value=mock_k32):
        job = WindowsJobObject()
        res = job.terminate(exit_code=1)
        assert res is True
        mock_k32.TerminateJobObject.assert_called_once_with(0x1111, 1)

        job.close()
        mock_k32.CloseHandle.assert_called_once_with(0x1111)
        assert job.handle is None


# ============================================================================
# 5. SUSPENDED THREAD RESUMPTION
# ============================================================================

def test_resume_process_threads_via_ntdll(monkeypatch):
    """Verify resume_process_threads uses ntdll.NtResumeProcess when available."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_ntdll = MagicMock()
    mock_ntdll.NtResumeProcess.return_value = 0  # STATUS_SUCCESS

    with patch("forge.core.platform._get_ntdll", return_value=mock_ntdll):
        res = resume_process_threads(1234, proc_handle=0x5678)
        assert res is True
        assert mock_ntdll.NtResumeProcess.called
        call_arg = mock_ntdll.NtResumeProcess.call_args[0][0]
        assert getattr(call_arg, "value", call_arg) == 0x5678


def test_resume_process_threads_via_toolhelp(monkeypatch):
    """Verify resume_process_threads falls back to Toolhelp snapshot and ResumeThread."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_k32 = MagicMock()
    mock_k32.CreateToolhelp32Snapshot.return_value = 0xAAAA
    mock_k32.OpenThread.return_value = 0xBBBB
    mock_k32.ResumeThread.return_value = 1

    # Simulate 2 threads: one belonging to PID 1234, one belonging to PID 9999
    call_count = 0
    def fake_thread32_first(h_snap, byref_te):
        byref_te._obj.th32OwnerProcessID = 1234
        byref_te._obj.th32ThreadID = 501
        return 1

    def fake_thread32_next(h_snap, byref_te):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            byref_te._obj.th32OwnerProcessID = 9999  # Other process
            byref_te._obj.th32ThreadID = 502
            return 1
        return 0  # No more threads

    mock_k32.Thread32First.side_effect = fake_thread32_first
    mock_k32.Thread32Next.side_effect = fake_thread32_next

    with patch("forge.core.platform._get_ntdll", return_value=None), \
         patch("forge.core.platform._get_kernel32", return_value=mock_k32):
        res = resume_process_threads(1234)
        assert res is True
        mock_k32.OpenThread.assert_called_once_with(2, False, 501)
        assert mock_k32.ResumeThread.called
        res_arg = mock_k32.ResumeThread.call_args[0][0]
        assert getattr(res_arg, "value", res_arg) == 0xBBBB
        close_args = [getattr(c[0][0], "value", c[0][0]) for c in mock_k32.CloseHandle.call_args_list]
        assert 0xBBBB in close_args
        assert 0xAAAA in close_args


def test_resume_process_threads_falls_back_when_ntdll_returns_error(monkeypatch):
    """Verify resume_process_threads falls back to Toolhelp when NtResumeProcess returns NTSTATUS error."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_ntdll = MagicMock()
    mock_ntdll.NtResumeProcess.return_value = -1073741816  # STATUS_INVALID_HANDLE (0xC0000008)

    mock_k32 = MagicMock()
    mock_k32.CreateToolhelp32Snapshot.return_value = 0xAAAA
    mock_k32.OpenThread.return_value = 0xBBBB
    mock_k32.ResumeThread.return_value = 1

    def fake_thread32_first(h_snap, byref_te):
        byref_te._obj.th32OwnerProcessID = 1234
        byref_te._obj.th32ThreadID = 501
        return 1

    def fake_thread32_next(h_snap, byref_te):
        return 0

    mock_k32.Thread32First.side_effect = fake_thread32_first
    mock_k32.Thread32Next.side_effect = fake_thread32_next

    with patch("forge.core.platform._get_ntdll", return_value=mock_ntdll), \
         patch("forge.core.platform._get_kernel32", return_value=mock_k32):
        res = resume_process_threads(1234, proc_handle=0x5678)
        assert res is True
        # Both NtResumeProcess and Toolhelp were invoked
        assert mock_ntdll.NtResumeProcess.called
        assert mock_k32.CreateToolhelp32Snapshot.called
        assert mock_k32.ResumeThread.called


# ============================================================================
# 6. LAUNCH_CONTAINED_PROCESS & RACE ELIMINATION
# ============================================================================

def test_launch_contained_process_windows_happy_path(monkeypatch, tmp_path):
    """Verify launch_contained_process creates job, sets CREATE_SUSPENDED, assigns, and resumes."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_job = MagicMock(spec=WindowsJobObject)
    mock_job.handle = 0x1111

    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 7777
    mock_proc._handle = 0x8888

    mock_popen = MagicMock(return_value=mock_proc)
    mock_resume = MagicMock(return_value=True)

    tracker = ProcessTracker("run-test", "planner", 1, "test_adapter", run_dir=tmp_path)

    with patch("forge.core.platform.WindowsJobObject", return_value=mock_job), \
         patch("forge.core.platform.resume_process_threads", mock_resume):
        proc = launch_contained_process(
            ["agent", "start"],
            popen_kwargs={"cwd": str(tmp_path)},
            tracker=tracker,
            popen_fn=mock_popen,
        )

        assert proc is mock_proc
        assert getattr(proc, "_job_object", None) is mock_job
        assert getattr(proc, "_pgid", None) == 7777

        # Verify CREATE_SUSPENDED was passed to popen
        called_kwargs = mock_popen.call_args[1]
        assert "creationflags" in called_kwargs
        flags = called_kwargs["creationflags"]
        assert (flags & CREATE_SUSPENDED) != 0
        assert (flags & CREATE_NEW_PROCESS_GROUP) != 0

        # Verify assignment occurred
        mock_job.assign_process.assert_called_once_with(mock_proc)

        # Verify resume occurred with process handle
        mock_resume.assert_called_once_with(7777, proc_handle=0x8888)

        # Verify tracker recorded containment type
        assert tracker.record.pid == 7777
        assert tracker.record.containment_type == "windows_job_object"
        assert tracker.record.state == ProcessLifecycleState.RUNNING.value


def test_launch_contained_process_windows_refuses_uncontained_on_assign_error(monkeypatch, tmp_path):
    """Verify that if Job Object assignment fails, the suspended process is immediately killed."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_job = MagicMock(spec=WindowsJobObject)
    mock_job.assign_process.side_effect = WindowsJobContainmentError("Parent sandbox restricted job assignment")

    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 9999

    mock_popen = MagicMock(return_value=mock_proc)
    mock_resume = MagicMock()

    tracker = ProcessTracker("run-test", "planner", 1, "test_adapter", run_dir=tmp_path)

    with patch("forge.core.platform.WindowsJobObject", return_value=mock_job), \
         patch("forge.core.platform.resume_process_threads", mock_resume):
        with pytest.raises(WindowsJobContainmentError, match="Parent sandbox restricted job assignment"):
            launch_contained_process(
                ["agent", "start"],
                popen_kwargs={"cwd": str(tmp_path)},
                tracker=tracker,
                popen_fn=mock_popen,
            )

        # The suspended process MUST be killed immediately
        mock_proc.kill.assert_called_once()
        # Resume thread must NEVER have been called! Process executed 0 instructions!
        mock_resume.assert_not_called()
        # Job Object handle must be closed
        mock_job.close.assert_called_once()


def test_launch_contained_process_windows_fails_when_resumption_fails(monkeypatch, tmp_path):
    """Verify that if thread resumption fails, suspended process is killed, streams closed, and error raised."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", True)

    mock_job = MagicMock(spec=WindowsJobObject)
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 8888
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    mock_popen = MagicMock(return_value=mock_proc)
    mock_resume = MagicMock(return_value=False)  # Resumption failed!

    tracker = ProcessTracker("run-test", "planner", 1, "test_adapter", run_dir=tmp_path)

    with patch("forge.core.platform.WindowsJobObject", return_value=mock_job), \
         patch("forge.core.platform.resume_process_threads", mock_resume):
        with pytest.raises(WindowsJobContainmentError, match="Failed to resume suspended primary thread for PID 8888"):
            launch_contained_process(
                ["agent", "start"],
                popen_kwargs={"cwd": str(tmp_path)},
                tracker=tracker,
                popen_fn=mock_popen,
            )

        mock_proc.kill.assert_called_once()
        mock_job.close.assert_called_once()
        mock_proc.stdin.close.assert_called_once()
        mock_proc.stdout.close.assert_called_once()
        mock_proc.stderr.close.assert_called_once()


def test_launch_contained_process_posix_happy_path(monkeypatch, tmp_path):
    """Verify launch_contained_process sets start_new_session=True on POSIX."""
    monkeypatch.setattr("forge.core.platform.IS_WINDOWS", False)

    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 1234
    mock_popen = MagicMock(return_value=mock_proc)

    tracker = ProcessTracker("run-posix", "coder", 2, "test_adapter", run_dir=tmp_path)

    with patch("forge.core.platform.get_process_identity", return_value={"start_time_ticks": 100}):
        proc = launch_contained_process(
            ["agent", "run"],
            popen_kwargs={"cwd": str(tmp_path)},
            tracker=tracker,
            popen_fn=mock_popen,
        )

        assert proc is mock_proc
        assert getattr(proc, "_pgid", None) == 1234
        assert getattr(proc, "_start_time_ticks", None) == 100

        called_kwargs = mock_popen.call_args[1]
        assert called_kwargs.get("start_new_session") is True

        assert tracker.record.pid == 1234
        assert tracker.record.containment_type == "posix_session"
        assert tracker.record.start_time_ticks == 100


# ============================================================================
# 7. TERMINATE_PROCESS_TREE WITH JOB OBJECT
# ============================================================================

def test_terminate_process_tree_windows_with_job_object(monkeypatch):
    """Verify terminate_process_tree terminates Job Object and waits for active processes to reach 0."""
    mock_job = MagicMock(spec=WindowsJobObject)
    mock_job.terminate.return_value = True
    # active_processes drops from 2 to 0
    mock_job.active_processes_count.side_effect = [2, 0, 0]

    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 8888
    setattr(mock_proc, "_job_object", mock_job)

    with patch("os.name", "nt"), \
         patch("sys.platform", "win32"), \
         patch("forge.core.platform.is_pid_alive", return_value=False), \
         patch("forge.core.platform.safe_kill") as mock_safe_kill:
        res = terminate_process_tree(mock_proc, timeout=1.0)

        assert res is True
        mock_job.terminate.assert_called_once_with(exit_code=1)
        mock_job.close.assert_called_once()
        mock_safe_kill.assert_called_once_with(mock_proc)


# ============================================================================
# 8. PROCESS RECORD PERSISTENCE & DESERIALIZATION
# ============================================================================

def test_process_record_containment_type_persistence(tmp_path):
    """Verify ProcessRecord persists and restores containment_type across JSON serialization."""
    record = ProcessRecord(
        run_id="run-001",
        stage_name="planner",
        sequence_number=1,
        adapter="antigravity",
        pid=12345,
        pgid=12345,
        containment_type="windows_job_object",
    )
    rec_path = tmp_path / "processes" / "01_planner.json"
    record.save_atomic(rec_path)

    loaded = ProcessRecord.load(rec_path)
    assert loaded is not None
    assert loaded.pid == 12345
    assert loaded.containment_type == "windows_job_object"
    assert loaded.status == "CONFIRMED"


# ============================================================================
# 9. NATIVE WINDOWS INTEGRATION TESTS (Windows CI / host only)
# ============================================================================

@pytest.mark.skipif(not IS_WINDOWS, reason="Requires native Windows OS with kernel32.dll")
def test_native_windows_job_object_lifecycle():
    """Native Windows test: Create real Job Object, assign suspended Python process, verify and terminate."""
    job = WindowsJobObject()
    assert job.handle is not None

    # Spawn real Python process in suspended state
    creationflags = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        creationflags=creationflags,
    )
    try:
        # Assign to Job Object while suspended
        job.assign_process(proc)
        assert job.is_process_in_job(proc) is True
        assert job.active_processes_count() == 1

        # Resume suspended thread
        resumed = resume_process_threads(proc.pid, proc_handle=proc._handle)
        assert resumed is True

        # Terminate Job Object
        term_res = job.terminate(exit_code=1)
        assert term_res is True

        # Wait for active processes in job to reach 0
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if job.active_processes_count() == 0:
                break
            time.sleep(0.05)

        assert job.active_processes_count() == 0
        proc.wait(timeout=2.0)
    finally:
        job.close()
        try:
            proc.kill()
        except Exception:
            pass


@pytest.mark.skipif(not IS_WINDOWS, reason="Requires native Windows OS with kernel32.dll")
def test_native_windows_job_kills_orphaned_descendants_on_close(tmp_path):
    """Native Windows test: Verify KILL_ON_JOB_CLOSE kills child processes even after parent exits."""
    # Create a parent script that spawns a long-running child and immediately exits
    child_script = tmp_path / "child.py"
    child_script.write_text("import time; time.sleep(60)", encoding="utf-8")

    child_pid_file = tmp_path / "child.pid"
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        f"import subprocess, sys\n"
        f"p = subprocess.Popen([sys.executable, r'{child_script}'])\n"
        f"with open(r'{child_pid_file}', 'w', encoding='utf-8') as f:\n"
        f"    f.write(str(p.pid))\n"
        f"sys.exit(0)\n",
        encoding="utf-8",
    )

    proc = launch_contained_process(
        [sys.executable, str(parent_script)],
        popen_kwargs={"cwd": str(tmp_path)},
    )

    # Parent exits cleanly
    proc.wait(timeout=5.0)
    assert proc.returncode == 0

    # Wait for child PID to be written
    deadline = time.monotonic() + 3.0
    child_pid = None
    while time.monotonic() < deadline:
        if child_pid_file.exists():
            try:
                child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
                break
            except Exception:
                pass
        time.sleep(0.05)

    assert child_pid is not None, "Child PID file was not written"

    # Verify child process is alive and Job Object contains the running child process
    assert is_pid_alive(child_pid) is True
    assert proc._job_object.active_processes_count() >= 1

    # Closing Job Object handle triggers KILL_ON_JOB_CLOSE
    proc._job_object.close()

    # Verify child process was reaped by the OS kernel within deadline
    reaped = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not is_pid_alive(child_pid):
            reaped = True
            break
        time.sleep(0.1)

    assert reaped is True, f"Child process {child_pid} was not killed on Job Object close"


@pytest.mark.skipif(not IS_WINDOWS, reason="Requires native Windows OS with kernel32.dll")
def test_native_windows_contained_root_spawns_descendants(tmp_path):
    """Native Windows test: Contained root spawns children; verify all are tracked in Job Object."""
    # Script spawns 2 background python children
    parent_script = tmp_path / "spawner.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "p1 = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "p2 = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )

    proc = launch_contained_process(
        [sys.executable, str(parent_script)],
        popen_kwargs={"cwd": str(tmp_path)},
    )
    try:
        # Give children a moment to start and verify count
        deadline = time.monotonic() + 5.0
        active = 0
        while time.monotonic() < deadline:
            active = proc._job_object.active_processes_count()
            if active >= 3:
                break
            time.sleep(0.1)
        assert active >= 3, f"Expected >= 3 active processes (root + 2 children), got {active}"

        # Terminate Job Object
        proc._job_object.terminate(exit_code=1)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if proc._job_object.active_processes_count() == 0:
                break
            time.sleep(0.05)
        assert proc._job_object.active_processes_count() == 0
    finally:
        if getattr(proc, "_job_object", None):
            proc._job_object.close()
        try:
            proc.kill()
        except Exception:
            pass


@pytest.mark.skipif(not IS_WINDOWS, reason="Requires native Windows OS with kernel32.dll")
def test_native_windows_descendant_retaining_pipe_does_not_hang(tmp_path):
    """Native Windows test: Child holds stdout pipe open; cleanup terminates job within timeout."""
    # Script prints message, spawns a child that sleeps 60s holding stdout, and exits
    holder_script = tmp_path / "pipe_holder.py"
    holder_script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('PARENT_DONE')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )

    start_time = time.monotonic()
    adapter = DummyAdapter()
    stdout, stderr, code = adapter._run_subprocess(
        [sys.executable, str(holder_script)],
        cwd=tmp_path,
        timeout=10,
    )
    elapsed = time.monotonic() - start_time

    assert "PARENT_DONE" in stdout
    # Must complete cleanly without waiting for the 60s child!
    assert elapsed < 15.0


@pytest.mark.skipif(not IS_WINDOWS, reason="Requires native Windows OS with kernel32.dll")
def test_native_windows_adapter_timeout_and_cancellation_cleanup(tmp_path):
    """Native Windows test: Adapter timeout cleans up entire Job Object without leaking."""
    adapter = DummyAdapter()
    sleep_script = tmp_path / "sleeper.py"
    sleep_script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    with pytest.raises(subprocess.TimeoutExpired):
        adapter._run_subprocess(
            [sys.executable, str(sleep_script)],
            cwd=tmp_path,
            timeout=1,
        )

    # All subprocesses must be terminated
    assert len(adapter._active_procs) == 0


@pytest.mark.skipif(not IS_WINDOWS, reason="Requires native Windows OS with kernel32.dll")
def test_native_windows_repeated_cleanup_idempotent():
    """Native Windows test: Repeated terminate_process_tree calls are idempotent and leak-free."""
    job = WindowsJobObject()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        creationflags=CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP,
    )
    try:
        job.assign_process(proc)
        resume_process_threads(proc.pid, proc_handle=proc._handle)
        setattr(proc, "_job_object", job)

        res1 = terminate_process_tree(proc, timeout=1.0)
        assert res1 is True

        # Second cleanup call must be safe and return True
        res2 = terminate_process_tree(proc, timeout=1.0)
        assert res2 is True
    finally:
        job.close()
        try:
            proc.kill()
        except Exception:
            pass


# ============================================================================
# 10. STAGE COMPLETION & PID REUSE CONTRACT TESTS
# ============================================================================

def test_stage_completion_distinguishes_natural_zero_exit_from_forced_termination(tmp_path):
    """Verify that stage validation requires EXIT_VERIFIED and rejects TERMINATED/CLEANUP_FAILED."""
    from forge.core.role import Role
    from forge.core.context import Context
    from forge.core.config import Config
    from forge.core.git import GitService
    from forge.stages.stage import Stage
    from forge.storage.run_manager import RunManager

    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Test stage completion semantics")
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))
    role = Role(name="planner", sequence_number=1, template_content="Test", protocol_content="Test")

    # 1. Natural exit with code 0 -> EXIT_VERIFIED
    tracker_clean = ProcessTracker(run.run_id, "planner", 1, "test_adapter", run_dir=run.run_dir)
    tracker_clean.record_launched(pid=1001, pgid=1001, containment_type="windows_job_object")
    tracker_clean.record_exit(0)
    tracker_clean.record_cleanup_result(success=True, method="windows_job_object")
    assert tracker_clean.record.state == ProcessLifecycleState.EXIT_VERIFIED.value
    assert tracker_clean.record.exit_code == 0

    # 2. Forced kill during cleanup -> TERMINATED (must NOT authorize success!)
    tracker_forced = ProcessTracker(run.run_id, "planner", 2, "test_adapter", run_dir=run.run_dir)
    tracker_forced.record_launched(pid=1002, pgid=1002, containment_type="windows_job_object")
    # Never called record_exit(0); process hung and was forcibly reaped
    tracker_forced.record_cleanup_start(method="windows_job_object")
    tracker_forced.record_cleanup_result(success=True, method="windows_job_object")
    assert tracker_forced.record.state == ProcessLifecycleState.TERMINATED.value

    # 3. Cleanup failed -> CLEANUP_FAILED (status UNRESOLVED)
    tracker_failed = ProcessTracker(run.run_id, "planner", 3, "test_adapter", run_dir=run.run_dir)
    tracker_failed.record_launched(pid=1003, pgid=1003, containment_type="windows_job_object")
    tracker_failed.record_cleanup_result(success=False, method="windows_job_object", error="Descendants survived")
    assert tracker_failed.record.state == ProcessLifecycleState.CLEANUP_FAILED.value
    assert tracker_failed.record.status == "UNRESOLVED"


def test_windows_pid_reuse_rejection_with_creation_time():
    """Verify verify_process_identity rejects stale PID if creation_time does not match."""
    from forge.core.platform import verify_process_identity

    # Mock get_process_identity returning Windows creation_time
    with patch("forge.core.platform.get_process_identity") as mock_ident:
        mock_ident.return_value = {
            "pid": 5555,
            "creation_time": 133500000000000000,  # Reused PID with different timestamp
        }

        # Expected creation time was from the original process
        original_creation_time = 133400000000000000

        # Verification must FAIL, protecting the reused PID from accidental termination
        is_valid = verify_process_identity(5555, expected_start_time_ticks=original_creation_time)
        assert is_valid is False
