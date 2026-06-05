"""Tool registry and built-in tools.

Import side effects register tools via the @tool decorator. The agent
constructs its tool list from REGISTRY filtered by ToolToggles.
"""

from evi.tools.base import REGISTRY, Tool, tool  # noqa: F401
