"""Batch mode — run many prompts headlessly, the local analog of a Batch API.

Reads an input file of prompts and runs each through its own headless agent
(optionally in parallel), writing one JSON result per line. Input formats:

- ``.jsonl`` / ``.ndjson`` — one JSON object per line, e.g.
  ``{"id": "a1", "prompt": "...", "mode": "code", "schema": "out.json"}``
- ``.json`` — a JSON array of such objects
- anything else — one prompt per non-blank line (``#`` comments ignored)

The engine takes a ``run_one`` callable so it's testable without a model.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable


class BatchError(Exception):
    """The input file is missing or malformed."""


def parse_batch_file(path: str | Path) -> list[dict[str, Any]]:
    """Parse an input file into a list of ``{"id", "prompt", ...}`` items."""
    p = Path(path)
    if not p.is_file():
        raise BatchError(f"input file not found: {p}")
    text = p.read_text(encoding="utf-8")
    items: list[dict[str, Any]] = []
    suffix = p.suffix.lower()

    if suffix in (".jsonl", ".ndjson"):
        for n, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BatchError(f"{p.name} line {n}: {exc}") from exc
            if not isinstance(obj, dict) or not str(obj.get("prompt", "")).strip():
                raise BatchError(f"{p.name} line {n}: needs a non-empty 'prompt'")
            items.append(obj)
    elif suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BatchError(f"{p.name}: {exc}") from exc
        if not isinstance(data, list):
            raise BatchError(f"{p.name}: expected a JSON array of objects")
        for i, obj in enumerate(data):
            if not isinstance(obj, dict) or not str(obj.get("prompt", "")).strip():
                raise BatchError(f"{p.name} item {i}: needs a non-empty 'prompt'")
            items.append(obj)
    else:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                items.append({"prompt": line})

    # Stable ids for output correlation.
    for i, it in enumerate(items):
        it.setdefault("id", i)
    return items


def _safe(run_one: Callable[[dict], dict], item: dict) -> dict:
    try:
        return run_one(item)
    except Exception as exc:  # one bad item must not abort the batch
        return {"id": item.get("id"), "prompt": item.get("prompt", ""),
                "error": f"{type(exc).__name__}: {exc}"}


def run_batch(
    items: list[dict[str, Any]],
    run_one: Callable[[dict], dict],
    *,
    parallel: int = 1,
) -> list[dict[str, Any]]:
    """Run each item through ``run_one`` (order preserved). ``parallel`` > 1
    runs that many concurrently. Per-item errors are captured, not raised."""
    if not items:
        return []
    if parallel <= 1:
        return [_safe(run_one, it) for it in items]
    results: list[dict | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=min(parallel, len(items))) as ex:
        fut_to_i = {ex.submit(_safe, run_one, it): i for i, it in enumerate(items)}
        for fut, i in fut_to_i.items():
            results[i] = fut.result()
    return [r for r in results if r is not None]


def to_jsonl(results: list[dict[str, Any]]) -> str:
    """Render batch results as JSONL (one object per line)."""
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in results)
