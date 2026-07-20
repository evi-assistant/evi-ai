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


# Every tool that mutates a user file must journal first, or `rewind` silently
# fails to undo it. write_file was covered above; these guard the other paths,
# which had the record_before_write call but nothing asserting it stayed.


def test_edit_file_tool_records_checkpoint(tmp_path, monkeypatch):
    import evi.config as config
    import evi.tools.fs as fs

    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    target = tmp_path / "code.py"
    target.write_text("x = 1\ny = 2\n", encoding="utf-8")

    fs.edit_file(str(target), "x = 1", "x = 99")
    assert "x = 99" in target.read_text(encoding="utf-8")

    checkpoints.rewind(root=tmp_path / "home")
    assert target.read_text(encoding="utf-8") == "x = 1\ny = 2\n"


def test_apply_patch_tool_records_checkpoint(tmp_path, monkeypatch):
    import evi.config as config
    import evi.tools.fs as fs

    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    target = tmp_path / "code.py"
    original = "a = 1\nb = 2\n"
    target.write_text(original, encoding="utf-8")

    patch = (
        "<<<<<<< SEARCH\na = 1\n=======\na = 10\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\nb = 2\n=======\nb = 20\n>>>>>>> REPLACE\n"
    )
    fs.apply_patch(str(target), patch)
    assert target.read_text(encoding="utf-8") == "a = 10\nb = 20\n"

    # One checkpoint for the whole multi-hunk call → one rewind restores it all.
    checkpoints.rewind(root=tmp_path / "home")
    assert target.read_text(encoding="utf-8") == original


def test_evi_edit_write_records_checkpoint(tmp_path, monkeypatch):
    # `evi edit <file> --write` overwrites a user's source file outside the fs
    # tools, so it needs its own checkpoint — it had none until this was fixed.
    import evi.apps.cli.main as cli
    import evi.config as config
    from evi.llm.agent import Done, TextDelta

    root = tmp_path / "home"
    monkeypatch.setattr(config, "HOME", root)
    monkeypatch.setattr(cli, "ensure_dirs", lambda *a, **k: None)
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: object())

    class _FakeAgent:
        def __init__(self, *a, **k):
            pass

        def chat(self, *a, **k):
            yield TextDelta(text="edited!\n")
            yield Done(reason="stop")

    monkeypatch.setattr(cli, "Agent", _FakeAgent)

    target = tmp_path / "src.py"
    target.write_text("original\n", encoding="utf-8")

    cli.edit(file=str(target), instruction="change it", write=True, diff=False, yes=True)
    assert target.read_text(encoding="utf-8").strip() == "edited!"

    checkpoints.rewind(root=root)
    assert target.read_text(encoding="utf-8") == "original\n"


def test_failed_edit_does_not_journal(tmp_path, monkeypatch):
    # A rejected edit must not leave a phantom checkpoint, or the next rewind
    # would consume it and appear to do nothing.
    import evi.config as config
    import evi.tools.fs as fs

    root = tmp_path / "home"
    monkeypatch.setattr(config, "HOME", root)
    target = tmp_path / "code.py"
    target.write_text("only once\n", encoding="utf-8")

    out = fs.edit_file(str(target), "NOT PRESENT", "whatever")
    assert out.startswith("ERROR")
    assert checkpoints.list_checkpoints(root=root) == []
