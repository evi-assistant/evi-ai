"""Verify-before-done: the project checks a turn runs once it has changed files.

Small models declare success early; an executed check is ground truth. When a
turn wrote files and would end, the agent (`evi.llm.agent.Agent`) runs the
checks `detect` finds for what changed, feeds the failures back to the model
as a framed note and lets it go on, up to ``[checks] max_rounds`` failing
rounds; then the turn ends with ``checks_failed``. Each run is recorded
(`CheckRun.record`: name, cmd, exit, duration_ms, tail, round, source), in the
run report's ``checks`` and as a ``CheckResult`` event. Once a round passed,
the checks run again at a later would-be end of the turn (a stop hook sent
the model back) only if what changed is different now (`fingerprint`).

What `detect` finds, for the files changed in the turn (paths relative to the
run's folder), never the whole suite by default:

- ruff — ``ruff check --no-cache <changed .py files>`` when the folder has a
  ruff config (``[tool.ruff]`` in pyproject.toml, ruff.toml, .ruff.toml);
- pytest — the targeted tests only: a changed test file itself
  (``test_*.py`` / ``*_test.py``), plus ``tests/test_<module>.py`` for each
  changed module that has one;
- npm — ``npm test`` when package.json has a real ``scripts.test`` and a
  JS / TS file changed;
- cargo — ``cargo check`` when the folder has a Cargo.toml and a ``.rs`` file
  changed;
- ``[checks] commands`` — commands of your own (each a command line, split
  like a POSIX shell would, or an argv array), run whenever files changed:
  from config.toml, and from the folder's ``.evi.toml`` only when the folder
  is trusted and that version of the file approved (`evi.trust`).
  ``[checks] detect = false`` runs only these.

In an untrusted folder no check runs at all (pytest runs the repository's
code): the caller reports that the checks were skipped and why.

Settings (``[checks]``, `settings`): ``verify`` = "auto" (default: on for a
run under the implementer policy, `evi.policy`, off in an interactive
session), "always" (also an interactive Code-mode session, each check first
offered through the normal permission flow as a run_command call) or "off";
``max_rounds`` = 2; ``timeout`` = 300 seconds per command; ``tail_lines`` =
40 lines of output kept.

A check runs through `evi.proc.run` in the run's folder: its whole process
tree killed at the timeout or when the turn is cancelled, with no
``*_KEY`` / ``*_TOKEN`` / ``*_SECRET`` variables in its environment and no
bytecode written. Its output is redacted (`evi.redact`) and cut to the tail.
Under the implementer policy every check must also pass the policy
(`evi.policy.Policy.allows_argv`); `allowlist` gives the commands the policy
adds at run start.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

VERIFY_MODES = ("auto", "always", "off")
DEFAULT_MAX_ROUNDS = 2
DEFAULT_TIMEOUT = 300.0
DEFAULT_TAIL_LINES = 40
# The most of a check's output a record (and the note to the model) keeps.
MAX_TAIL_CHARS = 4000
# A changed file up to this size is hashed for `fingerprint`; a bigger one
# counts by its size and mtime.
FINGERPRINT_HASH_MAX = 4 * 1024 * 1024
_JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
# A changed path under one of these is a cache or a dependency, not the
# turn's work (`evi.headless.ChangeTracker.SKIP_DIRS`).
_SKIP_PARTS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox",
})
# npm init's placeholder test script: it always fails.
_NPM_PLACEHOLDER = "no test specified"


@dataclass(frozen=True)
class Check:
    """One check to run: `argv` (no shell), a lint or a test, and where it
    came from ("pyproject", "ruff.toml", "pytest", "package.json",
    "Cargo.toml", "config", "project"). `ok_codes` are the exit codes that
    pass (pytest's 5, no tests collected, too)."""

    name: str
    argv: tuple[str, ...]
    kind: str  # "lint" | "test"
    source: str
    ok_codes: tuple[int, ...] = (0,)

    @property
    def cmd(self) -> str:
        return display(self.argv)


@dataclass
class CheckRun:
    """What running one check gave. `exit` is None when it didn't finish
    (timed out, cancelled, could not start, refused): `note` says which."""

    check: Check
    exit: int | None
    duration_ms: int
    tail: str
    round: int = 1
    note: str = ""
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.exit is not None and self.exit in self.check.ok_codes

    def record(self) -> dict[str, Any]:
        """The run report's entry (`evi.events` ``$defs/check``)."""
        return {"name": self.check.name, "cmd": self.check.cmd, "exit": self.exit,
                "duration_ms": self.duration_ms, "tail": self.tail, "round": self.round,
                "source": self.check.source}


@dataclass(frozen=True)
class Settings:
    """``[checks]`` as the agent uses it, every value checked."""

    verify: str = "auto"
    max_rounds: int = DEFAULT_MAX_ROUNDS
    timeout: float = DEFAULT_TIMEOUT
    tail_lines: int = DEFAULT_TAIL_LINES
    detect: bool = True
    commands: tuple[Any, ...] = ()


def _positive(value: Any, default: float, cast: type = int) -> Any:
    try:
        n = cast(value)
    except (TypeError, ValueError):
        return cast(default)
    return n if n > 0 else cast(default)


def settings(config: Any) -> Settings:
    """``[checks]`` of `config`: an unknown ``verify`` means "auto", a
    number that isn't positive its default."""
    cs = getattr(config, "checks", None)
    verify = str(getattr(cs, "verify", "auto") or "auto").strip().lower()
    commands = getattr(cs, "commands", None)
    return Settings(
        verify=verify if verify in VERIFY_MODES else "auto",
        max_rounds=_positive(getattr(cs, "max_rounds", DEFAULT_MAX_ROUNDS), DEFAULT_MAX_ROUNDS),
        timeout=_positive(getattr(cs, "timeout", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT, float),
        tail_lines=_positive(getattr(cs, "tail_lines", DEFAULT_TAIL_LINES), DEFAULT_TAIL_LINES),
        detect=getattr(cs, "detect", True) is not False,
        commands=tuple(commands) if isinstance(commands, (list, tuple)) else (),
    )


def display(argv: Sequence[str]) -> str:
    """`argv` as a command line to show (and to offer as a run_command)."""
    words = [str(a) for a in argv]
    return subprocess.list2cmdline(words) if os.name == "nt" else shlex.join(words)


# ---- detection -----------------------------------------------------------------


def _changed(paths: Iterable[str]) -> list[str]:
    """The changed paths that count: ``/``-separated, relative, not under a
    cache or dependency folder; in order, once each."""
    out: list[str] = []
    for raw in paths or ():
        p = str(raw or "").strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        if not p or p.startswith("/") or ".." in p.split("/"):
            continue
        if any(part in _SKIP_PARTS for part in p.split("/")):
            continue
        if p not in out:
            out.append(p)
    return out


def _toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _ruff_config(root: Path) -> str | None:
    """Where the folder's ruff config is, as a check's source; None for none."""
    for name in ("ruff.toml", ".ruff.toml"):
        if (root / name).is_file():
            return name
    tool = _toml(root / "pyproject.toml").get("tool")
    if isinstance(tool, dict) and isinstance(tool.get("ruff"), dict):
        return "pyproject"
    return None


def _module_argv(module: str) -> tuple[str, ...] | None:
    """``<this interpreter> -m <module>`` when it has `module`."""
    try:
        found = importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        found = False
    return (sys.executable, "-m", module) if found and sys.executable else None


def _which(program: str) -> str | None:
    return shutil.which(program)


def _project_python(root: Path) -> str | None:
    """The folder's own virtualenv interpreter (.venv / venv), if it has one."""
    for venv in (".venv", "venv"):
        for rel in ("Scripts/python.exe", "bin/python"):
            exe = root / venv / rel
            if exe.is_file():
                return str(exe)
    return None


def _ruff_argv() -> tuple[str, ...] | None:
    mod = _module_argv("ruff")
    if mod is not None:
        return mod
    exe = _which("ruff")
    return (exe,) if exe else None


def _pytest_argv(root: Path) -> tuple[str, ...] | None:
    own = _project_python(root)
    if own:
        return (own, "-m", "pytest")
    mod = _module_argv("pytest")
    if mod is not None:
        return mod
    exe = _which("pytest")
    return (exe,) if exe else None


def _npm_test(root: Path) -> bool:
    """package.json has a real ``scripts.test`` (not npm init's placeholder)."""
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return False
    scripts = data.get("scripts") if isinstance(data, dict) else None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    return isinstance(test, str) and bool(test.strip()) and _NPM_PLACEHOLDER not in test


def _ruff(root: Path, changed: list[str]) -> list[Check]:
    files = [p for p in changed if p.endswith(".py") and (root / p).is_file()]
    source = _ruff_config(root) if files else None
    argv = _ruff_argv() if source else None
    if not argv:
        return []
    return [Check("ruff", (*argv, "check", "--no-cache", *files), "lint", source)]


def _test_targets(root: Path, changed: list[str]) -> list[str]:
    targets: list[str] = []
    for p in changed:
        if not p.endswith(".py"):
            continue
        name = p.rsplit("/", 1)[-1]
        if name.startswith("test_") or name.endswith("_test.py"):
            candidate = p
        elif name in ("conftest.py", "__init__.py", "__main__.py"):
            continue
        else:
            candidate = f"tests/test_{name}"
        if (root / candidate).is_file() and candidate not in targets:
            targets.append(candidate)
    return targets


def _pytest(root: Path, changed: list[str]) -> list[Check]:
    targets = _test_targets(root, changed)
    argv = _pytest_argv(root) if targets else None
    if not argv:
        return []
    return [Check("pytest", (*argv, "-q", "-p", "no:cacheprovider", *targets), "test",
                  "pytest", ok_codes=(0, 5))]


def _npm(root: Path, changed: list[str]) -> list[Check]:
    if not any(p.endswith(_JS_SUFFIXES) for p in changed) or not _npm_test(root):
        return []
    exe = _which("npm")
    return [Check("npm test", (exe, "test"), "test", "package.json")] if exe else []


def _cargo(root: Path, changed: list[str]) -> list[Check]:
    if not any(p.endswith(".rs") for p in changed) or not (root / "Cargo.toml").is_file():
        return []
    exe = _which("cargo")
    return [Check("cargo check", (exe, "check"), "lint", "Cargo.toml")] if exe else []


def _argv_of(command: Any) -> tuple[str, ...] | None:
    """A ``[checks] commands`` entry as argv: a list as it is, a string split
    the POSIX way (use ``/`` in paths, or an array)."""
    if isinstance(command, (list, tuple)):
        words = tuple(str(w) for w in command if str(w))
    elif isinstance(command, str):
        try:
            words = tuple(shlex.split(command, posix=True))
        except ValueError:
            return None
    else:
        return None
    return words or None


def project_commands(root: str | Path) -> list[Any]:
    """``[checks] commands`` from the folder's ``.evi.toml``, only when the
    folder is trusted and that version of the file approved
    (`evi.trust.TrustStatus.project_layer_on`); else []. Never raises."""
    try:
        from evi import trust
        from evi.project import read_project_config

        pc = read_project_config(Path(root))
        if pc is None or not pc.data:
            return []
        if not trust.status(Path(root), config=pc).project_layer_on:
            return []
        body = pc.data.get("checks")
        commands = body.get("commands") if isinstance(body, dict) else None
        return list(commands) if isinstance(commands, list) else []
    except Exception:  # noqa: BLE001 — fail closed: no project commands
        return []


def _configured(config: Any, root: Path) -> list[Check]:
    out: list[Check] = []
    for source, commands in (("config", settings(config).commands),
                             ("project", project_commands(root))):
        for command in commands:
            argv = _argv_of(command)
            if argv:
                name = Path(argv[0]).name or argv[0]
                out.append(Check(name, argv, "test", source))
    return out


def detect(
    root: str | Path, config: Any, trusted: bool, changed_files: Iterable[str]
) -> list[Check]:
    """The checks to run for `changed_files` (relative to `root`; see the
    module doc). [] in an untrusted folder, or when nothing that counts
    changed."""
    if not trusted:
        return []
    folder = Path(root)
    changed = _changed(changed_files)
    if not changed:
        return []
    out: list[Check] = []
    if settings(config).detect:
        for finder in (_ruff, _pytest, _npm, _cargo):
            out += finder(folder, changed)
    out += _configured(config, folder)
    seen: set[tuple[str, ...]] = set()
    unique = []
    for c in out:
        if c.argv not in seen:
            seen.add(c.argv)
            unique.append(c)
    return unique


def allowlist(root: str | Path, config: Any, trusted: bool) -> list[tuple[str, ...]]:
    """The argv prefixes of every check `detect` could run in `root`, for
    `evi.policy.Policy.shell_allow` at run start: the detected tools'
    commands (without their file arguments) and the configured commands
    whole. [] in an untrusted folder."""
    if not trusted:
        return []
    folder = Path(root)
    out: list[tuple[str, ...]] = []
    if settings(config).detect:
        ruff = _ruff_argv() if _ruff_config(folder) else None
        if ruff:
            out.append((*ruff, "check"))
        pytest = _pytest_argv(folder)
        if pytest:
            out.append(pytest)
        npm = _which("npm") if _npm_test(folder) else None
        if npm:
            out.append((npm, "test"))
        cargo = _which("cargo") if (folder / "Cargo.toml").is_file() else None
        if cargo:
            out.append((cargo, "check"))
    out += [c.argv for c in _configured(config, folder)]
    return list(dict.fromkeys(out))


# ---- running -------------------------------------------------------------------


def fingerprint(root: str | Path, changed_files: Iterable[str]) -> tuple:
    """What `changed_files` (relative to `root`) hold now: each path with a
    hash of its bytes (its size and mtime past FINGERPRINT_HASH_MAX bytes;
    None when it can't be read, a folder or gone). Equal fingerprints mean
    nothing changed since, so checks that passed on one need not run again."""
    folder = Path(root)
    out = []
    for rel in _changed(changed_files):
        p = folder / rel
        try:
            st = p.stat()
            if st.st_size <= FINGERPRINT_HASH_MAX:
                mark = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                mark = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            mark = None
        out.append((rel, mark))
    return tuple(out)


def tail_text(text: str, lines: int = DEFAULT_TAIL_LINES) -> str:
    """The last `lines` lines of `text`, at most MAX_TAIL_CHARS characters."""
    kept = "\n".join(str(text or "").rstrip().splitlines()[-max(1, int(lines)):])
    return kept if len(kept) <= MAX_TAIL_CHARS else "…" + kept[-(MAX_TAIL_CHARS - 1):]


def run_check(
    check: Check,
    root: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    tail_lines: int = DEFAULT_TAIL_LINES,
    cancel_token: threading.Event | None = None,
    round: int = 1,
) -> CheckRun:
    """Run `check` in `root` (see the module doc) and say how it went."""
    from evi import proc
    from evi.redact import Redactor

    red = Redactor()
    env = proc.scrubbed_env()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()

    def elapsed() -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    try:
        res = proc.run(list(check.argv), cwd=str(root), timeout=timeout,
                       cancel_token=cancel_token, env=env, merge_stderr=True,
                       encoding="utf-8")
    except OSError as exc:
        return CheckRun(check, None, elapsed(), red(f"could not start: {exc}"), round,
                        note="could not start")
    tail = tail_text(red(res.output), tail_lines)
    if res.cancelled:
        return CheckRun(check, None, elapsed(), tail, round, note="cancelled", cancelled=True)
    if res.timed_out:
        note = f"timed out after {timeout:g}s (its process tree was killed)"
        return CheckRun(check, None, elapsed(), tail_text(f"{tail}\n({note})", tail_lines),
                        round, note=note)
    return CheckRun(check, res.returncode, elapsed(), tail, round)


def refused(check: Check, reason: str, round: int = 1) -> CheckRun:
    """The record of a check that was not run: the policy or the user refused it."""
    return CheckRun(check, None, 0, f"not run: {reason}", round, note="refused")


def failure_note(runs: Sequence[CheckRun], round: int, max_rounds: int) -> str:
    """The note that tells the model what failed (each failing command, its
    exit and its output's tail)."""
    parts = [f"eVi ran the project's checks on the files you changed, and they failed "
             f"(round {round} of {max_rounds}). Fix what they report before you finish."]
    for run in runs:
        if run.ok:
            continue
        status = f"exit {run.exit}" if run.exit is not None else (run.note or "did not finish")
        parts.append(f"$ {run.check.cmd}\n{status}\n{run.tail}".rstrip())
    return "\n\n".join(parts)
