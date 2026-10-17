"""Evidence bundles: what a run did, kept for the director's review.

eVi stops a run with a machine-readable report and a redacted evidence
bundle (docs/roadmap.md, Phase 1, loop-evidence-provenance): the work as a
diff, eVi's own run of the checks, an excerpt of the conversation and the
environment, so a review reads deterministic evidence instead of the
model's word for it.

A bundle is a folder (`collect`):

    report.json    what the bundle holds (`SCHEMA`, ``evidence_version`` 1):
                   the environment (eVi and Python versions, platform,
                   backend and model, repository, worktree, branch, base and
                   head, the policy, whether the folder is trusted), the
                   task (id, title, attempt) when there is one, the v1 run
                   report (`evi.events.report_schema`) when there is one,
                   the diff's numbers, each check's result with its junit
                   totals, the transcript excerpt's numbers, the bundle's
                   files, what was redacted and when it was collected
    diff.patch     the work against its base: everything committed since
                   the base plus what is uncommitted in the work tree, and
                   each untracked file as a new one (at most
                   ``MAX_PATCH_CHARS``, then cut)
    diffstat.txt   one line per changed file, and the totals
    checks/        ``NN-<name>.txt``: each check's output, its end at most
                   ``MAX_CHECK_CHARS``; for a pytest check also
                   ``NN-<name>.junit.xml``, the junit report eVi's own run
                   asked pytest for (``--junitxml`` into the bundle: this is
                   director-side tooling, not a tool call the implementer
                   policy judges), whose totals report.json carries next to
                   the counts pytest printed
    transcript.md  the last ``TRANSCRIPT_MESSAGES`` messages of the run's
                   session transcript (only one under eVi's home), each cut
                   to ``TRANSCRIPT_MESSAGE_CHARS``, at most
                   ``TRANSCRIPT_CHARS`` in all

Every text in it goes through `evi.redact` (one Redactor, whose tally is
report.json's ``redactions``) before it is cut short, so no secret, nor a
prefix of one, is written; a junit report is redacted as the XML it is
(each text and attribute value as it reads unescaped). The checks run only
in a trusted folder (`evi.policy.folder_trusted`: each runs the folder's
code), through `evi.proc.run` without ``*_KEY`` / ``*_TOKEN`` /
``*_SECRET`` variables, and not for a task run that stopped before its
checks (an error, an interrupt); then ``checks_skipped`` says why none ran.

Where bundles go
    ``<eVi home>/evidence/<repo key>/<task id>/<attempt>/`` for a task
    (`task_bundle_path`; the repo key is `evi.tasks.task_home`'s),
    ``<eVi home>/evidence/<repo key>/.runs/<time>-<hex>/`` for a plain run
    (`run_bundle_path`), or a folder you name (``evi run --evidence DIR``,
    ``evi evidence collect --out DIR``: outside the evidence root, and not
    one another task of the repository names, `check_named_target`), which
    is then recorded in ``<eVi home>/evidence/registry.json``
    (`registered`). Only a task's
    summary of it (``evidence_summary``: hash, files, insertions, deletions,
    checks passed / failed, junit totals; `summarize`) goes in the tracked
    task file, with the bundle's path (``evidence``).

Writing
    A bundle is built in a hidden sibling folder and then renamed into
    place, replacing an older bundle there (never a folder that holds
    anything else): a reader sees a whole bundle or none. Its hash is
    `evi.attest.evidence_hash` of the folder, what an attestation signs.

Reading a path a task file names (`acceptable`, behind
`evi.tasks.evidence_path`)
    A task file is tracked, so its evidence path may come from anyone. It is
    read only when it is a bundle (a folder holding report.json, neither a
    link) under the repository's folder of the evidence root, reached
    through no link, or exactly a folder `registered` lists, with no link on
    the way to it.

Retention (`prune`)
    ``[evidence] keep_days`` (default ``DEFAULT_KEEP_DAYS``): ``evi evidence
    prune`` removes the repository's bundles older than that under the
    evidence root (a folder you named is yours), never one a task file
    still names as its evidence (`referenced`; a verified task's
    attestation re-checks its hash), nor any of a task whose file can't be
    read. Superseded attempts and plain-run bundles go once they are old
    enough, and so do half-written folders an interrupted write left.

Commit messages (`commit_body`)
    The subject from the task's title, its why, its acceptance criteria and
    the bundle's check results, the files changed, then the trailers
    ``Implemented-by: eVi (<backend>/<model>)``, ``Task: <id>`` and
    ``Evidence: <hash>``. The ``Verified-by``, ``Review-rounds`` and
    ``Attestation`` lines come only from `evi.attest.trailers` (a valid
    "verified" attestation at the branch's current head), never from here,
    and a line of the task's text that reads like any of these trailers is
    quoted (``> ``) so it can't pass for one.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

EVIDENCE_VERSION = 1
KIND = "evi-evidence"
DIRNAME = "evidence"
REPORT = "report.json"
PATCH = "diff.patch"
DIFFSTAT = "diffstat.txt"
CHECKS_DIR = "checks"
TRANSCRIPT = "transcript.md"
REGISTRY = "registry.json"
# Plain runs' bundles under a repository's folder: a name no task id can have.
RUNS = ".runs"
DEFAULT_KEEP_DAYS = 30
# More days than this keep every bundle (and no date goes out of range).
MAX_KEEP_DAYS = 36_500
# How much of each part a bundle keeps (characters, after redaction).
MAX_PATCH_CHARS = 8_000_000
MAX_CHECK_CHARS = 256_000
TRANSCRIPT_MESSAGES = 60
TRANSCRIPT_MESSAGE_CHARS = 4_000
TRANSCRIPT_CHARS = 64_000
# An untracked file bigger than this is named in the patch, not shown.
MAX_NEW_FILE_BYTES = 1_000_000
# A junit report bigger than this is dropped (its totals with it).
MAX_JUNIT_BYTES = 32 * 1024 * 1024
# How much of a transcript's end is read for the excerpt.
TRANSCRIPT_READ_BYTES = 4 * 1024 * 1024
# The most changed paths report.json lists.
MAX_PATHS = 1_000
_REGISTRY_MAX = 500
_GIT_TIMEOUT = 120.0
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# A half-written bundle older than this is a leftover `prune` removes.
_LEFTOVER_HOURS = 24
# Waits between tries while Windows refuses to rename a folder something
# (an indexer, a virus scanner) has open for a moment.
_SHARING_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)
# The trailers a commit message's provenance rests on: a line of the task's
# own text that reads like one is quoted (`commit_body`).
_PROVENANCE = ("Verified-by", "Review-rounds", "Attestation", "Implemented-by", "Task",
               "Evidence")
_TRAILER_LIKE = re.compile(r"\s*(?:%s)\s*:" % "|".join(map(re.escape, _PROVENANCE)), re.I)
# One count of pytest's closing summary line ("2 passed, 1 failed in 0.12s").
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")
_PYTEST_TIME = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_SLUG = re.compile(r"[^A-Za-z0-9_.-]+")


class EvidenceError(RuntimeError):
    """A bundle that can't be collected (the CLI exits 2)."""


