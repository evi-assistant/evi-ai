#!/usr/bin/env python3
"""Fail a change that adds a CLI command or a web route without touching docs/.

A new `evi` subcommand (a Typer `@x.command(...)` or `app.add_typer(...)`) or a
new web route (a FastAPI `@app.get/post/...(...)` or `add_api_route(...)`) is
user-visible surface. If the same diff changes nothing under `docs/`, the
feature ships undocumented and the docs quietly fall behind. This check reads
the diff and fails it in that case.

Escape hatches (an internal route, a rename, or docs that land in a
follow-up): a PR labelled `docs-skip` passes, and so does a `--base` range
with a `Docs-skip: <reason>` trailer line in one of its commit messages (the
only option for a direct push to main). Pass the PR's labels with `--labels`
(comma-separated, or the JSON list `toJSON(...labels.*.name)` produces);
`.github/workflows/docs-freshness.yml` runs this on every PR and every push
to main.

    python scripts/check_docs_freshness.py --base origin/main
    python scripts/check_docs_freshness.py --diff-file change.diff --labels docs-skip

Only added lines in Python files under `evi/` count. A registration the same
diff also removes is treated as moved rather than new; a decorator without a
quoted name or path (a bare `@app.command()`) only matches when it decorates a
function of the same name. Exit codes: 0 ok, 1 surface added without docs,
2 usage or git error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

SKIP_LABEL = "docs-skip"
DOCS_PREFIX = "docs/"
# Only the shipped package declares user-facing surface; tests and scripts
# define throwaway apps and routes all the time.
SOURCE_PREFIX = "evi/"

_HTTP_METHODS = (
    "get|post|put|patch|delete|head|options|trace|websocket|route|api_route|api_websocket_route"
)
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CLI command", re.compile(r"^\s*@\s*[A-Za-z_][\w.]*\.command\s*\(")),
    # The call form of the decorator: `app.command("name")(func)`.
    ("CLI command", re.compile(r"\.command\s*\([^()]*\)\s*\(")),
    ("CLI command group", re.compile(r"\.add_typer\s*\(")),
    ("web route", re.compile(rf"^\s*@\s*[A-Za-z_][\w.]*\.(?:{_HTTP_METHODS})\s*\(")),
    ("web route", re.compile(r"\.add_api(?:_websocket)?_route\s*\(")),
)
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")
# A decorator whose first argument is a string literal names its command or
# path itself; a bare one (`@app.command()`) takes its name from the function.
_NAMED = re.compile(r"""\(\s*[rRbBuUfF]{0,2}["']""")
# `Docs-skip: <reason>` on its own line of a commit message.
_SKIP_TRAILER = re.compile(r"^docs-skip:[ \t]*(\S.*)$", re.IGNORECASE | re.MULTILINE)


@dataclass
class Finding:
    path: str
    line: int
    kind: str
    text: str
    # The function a decorator decorates, once the diff shows its `def`.
    func: str | None = None

    @property
    def move_key(self) -> str:
        """What a removal must match for this to count as a move."""
        if self.text.startswith("@") and not _NAMED.search(self.text):
            return f"{self.text} -> def {self.func or '?'}"
        return self.text


@dataclass
class DiffScan:
    findings: list[Finding] = field(default_factory=list)
    changed_paths: set[str] = field(default_factory=set)

    @property
    def docs_changed(self) -> bool:
        return any(p.startswith(DOCS_PREFIX) for p in self.changed_paths)


def _diff_path(raw: str) -> str | None:
    """`a/x.py` / `b/x.py` / `"b/sp ace.py"` -> `x.py`; `/dev/null` -> None."""
    raw = raw.split("\t", 1)[0].strip()  # `diff -u` appends "\t<timestamp>"
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    if raw == "/dev/null":
        return None
    return raw[2:] if raw[:2] in ("a/", "b/") else raw


def _kind(line: str) -> str | None:
    for kind, pattern in _PATTERNS:
        if pattern.search(line):
            return kind
    return None


def _lines(diff: str) -> list[str]:
    """Split a diff on "\\n" only. `str.splitlines()` also breaks on \\f, \\v,
    \\x1c-\\x1e, \\x85, U+2028 and U+2029, which turns one added line into
    several and runs the hunk counter out early, hiding the rest of the hunk."""
    lines = [line.removesuffix("\r") for line in diff.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def scan_diff(diff: str) -> DiffScan:
    """Find CLI commands and web routes that a unified diff adds under evi/."""
    scan = DiffScan()
    added: list[Finding] = []
    removed: list[Finding] = []
    # Decorators still waiting for the `def` they decorate, per side.
    pending_new: list[Finding] = []
    pending_old: list[Finding] = []
    old_path: str | None = None
    new_path: str | None = None
    new_line = 0
    # Lines left in the current hunk (old side, new side). Counting them, rather
    # than guessing from prefixes, keeps a removed "-- x" line from reading as
    # a "--- file" header.
    old_left = new_left = 0

    def name_pending(pending: list[Finding], body: str) -> None:
        m = _DEF.match(body)
        if m:
            for finding in pending:
                finding.func = m.group(1)
            pending.clear()

    for line in _lines(diff):
        if old_left <= 0 and new_left <= 0:
            pending_new.clear()
            pending_old.clear()
            header = _GIT_HEADER.match(line)
            if header:
                old_path = new_path = None
                scan.changed_paths.update(header.groups())
            elif line.startswith("--- "):
                old_path = _diff_path(line[4:])
                if old_path:
                    scan.changed_paths.add(old_path)
            elif line.startswith("+++ "):
                new_path = _diff_path(line[4:])
                if new_path:
                    scan.changed_paths.add(new_path)
            elif line.startswith(("rename from ", "rename to ")):
                scan.changed_paths.add(line.split(" ", 2)[2])
            elif hunk := _HUNK.match(line):
                old_left = int(hunk.group(1) or 1)
                new_line = int(hunk.group(2))
                new_left = int(hunk.group(3) or 1)
            continue
        if line.startswith("\\"):  # "\ No newline at end of file"
            continue
        body = line[1:]
        if line.startswith("+"):
            new_left -= 1
            path = new_path or ""
            kind = _kind(body)
            if kind and path.startswith(SOURCE_PREFIX) and path.endswith(".py"):
                finding = Finding(path, new_line, kind, body.strip())
                added.append(finding)
                if finding.text.startswith("@"):
                    pending_new.append(finding)
            else:
                name_pending(pending_new, body)
            new_line += 1
        elif line.startswith("-"):
            old_left -= 1
            path = old_path or ""
            kind = _kind(body)
            if kind and path.startswith(SOURCE_PREFIX) and path.endswith(".py"):
                finding = Finding(path, 0, kind, body.strip())  # line unused
                removed.append(finding)
                if finding.text.startswith("@"):
                    pending_old.append(finding)
            else:
                name_pending(pending_old, body)
        else:  # context line (a blank one may have lost its leading space)
            old_left -= 1
            new_left -= 1
            name_pending(pending_new, body)
            name_pending(pending_old, body)
            new_line += 1
    # A registration removed in one place and added in another is a move (a
    # refactor or a file split), not new surface.
    moved: Counter[str] = Counter(f.move_key for f in removed)
    for finding in added:
        if moved[finding.move_key] > 0:
            moved[finding.move_key] -= 1
        else:
            scan.findings.append(finding)
    return scan


def parse_labels(raw: str) -> set[str]:
    """Accept `a,b` or a JSON list (what `toJSON(labels.*.name)` produces)."""
    raw = (raw or "").strip()
    if not raw:
        return set()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, list):
            return {str(x).strip() for x in data if str(x).strip()}
    return {part.strip() for part in raw.split(",") if part.strip()}


def skip_trailer(messages: str) -> str | None:
    """The reason of the first `Docs-skip: <reason>` trailer line, if any."""
    m = _SKIP_TRAILER.search(messages or "")
    return m.group(1).strip() if m else None


def check(
    diff: str,
    labels: set[str] | frozenset[str] = frozenset(),
    trailer: str | None = None,
) -> tuple[bool, list[str]]:
    """Return (ok, report lines) for a diff, the PR's labels and the reason of
    a `Docs-skip:` commit trailer (None when there is none)."""
    scan = scan_diff(diff)
    if not scan.findings:
        return True, ["docs freshness: no new CLI commands or web routes."]
    listed = [f"  {f.path}:{f.line}: {f.kind}: {f.text}" for f in scan.findings]
    if scan.docs_changed:
        return True, ["docs freshness: new surface comes with a docs/ change:", *listed]
    if SKIP_LABEL in labels:
        return True, [f"docs freshness: skipped by the `{SKIP_LABEL}` label for:", *listed]
    if trailer:
        return True, [f"docs freshness: skipped by a `Docs-skip: {trailer}` commit trailer for:",
                      *listed]
    return False, [
        "docs freshness: this change adds user-visible surface but changes nothing "
        "under docs/:",
        *listed,
        "Document it under docs/ (docs/commands.md lists the CLI), or label the PR "
        f"`{SKIP_LABEL}` / add a `Docs-skip: <reason>` commit trailer if it needs no docs.",
    ]


def _annotate(scan: DiffScan) -> None:
    for f in scan.findings:
        print(f"::error file={f.path},line={f.line}::{f.kind} added without a docs/ change "
              f"(label the PR `{SKIP_LABEL}` or add a `Docs-skip:` trailer if it needs none)")


def _git(*args: str) -> str:
    """Run git and decode its output as UTF-8 WITHOUT newline translation: text
    mode would turn a bare carriage return inside a line into a line break."""
    proc = subprocess.run(["git", *args], capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(err or f"git {args[0]} exited {proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


def _git_diff(base: str, head: str) -> str:
    return _git("-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff",
                "--find-renames", f"{base}...{head}")


def _git_messages(base: str, head: str) -> str:
    """The commit messages of the commits in `head` that `base` lacks."""
    return _git("log", "--format=%B", f"{base}..{head}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base", help="git ref to diff against (merge-base...HEAD)")
    source.add_argument("--diff-file", type=Path, help="read a unified diff ('-' = stdin)")
    parser.add_argument("--head", default="HEAD", help="git ref with the change (default HEAD)")
    parser.add_argument("--labels", default=os.environ.get("EVI_PR_LABELS", ""),
                        help="PR labels, comma-separated or a JSON list")
    args = parser.parse_args(argv)

    trailer = None
    try:
        if args.diff_file is not None:
            # Bytes, then decode: no newline translation (see _git).
            if str(args.diff_file) == "-":
                raw = sys.stdin.buffer.read()
            else:
                raw = args.diff_file.read_bytes()
            diff = raw.decode("utf-8", "replace")
        else:
            diff = _git_diff(args.base, args.head)
            trailer = skip_trailer(_git_messages(args.base, args.head))
    except (OSError, RuntimeError) as exc:
        print(f"docs freshness: cannot read the diff: {exc}", file=sys.stderr)
        return 2

    ok, report = check(diff, parse_labels(args.labels), trailer)
    print("\n".join(report))
    if not ok and os.environ.get("GITHUB_ACTIONS") == "true":
        _annotate(scan_diff(diff))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
