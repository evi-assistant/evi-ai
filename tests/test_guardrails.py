"""Tests for the content-filter guardrails — regex + the LLM-judge layer."""

from __future__ import annotations

from evi.guardrails import ClassifierRule, GuardrailRule, Guardrails, JudgeRule


# ---- regex layer ---------------------------------------------------------


def _g(rules, judge_rules=None, classifier_rules=None):
    return Guardrails(rules, judge_rules=judge_rules,
                      classifier_rules=classifier_rules, enabled=True)


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


# ---- offline-classifier layer --------------------------------------------


def test_classifier_blocks_over_threshold():
    g = _g([], classifier_rules=[ClassifierRule(name="tox", labels=["toxic"], threshold=0.7)])
    res = g.check("x", "input", classify_fn=lambda model, text: {"toxic": 0.9, "insult": 0.1})
    assert not res.allowed and res.blocked_by == ["tox"]
    assert res.notes and "toxic" in res.notes[0]


def test_classifier_allows_under_threshold():
    g = _g([], classifier_rules=[ClassifierRule(name="tox", labels=["toxic"], threshold=0.7)])
    res = g.check("x", "input", classify_fn=lambda model, text: {"toxic": 0.3})
    assert res.allowed


def test_classifier_label_filter():
    # high score on a non-listed label must NOT block
    g = _g([], classifier_rules=[ClassifierRule(name="tox", labels=["threat"], threshold=0.5)])
    res = g.check("x", "input", classify_fn=lambda m, t: {"toxic": 0.99, "threat": 0.01})
    assert res.allowed


def test_classifier_any_label_when_unset():
    g = _g([], classifier_rules=[ClassifierRule(name="any", labels=[], threshold=0.6)])
    res = g.check("x", "input", classify_fn=lambda m, t: {"obscene": 0.8})
    assert not res.allowed


def test_classifier_fail_open_on_error():
    def boom(model, text):
        raise RuntimeError("no transformers")

    g = _g([], classifier_rules=[ClassifierRule(name="tox", threshold=0.5)])
    assert g.check("x", "input", classify_fn=boom).allowed


def test_classifier_inert_without_fn():
    g = _g([], classifier_rules=[ClassifierRule(name="tox", threshold=0.1)])
    assert g.check("anything", "input").allowed


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


def test_load_classifier(tmp_path):
    p = tmp_path / "g.toml"
    p.write_text(
        '[[classifier]]\nname = "tox"\nmodel = "unitary/toxic-bert"\n'
        'labels = ["toxic", "threat"]\nthreshold = 0.8\napplies_to = "input"\n',
        encoding="utf-8",
    )
    g = Guardrails.load(p)
    assert g.enabled and len(g.classifier_rules) == 1
    cr = g.classifier_rules[0]
    assert cr.model == "unitary/toxic-bert" and cr.labels == ["toxic", "threat"]
    assert cr.threshold == 0.8 and cr.applies_to == "input"