# ---- where bundles go ---------------------------------------------------------------


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _stamp(when: _dt.datetime) -> str:
    return when.astimezone(_dt.timezone.utc).strftime(_TIME_FORMAT)


def evidence_root() -> Path:
    """``<eVi home>/evidence``."""
    from evi import config

    return Path(config.HOME) / DIRNAME


def repo_key(root: str | os.PathLike[str]) -> str:
    """The repository's key: the same as `evi.tasks.task_home`'s, a hash of
    the main checkout's path."""
    return hashlib.sha256(
        os.path.normcase(os.path.realpath(root)).encode("utf-8", "surrogateescape")
    ).hexdigest()[:16]


def repo_folder(root: str | os.PathLike[str]) -> Path:
    """The repository's folder under the evidence root."""
    return evidence_root() / repo_key(root)


def task_bundle_path(root: str | os.PathLike[str], task_id: str, attempt: int) -> Path:
    """Where a task's bundle for `attempt` goes."""
    from evi.tasks import validate_id

    return repo_folder(root) / validate_id(task_id) / str(int(attempt))


def run_bundle_path(root: str | os.PathLike[str], now: _dt.datetime | None = None) -> Path:
    """A new folder for a plain run's bundle."""
    when = (now or now_utc()).astimezone(_dt.timezone.utc)
    return repo_folder(root) / RUNS / f"{when.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def _is_link(path: str | os.PathLike[str]) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _p: False)
    return os.path.islink(path) or isjunction(path)


def _under(path: str | os.PathLike[str], base: str | os.PathLike[str]) -> bool:
    try:
        a = os.path.normcase(os.path.abspath(path))
        b = os.path.normcase(os.path.abspath(base))
        return os.path.commonpath([a, b]) == b
    except ValueError:  # another drive
        return False


def is_bundle(path: str | os.PathLike[str]) -> bool:
    """`path` is a folder (not a link or a junction) holding report.json as
    a regular file."""
    try:
        if _is_link(path) or not os.path.isdir(path):
            return False
        return stat.S_ISREG(os.lstat(Path(path) / REPORT).st_mode)
    except OSError:
        return False


