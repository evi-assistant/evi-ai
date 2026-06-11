"""End-to-end tests that drive the real `evi` CLI per subsystem.

Each test runs offline (no LLM backend / network) — it exercises the commands
that don't need a model. Model-driven commands (`chat`, `run`, `eval run`,
`recipe run`, `batch`) are covered by the browser e2e + unit suites instead.
"""

from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.e2e


# --- helpers --------------------------------------------------------------


def _make_skill(workdir, name="demo", desc="A demo skill.", body="Do the thing."):
    d = workdir / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8"
    )
    return d


def _make_plugin(workdir, name="demo-plugin"):
    d = workdir / name
    (d / "commands").mkdir(parents=True)
    (d / "plugin.toml").write_text(
        f'name = "{name}"\nversion = "0.1.0"\ndescription = "A demo plugin."\n',
        encoding="utf-8",
    )
    (d / "commands" / "hi.md").write_text("Say hello.\n", encoding="utf-8")
    return d


def _seed_transcript(home, day="2026-06-01", sid="s1"):
    d = home / "transcripts" / day
    d.mkdir(parents=True, exist_ok=True)
    rows = [
        {"role": "user", "content": "what is 2+2?", "ts": 1000.0},
        {"role": "assistant", "content": "4", "ts": 1001.0},
    ]
    (d / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


# --- core / config --------------------------------------------------------


def test_config(evi_cli):
    assert "config.toml" in evi_cli("config", "path").out
    show = evi_cli("config", "show")
    assert "llm" in show.out.lower()


def test_doctor(evi_cli):
    # doctor may exit non-zero when no backend is reachable; we only assert it
    # produces a diagnostics report rather than crashing.
    r = evi_cli("doctor", check=False, timeout=60)
    assert r.code in (0, 1)
    assert any(w in r.out.lower() for w in ("python", "backend", "config", "evi"))


def test_help(evi_cli):
    assert "Usage" in evi_cli("--help").out


# --- agents / tools surfaces ----------------------------------------------


def test_agents_list(evi_cli):
    assert evi_cli("agents").code == 0


def test_tools_list(evi_cli):
    assert evi_cli("tools").code == 0


# --- skills ----------------------------------------------------------------


def test_skill_lifecycle(evi_cli):
    src = _make_skill(evi_cli.workdir, "reviewer", "Reviews code.", "Review carefully.")
    assert "imported" in evi_cli("skill", "import", str(src)).out
    assert "reviewer" in evi_cli("skill", "list").out
    assert "Review carefully" in evi_cli("skill", "show", "reviewer").out
    assert "removed" in evi_cli("skill", "remove", "reviewer").out
    assert "reviewer" not in evi_cli("skill", "list").out


def test_skill_import_with_companion_files(evi_cli):
    src = _make_skill(evi_cli.workdir, "pdf", "Fill PDFs.", "See reference.md")
    (src / "reference.md").write_text("maps", encoding="utf-8")
    out = evi_cli("skill", "import", str(src)).out
    assert "pdf" in out
    show = evi_cli("skill", "show", "pdf").out
    assert "reference.md" in show  # companion file surfaced


# --- plugins ---------------------------------------------------------------


def test_plugin_lifecycle(evi_cli):
    src = _make_plugin(evi_cli.workdir, "demo-plugin")
    assert "demo-plugin" in evi_cli("plugin", "add", str(src)).out
    assert "demo-plugin" in evi_cli("plugin", "list").out
    assert evi_cli("plugin", "search", "").code == 0  # no index → graceful
    assert evi_cli("plugin", "remove", "demo-plugin").code == 0
    assert "demo-plugin" not in evi_cli("plugin", "list").out


# --- guardrails ------------------------------------------------------------


def test_guardrails(evi_cli):
    (evi_cli.home / "guardrails.toml").write_text(
        'enabled = true\n[[rule]]\nname = "secret"\npattern = "api_key"\naction = "block"\n',
        encoding="utf-8",
    )
    assert "guardrails.toml" in evi_cli("guardrails", "path").out
    assert "secret" in evi_cli("guardrails", "list").out
    blocked = evi_cli("guardrails", "test", "here is my api_key=123").out
    assert "BLOCK" in blocked.upper()


# --- multi-model routing ---------------------------------------------------


def test_route(evi_cli):
    assert evi_cli("route", "add", "code", "--model", "coder-x",
                   "--keywords", "debug,refactor").code == 0
    listing = evi_cli("route", "list").out
    assert "code" in listing and "coder-x" in listing
    decided = evi_cli("route", "test", "please debug this crash").out
    assert "coder-x" in decided or "code" in decided
    assert evi_cli("route", "remove", "code").code == 0


# --- recipes ---------------------------------------------------------------


def test_recipe(evi_cli):
    assert evi_cli("recipe", "new", "standup").code == 0
    assert "standup" in evi_cli("recipe", "list").out
    assert evi_cli("recipe", "show", "standup").code == 0


# --- workflows -------------------------------------------------------------


def test_workflow(evi_cli):
    assert evi_cli("workflow", "new", "research").code == 0
    assert "research" in evi_cli("workflow", "list").out
    assert evi_cli("workflow", "show", "research").code == 0


# --- evals -----------------------------------------------------------------


def test_eval(evi_cli):
    assert evi_cli("eval", "new", "smoke").code == 0
    assert "smoke" in evi_cli("eval", "list").out


# --- scheduler -------------------------------------------------------------


def test_schedule(evi_cli):
    added = evi_cli("schedule", "add", "--name", "nightly",
                    "--cron", "0 3 * * *", "--prompt", "daily digest").out
    assert "nightly" in added
    assert "nightly" in evi_cli("schedule", "list").out
    m = re.search(r"\b([0-9a-f]{6,})\b", added)
    if m:
        task_id = m.group(1)
        assert evi_cli("schedule", "disable", task_id).code == 0
        assert evi_cli("schedule", "remove", task_id).code == 0


# --- sessions --------------------------------------------------------------


def test_sessions_empty(evi_cli):
    assert evi_cli("sessions", "list").code == 0


# --- output styles ---------------------------------------------------------


def test_style_list(evi_cli):
    assert evi_cli("style", "list").code == 0


# --- federation / peers ----------------------------------------------------


def test_peer_lifecycle(evi_cli):
    assert evi_cli("peer", "list").code == 0
    # add an (unreachable) peer; status note shouldn't fail the command
    out = evi_cli("peer", "add", "gpu", "http://127.0.0.1:1").out
    assert "added" in out and "gpu" in out
    assert "gpu" in evi_cli("peer", "list").out
    # duplicate rejected without --overwrite
    assert evi_cli("peer", "add", "gpu", "http://x:8473", check=False).code == 1
    assert evi_cli("peer", "add", "gpu", "http://x:8473", "--overwrite").code == 0
    assert "removed" in evi_cli("peer", "remove", "gpu").out
    assert evi_cli("peer", "remove", "gpu", check=False).code == 1


# --- web auth token --------------------------------------------------------


def test_token(evi_cli):
    # the web bearer token lives under the `web-config` group
    assert evi_cli("web-config", "token", "show").code == 0
    assert evi_cli("web-config", "token", "rotate").code == 0
    # after rotate, `show` prints the actual token value
    assert len(evi_cli("web-config", "token", "show").stdout.strip()) >= 24
    assert evi_cli("web-config", "token", "clear").code == 0


# --- MCP -------------------------------------------------------------------


def test_mcp(evi_cli):
    assert evi_cli("mcp", "path").code == 0
    assert evi_cli("mcp", "list-servers").code == 0


# --- usage stats -----------------------------------------------------------


def test_stats(evi_cli):
    assert "transcript" in evi_cli("stats").out.lower() or evi_cli("stats").code == 0
    _seed_transcript(evi_cli.home)
    data = evi_cli("stats", "--json").json()
    assert data["sessions"] == 1 and data["messages"] == 2


# --- backup ----------------------------------------------------------------


def test_backup_create(evi_cli):
    assert evi_cli("backup", "create").code == 0
    # a backup archive should now exist under the home
    assert list(evi_cli.home.rglob("evi-backup-*.tar.gz"))


# --- finetune --------------------------------------------------------------


def test_finetune_export(evi_cli):
    _seed_transcript(evi_cli.home)
    out_file = evi_cli.workdir / "ft.jsonl"
    assert evi_cli("finetune", "export", "--out", str(out_file)).code == 0
    assert out_file.exists()
    # one user->assistant pair was seeded, so at least one JSONL record
    assert out_file.read_text(encoding="utf-8").strip()


# --- routines (webhook → recipe) -------------------------------------------


def test_routine(evi_cli):
    assert evi_cli("recipe", "new", "rr").code == 0
    assert evi_cli("routine", "add", "trigger", "--recipe", "rr").code == 0
    assert "trigger" in evi_cli("routine", "list").out
    assert evi_cli("routine", "remove", "trigger").code == 0
