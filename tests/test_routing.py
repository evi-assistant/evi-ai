"""Tests for evi/routing.py — Route, RouterStore, Router."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


from evi.routing import PRESET_ROUTES, Route, Router, RouterStore


# --- RouterStore: load / save / mutations -----------------------------------


def test_store_load_missing_returns_empty(tmp_path: Path) -> None:
    store = RouterStore(path=tmp_path / "missing.json")
    assert store.load() == []


def test_store_round_trip(tmp_path: Path) -> None:
    store = RouterStore(path=tmp_path / "routes.json")
    r = Route(name="coder", model="qwen-coder", description="code", match_keywords=["debug"])
    store.save([r])
    assert store.load() == [r]


def test_store_load_skips_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({
        "routes": [
            {"name": "ok", "model": "m1"},
            {"name": "", "model": "m1"},      # missing name
            {"name": "no-model"},              # missing model
            "not a dict",
            {"name": "kws", "model": "m2", "match_keywords": "not-a-list"},
        ]
    }))
    loaded = RouterStore(path=path).load()
    assert [r.name for r in loaded] == ["ok", "kws"]
    # "match_keywords" gets coerced to an empty list when malformed.
    assert loaded[1].match_keywords == []


def test_store_load_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    path.write_text("{not json")
    assert RouterStore(path=path).load() == []


def test_store_add_appends(tmp_path: Path) -> None:
    store = RouterStore(path=tmp_path / "routes.json")
    assert store.add(Route(name="a", model="m1"))
    assert store.add(Route(name="b", model="m2"))
    assert [r.name for r in store.load()] == ["a", "b"]


def test_store_add_existing_without_overwrite_returns_false(tmp_path: Path) -> None:
    store = RouterStore(path=tmp_path / "routes.json")
    store.add(Route(name="a", model="m1"))
    assert not store.add(Route(name="a", model="m2"))
    # Original survives.
    assert store.load()[0].model == "m1"


def test_store_add_overwrites_when_allowed(tmp_path: Path) -> None:
    store = RouterStore(path=tmp_path / "routes.json")
    store.add(Route(name="a", model="m1"))
    assert store.add(Route(name="a", model="m2"), overwrite=True)
    assert store.load()[0].model == "m2"


def test_store_remove(tmp_path: Path) -> None:
    store = RouterStore(path=tmp_path / "routes.json")
    store.add(Route(name="a", model="m1"))
    store.add(Route(name="b", model="m2"))
    assert store.remove("a")
    assert [r.name for r in store.load()] == ["b"]
    assert not store.remove("missing")


# --- Router: keyword matching ------------------------------------------------


def _routes() -> list[Route]:
    return [
        Route(
            name="coder",
            model="qwen-coder",
            description="programming",
            match_keywords=["debug", "function"],
        ),
        Route(
            name="fast",
            model="qwen-3b",
            description="chitchat",
            match_keywords=["hi", "hello"],
        ),
    ]


def test_router_keyword_match() -> None:
    r = Router(_routes(), default_model="qwen-7b")
    d = r.pick("help me debug this loop")
    assert d.model == "qwen-coder"
    assert d.route_name == "coder"
    assert d.reason == "keyword:debug"


def test_router_case_insensitive_keyword() -> None:
    r = Router(_routes(), default_model="qwen-7b")
    assert r.pick("DEBUG please").route_name == "coder"
    assert r.pick("Hi there").route_name == "fast"


def test_router_first_match_wins() -> None:
    routes = [
        Route(name="a", model="m1", match_keywords=["debug"]),
        Route(name="b", model="m2", match_keywords=["debug"]),
    ]
    r = Router(routes, default_model="default")
    assert r.pick("debug it").route_name == "a"


def test_router_falls_through_to_default() -> None:
    r = Router(_routes(), default_model="qwen-7b")
    d = r.pick("tell me about ancient Rome")
    assert d.model == "qwen-7b"
    assert d.route_name == "default"
    assert d.reason == "default"


def test_router_empty_message_returns_default() -> None:
    r = Router(_routes(), default_model="qwen-7b")
    assert r.pick("").route_name == "default"
    assert r.pick("   ").route_name == "default"


def test_router_no_routes_returns_default() -> None:
    r = Router([], default_model="qwen-7b")
    assert r.pick("debug this").route_name == "default"


# --- Router: classifier fallback ---------------------------------------------


def _make_client(reply: str) -> MagicMock:
    """Build a MagicMock that quacks like openai.OpenAI for the classifier."""
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=reply))]
    client.chat.completions.create.return_value = response
    return client


def test_classifier_picks_named_route() -> None:
    client = _make_client("coder")
    r = Router(
        _routes(),
        default_model="qwen-7b",
        classifier_model="qwen-3b",
        client=client,
    )
    d = r.pick("rewrite this snippet")  # no keyword hit
    assert d.route_name == "coder"
    assert d.reason == "classifier"


def test_classifier_default_falls_through() -> None:
    client = _make_client("default")
    r = Router(
        _routes(),
        default_model="qwen-7b",
        classifier_model="qwen-3b",
        client=client,
    )
    d = r.pick("something off-topic")
    assert d.route_name == "default"


def test_classifier_unknown_name_falls_through() -> None:
    client = _make_client("does-not-exist")
    r = Router(
        _routes(),
        default_model="qwen-7b",
        classifier_model="qwen-3b",
        client=client,
    )
    assert r.pick("anything").route_name == "default"


def test_classifier_failure_falls_through() -> None:
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("boom")
    r = Router(
        _routes(),
        default_model="qwen-7b",
        classifier_model="qwen-3b",
        client=client,
    )
    assert r.pick("anything").route_name == "default"


def test_classifier_skipped_when_keyword_matches() -> None:
    client = _make_client("fast")
    r = Router(
        _routes(),
        default_model="qwen-7b",
        classifier_model="qwen-3b",
        client=client,
    )
    d = r.pick("debug it")
    # keyword match wins — classifier never called
    assert d.route_name == "coder"
    client.chat.completions.create.assert_not_called()


def test_classifier_handles_extra_punctuation() -> None:
    client = _make_client("coder.")
    r = Router(
        _routes(),
        default_model="qwen-7b",
        classifier_model="qwen-3b",
        client=client,
    )
    assert r.pick("anything").route_name == "coder"


# --- Presets -----------------------------------------------------------------


def test_common_preset_has_expected_routes() -> None:
    names = {r.name for r in PRESET_ROUTES["common"]}
    assert "coder" in names
    assert "fast" in names
