"""Local usage analytics — computed from transcripts, printed locally.

Reads ``~/.evi/transcripts/`` and aggregates: session/message counts, a
role breakdown, the most-used tools, the busiest days, and a rough token volume
(chars/4 — not a tokenizer). Everything stays on disk; this is deliberately the
*local* counterpart to a cloud analytics dashboard (which eVi does not do).
Only messages count: a meta record in a transcript (a compaction, say) is not
part of the conversation.

`provenance` (``evi stats --provenance``) reads the current repository's git
history instead: how many commits there are, how many say ``Implemented-by:
eVi`` in their trailers, and how many carry a director attestation that
checks (`evi.attest.verify_trailers` is VALID under this machine's director
key). Only those count: the eVi share is attested eVi commits over all
commits, and review rounds are summed over attested commits. An attestation
(its task, head and key) counts once, at the oldest commit that carries it:
the trailers are public text, so a later commit that copies them (replayed)
signs nothing new. A Verified-by line without its signed Attestation
(self-reported), an attestation that doesn't check (forged, edited, another
key), a replayed one and an eVi claim without a valid attestation are
counted apart and never in the share. The attestation is an HMAC, so it
checks only where the director key is: elsewhere (CI, an agent's process)
nothing is attested and the report says why. The ``Implemented-by`` line
itself is not signed: an attested commit counts as eVi's when it says so.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from evi.transcripts import MESSAGE


def _read_entries(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except OSError:
        pass
    return out


def compute_stats(*, root: Path | None = None, days: int | None = None) -> dict[str, Any]:
    """Aggregate transcript stats. Returns a dict (printed by `evi stats`)."""
    from evi.sessions import list_sessions

    # Resolve tool name -> category so we can attribute usage per category
    # (fs, code, web, mcp, …). Falls back to "other" for names not in the
    # registry (e.g. tools from a since-removed plugin).
    try:
        from evi.tools.base import REGISTRY

        _cat_of = {name: getattr(t, "category", "other") for name, t in REGISTRY.items()}
    except Exception:  # registry import must never break stats
        _cat_of = {}

    infos = list_sessions(root=root, days=days, limit=100_000)
    roles: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    tool_categories: Counter[str] = Counter()
    per_day: Counter[str] = Counter()
    total_msgs = 0
    char_total = 0
    first_ts: float | None = None
    last_ts: float | None = None

    for info in infos:
        per_day[info.day] += 1
        for e in _read_entries(info.path):
            if (e.get("type") or MESSAGE) != MESSAGE:
                continue  # a meta record (evi.transcripts), not a message
            role = str(e.get("role", "?"))
            roles[role] += 1
            total_msgs += 1
            content = e.get("content")
            if isinstance(content, str):
                char_total += len(content)
            ts = e.get("ts")
            if isinstance(ts, (int, float)):
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)
            # Count actual tool executions (tool-result entries carry tool_name).
            if role == "tool" and e.get("tool_name"):
                tname = str(e["tool_name"])
                tools[tname] += 1
                # MCP tools are "server.tool"; bucket them as mcp even when the
                # specific server isn't in this process's registry.
                cat = _cat_of.get(tname) or ("mcp" if "." in tname else "other")
                tool_categories[cat] += 1

    return {
        "sessions": len(infos),
        "messages": total_msgs,
        "roles": dict(roles),
        "tools": dict(tools.most_common()),
        "tool_categories": dict(tool_categories.most_common()),
        "busiest_days": dict(per_day.most_common(7)),
        "approx_tokens": char_total // 4,
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


# ---- provenance (`evi stats --provenance`) ----------------------------------

# A line of a commit's trailer block ("Key: value").
_TRAILER = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*)[ \t]*:[ \t]*(.*?)[ \t]*\Z")
# What eVi's commit bodies say of who implemented the work (evi.evidence).
_EVI_IMPLEMENTED = re.compile(r"evi\b", re.I)
# The director trailers evi.attest renders: their presence is a claim.
_DIRECTOR_LINE = re.compile(r"\s*(?:Verified-by|Review-rounds|Attestation)\s*:", re.I)
# A commit id as `git log` prints it (SHA-1, or SHA-256).
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class ProvenanceError(RuntimeError):
    """The history can't be read (not a git repository, a bad --since)."""


def _paragraph_trailers(paragraph: str) -> list[tuple[str, str]] | None:
    """The "Key: value" lines of `paragraph`, or None unless every line is
    one or continues one (indented), as git reads a trailer block."""
    found: list[tuple[str, str]] = []
    for line in paragraph.splitlines():
        if line[:1] in (" ", "\t") and found:
            continue  # a folded value's continuation
        m = _TRAILER.match(line.strip())
        if m is None:
            return None
        found.append((m.group(1), m.group(2)))
    return found


