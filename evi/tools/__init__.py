"""Tool registry and built-in tools.

Import side effects register tools via the @tool decorator. The agent
constructs its tool list from REGISTRY filtered by ToolToggles. Importing this
package alone does NOT pull in the individual tool modules — call
:func:`register_builtin_tools` (the SDK, the MCP server, and anything that reads
REGISTRY without the CLI's explicit imports use it) to populate the registry
deterministically.
"""

from evi.tools.base import REGISTRY, Tool, get_enabled_tools, tool  # noqa: F401

# Every built-in tool module. Optional-dependency failures (e.g. no PDF / STT
# extra installed) just omit that module's tools rather than erroring.
_BUILTIN_TOOL_MODULES = (
    "fs", "code", "memory", "skills", "subagent", "websearch", "git",
    "index", "calendar", "pdf", "sqlite", "ocr", "rerank", "monitor",
    "image_comfy", "voice", "computer", "federation", "ask",
)


def register_builtin_tools() -> dict[str, Tool]:
    """Import every built-in tool module so REGISTRY is fully populated, then
    return REGISTRY. Idempotent (imports are cached) and tolerant of missing
    optional deps — the one canonical way to fill the registry for code that
    reads it without the CLI's explicit per-module imports."""
    import importlib

    for mod in _BUILTIN_TOOL_MODULES:
        try:
            importlib.import_module(f"evi.tools.{mod}")
        except Exception:  # noqa: BLE001 — a missing optional dep just omits its tools
            pass
    return REGISTRY


__all__ = ["REGISTRY", "Tool", "tool", "get_enabled_tools", "register_builtin_tools"]
