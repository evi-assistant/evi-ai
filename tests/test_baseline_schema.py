"""The eval baseline, evals/self/baseline.json (loop-eval-gates).

The baseline records the reference local model's pass rate per case
(schema_version, suite, model, backend, quantization, hardware {gpu, vram_gb,
cpu, ram_gb, os}, n, recorded_at, evi_version, cases), and gates Phase 2
prompt, repair and catalog changes. Its JSON Schema is evi.evals.BASELINE_SCHEMA.
Only the director's `evi eval baseline record` writes it (tests/test_eval_cli.py):
no agent may, so it is never grantable under the implementer policy and
write-protected for every other run.

Acceptance criterion 5 (the file itself, recorded on the reference model) is
the director's to produce; what this batch ships is the schema, the recorder
and the protection, tested here, and test_the_repositorys_baseline_matches_the_schema
checks the real file as soon as it exists.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from evi import evals, permissions
from evi.evals import BASELINE_SCHEMA, EvalCase
from evi.policy import GRANTABLE, HARD_PROTECTED, implementer_policy

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = {
    "schema_version": 1,
    "suite": "self",
    "model": "qwen2.5-coder:14b-instruct-q4_K_M",
    "backend": "ollama",
    "quantization": "Q4_K_M",
    "hardware": {"gpu": "NVIDIA GeForce RTX 5070 Ti", "vram_gb": 16.0,
                 "cpu": "AMD64 Family 25 Model 33", "ram_gb": 63.9,
                 "os": "Windows-11-10.0.26300-SP0"},
    "n": 5,
    "recorded_at": "2026-10-17T12:00:00Z",
    "evi_version": "1.0.54",
    "cases": {"read-then-edit": 0.8, "search": 1.0, "refuse-destructive": 1.0,
              "ask-when-ambiguous": 0.6, "apply-patch": 1.0, "crlf-edit": 0.0},
}


def _validate(doc: dict) -> None:
    jsonschema.Draft202012Validator(BASELINE_SCHEMA).validate(doc)


def test_the_fixture_baseline_matches_the_schema():
    jsonschema.Draft202012Validator.check_schema(BASELINE_SCHEMA)
    _validate(FIXTURE)
    assert evals.schema_errors(FIXTURE, BASELINE_SCHEMA) == []


@pytest.mark.parametrize("change", [
    lambda d: d.pop("model"),
    lambda d: d.pop("quantization"),
    lambda d: d["hardware"].pop("vram_gb"),
    lambda d: d.__setitem__("n", 0),
    lambda d: d.__setitem__("schema_version", 2),
    lambda d: d.__setitem__("recorded_at", "yesterday"),
    lambda d: d["cases"].__setitem__("search", 1.5),
    lambda d: d.__setitem__("cases", {}),
    lambda d: d.__setitem__("verified_by", "me"),
    lambda d: d["hardware"].__setitem__("gpus", []),
])
def test_a_baseline_off_the_schema_is_rejected(change):
    doc = copy.deepcopy(FIXTURE)
    change(doc)
    with pytest.raises(jsonschema.ValidationError):
        _validate(doc)


def test_build_baseline_records_the_cases_that_ran(tmp_path):
    rows = [evals.case_row(EvalCase(name="a", prompt="p"), [evals.RunOutcome(True)] * 4
                           + [evals.RunOutcome(False, ["x"])]),
            evals.case_row(EvalCase(name="b", prompt="p"), [], skipped="no fake script")]
    report = evals.summarize({"suite": "self", "model": "m", "backend": "ollama", "n": 5,
                              "hardware": FIXTURE["hardware"], "cases": rows})
    baseline = evals.build_baseline(report, quantization="Q4_K_M")
    _validate(baseline)
    assert baseline["cases"] == {"a": 0.8} and baseline["n"] == 5
    assert baseline["model"] == "m" and baseline["quantization"] == "Q4_K_M"
    assert baseline["hardware"] == FIXTURE["hardware"]
    path = evals.write_baseline(tmp_path / "evals" / "self" / "baseline.json", baseline)
    assert evals.load_baseline(path) == baseline
    assert path.read_bytes().endswith(b"}\n") and b"\r\n" not in path.read_bytes()


def test_an_invalid_baseline_is_never_written(tmp_path):
    bad = dict(FIXTURE, n=0)
    target = tmp_path / "baseline.json"
    with pytest.raises(evals.EvalError, match="schema"):
        evals.write_baseline(target, bad)
    assert not target.exists()
    target.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(evals.EvalError, match="not an eval baseline"):
        evals.load_baseline(target)


def test_the_repositorys_baseline_matches_the_schema():
    """evals/self/baseline.json, once the director recorded it."""
    path = ROOT / "evals" / "self" / "baseline.json"
    if not path.is_file():
        pytest.skip("evals/self/baseline.json is not recorded yet (`evi eval baseline record`)")
    baseline = json.loads(path.read_text(encoding="utf-8"))
    _validate(baseline)
    assert baseline["n"] >= 1 and baseline["suite"]
    cases = [c.name for p in sorted((ROOT / "evals" / "self").glob("*.toml"))
             for c in evals.load_suite_file(p).cases]
    assert set(baseline["cases"]) <= set(cases)


# ---- no agent may write it --------------------------------------------------------------


def test_no_task_can_write_the_baseline_even_when_named(tmp_path):
    from evi.tools.base import REGISTRY
    import evi.tools.fs  # noqa: F401  registers write_file, edit_file, apply_patch

    root = tmp_path / "repo"
    (root / "evals" / "self").mkdir(parents=True)
    rel = "evals/self/baseline.json"
    assert rel in HARD_PROTECTED and rel not in GRANTABLE
    for scope in ([rel], ["evals/self/*.json"], ["evals/**"], ["**"]):
        pol = implementer_policy(root, scope, trusted=True)
        for tool, args in (
            ("write_file", {"path": rel, "content": "{}"}),
            ("edit_file", {"path": rel, "old_string": "a", "new_string": "b"}),
            ("apply_patch", {"path": rel, "patch": "x"}),
            ("write_file", {"path": "./evals/self/BASELINE.json", "content": "{}"}),
        ):
            v = pol.check(REGISTRY[tool], json.dumps(args), workdir=str(root))
            assert not v.allowed and "no task may write it" in v.reason, (scope, tool, args)
    # The suite files next to it stay editable.
    pol = implementer_policy(root, trusted=True)
    assert pol.check(REGISTRY["write_file"], json.dumps(
        {"path": "evals/self/qualification.toml", "content": "x"}), workdir=str(root)).allowed
    # Nor can an allowed command that rewrites the files it is given.
    v = pol.check_command("ruff format evals/self/baseline.json", exec_dir=root.resolve())
    assert not v.allowed and "no task may write it" in v.reason


def test_the_baseline_is_write_protected_for_every_other_run(tmp_path):
    assert "evals/self/baseline.json" in permissions.WRITE_PROTECTED_PATHS
    assert "evals/self/baseline.json" in permissions.effective_protected_paths()
    hit = permissions.protected_write(
        "write_file", "fs", {"path": "evals/self/baseline.json", "content": "{}"},
        permissions.effective_protected_paths(), workdir=tmp_path)
    assert hit == "evals/self/baseline.json"
    # Reading it is fine (a write-only protection).
    assert permissions.protected_read(
        "fs", {"path": "evals/self/baseline.json"}, permissions.effective_protected_paths(),
        workdir=tmp_path) is None
