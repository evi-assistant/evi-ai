"""Eval cases that seed a conversation and compact it first (SK12-3).

A recall case (memory-sessions-structured-compaction) gives the agent a
`history` before its prompt and, with `compact = true`, compacts it first, so
the case checks that a fact from the start of the conversation survives the
summary. The fake backend writes no summary; the "real model" runs here use a
client that answers the summary request itself and scripts the rest.

eVi's home and config live under tmp_path, never ~/.evi.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evi import evals
from evi.evals_fake import ScriptedClient

HW = {"gpu": "none", "vram_gb": None, "cpu": "test", "ram_gb": None, "os": "test"}

RECALL = r'''
[[case]]
name = "recall"
prompt = "What was the codename?"
history = [
  { role = "user", content = "The project codename is PELICAN. Remember it." },
  { role = "assistant", content = "Noted: PELICAN." },
  { role = "user", content = "Now list three colours." },
  { role = "assistant", content = "Red, green, blue." },
  { role = "user", content = "And three animals." },
  { role = "assistant", content = "Cat, dog, owl." },
]
compact = true
regex = "PELICAN"
fake = [{ text = "The codename is PELICAN." }]
'''


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config and trust list under tmp_path."""
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    (evi_home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    return evi_home


def _suite(tmp_path: Path, body: str) -> evals.EvalSuite:
    path = tmp_path / "suite.toml"
    path.write_text('name = "t"\n' + body, encoding="utf-8")
    return evals.load_suite_file(path)


def _run(tmp_path: Path, body: str, ctx: evals.EvalContext | None = None) -> dict:
    """Run the one-case suite `body` (on the fake backend by default); its row."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    ctx = ctx or evals.EvalContext(make_agent=evals.fake_agent_factory(), backend="fake",
                                   model="fake", tmp_root=work)
    report = evals.run_suite(_suite(tmp_path, body), ctx, hardware=HW)
    # Every workspace is gone afterwards.
    assert list(work.iterdir()) == []
    [row] = report["cases"]
    return row


class _Summarising:
    """A model that writes `summary` for the compaction request (stream=False)
    and plays the case's script for the turn; it keeps the last turn's messages."""

    def __init__(self, script, summary: str):
        self.scripted = ScriptedClient(script)
        self.summary = summary
        self.sent: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        if not kw.get("stream"):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.summary))])
        self.sent = [dict(m) for m in kw["messages"]]
        return self.scripted.chat.completions.create(**kw)


def _real_ctx(tmp_path: Path, summary: str) -> tuple[evals.EvalContext, list]:
    """A context reported as a real backend, whose agent gets a _Summarising client."""
    base = evals.fake_agent_factory()
    clients: list[_Summarising] = []

    def make(case, workspace):
        agent = base(case, workspace)
        agent.client = _Summarising(case.fake or [], summary)
        clients.append(agent.client)
        return agent

    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return evals.EvalContext(make_agent=make, backend="rtx", model="m", tmp_root=work), clients


# ---- loading ---------------------------------------------------------------------


def test_history_and_compact_load(tmp_path):
    [case] = _suite(tmp_path, r'''
[[case]]
name = "c"
prompt = "p"
history = [{ role = "user", content = "hi" }, { role = "assistant", content = "hello" }]
compact = true
''').cases
    assert case.history == [{"role": "user", "content": "hi"},
                            {"role": "assistant", "content": "hello"}]
    assert case.compact is True


def test_a_case_without_them_has_no_history_and_no_compact(tmp_path):
    [case] = _suite(tmp_path, '[[case]]\nname = "c"\nprompt = "p"\n').cases
    assert case.history == [] and case.compact is False


@pytest.mark.parametrize("bad", [
    "history = 3",
    'history = [{ role = "system", content = "x" }]',
    'history = [{ role = "user" }]',
    'history = [{ role = "user", content = "x", name = "y" }]',
])
def test_a_malformed_history_fails_to_load(tmp_path, bad):
    with pytest.raises(evals.EvalError, match="`history`"):
        _suite(tmp_path, f'[[case]]\nname = "c"\nprompt = "p"\n{bad}\n')


def test_the_role_message_closes_with_one_brace(tmp_path):
    with pytest.raises(evals.EvalError) as exc:
        _suite(tmp_path, '[[case]]\nname = "c"\nprompt = "p"\n'
                         'history = [{ role = "tool", content = "x" }]\n')
    assert str(exc.value).endswith('content = "..."}')


def test_compact_without_a_history_fails_to_load(tmp_path):
    with pytest.raises(evals.EvalError, match="`compact`"):
        _suite(tmp_path, '[[case]]\nname = "c"\nprompt = "p"\ncompact = true\n')


# ---- running ---------------------------------------------------------------------


def test_a_recall_case_passes_on_the_fake_backend(home, tmp_path):
    row = _run(tmp_path, RECALL)
    assert row["passes"] == 1, row["failures"]


def test_a_recall_case_compacts_on_a_real_backend(home, tmp_path):
    ctx, clients = _real_ctx(tmp_path, "## Objective\nThe project codename is PELICAN.")
    row = _run(tmp_path, RECALL, ctx)
    assert row["passes"] == 1, row["failures"]
    texts = [str(m.get("content")) for m in clients[0].sent]
    # The summary replaced the start of the conversation...
    assert any("[compacted:" in t for t in texts)
    assert not any("Remember it." in t for t in texts)
    # ...and the prompt still came last.
    assert texts[-1] == "What was the codename?"


def test_an_empty_summary_fails_the_run(home, tmp_path):
    ctx, _ = _real_ctx(tmp_path, "")
    row = _run(tmp_path, RECALL, ctx)
    assert row["passes"] == 0
    assert "compacting the case's history summarised nothing" in "\n".join(row["failures"])


def test_a_history_without_compact_is_sent_as_it_is(home, tmp_path):
    # Without compact the summary request never happens, whatever the client would write.
    ctx, clients = _real_ctx(tmp_path, "unused")
    row = _run(tmp_path, r'''
[[case]]
name = "seeded"
prompt = "And now?"
history = [
  { role = "user", content = "first" },
  { role = "assistant", content = "second" },
  { role = "user", content = "third" },
  { role = "assistant", content = "fourth" },
]
contains = "done"
fake = [{ text = "done" }]
''', ctx)
    assert row["passes"] == 1, row["failures"]
    sent = clients[0].sent
    assert sent[0]["role"] == "system"
    assert [(m["role"], m["content"]) for m in sent[1:]] == [
        ("user", "first"), ("assistant", "second"), ("user", "third"),
        ("assistant", "fourth"), ("user", "And now?")]
