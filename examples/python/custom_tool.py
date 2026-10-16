"""eVi Agent SDK — define a custom tool and give it to an agent.

The ``@tool`` decorator reads the signature's type hints to build the JSON
schema and the first docstring line as the description. Pass the decorated
function straight to ``build_agent(tools=[...])`` — the SDK resolves it to the
registered Tool. Run with:  python examples/python/custom_tool.py
"""

from evi.sdk import build_agent, run_headless, tool


@tool(category="math", description="Add two integers and return the sum")
def add(a: int, b: int) -> int:
    return a + b


@tool(category="math", description="Multiply two integers")
def multiply(a: int, b: int) -> int:
    return a * b


def main() -> None:
    # tools=[...] replaces the default toolset entirely — the agent sees only
    # these two. (Use tool_categories=[...] to pick from the built-ins instead.)
    #
    # Nobody is asked about tool calls here, and "math" is not an auto-approved
    # category, so without a permission_callback every call would be denied
    # (an agent with nobody to ask fails closed). These two tools only do
    # arithmetic, so let this agent's calls run.
    agent = build_agent(tools=[add, multiply], unattended_policy="allow")
    print(run_headless(agent, "What is (12 + 30) multiplied by 3?").text)


if __name__ == "__main__":
    main()
