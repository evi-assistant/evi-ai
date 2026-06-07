"""PDF tools — extract text from a local PDF.

Backed by PyMuPDF (`fitz`), shipped as the `evi[pdf]` extra. We import
lazily so users who never use the tool aren't forced to install a heavy
binary dep.

`read_pdf(path, pages=None)`:
- `pages` is None ⇒ extract every page (capped at 32 KB of text total).
- `pages="1-5"` ⇒ a contiguous range, inclusive.
- `pages="3"` ⇒ a single page.

Pages are 1-indexed in the public API (matches what people see in a PDF
viewer) but PyMuPDF is 0-indexed internally — we handle the conversion.
"""

from __future__ import annotations

import re

from evi.tools.base import tool


_MAX_OUTPUT_BYTES = 32 * 1024
_PAGE_SPEC_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")


def _import_fitz():
    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "PDF reading requires PyMuPDF — install with: pip install 'evi-assistant[pdf]'"
        ) from exc
    return fitz


def _parse_page_spec(spec: str, total: int) -> list[int]:
    """Return a 0-indexed list of page numbers from a `1-5` / `3` spec."""
    m = _PAGE_SPEC_RE.match(spec)
    if not m:
        raise ValueError(f"invalid page spec {spec!r} — use '3' or '1-5'")
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if start < 1 or end < start:
        raise ValueError(f"invalid page range {spec!r}")
    end = min(end, total)
    return list(range(start - 1, end))


@tool(
    description=(
        "Extract text from a local PDF file. `pages` is optional: omit for "
        "the whole document, use '3' for a single page, or '2-7' for a "
        "range. Output is capped at ~32 KB of text — for big PDFs, "
        "request specific pages."
    ),
    category="pdf",
)
def read_pdf(path: str, pages: str = "") -> str:
    try:
        fitz = _import_fitz()
    except RuntimeError as exc:
        return f"ERROR: {exc}"

    try:
        doc = fitz.open(path)
    except Exception as exc:
        return f"ERROR: failed to open PDF: {type(exc).__name__}: {exc}"

    try:
        total = doc.page_count
        if pages.strip():
            try:
                indices = _parse_page_spec(pages, total)
            except ValueError as exc:
                return f"ERROR: {exc}"
        else:
            indices = list(range(total))

        parts: list[str] = []
        total_bytes = 0
        for i in indices:
            page = doc.load_page(i)
            text = page.get_text("text") or ""
            chunk = f"--- page {i + 1} ---\n{text.strip()}\n"
            if total_bytes + len(chunk) > _MAX_OUTPUT_BYTES:
                parts.append(
                    f"… ({len(indices) - len(parts)} more page(s) elided; "
                    "request a narrower range)"
                )
                break
            parts.append(chunk)
            total_bytes += len(chunk)
    finally:
        doc.close()

    return "\n".join(parts).strip() or "(no text extracted)"
