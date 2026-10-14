"""Process helpers for children eVi may have to stop.

``kill_tree(proc)`` kills a spawned process AND every descendant. Killing only
the direct child is not enough: on Windows an npm-installed CLI is a ``.cmd``
shim, so the child is ``cmd.exe`` and the real work happens in a ``node``
grandchild that survives it, keeps the stdout/stderr pipes open and can hold a
login or a tool run going. A POSIX shell wrapper has the same shape.

``spawn(argv, **popen_kwargs)`` starts a child as the root of a tree that
``kill_tree`` can kill as a unit even after the child itself has exited: on
Windows the child goes into its own Job Object, on POSIX it leads its own
session (process group). Once the child is gone, a process it left behind (an
MCP server or helper still holding the stdout/stderr pipes) can no longer be
found by walking down from it, but it is still in the job / group. On Windows
the child is created suspended and only resumed once it is in the job, so
nothing it starts can be created outside it, however long eVi takes to get
there (a first ``import ctypes``, another busy thread holding the GIL).
"""

from __future__ import annotations

import atexit
import functools
import os
import signal
import subprocess
import sys
import threading
import weakref

_WAIT = 3.0  # seconds to wait for killed processes to be reaped
_POPEN = subprocess.Popen  # the real class; a test may patch subprocess.Popen

# Windows job settings. BREAKAWAY_OK lets a process that explicitly asks to
# leave the job (CREATE_BREAKAWAY_FROM_JOB) do so; without it that
# CreateProcess would fail. Everything else the child starts stays in the job.
_JOB_BASIC_LIMITS = 2  # JobObjectBasicLimitInformation
_JOB_LIMIT_BREAKAWAY_OK = 0x00000800
_CREATE_SUSPENDED = 0x00000004

# Children spawn() started, so any still running at interpreter exit are
# killed: on POSIX they lead their own session, out of reach of the terminal's
# Ctrl-C, and must not outlive eVi.
_live: weakref.WeakSet = weakref.WeakSet()
_live_lock = threading.Lock()


def spawn(argv, **kwargs):
    """``subprocess.Popen(argv, **kwargs)`` started as the root of a killable
    tree (see the module docstring). On Windows the child is created
    suspended, put in its job, then resumed; if it can't be resumed it is
    killed and ``OSError`` is raised. ``subprocess.Popen`` is looked up at call
    time, so a test that patches it gets its stand-in back, untouched."""
    popen = subprocess.Popen
    # Suspend only under the real Popen: a test stand-in that wraps it might
    # not hand the Popen back, and then nothing would resume the child.
    suspend = (sys.platform == "win32" and popen is _POPEN
               and not kwargs.get("creationflags", 0) & _CREATE_SUSPENDED)
    if suspend:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_SUSPENDED
    elif sys.platform != "win32":
        kwargs.setdefault("start_new_session", True)
    proc = popen(argv, **kwargs)
    if not isinstance(proc, _POPEN):
        return proc  # a test stand-in: no job, no group, not tracked
    if sys.platform == "win32":
        job = None
        try:
            job = _new_job(proc)
            if suspend and not _resume(proc):
                raise OSError(f"could not resume new process {proc.pid} after "
                              "creating it suspended")
        except BaseException:
            # Never hand back (or leave behind) a child that is still suspended.
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            if job:
                _close_handle(job)
            raise
        if job:
            proc._evi_job = job
            # Closed with the Popen. Not at exit: _kill_running_at_exit may
            # still need it, and the OS closes it when eVi exits anyway.
            weakref.finalize(proc, _close_handle, job).atexit = False
    elif kwargs.get("start_new_session"):
        proc._evi_pgid = proc.pid
    with _live_lock:
        _live.add(proc)
    return proc


def kill_tree(proc, *, wait: float = _WAIT) -> None:
    """Best-effort kill of `proc` (a ``subprocess.Popen``) and all its
    descendants. Never raises.

    Descendants are listed BEFORE anything is killed: once the parent is gone,
    POSIX re-parents its children and Windows leaves them pointing at a dead
    parent id, so they can no longer be found from it. That walk skips a
    process that has already exited, since its id may belong to someone else
    now. The job / process group :func:`spawn` made is eVi's own, so it is
    killed either way, which also reaches what an exited child left behind.
    """
    pid = getattr(proc, "pid", None)
    try:
        exited = proc.poll() is not None
    except Exception:  # noqa: BLE001
        exited = False
    if isinstance(pid, int) and pid > 0 and not exited:
        if not _kill_with_psutil(pid, wait):
            _kill_fallback(pid)
    _kill_job(getattr(proc, "_evi_job", None))
    _kill_pgid(getattr(proc, "_evi_pgid", None))
    try:
        proc.kill()  # keep Popen's own bookkeeping in step; a no-op once dead
    except Exception:  # noqa: BLE001
        pass


