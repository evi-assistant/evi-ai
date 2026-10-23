"""A config.toml that is there but can't be read or parsed: eVi starts on the
default settings with a warning, and never writes over the file (a typo or a
half-finished edit must not cost the user every setting)."""

from __future__ import annotations

import logging

import pytest
from typer.testing import CliRunner

import evi.config as C
from evi.config import Config, ConfigUnreadable, config_error

BROKEN = '[llm\nmodel = "mine"\n[auto]\nhard_deny = ["shell rm*"]\n'


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(C, "CONFIG_PATH", path)
    monkeypatch.setattr(C, "_WARNED_LOAD", set())
    return path


def test_an_unparseable_file_loads_the_defaults_and_says_why(cfg_path, caplog):
    cfg_path.write_text(BROKEN, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="evi.config"):
        cfg = Config.load()
        Config.load()  # warned once per process, not per load
    assert cfg.load_error and cfg.load_error.startswith("TOMLDecodeError")
    assert cfg.llm.model == Config().llm.model  # the default, not "mine"
    warnings = [r for r in caplog.records if "couldn't be read" in r.getMessage()]
    assert len(warnings) == 1 and "default settings" in warnings[0].getMessage()
    assert config_error() == cfg.load_error
    assert cfg_path.read_text(encoding="utf-8") == BROKEN  # untouched


def test_bad_encoding_is_handled_the_same_way(cfg_path):
    cfg_path.write_bytes(b'[llm]\nmodel = "\xff\xfe"\n')
    cfg = Config.load()
    assert cfg.load_error and config_error()


def test_saving_never_writes_over_an_unreadable_file(cfg_path):
    cfg_path.write_text(BROKEN, encoding="utf-8")
    cfg = Config.load()
    cfg.llm.temperature = 0.11
    with pytest.raises(ConfigUnreadable, match="won't write over it"):
        cfg.save()
    assert cfg_path.read_text(encoding="utf-8") == BROKEN


def test_a_file_that_breaks_after_a_good_load_is_not_overwritten_either(cfg_path):
    cfg_path.write_text('[llm]\nmodel = "mine"\n', encoding="utf-8")
    cfg = Config.load()
    assert cfg.load_error is None and cfg.llm.model == "mine"
    half_edit = '[llm]\nmodel = "mine"\ntemperature = \n'  # the user mid-edit
    cfg_path.write_text(half_edit, encoding="utf-8")
    cfg.llm.top_p = 0.5
    with pytest.raises(ConfigUnreadable):
        cfg.save()
    assert cfg_path.read_text(encoding="utf-8") == half_edit


def test_once_fixed_it_loads_and_saves_as_usual(cfg_path):
    cfg_path.write_text(BROKEN, encoding="utf-8")
    assert Config.load().load_error
    cfg_path.write_text('[llm]\nmodel = "mine"\n', encoding="utf-8")
    cfg = Config.load()
    assert cfg.load_error is None and cfg.llm.model == "mine" and config_error() is None
    cfg.llm.temperature = 0.2
    cfg.save()
    assert Config.load().llm.temperature == 0.2


def test_a_missing_file_is_still_created_from_the_defaults(cfg_path):
    assert config_error() is None
    cfg = Config.load()
    assert cfg.load_error is None and cfg_path.exists()


def test_the_cli_warns_and_turns_a_refused_save_into_one_line(cfg_path, monkeypatch, capsys):
    import sys

    import evi.apps.cli.main as cli_main

    cfg_path.write_text(BROKEN, encoding="utf-8")
    res = CliRunner().invoke(cli_main.app, ["config", "path"])
    assert "couldn't be read" in res.stderr and "default settings" in res.stderr
    # A save refused later in the command: one line on stderr, no traceback,
    # and the previous hook (Typer's / crash reporting) isn't called for it.
    called = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: called.append(a))
    cli_main._warn_unreadable_config()
    capsys.readouterr()
    err = ConfigUnreadable("config.toml can't be read, so the change wasn't saved")
    sys.excepthook(ConfigUnreadable, err, None)
    out = capsys.readouterr().err
    assert "error:" in out and "wasn't saved" in out and "Traceback" not in out
    assert called == []
    other = ValueError("boom")
    sys.excepthook(ValueError, other, None)
    assert called and called[0][1] is other
