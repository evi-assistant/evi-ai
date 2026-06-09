"""Tests for the content-filter guardrails — regex + the LLM-judge layer."""

from __future__ import annotations

from evi.guardrails import GuardrailRule, Guardrails, JudgeRule


# ---- regex layer ---------------------------------------------------------


def _g(rules, judge_rules=None):
    return Guardrails(rules, judge_rules=judge_rules, enabled=True)


def test_regex_block_input():
    g = _g([GuardrailRule(name="secret", pattern=r"api_key", action="block", applies_to="input")])
    res = g.check("here is my api_key=123", "input")
    assert not res.allowed and res.blocked_by == ["secret"]


def test_regex_redact():
    g = _g([GuardrailRule(name="email", pattern=r"\S+@\S+", action="redact", applies_to="both")])
    res = g.check("mail me at a@b.com please", "output")
    assert res.allowed and res.redacted_by == ["email"]
    assert "[REDACTED]" in res.text and "a@b.com" not in res.text


def test_direction_scoping():
    g = _g([GuardrailRule(name="x", pattern=r"foo", action="block", applies_to="output")])
    assert g.check("foo", "input").allowed       # input not covered
    assert not g.check("foo", "output").allowed  # output covered


def test_disabled_passes_through():
    g = Guardrails([GuardrailRule(name="x", pattern="foo", action="block")], enabled=False)
    assert g.check("foo", "input").allowed


# ---- LLM-judge layer -----------------------------------------------------


def test_judge_blocks():
    g = _g([], judge_rules=[JudgeRule(name="harm", policy="self-harm", applies_to="input")])
    res = g.check("bad text", "input", judge_fn=lambda policy, text: (False, "matches self-harm"))
    assert not res.allowed and res.blocked_by == ["harm"]
    assert res.notes and "self-harm" in res.notes[0]


def test_judge_allows():
    g = _g([], judge_rules=[JudgeRule(name="harm", policy="self-harm")])
    res = g.check("hello", "input", judge_fn=lambda p, t: (True, "fine"))
    assert res.allowed and not res.blocked_by


def test_judge_fail_open_on_error():
    def boom(policy, text):
        raise RuntimeError("model down")

    g = _g([], judge_rules=[JudgeRule(name="harm", policy="x")])
    res = g.check("anything", "input", judge_fn=boom)
    assert res.allowed  # a broken grader can't wedge the turn


def test_judge_not_run_without_fn():
    g = _g([], judge_rules=[JudgeRule(name="harm", policy="x")])
    # No judge_fn supplied -> judge rules are inert (regex-only behaviour).
    assert g.check("anything", "input").allowed


def test_judge_skipped_when_regex_already_blocked():
    calls = []
    g = _g(
        [GuardrailRule(name="re", pattern="bad", action="block")],
        judge_rules=[JudgeRule(name="j", policy="x")],
    )
    res = g.check("bad", "input", judge_fn=lambda p, t: (calls.append(1), (False, "j"))[1])
    assert res.blocked_by == ["re"] and calls == []  # judge not consulted


def test_judge_direction_scoping():
    g = _g([], judge_rules=[JudgeRule(name="j", policy="x", applies_to="output")])
    # input not covered -> judge not consulted even with a blocking fn
    assert g.check("t", "input", judge_fn=lambda p, x: (False, "no")).allowed
    assert not g.check("t", "output", judge_fn=lambda p, x: (False, "no")).allowed


# ---- loading -------------------------------------------------------------


def test_load_regex_and_judge(tmp_path):
    p = tmp_path / "guardrails.toml"
    p.write_text(
        'enabled = true\n'
        '[[rule]]\nname = "k"\npattern = "api_key"\naction = "block"\n'
        '[[judge]]\nname = "harm"\npolicy = "self-harm or violence"\napplies_to = "both"\n',
        encoding="utf-8",
    )
    g = Guardrails.load(p)
    assert g.enabled
    assert [r.name for r in g.rules] == ["k"]
    assert [j.name for j in g.judge_rules] == ["harm"]
    assert g.judge_rules[0].policy.startswith("self-harm")


def test_load_judge_only_enables(tmp_path):
    p = tmp_path / "g.toml"
    p.write_text('[[judge]]\nname = "h"\npolicy = "bad stuff"\n', encoding="utf-8")
    g = Guardrails.load(p)
    assert g.enabled and not g.rules and len(g.judge_rules) == 1
