"""[llm] compaction_model names the model that writes compaction summaries;
empty keeps the chat model (never fast_model implicitly)."""

from types import SimpleNamespace

import evi.config as config_mod
from evi.config import Config
from evi.llm.agent import Agent


class _Summarizer:
    """A client for compaction's non-streaming summary request."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        text = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _compact(cfg: Config) -> dict:
    client = _Summarizer("summary")
    agent = Agent(client=client, config=cfg, tools=[], transcripts=None)
    for i in range(30):
        agent.history += [{"role": "user", "content": f"user {i}"},
                          {"role": "assistant", "content": f"assistant {i}"}]
    agent.compact_history(keep_recent=4)
    return client.requests[0]


def test_the_compaction_model_writes_the_summary():
    cfg = Config()
    cfg.llm.compaction_model = "small-summarizer"
    assert _compact(cfg)["model"] == "small-summarizer"


def test_without_one_the_chat_model_does_even_when_fast_model_is_set():
    cfg = Config()
    cfg.llm.model, cfg.llm.fast_model = "main-model", "fast-model"
    assert _compact(cfg)["model"] == "main-model"


def test_it_defaults_empty_and_loads_from_config_toml(tmp_path, monkeypatch):
    assert Config().llm.compaction_model == ""
    path = tmp_path / "config.toml"  # not the test process's shared home
    path.write_text('[llm]\ncompaction_model = "x"\n', encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    assert Config.load().llm.compaction_model == "x"
