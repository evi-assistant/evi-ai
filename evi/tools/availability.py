"""Static per-tool-category availability probe for the Settings → Tools UI.

Answers "will this tool actually work in THIS install?" — which is distinct from
the ⚠ RISK icon (tools that act outside the sandbox) and from whether a tool is
toggled on. It exists because most tool modules register *unconditionally* and
import their heavy optional dependency LAZILY (that lazy-import property is
exactly what let web search ship broken in the frozen desktop app). So a tool
being present in the registry does NOT mean its dependency is installed — we must
probe the real gate for each category:

  * import gates — ``importlib.util.find_spec`` (we never import the module)
  * binary gates — the executable resolves on PATH / via the tool's own resolver

Only categories with a real *install* gate are reported. Always-available core
tools (fs, code, memory, …) are omitted, and so are categories whose availability
is a live-service or configuration concern (``image`` → ComfyUI running,
``vision`` → a configured model, ``gmail``/``outlook`` → a connected account) —
a static badge can't speak to those without false positives.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from typing import Any

# PyInstaller sets this on the frozen desktop sidecar. When frozen, "pip install
# the extra" is misleading (the bundled Python isn't user-writable), so we tailor
# the hint to say the tool simply isn't bundled in the desktop app.
_FROZEN = bool(getattr(sys, "frozen", False))


def _have_module(name: str) -> bool:
    """True if ``name`` is importable, WITHOUT importing it. A broken/partial
    install (find_spec raising) counts as unavailable rather than crashing."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False


# category -> (import module name, reason-when-missing, pip hint)
_IMPORT_GATES: dict[str, tuple[str, str, str]] = {
    "web": ("ddgs", "Web search needs the ddgs package.",
            "pip install 'evi-assistant[web-tools]'"),
    "voice": ("faster_whisper",
              "Speech-to-text isn't installed (text-to-speech still works).",
              "pip install 'evi-assistant[stt]'"),
    "computer": ("pyautogui",
                 "Computer control (mouse/keyboard) isn't installed.",
                 "pip install 'evi-assistant[computer]'"),
    "pdf": ("fitz", "PDF reading needs PyMuPDF.",
            "pip install 'evi-assistant[pdf]'"),
    "index": ("numpy", "The project vector index needs numpy.",
              "pip install 'evi-assistant[index]'"),
    "calendar": ("icalendar", "Calendar reading needs the calendar extra.",
                 "pip install 'evi-assistant[calendar]'"),
    "mcp": ("mcp", "MCP support (external tool servers) isn't installed.",
            "pip install 'evi-assistant[mcp]'"),
}


def _git_ok() -> bool:
    return shutil.which("git") is not None


def _tesseract_ok() -> bool:
    # Reuse OCR's own resolver so we honor $EVI_TESSERACT_CMD and the
    # `evi-tools install tesseract` drop-in dir, not just PATH.
    try:
        from evi.tools.ocr import _tesseract_available
        return _tesseract_available()
    except Exception:  # noqa: BLE001
        return shutil.which("tesseract") is not None


def _import_hint(pip_hint: str) -> str:
    if _FROZEN:
        return (f"Not bundled in the desktop app — use the pip install "
                f"({pip_hint}) to enable this tool.")
    return pip_hint


def tool_availability() -> dict[str, dict[str, Any]]:
    """Map of tool category → availability info, for categories with a real
    install gate whose dependency is currently MISSING. An absent key means
    "no install gate, or the gate is satisfied" (i.e. show no badge).

    Side-effect-free and fast (``find_spec`` + ``shutil.which``), so it's safe to
    compute on every Settings open.
    """
    out: dict[str, dict[str, Any]] = {}
    for cat, (mod, reason, pip_hint) in _IMPORT_GATES.items():
        if not _have_module(mod):
            out[cat] = {"available": False, "reason": reason,
                        "how_to_enable": _import_hint(pip_hint)}
    # Binary gates — called by name (not stored as function refs) so they reflect
    # the current module state and stay straightforward to test/patch.
    if not _git_ok():
        out["git"] = {"available": False, "reason": "Git isn't on your PATH.",
                      "how_to_enable": "Install Git and make sure `git` is on your PATH."}
    if not _tesseract_ok():
        out["ocr"] = {"available": False,
                      "reason": "The tesseract OCR engine isn't installed.",
                      "how_to_enable": "Install tesseract (winget / brew / apt), or "
                      "set a vision model under Specialty models."}
    return out
