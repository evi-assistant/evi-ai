"""Batch A quick wins: notify, check-on-edit, pluggable web search, skills add."""

import json

import pytest

from evi import notify
from evi.config import Config, NotifySettings


# --- notifications ------------------------------------------------------------

def test_notify_never_raises(monkeypatch):
    # Force every channel down a no-op path; notify() must swallow everything.
    monkeypatch.setattr(notify, "_play_sound", lambda: None)
    monkeypatch.setattr(notify, "_desktop_toast", lambda t, b: None)
    monkeypatch.setattr(notify, "_post_url", lambda u, t, b: None)
    notify.notify("eVi", "done", sound=True, desktop=True, url="http://x")


def test_notify_if_enabled_off_by_default():
    cfg = Config()
    assert cfg.notify.enabled is False
    assert notify.notify_if_enabled("t", "b", config=cfg) is False


def test_notify_if_enabled_fires_when_on(monkeypatch):
    calls = {}
    monkeypatch.setattr(notify, "notify",
                        lambda *a, **k: calls.update(args=a, kw=k))
    cfg = Config()
    cfg.notify = NotifySettings(enabled=True, sound=False, desktop=True, url="http://n")
    assert notify.notify_if_enabled("eVi", "Turn complete", config=cfg) is True
    assert calls["kw"]["url"] == "http://n"
    assert calls["kw"]["sound"] is False


# --- check-on-edit ------------------------------------------------------------

def test_post_write_appends_diagnostics(monkeypatch, tmp_path):
    from evi import codeintel
    from evi.tools import fs

    cfg = Config()
    cfg.tools.format_on_edit = False
    cfg.tools.check_on_edit = True
    monkeypatch.setattr(Config, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(codeintel, "diagnose", lambda p: "E501 line too long")
    note = fs._post_write(tmp_path / "x.py")
    assert "[check]" in note and "E501" in note


def test_post_write_skips_clean_and_no_linter(monkeypatch, tmp_path):
    from evi import codeintel
    from evi.tools import fs

    cfg = Config()
    cfg.tools.check_on_edit = True
    monkeypatch.setattr(Config, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(codeintel, "diagnose", lambda p: "ruff: no issues found")
    assert "[check]" not in fs._post_write(tmp_path / "x.py")
    monkeypatch.setattr(codeintel, "diagnose", lambda p: "(no linter configured for .xyz)")
    assert "[check]" not in fs._post_write(tmp_path / "x.xyz")


# --- pluggable web search -----------------------------------------------------

def test_web_search_searxng_requires_url(monkeypatch):
    from evi.tools import websearch

    cfg = Config()
    cfg.tools.search_backend = "searxng"
    cfg.tools.searxng_url = ""
    monkeypatch.setattr(Config, "load", staticmethod(lambda: cfg))
    out = websearch.web_search("hello")
    assert out.startswith("ERROR") and "searxng_url" in out


def test_web_search_dispatches_to_backend(monkeypatch):
    from evi.tools import websearch

    cfg = Config()
    cfg.tools.search_backend = "searxng"
    cfg.tools.searxng_url = "http://localhost:8888"
    monkeypatch.setattr(Config, "load", staticmethod(lambda: cfg))
    seen = {}
    monkeypatch.setattr(websearch, "_search_searxng",
                        lambda url, q, n: seen.update(url=url, q=q) or "[]")
    assert websearch.web_search("cats", 3) == "[]"
    assert seen["url"] == "http://localhost:8888"


def test_web_search_ollama_needs_key(monkeypatch):
    from evi.tools import websearch

    cfg = Config()
    cfg.tools.search_backend = "ollama"
    monkeypatch.setattr(Config, "load", staticmethod(lambda: cfg))
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    out = websearch.web_search("hello")
    assert out.startswith("ERROR") and "OLLAMA_API_KEY" in out


# --- skills add ---------------------------------------------------------------

def test_skill_index_parses_skills_section(tmp_path):
    from evi import marketplace

    idx = tmp_path / "marketplace.json"
    idx.write_text(json.dumps({
        "plugins": [{"name": "p1", "source": "https://x/p1.git"}],
        "skills": [{"name": "pdf-pro", "source": "https://x/pdf.git", "tags": ["doc"]}],
    }), encoding="utf-8")
    skills = marketplace.load_skill_index(idx)
    assert [e.name for e in skills] == ["pdf-pro"]
    assert marketplace.resolve("pdf-pro", skills).source == "https://x/pdf.git"


def test_install_skill_from_local_dir(tmp_path):
    from evi import skills

    src = tmp_path / "mskill"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: does a thing\n---\n# body\n", encoding="utf-8"
    )
    dest_root = tmp_path / "installed"
    name = skills.install_skill(str(src), root=dest_root)
    assert name == "my-skill"
    assert (dest_root / "my-skill" / "SKILL.md").is_file()


def test_install_skill_unknown_name_raises(tmp_path, monkeypatch):
    from evi import marketplace, skills

    monkeypatch.setattr(marketplace, "load_skill_index", lambda **k: [])
    with pytest.raises(skills.SkillError):
        skills.install_skill("does-not-exist")
