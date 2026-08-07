"""Vision support — attach images to chat turns for VLM-capable backends.

OpenAI's vision schema is the de-facto standard across local backends:

    {
        "role": "user",
        "content": [
            {"type": "text", "text": "what's in this image?"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,..."},
            },
        ],
    }

Both LM Studio (with vision models like Qwen2.5-VL or Llama-3.2-Vision)
and Ollama (with `llava`, `minicpm-v`, `qwen2.5-vl`) accept this shape.
llama.cpp's mtmd-cli and llama-server (vision build) speak it too.

This module handles:

- `model_supports_vision(name)` — heuristic by model id, since none of the
  backends report capabilities via API.
- `build_image_content(text, image_paths)` — read files, infer mime,
  base64-encode, return the content list.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Iterable


# Substrings in a model id that indicate vision capability. Conservative
# on purpose; users with weird custom names can call `build_image_content`
# directly and bypass the check.
_VISION_HINTS = (
    "vl",          # qwen2.5-vl, qwen-vl, internvl, intern-vl, deepseek-vl
    "vision",      # llama-3.2-11b-vision, etc.
    "llava",
    "minicpm-v",
    "minicpm-o",
    "moondream",
    "bakllava",
    "cogvlm",
    "phi-3-vision",
    "phi-3.5-vision",
    "phi-vision",
    "pixtral",
    "molmo",
    "florence",
)


def model_supports_vision(model_id: str) -> bool:
    """Heuristic: does this model id look like a VLM?"""
    if not model_id:
        return False
    name = model_id.lower()
    return any(hint in name for hint in _VISION_HINTS)


_IMAGE_MIME_FALLBACK = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _data_url_for(path: Path) -> str:
    """Read `path` and return a `data:<mime>;base64,<...>` URL."""
    suffix = path.suffix.lower()
    mime, _ = mimetypes.guess_type(path.as_posix())
    if not mime:
        mime = _IMAGE_MIME_FALLBACK.get(suffix, "application/octet-stream")
    data = path.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def build_image_content(text: str, image_paths: Iterable[str | Path]) -> list[dict]:
    """Return an OpenAI-style multipart content list for one user message.

    Skips paths that don't exist, so a stale upload reference doesn't blow
    up the whole turn. Always returns at least the text part.
    """
    parts: list[dict] = [{"type": "text", "text": text}]
    for raw in image_paths:
        p = Path(raw).expanduser()
        if not p.is_file():
            continue
        try:
            url = _data_url_for(p)
        except OSError:
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


# Minimum OCR characters before we treat an image as "text-heavy" and fold in
# the extracted text — a photo yields empty/garbage, a screenshot or document
# yields real substance. Kept low so a short label (a sign, a code) still counts.
_OCR_MIN_CHARS = 8


def describe_for_fallback(image_paths: Iterable[str | Path], *, ocr: bool = True) -> str:
    """Describe attached images as text for a NON-vision chat model.

    The visual analogue of ``audio_input.transcribe_for_fallback``: when the
    active chat model can't see, run the vision specialty (``describe_image`` ->
    the ``[models] vision`` VLM) so "what is this?" still works, and — when the
    image holds real text — also OCR it (``ocr_image``) and fold the extracted
    text in. Never raises: vision/OCR being unconfigured must not break a turn.

    Returns a text block, or "" if nothing at all could be produced (so the
    caller can decide whether to still surface the raw paths).
    """
    blocks: list[str] = []
    for raw in image_paths:
        p = Path(raw).expanduser()
        if not p.is_file():
            blocks.append(f"[image {raw}: file not found]")
            continue
        found: list[str] = []
        # 1) General description via the vision specialty model.
        try:
            from evi.tools.vision_tool import describe_image

            desc = describe_image(str(p))
            if desc and not desc.startswith("ERROR"):
                found.append(desc.strip())
        except Exception:  # noqa: BLE001  (missing dep / backend error)
            pass
        # 2) OCR — only included when it actually finds substantive text, so a
        #    plain photo doesn't get a garbage "Text:" block appended.
        if ocr:
            try:
                from evi.tools.ocr import ocr_image

                text = ocr_image(str(p))
                if (
                    text
                    and not text.startswith("ERROR")
                    and len(text.strip()) >= _OCR_MIN_CHARS
                ):
                    found.append("Text found in the image:\n" + text.strip())
            except Exception:  # noqa: BLE001
                pass
        if found:
            blocks.append(f"[attached image {p.name}]\n" + "\n\n".join(found))
        else:
            blocks.append(
                f"[attached image {p.name}: couldn't analyze it — set a vision "
                "model ([models] vision, e.g. llava) or install Tesseract for OCR]"
            )
    return "\n\n".join(blocks)
