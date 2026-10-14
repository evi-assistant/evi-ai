"""CHANGELOG.md lint, plus the release gate's CHANGELOG/tag check.

Release notes have gone wrong in quiet ways: four times a release's header was
lost when the next release was written on top of it, so its notes read as part
of the next version (1.0.2, 1.0.29, 1.0.31/1.0.32 and 1.0.35); 0.32.0 never got
an entry; and a few entries kept the date of an earlier draft (1.0.10, 1.0.13,
1.0.43-1.0.46). These tests keep every entry well formed:

- headers are `## [X.Y.Z] — YYYY-MM-DD`, plus one empty-able `## [Unreleased]` at
  the top (and the historical desktop-only `## [desktop X.Y.Z]` form);
- versions are unique and newest first; dates are real, newest first and not in
  the future; every released entry has notes;
- no entry repeats a `### Section` heading (the trace a lost header leaves);
- every `vX.Y.Z` git tag has an entry dated within a day of the tagged commit
  (skipped when the checkout has no tags, as in a shallow CI clone).

release.yml runs this module as a script before it builds or publishes:

    python -m tests.test_changelog_lint --tag v1.0.54 --date <tagged commit date>

which exits 1 unless the CHANGELOG lints clean, its newest entry is the tag's
version, and that entry is dated within a day of the tagged commit. Only final
`vX.Y.Z` tags are accepted; pre-release tags (v1.1.0rc1) are refused.

The release workflow's shape is checked at the bottom of this file, since it is
the other half of the same gate: every publishing job (PyPI, the desktop
release, the Docker image) sits downstream of the e2e suite, no workflow but
release.yml starts on a version tag, and each gating step fails its job when
its command fails (no `|| true`, `; exit 0`, continue-on-error, skipping `if:`
or shell without `-e`). Mutation tests prove those checks catch each bypass.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
WORKFLOWS = ROOT / ".github" / "workflows"

# One day either way absorbs the gap between the author's clock and UTC.
DATE_SLACK = timedelta(days=1)

_HEADER = re.compile(r"^## \[(?P<name>[^\]]*)\](?P<rest>.*)$")
_VERSION = re.compile(r"^(?P<desktop>desktop )?(?P<v>\d+\.\d+\.\d+)$")
_DATED = re.compile(r"^ — (?P<date>\d{4}-\d{2}-\d{2})$")
_TAG = re.compile(r"^v(?P<v>\d+\.\d+\.\d+)$")
_PRE_RELEASE_TAG = re.compile(
    r"^v\d+\.\d+\.\d+[-.]?(?:a|b|c|rc|alpha|beta|pre|preview|dev)\.?\d*$", re.IGNORECASE
)


@dataclass
class Entry:
    name: str  # "Unreleased", "1.0.53" or "desktop 0.2.10"
    line: int  # 1-based line of the `## ` header
    date: date | None = None
    version: tuple[int, int, int] | None = None
    desktop: bool = False
    sections: list[tuple[int, str]] = field(default_factory=list)
    body: list[str] = field(default_factory=list)

    @property
    def released(self) -> bool:
        return self.version is not None

    @property
    def has_notes(self) -> bool:
        return any(line.strip() for line in self.body)


def _vtuple(v: str) -> tuple[int, int, int]:
    a, b, c = (int(x) for x in v.split("."))
    return a, b, c


def parse(text: str) -> tuple[list[Entry], list[str]]:
    """Split the CHANGELOG into entries. Returns (entries, header problems)."""
    entries: list[Entry] = []
    problems: list[str] = []
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if in_fence or not line.startswith("## "):
            if entries:
                if not in_fence and line.startswith("### "):
                    entries[-1].sections.append((n, line[4:].strip()))
                entries[-1].body.append(line)
            continue
        m = _HEADER.match(line)
        if not m:
            problems.append(f"line {n}: `{line}` is not a `## [X.Y.Z] — YYYY-MM-DD` header")
            continue
        entry = Entry(name=m["name"], line=n)
        entries.append(entry)
        if entry.name == "Unreleased":
            if m["rest"]:
                problems.append(f"line {n}: `## [Unreleased]` takes no date or suffix")
            continue
        vm = _VERSION.match(entry.name)
        if not vm:
            problems.append(f"line {n}: `[{entry.name}]` is not a version (X.Y.Z)")
            continue
        entry.version = _vtuple(vm["v"])
        entry.desktop = bool(vm["desktop"])
        dm = _DATED.match(m["rest"])
        if not dm:
            problems.append(
                f"line {n}: `{line}` needs ` — YYYY-MM-DD` after the version (an em dash)"
            )
            continue
        try:
            entry.date = date.fromisoformat(dm["date"])
        except ValueError:
            problems.append(f"line {n}: {dm['date']} is not a real date")
    return entries, problems


def lint(text: str, today: date | None = None) -> list[str]:
    """Every problem with the CHANGELOG's structure, as `line N: ...` strings."""
    today = today or datetime.now(timezone.utc).date()
    problems: list[str] = []
    if not text.startswith("# Changelog\n"):
        problems.append("line 1: the file must start with `# Changelog`")
    entries, header_problems = parse(text)
    problems += header_problems

    for i, e in enumerate(entries):
        if e.name == "Unreleased" and i != 0:
            problems.append(f"line {e.line}: `## [Unreleased]` must be the first entry")
        if e.released and not e.has_notes:
            problems.append(f"line {e.line}: [{e.name}] has no notes")
        if e.date and e.date > today + DATE_SLACK:
            problems.append(f"line {e.line}: [{e.name}] is dated in the future ({e.date})")
        if not e.released:
            # [Unreleased] collects notes from parallel work; its headings are
            # merged when it becomes a release, and checked then.
            continue
        seen: dict[str, int] = {}
        for n, heading in e.sections:
            if heading in seen:
                problems.append(
                    f"line {n}: [{e.name}] repeats `### {heading}` (first at line "
                    f"{seen[heading]}) — a lost release header usually looks like this"
                )
            else:
                seen[heading] = n

    names: dict[str, int] = {}
    for e in entries:
        if e.name in names:
            problems.append(f"line {e.line}: [{e.name}] already appears at line {names[e.name]}")
        names.setdefault(e.name, e.line)

    pypi = [e for e in entries if e.released and not e.desktop]
    for newer, older in zip(pypi, pypi[1:]):
        if older.version >= newer.version:
            problems.append(
                f"line {older.line}: [{older.name}] is listed below [{newer.name}] — "
                "newest version first"
            )
    dated = [e for e in entries if e.date]
    for newer, older in zip(dated, dated[1:]):
        if older.date > newer.date:
            problems.append(
                f"line {older.line}: [{older.name}] ({older.date}) is newer than "
                f"[{newer.name}] above it ({newer.date}) — newest first"
            )
    return problems


