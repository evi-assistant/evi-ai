"""One eval engine everywhere; tag filters can't excuse a baseline case
(polish-evals-engine).

The web Evals panel's Run button (POST /api/evals/run) and scheduled eval
tasks (evi/scheduler.py) ran the old text-only engine with every tool
approved in the process's folder: they gave other verdicts than `evi eval
run` and failed behavioral cases outright. Both now run `evals.run_suite` on
the configured [llm] backend and model (`evals.configured_context`) and keep
the report as the CLI does (`evals.write_report`). Here the configured
model is a scripted client (`evi.evals_fake.ScriptedClient`, injected as
[llm]'s client) that replays, for each run, the `fake` script of the case
whose prompt it was sent.

Acceptance criterion 1 (the web route):
test_the_web_run_gives_the_clis_verdicts_and_keeps_the_report (behavioral
assertions, a fixture workspace, a command the implementer policy refuses,
a worktree of a trusted repository; the same rows as `evi eval run`),
test_a_repo_case_of_an_untrusted_repository_is_skipped_on_the_web,
test_the_web_never_offers_the_fake_backend,
test_a_cli_agent_backend_is_refused_on_the_web,
test_the_web_run_does_not_block_the_event_loop,
test_the_run_button_names_a_suite_by_its_file and
test_a_report_that_cannot_be_kept_is_a_clear_500.

Acceptance criterion 2 (scheduled eval tasks):
test_a_scheduled_eval_runs_the_cli_engine_and_keeps_the_report,
test_a_green_scheduled_eval_is_ok,
test_a_scheduled_eval_in_which_nothing_ran_fails,
test_a_scheduled_eval_that_skipped_a_case_is_not_ok and
test_a_scheduled_eval_counts_an_expected_failure (tests/test_loop_state.py:
the web and scheduled runs skip user hooks).

Acceptance criterion 3: test_the_text_only_engine_is_gone.

Acceptance criterion 4 (--partial, and no tag filter on baseline record):
test_a_tag_filter_cannot_excuse_a_failing_baselined_case,
test_partial_needs_a_baseline, test_baseline_record_refuses_tag_filters and
test_baseline_record_refuses_one_file_of_self_without_out.

eVi's home, config, trust list and reports live under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from evi import evals, trust
from evi.evals_fake import ScriptedClient

pytest.importorskip("fastapi")

HW = {"gpu": "none", "vram_gb": None, "cpu": "test", "ram_gb": None, "os": "test"}
CONFIG = ('[llm]\nbackend = "ollama"\nmodel = "tiny:1b"\n'
          "[tools]\nmemory = false\nskills = false\ntranscripts = false\n")

UNIFY = r'''
name = "unify"

[[case]]
name = "answer"
prompt = "say ok"
contains = ["ok"]
fake = [{ text = "ok" }]

[[case]]
name = "fix-add"
prompt = "add() in calc.py subtracts. Fix it."
files = { "calc.py" = "def add(a, b):\n    return a - b\n" }
expect_tools = ["write_file"]
file_contains = [{ path = "calc.py", contains = "a + b" }]
diff_touches_only = ["calc.py"]
fake = [
  { tool = "read_file", args = { path = "calc.py" } },
  { tool = "write_file", args = { path = "calc.py", content = "def add(a, b):\n    return a + b\n" } },
  { text = "Fixed add()." },
]

[[case]]
name = "refused-command"
prompt = "leave the folder as it is"
command_succeeds = ["curl https://example.com"]
fake = [{ text = "left it" }]

[[case]]
name = "repo"
prompt = "look at the repository"
workspace = "repo"
file_contains = [{ path = "README.md", contains = "hello" }]
fake = [{ text = "looked" }]
'''


@pytest.fixture
def home(monkeypatch, tmp_path):
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    # `evi eval run` marks its process EVI_AGENT=1; undone after the test.
    monkeypatch.setenv("EVI_AGENT", "0")
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr("evi.backends.registry.BACKENDS_PATH", evi_home / "backends.json")
    # nvidia-smi and friends are not what these tests are about.
    monkeypatch.setattr(evals, "hardware_summary", lambda **kw: dict(HW))
    (evi_home / "config.toml").write_text(CONFIG, encoding="utf-8")
    (evi_home / "evals").mkdir()
    return evi_home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


def _repo(tmp_path: Path, *, trusted: bool, name: str = "checkout") -> Path:
    """A git repository with one commit (README.md says hello)."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "-c",
         "commit.gpgsign=false", "commit", "-q", "-m", "init")
    if trusted:
        trust.grant(repo)
    return repo.resolve()


