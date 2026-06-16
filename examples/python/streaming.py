"""eVi Agent SDK — stream a turn and react to events yourself.

``Agent.chat`` yields typed events: text/thinking deltas, tool calls and
results, usage stats, the per-turn route decision, and a terminal Done/Error.
Run with:  python examples/python/streaming.py
"""

from evi.sdk import (
    Done,
    Error,
    RouteInfo,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    build_agent,
)


def main() -> None:
    agent = build_agent()

    for ev in agent.chat("List the Python files in the current directory."):
        if isinstance(ev, RouteInfo):
            print(f"[route: {ev.route} -> {ev.model}]")
        elif isinstance(ev, ThinkingDelta):
            print(ev.text, end="", flush=True)  # reasoning, if the model emits it
        elif isinstance(ev, TextDelta):
            print(ev.text, end="", flush=True)
        elif isinstance(ev, ToolCall):
            print(f"\n[tool call] {ev.name}({ev.arguments})")
        elif isinstance(ev, ToolResult):
            print(f"[tool result] {ev.name}: {ev.output[:120]}")
        elif isinstance(ev, Error):
            print(f"\n[error] {ev.message}")
            break
        elif isinstance(ev, Done):
            print("\n[done]")
            break


if __name__ == "__main__":
    main()
