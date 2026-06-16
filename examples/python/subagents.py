"""eVi Agent SDK — fan work out to parallel scoped subagents.

``run_subagents_parallel`` runs each task in its own one-shot Agent (sharing the
parent's LLM client/config) and returns ``[(task, result), ...]`` in input
order. Each subagent gets only the tool *categories* you grant it.
Run with:  python examples/python/subagents.py
"""

from evi.sdk import run_subagents_parallel


def main() -> None:
    tasks = [
        "Summarise what a JSON Web Token is in 2 sentences.",
        "Summarise what a UUID is in 2 sentences.",
        "Summarise what a bloom filter is in 2 sentences.",
    ]

    results = run_subagents_parallel(
        tasks,
        system_prompt="You are a terse technical explainer.",
        tool_categories=(),  # no tools: pure explanation
        max_workers=3,
    )

    for task, answer in results:
        print(f"## {task}\n{answer}\n")


if __name__ == "__main__":
    main()
