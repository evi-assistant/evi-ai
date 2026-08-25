"""Managed llama.cpp runtime — asset resolution, setup state, autostart guard.

Pure logic: no network, no subprocess, injectable platform.
"""

from __future__ import annotations

import pytest

from evi.runtime import llamacpp_runtime as rt_bin
from evi.runtime import setup as rt_setup


def test_cpu_asset_per_platform():
    v = rt_bin.LLAMACPP_VERSION
    assert rt_bin.cpu_asset("Windows", "AMD64") == f"llama-{v}-bin-win-cpu-x64.zip"
    assert rt_bin.cpu_asset("Windows", "ARM64") == f"llama-{v}-bin-win-cpu-arm64.zip"
    assert rt_bin.cpu_asset("Linux", "x86_64") == f"llama-{v}-bin-ubuntu-x64.tar.gz"
    assert rt_bin.cpu_asset("Linux", "aarch64") == f"llama-{v}-bin-ubuntu-arm64.tar.gz"
    assert rt_bin.cpu_asset("Darwin", "arm64") == f"llama-{v}-bin-macos-arm64.tar.gz"
    assert rt_bin.cpu_asset("Darwin", "x86_64") == f"llama-{v}-bin-macos-x64.tar.gz"
    # arch aliases normalize
    assert rt_bin.cpu_asset("linux", "X86_64") == rt_bin.cpu_asset("Linux", "amd64")


def test_cpu_asset_unsupported():
    assert rt_bin.cpu_asset("Plan9", "riscv") is None
    assert rt_bin.cpu_asset("Linux", "s390x") is None


def test_runtime_root_and_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(rt_bin, "RUNTIME_DIR", tmp_path)
    assert rt_bin.runtime_root() == tmp_path / "llama" / rt_bin.LLAMACPP_VERSION
    assert rt_bin.server_path() is None
    assert rt_bin.is_installed() is False


