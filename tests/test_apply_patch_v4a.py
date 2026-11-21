"""apply_patch also takes the `*** Begin Patch` format gpt-oss writes.

gpt-oss (and Codex-trained models) write OpenAI's apply_patch format; eVi
took SEARCH/REPLACE blocks only, so every edit to an existing file failed
and the run looped. Hunks apply in order, each found after the one before
it (and after its `@@ anchor`); the patch's one `*** Update File` must name
the `path` the permission checks saw.
"""

from __future__ import annotations

import pytest

from evi import workdir
from evi.tools.fs import _is_v4a, _parse_v4a, apply_patch

SRC = "import os\n\n\ndef lint_auto(auto):\n    out = a() + b()\n    return out\n"


@pytest.fixture
def here(tmp_path):
    (tmp_path / "evi").mkdir()
    f = tmp_path / "evi" / "lint.py"
    f.write_text(SRC, encoding="utf-8")
    tok = workdir.set_cwd(tmp_path)
    yield f
    workdir.reset(tok)


def test_an_insert_and_a_change_in_two_hunks(here):
    patch = (
        "*** Begin Patch\n"
        "*** Update File: evi/lint.py\n"
        "@@\n"
        " import os\n"
        "+import re\n"
        "@@ def lint_auto(auto):\n"
        "-    out = a() + b()\n"
        "+    out = a() + b() + c()\n"
        "     return out\n"
        "*** End Patch\n"
    )
    out = apply_patch("evi/lint.py", patch)
    assert "applied 2 hunk(s)" in out, out
    assert here.read_text(encoding="utf-8") == (
        "import os\nimport re\n\n\ndef lint_auto(auto):\n    out = a() + b() + c()\n"
        "    return out\n")


def test_crlf_files_keep_their_line_ends(here):
    here.write_bytes(SRC.replace("\n", "\r\n").encode())
    patch = "*** Begin Patch\n*** Update File: evi/lint.py\n@@\n-import os\n+import sys\n*** End Patch"
    assert "applied 1 hunk(s)" in apply_patch("evi/lint.py", patch)
    assert here.read_bytes().startswith(b"import sys\r\n\r\n")


def test_a_blank_context_line_without_its_space_is_still_context(here):
    patch = ("*** Begin Patch\n*** Update File: evi/lint.py\n@@\n import os\n\n+X = 1\n"
             "*** End Patch")
    assert "applied" in apply_patch("evi/lint.py", patch)
    assert here.read_text(encoding="utf-8").startswith("import os\n\nX = 1\n\ndef")


def test_hunks_apply_in_order_after_their_anchor(here):
    # gpt-oss walks the file with context-only hunks; context that repeats
    # ("    return 1") is found after the hunk before it and its anchor.
    here.write_text("def a():\n    return 1\n\n\ndef b():\n    return 1\n", encoding="utf-8")
    patch = ("*** Begin Patch\n*** Update File: evi/lint.py\n"
             "@@\n def a():\n@@ def b():\n-    return 1\n+    return 2\n*** End Patch")
    assert "applied 2 hunk(s)" in apply_patch("evi/lint.py", patch)
    text = here.read_text(encoding="utf-8")
    assert text.startswith("def a():\n    return 1\n") and text.endswith("def b():\n    return 2\n")


def test_a_hunk_missing_after_the_one_before_it_changes_nothing(here):
    patch = ("*** Begin Patch\n*** Update File: evi/lint.py\n@@\n-    return out\n+    pass\n"
             "@@\n-import os\n+import sys\n*** End Patch")
    out = apply_patch("evi/lint.py", patch)
    assert out.startswith("ERROR: hunk 2") and "after the hunk before it" in out
    assert here.read_text(encoding="utf-8") == SRC


@pytest.mark.parametrize(("patch", "words"), [
    ("*** Begin Patch\n*** Update File: evi/other.py\n@@\n-import os\n+import sys\n", "same file"),
    ("*** Begin Patch\n*** Add File: evi/new.py\n+x\n", "write_file"),
    ("*** Begin Patch\n*** Update File: evi/lint.py\n@@\n-import os\n+x\n"
     "*** Update File: evi/lint.py\n@@\n-y\n+z\n", "ONE file"),
    ("*** Begin Patch\n*** Update File: evi/lint.py\n*** Move to: evi/b.py\n", "can't apply"),
    ("*** Begin Patch\n*** Update File: evi/lint.py\n@@\nimport os\n", "starts with none"),
    ("*** Begin Patch\n*** Update File: evi/lint.py\n*** End Patch", "no hunks"),
    ("*** Begin Patch\n*** Update File: evi/lint.py\n@@\n+x = 1\n", "no context"),
])
def test_what_it_refuses_changes_nothing(here, patch, words):
    out = apply_patch("evi/lint.py", patch)
    assert out.startswith("ERROR") and words in out, out
    assert here.read_text(encoding="utf-8") == SRC


def test_search_replace_blocks_win_over_a_quoted_header():
    # A SEARCH/REPLACE patch that edits text holding '*** Begin Patch'.
    sr = "<<<<<<< SEARCH\n*** Begin Patch\n=======\nx\n>>>>>>> REPLACE"
    assert not _is_v4a(sr)
    assert _is_v4a("*** Begin Patch\n*** Update File: a\n@@\n-a\n+b\n")
    assert _parse_v4a("*** Update File: a\n@@ def f\n-a\n+b\n") == ("a", [("def f", "a", "b")])
