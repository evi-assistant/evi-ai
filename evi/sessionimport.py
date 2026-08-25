"""Import conversations from the CLI agents eVi already drives.

eVi runs Claude Code, Codex and friends as backends, which means it knows where
they keep their history — so the sessions you already had shouldn't be stranded
in another tool. This reads their on-disk session logs and writes them into
eVi's own transcript store, where search, dreaming and export can reach them.

Read-only with respect to the source: nothing here writes to or deletes from
another tool's directory.

Formats (verified against real files, both JSONL, one record per line):

  Claude Code  ~/.claude/projects/<project-slug>/<session-uuid>.jsonl
      {"type":"user",      "message":{"role":"user","content": str | [blocks]}}
      {"type":"assistant", "message":{"role":"assistant","content":[blocks]}}
      blocks: {"type":"text"|"thinking"|"tool_use", ...}
      Other record types (attachment, queue-operation, last-prompt, mode) are
      bookkeeping and carry no conversation.

  Codex        ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl
      {"type":"response_item","payload":{"type":"message","role":...,
                                         "content":[{"type":"input_text"|"output_text","text":...}]}}
      The `developer` role is Codex's own system prompt, not the user's words.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from evi.transcripts import TranscriptStore

CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"

SOURCES = ("claude-code", "codex")


@dataclass
class ImportedSession:
    """One source conversation, normalised to eVi's roles."""

    source: str
    session_id: str
    path: Path
    started: float
    label: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def turns(self) -> int:
        return sum(1 for m in self.messages if m["role"] == "user")


def _ts(value: Any) -> float:
    """Best-effort ISO-8601 (or epoch) -> epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            pass
    return 0.0


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue          # a torn last line shouldn't lose the file
                if isinstance(rec, dict):
                    yield rec
    except Exception:  # noqa: BLE001
        return


def _claude_text(content: Any) -> tuple[str, list[str]]:
    """(visible text, tool names) from a Claude Code content field.

    Thinking blocks are dropped: they're the model's scratchpad, not transcript,
    and eVi's own store doesn't keep them either.
    """
    if isinstance(content, str):
        return content, []
    parts: list[str] = []
    tools: list[str] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            parts.append(str(block["text"]))
        elif kind == "tool_use" and block.get("name"):
            tools.append(str(block["name"]))
        elif kind == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str) and inner:
                parts.append(inner)
    return "\n".join(parts), tools


def parse_claude_session(path: Path) -> ImportedSession | None:
    sess = ImportedSession(
        source="claude-code", session_id=path.stem, path=path, started=0.0,
        label=path.parent.name,
    )
    for rec in _read_jsonl(path):
        kind = rec.get("type")
        if kind not in ("user", "assistant"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        text, tools = _claude_text(msg.get("content"))
        if not text.strip() and not tools:
            continue
        ts = _ts(rec.get("timestamp"))
        if ts and not sess.started:
            sess.started = ts
        sess.messages.append({
            "role": "assistant" if kind == "assistant" else "user",
            "content": text,
            "ts": ts,
            "tools": tools,
        })
    return sess if sess.messages else None


def parse_codex_session(path: Path) -> ImportedSession | None:
    sess = ImportedSession(
        source="codex", session_id=path.stem, path=path, started=0.0,
    )
    for rec in _read_jsonl(path):
        if rec.get("type") == "session_meta":
            payload = rec.get("payload") or {}
            sess.label = str(payload.get("cwd") or payload.get("id") or "")
            sess.started = sess.started or _ts(rec.get("timestamp"))
            continue
        if rec.get("type") != "response_item":
            continue
        payload = rec.get("payload") or {}
        if payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue          # `developer` is Codex's system prompt, not a turn
        text = "\n".join(
            str(b.get("text", "")) for b in payload.get("content") or []
            if isinstance(b, dict) and b.get("text")
        )
        if not text.strip():
            continue
        ts = _ts(rec.get("timestamp"))
        if ts and not sess.started:
            sess.started = ts
        sess.messages.append({"role": role, "content": text, "ts": ts, "tools": []})
    return sess if sess.messages else None


def discover(source: str = "") -> list[ImportedSession]:
    """Every importable session found on this machine, newest first.

    Parses headers and bodies eagerly — these files are small and the count is in
    the hundreds at most, and the caller needs turn counts to choose sensibly.
    """
    out: list[ImportedSession] = []
    want = {source} if source else set(SOURCES)
    if "claude-code" in want and CLAUDE_ROOT.is_dir():
        for p in CLAUDE_ROOT.glob("*/*.jsonl"):
            s = parse_claude_session(p)
            if s:
                out.append(s)
    if "codex" in want and CODEX_ROOT.is_dir():
        for p in CODEX_ROOT.glob("*/*/*/rollout-*.jsonl"):
            s = parse_codex_session(p)
            if s:
                out.append(s)
    for s in out:
        if not s.started:
            try:
                s.started = s.path.stat().st_mtime
            except Exception:  # noqa: BLE001
                s.started = time.time()
    out.sort(key=lambda s: s.started, reverse=True)
    return out


def import_session(sess: ImportedSession, *, store: TranscriptStore | None = None) -> str:
    """Write one parsed session into eVi's transcript store.

    The eVi session id is prefixed with the source so an import is always
    distinguishable from a native conversation, and re-importing the same source
    session lands in the same id rather than multiplying copies.
    """
    st = store or TranscriptStore()
    session_id = f"{sess.source}-{sess.session_id}"
    base = sess.started or time.time()
    for i, m in enumerate(sess.messages):
        content = m["content"]
        # Record which tools ran as TEXT, never as `tool_calls`.
        # `sessions.history_from_transcript` copies tool_calls straight into
        # Agent.history, and the API requires every assistant message carrying
        # tool_calls to be followed by matching tool-result messages. The source
        # logs don't give us those, so emitting tool_calls here would produce a
        # history that 400s the moment someone resumes the imported session.
        if m["tools"]:
            note = "[used: " + ", ".join(dict.fromkeys(m["tools"])) + "]"
            content = f"{content}\n{note}" if content.strip() else note
        st.write_message(
            session_id, m["role"], content, timestamp=m["ts"] or (base + i),
        )
    return session_id
