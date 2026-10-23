"""edit_file on a miss: already applied, the lines of an ambiguous match, the closest region."""

from evi.tools.fs import edit_file


def test_edit_already_applied(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("hello world\nfoo bar", encoding="utf-8")

    # Apply it first
    edit_file(
        path=str(p),
        old_string="hello world",
        new_string="hello eVi"
    )

    # Now try again
    res = edit_file(
        path=str(p),
        old_string="hello world",
        new_string="hello eVi"
    )
    assert res.startswith("edited")
    assert "already applied" in res
    assert p.read_text(encoding="utf-8") == "hello eVi\nfoo bar"


def test_edit_closest_match(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("line 1\nline 2\nline 3\nline 4", encoding="utf-8")

    # Case 2: near miss
    res = edit_file(
        path=str(p),
        old_string="line 2\nline X", # 'X' instead of '3'
        new_string="changed"
    )
    assert res.startswith("ERROR: old_string not found")
    assert "closest match, lines 2-3" in res
    assert "    2| line 2" in res
    assert "    3| line 3" in res


def test_edit_no_close_match(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("abc\ndef\nghi", encoding="utf-8")

    # Case 3: no match >= 50%
    res = edit_file(
        path=str(p),
        old_string="xyz\n123",
        new_string="changed"
    )
    assert res.startswith("ERROR: old_string not found")
    assert "closest match" not in res


def test_edit_ambiguous_match(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("foo\nbar\nfoo\nbaz\nfoo", encoding="utf-8")

    # Case 4: ambiguous match
    res = edit_file(
        path=str(p),
        old_string="foo",
        new_string="baz"
    )
    assert "appears 3 times" in res
    assert "(lines 1, 3, 5)" in res
    assert p.read_text(encoding="utf-8") == "foo\nbar\nfoo\nbaz\nfoo"

    # replace_all=true
    res = edit_file(
        path=str(p),
        old_string="foo",
        new_string="baz",
        replace_all=True
    )
    assert "3 replacement(s)" in res
    assert p.read_text(encoding="utf-8") == "baz\nbar\nbaz\nbaz\nbaz"


def test_edit_deletion_not_applied(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("hello world", encoding="utf-8")

    # Case 5: deletion of missing text should be ERROR, not 'already applied'
    res = edit_file(
        path=str(p),
        old_string="missing",
        new_string=""
    )
    assert res.startswith("ERROR: old_string not found")
    assert "already applied" not in res


def test_a_blank_new_string_is_never_already_applied(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("a\nb\n", encoding="utf-8")
    res = edit_file(path=str(p), old_string="missing", new_string="\n")
    assert res.startswith("ERROR: old_string not found")


def test_the_closest_region_shows_at_most_twenty_lines(tmp_path):
    p = tmp_path / "test.txt"
    body = "".join(f"line {i}\n" for i in range(1, 41))
    p.write_text(body, encoding="utf-8")
    old = body.replace("line 30", "line XXX")
    res = edit_file(path=str(p), old_string=old, new_string="x")
    assert "closest match, lines 1-40" in res
    assert "   20| line 20" in res and "   21| line 21" not in res
