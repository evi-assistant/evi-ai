"""Tool framework: @tool decorator builds an OpenAI tool schema from type hints.

Usage:

    @tool(description="Read a UTF-8 text file from disk.")
    def read_file(path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

The decorator inspects the function signature and docstring, builds a JSON
schema for the parameters, and registers a `Tool` in the module-level REGISTRY.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin, get_type_hints

from evi.citations import ToolOutput


REGISTRY: dict[str, "Tool"] = {}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON-schema object
    func: Callable[..., Any]
    category: str = "general"

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def call_rich(self, arguments: str | dict[str, Any]) -> ToolOutput:
        """Invoke and return both visible text + structured citations.

        Tools may return either:
        - `str` — wrapped into `ToolOutput(text=str, citations=[])`
        - `ToolOutput` — passed through
        - `dict` / `list` — JSON-encoded into the text, no citations

        Exceptions are caught here so a misbehaving tool can't crash the
        agent loop. They surface as `ToolOutput(text="ERROR: ...")`.
        """
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
        try:
            result = self.func(**args)
        except Exception as e:
            return ToolOutput(text=f"ERROR: {type(e).__name__}: {e}")
        if isinstance(result, ToolOutput):
            return result
        if isinstance(result, (dict, list)):
            return ToolOutput(text=json.dumps(result, default=str))
        return ToolOutput(text=str(result))

    def call(self, arguments: str | dict[str, Any]) -> str:
        """Back-compat: legacy str-returning call.

        New code (and the Agent itself) should use `call_rich()` to
        preserve citations. This wrapper exists so tests and callers
        that just want the visible text keep working.
        """
        return self.call_rich(arguments).text


_PRIMITIVE_SCHEMA = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}


def _type_to_schema(tp: Any) -> dict[str, Any]:
    if tp in _PRIMITIVE_SCHEMA:
        return dict(_PRIMITIVE_SCHEMA[tp])
    origin = get_origin(tp)
    if origin is list:
        (item_tp,) = get_args(tp) or (str,)
        return {"type": "array", "items": _type_to_schema(item_tp)}
    if origin is dict:
        return {"type": "object"}
    # Optional[X] / X | None
    if origin is type(None) or tp is type(None):
        return {"type": "null"}
    args = get_args(tp)
    if args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _type_to_schema(non_none[0])
    return {"type": "string"}  # fallback


def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    category: str = "general",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register a function as an LLM tool.

    Reads parameter types from type hints. The function's first docstring line
    is used as the description if `description` is not supplied.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)
        hints = get_type_hints(fn)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for pname, param in sig.parameters.items():
            tp = hints.get(pname, str)
            schema = _type_to_schema(tp)
            properties[pname] = schema
            if param.default is inspect.Parameter.empty:
                required.append(pname)
            else:
                schema["default"] = param.default

        doc = (description or (fn.__doc__ or "").strip().split("\n")[0]).strip()
        tool_name = name or fn.__name__
        t = Tool(
            name=tool_name,
            description=doc or tool_name,
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
            },
            func=fn,
            category=category,
        )
        REGISTRY[tool_name] = t
        return fn

    return decorator


def get_enabled_tools(toggles: dict[str, bool]) -> list[Tool]:
    """Return tools whose category is enabled in the toggle map.

    A toggle with name == category enables every tool in that category.
    """
    return [t for t in REGISTRY.values() if toggles.get(t.category, False)]