def _kill_with_psutil(pid: int, wait: float) -> bool:
    """Kill `pid` and its descendants with psutil (a core dependency). False
    when psutil is unusable, so the caller falls back to OS tools."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return False
    try:
        parent = psutil.Process(pid)
        victims = [parent, *parent.children(recursive=True)]
    except psutil.NoSuchProcess:
        return True
    except Exception:  # noqa: BLE001
        return False
    # Parent first, so it can't start anything new while its children go.
    for p in victims:
        try:
            p.kill()
        except Exception:  # noqa: BLE001 — already gone / access denied
            pass
    try:
        psutil.wait_procs(victims, timeout=wait)
    except Exception:  # noqa: BLE001
        pass
    _kill_group(pid)
    return True


def _kill_group(pid: int) -> None:
    """POSIX: also kill the child's process group when the child leads its own
    group (``start_new_session=True``), which catches re-parented stragglers.
    Never touches eVi's own group."""
    if sys.platform == "win32" or not hasattr(os, "killpg"):
        return
    try:
        pgid = os.getpgid(pid)
    except Exception:  # noqa: BLE001 — already reaped
        return
    if pgid == pid and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass


def _kill_fallback(pid: int) -> None:
    """Without psutil: ``taskkill /T`` on Windows, the process group on POSIX."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10,
            )
        except Exception:  # noqa: BLE001
            pass
        return
    _kill_group(pid)


def _kill_pgid(pgid) -> None:
    """POSIX: kill the process group :func:`spawn` started, even once its
    leader has exited. While any member is alive the group id can't be reused,
    so this only reaches the child's own leftovers."""
    if sys.platform == "win32" or not isinstance(pgid, int) or pgid <= 0:
        return
    try:
        if pgid != os.getpgrp():
            os.killpg(pgid, signal.SIGKILL)
    except Exception:  # noqa: BLE001 — the group is already empty
        pass


# --- Windows Job Objects -----------------------------------------------------


@functools.lru_cache(maxsize=1)
def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # a private copy: own argtypes
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k32


def _new_job(proc):
    """Put `proc` in a fresh Job Object and return the job handle, or None when
    that fails (kill_tree's psutil walk still covers live descendants).
    :func:`spawn` calls this while the child is still suspended, so everything
    it later starts lands in the job too (unless it explicitly breaks away)."""
    handle = getattr(proc, "_handle", None)
    if handle is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _BasicLimits(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
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

        k32 = _kernel32()
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = _BasicLimits(LimitFlags=_JOB_LIMIT_BREAKAWAY_OK)
        k32.SetInformationJobObject(job, _JOB_BASIC_LIMITS, ctypes.byref(limits),
                                    ctypes.sizeof(limits))
        if not k32.AssignProcessToJobObject(job, int(handle)):
            k32.CloseHandle(job)
            return None
        return job
    except Exception:  # noqa: BLE001
        return None


@functools.lru_cache(maxsize=1)
def _nt_resume_process():
    import ctypes
    from ctypes import wintypes

    fn = ctypes.WinDLL("ntdll").NtResumeProcess  # what psutil's resume() calls
    fn.restype = ctypes.c_long  # NTSTATUS
    fn.argtypes = (wintypes.HANDLE,)
    return fn


def _resume(proc) -> bool:
    """Resume a child :func:`spawn` created suspended, through the Popen's own
    process handle (psutil by pid as the fallback; the open handle keeps the
    pid from being reused). False when it couldn't be resumed."""
    try:
        if _nt_resume_process()(int(proc._handle)) >= 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        import psutil

        psutil.Process(proc.pid).resume()
        return True
    except Exception:  # noqa: BLE001
        return False


def _kill_job(job) -> None:
    if not job or sys.platform != "win32":
        return
    try:
        _kernel32().TerminateJobObject(job, 1)
    except Exception:  # noqa: BLE001
        pass


def _close_handle(handle) -> None:
    try:
        _kernel32().CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass


@atexit.register
def _kill_running_at_exit() -> None:
    """Kill every :func:`spawn` child still running when eVi exits."""
    with _live_lock:
        procs = list(_live)
    for p in procs:
        try:
            running = p.poll() is None
        except Exception:  # noqa: BLE001
            running = False
        if running:
            kill_tree(p, wait=0.5)
