"""Re-rank candidate passages with a local cross-encoder.

After `find_in_project` returns its top-K hits by cosine similarity over
a bi-encoder, those candidates can be re-scored by a cross-encoder for
much better quality. Bi-encoders embed query + passage independently;
cross-encoders score the pair *together*, which is roughly the
difference between "looks vaguely related" and "actually answers the
question".

Implementation: sentence-transformers' `CrossEncoder`. The default
model `cross-encoder/ms-marco-MiniLM-L-6-v2` is ~80 MB; it downloads
on first call to the HuggingFace cache.

This tool is category `index` — it shares the toggle with
`find_in_project` since they're used together. Install
`pip install 'evi-assistant[rerank]'` to enable.

Usage from the LLM side: first run `find_in_project(query, path, k=20)`
to get a broad set, then `rerank(query, candidates, top_k=5)` to pick
the actually-best ones.
"""

from __future__ import annotations

import json
from typing import Any

from evi.citations import Citation, ToolOutput, trim_excerpt
from evi.tools.base import tool


_CROSS_ENCODER = None
_CROSS_ENCODER_NAME: str | None = None
_DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def _load_encoder(model_name: str):
    """Lazy-load the cross-encoder. Cached across calls."""
    global _CROSS_ENCODER, _CROSS_ENCODER_NAME
    if _CROSS_ENCODER is not None and _CROSS_ENCODER_NAME == model_name:
        return _CROSS_ENCODER
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise RuntimeError(
            "rerank needs sentence-transformers — "
            "install with: pip install 'evi-assistant[rerank]'"
        ) from exc
    _CROSS_ENCODER = CrossEncoder(model_name)
    _CROSS_ENCODER_NAME = model_name
    return _CROSS_ENCODER


def _parse_candidates(candidates: Any) -> list[dict]:
    """Normalise into a list of `{text, path?, lines?}` dicts.

    Accepts:
    - `[{text, path?, lines?}, …]`    — preferred shape (find_in_project output)
    - `[str, …]`                       — bare strings
    - JSON string of either of the above (the LLM sometimes serialises)
    """
    if isinstance(candidates, str):
        try:
            candidates = json.loads(candidates)
        except json.JSONDecodeError:
            return [{"text": candidates}]
    if not isinstance(candidates, list):
        return []
    out: list[dict] = []
    for c in candidates:
        if isinstance(c, dict):
            text = str(c.get("text") or c.get("content") or "")
            if not text:
                continue
            out.append({
                "text": text,
                "path": str(c.get("path") or c.get("source") or ""),
                "lines": str(c.get("lines") or ""),
            })
        elif isinstance(c, str) and c.strip():
            out.append({"text": c, "path": "", "lines": ""})
    return out


@tool(
    description=(
        "Re-rank candidate passages by relevance to a query using a local "
        "cross-encoder. Pass `candidates` as either a JSON array of "
        "`{text, path, lines}` objects (the shape `find_in_project` "
        "returns) or a plain array of strings. Returns the top_k "
        "candidates sorted by relevance, with scores."
    ),
    category="index",
)
def rerank(query: str, candidates: list, top_k: int = 5, model: str = "") -> Any:
    if not query.strip():
        return "ERROR: empty query"
    parsed = _parse_candidates(candidates)
    if not parsed:
        return "ERROR: no usable candidates"

    model_name = (model or _DEFAULT_MODEL).strip()
    try:
        encoder = _load_encoder(model_name)
    except RuntimeError as exc:
        return f"ERROR: {exc}"

    pairs = [[query, c["text"]] for c in parsed]
    try:
        scores = encoder.predict(pairs)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: cross-encoder failed: {type(exc).__name__}: {exc}"

    # sentence-transformers returns either a list or a numpy array. Coerce
    # to plain floats so we don't drag numpy into the citation payload.
    score_list = [float(s) for s in scores]
    ranked = sorted(
        zip(parsed, score_list),
        key=lambda x: x[1],
        reverse=True,
    )[: max(1, int(top_k))]

    payload = [
        {
            "score": round(score, 4),
            "path": c.get("path", ""),
            "lines": c.get("lines", ""),
            "text": c["text"],
        }
        for c, score in ranked
    ]
    citations = [
        Citation(
            id=str(i + 1),
            source_type="index" if c.get("path") else "other",
            source_id=c.get("path", "(unattributed)"),
            excerpt=trim_excerpt(c["text"]),
        )
        for i, (c, _) in enumerate(ranked)
    ]
    return ToolOutput(text=json.dumps(payload), citations=citations)
