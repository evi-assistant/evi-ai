"""Tests for agent teams — shared claimable task list + drain."""

from __future__ import annotations

import threading

from evi import teams
from evi.teams import TeamStore, drain_team, ready_tasks


def _store(tmp_path) -> TeamStore:
    return TeamStore(path=tmp_path / "team.json")


# ---- store CRUD + ids ------------------------------------------------------


def test_add_and_load(tmp_path):
    s = _store(tmp_path)
    a = s.add("first")
    b = s.add("second", [a.id])
    assert a.id == "t1" and b.id == "t2"
    loaded = s.load()
    assert [t.subject for t in loaded] == ["first", "second"]
    assert loaded[1].blocked_by == ["t1"]


def test_ready_respects_dependencies(tmp_path):
    s = _store(tmp_path)
    a = s.add("a")
    s.add("b", [a.id])
    ready = ready_tasks(s.load())
    assert [t.id for t in ready] == ["t1"]  # b blocked until a completes
    s.complete(a.id, "done")
    ready = ready_tasks(s.load())
    assert [t.id for t in ready] == ["t2"]


# ---- claim atomicity -------------------------------------------------------


def test_claim_is_exclusive(tmp_path):
    s = _store(tmp_path)
    for i in range(20):
        s.add(f"task {i}")
    claimed: list[str] = []
    lock = threading.Lock()

    def grab():
        while True:
            t = s.claim("w")
            if t is None:
                return
            with lock:
                claimed.append(t.id)

    threadlist = [threading.Thread(target=grab) for _ in range(8)]
    for th in threadlist:
        th.start()
    for th in threadlist:
        th.join()
    # every task claimed exactly once (no double-claim despite 8 racers)
    assert sorted(claimed) == sorted(t.id for t in s.load())
    assert len(claimed) == len(set(claimed)) == 20


# ---- drain (model-free) ----------------------------------------------------


def test_drain_runs_all_in_dependency_order(tmp_path):
    s = _store(tmp_path)
    a = s.add("build")
    b = s.add("test", [a.id])
    s.add("ship", [b.id])

    order: list[str] = []
    olock = threading.Lock()

    def run_one(task):
        with olock:
            order.append(task.id)
        return f"did {task.subject}"

    final = drain_team(s, run_one, max_workers=4)
    assert all(t.status == "completed" for t in final)
    # dependency order respected: build before test before ship
    assert order.index("t1") < order.index("t2") < order.index("t3")


def test_drain_marks_failures_and_does_not_hang(tmp_path):
    s = _store(tmp_path)
    a = s.add("flaky")
    s.add("depends-on-flaky", [a.id])

    def run_one(task):
        if task.id == "t1":
            raise RuntimeError("boom")
        return "ok"

    final = {t.id: t for t in drain_team(s, run_one, max_workers=2)}
    assert final["t1"].status == "failed" and "boom" in final["t1"].result
    # the dependent never becomes ready (its dep failed) -> stays pending, run ended
    assert final["t2"].status == "pending"


# ---- plan population (id remap) --------------------------------------------


def test_populate_from_plan_remaps_dependency_ids(tmp_path):
    s = _store(tmp_path)
    plan = [
        {"id": "1", "subject": "design"},
        {"id": "2", "subject": "implement", "blocked_by": ["1"]},
        {"id": "3", "subject": "review", "blocked_by": ["2", "99"]},  # 99 is dangling
    ]
    tasks = {t.subject: t for t in teams.populate_from_plan(s, plan)}
    # lead ids "1"/"2" remap to store ids; the dangling "99" is dropped
    assert tasks["implement"].blocked_by == [tasks["design"].id]
    assert tasks["review"].blocked_by == [tasks["implement"].id]


def test_clear(tmp_path):
    s = _store(tmp_path)
    s.add("x")
    s.clear()
    assert s.load() == []


# ---- peers-as-teammates (distributed runner) -------------------------------


class _Task:
    def __init__(self, subject):
        self.subject = subject


def test_distributed_runner_round_robins_local_and_peers(monkeypatch):
    from evi import federation

    p1 = federation.Peer(name="gpu1", url="http://gpu1:8473", token="")
    p2 = federation.Peer(name="gpu2", url="http://gpu2:8473", token="")
    monkeypatch.setattr(federation, "delegate",
                        lambda peer, task, **kw: f"done-by-{peer.name}")

    run_one = teams.make_distributed_runner(lambda t: "done-by-local", [p1, p2])
    out = [run_one(_Task(f"t{i}")) for i in range(6)]
    # cycle is local, gpu1, gpu2, local, gpu1, gpu2
    assert out[0] == "done-by-local"
    assert out[1] == "[peer:gpu1] done-by-gpu1"
    assert out[2] == "[peer:gpu2] done-by-gpu2"
    assert out[3] == "done-by-local"
    # work landed on every target
    assert any("gpu1" in o for o in out) and any("gpu2" in o for o in out)


def test_distributed_runner_falls_back_to_local_on_peer_error(monkeypatch):
    from evi import federation

    p1 = federation.Peer(name="flaky", url="http://flaky:8473", token="")

    def boom(peer, task, **kw):
        raise federation.FederationError("unreachable")

    monkeypatch.setattr(federation, "delegate", boom)
    run_one = teams.make_distributed_runner(lambda t: "LOCAL", [p1])
    # first call -> local; second -> peer (errors) -> local fallback
    assert run_one(_Task("a")) == "LOCAL"
    assert run_one(_Task("b")) == "LOCAL"  # peer failed, ran locally
