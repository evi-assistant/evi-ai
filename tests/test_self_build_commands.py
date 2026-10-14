"""Every command in docs/self-build.md runs, against a fake LLM backend.

The guide once told eVi to run `evi chat --mode code`, an option that did not
exist. This test reads each line of the guide's code blocks and runs it:

- `evi ...` / `python -m evi ...` run for real as a subprocess, with EVI_HOME a
  temp dir whose config points at a fake OpenAI-compatible server (a stdlib
  HTTP server below), so chat/run get a scripted reply and the test can read
  the tool list the CLI sent;
- the dev bootstraps run with --dry-run / -DryRun (placed right after the
  script path, so no option can swallow it) and a temp venv path;
- pytest commands run with --collect-only (on one small file when the command
  names no test path), ruff with --exit-zero, `playwright install` with
  --dry-run;
- the desktop build and cargo lines need Node/Tauri/Rust and are not run: the
  test checks the scripts and the Cargo project they use exist.

A line no handler recognises fails the test, so a new command in the guide
needs a handler here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "self-build.md"
FAKE_REPLY = "Hello from the fake self-build backend."


def guide_commands() -> list[tuple[str, str]]:
    """(fence language, command line) for every line in a fenced code block."""
    out: list[tuple[str, str]] = []
    lang: str | None = None
    for raw in GUIDE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            lang = (stripped[3:].strip() or "text") if lang is None else None
            continue
        if lang is None or not stripped or stripped.startswith("#"):
            continue
        out.append((lang, re.sub(r"\s+#\s.*$", "", stripped)))
    return out


COMMANDS = guide_commands()


# --- the fake backend ------------------------------------------------------


class _FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep test output clean
        pass

    def _json(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._json({"object": "list", "data": [{"id": "fake", "object": "model"}]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        self.server.requests.append(body)  # type: ignore[attr-defined]
        if not body.get("stream"):
            self._json({
                "id": "x", "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": FAKE_REPLY}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in (({"role": "assistant", "content": FAKE_REPLY}, None), ({}, "stop")):
            chunk = {"id": "x", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture(scope="module")
def fake_llm_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
    server.requests = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def env(tmp_path, fake_llm_server) -> dict[str, str]:
    """Environment for a subprocess: an isolated EVI_HOME aimed at the fake
    backend, this checkout's evi first on the path, a temp dev venv path."""
    home = tmp_path / "evihome"
    home.mkdir()
    port = fake_llm_server.server_address[1]
    (home / "config.toml").write_text(
        "[llm]\n"
        'backend = "openai_compat"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'api_key = "test"\n'
        'model = "fake"\n',
        encoding="utf-8",
    )
    fake_llm_server.requests.clear()
    e = {k: v for k, v in os.environ.items() if not k.startswith("EVI_")}
    e.update({
        "EVI_HOME": str(home),
        "EVI_DEV_VENV": (tmp_path / "venvs" / "evi-dev").as_posix(),
        "NO_COLOR": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
        # a Python 3.12+ the bootstraps can find without uv
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
    })
    return e


def _run(argv: list[str], env: dict[str, str], cwd: Path, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, cwd=str(cwd), input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=240)


def _ok(r: subprocess.CompletedProcess, codes=(0,)) -> str:
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode in codes, f"exit {r.returncode}\n{out}"
    return out


# --- evi -----------------------------------------------------------------

# prompt_toolkit needs a real console, so the REPL reads stdin through
# ReplInput's own plain fallback (what it uses when prompt_toolkit is absent).
_CHAT = """
import sys
import evi.repl_input as ri

def _plain(self, agent, history_path=None):
    self.agent = agent
    self._fallback = True
    self._session = None

ri.ReplInput.__init__ = _plain
from evi.apps.cli.main import app
app(sys.argv[1:], prog_name="evi")
"""


def _sent_tools(server) -> set[str]:
    assert server.requests, "the CLI never called the backend"
    return {t["function"]["name"] for t in server.requests[0].get("tools") or []}


def _assert_code_preset(names: set[str]) -> None:
    from evi.modes import MODES
    from evi.tools import register_builtin_tools

    reg = register_builtin_tools()
    cats = set(MODES["code"].categories)
    assert "run_command" in names, sorted(names)
    assert "web_search" not in names, sorted(names)
    assert all(reg[n].category in cats for n in names if n in reg), sorted(names)


def _project(tmp_path: Path) -> Path:
    """A git repo carrying this checkout's EVI.md and one test file."""
    p = tmp_path / "project"
    p.mkdir()
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    shutil.copyfile(ROOT / "EVI.md", p / "EVI.md")
    (p / "test_smoke.py").write_text("def test_smoke():\n    pass\n", encoding="utf-8")
    return p