def test_ensure_runtime_unsupported_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(rt_bin, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(rt_bin.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(rt_bin.platform, "machine", lambda: "riscv")
    with pytest.raises(RuntimeError):
        rt_bin.ensure_runtime()


def test_setup_status_shape():
    s = rt_setup.status()
    for k in ("stage", "pct", "message", "running", "done", "error",
              "server_running", "runtime_installed", "supported", "active_model_id"):
        assert k in s


def test_starter_model_path_under_models_dir(monkeypatch, tmp_path):
    from evi.runtime import catalog as cat

    monkeypatch.setattr(cat, "MODELS_DIR", tmp_path)
    p = rt_setup.starter_model_path()
    assert p.parent.parent == tmp_path
    assert p.name.endswith(".gguf")


def test_catalog_entries_wellformed():
    from evi.runtime import catalog as cat

    models = cat.catalog()
    assert len(models) >= 6
    ids = [m["id"] for m in models]
    assert cat.STARTER_ID in ids
    assert len(ids) == len(set(ids))  # unique ids
    for m in models:
        for k in ("id", "name", "hf_repo", "filename", "quant", "size_gb",
                  "params_b", "min_ram_gb", "min_vram_gb", "license"):
            assert k in m, f"{m['id']} missing {k}"
        assert m["filename"].endswith(".gguf")


def test_get_unknown_returns_none():
    from evi.runtime import catalog as cat

    assert cat.get("nope") is None


def test_recommended_scales_with_memory(monkeypatch):
    from evi.runtime import catalog as cat

    monkeypatch.setattr(cat, "_memory_gb", lambda: (2.0, 0.0))
    small = cat.get(cat.recommended_id())
    assert "coding" not in small.get("tags", [])  # never recommend a specialist

    monkeypatch.setattr(cat, "_memory_gb", lambda: (64.0, 0.0))
    big = cat.get(cat.recommended_id())
    assert big["params_b"] >= small["params_b"]  # more memory -> at least as large


def test_cuda_build_table():
    v = rt_bin.LLAMACPP_VERSION  # noqa: F841 — referenced for clarity
    assert rt_bin._cuda_build_for("12.0") == "13.3"   # Blackwell sm_120 needs >=12.8
    assert rt_bin._cuda_build_for("12.5") == "13.3"
    assert rt_bin._cuda_build_for("8.9") == "12.4"    # Ada
    assert rt_bin._cuda_build_for("6.1") == "12.4"
    assert rt_bin._cuda_build_for("3.5") is None      # pre-Maxwell, too old
    assert rt_bin._cuda_build_for(None) is None
    assert rt_bin._cuda_build_for("garbage") is None


def test_gpu_plan_per_platform():
    assert rt_bin.gpu_plan(system="Darwin", machine="arm64", compute_cap=None) == {"mode": "metal"}
    p = rt_bin.gpu_plan(system="Windows", machine="AMD64", compute_cap="12.0")
    assert p["mode"] == "cuda" and p["build"] == "13.3" and len(p["assets"]) == 2
    assert rt_bin.gpu_plan(system="Windows", machine="AMD64", compute_cap="8.9")["build"] == "12.4"
    assert rt_bin.gpu_plan(system="Windows", machine="AMD64", compute_cap=None) is None
    assert rt_bin.gpu_plan(system="Linux", machine="x86_64", compute_cap="8.9") is None


def test_gpu_root_under_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(rt_bin, "RUNTIME_DIR", tmp_path)
    assert rt_bin.gpu_root() == rt_bin.runtime_root() / "gpu"
    assert rt_bin.gpu_server_path() is None
    assert rt_bin.gpu_installed() is False


def test_reap_assign_is_safe():
    from evi.runtime import reap

    # Never raises; returns a bool (False off Windows / for a bogus pid). The
    # real kill-on-close behaviour is Windows-only and exercised out-of-band.
    assert isinstance(reap.assign_to_parent_lifetime(0), bool)


# ---- locate-existing-install (external binary) ----------------------------


def test_config_has_runtime_section():
    from evi.config import Config, RuntimeSettings

    c = Config()
    assert isinstance(c.runtime, RuntimeSettings)
    assert c.runtime.server_path == ""


def test_validate_server_binary_rejects_missing(tmp_path):
    # A path that isn't a file is rejected without running anything.
    assert rt_bin.validate_server_binary(tmp_path / "nope-llama-server") is False


def test_detect_external_excludes_managed(monkeypatch, tmp_path):
    import shutil

    monkeypatch.setattr(rt_bin, "RUNTIME_DIR", tmp_path)
    managed = rt_bin.runtime_root() / rt_bin._server_name()
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_text("x")
    # Even if eVi's own managed binary is what's on PATH, it's not an "existing
    # external install" — it must be filtered out of the locate candidates.
    monkeypatch.setattr(shutil, "which", lambda *a, **k: str(managed))
    assert str(managed) not in rt_bin.detect_external()


def test_pick_runtime_prefers_external(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(rt_setup, "_gpu_info", lambda: (None, 0.0, None))
    bin_, ngl = rt_setup._pick_runtime({"name": "x", "min_vram_gb": 999},
                                       external="C:/tools/llama-server.exe")
    assert bin_ == Path("C:/tools/llama-server.exe")
    assert ngl == 0  # no GPU detected → CPU offload


def test_locate_rejects_invalid_binary():
    # Validation fails for a non-llama-server path → error, no install kicked off.
    st = rt_setup.locate("/definitely/not/a/llama-server-xyz", background=False)
    assert st.get("error")


def test_install_plan_lists_sizes_paths_and_urls(monkeypatch, tmp_path):
    """The manifest must state what lands on disk BEFORE anything downloads."""
    from fastapi.testclient import TestClient

    from evi.apps.web.server import create_app
    from evi.runtime import llamacpp_runtime as rt

    monkeypatch.setattr(rt, "RUNTIME_DIR", tmp_path)          # nothing installed
    monkeypatch.setattr(rt, "asset_size_label", lambda: "18 MB")  # no network in tests
    c = TestClient(create_app())
    p = c.get("/api/runtime/plan").json()

    kinds = [i["kind"] for i in p["items"]]
    assert kinds == ["runtime", "model", "server", "admin"]
    runtime_item = p["items"][0]
    model_item = p["items"][1]
    assert runtime_item["needed"] is True and runtime_item["size"] == "18 MB"
    assert str(tmp_path) in runtime_item["path"]
    assert model_item["size"].endswith("GB")
    # Every pending download is auditable: the literal URL is disclosed.
    assert len(p["commands"]) == 2
    assert all(u.startswith("https://") for u in p["commands"])
    # The loopback promise is stated, not implied.
    assert "not reachable" in p["items"][2]["detail"].lower()


def test_install_plan_marks_present_items_not_needed(monkeypatch, tmp_path):
    """A second run must not threaten to re-download what's already on disk."""
    from fastapi.testclient import TestClient

    from evi.apps.web.server import create_app
    from evi.runtime import catalog as cat
    from evi.runtime import llamacpp_runtime as rt

    monkeypatch.setattr(rt, "is_installed", lambda: True)
    monkeypatch.setattr(cat, "is_installed", lambda e: True)
    p = TestClient(create_app()).get("/api/runtime/plan").json()
    assert p["items"][0]["needed"] is False and p["items"][0]["size"] == ""
    assert p["items"][1]["needed"] is False
    assert p["commands"] == []          # nothing to download -> nothing to show


def test_install_plan_ignores_a_stale_external_binary(monkeypatch, tmp_path):
    """A moved/deleted located binary must not make the manifest promise that no
    runtime download is coming — the run would download one anyway."""
    from fastapi.testclient import TestClient

    from evi.apps.web.server import create_app
    from evi.config import Config
    from evi.runtime import llamacpp_runtime as rt

    monkeypatch.setattr(rt, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(rt, "asset_size_label", lambda: "18 MB")
    cfg = Config.load()
    cfg.runtime.server_path = str(tmp_path / "gone" / "llama-server.exe")  # not a file
    cfg.save()

    p = TestClient(create_app()).get("/api/runtime/plan").json()
    assert p["external"] == ""                       # stale path ignored
    assert p["items"][0]["kind"] == "runtime"
    assert p["items"][0]["needed"] is True           # the download IS disclosed
    assert any("llama.cpp" in u or "releases/download" in u for u in p["commands"])


def test_external_binary_clears_a_path_that_is_not_llama_server(monkeypatch, tmp_path):
    """Present but not a usable llama-server: clear it, so config and the setup
    card agree instead of the dead path lingering."""
    from evi.config import Config
    from evi.runtime import llamacpp_runtime as rt
    from evi.runtime import setup as s

    fake = tmp_path / "llama-cli.exe"
    fake.write_text("x")
    cfg = Config.load()
    cfg.runtime.server_path = str(fake)
    cfg.save()
    monkeypatch.setattr(rt, "validate_server_binary", lambda p: False)

    assert s._external_binary() == ""
    assert Config.load().runtime.server_path == ""   # self-healed


def test_runtime_log_endpoint_handles_missing_log(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from evi.apps.web.server import create_app
    from evi.runtime import setup as s

    monkeypatch.setattr(s, "server_log_path", lambda: tmp_path / "nope.log")
    r = TestClient(create_app()).get("/api/runtime/log").json()
    assert r["exists"] is False and r["text"] == ""

    p = tmp_path / "there.log"
    p.write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
    monkeypatch.setattr(s, "server_log_path", lambda: p)
    r = TestClient(create_app()).get("/api/runtime/log?lines=10").json()
    assert r["exists"] is True
    assert r["text"].splitlines()[-1] == "line 499"      # it's a TAIL
    assert len(r["text"].splitlines()) == 10


def test_asset_size_label_never_raises(monkeypatch):
    """Offline or rate-limited, the manifest omits a size rather than blocking."""
    from evi.runtime import llamacpp_runtime as rt

    rt._ASSET_SIZE_CACHE.clear()
    monkeypatch.setattr(rt, "cpu_asset", lambda *a, **k: "x.zip")

    def boom(*a, **k):
        raise OSError("no network")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert rt.asset_size_label() == ""
    rt._ASSET_SIZE_CACHE.clear()


def test_first_run_ladder_ranks_installed_before_download():
    from evi.apps.web import server as srv

    data = {
        "cli_agents": ["claude_agent"],
        "candidates": [{"kind": "ollama", "url": "http://x", "reachable": True},
                       {"kind": "lmstudio", "url": "http://y", "reachable": False}],
        "api_keys": ["openai"],
        "runtime_supported": True,
        "runtime_installed": False,
    }
    out = srv._first_run_suggestions(data)
    kinds = [s["kind"] for s in out]
    # Something already installed/running must outrank the multi-hundred-MB
    # download — that ordering IS the feature.
    assert kinds.index("claude_agent") < kinds.index("ollama") < kinds.index("llamacpp")
    assert "lmstudio" not in kinds          # not reachable -> not offered
    assert out[0]["instant"] is True
    # Detection is a PATH lookup, so the copy must not claim a working login.
    assert "signed in" not in out[0]["detail"].lower()


def test_first_run_ladder_empty_when_nothing_available():
    from evi.apps.web import server as srv

    assert srv._first_run_suggestions(
        {"cli_agents": [], "candidates": [], "api_keys": [], "runtime_supported": False}
    ) == []


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("127.0.0.1:8473", True), ("localhost:8473", True), ("[::1]:8473", True),
        ("localhost", True), ("", True),
        ("evil.example", False), ("attacker.com:8473", False),
    ],
)
def test_host_guard_blocks_dns_rebinding(host, ok, monkeypatch):
    """A hostile page can point its own hostname at loopback, but can't forge Host."""
    from evi.apps.web import server as srv

    monkeypatch.delenv("EVI_ALLOW_ANY_HOST", raising=False)
    req = type("R", (), {"headers": {"host": host}})()
    assert srv._host_is_local(req) is ok


def test_host_guard_env_override(monkeypatch):
    from evi.apps.web import server as srv

    monkeypatch.setenv("EVI_ALLOW_ANY_HOST", "1")
    req = type("R", (), {"headers": {"host": "anything.example"}})()
    assert srv._host_is_local(req) is True


def test_kick_resets_stale_state_before_returning():
    # A prior run's done/error must NOT leak into the next op's first status()
    # (that caused a premature "ready" and a spurious 400 on locate).
    rt_setup._set(error="llama-server exited early", done=True, running=False)
    try:
        st = rt_setup._kick(lambda: None, background=False)
        assert st["error"] == ""
        assert st["done"] is False
    finally:
        rt_setup._set(running=False, done=False, error="", stage="idle", pct=0, message="")


def test_ensure_running_noop_when_not_llamacpp(monkeypatch):
    from evi.config import Config

    c = Config()
    c.llm.backend = "ollama"
    monkeypatch.setattr(rt_setup, "Config", type("C", (), {"load": staticmethod(lambda: c)}))
    # backend != llamacpp -> returns immediately, never probes/spawns anything.
    assert rt_setup.ensure_running() is False