def release_problems(text: str, tag: str, released_on: date) -> list[str]:
    """What stops `tag` from being released with this CHANGELOG: the newest
    entry must be the tag's version, have notes, and be dated within a day of
    `released_on` (the tagged commit's date)."""
    m = _TAG.match(tag)
    if not m:
        if _PRE_RELEASE_TAG.match(tag):
            return [f"tag {tag!r} is not vX.Y.Z: pre-release tags are not supported "
                    "(release.yml publishes final vX.Y.Z releases only)"]
        return [f"tag {tag!r} is not vX.Y.Z"]
    version = m["v"]
    entries, _ = parse(text)
    released = [e for e in entries if e.released and not e.desktop]
    if not released:
        return [f"CHANGELOG.md has no released entries; add `## [{version}] — <date>`"]
    newest = released[0]
    if newest.name != version:
        if any(e.name == version for e in released):
            return [f"[{version}] is not the newest CHANGELOG entry ([{newest.name}] is above it)"]
        return [
            f"CHANGELOG.md has no entry for {version} (the newest is [{newest.name}]); "
            f"add `## [{version}] — {released_on.isoformat()}` above it"
        ]
    problems = []
    if not newest.has_notes:
        problems.append(f"[{version}] has no notes")
    if newest.date is None:
        problems.append(f"[{version}] has no valid date")
    elif abs(newest.date - released_on) > DATE_SLACK:
        problems.append(
            f"[{version}] is dated {newest.date}, but the tagged commit is from "
            f"{released_on} — fix the date in the header"
        )
    return problems


