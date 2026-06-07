"""Tests for conversation export (markdown / html / json)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from evi.sessions import (
    export_html,
    export_json,
    export_markdown,
    export_session,
)


def _seed_session(root: Path, day: str, sid: str) -> Path:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    now = time.time()
    rows = [
        {"role": "user", "content": "hi there", "ts": now},
        {"role": "assistant", "content": "**hello**", "ts": now + 1,
         "tool_calls": [{"function": {"name": "echo", "arguments": "{}"}}]},
        {"role": "tool", "content": "echo result", "tool_name": "echo", "ts": now + 2},
        {"role": "assistant", "content": "all done", "ts": now + 3},
        {"role": "system", "content": "internal note", "ts": now},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return f


def test_markdown_export(tmp_path: Path) -> None:
    f = _seed_session(tmp_path, "2026-05-27", "s1")
    md = export_markdown(f)
    # Header + metadata block.
    assert "Session s1" in md
    # All non-system messages rendered.
    assert "hi there" in md
    assert "**hello**" in md
    assert "echo result" in md
    assert "all done" in md
    # System messages omitted.
    assert "internal note" not in md
    # Tool call line surfaced.
    assert "echo(" in md


def test_json_export_preserves_entries(tmp_path: Path) -> None:
    f = _seed_session(tmp_path, "2026-05-27", "s2")
    raw = export_json(f)
    data = json.loads(raw)
    assert len(data) == 5
    assert data[0]["content"] == "hi there"


def test_html_export_wraps_in_doc(tmp_path: Path) -> None:
    f = _seed_session(tmp_path, "2026-05-27", "s3")
    html = export_html(f)
    assert html.startswith("<!doctype html>")
    assert "<title>eVi session s3</title>" in html
    # Bold markdown surfaces as <strong>.
    assert "<strong>hello</strong>" in html
    # Tool result in a code block.
    assert "echo result" in html


def test_export_session_unknown_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        export_session("does-not-exist", root=tmp_path)


def test_export_session_unknown_format(tmp_path: Path) -> None:
    _seed_session(tmp_path, "2026-05-27", "s4")
    with pytest.raises(ValueError):
        export_session("s4", fmt="pdf", root=tmp_path)


def test_export_session_dispatches(tmp_path: Path) -> None:
    _seed_session(tmp_path, "2026-05-27", "s5")
    md = export_session("s5", fmt="md", root=tmp_path)
    js = export_session("s5", fmt="json", root=tmp_path)
    ht = export_session("s5", fmt="html", root=tmp_path)
    assert md.startswith("# Session s5")
    assert js.startswith("[")
    assert ht.startswith("<!doctype html>")
