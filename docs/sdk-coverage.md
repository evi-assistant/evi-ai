# SDK coverage + borrowable features

A review of which OpenAI Chat Completions features we've wired up,
which are still useful and unbuilt, and what we could borrow from other
vendors' SDKs (Anthropic, Google, Bedrock, Cohere, xAI).

Status legend: ✅ done · ➕ proposed · ❌ not relevant for local-first

## OpenAI SDK — what we have, what we don't

### ✅ Already supported

| Feature | Where |
|---|---|
| Streaming chat completions | `Agent.chat` since Phase 1 |
| Function calling (tools) | Phase 1 |
| Parallel tool calls | Phase 16.2 |
| `response_format` (json_object + json_schema) | Phase 18.1 |
| `tool_choice` (auto / none / required / specific) | Phase 18.2 |
| `stop` sequences | Phase 18.3 |
| `seed` | Phase 18.3 |
| `top_p`, `presence_penalty`, `frequency_penalty` | Phase 18.3 |
| `stream_options.include_usage` (real token counts) | Phase 18.4 |
| Vision input (`image_url`) | Phase 15 |
| Embeddings | Phase 16.4 |
| `extra_body.reasoning_effort` for o-series / DeepSeek-R1 / Qwen3 | Phase 17.1 |
| Multi-tool dispatch in one turn | Phase 16.2 |

### ➕ Useful and unbuilt

| Feature | Why it's useful (locally) | Effort |
|---|---|---|
| ✅ **`prediction`** | Speculative-decoding hint shipped Phase 34 (0.16.0): `Agent.chat(prediction=...)`, `/predict` slash, `evi edit <file> "<instr>"` command, web `ChatRequest.prediction`. First-round-only semantics so post-tool re-rolls drop the stale hint. | Shipped. |
| ✅ **`logprobs` / `top_logprobs`** | Shipped Phase 37 (0.19.0): config `[llm] logprobs` + `top_logprobs`; a `LogProbs` event carries per-token data + avg/min/low-count; CLI prints a confidence line, web shows a per-bubble confidence badge. | Shipped. |
| ✅ **`logit_bias`** | Shipped Phase 35 (0.17.0): config `[llm] logit_bias` as a JSON string (`'{"123": -100}'`, clamped to ±100), per-turn `Agent.chat(logit_bias=...)`, web `ChatRequest.logit_bias`. Invalid JSON is dropped, not fatal. | Shipped. |
| ✅ **`n` (multiple completions)** | Shipped Phase 35 (0.17.0): `Agent.complete_variants(prompt, n)` non-streaming + stateless, plus `evi variants "<prompt>" -n 3`. Backends that ignore `n` return one variant; we surface a note. | Shipped. |
| ✅ **Audio input content parts** | Shipped Phase 37 (0.19.0): `evi/audio_input.py` builds `input_audio` parts for omni models (`model_supports_audio`); `Agent.chat(audio=...)`, web `ChatRequest.audio`, CLI `/audioraw`. Non-omni models degrade to local Whisper transcription folded into the text. | Shipped. |
| ✅ **`max_completion_tokens`** | Shipped Phase 35 (0.17.0): config `[llm] max_completion_tokens` (0=unset). When >0 we send it INSTEAD of `max_tokens` (reasoning models reject the latter). | Shipped. |
| ✅ **`parallel_tool_calls=false`** flag | Shipped Phase 35 (0.17.0): config `[llm] parallel_tool_calls` (default True) + per-turn `Agent.chat(parallel_tool_calls=...)` + web field. Only forwarded when False AND tools are present. | Shipped. |
| **Responses API** (`responses.create`) | OpenAI's new shape replacing chat completions. Cleaner streaming + tool model. Some local backends starting to expose it. Big migration but future-proofs the core. | Large. |

### ❌ Not relevant for local-first

| Feature | Why we skip |
|---|---|
| Realtime API (websocket voice) | Cloud-only; we have a local voice loop. |
| Files / Assistants / Threads / Vector Stores | Cloud-only state; we have local memory + index. |
| Batch API | Cloud-only. |
| Moderation API | Cloud-only. See "Guardrails" below for a local alternative. |
| Built-in tools (code interpreter, web search, file search) | Cloud-hosted; we have local equivalents (`run_python`, `web_search`, `find_in_project`). |
| DALL-E image generation | Cloud; we use ComfyUI. |
| Fine-tuning | Cloud-side; local users fine-tune via llama-factory / unsloth. |
| `service_tier`, `store`, `user` flags | Cloud-only billing knobs. |

## Other vendors — what's worth borrowing

### Anthropic Claude SDK

| Feature | Verdict |
|---|---|
| Extended thinking (`<think>`) | ✅ Have it — Phase 16.1. |
| Computer use (mouse/keyboard) | ✅ Have it — Phase 12. |
| **Prompt caching** (`cache_control: {"type": "ephemeral"}`) | ✅ **Shipped Phase 37 (0.19.0)** as `[llm] cache_prompt`. When on, forwards `extra_body.cache_prompt=true` — llama.cpp's server reuses the KV cache for the stable prefix; vLLM does the same via `--enable-prefix-caching`. Our system+memory+project prefix is already stable across turns. |
| **Citations** (`citations: enabled`) | ✅ **Shipped Phase 30 (0.13.0).** `read_file` / `find_in_project` / `web_fetch` return a `ToolOutput(text, citations)`; the `ToolResult` event carries them and the web UI renders `[1] path/to/file:42` chips under each tool bubble. |
| Files API | ❌ Cloud-only. We have local fs tools. |
| Web search / code execution tools | ❌ Cloud-hosted. We have local versions. |
| PDF in messages | ❌ We have `read_pdf` tool — functionally equivalent. |

