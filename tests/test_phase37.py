"""Tests for Phase 37 medium-value items: cache_prompt, logprobs,
audio input, guardrails."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evi.config import Config
from evi.llm.agent import Agent, Guardrail, LogProbs


# ---- shared fakes -------------------------------------------------------


class _Delta:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _LPItem:
    def __init__(self, token, logprob):
        self.token = token
        self.logprob = logprob


class _LP:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content="", finish="stop", logprobs=None):
        self.delta = _Delta(content=content)
        self.finish_reason = finish
        self.logprobs = logprobs


class _Chunk:
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


class _CapturingCompletions:
    def __init__(self, chunks=None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._chunks = chunks

    def create(self, **kwargs):
        record = dict(kwargs)
        # Snapshot the messages list NOW — the agent mutates self.history
        # (appends the assistant reply) after create() returns, and kwargs
        # holds it by reference.
        if "messages" in record:
            record["messages"] = list(record["messages"])
        self.calls.append(record)
        if self._chunks is not None:
            return iter(self._chunks)
        return iter([_Chunk(choices=[_Choice(content="ok", finish="stop")])])


def _make_agent(cfg=None, chunks=None, **kw):
    cc = _CapturingCompletions(chunks=chunks)
    client = type("C", (), {"chat": type("X", (), {"completions": cc})()})()
    return Agent(client=client, config=cfg or Config(), tools=[], **kw), cc


# ---- cache_prompt -------------------------------------------------------


def test_cache_prompt_default_off() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi"))
    assert "extra_body" not in cc.calls[0] or "cache_prompt" not in cc.calls[0].get("extra_body", {})


def test_cache_prompt_forwarded_when_on() -> None:
    cfg = Config()
    cfg.llm.cache_prompt = True
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0]["extra_body"]["cache_prompt"] is True


# ---- logprobs -----------------------------------------------------------


def test_logprobs_not_requested_by_default() -> None:
    agent, cc = _make_agent()
    list(agent.chat("hi"))
    assert "logprobs" not in cc.calls[0]


def test_logprobs_requested_and_summarised() -> None:
    cfg = Config()
    cfg.llm.logprobs = True
    cfg.llm.top_logprobs = 3
    chunks = [
        _Chunk(choices=[_Choice(
            content="hello",
            finish="stop",
            logprobs=_LP([_LPItem("hel", -0.1), _LPItem("lo", -3.5)]),
        )]),
    ]
    agent, cc = _make_agent(cfg, chunks=chunks)
    events = list(agent.chat("hi"))
    assert cc.calls[0]["logprobs"] is True
    assert cc.calls[0]["top_logprobs"] == 3
    lp = [e for e in events if isinstance(e, LogProbs)]
    assert len(lp) == 1
    assert lp[0].low_count == 1            # -3.5 is below -2.0
    assert lp[0].min_logprob == -3.5
    assert len(lp[0].tokens) == 2


# ---- audio input --------------------------------------------------------


def test_model_supports_audio_heuristic() -> None:
    from evi.audio_input import model_supports_audio

    assert model_supports_audio("qwen2.5-omni-7b")
    assert model_supports_audio("gpt-4o-audio-preview")
    assert not model_supports_audio("qwen2.5-7b-instruct")


def test_build_audio_content_shape(tmp_path: Path) -> None:
    from evi.audio_input import build_audio_content

    clip = tmp_path / "a.wav"
    clip.write_bytes(b"RIFFfake")
    parts = build_audio_content("what is said?", [str(clip)])
    assert parts[0] == {"type": "text", "text": "what is said?"}
    assert parts[1]["type"] == "input_audio"
    assert parts[1]["input_audio"]["format"] == "wav"
    assert parts[1]["input_audio"]["data"]  # base64 non-empty


def test_build_audio_content_skips_unknown_ext(tmp_path: Path) -> None:
    from evi.audio_input import build_audio_content

    clip = tmp_path / "a.xyz"
    clip.write_bytes(b"data")
    parts = build_audio_content("text", [str(clip)])
    assert len(parts) == 1  # only the text part


def test_chat_audio_native_builds_multipart(tmp_path: Path) -> None:
    clip = tmp_path / "a.wav"
    clip.write_bytes(b"RIFFfake")
    cfg = Config()
    cfg.llm.model = "qwen2.5-omni-7b"
    agent, cc = _make_agent(cfg)
    list(agent.chat("transcribe", audio=[str(clip)]))
    sent = cc.calls[0]["messages"][-1]["content"]
    assert isinstance(sent, list)
    assert any(p.get("type") == "input_audio" for p in sent)


def test_chat_audio_degrades_to_transcript(tmp_path: Path, monkeypatch) -> None:
    """Non-omni model → audio is transcribed and folded into the text."""
    import evi.llm.agent as agent_mod

    # agent.py binds the name at import, so patch it there (not in audio_input).
    monkeypatch.setattr(
        agent_mod, "transcribe_for_fallback",
        lambda paths: "[audio x transcript] hello there",
    )
    clip = tmp_path / "a.wav"
    clip.write_bytes(b"RIFFfake")
    cfg = Config()
    cfg.llm.model = "qwen2.5-7b-instruct"  # not omni
    agent, cc = _make_agent(cfg)
    list(agent.chat("what was said?", audio=[str(clip)]))
    sent = cc.calls[0]["messages"][-1]["content"]
    assert isinstance(sent, str)
    assert "hello there" in sent


# ---- guardrails ---------------------------------------------------------


def _write_guardrails(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "guardrails.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_guardrails_load_missing_is_disabled(tmp_path: Path) -> None:
    from evi.guardrails import Guardrails

    g = Guardrails.load(tmp_path / "nope.toml")
    assert g.enabled is False
    assert g.rules == []


def test_guardrails_block_input(tmp_path: Path) -> None:
    from evi.guardrails import Guardrails

    p = _write_guardrails(tmp_path, """
