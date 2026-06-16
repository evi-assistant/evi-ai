"""Real multi-instance federation/peering e2e.

Spins up TWO real eVi servers (node A + node B, see the `federation_net`
fixture) against a fake LLM backend and exercises the cross-instance protocol
end to end — the layer that single-instance tests can't reach, and that should
have caught peering regressions:

- probe / health fingerprint of a live peer
- `/api/peers` reachability status from A's perspective
- `delegate()` actually running a task on peer B and returning its answer
- serve-gating (a node with `[federation] serve = false` returns 403)
- the peers-as-teammates distributed runner routing to a real peer

Note: the original "scan finds nothing" report was the *desktop* hard-coding
`--host 127.0.0.1` (a Rust/Tauri concern, covered separately) — the federation
*protocol* itself is what these tests pin down.
"""

from __future__ import annotations

import pytest

pytest.importorskip("httpx")
import httpx  # noqa: E402

from evi import federation  # noqa: E402

pytestmark = pytest.mark.e2e


def test_peer_health_probe(federation_net):
    host, port = federation._host_port(federation_net.b)
    info = federation.probe_evi(host, port, timeout=5.0)
    assert info is not None and info.get("version")


def test_check_peer_reachable(federation_net):
    peer = federation.Peer(name="boxB", url=federation_net.b, token="")
    st = federation.check_peer(peer, timeout=5.0)
    assert st["reachable"] is True


def test_node_a_lists_b_reachable(federation_net):
    d = httpx.get(federation_net.a + "/api/peers", timeout=10).json()
    boxb = next((p for p in d["peers"] if p["name"] == "boxB"), None)
    assert boxb is not None and boxb["reachable"] is True


def test_delegate_runs_task_on_peer(federation_net):
    peer = federation.Peer(name="boxB", url=federation_net.b, token="")
    out = federation.delegate(peer, "say hi", timeout=60.0)
    assert "Hello from the fake backend" in out  # B actually ran it


def test_serve_gating_returns_403_when_off(federation_net):
    # Node A has [federation] serve = false → it must refuse delegations.
    r = httpx.post(federation_net.a + "/api/federate", json={"task": "hi"}, timeout=10)
    assert r.status_code == 403


def test_serve_toggle_endpoint_enables_then_delegation_works(federation_net):
    # Flip A's serve ON via the API (the toggle that replaces hand-editing), then
    # a delegation to A succeeds.
    httpx.post(federation_net.a + "/api/peers/serve", json={"enabled": True}, timeout=10)
    try:
        peer_a = federation.Peer(name="boxA", url=federation_net.a, token="")
        out = federation.delegate(peer_a, "say hi", timeout=60.0)
        assert "Hello from the fake backend" in out
    finally:
        httpx.post(federation_net.a + "/api/peers/serve", json={"enabled": False}, timeout=10)


def test_distributed_team_runner_routes_to_peer(federation_net):
    from evi import teams

    peer = federation.Peer(name="boxB", url=federation_net.b, token="")
    run_one = teams.make_distributed_runner(lambda _t: "LOCAL", [peer])

    class _T:
        subject = "say hi"

    outs = [run_one(_T()) for _ in range(4)]
    assert any("[peer:boxB]" in o and "Hello from the fake backend" in o for o in outs)
    assert any(o == "LOCAL" for o in outs)  # round-robin also used local