def tag_problems(text: str, tags: dict[str, date]) -> list[str]:
    """Every `vX.Y.Z` tag (version -> tagged commit date, UTC) needs an entry
    dated within a day of its commit."""
    entries, _ = parse(text)
    by_name = {e.name: e for e in entries if e.released and not e.desktop}
    problems = []
    for version, tagged_on in sorted(tags.items(), key=lambda kv: _vtuple(kv[0])):
        e = by_name.get(version)
        if e is None:
            problems.append(f"tag v{version} has no CHANGELOG entry")
        elif e.date is None or abs(e.date - tagged_on) > DATE_SLACK:
            problems.append(f"[{version}] is dated {e.date}, but v{version} was tagged {tagged_on}")
    return problems


def _utc_date(value: str) -> date:
    """`2026-10-09` or an ISO datetime with an offset (git's `%cI`) -> UTC date."""
    value = value.strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).date()


def _git_release_tags() -> dict[str, date]:
    """`vX.Y.Z` tags in this checkout -> their commit's UTC date ({} without git)."""
    fmt = "%(refname:strip=2) %(*committerdate:iso-strict) %(committerdate:iso-strict)"
    try:
        proc = subprocess.run(
            ["git", "for-each-ref", f"--format={fmt}", "refs/tags/v*"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    tags = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and (m := _TAG.match(parts[0])):
            tags[m["v"]] = _utc_date(parts[1])
    return tags


# ---- the release script ----------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.test_changelog_lint",
        description="Check CHANGELOG.md before publishing a release tag.",
    )
    parser.add_argument("--tag", required=True, help="the release tag, e.g. v1.0.54")
    parser.add_argument(
        "--date", type=_utc_date, default=None,
        help="the tagged commit's date: YYYY-MM-DD or ISO datetime (default: today, UTC)",
    )
    parser.add_argument("--changelog", type=Path, default=CHANGELOG)
    args = parser.parse_args(argv)
    released_on = args.date or datetime.now(timezone.utc).date()
    text = args.changelog.read_text(encoding="utf-8")
    problems = lint(text) + release_problems(text, args.tag, released_on)
    for problem in problems:
        print(f"CHANGELOG check: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"CHANGELOG check: {args.tag} has a dated entry and the file lints clean.")
    return 0


# ---- the real CHANGELOG ----------------------------------------------------

def test_changelog_lints_clean():
    assert lint(CHANGELOG.read_text(encoding="utf-8")) == []


def test_every_release_tag_has_a_dated_entry():
    tags = _git_release_tags()
    if not tags:
        pytest.skip("no vX.Y.Z tags in this checkout (shallow CI clone)")
    assert tag_problems(CHANGELOG.read_text(encoding="utf-8"), tags) == []


# ---- lint rules, on synthetic CHANGELOGs ------------------------------------

GOOD = """\
# Changelog

Intro text.

## [Unreleased]

## [1.2.3] — 2026-01-05

### Fixed
- A thing.

## [1.2.2] — 2026-01-02

### Added
- Another thing.

```text
## [not a header] inside a code fence
```

## [desktop 0.2.10] — 2026-01-01

Desktop-only rebuild.

## [1.2.1] — 2026-01-01

### Changed
- Something.
"""
TODAY = date(2026, 1, 10)


def _bad(text: str) -> list[str]:
    return lint(text, today=TODAY)


def test_good_sample_passes():
    assert _bad(GOOD) == []


@pytest.mark.parametrize("header, needle", [
    ("## [1.2.4] - 2026-01-06", "em dash"),          # hyphen instead of em dash
    ("## [1.2.4]", "em dash"),                       # no date at all
    ("## [1.2.4] — 2026-02-30", "not a real date"),
    ("## [1.2.4] — 06/01/2026", "em dash"),          # wrong date format
    ("## 1.2.4 — 2026-01-06", "not a `## [X.Y.Z]"),  # no brackets
    ("## [v1.2.4] — 2026-01-06", "not a version"),
    ("## [1.2.4] — 2026-01-20", "in the future"),
])
def test_bad_headers_are_reported(header, needle):
    text = GOOD.replace("## [Unreleased]\n", f"## [Unreleased]\n\n{header}\n\n### Added\n- x.\n")
    problems = _bad(text)
    assert any(needle in p for p in problems), problems


def test_duplicate_section_is_reported():
    # What a lost header looks like: the next release's notes carry two
    # "### Added" blocks.
    text = GOOD.replace("### Fixed\n- A thing.\n", "### Fixed\n- A thing.\n\n### Fixed\n- B.\n")
    problems = _bad(text)
    assert any("repeats `### Fixed`" in p for p in problems), problems


def test_unreleased_may_repeat_a_section_until_it_is_released():
    text = GOOD.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n- a.\n\n### Fixed\n- b.\n")
    assert _bad(text) == []


def test_versions_must_be_newest_first():
    text = GOOD.replace("## [1.2.2] — 2026-01-02", "## [1.2.9] — 2026-01-02")
    assert any("newest version first" in p for p in _bad(text))


def test_dates_must_be_newest_first():
    text = GOOD.replace("## [1.2.2] — 2026-01-02", "## [1.2.2] — 2026-01-07")
    assert any("newest first" in p for p in _bad(text))


def test_duplicate_version_is_reported():
    text = GOOD.replace("## [1.2.1] — 2026-01-01", "## [1.2.2] — 2026-01-01")
    assert any("already appears" in p for p in _bad(text))


def test_empty_release_entry_is_reported():
    text = GOOD.replace("### Fixed\n- A thing.\n", "")
    assert any("[1.2.3] has no notes" in p for p in _bad(text))


def test_unreleased_must_come_first_and_carry_no_date():
    text = GOOD.replace("## [Unreleased]\n", "").replace(
        "## [desktop 0.2.10]", "## [Unreleased]\n\n## [desktop 0.2.10]")
    assert any("must be the first entry" in p for p in _bad(text))
    dated = GOOD.replace("## [Unreleased]", "## [Unreleased] — 2026-01-09")
    assert any("takes no date" in p for p in _bad(dated))


def test_title_line_is_required():
    assert any("# Changelog" in p for p in _bad(GOOD.replace("# Changelog\n", "# Notes\n")))


# ---- tags -> entries ---------------------------------------------------------

def test_tag_without_entry_is_reported():
    problems = tag_problems(GOOD, {"1.2.3": date(2026, 1, 5), "1.2.4": date(2026, 1, 6)})
    assert problems == ["tag v1.2.4 has no CHANGELOG entry"]


def test_tag_date_mismatch_is_reported_with_a_day_of_slack():
    assert tag_problems(GOOD, {"1.2.3": date(2026, 1, 6)}) == []  # timezone slack
    problems = tag_problems(GOOD, {"1.2.3": date(2026, 1, 9)})
    assert problems == ["[1.2.3] is dated 2026-01-05, but v1.2.3 was tagged 2026-01-09"]


@pytest.mark.parametrize("value, expected", [
    ("2026-06-16", date(2026, 6, 16)),
    ("2026-06-16T17:33:32-07:00", date(2026, 6, 17)),  # git %cI, converted to UTC
    ("2026-06-16T03:00:00+09:00", date(2026, 6, 15)),
    ("2026-06-16T23:59:59Z", date(2026, 6, 16)),
])
def test_utc_date(value, expected):
    assert _utc_date(value) == expected


# ---- the release check -------------------------------------------------------

def test_release_matching_tag_and_date_passes():
    assert release_problems(GOOD, "v1.2.3", date(2026, 1, 5)) == []
    assert release_problems(GOOD, "v1.2.3", date(2026, 1, 6)) == []  # a day of slack


def test_release_tag_newer_than_changelog_fails():
    problems = release_problems(GOOD, "v1.2.4", date(2026, 1, 6))
    assert problems and "no entry for 1.2.4" in problems[0]


def test_release_tag_for_an_older_entry_fails():
    problems = release_problems(GOOD, "v1.2.2", date(2026, 1, 2))
    assert problems and "not the newest" in problems[0]


def test_release_date_mismatch_fails():
    problems = release_problems(GOOD, "v1.2.3", date(2026, 1, 9))
    assert problems and "dated 2026-01-05" in problems[0]


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2", "v1.2.3.4", "desktop-v1.2.3"])
def test_release_rejects_non_version_tags(tag):
    assert release_problems(GOOD, tag, date(2026, 1, 5)) == [f"tag {tag!r} is not vX.Y.Z"]


@pytest.mark.parametrize("tag", ["v1.2.3-rc1", "v1.2.3rc1", "v1.2.3a1", "v1.2.3.dev0",
                                 "v1.2.3-beta.2"])
def test_release_refuses_pre_release_tags_and_says_why(tag):
    # release.yml triggers on v*.*.*, which also matches these; the check
    # refuses them with a reason instead of publishing a pre-release.
    (problem,) = release_problems(GOOD, tag, date(2026, 1, 5))
    assert problem.startswith(f"tag {tag!r} is not vX.Y.Z")
    assert "pre-release tags are not supported" in problem


def test_release_entry_without_notes_fails():
    text = GOOD.replace("### Fixed\n- A thing.\n", "")
    assert "[1.2.3] has no notes" in release_problems(text, "v1.2.3", date(2026, 1, 5))


def _release_script(tmp_path: Path, text: str, *args: str) -> subprocess.CompletedProcess:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "tests.test_changelog_lint", "--changelog", str(changelog), *args],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )


