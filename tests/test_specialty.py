"""Tests for the specialty-model registry + OCR/vision routing."""

from __future__ import annotations

from evi.config import LLMSettings, SpecialtyModels
from evi.llm.specialty import SpecialtyRegistry


class _FakeResp:
    def __init__(self, text):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


class _FakeCompletions:
    def __init__(self, captured):
        self._captured = captured

    def create(self, **kwargs):
        self._captured.update(kwargs)
        return _FakeResp("TRANSCRIBED")


class _FakeClient:
    def __init__(self, captured):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(captured)})()


def test_model_id_reads_config():
    reg = SpecialtyRegistry(LLMSettings(), SpecialtyModels(ocr="glm-ocr"))
    assert reg.model_id("ocr") == "glm-ocr"
    assert reg.model_id("vision") == ""


def test_client_for_unconfigured_is_none():
    reg = SpecialtyRegistry(LLMSettings(), SpecialtyModels())
    assert reg.client_for("ocr") is None


def test_client_for_builds_and_caches(monkeypatch):
    captured = {}
    built = []

    def fake_make_client(settings):
        built.append(settings.model)
        return _FakeClient(captured)

    monkeypatch.setattr("evi.llm.client.make_client", fake_make_client)
    reg = SpecialtyRegistry(LLMSettings(model="main"), SpecialtyModels(ocr="glm-ocr"))
    c1 = reg.client_for("ocr")
    c2 = reg.client_for("ocr")
    assert c1 is c2  # cached
    assert built == ["glm-ocr"]  # built once, with the specialty id (not "main")


def test_run_image_sends_image_and_returns_text(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("evi.llm.client.make_client", lambda s: _FakeClient(captured))
    # build_image_content skips non-existent paths but always includes the text part
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n")
    reg = SpecialtyRegistry(LLMSettings(), SpecialtyModels(ocr="glm-ocr"))
    out = reg.run_image("ocr", img, "read it")
    assert out == "TRANSCRIBED"
    assert captured["model"] == "glm-ocr"
    assert captured["temperature"] == 0.0


def test_ocr_image_falls_back_to_tesseract_when_no_vlm(monkeypatch, tmp_path):
    # No [models] ocr configured -> _ocr_via_vlm returns None -> tesseract path.
    import evi.tools.ocr as ocrmod

    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(ocrmod, "_run_tesseract", lambda p, lang: "hello from tesseract")
    monkeypatch.setattr("evi.llm.specialty.load_registry",
                        lambda: SpecialtyRegistry(LLMSettings(), SpecialtyModels()))
    assert ocrmod.ocr_image(str(img)) == "hello from tesseract"


def test_ocr_image_uses_vlm_when_configured(monkeypatch, tmp_path):
    import evi.tools.ocr as ocrmod

    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n")

    class _Reg(SpecialtyRegistry):
        def run_image(self, task, image_path, prompt, *, max_tokens=4096):
            return "## markdown from vlm"

    monkeypatch.setattr(
        "evi.llm.specialty.load_registry",
        lambda: _Reg(LLMSettings(), SpecialtyModels(ocr="glm-ocr")),
    )
    out = ocrmod.ocr_image(str(img))
    assert out == "## markdown from vlm"


def test_ocr_image_engine_vlm_errors_when_unconfigured(monkeypatch, tmp_path):
    import evi.tools.ocr as ocrmod

    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr("evi.llm.specialty.load_registry",
                        lambda: SpecialtyRegistry(LLMSettings(), SpecialtyModels()))
    out = ocrmod.ocr_image(str(img), engine="vlm")
    assert out.startswith("ERROR") and "no OCR VLM" in out


def test_describe_image_errors_without_vision_model(monkeypatch, tmp_path):
    import evi.tools.vision_tool as vt

    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n")
    # main model is a non-VLM, no vision specialty -> clear error
    monkeypatch.setattr(
        "evi.llm.specialty.load_registry",
        lambda: SpecialtyRegistry(LLMSettings(model="qwen2.5:14b"), SpecialtyModels()),
    )
    out = vt.describe_image(str(img))
    assert out.startswith("ERROR") and "no vision model" in out


def test_describe_image_uses_specialty(monkeypatch, tmp_path):
    import evi.tools.vision_tool as vt

    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n")

    class _Reg(SpecialtyRegistry):
        def run_image(self, task, image_path, prompt, *, max_tokens=4096):
            return "a cat on a mat"

    monkeypatch.setattr(
        "evi.llm.specialty.load_registry",
        lambda: _Reg(LLMSettings(), SpecialtyModels(vision="moondream")),
    )
    assert vt.describe_image(str(img)) == "a cat on a mat"
