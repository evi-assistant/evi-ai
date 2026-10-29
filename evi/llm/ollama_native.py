"""Chat completions through Ollama's native API (``POST /api/chat``).

Ollama's OpenAI-compatible ``/v1`` ignores ``options.num_ctx``, so a model
there runs with whatever window the server picks (``OLLAMA_CONTEXT_LENGTH``,
often 4K) and the start of a longer prompt is silently dropped. The native API
takes the window with every request. `OllamaNativeClient` keeps the OpenAI SDK
shape the agent loop speaks (``client.chat.completions.create(**kwargs)``,
streamed chunks with ``choices[0].delta`` and a closing usage chunk, the
`evi.llm.cli_agent` chunk helpers) and translates both ways:

- messages: multimodal content to text plus ``images``; an assistant's tool
  calls with their arguments as objects; a tool result with ``tool_name``;
- tools as they are (the same function-schema shape);
- options: ``num_ctx`` (from ``extra_body["options"]``, which the agent fills
  with ``[llm] context_size``), ``temperature``, ``top_p``, ``num_predict``
  (``max_tokens``), ``seed``, ``stop``, the penalties; ``response_format`` as
  ``format``; ``think`` / ``reasoning_effort`` as ``think``;
- replies: ``message.content`` as text, ``message.thinking`` inside
  ``<think>`` tags (the agent's think parser takes it out), whole tool calls
  (Ollama sends each one complete), ``done_reason``, and the prompt and reply
  token counts as usage.

Everything else on the client (``models``, ``embeddings``) is the OpenAI
client for ``/v1``, unchanged.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Iterator

import httpx

from evi.llm.cli_agent import delta_chunk, tool_call_delta, usage_chunk

_THINK_LEVELS = ("low", "medium", "high")


class OllamaError(RuntimeError):
    """A request Ollama refused or failed; the message is Ollama's own."""


def _content_and_images(content: Any) -> tuple[str, list[str]]:
    """OpenAI message content as Ollama's text and base64 ``images``."""
    if isinstance(content, str) or content is None:
        return content or "", []
    texts: list[str] = []
    images: list[str] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(str(part.get("text") or ""))
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if isinstance(url, str) and url.startswith("data:") and "," in url:
                images.append(url.split(",", 1)[1])
    return "\n".join(texts), images


def _arguments(raw: Any) -> dict:
    """A tool call's arguments as the object Ollama wants ({} if unreadable)."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def native_messages(messages: list[dict]) -> list[dict]:
    """OpenAI chat messages in Ollama's native shape."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        text, images = _content_and_images(m.get("content"))
        msg: dict[str, Any] = {"role": role, "content": text}
        if images:
            msg["images"] = images
        if role == "assistant" and m.get("tool_calls"):
            msg["tool_calls"] = [
                {"function": {"name": (tc.get("function") or {}).get("name", ""),
                              "arguments": _arguments((tc.get("function") or {}).get("arguments"))}}
                for tc in m["tool_calls"]
            ]
        if role == "tool" and m.get("name"):
            msg["tool_name"] = m["name"]
        out.append(msg)
    return out


def native_body(kwargs: dict) -> dict:
    """The ``/api/chat`` request body for chat-completions `kwargs`."""
    extra = dict(kwargs.get("extra_body") or {})
    options: dict[str, Any] = dict(extra.pop("options", None) or {})
    for ours, theirs in (("temperature", "temperature"), ("top_p", "top_p"),
                         ("seed", "seed"), ("presence_penalty", "presence_penalty"),
                         ("frequency_penalty", "frequency_penalty")):
        if kwargs.get(ours) is not None:
            options[theirs] = kwargs[ours]
    limit = kwargs.get("max_completion_tokens") or kwargs.get("max_tokens")
    if limit:
        options["num_predict"] = int(limit)
    stop = kwargs.get("stop")
    if stop:
        options["stop"] = [stop] if isinstance(stop, str) else list(stop)
    body: dict[str, Any] = {
        "model": kwargs.get("model", ""),
        "messages": native_messages(list(kwargs.get("messages") or [])),
        "stream": bool(kwargs.get("stream", False)),
    }
    if kwargs.get("tools"):
        body["tools"] = kwargs["tools"]
    if options:
        body["options"] = options
    fmt = kwargs.get("response_format")
    if isinstance(fmt, dict):
        if fmt.get("type") == "json_object":
            body["format"] = "json"
        elif fmt.get("type") == "json_schema":
            schema = (fmt.get("json_schema") or {}).get("schema")
            body["format"] = schema if isinstance(schema, dict) else "json"
    if "think" in extra:
        body["think"] = extra.pop("think")
    effort = extra.pop("reasoning_effort", None)
    if effort and "think" not in body:
        body["think"] = effort if effort in _THINK_LEVELS else True
    if "keep_alive" in extra:
        body["keep_alive"] = extra.pop("keep_alive")
    return body