def test_release_script_fails_on_a_tag_changelog_mismatch(tmp_path):
    # The exact command release.yml runs, with a tag the CHANGELOG doesn't have.
    proc = _release_script(tmp_path, GOOD, "--tag", "v1.2.4", "--date", "2026-01-05")
    assert proc.returncode == 1
    assert "no entry for 1.2.4" in proc.stderr


def test_release_script_fails_on_a_date_mismatch(tmp_path):
    proc = _release_script(tmp_path, GOOD, "--tag", "v1.2.3",
                           "--date", "2026-01-09T10:00:00-07:00")
    assert proc.returncode == 1
    assert "dated 2026-01-05" in proc.stderr


def test_release_script_passes_on_a_match(tmp_path):
    proc = _release_script(tmp_path, GOOD, "--tag", "v1.2.3",
                           "--date", "2026-01-04T20:00:00-08:00")  # 2026-01-05 UTC
    assert proc.returncode == 0, proc.stderr
    assert "v1.2.3 has a dated entry" in proc.stdout


def test_release_script_fails_on_a_malformed_changelog(tmp_path):
    text = GOOD.replace("## [1.2.2] — 2026-01-02", "## [1.2.2] - 2026-01-02")
    proc = _release_script(tmp_path, text, "--tag", "v1.2.3", "--date", "2026-01-05")
    assert proc.returncode == 1
    assert "em dash" in proc.stderr


