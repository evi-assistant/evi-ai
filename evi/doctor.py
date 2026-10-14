"""`evi doctor` — one-shot environment diagnostic.

Checks the things that commonly go wrong on a fresh box: is `~/.evi/`
writable, does config.toml parse, is the configured LLM backend
reachable, which optional extras are installed, and are the external
binaries (git, tesseract, a TTS engine) on PATH. Pure inspection — never
mutates anything.

`evi doctor --dev` (:func:`run_dev_checks`) checks a development checkout
instead: the venv, git safe.directory, ruff / pytest / Playwright, and
working-tree line endings against .gitattributes. It prints fix commands but
never runs them. Stdlib-only, like the rest of this module.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from evi.config import HOME, CONFIG_PATH


# status is one of: "ok" | "warn" | "fail"
@dataclass
class Check:
    name: str
    status: str
    detail: str
    # Commands that would fix it, printed under the check. Never run by doctor.
    fix: tuple[str, ...] = ()


# Optional Python deps grouped by the extra that ships them. (import_name,
# extra, what-it-powers).
_OPTIONAL_DEPS: list[tuple[str, str, str]] = [
    ("fastapi", "web", "web UI server"),
    ("uvicorn", "web", "web UI server"),
    ("faster_whisper", "stt", "speech-to-text"),
    ("sounddevice", "stt", "mic capture / voice loop"),
    ("mcp", "mcp", "Model Context Protocol tools"),
    ("icalendar", "calendar", "calendar reading"),
    ("caldav", "calendar", "CalDAV calendars"),
    ("fitz", "pdf", "PDF text extraction"),
    ("numpy", "index", "semantic file search"),
    ("ddgs", "web-tools", "web search"),
    ("bs4", "web-tools", "web fetch / HTML parsing"),
    ("pyautogui", "computer", "computer-use control"),
    ("huggingface_hub", "downloads", "model downloads"),
    ("apscheduler", "scheduler", "scheduled tasks"),
    ("prompt_toolkit", "(core)", "REPL tab completion"),
]

# External binaries: (binary, why, hard?). hard=True → "fail" when missing,
# else "warn" (the feature is optional).
_BINARIES: list[tuple[str, str, bool]] = [
    ("git", "git tools + worktrees", False),
    ("tesseract", "OCR tool", False),
]


def _have_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _check_home() -> list[Check]:
    out: list[Check] = []
    if HOME.is_dir():
        # Probe writability with a temp file.
        try:
            probe = HOME / ".doctor_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            out.append(Check("~/.evi writable", "ok", str(HOME)))
        except OSError as exc:
            out.append(Check("~/.evi writable", "fail", f"{HOME}: {exc}"))
    else:
        out.append(Check("~/.evi exists", "warn", f"{HOME} not created yet (run any command once)"))
    return out


def _check_config() -> list[Check]:
    if not CONFIG_PATH.is_file():
        return [Check("config.toml", "warn", f"{CONFIG_PATH} missing (defaults will be written)")]
    try:
        from evi.config import Config

        cfg = Config.load()
        return [Check(
            "config.toml",
            "ok",
            f"backend={cfg.llm.backend} model={cfg.llm.model}",
        )]
    except Exception as exc:  # noqa: BLE001
        return [Check("config.toml", "fail", f"parse error: {type(exc).__name__}: {exc}")]


def _check_backend() -> list[Check]:
    try:
        from evi.config import Config

        cfg = Config.load()
    except Exception:  # noqa: BLE001
        return [Check("backend reachable", "warn", "skipped (config did not load)")]
    base = (cfg.llm.base_url or "").rstrip("/")
    if not base:
        return [Check("backend reachable", "warn", "no base_url configured")]
    url = base + "/models"
    try:
        import httpx

        r = httpx.get(url, timeout=2.5)
        if r.status_code < 500:
            return [Check("backend reachable", "ok", f"{base} (HTTP {r.status_code})")]
        return [Check("backend reachable", "warn", f"{base} returned HTTP {r.status_code}")]
    except Exception as exc:  # noqa: BLE001
        return [Check(
            "backend reachable",
            "fail",
            f"{base} unreachable ({type(exc).__name__}) — is the server running?",
        )]


def _check_optional_deps() -> list[Check]:
    out: list[Check] = []
    for import_name, extra, what in _OPTIONAL_DEPS:
        if _have_module(import_name):
            out.append(Check(f"dep: {import_name}", "ok", what))
        else:
            hint = "core dep missing!" if extra == "(core)" else f"pip install 'evi-assistant[{extra}]'"
            status = "fail" if extra == "(core)" else "warn"
            out.append(Check(f"dep: {import_name}", status, f"{what} — {hint}"))
    return out


def _check_binaries() -> list[Check]:
    out: list[Check] = []
    for binary, why, hard in _BINARIES:
        if shutil.which(binary):
            out.append(Check(f"bin: {binary}", "ok", why))
        else:
            out.append(Check(f"bin: {binary}", "fail" if hard else "warn", f"{why} — not on PATH"))
    # TTS backend (platform-specific) is its own check.
    try:
        from evi.voice import detect_backend

        backend = detect_backend()
        if backend == "none":
            out.append(Check("TTS backend", "warn", "no TTS engine (install espeak-ng on Linux)"))
        else:
            out.append(Check("TTS backend", "ok", backend))
    except Exception:  # noqa: BLE001
        out.append(Check("TTS backend", "warn", "could not detect"))
    return out


def _check_hardware() -> list[Check]:
    try:
        from evi.hardware import detect

        hw = detect()
        ram = f"{hw.ram_total_gb:.1f} GB RAM"
        if hw.gpus:
            g = hw.gpus[0]
            gpu = f"{g.name} ({g.vram_total_mb} MB VRAM)"
        else:
            gpu = "no NVIDIA GPU detected"
        return [Check("hardware", "ok", f"{ram} · {gpu}")]
    except Exception as exc:  # noqa: BLE001
        return [Check("hardware", "warn", f"detection failed: {type(exc).__name__}")]


def _check_federation() -> list[Check]:
    """When federation serving is enabled, verify this machine is actually
    reachable BY peers — the loopback-bind trap (desktop < 0.2.15 launches the
    sidecar with --host 127.0.0.1, so `bind_lan` never takes effect)."""
    from evi.config import Config

    try:
        cfg = Config.load()
    except Exception:
        return []
    fed = getattr(cfg, "federation", None)
    if fed is None or not getattr(fed, "serve", False):
        return []  # not serving — nothing to verify

    from evi.federation import DEFAULT_PEER_PORT, self_serving_status

    st = self_serving_status(DEFAULT_PEER_PORT, serve=True)
    status, lan_ip = st["status"], st["lan_ip"]
    if status == "lan":
        return [Check("federation reachable", "ok",
                      f"peers can reach this node at {lan_ip}:{st['port']}")]
    if status == "loopback":
        bl = getattr(fed, "bind_lan", False)
        hint = (
            "running server is bound to 127.0.0.1 only — reinstall desktop "
            "0.2.15+ or relaunch `evi web --host 0.0.0.0` so bind_lan takes "
            "effect"
        ) if bl else (
            "set [federation] bind_lan = true and relaunch on 0.0.0.0 so peers "
            "can reach this node"
        )
        return [Check("federation reachable", "warn",
                      f"serving but loopback-only — {hint} (+ open inbound TCP "
                      f"{st['port']} in the firewall)")]
    if status == "down":
        return [Check("federation reachable", "warn",
                      f"[federation] serve is on but nothing is listening on "
                      f"{st['port']} (start `evi web`)")]
    return []


def run_checks() -> list[Check]:
    """Run every diagnostic and return the flat list of results."""
    checks: list[Check] = []
    checks += _check_home()
    checks += _check_config()
    checks += _check_backend()
    checks += _check_hardware()
    checks += _check_binaries()
    checks += _check_optional_deps()
    checks += _check_federation()
    return checks


def summarize(checks: list[Check]) -> tuple[int, int, int]:
    """Return (ok, warn, fail) counts."""
    ok = sum(1 for c in checks if c.status == "ok")
    warn = sum(1 for c in checks if c.status == "warn")
    fail = sum(1 for c in checks if c.status == "fail")
    return ok, warn, fail


# --- developer checks (`evi doctor --dev`) ---------------------------------
#
# The development-checkout counterpart of run_checks(). scripts/dev-bootstrap
# runs it last (`python -m evi doctor --dev --strict`), so everything here must
# work with a bare venv: stdlib only, and every external tool is probed through
# a subprocess of the venv's own python.
#
# Pointed at a checkout eVi does not come from, it runs none of its code: git
# runs hardened (evi/gitsafe.py) and never through a command that would invoke
# the checkout's clean filters (see _edited_beyond_eol), the interpreter probe
# runs isolated (`python -I`), and `pytest --collect-only`, which imports the
# checkout's conftest.py, runs only for the eVi checkout in use unless asked
# (`--collect`).

DEV_MIN_PYTHON = (3, 12)

# Venvs a checkout may carry. pyvenv.cfg records the interpreter of the machine
# that created it, so on a checkout shared between machines (a network share)
# each one is "foreign" everywhere else.
_REPO_VENVS = (".venv", ".venv-build")

# Paths per printed fix command, so one line never hits a shell's length limit.
_FIX_CHUNK = 40

# An argument printed unquoted in a fix command. No `,` or `@` (PowerShell reads
# them as an array or a splat), no glob, quote, `$` or space.
_PLAIN_ARG = re.compile(r"[\w./-]+")
# Characters a single-quoted argument cannot carry in both bash and PowerShell:
# quotes (PowerShell also takes the typographic single quotes as quotes, and
# Windows PowerShell 5.1 does not escape `"` for native commands) and controls.
_UNQUOTABLE = re.compile("['\"\u2018\u2019\u201a\u201b\\x00-\\x1f\\x7f]")


def _shell_arg(s: str) -> str | None:
    """`s` as one argument that bash and PowerShell both read verbatim, or
    None when no spelling does (it contains a quote)."""
    if _PLAIN_ARG.fullmatch(s):
        return s
    if _UNQUOTABLE.search(s):
        return None
    return f"'{s}'"


def _shell_exe(path: str | Path) -> str | None:
    """An executable path as the first word of a fix command. A quoted string
    there is an expression in PowerShell, so it takes `&`, and bash does not."""
    s = Path(path).as_posix()
    if re.fullmatch(r"[\w./:-]+", s):
        return s  # C:/x/python.exe: bash and PowerShell both run it as is
    q = _shell_arg(s)
    if q is None:
        return None
    return f"& {q}" if os.name == "nt" else q


def _bootstrap_fix(venv: Path | None = None, *flags: str) -> tuple[str, ...]:
    """The dev-bootstrap command for `venv` (default: the bootstrap's own
    default venv) with bash-style `flags` such as "recreate"."""
    if os.name == "nt":
        words = ["powershell -ExecutionPolicy Bypass -File scripts\\dev-bootstrap.ps1"]
        opt = {"venv": "-Venv", "recreate": "-Recreate", "reinstall": "-Reinstall"}
    else:
        words = ["bash scripts/dev-bootstrap.sh"]
        opt = {"venv": "--venv", "recreate": "--recreate", "reinstall": "--reinstall"}
    if venv is not None:
        arg = _shell_arg(str(venv))
        if arg is None:
            return ()  # a name no shell-neutral spelling carries: no command to print
        words += [opt["venv"], arg]
    words += [opt[f] for f in flags]
    return (" ".join(words),)


def _same_path(a: str | Path, b: str | Path) -> bool:
    # Compare both spellings: as given, and fully resolved. Resolving alone is
    # not enough when only one side exists; as-given alone misses a mapped
    # drive vs its UNC path, or an 8.3 short name (C:\Users\CLAUDE~1) vs the
    # long one.
    for norm in (os.path.abspath, os.path.realpath):
        try:
            if os.path.normcase(norm(a)) == os.path.normcase(norm(b)):
                return True
        except (OSError, ValueError):
            pass
    return False


def find_repo_root(start: Path) -> Path | None:
    """Nearest ancestor of `start` holding `.git` (a directory, or the file a
    worktree has). Walks the filesystem instead of asking git, which refuses a
    checkout it does not trust (safe.directory)."""
    here = Path(os.path.abspath(start))
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
    return None


def read_pyvenv_cfg(venv: Path) -> dict[str, str] | None:
    """`venv/pyvenv.cfg` as a lower-cased key -> value dict, or None."""
    try:
        text = (Path(venv) / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip().lower()] = value.strip()
    return out


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return Path(venv) / "Scripts" / "python.exe"
    return Path(venv) / "bin" / "python"


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text or "")[:3])


def _venv_created_at(cfg: dict[str, str]) -> str | None:
    """The target path from pyvenv.cfg's `command` (`python -m venv <path>`).
    uv writes no `command`, so None there.

    The stdlib writes the options, then the path, joined by spaces and
    unquoted (the path may hold spaces), with the prompt as `--prompt="..."`.
    So the options are dropped from the front, and the rest is the path."""
    m = re.search(r"-m\s+venv\s+(.+)$", cfg.get("command", ""))
    if not m:
        return None
    rest = m.group(1).strip()
    while opt := re.match(r'--prompt="[^"]*"\s*|--\S+\s*', rest):
        rest = rest[opt.end():]
    return rest.strip() or None


def inspect_venv(venv: Path, *, in_use: bool = True) -> Check:
    """Is `venv` usable on THIS machine? Reads pyvenv.cfg and the files it
    names; never runs the interpreter. `in_use=False` (a venv the checkout
    carries but this process isn't using) downgrades a failure to a warning:
    on a shared checkout it may be another machine's working venv."""
    name = f"venv: {venv}"
    bad = "fail" if in_use else "warn"
    note = "" if in_use else (
        " (not the venv in use; leave it if another machine uses this checkout)"
    )
    # The bootstrap repairs the venv you use; it never deletes a checkout's
    # other venvs, so only an in-use venv gets it as the fix. It replaces a
    # venv unasked only when the venv is plainly foreign and outside any
    # checkout; every other replacement needs --recreate, so the printed
    # command carries it when the bootstrap would ask for it.
    in_repo = find_repo_root(venv) is not None  # by walking the path, not a prefix test

    def fix(*, home_exists: bool) -> tuple[str, ...]:
        if not in_use:
            return ()
        return _bootstrap_fix(venv, *(("recreate",) if home_exists or in_repo else ()))

    cfg = read_pyvenv_cfg(venv)
    if cfg is None:
        # The bootstrap refuses to touch a directory that is not a venv.
        return Check(name, bad, "no pyvenv.cfg: not a virtualenv (point --venv at "
                     "another directory)" + note)
    home = cfg.get("home", "")
    if not home or not Path(home).is_dir():
        origin = cfg.get("command") or cfg.get("executable") or home or "?"
        return Check(
            name, bad,
            f"foreign or broken venv: its base interpreter ({home or 'unset'}) does "
            f"not exist on this machine (created by: {origin}){note}",
            fix(home_exists=False),
        )
    exe = venv_python(venv)
    if not exe.exists():
        return Check(name, bad, f"broken venv: {exe} is missing{note}",
                     fix(home_exists=True))
    ver = _version_tuple(cfg.get("version_info") or cfg.get("version") or "")
    shown = ".".join(map(str, ver)) or "?"
    if ver and ver[:2] < DEV_MIN_PYTHON:
        return Check(name, bad, f"Python {shown} is too old (eVi needs 3.12+){note}",
                     fix(home_exists=True))
    created = _venv_created_at(cfg)
    if created and not _same_path(created, venv):
        # Its python still starts, so the bootstrap would reuse it as is: no
        # command fixes this short of recreating the venv by hand.
        return Check(
            name, "warn",
            f"created at {created} and since moved or shared; its scripts "
            f"(evi, pytest, activate) still point there, so use `python -m ...` "
            f"or recreate it{note}",
        )
    return Check(name, "ok", f"Python {shown} ({home})")


def _git(repo: Path, *args: str, timeout: float = 60,
         stdin: bytes | None = None) -> subprocess.CompletedProcess | None:
    from evi import gitsafe

    # Hardened like every git call eVi makes for itself: the checkout may not
    # be one eVi came from, and its .git/config (core.fsmonitor, say) must not
    # run a program during a read. GIT_OPTIONAL_LOCKS=0 (from git_env) keeps
    # doctor read-only; English messages, because safe_directory_fix matches
    # git's wording. Bytes out: `-z` lists are decoded by the callers.
    env = {**gitsafe.git_env(), "LC_ALL": "C", "LANGUAGE": "C"}
    try:
        argv = gitsafe.git_argv(["-C", str(repo), *args])
        return subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout,
                              env=env)
    except (OSError, subprocess.TimeoutExpired):  # GitNotFoundError is an OSError
        return None


