"""`evi eval` — run, list, compare, baseline record — and `evi models recommend
--verify` (loop-eval-gates).

Acceptance criterion 4 — `evi eval run self --backend fake` exits 0 and
writes a report with one row per case:
test_eval_run_self_on_the_fake_backend_exits_0_with_a_row_per_case, on a
trusted throwaway copy of this repository's evals/self (the starter
qualification suite, every case with a fake script).

The rest: an untrusted repository's self suite is skipped (exit 2, nothing
runs), `evi eval compare` exits 1 on a regression, `evi eval baseline record`
is refused where an eVi agent may have started it (EVI_AGENT; nothing
written) and otherwise writes a baseline that matches its schema, and
`evi models recommend --verify` runs the suite per recommended model it can
run, ranked.

eVi's home, config and trust list live under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evi import evals, trust

ROOT = Path(__file__).resolve().parents[1]
STARTER = ["read-then-edit", "search", "refuse-destructive", "ask-when-ambiguous",
           "apply-patch", "crlf-edit"]
HW = {"gpu": "none", "vram_gb": None, "cpu": "test", "ram_gb": None, "os": "test"}


@pytest.fixture
def home(monkeypatch, tmp_path):
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr("evi.backends.registry.BACKENDS_PATH", evi_home / "backends.json")
    # nvidia-smi and friends are not what these tests are about.
    monkeypatch.setattr(evals, "hardware_summary", lambda **kw: {**HW, **{
        k: v for k, v in kw.items() if v not in (None, "")}})
    (evi_home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    return evi_home


def _cli(*args: str):
    import evi.apps.cli.main as cli_main

    return CliRunner().invoke(cli_main.app, list(args))


def _git_repo(folder: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(folder)], check=True, timeout=60)
    return folder.resolve()


def _self_copy(tmp_path: Path, *, trusted: bool = True) -> Path:
    """A throwaway repository holding a copy of this repository's evals/self
    suites and fixtures (never its baseline)."""
    repo = tmp_path / "copy"
    shutil.copytree(ROOT / "evals" / "self", repo / "evals" / "self",
                    ignore=shutil.ignore_patterns("baseline.json", "__pycache__"))
    _git_repo(repo)
    if trusted:
        trust.grant(repo)
    return repo.resolve()


MINI = r'''
name = "mini"

[[case]]
name = "quick"
prompt = "say ok"
contains = ["ok"]
fake = [{ text = "ok" }]
'''


def _mini_repo(tmp_path: Path, body: str = MINI, name: str = "mini") -> Path:
    repo = tmp_path / name
    (repo / "evals" / "self").mkdir(parents=True)
    (repo / "evals" / "self" / "mini.toml").write_text(body, encoding="utf-8")
    _git_repo(repo)
    trust.grant(repo)
    return repo.resolve()


def _reports(home: Path, suite: str = "self") -> list[Path]:
    return sorted((home / "evals" / "reports" / suite).glob("*.json"))


# ---- evi eval run self --backend fake (acceptance criterion 4) -------------------------


def test_eval_run_self_on_the_fake_backend_exits_0_with_a_row_per_case(home, tmp_path):
    repo = _self_copy(tmp_path)
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo),
               "--format", "json")
    assert res.exit_code == 0, res.output
    report = json.loads(res.stdout)
    assert evals.schema_errors(report, evals.REPORT_SCHEMA) == []
    assert [r["name"] for r in report["cases"]] == STARTER
    assert report["suite"] == "self" and report["backend"] == "fake"
    by = {r["name"]: r for r in report["cases"]}
    assert all(by[n]["passed"] for n in STARTER if n != "crlf-edit"), report["cases"]
    # The CRLF case is a known gap: it reports but doesn't fail the run.
    assert by["crlf-edit"]["expected_fail"] and by["crlf-edit"]["runs"] == 1
    assert not any(r["skipped"] for r in report["cases"])
    [path] = _reports(home)
    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert path.with_suffix(".html").is_file()
    assert f"report: {path}" in res.stderr


def test_eval_run_text_output_tags_and_listing(home, tmp_path):
    repo = _mini_repo(tmp_path, MINI + r'''
[[case]]
name = "tagged"
prompt = "say no"
contains = ["yes"]
tags = ["gap"]
expected_fail = true
fake = [{ text = "no" }]
''')
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo), "--tag", "gap")
    assert res.exit_code == 0, res.output
    assert "XFAIL" in res.stdout and "tagged" in res.stdout and "quick" not in res.stdout
    assert "1 expected to fail" in res.stdout
    res = _cli("eval", "run", "self/mini", "--backend", "fake", "--repo", str(repo),
               "--skip-tag", "gap")
    assert res.exit_code == 0 and "PASS" in res.stdout and "tagged" not in res.stdout
    assert _reports(home, "self-mini")
    listing = _cli("eval", "list", "--repo", str(repo))
    assert listing.exit_code == 0 and "self" in listing.stdout and "2 cases" in listing.stdout


def test_eval_run_on_the_configured_backend_with_a_judge(home, tmp_path, monkeypatch):
    """No --backend: [llm]'s client answers (here a scripted one standing in
    for the model), and a judge rubric is graded by the same model."""
    from evi.evals_fake import ScriptedClient

    clients: list = []

    def make_client(settings):
        clients.append(ScriptedClient([{"text": "PASS: friendly enough"}]))
        return clients[-1]

    monkeypatch.setattr("evi.llm.client.make_client", make_client)
    (home / "config.toml").write_text(
        "[llm]\nbackend = \"ollama\"\nmodel = \"tiny:1b\"\n"
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    (home / "evals").mkdir()
    (home / "evals" / "mine.toml").write_text(
        'name = "mine"\n[[case]]\nname = "nice"\nprompt = "greet me"\ncontains = ["PASS"]\n'
        'judge = "friendly"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    res = _cli("eval", "run", "mine", "--format", "json")
    assert res.exit_code == 0, res.output
    report = json.loads(res.stdout)
    assert report["backend"] == "ollama" and report["model"] == "tiny:1b"
    assert report["cases"][0]["passed"] and len(clients) == 2  # the answer, the judge
    assert _reports(home, "mine")


def test_a_failing_case_exits_1(home, tmp_path):
    repo = _mini_repo(tmp_path, MINI.replace('fake = [{ text = "ok" }]',
                                             'fake = [{ text = "nope" }]'))
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo))
    assert res.exit_code == 1, res.output
    assert "FAIL" in res.stdout and "missing 'ok'" in res.stdout


@pytest.mark.parametrize("args, message", [
    (["eval", "run", "nosuch", "--backend", "fake"], "no eval suite"),
    (["eval", "run", "self", "--backend", "nope"], "no saved backend named 'nope'"),
    (["eval", "run", "self", "--backend", "fake", "--format", "xml"], "unknown --format"),
    (["eval", "run", "self", "--backend", "fake", "--tag", "nothing-has-it"], "no case left"),
    (["eval", "run", "self", "--backend", "fake", "--baseline", "missing.json"],
     "could not read"),
])
def test_usage_errors_exit_2_without_a_run(home, tmp_path, args, message):
    repo = _mini_repo(tmp_path)
    res = _cli(*args, "--repo", str(repo))
    assert res.exit_code == 2, res.output
    assert message in res.stderr
    assert not (home / "evals" / "reports").exists()


def test_a_typo_in_a_case_is_a_usage_error_naming_the_key(home, tmp_path):
    repo = _mini_repo(tmp_path, MINI + '\n[[case]]\nname = "x"\nprompt = "p"\n'
                                       'forbid_tool = ["run_command"]\n')
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo))
    assert res.exit_code == 2 and "unknown key 'forbid_tool'" in res.stderr


def test_an_untrusted_repositorys_self_suite_is_skipped(home, tmp_path):
    repo = _self_copy(tmp_path, trusted=False)
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo))
    assert res.exit_code == 2, res.output
    assert "skipped:" in res.stderr and "not a trusted folder" in res.stderr
    assert not (home / "evals" / "reports").exists()
    listing = _cli("eval", "list", "--repo", str(repo))
    assert listing.exit_code == 0
    assert "self" in listing.stdout and "skipped" in listing.stdout


def test_eval_run_with_a_baseline(home, tmp_path):
    repo = _mini_repo(tmp_path)
    baseline = dict(evals.build_baseline(
        {"suite": "self", "model": "ref", "backend": "fake", "n": 5, "hardware": HW,
         "cases": [{"name": "quick", "pass_rate": 1.0, "skipped": False}]},
        quantization="Q4"))
    path = evals.write_baseline(tmp_path / "baseline.json", baseline)
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo),
               "--baseline", str(path), "--format", "json")
    assert res.exit_code == 0, res.output
    row = json.loads(res.stdout)["cases"][0]
    assert row["baseline"] == {"pass_rate": 1.0, "tolerance": 0.1, "regressed": False}
    (tmp_path / "bad.json").write_text('{"cases": {"quick": 2}}', encoding="utf-8")
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo),
               "--baseline", str(tmp_path / "bad.json"))
    assert res.exit_code == 2 and "not an eval baseline" in res.stderr


TWO = r'''
name = "mini"

[[case]]
name = "alpha"
prompt = "say ok"
contains = ["ok"]
fake = [{ text = "ok" }]

[[case]]
name = "beta"
prompt = "say ok"
contains = ["ok"]
tags = ["slow"]
fake = [{ text = "ok" }]
'''
BETA = 'name = "beta"\nprompt = "say ok"\ncontains = ["ok"]\ntags = ["slow"]\nfake = [{ text = "ok" }]'


def _gate(repo: Path, base: Path, *extra: str):
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo),
               "--baseline", str(base), "--format", "json", *extra)
    return res, json.loads(res.stdout) if res.stdout.strip().startswith("{") else None


@pytest.mark.parametrize("edit, why", [
    # The case now fails but was marked expected_fail.
    (BETA.replace('fake = [{ text = "ok" }]', 'expected_fail = true\nfake = [{ text = "no" }]'),
     "below"),
    # The case was renamed (or deleted): it is missing from the run.
    (BETA.replace('name = "beta"', 'name = "beta2"'), "missing"),
    # Its fake script was removed: skipped under the fake backend.
    (BETA.replace('\nfake = [{ text = "ok" }]', ''), "skipped"),
])
def test_editing_the_suite_does_not_get_past_the_baseline(home, tmp_path, monkeypatch, edit,
                                                         why):
    """evals/self/*.toml is not write-protected (only baseline.json is), so
    an edit to the suite must not turn a regression into a green run."""
    monkeypatch.setattr("evi.attest.agent_parent", lambda *a, **k: None)
    repo = _mini_repo(tmp_path, TWO)
    res = _cli("eval", "baseline", "record", "--backend", "fake", "--runs", "1",
               "--repo", str(repo), "--quantization", "Q4")
    assert res.exit_code == 0, res.output
    base = repo / "evals" / "self" / "baseline.json"
    assert json.loads(base.read_text(encoding="utf-8"))["cases"] == {"alpha": 1.0, "beta": 1.0}
    suite = repo / "evals" / "self" / "mini.toml"
    suite.write_text(TWO.replace(BETA, edit), encoding="utf-8")
    res, report = _gate(repo, base)
    assert res.exit_code == 1, res.output
    if why == "missing":
        assert report["baseline"]["missing"] == ["beta"]
    else:
        assert report["baseline"]["regressed"] == ["beta"]
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo),
               "--baseline", str(base))
    assert res.exit_code == 1 and "below baseline" in res.stdout


def test_a_case_left_out_by_skip_tag_is_not_held_to_the_baseline(home, tmp_path, monkeypatch):
    monkeypatch.setattr("evi.attest.agent_parent", lambda *a, **k: None)
    repo = _mini_repo(tmp_path, TWO)
    assert _cli("eval", "baseline", "record", "--backend", "fake", "--runs", "1",
                "--repo", str(repo), "--quantization", "Q4").exit_code == 0
    base = repo / "evals" / "self" / "baseline.json"
    res, report = _gate(repo, base, "--skip-tag", "slow")
    assert res.exit_code == 0, res.output
    assert report["baseline"]["excluded"] == ["beta"] and report["baseline"]["missing"] == []
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo),
               "--baseline", str(base), "--skip-tag", "slow")
    assert res.exit_code == 0 and "left out by --tag / --skip-tag): beta" in res.stdout


def test_a_fake_run_cannot_be_held_to_a_real_models_baseline(home, tmp_path):
    repo = _mini_repo(tmp_path)
    baseline = evals.build_baseline(
        {"suite": "self", "model": "qwen2.5-coder:7b", "backend": "ollama", "n": 5,
         "hardware": HW, "cases": [{"name": "quick", "pass_rate": 0.4, "skipped": False}]},
        quantization="Q4_K_M")
    path = evals.write_baseline(tmp_path / "baseline.json", baseline)
    res = _cli("eval", "run", "self", "--backend", "fake", "--repo", str(repo),
               "--baseline", str(path))
    assert res.exit_code == 2, res.output
    assert "can't be held to them" in res.stderr
    assert not (home / "evals" / "reports").exists()


def test_a_cli_agent_in_any_models_backend_is_a_clean_usage_error(home, tmp_path,
                                                                  monkeypatch):
    """[models] vision_backend = "codex" (a delegate CLI agent): the run is
    refused with exit 2 and the reason, not a traceback with exit 1 (the
    code that means a case failed)."""
    from evi.evals_fake import ScriptedClient

    monkeypatch.setattr("evi.llm.client.make_client",
                        lambda settings: ScriptedClient([{"text": "ok"}]))
    (home / "config.toml").write_text(
        "[llm]\nbackend = \"ollama\"\nmodel = \"tiny:1b\"\n[models]\nvision_backend = \"codex\"\n"
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    (home / "evals").mkdir()
    (home / "evals" / "smoke.toml").write_text(
        'name = "smoke"\n[[case]]\nname = "ok"\nprompt = "say ok"\ncontains = ["ok"]\n',
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    res = _cli("eval", "run", "smoke")
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "codex backend is a CLI agent" in res.stderr and "[models] vision_backend" in res.stderr
    assert not (home / "evals" / "reports").exists()


# ---- the reserved suite name `self` ------------------------------------------------------


def test_self_is_reserved_for_the_repositorys_suites(home, tmp_path, monkeypatch):
    res = _cli("eval", "new", "self")
    assert res.exit_code == 1 and "reserved" in res.stdout
    assert not (home / "evals" / "self.toml").exists()
    # One written by hand is listed as shadowed, not as a suite `self` would run.
    (home / "evals").mkdir(exist_ok=True)
    (home / "evals" / "self.toml").write_text(MINI, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    listing = _cli("eval", "list")
    assert listing.exit_code == 0
    assert "self.toml" in listing.stdout and "shadowed" in listing.stdout
    assert "(1 cases)" not in listing.stdout


# ---- evi eval compare ----------------------------------------------------------------


def _report_file(path: Path, rates: dict[str, float], model: str) -> Path:
    rows = []
    for name, rate in rates.items():
        row = evals.case_row(evals.EvalCase(name=name, prompt="p"), [])
        row.update(runs=5, passes=round(rate * 5), pass_rate=rate, passed=rate >= 0.8)
        rows.append(row)
    report = evals.summarize({
        "schema_version": 1, "kind": evals.REPORT_KIND, "suite": "self", "model": model,
        "backend": "test", "n": 5, "evi_version": "0", "started_at": "2026-10-17T00:00:00Z",
        "finished_at": "2026-10-17T00:01:00Z", "duration_s": 60.0, "hardware": HW,
        "baseline": None, "cases": rows})
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_eval_compare_exits_1_when_a_case_regressed(home, tmp_path):
    a = _report_file(tmp_path / "a.json", {"x": 0.8, "y": 1.0}, "old")
    b = _report_file(tmp_path / "b.json", {"x": 1.0, "y": 0.6}, "new")
    res = _cli("eval", "compare", str(a), str(b))
    assert res.exit_code == 1, res.output
    assert "regressed" in res.stdout and "improved" in res.stdout and "-40 pts" in res.stdout
    res = _cli("eval", "compare", str(b), str(a), "--format", "json")
    assert res.exit_code == 1 and json.loads(res.stdout)["regressed"] == ["x"]
    assert _cli("eval", "compare", str(a), str(a)).exit_code == 0
    assert _cli("eval", "compare", str(a), str(b), "--tolerance", "0.5").exit_code == 0
    (tmp_path / "junk.json").write_text('{"hello": 1}', encoding="utf-8")
    res = _cli("eval", "compare", str(a), str(tmp_path / "junk.json"))
    assert res.exit_code == 2 and "neither an eval report nor a baseline" in res.stderr


# ---- evi eval baseline record -----------------------------------------------------------


def test_baseline_record_is_refused_under_an_agent(home, tmp_path, monkeypatch):
    repo = _mini_repo(tmp_path)
    monkeypatch.setenv("EVI_AGENT", "1")
    res = _cli("eval", "baseline", "record", "--backend", "fake", "--repo", str(repo))
    assert res.exit_code == 2, res.output
    assert "refusing to record an eval baseline" in res.stderr and "EVI_AGENT" in res.stderr
    assert not (repo / "evals" / "self" / "baseline.json").exists()
    assert not (home / "evals" / "reports").exists()  # nothing even ran


def test_baseline_record_writes_a_baseline_that_matches_the_schema(home, tmp_path,
                                                                  monkeypatch):
    import jsonschema

    repo = _mini_repo(tmp_path)
    monkeypatch.delenv("EVI_AGENT", raising=False)
    monkeypatch.setattr("evi.attest.agent_parent", lambda *a, **k: None)
    res = _cli("eval", "baseline", "record", "--suite", "self", "--runs", "2",
               "--backend", "fake", "--model", "ref-model", "--quantization", "Q4_K_M",
               "--gpu", "Test GPU", "--vram-gb", "16", "--repo", str(repo))
    assert res.exit_code == 0, res.output
    path = repo / "evals" / "self" / "baseline.json"
    baseline = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(evals.BASELINE_SCHEMA).validate(baseline)
    assert baseline["cases"] == {"quick": 1.0} and baseline["n"] == 2
    assert baseline["model"] == "ref-model" and baseline["backend"] == "fake"
    assert baseline["quantization"] == "Q4_K_M"
    assert baseline["hardware"]["gpu"] == "Test GPU" and baseline["hardware"]["vram_gb"] == 16.0
    assert "baseline recorded" in res.stdout
    [report] = _reports(home)
    assert json.loads(report.read_text(encoding="utf-8"))["cases"][0]["runs"] == 2
    # A suite outside evals/self needs --out.
    (home / "evals").mkdir(exist_ok=True)
    (home / "evals" / "mine.toml").write_text(MINI, encoding="utf-8")
    res = _cli("eval", "baseline", "record", "--suite", "mine", "--backend", "fake")
    assert res.exit_code == 2 and "--out is needed" in res.stderr


# ---- evi models recommend --verify ---------------------------------------------------------


def _small_machine(monkeypatch):
    from evi.hardware import HardwareInfo

    monkeypatch.setattr("evi.hardware.detect",
                        lambda: HardwareInfo(gpus=[], ram_total_bytes=16 * 1024 ** 3,
                                             platform="windows"))


def test_models_recommend_verify_ranks_the_recommended_models(home, tmp_path, monkeypatch):
    from evi.hardware import HardwareInfo
    from evi.recommend import recommend

    _small_machine(monkeypatch)
    rec = recommend(HardwareInfo(gpus=[], ram_total_bytes=16 * 1024 ** 3, platform="windows"))
    ids = list(dict.fromkeys(m.id for m in (rec.chat, rec.coder, rec.fast) if m is not None))
    assert ids
    repo = _mini_repo(tmp_path)
    res = _cli("models", "recommend", "--verify", "--backend", "fake", "--repo", str(repo))
    assert res.exit_code == 0, res.output
    assert "Ranked by mean pass rate" in res.stdout
    models = sorted(json.loads(p.read_text(encoding="utf-8"))["model"] for p in _reports(home))
    assert models == sorted(ids)


def test_models_recommend_verify_without_a_served_model_exits_1(home, tmp_path, monkeypatch):
    import evi.apps.cli.main as cli_main

    _small_machine(monkeypatch)
    monkeypatch.setattr(cli_main, "_models_served", lambda backend: [])
    repo = _mini_repo(tmp_path)
    res = _cli("models", "recommend", "--verify", "--repo", str(repo))
    assert res.exit_code == 1, res.output
    assert "nothing to verify" in res.stderr
    assert not (home / "evals" / "reports").exists()


@pytest.mark.parametrize("where", ["no-repo", "repo-without-evals"])
def test_models_recommend_verify_outside_an_evi_checkout_says_where_the_suite_is(
        home, tmp_path, monkeypatch, where):
    """The qualification suite lives in eVi's source repository, not in an
    installed eVi: outside a checkout --verify says so (exit 2) instead of
    a bare 'no eval suites'."""
    _small_machine(monkeypatch)
    folder = tmp_path / "myproject"
    folder.mkdir()
    if where == "repo-without-evals":
        _git_repo(folder)
        trust.grant(folder)
    else:
        monkeypatch.setattr(evals, "find_repo", lambda start=None: None)
    monkeypatch.chdir(folder)
    res = _cli("models", "recommend", "--verify", "--backend", "fake")
    assert res.exit_code == 2, res.output
    assert "lives in eVi's source repository" in res.stderr
    assert "--suite" in res.stderr and "--repo" in res.stderr
    assert not (home / "evals" / "reports").exists()


def test_models_recommend_without_verify_runs_nothing(home, tmp_path, monkeypatch):
    _small_machine(monkeypatch)
    res = _cli("models", "recommend")
    assert res.exit_code == 0, res.output
    assert "Hardware" in res.stdout
    assert not (home / "evals" / "reports").exists()
