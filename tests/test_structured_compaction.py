from types import SimpleNamespace

from evi.config import Config
from evi.history import COMPACTION_ACK, previous_summary, validate_history
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


def test_previous_summary():
    note = {'role': 'user', 'content': '[compacted: 8 earlier messages summarised]\n\nold'}
    ack = {'role': 'assistant', 'content': COMPACTION_ACK}
    rest = [{'role': 'user', 'content': 'hello'}]

    assert previous_summary([note, ack, *rest]) == ('old', rest)
    assert previous_summary([note, *rest]) == ('old', rest)
    assert previous_summary(rest) == ('', rest)
    assert previous_summary([]) == ('', [])


def test_structured_compaction_flow():
    # 1. First compaction
    client = _Summarizer("## Objective\nDo X\n\n## Decisions\nNone\n\n## Done\nNone\n\n## In progress\nNone\n\n## Files\nNone\n\n## Blockers\nNone\n\n## Next step\nNone")
    agent = Agent(client=client, config=Config(), tools=[], transcripts=None)

    # Fill history to trigger compaction
    for i in range(30):
        agent.history.append({"role": "user", "content": f"user {i}"})
        agent.history.append({"role": "assistant", "content": f"assistant {i}"})

    # compact_history(keep_recent=4)
    collapsed = agent.compact_history(keep_recent=4)
    assert collapsed > 0

    # Check prompt
    prompt = client.requests[0]['messages'][1]['content']
    for section in ("Objective", "Decisions", "Done", "In progress", "Files", "Blockers", "Next step"):
        assert f"## {section}" in prompt
    assert "Earlier summary:" not in prompt
    assert len(validate_history(agent.history)) == 0

    # 2. Second compaction
    # Add 10 more pairs
    for i in range(10):
        agent.history.append({"role": "user", "content": f"user {i+30}"})
        agent.history.append({"role": "assistant", "content": f"assistant {i+30}"})

    # New client for second summary
    client2 = _Summarizer("## Objective\nDo Y\n\n## Decisions\nNone\n\n## Done\nNone\n\n## In progress\nNone\n\n## Files\nNone\n\n## Blockers\nNone\n\n## Next step\nNone")
    agent.client = client2

    collapsed2 = agent.compact_history(keep_recent=4)
    assert collapsed2 > 0

    prompt2 = client2.requests[0]['messages'][1]['content']
    assert "Earlier summary:" in prompt2
    assert "## Objective\nDo X" in prompt2
    assert "[compacted:" not in prompt2.split("Conversation:")[1]

    # History should hold exactly one [compacted: message
    compacted_count = sum(1 for m in agent.history if m.get("content", "").startswith("[compacted: "))
    assert compacted_count == 1
    assert len(validate_history(agent.history)) == 0


def test_compaction_focus():
    client = _Summarizer("Summary text")
    agent = Agent(client=client, config=Config(), tools=[], transcripts=None)
    for i in range(20):
        agent.history.append({"role": "user", "content": "u"})
        agent.history.append({"role": "assistant", "content": "a"})

    # With focus
    agent.compact_history(keep_recent=4, focus="the database migration")
    prompt_focus = client.requests[0]['messages'][1]['content']
    assert "Pay particular attention to: the database migration" in prompt_focus

    # Without focus
    client_no_focus = _Summarizer("Summary text")
    agent.client = client_no_focus
    agent.compact_history(keep_recent=4)
    prompt_no_focus = client_no_focus.requests[0]['messages'][1]['content']
    assert "Pay particular attention to:" not in prompt_no_focus
