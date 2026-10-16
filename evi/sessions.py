"""Session browsing — list, show, and resume past chats.

Transcripts persist as one JSONL file per session under
`~/.evi/transcripts/<YYYY-MM-DD>/<session>.jsonl`. This module reads them
back into structured objects so the CLI (and eventually the web UI) can:

- enumerate sessions across days
- preview a session's contents
- hydrate an `Agent.history` from one and continue the conversation

Resuming is a best-effort reconstruction. We re-emit:

- the user/assistant/tool messages in order
- the assistant's `tool_calls` payload, with each tool result paired to the
  call it answers (by its logged `tool_call_id`, else by position), so strict
  OpenAI-compatible backends accept the resumed history
- on request (`apply_compaction=True`, what a resume wants), any compaction,
  so the history is the one the model last saw

Things we DON'T try to perfectly restore:

- the original Agent's transient flags (goal, plan_mode_once) — the
  caller can re-set them if needed
- the backend's KV cache (it's a fresh prompt to the LLM)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from evi.config import TRANSCRIPTS_DIR
from evi.history import compaction_messages, pair_tool_results
from evi.transcripts import COMPACTION, MESSAGE


@dataclass(frozen=True)
class SessionInfo:
    """Lightweight summary of a stored session."""

    session_id: str
    day: str                  # YYYY-MM-DD
    path: Path
    message_count: int
    first_user_message: str   # truncated to 80 chars
    started_at: float | None  # earliest timestamp in the file
    ended_at: float | None    # latest timestamp

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.ended_at:
            return self.ended_at - self.started_at
        return None


def _read_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def _summarize(path: Path, day: str) -> SessionInfo | None:
    entries = _read_entries(path)
    if not entries:
        return None
    first_user = ""
    for e in entries:
        if e.get("role") == "user":
            content = str(e.get("content", "")).strip()
            # Strip the "[ongoing goal: …]" prefix injected by Agent.chat
            # so the summary reflects what the human actually typed.
            if content.startswith("[ongoing goal:"):
                marker = "]\n\n"
                idx = content.find(marker)
                if idx > 0:
                    content = content[idx + len(marker):].strip()
            first_user = content[:80]
            break

    timestamps = [float(e["ts"]) for e in entries if "ts" in e]
    return SessionInfo(
        session_id=path.stem,
        day=day,
        path=path,
        message_count=sum(1 for e in entries if (e.get("type") or MESSAGE) == MESSAGE),
        first_user_message=first_user or "(no user message)",
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
    )


def list_sessions(
    *,
    root: Path | None = None,
    days: int | None = None,
    limit: int = 50,
) -> list[SessionInfo]:
    """Enumerate sessions, newest first.

    Args:
        root: override `~/.evi/transcripts/` (mostly for tests).
        days: cap how many calendar days back we walk. None = all.
        limit: cap on returned entries.
    """
    base = root or TRANSCRIPTS_DIR
    if not base.is_dir():
        return []

    day_dirs = sorted(
        (p for p in base.iterdir() if p.is_dir()),
        reverse=True,
    )
    if days is not None:
        day_dirs = day_dirs[:days]

    out: list[SessionInfo] = []
    for day_dir in day_dirs:
        for f in sorted(day_dir.glob("*.jsonl"), reverse=True):
            info = _summarize(f, day_dir.name)
            if info is not None:
                out.append(info)
                if len(out) >= limit:
                    return out
    return out


@dataclass(frozen=True)
class SessionMatch:
    """A transcript search hit: the session it's in plus a context snippet."""

    session: SessionInfo
    role: str
    snippet: str


def search_sessions(
    query: str,
    *,
    root: Path | None = None,
    days: int | None = None,
    limit: int = 50,
    max_per_session: int = 3,
) -> list[SessionMatch]:
    """Full-text search across stored transcripts (case-insensitive substring).

    Returns matches newest-session first, each with a trimmed snippet centred
    on the hit. Caps hits per session so one chatty session can't crowd out
    the rest. Mirrors Claude Code's transcript search (Ctrl+R / `--resume`)."""
    q = query.strip().lower()
    if not q:
        return []
    out: list[SessionMatch] = []
    for info in list_sessions(root=root, days=days, limit=100_000):
        hits = 0
        for e in _read_entries(info.path):
            content = str(e.get("content", ""))
            idx = content.lower().find(q)
            if idx < 0:
                continue
            start = max(0, idx - 40)
            end = min(len(content), idx + len(q) + 40)
            snippet = ("…" if start else "") + content[start:end].replace("\n", " ").strip() + ("…" if end < len(content) else "")
            out.append(SessionMatch(session=info, role=str(e.get("role", "")), snippet=snippet))
            hits += 1
            if hits >= max_per_session:
                break
        if len(out) >= limit:
            break
    return out[:limit]


