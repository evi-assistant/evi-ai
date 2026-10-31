import threading
import time
from pathlib import Path

from evi import workdir
from evi.tools import fs
from evi import config
import evi.checkpoints


def test_path_lock_identity():
    """AC1: _path_lock returns same lock for same file, different for different."""
    p1 = Path("test_file_1.txt")
    p2 = Path("test_file_2.txt")

    lock1 = fs._path_lock(p1)
    lock1_again = fs._path_lock(p1)
    lock2 = fs._path_lock(p2)

    assert lock1 is lock1_again
    assert lock1 is not lock2


def test_path_lock_case_insensitivity(monkeypatch):
    """AC1: _path_lock is case-insensitive on Windows (via normcase)."""
    import os
    p1 = Path("FILE.txt")
    p2 = Path("file.txt")

    lock1 = fs._path_lock(p1)
    lock2 = fs._path_lock(p2)

    if os.name == 'nt':
        assert lock1 is lock2
    else:
        assert lock1 is not lock2

def test_path_lock_symlink(tmp_path):
    """AC1: _path_lock returns same lock for symlink and target."""
    import os
    target = tmp_path / "target.txt"
    target.write_text("content")
    link = tmp_path / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("Symlinks not supported on this platform")

    lock_target = fs._path_lock(target)
    lock_link = fs._path_lock(link)

    assert lock_target is lock_link

def test_path_lock_weakref(tmp_path):
    """AC2: a file's lock is kept only while someone holds it."""
    import gc
    import os

    p = tmp_path / "weakref_test.txt"
    p.write_text("content", encoding="utf-8")
    key = os.path.normcase(os.path.realpath(p))
    lock = fs._path_lock(p)
    assert fs._path_lock(p) is lock and key in fs._PATH_LOCKS
    del lock
    gc.collect()
    assert key not in fs._PATH_LOCKS
    with fs._path_lock(p):
        assert key in fs._PATH_LOCKS


def test_path_lock_through_a_junction_is_the_targets(tmp_path):
    """A folder junction (Windows; no privilege needed, unlike a symlink)."""
    import sys

    import pytest

    if sys.platform != "win32":
        pytest.skip("junctions are Windows-only")
    import _winapi

    real = tmp_path / "real"
    real.mkdir()
    (real / "f.txt").write_text("x", encoding="utf-8")
    _winapi.CreateJunction(str(real), str(tmp_path / "via"))
    assert fs._path_lock(tmp_path / "via" / "f.txt") is fs._path_lock(real / "f.txt")


def test_lost_update_edit_file(tmp_path, monkeypatch):
    """AC3: Parallel edit_file calls do not lose updates."""
    p = tmp_path / "lost_update.txt"
    p.write_text("A\nB\n", encoding="utf-8", newline="")

    monkeypatch.setattr(workdir, "resolve", lambda path: Path(path) if isinstance(path, Path) else (tmp_path / path))

    def slow_record(path, *args, **kwargs):
        time.sleep(0.2)
        return 0
    monkeypatch.setattr(evi.checkpoints, "record_before_write", slow_record)

    barrier = threading.Barrier(2)
    results = []

    def do_edit(old, new):
        barrier.wait()
        res = fs.edit_file(str(p), old, new)
        results.append(res)

    t1 = threading.Thread(target=do_edit, args=("A", "A1"))
    t2 = threading.Thread(target=do_edit, args=("B", "B1"))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert p.read_bytes().decode("utf-8").strip() == "A1\nB1"  # read_text(newline=) is 3.13+


def test_lost_update_mixed_edit_patch(tmp_path, monkeypatch):
    """AC4: Parallel edit_file and apply_patch do not lose updates."""
    p = tmp_path / "mixed_update.txt"
    p.write_text("A\nB\n", encoding="utf-8", newline="")

    monkeypatch.setattr(workdir, "resolve", lambda path: Path(path) if isinstance(path, Path) else (tmp_path / path))

    def slow_record(path, *args, **kwargs):
        time.sleep(0.2)
        return 0
    monkeypatch.setattr(evi.checkpoints, "record_before_write", slow_record)

    barrier = threading.Barrier(2)

    def do_edit():
        barrier.wait()
        fs.edit_file(str(p), "A", "A1")

    def do_patch():
        barrier.wait()
        patch = "<<<<<<< SEARCH\nB\n=======\nB1\n>>>>>>> REPLACE"
        fs.apply_patch(str(p), patch)

    t1 = threading.Thread(target=do_edit)
    t2 = threading.Thread(target=do_patch)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert p.read_bytes().decode("utf-8").strip() == "A1\nB1"  # read_text(newline=) is 3.13+


def test_parallel_seq_write_file(tmp_path, monkeypatch):
    """AC5: Parallel write_file calls produce distinct sequential seq values."""
    home = tmp_path / "home"
    home.mkdir()

    # Mock HOME in config
    monkeypatch.setattr(config, "HOME", home)

    # Mock workdir.resolve to use tmp_path
    monkeypatch.setattr(workdir, "resolve", lambda path: Path(path) if isinstance(path, Path) else (tmp_path / path))

    num_threads = 8
    barrier = threading.Barrier(num_threads)

    def do_write(i):
        barrier.wait()
        fs.write_file(f"file_{i}.txt", f"content {i}")

    threads = [threading.Thread(target=do_write, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = evi.checkpoints.list_checkpoints(limit=100, root=home)
    actual_seqs = {entry['seq'] for entry in seqs}
    assert actual_seqs == set(range(1, num_threads + 1))


def test_parallel_record_before_write(tmp_path, monkeypatch):
    """AC5: Parallel record_before_write calls return distinct seqs."""
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setattr(config, "HOME", home)

    num_threads = 16
    barrier = threading.Barrier(num_threads)
    results = []
    lock = threading.Lock()

    def do_record(i):
        barrier.wait()
        seq = evi.checkpoints.record_before_write(Path(f"file_{i}.txt"), root=home)
        with lock:
            results.append(seq)

    threads = [threading.Thread(target=do_record, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) == set(range(1, num_threads + 1))
