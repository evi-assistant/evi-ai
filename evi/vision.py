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
