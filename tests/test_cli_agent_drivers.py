"""CLI-agent drivers (Phase 0 fix-cli-agent-drivers).

* gemini, qwen and copilot get the conversation on stdin, never in argv. On
  Windows an npm-installed CLI is a ``.cmd`` shim that CreateProcess runs through
  ``cmd.exe`` (8191-char cap, a newline ends the command, ``&``/``|``/``%``/quotes
  are live), so argv must not carry it.
* gemini never runs ``--approval-mode yolo``.
* Every turn is bounded and a hung CLI is killed with its whole process tree
  (``evi.proc.kill_tree``).
* amp hints name ``@ampcode/cli`` and the ``ultra`` mode is selectable.
* A delegate CLI agent refuses an unattended entry point unless allowed.

The shim tests install a fake CLI the way npm does — a ``.cmd`` shim on Windows,
a sh wrapper elsewhere — whose Python body reports the argv and the stdin it got.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from evi.llm import cli_agent
from evi.llm import copilot_agent as co
from evi.llm import gemini_agent as gm
from evi.llm import qwen_agent as qw
from evi.llm.cli_agent import render_transcript
from evi.proc import kill_tree

# --- a fake CLI behind an npm-style shim ---------------------------------------

_FAKE_CLI = r'''
import hashlib, json, os, subprocess, sys, time
kind, args = sys.argv[1], sys.argv[2:]
if "--help" in args:
    print("Usage: fake [options]\n  -o, --output-format\n  --approval-mode\n"
          "  --no-ask-user\n  -p, --prompt\n  -m, --model")
    sys.exit(0)
if os.environ.get("FAKE_CLI_HANG"):
    # A hung CLI with a child of its own (node under a .cmd shim, say). It never
    # reads stdin, so a big prompt also leaves the parent's write blocked.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)",
                              os.environ["FAKE_CLI_TOKEN"]])
    with open(os.environ["FAKE_CLI_HANG"], "w") as f:
        f.write(f"{os.getpid()} {child.pid}")
    time.sleep(300)
data = sys.stdin.buffer.read()
report = json.dumps({"argv": args, "sha": hashlib.sha256(data).hexdigest(), "len": len(data)})
if kind == "gemini":
    out = json.dumps({"response": report, "stats": {}})
elif kind == "qwen":
    out = json.dumps([
        {"type": "assistant", "message": {"content": [{"type": "text", "text": report}]}},
        {"type": "result", "subtype": "success", "usage": {}},
    ])
else:
    out = report
sys.stdout.write(out)
'''


def _install_fake(tmp_path: Path, kind: str, monkeypatch) -> Path:
    """Put a fake `kind` CLI first on PATH, shaped like an npm global install.
    Returns the shim path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "fake_cli.py"
    script.write_text(_FAKE_CLI, encoding="utf-8")
    if sys.platform == "win32":
        shim = bin_dir / f"{kind}.cmd"
        shim.write_bytes(
            f'@ECHO off\r\n"{sys.executable}" "%~dp0fake_cli.py" {kind} %*\r\n'.encode()
        )
    else:
        shim = bin_dir / kind
        shim.write_text(f'#!/bin/sh\n"{sys.executable}" "{script}" {kind} "$@"\n',
                        encoding="utf-8")
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_CLI_TOKEN", f"evi-fake-cli-{os.getpid()}-{tmp_path.name}")
    monkeypatch.delenv("FAKE_CLI_HANG", raising=False)
    return shim


