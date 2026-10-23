import json
from evi.tools.base import REGISTRY
import evi.tools.fs  # noqa: F401
from evi.index import _walk_text_files


def test_ignore_wiring_fs(tmp_path):
    # Setup project structure
    # tmp_path/
    #   .gitignore (contains *.log, out/)
    #   .eviignore (contains secret.txt)
    #   keep.py
    #   debug.log
    #   secret.txt
    #   out/
    #     ignored.py
    #   .git/
    #     config

    (tmp_path / ".gitignore").write_text("*.log\nout/\n")
    (tmp_path / ".eviignore").write_text("secret.txt\n")
    (tmp_path / "keep.py").write_text("print('keep')")
    (tmp_path / "debug.log").write_text("log data")
    (tmp_path / "secret.txt").write_text("secret data")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "ignored.py").write_text("print('ignored')")

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("git config")

    # 1. find_files: should ignore *.log, out/, .git/, and .eviignore entries
    res = REGISTRY['find_files'].call(json.dumps({
        "pattern": "**/*",
        "path": str(tmp_path)
    }))
    assert "keep.py" in res
    assert "debug.log" not in res
    assert "ignored.py" not in res
    assert "config" not in res
    assert "secret.txt" not in res

    # 2. search_files: should ignore same
    res = REGISTRY['search_files'].call(json.dumps({
        "pattern": "data",
        "path": str(tmp_path),
        "glob": "**/*"
    }))
    assert "debug.log" not in res

    # 3. Explicit request wins
    # Search in an ignored file
    res = REGISTRY['search_files'].call(json.dumps({
        "pattern": "log data",
        "path": str(tmp_path / "debug.log"),
        "glob": ""
    }))
    assert "debug.log" in res

    # List an ignored folder
    res = REGISTRY['list_dir'].call(json.dumps({
        "path": str(tmp_path / "out")
    }))
    assert "ignored.py" in res

    # Find files in an ignored folder
    res = REGISTRY['find_files'].call(json.dumps({
        "pattern": "*.py",
        "path": str(tmp_path / "out")
    }))
    assert "ignored.py" in res

    # Search files in an ignored folder
    res = REGISTRY['search_files'].call(json.dumps({
        "pattern": "ignored",
        "path": str(tmp_path / "out"),
        "glob": "**/*"
    }))
    assert "ignored.py" in res

    # 4. list_dir hides ignored entries and adds count
    res = REGISTRY['list_dir'].call(json.dumps({
        "path": str(tmp_path)
    }))
    assert "F keep.py" in res
    assert "F debug.log" not in res
    assert "F secret.txt" not in res
    assert "D out" not in res
    assert "D .git" in res
    assert "(3 entries hidden by .gitignore/.eviignore)" in res


def test_ignore_wiring_index(tmp_path):
    # Setup project structure
    (tmp_path / ".gitignore").write_text("*.log\nout/\n")
    (tmp_path / ".eviignore").write_text("secret.txt\n")
    (tmp_path / "keep.py").write_text("print('keep')")
    (tmp_path / "debug.log").write_text("log data")
    (tmp_path / "secret.txt").write_text("secret data")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "ignored.py").write_text("print('ignored')")

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("git config")

    # Test _walk_text_files directly
    files = list(_walk_text_files(tmp_path))

    # Should only yield keep.py
    # debug.log is ignored by .gitignore
    # secret.txt is ignored by .eviignore
    # out/ignored.py is ignored by .gitignore
    # .git/config is ignored by _SKIP_DIRS

    # Filter for just the names to make it easier
    file_names = [p.name for p in files]
    assert "keep.py" in file_names
    assert "debug.log" not in file_names
    assert "secret.txt" not in file_names
    assert "ignored.py" not in file_names
    assert "config" not in file_names