# ---- release.yml: every publish is gated on e2e and on this check -----------

def _workflow(name: str) -> dict:
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as the boolean True.
    data["on"] = data.get("on", data.pop(True, None))
    return data


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _upstream(jobs: dict, name: str) -> set[str]:
    """Every job `name` needs, directly or through other jobs."""
    seen: set[str] = set()
    todo = _needs(jobs[name])
    while todo:
        dep = todo.pop()
        if dep not in seen and dep in jobs:
            seen.add(dep)
            todo += _needs(jobs[dep])
    return seen


def _publish_jobs(jobs: dict) -> list[str]:
    return [
        name for name, job in jobs.items()
        if any(str(s.get("uses", "")).startswith("pypa/gh-action-pypi-publish")
               for s in job.get("steps", []))
    ]


# Shell operators that let a command fail without failing its step
# (`cmd || true`, `cmd; exit 0`, `cmd &`, `cmd | tee`), and commands that end
# or reconfigure the script (`exit 0`, `set +e`, `trap`, a bare `true`).
_MASKING_OPERATORS = {"||", "&&", ";", ";;", "&", "|", "|&"}
_MASKING_COMMANDS = {"exit", "set", "trap", "return", "true", ":"}


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _shell(step: dict, job: dict, workflow: dict) -> object:
    for scope in (step, (job.get("defaults") or {}).get("run") or {},
                  (workflow.get("defaults") or {}).get("run") or {}):
        if scope.get("shell") is not None:
            return scope["shell"]
    return None


