import pytest
import sys
import ctypes
from pathlib import Path
from evi.ignore import parse_rules, IgnoreRules, ignore_filter


def test_basic_rules():
    rules = IgnoreRules(parse_rules("""# Comment

*.log
!keep.log
out/
/build
docs/*.md
**/tmp
logs/**
*.py[cod]
[!a-z].txt
"""))

    # AC 2: Blank lines, comments, negation
    assert rules.ignored('a.log', False) is True
    assert rules.ignored('keep.log', False) is False

    # AC 3: Trailing / matches folders and their contents
    assert rules.ignored('out', True) is True
    assert rules.ignored('out/x.txt', False) is True
    assert rules.ignored('out', False) is False # File named 'out' should not be ignored by 'out/'

    # AC 4: Anchoring
    assert rules.ignored('a/b/x.log', False) is True # *.log matches any depth
    assert rules.ignored('build', True) is True # /build matches root
    assert rules.ignored('src/build', True) is False # /build does not match src/build
    assert rules.ignored('docs/a.md', False) is True # docs/*.md matches
    assert rules.ignored('docs/sub/a.md', False) is False # docs/*.md does not match nested

    # AC 5: Wildcards and classes
    assert rules.ignored('a/b/tmp', True) is True # **/tmp
    assert rules.ignored('tmp', True) is True # **/tmp
    assert rules.ignored('logs/a/b.txt', False) is True # logs/**
    assert rules.ignored('m.pyc', False) is True # *.py[cod]
    assert rules.ignored('m.py', False) is False # *.py[cod]
    assert rules.ignored('1.txt', False) is True # [!a-z].txt
    assert rules.ignored('a.txt', False) is False # [!a-z].txt


def test_folder_persistence():
    # AC 6: File inside ignored folder stays ignored
    rules = IgnoreRules(parse_rules("out/\n!out/keep.txt"))
    assert rules.ignored('out/keep.txt', False) is True

    # But if it's not a folder ignore, negation works
    rules2 = IgnoreRules(parse_rules("*.log\n!keep.log"))
    assert rules2.ignored('keep.log', False) is False


def test_ignore_filter(tmp_path):
    # AC 7 & 8: ignore_filter
    # Setup: tmp_path/repo/.git
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    # Create .gitignore
    (repo / ".gitignore").write_text("*.log\nout/", encoding='utf-8')
    (repo / ".eviignore").write_text("!keep.log", encoding='utf-8')

    # Test base = repo
    filt = ignore_filter(repo)

    # Mock paths
    log_file = repo / "x.log"
    keep_log = repo / "keep.log"
    out_dir = repo / "out"
    out_file = repo / "out" / "y.txt"
    src_file = repo / "src" / "main.py"

    # We need to actually create them for p.is_dir() to work in the predicate
    log_file.touch()
    keep_log.touch()
    out_dir.mkdir()
    out_file.touch()
    (repo / "src").mkdir()
    src_file.touch()

    assert filt(log_file) is True
    assert filt(keep_log) is False
    assert filt(out_dir) is True
    assert filt(out_file) is True
    assert filt(src_file) is False

    # Test subfolder base
    sub = repo / "src"
    filt_sub = ignore_filter(sub)
    sub_log = sub / "debug.log"
    sub_log.touch()
    assert filt_sub(sub_log) is True # Should still apply root's *.log

    # Test base itself is ignored
    out_filt = ignore_filter(out_dir)
    # "The predicate ignores nothing ... when base itself is ignored (an explicit request wins)"
    assert out_filt(out_file) is False


def test_trailing_whitespace_and_cr_are_stripped():
    assert IgnoreRules(parse_rules("*.log  \r\n")).ignored("a.log", False) is True


def test_ignore_filter_path_variants(tmp_path):
    # AC 8: Works when base is spelled differently from resolved root
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".gitignore").write_text("*.log")

    abs_base = repo.resolve()
    filt = ignore_filter(abs_base)
    log_file = abs_base / "test.log"
    log_file.touch()
    assert filt(log_file) is True

    if sys.platform == 'win32':
        # Test Windows 8.3 short paths
        try:
            buf = ctypes.create_unicode_buffer(260)
            ctypes.windll.kernel32.GetShortPathNameW(str(abs_base), buf, 260)
            short_path_str = buf.value
            if short_path_str and short_path_str != str(abs_base):
                short_base = Path(short_path_str)
                filt_short = ignore_filter(short_base)

                log_file_short = short_base / "x.log"
                keep_file_short = short_base / "keep.py"
                log_file_short.touch()
                keep_file_short.touch()

                assert filt_short(log_file_short) is True
                assert filt_short(keep_file_short) is False
        except Exception as e:
            pytest.fail(f"Short path test failed: {e}")