def handoff_info(
    session_id: str, *, base_url: str = "", root: Path | None = None
) -> dict[str, Any] | None:
    """Resume affordances for picking a session up on another device (Phase 87).

    Returns ``{session_id, day, messages, resume_cmd, resume_url}`` or None when
    the session isn't persisted on disk (nothing to hand off yet). Because
    transcripts are written per-turn, the on-disk copy is current as of the last
    completed turn — `evi sync` it, then resume on the other device.
    """
    path = find_session(session_id, root=root)
    if path is None:
        return None
    day = path.parent.name
    info = _summarize(path, day)
    base = base_url.rstrip("/")
    return {
        "session_id": session_id,
        "day": day,
        "messages": info.message_count if info else 0,
        "resume_cmd": f"evi sessions resume {session_id}",
        "resume_url": f"{base}/?session={session_id}",
    }


def most_recent_session_id(*, root: Path | None = None) -> str | None:
    """The id of the most recently active session (by last timestamp), or None.

    list_sessions orders by day-dir + filename, so we sort by `ended_at` here to
    get the true most-recent rather than the lexically-last filename."""
    items = [s for s in list_sessions(root=root, limit=100_000) if s.ended_at is not None]
    if not items:
        return None
    return max(items, key=lambda s: s.ended_at).session_id


def find_session(session_id: str, *, root: Path | None = None) -> Path | None:
    """Locate the JSONL path for a session id by scanning day dirs."""
    base = root or TRANSCRIPTS_DIR
    if not base.is_dir():
        return None
    for day_dir in base.iterdir():
        if not day_dir.is_dir():
            continue
        candidate = day_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def history_from_transcript(path: Path, *, apply_compaction: bool = False) -> list[dict[str, Any]]:
    """Rebuild an Agent.history list (without its system prompt) from a stored
    transcript.

    Order is preserved. Goal-reminder prefixes injected by Agent.chat are
    LEFT IN PLACE so the reconstructed history matches what the model
    originally saw — important for KV cache symmetry on resume. For the same
    reason a resume passes `apply_compaction=True`: each compaction record then
    replaces what it summarised with the summary
    (evi.history.compaction_messages). By default every logged message is
    kept, which is what a reader of the whole session (`evi sessions show`,
    fine-tune export) wants.

    Each tool result is paired with the call it answers: by the
    `tool_call_id` it was logged with, or by position for transcripts written
    before that was logged. A call with no logged result (the turn was cut
    short) gets a marked placeholder, and a result with no call is dropped
    (evi.history.pair_tool_results).
    """
    history: list[dict[str, Any]] = []
    for raw in _read_entries(path):
        kind = raw.get("type") or MESSAGE
        if kind == COMPACTION:
            if apply_compaction:
                history = _apply_compaction(history, raw)
            continue
        if kind != MESSAGE:
            continue  # a meta record this version doesn't replay
        role = raw.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        content = raw.get("content", "")
        msg: dict[str, Any] = {"role": role, "content": "" if content is None else content}
        if role == "tool":
            msg["name"] = raw.get("tool_name", "")
            msg["tool_call_id"] = raw.get("tool_call_id")  # None: paired by position
        if role == "assistant" and raw.get("tool_calls"):
            msg["tool_calls"] = raw["tool_calls"]
        history.append(msg)
    return pair_tool_results(history)