def _error_text(resp: httpx.Response) -> str:
    try:
        data = json.loads(resp.read() or b"{}")
    except (ValueError, httpx.HTTPError):
        data = {}
    message = data.get("error") if isinstance(data, dict) else None
    return f"Ollama {resp.status_code}: {message or resp.reason_phrase}"


class _Stream:
    """The streamed reply as chunks: text (thinking in <think> tags), whole
    tool calls, a finish reason, then usage. `close` ends the request."""

    def __init__(self, client: httpx.Client, url: str, body: dict) -> None:
        self._ctx = client.stream("POST", url, json=body)
        self._resp = self._ctx.__enter__()
        if self._resp.status_code >= 400:
            message = _error_text(self._resp)
            self.close()
            raise OllamaError(message)
        self._iter = self._chunks()

    def __iter__(self) -> "_Stream":
        return self

    def __next__(self):
        return next(self._iter)

    def close(self) -> None:
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 — closing a half-read response
            pass

    def _chunks(self) -> Iterator[Any]:
        thinking = False
        calls = 0
        try:
            for line in self._resp.iter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise OllamaError(f"Ollama: {data['error']}")
                msg = data.get("message") or {}
                think_text, text = msg.get("thinking") or "", msg.get("content") or ""
                piece = ""
                if think_text:
                    piece += ("" if thinking else "<think>") + think_text
                    thinking = True
                if text:
                    piece += ("</think>" if thinking else "") + text
                    thinking = False
                if piece:
                    yield delta_chunk(content=piece)
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    yield tool_call_delta(
                        calls, f"call_{calls}", str(fn.get("name", "")),
                        args if isinstance(args, str) else json.dumps(args or {}))
                    calls += 1
                if data.get("done"):
                    if thinking:
                        yield delta_chunk(content="</think>")
                    reason = "tool_calls" if calls else (data.get("done_reason") or "stop")
                    yield delta_chunk(finish_reason=reason)
                    yield usage_chunk(int(data.get("prompt_eval_count") or 0),
                                      int(data.get("eval_count") or 0))
                    return
        finally:
            self.close()


class _Completions:
    def __init__(self, owner: "OllamaNativeClient") -> None:
        self._owner = owner

    def create(self, **kwargs: Any):
        owner = self._owner
        body = native_body(kwargs)
        url = f"{owner.native_base}/api/chat"
        if body["stream"]:
            return _Stream(owner.http, url, body)
        try:
            resp = owner.http.post(url, json=body)
        except httpx.HTTPError as e:
            raise OllamaError(f"Ollama: {e}") from e
        if resp.status_code >= 400:
            raise OllamaError(_error_text(resp))
        data = resp.json()
        msg = data.get("message") or {}
        tool_calls = [
            SimpleNamespace(id=f"call_{i}", type="function", function=SimpleNamespace(
                name=str((tc.get("function") or {}).get("name", "")),
                arguments=json.dumps((tc.get("function") or {}).get("arguments") or {})))
            for i, tc in enumerate(msg.get("tool_calls") or [])
        ]
        content = msg.get("content") or ""
        if msg.get("thinking"):
            content = f"<think>{msg['thinking']}</think>{content}"
        message = SimpleNamespace(role="assistant", content=content,
                                  tool_calls=tool_calls or None)
        prompt, completion = int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
        usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                                total_tokens=prompt + completion)
        reason = "tool_calls" if tool_calls else (data.get("done_reason") or "stop")
        return SimpleNamespace(choices=[SimpleNamespace(index=0, message=message,
                                                        finish_reason=reason)], usage=usage)


class OllamaNativeClient:
    """An OpenAI-SDK-shaped client whose chat completions go to Ollama's
    native ``/api/chat``; any other attribute is the `openai` client's."""

    def __init__(self, openai_client: Any, native_base: str, timeout: float) -> None:
        self._openai = openai_client
        self.native_base = native_base.rstrip("/")
        self.http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))
        self.chat = SimpleNamespace(completions=_Completions(self))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._openai, name)
