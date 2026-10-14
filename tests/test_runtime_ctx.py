"""Managed llama.cpp context window: the server's -c is sized to the machine
and written into llm.context_size, so the agent's compaction ceiling is the
window the server actually holds.

No network, no subprocess: hardware.detect, the runtime download and
subprocess.Popen are stubbed; config lives in tmp_path, and a directory made
outside it fails the test.
"""

from __future__ import annotations

import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from evi.hardware import GPU, HardwareInfo
from evi.runtime import catalog as cat
from evi.runtime import llama_server, llamacpp_runtime
from evi.runtime import setup as rt_setup

GIB = 1024 ** 3


def _hw(ram_gb: float, vram_gb: tuple[float, ...] = ()) -> HardwareInfo:
    gpus = [GPU(name=f"gpu{i}", vram_total_mb=int(v * 1024)) for i, v in enumerate(vram_gb)]
    return HardwareInfo(gpus=gpus, ram_total_bytes=int(ram_gb * GIB), platform="windows")


def _stub_hw(monkeypatch, ram_gb: float, vram_gb: tuple[float, ...] = ()) -> None:
    """Stub the real probe (hardware.detect), not catalog._memory_gb, so the
    attribute names _memory_gb reads are exercised too."""
    import evi.hardware

    monkeypatch.setattr(evi.hardware, "detect", lambda: _hw(ram_gb, vram_gb))


# ---- RAM probe + recommender ----------------------------------------------


def test_memory_gb_reads_ram_total_and_largest_gpu(monkeypatch):
    _stub_hw(monkeypatch, 32)
    assert cat._memory_gb() == pytest.approx((32.0, 0.0))
    # Largest card wins, whatever order the GPUs come in.
    _stub_hw(monkeypatch, 16, vram_gb=(2.0, 16.0))
    assert cat._memory_gb() == pytest.approx((16.0, 16.0))


def test_memory_gb_falls_back_when_unknown(monkeypatch):
    import evi.hardware

    _stub_hw(monkeypatch, 0)  # RAM couldn't be read
    assert cat._memory_gb() == pytest.approx((8.0, 0.0))

    def boom():
        raise OSError("no probe")

    monkeypatch.setattr(evi.hardware, "detect", boom)
    assert cat._memory_gb() == pytest.approx((8.0, 0.0))


def test_recommender_on_32gb_cpu_box_skips_the_smallest_tier(monkeypatch):
    _stub_hw(monkeypatch, 32)
    generals = [m for m in cat.catalog() if "coding" not in m.get("tags", ())]
    smallest = min(m["params_b"] for m in generals)
    pick = cat.get(cat.recommended_id())
    assert pick["params_b"] > smallest
    assert pick["id"] == "qwen2.5-14b-instruct"  # 22 GB of headroom fits the largest


def test_recommender_scales_with_real_ram(monkeypatch):
    _stub_hw(monkeypatch, 8)
    small = cat.get(cat.recommended_id())
    _stub_hw(monkeypatch, 32)
    big = cat.get(cat.recommended_id())
    assert big["params_b"] > small["params_b"]  # the old probe saw 8 GB for both


def test_recommender_uses_gpu_vram(monkeypatch):
    _stub_hw(monkeypatch, 8, vram_gb=(16.0,))
    assert cat.recommended_id() == "qwen2.5-14b-instruct"  # min_vram_gb 11 <= 16


