"""OCR tools — extract text from images via Tesseract.

We shell out to the `tesseract` binary directly rather than depending on
`pytesseract`. It's a single subprocess call and removes a Python dep
that just wraps the same binary. Users still need Tesseract installed:

- Windows: `winget install UB-Mannheim.TesseractOCR` (or chocolatey)
- macOS:   `brew install tesseract`
- Linux:   `sudo apt install tesseract-ocr`  (or distro equivalent)

The `ocr_screen()` tool composes nicely with `screenshot` from the
computer-use category: take a shot, read the text. The agent can do that
with two calls today; ocr_screen() is just the convenience version.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from evi.tools.base import tool


_TIMEOUT_SECONDS = 30.0
_MAX_OUTPUT_BYTES = 32 * 1024


def _tesseract_cmd() -> str | None:
    """Resolve the tesseract binary. Order:

    1. `$EVI_TESSERACT_CMD` — an explicit path. The desktop bundle sets this
       to its bundled binary; power users can point at any install.
    2. `~/.evi/tools/bin/tesseract[.exe]` — what `evi-tools install
       tesseract` drops in (its download fallback).
    3. `tesseract` on PATH.

    Returns the command (path or bare name) or None if nothing's found.
    """
    explicit = os.environ.get("EVI_TESSERACT_CMD", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit

    home = Path(os.environ.get("EVI_HOME") or (Path.home() / ".evi"))
    exe = "tesseract.exe" if os.name == "nt" else "tesseract"
    local = home / "tools" / "bin" / exe
    if local.is_file():
        return str(local)

    return shutil.which("tesseract")


def _tesseract_available() -> bool:
    return _tesseract_cmd() is not None


def _run_tesseract(image: Path, language: str) -> str:
    """Invoke tesseract on `image` and return its stdout (the OCR text).

    Raises `RuntimeError` if tesseract isn't installed or the run fails.
    """
    cmd = _tesseract_cmd()
    if cmd is None:
        raise RuntimeError(
            "tesseract not found. Install it with `evi-tools install "
            "tesseract`, or: `winget install UB-Mannheim.TesseractOCR` "
            "(Windows), `brew install tesseract` (macOS), "
            "`apt install tesseract-ocr` (Linux). You can also point "
            "$EVI_TESSERACT_CMD at an existing binary."
        )
    if not image.is_file():
        raise RuntimeError(f"no such file: {image}")
    try:
        proc = subprocess.run(
            [cmd, str(image), "stdout", "-l", language],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"tesseract timed out after {_TIMEOUT_SECONDS}s")
    if proc.returncode != 0:
        # Tesseract often writes warnings to stderr even on success.
        err = (proc.stderr or "").strip() or "(no stderr)"
        raise RuntimeError(f"tesseract exit={proc.returncode}: {err[:400]}")
    return proc.stdout or ""


_VLM_OCR_PROMPT = (
    "Transcribe ALL text in this image exactly. Preserve the layout and "
    "structure as Markdown (tables as Markdown tables, formulas as LaTeX). "
    "Output only the transcribed content — no commentary."
)


def _ocr_via_vlm(image: Path) -> str | None:
    """OCR via the configured [models] ocr VLM, or None if unconfigured.
    Raises RuntimeError on a backend failure so the caller can fall back."""
    from evi.llm.specialty import load_registry

    reg = load_registry()
    if not reg.model_id("ocr"):
        return None
    try:
        return reg.run_image("ocr", image, _VLM_OCR_PROMPT)
    except Exception as exc:  # noqa: BLE001 — surface as a fallable error
        raise RuntimeError(f"OCR VLM ({reg.model_id('ocr')}) failed: {exc}") from exc


def _clip_ocr(text: str) -> str:
    text = (text or "").strip()
    if len(text) > _MAX_OUTPUT_BYTES:
        text = text[:_MAX_OUTPUT_BYTES] + "\n…(truncated)"
    return text or "(no text recognised)"


@tool(
    description=(
        "Extract text from an image file. By default uses the configured OCR "
        "specialty VLM ([models] ocr, e.g. glm-ocr / qwen2.5vl) when set — "
        "which preserves layout/tables/formulas as Markdown — and otherwise "
        "Tesseract. `engine`: 'auto' (default) | 'vlm' | 'tesseract'. "
        "`language` is an ISO 639-2 code for Tesseract (default `eng`)."
    ),
    category="ocr",
)
def ocr_image(path: str, language: str = "eng", engine: str = "auto") -> str:
    target = Path(path).expanduser()
    engine = (engine or "auto").strip().lower()
    exists = target.is_file()

    # VLM path — only on a real file; otherwise fall through so tesseract emits
    # the canonical missing-file / install-hint errors (preserving precedence).
    if engine in ("auto", "vlm") and exists:
        try:
            vlm = _ocr_via_vlm(target)
        except RuntimeError as exc:
            if engine == "vlm":
                return f"ERROR: {exc}"
            vlm = None  # auto → fall through to tesseract
        if vlm is not None:
            return _clip_ocr(vlm)
        if engine == "vlm":
            return "ERROR: no OCR VLM configured — set [models] ocr (e.g. glm-ocr)"
    if engine == "vlm" and not exists:
        return f"ERROR: no such file: {target}"

    try:
        text = _run_tesseract(target, language)
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    return _clip_ocr(text)


@tool(
    description=(
        "Take a fresh screenshot of the primary display and OCR it. "
        "Requires both `[computer]` and `[ocr]` toolchains to be ready. "
        "Returns the recognised text."
    ),
    category="ocr",
)
def ocr_screen(language: str = "eng") -> str:
    # Lazy-import so we don't force pyautogui on users who only want ocr_image.
    try:
        from evi.tools.computer import _import_pyautogui
        from evi.config import SCREENSHOT_DIR, ensure_dirs
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    try:
        pg = _import_pyautogui()
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    from datetime import datetime

    ensure_dirs()
    target = SCREENSHOT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_ocr.png"
    pg.screenshot().save(str(target))
    try:
        text = _run_tesseract(target, language)
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    text = text.strip()
    if len(text) > _MAX_OUTPUT_BYTES:
        text = text[:_MAX_OUTPUT_BYTES] + "\n…(truncated)"
    return text or "(no text recognised)"
