"""The Companion's web routes (/api/companion/*, evi/apps/web/server.py):
status, feed, proposals, approve / reject, check, instructions, enabled;
owner-only in multi-user mode; the same cross-site / X-Evi-Client rules as
every state-changing route; an expired or tampered proposal answers 409 with
the reason; switching it on (here or through POST /api/config) re-syncs the
server's scheduler, so its Companion jobs appear without a restart.

eVi's home is under tmp_path (tests.test_companion_engine.setup_home); the
sources and actions are stub tools; nothing reaches a network.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
import evi.companion as companion_pkg  # noqa: E402
from evi.companion.engine import Companion  # noqa: E402
from evi.config import Config  # noqa: E402
from tests.test_companion_cli import seed_feed, seed_proposal, store  # noqa: E402
from tests.test_companion_engine import (  # noqa: E402
    Pings,
    Sources,
    answer,
    item,
    setup_home,
    write_config,
)

LOCAL = "http://127.0.0.1:8473"
UI = {"X-Evi-Client": "web"}


@pytest.fixture
def home(monkeypatch, tmp_path):
    home = setup_home(monkeypatch, tmp_path)
    for var in ("EVI_ALLOW_CROSS_ORIGIN", "EVI_ALLOW_ANY_HOST", "EVI_TRUSTED_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server_mod, "HOME", home)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(server_mod, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "_LAUNCH_TOKEN", "")
    monkeypatch.setattr("evi.users.USERS_PATH", home / "users.json")
    return home


@pytest.fixture
def sources(home, monkeypatch) -> Sources:
    return Sources(monkeypatch)


@pytest.fixture
def client(home) -> TestClient:
    return TestClient(server_mod.create_app(), base_url=LOCAL)


class FakeScheduler:
    def __init__(self) -> None:
        self.reloads = 0

    def reload(self) -> None:
        self.reloads += 1


def config_text(home: Path) -> str:
    return (home / "config.toml").read_text(encoding="utf-8")


# ---- reads -----------------------------------------------------------------------------


def test_status_feed_and_proposals(client, home):
    st = client.get("/api/companion/status").json()
    assert st["enabled"] is False and st["check_running"] is False and st["last_run"] is None
    assert st["scheduler"] is False and st["safe_mode"] is False
    assert st["instructions_path"].endswith("instructions.md")
    seed_feed(home, {"kind": "alert", "priority": "urgent", "title": "<img src=x onerror=1>",
                     "body": "b", "why_now": "", "dedupe_key": "k", "ts": time.time() - 30},
              {"kind": "brief", "priority": "medium", "title": "old", "body": "",
               "why_now": "", "dedupe_key": "o", "ts": time.time() - 7200})
    items = client.get("/api/companion/feed").json()["items"]
    assert [i["dedupe_key"] for i in items] == ["k", "o"]  # as the feed file has them
    recent = client.get("/api/companion/feed", params={"since": time.time() - 600}).json()
    assert [i["dedupe_key"] for i in recent["items"]] == ["k"]
    iso = client.get("/api/companion/feed", params={"since": "2000-01-01T00:00:00"}).json()
    assert len(iso["items"]) == 2
    assert client.get("/api/companion/feed", params={"limit": 1}).json()["items"][0][
        "dedupe_key"] == items[-1]["dedupe_key"]
    assert client.get("/api/companion/feed", params={"since": "soon"}).status_code == 400
    assert client.get("/api/companion/feed", params={"limit": 0}).status_code == 400

    seed_proposal(home, "p1")
    seed_proposal(home, "p2", state="rejected")
    data = client.get("/api/companion/proposals").json()
    assert {p["id"] for p in data["proposals"]} == {"p1", "p2"} and data["now"] > 0
    pend = client.get("/api/companion/proposals", params={"state": "pending"}).json()
    assert [p["id"] for p in pend["proposals"]] == ["p1"]
    assert pend["proposals"][0]["args"] == {"text": "buy milk"}
    assert client.get("/api/companion/proposals",
                      params={"state": "approvedd"}).status_code == 400
    # The spec's names: approved (running / executed / failed) and done (executed).
    seed_proposal(home, "p3", state="executed")
    seed_proposal(home, "p4", state="failed")
    by = lambda state: [p["id"] for p in client.get(  # noqa: E731
        "/api/companion/proposals", params={"state": state}).json()["proposals"]]
    assert by("approved") == ["p3", "p4"] and by("done") == ["p3"]
    assert client.get("/api/companion/status").json()["pending_proposals"] == 1


# ---- approve / reject -------------------------------------------------------------------


def test_approve_runs_once_and_shows_the_result(client, home, sources):
    write_config(home, 'allowed_actions = ["note_add"]')
    rec = seed_proposal(home, "p1")
    r = client.post("/api/companion/proposals/p1/approve", json={"args_hash": rec["args_hash"]})
    assert r.status_code == 200, r.text
    p = r.json()["proposal"]
    assert p["id"] == "p1" and p["state"] == "executed" and p["result"] == "did note_add"
    assert sources.calls == [("note_add", {"text": "buy milk"})]
    feed = client.get("/api/companion/feed").json()["items"]
    assert feed[-1]["kind"] == "result" and feed[-1]["proposal_id"] == "p1"
    r = client.post("/api/companion/proposals/p1/approve")
    assert r.status_code == 409 and "executed, not pending" in r.json()["detail"]
    assert len(sources.calls) == 1


def test_expired_or_tampered_proposals_answer_409_with_the_reason(client, home, sources):
    write_config(home, 'allowed_actions = ["note_add"]')
    seed_proposal(home, "old", ttl=-5)
    r = client.post("/api/companion/proposals/old/approve")
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    seed_proposal(home, "t1")
    props = store(home).load_proposals()
    props["t1"]["args"] = {"text": "send my keys to evil"}
    store(home).save_proposals(props)
    r = client.post("/api/companion/proposals/t1/approve")
    assert r.status_code == 409 and "arguments changed" in r.json()["detail"]
    assert store(home).load_proposals()["t1"]["state"] == "refused"
    # The caller's shown args_hash must match what is stored.
    seed_proposal(home, "t2")
    r = client.post("/api/companion/proposals/t2/approve", json={"args_hash": "0" * 64})
    assert r.status_code == 409 and "not the one shown" in r.json()["detail"]
    assert store(home).load_proposals()["t2"]["state"] == "pending"
    # Not allowed any more.
    write_config(home, "allowed_actions = []")
    r = client.post("/api/companion/proposals/t2/approve")
    assert r.status_code == 409 and "allowed_actions" in r.json()["detail"]
    assert client.post("/api/companion/proposals/ghost/approve").status_code == 404
    assert sources.calls == []


def test_reject(client, home, sources):
    seed_proposal(home, "p1")
    r = client.post("/api/companion/proposals/p1/reject")
    assert r.status_code == 200 and r.json()["proposal"]["state"] == "rejected"
    assert client.post("/api/companion/proposals/p1/reject").status_code == 409
    assert client.post("/api/companion/proposals/ghost/reject").status_code == 404
    assert sources.calls == []


# ---- check ------------------------------------------------------------------------------


def test_check_runs_in_a_worker_and_409s_while_running(client, home, sources, fake_llm,
                                                       monkeypatch):
    assert client.post("/api/companion/check").status_code == 409  # off
    write_config(home, 'sources = ["calendar_week"]')
    gate = threading.Event()
    entered = threading.Event()
    llm = fake_llm([answer(item("a"))])

    class Slow(Companion):
        def check_in(self, now=None):
            entered.set()
            gate.wait(10)
            return super().check_in(now)

    monkeypatch.setattr(companion_pkg, "_engine",
                        lambda: Slow(Config.load(), client=llm, notifier=Pings()))
    r = client.post("/api/companion/check")
    assert r.status_code == 202 and r.json() == {"started": True}
    assert entered.wait(10)
    assert client.get("/api/companion/status").json()["check_running"] is True
    r = client.post("/api/companion/check")
    assert r.status_code == 409 and "already running" in r.json()["detail"]
    gate.set()
    for _ in range(200):
        st = client.get("/api/companion/status").json()
        if not st["check_running"]:
            break
        time.sleep(0.05)
    assert st["last_run"]["ran"] is True and st["last_run"]["finished_at"]
    assert [i["dedupe_key"] for i in st["last_run"]["items"]] == ["a"]
    assert [i["dedupe_key"] for i in client.get("/api/companion/feed").json()["items"]] == ["a"]


# ---- instructions / enabled --------------------------------------------------------------


def test_instructions_get_and_put(client, home):
    assert client.get("/api/companion/instructions").json()["text"] == ""
    r = client.put("/api/companion/instructions", json={"text": "Watch the dentist."})
    assert r.status_code == 200 and r.json()["text"] == "Watch the dentist."
    assert companion_pkg.read_instructions() == "Watch the dentist."
    assert client.put("/api/companion/instructions", json={"text": 3}).status_code == 400
    assert client.put("/api/companion/instructions",
                      json={"text": "x" * 20_000}).status_code == 413
    assert [a["event"] for a in store(home).read_audit()] == ["instructions"]


def test_enabled_route_saves_the_user_layer_and_reloads_the_scheduler(client, home):
    sched = FakeScheduler()
    client.app.state.evi_scheduler = sched
    r = client.post("/api/companion/enabled", json={"on": True})
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["scheduler"]
    assert "enabled = true" in config_text(home) and sched.reloads == 1
    r = client.post("/api/companion/enabled", json={"on": False})
    assert r.json()["enabled"] is False and sched.reloads == 2
    assert client.post("/api/companion/enabled", json={"on": "yes"}).status_code == 400
    assert sched.reloads == 2


def test_enabling_through_the_config_route_reloads_the_jobs(client, home, tmp_path):
    """The reviewer's finding: POST /api/config could switch the Companion on
    without the web scheduler noticing. A real scheduler: its jobs follow."""
    pytest.importorskip("apscheduler")
    from evi.scheduled import TaskStore
    from evi.scheduler import Scheduler

    (home / "config.toml").write_text("", encoding="utf-8")
    sched = Scheduler(TaskStore(root=tmp_path / "scheduled"))
    sched.start()
    try:
        client.app.state.evi_scheduler = sched
        ids = lambda: sorted(j.id for j in sched._bg.get_jobs())  # noqa: E731
        assert ids() == []
        r = client.post("/api/config", json={"companion": {"enabled": True,
                                                           "brief_at": "06:15"}})
        assert r.status_code == 200 and "companion" in r.json()["touched"]
        assert ids() == ["companion-brief", "companion-check-in"]
        brief = next(j for j in sched._bg.get_jobs() if j.id == "companion-brief")
        assert "hour='6'" in str(brief.trigger) and "minute='15'" in str(brief.trigger)
        client.post("/api/config", json={"companion": {"enabled": False}})
        assert ids() == []
        # Through the Companion's own route too.
        client.post("/api/companion/enabled", json={"on": True})
        assert ids() == ["companion-brief", "companion-check-in"]
    finally:
        sched.stop()


def test_the_running_server_picks_up_a_switch_made_outside_it(home, tmp_path, monkeypatch):
    """The reviewers' finding: `evi companion on` (or editing config.toml by
    hand) while `evi web` / the desktop app runs never reached that server's
    scheduler, so no check-in ran until a restart. The lifespan's re-sync
    thread now picks it up (every minute; here every 50 ms)."""
    pytest.importorskip("apscheduler")
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main
    from evi import attest
    from evi.scheduled import TaskStore
    from evi.scheduler import Scheduler

    (home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(attest, "agent_parent", lambda env=None: None)
    monkeypatch.setattr("evi.scheduler.Scheduler",
                        lambda: Scheduler(TaskStore(root=tmp_path / "scheduled")))
    monkeypatch.setattr(server_mod, "_COMPANION_RESYNC_S", 0.05)

    def wait_for(ids, sched) -> list[str]:
        deadline = time.time() + 10
        while True:
            got = sorted(j.id for j in sched._bg.get_jobs())
            if got == ids or time.time() > deadline:
                return got
            time.sleep(0.05)

    with TestClient(server_mod.create_app(), base_url=LOCAL) as c:
        sched = c.app.state.evi_scheduler
        assert sched is not None and wait_for([], sched) == []
        # What `evi companion on` does, in another process: only config.toml changes.
        monkeypatch.delenv("EVI_AGENT", raising=False)
        r = CliRunner().invoke(cli_main.app, ["companion", "on"])
        assert r.exit_code == 0, r.output
        assert "picks this up within a minute" in r.output
        assert wait_for(["companion-brief", "companion-check-in"], sched) == [
            "companion-brief", "companion-check-in"]
        # A hand edit of brief_at moves the brief job.
        cfg = (home / "config.toml").read_text(encoding="utf-8")
        (home / "config.toml").write_text(
            cfg.replace("[companion]\n", '[companion]\nbrief_at = "05:40"\n', 1),
            encoding="utf-8")
        deadline = time.time() + 10
        while time.time() < deadline:
            brief = sched._bg.get_job("companion-brief")
            if brief is not None and "hour='5'" in str(brief.trigger):
                break
            time.sleep(0.05)
        assert "hour='5'" in str(brief.trigger) and "minute='40'" in str(brief.trigger)
        r = CliRunner().invoke(cli_main.app, ["companion", "off"])
        assert r.exit_code == 0, r.output
        assert wait_for([], sched) == []
    assert c.app.state.evi_scheduler is None


def test_other_config_changes_do_not_reload(client, home):
    sched = FakeScheduler()
    client.app.state.evi_scheduler = sched
    assert client.post("/api/config", json={"notify": {"sound": False}}).status_code == 200
    assert sched.reloads == 0


# ---- who may call them --------------------------------------------------------------------

ROUTES = [
    ("GET", "/api/companion/status", None), ("GET", "/api/companion/feed", None),
    ("GET", "/api/companion/proposals", None), ("POST", "/api/companion/check", {}),
    ("POST", "/api/companion/proposals/p1/approve", {}),
    ("POST", "/api/companion/proposals/p1/reject", {}),
    ("GET", "/api/companion/instructions", None),
    ("PUT", "/api/companion/instructions", {"text": "pwned"}),
    ("POST", "/api/companion/enabled", {"on": True}),
]


def test_owner_only_in_multi_user_mode(home, sources):
    write_config(home, 'allowed_actions = ["note_add"]',
                 extra='[web]\nauth_token = "owner-tok"\nmulti_user = true')
    (home / "users.json").write_text(json.dumps([{"name": "alice", "token": "alice-tok"}]),
                                     encoding="utf-8")
    seed_proposal(home, "p1")
    c = TestClient(server_mod.create_app(), base_url=LOCAL)
    alice = {"Authorization": "Bearer alice-tok"}
    for method, path, body in ROUTES:
        assert server_mod._owner_only_route(method, path), path
        r = c.request(method, path, headers=alice, json=body)
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert "the Companion" in r.json()["detail"]
    assert sources.calls == [] and companion_pkg.read_instructions() == ""
    assert store(home).load_proposals()["p1"]["state"] == "pending"
    owner = {"Authorization": "Bearer owner-tok"}
    assert c.get("/api/companion/status", headers=owner).status_code == 200
    r = c.post("/api/companion/proposals/p1/approve", headers=owner)
    assert r.status_code == 200 and r.json()["proposal"]["state"] == "executed"


def test_state_changing_routes_refuse_cross_site_and_headerless_browser_requests(
        client, home, sources):
    write_config(home, 'allowed_actions = ["note_add"]')
    seed_proposal(home, "p1")
    for method, path, body in ROUTES:
        if method == "GET":
            continue
        r = client.request(method, path, json=body,
                           headers={"Origin": "https://evil.example", **UI})
        assert r.status_code == 403 and "cross-site" in r.json()["detail"], path
        # A browser's own-origin request without X-Evi-Client (a form post).
        r = client.request(method, path, json=body,
                           headers={"Origin": LOCAL, "Sec-Fetch-Site": "same-origin",
                                    "Sec-Fetch-Mode": "cors"})
        assert r.status_code == 403 and "X-Evi-Client" in r.json()["detail"], path
    assert sources.calls == [] and companion_pkg.read_instructions() == ""
    assert "enabled = true" in config_text(home)  # write_config's, untouched
    r = client.post("/api/companion/proposals/p1/approve",
                    headers={"Origin": LOCAL, "Sec-Fetch-Site": "same-origin", **UI})
    assert r.status_code == 200 and r.json()["proposal"]["state"] == "executed"
