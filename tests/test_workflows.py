"""Tests for dynamic workflows (Phase 86)."""

from __future__ import annotations

import pytest

from evi import workflows
from evi.workflows import WStep, Workflow


def _wf(steps, variables=None):
    return Workflow(name="t", steps=steps, vars=variables or {})


def _echo_step(prompt, step):
    # Deterministic: the "output" is the (already-interpolated) prompt, tagged.
    return f"{step.id}:{prompt}"


# ---- loading -------------------------------------------------------------


def test_load_workflow_file(tmp_path):
    p = tmp_path / "w.toml"
    p.write_text(
        'name = "demo"\ndescription = "d"\n[vars]\ntopic = "AI"\n'
        '[[steps]]\nid = "plan"\nprompt = "plan {topic}"\n'
        '[[steps]]\nid = "a"\nparallel = true\nprompt = "do {plan}"\n',
        encoding="utf-8",
    )
    w = workflows.load_workflow_file(p)
    assert w.name == "demo" and w.vars == {"topic": "AI"}
    assert [s.id for s in w.steps] == ["plan", "a"]
    assert w.steps[1].parallel is True


def test_load_rejects_duplicate_ids(tmp_path):
    p = tmp_path / "w.toml"
    p.write_text(
        '[[steps]]\nid = "x"\nprompt = "a"\n[[steps]]\nid = "x"\nprompt = "b"\n',
        encoding="utf-8",
    )
    with pytest.raises(workflows.WorkflowError):
        workflows.load_workflow_file(p)


def test_load_rejects_empty_prompt(tmp_path):
    p = tmp_path / "w.toml"
    p.write_text('[[steps]]\nid = "x"\nprompt = "  "\n', encoding="utf-8")
    with pytest.raises(workflows.WorkflowError):
        workflows.load_workflow_file(p)


def test_create_and_reload(tmp_path):
    path = workflows.create_workflow("demo", root=tmp_path)
    assert path.is_file()
    w = workflows.load_workflow("demo", root=tmp_path)
    assert w.name == "demo" and len(w.steps) >= 2
    with pytest.raises(workflows.WorkflowError):
        workflows.create_workflow("demo", root=tmp_path)  # exists


# ---- execution -----------------------------------------------------------


def test_sequential_interpolation():
    wf = _wf([
        WStep(id="a", prompt="hello {name}"),
        WStep(id="b", prompt="prev was: {a}"),
    ], variables={"name": "world"})
    out = workflows.run_workflow(wf, run_step=_echo_step)
    assert out["a"] == "a:hello world"
    assert out["b"] == "b:prev was: a:hello world"


def test_run_variables_override_vars():
    wf = _wf([WStep(id="a", prompt="{topic}")], variables={"topic": "default"})
    out = workflows.run_workflow(wf, run_step=_echo_step, variables={"topic": "override"})
    assert out["a"] == "a:override"


def test_parallel_block_then_fan_in():
    wf = _wf([
        WStep(id="plan", prompt="plan it"),
        WStep(id="pros", parallel=True, prompt="pros given {plan}"),
        WStep(id="cons", parallel=True, prompt="cons given {plan}"),
        WStep(id="synth", prompt="{pros} || {cons}"),
    ])
    out = workflows.run_workflow(wf, run_step=_echo_step)
    assert out["pros"] == "pros:pros given plan:plan it"
    assert out["cons"] == "cons:cons given plan:plan it"
    # fan-in sees both parallel outputs
    assert "pros:pros given" in out["synth"] and "cons:cons given" in out["synth"]


def test_parallel_step_cannot_see_sibling():
    # cons references pros, but they're in the same parallel block → unknown ref.
    wf = _wf([
        WStep(id="pros", parallel=True, prompt="ok"),
        WStep(id="cons", parallel=True, prompt="needs {pros}"),
    ])
    with pytest.raises(workflows.WorkflowError):
        workflows.run_workflow(wf, run_step=_echo_step)


def test_unknown_reference_errors():
    wf = _wf([WStep(id="a", prompt="{missing}")])
    with pytest.raises(workflows.WorkflowError):
        workflows.run_workflow(wf, run_step=_echo_step)


def test_literal_braces_need_escaping():
    wf = _wf([WStep(id="a", prompt="json {{}}")])
    out = workflows.run_workflow(wf, run_step=_echo_step)
    assert out["a"] == "a:json {}"


# ---- step results ----------------------------------------------------------


def _result(text="", error=None):
    from types import SimpleNamespace

    return SimpleNamespace(text=text, error=error)


def test_step_output_fails_a_run_that_ended_in_an_error():
    assert workflows.step_output(_result("the answer")) == "the answer"
    assert workflows.step_output(_result("", "LLM request failed: boom")) == (
        "ERROR: LLM request failed: boom"
    )
    # Partial text before the error is not the step's answer.
    out = workflows.step_output(_result("half an ans", "LLM stream failed mid-reply: x"))
    assert out.startswith("ERROR: LLM stream failed mid-reply: x\n")
    assert out.endswith("(incomplete reply before the error)\nhalf an ans")
    assert workflows.step_output(_result()) == ""


def test_run_workflow_reports_a_step_whose_run_failed():
    wf = _wf([WStep(id="a", prompt="go"), WStep(id="b", prompt="after {a}"),
              WStep(id="c", prompt="x", parallel=True), WStep(id="d", prompt="y", parallel=True)])
    results = {
        "a": _result("partial", "LLM stream failed mid-reply: reset"),
        "c": _result("fine"),
        "d": _result("", "LLM request failed: 500"),
    }
    out = workflows.run_workflow(
        wf, run_step=lambda prompt, step: results.get(step.id) or f"{step.id}:{prompt}"
    )
    assert out["a"].startswith("ERROR: LLM stream failed mid-reply: reset")
    assert out["b"] == f"b:after {out['a']}"  # a plain string still passes through
    assert out["c"] == "fine"
    assert workflows.failed_steps(out) == ["a", "d"]
