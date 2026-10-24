"""Tool output that would crowd the model's window: cut to its head and tail,
the whole of it kept in a spill file the model can page with read_file.

A result is held to a share of the context window (`cap_chars`); what is cut
goes to ``<eVi home>/tool-output/<session>/<call>.txt`` (secrets redacted,
readable by the user only), and the result says where. Spill files are
scratch: `prune` deletes old ones.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

# A tool result may fill this share of the context window.
WINDOW_SHARE = 0.15
# The cap in characters, whatever the window: a tiny window still sees
# something useful, a huge one never takes a whole log at once.
MIN_CHARS, MAX_CHARS = 2_000, 60_000
# The cap when the window is unknown (no [llm] context_size, nothing reported).
DEFAULT_CHARS = 16_000
# Spill files are kept this long when [tools] cleanup_period_days is 0.
DEFAULT_KEEP_DAYS = 14
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def cap_chars(window_tokens: int) -> int:
    """How many characters one tool result may keep: WINDOW_SHARE of a
    `window_tokens` window at ~4 characters a token, within MIN_CHARS and
    MAX_CHARS; DEFAULT_CHARS when the window is unknown (0)."""
    if window_tokens <= 0:
        return DEFAULT_CHARS
    return max(MIN_CHARS, min(MAX_CHARS, int(window_tokens * WINDOW_SHARE * 4)))


def clip(text: str, cap: int, note: str, *, tail_biased: bool = False) -> str:
    """`text` cut to about `cap` characters: its head and its tail, joined by
    a line saying how much was cut and `note` (where the whole of it is).
    Command and test output keeps more tail (`tail_biased`: the error is at
    the end); anything else more head. Cuts fall on line breaks when one is
    near. `text` within `cap` comes back as it is."""
    if len(text) <= cap:
        return text
    budget = max(0, cap - len(note) - 80)
    head_n = budget * (3 if not tail_biased else 1) // 4
    tail_n = budget - head_n
    head = text[:head_n]
    nl = head.rfind("\n")
    if nl > head_n // 2:
        head = head[:nl + 1]
    tail = text[len(text) - tail_n:] if tail_n else ""
    nl = tail.find("\n")
    if 0 <= nl < tail_n // 2:
        tail = tail[nl + 1:]
    cut = len(text) - len(head) - len(tail)
    return f"{head}\n[... {cut:,} characters cut. {note} ...]\n{tail}"


def spill_dir(home: Path, session_id: str) -> Path:
    """The folder a session's spill files go in."""
    return Path(home) / "tool-output" / (_UNSAFE.sub("_", session_id) or "session")


def spill(home: Path, session_id: str, call_id: str, text: str) -> Path:
    """Write `text` (secrets redacted) to the session's spill file for
    `call_id`, readable by the user only, and return its path."""
    from evi.redact import Redactor

    folder = spill_dir(home, session_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_UNSAFE.sub('_', call_id) or 'call'}.txt"
    path.write_text(Redactor()(text), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows: the user's profile folder is already theirs
    return path


def prune(home: Path, keep_days: int) -> int:
    """Delete the session spill folders nothing was written to for
    `keep_days` days; returns how many."""
    root = Path(home) / "tool-output"
    if keep_days <= 0 or not root.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86_400
    removed = 0
    for folder in root.iterdir():
        try:
            newest = max((p.stat().st_mtime for p in folder.iterdir()),
                         default=folder.stat().st_mtime)
        except OSError:
            continue
        if folder.is_dir() and newest < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed
