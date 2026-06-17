"""Python source analysis — AST-based code outline / symbol extraction.

A real AST-traversal workload for eVi: outline a Python file's structure
(functions, classes + methods, imports) and basic call counts by walking the
AST. Powers the `python_symbols` tool (code navigation / "what's in this file?"
without reading the whole thing) and is the natural place to accelerate
traversal.

Walking is done through :func:`walk`, which uses Reflex's Rust **fast-walk**
(``pip install 'evi-assistant[ast]'``, Python 3.13+) when installed and falls
back to stdlib ``ast.walk`` otherwise — same node set either way, so callers
that only collect nodes (like this module) are unaffected by order.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Iterator

try:  # optional Rust accelerator (prebuilt wheels, Python 3.13+)
    from fast_walk import walk_unordered as _fast_walk  # type: ignore[import-not-found]
    _HAVE_FAST_WALK = True
except Exception:  # noqa: BLE001
    _fast_walk = None
    _HAVE_FAST_WALK = False


def have_fast_walk() -> bool:
    """Whether the fast-walk accelerator is active."""
    return _HAVE_FAST_WALK


def walk(tree: ast.AST) -> Iterator[ast.AST]:
    """Yield every node in `tree`. Uses fast-walk (Rust) when installed, else
    stdlib ast.walk. Order is unspecified — collect, don't rely on order."""
    if _fast_walk is not None:
        return iter(_fast_walk(tree))
    return ast.walk(tree)


def analyze_source(source: str, filename: str = "<string>") -> dict[str, Any]:
    """Parse + walk Python `source`, returning a structured outline.

    Raises SyntaxError on unparseable source (callers should handle)."""
    tree = ast.parse(source, filename=filename)
    funcs: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    imports: list[str] = []
    calls = 0
    nodes = 0
    for node in walk(tree):
        nodes += 1
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append({
                "name": node.name,
                "line": node.lineno,
                "args": [a.arg for a in node.args.args],
                "async": isinstance(node, ast.AsyncFunctionDef),
            })
        elif isinstance(node, ast.ClassDef):
            methods = [
                n.name for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            classes.append({"name": node.name, "line": node.lineno, "methods": methods})
        elif isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imports.extend(f"{mod}.{a.name}" if mod else a.name for a in node.names)
        elif isinstance(node, ast.Call):
            calls += 1
    return {
        "functions": funcs,
        "classes": classes,
        "imports": sorted(set(imports)),
        "calls": calls,
        "nodes": nodes,
        "walker": "fast-walk" if _HAVE_FAST_WALK else "ast.walk",
    }


def analyze_file(path: str | Path) -> dict[str, Any]:
    """Outline a Python file. Raises OSError / SyntaxError to the caller."""
    p = Path(path)
    return analyze_source(p.read_text(encoding="utf-8"), str(p))
