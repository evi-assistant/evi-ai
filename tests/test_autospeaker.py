"""Tests for the streaming sentence splitter in AutoSpeaker.

We don't exercise the worker-thread → subprocess TTS path here (that's
platform-dependent). Instead we drive the splitter logic by intercepting
the queue and recording what would have been spoken."""

from __future__ import annotations


import pytest

import evi.voice as voice_mod
from evi.voice import AutoSpeaker, _clean_for_tts


# ---- _clean_for_tts -----------------------------------------------------


def test_clean_strips_code_fences() -> None:
    src = "Here is code:\n```python\nprint('hi')\n```\nThat was it."
    out = _clean_for_tts(src)
    assert "print" not in out
    assert "code block" in out


def test_clean_inline_code_replaced() -> None:
    src = "Try the `read_file` tool."
    out = _clean_for_tts(src)
    assert "read_file" not in out
    assert "code" in out


def test_clean_urls_replaced() -> None:
    src = "See https://example.com for details."
    out = _clean_for_tts(src)
    assert "example.com" not in out
    assert "link" in out


def test_clean_collapses_whitespace() -> None:
    out = _clean_for_tts("hello\n\n\nworld\t\tfriend")
    assert "  " not in out
    assert "hello" in out and "world" in out and "friend" in out


# ---- AutoSpeaker --------------------------------------------------------


@pytest.fixture
def speaker(monkeypatch: pytest.MonkeyPatch):
    """Build an AutoSpeaker but intercept the worker thread so we can
    inspect what would have been spoken without invoking subprocess TTS.
    """
    spoken: list[str] = []

    def fake_speak(text: str, *, rate=None, blocking=True) -> None:
        spoken.append(text)

    monkeypatch.setattr(voice_mod, "speak", fake_speak)
    sp = AutoSpeaker()
    yield sp, spoken
    sp.close()


def _wait_for_count(spoken: list[str], target: int, timeout: float = 2.0) -> None:
    """Spin until the worker has processed `target` items or we time out."""
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline and len(spoken) < target:
        _t.sleep(0.02)


def test_feeds_one_sentence_when_terminator_arrives(speaker) -> None:
    sp, spoken = speaker
    sp.feed("Hello there. ")
    _wait_for_count(spoken, 1)
    assert len(spoken) == 1
    assert "Hello there" in spoken[0]


def test_holds_partial_until_terminator(speaker) -> None:
    sp, spoken = speaker
    sp.feed("This is half")
    _wait_for_count(spoken, 1, timeout=0.3)
    # No terminator seen yet — nothing spoken.
    assert spoken == []
    sp.feed(" of a sentence. And here is another!")
    _wait_for_count(spoken, 2)
    # Both sentences should land.
    assert len(spoken) == 2
    assert "half of a sentence" in spoken[0]
    assert "another" in spoken[1]


def test_flush_emits_buffered_partial(speaker) -> None:
    sp, spoken = speaker
    sp.feed("No terminator yet")
    _wait_for_count(spoken, 1, timeout=0.3)
    assert spoken == []
    sp.flush()
    _wait_for_count(spoken, 1)
    assert "No terminator yet" in spoken[0]


def test_close_stops_further_feeds(speaker) -> None:
    sp, spoken = speaker
    sp.close()
    sp.feed("This should not speak.")
    # No way to wait reliably for "nothing happens"; give the worker a beat.
    import time as _t
    _t.sleep(0.1)
    assert spoken == []


def test_code_blocks_replaced_in_spoken_output(speaker) -> None:
    sp, spoken = speaker
    sp.feed("Run this code: ```python\nprint(1)\n```\n")
    sp.flush()
    _wait_for_count(spoken, 1)
    assert spoken, "expected at least one spoken chunk"
    full = " ".join(spoken)
    assert "print" not in full
    assert "code block" in full
