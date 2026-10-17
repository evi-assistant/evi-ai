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

``run(argv, ...)`` runs a command to completion the way an agent's tools need
it (run_command, run_python, monitor): started by ``spawn``, its output read by
threads, and its whole tree killed at a timeout, on a cancel or when an
``until`` line shows up, with the output read so far coming back.
``subprocess.run(timeout=)`` kills only the direct child and, on Windows, then
waits for pipes a grandchild still holds, which can hang a turn for good.

A spawned child is out of reach of the console's Ctrl+C (its own process group
or session), so eVi stops the ones still running when it exits, before the
interpreter waits for the threads that run them (`_stop_at_exit`).

Every process ``spawn`` starts (and so ``run``: the agent's run_command,
run_python and monitor, the delegate CLI-agent drivers) runs for an agent and
gets ``EVI_AGENT=1`` in its environment (`agent_env`), on top of whatever
environment the caller passed: director tooling (`evi.attest`) refuses to run,
and never reads the director key, in a process that has it, nor in anything
that process starts.
"""

from __future__ import annotations

import atexit
import codecs
# Imported before _stop_at_exit is registered, so that it runs before this
# module's exit hook, which waits for every tool pool thread.
import concurrent.futures.thread  # noqa: F401
import contextlib
import functools
import locale
import os
import signal
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable

_WAIT = 3.0  # seconds to wait for killed processes to be reaped
_POPEN = subprocess.Popen  # the real class; a test may patch subprocess.Popen

# run(): how often it checks on the child, how long it keeps reading once the
# child has exited (something it left in the background may hold the pipes
# for good), and how long it waits for the pipes to close after a kill.
_RUN_POLL = 0.05
_EXIT_GRACE = 2.0
_KILL_GRACE = 2.0
# Windows: a child in its own process group gets no CTRL_C_EVENT when the
# user presses Ctrl+C in eVi's console; eVi decides what Ctrl+C stops.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
# Environment variables a command run unattended or sandboxed doesn't get
# (`scrubbed_env`): API keys, access tokens, client secrets.
_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET")
# Set in the environment of every process spawn() starts (`agent_env`); the
# name evi.attest.AGENT_ENV reads (not imported: attest is a director module).
AGENT_ENV = "EVI_AGENT"

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
# Set once eVi is exiting (`_stop_at_exit`): run() stops what it runs.
_exiting = threading.Event()


def agent_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of `env` (default: eVi's environment) with ``EVI_AGENT=1``:
    the environment of a process run for an agent (see the module doc)."""
    out = dict(os.environ if env is None else env)
    out[AGENT_ENV] = "1"
    return out


def spawn(argv, **kwargs):
    """``subprocess.Popen(argv, **kwargs)`` started as the root of a killable
    tree (see the module docstring), with ``EVI_AGENT=1`` added to its
    environment (`agent_env`: the `env` passed, else eVi's own). On Windows
    the child is created suspended, put in its job, then resumed; if it can't
    be resumed it is killed and ``OSError`` is raised. ``subprocess.Popen`` is
    looked up at call time, so a test that patches it gets its stand-in back,
    untouched."""
    kwargs["env"] = agent_env(kwargs.get("env"))
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


# --- running a command to completion ------------------------------------------


@dataclass
class RunResult:
    """What :func:`run` saw. ``stdout`` holds stderr too when it was merged.

    At most one of ``timed_out``, ``cancelled`` and ``matched`` is set: why
    run() stopped early. ``killed`` says whether it then killed the tree (a
    line can match after the command has exited, which needs no kill).
    ``detached``: the command exited, but something it started in the
    background still held its output; run() stopped reading and left that
    running (a server it launched, a build daemon)."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    matched: bool = False
    killed: bool = False
    detached: bool = False

    @property
    def output(self) -> str:
        """stdout then stderr, as a tool shows them."""
        return self.stdout + self.stderr


def scrubbed_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of `env` (default: eVi's environment) without the variables whose
    names end in _KEY, _TOKEN or _SECRET, in any case: what a command run for
    an unattended or sandboxed agent has no business reading."""
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if not k.upper().endswith(_SECRET_SUFFIXES)}


def runs_unattended(ctx: Any) -> bool:
    """True when `ctx` (a tool call's ToolContext, or None) belongs to a run
    nobody watches:

    - its agent has no permission callback (a scheduled task, `evi run`, a
      subagent of an unattended run);
    - its agent serves a remote trigger (``remote_trigger``: a channel push,
      federation, A2A, a routine), which may keep a callback that refuses
      every call (`evi.llm.agent.remote_unattended`);
    - it runs inside an unattended entry point
      (`evi.llm.cli_agent.unattended`, which tool threads see too).

    A call made outside any agent (ctx None or without one) is not."""
    agent = getattr(ctx, "agent", None)
    if agent is None:
        return False
    if getattr(agent, "remote_trigger", False) is True:
        return True
    if (getattr(agent, "permission_callback", None) is None
            and getattr(agent, "permission_batch_callback", None) is None):
        return True
    from evi.llm.cli_agent import current_unattended  # it imports this module

    return current_unattended() is not None


