"""Tests for the apply_patch multi-hunk file edit tool."""

from __future__ import annotations

from evi import workdir
from evi.tools.fs import _parse_patch, apply_patch

_BLOCK = (
    "<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE"
)


def _patch(*pairs):
    return "\n".join(_BLOCK.format(old=o, new=n) for o, n in pairs)


def test_parse_patch_multiple_blocks():
    blocks = _parse_patch(_patch(("a", "b"), ("c", "d")))
    assert blocks == [("a", "b"), ("c", "d")]


def test_apply_patch_multi_hunk(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tok = workdir.set_cwd(tmp_path)
    try:
        out = apply_patch("code.py", _patch(("alpha", "ALPHA"), ("gamma", "GAMMA")))
    finally:
        workdir.reset(tok)
    assert "applied 2 hunk(s)" in out
    assert f.read_text(encoding="utf-8") == "ALPHA\nbeta\nGAMMA\n"


def test_apply_patch_search_not_found(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello", encoding="utf-8")
    out = apply_patch(str(f), _patch(("nope", "x")))
    assert out.startswith("ERROR") and "not found" in out


def test_apply_patch_ambiguous_match(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("x x x", encoding="utf-8")
    out = apply_patch(str(f), _patch(("x", "y")))
    assert out.startswith("ERROR") and "matches" in out


def test_apply_patch_no_blocks(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello", encoding="utf-8")
    assert apply_patch(str(f), "not a patch").startswith("ERROR")


def test_apply_patch_missing_file(tmp_path):
    assert apply_patch(str(tmp_path / "nope.txt"), _patch(("a", "b"))).startswith("ERROR")


def test_apply_patch_registered():
    from evi.tools.base import REGISTRY

    assert "apply_patch" in REGISTRY and REGISTRY["apply_patch"].category == "fs"
