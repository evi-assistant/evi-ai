"""models.dev catalog — ground-truth model metadata, local-first.

eVi's capability detection (vision/reasoning/tools/infill/…) and context-window
sizing are best-effort substring heuristics. [models.dev](https://models.dev) is
an open, community-maintained DB of models with exact metadata: context limit,
input/output modalities, tool-calling + reasoning flags, and (for hosted models)
pricing. This module consults a catalog snapshot so those answers are grounded —
while staying local-first:

- A small **baked snapshot** ships in the package (`evi/data/models-catalog.json`)
  so it works offline out of the box.
- `evi models refresh` downloads the full catalog to `~/.evi/models-catalog.json`,
  which then takes precedence.
- Lookups fall back to the existing heuristics when a model isn't in the catalog
  (e.g. an exotic local GGUF tag), so nothing regresses.

The catalog is the models.dev `api.json` shape: ``{provider: {models: {id: {...}}}}``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from evi.config import HOME

DEFAULT_URL = "https://models.dev/api.json"
USER_CATALOG = HOME / "models-catalog.json"
BAKED_CATALOG = Path(__file__).with_name("data") / "models-catalog.json"


@dataclass(frozen=True)
class ModelInfo:
    id: str
    context: int = 0          # max input context (tokens), 0 = unknown
    output: int = 0           # max output tokens
    tool_call: bool = False
    reasoning: bool = False
    vision: bool = False      # image in the input modalities
    audio: bool = False       # audio in the input modalities
    input_cost: float = 0.0   # USD per 1M input tokens (hosted only)
    output_cost: float = 0.0
    open_weights: bool = False


def _coerce(model_id: str, m: dict) -> ModelInfo:
    """Build a ModelInfo from one models.dev model record (defensive about the
    exact field names — the schema has shifted over time)."""
    limit = m.get("limit") or {}
    modalities = m.get("modalities") or {}
    inputs = [str(x).lower() for x in (modalities.get("input") or [])]
    cost = m.get("cost") or {}

    def _num(*keys) -> float:
        for src in (limit, cost, m):
            for k in keys:
                v = src.get(k) if isinstance(src, dict) else None
                if isinstance(v, (int, float)):
                    return float(v)
        return 0.0

    return ModelInfo(
        id=str(m.get("id") or model_id),
        context=int(_num("context")),
        output=int(_num("output")),
        tool_call=bool(m.get("tool_call", m.get("tool_use", False))),
        reasoning=bool(m.get("reasoning", False)),
        vision="image" in inputs or bool(m.get("attachment", False) and "image" in inputs),
        audio="audio" in inputs,
        input_cost=_num("input"),
        output_cost=_num("output"),
        open_weights=bool(m.get("open_weights", False)),
    )


def _flatten(raw: dict) -> dict[str, ModelInfo]:
    """Flatten the nested ``{provider: {models: {id: {...}}}}`` catalog into a
    single id→ModelInfo map (lowercased keys). Also accepts an already-flat
    ``{id: {...}}`` map for convenience."""
    out: dict[str, ModelInfo] = {}
    if not isinstance(raw, dict):
        return out
    for _prov, pdata in raw.items():
        if not isinstance(pdata, dict):
            continue
        models = pdata.get("models") if isinstance(pdata.get("models"), dict) else None
        if models is None:
            # Flat shape: this top-level value is itself a model record.
            if "id" in pdata or "limit" in pdata or "modalities" in pdata:
                out[str(_prov).lower()] = _coerce(str(_prov), pdata)
            continue
        for mid, mrec in models.items():
            if isinstance(mrec, dict):
                out[str(mid).lower()] = _coerce(str(mid), mrec)
    return out


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, ModelInfo]:
    """The active catalog: the downloaded user copy if present, else the baked
    snapshot. Cached; call :func:`reset_cache` after a refresh."""
    for path in (USER_CATALOG, BAKED_CATALOG):
        try:
            if path.is_file():
                return _flatten(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def reset_cache() -> None:
    load_catalog.cache_clear()


def _canonical(model_id: str) -> str:
    """Normalise a model id for lookup: lowercase, drop a provider prefix
    (``openai/gpt-4o`` → ``gpt-4o``) and an Ollama tag (``qwen2.5-coder:14b`` →
    ``qwen2.5-coder``)."""
    mid = (model_id or "").strip().lower()
    if "/" in mid:
        mid = mid.rsplit("/", 1)[-1]
    if ":" in mid:
        mid = mid.split(":", 1)[0]
    return mid


def lookup(model_id: str) -> ModelInfo | None:
    """Catalog entry for a model id, or None.

    Matches the exact id, then the canonical form (provider prefix + Ollama tag
    stripped, so ``openai/gpt-4o`` and ``qwen2.5-coder:14b`` resolve). We do NOT
    do loose substring matching — that mis-resolves e.g. ``qwen2.5-vl`` to
    ``qwen2.5`` and silently drops the vision flag; an unlisted id falls through
    to eVi's heuristics instead, which is the safe default."""
    cat = load_catalog()
    if not cat:
        return None
    mid = (model_id or "").strip().lower()
    if mid and mid in cat:
        return cat[mid]
    canon = _canonical(model_id)
    if canon and canon in cat:
        return cat[canon]
    return None


def refresh(url: str = DEFAULT_URL, *, dest: Path | None = None) -> int:
    """Download the full catalog to ``~/.evi/models-catalog.json`` and return the
    number of models. Raises on a network/parse error so the CLI can report it."""
    import urllib.request

    target = dest or USER_CATALOG
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (default models.dev)
        data = resp.read().decode("utf-8", errors="replace")
    parsed = _flatten(json.loads(data))  # validate before writing
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(data, encoding="utf-8")
    reset_cache()
    return len(parsed)