def _blocking_problems(where: str, step: dict, job: dict, workflow: dict) -> list[str]:
    """A gating step must fail its job exactly when its command fails: no
    continue-on-error, Actions' default `bash -e` shell, and no shell operator
    or command in its script that could swallow the exit status."""
    problems = []
    if step.get("continue-on-error"):
        problems.append(f"{where}: continue-on-error")
    shell = _shell(step, job, workflow)
    if shell not in (None, "bash"):
        problems.append(f"{where}: runs under `shell: {shell}`, not the default `bash -e`")
    for line in str(step.get("run", "")).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = _tokens(line)
        except ValueError as exc:
            problems.append(f"{where}: cannot parse `{line}`: {exc}")
            continue
        ops = sorted({t for t in tokens if t in _MASKING_OPERATORS})
        if ops:
            problems.append(f"{where}: `{line}` uses {ops}, which can hide a failure")
        if tokens and tokens[0] in _MASKING_COMMANDS:
            problems.append(f"{where}: `{line}` can end the script without the gate's status")
    return problems


def publish_gate_problems(release: dict) -> list[str]:
    """Every job in release.yml except e2e itself runs downstream of the e2e
    suite, and only when everything it needs succeeded."""
    jobs = release["jobs"]
    problems = []
    e2e = jobs.get("e2e") or {}
    if e2e.get("uses") != "./.github/workflows/e2e.yml":
        problems.append("release.yml has no `e2e` job calling ./.github/workflows/e2e.yml")
    if "continue-on-error" in e2e:
        problems.append("the e2e job has continue-on-error")
    if not _publish_jobs(jobs):
        problems.append("release.yml no longer publishes to PyPI?")
    called = {job.get("uses") for job in jobs.values()}
    for workflow in ("./.github/workflows/desktop-release.yml", "./.github/workflows/docker.yml"):
        if workflow not in called:
            problems.append(f"release.yml no longer calls {workflow}")
    for name, job in jobs.items():
        if name == "e2e":
            continue
        if "e2e" not in _upstream(jobs, name):
            problems.append(f"{name} does not need the e2e job")
        # A status function in `if:` (always(), failure() ...) would run the
        # job even when e2e (or a job in between) failed.
        condition = str(job.get("if", ""))
        if re.search(r"\b(always|failure|cancelled)\s*\(", condition):
            problems.append(f"{name} runs on `if: {condition}`")
        if "continue-on-error" in job:
            problems.append(f"{name} has continue-on-error")
    return problems


def e2e_gate_problems(e2e: dict) -> list[str]:
    """e2e.yml can be called, runs on main, and its non-quarantine pytest step
    fails the job when a test fails twice in a row."""
    problems = []
    triggers = e2e["on"]
    if "workflow_call" not in triggers:
        problems.append("e2e.yml has no workflow_call trigger")
    if "main" not in ((triggers.get("push") or {}).get("branches") or []):
        problems.append("e2e.yml does not run on pushes to main")
    for name, job in e2e["jobs"].items():
        if "continue-on-error" in job:
            problems.append(f"e2e.yml {name}: continue-on-error")
        gate = [s for s in job.get("steps", [])
                if "pytest" in str(s.get("run", "")) and "not quarantine" in str(s.get("run", ""))]
        if not gate:
            problems.append(f"e2e.yml {name}: no blocking step that leaves out quarantined tests")
        for step in gate:
            where = f"e2e.yml {name}: {step.get('name', 'gate')}"
            if "if" in step:
                problems.append(f"{where}: `if: {step['if']}` can skip the gate")
            if "--reruns 1" not in step["run"]:
                problems.append(f"{where}: not `--reruns 1`")
            problems += _blocking_problems(where, step, job, e2e)
    return problems