def safe_directory_fix(stderr: str) -> str | None:
    """The `git config --global --add safe.directory ...` command git prints
    when it refuses a checkout ("dubious ownership"), or None if it didn't."""
    if "dubious ownership" not in stderr:
        return None
    m = re.search(r"git config --global --add safe\.directory .+", stderr)
    if m:
        return m.group(0).strip()
    return "git config --global --add safe.directory <checkout path>"


def _check_safe_directory(repo: Path) -> Check:
    name = "git safe.directory"
    r = _git(repo, "rev-parse", "--show-toplevel")
    if r is None:
        return Check(name, "fail", "git is not on PATH (or did not respond)")
    if r.returncode == 0:
        return Check(name, "ok", "git trusts this checkout")
    err = r.stderr.decode("utf-8", "replace")
    fix = safe_directory_fix(err)
    if fix:
        return Check(
            name, "fail",
            "git refuses this checkout: it is owned by another account "
            "(usual for a network share). Add it once:",
            (fix,),
        )
    first = (err.strip().splitlines() or ["no output"])[0]
    return Check(name, "fail", f"git rev-parse failed: {first}")


def _check_interpreter(repo: Path) -> list[Check]:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    out: list[Check] = []
    if (v.major, v.minor) < DEV_MIN_PYTHON:
        out.append(Check("python", "fail", f"{sys.executable} is Python {ver}; eVi needs 3.12+",
                         _bootstrap_fix()))
    elif sys.prefix == sys.base_prefix:
        out.append(Check("python", "warn", f"{sys.executable} (Python {ver}) is not a venv",
                         _bootstrap_fix()))
    else:
        out.append(Check("python", "ok", f"{sys.executable} (Python {ver})"))
    pkg_root = _evi_root()
    if _same_path(pkg_root, repo):
        out.append(Check("evi import", "ok", "evi is imported from this checkout"))
    else:
        out.append(Check(
            "evi import", "warn",
            f"evi is imported from {pkg_root}, not {repo}. In a worktree run "
            "`python -m evi` / `python -m pytest` from the worktree (the venv's "
            "editable install points at the main checkout)",
        ))
    return out


