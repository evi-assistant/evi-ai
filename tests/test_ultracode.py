"""Tests for the ultracode pipeline core (model-free)."""

from __future__ import annotations

import pytest

from evi import ultracode as uc
from evi.ultracode import UltraConfig


def _echo_run_one(tag_by_system=None):
    """A fake run_one that echoes a compact, inspectable record of each call:
    'SYS=<first 12 chars of system prompt> | MODE=<mode> | <task first line>'.
    """
    calls = []

    def run_one(system_prompt, task, mode):
        calls.append({"system": system_prompt, "task": task, "mode": mode})
        first = task.splitlines()[0] if task else ""
        return f"[sys={system_prompt[:14]}|mode={mode}] {first}"

    run_one.calls = calls
    return run_one


# ---- stage order + fan-in --------------------------------------------------


def test_stage_order_and_fanin():
    run_one = _echo_run_one()
    res = uc.run_ultracode("build a thing", run_one=run_one, cfg=UltraConfig(breadth=3, rounds=1))
    names = [s.name for s in res.stages]
    assert names == ["decompose", "solve", "solve", "solve",
                     "verify", "verify", "verify", "synthesize"]
    # the synthesize call's task must contain all 3 candidate outputs (fan-in)
    synth_task = run_one.calls[-1]["task"]
    for s in [st for st in res.stages if st.name == "solve"]:
        assert s.output in synth_task
    assert res.answer == res.stages[-1].output


def test_breadth1_rounds0_is_three_stages():
    run_one = _echo_run_one()
    res = uc.run_ultracode("x", run_one=run_one, cfg=UltraConfig(breadth=1, rounds=0))
    assert [s.name for s in res.stages] == ["decompose", "solve", "synthesize"]
    assert [s.label for s in res.stages] == ["plan", "direct", "final"]


# ---- angle selection -------------------------------------------------------


def test_select_angles_first_n():
    assert uc.select_angles(UltraConfig(breadth=2)) == ["direct", "first_principles"]


def test_select_angles_explicit_overrides_breadth():
    cfg = UltraConfig(breadth=5, angles=["alt", "edge_cases"])
    assert uc.select_angles(cfg) == ["alt", "edge_cases"]


def test_select_angles_unknown_raises():
    with pytest.raises(ValueError, match="unknown ultracode angle"):
        uc.select_angles(UltraConfig(angles=["nope"]))


# ---- refine rounds (adversarial loop wiring) -------------------------------


def test_rounds2_refines_with_prior_critique():
    run_one = _echo_run_one()
    res = uc.run_ultracode("task", run_one=run_one, cfg=UltraConfig(breadth=2, rounds=2))
    names = [s.name for s in res.stages]
    # decompose, 2 solve, 2 verify, 2 refine-solve, 2 verify, synthesize
    assert names == ["decompose", "solve", "solve", "verify", "verify",
                     "solve", "solve", "verify", "verify", "synthesize"]
    # a refine solver call must carry its prior critique text into the prompt
    refine_solves = [c for c in run_one.calls
                     if c["system"] == uc.SOLVER_SYSTEM_PROMPT and "reviewer critiqued" in c["task"]]
    assert len(refine_solves) == 2


# ---- prompt builders -------------------------------------------------------


def test_prompt_builders_contain_inputs():
    assert "do X" in uc.decompose_prompt("do X")
    sp = uc.solver_prompt("do X", "be terse", "the plan", "fix the bug")
    assert "do X" in sp and "be terse" in sp and "the plan" in sp and "fix the bug" in sp
    assert "cand text" in uc.critic_prompt("t", "cand text")
    syn = uc.synthesize_prompt("t", ["c1", "c2"], ["crit1", ""])
    assert "c1" in syn and "c2" in syn and "crit1" in syn


# ---- on_stage hook ---------------------------------------------------------


def test_on_stage_hook_called_per_stage():
    seen = []
    run_one = _echo_run_one()
    res = uc.run_ultracode("t", run_one=run_one, cfg=UltraConfig(breadth=2, rounds=1),
                           on_stage=lambda st: seen.append((st.name, st.label)))
    assert len(seen) == len(res.stages)
    assert seen[0] == ("decompose", "plan") and seen[-1] == ("synthesize", "final")


# ---- error tolerance -------------------------------------------------------


def test_errored_stage_does_not_crash_and_reaches_synthesis():
    def run_one(system_prompt, task, mode):
        if system_prompt == uc.SOLVER_SYSTEM_PROMPT and "first_principles" in task.lower() \
                or "Ignore convention" in task:
            return "ERROR: model fell over"
        first = task.splitlines()[0] if task else ""
        return f"ok: {first}"

    res = uc.run_ultracode("t", run_one=run_one, cfg=UltraConfig(breadth=2, rounds=0))
    # pipeline completes; the errored candidate is present among the solves
    solves = [s.output for s in res.stages if s.name == "solve"]
    assert any(o.startswith("ERROR:") for o in solves)
    assert res.answer  # synthesize still produced something