def changelog_step_problems(release: dict) -> list[str]:
    """The CHANGELOG/tag check runs on every tag push, before anything is
    built or published, and fails its job when it fails."""
    jobs = release["jobs"]
    publishers = _publish_jobs(jobs)
    if len(publishers) != 1:
        return [f"expected one PyPI publish job, found {publishers}"]
    job = jobs[publishers[0]]
    steps = job["steps"]
    checks = [i for i, s in enumerate(steps) if "tests.test_changelog_lint" in str(s.get("run", ""))]
    if len(checks) != 1:
        return ["the publish job does not run `python -m tests.test_changelog_lint` once"]
    check = checks[0]
    publish = next(i for i, s in enumerate(steps)
                   if str(s.get("uses", "")).startswith("pypa/gh-action-pypi-publish"))
    builds = [i for i, s in enumerate(steps) if "python -m build" in str(s.get("run", ""))]
    problems = []
    if not builds or not check < builds[0] < publish:
        problems.append("the CHANGELOG check must run before the build and the publish")
    step = steps[check]
    if '--tag "$GITHUB_REF_NAME"' not in step["run"]:
        problems.append("the CHANGELOG check is not given the pushed tag")
    # Runs on every tag push (a manual re-publish has no tag to check).
    if step.get("if", "github.event_name == 'push'") != "github.event_name == 'push'":
        problems.append(f"the CHANGELOG check runs only `if: {step['if']}`")
    if "push" not in release["on"]:
        problems.append("release.yml no longer runs on tag pushes")
    problems += _blocking_problems("CHANGELOG check", step, job, release)
    return problems


def _gate_problems(release: dict, e2e: dict) -> list[str]:
    return publish_gate_problems(release) + e2e_gate_problems(e2e) + changelog_step_problems(release)


def test_release_publish_needs_the_e2e_suite():
    assert publish_gate_problems(_workflow("release.yml")) == []


def test_e2e_workflow_is_callable_runs_on_main_and_blocks_on_failure():
    assert e2e_gate_problems(_workflow("e2e.yml")) == []


def test_release_runs_the_changelog_check_before_publishing():
    assert changelog_step_problems(_workflow("release.yml")) == []


def test_only_release_yml_starts_on_a_version_tag():
    # Anything published from its own `v*` tag trigger (the Docker image used
    # to be) skips the e2e and CHANGELOG gates; release.yml calls it instead.
    for path in sorted(WORKFLOWS.glob("*.yml")):
        triggers = _workflow(path.name)["on"]
        push = triggers.get("push") if isinstance(triggers, dict) else None
        tags = (push or {}).get("tags") or []
        starts = [t for t in tags if fnmatch.fnmatchcase("v1.2.3", t)]
        if path.name != "release.yml":
            assert not starts, f"{path.name} publishes on its own tag trigger {starts}"
    assert "workflow_call" in _workflow("docker.yml")["on"]


def test_desktop_release_runs_the_windows_shell_tests_before_publishing():
    # The open_external tests that keep links away from cmd.exe only run on
    # Windows; ci.yml runs them on PRs only, so the release must run them too.
    steps = _workflow("desktop-release.yml")["jobs"]["build"]["steps"]
    tests = [i for i, s in enumerate(steps)
             if str(s.get("run", "")).strip().startswith("cargo test")
             and s.get("working-directory") == "desktop/src-tauri"]
    publish = next(i for i, s in enumerate(steps)
                   if str(s.get("uses", "")).startswith("tauri-apps/tauri-action"))
    assert tests and tests[0] < publish
    step = steps[tests[0]]
    assert step.get("if") == "runner.os == 'Windows'"
    assert not step.get("continue-on-error")


