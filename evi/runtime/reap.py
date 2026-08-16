"""Make the managed llama-server die with eVi even on a HARD kill — the desktop
"sidecar owns/reaps the process" guarantee (Phase 4).

`atexit` (in llama_server.py) covers a clean exit. But the desktop shell may
`TerminateProcess` the sidecar (`evi-server`) on quit, which skips atexit and
would orphan `llama-server` (holding VRAM/RAM + the port). On **Windows** we tie
the child to a kill-on-close **Job Object** owned by this process: when eVi's
process terminates — however it dies — the OS closes the job and kills its
members. (macOS/Linux rely on atexit; a force-SIGKILL there can still orphan, but
the next launch reuses the live server via the port check.)

Job Objects are process-scoped (not thread-scoped), so this is correct even
though the server is spawned from a background thread.
"""

from __future__ import annotations

import platform

_job_handle = None  # kept alive for this process's lifetime (closed only on exit)


def assign_to_parent_lifetime(pid: int) -> bool:
    """Tie the child process `pid` to THIS process's lifetime via a kill-on-close
    Windows Job Object. Returns True if assigned; a best-effort no-op (False) off
    Windows or on any failure — reaping is a safety net, never load-bearing."""
    if platform.system().lower() != "windows":
        return False
    global _job_handle
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo", _IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        PROCESS_TERMINATE = 0x0001
        PROCESS_SET_QUOTA = 0x0100

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE

        if _job_handle is None:
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                return False
            info = _EXT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                handle, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info),
            ):
                k32.CloseHandle(handle)
                return False
            _job_handle = handle  # never closed explicitly — dies with the process

        hproc = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, pid)
        if not hproc:
            return False
        ok = bool(k32.AssignProcessToJobObject(_job_handle, hproc))
        k32.CloseHandle(hproc)
        return ok
    except Exception:  # noqa: BLE001 — best-effort; never break server start
        return False
