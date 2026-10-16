"""Tests for the evi-run composite action's config writer (evi_run_config.py).

The action runs prompts that may carry untrusted text, so the config it writes
must approve no tool category: eVi's default auto-approve list (fs, code,
memory, skills, image) would let the model run Python and read or write any
file, the API key in this very config included, even without `--yes`.

Run with `python -m pytest .github/actions/evi-run` (ci.yml runs it in the main
test job).
"""

from __future__ import annotations

import importlib.util
import os
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
