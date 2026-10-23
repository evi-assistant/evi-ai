from unittest.mock import patch
from evi.config import Config
from evi.dream import MemorySnapshot, diff_snapshots, run_dream
from evi.memory import MemoryStore


def test_snapshot_hashes_content(tmp_path):
    store = MemoryStore(root=tmp_path / "memory")
    store.write("test1", "Line 1\nLine 2", tags=["tag1"])

    snapshot = MemorySnapshot.take(store)

    # Calculate expected hash
    content = (tmp_path / "memory" / "test1.md").read_bytes()
    import hashlib
    expected_hash = hashlib.sha256(content).hexdigest()

    assert snapshot.entries["test1"] == expected_hash
    assert len(snapshot.entries["test1"]) == 64


def test_diff_detects_body_change(tmp_path):
    store = MemoryStore(root=tmp_path / "memory")
    store.write("test1", "Same first line\nBody one")

    before = MemorySnapshot.take(store)

    store.write("test1", "Same first line\nBody two")
    after = MemorySnapshot.take(store)

    added, removed, changed = diff_snapshots(before, after)
    assert "test1" in changed
    assert added == []
    assert removed == []


def test_diff_detects_tag_change(tmp_path):
    store = MemoryStore(root=tmp_path / "memory")
    store.write("test1", "Content", tags=["tag1"])

    before = MemorySnapshot.take(store)

    store.write("test1", "Content", tags=["tag2"])
    after = MemorySnapshot.take(store)

    added, removed, changed = diff_snapshots(before, after)
    assert "test1" in changed


def test_diff_unchanged_and_basic_ops(tmp_path):
    store = MemoryStore(root=tmp_path / "memory")
    store.write("test1", "Content 1")
    store.write("test2", "Content 2")

    before = MemorySnapshot.take(store)

    # Unchanged
    after_same = MemorySnapshot.take(store)
    assert diff_snapshots(before, after_same) == ([], [], [])

    # Added
    store.write("test3", "Content 3")
    after_add = MemorySnapshot.take(store)
    added, removed, changed = diff_snapshots(before, after_add)
    assert added == ["test3"]
    assert removed == []
    assert changed == []

    # Removed
    store.delete("test1")
    after_rem = MemorySnapshot.take(store)
    added, removed, changed = diff_snapshots(before, after_rem)
    assert removed == ["test1"]


def test_run_dream_e2e_detects_change(tmp_path):
    # Setup environment
    mem_root = tmp_path / "memory"
    log_dir = tmp_path / "logs" / "dreams"

    # Mock Config to use our tmp log dir
    with patch("evi.dream.DREAM_LOG_DIR", log_dir), \
         patch("evi.dream.ensure_dirs"):

        store = MemoryStore(root=mem_root)
        store.write("target_mem", "First line\nOld body")

        # Stub run_subagent to modify the memory
        def mock_run_subagent(**kwargs):
            store.write("target_mem", "First line\nNew body")
            return "I updated target_mem"

        with patch("evi.dream.run_subagent", side_effect=mock_run_subagent), \
             patch("evi.dream.TranscriptStore") as mock_ts_cls:

            # Mock transcripts to be empty
            mock_ts = mock_ts_cls.return_value
            mock_ts.iter_since.return_value = []

            report = run_dream(
                hours=24,
                config=Config(),
                memory=store,
                transcripts=mock_ts
            )

            assert "target_mem" in report.changed

            log_content = report.log_path.read_text(encoding="utf-8")
            assert "## Changed (1)" in log_content
            assert "  ~ target_mem" in log_content
