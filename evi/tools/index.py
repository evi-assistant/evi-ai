"""Semantic search tools — talk to your codebase by meaning, not just regex.

`index_project(path)` walks a directory, chunks every supported text file,
runs each chunk through the configured embedding model, and persists the
result under `~/.evi/indices/<hash>/`.

`find_in_project(query, path, k)` looks up the previously built index for
that path and returns the top-k most similar chunks as JSON the agent can
read.

Tool category is `index`. Default off because building an index costs
real embedding calls — the user should opt in.
"""

from __future__ import annotations

import json

from evi.citations import Citation, ToolOutput, trim_excerpt
from evi.config import Config
from evi.index import ProjectIndex
from evi.tools.base import tool


@tool(
    description=(
        "Build (or rebuild) a semantic index over a directory tree so "
        "`find_in_project` can search it by meaning. Runs every text "
        "file through the embedding model — slow on first call, fast "
        "thereafter. Returns the number of chunks indexed."
    ),
    category="index",
    long=True,
)
def index_project(path: str) -> str:
    from pathlib import Path

    config = Config.load()
    root = Path(path).expanduser()
    if not root.is_dir():
        return f"ERROR: not a directory: {root}"
    try:
        idx = ProjectIndex(root, config.llm)
        n = idx.build()
    except Exception as exc:
        return f"ERROR: indexing failed: {type(exc).__name__}: {exc}"
    return json.dumps({"chunks": n, "root": str(root)})


@tool(
    description=(
        "Semantic search across a previously indexed project. Returns "
        "the top-k matching chunks as JSON: [{score, path, lines, "
        "text}, …]. Run `index_project` once before calling this for "
        "a given path."
    ),
    category="index",
)
def find_in_project(query: str, path: str, k: int = 5) -> ToolOutput | str:
    from pathlib import Path

    if not query.strip():
        return "ERROR: empty query"
    config = Config.load()
    root = Path(path).expanduser()
    if not root.is_dir():
        return f"ERROR: not a directory: {root}"
    idx = ProjectIndex(root, config.llm)
    if not idx.exists():
        return f"ERROR: no index for {root}. Run `index_project` first."
    try:
        hits = idx.query(query, k=max(1, int(k)))
    except Exception as exc:
        return f"ERROR: query failed: {type(exc).__name__}: {exc}"
    # Surface one Citation per hit so the web UI can render them as a
    # "Sources" footer pointing at file:line. Citation ids match the
    # 1-indexed position in the result list.
    citations = [
        Citation(
            id=str(i + 1),
            source_type="index",
            source_id=h.chunk.path,
            excerpt=trim_excerpt(h.chunk.text),
            start=h.chunk.start_line,
            end=h.chunk.end_line,
        )
        for i, h in enumerate(hits)
    ]
    payload = [
        {
            "score": round(h.score, 4),
            "path": h.chunk.path,
            "lines": f"{h.chunk.start_line}-{h.chunk.end_line}",
            "text": h.chunk.text,
        }
        for h in hits
    ]
    return ToolOutput(text=json.dumps(payload), citations=citations)


@tool(
    description="Return summary stats for the index over a directory.",
    category="index",
)
def project_index_stats(path: str) -> str:
    from pathlib import Path

    config = Config.load()
    root = Path(path).expanduser()
    idx = ProjectIndex(root, config.llm)
    return json.dumps(idx.stats())