def _hostile_messages(canary: Path) -> list[dict]:
    """A > 8191-char conversation full of cmd.exe syntax. If any of it reached a
    command line, `echo PWNED>canary` would create the canary file."""
    line = (f'He said "hi" & echo PWNED>"{canary}" | type %PATH% ^& calc < x > y '
            "!bang! 100% done\nnext line\r\n")
    big = line * (9000 // len(line) + 1)
    return [
        {"role": "system", "content": "You are eVi. " + line},
        {"role": "user", "content": big + " café ✓ \U0001F600"},
    ]


_CLIENTS = {
    "gemini": (gm.GeminiAgentClient, "gemini-2.5-flash"),
    "qwen": (qw.QwenAgentClient, "qwen3-coder-plus"),
    "copilot": (co.CopilotAgentClient, "auto"),
}


def _run_shim_turn(kind: str, tmp_path: Path, monkeypatch) -> tuple[dict, list[dict], Path]:
    shim = _install_fake(tmp_path, kind, monkeypatch)
    canary = tmp_path / "pwned.txt"
    messages = _hostile_messages(canary)
    cls, model = _CLIENTS[kind]
    resp = cls().chat.completions.create(model=model, messages=messages, stream=False)
    report = json.loads(resp.choices[0].message.content)
    assert not canary.exists(), "conversation text was run as a command"
    return report, messages, shim


def _assert_transcript_on_stdin(kind: str, report: dict, messages: list[dict]) -> None:
    expected = render_transcript(messages).encode("utf-8")
    assert len(expected) > 8191
    assert report["len"] == len(expected)
    assert report["sha"] == hashlib.sha256(expected).hexdigest()   # exact bytes
    argv = report["argv"]
    assert not any("PWNED" in a or "\n" in a or "%PATH%" in a for a in argv)
    assert len(" ".join(argv)) < 300
    if kind == "copilot":
        assert "-p" not in argv and "--prompt" not in argv   # stdin is ignored with -p
    else:
        assert argv[argv.index("-p") + 1] == gm._HEADLESS_PROMPT


@pytest.mark.skipif(sys.platform != "win32", reason="the .cmd shim path is Windows-only")
@pytest.mark.parametrize("kind", ["gemini", "qwen", "copilot"])
def test_windows_cmd_shim_turn_with_hostile_long_transcript(kind, tmp_path, monkeypatch):
    report, messages, shim = _run_shim_turn(kind, tmp_path, monkeypatch)
    assert shim.suffix == ".cmd"   # the CLI really was reached through cmd.exe
    _assert_transcript_on_stdin(kind, report, messages)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX counterpart of the .cmd test")
@pytest.mark.parametrize("kind", ["gemini", "qwen", "copilot"])
def test_posix_shim_turn_with_hostile_long_transcript(kind, tmp_path, monkeypatch):
    report, messages, _shim = _run_shim_turn(kind, tmp_path, monkeypatch)
    _assert_transcript_on_stdin(kind, report, messages)


def test_gemini_shim_argv_has_default_approval_and_no_yolo(tmp_path, monkeypatch):
    report, _messages, _shim = _run_shim_turn("gemini", tmp_path, monkeypatch)
    argv = report["argv"]
    assert not any("yolo" in a.lower() for a in argv)
    i = argv.index("--approval-mode")
    assert argv[i + 1] == "default"


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _survivors(token: str) -> list[int]:
    found = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            if any(token in (part or "") for part in (p.info.get("cmdline") or [])):
                found.append(p.pid)
        except (psutil.Error, OSError):
            continue
    return found


def test_hung_gemini_is_killed_at_timeout_with_no_surviving_child(tmp_path, monkeypatch):
    _install_fake(tmp_path, "gemini", monkeypatch)
    pidfile = tmp_path / "pids.txt"
    monkeypatch.setenv("FAKE_CLI_HANG", str(pidfile))
    monkeypatch.setattr(gm, "_TURN_TIMEOUT", 4.0)
    client = gm.GeminiAgentClient()
    client._driver._flags = []   # skip the --help probe; this test is about the turn
    t0 = time.monotonic()
    with pytest.raises(gm.GeminiUnavailable, match="exceeded 4s"):
        client.chat.completions.create(   # a > pipe-buffer prompt the CLI never reads
            model="gemini-2.5-flash", messages=_hostile_messages(tmp_path / "c"), stream=False)
    assert time.monotonic() - t0 < 4.0 + 20.0
    script_pid, child_pid = (int(x) for x in pidfile.read_text().split())
    deadline = time.monotonic() + 5
    while (_alive(script_pid) or _alive(child_pid)) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(script_pid) and not _alive(child_pid)
    # Nothing from the tree survives: not the shim, the script, or its child.
    assert _survivors(os.environ["FAKE_CLI_TOKEN"]) == []
    assert _survivors(str(tmp_path / "bin")) == []


# --- argv / stdin, unit level (no subprocess) ----------------------------------


class _Recorder:
    """Stands in for cli_agent.run_cli and records what the driver passed."""

    def __init__(self, stdout: str):
        self.stdout = stdout
        self.calls: list[dict] = []

    def __call__(self, argv, *, input="", timeout=None):
        self.calls.append({"argv": list(argv), "input": input, "timeout": timeout})
        return SimpleNamespace(stdout=self.stdout, stderr="", returncode=0)


_REPLIES = {
    "gemini": json.dumps({"response": "ok", "stats": {}}),
    "qwen": json.dumps([{"type": "result", "subtype": "success", "result": "ok", "usage": {}}]),
    "copilot": "ok",
}
_MODULES = {"gemini": (gm, "_gemini_path"), "qwen": (qw, "_qwen_path"),
            "copilot": (co, "_copilot_path")}


def _client_with_recorder(kind, monkeypatch, help_text="--output-format --approval-mode"):
    mod, path_fn = _MODULES[kind]
    monkeypatch.setattr(mod, path_fn, lambda: kind)
    monkeypatch.setattr(cli_agent, "help_text", lambda _p, **_k: help_text)
    rec = _Recorder(_REPLIES[kind])
    monkeypatch.setattr(mod, "run_cli", rec)
    return _CLIENTS[kind][0](), rec


@pytest.mark.parametrize("kind", ["gemini", "qwen", "copilot"])
def test_argv_has_no_transcript_and_stdin_carries_it(kind, monkeypatch, tmp_path):
    client, rec = _client_with_recorder(kind, monkeypatch)
    messages = _hostile_messages(tmp_path / "c")
    resp = client.chat.completions.create(model=_CLIENTS[kind][1], messages=messages)
    assert resp.choices[0].message.content == "ok"
    (call,) = rec.calls
    assert call["input"] == render_transcript(messages)
    assert not any("PWNED" in a or "\n" in a for a in call["argv"])
    assert call["timeout"] == _MODULES[kind][0]._TURN_TIMEOUT


@pytest.mark.parametrize("help_text,expected", [
    ("--output-format --approval-mode", ["--approval-mode", "default"]),
    ("", ["--approval-mode", "default"]),           # probe failed: still pinned
    ("--output-format --yolo", []),                  # predates approval modes
])
def test_gemini_never_yolo(help_text, expected, monkeypatch):
    client, rec = _client_with_recorder("gemini", monkeypatch, help_text)
    client.chat.completions.create(model="gemini-2.5-pro",
                                   messages=[{"role": "user", "content": "hi"}])
    argv = rec.calls[0]["argv"]
    assert not any("yolo" in a.lower() for a in argv)
    assert ("--approval-mode" in argv) == bool(expected)
    if expected:
        assert argv[argv.index("--approval-mode"):][:2] == expected


@pytest.mark.parametrize("kind,cls", [("gemini", gm.GeminiUnavailable),
                                      ("qwen", qw.QwenUnavailable)])
def test_too_old_cli_gets_a_clear_error(kind, cls, monkeypatch):
    client, rec = _client_with_recorder(kind, monkeypatch, "Usage: x [options]  -p, --prompt")
    with pytest.raises(cls, match="too old.*@latest"):
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert rec.calls == []


def test_qwen_pins_default_approval_mode(monkeypatch):
    client, rec = _client_with_recorder("qwen", monkeypatch)
    client.chat.completions.create(model="qwen3-coder-plus",
                                   messages=[{"role": "user", "content": "hi"}])
    argv = rec.calls[0]["argv"]
    assert argv[argv.index("--approval-mode") + 1] == "default"


def test_copilot_no_ask_user_only_when_supported(monkeypatch):
    client, rec = _client_with_recorder("copilot", monkeypatch, "--output-format --no-ask-user")
    client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "hi"}])
    assert "--no-ask-user" in rec.calls[0]["argv"]
    assert "--allow-all-tools" not in rec.calls[0]["argv"]


