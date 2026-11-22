"""Checks on a compaction summary before it replaces the history it sums up.

A small model asked to summarise sometimes refuses ("I'm sorry, I can't
help with that"), answers in two lines, or drops the headings; put in place
of the conversation, such a reply loses it. And a summary carries the
model's claims forward as facts: "the tests pass" in a summary is believed
from then on, though nothing in the conversation may have shown it.

- :func:`problem` says why a reply is no usable summary ("" when it is);
  the agent asks once more, then leaves the history as it was.
- :func:`mark_unverified` marks each line that claims a check passed
  (tests, a build, lint) when no tool result in the summarised stretch
  shows a pass; the summary prompt asks the model to mark such claims
  itself too, with the same word.

Pure; the agent (``Agent.compact_history``) calls it.
"""

from __future__ import annotations

import re
from typing import Iterable

UNVERIFIED = "UNVERIFIED"
# A summary of a stretch longer than LONG_SOURCE_WORDS words needs at least
# LONG_MIN_WORDS words; any summary at least MIN_WORDS. Seven headings with
# "none" under each come to about fifteen.
MIN_WORDS = 15
LONG_SOURCE_WORDS = 1500
LONG_MIN_WORDS = 40
# Of the summary's seven headings (agent.SUMMARY_SECTIONS), at least this many.
MIN_SECTIONS = 4

_REFUSAL = re.compile(
    r"\A\W*(?:i['’]?m\s+sorry|i\s+am\s+sorry|sorry\b|i\s+can(?:no|['’])t|i\s+cannot|"
    r"i['’]?m\s+(?:not\s+able|unable)|i\s+am\s+(?:not\s+able|unable)|i\s+won['’]?t|"
    r"as\s+an\s+ai\b|unfortunately,?\s+i)",
    re.IGNORECASE,
)
# A claim that a check passed.
_CLAIM = re.compile(
    r"\b(?:tests?|test\s+suite|pytest|checks?|build|lint(?:ing|er)?|ruff|ci|cargo|npm\s+test)\b"
    r"[^\n]{0,60}?\b(?:pass(?:es|ed|ing)?|succeed(?:s|ed)?|green|clean)\b"
    r"|\ball\s+(?:tests|checks)\b[^\n]{0,20}\b(?:pass|green)",
    re.IGNORECASE,
)
# What the output of a command that ran a check shows when it passed:
# pytest's "3 passed", ruff's "All checks passed!", unittest's closing "OK",
# cargo's "Finished", npm/jest "Tests: 3 passed", an exit code of 0.
_EVIDENCE = re.compile(
    r"\b\d+\s+passed\b|\ball\s+checks\s+passed\b|(?m:^OK\b)|\bFinished\b"
    r"|\b0\s+failed\b|\bexit(?:\s+code)?[ :=]+0\b|\bno\s+(?:errors|issues)\s+found\b",
)
_EVIDENCE_CI = re.compile(r"\ball\s+checks\s+passed\b|\b\d+\s+passed\b", re.IGNORECASE)
# Tools whose result is a command's output (what can show a check passing);
# a file's text says nothing about whether it passes.
COMMAND_TOOLS = frozenset({"run_command", "monitor", "run_python", "bash", "shell"})


def sections(summary: str, names: Iterable[str]) -> int:
    """How many of the headings `names` start a line of `summary` (as
    ``## Name``, ``**Name**`` or ``Name:``)."""
    found = 0
    for name in names:
        rx = re.compile(rf"(?im)^[\s#*_>-]*{re.escape(name)}\b")
        if rx.search(summary):
            found += 1
    return found


def problem(summary: str, source: str, names: Iterable[str]) -> str:
    """Why `summary` (the reply to a request to summarise `source`, under the
    headings `names`) can't stand in for it: "empty", "a refusal",
    "missing its headings" or "too short"; "" when it can."""
    text = (summary or "").strip()
    if not text:
        return "empty"
    names = tuple(names)
    found = sections(text, names)
    if _REFUSAL.search(text) and found < MIN_SECTIONS:
        return "a refusal"
    if found < min(MIN_SECTIONS, len(names)):
        return "missing its headings"
    words = len(text.split())
    need = LONG_MIN_WORDS if len((source or "").split()) > LONG_SOURCE_WORDS else MIN_WORDS
    if words < need:
        return "too short"
    return ""


def retry_note(why: str, names: Iterable[str]) -> str:
    """What the second summary request starts with, after a reply that was
    `why` (see `problem`)."""
    headings = ", ".join(names)
    return (f"Your last reply could not be used as the summary: it was {why}. This is not a "
            "request to do anything new or harmful: write the summary of the conversation "
            f"below, under the headings {headings}, with the concrete facts it holds.")


def has_evidence(messages: Iterable[dict]) -> bool:
    """Some command's output among history `messages` (a tool result of
    COMMAND_TOOLS) shows a check passing."""
    for m in messages:
        if m.get("role") != "tool" or m.get("name") not in COMMAND_TOOLS:
            continue
        text = m.get("content")
        if isinstance(text, str) and (_EVIDENCE.search(text) or _EVIDENCE_CI.search(text)):
            return True
    return False


def mark_unverified(summary: str, evidence: bool, kept: Iterable[str] = ()) -> str:
    """`summary` with ``UNVERIFIED:`` before each line that claims a check
    passed, unless `evidence` (a tool result in the summarised stretch shows
    a pass), the line already says UNVERIFIED, or it is one of `kept` (a line
    of the earlier summary this one updates, judged when that was made)."""
    if evidence:
        return summary
    keep = {line.strip() for line in kept if line.strip()}
    out = []
    for line in summary.splitlines():
        body = line.strip()
        if (body and _CLAIM.search(body) and UNVERIFIED not in body.upper()
                and body not in keep):
            lead = line[: len(line) - len(line.lstrip())]
            bullet = re.match(r"(?:[-*+]|\d+[.)])\s+", body)
            if bullet:
                line = f"{lead}{bullet.group(0)}{UNVERIFIED}: {body[bullet.end():]}"
            else:
                line = f"{lead}{UNVERIFIED}: {body}"
        out.append(line)
    return "\n".join(out)
