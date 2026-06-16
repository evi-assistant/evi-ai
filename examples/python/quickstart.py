"""eVi Agent SDK — quickstart.

Build a batteries-included agent and run one prompt to completion. Requires a
local model backend reachable per ``~/.evi/config.toml`` (e.g. Ollama / llama.cpp
/ vLLM). Run with:  python examples/python/quickstart.py
"""

from evi.sdk import build_agent, run_headless


def main() -> None:
    # build_agent() loads ~/.evi/config.toml and wires the tools enabled there
    # plus memory, skills, project context, hooks and guardrails.
    agent = build_agent()

    result = run_headless(agent, "In one sentence, what is eVi?")
    print(result.text)
    if result.usage:
        print(f"\n[tokens] {result.usage}")


if __name__ == "__main__":
    main()
