"""Tests for vision detection + content-building helpers."""

from __future__ import annotations

import base64
from pathlib import Path

from evi.vision import build_image_content, model_supports_vision


def test_model_supports_vision_known_names() -> None:
    assert model_supports_vision("qwen2.5-vl-7b-instruct") is True
    assert model_supports_vision("llama-3.2-11b-vision-instruct") is True
    assert model_supports_vision("llava:13b") is True
    assert model_supports_vision("minicpm-v") is True
    assert model_supports_vision("moondream") is True
    assert model_supports_vision("pixtral-12b") is True


def test_model_supports_vision_false_for_text_only() -> None:
    assert model_supports_vision("qwen2.5-7b-instruct") is False
    assert model_supports_vision("llama-3.1-8b") is False
    assert model_supports_vision("") is False


def test_build_image_content_includes_text_first(tmp_path: Path) -> None:
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    parts = build_image_content("describe this", [img])
    assert parts[0] == {"type": "text", "text": "describe this"}
    assert parts[1]["type"] == "image_url"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(url.split(",", 1)[1])
    assert decoded == b"\x89PNG\r\n\x1a\nFAKE"


def test_build_image_content_skips_missing_files(tmp_path: Path) -> None:
    good = tmp_path / "good.png"
    good.write_bytes(b"\x89PNG")
    parts = build_image_content("hi", [good, tmp_path / "missing.png"])
    image_parts = [p for p in parts if p["type"] == "image_url"]
    assert len(image_parts) == 1


def test_build_image_content_text_only_when_no_images() -> None:
    parts = build_image_content("just text", [])
    assert parts == [{"type": "text", "text": "just text"}]


def test_build_image_content_mime_fallback(tmp_path: Path) -> None:
    """Files with unknown suffix should still encode (data URL falls back)."""
    weird = tmp_path / "image.unknown"
    weird.write_bytes(b"binary")
    parts = build_image_content("?", [weird])
    assert len(parts) == 2
    assert parts[1]["image_url"]["url"].startswith("data:application/octet-stream;base64,")
