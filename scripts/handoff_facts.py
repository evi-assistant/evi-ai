#!/usr/bin/env python3
"""Generate the facts in HANDOFF.md from the code, and check the docs cover
the CLI and web surface added since a release.

Hand-typed numbers in HANDOFF.md rotted: by 1.0.52 it still quoted the 1.0.29
test count, "~74 /api/* routes" and "22 modules". The facts block between the
`BEGIN GENERATED FACTS` / `END GENERATED FACTS` markers is now written by this
script, from the source tree alone: no import of eVi, no installed extras, no
network, so the output is the same on every machine.

    python scripts/handoff_facts.py show            # print the block
    python scripts/handoff_facts.py show --json     # the facts as JSON, with test-function counts
    python scripts/handoff_facts.py write           # rewrite the block in HANDOFF.md
    python scripts/handoff_facts.py check           # exit 1 if HANDOFF.md is stale
    python scripts/handoff_facts.py docs-coverage   # exit 1 if new surface is undocumented

The block changes whenever a module, route, CLI command, test file, extra or
workflow is added, so `write` and `check` belong with cutting a release, when
the version changes anyway; a feature commit need not regenerate it.

`docs-coverage` compares the CLI commands and `/api` routes in the working
tree with those at a git ref (`--since`, default v1.0.36: everything 1.0.37
and later added) and fails unless each new one is mentioned somewhere under
docs/: a command as `evi <group> <name>`, a route by its path (`{param}`,
`<param>` and `:param` placeholders all match). Hidden Typer commands are
internal and skipped. It needs the tag, so not a shallow clone.
scripts/check_docs_freshness.py guards each new diff; this check closes the
backlog from before that gate existed.

Exit codes: 0 ok, 1 stale or undocumented, 2 usage or git error.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "evi"
CLI_MODULE = "evi/apps/cli/main.py"
CLI_ROOT_VAR = "app"
TOOLS_INIT = "evi/tools/__init__.py"
DEFAULT_SINCE = "v1.0.36"

BEGIN = "<!-- BEGIN GENERATED FACTS (scripts/handoff_facts.py write; do not edit by hand) -->"
END = "<!-- END GENERATED FACTS -->"

_HTTP_METHODS = {
    "get", "post", "put", "patch", "delete", "head", "options", "trace", "websocket",
    "api_route", "route",
}
_TEST_LAYERS = (
    ("unit", "tests"),
    ("UI e2e", "tests/e2e"),
    ("CLI e2e", "tests/cli_e2e"),
    ("desktop e2e", "tests/desktop_e2e"),
)


# ---- reading source --------------------------------------------------------

def tree_sources(root: Path) -> dict[str, str]:
    """Every .py file under evi/ in the working tree, keyed by posix path."""
    out: dict[str, str] = {}
    for p in sorted((root / PACKAGE).rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        out[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
    return out


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(err or f"git {args[0]} exited {proc.returncode}")
    return proc.stdout


def git_sources(root: Path, ref: str) -> dict[str, str]:
    """Every .py file under evi/ at `ref`, read with one `git cat-file --batch`."""
    listing = _git(root, "ls-tree", "-r", "-z", "--name-only", ref, "--", PACKAGE)
    paths = [p for p in listing.decode("utf-8", "replace").split("\0") if p.endswith(".py")]
    if not paths:
        return {}
    blob = _git(root, "cat-file", "--batch",
                stdin="".join(f"{ref}:{p}\n" for p in paths).encode("utf-8"))
    out: dict[str, str] = {}
    pos = 0
    for path in paths:
        nl = blob.index(b"\n", pos)
        header = blob[pos:nl].split()
        if header[-1:] == [b"missing"] or len(header) < 3:
            pos = nl + 1
            continue
        size = int(header[2])
        out[path] = blob[nl + 1 : nl + 1 + size].decode("utf-8", "replace")
        pos = nl + 1 + size + 1  # the object is followed by a newline
    return out


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _str_arg(call: ast.Call, index: int | None = 0, kw: str | None = None) -> str | None:
    """The string literal at positional `index` (None: none) or keyword `kw`."""
    if index is not None and len(call.args) > index:
        a = call.args[index]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
    if kw:
        for k in call.keywords:
            if k.arg == kw and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str):
                return k.value.value
    return None


def _true_kw(call: ast.Call, kw: str) -> bool:
    return any(k.arg == kw and isinstance(k.value, ast.Constant) and k.value.value is True
               for k in call.keywords)


# ---- surface: web routes and CLI commands -----------------------------------

@dataclass(frozen=True, order=True)
class Route:
    path: str
    method: str
    file: str
    line: int


@dataclass(frozen=True, order=True)
class Command:
    path: tuple[str, ...]  # ("channel", "approve"); a group is a command too
    group: bool
    hidden: bool

    @property
    def text(self) -> str:
        return " ".join((PACKAGE, *self.path))


def _route_call(node: ast.expr, attrs: set[str] | tuple[str, ...]) -> ast.Call | None:
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in attrs):
        return node
    return None


def web_routes(sources: dict[str, str]) -> list[Route]:
    """FastAPI routes: `@x.get("/path")`-style decorators on a function (or the
    call form `x.get("/path")(func)`) and `x.add_api_route("/path", ...)`.

    Only those shapes count: a plain `client.get("/v1/models")` is an HTTP
    request to some other server, not a route eVi serves."""
    found: set[Route] = set()
    for file, text in sources.items():
        tree = _parse(text)
        if tree is None:
            continue
        calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls += [c for d in node.decorator_list if (c := _route_call(d, _HTTP_METHODS))]
            elif isinstance(node, ast.Call):
                if c := _route_call(node.func, _HTTP_METHODS):  # x.get("/path")(func)
                    calls.append(c)
                elif c := _route_call(node, ("add_api_route", "add_api_websocket_route")):
                    calls.append(c)
        for call in calls:
            attr = call.func.attr  # type: ignore[union-attr]
            if attr in ("add_api_route", "add_api_websocket_route"):
                method = "WEBSOCKET" if "websocket" in attr else "ANY"
            else:
                method = "ANY" if attr in ("api_route", "route") else attr.upper()
            path = _str_arg(call, 0, "path")
            if path and path.startswith("/"):
                found.add(Route(path, method, file, call.lineno))
    return sorted(found)


def _is_typer(func: ast.expr) -> bool:
    return (isinstance(func, ast.Attribute) and func.attr == "Typer") or (
        isinstance(func, ast.Name) and func.id == "Typer")


def cli_commands(sources: dict[str, str]) -> list[Command]:
    """The `evi` command tree, walked from the root Typer in CLI_MODULE."""
    tree = _parse(sources.get(CLI_MODULE, ""))
    if tree is None:
        return []
    own_name: dict[str, str | None] = {}            # Typer var -> Typer(name=...)
    groups: dict[str, list[tuple[str | None, str]]] = {}   # parent var -> [(name, child var)]
    commands: dict[str, list[tuple[str, bool]]] = {}       # Typer var -> [(name, hidden)]

    def add_command(dec: ast.Call, fallback: str) -> None:
        owner = dec.func.value  # type: ignore[union-attr]
        if isinstance(owner, ast.Name):
            name = _str_arg(dec, 0, "name") or fallback.lower().replace("_", "-")
            commands.setdefault(owner.id, []).append((name, _true_kw(dec, "hidden")))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call) \
                and _is_typer(node.value.func):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    own_name[t.id] = _str_arg(node.value, None, "name")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr == "add_typer" and isinstance(node.func.value, ast.Name)
                    and node.args and isinstance(node.args[0], ast.Name)):
                groups.setdefault(node.func.value.id, []).append(
                    (_str_arg(node, None, "name"), node.args[0].id))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "command"):
                    add_command(dec, node.name)
        # The call form: `app.command("name")(func)`.
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Call)
                and isinstance(node.func.func, ast.Attribute) and node.func.func.attr == "command"
                and node.args and isinstance(node.args[0], ast.Name)):
            add_command(node.func, node.args[0].id)

    out: list[Command] = []

    def walk(var: str, prefix: tuple[str, ...], seen: frozenset[str]) -> None:
        for name, hidden in commands.get(var, []):
            out.append(Command((*prefix, name), False, hidden))
        for name, child in groups.get(var, []):
            name = name or own_name.get(child)
            if not name or child in seen:
                continue
            out.append(Command((*prefix, name), True, False))
            walk(child, (*prefix, name), seen | {child})

    walk(CLI_ROOT_VAR, (), frozenset({CLI_ROOT_VAR}))
    return sorted(set(out))


# ---- the rest of the facts --------------------------------------------------

def project_meta(root: Path) -> dict:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project", {})
    return {
        "name": project.get("name", ""),
        "version": project.get("version", ""),
        "requires_python": project.get("requires-python", ""),
        "scripts": sorted(project.get("scripts", {})),
        "extras": list(project.get("optional-dependencies", {})),  # file order
    }


def module_map(root: Path) -> dict[str, list[str]]:
    """Package dir (posix, trailing slash) -> its module names, sorted."""
    out: dict[str, list[str]] = {}
    for init in sorted((root / PACKAGE).rglob("__init__.py")):
        pkg = init.parent
        if "__pycache__" in pkg.parts:
            continue
        mods = sorted(p.stem for p in pkg.glob("*.py")
                      if p.name not in ("__init__.py", "__main__.py"))
        out[pkg.relative_to(root).as_posix() + "/"] = mods
    return out


def builtin_tools(sources: dict[str, str]) -> dict:
    """`_BUILTIN_TOOL_MODULES` and the number of `@tool(...)` functions."""
    modules: list[str] = []
    tree = _parse(sources.get(TOOLS_INIT, ""))
    for node in ast.walk(tree) if tree else ():
        if (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_BUILTIN_TOOL_MODULES" for t in node.targets)
                and isinstance(node.value, (ast.Tuple, ast.List))):
            modules = [e.value for e in node.value.elts
                       if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    count = 0
    for file, text in sources.items():
        if not file.startswith(f"{PACKAGE}/tools/"):
            continue
        t = _parse(text)
        for node in ast.walk(t) if t else ():
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    f = dec.func if isinstance(dec, ast.Call) else dec
                    if (isinstance(f, ast.Name) and f.id == "tool") or (
                            isinstance(f, ast.Attribute) and f.attr == "tool"):
                        count += 1
    return {"modules": modules, "tools": count}


def _tests_in(text: str) -> int:
    tree = _parse(text)
    if tree is None:
        return 0
    n = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            n += 1
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            n += sum(1 for b in node.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and b.name.startswith("test"))
    return n


def count_tests(root: Path) -> list[dict]:
    """Per layer: test files and test functions (before parametrize expands
    them; `pytest --collect-only -q` gives the collected total). Only `show
    --json` reports the functions: in HANDOFF.md they would make the block
    stale on nearly every commit."""
    out = []
    for label, rel in _TEST_LAYERS:
        d = root / rel
        files = sorted(d.glob("test_*.py")) if d.is_dir() else []
        out.append({
            "layer": label,
            "dir": rel + "/",
            "files": len(files),
            "functions": sum(_tests_in(f.read_text(encoding="utf-8", errors="replace"))
                             for f in files),
        })
    return out


_TOP_KEY = re.compile(r"^(?P<q>['\"]?)(?P<key>[A-Za-z_][\w-]*)(?P=q)\s*:(?P<rest>.*)$")
_SUB_KEY = re.compile(r"^  (?P<q>['\"]?)(?P<key>[A-Za-z_][\w-]*)(?P=q)\s*:")


def workflows(root: Path) -> list[dict]:
    """Each workflow's name, triggers (the keys of `on:`) and job ids.

    A line-based reader rather than a YAML parser (no dependency): it only needs
    top-level keys and the keys directly under `on:` and `jobs:`."""
    out = []
    for path in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        name, triggers, jobs, section = path.stem, [], [], ""
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
            if not line.strip():
                continue
            top = _TOP_KEY.match(line)
            if top:
                section, rest = top.group("key"), top.group("rest").strip()
                if section == "name" and rest:
                    name = rest.strip("'\"")
                elif section in ("on", "true") and rest:  # `on: push` / `on: [a, b]`
                    triggers += [t.strip(" '\"") for t in rest.strip("[]").split(",") if t.strip()]
                continue
            sub = _SUB_KEY.match(line)
            if sub and section in ("on", "true"):
                triggers.append(sub.group("key"))
            elif sub and section == "jobs":
                jobs.append(sub.group("key"))
        out.append({"file": path.name, "name": name, "triggers": triggers, "jobs": jobs})
    return out


def collect(root: Path = ROOT) -> dict:
    sources = tree_sources(root)
    routes = web_routes(sources)
    cmds = [c for c in cli_commands(sources) if not c.hidden]
    mods = module_map(root)
    return {
        "project": project_meta(root),
        "modules": mods,
        "tools": builtin_tools(sources),
        "routes": [asdict(r) for r in routes],
        "cli": [{"command": c.text, "group": c.group} for c in cmds],
        "tests": count_tests(root),
        "workflows": workflows(root),
    }


# ---- rendering ---------------------------------------------------------------

def _codes(items: list[str]) -> str:
    return ", ".join(f"`{i}`" for i in items) if items else "(none)"


def _route_prefix(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if parts[:1] == ["api"] and len(parts) > 1 and not parts[1].startswith("{"):
        return f"/api/{parts[1]}"
    return "/" + (parts[0] if parts else "")


def render(facts: dict) -> str:
    p = facts["project"]
    mods = facts["modules"]
    top = mods.get(f"{PACKAGE}/", [])
    subs = [k for k in mods if k != f"{PACKAGE}/"]
    routes = facts["routes"]
    api = [r for r in routes if r["path"].startswith("/api/")]
    cli = facts["cli"]
    top_cmds = [c for c in cli if c["command"].count(" ") == 1]
    groups = [c for c in cli if c["group"]]
    tools = facts["tools"]
    tests = facts["tests"]
    wfs = facts["workflows"]

    lines = [
        BEGIN,
        "",
        "_Generated from the working tree by `python scripts/handoff_facts.py write`;"
        " `python scripts/handoff_facts.py check` fails when this block is stale._",
        "",
        f"**Package:** `{p['name']}` {p['version']} · Python `{p['requires_python']}`"
        f" · console scripts {_codes(p['scripts'])}",
        "",
        "| Fact | Count |",
        "|---|---|",
        f"| Top-level modules in `{PACKAGE}/` | {len(top)} |",
        f"| Subpackages | {len(subs)} |",
        f"| Built-in tool modules (`_BUILTIN_TOOL_MODULES`) | {len(tools['modules'])} |",
        f"| Built-in tools (`@tool` functions under `{PACKAGE}/tools/`) | {tools['tools']} |",
        f"| Web routes (method + path) | {len(routes)}, {len(api)} under `/api/` |",
        f"| CLI: top-level `evi` commands and groups | {len(top_cmds)}"
        f" ({sum(1 for c in top_cmds if c['group'])} groups) |",
        f"| CLI: every command and group, nested | {len(cli)} ({len(groups)} groups) |",
    ]
    for t in tests:
        lines.append(f"| Test files, {t['layer']} (`{t['dir']}test_*.py`) | {t['files']} |")
    lines += [
        f"| Optional-dependency extras | {len(p['extras'])} |",
        f"| GitHub workflows | {len(wfs)} |",
        "",
        "Test files, not tests: the number of tests changes with nearly every commit."
        " `python -m pytest --collect-only -q` counts them.",
        "",
        f"**Extras ({len(p['extras'])}):** {_codes(p['extras'])}",
        "",
        f"**Built-in tool modules ({len(tools['modules'])}):** {_codes(tools['modules'])}",
        "",
        "**Workflows** (`.github/workflows/`)",
        "",
        "| File | Name | Triggers | Jobs |",
        "|---|---|---|---|",
    ]
    for w in wfs:
        lines.append(f"| `{w['file']}` | {w['name']} | {_codes(w['triggers'])} |"
                     f" {_codes(w['jobs'])} |")
    lines += ["", "**Module map**", "", "| Package | Modules |", "|---|---|"]
    for pkg, names in mods.items():
        if names:  # a namespace-only package (evi/apps/) has nothing to list
            lines.append(f"| `{pkg}` ({len(names)}) | {', '.join(names)} |")
    lines += ["", "**Web routes**", "", "| Prefix | Routes |", "|---|---|"]
    by_prefix: dict[str, list[str]] = {}
    for r in routes:
        by_prefix.setdefault(_route_prefix(r["path"]), []).append(f"{r['method']} {r['path']}")
    for prefix in sorted(by_prefix):
        lines.append(f"| `{prefix}` ({len(by_prefix[prefix])}) | "
                     + ", ".join(f"`{x}`" for x in by_prefix[prefix]) + " |")
    lines += ["", "**CLI**", "", "| Command | Subcommands |", "|---|---|"]
    plain = [c["command"] for c in top_cmds if not c["group"]]
    lines.append(f"| `{PACKAGE}` (commands) | {', '.join(c.split(' ', 1)[1] for c in plain)} |")
    for g in groups:
        prefix = g["command"] + " "
        subs_of = [c["command"][len(prefix):] for c in cli
                   if c["command"].startswith(prefix) and " " not in c["command"][len(prefix):]]
        lines.append(f"| `{g['command']}` | {', '.join(subs_of) or '(none)'} |")
    lines += ["", END]
    return "\n".join(lines) + "\n"


# ---- HANDOFF.md --------------------------------------------------------------

def replace_block(text: str, block: str) -> str:
    """Swap the generated block in `text` for `block` (which carries its own
    markers). Raises ValueError unless the markers appear exactly once, in order."""
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) > text.index(END):
        raise ValueError(f"expected one {BEGIN!r} ... {END!r} pair")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return head + block.rstrip("\n") + tail


# ---- docs coverage -------------------------------------------------------------

_PARAM = re.compile(r"\{[^}/]*\}")
_DOC_PARAM = r"(?:\{[^}/\s]*\}|<[^>/\s]*>|:[\w-]+)"


def _route_key(path: str) -> str:
    return _PARAM.sub("{}", path)


def _route_pattern(path: str) -> re.Pattern[str]:
    """The path as docs may write it: any placeholder style for `{param}`, and
    not merely the start of a longer path (`/api/runtime` != `/api/runtime/log`)."""
    parts = [re.escape(seg) for seg in _PARAM.split(path)]
    return re.compile(_DOC_PARAM.join(parts) + r"(?![\w/-])")


def _command_pattern(cmd: Command) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + r"\s+".join(re.escape(w) for w in (PACKAGE, *cmd.path))
                      + r"(?![\w-])")


def docs_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in sorted((root / "docs").rglob("*.md")))


def added_surface(old: dict[str, str], new: dict[str, str]) -> tuple[list[Command], list[Route]]:
    """Visible CLI commands and `/api` routes in `new` that `old` lacks."""
    old_cmds = {c.path for c in cli_commands(old)}
    cmds = [c for c in cli_commands(new) if not c.hidden and c.path not in old_cmds]
    old_routes = {_route_key(r.path) for r in web_routes(old)}
    seen: set[str] = set()
    routes = []
    for r in web_routes(new):
        key = _route_key(r.path)
        if r.path.startswith("/api/") and key not in old_routes and key not in seen:
            seen.add(key)
            routes.append(r)
    return cmds, routes


def undocumented(cmds: list[Command], routes: list[Route], docs: str) -> list[str]:
    missing = [f"CLI   {c.text}" for c in cmds if not _command_pattern(c).search(docs)]
    missing += [f"route {r.path}  ({r.file}:{r.line})" for r in routes
                if not _route_pattern(r.path).search(docs)]
    return missing


# ---- main ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="print the facts block")
    show.add_argument("--json", action="store_true", help="print the facts as JSON")
    for name, text in (("write", "rewrite the block in HANDOFF.md"),
                       ("check", "exit 1 if HANDOFF.md's block is stale")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--file", type=Path, default=None, help="default: HANDOFF.md")
    cov = sub.add_parser("docs-coverage", help="new CLI commands and /api routes must be in docs/")
    cov.add_argument("--since", default=DEFAULT_SINCE,
                     help=f"git ref to compare with (default {DEFAULT_SINCE})")
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()

    if args.cmd == "docs-coverage":
        try:
            old = git_sources(root, args.since)
        except (OSError, RuntimeError) as exc:
            print(f"docs coverage: cannot read {args.since}: {exc}\n(a shallow clone has no"
                  " tags: run `git fetch --tags`, or check out with fetch-depth: 0)",
                  file=sys.stderr)
            return 2
        cmds, routes = added_surface(old, tree_sources(root))
        missing = undocumented(cmds, routes, docs_text(root))
        print(f"docs coverage: {len(cmds)} CLI commands and {len(routes)} /api routes"
              f" added since {args.since}; {len(missing)} not mentioned under docs/.")
        for m in missing:
            print(f"  {m}")
        return 1 if missing else 0

    facts = collect(root)
    if args.cmd == "show":
        if args.json:
            print(json.dumps(facts, indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(render(facts))
        return 0

    path = args.file or root / "HANDOFF.md"
    try:
        text = path.read_text(encoding="utf-8")
        updated = replace_block(text, render(facts))
    except (OSError, ValueError) as exc:
        print(f"handoff facts: {path}: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        if updated != text:
            print(f"handoff facts: {path.name} is stale; run"
                  " `python scripts/handoff_facts.py write`.")
            return 1
        print(f"handoff facts: {path.name} is up to date.")
        return 0
    if updated != text:
        path.write_bytes(updated.encode("utf-8"))
        print(f"handoff facts: rewrote the generated block in {path.name}.")
    else:
        print(f"handoff facts: {path.name} already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