enabled = true
[[rule]]
name = "no-secrets"
pattern = "(?i)api[_-]?key"
action = "block"
applies_to = "input"
""")
    g = Guardrails.load(p)
    res = g.check("here is my API_KEY=123", "input")
    assert res.allowed is False
    assert "no-secrets" in res.blocked_by


def test_guardrails_redact(tmp_path: Path) -> None:
    from evi.guardrails import Guardrails

    p = _write_guardrails(tmp_path, """
enabled = true
[[rule]]
name = "emails"
pattern = "[\\\\w.+-]+@[\\\\w-]+\\\\.[\\\\w.-]+"
action = "redact"
applies_to = "both"
""")
    g = Guardrails.load(p)
    res = g.check("mail me at bob@example.com please", "output")
    assert res.allowed is True
    assert "emails" in res.redacted_by
    assert "bob@example.com" not in res.text
    assert "[REDACTED]" in res.text


def test_guardrails_direction_scoping(tmp_path: Path) -> None:
    from evi.guardrails import Guardrails

    p = _write_guardrails(tmp_path, """
enabled = true
[[rule]]
name = "out-only"
pattern = "secret"
action = "block"
applies_to = "output"
""")
    g = Guardrails.load(p)
    # input direction not covered → allowed
    assert g.check("the secret", "input").allowed is True
    # output direction covered → blocked
    assert g.check("the secret", "output").allowed is False


def test_agent_input_guardrail_blocks_turn(tmp_path: Path) -> None:
    from evi.guardrails import Guardrails

    p = _write_guardrails(tmp_path, """
enabled = true
[[rule]]
name = "blockword"
pattern = "forbidden"
action = "block"
applies_to = "input"
""")
    g = Guardrails.load(p)
    agent, cc = _make_agent(guardrails=g)
    events = list(agent.chat("this is forbidden content"))
    # No LLM call happened.
    assert cc.calls == []
    kinds = [type(e).__name__ for e in events]
    assert "Guardrail" in kinds
    assert "Done" in kinds
    gr = [e for e in events if isinstance(e, Guardrail)][0]
    assert gr.blocked is True
    assert gr.direction == "input"


def test_agent_input_guardrail_redacts_and_proceeds(tmp_path: Path) -> None:
    from evi.guardrails import Guardrails

    p = _write_guardrails(tmp_path, """
enabled = true
[[rule]]
name = "ssn"
pattern = "\\\\d{3}-\\\\d{2}-\\\\d{4}"
action = "redact"
applies_to = "input"
""")
    g = Guardrails.load(p)
    agent, cc = _make_agent(guardrails=g)
    list(agent.chat("my ssn is 123-45-6789 ok"))
    # LLM was called with the redacted text.
    sent = cc.calls[0]["messages"][-1]["content"]
    assert "123-45-6789" not in sent
    assert "[REDACTED]" in sent


def test_agent_output_guardrail_redacts_stored_reply(tmp_path: Path) -> None:
    from evi.guardrails import Guardrails

    p = _write_guardrails(tmp_path, """
enabled = true
[[rule]]
name = "phone"
pattern = "\\\\d{3}-\\\\d{4}"
action = "redact"
applies_to = "output"
""")
    g = Guardrails.load(p)
    chunks = [_Chunk(choices=[_Choice(content="call 555-1234 now", finish="stop")])]
    agent, cc = _make_agent(chunks=chunks, guardrails=g)
    events = list(agent.chat("give me the number"))
    # The assistant message stored in history is redacted.
    last = agent.history[-1]
    assert last["role"] == "assistant"
    assert "555-1234" not in last["content"]
    assert any(isinstance(e, Guardrail) and e.direction == "output" for e in events)
