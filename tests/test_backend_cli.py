"""Smoke tests for the `evi backend` CLI group."""

from __future__ import annotations

from typer.testing import CliRunner

import evi.apps.cli.main as cli_main
import evi.config as config_mod
from evi.backends import registry as R
from evi.config import Config


def _runner(monkeypatch, tmp_path) -> CliRunner:
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(R, "BACKENDS_PATH", tmp_path / "backends.json")
    return CliRunner()


def test_backend_help(monkeypatch, tmp_path):
    r = _runner(monkeypatch, tmp_path).invoke(cli_main.app, ["backend", "--help"])
    assert r.exit_code == 0 and "backend" in r.stdout.lower()


def test_backend_add_list_remove(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    # add from preset (key defaults to an env ref)
    assert runner.invoke(cli_main.app, ["backend", "add", "openai"]).exit_code == 0
    assert R.get_entry("openai").api_key == "env:OPENAI_API_KEY"
    # add a custom local backend
    assert runner.invoke(cli_main.app, ["backend", "add", "local", "--kind", "ollama"]).exit_code == 0
    assert R.get_entry("local").kind == "ollama"
    # list shows both
    out = runner.invoke(cli_main.app, ["backend", "list"])
    assert out.exit_code == 0 and "openai" in out.stdout and "local" in out.stdout
    # duplicate without --overwrite fails
    assert runner.invoke(cli_main.app, ["backend", "add", "openai"]).exit_code == 1
    # remove
    assert runner.invoke(cli_main.app, ["backend", "remove", "openai"]).exit_code == 0
    assert R.get_entry("openai") is None
    # removing a missing one fails
    assert runner.invoke(cli_main.app, ["backend", "remove", "ghost"]).exit_code == 1


def test_backend_use_switches_active(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    runner.invoke(
        cli_main.app,
        ["backend", "add", "grok", "--kind", "openai_compat",
         "--base-url", "https://api.x.ai/v1", "--api-key", "env:XAI_API_KEY"],
    )
    r = runner.invoke(cli_main.app, ["backend", "use", "grok", "--model", "grok-2-latest"])
    assert r.exit_code == 0
    cfg = Config.load()
    assert cfg.llm.base_url == "https://api.x.ai/v1"
    assert cfg.llm.model == "grok-2-latest"
    assert cfg.llm.api_key == "env:XAI_API_KEY"
    # unknown backend → non-zero exit
    assert runner.invoke(cli_main.app, ["backend", "use", "ghost"]).exit_code == 1
