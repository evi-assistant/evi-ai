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


def test_list_dir(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    out = REGISTRY["list_dir"].call(json.dumps({"path": str(tmp_path)}))
    assert "D sub" in out
    assert "F a.txt" in out


def test_run_python_basic() -> None:
    out = REGISTRY["run_python"].call(json.dumps({"code": "print(2+2)"}))
    assert "4" in out
