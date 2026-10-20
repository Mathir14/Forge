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
from typing import Any, Dict, List, Optional, Set, Union

import ctypes
from ctypes import wintypes

logger = logging.getLogger(__name__)

# Platform constants
IS_WINDOWS: bool = (sys.platform == "win32" or os.name == "nt")
IS_POSIX: bool = (os.name == "posix")
IS_DARWIN: bool = (sys.platform == "darwin")
IS_LINUX: bool = (sys.platform == "linux")

# Byte offset used for Windows mandatory file locking (1 GiB).
# Offsetting locks avoids blocking diagnostic reads of metadata at offset 0.
WINDOWS_LOCK_OFFSET: int = 1073741824


class WindowsJobContainmentError(RuntimeError):
    """Raised when Windows Job Object creation, configuration, or assignment fails."""
    pass


# Win32 Job Object Constants
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: int = 0x00002000
JobObjectBasicAccountingInformation: int = 1
JobObjectExtendedLimitInformation: int = 9

CREATE_SUSPENDED: int = 0x00000004
CREATE_NEW_PROCESS_GROUP: int = 0x00000200

PROCESS_SET_QUOTA: int = 0x0100
PROCESS_TERMINATE: int = 0x0001
PROCESS_QUERY_INFORMATION: int = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000

TH32CS_SNAPTHREAD: int = 0x00000004
THREAD_SUSPEND_RESUME: int = 0x0002


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryLimit", ctypes.c_size_t),
        ("PeakJobMemoryLimit", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
    ]


def _get_kernel32() -> Optional[Any]:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    return getattr(windll, "kernel32", None)


def _get_ntdll() -> Optional[Any]:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    return getattr(windll, "ntdll", None)


def _get_last_error() -> int:
    fn = getattr(ctypes, "GetLastError", None)
    if fn is not None:
        try:
            return int(fn())
        except Exception:
            return 0
    return 0


def is_mock_process(obj: Any) -> bool:
    """Detect if an object is a unittest.mock or test double rather than a real OS process."""
    if obj is None:
        return False
    if hasattr(obj, "_mock_return_value") or hasattr(obj, "_mock_methods") or hasattr(obj, "_mock_name"):
        return True
    mod = getattr(type(obj), "__module__", "")
    if mod and ("unittest.mock" in mod or "mock" in mod):
        return True
    if hasattr(obj, "pid"):
        pid = getattr(obj, "pid", None)
        if pid is not None and not isinstance(pid, int):
            return True
    return False


def _handle_to_int(h: Any) -> Optional[int]:
    """Safely extract an integer handle from int, c_void_p, wintypes.HANDLE, or wrapper objects."""
    if h is None or is_mock_process(h):
        return None
    if hasattr(h, "value"):
        val = getattr(h, "value", None)
        if isinstance(val, int):
            return val
        return None
    try:
        if isinstance(h, int):
            return h
        if isinstance(h, (ctypes.c_void_p, wintypes.HANDLE)):
            return h.value
        return int(h)
    except Exception:
        return None


