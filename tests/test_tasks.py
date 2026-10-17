"""The task queue (Phase 1, loop-task-queue): evi/tasks.py.

A task is ``<repo>/.evi/tasks/<id>.toml`` in the main checkout, tracked in
git. These tests cover the pure state machine (every legal step, and an
IllegalTransition for every other one), leases (a live one refuses another
holder, an expired or unreadable one is reclaimable, --force takes over),
the TOML round trip (a task reads back exactly as written, or isn't
written), atomic writes under a lock (a failed write leaves the old file and
no stray file and is a TaskError; a reader holding the file on Windows only
delays a write; a second process can't write while the lock is held; of two
concurrent claims one wins), task ids, finding the main checkout from inside
a linked worktree, the queue's refusal to write a verdict nothing signed or
to change a verified task but by reopening it, a run summary that is
printable and writable whatever the model said, an evidence path from a task
file read only when it is a bundle eVi wrote under its evidence folder (never
the repository, a UNC path or through a link), task branches read as
refs/heads/..., and the
repository's .gitignore keeping .evi/ ignored but for the shared files at
the root (`git check-ignore`).

Every queue lives in a git repository under tmp_path; eVi's home is the
throwaway one tests/conftest.py gives every test process.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest

from evi import attest, tasks
from evi.tasks import IllegalTransition, LeaseHeld, TaskError, TaskQueue

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    return root.resolve()


def _task(task_id: str = "T1", **kw) -> dict:
    kw.setdefault("title", "Fix the thing")
    return tasks.new_task(task_id, now=NOW, **kw)


def _claimed(record=None, holder: str = "alice", now=NOW, ttl: int = 60) -> dict:
    return tasks.claim(record or _task(), holder=holder, now=now, ttl_minutes=ttl)


def _done(record: dict, holder: str = "alice", ok: bool = True) -> dict:
    return tasks.finish(record, holder=holder, implemented=ok,
                        stop_reason="done" if ok else "checks_failed", exit_code=0 if ok else 5,
                        report="r.json", summary="s")


def _signed(record: dict, verdict: str = "verified") -> dict:
    """What `evi.attest.sign` returns, without the key: the status set and an
    attestation table added (the queue checks neither signature nor key)."""
    body = tasks.for_verdict(record, verdict=verdict)
    return {**body, "status": verdict, "attestation": {"v": 1, "verdict": verdict}}


# ---- records and the state machine ---------------------------------------------


def test_a_new_task_has_every_field_and_starts_open():
    record = _task(why="because", acceptance=["fails before", "passes after"],
                   files_in_scope=["evi/x.py", ".github/workflows/ci.yml"],
                   required_checks=["python -m pytest -q tests/test_x.py", ["ruff", "check"]],
                   implementer="evi-local")
    assert list(record)[:3] == ["id", "title", "status"]
    assert set(record) == set(tasks.FIELDS) - {"last_result", "attestation", "evidence_summary"}
    assert record["status"] == "open" and record["attempts"] == 0 and record["lease"] == {}
    assert record["created"] == "2026-10-17T12:00:00Z"
    assert record["required_checks"][1] == ["ruff", "check"]
    assert tasks.required_argvs(record) == [
        ("python", "-m", "pytest", "-q", "tests/test_x.py"), ("ruff", "check")]


@pytest.mark.parametrize("kw,match", [
    ({"title": "  "}, "needs a title"),
    ({"files_in_scope": ["/etc/passwd"]}, "files_in_scope"),
    ({"files_in_scope": ["../elsewhere/x.py"]}, "files_in_scope"),
    ({"required_checks": ["pytest 'unclosed"]}, "doesn't parse"),
    ({"required_checks": [""]}, "empty"),
    ({"required_checks": "pytest"}, "required_checks must be a list"),  # not p, y, t ...
    ({"acceptance": "one string"}, "list of strings"),
])
def test_a_bad_task_is_refused(kw, match):
    with pytest.raises(TaskError, match=match):
        _task(**kw)


def test_the_whole_lifecycle_through_every_legal_step():
    record = _claimed()
    assert record["status"] == "claimed" and record["lease"] == {
        "holder": "alice", "expires_at": "2026-10-17T13:00:00Z"}
    record = _done(record)
    assert record["status"] == "implemented" and record["attempts"] == 1 and record["lease"] == {}
    assert record["last_result"] == {"stop_reason": "done", "exit_code": 0, "report": "r.json",
                                     "summary": "s"}
    record = tasks.submit(record)
    assert record["status"] == "needs_review"
    body = tasks.for_verdict(record, verdict="rejected", note="the test is missing", now=NOW)
    assert body["status"] == "needs_review"  # attest.sign sets the verdict
    assert body["reviewer_notes"] == [{"at": "2026-10-17T12:00:00Z", "verdict": "rejected",
                                       "note": "the test is missing"}]
    signed = _signed(record, "rejected")
    reopened = tasks.reopen(signed, note="rework", now=NOW)
    assert reopened["status"] == "open" and "attestation" not in reopened
    assert reopened["reviewer_notes"][-1]["verdict"] == "reopened"
    # ... and round again: a failed start goes back to open, attempt counted.
    again = _done(_claimed(reopened), ok=False)
    assert again["status"] == "open" and again["attempts"] == 2
    assert again["last_result"]["stop_reason"] == "checks_failed"


def test_release_puts_a_claim_back():
    record = tasks.release(_claimed(), holder="alice", now=NOW)
    assert record["status"] == "open" and record["lease"] == {}


@pytest.mark.parametrize("status,step", [
    ("open", "submit"), ("open", "release"), ("open", "reopen"), ("open", "verdict"),
    ("claimed", "submit"), ("claimed", "reopen"), ("claimed", "verdict"),
    ("implemented", "claim"), ("implemented", "release"), ("implemented", "verdict"),
    ("needs_review", "claim"), ("needs_review", "submit"), ("needs_review", "finish"),
    ("verified", "claim"), ("verified", "submit"), ("verified", "verdict"),
    ("verified", "finish"), ("rejected", "claim"), ("rejected", "release"),
    ("rejected", "verdict"), ("Verified", "claim"), ("garbage", "claim"),
])
def test_illegal_transitions_raise_a_clear_error(status, step):
    record = {**_task(), "status": status}
    if status == "claimed":
        record["lease"] = {"holder": "alice", "expires_at": "2026-10-17T13:00:00Z"}
    steps = {
        "claim": lambda r: tasks.claim(r, holder="alice", now=NOW),
        "release": lambda r: tasks.release(r, holder="alice", now=NOW),
        "submit": tasks.submit,
        "reopen": lambda r: tasks.reopen(r, now=NOW),
        "verdict": lambda r: tasks.for_verdict(r, verdict="verified"),
        "finish": lambda r: _done(r),
    }
    with pytest.raises(TaskError) as info:
        steps[step](record)
    assert "T1" in str(info.value) and (status in str(info.value) or "claimed" in str(info.value))


def test_reopen_takes_a_verdict_that_only_looks_like_one():
    # A hand-edited "verifed" (or "unverified") reads as a verdict: reopen
    # is the way out, and it drops whatever attestation is there.
    for status in ("verifed", "unverified", "REJECTED"):
        record = {**_task(), "status": status, "attestation": {"v": 1}}
        out = tasks.reopen(record, now=NOW)
        assert out["status"] == "open" and "attestation" not in out


# ---- leases --------------------------------------------------------------------------


def test_a_live_lease_refuses_another_holder_unless_forced():
    record = _claimed(holder="alice")
    with pytest.raises(LeaseHeld, match="alice"):
        tasks.claim(record, holder="bob", now=NOW + dt.timedelta(minutes=30))
    with pytest.raises(LeaseHeld, match="--force"):
        tasks.release(record, holder="bob", now=NOW)
    taken = tasks.claim(record, holder="bob", now=NOW, force=True)
    assert taken["lease"]["holder"] == "bob"
    assert tasks.release(record, holder="bob", now=NOW, force=True)["status"] == "open"
    # the holder itself renews
    renewed = tasks.claim(record, holder="alice", now=NOW + dt.timedelta(minutes=30),
                          ttl_minutes=60)
    assert renewed["lease"]["expires_at"] == "2026-10-17T13:30:00Z"


def test_an_expired_lease_can_be_reclaimed():
    record = _claimed(holder="alice", ttl=60)
    later = NOW + dt.timedelta(minutes=61)
    assert tasks.lease_live(record, NOW) and not tasks.lease_live(record, later)
    reclaimed = tasks.claim(record, holder="bob", now=later)
    assert reclaimed["status"] == "claimed" and reclaimed["lease"]["holder"] == "bob"
    assert tasks.release(record, holder="bob", now=later)["status"] == "open"
    # The old holder's run can't record its outcome over the new claim.
    with pytest.raises(LeaseHeld, match="bob"):
        _done(reclaimed, holder="alice")


def test_an_unreadable_expiry_counts_as_expired():
    record = {**_claimed(holder="alice"), "lease": {"holder": "alice", "expires_at": "soon"}}
    assert not tasks.lease_live(record, NOW)
    assert tasks.claim(record, holder="bob", now=NOW)["lease"]["holder"] == "bob"


def test_finish_needs_the_claim_it_ran_under():
    with pytest.raises(IllegalTransition):
        _done(_task())
    with pytest.raises(LeaseHeld):
        _done(_claimed(holder="alice"), holder="bob")


# ---- ids -----------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", ["T1", "fix-login_2", "a" * 64, "T0042"])
def test_valid_ids(task_id):
    assert tasks.validate_id(task_id) == task_id


@pytest.mark.parametrize("task_id", ["", "a" * 65, "has space", "a.b", "../x", "x/y", "x\\y",
                                     "CON", "nul", "Com1", "lpt9", "T1\n", None])
def test_invalid_ids_are_refused(task_id):
    with pytest.raises(TaskError):
        tasks.validate_id(task_id)
    if task_id:
        with pytest.raises(TaskError):
            _task(task_id)


def test_ids_are_the_next_free_number_and_unique_whatever_the_case(repo):
    q = TaskQueue(repo)
    assert [q.create(_task("")).get("id") for _ in range(3)] == ["T1", "T2", "T3"]
    q.create(_task("T10"))
    q.create(_task("login"))
    assert q.next_id() == "T11"
    assert q.ids() == ["T1", "T2", "T3", "T10", "login"]
    for taken in ("t2", "LOGIN", "T1"):
        with pytest.raises(TaskError, match="already exists"):
            q.create(_task(taken))
    assert q.read("Login")["id"] == "login"  # found whatever the case
    with pytest.raises(tasks.TaskNotFound):
        q.read("T99")


# ---- the TOML round trip and atomic writes -----------------------------------------


def test_a_task_reads_back_exactly_as_written(repo):
    q = TaskQueue(repo)
    record = _task(why="line one\nline \"two\"\tand a tab — and ünïcode",
                   acceptance=["a = b", "[not a table]"], required_checks=[["py", "-c", "x"]])
    record = tasks.reopen(tasks.submit(_done(_claimed(record))), note="once more", now=NOW)
    record["custom"] = {"nested": {"deep": [1, 2]}, "flag": True}
    written = q.create(record)
    assert written == record
    text = q.path("T1").read_text(encoding="utf-8")
    assert tomllib.loads(text) == record and "\r" not in text
    assert text.startswith('id = "T1"\ntitle = ')
    # The digest attest signs is over parsed values: unchanged by the trip.
    assert attest.task_digest(q.read("T1")) == attest.task_digest(record)


def test_a_value_toml_cannot_hold_is_never_written(repo):
    q = TaskQueue(repo)
    with pytest.raises(TaskError, match="TOML"):
        q.create({**_task(), "model": None})
    assert not q.path("T1").exists()


def test_a_failed_write_leaves_the_old_task_and_no_stray_file(repo, monkeypatch):
    q = TaskQueue(repo)
    q.create(_task())
    before = q.path("T1").read_bytes()

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(tasks.os, "replace", broken_replace)
    with pytest.raises(TaskError, match="can't write .*disk full"):  # not a raw OSError
        q.update("T1", lambda r: tasks.claim(r, holder="alice", now=NOW))
    monkeypatch.undo()
    assert q.path("T1").read_bytes() == before
    assert sorted(p.name for p in (repo / ".evi").iterdir()) == ["tasks", "tasks.lock"]
    assert [p.name for p in q.folder.iterdir()] == ["T1.toml"]


def test_a_reader_holding_the_task_file_only_delays_a_write(repo, monkeypatch):
    """On Windows a process reading the task file (`evi task list`, a status
    poll) makes os.replace onto it fail with PermissionError until it is
    done: the write tries again, and a lasting one is a TaskError (the CLI
    exits 2), the old file left as it was."""
    q = TaskQueue(repo)
    q.create(_task())
    real = os.replace
    refusals = [2]

    def busy_replace(src, dst):
        if refusals[0] > 0:
            refusals[0] -= 1
            raise PermissionError(13, "Access is denied", str(dst))
        return real(src, dst)

    monkeypatch.setattr(tasks.os, "replace", busy_replace)
    assert q.update("T1", lambda r: tasks.claim(r, holder="alice", now=NOW))["status"] == "claimed"
    assert refusals == [0] and q.read("T1")["lease"]["holder"] == "alice"
    claimed = q.path("T1").read_bytes()
    # One that never lets go.
    monkeypatch.setattr(tasks, "_SHARING_DELAYS", (0, 0))
    refusals[0] = 10**6
    with pytest.raises(TaskError, match="can't write"):
        q.update("T1", lambda r: tasks.release(r, holder="alice", now=NOW))
    monkeypatch.undo()
    assert q.path("T1").read_bytes() == claimed
    assert [p.name for p in q.folder.iterdir()] == ["T1.toml"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing")
def test_a_real_reader_on_windows_only_delays_a_write(repo, monkeypatch):
    q = TaskQueue(repo)
    q.create(_task())
    reader = open(q.path("T1"), "rb")  # noqa: SIM115 — closed by the timer
    timer = threading.Timer(0.15, reader.close)
    timer.start()
    try:
        record = q.update("T1", lambda r: tasks.claim(r, holder="alice", now=NOW))
    finally:
        timer.join()
        reader.close()
    assert record["status"] == "claimed" and q.read("T1")["status"] == "claimed"
    monkeypatch.setattr(tasks, "_SHARING_DELAYS", (0.01, 0.01))
    with open(q.path("T1"), "rb"):
        with pytest.raises(TaskError, match="can't write"):
            q.update("T1", lambda r: tasks.release(r, holder="alice", now=NOW))
    assert q.read("T1")["status"] == "claimed"


def test_a_summary_is_one_printable_line_that_can_be_written(repo):
    """The model's answer may hold terminal escapes or a lone surrogate (a
    broken emoji): its first line goes in last_result.summary without the
    control characters and with the surrogate escaped, so `evi task show`
    can't repaint the terminal and the write can't fail. A record that
    can't go in a UTF-8 file is refused before anything is written."""
    from types import SimpleNamespace

    answer = "Done \ud83d \x1b[1A\x1b[2K\rstatus: verified\x9b\x07\nsecond line"
    summary = tasks._summary(SimpleNamespace(text=answer, error=None), "done", [])
    assert summary == "Done \\ud83d [1A[2Kstatus: verified"
    assert tasks._summary(SimpleNamespace(text="", error="\x1b[31mboom"), "error", []) == "[31mboom"
    q = TaskQueue(repo)
    q.create(_task())
    record = q.update("T1", lambda r: tasks.finish(
        tasks.claim(r, holder="a", now=NOW), holder="a", implemented=True, stop_reason="done",
        exit_code=0, summary=summary))
    assert record["last_result"]["summary"] == summary
    assert q.read("T1")["last_result"]["summary"] == summary
    with pytest.raises(TaskError, match="surrogates"):
        tasks.dumps({**_task(), "why": "broken \ud83d"})
    with pytest.raises(TaskError, match="surrogates"):
        q.update("T1", lambda r: {**r, "why": "broken \ud83d"})
    assert q.read("T1") == record
    assert tasks.printable("a\x1b[1A\tb\r\nc\x9bd\x7f") == "a[1A\tb\ncd"