def _suite(home: Path, text: str = UNIFY, name: str = "unify") -> None:
    (home / "evals" / f"{name}.toml").write_text(text, encoding="utf-8")


class _Routed:
    """[llm]'s client for one agent: replays the `fake` script of the case
    whose prompt its first request carries (`scripts`: prompt -> turns)."""

    def __init__(self, scripts: dict[str, list]) -> None:
        self.scripts = scripts
        self.inner: ScriptedClient | None = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @property
    def exhausted(self) -> bool:
        return bool(self.inner is not None and self.inner.exhausted)

    @property
    def scripted(self) -> int:
        return self.inner.scripted if self.inner is not None else 0

    def _create(self, **kw):
        if self.inner is None:
            said = " ".join(str(m.get("content") or "") for m in kw.get("messages") or []
                            if isinstance(m, dict) and m.get("role") == "user")
            self.inner = ScriptedClient(next(
                (turns for prompt, turns in self.scripts.items() if prompt in said), []))
        return self.inner.chat.completions.create(**kw)


def _scripted_model(monkeypatch, text: str = UNIFY) -> list:
    """Stand a scripted model in for [llm] (`evi.llm.client.make_client`):
    every agent built gets a `_Routed` client over the suite's scripts.
    Returns the clients made."""
    scripts = {c["prompt"]: c.get("fake") or [] for c in tomllib.loads(text)["case"]}
    made: list = []

    def make_client(settings):
        made.append(_Routed(scripts))
        return made[-1]

    monkeypatch.setattr("evi.llm.client.make_client", make_client)
    return made


def _app(monkeypatch, home: Path, tmp_path: Path):
    import evi.apps.web.server as server_mod

    monkeypatch.setattr(server_mod, "HOME", home)
    monkeypatch.setattr(server_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    return server_mod.create_app()


def _web(monkeypatch, home: Path, tmp_path: Path):
    from fastapi.testclient import TestClient

    return TestClient(_app(monkeypatch, home, tmp_path))


def _cli(*args: str):
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    return CliRunner().invoke(cli_main.app, list(args))


def _kept(home: Path, suite: str = "unify") -> list[Path]:
    return sorted((home / "evals" / "reports" / suite).glob("*.json"))


def _verdicts(report: dict) -> list[tuple]:
    return [(r["name"], r["passed"], r["skipped"], r["skip_reason"], r["runs"], r["passes"],
             r["failures"]) for r in report["cases"]]


# ---- the web Run button (POST /api/evals/run) ------------------------------------------


def test_the_web_run_gives_the_clis_verdicts_and_keeps_the_report(home, tmp_path,
                                                                   monkeypatch):
    repo = _repo(tmp_path, trusted=True)
    monkeypatch.chdir(repo)
    _suite(home)
    _scripted_model(monkeypatch)
    r = _web(monkeypatch, home, tmp_path).post("/api/evals/run", json={"name": "unify"})
    assert r.status_code == 200, r.text
    report = r.json()
    assert evals.schema_errors(report, evals.REPORT_SCHEMA) == []
    # The configured [llm] backend and model, never the fake one.
    assert (report["backend"], report["model"]) == ("ollama", "tiny:1b")
    by = {row["name"]: row for row in report["cases"]}
    assert by["answer"]["passed"]
    # Behavioral assertions on a fixture workspace: the model's edit, the
    # tools it called and the files it changed.
    assert by["fix-add"]["passed"], by["fix-add"]["failures"]
    # The same implementer policy as `evi eval run`: refused, never run.
    assert not by["refused-command"]["passed"]
    assert "refused by the implementer policy" in by["refused-command"]["failures"][0]
    # A throwaway worktree of the trusted repository the server runs in.
    assert by["repo"]["passed"] and not by["repo"]["skipped"], by["repo"]["failures"]
    assert (report["ran"], report["passed"], report["failed"]) == (4, 3, 1)
    # Nothing landed in the server's folder; the repository is as it was.
    assert not (repo / "calc.py").exists()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                          text=True, timeout=60).stdout == ""
    # Kept as the CLI keeps it.
    [kept] = _kept(home)
    assert json.loads(kept.read_text(encoding="utf-8")) == report
    assert kept.with_suffix(".html").is_file()
    # `evi eval run` on the same suite and model gives the same verdicts.
    res = _cli("eval", "run", "unify", "--format", "json")
    assert res.exit_code == 1, res.output  # refused-command fails
    assert _verdicts(json.loads(res.stdout)) == _verdicts(report)
    assert len(_kept(home)) == 2


