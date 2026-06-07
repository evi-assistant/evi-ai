"""Dream engine — periodic memory consolidation from recent transcripts.

Spawned by `evi dream` (one-shot) or as a scheduled task. Builds a scoped
agent with the dream system prompt, hands it the last N hours of session
transcripts, and lets it use the memory tools to update / prune / add
entries. Before/after memory snapshots get diffed and written to an audit
log so you can spot bad consolidations.

The dream agent gets read-only fs tools (in case it wants to peek at a
file the transcripts reference) plus the memory tools. Crucially we do
NOT give it shell, code, or computer tools — dreams are about *memory*,
not action.

Memory's `forget` is soft-delete (Phase 12.2a) — anything the dream
removes is recoverable from `~/.evi/memory/.attic/`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from evi.config import DREAM_LOG_DIR, Config, ensure_dirs
from evi.llm.subagent import run_subagent
from evi.memory import MemoryStore
from evi.transcripts import TranscriptEntry, TranscriptStore


logger = logging.getLogger(__name__)


_DREAM_SYSTEM_PROMPT = (
    "You are the eVi dream agent. Your job is to review recent eVi "
    "conversation transcripts and update long-term memory so durable facts "
    "survive across sessions.\n\n"
    "Guidelines:\n"
    "- Memory entries are short markdown files. The first line is treated "
    "  as the summary in the index.\n"
    "- Prefer updating an existing entry over creating a near-duplicate.\n"
    "- Bias toward conservatism: only promote something to memory if it "
    "  appeared as a clear, durable fact (user preferences, project "
    "  conventions, recurring entities, environment details).\n"
    "- Do NOT memorise transient conversation: questions, tasks, opinions.\n"
    "- Use `forget(name)` to remove stale or wrong entries. This is a soft "
    "  delete — recoverable — so prefer removal over leaving wrong info.\n"
    "- Be brief; each memory entry should be a few lines, not an essay.\n"
    "- When you're done, summarise what you changed in plain text."
)


@dataclass
class MemorySnapshot:
    entries: dict[str, str]  # name -> first-line summary

    @classmethod
    def take(cls, store: MemoryStore) -> "MemorySnapshot":
        return cls(entries={e.name: e.summary for e in store.list()})


@dataclass
class DreamReport:
    started_at: datetime
    hours_reviewed: int
    transcript_entry_count: int
    added: list[str]
    removed: list[str]
    changed: list[str]
    final_text: str
    log_path: Path

    def render(self) -> str:
        bits = [
            f"# Dream report {self.started_at.isoformat(timespec='seconds')}",
            f"hours_reviewed: {self.hours_reviewed}",
            f"transcripts: {self.transcript_entry_count} entries",
            "",
            f"## Added ({len(self.added)})",
            *(f"  + {n}" for n in self.added),
            "",
            f"## Removed ({len(self.removed)})",
            *(f"  - {n}" for n in self.removed),
            "",
            f"## Changed ({len(self.changed)})",
            *(f"  ~ {n}" for n in self.changed),
            "",
            "## Agent summary",
            "",
            self.final_text.strip() or "(no summary)",
            "",
        ]
        return "\n".join(bits) + "\n"


def diff_snapshots(
    before: MemorySnapshot, after: MemorySnapshot
) -> tuple[list[str], list[str], list[str]]:
    """Return (added, removed, changed) lists of memory names."""
    added = sorted(set(after.entries) - set(before.entries))
    removed = sorted(set(before.entries) - set(after.entries))
    common = set(before.entries) & set(after.entries)
    changed = sorted(n for n in common if before.entries[n] != after.entries[n])
    return added, removed, changed


def _format_transcripts(entries: Iterable[TranscriptEntry]) -> str:
    """Pack transcript entries into a single user-message-friendly block.

    We strip system messages and truncate long tool outputs so the dream
    prompt doesn't blow context. ~12 KB is a reasonable ceiling on a 14B
    model with 8K-32K context.
    """
    MAX_TOTAL = 12 * 1024
    MAX_PER_MSG = 800
    lines: list[str] = []
    total = 0
    for e in entries:
        if e.role == "system":
            continue
        when = datetime.fromtimestamp(e.timestamp).strftime("%Y-%m-%d %H:%M")
        body = e.content.strip()
        if len(body) > MAX_PER_MSG:
            body = body[:MAX_PER_MSG] + " …(truncated)"
        tag = e.role.upper()
        if e.tool_name:
            tag = f"TOOL[{e.tool_name}]"
        chunk = f"[{when}] {tag}: {body}\n"
        if total + len(chunk) > MAX_TOTAL:
            lines.append("…(further transcripts elided for context size)\n")
            break
        lines.append(chunk)
        total += len(chunk)
    return "".join(lines) if lines else "(no transcripts found in window)"


def run_dream(
    *,
    hours: int = 24,
    config: Config | None = None,
    memory: MemoryStore | None = None,
    transcripts: TranscriptStore | None = None,
) -> DreamReport:
    """Execute one dream cycle. Returns a `DreamReport` and writes a log.

    Side effects: spawns the dream agent (one LLM round of inference, with
    up to 8 internal turns for tool calls), may mutate `~/.evi/memory/`,
    writes a log file under `~/.evi/logs/dreams/`.
    """
    ensure_dirs()
    config = config or Config.load()
    memory = memory or MemoryStore()
    transcripts = transcripts or TranscriptStore()

    cutoff = datetime.now() - timedelta(hours=hours)
    transcript_entries = list(transcripts.iter_since(cutoff))

    before = MemorySnapshot.take(memory)
    transcript_block = _format_transcripts(transcript_entries)

    task = (
        f"Here are the eVi conversations from the past {hours} hours. "
        f"Review them and curate long-term memory.\n\n"
        f"--- transcripts ({len(transcript_entries)} entries) ---\n\n"
        f"{transcript_block}"
    )

    final_text = run_subagent(
        system_prompt=_DREAM_SYSTEM_PROMPT,
        task=task,
        tool_categories=("memory", "fs"),
        max_turns=8,
    )

    after = MemorySnapshot.take(memory)
    added, removed, changed = diff_snapshots(before, after)

    started_at = datetime.now()
    report = DreamReport(
        started_at=started_at,
        hours_reviewed=hours,
        transcript_entry_count=len(transcript_entries),
        added=added,
        removed=removed,
        changed=changed,
        final_text=final_text,
        log_path=DREAM_LOG_DIR / f"{started_at.strftime('%Y%m%d_%H%M%S')}.log",
    )
    DREAM_LOG_DIR.mkdir(parents=True, exist_ok=True)
    report.log_path.write_text(report.render(), encoding="utf-8")
    logger.info(
        "dream complete: +%d -%d ~%d (log: %s)",
        len(added), len(removed), len(changed), report.log_path,
    )
    return report
