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


# ---- read_file pagination --------------------------------------------------


def test_read_file_offset_and_limit(tmp_path: Path) -> None:
    p = tmp_path / "lines.txt"
    p.write_text("".join(f"line{i}\n" for i in range(1, 11)), encoding="utf-8")
    # lines 3,4,5 (1-based offset, limit 3)
    out = REGISTRY["read_file"].call(json.dumps({"path": str(p), "offset": 3, "limit": 3}))
    assert out == "line3\nline4\nline5\n"


def test_read_file_offset_to_eof(tmp_path: Path) -> None:
    p = tmp_path / "lines.txt"
    p.write_text("a\nb\nc\nd\n", encoding="utf-8")
    out = REGISTRY["read_file"].call(json.dumps({"path": str(p), "offset": 3}))
    assert out == "c\nd\n"


def test_read_file_offset_past_eof_errors(tmp_path: Path) -> None:
    p = tmp_path / "lines.txt"
    p.write_text("a\nb\n", encoding="utf-8")
    out = REGISTRY["read_file"].call(json.dumps({"path": str(p), "offset": 99}))
    assert out.startswith("ERROR: no lines at offset 99")


def test_read_file_slice_roundtrips_through_edit(tmp_path: Path) -> None:
    # A slice carries no line-number prefixes, so it can be fed back to edit_file.
    p = tmp_path / "lines.txt"
    p.write_text("alpha\nbravo\ncharlie\n", encoding="utf-8")
    sliced = REGISTRY["read_file"].call(json.dumps({"path": str(p), "offset": 2, "limit": 1}))
    assert sliced == "bravo\n"


# ---- find_files (glob) -----------------------------------------------------


def test_find_files_glob(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("y")
    (tmp_path / "c.txt").write_text("z")
    out = REGISTRY["find_files"].call(json.dumps({"pattern": "*.py", "path": str(tmp_path)}))
    assert "a.py" in out and "b.py" in out and "c.txt" not in out


def test_find_files_recursive_skips_noise_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.py").write_text("x")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "junk.py").write_text("y")
    out = REGISTRY["find_files"].call(json.dumps({"pattern": "**/*.py", "path": str(tmp_path)}))
    assert "keep.py" in out
    assert "junk.py" not in out  # .venv is an ignored dir


def test_find_files_no_match(tmp_path: Path) -> None:
    out = REGISTRY["find_files"].call(json.dumps({"pattern": "*.rs", "path": str(tmp_path)}))
    assert out.startswith("(no files match")


# ---- search_files (regex grep) ---------------------------------------------


def test_search_files_content_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import os\nx = TODO_here\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("clean\n", encoding="utf-8")
    out = REGISTRY["search_files"].call(json.dumps({"pattern": r"TODO_\w+", "path": str(tmp_path)}))
    assert "a.py:2:" in out and "TODO_here" in out
    assert "b.py" not in out


def test_search_files_ignore_case_and_glob(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("Hello World\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("hello world\n", encoding="utf-8")
    # case-insensitive, restricted to *.py via glob → only the .py hit
    out = REGISTRY["search_files"].call(json.dumps({
        "pattern": "hello", "path": str(tmp_path), "glob": "*.py", "ignore_case": True,
    }))
    assert "a.py:1:" in out and "a.md" not in out


def test_search_files_no_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("nothing here\n", encoding="utf-8")
    out = REGISTRY["search_files"].call(json.dumps({"pattern": "zzz", "path": str(tmp_path)}))
    assert out.startswith("(no matches")


def test_search_files_bad_regex(tmp_path: Path) -> None:
    out = REGISTRY["search_files"].call(json.dumps({"pattern": "(", "path": str(tmp_path)}))
    assert out.startswith("ERROR: bad regex")


def test_run_python_basic() -> None:
    out = REGISTRY["run_python"].call(json.dumps({"code": "print(2+2)"}))
    assert "4" in out


def test_python_exe_dev_is_sys_executable() -> None:
    import sys

    from evi.tools.code import _python_exe

    # Not frozen in tests → sys.executable.
    assert _python_exe() == sys.executable


def test_run_python_errors_when_frozen_without_python(monkeypatch) -> None:
    import evi.tools.code as code_mod

    # Simulate the desktop sidecar: frozen + no python on PATH.
    monkeypatch.setattr(code_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(code_mod.shutil, "which", lambda _name: None)
    out = code_mod.run_python("print(1)")
    assert out.startswith("ERROR") and "no Python interpreter" in out