@pytest.mark.parametrize("kind", ["gemini", "qwen", "copilot"])
def test_hostile_model_id_never_reaches_argv(kind, monkeypatch):
    client, rec = _client_with_recorder(kind, monkeypatch)
    with pytest.raises(ValueError, match="Refusing model"):
        client.chat.completions.create(model='x" & calc & "',
                                       messages=[{"role": "user", "content": "hi"}])
    assert rec.calls == []


def test_safe_arg_accepts_real_model_ids():
    for mid in ("gemini-2.5-pro", "qwen3-coder-plus", "claude-sonnet-4.5", "gpt-5",
                "models/gemini-2.5-flash", "llama3.1:8b", "org/model@v2"):
        assert cli_agent.safe_arg(mid) == mid
    for bad in ("", "-x", "a b", "a&b", "a|b", "%PATH%", 'a"b', "a^b", "a\nb", "x" * 201):
        with pytest.raises(ValueError):
            cli_agent.safe_arg(bad)


def test_help_text_is_probed_once_per_binary(tmp_path):
    exe = tmp_path / "cli"
    exe.write_text("x", encoding="utf-8")
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(stdout="--approval-mode", stderr="", returncode=0)

    assert "--approval-mode" in cli_agent.help_text(str(exe), run=run)
    assert "--approval-mode" in cli_agent.help_text(str(exe), run=run)
    assert calls == [[str(exe), "--help"]]


def test_help_text_failed_probe_is_empty(tmp_path):
    def run(argv, **kw):
        raise OSError("boom")

    assert cli_agent.help_text(str(tmp_path / "missing-cli"), run=run) == ""


# --- timeouts and the process tree ---------------------------------------------


