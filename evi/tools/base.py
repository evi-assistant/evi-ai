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
import re
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin, get_type_hints

from evi.citations import ToolOutput


REGISTRY: dict[str, "Tool"] = {}

# Argument names that hold a filesystem path, for any tool. The permission
# policy checks only these against `[auto] trusted_dirs` and `protected_paths`,
# never a code, command or content body.
PATH_PARAMS = frozenset({"path", "paths", "file_path", "cwd", "dir"})
# MCP servers name their own arguments, so an MCP tool's argument also counts
# when its name (camelCase / snake_case / kebab-case) ends in one of these
# words, or is one of `_MCP_PATH_NAMES` ("source" / "destination" of a move).
_MCP_PATH_WORDS = frozenset({
    "path", "paths", "filepath", "filename", "filenames", "file", "files",
    "dir", "dirs", "directory", "directories", "folder", "cwd", "workdir",
})
_MCP_PATH_NAMES = frozenset({"source", "destination", "src", "dst", "dest", "root"})
# Built-in tools that only read the paths they are given. Protected paths guard
# writes, so these never trigger them; any other tool (MCP tools included) is
# treated as one that may write its path arguments.
READ_ONLY_TOOLS = frozenset({
    "read_file", "list_dir", "find_files", "search_files", "check_file",
    "python_symbols", "git_blame", "git_diff", "git_info", "git_log", "git_show",
    "git_status", "read_pdf", "ocr_image", "describe_image", "find_in_project",
    "project_index_stats", "sqlite_schema",
})
# Built-in tools whose glob argument picks files below their `path` argument.
# A glob with a ".." part (or an absolute one) reaches outside that folder,
# so `[auto] trusted_dirs` never approves such a call (evi/permissions.py).
GLOB_PARAMS: dict[str, tuple[str, ...]] = {
    "find_files": ("pattern",),
    "search_files": ("glob",),
}


def is_path_param(name: str, category: str = "") -> bool:
    """True if a tool argument called `name` holds a filesystem path."""
    if name in PATH_PARAMS:
        return True
    if category != "mcp" or not name:
        return False
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    parts = [w for w in re.split(r"[_\-.\s]+", words) if w]
    return bool(parts) and (parts[-1] in _MCP_PATH_WORDS or "".join(parts) in _MCP_PATH_NAMES)


def path_values(
    args: dict[str, Any], category: str = "", names: Any = None
) -> list[str]:
    """The string values (or string-list items) of a call's path arguments:
    the arguments in `names`, or by default those `is_path_param` names."""
    out: list[str] = []
    for key, value in args.items():
        if names is not None:
            if key not in names:
                continue
        elif not is_path_param(str(key), category):
            continue
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in value if isinstance(v, str))
    return out


def glob_values(tool_name: str, args: dict[str, Any]) -> list[str]:
    """The string values of a call's glob arguments (`GLOB_PARAMS`)."""
    values = (args.get(k) for k in GLOB_PARAMS.get(tool_name, ()))
    return [v for v in values if isinstance(v, str) and v]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON-schema object
    func: Callable[..., Any]
    category: str = "general"
    # Known-slow tools (web fetch, index build, model pull). The agent emits a
    # ToolProgress heartbeat immediately when one starts (rather than only after
    # the first interval) so the UI shows status without an apparent hang.
    long: bool = False
    # Names of the arguments that hold filesystem paths (see `path_args`).
    # None = the schema properties `is_path_param` recognises.
    path_params: tuple[str, ...] | None = None
    # True for a tool that never writes the paths it is given, so protected
    # paths (which guard writes) don't apply to it. None = a built-in named in
    # `READ_ONLY_TOOLS`; any other tool, MCP tools included, may write.
    read_only: bool | None = None

    def __post_init__(self) -> None:
        if self.read_only is None:
            self.read_only = self.name in READ_ONLY_TOOLS

    def path_param_names(self) -> tuple[str, ...]:
        if self.path_params is not None:
            return tuple(self.path_params)
        props = (self.parameters or {}).get("properties") or {}
        if not isinstance(props, dict):
            return ()
        return tuple(k for k in props if is_path_param(str(k), self.category))

    def path_args(self, arguments: str | dict[str, Any]) -> list[str]:
        """The path arguments of one call, as written (relative paths are
        relative to the session working folder). A path argument the call
        leaves out counts with its schema default (``list_dir()`` reads "."),
        and without explicit `path_params` an argument the schema doesn't
        declare counts too when its name is a path name."""
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return []
        if not isinstance(args, dict):
            return []
        names = set(self.path_param_names())
        if self.path_params is None:
            names.update(k for k in args if is_path_param(str(k), self.category))
        props = (self.parameters or {}).get("properties") or {}
        for n in sorted(names):
            schema = props.get(n) if isinstance(props, dict) else None
            default = schema.get("default") if isinstance(schema, dict) else None
            if n not in args and isinstance(default, (str, list)):
                args[n] = default
        return path_values(args, self.category, names)

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
    long: bool = False,
    read_only: bool | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register a function as an LLM tool.

    Reads parameter types from type hints. The function's first docstring line
    is used as the description if `description` is not supplied. Pass
    ``long=True`` for known-slow tools so the agent announces progress as soon
    as they start (see Agent's ToolProgress), and ``read_only=True`` for one
    that never writes its path arguments (`Tool.read_only`).
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
            long=long,
            read_only=read_only,
        )
        REGISTRY[tool_name] = t
        return fn

    return decorator


def get_enabled_tools(toggles: dict[str, bool]) -> list[Tool]:
    """Return tools whose category is enabled in the toggle map.

    A toggle with name == category enables every tool in that category.
    """
    return [t for t in REGISTRY.values() if toggles.get(t.category, False)]