# ---- context budget --------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "ram", "vram", "on_gpu", "want"),
    [
        ("qwen2.5-1.5b-instruct", 8, 0, False, 32768),  # starter on an 8 GB laptop
        ("qwen2.5-1.5b-instruct", 4, 0, False, 16384),  # 32K's KV doesn't fit 4 GB
        ("qwen2.5-7b-instruct", 8, 0, False, 8192),     # weights fill RAM: floor
        ("qwen2.5-7b-instruct", 16, 0, False, 16384),
        ("qwen2.5-7b-instruct", 32, 0, False, 32768),
        ("qwen2.5-14b-instruct", 64, 16, True, 8192),   # VRAM, not RAM, bounds it
        ("qwen2.5-14b-instruct", 8, 20, True, 16384),
        ("qwen2.5-7b-instruct", 8, 24, False, 8192),    # on CPU, VRAM is no help
        ("qwen2.5-7b-instruct", 32, 0, True, 32768),    # Metal: no VRAM -> RAM
    ],
)
def test_context_budget_table(monkeypatch, model_id, ram, vram, on_gpu, want):
    monkeypatch.setattr(cat, "_memory_gb", lambda: (float(ram), float(vram)))
    assert cat.context_budget(cat.get(model_id), on_gpu=on_gpu) == want


def test_context_budget_respects_native_window(monkeypatch):
    monkeypatch.setattr(cat, "_memory_gb", lambda: (256.0, 0.0))
    entry = cat.get("qwen2.5-1.5b-instruct")
    assert cat.context_budget(entry) == 32768
    assert cat.context_budget({**entry, "native_ctx": 16384}) == 16384
    assert cat.context_budget({**entry, "native_ctx": 4096}) == 4096  # never past it


def test_context_budget_honours_a_pinned_kv_size(monkeypatch):
    monkeypatch.setattr(cat, "_memory_gb", lambda: (16.0, 0.0))
    entry = cat.get("qwen2.5-7b-instruct")
    assert cat.context_budget(entry) == 16384                          # estimate
    assert cat.context_budget({**entry, "kv_kib_per_token": 56}) == 32768  # exact


# f16 K+V bytes per token, from each model's config: 2 * layers * kv_heads *
# head_dim * 2 bytes.
_REAL_KV_KIB = {
    "qwen2.5-0.5b-instruct": 2 * 24 * 2 * 64 * 2 / 1024,     # 12
    "qwen2.5-1.5b-instruct": 2 * 28 * 2 * 128 * 2 / 1024,    # 28
    "qwen2.5-3b-instruct": 2 * 36 * 2 * 128 * 2 / 1024,      # 36
    "qwen2.5-7b-instruct": 2 * 28 * 4 * 128 * 2 / 1024,      # 56
    "qwen2.5-14b-instruct": 2 * 48 * 8 * 128 * 2 / 1024,     # 192
    "qwen2.5-coder-7b-instruct": 2 * 28 * 4 * 128 * 2 / 1024,
    "llama-3.2-1b-instruct": 2 * 16 * 8 * 64 * 2 / 1024,     # 32
    "llama-3.2-3b-instruct": 2 * 28 * 8 * 128 * 2 / 1024,    # 112
}


def test_kv_estimate_covers_every_catalog_model():
    # The estimate may overshoot (a smaller window) but never undershoot (an
    # out-of-memory start). A new catalog model needs its real figure here.
    assert {m["id"] for m in cat.catalog()} == set(_REAL_KV_KIB)
    for m in cat.catalog():
        est_kib = cat._kv_gb(m, 1) * 1024 * 1024
        assert est_kib >= _REAL_KV_KIB[m["id"]], m["id"]
    # ...and the range _kv_gb's docstring argues from is the real one.
    real = f"{min(_REAL_KV_KIB.values()):g}-{max(_REAL_KV_KIB.values()):g} KiB/token"
    assert real == "12-192 KiB/token"
    assert real in " ".join(cat._kv_gb.__doc__.split())


def test_every_catalog_model_gets_the_floor_on_8gb_cpu(monkeypatch):
    _stub_hw(monkeypatch, 8)
    for m in cat.catalog():
        assert cat.context_budget(m) >= cat.CTX_FLOOR, m["id"]


def test_server_default_window_is_the_floor():
    # A caller that doesn't size -c must still get a window a turn fits in.
    assert llama_server.DEFAULT_CTX == cat.CTX_FLOOR
    srv = llama_server.ManagedServer(server_bin="llama-server", model_path="m.gguf", port=1)
    assert srv.ctx == cat.CTX_FLOOR