def _decode(data: bytes, encoding: str) -> str:
    """Bytes as text the way ``text=True`` reads them (universal newlines),
    but a byte the encoding can't read never fails the call."""
    text = data.decode(encoding, errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def run(
    argv: Any,
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    cancel_token: threading.Event | None = None,
    shell: bool = False,
    env: dict[str, str] | None = None,
    input: str | bytes | None = None,  # subprocess.run's name for it
    merge_stderr: bool = False,
    until: Callable[[str], Any] | None = None,
    encoding: str | None = None,
    exit_grace: float = _EXIT_GRACE,
) -> RunResult:
    """Run `argv` to completion as the root of a killable tree (:func:`spawn`)
    and return what it wrote. Raises OSError when it can't be started; never
    for a timeout or a cancel.

    - Threads read stdout and stderr (merged into stdout with `merge_stderr`),
      so a full pipe can't stall the child and the wait itself stays bounded.
    - The whole tree is killed (:func:`kill_tree`: the Job Object / process
      group reaches what an exited child left behind) after `timeout` seconds,
      as soon as `cancel_token` is set (or eVi is exiting: ``cancelled`` too),
      or once `until` returns true for a line of stdout (without its line
      break). Whatever was read until then comes back, partial.
    - Once the child itself has exited, the pipes are read for at most
      `exit_grace` more seconds (or until `timeout`): a process it started in
      the background may hold them open for good. That returns ``detached``
      and is left running; what it writes later is read and dropped, so it
      never blocks on a full pipe.

    stdin is the null device unless `input` is given: a command run for an
    agent can't wait on the terminal. Its environment is `env` (default:
    eVi's) plus ``EVI_AGENT=1`` (:func:`spawn`). Text is decoded with
    `encoding` (default: the locale's, as ``text=True`` does), bad bytes
    replaced."""
    enc = encoding or locale.getpreferredencoding(False)
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "shell": shell,
        "stdin": subprocess.DEVNULL if input is None else subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT if merge_stderr else subprocess.PIPE,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP
    proc = spawn(argv, **kwargs)

    lock = threading.Lock()
    wake = threading.Event()     # set when output arrives or a pipe closes
    discard = threading.Event()  # set once nobody will read what comes next
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    def _drain(pipe, sink: list[bytes]) -> None:
        try:
            read = getattr(pipe, "read1", pipe.read)
            while True:
                data = read(65536)
                if not data:
                    break
                if not discard.is_set():
                    with lock:
                        sink.append(data)
                wake.set()
        except (OSError, ValueError):  # a pipe closed under us is just EOF
            pass
        finally:
            with contextlib.suppress(Exception):
                pipe.close()
            wake.set()

    readers = []
    for pipe, sink in ((proc.stdout, out_chunks), (proc.stderr, err_chunks)):
        if pipe is not None:
            t = threading.Thread(target=_drain, args=(pipe, sink), name="evi-proc-read",
                                 daemon=True)
            t.start()
            readers.append(t)
    if input is not None and proc.stdin is not None:
        data = input.encode("utf-8") if isinstance(input, str) else input

        def _feed() -> None:
            try:
                proc.stdin.write(data)
            except (OSError, ValueError):  # the child stopped reading
                pass
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.close()

        threading.Thread(target=_feed, name="evi-proc-write", daemon=True).start()

    # `until` sees whole lines of stdout, decoded as they arrive.
    decoder = codecs.getincrementaldecoder(enc)(errors="replace")
    partial = ""
    seen = 0

    def _matched() -> bool:
        nonlocal partial, seen
        with lock:
            fresh = out_chunks[seen:]
            seen = len(out_chunks)
        if not fresh:
            return False
        *lines, partial = (partial + decoder.decode(b"".join(fresh))).split("\n")
        return any(until(line.rstrip("\r")) for line in lines)

    result = RunResult(returncode=None)
    deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
    exited_at: float | None = None
    finished = False
    while True:
        wake.wait(_RUN_POLL)
        wake.clear()
        rc = proc.poll()
        finished = rc is not None and not any(t.is_alive() for t in readers)
        if until is not None and _matched():
            result.matched = True
            break
        if finished:
            break
        if (cancel_token is not None and cancel_token.is_set()) or _exiting.is_set():
            result.cancelled = True
            break
        now = time.monotonic()
        if rc is None:
            if deadline is not None and now >= deadline:
                result.timed_out = True
                break
            continue
        if exited_at is None:
            exited_at = now
        if now - exited_at >= exit_grace or (deadline is not None and now >= deadline):
            result.detached = True
            break
    if finished and until is not None and not result.matched and partial:
        result.matched = bool(until(partial.rstrip("\r")))  # a last line with no break

    if result.timed_out or result.cancelled or (result.matched and not finished):
        result.killed = True
        kill_tree(proc)
        # Every holder of the pipes is dead now, so the readers reach EOF
        # (unless something broke away from the job / group: then drop it).
        stop = time.monotonic() + _KILL_GRACE
        for t in readers:
            t.join(max(0.0, stop - time.monotonic()))
    discard.set()
    try:
        result.returncode = proc.wait(timeout=_WAIT)
    except subprocess.TimeoutExpired:
        result.returncode = proc.poll()
    with lock:
        out, err = b"".join(out_chunks), b"".join(err_chunks)
    result.stdout, result.stderr = _decode(out, enc), _decode(err, enc)
    return result


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


def _stop_at_exit() -> None:
    """eVi is exiting (Ctrl+C at `evi serve` or the scheduler daemon, a quit):
    stop every :func:`spawn` child still running, and make run() report
    ``cancelled`` and stop anything it starts from now on. The interpreter
    runs this before it waits for its non-daemon threads, a tool call's pool
    thread blocked in run() among them; `atexit` hooks only run after that
    wait, which would last until the command's own timeout. Never raises: that
    would skip the rest of the interpreter's shutdown."""
    _exiting.set()
    with contextlib.suppress(Exception):
        _kill_running_at_exit()


# Called in reverse order of registration: before concurrent.futures' own hook
# (registered when it was imported, above), which joins the pool threads.
with contextlib.suppress(AttributeError, RuntimeError):  # a private hook; shutdown begun
    threading._register_atexit(_stop_at_exit)
