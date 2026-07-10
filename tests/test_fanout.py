"""Phase-2 multi-backend fan-out: build_agent(backend=…), make_runner pool
round-robin, and fanout_models interleaving."""

from __future__ import annotations


from evi.backends import registry as R


def test_fanout_models_interleaves_backends(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "BACKENDS_PATH", tmp_path / "backends.json")
    R.save_backends(
        [
            R.BackendEntry(name="a", kind="openai_compat", base_url="https://a/v1", fanout=True),
            R.BackendEntry(name="b", kind="ollama", base_url="http://b/v1", fanout=True),
        ],
        R.BACKENDS_PATH,
    )
    monkeypatch.setattr(R, "list_models_for", lambda e: [f"{e.name}1", f"{e.name}2"])
    order = [(m["backend"], m["model"]) for m in R.fanout_models()]
    # interleaved by backend, not all of 'a' before 'b'
    assert order == [("a", "a1"), ("b", "b1"), ("a", "a2"), ("b", "b2")]


def test_make_runner_fanout_round_robins_solve(monkeypatch):
    from evi import ultracode as uc

    calls = []

    class _Ag:
        def __init__(self):
            self.tools = {}

        def enable_auto_all(self):
            pass

    def factory(sp, model=None, backend=None):
        calls.append((model, backend))
        return _Ag()

    import evi.headless as hl
    monkeypatch.setattr(hl, "run_headless",
                        lambda agent, task: type("R", (), {"text": "ok", "error": ""})())

    pool = [{"backend": "a", "model": "m1"}, {"backend": "b", "model": "m2"}]
    run_one = uc.make_runner(factory, fanout_pool=pool)

    for _ in range(4):
        run_one("sp", "task", uc.NO_TOOLS, "solve")  # NO_TOOLS avoids real mode_tools
    assert calls == [("m1", "a"), ("m2", "b"), ("m1", "a"), ("m2", "b")]

    calls.clear()
    run_one("sp", "task", uc.NO_TOOLS, "decompose")  # non-solve → default, no backend
    assert calls == [(None, None)]


def test_make_runner_no_pool_uses_stage_models(monkeypatch):
    from evi import ultracode as uc

    calls = []

    class _Ag:
        def __init__(self):
            self.tools = {}

        def enable_auto_all(self):
            pass

    import evi.headless as hl
    monkeypatch.setattr(hl, "run_headless",
                        lambda agent, task: type("R", (), {"text": "ok", "error": ""})())
    run_one = uc.make_runner(
        lambda sp, model=None, backend=None: (calls.append((model, backend)) or _Ag()),
        stage_models={"solve": "fast-model"},
    )
    run_one("sp", "task", uc.NO_TOOLS, "solve")
    assert calls == [("fast-model", None)]


def test_build_agent_backend_binds_config(tmp_path, monkeypatch):
    import evi.config as config_mod
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")

    from evi.sdk.builder import build_agent

    entry = R.BackendEntry(name="grok", kind="openai_compat",
                           base_url="https://api.x.ai/v1", api_key="sk-x")
    agent = build_agent(
        backend=entry, model="grok-2-latest",
        enable_project=False, enable_hooks=False, enable_guardrails=False,
    )
    assert agent.config.llm.backend == "openai_compat"
    assert agent.config.llm.base_url == "https://api.x.ai/v1"
    assert agent.config.llm.model == "grok-2-latest"
    # a client was built for the entry (not the default backend)
    assert agent.client is not None