# ---- make_runner (system prompt threaded through the factory) --------------


def test_make_runner_fresh_agent_per_stage_with_system_prompt(monkeypatch):
    built = []

    class FakeAgent:
        def __init__(self, system_prompt):
            self.system_prompt = system_prompt
            self.tools = {"read_file": object()}

        def enable_auto_all(self):
            pass

    def factory(system_prompt):
        a = FakeAgent(system_prompt)
        built.append(a)
        return a

    from evi.headless import HeadlessResult
    monkeypatch.setattr("evi.headless.run_headless",
                        lambda agent, task: HeadlessResult(text=f"ran:{agent.system_prompt[:6]}"))
    monkeypatch.setattr("evi.modes.mode_tools", lambda m: [])

    run_one = uc.make_runner(factory)
    out_default = run_one("SYSTEM-A", "task", uc.NO_TOOLS)
    assert out_default == "ran:SYSTEM" and built[-1].tools == {}  # NO_TOOLS strips tools
    run_one("SYSTEM-B", "task", "code")
    assert built[-1].system_prompt == "SYSTEM-B"  # threaded through construction
    assert len(built) == 2  # a fresh agent per call


# ---- config + tuning -------------------------------------------------------


def test_load_ultra_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    from evi.config import Config

    cfg = Config.load()
    cfg.ultracode.breadth = 4
    cfg.ultracode.angles = ["direct", "alt"]
    cfg.save()
    loaded = uc.load_ultra_config(Config.load())
    assert loaded.breadth == 4 and loaded.angles == ["direct", "alt"]


def test_looks_small_matches_on_number_boundaries():
    # genuinely small → True
    for m in ("qwen2.5:1.5b", "llama3.2:1b", "qwen2.5:3b-instruct-q4_K_M",
              "qwen2.5:0.5b", "phi-3.5-mini", "tiny-small-model"):
        assert uc._looks_small(m), m
    # digit-adjacent sizes that are NOT small (the old substring bug) → False
    for m in ("qwen2.5:14b", "qwen2.5:21b", "command-r:31b", "llama3.1:11b",
              "qwen2:72b", "deepseek-coder:33b", "phi-4", "qwen2.5:13b"):
        assert not uc._looks_small(m), m


def test_default_tuning_keeps_large_models_with_size_in_name():
    base = UltraConfig(breadth=3, rounds=2)
    for m in ("qwen2.5:21b", "command-r:31b", "llama3.1:11b"):
        kept = uc.default_tuning(m, 32768, base)
        assert kept.breadth == 3 and kept.rounds == 2, m


def test_make_runner_stage_exception_becomes_error(monkeypatch):
    class FakeAgent:
        def __init__(self, sp):
            self.tools = {}

        def enable_auto_all(self):
            pass

    def boom(agent, task):
        raise RuntimeError("mid-stream drop")

    monkeypatch.setattr("evi.headless.run_headless", boom)
    monkeypatch.setattr("evi.modes.mode_tools", lambda m: [])
    run_one = uc.make_runner(lambda sp: FakeAgent(sp))
    out = run_one("sys", "task", "code")
    assert out.startswith("ERROR:") and "mid-stream drop" in out  # never raises


def test_pipeline_survives_a_raising_stage_via_make_runner(monkeypatch):
    from evi.headless import HeadlessResult

    class FakeAgent:
        def __init__(self, sp):
            self.tools = {}

        def enable_auto_all(self):
            pass

    def rh(agent, task):
        # the first_principles solver ("Ignore convention…") blows up mid-stream
        if "Ignore convention" in task:
            raise RuntimeError("solver exploded")
        return HeadlessResult(text="ok")

    monkeypatch.setattr("evi.headless.run_headless", rh)
    monkeypatch.setattr("evi.modes.mode_tools", lambda m: [])
    res = uc.run_ultracode("t", run_one=uc.make_runner(lambda sp: FakeAgent(sp)),
                           cfg=UltraConfig(breadth=2, rounds=0))
    assert res.answer  # the run completed instead of crashing
    solves = [s.output for s in res.stages if s.name == "solve"]
    assert any(o.startswith("ERROR:") for o in solves)


def test_default_tuning_downshifts_small_models():
    base = UltraConfig(breadth=3, rounds=2)
    assert uc.default_tuning("phi-3-mini", 8000, base).rounds == 0
    assert uc.default_tuning("qwen2.5:1.5b-instruct", 32000, base).breadth <= 2
    # short context downshifts even a big-named model
    assert uc.default_tuning("qwen2.5:14b", 8000, base).rounds == 0
    # capable model + long context keeps the base tuning
    kept = uc.default_tuning("qwen2.5-coder:14b-instruct-q4_K_M", 32768, base)
    assert kept.breadth == 3 and kept.rounds == 2