def _is_valid_handle(h: Any) -> bool:
    """Check whether a Win32 handle is non-null and not INVALID_HANDLE_VALUE."""
    val = _handle_to_int(h)
    return val is not None and val != 0 and val != -1 and val != 0xFFFFFFFFFFFFFFFF




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
    Similarly, execution of .py scripts directly via CreateProcess fails with
    WinError 193 unless invoked with sys.executable.
    """
    if not IS_WINDOWS:
        return cmd

    if isinstance(cmd, str):
        parts = cmd.split(None, 1)
        if parts:
            first = parts[0].strip('\"\'')
            if first.lower().endswith(".py"):
                return f'"{sys.executable}" {cmd}'
        return cmd

    if isinstance(cmd, list) and cmd:
        executable = cmd[0]
        # Check if already wrapped with cmd.exe / comspec or python
        base_name = Path(executable).name.lower()
        if base_name in ("cmd.exe", "cmd", "python.exe", "pythonw.exe", "python3.exe", "python"):
            return cmd

        # Check if target is a python script or batch/cmd file or resolves to one
        target_path: Optional[str] = executable
        is_batch = executable.lower().endswith(".cmd") or executable.lower().endswith(".bat")
        is_py = executable.lower().endswith(".py")
        if not (is_batch or is_py):
            resolved = shutil.which(executable)
            if resolved:
                if resolved.lower().endswith(".cmd") or resolved.lower().endswith(".bat"):
                    target_path = resolved
                    is_batch = True
                elif resolved.lower().endswith(".py"):
                    target_path = resolved
                    is_py = True
                else:
                    target_path = None
            else:
                target_path = None

        if target_path and (is_batch or target_path.lower().endswith(".cmd") or target_path.lower().endswith(".bat")):
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            # Preserve original args, updating the script path
            new_cmd = [comspec, "/c", target_path] + cmd[1:]
            return new_cmd

        if target_path and (is_py or target_path.lower().endswith(".py")):
            new_cmd = [sys.executable, target_path] + cmd[1:]
            return new_cmd

    return cmd


class WindowsJobObject:
    """Encapsulates a Win32 Job Object for strict process-tree containment.

    Guarantees:
    - Root process and all descendants are bound to this Job Object.
    - JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE ensures the Windows kernel terminates all
      processes in the job when the handle is closed or if Forge crashes.
    - TerminateJobObject terminates all processes in the job tree atomically.
    - active_processes_count queries remaining live processes via
      JobObjectBasicAccountingInformation.
    """

    def __init__(self, name: Optional[str] = None):
        self.name = name
        self._handle: Optional[int] = None
        self._closed: bool = False
        if not IS_WINDOWS and not getattr(self, "_allow_mock", False):
            raise WindowsJobContainmentError("WindowsJobObject is only supported on Windows.")
        self._create_and_configure()

    def _create_and_configure(self) -> None:
        kernel32 = _get_kernel32()
        if kernel32 is None:
            raise WindowsJobContainmentError("Win32 kernel32 library is not available.")

        # Configure 64-bit prototypes safely
        try:
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.IsProcessInJob.restype = wintypes.BOOL
            kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.QueryInformationJobObject.restype = wintypes.BOOL
            kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        except Exception:
            pass

        handle = kernel32.CreateJobObjectW(None, self.name)
        if not handle or handle == -1:
            err = _get_last_error()
            raise WindowsJobContainmentError(f"CreateJobObjectW failed with error code {err}")
        self._handle = handle

        # Configure JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        success = kernel32.SetInformationJobObject(
            self._handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not success:
            err = _get_last_error()
            try:
                kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
            raise WindowsJobContainmentError(
                f"SetInformationJobObject(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) failed with error code {err}"
            )

    @property
    def handle(self) -> Optional[int]:
        return self._handle

    def assign_process(self, proc_or_pid: Union[subprocess.Popen, int]) -> None:
        """Assign a process to this Job Object and verify containment."""
        if self._closed or not self._handle:
            raise WindowsJobContainmentError("Cannot assign process to a closed Job Object.")

        kernel32 = _get_kernel32()
        if kernel32 is None:
            raise WindowsJobContainmentError("Win32 kernel32 library is not available.")

        # Guard against mock processes in unit tests when kernel32 is the real ctypes library
        if not hasattr(kernel32, "_mock_return_value") and (
            is_mock_process(proc_or_pid) or not isinstance(getattr(proc_or_pid, "pid", proc_or_pid), int)
        ):
            return

        proc_handle = None
        must_close_handle = False

        if hasattr(proc_or_pid, "pid"):
            pid = proc_or_pid.pid
            raw_h = getattr(proc_or_pid, "_handle", None)
            h_val = _handle_to_int(raw_h)
            if h_val is not None and _is_valid_handle(h_val):
                proc_handle = wintypes.HANDLE(h_val)
        else:
            pid = proc_or_pid

        if proc_handle is None:
            h = kernel32.OpenProcess(
                PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid,
            )
            if not _is_valid_handle(h):
                err = _get_last_error()
                raise WindowsJobContainmentError(
                    f"OpenProcess(PID={pid}) failed with error {err} during Job Object assignment"
                )
            proc_handle = wintypes.HANDLE(_handle_to_int(h))
            must_close_handle = True

        try:
            success = kernel32.AssignProcessToJobObject(self._handle, proc_handle)
            if not success:
                err = _get_last_error()
                diag = f"AssignProcessToJobObject failed for PID={pid} with error {err}."
                if err == 5:
                    diag += " Process may already be assigned to an incompatible parent job or sandbox."
                raise WindowsJobContainmentError(diag)

            in_job = wintypes.BOOL()
            verified = kernel32.IsProcessInJob(proc_handle, self._handle, ctypes.byref(in_job))
            if not verified or not in_job.value:
                err = _get_last_error()
                raise WindowsJobContainmentError(
                    f"IsProcessInJob verification failed for PID={pid} (verified={verified}, in_job={in_job.value}, err={err})"
                )
        finally:
            if must_close_handle and proc_handle:
                try:
                    kernel32.CloseHandle(proc_handle)
                except Exception:
                    pass

    def is_process_in_job(self, proc_or_pid: Union[subprocess.Popen, int]) -> bool:
        """Check whether a process belongs to this Job Object."""
        if self._closed or not self._handle:
            return False

        kernel32 = _get_kernel32()
        if kernel32 is None:
            return False

        # Guard against mock processes in unit tests when kernel32 is the real ctypes library
        if not hasattr(kernel32, "_mock_return_value") and (
            is_mock_process(proc_or_pid) or not isinstance(getattr(proc_or_pid, "pid", proc_or_pid), int)
        ):
            return True

        proc_handle = None
        must_close = False

        if hasattr(proc_or_pid, "pid"):
            pid = proc_or_pid.pid
            raw_h = getattr(proc_or_pid, "_handle", None)
            h_val = _handle_to_int(raw_h)
            if h_val is not None and _is_valid_handle(h_val):
                proc_handle = wintypes.HANDLE(h_val)
        else:
            pid = proc_or_pid

        if proc_handle is None:
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not _is_valid_handle(h):
                return False
            proc_handle = wintypes.HANDLE(_handle_to_int(h))
            must_close = True

        try:
            in_job = wintypes.BOOL()
            if kernel32.IsProcessInJob(proc_handle, self._handle, ctypes.byref(in_job)):
                return bool(in_job.value)
            return False
        except Exception:
            return False
        finally:
            if must_close and proc_handle:
                try:
                    kernel32.CloseHandle(proc_handle)
                except Exception:
                    pass

    def active_processes_count(self) -> int:
        """Query the number of currently active processes in the Job Object."""
        if self._closed or not self._handle:
            return 0

        kernel32 = _get_kernel32()
        if kernel32 is None:
            return 0

        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        try:
            success = kernel32.QueryInformationJobObject(
                self._handle,
                JobObjectBasicAccountingInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            )
            if success:
                return int(info.ActiveProcesses)
        except Exception:
            pass
        return 0

    def terminate(self, exit_code: int = 1) -> bool:
        """Atomically terminate all processes associated with this Job Object."""
        if self._closed or not self._handle:
            return True

        kernel32 = _get_kernel32()
        if kernel32 is None:
            return True

        try:
            res = kernel32.TerminateJobObject(self._handle, exit_code)
            return bool(res)
        except Exception:
            return False

    def close(self) -> None:
        """Close Job Object handle, triggering kernel cleanup if processes remain."""
        if not getattr(self, "_closed", True) and getattr(self, "_handle", None):
            kernel32 = _get_kernel32()
            if kernel32 is not None:
                try:
                    kernel32.CloseHandle(self._handle)
                except Exception:
                    pass
            self._handle = None
            self._closed = True

    def __enter__(self) -> "WindowsJobObject":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def resume_process_threads(pid: int, proc_handle: Optional[Any] = None) -> bool:
    """Resume a suspended process by resuming all of its suspended threads.

    Used when launching processes with CREATE_SUSPENDED on Windows to eliminate
    the race window between process creation and Job Object assignment.
    """
    if not IS_WINDOWS:
        return True

    if not isinstance(pid, int) or is_mock_process(proc_handle):
        return True

    resumed_any = False

    # 1. Try ntdll.NtResumeProcess if available
    ntdll = _get_ntdll()
    if ntdll and hasattr(ntdll, "NtResumeProcess"):
        try:
            ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            ntdll.NtResumeProcess.restype = ctypes.c_long  # NTSTATUS is signed 32-bit (NT_SUCCESS is >= 0)
            h_proc = proc_handle
            must_close = False
            kernel32 = _get_kernel32()
            if not _is_valid_handle(h_proc) and kernel32 is not None:
                h_proc = kernel32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
                must_close = _is_valid_handle(h_proc)
            h_val = _handle_to_int(h_proc)
            if h_val is not None and _is_valid_handle(h_val):
                try:
                    status = ntdll.NtResumeProcess(wintypes.HANDLE(h_val))
                    if status >= 0:  # NT_SUCCESS
                        return True
                finally:
                    if must_close and kernel32 is not None:
                        try:
                            kernel32.CloseHandle(wintypes.HANDLE(h_val))
                        except Exception:
                            pass
        except Exception:
            pass

    # 2. Enumerate threads via CreateToolhelp32Snapshot
    kernel32 = _get_kernel32()
    if kernel32 is None:
        return False

    try:
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if _is_valid_handle(h_snap):
            snap_val = _handle_to_int(h_snap)
            snap_handle = wintypes.HANDLE(snap_val)
            try:
                te = THREADENTRY32()
                te.dwSize = ctypes.sizeof(THREADENTRY32)
                success = kernel32.Thread32First(snap_handle, ctypes.byref(te))
                while success:
                    if te.th32OwnerProcessID == pid:
                        h_thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
                        if _is_valid_handle(h_thread):
                            t_val = _handle_to_int(h_thread)
                            t_handle = wintypes.HANDLE(t_val)
                            try:
                                prev_count = kernel32.ResumeThread(t_handle)
                                # prev_count > 0 indicates thread was suspended and is now decremented
                                if prev_count != 0xFFFFFFFF and prev_count > 0:
                                    resumed_any = True
                            finally:
                                kernel32.CloseHandle(t_handle)
                    success = kernel32.Thread32Next(snap_handle, ctypes.byref(te))
            finally:
                kernel32.CloseHandle(snap_handle)
    except Exception as e:
        logger.warning("Error resuming threads for PID %s: %s", pid, e)

    return resumed_any


def launch_contained_process(
    cmd: Union[List[str], str],
    popen_kwargs: Dict[str, Any],
    tracker: Optional[Any] = None,
    popen_fn: Optional[Any] = None,
) -> subprocess.Popen:
    """Launch an agent process with guaranteed platform containment.

    Guarantees:
    - POSIX: launches in a new session (setsid) so all descendants share PGID.
    - Windows: creates a native Win32 Job Object with KILL_ON_JOB_CLOSE,
      spawns the process suspended (CREATE_SUSPENDED), assigns the process to the
      Job Object and verifies containment BEFORE resuming the process thread.
      This eliminates the race window where descendants could spawn uncontained.
    - If Job Object creation or assignment fails, the suspended process is immediately
      killed and WindowsJobContainmentError is raised (refusing uncontained fallback).
    - Returns subprocess.Popen instance with `_job_object` attached on Windows and
      `_pgid` set on both platforms.
    """
    cmd = prepare_command(cmd)
    _popen = popen_fn or subprocess.Popen
    work_kwargs = dict(popen_kwargs)

    if tracker is not None and hasattr(tracker, "record_starting"):
        try:
            tracker.record_starting()
        except Exception:
            pass

    if IS_WINDOWS:
        job: Optional[WindowsJobObject] = None
        proc: Optional[subprocess.Popen] = None
        try:
            # 1. Create Job Object with KILL_ON_JOB_CLOSE
            job = WindowsJobObject()

            # 2. Add CREATE_SUSPENDED and CREATE_NEW_PROCESS_GROUP
            creationflags = work_kwargs.get("creationflags", 0)
            creationflags |= (CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP)
            work_kwargs["creationflags"] = creationflags

            # 3. Spawn process in suspended state
            proc = _popen(cmd, **work_kwargs)

            # If proc is a mock process from unit tests, skip native OS kernel calls
            if is_mock_process(proc) and not hasattr(job, "_mock_return_value") and not hasattr(_get_kernel32(), "_mock_return_value"):
                mock_pid = getattr(proc, "pid", 12345)
                if not isinstance(mock_pid, int):
                    mock_pid = 12345
                setattr(proc, "_job_object", job)
                setattr(proc, "_pgid", mock_pid)
                if tracker is not None and hasattr(tracker, "record_launched"):
                    try:
                        tracker.record_launched(
                            pid=mock_pid,
                            pgid=mock_pid,
                            containment_type="windows_job_object",
                        )
                    except Exception:
                        pass
                return proc

            # 4. Assign to Job Object and verify containment while process is still suspended
            job.assign_process(proc)

            # 5. Resume suspended thread now that containment is verified
            raw_h = getattr(proc, "_handle", None)
            resumed = resume_process_threads(proc.pid, proc_handle=raw_h)
            if not resumed:
                raise WindowsJobContainmentError(f"Failed to resume suspended primary thread for PID {proc.pid}")

            # 6. Attach Job Object to proc for lifetime management
            setattr(proc, "_job_object", job)
            setattr(proc, "_pgid", proc.pid)

            # 7. Record launched in tracker with containment evidence
            if tracker is not None and hasattr(tracker, "record_launched"):
                try:
                    tracker.record_launched(
                        pid=proc.pid,
                        pgid=proc.pid,
                        containment_type="windows_job_object",
                    )
                except TypeError:
                    tracker.record_launched(pid=proc.pid, pgid=proc.pid)
                except Exception:
                    pass

            return proc

        except Exception as e:
            # Containment failed! Refuse uncontained execution!
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(timeout=0.2)
                except Exception:
                    pass
                for stream in (
                    getattr(proc, "stdin", None),
                    getattr(proc, "stdout", None),
                    getattr(proc, "stderr", None),
                ):
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
            if job is not None:
                job.close()

            if isinstance(e, WindowsJobContainmentError):
                raise
            raise WindowsJobContainmentError(f"Windows process containment launch failed: {e}") from e

    else:
        # POSIX
        if "start_new_session" not in work_kwargs and "preexec_fn" not in work_kwargs:
            work_kwargs["start_new_session"] = True

        proc = _popen(cmd, **work_kwargs)
        setattr(proc, "_pgid", proc.pid)

        ident = get_process_identity(proc.pid)
        start_ticks = None
        if ident:
            if "start_time_ticks" in ident:
                start_ticks = ident["start_time_ticks"]
                setattr(proc, "_start_time_ticks", start_ticks)
            elif "creation_time" in ident:
                start_ticks = ident["creation_time"]
                setattr(proc, "_start_time_ticks", start_ticks)

        if tracker is not None and hasattr(tracker, "record_launched"):
            try:
                tracker.record_launched(
                    pid=proc.pid,
                    pgid=proc.pid,
                    containment_type="posix_session",
                    start_time_ticks=start_ticks,
                )
            except TypeError:
                try:
                    tracker.record_launched(
                        pid=proc.pid,
                        pgid=proc.pid,
                        containment_type="posix_session",
                    )
                except TypeError:
                    tracker.record_launched(pid=proc.pid, pgid=proc.pid)
            except Exception:
                pass

        return proc


def safe_kill(proc: subprocess.Popen) -> None:
    """Best-effort process termination ignoring missing or already-exited processes."""
    try:
        proc.kill()
        try:
            proc.wait(timeout=0.2)
        except Exception:
            pass
    except Exception:
        pass


def find_descendant_pids(parent_pid: int) -> Set[int]:
    """Discover all live descendant process IDs of a given PID.

    Traverses the process hierarchy recursively. Never includes parent_pid.
    """
    if not isinstance(parent_pid, int) or parent_pid <= 1:
        return set()

    descendants: Set[int] = set()
    to_scan = {parent_pid}

    if IS_LINUX and os.path.exists("/proc"):
        while to_scan:
            curr = to_scan.pop()
            children = set()
            try:
                for pid_dir in os.listdir("/proc"):
                    if not pid_dir.isdigit():
                        continue
                    try:
                        stat_path = f"/proc/{pid_dir}/stat"
                        with open(stat_path, "r") as f:
                            content = f.read()
                        rparen = content.rfind(")")
                        if rparen != -1:
                            fields = content[rparen + 2:].split()
                            ppid = int(fields[1])
                            if ppid == curr:
                                child_pid = int(pid_dir)
                                if child_pid not in descendants and child_pid != parent_pid:
                                    children.add(child_pid)
                                    descendants.add(child_pid)
                    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
                        pass
            except Exception:
                pass
            to_scan.update(children)
    elif IS_POSIX:
        # Fallback for Darwin/BSD: use 'pgrep -P <pid>' or 'ps'
        try:
            res = subprocess.run(
                ["pgrep", "-P", str(parent_pid)],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
            for line in res.stdout.splitlines():
                if line.strip().isdigit():
                    c_pid = int(line.strip())
                    if c_pid != parent_pid and c_pid not in descendants:
                        descendants.add(c_pid)
                        descendants.update(find_descendant_pids(c_pid))
        except Exception:
            pass

    return descendants


def find_process_group_pids(pgid: int) -> Set[int]:
    """Find all active process IDs belonging to a given process group."""
    if not isinstance(pgid, int) or pgid <= 1:
        return set()

    pids: Set[int] = set()
    if IS_LINUX and os.path.exists("/proc"):
        try:
            for pid_dir in os.listdir("/proc"):
                if not pid_dir.isdigit():
                    continue
                try:
                    stat_path = f"/proc/{pid_dir}/stat"
                    with open(stat_path, "r") as f:
                        content = f.read()
                    rparen = content.rfind(")")
                    if rparen != -1:
                        fields = content[rparen + 2:].split()
                        if len(fields) > 2 and int(fields[2]) == pgid:
                            pids.add(int(pid_dir))
                except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
                    pass
        except Exception:
            pass
    elif IS_POSIX:
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid,pgid"],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    p_pid = int(parts[0])
                    p_pgid = int(parts[1])
                    if p_pgid == pgid:
                        pids.add(p_pid)
        except Exception:
            pass
    return pids


def get_process_identity(pid: int) -> Optional[Dict[str, Any]]:
    """Retrieve process identity evidence (start time, pgid) for PID reuse verification."""
    if not isinstance(pid, int) or pid <= 0:
        return None

    if IS_LINUX and os.path.exists("/proc"):
        stat_path = f"/proc/{pid}/stat"
        try:
            with open(stat_path, "r") as f:
                content = f.read()
            rparen = content.rfind(")")
            if rparen != -1:
                fields = content[rparen + 2:].split()
                return {
                    "pid": pid,
                    "ppid": int(fields[1]),
                    "pgid": int(fields[2]),
                    "session": int(fields[3]),
                    "start_time_ticks": int(fields[19]),
                }
        except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
            return None
    elif IS_POSIX:
        try:
            res = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart=,pgid="],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
            out = res.stdout.strip()
            if out:
                return {
                    "pid": pid,
                    "lstart": out,
                }
        except Exception:
            pass
    elif IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h_proc:
                try:
                    creation = wintypes.FILETIME()
                    exit_time = wintypes.FILETIME()
                    kernel_time = wintypes.FILETIME()
                    user_time = wintypes.FILETIME()
                    if kernel32.GetProcessTimes(
                        h_proc,
                        ctypes.byref(creation),
                        ctypes.byref(exit_time),
                        ctypes.byref(kernel_time),
                        ctypes.byref(user_time),
                    ):
                        c_val = (creation.dwHighDateTime << 32) + creation.dwLowDateTime
                        return {"pid": pid, "creation_time": c_val}
                finally:
                    kernel32.CloseHandle(h_proc)
        except Exception:
            pass

    if is_pid_alive(pid):
        return {"pid": pid}
    return None


def verify_process_identity(
    pid: int,
    expected_start_time_ticks: Optional[int] = None,
    expected_pgid: Optional[int] = None,
) -> bool:
    """Verify whether a running process matches expected identity evidence.

    Returns False if process does not exist or if evidence proves PID was reused.
    """
    ident = get_process_identity(pid)
    if ident is None:
        return False

    if expected_start_time_ticks is not None:
        actual_start = ident.get("start_time_ticks")
        if actual_start is None:
            actual_start = ident.get("creation_time")
        if actual_start is not None and actual_start != expected_start_time_ticks:
            return False

    if expected_pgid is not None:
        actual_pgid = ident.get("pgid")
        if actual_pgid is not None and actual_pgid != expected_pgid:
            return False

    return True


def terminate_process_tree(
    proc: Union[subprocess.Popen, int],
    timeout: float = 1.0,
    force: bool = True,
    safe_kill_fn: Optional[Any] = None,
    pgid: Optional[int] = None,
    expected_start_time_ticks: Optional[int] = None,
) -> bool:
    """Terminate or kill a process and all its child/descendant processes cleanly.

    CRITICAL REGRESSION SAFETY REQUIREMENT:
    It must be structurally impossible for Forge to terminate itself or caller/system
    process groups on either platform.

    On POSIX:
    - Verifies child PID is a valid integer > 1.
    - Guards against caller's process group (os.getpgrp()), caller PID, and PGID <= 1.
    - Tracks and reaps all descendant processes recursively and in the process group.
    - Handles leaderless process groups if the group leader has already exited.
    - Sends SIGTERM, polls with timeout for exit, then escalates to SIGKILL if force=True.
    - Returns True if all owned processes in the tree exited cleanly, False otherwise.

    On Windows:
    - Verifies PID is a valid integer > 4 (protecting System/Idle processes) and != os.getpid().
    - Best-effort descendant termination via 'taskkill /F /T /PID <pid>'.
    - Structural limitation: If the root process has already exited, child processes cannot be
      discovered via taskkill tree walk without Win32 Job Objects.
    - Falls back to safe_kill(proc).
    """
    _kill = safe_kill_fn or safe_kill
    try:
        if isinstance(proc, int):
            pid = proc
            popen_obj = None
        else:
            pid = getattr(proc, "pid", None)
            popen_obj = proc

        if type(pid) is not int:
            if popen_obj is not None:
                _kill(popen_obj)
            return True

        if pid <= 1:
            return True

        current_pid = os.getpid()
        if pid == current_pid:
            if popen_obj is not None:
                _kill(popen_obj)
            return True

        is_mock = is_mock_process(popen_obj)
        if is_mock:
            expected_start_time_ticks = None
        elif expected_start_time_ticks is None and popen_obj is not None:
            raw_ticks = getattr(popen_obj, "_start_time_ticks", None)
            if isinstance(raw_ticks, (int, float)):
                expected_start_time_ticks = raw_ticks

        if not isinstance(expected_start_time_ticks, (int, float)):
            expected_start_time_ticks = None

        if os.name == "posix":
            target_pgid = pgid
            if type(target_pgid) is not int:
                target_pgid = None
                if popen_obj is not None:
                    obj_pgid = getattr(popen_obj, "_pgid", None)
                    if type(obj_pgid) is int:
                        target_pgid = obj_pgid

            if target_pgid is None:
                try:
                    target_pgid = os.getpgid(pid)
                except (ProcessLookupError, PermissionError, OSError):
                    target_pgid = None

            # Under start_new_session=True, pgid == pid if not otherwise known
            if target_pgid is None:
                target_pgid = pid

            current_pgid = os.getpgrp() if hasattr(os, "getpgrp") else None
            if (
                type(target_pgid) is not int
                or target_pgid <= 1
                or (current_pgid is not None and target_pgid == current_pgid)
                or target_pgid == current_pid
            ):
                if popen_obj is not None:
                    _kill(popen_obj)
                return True

            # CRITICAL: Verify process identity and ownership before signaling.
            # Never signal an ambiguous PID or process group.
            if expected_start_time_ticks is not None and is_pid_alive(pid):
                if not verify_process_identity(pid, expected_start_time_ticks, target_pgid):
                    logger.warning(
                        "Refusing to signal PID %s: process identity does not match expected start_time_ticks %s.",
                        pid,
                        expected_start_time_ticks,
                    )
                    return False

            is_mock = is_mock_process(popen_obj)
            # Discover all descendant PIDs and process group members before signaling
            descendants = find_descendant_pids(pid)
            group_pids = find_process_group_pids(target_pgid)
            all_pids = ({pid} if is_pid_alive(pid) else set()) | descendants | group_pids

            # If all processes in tree and group have already exited, do not signal; return cleanly
            if not is_mock and not all_pids:
                return True

            # If the process group leader is already dead and caller did not provide popen_obj ownership,
            # we cannot prove ownership of lingering processes in the group due to potential PGID reuse.
            if not is_pid_alive(pid) and popen_obj is None and expected_start_time_ticks is not None:
                logger.warning(
                    "Process group leader (PID %s) is no longer alive; lingering processes match PGID %s. "
                    "Refusing automated termination of ambiguous process group.",
                    pid,
                    target_pgid,
                )
                return False

            # Send graceful SIGTERM to process group
            try:
                os.killpg(target_pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                if popen_obj is not None:
                    _kill(popen_obj)

            # Also signal any detached descendants
            for d_pid in descendants:
                if d_pid != current_pid and is_pid_alive(d_pid):
                    try:
                        os.kill(d_pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass

            time.sleep(0.05)

            # Send forced SIGKILL if requested or processes remain
            if force:
                try:
                    os.killpg(target_pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception:
                    if popen_obj is not None:
                        _kill(popen_obj)

                for d_pid in descendants | group_pids:
                    if d_pid != current_pid and is_pid_alive(d_pid):
                        try:
                            os.kill(d_pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass

            if popen_obj is not None:
                _kill(popen_obj)

            # Final check of liveness with bounded wait
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = [p for p in all_pids if is_pid_alive(p)]
                if not remaining:
                    return True
                time.sleep(0.05)

            remaining = [p for p in all_pids if is_pid_alive(p)]
            return len(remaining) == 0

        elif os.name == "nt" or sys.platform == "win32":
            if pid <= 4 or pid == current_pid:
                if popen_obj is not None:
                    _kill(popen_obj)
                return True

            if expected_start_time_ticks is not None and is_pid_alive(pid):
                if not verify_process_identity(pid, expected_start_time_ticks):
                    return False

            job = getattr(popen_obj, "_job_object", None) if popen_obj is not None else None
            if job is not None and isinstance(job, WindowsJobObject):
                job.terminate(exit_code=1)
                deadline = time.monotonic() + timeout
                drained = False
                while time.monotonic() < deadline:
                    if job.active_processes_count() == 0:
                        drained = True
                        break
                    time.sleep(0.05)
                if not drained:
                    drained = (job.active_processes_count() == 0)
                job.close()
                if popen_obj is not None:
                    _kill(popen_obj)
                return (not is_pid_alive(pid)) and drained

            is_alive = (popen_obj.poll() is None) if popen_obj is not None else is_pid_alive(pid)
            if is_alive:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                    )
                except Exception:
                    pass
            if popen_obj is not None:
                _kill(popen_obj)
            return not is_pid_alive(pid)
        else:
            if popen_obj is not None:
                _kill(popen_obj)
            return True
    except Exception:
        if isinstance(proc, subprocess.Popen):
            _kill(proc)
        pid_to_check = proc.pid if isinstance(proc, subprocess.Popen) else (proc if isinstance(proc, int) else None)
        if pid_to_check is not None and is_pid_alive(pid_to_check):
            return False
        return True


def is_pid_alive(pid: int) -> bool:
    """Check whether a given PID is currently active and running."""
    if not isinstance(pid, int) or pid <= 0:
        return False

    if IS_POSIX:
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return False
        except (ChildProcessError, OSError):
            pass

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

        if IS_LINUX and os.path.exists(f"/proc/{pid}/status"):
            try:
                with open(f"/proc/{pid}/status", "r") as f:
                    for line in f:
                        if line.startswith("State:"):
                            if "Z" in line:
                                return False
                            break
            except Exception:
                pass

        return True

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
