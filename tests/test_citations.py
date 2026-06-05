"""Tests for evi/citations.py + Tool.call_rich + tool-side citation emission."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


from evi.citations import Citation, ToolOutput, trim_excerpt
from evi.tools.base import REGISTRY, Tool


# ----- trim_excerpt ---------------------------------------------------------


def test_trim_excerpt_short() -> None:
    assert trim_excerpt("hello world") == "hello world"


def test_trim_excerpt_long_truncates() -> None:
    out = trim_excerpt("x" * 500, max_chars=100)
    assert len(out) == 100
    assert out.endswith("…")


def test_trim_excerpt_collapses_whitespace() -> None:
    assert trim_excerpt("a\nb\n\nc") == "a b c"
    assert trim_excerpt("   spaced   ") == "spaced"


def test_trim_excerpt_empty() -> None:
    assert trim_excerpt("") == ""
    assert trim_excerpt("   \n\n") == ""


# ----- Tool.call_rich wrapping ---------------------------------------------


def _register_tool(name: str, fn) -> Tool:
    """Manually register a tool for these tests, bypassing the decorator's
    type-hint inspection (we want explicit control)."""
    t = Tool(
        name=name,
        description="test",
        parameters={"type": "object", "properties": {}, "required": []},
        func=fn,
        category="test",
    )
    REGISTRY[name] = t
    return t


def test_call_rich_wraps_plain_string() -> None:
    t = _register_tool("_cr_str", lambda: "hello")
    out = t.call_rich("{}")
    assert isinstance(out, ToolOutput)
    assert out.text == "hello"
    assert out.citations == []


def test_call_rich_passes_tool_output_through() -> None:
    cit = Citation(id="1", source_type="file", source_id="/x.py", excerpt="def f")
    t = _register_tool(
        "_cr_rich",
        lambda: ToolOutput(text="rich", citations=[cit]),
    )
    out = t.call_rich("{}")
    assert out.text == "rich"
    assert out.citations == [cit]


def test_call_rich_jsonifies_dict_results() -> None:
    t = _register_tool("_cr_dict", lambda: {"ok": True, "n": 3})
    out = t.call_rich("{}")
    assert json.loads(out.text) == {"ok": True, "n": 3}
    assert out.citations == []


def test_call_rich_catches_exceptions() -> None:
    def boom():
        raise ValueError("nope")
    t = _register_tool("_cr_boom", boom)
    out = t.call_rich("{}")
    assert out.text.startswith("ERROR: ValueError")
    assert out.citations == []


def test_call_returns_str_for_back_compat() -> None:
    cit = Citation(id="1", source_type="file", source_id="/x", excerpt="ex")
    t = _register_tool(
        "_cr_bc", lambda: ToolOutput(text="visible", citations=[cit]),
    )
    # The legacy str-returning call() must NOT leak ToolOutput.
    out = t.call("{}")
    assert isinstance(out, str)
    assert out == "visible"


# ----- read_file emits a citation ------------------------------------------


def test_read_file_emits_one_citation(tmp_path: Path) -> None:
    import evi.tools.fs  # noqa: F401 — register

    p = tmp_path / "hello.py"
    body = "def hello():\n    return 'hi'\n"
    # write_bytes (not write_text) so we match the read_file decode shape
    # without Windows' \n → \r\n text-mode translation.
    p.write_bytes(body.encode("utf-8"))

    out = REGISTRY["read_file"].call_rich(json.dumps({"path": str(p)}))
    assert out.text == body
    assert len(out.citations) == 1
    c = out.citations[0]
    assert c.source_type == "file"
    assert c.source_id == str(p)
    # File has 2 lines (the trailing \n produces a final empty line in count).
    assert c.start == 1
    assert c.end >= 2


def test_read_file_error_path_has_no_citation(tmp_path: Path) -> None:
    out = REGISTRY["read_file"].call_rich(
        json.dumps({"path": str(tmp_path / "does-not-exist.py")})
    )
    assert "ERROR: not a file" in out.text
    assert out.citations == []


# ----- find_in_project emits per-hit citations -----------------------------


def test_find_in_project_emits_citations(tmp_path: Path) -> None:
    """Mock ProjectIndex.query → controlled hits; verify Citations come out
    one-per-hit with the right ids + line numbers."""
    import evi.tools.index as idx_mod  # noqa: F401

    class FakeChunk:
        def __init__(self, path, start, end, text):
            self.path = path
            self.start_line = start
            self.end_line = end
            self.text = text

    class FakeHit:
        def __init__(self, score, chunk):
            self.score = score
            self.chunk = chunk

    fake_hits = [
        FakeHit(0.95, FakeChunk("foo.py", 10, 25, "def foo():\n    pass\n")),
        FakeHit(0.81, FakeChunk("bar.py", 5, 12, "class Bar:\n    ...\n")),
    ]

    fake_idx = MagicMock()
    fake_idx.exists.return_value = True
    fake_idx.query.return_value = fake_hits

    with patch("evi.tools.index.ProjectIndex", return_value=fake_idx):
        out = REGISTRY["find_in_project"].call_rich(
            json.dumps({"query": "foo", "path": str(tmp_path), "k": 5})
        )

    assert isinstance(out, ToolOutput)
    payload = json.loads(out.text)
    assert len(payload) == 2
    assert payload[0]["path"] == "foo.py"
    assert len(out.citations) == 2
    assert out.citations[0].id == "1"
    assert out.citations[0].source_type == "index"
    assert out.citations[0].source_id == "foo.py"
    assert out.citations[0].start == 10
    assert out.citations[0].end == 25
    assert out.citations[1].id == "2"


# ----- ToolResult dataclass carries citations -----------------------------


def test_tool_result_event_carries_citations() -> None:
    from dataclasses import asdict

    from evi.llm.agent import ToolResult

    c = Citation(id="1", source_type="file", source_id="/x", excerpt="ex")
    ev = ToolResult(name="read_file", output="visible", citations=[c])
    payload = asdict(ev)
    assert payload["name"] == "read_file"
    assert payload["output"] == "visible"
    assert payload["citations"] == [
        {
            "id": "1",
            "source_type": "file",
            "source_id": "/x",
            "excerpt": "ex",
            "start": 0,
            "end": 0,
        }
    ]


def test_tool_result_default_citations_is_empty_list() -> None:
    from evi.llm.agent import ToolResult

    ev = ToolResult(name="x", output="y")
    assert ev.citations == []
    # Confirm it's a NEW list each time (default_factory, not shared state).
    ev2 = ToolResult(name="x", output="y")
    ev.citations.append(Citation(id="1", source_type="file", source_id="/a"))
    assert ev2.citations == []
