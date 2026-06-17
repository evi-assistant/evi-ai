"""Tests for AST-based Python analysis (evi.pyanalyze) + python_symbols tool."""

from __future__ import annotations

import ast

from evi import pyanalyze
from evi import workdir

_SRC = '''\
import os
from pathlib import Path, PurePath


def top(a, b):
    return a + b


async def fetch(url):
    return url


class Widget:
    def __init__(self, n):
        self.n = n

    def render(self):
        return str(self.n)
'''


def test_walk_matches_ast_walk_node_set():
    tree = ast.parse(_SRC)
    assert {id(n) for n in pyanalyze.walk(tree)} == {id(n) for n in ast.walk(tree)}


def test_analyze_source_outline():
    info = pyanalyze.analyze_source(_SRC, "x.py")
    fnames = {f["name"] for f in info["functions"]}
    assert {"top", "fetch"} <= fnames  # methods live under the class, not here
    assert next(f for f in info["functions"] if f["name"] == "fetch")["async"] is True
    cls = {c["name"]: c for c in info["classes"]}
    assert "Widget" in cls
    assert set(cls["Widget"]["methods"]) == {"__init__", "render"}
    assert "os" in info["imports"]
    assert "pathlib.Path" in info["imports"]
    assert info["walker"] in ("fast-walk", "ast.walk")


def test_analyze_file_and_tool(tmp_path):
    from evi.tools.code import python_symbols

    f = tmp_path / "m.py"
    f.write_text(_SRC, encoding="utf-8")
    tok = workdir.set_cwd(tmp_path)
    try:
        out = python_symbols("m.py")
    finally:
        workdir.reset(tok)
    assert "class Widget" in out
    assert "def top(a, b)" in out
    assert "async def fetch" in out


def test_python_symbols_rejects_non_python(tmp_path):
    from evi.tools.code import python_symbols

    f = tmp_path / "n.txt"
    f.write_text("hello", encoding="utf-8")
    assert python_symbols(str(f)).startswith("ERROR")


def test_python_symbols_syntax_error(tmp_path):
    from evi.tools.code import python_symbols

    f = tmp_path / "bad.py"
    f.write_text("def (oops", encoding="utf-8")
    assert "syntax error" in python_symbols(str(f))
