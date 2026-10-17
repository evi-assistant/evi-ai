"""Tests for the evi-run composite action: its config writer
(evi_run_config.py) and its run report outputs (evi_run_outputs.py).

The action runs prompts that may carry untrusted text, so the config it writes
must approve no tool category: eVi's default auto-approve list (fs, code,
memory, skills, image) would let the model run Python and read or write any
file, the API key in this very config included, even without `--yes`.

Run with `python -m pytest .github/actions/evi-run` (ci.yml runs it in the main
test job).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location("evi_run_config", HERE / "evi_run_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cfgw = _load()

BACKEND = {
    "EVI_CFG_BACKEND": "openai_compat",
    "EVI_CFG_BASE_URL": "https://llm.example.invalid/v1",
    "EVI_CFG_API_KEY": "test-key-not-real",
    "EVI_CFG_MODEL": "some-model",
}


@pytest.fixture
def backend_env(monkeypatch, tmp_path):
    for key, value in BACKEND.items():
        monkeypatch.setenv(key, value)
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    return output


def _written(home: Path) -> dict:
    with (home / "config.toml").open("rb") as f:
        return tomllib.load(f)


def test_written_config_approves_no_tool_category(tmp_path, backend_env):
    home = tmp_path / "evi-ci"
    assert cfgw.main([str(home)]) == 0
    data = _written(home)
    assert data["auto"] == {"auto_approve": []}
    assert data["llm"] == {
        "backend": "openai_compat",
        "base_url": "https://llm.example.invalid/v1",
        "api_key": "test-key-not-real",
        "model": "some-model",
    }
    assert backend_env.read_text(encoding="utf-8") == f"home={home}\n"


def test_without_a_backend_nothing_is_written(tmp_path, monkeypatch, backend_env):
    for key in BACKEND:
        monkeypatch.delenv(key)
    home = tmp_path / "evi-ci"
    assert cfgw.main([str(home)]) == 0
    assert not home.exists()
    assert backend_env.read_text(encoding="utf-8") == ""  # the caller's EVI_HOME applies


def test_the_script_runs_the_way_the_action_calls_it(tmp_path):
    home = tmp_path / "runner-temp" / "evi-ci"
    output = tmp_path / "github_output"
    proc = subprocess.run(
        [sys.executable, str(HERE / "evi_run_config.py"), str(home)],
        env={**os.environ, **BACKEND, "GITHUB_OUTPUT": str(output)},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert _written(home)["auto"]["auto_approve"] == []
    if os.name == "posix":
        assert (home / "config.toml").stat().st_mode & 0o077 == 0  # owner-only


@pytest.mark.parametrize("value", [
    'quote " and backslash \\ end',
    "new\nline and\ttab",
    "nul\x00 del\x7f esc\x1b",
    "unicodé ✓ 🚀",
])
def test_toml_strings_round_trip(value):
    assert tomllib.loads(f"v = {cfgw.toml_string(value)}\n")["v"] == value


def test_a_newline_in_an_input_cannot_add_config(tmp_path, backend_env, monkeypatch):
    monkeypatch.setenv("EVI_CFG_MODEL", 'm"\n[auto]\nauto_approve = ["shell"]')
    home = tmp_path / "evi-ci"
    assert cfgw.main([str(home)]) == 0
    data = _written(home)
    assert data["auto"]["auto_approve"] == []
    assert data["llm"]["model"] == 'm"\n[auto]\nauto_approve = ["shell"]'


def test_evi_loads_the_config_and_its_tools_need_approval(tmp_path, backend_env, monkeypatch):
    """What `evi run` (without --yes) sees: no category pre-approved, so
    run_python and file reads/writes are not allowed, where eVi's default
    config allows them."""
    config_mod = pytest.importorskip("evi.config")
    from evi import permissions, profiles, safemode

    home = tmp_path / "evi-ci"
    assert cfgw.main([str(home)]) == 0
    monkeypatch.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    monkeypatch.delenv(profiles.ENV_VAR, raising=False)
    monkeypatch.delenv(safemode.ENV_VAR, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)  # no project .evi.toml overlay

    cfg = config_mod.Config.load()
    assert cfg.llm.api_key == "test-key-not-real"
    assert cfg.auto.auto_approve == []

    calls = [
        ("run_python", "code", {"code": "print(open('config.toml').read())"}),
        ("read_file", "fs", {"path": str(home / "config.toml")}),
        ("write_file", "fs", {"path": str(workspace / "x.py"), "content": "x"}),
    ]

    def verdict(auto, name, category, args):
        return permissions.decide(
            auto.mode, auto.auto_approve, auto.rules, name, category, args,
            auto.trusted_dirs, auto.trusted_domains, auto.hard_deny, auto.protected_paths,
            workdir=str(workspace),
        )

    for name, category, args in calls:
        assert verdict(cfg.auto, name, category, args) != "allow", name
    # eVi's stock policy would have allowed the workspace write (fs is
    # auto-approved): the hole this config closes. Running code no longer rides
    # the stock defaults at all (auto_approve has code_read, not code).
    stock = config_mod.AutoSettings()
    assert verdict(stock, *calls[2]) == "allow"
    assert verdict(stock, *calls[0]) != "allow"


def test_the_action_writes_its_config_with_the_helper():
    yaml = pytest.importorskip("yaml")
    action = yaml.safe_load((HERE / "action.yml").read_text(encoding="utf-8"))
    (step,) = [s for s in action["runs"]["steps"] if s.get("id") == "config"]
    assert "evi_run_config.py" in step["run"]
    assert set(step["env"]) == {env for _, env in cfgw.ENV_KEYS}
    run = next(s for s in action["runs"]["steps"] if s.get("id") == "run")
    assert run["env"]["EVI_CI_HOME"] == "${{ steps.config.outputs.home }}"


# ---- the run report outputs (evi_run_outputs.py) ----------------------------------


def _load_outputs():
    spec = importlib.util.spec_from_file_location("evi_run_outputs", HERE / "evi_run_outputs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


outw = _load_outputs()

REPORT = {"schema_version": 1, "session_id": "a1b2c3d4e5f6", "stop_reason": "max_turns",
          "text": "partial", "tool_calls": []}


def test_outputs_come_from_the_json_report():
    assert outw.outputs("json", "3", json.dumps(REPORT)) == {
        "exit_code": "3", "stop_reason": "max_turns", "session_id": "a1b2c3d4e5f6"}


def test_outputs_come_from_the_stream_json_result_line():
    stdout = "\n".join([
        json.dumps({"kind": "RunStart", "seq": 1, "session_id": "nope"}),
        json.dumps({"kind": "TextDelta", "seq": 2, "text": '{"stop_reason": "done"}'}),
        json.dumps({"kind": "RunResult", "seq": 3, **REPORT}),
    ])
    assert outw.outputs("stream-json", "3", stdout)["stop_reason"] == "max_turns"
    # A last line that is not eVi's RunResult (the run died mid-stream) says nothing.
    cut = stdout.rsplit("\n", 1)[0]
    assert outw.outputs("stream-json", "1", cut) == {
        "exit_code": "1", "stop_reason": "", "session_id": ""}


def test_text_format_has_only_the_exit_code():
    assert outw.outputs("text", "0", json.dumps(REPORT)) == {
        "exit_code": "0", "stop_reason": "", "session_id": ""}


@pytest.mark.parametrize("report", [
    {"stop_reason": "done\nresult=forged", "session_id": "x"},
    {"stop_reason": "anything", "session_id": "x\nresult=forged"},
    {"stop_reason": ["done"], "session_id": 7},
])
def test_values_evi_would_not_write_are_left_out(report):
    out = outw.outputs("json", "0", json.dumps(report))
    assert all("\n" not in v and "forged" not in v for v in out.values())
    assert out["stop_reason"] in ("", "done") and out["session_id"] in ("", "x")


def test_main_appends_the_outputs(tmp_path, monkeypatch):
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    proc = subprocess.run(
        [sys.executable, str(HERE / "evi_run_outputs.py"), "json", "4"],
        input=json.dumps({**REPORT, "stop_reason": "permission_denied"}).encode(),
        env={**os.environ, "GITHUB_OUTPUT": str(output)}, capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert output.read_text(encoding="utf-8").splitlines() == [
        "exit_code=4", "stop_reason=permission_denied", "session_id=a1b2c3d4e5f6"]


def test_stop_reasons_match_evi():
    events = pytest.importorskip("evi.events")
    assert outw.STOP_REASONS == events.STOP_REASONS


def _run_step(action: dict) -> dict:
    return next(s for s in action["runs"]["steps"] if s.get("id") == "run")


def test_the_action_exposes_the_report_and_fails_with_evis_exit_code():
    yaml = pytest.importorskip("yaml")
    action = yaml.safe_load((HERE / "action.yml").read_text(encoding="utf-8"))
    for name in ("exit_code", "stop_reason", "session_id"):
        assert action["outputs"][name]["value"] == f"${{{{ steps.run.outputs.{name} }}}}"
    run = _run_step(action)
    assert run["env"]["EVI_MAX_TURNS"] == "${{ inputs.max_turns }}"  # inputs via env:
    script = run["run"]
    assert script.count("|| code=$?") == 2
    assert "evi_run_outputs.py" in script
    assert script.rstrip().endswith('exit "$code"')
    assert "${{" not in script  # nothing interpolated into the script


def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None or "system32" in bash.lower():  # not WSL's launcher
        pytest.skip("no bash")
    return bash


def test_the_run_step_writes_the_outputs_then_exits_with_the_code(tmp_path):
    """The step's script, as the runner runs it (bash -eo pipefail), with a
    stand-in `evi` that reports max_turns and exits 3."""
    yaml = pytest.importorskip("yaml")
    bash = _bash()
    action = yaml.safe_load((HERE / "action.yml").read_text(encoding="utf-8"))
    script = tmp_path / "step.sh"
    script.write_text(_run_step(action)["run"], encoding="utf-8", newline="\n")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    argv_log = tmp_path / "argv"
    report = json.dumps({**REPORT, "text": "::error::forged"})
    (fakebin / "evi").write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\n' \"$@\" > '{argv_log.as_posix()}'\n"
        f"printf '%s\n' '{report}'\n"
        "exit 3\n", encoding="utf-8", newline="\n")
    (fakebin / "python").write_text(
        f"#!/usr/bin/env bash\nexec '{Path(sys.executable).as_posix()}' \"$@\"\n",
        encoding="utf-8", newline="\n")
    for f in fakebin.iterdir():
        f.chmod(0o755)
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),  # (MSYS converts it)
        "EVI_PROMPT": "go", "EVI_PROMPT_FILE": "", "EVI_FORMAT": "json", "EVI_MAX_TURNS": "2",
        "EVI_MODE": "", "EVI_AUTO_APPROVE": "false", "EVI_CI_HOME": "",
        "GITHUB_OUTPUT": str(output), "GITHUB_ACTION_PATH": str(HERE),
    }
    proc = subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert argv_log.read_text(encoding="utf-8").split() == [
        "run", "--format", "json", "--max-turns", "2", "--", "go"]
    written = output.read_text(encoding="utf-8")
    assert "exit_code=3\nstop_reason=max_turns\nsession_id=a1b2c3d4e5f6\n" in written
    assert written.startswith("result<<EVI_EOF_") and "::error::forged" in written
