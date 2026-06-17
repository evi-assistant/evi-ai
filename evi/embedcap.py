"""Embedding / reranker model-class detection.

Embedding models (and their cousins, cross-encoder rerankers) are a different
model *class* from chat/instruct models: you don't chat with them, you call them
for vectors or relevance scores. eVi already uses them — `[index] embed_model`
for semantic project search and `evi/tools/rerank.py` for retrieval reranking —
but the model picker gave no signal that an id is an embedder/reranker rather
than a chat model. This adds the heuristic behind the ◆ capability chip so a
nomic-embed / bge-reranker id is visibly *not* something to set as your chat
model.

Best-effort substring match on the model id, same as the other capability
detectors (vision / reasoning / infill / audio / tools / guard).
"""

from __future__ import annotations

# Embedding model families.
_EMBED_HINTS = (
    "embed", "embedding",                 # nomic-embed-text, text-embedding-3, *-embed
    "bge-", "bge-m3", "gte-", "e5-", "e5-mistral", "multilingual-e5",
    "all-minilm", "minilm", "all-mpnet", "sentence-t5",
    "snowflake-arctic-embed", "arctic-embed", "mxbai-embed",
    "jina-embeddings", "nomic-embed", "instructor-", "stella",
)

# Reranker / cross-encoder families (relevance scoring, not generation).
_RERANK_HINTS = (
    "rerank", "reranker", "cross-encoder", "ms-marco",
    "bge-reranker", "jina-reranker", "mxbai-rerank",
)


def model_is_embedding(model_id: str) -> bool:
    """Heuristic: is this an embedding model?"""
    if not model_id:
        return False
    mid = model_id.lower()
    # A reranker id often contains "bge" too — keep them distinct.
    if model_is_reranker(mid):
        return False
    return any(h in mid for h in _EMBED_HINTS)


def model_is_reranker(model_id: str) -> bool:
    """Heuristic: is this a reranker / cross-encoder model?"""
    if not model_id:
        return False
    return any(h in model_id.lower() for h in _RERANK_HINTS)


def model_is_embed_class(model_id: str) -> bool:
    """True for either an embedding or a reranker model — the ◆ chip."""
    return model_is_embedding(model_id) or model_is_reranker(model_id)