def test_a_file_that_is_not_a_task_is_reported_not_read(repo):
    q = TaskQueue(repo)
    q.create(_task())
    q.folder.joinpath("T2.toml").write_text('id = "T3"\nstatus = "open"\n', encoding="utf-8")
    q.folder.joinpath("T4.toml").write_text("this is [not toml", encoding="utf-8")
    found, problems = q.all()
    assert [r["id"] for r in found] == ["T1"]
    assert [p[0] for p in problems] == ["T2", "T4"]
    with pytest.raises(TaskError, match="names task"):
        q.read("T2")


# ---- the lock ------------------------------------------------------------------------


def test_another_process_cannot_write_while_the_lock_is_held(repo):
    q = TaskQueue(repo)
    q.create(_task())
    code = (
        "import sys\n"
        "from evi import tasks\n"
        "q = tasks.TaskQueue(sys.argv[1])\n"
        "try:\n"
        "    with q.locked(timeout=0.5):\n"
        "        print('GOT THE LOCK')\n"
        "except tasks.TaskError as exc:\n"
        "    print('refused:', exc)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"}

    def child() -> str:
        out = subprocess.run([sys.executable, "-c", code, str(repo)], env=env, cwd=str(repo),
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        return out.stdout

    with q.locked():
        assert "refused: another eVi is changing the task queue" in child()
    assert "GOT THE LOCK" in child()


def test_of_two_concurrent_claims_one_wins(repo):
    q = TaskQueue(repo)
    q.create(_task())
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def claim(holder: str) -> None:
        barrier.wait()
        try:
            q.update("T1", lambda r: tasks.claim(r, holder=holder))
            results[holder] = "won"
        except LeaseHeld:
            results[holder] = "refused"

    threads = [threading.Thread(target=claim, args=(h,)) for h in ("alice", "bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(results.values()) == ["refused", "won"]
    winner = next(h for h, r in results.items() if r == "won")
    assert q.read("T1")["lease"]["holder"] == winner


# ---- what the queue never writes ---------------------------------------------------


def test_a_verdict_nothing_signed_is_never_written(repo):
    q = TaskQueue(repo)
    with pytest.raises(TaskError, match="only the director"):
        q.create({**_task(), "status": "verified"})
    q.create(_task())
    with pytest.raises(TaskError, match="only the director"):
        q.update("T1", lambda r: {**r, "status": "Verified"})
    assert q.read("T1")["status"] == "open"


def test_a_decided_task_changes_only_by_reopening(repo):
    q = TaskQueue(repo)
    record = tasks.submit(_done(_claimed()))
    q.create(record)
    q.update("T1", lambda r: _signed(r))  # what `evi task verify` writes
    assert q.read("T1")["status"] == "verified"
    for change in (lambda r: {**r, "title": "edited"},
                   lambda r: {**r, "attestation": {"v": 1, "verdict": "verified", "x": 1}},
                   lambda r: _signed({**r, "status": "needs_review"}, "rejected")):
        with pytest.raises(TaskError, match="reopen"):
            q.update("T1", change)
    reopened = q.update("T1", lambda r: tasks.reopen(r, now=NOW))
    assert reopened["status"] == "open" and "attestation" not in reopened


def test_replace_refuses_a_task_that_changed_meanwhile(repo):
    q = TaskQueue(repo)
    q.create(tasks.submit(_done(_claimed())))
    seen = q.read("T1")
    q.update("T1", lambda r: tasks.reopen(r, now=NOW))  # someone reopened it meanwhile
    with pytest.raises(TaskError, match="changed meanwhile"):
        q.replace(seen, _signed(seen))
    assert q.read("T1")["status"] == "open"


# ---- where the queue lives -----------------------------------------------------------


def test_the_queue_is_the_main_checkouts_also_from_a_linked_worktree(repo, tmp_path):
    main = TaskQueue.at(repo)
    main.create(_task())
    linked = tmp_path / "elsewhere" / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    (linked / "deep").mkdir()
    for start in (linked, linked / "deep"):
        q = TaskQueue.at(start)
        assert q.root == repo and q.folder == repo / ".evi" / "tasks"
        assert q.read("T1")["title"] == "Fix the thing"
    assert not (linked / ".evi").exists()


def test_outside_git_there_is_no_queue(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    with pytest.raises(TaskError, match="git repository"):
        TaskQueue.at(folder)


@needs_git
def test_gitignore_keeps_evi_ignored_but_the_shared_files(tmp_path):
    """The repository's own .gitignore: the task queue, the bug ledger and
    the review guidance are tracked; every other file under .evi/ (the
    queue's lock, a stray dump) stays ignored."""
    r = tmp_path / "ignores"
    r.mkdir()
    _git(r, "init", "-q")
    shutil.copyfile(ROOT / ".gitignore", r / ".gitignore")

    def ignored(path: str) -> bool:
        proc = subprocess.run(["git", "check-ignore", "-q", "--no-index", "--", path], cwd=r,
                              capture_output=True, timeout=60)
        assert proc.returncode in (0, 1), proc.stderr
        return proc.returncode == 0

    for kept in (".evi/tasks/T1.toml", ".evi/tasks/fix-login.toml", ".evi/bug-ledger.jsonl",
                 ".evi/REVIEW.md", ".evi/review-rules.md"):
        assert not ignored(kept), kept
    # An .evi/ below the root (a session started in a subfolder writes its
    # bug ledger and anatomy there) stays ignored whole, shared names too.
    nested = ("sub/.evi/sessions.json", "sub/.evi/tasks/T9.toml", "a/b/c/.evi/anatomy.md",
              "evi/apps/web/.evi/bug-ledger.jsonl", "tests/fixtures/.evi/review-rules.md")
    for dropped in (".evi/tasks.lock", ".evi/.task-T1.abcd.tmp", ".evi/checkpoints/x.json",
                    ".evi/anything.json", ".evi/BUGBOT.md", *nested):
        assert ignored(dropped), dropped
    # and git status shows exactly the shared files
    for rel in (".evi/tasks/T1.toml", ".evi/bug-ledger.jsonl", ".evi/tasks.lock",
                ".evi/other/state.json", "sub/keep.txt", *nested):
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text("x\n", encoding="utf-8")
    status = _git(r, "status", "--porcelain", "--untracked-files=all")
    listed = sorted(line[3:] for line in status.splitlines())
    assert listed == [".evi/bug-ledger.jsonl", ".evi/tasks/T1.toml", ".gitignore",
                      "sub/keep.txt"]


# ---- the prompt and the report ---------------------------------------------------------


def test_the_prompt_carries_the_task_and_the_contract():
    record = _task(why="users can't log in", acceptance=["a test reproduces it"],
                   files_in_scope=["evi/auth.py"], required_checks=["python -m pytest -q"])
    prompt = tasks.build_prompt(record, "/w/.worktrees/task__T1")
    for bit in ("task T1", "Fix the thing", "users can't log in", "1. a test reproduces it",
                "- evi/auth.py", "python -m pytest -q", "EVI.md", "/w/.worktrees/task__T1",
                "Never commit"):
        assert bit in prompt, bit


def test_a_task_branch_is_read_as_a_branch_never_as_a_tag():
    assert tasks.head_ref("task/T1") == "refs/heads/task/T1"
    assert tasks.head_ref(tasks.branch_for("fix-2")) == "refs/heads/task/fix-2"


def test_an_evidence_path_from_a_task_file_is_never_taken_as_it_is(repo, tmp_path):
    """The task file is tracked, so its evidence path may come from anyone:
    it is read only when it is an evidence bundle eVi wrote (a folder with
    report.json under the repository's folder of <eVi home>/evidence/, or
    exactly one `evi run --evidence` recorded), through no link, and never
    as a UNC or device path (stat'ing \\\\host\\share makes Windows log on to
    that host). Never ".", the repository, its .git or a folder elsewhere:
    a forged task file can't make `evi task list` hash the repository."""
    from evi import evidence

    record = _task()

    def path(value: str) -> str:
        return tasks.evidence_path({**record, "evidence": value}, repo)

    def bundle(at: Path) -> Path:
        at.mkdir(parents=True, exist_ok=True)
        (at / "report.json").write_text("{}\n", encoding="utf-8")
        return at

    assert path("") == path("  ") == ""
    mine = bundle(evidence.task_bundle_path(repo, "T1", 1))
    assert Path(path(str(mine))) == mine
    run = bundle(evidence.run_bundle_path(repo))  # the repository's plain-run bundles too
    assert Path(path(str(run))) == run
    assert tasks.report_path(repo, "T1", 3).parent.parent == tasks.task_home(repo, "T1")
    outside = bundle(tmp_path / "outside")  # shaped like a bundle, but nobody recorded it
    bundle(repo / "bundle")
    other_repo = bundle(evidence.task_bundle_path(tmp_path / "other", "T1", 1))
    not_a_bundle = evidence.task_bundle_path(repo, "T1", 2)
    not_a_bundle.mkdir(parents=True)
    for bad in ("//attacker.invalid/share/bundle", r"\\attacker.invalid\share\bundle",
                r"\\?\C:\bundle", r"\\.\pipe\bundle", "/", ".", str(repo), ".git", "bundle",
                str(repo / "bundle"), str(outside), str(repo.parent), "../outside", "CON",
                "sub/nul.txt", "a\0b", str(other_repo), str(not_a_bundle),
                str(evidence.repo_folder(repo)), str(evidence.evidence_root()),
                str(mine / "report.json"), str(tasks.task_home(repo, "T1"))):
        with pytest.raises(TaskError, match="not read"):
            path(bad)
    # A folder `evi run --evidence` wrote (recorded in the registry) is read
    # as it is, nothing below it.
    evidence._register(outside)
    assert Path(path(str(outside))) == outside
    bundle(outside / "inner")
    with pytest.raises(TaskError, match="not read"):
        path(str(outside / "inner"))
    # Nor through a link (or a junction) from the evidence folder elsewhere,
    # nor a bundle that is itself one.
    elsewhere = tmp_path / "elsewhere"
    bundle(elsewhere / "1")
    link = evidence.repo_folder(repo) / "T3"
    bundle_link = evidence.repo_folder(repo) / "T4"
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(elsewhere), str(link))
            _winapi.CreateJunction(str(elsewhere / "1"), str(bundle_link))
        else:
            link.symlink_to(elsewhere, target_is_directory=True)
            bundle_link.symlink_to(elsewhere / "1", target_is_directory=True)
    except OSError:  # no junctions or links here: the rest stands
        return
    for bad in (str(link / "1"), str(bundle_link)):
        with pytest.raises(TaskError, match="not read"):
            path(bad)


def test_reports_go_under_evi_home_per_repository_task_and_attempt(repo, tmp_path):
    from evi import config

    first = tasks.report_path(repo, "T1", 1)
    assert first.parent.parent.parent.parent == Path(config.HOME) / "tasks"
    assert first.parts[-3:] == ("T1", "1", "report.json")
    assert tasks.report_path(repo, "T1", 2).parent.name == "2"
    assert tasks.report_path(tmp_path / "other", "T1", 1) != first