def _e2e_job(e2e: dict) -> dict:
    return next(iter(e2e["jobs"].values()))


def _e2e_gate(e2e: dict) -> dict:
    return next(s for s in _e2e_job(e2e)["steps"] if "not quarantine" in str(s.get("run", "")))


def _changelog_step(release: dict) -> dict:
    return next(s for s in release["jobs"]["build-and-publish"]["steps"]
                if "tests.test_changelog_lint" in str(s.get("run", "")))


def _append(step: dict, text: str) -> None:
    step["run"] = step["run"].rstrip("\n") + text


def _move_changelog_after_build(release: dict) -> None:
    steps = release["jobs"]["build-and-publish"]["steps"]
    step = _changelog_step(release)
    steps.remove(step)
    build = next(i for i, s in enumerate(steps) if "python -m build" in str(s.get("run", "")))
    steps.insert(build + 1, step)


def _shell_without_errexit(e2e: dict) -> None:
    _e2e_job(e2e)["defaults"] = {"run": {"shell": "bash {0}"}}
    _append(_e2e_gate(e2e), "\necho done")


# Each mutation lets a failing e2e run or a mismatched CHANGELOG reach a
# publish; the checks above must catch every one of them.
GATE_BYPASSES = {
    "e2e gate `|| true`": lambda r, e: _append(_e2e_gate(e), " || true"),
    "e2e gate `; exit 0`": lambda r, e: _append(_e2e_gate(e), "; exit 0"),
    "e2e gate `|| :`": lambda r, e: _append(_e2e_gate(e), " || :"),
    "e2e gate piped": lambda r, e: _append(_e2e_gate(e), " | tee e2e.log"),
    "e2e gate in the background": lambda r, e: _append(_e2e_gate(e), " &"),
    "e2e gate, then `exit 0`": lambda r, e: _append(_e2e_gate(e), "\nexit 0"),
    "e2e gate continue-on-error": lambda r, e: _e2e_gate(e).update({"continue-on-error": True}),
    "e2e gate skipped": lambda r, e: _e2e_gate(e).update({"if": "false"}),
    "e2e gate under a shell without -e": lambda r, e: _shell_without_errexit(e),
    "e2e job continue-on-error": lambda r, e: _e2e_job(e).update({"continue-on-error": True}),
    "e2e job dropped from release": lambda r, e: r["jobs"].pop("e2e"),
    "publish ignores the e2e result": lambda r, e: r["jobs"]["build-and-publish"].update(
        {"if": "always()"}),
    "publish no longer needs e2e": lambda r, e: r["jobs"]["build-and-publish"].pop("needs"),
    "docker image published without e2e": lambda r, e: r["jobs"]["docker"].pop("needs"),
    "docker image not published by release": lambda r, e: r["jobs"].pop("docker"),
    "desktop runs after a failure": lambda r, e: r["jobs"]["desktop"].update(
        {"if": "${{ !cancelled() && github.event_name == 'push' }}"}),
    "CHANGELOG check `|| true`": lambda r, e: _append(_changelog_step(r), " || true"),
    "CHANGELOG check `; exit 0`": lambda r, e: _append(_changelog_step(r), "; exit 0"),
    "CHANGELOG check continue-on-error": lambda r, e: _changelog_step(r).update(
        {"continue-on-error": True}),
    "CHANGELOG check never runs": lambda r, e: _changelog_step(r).update({"if": "false"}),
    "CHANGELOG check under pwsh": lambda r, e: _changelog_step(r).update({"shell": "pwsh"}),
    "CHANGELOG check after the build": lambda r, e: _move_changelog_after_build(r),
}


@pytest.mark.parametrize("bypass", sorted(GATE_BYPASSES))
def test_gate_checks_catch_each_bypass(bypass):
    release, e2e = _workflow("release.yml"), _workflow("e2e.yml")
    assert _gate_problems(release, e2e) == []
    GATE_BYPASSES[bypass](release, e2e)
    assert _gate_problems(release, e2e), f"{bypass!r} was not caught"


if __name__ == "__main__":
    sys.exit(main())