def _evi_root() -> Path:
    """The directory holding the `evi` package this process runs."""
    import evi

    return Path(os.path.abspath(evi.__file__)).parent.parent


def _git_common_dir(path: Path) -> Path | None:
    r = _git(path, "rev-parse", "--git-common-dir")
    if r is None or r.returncode != 0:
        return None
    out = r.stdout.decode("utf-8", "replace").strip()
    if not out:
        return None
    p = Path(out)
    return p if p.is_absolute() else Path(path) / p


def is_evi_checkout(repo: Path) -> bool:
    """`repo` is the checkout the running eVi is imported from, or another
    worktree of that repository. Collecting its tests imports its conftest.py,
    which is then code from the repository eVi already runs from, not from
    whatever directory doctor was pointed at."""
    pkg_root = _evi_root()
    if _same_path(pkg_root, repo):
        return True
    if not (pkg_root / ".git").exists():
        return False  # eVi is not running from a checkout (a regular install)
    mine, theirs = _git_common_dir(pkg_root), _git_common_dir(repo)
    return mine is not None and theirs is not None and _same_path(mine, theirs)


_PROBE_MODULES = ("pytest", "pytest_timeout", "ruff", "xdist", "playwright", "pip")
_PROBE = (
    "import importlib.util as u, json, sys; print(json.dumps({'version': "
    "list(sys.version_info[:3]), 'mods': {m: u.find_spec(m) is not None for m in %r}}))"
)


