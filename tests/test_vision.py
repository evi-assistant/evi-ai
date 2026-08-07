"""Tests for vision detection + content-building helpers."""

from __future__ import annotations

import base64
from pathlib import Path

from evi.vision import build_image_content, describe_for_fallback, model_supports_vision


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


# ---- describe_for_fallback (vision/OCR for a non-VLM chat model) ----------


def _img(tmp_path: Path, name: str = "pic.png") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n")  # any bytes — describe/ocr are mocked
    return p


def test_describe_for_fallback_folds_vision_and_ocr(tmp_path, monkeypatch) -> None:
    """A text-heavy image gets BOTH the vision description and the OCR text."""
    p = _img(tmp_path)
    monkeypatch.setattr(
        "evi.tools.vision_tool.describe_image",
        lambda path, *a, **k: "a screenshot of a login form",
    )
    monkeypatch.setattr(
        "evi.tools.ocr.ocr_image",
        lambda path, *a, **k: "Username: admin\nPassword: hunter2",
    )
    out = describe_for_fallback([p])
    assert "a screenshot of a login form" in out
    assert "Text found in the image:" in out
    assert "hunter2" in out
    assert p.name in out


def test_describe_for_fallback_skips_ocr_when_not_text_heavy(tmp_path, monkeypatch) -> None:
    """A plain photo (no real OCR text) gets the description only — no garbage
    'Text found' block appended."""
    p = _img(tmp_path)
    monkeypatch.setattr(
        "evi.tools.vision_tool.describe_image", lambda path, *a, **k: "a photo of a cat"
    )
    monkeypatch.setattr("evi.tools.ocr.ocr_image", lambda path, *a, **k: "  \n ")
    out = describe_for_fallback([p])
    assert "a photo of a cat" in out
    assert "Text found in the image:" not in out


def test_describe_for_fallback_ignores_tool_errors_but_keeps_ocr(tmp_path, monkeypatch) -> None:
    """Vision model unset (ERROR) but OCR still carries the result; the ERROR
    string is never leaked into the folded text."""
    p = _img(tmp_path)
    monkeypatch.setattr(
        "evi.tools.vision_tool.describe_image",
        lambda path, *a, **k: "ERROR: no vision model available",
    )
    monkeypatch.setattr(
        "evi.tools.ocr.ocr_image", lambda path, *a, **k: "INVOICE #4471 total $52.10"
    )
    out = describe_for_fallback([p])
    assert "ERROR" not in out
    assert "INVOICE #4471" in out


def test_describe_for_fallback_note_when_nothing_available(tmp_path, monkeypatch) -> None:
    """No vision model + no OCR -> a helpful note (not a crash, not raw paths)."""
    p = _img(tmp_path)
    monkeypatch.setattr(
        "evi.tools.vision_tool.describe_image",
        lambda path, *a, **k: "ERROR: no vision model available",
    )
    monkeypatch.setattr(
        "evi.tools.ocr.ocr_image", lambda path, *a, **k: "ERROR: tesseract not found"
    )
    out = describe_for_fallback([p])
    assert "couldn't analyze" in out
    assert "vision model" in out


def test_describe_for_fallback_missing_file(tmp_path) -> None:
    out = describe_for_fallback([tmp_path / "nope.png"])
    assert "file not found" in out


def test_describe_for_fallback_never_raises(tmp_path, monkeypatch) -> None:
    """A tool raising must not break the turn — swallowed into the note."""
    p = _img(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr("evi.tools.vision_tool.describe_image", boom)
    monkeypatch.setattr("evi.tools.ocr.ocr_image", boom)
    out = describe_for_fallback([p])  # must not raise
    assert "couldn't analyze" in out