def test_a_repo_case_of_an_untrusted_repository_is_skipped_on_the_web(home, tmp_path,
                                                                      monkeypatch):
    repo = _repo(tmp_path, trusted=False)
    monkeypatch.chdir(repo)
    _suite(home)
    made = _scripted_model(monkeypatch)
    r = _web(monkeypatch, home, tmp_path).post("/api/evals/run", json={"name": "unify"})
    assert r.status_code == 200, r.text
    row = {row["name"]: row for row in r.json()["cases"]}["repo"]
    assert row["skipped"] and "not a trusted folder" in row["skip_reason"]
    # The skipped case built no agent: three runs, three clients.
    assert len(made) == 3
    assert subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True,
                          text=True, timeout=60).stdout.count("\n") == 1


def test_the_web_never_offers_the_fake_backend(home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _suite(home)
    made = _scripted_model(monkeypatch)
    http = _web(monkeypatch, home, tmp_path)
    r = http.post("/api/evals/run", json={"name": "unify", "backend": "fake"})
    assert r.status_code == 400 and "configured [llm] backend" in r.json()["detail"]
    r = http.post("/api/evals/run", json={"name": "self"})
    assert r.status_code == 400 and "evi eval run self" in r.json()["detail"]
    # [llm] backend = "fake" is no way in either: it would replay scripts.
    (home / "config.toml").write_text(CONFIG.replace('"ollama"', '"fake"'), encoding="utf-8")
    r = http.post("/api/evals/run", json={"name": "unify"})
    assert r.status_code == 409 and "not a backend" in r.json()["detail"]
    assert made == [] and not (home / "evals" / "reports").exists()


def test_a_cli_agent_backend_is_refused_on_the_web(home, tmp_path, monkeypatch):
    """A delegate CLI agent runs its own tools, outside the implementer
    policy: refused before any case runs (409), as `evi eval run` refuses it
    (exit 2)."""
    monkeypatch.chdir(tmp_path)
    _suite(home)
    made = _scripted_model(monkeypatch)
    (home / "config.toml").write_text(CONFIG.replace('"ollama"', '"codex"'), encoding="utf-8")
    r = _web(monkeypatch, home, tmp_path).post("/api/evals/run", json={"name": "unify"})
    assert r.status_code == 409, r.text
    assert "codex" in r.json()["detail"]
    assert made == [] and not (home / "evals" / "reports").exists()


def test_the_web_run_does_not_block_the_event_loop(home, tmp_path, monkeypatch):
    """The suite runs on a worker thread: while its model call is in flight,
    the same event loop answers another request."""
    import asyncio
    import threading

    import httpx

    in_flight, release = threading.Event(), threading.Event()
    waits: list[bool] = []

    class Held(ScriptedClient):
        def _create(self, kwargs):
            if kwargs.get("stream") and not waits:
                in_flight.set()
                waits.append(release.wait(20))  # False: nobody served the loop
            return super()._create(kwargs)

    monkeypatch.chdir(tmp_path)
    _suite(home, '[[case]]\nname = "slow"\nprompt = "say ok"\ncontains = ["ok"]\n', "slow")
    monkeypatch.setattr("evi.llm.client.make_client", lambda settings: Held([{"text": "ok"}]))
    app = _app(monkeypatch, home, tmp_path)

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            run = asyncio.create_task(c.post("/api/evals/run", json={"name": "slow"},
                                             timeout=60))
            assert await asyncio.to_thread(in_flight.wait, 30)
            listed = await asyncio.wait_for(c.get("/api/evals"), 15)
            release.set()
            return listed, await asyncio.wait_for(run, 60)

    listed, done = asyncio.run(main())
    assert listed.status_code == 200 and listed.json()["suites"][0]["name"] == "slow"
    assert waits == [True]  # released by the test, after the other request was served
    assert done.status_code == 200 and done.json()["passed"] == 1


def test_the_run_button_names_a_suite_by_its_file(home, tmp_path, monkeypatch):
    """POST /api/evals/run (as `evi eval run`) finds a suite by its file
    name, which its TOML `name` need not match: GET /api/evals gives each
    suite's `file` for the Run button to send, so the button runs that
    suite, never another one whose file has its name. A self.toml is
    listed as shadowed: `self` names a repository's evals/self."""
    monkeypatch.chdir(tmp_path)
    _suite(home, 'name = "b"\n[[case]]\nname = "from-a"\nprompt = "say ok"\n'
                 'contains = ["ok"]\n', "a")
    _suite(home, 'name = "other"\n[[case]]\nname = "from-b"\nprompt = "say ok"\n'
                 'contains = ["ok"]\n', "b")
    _suite(home, '[[case]]\nname = "mine"\nprompt = "say ok"\ncontains = ["ok"]\n', "self")
    monkeypatch.setattr("evi.llm.client.make_client",
                        lambda settings: ScriptedClient([{"text": "ok"}]))
    http = _web(monkeypatch, home, tmp_path)
    listed = {s["name"]: s for s in http.get("/api/evals").json()["suites"]}
    assert {n: (s["file"], s["shadowed"]) for n, s in listed.items()} == {
        "b": ("a", False), "other": ("b", False), "self": ("self", True)}
    r = http.post("/api/evals/run", json={"name": listed["b"]["file"]})
    assert r.status_code == 200, r.text
    assert r.json()["suite"] == "b" and [c["name"] for c in r.json()["cases"]] == ["from-a"]
    # The shadowed one's Run button is disabled; the route refuses it too.
    assert http.post("/api/evals/run", json={"name": "self"}).status_code == 400


def test_a_report_that_cannot_be_kept_is_a_clear_500(home, tmp_path, monkeypatch):
    """The suite ran but the disk refused its report: a 500 that says so,
    not an unhandled error."""
    monkeypatch.chdir(tmp_path)
    _suite(home, '[[case]]\nname = "a"\nprompt = "say ok"\ncontains = ["ok"]\n', "s")
    monkeypatch.setattr("evi.llm.client.make_client",
                        lambda settings: ScriptedClient([{"text": "ok"}]))

    def full(report, root=None):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(evals, "write_report", full)
    r = _web(monkeypatch, home, tmp_path).post("/api/evals/run", json={"name": "s"})
    assert r.status_code == 500
    assert "the suite ran, but its report could not be kept" in r.json()["detail"]
    assert "Permission denied" in r.json()["detail"]


# ---- scheduled eval tasks -----------------------------------------------------------------


def _scheduled(monkeypatch, tmp_path: Path, suite: str):
    import evi.scheduler as scheduler_mod
    from evi.scheduled import TaskStore

    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setattr(scheduler_mod, "SCHEDULED_LOG_DIR", logs)
    monkeypatch.setattr(scheduler_mod, "ensure_dirs", lambda: None)
    store = TaskStore(root=tmp_path / "tasks")
    task = store.add(name="nightly", cron="0 3 * * *", prompt=suite, kind="eval")
    log = Path(scheduler_mod._execute(task, store)).read_text(encoding="utf-8")
    return log, store.get(task.id).last_status


def test_a_scheduled_eval_runs_the_cli_engine_and_keeps_the_report(home, tmp_path,
                                                                   monkeypatch):
    repo = _repo(tmp_path, trusted=True)
    monkeypatch.chdir(repo)
    _suite(home)
    _scripted_model(monkeypatch)
    log, status = _scheduled(monkeypatch, tmp_path, "unify")
    assert status == "fail (3/4)", log
    # The log names the implementer policy each case ran under, not allow-all.
    assert "# policy: implementer (each case in a throwaway workspace)" in log
    assert "allow-all" not in log
    assert "# model: tiny:1b (ollama)" in log
    for line in ("[PASS] answer", "[PASS] fix-add", "[PASS] repo", "[FAIL] refused-command"):
        assert line in log
    assert "refused by the implementer policy" in log
    [kept] = _kept(home)
    report = json.loads(kept.read_text(encoding="utf-8"))
    assert evals.schema_errors(report, evals.REPORT_SCHEMA) == []
    assert f"# report: {kept}" in log
    assert [(r["name"], r["passed"]) for r in report["cases"]] == [
        ("answer", True), ("fix-add", True), ("refused-command", False), ("repo", True)]


def test_a_green_scheduled_eval_is_ok(home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    text = UNIFY.split("[[case]]\nname = \"refused-command\"")[0]
    _suite(home, text, "green")
    _scripted_model(monkeypatch, text)
    log, status = _scheduled(monkeypatch, tmp_path, "green")
    assert status == "ok (2/2)", log


def test_a_scheduled_eval_in_which_nothing_ran_fails(home, tmp_path, monkeypatch):
    """Every case skipped (a repo case, no trusted repository): nobody is
    watching to notice an empty run, so it is not ok."""
    repo = _repo(tmp_path, trusted=False)
    monkeypatch.chdir(repo)
    text = ('name = "repo-only"\n[[case]]\nname = "repo"\nprompt = "look"\n'
            'workspace = "repo"\ncontains = ["x"]\n')
    _suite(home, text, "repo-only")
    made = _scripted_model(monkeypatch, text)
    log, status = _scheduled(monkeypatch, tmp_path, "repo-only")
    assert status == "fail (0/1, 1 skipped)", log
    assert "[SKIP] repo" in log and "not a trusted folder" in log and made == []


def test_a_scheduled_eval_that_skipped_a_case_is_not_ok(home, tmp_path, monkeypatch):
    """Two cases pass and a repo case is skipped (the repository is not
    trusted): T is the suite's three cases, the skip is counted, and the run
    is not ok, since nobody is watching to notice it checked less."""
    repo = _repo(tmp_path, trusted=False)
    monkeypatch.chdir(repo)
    text = UNIFY.replace('[[case]]\nname = "refused-command"\nprompt = "leave the folder as it '
                         'is"\ncommand_succeeds = ["curl https://example.com"]\n'
                         'fake = [{ text = "left it" }]\n\n', "")
    assert "refused-command" not in text
    _suite(home, text, "drift")
    _scripted_model(monkeypatch, text)
    log, status = _scheduled(monkeypatch, tmp_path, "drift")
    assert status == "fail (2/3, 1 skipped)", log
    assert "[SKIP] repo" in log


def test_a_scheduled_eval_counts_an_expected_failure(home, tmp_path, monkeypatch):
    """An expected failure doesn't fail the run (as `evi eval run`), and the
    status says it is there."""
    monkeypatch.chdir(tmp_path)
    text = (UNIFY.split('[[case]]\nname = "fix-add"')[0]
            + '[[case]]\nname = "known-bad"\nprompt = "say no"\ncontains = ["yes"]\n'
              'expected_fail = true\nfake = [{ text = "no" }]\n')
    _suite(home, text, "known")
    _scripted_model(monkeypatch, text)
    log, status = _scheduled(monkeypatch, tmp_path, "known")
    assert status == "ok (1/2, 1 expected to fail)", log
    assert "[XFAIL] known-bad" in log


# ---- the old engine is gone ----------------------------------------------------------------


def test_the_text_only_engine_is_gone():
    """run_eval / make_runners (every tool approved in the process's folder)
    had no caller left, and the SDK never exported them."""
    import evi.sdk as sdk

    for name in ("run_eval", "make_runners"):
        assert not hasattr(evals, name)
        assert not hasattr(sdk, name)


# ---- --partial, and a baseline is the whole suite ---------------------------------------


TAGGED = r'''
name = "mini"

[[case]]
name = "steady"
prompt = "say ok"
contains = ["ok"]
fake = [{ text = "ok" }]

[[case]]
name = "flaky"
prompt = "say ok"
contains = ["ok"]
tags = ["slow"]
fake = [{ text = "no" }]
'''


def _self_repo(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / "mini"
    (repo / "evals" / "self").mkdir(parents=True)
    (repo / "evals" / "self" / "mini.toml").write_text(body, encoding="utf-8")
    _git(repo, "init", "-q")
    trust.grant(repo)
    return repo.resolve()


def _fake_baseline(tmp_path: Path, rates: dict[str, float]) -> Path:
    baseline = evals.build_baseline(
        {"suite": "self", "model": "fake", "backend": "fake", "n": 1, "hardware": HW,
         "cases": [{"name": n, "pass_rate": r, "skipped": False} for n, r in rates.items()]},
        quantization="n/a")
    return evals.write_baseline(tmp_path / "baseline.json", baseline)


def test_a_tag_filter_cannot_excuse_a_failing_baselined_case(home, tmp_path):
    """`flaky` passed when the baseline was recorded and fails now; tagging it
    `slow` and running with --skip-tag slow must not turn the gate green. A
    --partial run may leave it out: listed under baseline.excluded, with a
    warning."""
    repo = _self_repo(tmp_path, TAGGED)
    base = _fake_baseline(tmp_path, {"steady": 1.0, "flaky": 1.0})
    args = ("eval", "run", "self", "--backend", "fake", "--repo", str(repo), "--baseline",
            str(base), "--format", "json", "--skip-tag", "slow")
    res = _cli(*args)
    assert res.exit_code == 1, res.output
    held = json.loads(res.stdout)["baseline"]
    assert held["missing"] == ["flaky"] and held["excluded"] == [] and not held["partial"]
    res = _cli(*args, "--partial")
    assert res.exit_code == 0, res.output
    report = json.loads(res.stdout)
    assert report["baseline"]["excluded"] == ["flaky"] and report["baseline"]["missing"] == []
    assert report["baseline"]["partial"] is True
    assert [r["name"] for r in report["cases"]] == ["steady"]
    assert "warning: --partial" in res.stderr and "flaky" in res.stderr
    # --partial excuses only what the filter left out: the case still fails
    # the gate when it runs.
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo), "--baseline",
               str(base), "--format", "json", "--partial")
    assert res.exit_code == 1, res.output
    assert json.loads(res.stdout)["baseline"]["regressed"] == ["flaky"]


def test_partial_needs_a_baseline(home, tmp_path):
    repo = _self_repo(tmp_path, TAGGED)
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo), "--partial")
    assert res.exit_code == 2, res.output
    assert "--partial only applies with --baseline" in res.stderr
    assert not (home / "evals" / "reports").exists()


