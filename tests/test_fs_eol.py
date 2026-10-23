"""Tests for filesystem EOL preservation and normalization."""

from __future__ import annotations

import json
from pathlib import Path

from evi.tools.base import REGISTRY
import evi.tools.fs  # noqa: F401


def test_edit_file_preserves_crlf(tmp_path: Path) -> None:
    # AC 1: edit_file on CRLF file with LF strings preserves CRLF
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")

    msg = REGISTRY["edit_file"].call(json.dumps({
        "path": str(p),
        "old_string": "a = 1\nb = 2",
        "new_string": "a = 10\nb = 20",
    }))

    assert "1 replacement(s)" in msg
    assert p.read_bytes() == b"a = 10\r\nb = 20\r\nc = 3\r\n"


def test_edit_file_accepts_echoed_crlf(tmp_path: Path) -> None:
    # AC 2: edit_file on CRLF file with CRLF strings succeeds
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")

    msg = REGISTRY["edit_file"].call(json.dumps({
        "path": str(p),
        "old_string": "a = 1\r\nb = 2",
        "new_string": "a = 9\r\nb = 2",
    }))

    assert "1 replacement(s)" in msg
    res = p.read_bytes()
    assert b"\n" not in res.replace(b"\r\n", b"")  # No bare LF
    assert b"\r\r\n" not in res
    assert res == b"a = 9\r\nb = 2\r\nc = 3\r\n"


def test_edit_file_lf_stays_lf(tmp_path: Path) -> None:
    # AC 3: edit_file on LF file with CRLF strings stays LF
    p = tmp_path / "lf.txt"
    p.write_bytes(b"a = 1\nb = 2\n")

    msg = REGISTRY["edit_file"].call(json.dumps({
        "path": str(p),
        "old_string": "a = 1\r\nb = 2",
        "new_string": "a = 9\r\nb = 2",
    }))

    assert "1 replacement(s)" in msg
    assert p.read_bytes() == b"a = 9\nb = 2\n"
    assert b"\r" not in p.read_bytes()


def test_edit_file_mixed_eol_fallback(tmp_path: Path) -> None:
    # AC 4: Mixed-EOL file fallback to raw strings when normalized version not found.
    p = tmp_path / "mixed.txt"
    p.write_bytes(b"x = 1\r\ny = 2\nz = 3\n")

    msg = REGISTRY["edit_file"].call(json.dumps({
        "path": str(p),
        "old_string": "y = 2\nz = 3",
        "new_string": "y = 20\nz = 30",
    }))

    assert "1 replacement(s)" in msg
    assert p.read_bytes() == b"x = 1\r\ny = 20\nz = 30\n"


def test_apply_patch_preserves_crlf(tmp_path: Path) -> None:
    # AC 5: apply_patch with LF patch on CRLF file preserves CRLF for every hunk.
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"line1\r\nline2\r\nline3\r\nline4\r\n")

    patch = (
        "<<<<<<< SEARCH\n"
        "line1\n"
        "line2\n"
        "=======\n"
        "new1\n"
        "new2\n"
        ">>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\n"
        "line3\n"
        "line4\n"
        "=======\n"
        "new3\n"
        "new4\n"
        ">>>>>>> REPLACE"
    )

    msg = REGISTRY["apply_patch"].call(json.dumps({
        "path": str(p),
        "patch": patch,
    }))

    assert "applied 2 hunk(s)" in msg
    assert p.read_bytes() == b"new1\r\nnew2\r\nnew3\r\nnew4\r\n"


def test_read_file_slice_preserves_eol(tmp_path: Path) -> None:
    # AC 6: read_file slice on CRLF file returns CRLF
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"line1\r\nline2\r\nline3\r\n")

    # Read line 2
    out = REGISTRY["read_file"].call(json.dumps({
        "path": str(p),
        "offset": 2,
        "limit": 1,
    }))

    assert out == "line2\r\n"


def test_edit_file_identical_eol_error(tmp_path: Path) -> None:
    # AC 7: old/new differing only in EOL returns identical error
    p = tmp_path / "test.txt"
    p.write_bytes(b"hello\nworld\n")

    msg = REGISTRY["edit_file"].call(json.dumps({
        "path": str(p),
        "old_string": "hello\nworld",
        "new_string": "hello\r\nworld",
    }))

    assert msg == "ERROR: old_string and new_string are identical"
    assert p.read_bytes() == b"hello\nworld\n"


def test_apply_patch_mixed_eol_fallback(tmp_path: Path) -> None:
    # AC 1: Mixed-EOL file fallback to raw strings when normalized version not found.
    p = tmp_path / "mixed.txt"
    p.write_bytes(b"x = 1\r\ny = 2\nz = 3\n")

    patch = (
        "<<<<<<< SEARCH\n"
        "y = 2\nz = 3\n"
        "=======\n"
        "y = 20\nz = 30\n"
        ">>>>>>> REPLACE"
    )

    msg = REGISTRY["apply_patch"].call(json.dumps({
        "path": str(p),
        "patch": patch,
    }))

    assert "applied 1 hunk(s)" in msg
    assert p.read_bytes() == b"x = 1\r\ny = 20\nz = 30\n"


def test_apply_patch_not_found_error(tmp_path: Path) -> None:
    # AC 3: A SEARCH found neither normalized nor as given still returns 'block N SEARCH not found'.
    p = tmp_path / "test.txt"
    p.write_bytes(b"hello\nworld\n")

    patch = (
        "<<<<<<< SEARCH\n"
        "missing\n"
        "=======\n"
        "found\n"
        ">>>>>>> REPLACE"
    )

    msg = REGISTRY["apply_patch"].call(json.dumps({
        "path": str(p),
        "patch": patch,
    }))

    assert "ERROR: block 1 SEARCH not found" in msg
    assert p.read_bytes() == b"hello\nworld\n"
