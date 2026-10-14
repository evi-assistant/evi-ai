"""Tests for workflow_guard.py, the CI gate against template injection.

The strict check is exercised on synthetic workflows and composite actions:
every script sink it knows (run:, github-script `script:`, nick-fields/retry
`command:`, a docker:// step's `args:`) must fail on `${{ ... }}` and pass when
the value comes through `env:`. The zizmor half is exercised against zizmor's
documented `json-v1` output (a flat list of findings; the path is
`locations[].symbolic.key.Local.verbatim_path`, the line is the 0-based
`concrete.location.start_point.row` of the location whose `kind` is
"Primary"), with zizmor itself replaced by a fake so the tests run offline.

Run with `python -m pytest .github/actions/workflow-guard` (ci.yml runs them
in the workflow-injection job and in the main test job).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("workflow_guard", HERE / "workflow_guard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


wg = _load()


def _workflow(*step_lines: str) -> str:
    return "\n".join([
        "on: push",
        "jobs:",
        "  build:",
        "    runs-on: ubuntu-latest",
        "    steps:",
        *step_lines,
    ]) + "\n"


def _where(findings) -> list[tuple[int, str]]:
    return [(f.line, f.where) for f in findings]


# ---- the strict check: every sink fails on an expression -------------------

def test_run_with_an_expression_fails_at_its_line():
    text = _workflow(
        "      - name: greet",
        "        run: |",
        "          echo start",
        '          echo "${{ steps.x.outputs.y }}"',
    )
    assert _where(wg.scan_text(text, "w.yml")) == [(9, "jobs.build.steps[0] (greet).run")]


def test_single_line_run_with_an_expression_fails():
    text = _workflow("      - run: echo ${{ github.event.pull_request.title }}")
    assert _where(wg.scan_text(text, "w.yml")) == [(6, "jobs.build.steps[0].run")]


@pytest.mark.parametrize("uses", [
    "actions/github-script@v7",
    "Actions/GitHub-Script@v7",  # GitHub resolves owner/repo case-insensitively
    "actions/github-script@60a0d83039c74a4aee543508d2ffcb1c3799cdea",
])
def test_github_script_with_an_expression_fails(uses):
    text = _workflow(
        f"      - uses: {uses}",
        "        with:",
        "          script: |",
        "            console.log(`${{ steps.x.outputs.y }}`)",
    )
    assert _where(wg.scan_text(text, "w.yml")) == [(9, "jobs.build.steps[0].with.script")]


def test_retry_command_with_an_expression_fails():
    text = _workflow(
        "      - uses: nick-fields/retry@v4",
        "        with:",
        "          max_attempts: 3",
        "          command: echo ${{ steps.x.outputs.y }}",
    )
    assert _where(wg.scan_text(text, "w.yml")) == [(9, "jobs.build.steps[0].with.command")]


@pytest.mark.parametrize("key", ["args", "entrypoint"])
def test_docker_step_command_line_with_an_expression_fails(key):
    text = _workflow(
        "      - uses: docker://alpine:3.20",
        "        with:",
        f'          {key}: sh -c "echo ${{{{ steps.x.outputs.y }}}}"',
    )
    assert _where(wg.scan_text(text, "w.yml")) == [(8, f"jobs.build.steps[0].with.{key}")]


def test_composite_action_run_with_an_expression_fails():
    text = "\n".join([
        "name: x",
        "runs:",
        "  using: composite",
        "  steps:",
        "    - shell: bash",
        "      run: echo '${{ inputs.prompt }}'",
    ]) + "\n"
    assert _where(wg.scan_text(text, "action.yml")) == [(6, "runs.steps[0].run")]


def test_values_through_env_pass():
    text = _workflow(
        "      - env:",
        "          Y: ${{ steps.x.outputs.y }}",
        "        if: ${{ github.event_name == 'push' }}",
        '        run: echo "$Y"',
        "      - uses: actions/github-script@v7",
        "        env:",
        "          Y: ${{ steps.x.outputs.y }}",
        "        with:",
        "          github-token: ${{ secrets.GITHUB_TOKEN }}",
        "          script: console.log(process.env.Y)",
        "      - uses: actions/setup-python@v7",
        "        with:",
        "          python-version: ${{ matrix.python }}",
    )
    assert wg.scan_text(text, "w.yml") == []


def test_other_actions_inputs_are_not_script_sinks():
    # `command` only runs as code for the actions that define it so.
    text = _workflow(
        "      - uses: some/other-action@v1",
        "        with:",
        "          command: ${{ steps.x.outputs.y }}",
    )
    assert wg.scan_text(text, "w.yml") == []


def test_cli_fails_on_a_synthetic_workflow_and_passes_on_a_clean_one(tmp_path, capsys):
    bad = tmp_path / "bad.yml"
    bad.write_text(_workflow('      - run: echo "${{ steps.x.outputs.y }}"'), encoding="utf-8")
    good = tmp_path / "good.yml"
    good.write_text(_workflow("      - run: echo ok"), encoding="utf-8")
    assert wg.main(["--no-defaults", str(bad)]) == 1
    assert "script injection finding" in capsys.readouterr().out
    assert wg.main(["--no-defaults", str(good)]) == 0
    assert wg.main(["--no-defaults", str(tmp_path / "missing-*.yml")]) == 2


def test_the_repos_own_workflows_and_examples_are_clean(monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    assert wg.main(["examples/github/*.yml"]) == 0, capsys.readouterr().out


# ---- the zizmor half, against zizmor's json-v1 schema -----------------------

def _location(kind: str, row: int, path: str = "./.github/workflows/x.yml") -> dict:
    return {
        "symbolic": {
            "key": {"Local": {"verbatim_path": path}},
            "annotation": "this step",
            "route": {"route": [{"Key": "jobs"}, {"Key": "build"}, {"Key": "steps"}, {"Index": 0}]},
            "feature_kind": "Normal",
            "kind": kind,
        },
        "concrete": {
            "location": {
                "start_point": {"row": row, "column": 8},
                "end_point": {"row": row + 1, "column": 0},
                "offset_span": {"start": 100, "end": 140},
            },
            "feature": "run: echo ${{ github.event.issue.title }}",
            "comments": [],
        },
    }


def _finding(ident: str, desc: str, severity: str, locations: list, ignored: bool = False) -> dict:
    return {
        "ident": ident,
        "desc": desc,
        "url": f"https://docs.zizmor.sh/audits/#{ident}",
        "determinations": {"confidence": "High", "severity": severity, "persona": "Regular"},
        "locations": locations,
        "ignored": ignored,
        "fixes": [],
    }


ZIZMOR_V1 = [
    _finding("template-injection", "code injection via template expansion", "High",
             [_location("Hidden", 2), _location("Primary", 9), _location("Related", 4)]),
    _finding("artipacked", "credential persistence through GitHub Actions artifacts",
             "Medium", [_location("Primary", 20)]),
    _finding("template-injection", "code injection via template expansion", "High",
             [_location("Primary", 30)], ignored=True),
]


def test_split_zizmor_fails_on_template_injection_and_reports_the_rest():
    errors, notes = wg._split_zizmor(ZIZMOR_V1)
    assert [(e.path, e.line, e.where) for e in errors] == [
        ("./.github/workflows/x.yml", 10, "zizmor template-injection")  # Primary row 9 -> line 10
    ]
    assert notes == [
        "./.github/workflows/x.yml:21: zizmor artipacked (Medium): "
        "credential persistence through GitHub Actions artifacts"
    ]


def test_split_zizmor_reads_older_and_remote_keys():
    old = _finding("template-injection", "d", "High", [_location("Primary", 0)])
    old["locations"][0]["symbolic"]["key"] = {"Local": {"prefix": None, "given_path": "a.yml"}}
    remote = _finding("template-injection", "d", "High", [_location("Primary", 1)])
    remote["locations"][0]["symbolic"]["key"] = {"Remote": {"slug": {}, "path": "b.yml"}}
    errors, _ = wg._split_zizmor([old, remote])
    assert [(e.path, e.line) for e in errors] == [("a.yml", 1), ("b.yml", 2)]


@pytest.mark.parametrize("result", [{"desc": "no ident"}, "template-injection", None])
def test_split_zizmor_fails_closed_on_an_unknown_shape(result):
    with pytest.raises(RuntimeError, match="unrecognised zizmor finding"):
        wg._split_zizmor([result])


class _FakeZizmor:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.argv: list[str] = []

    def __call__(self, argv, **kwargs):
        self.argv = list(argv)
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def fake_zizmor(monkeypatch):
    def install(stdout: str, returncode: int = 0, stderr: str = "") -> _FakeZizmor:
        fake = _FakeZizmor(stdout, returncode, stderr)
        monkeypatch.setattr(wg.shutil, "which", lambda name: "/usr/bin/zizmor")
        monkeypatch.setattr(wg.subprocess, "run", fake)
        return fake
    return install


def test_run_zizmor_asks_for_the_frozen_schema_without_exit_codes(fake_zizmor):
    fake = fake_zizmor(json.dumps(ZIZMOR_V1))
    errors, notes = wg.run_zizmor(["w.yml"])
    assert fake.argv[1:] == ["--format=json-v1", "--no-exit-codes", "--persona=pedantic", "w.yml"]
    assert len(errors) == 1 and len(notes) == 1


@pytest.mark.parametrize("stdout, returncode", [
    ("[]", 1),               # an audit error (findings no longer set the exit code)
    ("[]", 3),               # no inputs collected
    ("not json", 0),
    ('{"findings": []}', 0),
])
def test_run_zizmor_fails_closed(fake_zizmor, stdout, returncode):
    fake_zizmor(stdout, returncode, stderr="boom")
    with pytest.raises(RuntimeError):
        wg.run_zizmor(["w.yml"])


def test_cli_fails_on_a_zizmor_template_injection(tmp_path, fake_zizmor, monkeypatch, capsys):
    clean = tmp_path / "clean.yml"
    clean.write_text(_workflow("      - run: echo ok"), encoding="utf-8")
    fake_zizmor(json.dumps(ZIZMOR_V1))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert wg.main(["--no-defaults", "--zizmor", str(clean)]) == 1
    out = capsys.readouterr().out
    assert "::error file=./.github/workflows/x.yml,line=10::zizmor template-injection" in out
    assert "1 advisory finding(s), not gating" in out


def test_cli_passes_when_zizmor_finds_nothing(tmp_path, fake_zizmor):
    clean = tmp_path / "clean.yml"
    clean.write_text(_workflow("      - run: echo ok"), encoding="utf-8")
    fake_zizmor("[]")
    assert wg.main(["--no-defaults", "--zizmor", str(clean)]) == 0


def test_cli_without_zizmor_is_a_usage_error(tmp_path, monkeypatch):
    clean = tmp_path / "clean.yml"
    clean.write_text(_workflow("      - run: echo ok"), encoding="utf-8")
    monkeypatch.setattr(wg.shutil, "which", lambda name: None)
    assert wg.main(["--no-defaults", "--zizmor", str(clean)]) == 2


def test_action_pins_an_exact_zizmor_release():
    import yaml

    action = yaml.safe_load((HERE / "action.yml").read_text(encoding="utf-8"))
    spec = action["inputs"]["zizmor-version"]["default"]
    assert spec.startswith("zizmor==") and "," not in spec and "*" not in spec, spec
    major, minor, *_ = (int(x) for x in spec.removeprefix("zizmor==").split("."))
    assert (major, minor) >= (1, 6), "json-v1 output needs zizmor >= 1.6"