def run_evi(args: list[str], env, tmp_path, server) -> None:
    project = _project(tmp_path)
    if args[:1] == ["chat"]:
        out = _ok(_run([sys.executable, "-c", _CHAT, *args], env, project, stdin="hello\n"))
        assert FAKE_REPLY in out
        assert "project=EVI.md" in out  # loaded as project context
        if "--mode" in args:
            _assert_code_preset(_sent_tools(server))
    elif args[:1] == ["run"]:
        out = _ok(_run([sys.executable, "-m", "evi", *args], env, project))
        assert FAKE_REPLY in out
        if "--mode" in args:
            _assert_code_preset(_sent_tools(server))
    elif args[:2] == ["doctor", "--dev"]:
        # --strict: a failing check fails the command, not just prints. The
        # project is not the eVi checkout, so collection is skipped (it would
        # run the project's conftest.py) and says so.
        strict = args if "--strict" in args else [*args, "--strict"]
        out = _ok(_run([sys.executable, "-m", "evi", *strict], env, project))
        assert "line endings" in out and "pytest --collect-only" in out
    else:
        pytest.fail(f"no handler for `evi {' '.join(args)}`")


# --- everything else -------------------------------------------------------


def _git_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if git:  # Git for Windows: <Git>/cmd/git.exe next to <Git>/bin/bash.exe
        for cand in (Path(git).parents[1] / "bin" / "bash.exe",
                     Path(git).parents[1] / "usr" / "bin" / "bash.exe"):
            if cand.is_file():
                return str(cand)
    found = shutil.which("bash")
    if found and "system32" not in found.lower():  # not the WSL launcher
        return found
    return None


def _need_module(name: str) -> None:
    if importlib.util.find_spec(name) is None:
        pytest.skip(f"{name} is not installed here")


def run_pytest(args: list[str], env) -> None:
    if "-n" in args:
        _need_module("xdist")
    if not any(a.startswith("tests") for a in args):
        args = [*args, "tests/test_bootstrap.py"]  # validate the flags, not the whole suite
    r = _run([sys.executable, "-m", "pytest", *args, "--collect-only", "-q",
              "-p", "no:cacheprovider"], env, ROOT)
    _ok(r, codes=(0, 5))  # 5: nothing selected (tests/e2e without Playwright)


def run_line(lang: str, line: str, env, tmp_path, server) -> None:
    words = line.split() if lang == "powershell" else shlex.split(line)
    head = words[0]
    if head == "evi":
        return run_evi(words[1:], env, tmp_path, server)
    if head == "python" and words[1:3] == ["-m", "evi"]:
        return run_evi(words[3:], env, tmp_path, server)
    if head == "python" and words[1:3] == ["-m", "pytest"]:
        return run_pytest(words[3:], env)
    if head == "python" and words[1:4] == ["-m", "ruff", "check"]:
        out = _ok(_run([sys.executable, "-m", "ruff", "check", *words[4:], "--exit-zero"], env, ROOT))
        assert "E902" not in out  # every path it names exists
        return
    if head == "python" and words[1:4] == ["-m", "playwright", "install"]:
        _need_module("playwright")
        _ok(_run([sys.executable, "-m", "playwright", "install", "--dry-run", *words[4:]], env, ROOT))
        return
    if head == "powershell" and "scripts\\dev-bootstrap.ps1" in words:
        if os.name != "nt" or not shutil.which("powershell"):
            pytest.skip("Windows PowerShell only")
        at = words.index("scripts\\dev-bootstrap.ps1") + 1
        out = _ok(_run([*words[:at], "-DryRun", *words[at:]], env, ROOT))
        assert "dry run: nothing was changed" in out
        return
    if head == "bash" and words[1:2] == ["scripts/dev-bootstrap.sh"]:
        bash = _git_bash()
        if bash is None:
            pytest.skip("no bash (Git Bash on Windows)")
        out = _ok(_run([bash, words[1], "--dry-run", *words[2:]], env, ROOT))
        assert "dry run: nothing was changed" in out
        return
    # Not run: these need Node, Tauri, PyInstaller or Rust. Check what they use.
    if head in ("powershell", "bash") and any("build-desktop" in w for w in words):
        script = next(w for w in words if "build-desktop" in w).replace("\\", "/")
        assert (ROOT / script).is_file(), script
        return
    if head == "cd":
        assert (ROOT / words[1]).is_dir(), words[1]
        return
    if head == "cargo" and words[1] in ("check", "test") and "--locked" in words:
        assert (ROOT / "desktop" / "src-tauri" / "Cargo.lock").is_file()
        return
    if head == "export" and line.startswith('export PATH="'):
        return  # shell setup, nothing to run
    pytest.fail(f"no handler for this command in docs/self-build.md: {line!r}")


@pytest.mark.timeout(300)
@pytest.mark.parametrize(("lang", "line"), COMMANDS, ids=[c[1] for c in COMMANDS])
def test_self_build_command_runs(lang, line, env, tmp_path, fake_llm_server):
    run_line(lang, line, env, tmp_path, fake_llm_server)


def test_guide_covers_the_loop():
    lines = {line for _, line in COMMANDS}
    for want in ("evi chat --mode code", "evi doctor --dev",
                 "bash scripts/dev-bootstrap.sh",
                 "powershell -ExecutionPolicy Bypass -File scripts\\dev-bootstrap.ps1",
                 "python -m ruff check evi tests scripts"):
        assert want in lines, want
    assert any(line.startswith("bash scripts/dev-bootstrap.sh --worktree ") for line in lines)