def _probe_python(python: str) -> dict | None:
    """Python version + which dev modules `python` can import, or None when it
    doesn't start (the symptom of a foreign venv).

    Isolated (`-I`: no current directory or PYTHON* variables on sys.path) and
    started outside the checkout, so a json.py or importlib/ in the directory
    doctor runs from is never imported."""
    try:
        r = subprocess.run(
            [python, "-I", "-c", _PROBE % (_PROBE_MODULES,)],
            capture_output=True, text=True, timeout=60,
            cwd=os.path.dirname(os.path.abspath(python)) or None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def _playwright_browsers_dir() -> Path | None:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env == "0":
        return None  # browsers live inside the package; nothing to check here
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "ms-playwright" if base else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def _check_collect(python: str, repo: Path) -> Check:
    name = "pytest --collect-only"
    try:
        r = subprocess.run(
            [python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600,
        )
    except subprocess.TimeoutExpired:
        return Check(name, "fail", "timed out after 600 s")
    except OSError as exc:
        return Check(name, "fail", f"could not run pytest: {exc}")
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    summary = lines[-1] if lines else "(no output)"
    if r.returncode == 0:
        return Check(name, "ok", summary)
    if r.returncode == 5:
        return Check(name, "warn", f"no tests collected in {repo}")
    py, where = _shell_exe(python), _shell_arg(str(repo))
    fix = (f"cd {where}; {py} -m pytest --collect-only -q",) if py and where else ()
    return Check(name, "fail", f"exit {r.returncode}: {summary}", fix)


def _install_fix(python: str, venv: Path | None, has_pip: bool, *args: str) -> str | None:
    """`pip install <args>` aimed at `python`, the interpreter being checked
    (a bare `python` is whatever comes first on PATH). A uv venv has no pip,
    so that takes `uv pip install --python`; failing both, the bootstrap."""
    py, quoted = _shell_exe(python), [_shell_arg(a) for a in args]
    if py is not None and None not in quoted:
        if has_pip:
            return f"{py} -m pip install {' '.join(quoted)}"
        if shutil.which("uv"):
            target = _shell_arg(Path(python).as_posix())
            return f"uv pip install --python {target} {' '.join(quoted)}"
    return next(iter(_bootstrap_fix(venv, "reinstall")), None)


def _check_dev_tools(python: str, repo: Path, *, collect: bool | None,
                     venv: Path | None = None) -> list[Check]:
    info = _probe_python(python)
    if info is None:
        return [Check("dev python", "fail", f"{python} does not start", _bootstrap_fix(venv))]
    mods = info.get("mods", {})
    has_pip = bool(mods.get("pip"))

    def install(*args: str) -> tuple[str, ...]:
        cmd = _install_fix(python, venv, has_pip, *args)
        return (cmd,) if cmd else ()

    def editable(extra: str) -> tuple[str, ...]:
        return install("-e", f"{repo.as_posix()}[{extra}]")

    py = _shell_exe(python)
    browsers = (f"{py} -m playwright install chromium",) if py else ()
    out: list[Check] = []
    missing = [m for m in ("pytest", "pytest_timeout", "ruff") if not mods.get(m)]
    if missing:
        out.append(Check("dev tools", "fail", f"missing: {', '.join(missing)}",
                         editable("dev")))
    else:
        out.append(Check("dev tools", "ok", "pytest, pytest-timeout, ruff"))
    if mods.get("xdist"):
        out.append(Check("pytest-xdist", "ok", "`-n auto` available"))
    else:
        out.append(Check("pytest-xdist", "warn", "not installed, so no `-n auto`",
                         install("pytest-xdist")))
    if not mods.get("playwright"):
        out.append(Check(
            "playwright", "warn", "e2e extra not installed: tests/e2e will skip",
            editable("e2e") + browsers,
        ))
    else:
        bdir = _playwright_browsers_dir()
        if bdir is None:
            out.append(Check("playwright", "ok", "PLAYWRIGHT_BROWSERS_PATH=0 (bundled browsers)"))
        elif bdir.is_dir() and any(p.name.startswith("chromium") for p in bdir.iterdir()):
            out.append(Check("playwright", "ok", f"chromium in {bdir}"))
        else:
            out.append(Check("playwright", "warn", f"no chromium in {bdir}: tests/e2e will fail",
                             browsers))
    if collect is False or not mods.get("pytest"):
        return out
    if collect is None and not is_evi_checkout(repo):
        # Collection imports the checkout's conftest.py and test modules, so
        # a checkout eVi does not come from is only collected when asked.
        out.append(Check(
            "pytest --collect-only", "warn",
            f"skipped: {repo} is not the eVi checkout in use (evi runs from "
            f"{_evi_root()}), and collecting runs its conftest.py. Pass --collect "
            "to run it anyway",
        ))
        return out
    out.append(_check_collect(python, repo))
    return out


def _git_paths(repo: Path, *args: str) -> set[str] | None:
    r = _git(repo, *args)
    if r is None or r.returncode != 0:
        return None
    return {p for p in r.stdout.decode("utf-8", "replace").split("\0") if p}


def _edited_beyond_eol(repo: Path, paths: list[str]) -> set[str] | None:
    """The `paths` whose working file differs from its index blob in more than
    line endings, or None when git cannot say.

    Compared here rather than with `git diff --name-only`, which runs the
    checkout's `filter.<driver>.clean` programs on any file whose stat data
    changed. `ls-files -s` and `cat-file --batch` read the index and the
    object store only."""
    r = _git(repo, "ls-files", "-s", "-z")
    if r is None or r.returncode != 0:
        return None
    blob_of: dict[str, str] = {}
    for rec in r.stdout.decode("utf-8", "replace").split("\0"):
        info, tab, path = rec.partition("\t")  # "<mode> <sha> <stage>\t<path>"
        fields = info.split()
        if tab and len(fields) == 3 and fields[2] == "0":
            blob_of[path] = fields[1]
    wanted = sorted({blob_of[p] for p in paths if p in blob_of})
    r = _git(repo, "cat-file", "--batch", stdin="".join(f"{sha}\n" for sha in wanted).encode())
    if r is None or r.returncode != 0:
        return None
    blobs: dict[str, bytes] = {}
    out, pos = r.stdout, 0
    while pos < len(out):
        end = out.find(b"\n", pos)
        if end < 0:
            break
        header = out[pos:end].decode("ascii", "replace").split()
        pos = end + 1
        if len(header) != 3:  # "<sha> missing"
            continue
        size = int(header[2])
        blobs[header[0]] = out[pos:pos + size]
        pos += size + 1  # the content, then a newline

    def same(path: str) -> bool:
        blob = blobs.get(blob_of.get(path, ""))
        try:
            data = (repo / path).read_bytes()
        except OSError:
            return False
        return blob is not None and (
            data.replace(b"\r\n", b"\n") == blob.replace(b"\r\n", b"\n"))

    return {p for p in paths if not same(p)}


def _eol_wants(attrs: list[str]) -> str | None:
    """The working-tree EOL .gitattributes asks for, or None (binary, or left
    to core.autocrlf / core.eol)."""
    if "-text" in attrs or "binary" in attrs:
        return None
    if "eol=lf" in attrs:
        return "lf"
    if "eol=crlf" in attrs:
        return "crlf"
    return None


def _first(paths: list[str], n: int = 5) -> str:
    more = f" (+{len(paths) - n} more)" if len(paths) > n else ""
    return ", ".join(paths[:n]) + more


def check_eol(repo: Path) -> list[Check]:
    """Compare every tracked file's line endings with .gitattributes.

    Two problems, two fixes, both printed and never applied:
    - a working file disagrees (CRLF where eol=lf, say): re-check it out.
      `git checkout` alone skips a file whose stat data looks clean, which is
      the state an editor plus `git add` leaves behind, so the command drops
      the index entry first. Files with local edits are listed apart, because
      a re-checkout would discard the edits. The paths are literal pathspecs
      (`[ab].md` must not also match an edited a.md), and a name no
      shell-neutral quoting carries (one with a quote in it) is listed for a
      fix by hand instead of being put in the command;
    - the index itself holds CRLF for a text file: `git add --renormalize .`.
    """
    name = "line endings"
    r = _git(repo, "ls-files", "--eol", "-z")
    if r is None or r.returncode != 0:
        return [Check(name, "warn", "skipped: `git ls-files --eol` failed")]
    worktree_bad: list[tuple[str, str, str]] = []  # (path, have, want)
    index_bad: list[str] = []
    total = 0
    for rec in r.stdout.decode("utf-8", "replace").split("\0"):
        # "i/lf    w/crlf  attr/text=auto eol=lf \t<path>"
        info, tab, path = rec.partition("\t")
        if not tab or not path:
            continue
        total += 1
        fields = info.split()
        if len(fields) < 3 or not fields[2].startswith("attr/"):
            continue
        idx, wt = fields[0][2:], fields[1][2:]
        attrs = [fields[2][5:], *fields[3:]]
        textual = (
            "-text" not in attrs and "binary" not in attrs
            and any(a in ("text", "text=auto") or a.startswith("eol=") for a in attrs)
        )
        if textual and idx in ("crlf", "mixed"):
            index_bad.append(path)
            continue
        want = _eol_wants(attrs)
        if want is None or idx == "-text" or wt not in ("lf", "crlf", "mixed"):
            continue
        if wt != want:
            worktree_bad.append((path, wt, want))

    out: list[Check] = []
    if index_bad:
        out.append(Check(
            "line endings (index)", "warn",
            f"{len(index_bad)} file(s) are stored with CRLF in the index despite "
            f".gitattributes: {_first(index_bad)}. Renormalize, review, then commit:",
            ("git add --renormalize .",),
        ))
    if worktree_bad:
        paths = [p for p, _, _ in worktree_bad]
        unstaged = _edited_beyond_eol(repo, paths)
        staged = _git_paths(repo, "diff", "--cached", "--name-only", "-z")
        if unstaged is None or staged is None:
            dirty = set(paths)  # can't tell: assume edits
        else:
            dirty = unstaged | staged
        clean = [p for p, _, _ in worktree_bad if p not in dirty]
        edited = [p for p, _, _ in worktree_bad if p in dirty]
        quoted = [(p, _shell_arg(p)) for p in clean]
        by_hand = [p for p, q in quoted if q is None]
        args = [q for _, q in quoted if q is not None]
        fixes: list[str] = []
        for i in range(0, len(args), _FIX_CHUNK):
            paths = " ".join(args[i:i + _FIX_CHUNK])
            fixes.append(f"git --literal-pathspecs rm --cached -q -- {paths}; "
                         f"git --literal-pathspecs checkout HEAD -- {paths}")
        path0, have, want = worktree_bad[0]
        detail = (
            f"{len(worktree_bad)} working file(s) disagree with .gitattributes "
            f"(e.g. {path0} is {have.upper()}, wants {want.upper()})."
        )
        if edited:
            detail += (
                f" {len(edited)} also have local edits ({_first(edited)}): commit or "
                "stash them first (a re-checkout would discard the edits), or convert "
                "them in place."
            )
        if by_hand:
            detail += (
                f" {len(by_hand)} have a quote in the name ({_first(by_hand)}): "
                "re-check those out by hand."
            )
        if args:
            detail += f" Re-check out the {len(args)} unmodified one(s):"
        out.append(Check(name, "warn", detail, tuple(fixes)))
    if not out:
        out.append(Check(name, "ok", f"{total} tracked files match .gitattributes"))
    return out


def run_dev_checks(
    repo: Path | None = None,
    *,
    venv: Path | None = None,
    collect: bool | None = None,
) -> list[Check]:
    """Check a development checkout. `repo` defaults to the checkout around the
    current directory; `venv` is the venv to validate (default: the one running
    this process). `pytest --collect-only` runs for the eVi checkout in use
    (collect=None), always (True) or never (False)."""
    start = Path.cwd() if repo is None else Path(repo)
    root = find_repo_root(start)
    if root is None:
        return [Check("checkout", "fail", f"{start} is not inside a git checkout (pass --repo)")]
    checks = [Check("checkout", "ok", str(root))]
    checks += _check_interpreter(root)

    target = Path(venv) if venv is not None else (
        Path(sys.prefix) if sys.prefix != sys.base_prefix else None
    )
    python = sys.executable
    usable = True
    if target is not None:
        vc = inspect_venv(target, in_use=True)
        checks.append(vc)
        usable = vc.status != "fail"
        if venv is not None and usable:
            python = str(venv_python(target))
    for name in _REPO_VENVS:
        p = root / name
        if p.is_dir() and (target is None or not _same_path(p, target)):
            checks.append(inspect_venv(p, in_use=False))

    safe = _check_safe_directory(root)
    checks.append(safe)
    if usable:
        checks += _check_dev_tools(python, root, collect=collect, venv=target)
    else:
        checks.append(Check("dev tools", "warn", "skipped: the venv is not usable"))
    if safe.status == "ok":
        checks += check_eol(root)
    else:
        checks.append(Check("line endings", "warn",
                            "skipped: git does not trust this checkout (see safe.directory)"))
    return checks