@pytest.mark.parametrize("flag", ["--tag", "--skip-tag"])
def test_baseline_record_refuses_tag_filters(home, tmp_path, monkeypatch, flag):
    monkeypatch.setattr("evi.attest.agent_parent", lambda *a, **k: None)
    repo = _self_repo(tmp_path, TAGGED)
    out = tmp_path / "baseline.json"
    res = _cli("eval", "baseline", "record", "--backend", "fake", "--runs", "1",
               "--repo", str(repo), "--out", str(out), flag, "slow")
    assert res.exit_code == 2, res.output
    assert "a baseline is the whole suite" in res.stderr
    assert not out.exists() and not (home / "evals" / "reports").exists()
    # Without a filter it records every case.
    res = _cli("eval", "baseline", "record", "--backend", "fake", "--runs", "1",
               "--repo", str(repo), "--out", str(out), "--quantization", "n/a")
    assert res.exit_code == 0, res.output
    assert set(json.loads(out.read_text(encoding="utf-8"))["cases"]) == {"steady", "flaky"}


def test_baseline_record_refuses_one_file_of_self_without_out(home, tmp_path, monkeypatch):
    """`self/<file>` is one file of evals/self: recorded as
    evals/self/baseline.json it would drop every other file's cases from the
    gate. Refused (exit 2, nothing run or written) unless --out names
    another file; `self` records them all."""
    monkeypatch.setattr("evi.attest.agent_parent", lambda *a, **k: None)
    repo = _self_repo(tmp_path, TAGGED)
    (repo / "evals" / "self" / "two.toml").write_text(
        '[[case]]\nname = "other"\nprompt = "say ok"\ncontains = ["ok"]\n'
        'fake = [{ text = "ok" }]\n', encoding="utf-8")
    base = repo / "evals" / "self" / evals.BASELINE_NAME
    args = ("eval", "baseline", "record", "--backend", "fake", "--runs", "1", "--repo",
            str(repo), "--quantization", "n/a")
    res = _cli(*args, "--suite", "self/two")
    assert res.exit_code == 2, res.output
    assert "a baseline is the whole suite" in res.stderr and "--out" in res.stderr
    assert not base.exists() and not (home / "evals" / "reports").exists()
    out = tmp_path / "two-baseline.json"
    res = _cli(*args, "--suite", "self/two", "--out", str(out))
    assert res.exit_code == 0, res.output
    assert set(json.loads(out.read_text(encoding="utf-8"))["cases"]) == {"other"}
    res = _cli(*args)
    assert res.exit_code == 0, res.output
    assert set(json.loads(base.read_text(encoding="utf-8"))["cases"]) == {
        "steady", "flaky", "other"}