def test_command_passes_ctx():
    srv = llama_server.ManagedServer(server_bin="llama-server", model_path="m.gguf",
                                     port=8080, ngl=99, ctx=16384)
    cmd = srv.command()
    assert cmd[cmd.index("-c") + 1] == "16384"
    assert cmd[cmd.index("-ngl") + 1] == "99"


# ---- setup: start / swap / relaunch write context_size ---------------------


class _FakeProc:
    _next_pid = 40000

    def __init__(self, argv):
        self.argv = argv
        self.returncode = None
        _FakeProc._next_pid += 1
        self.pid = _FakeProc._next_pid

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _refuse_mkdir_outside(monkeypatch, root) -> list[str]:
    """Make creating a directory outside `root` fail, and list each attempt
    (code that swallows the error can't hide it). os.mkdir is where Path.mkdir
    and os.makedirs both end up."""
    real_mkdir = os.mkdir
    root = os.path.realpath(root)
    outside: list[str] = []

    def guarded(path, *args, **kwargs):
        p = os.path.realpath(os.fspath(path))
        try:
            inside = os.path.commonpath([root, p]) == root
        except ValueError:  # another drive
            inside = False
        if not inside:
            outside.append(p)
            raise PermissionError(13, "test: mkdir outside tmp_path", p)
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", guarded)
    return outside