def registered() -> list[str]:
    """The bundle folders written outside the evidence root (``evi run
    --evidence DIR``, ``evi evidence collect --out DIR``), as recorded."""
    try:
        data = json.loads((evidence_root() / REGISTRY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("bundles") if isinstance(data, dict) else None
    return [p for p in items if isinstance(p, str)] if isinstance(items, list) else []


def _register(path: Path) -> None:
    """Record a bundle written outside the evidence root (`registered`)."""
    full = os.path.abspath(path)
    if _under(full, evidence_root()):
        return
    items = [p for p in registered() if os.path.normcase(p) != os.path.normcase(full)]
    items.append(full)
    _write_json(evidence_root() / REGISTRY, {"bundles": items[-_REGISTRY_MAX:]})


def _linked(path: str | os.PathLike[str]) -> bool:
    """`path` (absolute), or a folder on the way to it, is a link or a
    junction (an lstat of each step, never one that follows a link)."""
    p = Path(path)
    return any(_is_link(step) for step in (*reversed(p.parents), p))


def _unlinked(path: Path) -> Path:
    """`path` (absolute, itself no link) with the folders on the way to it
    resolved when one of them is a link (macOS's /var, say): what the
    registry records is then a path no link is on, so a link put on the way
    later shows (`acceptable`)."""
    if _linked(path.parent):
        return Path(os.path.realpath(path.parent)) / path.name
    return path


def acceptable(full: str, root: str | os.PathLike[str]) -> bool:
    """Whether a task of the repository at `root` may name `full` (absolute
    and normalised) as its evidence: a bundle under the repository's folder
    of the evidence root, through no link (`evi.tasks._inside`), or exactly
    a `registered` one with no link on the way to it."""
    from evi.tasks import _inside

    folder = os.path.abspath(repo_folder(root))
    norm = os.path.normcase(full)
    if norm != os.path.normcase(folder) and _inside(full, folder):
        return is_bundle(full)
    if any(os.path.normcase(os.path.abspath(p)) == norm for p in registered()):
        return not _linked(full) and is_bundle(full)
    return False


# ---- the diff -----------------------------------------------------------------------


@dataclass
class DiffInfo:
    """The work in a folder against its base (`collect_diff`). `paths`:
    [{path, added, deleted}] (None: binary or not counted)."""

    base: str | None = None
    head: str | None = None
    patch: str = ""
    paths: list[dict[str, Any]] = field(default_factory=list)
    untracked: int = 0
    note: str | None = None

    @property
    def files(self) -> int:
        return len(self.paths)

    @property
    def insertions(self) -> int:
        return sum(p["added"] or 0 for p in self.paths)

    @property
    def deletions(self) -> int:
        return sum(p["deleted"] or 0 for p in self.paths)


def _git(folder: Path, *args: str) -> Any:
    """`git <args>` hardened (`evi.gitsafe`), without optional locks: reading
    a work tree never writes its index."""
    from evi import gitsafe

    return gitsafe.run_git(["--no-optional-locks", *args], cwd=folder, timeout=_GIT_TIMEOUT)


def _commit(folder: Path, ref: str) -> str | None:
    """The full sha `ref` names in `folder`'s repository, or None."""
    from evi import gitsafe

    try:
        safe = gitsafe.validate_ref(ref)
        proc = _git(folder, "rev-parse", "--verify", "--quiet", safe + "^{commit}")
    except Exception:  # noqa: BLE001 — an unsafe ref, no git, a timeout: no commit
        return None
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and out else None


def commit_of(folder: str | os.PathLike[str], ref: str = "HEAD") -> str | None:
    """The full sha `ref` names in the repository at `folder`, or None."""
    return _commit(Path(folder), ref)


def _work_tree_top(folder: Path) -> Path | None:
    """The top of the git work tree `folder` is in, or None outside one, or
    for a work tree that isn't the repository's own (`evi.gitsafe.
    is_own_work_tree`: its config points git at files somewhere else)."""
    from evi import gitsafe

    try:
        proc = _git(folder, "rev-parse", "--is-inside-work-tree", "--show-prefix",
                    "--show-cdup")
    except Exception:  # noqa: BLE001
        return None
    lines = (proc.stdout or "").splitlines() if proc.returncode == 0 else []
    if len(lines) < 2 or lines[0].strip() != "true":
        return None
    prefix = lines[1]
    cdup = lines[2].strip() if len(lines) > 2 else ""
    try:
        if not gitsafe.is_own_work_tree(folder, prefix):
            return None
    except Exception:  # noqa: BLE001
        return None
    return (folder / cdup).resolve() if cdup else folder


def _branch(folder: Path) -> str | None:
    try:
        proc = _git(folder, "symbolic-ref", "--quiet", "--short", "HEAD")
    except Exception:  # noqa: BLE001
        return None
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and out else None


def _new_file_patch(top: Path, rel: str) -> tuple[str, int | None]:
    """An untracked file as a new file's patch, and its line count (None for
    a binary, a link or one too big to show)."""
    path = top / rel
    head = f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n"
    if _is_link(path):
        return head + "(a link: not followed)\n", None
    try:
        size = path.stat().st_size
        if size > MAX_NEW_FILE_BYTES:
            return head + f"(a new file of {size} bytes: not shown)\n", None
        data = path.read_bytes()
    except OSError as exc:
        return head + f"(can't be read: {exc.strerror or exc})\n", None
    if b"\0" in data:
        return head + f"Binary files /dev/null and b/{rel} differ\n", None
    if not data:
        return head, 0
    lines = data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
    ends = lines[-1] == ""
    if ends:
        lines.pop()
    body = [f"--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n"]
    body.extend(f"+{line}\n" for line in lines)
    if not ends:
        body.append("\\ No newline at end of file\n")
    return head + "".join(body), len(lines)


def collect_diff(folder: str | os.PathLike[str], base: str | None) -> DiffInfo:
    """The work in the git work tree `folder` against `base`: committed since
    the base and uncommitted, untracked files as new ones. What git can't
    tell is in `note`; never raises."""
    from evi import gitsafe

    info = DiffInfo()
    top = _work_tree_top(Path(folder))
    info.head = _commit(top, "HEAD") if top is not None else None
    if top is None or info.head is None:
        info.note = f"{folder} is not a git work tree of its own with a commit"
        return info
    if not str(base or "").strip():
        info.note = "no base to compare against"
        return info
    info.base = _commit(top, str(base))
    if info.base is None:
        info.note = f"the base {str(base)[:80]!r} names no commit here"
        return info
    try:
        rev = gitsafe.rev_args(info.base, what="base")
        flags = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames")
        patch = _git(top, "diff", *flags, *rev, "--")
        numstat = _git(top, "diff", "--numstat", "-z", *flags, *rev, "--")
        untracked = _git(top, "ls-files", "--others", "--exclude-standard", "-z")
    except Exception as exc:  # noqa: BLE001 — a bundle says less rather than fail
        info.note = f"git could not diff: {type(exc).__name__}: {exc}"
        return info
    if patch.returncode != 0 or numstat.returncode != 0 or untracked.returncode != 0:
        bad = next(p for p in (patch, numstat, untracked) if p.returncode != 0)
        info.note = f"git could not diff: {(bad.stderr or '').strip()[:300]}"
        return info
    parts = [patch.stdout or ""]
    for entry in (numstat.stdout or "").split("\0"):
        bits = entry.split("\t", 2)
        if len(bits) != 3 or not bits[2]:
            continue
        info.paths.append({"path": bits[2],
                           "added": int(bits[0]) if bits[0].isdigit() else None,
                           "deleted": int(bits[1]) if bits[1].isdigit() else None})
    for rel in sorted(p for p in (untracked.stdout or "").split("\0") if p):
        text, lines = _new_file_patch(top, rel)
        parts.append(text)
        info.paths.append({"path": rel, "added": lines, "deleted": 0 if lines is not None else None})
        info.untracked += 1
    info.patch = "".join(parts)
    return info


def diffstat_text(diff: DiffInfo) -> str:
    """`diff` as diffstat.txt: one line per path, then the totals."""
    lines = []
    if diff.note:
        lines.append(f"({diff.note})")
    for p in diff.paths:
        added = "-" if p["added"] is None else f"+{p['added']}"
        deleted = "-" if p["deleted"] is None else f"-{p['deleted']}"
        lines.append(f" {p['path']} | {added} {deleted}")
    lines.append(f" {diff.files} file{'' if diff.files == 1 else 's'} changed, "
                 f"{diff.insertions} insertions(+), {diff.deletions} deletions(-)")
    return "\n".join(lines) + "\n"


# ---- checks -------------------------------------------------------------------------


def _pytest_at(argv: Sequence[str]) -> int | None:
    """Where `argv` names pytest (``pytest ...``, ``python -m pytest ...`` or
    a runner's ``uv run pytest ...``), or None when it doesn't run pytest."""
    words = [str(w) for w in argv]

    def name(word: str) -> str:
        base = os.path.basename(word.replace("\\", "/")).lower()
        return base[:-4] if base.endswith(".exe") else base

    if not words:
        return None
    if name(words[0]) in ("pytest", "py.test"):
        return 0
    for at, (word, nxt) in enumerate(zip(words, words[1:])):
        if word == "-m" and nxt == "pytest":
            return at + 1
    if len(words) > 2 and words[1] == "run" and name(words[2]) in ("pytest", "py.test"):
        return 2
    return None


def is_pytest(argv: Sequence[str]) -> bool:
    """`argv` runs pytest: ``pytest ...``, ``python -m pytest ...`` or a
    runner's ``uv run pytest ...``."""
    return _pytest_at(argv) is not None


def junit_argv(argv: Sequence[str], junit_file: str | os.PathLike[str]) -> list[str]:
    """The pytest check `argv` asking pytest for a junit report at
    `junit_file`: the flag goes last (after any of the check's own), or just
    before a ``--`` past the pytest word, after which pytest reads every
    word as a path."""
    words = [str(w) for w in argv]
    flag = f"--junitxml={junit_file}"
    at = _pytest_at(words)
    if at is not None and "--" in words[at + 1:]:
        end = words.index("--", at + 1)
        return [*words[:end], flag, *words[end:]]
    return [*words, flag]


def pytest_counts(output: str) -> dict[str, int] | None:
    """The counts in pytest's closing summary line ("2 passed, 1 failed in
    0.1s"), or None when the output has none."""
    for line in reversed(str(output or "").splitlines()):
        if not _PYTEST_TIME.search(line):
            continue
        found = _PYTEST_COUNT.findall(line)
        if not found and "no tests ran" not in line:
            continue
        counts = dict.fromkeys(("passed", "failed", "errors", "skipped", "xfailed", "xpassed",
                                "deselected"), 0)
        for n, word in found:
            counts["errors" if word.startswith("error") else word] += int(n)
        return counts
    return None


def junit_totals(text: str | bytes) -> dict[str, int] | None:
    """{tests, failures, errors, skipped, passed} of a junit report (summed
    over its top-level test suites), or None when it doesn't parse."""
    import xml.etree.ElementTree as ET

    data = text.encode("utf-8") if isinstance(text, str) else text
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    for suite in suites:
        for key in totals:
            try:
                totals[key] += max(0, int(suite.get(key) or 0))
            except ValueError:
                return None
    totals["passed"] = max(0, totals["tests"] - totals["failures"] - totals["errors"]
                           - totals["skipped"])
    return totals


def _slug(name: str) -> str:
    return _SLUG.sub("-", name).strip("-.")[:40] or "check"


def _cut_end(text: str, limit: int) -> tuple[str, bool]:
    """`text`'s end, at most `limit` characters."""
    if len(text) <= limit:
        return text, False
    return "…(the start is cut)\n" + text[-(limit - 20):], True


def _cut_start(text: str, limit: int) -> tuple[str, bool]:
    """`text`'s start, at most `limit` characters."""
    if len(text) <= limit:
        return text, False
    return text[:limit - 20] + "\n…(the rest is cut)\n", True


def _run_check(argv: Sequence[str], cwd: Path, out: Path, n: int, red: Any, *,
               timeout: float, cancel_token: threading.Event | None = None) -> dict[str, Any]:
    """Run one check in `cwd`, its output (and a pytest check's junit
    report) into `out`; its report.json entry. Its process tree is killed
    once `cancel_token` is set."""
    from evi import proc
    from evi.checks import display

    words = [str(w) for w in argv]
    slug = f"{n:02d}-{_slug(Path(words[0]).name or words[0])}"
    junit_file = out / f"{slug}.junit.xml" if is_pytest(words) else None
    run_argv = junit_argv(words, junit_file) if junit_file is not None else words
    env = proc.scrubbed_env()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    note = ""
    try:
        res = proc.run(run_argv, cwd=str(cwd), timeout=timeout, cancel_token=cancel_token,
                       env=env, merge_stderr=True, encoding="utf-8")
        output, code = res.output, res.returncode
        if res.timed_out:
            code, note = None, f"timed out after {timeout:g}s (its process tree was killed)"
        elif res.cancelled:
            code, note = None, "cancelled"
    except OSError as exc:
        output, code, note = f"could not start: {exc}", None, "could not start"
    duration = max(0, int((time.monotonic() - started) * 1000))
    text, _ = _cut_end(red(output), MAX_CHECK_CHARS)
    _write_text(out / f"{slug}.txt", text)
    junit = None
    junit_rel = None
    if junit_file is not None and junit_file.is_file() and not _is_link(junit_file):
        junit = _redact_junit(junit_file, red)
        if junit_file.exists():
            junit_rel = f"{CHECKS_DIR}/{junit_file.name}"
    return {
        "name": Path(words[0]).name or words[0], "cmd": display(words), "exit": code,
        "ok": code == 0, "duration_ms": duration, "note": note, "source": "required",
        "output": f"{CHECKS_DIR}/{slug}.txt", "junit_file": junit_rel, "junit": junit,
        "reported": pytest_counts(output) if junit_file is not None else None,
    }


def _redact_junit(path: Path, red: Any) -> dict[str, int] | None:
    """Redact the junit report at `path` in place, and return its totals.
    pytest writes captured output and failure messages XML-escaped (a
    password's ``&`` as ``&amp;``), so the report is parsed and each text and
    attribute value redacted as it reads, then written out again. None, and
    the file removed, when it is too big, can't be read or doesn't parse:
    what can't be redacted is not kept."""
    import xml.etree.ElementTree as ET

    root = None
    try:
        if path.stat().st_size <= MAX_JUNIT_BYTES:
            root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError):
        root = None
    if root is None:
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    for element in root.iter():
        if element.text:
            element.text = red(element.text)
        if element.tail:
            element.tail = red(element.tail)
        for key, value in list(element.attrib.items()):
            element.set(key, red(value))
    clean = '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode")
    _write_text(path, clean)
    return junit_totals(clean)


def _recorded_check(record: Mapping[str, Any], out: Path, n: int, red: Any) -> dict[str, Any]:
    """A check the run itself ran (`evi run`'s verify gate: its report's
    ``checks`` entry, the end of its output only)."""
    name = str(record.get("name") or "check")
    slug = f"{n:02d}-{_slug(name)}"
    _write_text(out / f"{slug}.txt", red(str(record.get("tail") or "")))
    code = record.get("exit")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    try:
        duration = max(0, int(record.get("duration_ms") or 0))
    except (TypeError, ValueError):
        duration = 0
    return {
        "name": name, "cmd": str(record.get("cmd") or ""), "exit": code, "ok": code == 0,
        "duration_ms": duration, "note": "", "source": "run",
        "output": f"{CHECKS_DIR}/{slug}.txt", "junit_file": None, "junit": None,
        "reported": None,
    }


def _junit_sum(records: Sequence[Mapping[str, Any]]) -> dict[str, int] | None:
    found = [r["junit"] for r in records if r.get("junit")]
    if not found:
        return None
    return {k: sum(int(j.get(k) or 0) for j in found)
            for k in ("tests", "failures", "errors", "skipped", "passed")}


# ---- the transcript excerpt ---------------------------------------------------------


def transcript_excerpt(path: str | os.PathLike[str], red: Any = None
                       ) -> tuple[str, dict[str, Any]] | None:
    """The end of the session transcript at `path` as Markdown, redacted
    (`red`, a Redactor) and bounded, with its numbers; None unless it is a
    ``.jsonl`` file (not a link) under eVi's home."""
    from evi import config
    from evi.redact import Redactor

    red = red if red is not None else Redactor()
    p = Path(path)
    if p.suffix != ".jsonl" or _is_link(p) or not (
            _under(p, config.HOME) or _under(p, config.TRANSCRIPTS_DIR)):
        return None
    try:
        if not p.is_file():
            return None
        size = p.stat().st_size
        with p.open("rb") as fh:
            cut = size > TRANSCRIPT_READ_BYTES
            if cut:
                fh.seek(size - TRANSCRIPT_READ_BYTES)
            data = fh.read(TRANSCRIPT_READ_BYTES)
    except OSError:
        return None
    if cut:
        data = data.split(b"\n", 1)[1] if b"\n" in data else b""
    entries = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and str(obj.get("type") or "message") == "message" and (
                obj.get("role") in ("user", "assistant", "tool")):
            entries.append(obj)
    shown = entries[-TRANSCRIPT_MESSAGES:]
    session = next((str(e.get("session")) for e in reversed(entries) if e.get("session")), None)

    def part(value: Any, limit: int = TRANSCRIPT_MESSAGE_CHARS) -> str:
        return _cut_start(red(str(value if value is not None else "")).strip(), limit)[0]

    out = ["# Transcript excerpt", "",
           f"Session {session or '(unknown)'}: the last {len(shown)} of {len(entries)}"
           f"{'+' if cut else ''} messages, redacted.", ""]
    for e in shown:
        role = e.get("role")
        if role == "tool":
            out += [f"## tool {part(e.get('tool_name') or '', 200)}", ""]
        else:
            out += [f"## {role}", ""]
        if str(e.get("content") or "").strip():
            out += [part(e.get("content")), ""]
        for call in e.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                out += [f"- tool call {part(fn.get('name') or '', 200)}: "
                        f"{part(red.args(str(fn.get('arguments') or '')), 1000)}"]
        if e.get("tool_calls"):
            out.append("")
    text, truncated = _cut_end(red("\n".join(out)), TRANSCRIPT_CHARS)
    return text, {"file": TRANSCRIPT, "source": str(p), "session_id": session,
                  "messages": len(entries), "shown": len(shown), "truncated": truncated or cut}


# ---- the report and its schema ------------------------------------------------------


def _str() -> dict[str, Any]:
    return {"type": "string"}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [{"type": "null"}, schema]}


