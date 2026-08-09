"""Federation preflight diagnostic (evi.federation.federation_preflight)."""

from __future__ import annotations

import io
import urllib.error

import evi.federation as fed


def _status(name: str, checks: list[dict]) -> str | None:
    for c in checks:
        if c["name"] == name:
            return c["status"]
    return None


def _ss(status: str, lan_ip: str = "192.168.1.5"):
    return lambda *a, **k: {"status": status, "lan_ip": lan_ip, "port": 8473,
                            "loopback": True, "lan": status == "lan"}


# ---- this-node posture ---------------------------------------------------

def test_serve_off_warns(monkeypatch) -> None:
    monkeypatch.setattr(fed, "self_serving_status", _ss("off", ""))
    out = fed.federation_preflight(serve=False, auth_token="", peers=[])
    assert _status("this node: serving", out) == "warn"


def test_lan_without_token_is_failsafe_fail(monkeypatch) -> None:
    monkeypatch.setattr(fed, "self_serving_status", _ss("lan"))
    out = fed.federation_preflight(serve=True, auth_token="", peers=[])
    assert _status("this node: serving", out) == "ok"
    assert _status("this node: auth token", out) == "fail"  # fail-safe will block peers


def test_lan_with_token_ok(monkeypatch) -> None:
    monkeypatch.setattr(fed, "self_serving_status", _ss("lan"))
    out = fed.federation_preflight(serve=True, auth_token="secret", peers=[])
    assert _status("this node: auth token", out) == "ok"


def test_loopback_warns(monkeypatch) -> None:
    monkeypatch.setattr(fed, "self_serving_status", _ss("loopback", ""))
    out = fed.federation_preflight(serve=True, auth_token="", peers=[])
    assert _status("this node: serving", out) == "warn"


# ---- per-peer ------------------------------------------------------------

def test_peer_unreachable_fails(monkeypatch) -> None:
    monkeypatch.setattr(fed, "self_serving_status", _ss("off", ""))
    monkeypatch.setattr(fed, "check_peer", lambda p, **k: {"reachable": False, "version": "", "model": ""})
    p = fed.Peer(name="vm", url="http://x:8473", token="")
    out = fed.federation_preflight(serve=False, peers=[p])
    assert _status("peer 'vm'", out) == "fail"
    # unreachable peers skip the delegation probe
    assert _status("peer 'vm': delegation", out) is None


def test_peer_reachable_runs_delegation(monkeypatch) -> None:
    monkeypatch.setattr(fed, "self_serving_status", _ss("off", ""))
    monkeypatch.setattr(fed, "check_peer", lambda p, **k: {"reachable": True, "version": "1.0.34", "model": "qwen"})
    monkeypatch.setattr(fed, "_probe_delegation",
                        lambda p, t: fed._pf(f"peer '{p.name}': delegation", "ok", "works (2s)"))
    p = fed.Peer(name="vm", url="http://x:8473", token="t")
    out = fed.federation_preflight(serve=False, peers=[p])
    assert _status("peer 'vm'", out) == "ok"
    assert _status("peer 'vm': delegation", out) == "ok"


# ---- delegation-error classification (the useful part) -------------------

def _http_error(code: str, body: bytes):
    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(body))
    return fake


def test_delegation_403_failsafe(monkeypatch) -> None:
    monkeypatch.setattr(fed.urllib.request, "urlopen",
                        _http_error(403, b'{"detail":"federation refused: serving on the network without an auth token"}'))
    c = fed._probe_delegation(fed.Peer(name="vm", url="http://x:8473", token=""), 5)
    assert c["status"] == "fail" and "auth token" in c["detail"].lower()


def test_delegation_403_serve_off(monkeypatch) -> None:
    monkeypatch.setattr(fed.urllib.request, "urlopen",
                        _http_error(403, b'{"detail":"federation serving is disabled (set [federation] serve = true)"}'))
    c = fed._probe_delegation(fed.Peer(name="vm", url="http://x:8473", token="t"), 5)
    assert c["status"] == "fail" and "serve" in c["detail"].lower()


def test_delegation_401_token_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(fed.urllib.request, "urlopen",
                        _http_error(401, b'{"error":"unauthorized"}'))
    c = fed._probe_delegation(fed.Peer(name="vm", url="http://x:8473", token="bad"), 5)
    assert c["status"] == "fail" and "401" in c["detail"]


def test_delegation_timeout_warns(monkeypatch) -> None:
    def slow(req, timeout=None):
        raise TimeoutError("timed out")
    monkeypatch.setattr(fed.urllib.request, "urlopen", slow)
    c = fed._probe_delegation(fed.Peer(name="vm", url="http://x:8473", token="t"), 5)
    assert c["status"] == "warn" and "slow" in c["detail"].lower()


def test_delegation_success(monkeypatch) -> None:
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"text":"ok"}'
    monkeypatch.setattr(fed.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    c = fed._probe_delegation(fed.Peer(name="vm", url="http://x:8473", token="t"), 5)
    assert c["status"] == "ok" and "ok" in c["detail"]