def _apply_compaction(history: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    """Replay one compaction record onto the messages rebuilt so far: keep the
    last `kept` of them (the logged messages the kept tail held) and put the
    summary before them. A tail that would start with a tool result (its call
    was summarised away) drops it."""
    summary = str(record.get("summary") or "").strip()
    if not summary:
        return history
    try:
        kept = max(0, int(record.get("kept") or 0))
        collapsed = max(0, int(record.get("collapsed") or 0))
    except (TypeError, ValueError):
        return history
    tail = history[max(0, len(history) - kept):] if kept else []
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    if not collapsed:
        collapsed = len(history) - len(tail)
    head = compaction_messages(summary, collapsed, next_role=tail[0].get("role") if tail else None)
    return [*head, *tail]


def fmt_when(ts: float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---- export -------------------------------------------------------------


def export_markdown(path: Path) -> str:
    """Render a stored session as a self-contained markdown document."""
    info = _summarize(path, path.parent.name)
    title = info.first_user_message[:80] if info else path.stem
    lines = [
        f"# Session {path.stem}",
        "",
        f"- Day: {path.parent.name}",
        f"- Messages: {info.message_count if info else 0}",
        f"- Started: {fmt_when(info.started_at) if info else '—'}",
        f"- Topic: {title}",
        "",
        "---",
        "",
    ]
    for entry in _read_entries(path):
        role = entry.get("role", "")
        content = entry.get("content", "") or ""
        if role == "system":
            continue
        if role == "tool":
            name = entry.get("tool_name", "tool")
            lines.append(f"**🔧 {name}**\n")
            lines.append(f"```\n{content}\n```")
        elif role == "user":
            lines.append("**🧑 user**\n")
            lines.append(content)
        elif role == "assistant":
            lines.append("**🤖 evi**\n")
            lines.append(content)
            tc = entry.get("tool_calls")
            if tc:
                lines.append("")
                for call in tc:
                    fn = call.get("function", {})
                    lines.append(
                        f"> *called* `{fn.get('name', '?')}({fn.get('arguments', '{}')})`"
                    )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_json(path: Path) -> str:
    """Return raw transcript entries as a JSON array string."""
    import json as _json

    return _json.dumps(_read_entries(path), indent=2, default=str)


def export_html(path: Path) -> str:
    """Wrap the markdown export in a minimal HTML shell for browser viewing.

    No JS — pure styled document. Markdown features beyond the renderer's
    basics (bold, code blocks, headers) won't render here, but the
    transcript is fully readable.
    """
    md = export_markdown(path)

    def _escape(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Minimal markdown → HTML: handle code fences and bold runs.
    html_parts: list[str] = []
    in_code = False
    code_buf: list[str] = []
    for raw in md.splitlines():
        if raw.startswith("```"):
            if in_code:
                html_parts.append(
                    "<pre><code>" + "\n".join(_escape(line) for line in code_buf) + "</code></pre>"
                )
                code_buf = []
            in_code = not in_code
            continue
        if in_code:
            code_buf.append(raw)
            continue
        if raw.startswith("# "):
            html_parts.append(f"<h1>{_escape(raw[2:])}</h1>")
        elif raw.startswith("> "):
            html_parts.append(f"<blockquote>{_escape(raw[2:])}</blockquote>")
        elif raw.strip() == "---":
            html_parts.append("<hr>")
        else:
            line = _escape(raw)
            # Re-introduce **bold** runs.
            import re as _re
            line = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", line)
            html_parts.append(f"<p>{line}</p>")
    body = "\n".join(html_parts)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>eVi session {path.stem}</title>"
        "<style>body{font:14px/1.5 -apple-system,Segoe UI,system-ui,sans-serif;"
        "max-width:720px;margin:30px auto;padding:0 16px;color:#222}"
        "h1{font-size:18px}pre{background:#f4f4f4;padding:8px 10px;border-radius:4px;"
        "overflow-x:auto}blockquote{color:#666;border-left:3px solid #ddd;padding-left:10px;"
        "margin:0}strong{color:#000}</style></head><body>"
        f"{body}</body></html>"
    )


def export_session(
    session_id: str,
    *,
    fmt: str = "md",
    root: Path | None = None,
) -> str:
    """High-level export by session id. `fmt` is 'md', 'html', or 'json'."""
    path = find_session(session_id, root=root)
    if path is None:
        raise FileNotFoundError(f"no session {session_id!r}")
    fmt = fmt.lower()
    if fmt in ("md", "markdown"):
        return export_markdown(path)
    if fmt == "html":
        return export_html(path)
    if fmt == "json":
        return export_json(path)
    raise ValueError(f"unknown format {fmt!r} (use md / html / json)")
