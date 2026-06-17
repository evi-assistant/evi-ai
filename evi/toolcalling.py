"""Tool-calling (function-calling) capability detection.

eVi is an agent: the chat loop only works if the model emits OpenAI-style
``tool_calls``. Most modern instruct models do, but plenty of small, base, or
code-FIM models do not — and when they don't the agent silently produces prose
instead of acting. There's no way to ask a local backend "do you tool-call?",
so this is a best-effort substring heuristic over known-good families, mirroring
``vision`` / ``reasoning`` / ``complete.supports_fim`` / ``audio_input``.

A True means "this family is known to do function calling reliably" — a chip
worth showing. A False is "unknown / unlikely", i.e. eVi won't promise it; the
agent may still work if the backend bolts tool-calling on (many do).
"""

from __future__ import annotations

# Substring hints for model families with reliable OpenAI-style tool calling.
# Local-first families first, then the obvious cloud ones reached via
# openai_compat. Kept lowercase; matched against the lowercased model id.
_TOOL_HINTS = (
    # Local instruct families with native tool calling
    "qwen2.5", "qwen3", "qwen-2.5", "qwen-3", "qwq",
    "llama-3.1", "llama3.1", "llama-3.2", "llama3.2", "llama-3.3", "llama3.3",
    "mistral", "mixtral", "ministral", "magistral", "devstral", "codestral",
    "nemo", "command-r", "command-a", "cohere",
    "hermes", "firefunction", "functionary", "xlam", "watt-tool",
    "granite-3", "granite3", "deepseek-v3", "deepseek-chat", "deepseek-r1",
    "glm-4", "glm4", "gpt-oss", "smollm3", "ai21", "jamba",
    # Cloud families reached via openai_compat / responses
    "gpt-4", "gpt-5", "o1", "o3", "o4", "claude", "grok", "gemini",
)

# Families that look like the above but specifically do NOT tool-call well —
# checked first so e.g. a base/FIM-only "deepseek-coder" isn't a false positive.
_TOOL_ANTI_HINTS = (
    "deepseek-coder", "starcoder", "codellama", "code-llama",
    "embed", "embedding", "bge-", "rerank", "guard", "-base",
)


def model_supports_tools(model_id: str) -> bool:
    """Heuristic: is this model family known to do OpenAI-style tool calling?"""
    if not model_id:
        return False
    mid = model_id.lower()
    if any(a in mid for a in _TOOL_ANTI_HINTS):
        return False
    return any(h in mid for h in _TOOL_HINTS)
