"""eVi Agent SDK — headless run for CI / scripting.

Emits a machine-readable JSON result and sets a non-zero exit code on failure,
so it drops cleanly into a pipeline step. Usage:

    python examples/python/headless_ci.py "Does this repo have a README? Answer yes/no."
"""

import sys

from evi.sdk import build_agent, run_headless, to_json


def main(argv: list[str]) -> int:
    prompt = " ".join(argv[1:]) or "Summarise this project in one line."

    # tool_categories scopes the agent to read-only filesystem + search tools —
    # appropriate for an unattended CI run.
    agent = build_agent(tool_categories=["fs", "code"])
    result = run_headless(agent, prompt, max_turns=8)

    print(to_json(result))  # {"text": ..., "tools": [...], "usage": {...}, "error": ...}
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