@pytest.fixture()
def isolated_config(monkeypatch, tmp_path):
    """config.toml and the runtime dir in tmp_path, never ~/.evi: a directory
    made anywhere else fails the test."""
    import evi.config as _cfg

    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    monkeypatch.setenv("EVI_SAFE_MODE", "1")  # no profile / project overlay
    monkeypatch.setattr(_cfg, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(_cfg, "RUNTIME_DIR", tmp_path / "runtime")
    # evi.config's dirs were fixed at import (EVI_HOME above is too late for
    # them), and Config.save() creates them all.
    monkeypatch.setattr(_cfg, "ensure_dirs", lambda: None)
    monkeypatch.setattr(rt_setup, "ensure_dirs", lambda: None)
    monkeypatch.setattr("evi.backends.registry.BACKENDS_PATH", tmp_path / "backends.json")
    monkeypatch.setattr(rt_setup, "_gpu_info", lambda: (None, 0.0, None))
    monkeypatch.setattr(llamacpp_runtime, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(llama_server, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(llama_server, "_MANAGED", None)
    # Temp dirs the code makes (the web app's safe-mode command store) too.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    outside = _refuse_mkdir_outside(monkeypatch, tmp_path)
    yield tmp_path
    assert outside == [], f"made directories outside tmp_path: {outside}"


@pytest.fixture()
def managed(monkeypatch, isolated_config):
    """A llama-server that 'starts' without a process. Yields the list of argv
    the server was launched with."""
    import subprocess
    import types

    from evi.runtime import reap

    tmp_path = isolated_config
    monkeypatch.setattr(rt_setup.platform, "system", lambda: "Windows")
    monkeypatch.setattr(llamacpp_runtime, "ensure_runtime",
                        lambda on_bytes=None: tmp_path / "llama-server.exe")
    monkeypatch.setattr(cat, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(cat, "is_installed", lambda entry: True)

    launched: list[list[str]] = []

    def fake_popen(argv, **_kw):
        launched.append(list(argv))
        return _FakeProc(argv)

    # Only llama_server's view of subprocess: nothing else's Popen changes.
    monkeypatch.setattr(llama_server, "subprocess", types.SimpleNamespace(
        Popen=fake_popen, STDOUT=subprocess.STDOUT, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(llama_server, "is_openai_server", lambda *a, **k: True)
    monkeypatch.setattr(llama_server, "free_port", lambda *a, **k: 18080)
    monkeypatch.setattr(reap, "assign_to_parent_lifetime", lambda pid: False)
    try:
        yield launched
    finally:
        rt_setup._set(running=False, done=False, error="", stage="idle", pct=0, message="")


def _ctx_of(argv: list[str]) -> int:
    return int(argv[argv.index("-c") + 1])


def test_first_run_on_8gb_cpu_sizes_ctx_and_writes_context_size(managed, monkeypatch):
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    st = rt_setup.start(background=False)
    assert st["error"] == "" and st["done"] is True

    (argv,) = managed
    c = _ctx_of(argv)
    assert c >= 8192
    cfg = Config.load()
    assert cfg.llm.backend == "llamacpp"
    assert cfg.llm.base_url == "http://127.0.0.1:18080/v1"
    assert cfg.llm.context_size == c       # the agent's ceiling is the real -c
    assert rt_setup.status()["ctx"] == c   # and status reports it


def test_runtime_status_endpoint_reports_ctx(managed, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod

    monkeypatch.setattr(server_mod, "ensure_dirs", lambda: None)  # no ~/.evi mkdirs
    _stub_hw(monkeypatch, 8)
    client = TestClient(server_mod.create_app())
    assert client.get("/api/runtime/catalog").json()["ctx"] is None  # no server yet
    rt_setup.start(background=False)
    body = client.get("/api/runtime/status").json()
    assert body["ctx"] == _ctx_of(managed[-1]) == 32768
    # ...and the payload the Settings runtime card renders.
    assert client.get("/api/runtime/catalog").json()["ctx"] == 32768


def test_swap_resizes_ctx_and_the_agent_ceiling_follows(managed, monkeypatch):
    from evi.config import Config
    from evi.llm.agent import Agent

    _stub_hw(monkeypatch, 8)
    rt_setup.start(background=False)                       # 1.5B starter: 32K
    live = Agent(client=object(), config=Config.load(), tools=[])
    assert live.token_usage()[1] == _ctx_of(managed[0]) == 32768

    st = rt_setup.use_model("qwen2.5-7b-instruct", background=False)
    assert st["error"] == "" and st["done"] is True
    assert len(managed) == 2
    new_c = _ctx_of(managed[1])
    assert new_c == 8192                                   # 7B fills 8 GB: floor
    assert managed[1][managed[1].index("--port") + 1] == "18080"  # same port
    assert rt_setup.status()["ctx"] == new_c
    assert Config.load().llm.context_size == new_c

    # A session built after the swap, and a live one that reloads its config,
    # both compact against the new window.
    assert Agent(client=object(), config=Config.load(), tools=[]).token_usage()[1] == new_c
    live.refresh_config()
    assert live.token_usage()[1] == new_c


def test_gpu_switch_sizes_ctx_from_vram(managed, monkeypatch, tmp_path):
    from evi.config import Config

    _stub_hw(monkeypatch, 8, vram_gb=(16.0,))
    monkeypatch.setattr(rt_setup, "_gpu_info", lambda: ("8.9", 16.0, "gpu0"))
    monkeypatch.setattr(llamacpp_runtime, "gpu_installed", lambda: True)
    monkeypatch.setattr(llamacpp_runtime, "gpu_server_path", lambda: tmp_path / "gpu-server.exe")
    monkeypatch.setattr(llamacpp_runtime, "gpu_plan",
                        lambda compute_cap=None: {"mode": "cuda", "build": "12.4", "assets": []})
    monkeypatch.setattr(llamacpp_runtime, "ensure_gpu_runtime", lambda cc, on_bytes=None: None)

    st = rt_setup.use_model("qwen2.5-14b-instruct", background=False)
    assert st["error"] == ""
    argv = managed[-1]
    assert argv[argv.index("-ngl") + 1] == "99"
    # 16 GB VRAM - 9 GB of weights: 32K and 16K don't fit, the 8 GB of RAM is moot.
    assert _ctx_of(argv) == 8192
    assert Config.load().llm.context_size == 8192

    st = rt_setup.enable_gpu(background=False)
    assert st["error"] == "" and st["on_gpu"] is True
    assert Config.load().llm.context_size == _ctx_of(managed[-1]) == st["ctx"]


def test_ensure_running_relaunch_rewrites_a_stale_context_size(managed, monkeypatch):
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    monkeypatch.setattr(llamacpp_runtime, "server_path", lambda: "llama-server.exe")
    cfg = Config.load()
    cfg.llm.backend = "llamacpp"
    cfg.llm.base_url = "http://127.0.0.1:18099/v1"
    cfg.llm.model = "Qwen2.5 1.5B Instruct"
    cfg.llm.context_size = 32768 * 4                         # stale / hand-set
    cfg.save()
    # Nothing answers at the configured URL; the managed server does once up.
    monkeypatch.setattr("evi.portprobe.is_openai_server",
                        lambda url, **k: url.startswith("http://127.0.0.1:18080"))

    assert rt_setup.ensure_running() is True
    after = Config.load()
    assert after.llm.base_url == "http://127.0.0.1:18080/v1"
    assert after.llm.context_size == _ctx_of(managed[-1]) == 32768


def test_status_ctx_is_none_without_a_server(isolated_config):
    assert llama_server.managed() is None
    assert rt_setup.status()["ctx"] is None


# ---- open sessions follow the live window ----------------------------------


def test_ctx_for_answers_only_for_the_managed_address(managed, monkeypatch):
    _stub_hw(monkeypatch, 8)
    assert llama_server.ctx_for("http://127.0.0.1:18080/v1") is None  # no server yet
    rt_setup.start(background=False)
    url = llama_server.managed().base_url()
    assert llama_server.ctx_for(url) == llama_server.ctx_for(url + "/") == 32768
    assert llama_server.ctx_for("http://127.0.0.1:1234/v1") is None   # a server eVi didn't start
    assert llama_server.ctx_for("https://api.example.invalid/v1") is None
    assert llama_server.ctx_for("") is None
    assert llama_server.ctx_for(None) is None
    llama_server.stop_managed()
    assert llama_server.ctx_for(url) is None


def test_refresh_context_size_follows_only_the_managed_server(managed, monkeypatch):
    from evi.config import Config
    from evi.llm.agent import Agent

    _stub_hw(monkeypatch, 8)
    rt_setup.start(background=False)                       # starter: 32K
    live = Agent(client=object(), config=Config.load(), tools=[])
    cloud_cfg = Config.load()
    cloud_cfg.llm.base_url = "https://api.example.invalid/v1"
    cloud_cfg.llm.context_size = 200000
    cloud = Agent(client=object(), config=cloud_cfg, tools=[])

    rt_setup.use_model("qwen2.5-7b-instruct", background=False)  # 7B on 8 GB: 8K
    assert live.token_usage()[1] == 32768                  # its own copy, until...
    assert live.refresh_context_size() == _ctx_of(managed[-1]) == 8192
    assert live.token_usage()[1] == 8192
    assert cloud.refresh_context_size() == 200000          # another server: untouched
    llama_server.stop_managed()
    assert live.refresh_context_size() == 8192             # no server: keeps the last


def _web_app(monkeypatch):
    """The web app on the isolated config, and each session's Agent by id."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.sdk.builder as builder_mod

    monkeypatch.setattr(server_mod, "ensure_dirs", lambda: None)  # no ~/.evi mkdirs
    agents: dict = {}
    real_build = builder_mod.build_agent

    def build(*args, **kwargs):
        agents[kwargs.get("session_id")] = agent = real_build(*args, **kwargs)
        return agent

    monkeypatch.setattr(builder_mod, "build_agent", build)
    return TestClient(server_mod.create_app()), agents


def _wait_for_setup(timeout: float = 10.0) -> dict:
    """Wait out a setup op the web app started in the background."""
    deadline = time.monotonic() + timeout
    while rt_setup.status()["running"]:
        assert time.monotonic() < deadline, "setup did not finish"
        time.sleep(0.01)
    return rt_setup.status()


def _replying_llm():
    """An OpenAI-style client whose every completion is a plain 'ok'."""
    chunk = SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")])
    completions = SimpleNamespace(create=lambda **_: iter([chunk]))
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_open_sessions_compact_against_the_new_window_after_a_swap(managed, monkeypatch):
    # Two chats are open when the model is swapped from Settings. Neither is
    # reloaded nor the app restarted, yet each one's next turn compacts against
    # the relaunched server's window.
    _stub_hw(monkeypatch, 8)
    rt_setup.start(background=False)                       # starter: 32K
    client, agents = _web_app(monkeypatch)
    compacted: dict[str, list[int]] = {}
    for sid in ("s-a", "s-b"):
        assert client.post(f"/api/session/{sid}/name", json={"name": sid}).status_code == 200
        agent = agents[sid]
        agent.client = _replying_llm()
        compacted[sid] = []
        agent.compact_history = (
            lambda keep_recent=None, a=agent, log=compacted[sid]:
            log.append(a.token_usage()[1]) or 0
        )
        # ~10K tokens: under 85% of 32K, over 85% of 8K.
        agent.history.append({"role": "user", "content": "x" * 40000})

    def turn(sid: str) -> None:
        r = client.post("/api/chat", json={"session_id": sid, "message": "hi"})
        assert r.status_code == 200 and '"Done"' in r.text

    for sid in ("s-a", "s-b"):
        turn(sid)
    assert compacted == {"s-a": [], "s-b": []}             # 32K: plenty of room

    assert client.post("/api/runtime/model/use",
                       json={"id": "qwen2.5-7b-instruct"}).status_code == 200
    st = _wait_for_setup()
    assert st["error"] == "" and st["ctx"] == _ctx_of(managed[-1]) == 8192

    turn("s-a")                                            # straight into a turn
    assert compacted["s-a"] == [8192]                      # the new window
    # The context meter shows it before s-b's next turn, which compacts too.
    assert client.get("/api/session/s-b/usage").json()["ceiling"] == 8192
    turn("s-b")
    assert compacted == {"s-a": [8192], "s-b": [8192]}


# ---- leaving the managed runtime gives context_size back -------------------


def _set_llm(**kw) -> None:
    from evi.config import Config

    cfg = Config.load()
    for k, v in kw.items():
        setattr(cfg.llm, k, v)
    cfg.save()


def _own_llm() -> dict:
    """config.toml's own [llm] table: the user layer, no overlay on top."""
    import tomllib

    import evi.config

    with evi.config.CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("llm", {})


def test_switching_away_gives_the_users_context_size_back(managed, monkeypatch):
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    _set_llm(context_size=200000)                          # the user's own value
    rt_setup.start(background=False)                       # starter: 32K
    rt_setup.use_model("qwen2.5-7b-instruct", background=False)  # 7B on 8 GB: 8K
    assert Config.load().llm.context_size == _ctx_of(managed[-1]) == 8192

    # Still the managed runtime: its window stays.
    assert rt_setup.release_context_size() is None
    assert Config.load().llm.context_size == 8192

    # A switch to a cloud backend hands back the value from before the runtime
    # took over, not the 32K or 8K windows it wrote in between.
    _set_llm(backend="openai_compat", base_url="https://api.example.invalid/v1",
             model="gpt-big")
    assert rt_setup.release_context_size() == 200000
    assert Config.load().llm.context_size == 200000
    assert rt_setup.release_context_size() is None         # once


def test_without_an_own_value_the_default_comes_back(managed, monkeypatch):
    # config.toml that never set context_size: the runtime's window is dropped
    # again rather than left behind, or pinned as the default's number.
    import evi.config
    from evi.config import Config, LLMSettings

    _stub_hw(monkeypatch, 8)
    evi.config.CONFIG_PATH.write_text('[llm]\nbackend = "lmstudio"\n', encoding="utf-8")
    rt_setup.use_model("qwen2.5-7b-instruct", background=False)
    assert _own_llm()["context_size"] == 8192
    _set_llm(backend="openai_compat")
    assert rt_setup.release_context_size() == LLMSettings().context_size
    assert "context_size" not in _own_llm()
    assert Config.load().llm.context_size == LLMSettings().context_size


def test_a_value_set_after_the_switch_is_kept(managed, monkeypatch):
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    _set_llm(context_size=200000)
    rt_setup.use_model("qwen2.5-7b-instruct", background=False)
    _set_llm(backend="openai_compat", context_size=128000)
    assert rt_setup.release_context_size() is None
    assert Config.load().llm.context_size == 128000


def test_a_backend_switch_gives_context_size_back_at_once(managed, monkeypatch):
    # /api/backend/use moves [llm] off the managed runtime: config.toml, the
    # chat that is open and one opened afterwards all get the user's 200000
    # back in the same process — no restart.
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    _set_llm(context_size=200000)
    rt_setup.use_model("qwen2.5-7b-instruct", background=False)  # 8K
    client, agents = _web_app(monkeypatch)
    client.post("/api/session/s-open/name", json={"name": "open"})
    assert client.get("/api/session/s-open/usage").json()["ceiling"] == 8192

    body = client.post(
        "/api/backend/use",
        json={"kind": "openai_compat", "model": "gpt-big", "verify": False},
    ).json()
    assert body["ok"] is True and body["backend"] == "openai_compat"
    assert _own_llm()["context_size"] == 200000
    assert Config.load().llm.context_size == 200000
    assert agents["s-open"].token_usage()[1] == 200000
    client.post("/api/session/s-new/name", json={"name": "new"})
    assert client.get("/api/session/s-new/usage").json()["ceiling"] == 200000


def test_a_switch_that_fails_verify_keeps_the_runtime_window(managed, monkeypatch):
    # A verified switch that doesn't answer rolls back to the managed runtime,
    # so its window, and the value to give back later, both stay.
    import evi.apps.web.server as server_mod
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    _set_llm(context_size=200000)
    rt_setup.use_model("qwen2.5-7b-instruct", background=False)
    client, _agents = _web_app(monkeypatch)
    monkeypatch.setattr(server_mod, "_verify_llm", lambda cfg: {"chat": False})
    body = client.post(
        "/api/backend/use",
        json={"kind": "openai_compat", "model": "gpt-big", "verify": True},
    ).json()
    assert body["ok"] is False and body["backend"] == "llamacpp"
    assert Config.load().llm.context_size == 8192
    _set_llm(backend="openai_compat")
    assert rt_setup.release_context_size() == 200000


def test_the_model_picker_switch_gives_context_size_back(managed, monkeypatch):
    from evi.backends.registry import BackendEntry, save_backends
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    _set_llm(context_size=200000)
    rt_setup.use_model("qwen2.5-7b-instruct", background=False)
    save_backends([BackendEntry(name="cloud", kind="openai_compat",
                                base_url="https://api.example.invalid/v1", api_key="sk-test")])
    client, agents = _web_app(monkeypatch)
    client.post("/api/session/s-open/name", json={"name": "open"})
    r = client.post("/api/model-picker", json={"backend": "cloud", "model": "gpt-big"})
    assert r.status_code == 200 and r.json()["backend"] == "openai_compat"
    assert Config.load().llm.context_size == 200000
    assert agents["s-open"].token_usage()[1] == 200000


def test_startup_gives_context_size_back_after_a_switch_elsewhere(managed, monkeypatch):
    # A switch the web app didn't make (e.g. the CLI) leaves 8192 behind; the
    # next `evi web` / desktop start (ensure_running) gives 200000 back.
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    _set_llm(context_size=200000)
    rt_setup.use_model("qwen2.5-7b-instruct", background=False)
    _set_llm(backend="openai_compat", base_url="https://api.example.invalid/v1")
    assert Config.load().llm.context_size == 8192

    assert rt_setup.ensure_running() is False              # not the managed runtime
    after = Config.load()
    assert after.llm.backend == "openai_compat"
    assert after.llm.context_size == 200000


def test_relaunch_keeps_the_value_from_before_the_runtime(managed, monkeypatch):
    # Upgrading from a release that never wrote context_size: the first
    # relaunch replaces the user's value, and a later switch still restores it.
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    monkeypatch.setattr(llamacpp_runtime, "server_path", lambda: "llama-server.exe")
    _set_llm(backend="llamacpp", base_url="http://127.0.0.1:18099/v1",
             model="Qwen2.5 7B Instruct", context_size=65536)
    monkeypatch.setattr("evi.portprobe.is_openai_server",
                        lambda url, **k: url.startswith("http://127.0.0.1:18080"))
    assert rt_setup.ensure_running() is True
    assert Config.load().llm.context_size == _ctx_of(managed[-1]) == 8192
    assert rt_setup.ensure_running() is True               # already answering
    _set_llm(backend="ollama")
    assert rt_setup.ensure_running() is False
    assert Config.load().llm.context_size == 65536


# ---- profile / project layers stay out of config.toml ----------------------


@pytest.fixture()
def repo_overlay(monkeypatch, isolated_config):
    """A trusted repo's `.evi.toml` [llm] values: Config.load() applies them
    while the dict has any (inside the repo), not once it's cleared."""
    import evi.project

    monkeypatch.delenv("EVI_SAFE_MODE")                    # overlays apply again
    monkeypatch.delenv("EVI_PROFILE", raising=False)       # no profile
    layer: dict = {}

    def apply_project_config(base, start=None):
        if not layer:
            return base
        return {**base, "llm": {**(base.get("llm") or {}), **layer}}

    monkeypatch.setattr(evi.project, "apply_project_config", apply_project_config)
    return layer


@pytest.mark.parametrize("repo_value", [1000000, 8192])
def test_a_repo_value_is_never_written_into_config_toml(managed, repo_overlay, monkeypatch,
                                                        repo_value):
    # The runtime starts inside a repo whose .evi.toml sets context_size. What
    # is remembered, and later given back, is config.toml's own 65536 — and
    # config.toml gets the window even when the repo's value already equals it.
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    _set_llm(context_size=65536)
    repo_overlay["context_size"] = repo_value              # inside the repo
    rt_setup.use_model("qwen2.5-7b-instruct", background=False)
    assert _own_llm()["context_size"] == _ctx_of(managed[-1]) == 8192
    assert Config.load().llm.context_size == repo_value    # the repo's still wins there

    repo_overlay.clear()                                   # left the repo
    assert Config.load().llm.context_size == 8192
    _set_llm(backend="openai_compat", base_url="https://api.example.invalid/v1")
    assert rt_setup.release_context_size() == 65536
    assert _own_llm()["context_size"] == 65536


@pytest.mark.parametrize("repo_value", [4096, 200000])
def test_release_inside_a_repo_gives_config_toml_its_own_value(managed, repo_overlay,
                                                               monkeypatch, repo_value):
    # Given back while a repo overrides context_size — even with the very
    # value being given back, which Config.save wouldn't count as a change.
    from evi.config import Config

    _stub_hw(monkeypatch, 8)
    _set_llm(context_size=200000)
    rt_setup.use_model("qwen2.5-7b-instruct", background=False)  # 8K
    _set_llm(backend="openai_compat", base_url="https://api.example.invalid/v1")
    repo_overlay["context_size"] = repo_value              # e.g. a start inside the repo
    assert rt_setup.release_context_size() == repo_value   # what is in effect here
    assert _own_llm()["context_size"] == 200000
    assert rt_setup.release_context_size() is None         # once
    repo_overlay.clear()
    assert Config.load().llm.context_size == 200000
