"""`evi doctor` — one-shot environment diagnostic.

Checks the things that commonly go wrong on a fresh box: is `~/.evi/`
writable, does config.toml parse, is the configured LLM backend
reachable, which optional extras are installed, and are the external
binaries (git, tesseract, a TTS engine) on PATH. Pure inspection — never
mutates anything.
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass

from evi.config import HOME, CONFIG_PATH


# status is one of: "ok" | "warn" | "fail"
@dataclass
class Check:
    name: str
    status: str
    detail: str


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
    ("duckduckgo_search", "web-tools", "web search"),
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
            hint = "core dep missing!" if extra == "(core)" else f"pip install 'evi-ai[{extra}]'"
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


def run_checks() -> list[Check]:
    """Run every diagnostic and return the flat list of results."""
    checks: list[Check] = []
    checks += _check_home()
    checks += _check_config()
    checks += _check_backend()
    checks += _check_hardware()
    checks += _check_binaries()
    checks += _check_optional_deps()
    return checks


def summarize(checks: list[Check]) -> tuple[int, int, int]:
    """Return (ok, warn, fail) counts."""
    ok = sum(1 for c in checks if c.status == "ok")
    warn = sum(1 for c in checks if c.status == "warn")
    fail = sum(1 for c in checks if c.status == "fail")
    return ok, warn, fail
