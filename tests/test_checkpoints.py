"""Tests for file checkpointing + rewind (Phase 64)."""

from __future__ import annotations

from evi import checkpoints


def test_rewind_restores_modified_file(tmp_path):
    root = tmp_path / "home"
    f = tmp_path / "doc.txt"
    f.write_text("original", encoding="utf-8")

    checkpoints.record_before_write(f, root=root)
    f.write_text("changed", encoding="utf-8")

    actions = checkpoints.rewind(root=root)
    assert f.read_text(encoding="utf-8") == "original"
    assert any("restored" in a for _, a in actions)


def test_rewind_deletes_created_file(tmp_path):
    root = tmp_path / "home"
    f = tmp_path / "new.txt"  # does not exist yet

    checkpoints.record_before_write(f, root=root)
    f.write_text("brand new", encoding="utf-8")

    checkpoints.rewind(root=root)
    assert not f.exists()


def test_rewind_to_seq_undoes_multiple(tmp_path):
    root = tmp_path / "home"
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a0", encoding="utf-8")
    b.write_text("b0", encoding="utf-8")

    s1 = checkpoints.record_before_write(a, root=root)
    a.write_text("a1", encoding="utf-8")
    checkpoints.record_before_write(b, root=root)
    b.write_text("b1", encoding="utf-8")

    # rewind from the first seq → undoes both writes
    checkpoints.rewind(s1, root=root)
    assert a.read_text(encoding="utf-8") == "a0"
    assert b.read_text(encoding="utf-8") == "b0"
    assert checkpoints.list_checkpoints(root=root) == []


def test_rewind_default_undoes_only_latest(tmp_path):
    root = tmp_path / "home"
    a = tmp_path / "a.txt"
    a.write_text("v0", encoding="utf-8")
    checkpoints.record_before_write(a, root=root)
    a.write_text("v1", encoding="utf-8")
    checkpoints.record_before_write(a, root=root)
    a.write_text("v2", encoding="utf-8")

    checkpoints.rewind(root=root)  # only the latest
    assert a.read_text(encoding="utf-8") == "v1"
    # one checkpoint remains
    assert len(checkpoints.list_checkpoints(root=root)) == 1


def test_list_and_seq_monotonic(tmp_path):
    root = tmp_path / "home"
    f = tmp_path / "x.txt"
    seqs = []
    for i in range(3):
        seqs.append(checkpoints.record_before_write(f, root=root))
        f.write_text(f"v{i}", encoding="utf-8")
    assert seqs == [1, 2, 3]
    assert len(checkpoints.list_checkpoints(root=root)) == 3


def test_write_file_tool_records_checkpoint(tmp_path, monkeypatch):
    # The fs tool should journal via the real checkpoints module; point HOME at
    # a temp dir so the journal lands there.
    import evi.config as config
    import evi.tools.fs as fs

    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    target = tmp_path / "out.txt"
    target.write_text("before", encoding="utf-8")
    fs.write_file(str(target), "after")
    assert target.read_text(encoding="utf-8") == "after"
    # rewind via the same root
    checkpoints.rewind(root=tmp_path / "home")
    assert target.read_text(encoding="utf-8") == "before"
