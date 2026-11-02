"""reasoning_text: the reasoning a server sends beside `content` (the native
reasoning channel), under every field name it goes by."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion_chunk import ChoiceDelta

from evi.reasoning import REASONING_FIELDS, reasoning_text


def test_the_field_names_are_reasoning_content_then_reasoning():
    assert REASONING_FIELDS == ("reasoning_content", "reasoning")


@pytest.mark.parametrize("name", ["reasoning_content", "reasoning"])
def test_an_attribute_is_read(name):
    # A duck-typed delta (evi.llm.cli_agent.delta_chunk, the test fakes).
    assert reasoning_text(SimpleNamespace(content=None, **{name: "step 1"})) == "step 1"


@pytest.mark.parametrize("name", ["reasoning_content", "reasoning"])
def test_a_mapping_key_is_read(name):
    assert reasoning_text({"content": "", name: "step 1"}) == "step 1"


@pytest.mark.parametrize("name", ["reasoning_content", "reasoning"])
def test_the_openai_sdk_delta_keeps_the_undeclared_field(name):
    # The SDK's ChoiceDelta declares neither field; it keeps them as extras.
    delta = ChoiceDelta.model_validate({"content": None, name: "hmm"})
    assert reasoning_text(delta) == "hmm"


def test_a_streamed_sdk_chunk_delta_is_read():
    chunk = ChatCompletionChunk.model_validate({
        "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": {"reasoning": "think"}, "finish_reason": None}],
    })
    assert reasoning_text(chunk.choices[0].delta) == "think"


def test_a_whole_sdk_message_is_read():
    msg = ChatCompletionMessage.model_validate(
        {"role": "assistant", "content": "42", "reasoning_content": "6 times 7"})
    assert reasoning_text(msg) == "6 times 7"


def test_model_extra_is_read_when_the_attribute_is_missing():
    # An object that only keeps the field in model_extra, as pydantic does.
    obj = SimpleNamespace(content=None, model_extra={"reasoning": "via extra"})
    assert reasoning_text(obj) == "via extra"


def test_both_names_give_the_first_once_not_both():
    # OpenRouter sends the same text under both names: never doubled.
    obj = SimpleNamespace(reasoning_content="same", reasoning="same")
    assert reasoning_text(obj) == "same"
    assert reasoning_text({"reasoning_content": "a", "reasoning": "b"}) == "a"


def test_an_empty_first_field_falls_through_to_the_second():
    # vLLM's warning: reasoning_content empty while reasoning holds the text.
    assert reasoning_text(SimpleNamespace(reasoning_content="", reasoning="x")) == "x"
    assert reasoning_text({"reasoning_content": None, "reasoning": "x"}) == "x"


@pytest.mark.parametrize("obj", [
    None,
    SimpleNamespace(content="answer"),
    {"content": "answer"},
    SimpleNamespace(reasoning={"effort": "high"}),
    {"reasoning": ["a"], "reasoning_content": 3},
    ChoiceDelta.model_validate({"content": "answer"}),
])
def test_no_reasoning_is_the_empty_string(obj):
    assert reasoning_text(obj) == ""


def test_the_text_is_returned_as_it_is():
    # No stripping or tag handling: a literal tag in reasoning is just text.
    text = "  see </think> and <think>\n"
    assert reasoning_text({"reasoning": text}) == text
