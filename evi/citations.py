"""Citations — structured source references attached to tool outputs.

The pattern, borrowed loosely from Anthropic's citations API: a tool
emits both visible text AND a parallel list of `Citation` objects.
Tools that quote files / chunks / URLs label each excerpt with a small
`[N]` marker in the text and surface the same `N` in citations. The
LLM is free to reference `[N]` in its response. The web UI sees the
citations stream alongside `ToolResult` events and renders them as a
"Sources" footer beneath the tool bubble.

This file deliberately holds ONLY the dataclasses — keeps the import
graph quiet (no httpx / openai / etc). Tools and the agent both
import from here without dragging extras along.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Citation:
    """One source excerpt referenced from a tool output.

    Fields:
    - `id`: a short label (often "1", "2", ...) that pairs with `[N]`
      markers in the visible text. The model can quote it.
    - `source_type`: "file" | "index" | "url" | "other" — used by the
      UI to pick an icon.
    - `source_id`: human-readable identifier (path, url, etc).
    - `excerpt`: short snippet of the source content (≤ 240 chars).
      Caller is responsible for trimming.
    - `start` / `end`: line numbers (1-indexed) for file/index
      citations, byte offsets for URL citations — best-effort. Zero
      means "unknown".
    """

    id: str
    source_type: str
    source_id: str
    excerpt: str = ""
    start: int = 0
    end: int = 0


@dataclass
class ToolOutput:
    """Rich tool output: visible text + parallel structured citations.

    Tools can return either a plain `str` (no citations) or a
    `ToolOutput`. The base Tool wrapper unwraps both shapes so the
    agent always receives a `ToolOutput`.
    """

    text: str
    citations: list[Citation] = field(default_factory=list)


def trim_excerpt(text: str, *, max_chars: int = 240) -> str:
    """Trim a long string to a single-paragraph excerpt for citation display.

    Collapses internal whitespace runs (so newlines don't punch through
    the chip UI) and appends `…` when truncated. Empty / whitespace
    input returns an empty string.
    """
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"
