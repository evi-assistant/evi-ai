"""Guard model + [[guard]] guardrail layer, diarize/doclayout graceful degradation."""

import pytest

from evi import diarize, doclayout, guardmodel
from evi.guardrails import GuardRule, Guardrails


# --- guard model verdict parsing ---------------------------------------------

def test_parse_verdict_llama_guard():
    assert guardmodel._parse_verdict("safe") == (True, "")
    allowed, reason = guardmodel._parse_verdict("unsafe\nS1,S10")
    assert allowed is False
    assert "S1" in reason


def test_parse_verdict_shieldgemma_and_unknown():
    assert guardmodel._parse_verdict("No")[0] is True
    assert guardmodel._parse_verdict("Yes")[0] is False
    assert guardmodel._parse_verdict("")[0] is True          # fail open
    assert guardmodel._parse_verdict("???")[0] is True       # fail open


def test_classify_safety_no_model_raises():
    with pytest.raises(guardmodel.GuardError):
        guardmodel.classify_safety("", "hello")


class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _FakeClient:
    def __init__(self, content):
        self._content = content
        self.chat = type("C", (), {"completions": self})()

    def create(self, **kwargs):  # noqa: D401 — mimics OpenAI client
        return type("R", (), {"choices": [_FakeChoice(self._content)]})()


def test_classify_safety_with_fake_client():
    class _Reg:
        def client_for(self, task):
            return _FakeClient("unsafe\nS2")

    allowed, reason = guardmodel.classify_safety("llama-guard3", "bad", registry=_Reg())
    assert allowed is False and "S2" in reason


# --- [[guard]] guardrail layer ------------------------------------------------

def test_guard_rule_loads_from_toml(tmp_path):
    p = tmp_path / "guardrails.toml"
    p.write_text(
        "[[guard]]\nname = 'safety'\nmodel = 'llama-guard3'\napplies_to = 'input'\n",
        encoding="utf-8",
    )
    g = Guardrails.load(p)
    assert g.enabled
    assert len(g.guard_rules) == 1
    assert g.guard_rules[0].model == "llama-guard3"
    assert g.guard_rules[0].applies_to == "input"


def test_guard_layer_blocks_via_guard_fn():
    g = Guardrails(rules=[], guard_rules=[GuardRule(name="safety", model="m")])

    def guard_fn(model, text, direction):
        return (False, "unsafe S1")

    res = g.check("anything", "input", guard_fn=guard_fn)
    assert not res.allowed
    assert "safety" in res.blocked_by


def test_guard_layer_fails_open_on_error():
    g = Guardrails(rules=[], guard_rules=[GuardRule(name="safety")])

    def guard_fn(model, text, direction):
        raise RuntimeError("model down")

    res = g.check("anything", "input", guard_fn=guard_fn)
    assert res.allowed  # a broken guard never wedges the turn


def test_guard_layer_skipped_when_no_fn():
    g = Guardrails(rules=[], guard_rules=[GuardRule(name="safety")])
    res = g.check("anything", "input")  # no guard_fn supplied
    assert res.allowed


# --- diarize / doclayout: graceful when heavy deps absent ---------------------

def test_diarize_have_flag_is_bool():
    assert isinstance(diarize.have_diarize(), bool)


def test_diarize_raises_when_deps_missing():
    if diarize.have_diarize():
        pytest.skip("pyannote.audio is installed in this env")
    with pytest.raises(diarize.DiarizeError):
        diarize.diarize("nope.wav", "pyannote/speaker-diarization-3.1")


def test_doclayout_have_flag_is_bool():
    assert isinstance(doclayout.have_doclayout(), bool)


def test_doclayout_raises_when_deps_missing():
    if doclayout.have_doclayout():
        pytest.skip("docling is installed in this env")
    with pytest.raises(doclayout.DocLayoutError):
        doclayout.extract_document("nope.pdf")
