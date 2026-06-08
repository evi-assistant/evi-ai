"""Tests for fine-tune dataset export (Phase 90)."""

from __future__ import annotations

import json

from evi import finetune


def _write_session(root, day, sid, entries):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for i, e in enumerate(entries):
            row = {"session": sid, "ts": float(i + 1), **e}
            f.write(json.dumps(row) + "\n")
    return p


# ---- pure transform ------------------------------------------------------


def test_history_to_example_basic():
    hist = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    ex = finetune.history_to_example(hist)
    assert ex == {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]}


def test_history_to_example_system_prepended():
    hist = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    ex = finetune.history_to_example(hist, system="You are eVi.")
    assert ex["messages"][0] == {"role": "system", "content": "You are eVi."}


def test_history_to_example_none_when_no_exchange():
    assert finetune.history_to_example([{"role": "user", "content": "lonely"}]) is None
    assert finetune.history_to_example([]) is None


def test_pure_toolcall_turn_dropped_by_default():
    hist = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "fs", "arguments": "{}"}}]},
        {"role": "tool", "content": "ok", "name": "fs", "tool_call_id": "1"},
        {"role": "assistant", "content": "done"},
    ]
    ex = finetune.history_to_example(hist)
    roles = [m["role"] for m in ex["messages"]]
    assert roles == ["user", "assistant"]  # tool-call + tool result dropped
    assert ex["messages"][1]["content"] == "done"


def test_include_tools_keeps_them():
    hist = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "fs", "arguments": "{}"}}]},
        {"role": "tool", "content": "ok", "name": "fs", "tool_call_id": "1"},
        {"role": "assistant", "content": "done"},
    ]
    ex = finetune.history_to_example(hist, include_tools=True)
    roles = [m["role"] for m in ex["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert "tool_calls" in ex["messages"][1]


def test_multipart_user_content_flattened():
    hist = [
        {"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:zzz"}},
        ]},
        {"role": "assistant", "content": "a cat"},
    ]
    ex = finetune.history_to_example(hist)
    assert ex["messages"][0] == {"role": "user", "content": "describe"}


# ---- export over a transcripts tree --------------------------------------


def test_export_dataset(tmp_path):
    root = tmp_path / "transcripts"
    _write_session(root, "2026-06-01", "s1", [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "bye"},
        {"role": "assistant", "content": "cya"},
    ])
    _write_session(root, "2026-06-01", "empty", [{"role": "system", "content": "x"}])

    out = tmp_path / "ds.jsonl"
    written, seen = finetune.export_dataset(out, root=root)
    assert (written, seen) == (1, 2)  # empty session considered but skipped
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    ex = json.loads(lines[0])
    assert [m["role"] for m in ex["messages"]] == ["user", "assistant", "user", "assistant"]


def test_export_session_filter(tmp_path):
    root = tmp_path / "transcripts"
    _write_session(root, "2026-06-01", "keep", [
        {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
    ])
    _write_session(root, "2026-06-01", "skip", [
        {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"},
    ])
    out = tmp_path / "ds.jsonl"
    written, seen = finetune.export_dataset(out, root=root, sessions=["keep"])
    assert (written, seen) == (1, 1)


def test_export_min_turns(tmp_path):
    root = tmp_path / "transcripts"
    _write_session(root, "2026-06-01", "short", [
        {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
    ])
    out = tmp_path / "ds.jsonl"
    written, _ = finetune.export_dataset(out, root=root, min_user_turns=2)
    assert written == 0