def trailer_block(message: str) -> list[tuple[str, str]]:
    """The trailers of a commit `message`: the "Key: value" lines of the
    paragraphs at its end made only of them, past the subject. A paragraph
    of its own after eVi's trailers (the ``Co-Authored-By:`` line a tool or
    a squash merge adds) doesn't hide them."""
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", str(message or "").strip()) if p.strip()]
    found: list[tuple[str, str]] = []
    for paragraph in reversed(paragraphs[1:]):
        lines = _paragraph_trailers(paragraph)
        if lines is None:
            break
        found[:0] = lines
    return found


def implemented_by_evi(message: str) -> bool:
    """The commit's trailers say ``Implemented-by: eVi ...``."""
    return any(key.lower() == "implemented-by" and _EVI_IMPLEMENTED.match(value)
               for key, value in trailer_block(message))


def _git_log(top: Path, since: str | None) -> list[tuple[str, str]]:
    from evi import gitsafe

    # NUL-separated: a commit message can't hold a NUL (git ends it there),
    # so no message can pass for more commits.
    args = ["log", "--no-color", "-z", "--format=%H%n%B"]
    if since:
        args.append(f"--since={since}")
    try:
        proc = gitsafe.run_git(args, cwd=top, timeout=120)
    except Exception as exc:  # noqa: BLE001 — no git, an unsafe repository, a timeout
        raise ProvenanceError(f"git log failed: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "does not have any commits" in err:
            return []
        raise ProvenanceError(f"git log failed: {err[:300]}")
    out = []
    for record in (proc.stdout or "").split("\0"):
        sha, sep, body = record.partition("\n")
        if sep and _SHA.fullmatch(sha):
            out.append((sha, body))
    return out


def _in_history(top: Path, head: str, commit: str) -> bool:
    """`head` (an attestation's) is `commit` or one of its ancestors."""
    from evi import gitsafe

    try:
        proc = gitsafe.run_git(["merge-base", "--is-ancestor",
                                *gitsafe.rev_args(head, commit)], cwd=top, timeout=60)
    except Exception:  # noqa: BLE001 — an unsafe value, no git: not in history
        return False
    return proc.returncode == 0


def provenance(
    start: Path | None = None, *, since: str | None = None, verifier: Any = None
) -> dict[str, Any]:
    """Who did the work in the git history of the repository at `start`
    (default: here), counting only attested trailers (see the module doc).
    `since`: git log's --since (a date, "2 weeks ago"). ProvenanceError when
    there is no repository or git can't read it."""
    from evi import attest
    from evi.project import git_toplevel

    if since is not None:
        since = since.strip()
        if since.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in since):
            raise ProvenanceError(f"not a date for --since: {since[:80]!r}")
    top = git_toplevel(Path(start) if start is not None else Path.cwd())
    if top is None:
        raise ProvenanceError("not inside a git repository")
    verifier = verifier or attest.Verifier()
    commits = _git_log(top, since or None)
    counts = Counter()
    rounds: list[int] = []
    tasks: set[str] = set()
    signed: set[tuple[str, str, str]] = set()
    key_note = ""
    for sha, message in reversed(commits):  # oldest first: where an attestation counts
        evi_claim = implemented_by_evi(message)
        check = verifier.verify_trailers(message)
        if evi_claim:
            counts["implemented_by_evi"] += 1
        if check.state == attest.VALID:
            attestation = (check.task, check.head, check.key)
            if attestation in signed:
                counts["replayed"] += 1  # an earlier commit's trailers, copied
                continue
            signed.add(attestation)
            counts["attested"] += 1
            rounds.append(int(check.rounds))
            tasks.add(check.task)
            if evi_claim:
                counts["attested_evi"] += 1
            if _in_history(top, check.head, sha):
                counts["attested_head_in_history"] += 1
            continue
        if evi_claim:
            counts["unattested_evi_claims"] += 1
        if check.state == attest.INVALID:
            counts["invalid"] += 1
        elif check.state == attest.NO_KEY:
            counts["unverifiable"] += 1
            key_note = key_note or check.reason
        elif any(_DIRECTOR_LINE.match(line) for line in message.splitlines()):
            counts["self_reported"] += 1  # a Verified-by line nothing signed
    total = len(commits)
    return {
        "repo": str(top),
        "since": since or None,
        "commits": total,
        "implemented_by_evi": counts["implemented_by_evi"],
        "attested": counts["attested"],
        "attested_evi": counts["attested_evi"],
        "attested_tasks": len(tasks),
        "attested_head_in_history": counts["attested_head_in_history"],
        "evi_share": round(counts["attested_evi"] / total, 4) if total else 0.0,
        "review_rounds": {"total": sum(rounds),
                          "mean": round(sum(rounds) / len(rounds), 2) if rounds else 0.0},
        "not_counted": {
            "unattested_evi_claims": counts["unattested_evi_claims"],
            "self_reported": counts["self_reported"],
            "invalid": counts["invalid"],
            "replayed": counts["replayed"],
            "unverifiable": counts["unverifiable"],
        },
        "key_note": key_note or None,
    }
