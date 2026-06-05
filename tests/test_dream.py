"""Tests for the dream engine — diff logic + end-to-end with stubbed subagent."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import evi.dream as dream_mod
from evi.dream import MemorySnapshot, diff_snapshots
from evi.memory import MemoryStore
from evi.transcripts import TranscriptStore


# ---- diff logic ---------------------------------------------------------


def test_diff_added_removed_changed() -> None:
    before = MemorySnapshot(entries={"a": "x", "b": "y", "c": "z"})
    after = MemorySnapshot(entries={"a": "x", "b": "Y!", "d": "new"})
    added, removed, changed = diff_snapshots(before, after)
    assert added == ["d"]
    assert removed == ["c"]
    assert changed == ["b"]


def test_diff_empty_before() -> None:
    added, removed, changed = diff_snapshots(
        MemorySnapshot(entries={}), MemorySnapshot(entries={"a": "x"})
    )
    assert added == ["a"]
    assert removed == [] and changed == []


# ---- _format_transcripts truncation ------------------------------------


def test_format_transcripts_truncates_long_messages() -> None:
    from evi.transcripts import TranscriptEntry

    e = TranscriptEntry(
        session="s",
        timestamp=time.time(),
        role="assistant",
        content="x" * 2000,
    )
    out = dream_mod._format_transcripts([e])
    assert "…(truncated)" in out


def test_format_transcripts_drops_system_messages() -> None:
    from evi.transcripts import TranscriptEntry

    sys_msg = TranscriptEntry(
        session="s", timestamp=time.time(), role="system", content="boring"
    )
    user_msg = TranscriptEntry(
        session="s", timestamp=time.time(), role="user", content="interesting"
    )
    out = dream_mod._format_transcripts([sys_msg, user_msg])
    assert "boring" not in out
    assert "interesting" in out


# ---- end-to-end with a stubbed subagent --------------------------------


def test_run_dream_writes_log_and_diffs_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: pre-seed memory + transcripts, stub run_subagent to mutate
    memory, verify the report captures the diff and writes a log."""
    mem_dir = tmp_path / "memory"
    trans_dir = tmp_path / "transcripts"
    log_dir = tmp_path / "logs"
    mem_dir.mkdir()
    log_dir.mkdir()

    memory = MemoryStore(root=mem_dir)
    transcripts = TranscriptStore(root=trans_dir)

    # Pre-existing memory the dream agent might modify.
    memory.write("old_fact", "user prefers light mode")

    # Some transcript content for the dream to "review".
    transcripts.write_message(
        session="s", role="user",
        content="actually I prefer dark mode now",
        timestamp=time.time(),
    )

    # Stub run_subagent to mutate memory the way a real dream would, then
    # return a summary string.
    def fake_run_subagent(**kwargs):
        # Verify we got the dream prompt + memory + fs categories.
        assert "dream agent" in kwargs["system_prompt"]
        assert kwargs["tool_categories"] == ("memory", "fs")
        memory.write("old_fact", "user prefers dark mode")  # change existing
        memory.write("new_fact", "user's project lives at C:/evi")  # add
        return "Updated preference; recorded new project path."

    monkeypatch.setattr(dream_mod, "run_subagent", fake_run_subagent)
    monkeypatch.setattr(dream_mod, "DREAM_LOG_DIR", log_dir)
    monkeypatch.setattr(dream_mod, "ensure_dirs", lambda: None)

    report = dream_mod.run_dream(
        hours=24, memory=memory, transcripts=transcripts
    )

    assert report.added == ["new_fact"]
    assert report.changed == ["old_fact"]
    assert report.removed == []
    assert report.log_path.is_file()
    body = report.log_path.read_text("utf-8")
    assert "Dream report" in body
    assert "+1" in str(len(report.added)) or "new_fact" in body
    assert "Updated preference" in body
