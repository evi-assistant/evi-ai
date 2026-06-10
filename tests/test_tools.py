"""Tests for the tool framework and built-in tools."""

from __future__ import annotations

import json
from pathlib import Path

from evi.tools.base import REGISTRY, tool
import evi.tools.fs  # noqa: F401  register fs tools
import evi.tools.code  # noqa: F401  register code tools


def test_decorator_builds_schema_from_hints() -> None:
    @tool(description="echo", category="test")
    def _echo(msg: str, times: int = 1) -> str:
        return msg * times

    t = REGISTRY["_echo"]
    schema = t.openai_schema()
    fn = schema["function"]
    assert fn["name"] == "_echo"
    assert fn["description"] == "echo"
    props = fn["parameters"]["properties"]
    assert props["msg"] == {"type": "string"}
    assert props["times"]["type"] == "integer"
    assert props["times"]["default"] == 1
    assert fn["parameters"]["required"] == ["msg"]


def test_tool_call_with_json_string() -> None:
    t = REGISTRY["_echo"]
    out = t.call(json.dumps({"msg": "ab", "times": 3}))
    assert out == "ababab"


def test_tool_call_surfaces_exceptions() -> None:
    @tool(description="boom", category="test")
    def _boom() -> str:
        raise ValueError("nope")

    out = REGISTRY["_boom"].call("{}")
    assert out.startswith("ERROR: ValueError: nope")


def test_read_file_and_write_file(tmp_path: Path) -> None:
    p = tmp_path / "hello.txt"
    write_msg = REGISTRY["write_file"].call(
        json.dumps({"path": str(p), "content": "hi there"})
    )
    assert "wrote 8 chars" in write_msg
    assert REGISTRY["read_file"].call(json.dumps({"path": str(p)})) == "hi there"


def test_edit_file_replaces_once(tmp_path: Path) -> None:
    p = tmp_path / "code.py"
    p.write_text('x = a < "6.0"\n', encoding="utf-8")
    msg = REGISTRY["edit_file"].call(json.dumps({
        "path": str(p), "old_string": 'a < "6.0"', "new_string": "float(a) < 6.0",
    }))
    assert "edited" in msg and "1 replacement" in msg
    assert p.read_text(encoding="utf-8") == "x = float(a) < 6.0\n"


def test_edit_file_not_found_string(tmp_path: Path) -> None:
    p = tmp_path / "c.py"
    p.write_text("hello", encoding="utf-8")
    msg = REGISTRY["edit_file"].call(json.dumps({
        "path": str(p), "old_string": "nope", "new_string": "x"}))
    assert msg.startswith("ERROR") and "not found" in msg
    assert p.read_text(encoding="utf-8") == "hello"  # unchanged


def test_edit_file_ambiguous_without_replace_all(tmp_path: Path) -> None:
    p = tmp_path / "c.py"
    p.write_text("a a a", encoding="utf-8")
    msg = REGISTRY["edit_file"].call(json.dumps({
        "path": str(p), "old_string": "a", "new_string": "b"}))
    assert msg.startswith("ERROR") and "3 times" in msg
    assert p.read_text(encoding="utf-8") == "a a a"  # unchanged


def test_edit_file_replace_all(tmp_path: Path) -> None:
    p = tmp_path / "c.py"
    p.write_text("a a a", encoding="utf-8")
    msg = REGISTRY["edit_file"].call(json.dumps({
        "path": str(p), "old_string": "a", "new_string": "b", "replace_all": True}))
    assert "3 replacement" in msg
    assert p.read_text(encoding="utf-8") == "b b b"


def test_edit_file_read_sees_fresh_content(tmp_path: Path) -> None:
    # read (caches) -> edit -> read must reflect the edit (cache invalidated)
    p = tmp_path / "c.py"
    p.write_text("one", encoding="utf-8")
    assert REGISTRY["read_file"].call(json.dumps({"path": str(p)})) == "one"
    REGISTRY["edit_file"].call(json.dumps({
        "path": str(p), "old_string": "one", "new_string": "two"}))
    assert REGISTRY["read_file"].call(json.dumps({"path": str(p)})) == "two"


def test_list_dir(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    out = REGISTRY["list_dir"].call(json.dumps({"path": str(tmp_path)}))
    assert "D sub" in out
    assert "F a.txt" in out


def test_run_python_basic() -> None:
    out = REGISTRY["run_python"].call(json.dumps({"code": "print(2+2)"}))
    assert "4" in out