_COUNT = {"type": "integer", "minimum": 0}
_TIME = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"}
_JUNIT_KEYS = ("tests", "failures", "errors", "skipped", "passed")


def _schema() -> dict[str, Any]:
    from evi import events

    defs = events.schema()["$defs"]  # the run report and its parts
    defs.update({
        "evidence_environment": {
            "type": "object",
            "properties": {
                "evi_version": _str(), "python": _str(), "implementation": _str(),
                "platform": _str(), "backend": _nullable(_str()), "model": _nullable(_str()),
                "repo": _nullable(_str()), "worktree": _nullable(_str()),
                "branch": _nullable(_str()), "base": _nullable(_str()),
                "head": _nullable(_str()), "policy": _nullable(_str()),
                "trusted": {"type": "boolean"},
            },
            "required": ["evi_version", "python", "implementation", "platform", "backend",
                         "model", "repo", "worktree", "branch", "base", "head", "policy",
                         "trusted"],
        },
        "evidence_task": {
            "type": "object",
            "properties": {"id": _str(), "title": _str(), "attempt": {"type": "integer",
                                                                      "minimum": 1},
                           "branch": _str(), "status": _str()},
            "required": ["id", "title", "attempt", "branch", "status"],
        },
        "evidence_diff": {
            "type": "object",
            "properties": {
                "base": _nullable(_str()), "head": _nullable(_str()), "files": _COUNT,
                "insertions": _COUNT, "deletions": _COUNT, "untracked": _COUNT,
                "patch": _str(), "patch_chars": _COUNT, "truncated": {"type": "boolean"},
                "note": _nullable(_str()),
                "paths": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"path": _str(), "added": _nullable(_COUNT),
                                   "deleted": _nullable(_COUNT)},
                    "required": ["path", "added", "deleted"],
                }},
                "paths_listed": _COUNT,
            },
            "required": ["base", "head", "files", "insertions", "deletions", "untracked",
                         "patch", "patch_chars", "truncated", "note", "paths",
                         "paths_listed"],
        },
        "evidence_junit": {
            "type": "object",
            "properties": {k: _COUNT for k in _JUNIT_KEYS},
            "required": list(_JUNIT_KEYS),
        },
        "evidence_check": {
            "type": "object",
            "properties": {
                "name": _str(), "cmd": _str(), "exit": _nullable({"type": "integer"}),
                "ok": {"type": "boolean"}, "duration_ms": _COUNT, "note": _str(),
                # "required": eVi's own run of a required check; "run": a
                # check the run itself ran (its output's end only).
                "source": {"enum": ["required", "run"]},
                "output": _str(), "junit_file": _nullable(_str()),
                "junit": _nullable({"$ref": "#/$defs/evidence_junit"}),
                # What pytest printed in its closing summary line.
                "reported": _nullable({"type": "object", "additionalProperties": _COUNT}),
            },
            "required": ["name", "cmd", "exit", "ok", "duration_ms", "note", "source",
                         "output", "junit_file", "junit", "reported"],
        },
        "evidence_transcript": {
            "type": "object",
            "properties": {"file": _str(), "source": _str(), "session_id": _nullable(_str()),
                           "messages": _COUNT, "shown": _COUNT,
                           "truncated": {"type": "boolean"}},
            "required": ["file", "source", "session_id", "messages", "shown", "truncated"],
        },
    })
    props: dict[str, Any] = {
        "evidence_version": {"const": EVIDENCE_VERSION},
        "kind": {"const": KIND},
        "created": _TIME,
        "environment": {"$ref": "#/$defs/evidence_environment"},
        "task": _nullable({"$ref": "#/$defs/evidence_task"}),
        # The v1 run report (`evi run --format json`; a task's has a `task`
        # section too), redacted.
        "run": _nullable({"$ref": "#/$defs/report"}),
        "run_note": _nullable(_str()),
        "diff": {"$ref": "#/$defs/evidence_diff"},
        "checks": {"type": "array", "items": {"$ref": "#/$defs/evidence_check"}},
        "checks_skipped": _nullable(_str()),
        "junit": _nullable({"$ref": "#/$defs/evidence_junit"}),
        "transcript": _nullable({"$ref": "#/$defs/evidence_transcript"}),
        "files": {"type": "array", "items": _str()},
        "redactions": {"type": "object", "additionalProperties": {"type": "integer"}},
        "timestamps": {
            "type": "object",
            "properties": {"started": _TIME, "finished": _TIME},
            "required": ["started", "finished"],
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:evi:evidence:v{EVIDENCE_VERSION}",
        "title": f"eVi evidence bundle report, v{EVIDENCE_VERSION}",
        "description": "report.json of an evidence bundle (evi/evidence.py).",
        "type": "object",
        "properties": props,
        "required": list(props),
        "$defs": defs,
    }


# What report.json validates against (JSON Schema 2020-12).
SCHEMA: dict[str, Any] = _schema()


def schema_errors(report: Mapping[str, Any], schema: Mapping[str, Any] | None = None) -> list[str]:
    """Why `report` doesn't match `schema` (default `SCHEMA`); [] when it
    does, or when jsonschema (an optional dependency) isn't installed."""
    try:
        import jsonschema
    except ImportError:
        return []
    validator = jsonschema.Draft202012Validator(dict(schema or SCHEMA))
    return sorted(f"{'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.message}"
                  for e in validator.iter_errors(dict(report)))


def _run_report(report: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    """`report` when it is a v1 run report, else None and why not."""
    if report is None:
        return None, None
    if not isinstance(report, Mapping):
        return None, "the run report is not a JSON object"
    from evi import events

    bad = schema_errors(report, events.report_schema())
    if bad:
        return None, f"the run report does not match the v1 schema ({bad[0][:200]})"
    return dict(report), None


def environment(*, folder: Path | None, repo: Any, base: str | None, head: str | None,
                run: Mapping[str, Any] | None, task: Mapping[str, Any] | None,
                trusted: bool) -> dict[str, Any]:
    """report.json's ``environment``."""
    import evi

    def text(value: Any) -> str | None:
        return str(value) if value not in (None, "") else None

    run = run or {}
    task = task or {}
    return {
        "evi_version": str(getattr(evi, "__version__", "") or ""),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "backend": text(run.get("backend")) or text(task.get("backend")),
        "model": text(run.get("model")) or text(task.get("model")),
        "repo": text(repo),
        "worktree": text(folder),
        "branch": _branch(folder) if folder is not None else None,
        "base": base,
        "head": head,
        "policy": text(run.get("policy")),
        "trusted": bool(trusted),
    }


# ---- writing a bundle -----------------------------------------------------------------


def _write_text(path: Path, text: str) -> None:
    # A lone surrogate (a model's broken emoji) goes out as its \uXXXX escape.
    with open(path, "w", encoding="utf-8", errors="backslashreplace", newline="\n") as fh:
        fh.write(text)


def _write_json(path: Path, data: Any) -> None:
    """`data` as JSON at `path`, atomically (a temporary file, then a rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", errors="backslashreplace", newline="\n") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        _retry(os.replace, tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _retry(op: Any, *args: Any) -> Any:
    """`op(*args)`, tried again for a moment while Windows refuses it
    (PermissionError: something has the folder open)."""
    for delay in _SHARING_DELAYS:
        try:
            return op(*args)
        except PermissionError:
            time.sleep(delay)
    return op(*args)


def _check_target(out: Path) -> None:
    """Refuse a target folder that isn't a new or empty one or a bundle."""
    if _is_link(out):
        raise EvidenceError(f"{out} is a link: a bundle goes only in a folder of its own")
    if os.path.lexists(out):
        if not out.is_dir():
            raise EvidenceError(f"{out} is a file, not a folder for an evidence bundle")
        if not is_bundle(out) and any(out.iterdir()):
            raise EvidenceError(f"{out} holds something other than an evidence bundle; name a "
                                "new or empty folder")


def check_target(out: str | os.PathLike[str]) -> Path:
    """`out` as an absolute path, refused (EvidenceError) unless a bundle may
    be written there: a new or empty folder, or an older bundle."""
    path = Path(os.path.abspath(Path(out).expanduser()))
    try:
        _check_target(path)
    except OSError as exc:
        raise EvidenceError(f"{path} can't be used for a bundle: {exc}") from exc
    return path


def check_named_target(out: str | os.PathLike[str], root: str | os.PathLike[str] | None = None,
                       *, own: str = "") -> Path:
    """A folder you name for a bundle (``evi run --evidence DIR``, ``evi
    evidence collect --out DIR``) as the absolute path to write, refused
    (EvidenceError) unless `check_target` takes it, it is outside the
    evidence root (eVi files its own bundles there, and a task may name only
    those of its repository's folder) and no task file of the repository at
    `root` names it as its evidence (writing over it would change what that
    task, and any attestation of it, names), but task `own` collecting its
    own bundle again."""
    path = check_target(out)
    top = evidence_root()
    if _under(path, top):
        raise EvidenceError(f"{path} is under {top}, where eVi files its own bundles: name a "
                            "folder elsewhere, or none for eVi's own place")
    path = _unlinked(path)
    if root is not None and is_bundle(path):
        from evi.tasks import printable

        norm = os.path.normcase(str(path))
        for task_id, named in _named(root)[0]:
            if named == norm and task_id.casefold() != own.strip().casefold():
                raise EvidenceError(f"{path} is task {printable(task_id)}'s evidence bundle; "
                                    "writing over it would change what that task names, so "
                                    "name another folder")
    return path


def _swap_in(tmp: Path, out: Path) -> None:
    """Put the finished bundle `tmp` at `out`, replacing an older bundle (or
    an empty folder) there."""
    _check_target(out)
    old = None
    if os.path.lexists(out):
        if is_bundle(out):
            old = out.with_name(f".{out.name}.old-{secrets.token_hex(4)}")
            _retry(os.replace, out, old)
        else:
            out.rmdir()  # empty: _check_target says so
    try:
        _retry(os.replace, tmp, out)
    except BaseException:
        if old is not None:
            with contextlib.suppress(OSError):
                os.replace(old, out)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


@dataclass
class Bundle:
    """A bundle `collect` wrote: where, its hash (`evi.attest.evidence_hash`)
    and its report."""

    path: Path
    hash: str
    report: dict[str, Any]

    @property
    def summary(self) -> dict[str, Any]:
        return summarize(self.report, self.hash)


def summarize(report: Mapping[str, Any], digest: str) -> dict[str, Any]:
    """What a task file records of a bundle (``evidence_summary``): its hash,
    files changed, insertions and deletions, checks passed and failed, and
    the junit totals when a pytest check ran. Every value survives TOML."""
    diff = report.get("diff") or {}
    checks = report.get("checks") or []
    out: dict[str, Any] = {
        "hash": str(digest or ""),
        "files": int(diff.get("files") or 0),
        "insertions": int(diff.get("insertions") or 0),
        "deletions": int(diff.get("deletions") or 0),
        "checks_passed": sum(1 for c in checks if c.get("ok")),
        "checks_failed": sum(1 for c in checks if not c.get("ok")),
    }
    junit = report.get("junit")
    if isinstance(junit, Mapping):
        out["junit"] = {k: int(junit.get(k) or 0) for k in ("tests", "failures", "errors",
                                                             "skipped")}
    if report.get("checks_skipped"):
        out["checks_skipped"] = str(report["checks_skipped"])[:300]
    return out


def collect(
    out: str | os.PathLike[str],
    *,
    folder: str | os.PathLike[str] | None = None,
    base: str | None = None,
    run_report: Mapping[str, Any] | None = None,
    checks: Sequence[Sequence[str]] = (),
    check_records: Sequence[Mapping[str, Any]] = (),
    task: Mapping[str, Any] | None = None,
    attempt: int | None = None,
    repo: str | os.PathLike[str] | None = None,
    config: Any = None,
    no_checks: str = "",
    cancel_token: threading.Event | None = None,
) -> Bundle:
    """Write an evidence bundle to the folder `out` (see the module doc) and
    return it.

    `folder`: the git work tree the work is in (the diff against `base`, and
    where `checks`, argv lists, run: only when it is trusted). `run_report`:
    the v1 run report (its transcript is excerpted); `check_records`: checks
    the run ran itself (its report's ``checks``). `task` and `attempt` name
    the task, `repo` the main checkout; `config` gives ``[checks] timeout``.
    `no_checks`: why none of `checks` runs (the run stopped before its
    checks), said as ``checks_skipped``. `cancel_token`, once set, stops the
    running check and starts no other. A bundle written outside the
    evidence root is `registered`. EvidenceError when `out` can't take a
    bundle; nothing is written then."""
    from evi import attest, checks as _checks
    from evi.redact import Redactor

    started = now_utc()
    target = check_target(out)
    if not _under(target, evidence_root()):
        target = _unlinked(target)
    where = Path(folder).resolve() if folder is not None else None
    trusted = bool(where is not None and _trusted(where))
    diff = collect_diff(where, base) if where is not None else DiffInfo(note="no work tree")
    rr, run_note = _run_report(run_report)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{secrets.token_hex(4)}")
    tmp.mkdir()
    try:
        red = Redactor()
        patch, truncated = _cut_start(red(diff.patch), MAX_PATCH_CHARS)
        _write_text(tmp / PATCH, patch)
        _write_text(tmp / DIFFSTAT, red(diffstat_text(diff)))
        out_checks = tmp / CHECKS_DIR
        out_checks.mkdir()
        records: list[dict[str, Any]] = []
        skipped = None
        if checks:
            if no_checks:
                skipped = str(no_checks)
            elif where is None:
                skipped = "no work tree to run the checks in"
            elif not trusted:
                skipped = (f"{where} is not a trusted folder, so no check ran (each runs the "
                           "folder's code); `evi trust` it first")
            else:
                timeout = _checks.settings(config).timeout
                for n, argv in enumerate(checks, 1):
                    if cancel_token is not None and cancel_token.is_set():
                        break
                    records.append(_run_check(argv, where, out_checks, n, red, timeout=timeout,
                                              cancel_token=cancel_token))
                if cancel_token is not None and cancel_token.is_set():
                    skipped = (f"cancelled: eVi stopped running the checks after {len(records)} "
                               f"of {len(checks)}")
        for n, record in enumerate(check_records, len(records) + 1):
            if isinstance(record, Mapping):
                records.append(_recorded_check(record, out_checks, n, red))
        transcript = None
        source = (rr or {}).get("transcript_path")
        if source:
            excerpt = transcript_excerpt(str(source), red)
            if excerpt is not None:
                _write_text(tmp / TRANSCRIPT, excerpt[0])
                transcript = excerpt[1]
        task_part = None
        if task is not None:
            try:
                number = max(1, int(attempt or task.get("attempts") or 1))
            except (TypeError, ValueError):
                number = 1
            task_part = {"id": str(task.get("id") or ""), "title": str(task.get("title") or ""),
                         "attempt": number, "branch": str(task.get("branch") or ""),
                         "status": str(task.get("status") or "")}
        files = sorted(p.relative_to(tmp).as_posix() for p in tmp.rglob("*") if p.is_file())
        report: dict[str, Any] = {
            "evidence_version": EVIDENCE_VERSION,
            "kind": KIND,
            "created": _stamp(started),
            "environment": environment(folder=where, repo=repo, base=diff.base,
                                       head=diff.head, run=rr, task=task, trusted=trusted),
            "task": task_part,
            "run": red.obj(rr) if rr is not None else None,
            "run_note": run_note,
            "diff": {
                "base": diff.base, "head": diff.head, "files": diff.files,
                "insertions": diff.insertions, "deletions": diff.deletions,
                "untracked": diff.untracked, "patch": PATCH, "patch_chars": len(patch),
                "truncated": truncated, "note": diff.note,
                "paths": [dict(p) for p in diff.paths[:MAX_PATHS]],
                "paths_listed": min(len(diff.paths), MAX_PATHS),
            },
            "checks": records,
            "checks_skipped": skipped,
            "junit": _junit_sum(records),
            "transcript": transcript,
            "files": [REPORT, *files],
            "redactions": {},
            "timestamps": {"started": _stamp(started), "finished": _stamp(now_utc())},
        }
        report = red.obj(report)
        report["redactions"] = dict(red.counts)
        bad = schema_errors(report)
        if bad:
            raise EvidenceError(f"the bundle's report.json does not match its schema: {bad[0]}")
        _write_json(tmp / REPORT, report)
        _swap_in(tmp, target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if not _under(target, evidence_root()):
        with contextlib.suppress(OSError):
            _register(target)
    return Bundle(target, attest.evidence_hash(target), report)


def _trusted(folder: Path) -> bool:
    from evi.policy import folder_trusted

    return folder_trusted(folder)


# ---- a task's bundle ------------------------------------------------------------------


def task_worktree(root: str | os.PathLike[str], record: Mapping[str, Any]) -> Path:
    """The worktree git has the task's branch checked out in (never the path
    the tracked task file names); EvidenceError when there is none."""
    from evi import worktree as _wt
    from evi.tasks import printable

    branch = str(record.get("branch") or "").strip()
    if not branch:
        raise EvidenceError(f"task {record.get('id')} has no branch yet (start it first)")
    try:
        folder = next((e.path for e in _wt.list_worktrees(Path(root)) if e.branch == branch), None)
    except _wt.WorktreeError as exc:
        raise EvidenceError(str(exc)) from exc
    if folder is None or not folder.is_dir():
        raise EvidenceError(f"no worktree has {printable(branch)} checked out, so there is no "
                            "work to collect")
    return folder


def collect_task(
    root: str | os.PathLike[str],
    record: Mapping[str, Any],
    *,
    worktree: str | os.PathLike[str] | None = None,
    run_report: Mapping[str, Any] | None = None,
    attempt: int | None = None,
    config: Any = None,
    run_stop: str | None = None,
    cancel_token: threading.Event | None = None,
) -> Bundle:
    """Collect task `record`'s bundle for `attempt` (default: its latest) at
    `task_bundle_path`: the diff of its worktree (`worktree`, else the one
    git has its branch in) against its base, eVi's own run of its required
    checks, and the attempt's run report (default: the one `evi task start`
    wrote, `evi.tasks.report_path`) with its transcript.

    `run_stop`: the stop_reason of the task run this collects for, right
    after it (`evi.tasks.TaskRun`). A run that didn't get as far as its
    checks (any reason but "done" and "checks_failed": an error, an
    interrupt) proves nothing a check could add, so eVi runs none and the
    bundle says why. `cancel_token`: see `collect`."""
    from evi import tasks

    task_id = tasks.validate_id(str(record.get("id") or ""))
    try:
        attempt = int(attempt if attempt is not None else (record.get("attempts") or 0))
    except (TypeError, ValueError):
        attempt = 0
    if attempt < 1:
        raise EvidenceError(f"task {task_id} has not been started, so there is no work to "
                            "collect")
    folder = Path(worktree) if worktree is not None else task_worktree(root, record)
    try:
        argvs = tasks.required_argvs(record)
    except tasks.TaskError as exc:
        raise EvidenceError(str(exc)) from exc
    if run_report is None:
        path = tasks.report_path(root, task_id, attempt)
        with contextlib.suppress(OSError, ValueError):
            if path.is_file():
                run_report = json.loads(path.read_text(encoding="utf-8"))
    no_checks = ""
    if run_stop is not None and run_stop not in ("done", "checks_failed"):
        no_checks = f"the run stopped ({run_stop}) before its checks, so eVi ran none"
    return collect(
        task_bundle_path(root, task_id, attempt), folder=folder,
        base=str(record.get("base") or "") or None, run_report=run_report, checks=argvs,
        task=record, attempt=attempt, repo=root, config=config, no_checks=no_checks,
        cancel_token=cancel_token,
    )


def record_fields(bundle: Bundle) -> dict[str, Any]:
    """The task fields recording `bundle`: ``evidence`` (its path) and
    ``evidence_summary`` (`summarize`)."""
    return {"evidence": str(bundle.path), "evidence_summary": bundle.summary}


def _named(root: str | os.PathLike[str]) -> tuple[list[tuple[str, str]], set[str]]:
    """(task id, normalised path) of each bundle the repository's task files
    name as their evidence, and the ids of task files that can't be read."""
    from evi import tasks

    queue = tasks.TaskQueue(Path(root))
    records, problems = queue.all()
    found: list[tuple[str, str]] = []
    for record in records:
        with contextlib.suppress(tasks.TaskError):
            path = tasks.evidence_path(record, queue.root)
            if path:
                found.append((str(record.get("id") or ""),
                              os.path.normcase(os.path.abspath(path))))
    return found, {task_id for task_id, _ in problems}


def referenced(root: str | os.PathLike[str]) -> tuple[set[str], set[str]]:
    """The bundles the repository's task files name as their evidence
    (normalised paths), and the ids of task files that can't be read (any of
    their bundles may be named there)."""
    named, unreadable = _named(root)
    return {path for _, path in named}, unreadable


# ---- retention ---------------------------------------------------------------------


@dataclass
class PruneResult:
    removed: list[Path] = field(default_factory=list)
    kept: list[tuple[Path, str]] = field(default_factory=list)


def keep_days(config: Any) -> int:
    """``[evidence] keep_days``: a whole number of days >= 0 (at most
    ``MAX_KEEP_DAYS``, which keeps every bundle), else the default."""
    value = getattr(getattr(config, "evidence", None), "keep_days", DEFAULT_KEEP_DAYS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_KEEP_DAYS
    return min(value, MAX_KEEP_DAYS)


def _bundle_time(path: Path) -> _dt.datetime | None:
    """When a bundle was collected: its report's ``created``, else when its
    report.json was written."""
    report = path / REPORT
    try:
        created = json.loads(report.read_text(encoding="utf-8")).get("created")
        return _dt.datetime.strptime(str(created), _TIME_FORMAT).replace(tzinfo=_dt.timezone.utc)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        return _dt.datetime.fromtimestamp(report.stat().st_mtime, _dt.timezone.utc)
    except OSError:
        return None


def prune(
    root: str | os.PathLike[str],
    *,
    days: int,
    referenced: Iterable[str] = (),
    protected: Iterable[str] = (),
    dry_run: bool = False,
    now: _dt.datetime | None = None,
) -> PruneResult:
    """Remove the repository's bundles under the evidence root collected more
    than `days` days ago (``MAX_KEEP_DAYS`` or more: none), but those in
    `referenced` (normalised paths a task names) and any of the task ids in
    `protected`; and half-written folders
    older than a day. Nothing is followed through a link. `dry_run`: say
    what would go, remove nothing."""
    from evi.tasks import _inside

    result = PruneResult()
    folder = repo_folder(root)
    if _is_link(folder) or not folder.is_dir():
        return result
    now = now or now_utc()
    days = min(max(0, int(days)), MAX_KEEP_DAYS)
    cutoff = now - _dt.timedelta(days=days)
    stale = now - _dt.timedelta(hours=_LEFTOVER_HOURS)
    refs = {os.path.normcase(os.path.abspath(p)) for p in referenced if p}
    keep_ids = {str(t).casefold() for t in protected}
    for group in sorted(folder.iterdir()):
        if _is_link(group) or not group.is_dir():
            continue
        for entry in sorted(group.iterdir()):
            if _is_link(entry) or not entry.is_dir() or not _inside(str(entry), folder):
                continue
            leftover = entry.name.startswith(".") and (".tmp-" in entry.name
                                                       or ".old-" in entry.name)
            if leftover:
                try:
                    when = _dt.datetime.fromtimestamp(entry.stat().st_mtime, _dt.timezone.utc)
                except OSError:
                    continue
                if when > stale:
                    continue
            elif not is_bundle(entry):
                result.kept.append((entry, "not an evidence bundle"))
                continue
            elif os.path.normcase(os.path.abspath(entry)) in refs:
                result.kept.append((entry, "a task names it as its evidence"))
                continue
            elif group.name.casefold() in keep_ids:
                result.kept.append((entry, "its task's file can't be read"))
                continue
            else:
                when = _bundle_time(entry)
                if when is None or when > cutoff:
                    result.kept.append((entry, f"collected within {days} day(s)"))
                    continue
            if not dry_run:
                shutil.rmtree(entry, ignore_errors=True)
                if os.path.lexists(entry):
                    result.kept.append((entry, "could not be removed"))
                    continue
            result.removed.append(entry)
        if not dry_run:
            with contextlib.suppress(OSError):
                group.rmdir()  # only when emptied
    return result


# ---- commit messages ---------------------------------------------------------------


def _line(value: Any) -> str:
    """`value` on one printable line."""
    from evi.tasks import printable

    return " ".join(printable(value).split())


def _quoted(text: str) -> str:
    """`text` with each line that reads like a provenance trailer quoted."""
    return "\n".join(f"> {ln}" if _TRAILER_LIKE.match(ln) else ln
                     for ln in text.splitlines())


def _check_line(check: Mapping[str, Any]) -> str:
    if check.get("ok"):
        status = "passed"
    elif check.get("exit") is not None:
        status = f"failed (exit {check.get('exit')})"
    else:
        status = f"did not finish ({_line(check.get('note') or 'no exit code')})"
    junit = check.get("junit")
    if isinstance(junit, Mapping):
        status += (f"; junit: {junit.get('tests', 0)} tests, {junit.get('failures', 0)} "
                   f"failures, {junit.get('errors', 0)} errors, {junit.get('skipped', 0)} "
                   "skipped")
    return f"- {_line(check.get('cmd') or check.get('name') or '?')}: {status}"


def read_report(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """A bundle's report.json, or None when it can't be read as one."""
    try:
        data = json.loads((Path(path) / REPORT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def commit_body(
    root: str | os.PathLike[str], record: Mapping[str, Any], *, verifier: Any = None
) -> tuple[str, list[str], list[str]]:
    """The commit message for task `record` of the repository at `root` (see
    the module doc), the director trailers in it (`evi.attest.trailers`: []
    without a valid "verified" attestation at the branch's head) and notes
    on what it lacks and why."""
    from evi import attest, tasks

    verifier = verifier or attest.Verifier()
    notes: list[str] = []
    task_id = tasks.validate_id(str(record.get("id") or ""))
    subject = _line(record.get("title") or "") or f"Task {task_id}"
    parts = [_quoted(subject)]
    why = tasks.printable(record.get("why") or "").strip()
    if why:
        parts.append(_quoted(why))
    criteria = [_line(c) for c in record.get("acceptance") or [] if _line(c)]
    if criteria:
        parts.append(_quoted("Acceptance criteria:\n" + "\n".join(f"- {c}" for c in criteria)))
    digest = ""
    report = None
    try:
        bundle = tasks.evidence_path(record, Path(root))
    except tasks.TaskError as exc:
        bundle = ""
        notes.append(str(exc))
    if bundle:
        digest = attest.evidence_hash(bundle)
        report = read_report(bundle)
    elif not record.get("evidence"):
        notes.append(f"task {task_id} has no evidence bundle (`evi evidence collect "
                     f"{task_id}`)")
    if report is not None:
        checks = [c for c in report.get("checks") or [] if isinstance(c, Mapping)]
        if checks:
            parts.append(_quoted("Checks (eVi's own run, in the evidence bundle):\n"
                                 + "\n".join(_check_line(c) for c in checks)))
        elif report.get("checks_skipped"):
            parts.append(_quoted(f"Checks: none ran ({_line(report['checks_skipped'])})"))
        diff = report.get("diff") or {}
        if isinstance(diff, Mapping):
            lines = [f"Files changed: {diff.get('files', 0)} (+{diff.get('insertions', 0)} "
                     f"-{diff.get('deletions', 0)})"]
            for p in (diff.get("paths") or [])[:30]:
                if isinstance(p, Mapping):
                    added = "?" if p.get("added") is None else p.get("added")
                    deleted = "?" if p.get("deleted") is None else p.get("deleted")
                    lines.append(f"- {_line(p.get('path'))} (+{added} -{deleted})")
            parts.append(_quoted("\n".join(lines)))
    trailers: list[str] = []
    try:
        attempts = int(record.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if attempts > 0:
        backend = _line(record.get("backend") or "") or "?"
        model = _line(record.get("model") or "") or "?"
        trailers.append(f"Implemented-by: eVi ({backend}/{model})")
    trailers.append(f"Task: {task_id}")
    if digest:
        trailers.append(f"Evidence: {digest}")
    director: list[str] = []
    branch = str(record.get("branch") or "").strip()
    head = None
    if branch:
        try:
            head = attest.branch_head(tasks.head_ref(branch), cwd=Path(root))
        except attest.AttestError:
            notes.append(f"the task's branch {_line(branch)} names no commit here, so no "
                         "director trailers")
    if head:
        director = verifier.trailers(record, evidence_hash=digest, head=head)
        if not director:
            notes.append(f"no valid director attestation for task {task_id} at "
                         f"{head[:12]}, so no Verified-by trailer")
    parts.append("\n".join([*trailers, *director]))
    return "\n\n".join(parts) + "\n", director, notes
