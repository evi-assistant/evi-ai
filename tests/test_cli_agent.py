"""Shared cli_agent shim: the Claude-Code stream-event helpers (used by the amp +
qwen backends) — usage extraction, error extraction, and event → chunk emission."""

from __future__ import annotations

import queue

from evi.llm.cli_agent import cc_error_message, cc_usage, emit_claude_events


# --- cc_usage ----------------------------------------------------------------


def test_cc_usage_sums_input_and_cache_buckets():
    assert cc_usage({"input_tokens": 10, "cache_read_input_tokens": 2,
                     "cache_creation_input_tokens": 3, "output_tokens": 4}) == (15, 4)


def test_cc_usage_prompt_tokens_fallback():
    assert cc_usage({"prompt_tokens": 7, "completion_tokens": 1}) == (7, 1)


def test_cc_usage_empty():
    assert cc_usage({}) == (0, 0)
    assert cc_usage(None) == (0, 0)


# --- cc_error_message --------------------------------------------------------


def test_cc_error_message_nested_dict():
    assert cc_error_message({"error": {"message": "No auth type is selected"}}) == "No auth type is selected"


def test_cc_error_message_string_and_result_fallback():
    assert cc_error_message({"error": "flat error"}) == "flat error"
    assert cc_error_message({"result": "some result text"}) == "some result text"
    assert cc_error_message({}) == "turn failed"


# --- emit_claude_events ------------------------------------------------------


def _drain(events):
    q: queue.Queue = queue.Queue()
    result = emit_claude_events(events, q)
    items = []
    while not q.empty():
        items.append(q.get())
    return result, items


def test_emit_streams_assistant_text_and_reads_usage():
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello "},
            {"type": "tool_use", "name": "bash", "input": {}},   # ignored
            {"type": "text", "text": "world"},
        ]}},
        {"type": "result", "subtype": "success",
         "usage": {"input_tokens": 8, "output_tokens": 3}, "result": "hello world"},
    ]
    (saw_result, err, ptok, ctok), items = _drain(events)
    contents = [c.choices[0].delta.content for c in items
                if c.choices and c.choices[0].delta and c.choices[0].delta.content]
    assert contents == ["hello ", "world"]        # tool_use skipped; result text NOT re-emitted
    assert saw_result and err is None and (ptok, ctok) == (8, 3)


def test_emit_result_only_emits_final_text():
    events = [{"type": "result", "subtype": "success",
               "usage": {"output_tokens": 2}, "result": "just the result"}]
    (saw_result, err, ptok, ctok), items = _drain(events)
    contents = [c.choices[0].delta.content for c in items
                if c.choices and c.choices[0].delta and c.choices[0].delta.content]
    assert contents == ["just the result"] and saw_result and err is None and ctok == 2


def test_emit_result_error():
    events = [{"type": "result", "subtype": "error_during_execution", "is_error": True,
               "error": {"message": "boom"}, "usage": {}}]
    (saw_result, err, _p, _c), _items = _drain(events)
    assert saw_result and err == "boom"
