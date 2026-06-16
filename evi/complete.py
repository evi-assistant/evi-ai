"""Local FIM (fill-in-the-middle) code completion — eVi as a local Tab backend.

Cursor's "Tab" is a hosted next-edit model. eVi can't ship that, but the
local code models eVi already runs (Qwen2.5-Coder, DeepSeek-Coder, StarCoder2,
Codestral, …) expose FIM/infill, and llama.cpp / Ollama / LM Studio all serve
the legacy ``/v1/completions`` endpoint with a ``suffix`` parameter. So eVi can
be a fully-local autocomplete backend: give it the text before and after the
cursor and it returns the insertion.

This is the engine + a CLI (`evi complete`) and an HTTP endpoint
(`/api/complete`); a thin editor extension (VS Code / LSP) is the client that
turns this into ghost-text — kept out of the Python package on purpose.
"""

from __future__ import annotations

from pathlib import Path

# Substring hints for models that do FIM/infill well.
_FIM_HINTS = (
    "coder", "code-", "codellama", "codegemma", "starcoder", "stable-code",
    "deepseek-coder", "codestral", "qwen2.5-coder", "qwen3-coder", "granite-code",
)

_DEFAULT_MAX_TOKENS = 128


def supports_fim(model_id: str) -> bool:
    """Heuristic: does this model id look like a FIM-capable code model?"""
    if not model_id:
        return False
    return any(h in model_id.lower() for h in _FIM_HINTS)


def pick_fim_model(cfg) -> str:
    """Choose a FIM-capable model from config: an explicit coder fast_model
    wins, else the main model if it's a coder, else "" (caller errors)."""
    fm = (cfg.llm.fast_model or "").strip()
    if supports_fim(fm):
        return fm
    if supports_fim(cfg.llm.model):
        return cfg.llm.model
    # Fall back to the main model anyway — many servers still infill acceptably.
    return cfg.llm.model


def complete(
    prefix: str,
    suffix: str = "",
    *,
    config=None,
    model: str = "",
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> str:
    """Return the model's fill-in-the-middle insertion between prefix/suffix.

    Uses the legacy completions endpoint (prompt=prefix, suffix=suffix), which
    llama.cpp / Ollama / LM Studio implement as FIM for code models. Raises on
    a backend error so callers can surface it."""
    from evi.config import Config
    from evi.llm.client import make_client

    cfg = config or Config.load()
    mid = model or pick_fim_model(cfg)
    client = make_client(cfg.llm)
    kwargs: dict = {
        "model": mid,
        "prompt": prefix,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    if suffix:
        kwargs["suffix"] = suffix
    resp = client.completions.create(**kwargs)
    return resp.choices[0].text if resp.choices else ""


def complete_at(
    path: str | Path, line: int, col: int, *, config=None, model: str = "",
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> str:
    """Complete at a 1-based (line, col) cursor position in a file."""
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    # Byte/char offset of the cursor.
    idx = sum(len(ln) for ln in lines[: max(line - 1, 0)]) + max(col - 1, 0)
    idx = max(0, min(idx, len(text)))
    return complete(text[:idx], text[idx:], config=config, model=model, max_tokens=max_tokens)