_PARENT_WITH_CHILD = (
    "import subprocess, sys, time\n"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(c.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def test_kill_tree_kills_grandchildren():
    proc = subprocess.Popen([sys.executable, "-c", _PARENT_WITH_CHILD],
                            stdout=subprocess.PIPE, text=True)
    try:
        child_pid = int(proc.stdout.readline())
        assert _alive(child_pid)
        kill_tree(proc)
        proc.wait(timeout=10)
        deadline = time.monotonic() + 5
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(child_pid)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()


def test_kill_tree_leaves_an_exited_process_alone(monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)

    def _boom(*_a, **_k):
        raise AssertionError("must not look up a reaped pid")

    monkeypatch.setattr(psutil, "Process", _boom)
    kill_tree(proc)   # no raise, no psutil lookup


def test_run_cli_timeout_kills_tree_and_raises():
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        cli_agent.run_cli([sys.executable, "-c", _PARENT_WITH_CHILD], input="x" * 200_000,
                          timeout=1.5)
    assert time.monotonic() - t0 < 15


def test_run_cli_passes_exact_utf8_stdin():
    text = "a\nb\r\nc café \U0001F600"
    res = cli_agent.run_cli(
        [sys.executable, "-c",
         "import sys, hashlib; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"],
        input=text, timeout=60,
    )
    assert res.returncode == 0
    assert res.stdout.strip() == hashlib.sha256(text.encode("utf-8")).hexdigest()


# A CLI that answers and exits at once but leaves a helper (an MCP server, say)
# holding its stdout/stderr, so the pipes don't reach EOF when it exits. The
# helper's argv carries the pidfile path, so a test can find it to clean up.
_EXITS_LEAVING_A_HELPER = r'''
import json, subprocess, sys
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", sys.argv[1]],
                          stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)
with open(sys.argv[1], "w") as f:
    f.write(str(helper.pid))
sys.stdin.buffer.read()
kind, answer = sys.argv[2], "the real answer"
if kind == "gemini":
    lines = [{"response": answer, "stats": {}}]
elif kind == "codex":
    lines = [{"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
             {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}]
else:
    lines = [{"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}},
             {"type": "result", "subtype": "success", "usage": {}}]
sys.stdout.write("\n".join(json.dumps(x) for x in lines) + "\n")
sys.stdout.flush()
'''


def _slow_job_assignment(monkeypatch, pidfile: Path) -> list[bool]:
    """Make :func:`evi.proc.spawn` put a new child in its Job Object only once
    the helper the child starts has had every chance to exist (up to 1.5s).
    That is the window a first ``import ctypes`` or another thread holding the
    GIL opens in a real eVi; a child that runs during it starts its helper
    outside the job, where the tree kill can't reach it. Returns one flag per
    assignment: did the helper already exist? A no-op on POSIX (no jobs)."""
    from evi import proc as proc_mod

    real = proc_mod._new_job
    seen: list[bool] = []

    def slow(proc):
        deadline = time.monotonic() + 1.5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        seen.append(pidfile.exists())
        return real(proc)

    monkeypatch.setattr(proc_mod, "_new_job", slow)
    return seen


def _in_job(pid: int, job) -> bool:
    """Windows: is process `pid` in Job Object `job` (IsProcessInJob)?"""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE,
                                   ctypes.POINTER(wintypes.BOOL))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    assert handle, f"cannot open process {pid}"
    try:
        inside = wintypes.BOOL()
        assert k32.IsProcessInJob(handle, job, ctypes.byref(inside))
        return bool(inside.value)
    finally:
        k32.CloseHandle(handle)


def _kill_leftovers(token: str) -> None:
    for pid in _survivors(token):
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass


def _stream_turns():
    from evi.llm import amp_agent, codex_agent

    return {"gemini": gm.run_gemini_turn, "codex": codex_agent.run_codex_turn,
            "amp": amp_agent.run_amp_turn}


def _run_turn_bounded(run_turn, argv, *, timeout: float, bound: float):
    """Run one driver turn on a thread. Returns (returned, elapsed, errors,
    text); `returned` is False when the turn was still stuck after `bound`."""
    q: queue.Queue = queue.Queue()
    done = threading.Event()

    def go():
        try:
            run_turn(argv, "prompt", out=q, timeout=timeout)
        finally:
            done.set()

    t0 = time.monotonic()
    threading.Thread(target=go, daemon=True).start()
    returned = done.wait(bound)
    elapsed = time.monotonic() - t0
    items = list(q.queue)
    errors = [str(it[1]) for it in items if isinstance(it, tuple)]
    text = "".join(it.choices[0].delta.content or ""
                   for it in items if not isinstance(it, tuple) and it.choices)
    return returned, elapsed, errors, text


@pytest.mark.parametrize("kind", ["gemini", "codex", "amp"])
def test_cli_exiting_with_a_helper_on_its_pipes_keeps_its_answer(kind, tmp_path, monkeypatch):
    pidfile = tmp_path / "helper.pid"
    seen = _slow_job_assignment(monkeypatch, pidfile)
    try:
        returned, elapsed, errors, text = _run_turn_bounded(
            _stream_turns()[kind],
            [sys.executable, "-c", _EXITS_LEAVING_A_HELPER, str(pidfile), kind],
            timeout=30.0, bound=25.0)
        helper = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _alive(helper) and time.monotonic() < deadline:
            time.sleep(0.1)
        helper_alive = _alive(helper)
    finally:
        _kill_leftovers(str(pidfile))
    if sys.platform == "win32":
        assert seen == [False]   # the CLI could not run before it was in its job
    assert returned, "the turn hung on a pipe the CLI's helper still holds"
    assert errors == []   # no error, no timeout
    assert text == "the real answer"
    assert elapsed < 15   # a short grace after the CLI exits, not the 30s timeout
    assert not helper_alive   # what the CLI left behind is killed with its tree


def test_kill_tree_reaches_what_an_exited_child_left_behind(tmp_path, monkeypatch):
    from evi import proc as proc_mod

    pidfile = tmp_path / "helper.pid"
    seen = _slow_job_assignment(monkeypatch, pidfile)
    proc = proc_mod.spawn([sys.executable, "-c", _EXITS_LEAVING_A_HELPER, str(pidfile), "x"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=60)   # the child itself is gone ...
        helper = int(pidfile.read_text())
        assert _alive(helper)   # ... but its helper is not
        if sys.platform == "win32":
            # Started by the child at once, while the job assignment was still
            # held up: in the job all the same, because the child only ran once
            # it was in the job.
            job = getattr(proc, "_evi_job", None)
            assert job and _in_job(helper, job)
        kill_tree(proc)   # the child has exited: only the job / group reaches the helper
        deadline = time.monotonic() + 5
        while _alive(helper) and time.monotonic() < deadline:
            time.sleep(0.1)
        helper_alive = _alive(helper)
    finally:
        _kill_leftovers(str(pidfile))
    if sys.platform == "win32":
        assert seen == [False]   # spawned suspended: nothing ran before the job
    assert not helper_alive


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_spawn_resumes_the_child_and_keeps_caller_creationflags():
    from evi import proc as proc_mod

    no_window = 0x08000000   # CREATE_NO_WINDOW
    proc = proc_mod.spawn([sys.executable, "-c", "print('ran')"], stdout=subprocess.PIPE,
                          text=True, creationflags=no_window)
    try:
        out, _ = proc.communicate(timeout=60)
    finally:
        kill_tree(proc)
    assert out.strip() == "ran" and proc.returncode == 0
    assert getattr(proc, "_evi_job", None)


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_spawn_kills_a_child_it_cannot_resume(monkeypatch):
    from evi import proc as proc_mod

    started = []
    real_popen = subprocess.Popen

    class _Recording(real_popen):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            started.append(self)

    monkeypatch.setattr(proc_mod, "_POPEN", _Recording)
    monkeypatch.setattr(subprocess, "Popen", _Recording)
    monkeypatch.setattr(proc_mod, "_resume", lambda _proc: False)
    with pytest.raises(OSError, match="could not resume"):
        proc_mod.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    (child,) = started
    assert child.wait(timeout=10) is not None   # killed, not left suspended


# The CLI starts a helper holding its stdout, then never answers.
_HANGS_LEAVING_A_HELPER = r'''
import subprocess, sys, time
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", sys.argv[1]],
                          stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)
with open(sys.argv[1], "w") as f:
    f.write(str(helper.pid))
time.sleep(120)
'''


@pytest.mark.parametrize("hangs", [False, True], ids=["exits", "hangs"])
@pytest.mark.parametrize("kind", ["codex", "amp"])
def test_stream_turn_is_bounded_when_the_kill_cannot_free_stdout(kind, hangs, tmp_path,
                                                                 monkeypatch):
    """Something the tree kill can't reach (a process that broke away from the
    job or left the process group) still holds codex / amp's stdout. The turn
    still ends a short grace after the kill: with the answer when the CLI had
    finished, with a timeout error when it hadn't."""
    monkeypatch.setattr(cli_agent, "kill_tree", lambda p, **_k: p.kill())   # misses the helper
    monkeypatch.setattr(cli_agent, "_KILL_GRACE", 1.0)
    pidfile = tmp_path / "helper.pid"
    script = _HANGS_LEAVING_A_HELPER if hangs else _EXITS_LEAVING_A_HELPER
    timeout = 3.0 if hangs else 30.0
    try:
        returned, elapsed, errors, text = _run_turn_bounded(
            _stream_turns()[kind], [sys.executable, "-c", script, str(pidfile), kind],
            timeout=timeout, bound=25.0)
    finally:
        _kill_leftovers(str(pidfile))
    assert returned, "the turn hung on a pipe the kill could not free"
    if hangs:
        assert len(errors) == 1 and "exceeded 3s" in errors[0]
    else:
        assert errors == [] and text == "the real answer"
        assert elapsed < 15   # exit grace + kill grace, not the 30s timeout


_COLD_BUSY_TURN = r'''
import json, os, queue, sys, threading, time
cold = "ctypes" not in sys.modules
from evi.llm import amp_agent, codex_agent, gemini_agent
cold = cold and "ctypes" not in sys.modules   # evi.proc imports it on first spawn

def spin():   # another busy thread, as in `evi web` streaming another session
    while True:
        pass

threading.Thread(target=spin, daemon=True).start()
kind, helper_src, pidfile = sys.argv[1:4]
run = {"gemini": gemini_agent.run_gemini_turn, "codex": codex_agent.run_codex_turn,
       "amp": amp_agent.run_amp_turn}[kind]
q = queue.Queue()
done = threading.Event()

def go():
    try:
        run([sys.executable, "-c", helper_src, pidfile, kind], "prompt", out=q, timeout=30.0)
    finally:
        done.set()

t0 = time.monotonic()
threading.Thread(target=go, daemon=True).start()
returned = done.wait(20)
items = list(q.queue)
print(json.dumps({
    "cold": cold, "returned": returned, "elapsed": time.monotonic() - t0,
    "errors": [str(it[1]) for it in items if isinstance(it, tuple)],
    "text": "".join(it.choices[0].delta.content or "" for it in items
                    if not isinstance(it, tuple) and it.choices),
}), flush=True)
os._exit(0)
'''


@pytest.mark.parametrize("kind", ["gemini", "codex", "amp"])
def test_first_turn_with_a_busy_thread_keeps_its_answer(kind, tmp_path):
    """The real-world shape of the job race: the first CLI turn in a fresh
    process (``ctypes`` not imported yet, as under the scheduler or telegram)
    while another Python thread is busy."""
    pidfile = tmp_path / "helper.pid"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(Path(cli_agent.__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", "")]))
    try:
        res = subprocess.run(
            [sys.executable, "-c", _COLD_BUSY_TURN, kind, _EXITS_LEAVING_A_HELPER, str(pidfile)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90, env=env)
        report = json.loads(res.stdout.strip().splitlines()[-1])
        helper = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _alive(helper) and time.monotonic() < deadline:
            time.sleep(0.1)
        helper_alive = _alive(helper)
    finally:
        _kill_leftovers(str(pidfile))
    if sys.platform == "win32":
        assert report["cold"], report   # the premise: the drivers didn't import ctypes
    assert report["returned"], report
    assert report["errors"] == [] and report["text"] == "the real answer", report
    assert report["elapsed"] < 15, report
    assert not helper_alive, report


def test_running_children_are_killed_at_exit():
    from evi import proc as proc_mod

    proc = proc_mod.spawn([sys.executable, "-c", _PARENT_WITH_CHILD],
                          stdout=subprocess.PIPE, text=True)
    try:
        child_pid = int(proc.stdout.readline())
        proc_mod._kill_running_at_exit()
        proc.wait(timeout=10)
        deadline = time.monotonic() + 5
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(child_pid)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()


def test_spawn_returns_a_test_stand_in_untouched(monkeypatch):
    from evi import proc as proc_mod

    fake = SimpleNamespace(pid=4321)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: fake)
    assert proc_mod.spawn(["x"]) is fake
    assert not hasattr(fake, "_evi_job") and not hasattr(fake, "_evi_pgid")
    assert fake not in list(proc_mod._live)


@pytest.mark.parametrize("mod,run_fn,cls", [
    (gm, "run_gemini_turn", gm.GeminiUnavailable),
    (qw, "run_qwen_turn", qw.QwenUnavailable),
    (co, "run_copilot_turn", co.CopilotUnavailable),
])
def test_turn_timeout_surfaces_a_clear_error(mod, run_fn, cls):
    q: queue.Queue = queue.Queue()
    getattr(mod, run_fn)([sys.executable, "-c", _PARENT_WITH_CHILD], "prompt", out=q,
                         timeout=1.0)
    errs = [it for it in list(q.queue) if isinstance(it, tuple) and it[0] == "__error__"]
    assert len(errs) == 1 and isinstance(errs[0][1], cls)
    assert "exceeded 1s and was terminated" in str(errs[0][1])


def test_codex_and_amp_turns_are_bounded():
    from evi.llm import amp_agent, codex_agent

    for run_turn, cls in ((codex_agent.run_codex_turn, codex_agent.CodexUnavailable),
                          (amp_agent.run_amp_turn, amp_agent.AmpUnavailable)):
        q: queue.Queue = queue.Queue()
        t0 = time.monotonic()
        run_turn([sys.executable, "-c", _PARENT_WITH_CHILD], "prompt", out=q, timeout=1.0)
        assert time.monotonic() - t0 < 15
        errs = [it for it in list(q.queue) if isinstance(it, tuple) and it[0] == "__error__"]
        assert len(errs) == 1 and isinstance(errs[0][1], cls)
        assert "exceeded 1s" in str(errs[0][1])


def test_claude_agent_turn_is_bounded(monkeypatch):
    import asyncio

    from evi.llm import claude_agent as ca

    async def hang(*, prompt, options):
        await asyncio.sleep(60)
        yield None  # pragma: no cover

    sdk = SimpleNamespace(
        ClaudeAgentOptions=lambda **kw: SimpleNamespace(**kw), query=hang,
        AssistantMessage=type("A", (), {}), ResultMessage=type("R", (), {}),
        TextBlock=type("T", (), {}), ThinkingBlock=type("K", (), {}),
    )
    monkeypatch.setattr(ca, "_import_sdk", lambda: sdk)
    monkeypatch.setattr(ca, "_TURN_TIMEOUT", 0.5)
    with pytest.raises(ca.ClaudeAgentUnavailable, match="exceeded"):
        ca.ClaudeAgentClient().chat.completions.create(
            model="haiku", messages=[{"role": "user", "content": "hi"}])


# --- actionable auth / tier messages -----------------------------------------


def test_gemini_consumer_login_end_is_explained():
    msg = gm.explain_error(
        "This client is no longer supported for Gemini Code Assist for individuals. "
        "To continue using Gemini, please migrate to the Antigravity suite of products.")
    assert "2026-06-18" in msg and "Antigravity" in msg
    assert "GEMINI_API_KEY" in msg and "Vertex AI" in msg
    assert "(gemini: This client is no longer supported" in msg


@pytest.mark.parametrize("raw,needle", [
    ("Please set an Auth method in your settings.json", "GEMINI_API_KEY"),
    ("API key not valid. Please pass a valid API key.", "rejected the API key"),
    ("RESOURCE_EXHAUSTED: Quota exceeded for metric", "quota is used up"),
    ("[403] PERMISSION_DENIED", "Vertex AI API enabled"),
    ("Unknown argument: approval-mode", "@google/gemini-cli@latest"),
])
def test_gemini_errors_are_actionable(raw, needle):
    msg = gm.explain_error(raw)
    assert needle in msg and raw in msg


def test_gemini_unrelated_error_is_unchanged():
    assert gm.explain_error("something broke") == "something broke"
    assert gm.explain_error("request id 14290 failed") == "request id 14290 failed"


def test_gemini_retired_model_is_not_the_consumer_cut_off():
    raw = ("models/gemini-1.5-pro-002 is no longer supported. "
           "Please migrate to gemini-2.5-pro.")
    assert gm.explain_error(raw) == raw   # an API-key user picked an old model
    for_account = "The model gemini-1.0-pro is no longer supported for your account."
    assert gm.explain_error(for_account) == for_account   # names a model: not a sign-in
    signin = "Login with Google is no longer supported for this account."
    assert "2026-06-18" in gm.explain_error(signin)


def test_gemini_auth_failure_from_stderr_reaches_the_turn():
    q: queue.Queue = queue.Queue()
    stderr = json.dumps({"error": {"type": "Error", "code": 41,
                                   "message": "Please set an Auth method"}})
    gm.run_gemini_turn(["gemini"], "x", out=q,
                       run=lambda *a, **k: SimpleNamespace(stdout="", stderr=stderr,
                                                           returncode=41))
    (err,) = [it for it in list(q.queue) if isinstance(it, tuple)]
    assert "GEMINI_API_KEY" in str(err[1]) and "Antigravity" in str(err[1])


def test_gemini_missing_cli_hint_names_api_key_and_vertex(monkeypatch):
    monkeypatch.setattr(gm.shutil, "which", lambda _n: None)
    with pytest.raises(gm.GeminiUnavailable) as ei:
        gm._gemini_path()
    text = str(ei.value)
    assert "@google/gemini-cli" in text and "GEMINI_API_KEY" in text
    assert "Vertex AI" in text and "Antigravity" in text


def test_copilot_stdin_unsupported_is_explained():
    msg = co.explain_error("ERROR: Raw mode is not supported on the current process.stdin")
    assert "@github/copilot@latest" in msg and "stdin" in msg


# --- amp: @ampcode/cli and the ultra mode -------------------------------------


def test_amp_hints_name_ampcode_cli(monkeypatch):
    from evi.llm import amp_agent as am

    monkeypatch.setattr(am.shutil, "which", lambda _n: None)
    with pytest.raises(am.AmpUnavailable) as ei:
        am._amp_path()
    assert "npm i -g @ampcode/cli" in str(ei.value)
    assert cli_agent.CLI_AGENTS["amp"] == ("amp", "npm i -g @ampcode/cli")


REPO = Path(__file__).resolve().parents[1]
# The old amp package, and gemini sold as a free Google login (ended 2026-06-18).
STALE_INSTALL_HINTS = ("npm i -g @sourcegraph/amp", "Google free login",
                       "Gemini via the free login", "pick a Google login")


def _ui_code(root: Path) -> list[Path]:
    # The web UI's own code: index.html and every first-party script or page next
    # to it (its JS moved to app.js in B4). vendor/ is pinned third-party code.
    static = root / "evi" / "apps" / "web" / "static"
    return sorted(p for p in static.glob("*") if p.is_file() and p.suffix in (".js", ".html"))


def _stale_install_hints(root: Path) -> list[str]:
    files = [*(root / "evi").rglob("*.py"), *(root / "docs").rglob("*.md"), *_ui_code(root)]
    return [f"{p}: {s}" for p in files for s in STALE_INSTALL_HINTS
            if s in p.read_text(encoding="utf-8")]


def test_no_old_amp_install_command_left_in_the_package():
    assert _stale_install_hints(REPO) == []


def test_stale_hint_scan_covers_the_ui_code():
    # The UI's install hints live in app.js now; a scan that only read index.html
    # passed with a stale hint sitting in app.js.
    ui = _ui_code(REPO)
    names = {p.name for p in ui}
    assert {"index.html", "app.js"} <= names
    assert any("npm i -g @ampcode/cli" in p.read_text(encoding="utf-8") for p in ui)


def test_stale_hint_in_any_ui_script_is_caught(tmp_path):
    static = tmp_path / "evi" / "apps" / "web" / "static"
    (static / "vendor" / "lib").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (static / "index.html").write_text("<script src=app.js></script>", encoding="utf-8")
    (static / "app.js").write_text("// hint: npm i -g @sourcegraph/amp\n", encoding="utf-8")
    (static / "panels.js").write_text("const t = 'pick a Google login';\n", encoding="utf-8")
    # Pinned third-party bundles are not ours to fix.
    (static / "vendor" / "lib" / "lib.min.js").write_text(
        "/* Google free login */", encoding="utf-8")
    hits = _stale_install_hints(tmp_path)
    assert len(hits) == 2
    assert any("app.js: npm i -g @sourcegraph/amp" in h for h in hits)
    assert any("panels.js: pick a Google login" in h for h in hits)


def test_docs_name_the_current_install_commands():
    text = (Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text(
        encoding="utf-8")
    for kind, (_binary, install) in cli_agent.CLI_AGENTS.items():
        assert install in text, kind


def test_amp_ultra_mode_is_listed_and_passed(monkeypatch):
    from evi.backends.amp_agent import AmpAgentBackend
    from evi.llm import amp_agent as am

    assert "ultra" in [m.id for m in AmpAgentBackend().list_models()]
    seen = {}
    monkeypatch.setattr(am, "_amp_path", lambda: "amp")
    monkeypatch.setattr(am, "_require_auth", lambda: None)

    def fake_turn(argv, prompt, *, out, timeout=None, popen=None):
        seen["argv"] = argv
        out.put(cli_agent.delta_chunk(finish_reason="stop"))

    monkeypatch.setattr(am, "run_amp_turn", fake_turn)
    am.AmpAgentClient().chat.completions.create(
        model="ultra", messages=[{"role": "user", "content": "hi"}])
    assert seen["argv"][-2:] == ["--mode", "ultra"]


# --- unattended entry points -------------------------------------------------


@pytest.mark.parametrize("kind", sorted(cli_agent.DELEGATE_BACKENDS))
def test_unattended_refuses_delegate_backends(kind):
    settings = SimpleNamespace(backend=kind)
    with pytest.raises(cli_agent.CliAgentUnattended) as ei:
        cli_agent.check_unattended(settings, "telegram")
    text = str(ei.value)
    assert f"The {kind} backend" in text and "unattended telegram run" in text
    assert "allow_cli_agent_unattended = true" in text


@pytest.mark.parametrize("kind", ["lmstudio", "ollama", "llamacpp", "openai_compat",
                                  "claude_agent"])
def test_unattended_allows_non_delegate_backends(kind):
    cli_agent.check_unattended(SimpleNamespace(backend=kind), "telegram")


def test_unattended_opt_in_and_config_shapes():
    allowed = SimpleNamespace(backend="gemini", allow_cli_agent_unattended=True)
    cli_agent.check_unattended(allowed, "scheduled task")
    cli_agent.check_unattended(SimpleNamespace(llm=allowed), "scheduled task")   # a Config
    # Only a real True opts in; a stray string does not.
    with pytest.raises(cli_agent.CliAgentUnattended):
        cli_agent.check_unattended(
            SimpleNamespace(backend="gemini", allow_cli_agent_unattended="yes"), "x")


def test_unattended_fails_closed_with_current_llm_settings():
    from evi.config import LLMSettings

    with pytest.raises(cli_agent.CliAgentUnattended):
        cli_agent.check_unattended(LLMSettings(backend="gemini"), "routine")


def test_unattended_block_refuses_up_front():
    ran = []
    with pytest.raises(cli_agent.CliAgentUnattended):
        with cli_agent.unattended("telegram", SimpleNamespace(backend="qwen")):
            ran.append(True)
    assert ran == []
    assert cli_agent.current_unattended() is None


def test_delegate_client_refuses_inside_unattended_block(monkeypatch):
    client, rec = _client_with_recorder("gemini", monkeypatch)
    msgs = [{"role": "user", "content": "hi"}]
    with cli_agent.unattended("scheduled task"):
        with pytest.raises(cli_agent.CliAgentUnattended, match="unattended scheduled task"):
            client.chat.completions.create(model="gemini-2.5-pro", messages=msgs)
        with pytest.raises(cli_agent.CliAgentUnattended):
            client.chat.completions.create(model="gemini-2.5-pro", messages=msgs, stream=True)
    assert rec.calls == []   # no process was started
    # Outside the block, and inside an allowed one, it runs.
    client.chat.completions.create(model="gemini-2.5-pro", messages=msgs)
    with cli_agent.unattended("scheduled task",
                              SimpleNamespace(backend="gemini", allow_cli_agent_unattended=True)):
        client.chat.completions.create(model="gemini-2.5-pro", messages=msgs)
    assert len(rec.calls) == 2


def test_unattended_marker_does_not_leak_to_other_threads(monkeypatch):
    client, rec = _client_with_recorder("copilot", monkeypatch)
    msgs = [{"role": "user", "content": "hi"}]
    errors: list[BaseException] = []

    def attended_chat():
        try:
            client.chat.completions.create(model="auto", messages=msgs)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with cli_agent.unattended("telegram"):
        t = threading.Thread(target=attended_chat)
        t.start()
        t.join(30)
    assert errors == [] and len(rec.calls) == 1


def _call_on_thread(fn) -> list[BaseException]:
    errors: list[BaseException] = []

    def body():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=body)
    t.start()
    t.join(30)
    return errors


def test_client_built_inside_unattended_block_refuses_on_any_thread(monkeypatch):
    msgs = [{"role": "user", "content": "hi"}]
    with cli_agent.unattended("scheduled task"):
        client, rec = _client_with_recorder("gemini", monkeypatch)   # the agent's client
    # Drained later on a worker thread, outside the block's context.
    errors = _call_on_thread(lambda: client.chat.completions.create(
        model="gemini-2.5-pro", messages=msgs))
    assert len(errors) == 1 and isinstance(errors[0], cli_agent.CliAgentUnattended)
    assert "unattended scheduled task run" in str(errors[0])
    assert rec.calls == []
    # Built inside an ALLOWED block, it runs anywhere.
    with cli_agent.unattended("scheduled task",
                              SimpleNamespace(backend="gemini", allow_cli_agent_unattended=True)):
        allowed, rec2 = _client_with_recorder("gemini", monkeypatch)
    assert _call_on_thread(lambda: allowed.chat.completions.create(
        model="gemini-2.5-pro", messages=msgs)) == []
    assert len(rec2.calls) == 1


def test_copied_context_carries_the_refusal_into_a_worker_thread(monkeypatch):
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    client, rec = _client_with_recorder("qwen", monkeypatch)   # built outside the block
    msgs = [{"role": "user", "content": "hi"}]
    with cli_agent.unattended("telegram"), ThreadPoolExecutor(1) as ex:
        ctx = contextvars.copy_context()   # what tool dispatch does once it copies context
        fut = ex.submit(ctx.run, client.chat.completions.create, model="m", messages=msgs)
        with pytest.raises(cli_agent.CliAgentUnattended):
            fut.result(30)
    assert rec.calls == []


@pytest.mark.parametrize("key", ["vision_backend", "ocr_backend", "guard_backend"])
def test_unattended_refuses_a_delegate_specialty_backend(key, monkeypatch, tmp_path):
    from evi import config as config_mod
    from evi.config import Config

    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    cfg = Config()
    cfg.llm.backend = "ollama"
    setattr(cfg.models, key, "gemini")   # a tool's specialty client would run gemini
    with pytest.raises(cli_agent.CliAgentUnattended) as ei:
        with cli_agent.unattended("telegram", cfg):
            pytest.fail("the block must not run")
    text = str(ei.value)
    assert "The gemini backend" in text and f"Point [models] {key} at" in text
    assert cli_agent.current_unattended() is None
    # A local specialty, or the explicit opt-in, is fine.
    setattr(cfg.models, key, "ollama")
    cli_agent.check_unattended(cfg, "telegram")
    setattr(cfg.models, key, "gemini")
    cli_agent.check_unattended(
        SimpleNamespace(llm=SimpleNamespace(backend="ollama", allow_cli_agent_unattended=True),
                        models=cfg.models), "telegram")


def test_claude_agent_is_not_a_delegate():
    from evi.llm import claude_agent as ca

    assert ca.ClaudeAgentClient.delegate is False
    assert "claude_agent" not in cli_agent.DELEGATE_BACKENDS


def test_unattended_agent_run_reports_the_refusal(monkeypatch, tmp_path):
    from evi import config as config_mod
    from evi.config import Config
    from evi.headless import run_headless
    from evi.llm.agent import Agent

    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    client, rec = _client_with_recorder("gemini", monkeypatch)
    cfg = Config()
    cfg.llm.backend = "gemini"
    cfg.llm.model = "gemini-2.5-pro"
    cfg.llm.fallback_models = []
    agent = Agent(client=client, config=cfg, tools=[])
    with cli_agent.unattended("telegram"):
        res = run_headless(agent, "hello", max_turns=1)
    assert res.error and "won't use it for an unattended telegram run" in res.error
    assert rec.calls == []


# --- evi doctor: CLI-agent backends -------------------------------------------


def _doctor_with_backend(monkeypatch, kind, **llm):
    from evi import config as config_mod
    from evi import doctor

    cfg = config_mod.Config()
    cfg.llm.backend = kind
    for k, v in llm.items():
        setattr(cfg.llm, k, v)
    monkeypatch.setattr(config_mod.Config, "load", classmethod(lambda cls: cfg))
    return doctor


def test_doctor_cli_agent_missing_names_install_command(monkeypatch):
    doctor = _doctor_with_backend(monkeypatch, "amp", base_url="")
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: None)
    (check,) = doctor._check_backend()
    assert check.status == "fail" and check.fix == ("npm i -g @ampcode/cli",)


def test_doctor_amp_without_login_fails(monkeypatch, tmp_path):
    doctor = _doctor_with_backend(monkeypatch, "amp", base_url="")
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/bin/amp")
    monkeypatch.delenv("AMP_API_KEY", raising=False)
    monkeypatch.setenv("AMP_SETTINGS_FILE", str(tmp_path / "nope.json"))
    checks = doctor._check_backend()
    assert [c.status for c in checks] == ["ok", "fail"]
    assert checks[1].fix == ("amp login",)


def test_doctor_gemini_personal_login_warns(monkeypatch, tmp_path):
    doctor = _doctor_with_backend(monkeypatch, "gemini", base_url="")
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/bin/gemini")
    for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "settings.json").write_text(
        json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}), encoding="utf-8")
    checks = doctor._check_backend()
    assert checks[0].status == "ok"
    assert checks[1].status == "warn" and "Antigravity" in checks[1].detail
    assert "GEMINI_API_KEY" in checks[1].fix[0]


def test_doctor_gemini_api_key_ok(monkeypatch):
    doctor = _doctor_with_backend(monkeypatch, "gemini", base_url="")
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/bin/gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    checks = doctor._check_backend()
    assert [c.status for c in checks] == ["ok", "ok"]


def test_doctor_http_backend_rejected_key_fails(monkeypatch):
    import httpx

    doctor = _doctor_with_backend(monkeypatch, "openai_compat",
                                  base_url="http://127.0.0.1:9/v1", api_key="env:EVI_TEST_KEY")
    monkeypatch.setenv("EVI_TEST_KEY", "sk-test")
    seen = {}

    def fake_get(url, timeout=None, headers=None):
        seen["headers"] = headers
        return SimpleNamespace(status_code=401)

    monkeypatch.setattr(httpx, "get", fake_get)
    (check,) = doctor._check_backend()
    assert check.status == "fail" and "rejected the API key" in check.detail
    assert seen["headers"] == {"Authorization": "Bearer sk-test"}
