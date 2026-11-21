"""`evi lint` warns about allow rules that approve far more than they look:
every tool, run_python, every shell command, a code runner with any
arguments."""

from __future__ import annotations

import pytest

from evi.configlint import lint_auto

RES = "config.toml [auto] rules"


def _broad(rules: list[str]) -> list:
    return [i for i in lint_auto({"rules": rules})
            if i.resource == RES and "name the commands" in i.message]


@pytest.mark.parametrize(("rule", "says"), [
    ("allow *", "every tool"),
    ("allow * *", "every tool"),
    ("allow run_python", "runs any code"),
    ("allow code", "runs any code"),
    ("allow run_command", "every shell command"),
    ("allow run_command *", "every shell command"),
    ("allow shell *", "every shell command"),
    ("allow run_command python*", "'python' with any arguments"),
    ("allow run_command npx *", "'npx' with any arguments"),
    ("allow run_command uv run *", "'uv run' with any arguments"),
    ("allow run_command Bash *", "'bash' with any arguments"),
])
def test_a_broad_allow_rule_is_flagged(rule, says):
    found = _broad([rule])
    assert len(found) == 1, found
    assert found[0].level == "warn"
    assert repr(rule) in found[0].message and says in found[0].message


@pytest.mark.parametrize("rule", [
    "allow run_command python -m pytest*",
    "allow run_command git status*",
    "allow read_file",
    "deny *",
    "deny run_command *",
    "allow run_command npm test",
])
def test_a_narrow_or_deny_rule_is_not(rule):
    assert _broad([rule]) == []


def test_each_broad_rule_gets_its_own_issue():
    assert len(_broad(["allow *", "allow run_command *", "allow read_file"])) == 2
