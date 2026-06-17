"""Document layout / OCR — structure-aware extraction for PDFs & scanned docs.

eVi's existing ``[pdf]`` path (PyMuPDF) pulls a PDF's embedded text, and
``evi/tools/ocr.py`` OCRs a single image (tesseract or a VLM). Neither recovers
*layout* — reading order, tables, headings, multi-column flow — and neither
handles a scanned (image-only) PDF well. Docling does: it runs layout analysis +
OCR and emits clean Markdown.

Heavy (torch-backed models), so it's an optional extra —
``pip install 'evi-assistant[doc]'``. Lazy, cached converter (mirrors
``evi/moderation.py`` / ``evi/diarize.py``); raises ``DocLayoutError`` when the
deps are missing so callers can fall back to PyMuPDF/tesseract.
"""

from __future__ import annotations

from typing import Any

_CONVERTERS: dict[str, Any] = {}


class DocLayoutError(Exception):
    """Document-layout deps aren't available."""


def have_doclayout() -> bool:
    """True if docling is importable (deps installed)."""
    try:
        import docling  # type: ignore[import-not-found]  # noqa: F401
    except Exception:
        return False
    return True


def _converter(model_id: str = ""):
    key = model_id or "default"
    if key in _CONVERTERS:
        return _CONVERTERS[key]
    try:
        from docling.document_converter import (  # type: ignore[import-not-found]
            DocumentConverter,
        )
    except ImportError as exc:
        raise DocLayoutError(
            "document layout/OCR needs docling — "
            "install with: pip install 'evi-assistant[doc]'"
        ) from exc
    try:
        conv = DocumentConverter()
    except Exception as exc:
        raise DocLayoutError(f"could not initialise docling: {exc}") from exc
    _CONVERTERS[key] = conv
    return conv


def extract_document(path: str, model_id: str = "") -> str:
    """Convert a PDF / image / office doc at `path` to layout-aware Markdown.

    Raises DocLayoutError if docling isn't installed or the conversion fails."""
    conv = _converter(model_id)
    try:
        result = conv.convert(path)
        return result.document.export_to_markdown()
    except DocLayoutError:
        raise
    except Exception as exc:
        raise DocLayoutError(f"docling failed on {path!r}: {exc}") from exc


def reset_for_tests() -> None:
    _CONVERTERS.clear()
