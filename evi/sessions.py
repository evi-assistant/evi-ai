"""Session browsing — list, show, and resume past chats.

Transcripts persist as one JSONL file per session under
`~/.evi/transcripts/<YYYY-MM-DD>/<session>.jsonl`. This module reads them
back into structured objects so the CLI (and eventually the web UI) can:

- enumerate sessions across days
- preview a session's contents
- hydrate an `Agent.history` from one and continue the conversation

Resuming is a best-effort reconstruction. We re-emit:

- the user/assistant/tool messages in order
- the assistant's `tool_calls` payload if it was logged (so role="tool"
  messages have something to refer to via `tool_call_id`)

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
        message_count=len(entries),
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


def history_from_transcript(path: Path) -> list[dict[str, Any]]:
    """Rebuild an Agent.history list from a stored transcript.

    Order is preserved. Goal-reminder prefixes injected by Agent.chat are
    LEFT IN PLACE so the reconstructed history matches what the model
    originally saw — important for KV cache symmetry on resume.
    """
    history: list[dict[str, Any]] = []
    for raw in _read_entries(path):
        role = raw.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        msg: dict[str, Any] = {"role": role, "content": raw.get("content", "")}
        if role == "tool":
            # The transcript writer stores tool_name; OpenAI's API wants
            # both `name` and `tool_call_id`. We synthesise the id from
            # the position so reconstructed history is self-consistent
            # even if we never logged the original id.
            msg["name"] = raw.get("tool_name", "")
            msg["tool_call_id"] = f"resumed_{len(history)}"
        if role == "assistant" and raw.get("tool_calls"):
            msg["tool_calls"] = raw["tool_calls"]
            # An assistant message with tool_calls must NOT have None
            # content but may have empty string content per the spec.
            if msg["content"] is None:
                msg["content"] = ""
        history.append(msg)
    return history


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
        f"<title>Evi session {path.stem}</title>"
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