### Google Gemini SDK

| Feature | Verdict |
|---|---|
| System instructions | ✅ Have. |
| `response_schema` (Pydantic → JSON schema) | ✅ Have via `response_format`. |
| **Safety settings** (harassment/hate/sexual/dangerous content filters) | ✅ **Shipped Phase 37 (0.19.0)** as the local guardrails layer (`evi/guardrails.py`, `~/.evi/guardrails.toml`). Regex block/redact rules over input + output; off by default. |
| **Explicit caching** (`caching.create`) | ➕ Same as Anthropic's prompt caching. One concept, two SDKs. |
| Code execution | ❌ Sandboxed cloud-side Python; we have local `run_python`. |
| File API | ❌ Cloud. |
| Live API (bidirectional voice/video) | ❌ Cloud; we have voice loop locally. |
| Long-context (1M+) | ✅ We track `context_size` already. |
| Automatic function call dispatch | ✅ Have it in `Agent.chat`. |

### Amazon Bedrock

| Feature | Verdict |
|---|---|
| Converse API (unified shape) | ✅ Equivalent — our `Backend` protocol. |
| Knowledge bases (managed RAG) | ❌ We have local `find_in_project`. |
| Agents | ✅ Have it. |
| **Guardrails** (request/response content filters + PII detection) | ✅ **Shipped Phase 37 (0.19.0)** — local regex guardrails layer. See Gemini row. |
| Prompt management (versioned templates) | ➕ Borrow lightly. We have skills + slash commands which is half of this. A `~/.evi/prompts/` versioned store could fill the rest. |
| Provisioned throughput / inference profiles | ❌ Cloud-only. |

### Cohere

| Feature | Verdict |
|---|---|
| **Rerank API** | ✅ **Shipped Phase 30 (0.13.0).** `evi/tools/rerank.py` re-ranks `find_in_project` candidates with a local cross-encoder for better ordering than raw cosine. |
| Embed | ✅ Have. |
| Generate / Chat | ✅ Have via OpenAI shape. |

### xAI Grok

OpenAI-compatible API plus cloud-hosted **Live search** + **Image generation**.
Nothing borrow-worthy for local-first.

### Mistral La Plateforme

OpenAI-compatible, no novel features beyond what we have.

### Together / Fireworks / DeepInfra

OpenAI-compatible. Some expose `prediction` (speculative decoding) —
covered under OpenAI features above.

## Recommended pickup order

Ranked by user value, smallest delta first:

| # | Feature | Source | Effort | Value |
|---|---|---|---|---|
| # | Feature | Source | Status | Value |
|---|---|---|---|---|
| 1 | ✅ `prediction` (speculative decoding) | OpenAI | shipped 0.16.0 | High (code edits) |
| 2 | ✅ `parallel_tool_calls=false` flag | OpenAI | shipped 0.17.0 | Medium |
| 3 | ✅ `max_completion_tokens` for reasoning models | OpenAI | shipped 0.17.0 | Medium |
| 4 | ✅ `logit_bias` | OpenAI | shipped 0.17.0 | Low |
| 5 | ✅ `n`-best-of variants (`evi variants`) | OpenAI | shipped 0.17.0 | Medium |
| 6 | ✅ Citations (read_file, find_in_project, web_fetch) | Anthropic | shipped 0.13.0 | High |
| 7 | ✅ Local rerank tool over `find_in_project` | Cohere | shipped 0.13.0 | High |
| 8 | ✅ Prompt caching markers (`cache_prompt` KV reuse hint) | Anthropic/Gemini | shipped 0.19.0 | Medium |
| 9 | ✅ `logprobs` / `top_logprobs` + confidence surfacing | OpenAI | shipped 0.19.0 | Medium |
| 10 | ✅ Audio content input parts (+ STT degrade) | OpenAI | shipped 0.19.0 | Medium |
| 11 | ✅ Guardrails layer (regex content filter) | Bedrock/Gemini | shipped 0.19.0 | Medium (shared installs) |
| 12 | ⬜ eVi-as-MCP-server publish | cross-cutting | L | High (integration story) |
| 13 | ⬜ Responses API migration | OpenAI | XL | High (future-proof) |

**Score: 11 / 13 shipped.** Only the two big-ticket items remain:
MCP-server-publish (L) and the Responses API migration (XL).

S=small (≤200 LOC), M=medium (≤500 LOC), L=large (≤1000 LOC), XL=larger.

## What we're not building

Cloud-side state (Files / Assistants / Threads / Vector Stores / Batch
/ Moderation / Realtime / DALL-E / built-in cloud tools) is
deliberately out of scope. eVi is local-first; pushing user data into a
vendor's cloud-state surface defeats that.

If a user wants any of those, they can already point eVi at a
cloud-OpenAI-compatible endpoint via the `openai_compat` backend. That
gives access to the underlying provider's hosted features without
needing us to re-implement them.
