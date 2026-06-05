"""Tests for the lightweight debug logger."""

from __future__ import annotations

import os
import sys
from io import StringIO

import pytest

import evi.debug as debug_mod


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVI_DEBUG", raising=False)
    monkeypatch.setattr(debug_mod, "_ENABLED", None)
    assert debug_mod.is_enabled() is False


def test_env_var_enables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVI_DEBUG", "1")
    monkeypatch.setattr(debug_mod, "_ENABLED", None)
    assert debug_mod.is_enabled() is True


def test_env_var_falsy_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("", "0", "false", "no"):
        monkeypatch.setenv("EVI_DEBUG", value)
        monkeypatch.setattr(debug_mod, "_ENABLED", None)
        assert debug_mod.is_enabled() is False


def test_set_enabled_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVI_DEBUG", raising=False)
    debug_mod.set_enabled(True)
    assert debug_mod.is_enabled() is True
    assert os.environ.get("EVI_DEBUG") == "1"
    debug_mod.set_enabled(False)
    assert debug_mod.is_enabled() is False


def test_dlog_writes_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    debug_mod.set_enabled(True)
    sink = StringIO()
    monkeypatch.setattr(sys, "stderr", sink)
    debug_mod.dlog("test.tag", {"k": "v"})
    out = sink.getvalue()
    assert "test.tag" in out
    assert '"k": "v"' in out
    debug_mod.set_enabled(False)


def test_dlog_silent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    debug_mod.set_enabled(False)
    sink = StringIO()
    monkeypatch.setattr(sys, "stderr", sink)
    debug_mod.dlog("test.tag", {"k": "v"})
    assert sink.getvalue() == ""


def test_dlog_truncates_long_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    debug_mod.set_enabled(True)
    sink = StringIO()
    monkeypatch.setattr(sys, "stderr", sink)
    debug_mod.dlog("big", "x" * 10000, max_len=100)
    out = sink.getvalue()
    assert "…(+" in out
    debug_mod.set_enabled(False)
