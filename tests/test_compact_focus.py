from types import SimpleNamespace
from evi.config import Config
from evi.llm.agent import Agent

class _Summarizer:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        if not self.replies:
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.replies.pop(0)))])

def test_compact_history_focus_prompt():
    # AC 1: focus argument is passed to the LLM prompt
    client = _Summarizer("Summary text")
    agent = Agent(client=client, config=Config(), tools=[], transcripts=None)
    for i in range(20):
        agent.history.append({"role": "user", "content": "u"})
        agent.history.append({"role": "assistant", "content": "a"})

    focus_text = "the database migration"
    agent.compact_history(keep_recent=4, focus=focus_text)

    prompt = client.requests[0]['messages'][1]['content']
    assert f"Pay particular attention to: {focus_text}" in prompt

def test_compact_history_no_focus_prompt():
    # AC 2: focus is optional and doesn't add the phrase when absent
    client = _Summarizer("Summary text")
    agent = Agent(client=client, config=Config(), tools=[], transcripts=None)
    for i in range(20):
        agent.history.append({"role": "user", "content": "u"})
        agent.history.append({"role": "assistant", "content": "a"})

    agent.compact_history(keep_recent=4)

    prompt = client.requests[0]['messages'][1]['content']
    assert "Pay particular attention to:" not in prompt

def test_compact_history_empty_focus_prompt():
    # AC 3: empty string focus is treated as no focus
    client = _Summarizer("Summary text")
    agent = Agent(client=client, config=Config(), tools=[], transcripts=None)
    for i in range(20):
        agent.history.append({"role": "user", "content": "u"})
        agent.history.append({"role": "assistant", "content": "a"})

    agent.compact_history(keep_recent=4, focus="  ")

    prompt = client.requests[0]['messages'][1]['content']
    assert "Pay particular attention to:" not in prompt
