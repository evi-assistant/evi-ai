"""Tests for opt-in crash reporting (Phase 52): the PII scrubber, the
reporter factory (defaults to inert), and the chained excepthook."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evi import reporting  # noqa: E402


def _telemetry(*, crash_reports=False, dsn="", backend="sentry"):
    return SimpleNamespace(
        telemetry=SimpleNamespace(crash_reports=crash_reports, dsn=dsn, backend=backend)
    )


# --- scrubber ------------------------------------------------------------


def test_scrub_rewrites_home_and_user():
    ev = {"msg": r"failed at C:\Users\alice\evi\x.py for alice"}
    out = reporting.scrub_event(ev, home=r"C:\Users\alice", user="alice")
    assert "<HOME>" in out["msg"]
    assert "alice" not in out["msg"]
    assert "<USER>" in out["msg"]


def test_scrub_redacts_secrets():
    ev = {"a": "key sk-ABCDEFGH12345678 here",
          "b": "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
          "c": "Authorization: Bearer abc.def-ghi"}
    out = reporting.scrub_event(ev, home="", user="")
    # The shared evi.redact markers say what kind of secret was there.
    assert out["a"] == "key [REDACTED:api_key] here"
    assert out["b"] == "token [REDACTED:github_token]"
    assert out["c"] == "Authorization: Bearer [REDACTED:bearer]"


def test_scrub_redacts_this_machines_known_keys(monkeypatch):
    # A key with no recognisable shape, known only because it is in the env.
    from evi import redact

    monkeypatch.setenv("ACME_API_KEY", "Zq8vN2kLp4Xr7Tw1Ys5M")
    redact.refresh()
    ev = {"exception": {"values": [{"value": "401 for key Zq8vN2kLp4Xr7Tw1Ys5M"}]}}
    out = reporting.scrub_event(ev, home="", user="")
    assert out["exception"]["values"][0]["value"] == "401 for key [REDACTED:ACME_API_KEY]"


def test_scrub_redacts_a_token_before_rewriting_the_username():
    # The username inside the token must not break it up before it is seen.
    tok = "ghp_aliceABCDEFGHIJKLMNOPQRSTUVWXYZ0123"
    out = reporting.scrub_event({"m": f"push with {tok} as alice"}, home="", user="alice")
    assert out["m"] == "push with [REDACTED:github_token] as <USER>"


@pytest.mark.parametrize("user", ["ted", "act", "api", "token", "red"])
def test_scrub_leaves_markers_whole_whatever_the_username(user):
    # "[REDACTED:github_token]" holds "ted", "act", "red" and "token": the
    # username rewrite must not reach into it.
    tok = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    out = reporting.scrub_event({"m": f"push {tok} as {user}"}, home="", user=user)
    assert out["m"] == "push [REDACTED:github_token] as <USER>"


@pytest.mark.parametrize("text,want", [
    # The looser shapes the scrubber always caught (evi.redact's are tighter).
    ("requests sent Bearer s3cr3tT0k", "requests sent Bearer [REDACTED:bearer]"),
    ("token=Bearer abcdefghijklmnopqrstuvwxyz", "token=Bearer [REDACTED:bearer]"),
    ("upstream said: Bearer eyJhbGciOi.abc.def", "upstream said: Bearer [REDACTED:bearer]"),
    ("key sk-abcdefghijklmnop", "key [REDACTED:api_key]"),
    ("cfg sk-live-abc12345", "cfg [REDACTED:api_key]"),
    ("slack xoxb-abcdefghijkl", "slack [REDACTED:slack_token]"),
    # A token after an escape in a repr() or JSON-encoded message.
    (repr("config:\nsk-proj-Ab12Cd34Ef56Gh78Ij90Kl12Mn34"), "'config:\\n[REDACTED:api_key]'"),
    ('{"body": "x\\nghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"}',
     '{"body": "x\\n[REDACTED:github_token]"}'),
    (repr(["a\tghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"]), "['a\\t[REDACTED:github_token]']"),
])
def test_scrub_is_no_weaker_than_the_regex_it_replaced(text, want):
    out = reporting.scrub_event({"exception": {"values": [{"value": text}]}}, home="", user="")
    assert out["exception"]["values"][0]["value"] == want


def test_scrub_drops_sensitive_keys():
    ev = {
        "exception": {"values": [{"stacktrace": {"frames": [
            {"function": "f", "vars": {"prompt": "secret user text"}},
        ]}}]},
        "request": {"headers": {"cookie": "x"}},
        "extra": {"anything": "y"},
    }
    out = reporting.scrub_event(ev, home="", user="")
    frame = out["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert frame["vars"] == "<redacted>"          # locals dropped
    assert "secret user text" not in str(out)     # ...everywhere
    assert out["request"] == "<redacted>"
    assert out["extra"] == "<redacted>"


def test_scrub_anonymises_server_and_drops_user():
    out = reporting.scrub_event({"server_name": "my-laptop", "user": {"ip_address": "1.2.3.4"}},
                                home="", user="")
    assert out["server_name"] == "evi"
    assert "user" not in out


def test_make_scrubber_returns_before_send_callable():
    fn = reporting.make_scrubber()
    out = fn({"server_name": "host"}, None)
    assert out["server_name"] == "evi"


# --- factory: defaults to inert -----------------------------------------


def test_init_reporting_off_by_default(monkeypatch):
    monkeypatch.delenv("EVI_CRASH_REPORTS", raising=False)
    monkeypatch.delenv("EVI_TELEMETRY_DSN", raising=False)
    r = reporting.init_reporting(_telemetry(crash_reports=False, dsn="https://x@e/1"))
    assert isinstance(r, reporting.NullReporter)
    assert r.active is False


def test_init_reporting_enabled_but_no_dsn_is_null(monkeypatch):
    monkeypatch.delenv("EVI_TELEMETRY_DSN", raising=False)
    r = reporting.init_reporting(_telemetry(crash_reports=True, dsn=""))
    assert isinstance(r, reporting.NullReporter)


def test_init_reporting_backend_none_is_null():
    r = reporting.init_reporting(_telemetry(crash_reports=True, dsn="https://x@e/1", backend="none"))
    assert isinstance(r, reporting.NullReporter)


def test_init_reporting_env_can_enable(monkeypatch):
    monkeypatch.setenv("EVI_CRASH_REPORTS", "1")
    monkeypatch.setenv("EVI_TELEMETRY_DSN", "https://pub@example.invalid/1")
    inits = {}
    monkeypatch.setattr(reporting, "SentryReporter",
                        lambda dsn, release: inits.update(dsn=dsn, release=release) or SimpleNamespace(active=True))
    r = reporting.init_reporting(_telemetry(crash_reports=False, dsn=""))
    assert inits["dsn"] == "https://pub@example.invalid/1"
    assert r.active is True


def test_init_reporting_degrades_when_sentry_init_raises(monkeypatch):
    def boom(dsn, release):
        raise RuntimeError("no network")
    monkeypatch.setattr(reporting, "SentryReporter", boom)
    r = reporting.init_reporting(_telemetry(crash_reports=True, dsn="https://x@e/1"))
    assert isinstance(r, reporting.NullReporter)


# --- SentryReporter wiring (sentry-sdk mocked) --------------------------


def test_sentry_reporter_inits_with_scrubber(monkeypatch):
    sentry_sdk = pytest.importorskip("sentry_sdk")  # optional [telemetry] extra

    captured = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sentry_sdk, "capture_exception", lambda exc: captured.setdefault("exc", exc))
    monkeypatch.setattr(sentry_sdk, "set_context", lambda name, data: captured.setdefault("ctx", (name, data)))

    rep = reporting.SentryReporter("https://pub@example.invalid/1", release="evi@test")
    assert captured["send_default_pii"] is False
    assert captured["server_name"] == "evi"
    assert callable(captured["before_send"])

    rep.capture(ValueError("boom"), {"source": "test"})
    assert isinstance(captured["exc"], ValueError)
    assert captured["ctx"][0] == "evi"


# --- excepthook ----------------------------------------------------------


def test_install_excepthook_noop_for_null():
    before = sys.excepthook
    reporting.install_excepthook(reporting.NullReporter())
    assert sys.excepthook is before  # unchanged for inert reporter


def test_install_excepthook_chains_and_reports(monkeypatch):
    seen = {}

    class Rep:
        active = True
        def capture(self, exc, context=None):
            seen["exc"] = exc
            seen["ctx"] = context

    orig_called = {}
    monkeypatch.setattr(sys, "excepthook", lambda *a: orig_called.setdefault("hit", a))
    reporting.install_excepthook(Rep())
    exc = ValueError("x")
    sys.excepthook(ValueError, exc, None)
    assert seen["exc"] is exc
    assert orig_called["hit"][1] is exc  # original hook still invoked
