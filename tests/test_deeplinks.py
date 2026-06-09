"""Tests for evi:// deep links (lighter/later item)."""

from __future__ import annotations

import pytest

from evi import deeplinks


def test_build_session_link():
    assert deeplinks.build_link("session", "abc123") == "evi://session/abc123"


def test_build_with_params():
    url = deeplinks.build_link("workflow", "research", run="1")
    assert url == "evi://workflow/research?run=1"


def test_build_requires_kind():
    with pytest.raises(deeplinks.DeepLinkError):
        deeplinks.build_link("")


def test_build_quotes_value():
    url = deeplinks.build_link("session", "a/b c")
    assert url == "evi://session/a%2Fb%20c"


def test_parse_roundtrip():
    kind, value, params = deeplinks.parse_link("evi://session/abc123")
    assert kind == "session" and value == "abc123" and params == {}


def test_parse_quoted_value_and_params():
    kind, value, params = deeplinks.parse_link("evi://session/a%2Fb?x=1&y=2")
    assert value == "a/b" and params == {"x": "1", "y": "2"}


def test_parse_rejects_other_scheme():
    with pytest.raises(deeplinks.DeepLinkError):
        deeplinks.parse_link("https://example.com/session/x")


def test_parse_rejects_missing_route():
    with pytest.raises(deeplinks.DeepLinkError):
        deeplinks.parse_link("evi://")


def test_to_web_path():
    assert deeplinks.to_web_path("evi://session/s1") == "/?session=s1"
    assert deeplinks.to_web_path("evi://workflow/research") == "/?workflow=research"
    assert deeplinks.to_web_path("evi://new") == "/"
    # unknown route → root, never errors
    assert deeplinks.to_web_path("evi://bogus/thing") == "/"


def test_to_web_path_requantizes_value():
    assert deeplinks.to_web_path("evi://session/a%2Fb") == "/?session=a%2Fb"
