"""Atomic writes in the fs tools: temp file + os.replace, mode, symlinks, retries."""

import os
import pytest

from evi.tools.fs import write_file, edit_file, apply_patch


def test_atomic_write_cleanup(tmp_path):
    """AC1: No temp files left behind, and CRLF preserved."""
    p = tmp_path / "test.txt"
    content = "line1\r\nline2\r\n"

    # Use write_file to create it
    write_file(str(p), content)
    assert p.read_bytes() == content.encode("utf-8")

    # Check for temp files
    for f in tmp_path.glob("*.evi-tmp"):
        pytest.fail(f"Temp file left behind: {f}")

    # Edit it
    edit_file(str(p), "line1", "changed1")
    assert p.read_bytes() == "changed1\r\nline2\r\n".encode("utf-8")

    for f in tmp_path.glob("*.evi-tmp"):
        pytest.fail(f"Temp file left behind: {f}")


def test_atomic_write_failure_recovery(tmp_path, monkeypatch):
    """AC2: If os.replace fails with OSError, file is intact and no temp left."""
    p = tmp_path / "fail.txt"
    original_content = "a = 1\n"
    p.write_bytes(original_content.encode("utf-8"))

    # Patch os.replace to raise OSError
    def mock_replace(src, dst):
        raise OSError("Disk full or other error")

    monkeypatch.setattr(os, "replace", mock_replace)

    # edit_file should raise or return error
    with pytest.raises(OSError):
        edit_file(str(p), "a = 1", "a = 2")

    assert p.read_bytes() == original_content.encode("utf-8")
    for f in tmp_path.glob("*.evi-tmp"):
        pytest.fail(f"Temp file left behind: {f}")


def test_atomic_write_permission_retry(tmp_path, monkeypatch):
    """AC3: PermissionError is retried and eventually succeeds."""
    p = tmp_path / "retry.txt"
    content = "hello"

    real_replace = os.replace
    call_count = 0

    def mock_replace(src, dst):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise PermissionError("File locked")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", mock_replace)

    write_file(str(p), content)
    assert p.read_bytes() == content.encode("utf-8")
    assert call_count == 3


@pytest.mark.skipif(os.name == "nt", reason="Mode preservation not applicable on Windows")


def test_atomic_write_mode_preservation(tmp_path):
    """AC4: File mode is kept after edit."""
    p = tmp_path / "mode.txt"
    p.write_text("content", encoding="utf-8")
    p.chmod(0o640)

    edit_file(str(p), "content", "new content")

    assert oct(p.stat().st_mode & 0o777) == "0o640"


def test_atomic_write_symlink(tmp_path):
    """AC5: Symlink is preserved and target is updated."""
    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    link = tmp_path / "link.txt"

    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("Symlinks not supported/privileged on this system")

    write_file(str(link), "updated")

    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "updated"


def test_apply_patch_atomic(tmp_path):
    """Verify apply_patch also uses atomic write and cleans up."""
    p = tmp_path / "patch.txt"
    p.write_text("line1\nline2\n", encoding="utf-8")

    patch = "<<<<<<< SEARCH\nline1\n=======\nchanged1\n>>>>>>> REPLACE"
    apply_patch(str(p), patch)

    assert p.read_text(encoding="utf-8") == "changed1\nline2\n"
    for f in tmp_path.glob("*.evi-tmp"):
        pytest.fail(f"Temp file left behind: {f}")
