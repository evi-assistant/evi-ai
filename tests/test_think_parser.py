"""Tests for the <think>…</think> streaming splitter inside the agent."""

from __future__ import annotations

from evi.llm.agent import _ThinkParser


def _drain(p: _ThinkParser, chunks: list[str]) -> tuple[str, str]:
    """Feed each chunk + flush; return cumulative (visible, thinking)."""
    visible = ""
    thinking = ""
    for c in chunks:
        v, t = p.feed(c)
        visible += v
        thinking += t
    v, t = p.flush()
    visible += v
    thinking += t
    return visible, thinking


def test_plain_text_no_tags() -> None:
    p = _ThinkParser()
    v, t = _drain(p, ["hello ", "world"])
    assert v == "hello world"
    assert t == ""


def test_single_chunk_with_block() -> None:
    p = _ThinkParser()
    v, t = _drain(p, ["before <think>secret</think> after"])
    assert v == "before  after"
    assert t == "secret"


def test_tag_split_across_chunks() -> None:
    """The <think> tag straddles two stream chunks; parser must hold off."""
    p = _ThinkParser()
    v, t = _drain(p, ["before <thi", "nk>inside</thi", "nk> after"])
    assert v == "before  after"
    assert t == "inside"


def test_multiple_blocks() -> None:
    p = _ThinkParser()
    v, t = _drain(
        p,
        ["A <think>X</think> B <think>Y</think> C"],
    )
    assert v == "A  B  C"
    assert t == "XY"


def test_unterminated_think_flushed_as_thinking() -> None:
    """An open <think> with no close at end-of-stream falls into thinking."""
    p = _ThinkParser()
    v, t = _drain(p, ["before <think>", "still thinking…"])
    assert v == "before "
    assert t == "still thinking…"


def test_close_only_no_open() -> None:
    """A stray </think> with no opener should be passed through as visible."""
    p = _ThinkParser()
    v, t = _drain(p, ["odd </think> tag"])
    # No state ever flipped to in_think, so the close tag is just text.
    assert "</think>" in v
    assert t == ""
