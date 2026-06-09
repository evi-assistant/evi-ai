"""Web API for the plugin browser — list installed + marketplace, install, remove."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


def _make_plugin(d, name, *, version="1.0.0", description="a test plugin",
                 commands=0, skills=0):
    """Create a minimal installed-plugin directory under d/<name>."""
    pdir = d / name
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text(
        f'name = "{name}"\nversion = "{version}"\ndescription = "{description}"\n',
        encoding="utf-8",
    )
    if commands:
        cdir = pdir / "commands"
        cdir.mkdir()
        for i in range(commands):
            (cdir / f"c{i}.md").write_text("# cmd\n", encoding="utf-8")
    if skills:
        for i in range(skills):
            sk = pdir / "skills" / f"s{i}"
            sk.mkdir(parents=True)
            (sk / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    return pdir


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Point both the plugins dir and the marketplace at a tmp HOME."""
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.marketplace.MARKETPLACE_PATH", tmp_path / "marketplace.json")
    (tmp_path / "plugins").mkdir()
    return tmp_path


@pytest.fixture
def client(monkeypatch, home, tmp_path):
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_list_empty(client):
    d = client.get("/api/plugins").json()
    assert d["installed"] == [] and d["marketplace"] == []


def test_list_installed(client, home):
    _make_plugin(home / "plugins", "hello", commands=2, skills=1)
    d = client.get("/api/plugins").json()
    assert len(d["installed"]) == 1
    p = d["installed"][0]
    assert p["name"] == "hello" and p["commands"] == 2 and p["skills"] == 1
    assert p["version"] == "1.0.0"


def test_marketplace_listed_with_installed_flag(client, home):
    _make_plugin(home / "plugins", "alpha")
    (home / "marketplace.json").write_text(json.dumps({"plugins": [
        {"name": "alpha", "source": "/x/alpha", "description": "already in"},
        {"name": "beta", "source": "/x/beta", "description": "not yet", "tags": ["t"]},
    ]}), encoding="utf-8")
    market = client.get("/api/plugins").json()["marketplace"]
    by = {e["name"]: e for e in market}
    assert by["alpha"]["installed"] is True
    assert by["beta"]["installed"] is False and by["beta"]["tags"] == ["t"]


def test_install_from_source_dir(client, home, tmp_path):
    src = tmp_path / "src-plugin"
    _make_plugin(tmp_path, "src-plugin")  # creates tmp_path/src-plugin
    r = client.post("/api/plugins/install", json={"source": str(src)})
    assert r.status_code == 200 and r.json()["name"] == "src-plugin"
    assert (home / "plugins" / "src-plugin" / "plugin.toml").is_file()


def test_install_by_marketplace_name(client, home, tmp_path):
    src = tmp_path / "mk-plugin"
    _make_plugin(tmp_path, "mk-plugin")
    (home / "marketplace.json").write_text(json.dumps({"plugins": [
        {"name": "mk-plugin", "source": str(src), "description": "via index"},
    ]}), encoding="utf-8")
    r = client.post("/api/plugins/install", json={"name": "mk-plugin"})
    assert r.status_code == 200
    assert (home / "plugins" / "mk-plugin").is_dir()


def test_install_unknown_name_404(client, home):
    assert client.post("/api/plugins/install", json={"name": "nope"}).status_code == 404


def test_install_bad_source_400(client):
    r = client.post("/api/plugins/install", json={"source": "/no/such/dir-xyz"})
    assert r.status_code == 400


def test_install_requires_input(client):
    assert client.post("/api/plugins/install", json={}).status_code == 400


def test_remove(client, home):
    _make_plugin(home / "plugins", "gone")
    assert client.post("/api/plugins/remove", json={"name": "gone"}).status_code == 200
    assert not (home / "plugins" / "gone").exists()


def test_remove_unknown_404(client):
    assert client.post("/api/plugins/remove", json={"name": "ghost"}).status_code == 404


def test_remove_requires_name(client):
    assert client.post("/api/plugins/remove", json={}).status_code == 400
