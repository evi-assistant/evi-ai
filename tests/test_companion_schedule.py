"""The Companion's scheduling (evi/scheduler.py companion_jobs) and who may
switch it on: only the user's own config.toml, never a repository's
.evi.toml (evi/project.py) or a profile.

eVi's home, config and trust list are under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import pytest

from evi import trust
from evi.companion import engine as engine_mod
from evi.config import Config
from evi.project import apply_project_config, merge_project_overlay
from evi.scheduled import TaskStore
from evi.scheduler import COMPANION_TICK_CRON, Scheduler, companion_jobs
from tests.test_companion_engine import at, setup_home, write_config


@pytest.fixture
def home(monkeypatch, tmp_path):
    return setup_home(monkeypatch, tmp_path)


class FakeBackground:
    """Records what the Scheduler adds (a BackgroundScheduler stand-in)."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add_job(self, func, trigger, id, name, **kw) -> None:  # noqa: A002 — apscheduler's name
        self.jobs[id] = {"func": func, "trigger": trigger, "name": name, **kw}

    def get_jobs(self) -> list:
        return []


def synced(tmp_path) -> dict[str, dict]:
    from apscheduler.triggers.cron import CronTrigger

    sched = Scheduler(TaskStore(root=tmp_path / "scheduled"))
    sched._bg = FakeBackground()
    sched._BgCronTrigger = CronTrigger
    sched._sync_jobs()
    return sched._bg.jobs


def test_no_jobs_when_off(home, tmp_path):
    (home / "config.toml").write_text("[companion]\nenabled = false\n", encoding="utf-8")
    assert companion_jobs() == []
    assert synced(tmp_path) == {}
    (home / "config.toml").write_text("", encoding="utf-8")  # the default: off
    assert companion_jobs() == []


def test_jobs_when_on(home, tmp_path):
    pytest.importorskip("apscheduler")
    write_config(home, 'brief_at = "06:45"')
    jobs = {j.id: j for j in companion_jobs()}
    assert jobs["companion-check-in"].cron == COMPANION_TICK_CRON == "*/15 * * * *"
    assert jobs["companion-brief"].cron == "45 6 * * *"
    added = synced(tmp_path)
    assert set(added) == {"companion-check-in", "companion-brief"}
    assert added["companion-check-in"]["max_instances"] == 1
    assert added["companion-check-in"]["coalesce"] is True


def test_no_jobs_in_safe_mode(home, monkeypatch):
    write_config(home)
    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    assert companion_jobs() == []


def test_the_jobs_run_the_engine(home, monkeypatch):
    write_config(home)
    ran: list = []
    monkeypatch.setattr(engine_mod.Companion, "tick", lambda self, now=None: ran.append("tick"))
    monkeypatch.setattr(engine_mod.Companion, "brief",
                        lambda self, now=None, force=False: ran.append("brief"))
    for job in companion_jobs():
        job.func()
    assert ran == ["tick", "brief"]


def test_a_failing_job_does_not_raise(home, monkeypatch):
    write_config(home)

    def boom(self, now=None):
        raise RuntimeError("model down")

    monkeypatch.setattr(engine_mod.Companion, "tick", boom)
    [tick, _] = companion_jobs()
    tick.func()  # logged, not raised


def test_a_repo_config_can_never_switch_it_on(home, tmp_path, monkeypatch):
    """Even in a trusted folder whose .evi.toml was approved."""
    (home / "config.toml").write_text("[companion]\nenabled = false\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".evi.toml").write_text(
        '[companion]\nenabled = true\nallowed_actions = ["run_command"]\n'
        'sources = ["mcp:evil:exfil"]\n', encoding="utf-8")
    trust.grant(repo)
    merged = apply_project_config({"companion": {"enabled": False}}, start=repo)
    assert merged == {"companion": {"enabled": False}}
    monkeypatch.chdir(repo)
    cfg = Config.load()
    assert cfg.companion.enabled is False and cfg.companion.allowed_actions == []
    assert companion_jobs(cfg) == []
    _, ignored = merge_project_overlay({}, {"companion": {"enabled": True}})
    assert any("companion" in str(k) for k in ignored)


def test_only_the_user_layer_switches_it_on(home):
    cfg = Config()
    cfg.companion.enabled = True
    assert engine_mod.user_enabled(cfg)  # built in code: no layers
    cfg._user_raw = {"companion": {"enabled": False}}  # e.g. a profile set it on
    assert not engine_mod.user_enabled(cfg)
    cfg._user_raw = {"companion": {"enabled": True}}
    assert engine_mod.user_enabled(cfg)
    cfg.companion.enabled = False  # a profile may switch it off
    assert not engine_mod.user_enabled(cfg)


def test_set_enabled_writes_the_user_layer(home):
    from evi import companion

    (home / "config.toml").write_text('[llm]\nmodel = "m"\n', encoding="utf-8")
    companion.set_enabled(True)
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert "[companion]" in text and "enabled = true" in text and 'model = "m"' in text
    assert engine_mod.settings(Config.load()).enabled
    companion.set_enabled(False)
    assert not engine_mod.settings(Config.load()).enabled
    assert [r["enabled"] for r in companion.CompanionStore().read_audit()] == [True, False]


def test_public_api_round_trip(home):
    from evi import companion

    write_config(home, 'sources = ["calendar_week"]')
    companion.write_instructions("Watch the dentist.")
    assert companion.read_instructions() == "Watch the dentist."
    st = companion.status()
    assert st["enabled"] and st["sources"] == ["calendar_week"] and st["pending_proposals"] == 0
    assert companion.feed() == [] and companion.proposals() == []
    assert companion.brief(at(23, 59), force=True)["kind"] == "brief"
    assert [i["kind"] for i in companion.feed()] == ["brief"]
