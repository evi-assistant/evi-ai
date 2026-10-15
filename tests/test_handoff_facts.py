"""Tests for scripts/handoff_facts.py: the generated facts block in HANDOFF.md
and the check that docs/ mentions every CLI command and /api route added since
a release.

The parsers run on synthetic sources, and once against the real CLI and web
app (the static parse must agree with what Typer and FastAPI build). The
docs-coverage tests build a throwaway git repo under tmp_path, the way
tests/test_docs_freshness.py does. The roadmap tests keep docs/roadmap.md
internally consistent (every phase present, every dependency a listed item,
the counts in its prose equal to its tables), and the last ones check docs
that quote the code or the roadmap's status.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "handoff_facts.py"


def _load():
    spec = importlib.util.spec_from_file_location("handoff_facts", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


hf = _load()


# ---- web routes ---------------------------------------------------------------

SERVER = '''
from fastapi import FastAPI

def create_app():
    app = FastAPI()

    @app.get("/api/health")
    def health(): ...

    @app.post("/api/session/{session_id}/steer")
    async def steer(session_id: str): ...

    @router.api_route("/api/any", methods=["GET", "POST"])
    def any_(): ...

    @app.websocket("/ws/live")
    async def live(ws): ...

    @cache.get("not-a-path")
    def cached(): ...

    @app.get(PATH_FROM_A_VARIABLE)
    def dynamic(): ...

    app.add_api_route("/a2a", handler, methods=["POST"])
    return app
'''


def test_web_routes_find_decorators_and_add_api_route():
    routes = hf.web_routes({"evi/apps/web/server.py": SERVER})
    assert [(r.method, r.path) for r in routes] == [
        ("ANY", "/a2a"),
        ("ANY", "/api/any"),
        ("GET", "/api/health"),
        ("POST", "/api/session/{session_id}/steer"),
        ("WEBSOCKET", "/ws/live"),
    ]
    assert all(r.file == "evi/apps/web/server.py" and r.line > 0 for r in routes)


def test_web_routes_skip_unparsable_files():
    assert hf.web_routes({"evi/broken.py": "def (:\n"}) == []


def test_web_routes_ignore_http_client_calls():
    # A request eVi makes to another server (a backend probe, say) is not a
    # route eVi serves: it must not reach the HANDOFF counts or docs-coverage.
    probe = (
        "import httpx\n"
        "c = httpx.Client(base_url=base)\n"
        'c.get("/v1/models")\n'
        'r = client.post("/api/show", json={"model": m})\n'
        'requests.delete("/api/x").json()\n'
        "def f(client):\n"
        '    return client.get("/api/tags", timeout=2)\n'
        'app.get("/api/late")(handler)\n'  # FastAPI's call form is a route
    )
    routes = hf.web_routes({"evi/runtime/probe.py": probe})
    assert [(r.method, r.path) for r in routes] == [("GET", "/api/late")]


# ---- CLI commands ---------------------------------------------------------------

CLI = '''
import typer

app = typer.Typer()
model_app = typer.Typer(help="Models.")
app.add_typer(model_app, name="model")
web_app = typer.Typer()
app.add_typer(web_app, name="web-config")
token_app: typer.Typer = typer.Typer(name="token")
web_app.add_typer(token_app)
orphan_app = typer.Typer()

@app.command()
def review_remember(): ...

@app.command("trust")
def trust_cmd(): ...

@app.command(name="stats", hidden=True)
def stats_cmd(): ...

@model_app.command("use")
def model_use(): ...

@token_app.command()
def rotate(): ...

@orphan_app.command("never")
def never(): ...

app.command("link")(link_cmd)
'''


def _paths(cmds):
    return [(" ".join(c.path), c.group, c.hidden) for c in cmds]


def test_cli_commands_walk_groups_from_the_root():
    cmds = hf.cli_commands({hf.CLI_MODULE: CLI})
    assert _paths(cmds) == [
        ("link", False, False),
        ("model", True, False),
        ("model use", False, False),
        ("review-remember", False, False),  # Typer's default name: _ -> -
        ("stats", False, True),
        ("trust", False, False),
        ("web-config", True, False),
        ("web-config token", True, False),  # the name from Typer(name=...)
        ("web-config token rotate", False, False),
    ]
    # A Typer never attached to the root is not part of `evi`.
    assert not any("never" in c.path for c in cmds)
    assert cmds[2].text == "evi model use"


def test_cli_commands_only_read_the_cli_module():
    assert hf.cli_commands({"evi/other.py": CLI}) == []


def test_static_cli_parse_matches_the_real_typer_tree():
    typer = pytest.importorskip("typer")
    from evi.apps.cli.main import app

    real: set[tuple[str, ...]] = set()
    hidden: set[tuple[str, ...]] = set()

    def walk(cmd, prefix):
        for name, sub in (getattr(cmd, "commands", None) or {}).items():
            path = (*prefix, name)
            (hidden if getattr(sub, "hidden", False) else real).add(path)
            walk(sub, path)

    walk(typer.main.get_command(app), ())
    static = hf.cli_commands(hf.tree_sources(ROOT))
    assert {c.path for c in static if not c.hidden} == real
    assert {c.path for c in static if c.hidden} == hidden


def test_static_route_parse_matches_the_real_app():
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    import evi.apps.web.server as server_mod

    builtin = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    real = set()
    for route in server_mod.app.routes:
        methods = getattr(route, "methods", None)
        if not methods or route.path in builtin:
            continue  # mounts (/static) and FastAPI's own pages
        real |= {(m, route.path) for m in methods if m != "HEAD"}
    static = {(r.method, r.path) for r in hf.web_routes(hf.tree_sources(ROOT))
              if r.method not in ("ANY", "WEBSOCKET")}
    assert static == real


# ---- the facts block --------------------------------------------------------------

def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))  # LF, as the repo stores it


def _fake_repo(root: Path) -> None:
    _write(root, "pyproject.toml", (
        '[project]\nname = "evi-assistant"\nversion = "9.9.9"\n'
        'requires-python = ">=3.12"\n[project.scripts]\nevi = "evi.apps.cli.main:app"\n'
        '[project.optional-dependencies]\nweb = ["fastapi"]\nmcp = ["mcp"]\n'
    ))
    _write(root, "evi/__init__.py", "")
    _write(root, "evi/alpha.py", "")
    _write(root, "evi/beta.py", "")
    _write(root, "evi/apps/__init__.py", "")
    _write(root, "evi/apps/cli/__init__.py", "")
    _write(root, "evi/apps/cli/main.py", CLI)
    _write(root, "evi/apps/web/__init__.py", "")
    _write(root, "evi/apps/web/server.py", SERVER)
    _write(root, "evi/tools/__init__.py", '_BUILTIN_TOOL_MODULES = ("fs", "shell")\n')
    _write(root, "evi/tools/fs.py", (
        "from evi.tools.base import tool\n"
        "@tool(category='fs')\ndef read_file(path: str): ...\n"
        "@tool()\ndef write_file(path: str): ...\n"
        "def helper(): ...\n"
    ))
    _write(root, "tests/test_a.py", (
        "def test_one(): ...\n"
        "def helper(): ...\n"
        "class TestGroup:\n    def test_two(self): ...\n    def test_three(self): ...\n"
    ))
    _write(root, "tests/e2e/test_ui.py", "def test_page(): ...\n")
    _write(root, ".github/workflows/ci.yml", (
        "name: ci\n\n# what it does\non:\n  push:\n    branches: [main]\n"
        "  pull_request:   # PRs too\n  workflow_dispatch:\n\n"
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n  lint:\n    runs-on: ubuntu-latest\n"
    ))
    _write(root, ".github/workflows/example.yml",
           "name: 'evi-run (example)'\non: [workflow_dispatch, workflow_call]\njobs:\n  ask: {}\n")


def test_collect_reads_a_source_tree(tmp_path):
    _fake_repo(tmp_path)
    facts = hf.collect(tmp_path)
    assert facts["project"] == {
        "name": "evi-assistant", "version": "9.9.9", "requires_python": ">=3.12",
        "scripts": ["evi"], "extras": ["web", "mcp"],
    }
    assert facts["modules"]["evi/"] == ["alpha", "beta"]
    assert facts["modules"]["evi/apps/"] == []
    assert facts["modules"]["evi/tools/"] == ["fs"]
    assert facts["tools"] == {"modules": ["fs", "shell"], "tools": 2}
    assert len(facts["routes"]) == 5
    assert {"command": "evi model use", "group": False} in facts["cli"]
    assert not any(c["command"] == "evi stats" for c in facts["cli"])  # hidden
    tests = {t["layer"]: (t["files"], t["functions"]) for t in facts["tests"]}
    assert tests == {"unit": (1, 3), "UI e2e": (1, 1), "CLI e2e": (0, 0), "desktop e2e": (0, 0)}
    assert facts["workflows"] == [
        {"file": "ci.yml", "name": "ci", "triggers": ["push", "pull_request", "workflow_dispatch"],
         "jobs": ["test", "lint"]},
        {"file": "example.yml", "name": "evi-run (example)",
         "triggers": ["workflow_dispatch", "workflow_call"], "jobs": ["ask"]},
    ]


def test_render_is_deterministic_and_carries_its_markers(tmp_path):
    _fake_repo(tmp_path)
    block = hf.render(hf.collect(tmp_path))
    assert block == hf.render(hf.collect(tmp_path))
    assert block.startswith(hf.BEGIN + "\n") and block.endswith(hf.END + "\n")
    assert "| Built-in tools (`@tool` functions under `evi/tools/`) | 2 |" in block
    assert "| Test files, unit (`tests/test_*.py`) | 1 |" in block
    assert "test function" not in block  # changes too often; `show --json` has it
    assert "| `evi model` | use |" in block
    assert "| `evi web-config token` | rotate |" in block
    assert "`evi/apps/`" not in block  # nothing to list
    assert "\r" not in block


HANDOFF = "# Handoff\n\nIntro.\n\n{block}\n\n## After\n\nKept as is.\n"


def test_write_then_check(tmp_path):
    _fake_repo(tmp_path)
    handoff = tmp_path / "HANDOFF.md"
    stale = HANDOFF.format(block=f"{hf.BEGIN}\nold numbers\n{hf.END}")
    handoff.write_text(stale, encoding="utf-8")

    assert hf.main(["--root", str(tmp_path), "check"]) == 1
    assert handoff.read_text(encoding="utf-8") == stale  # check never writes

    assert hf.main(["--root", str(tmp_path), "write"]) == 0
    text = handoff.read_bytes().decode("utf-8")
    assert "old numbers" not in text and "\r" not in text
    assert text.startswith("# Handoff\n\nIntro.\n\n" + hf.BEGIN)
    assert text.endswith(hf.END + "\n\n## After\n\nKept as is.\n")
    assert hf.main(["--root", str(tmp_path), "check"]) == 0

    # A test added to an existing file leaves the block current, so a feature
    # commit need not regenerate HANDOFF.md...
    _write(tmp_path, "tests/test_a.py",
           (tmp_path / "tests/test_a.py").read_text(encoding="utf-8") + "def test_four(): ...\n")
    assert hf.main(["--root", str(tmp_path), "check"]) == 0

    # ...while a new module makes it stale again.
    _write(tmp_path, "evi/gamma.py", "")
    assert hf.main(["--root", str(tmp_path), "check"]) == 1


@pytest.mark.parametrize("text", [
    "no markers at all\n",
    f"{hf.BEGIN}\n{hf.BEGIN}\n{hf.END}\n",
    f"{hf.END}\nbackwards\n{hf.BEGIN}\n",
])
def test_a_missing_or_doubled_marker_is_a_usage_error(tmp_path, text):
    _fake_repo(tmp_path)
    (tmp_path / "HANDOFF.md").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        hf.replace_block(text, "x")
    assert hf.main(["--root", str(tmp_path), "write"]) == 2
    assert (tmp_path / "HANDOFF.md").read_text(encoding="utf-8") == text


def test_show_json_is_valid(tmp_path, capsys):
    import json

    _fake_repo(tmp_path)
    assert hf.main(["--root", str(tmp_path), "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["project"]["version"] == "9.9.9"


def test_the_real_handoff_carries_one_generated_block():
    text = (ROOT / "HANDOFF.md").read_text(encoding="utf-8")
    assert text.count(hf.BEGIN) == 1 and text.count(hf.END) == 1
    assert hf.replace_block(text, hf.BEGIN + "\n" + hf.END) != ""


# ---- docs coverage ------------------------------------------------------------------

def test_route_pattern_accepts_any_placeholder_style():
    pat = hf._route_pattern("/api/session/{session_id}/steer")
    for doc in ("POST /api/session/{id}/steer", "`/api/session/<id>/steer`",
                "/api/session/:sid/steer.", "/api/session/{session_id}/steer?x=1"):
        assert pat.search(doc), doc
    assert not pat.search("/api/session/{id}/steering")
    assert not pat.search("/api/session/steer")


def test_route_pattern_is_not_satisfied_by_a_longer_path():
    assert not hf._route_pattern("/api/runtime").search("GET /api/runtime/status")
    assert hf._route_pattern("/api/runtime/status").search("GET /api/runtime/status: x")


def test_command_pattern_needs_the_whole_command():
    cmd = hf.Command(("model", "use"), False, False)
    assert hf._command_pattern(cmd).search("Run `evi model use qwen` to switch.")
    assert hf._command_pattern(cmd).search("run evi model\n  use to switch")  # wrapped
    assert not hf._command_pattern(cmd).search("evi models use")
    assert not hf._command_pattern(hf.Command(("model",), True, False)).search("evi model-x")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
         "-c", "core.autocrlf=false", *args],
        cwd=repo, check=True, capture_output=True, timeout=60,
    )


OLD_CLI = 'import typer\napp = typer.Typer()\n@app.command("chat")\ndef chat(): ...\n'
NEW_CLI = OLD_CLI + (
    'runtime_app = typer.Typer()\napp.add_typer(runtime_app, name="runtime")\n'
    '@runtime_app.command("setup")\ndef setup(): ...\n'
    '@app.command("debug", hidden=True)\ndef debug(): ...\n'
)
OLD_SERVER = 'def f(app):\n    @app.get("/api/health")\n    def h(): ...\n'
NEW_SERVER = OLD_SERVER + (
    '    @app.post("/api/session/{session_id}/steer")\n    def s(): ...\n'
    '    @app.get("/api/health/{x}")\n    def hx(): ...\n'
    '    @app.get("/.well-known/agent-card.json")\n    def card(): ...\n'
)


def _coverage_repo(tmp_path: Path) -> Path:
    if not _have_git():
        pytest.skip("git not on PATH")
    repo = tmp_path / "repo"
    _write(repo, "evi/apps/cli/main.py", OLD_CLI)
    _write(repo, "evi/apps/web/server.py", OLD_SERVER)
    _write(repo, "docs/index.md", "# Docs\n\n`evi chat` and `GET /api/health`.\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "tag", "base")
    _write(repo, "evi/apps/cli/main.py", NEW_CLI)
    _write(repo, "evi/apps/web/server.py", NEW_SERVER)
    return repo


def _have_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def test_docs_coverage_lists_what_docs_do_not_mention(tmp_path, capsys):
    repo = _coverage_repo(tmp_path)
    assert hf.main(["--root", str(repo), "docs-coverage", "--since", "base"]) == 1
    out = capsys.readouterr().out
    assert "2 CLI commands and 2 /api routes added since base; 4 not mentioned" in out
    assert "CLI   evi runtime\n" in out and "CLI   evi runtime setup" in out
    assert "route /api/session/{session_id}/steer  (evi/apps/web/server.py:" in out
    assert "route /api/health/{x}" in out
    assert "evi debug" not in out  # hidden
    assert "agent-card" not in out  # not under /api/


def test_docs_coverage_passes_once_everything_is_mentioned(tmp_path, capsys):
    repo = _coverage_repo(tmp_path)
    _write(repo, "docs/features/runtime.md", (
        "# Runtime\n\n`evi runtime setup` downloads it; see the `evi runtime`\n"
        "group. Steer with `POST /api/session/<id>/steer`; `GET /api/health/{name}`.\n"
    ))
    assert hf.main(["--root", str(repo), "docs-coverage", "--since", "base"]) == 0
    assert "0 not mentioned under docs/" in capsys.readouterr().out


def test_git_sources_reads_the_tree_at_a_ref(tmp_path):
    repo = _coverage_repo(tmp_path)
    assert hf.git_sources(repo, "base") == {
        "evi/apps/cli/main.py": OLD_CLI, "evi/apps/web/server.py": OLD_SERVER}


def test_a_renamed_route_parameter_or_a_moved_route_is_not_new():
    def server(path: str, file: str = "evi/apps/web/server.py") -> dict[str, str]:
        return {file: f'def f(app):\n    @app.get("{path}")\n    def h(): ...\n'}

    assert hf.added_surface(server("/api/s/{a}"), server("/api/s/{b}")) == ([], [])
    assert hf.added_surface(server("/api/s"), server("/api/s", "evi/apps/web/x.py")) == ([], [])


def test_docs_coverage_with_an_unknown_ref_is_a_usage_error(tmp_path, capsys):
    repo = _coverage_repo(tmp_path)
    assert hf.main(["--root", str(repo), "docs-coverage", "--since", "no-such-ref"]) == 2
    err = capsys.readouterr().err
    assert "cannot read no-such-ref" in err
    assert "git fetch --tags" in err  # the usual cause: a shallow clone


def _has_ref(ref: str) -> bool:
    if not _have_git():
        return False
    proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                          cwd=ROOT, capture_output=True)
    return proc.returncode == 0


@pytest.mark.skipif(not _has_ref(hf.DEFAULT_SINCE),
                    reason=f"{hf.DEFAULT_SINCE} is not in this checkout (shallow clone)")
def test_docs_mention_every_cli_command_and_api_route_added_since_1_0_37(capsys):
    """The acceptance check for the 1.0.37-1.0.52 docs catch-up: everything
    added since v1.0.36 is mentioned somewhere under docs/."""
    assert hf.main(["docs-coverage"]) == 0, capsys.readouterr().out


# What 1.0.37 through Phase 0 batch 2 added: `docs-coverage` against v1.0.36.
# The test above needs that tag, and CI's unit job checks out shallow (no
# tags), so this list carries the same check into CI; the tests after it keep
# the list honest.
CATCH_UP_COMMANDS = (
    "evi channel", "evi channel approve", "evi channel revoke", "evi channel serve",
    "evi channel status",
    "evi model", "evi model list", "evi model remove", "evi model use",
    "evi runtime", "evi runtime gpu", "evi runtime setup", "evi runtime status",
    "evi runtime stop",
    "evi sessions import", "evi trust",
)
CATCH_UP_ROUTES = (
    "/api/backend/verify",
    "/api/channels/telegram", "/api/channels/telegram/approve",
    "/api/channels/telegram/restart", "/api/channels/telegram/revoke",
    "/api/integrations/status", "/api/project/trust",
    "/api/runtime/catalog", "/api/runtime/detect", "/api/runtime/gpu", "/api/runtime/locate",
    "/api/runtime/log", "/api/runtime/model/remove", "/api/runtime/model/use",
    "/api/runtime/plan", "/api/runtime/setup", "/api/runtime/status", "/api/runtime/stop",
    "/api/session/{session_id}/steer", "/api/specialty/status", "/api/voice/status",
)


def test_docs_mention_the_surface_added_since_1_0_37_without_git_history():
    cmds = [hf.Command(tuple(c.split()[1:]), False, False) for c in CATCH_UP_COMMANDS]
    routes = [hf.Route(p, "GET", "", 0) for p in CATCH_UP_ROUTES]
    assert hf.undocumented(cmds, routes, hf.docs_text(ROOT)) == []


def test_the_catch_up_list_names_real_commands_and_routes():
    sources = hf.tree_sources(ROOT)
    assert set(CATCH_UP_COMMANDS) <= {c.text for c in hf.cli_commands(sources) if not c.hidden}
    assert set(CATCH_UP_ROUTES) <= {r.path for r in hf.web_routes(sources)}


@pytest.mark.skipif(not _has_ref(hf.DEFAULT_SINCE),
                    reason=f"{hf.DEFAULT_SINCE} is not in this checkout (shallow clone)")
def test_the_catch_up_list_is_what_git_says_was_added():
    cmds, routes = hf.added_surface(hf.git_sources(ROOT, hf.DEFAULT_SINCE), hf.tree_sources(ROOT))
    added_cmds = {c.text for c in cmds}
    added_routes = {r.path for r in routes}
    assert set(CATCH_UP_COMMANDS) <= added_cmds
    assert set(CATCH_UP_ROUTES) <= added_routes


# ---- docs/roadmap.md ------------------------------------------------------------------

_ROW = re.compile(r"^\| `(?P<id>[a-z0-9-]+)` \|(?P<cells>.*)\|$")


def _roadmap_items() -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
    phases: list[str] = []
    deps: dict[str, list[str]] = {}
    status: dict[str, str] = {}
    for line in (ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^## (Phase \d+)\b", line):
            phases.append(m.group(1))
        elif m := _ROW.match(line):
            cells = [c.strip() for c in m.group("cells").split("|")]
            # | `id` | what | size | owner | depends on | status |
            deps[m.group("id")] = re.findall(r"`([a-z0-9-]+)`", cells[3])
            status[m.group("id")] = cells[4]
    return phases, deps, status


def test_roadmap_lists_six_phases_and_its_items():
    phases, deps, status = _roadmap_items()
    assert phases == [f"Phase {n}" for n in range(6)]
    assert len(deps) == 69
    assert {"fix-security-hotfix", "loose-docs-catch-up", "acp-server",
            "mcp-oauth-and-secrets"} <= set(deps)


def test_roadmap_dependencies_name_listed_items():
    _, deps, _ = _roadmap_items()
    unknown = {(item, d) for item, ds in deps.items() for d in ds if d not in deps}
    assert not unknown


def test_roadmap_marks_the_shipped_items():
    _, _, status = _roadmap_items()
    assert status["fix-security-hotfix"].startswith("Shipped in 1.0.53")
    done = {i for i, s in status.items() if s.startswith("Done")}
    assert {"loose-ci-release-gates", "safety-project-trust-gate",
            "fix-managed-runtime-ctx"} <= done
    assert all(s.startswith(("Shipped", "Done", "Planned")) for s in status.values())
    assert status["acp-server"].startswith("Planned")


_NUMBER_WORDS = {"five": 5, "six": 6, "seven": 7, "eight": 8}


def test_roadmap_counts_match_its_tables():
    """The counts in the prose are typed by hand when an item's Status cell
    flips, so they must agree with the tables."""
    text = (ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")
    phases, _, status = _roadmap_items()

    m = re.search(r"The plan has (\d+) items in (\w+) phases", text)
    assert m, "the intro no longer says how many items and phases there are"
    assert (int(m[1]), _NUMBER_WORDS.get(m[2])) == (len(status), len(phases))

    shipped = sum(s.startswith("Shipped") for s in status.values())
    done = sum(s.startswith("Done") for s in status.values())
    m = re.search(r"\*\*Status \([\d-]+\):\*\* (\d+) items? shipped in [\d.]+ and (\d+) more\s+are\s+done",
                  text)
    assert m, "the Status line no longer says how many items shipped and are done"
    assert (int(m[1]), int(m[2])) == (shipped, done)

    progress_lines = 0
    for section in re.split(r"^## ", text, flags=re.M)[1:]:
        p = re.search(r"^Progress: (\d+) of (\d+) items shipped or done\.$", section, re.M)
        if not p:
            continue
        progress_lines += 1
        cells = [[c.strip() for c in row["cells"].split("|")]
                 for row in map(_ROW.match, section.splitlines()) if row]
        finished = sum(c[4].startswith(("Shipped", "Done")) for c in cells)
        assert (int(p[1]), int(p[2])) == (finished, len(cells)), section.splitlines()[0]
    assert progress_lines >= 1


# ---- docs that quote the code ----------------------------------------------------------

def _doc(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _comparison_status(feature: str) -> str:
    for line in _doc("docs/claude-code-comparison.md").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells[0] == feature:
            return cells[-1]
    raise AssertionError(f"no {feature!r} row in docs/claude-code-comparison.md")


def test_skill_tool_scoping_docs_follow_the_roadmap():
    """A tool restriction people may rely on: the docs call it working only
    once the roadmap item that applies skill scope inside the agent loop is
    done, and then all of them do (the comparison row, its "Deferred" bullet
    and skills.md)."""
    _, _, status = _roadmap_items()
    landed = status["fix-loop-state-propagation"].startswith(("Done", "Shipped"))
    row = _comparison_status("Skills tool-scoping (allowed/disallowed-tools)")
    deferred = [line for line in _doc("docs/claude-code-comparison.md").splitlines()
                if line.startswith("- ") and "Skill tool-scoping" in line]
    not_enforced = "`allowed-tools` / `disallowed-tools` are not enforced yet" in _doc(
        "docs/features/skills.md")
    if landed:
        assert row.startswith("✅") and not not_enforced, (
            "fix-loop-state-propagation is done: update the comparison row and skills.md")
    else:
        assert not row.startswith("✅"), row
        assert not any("shipped" in line.lower() or "~~" in line for line in deferred), deferred
        assert not_enforced


def test_runtime_doc_gives_the_real_model_path():
    from evi.runtime import catalog

    path = catalog.model_path({"hf_repo": "owner/repo", "filename": "file.gguf"})
    assert path.relative_to(catalog.MODELS_DIR).as_posix() == "owner__repo/file.gguf"
    assert "`~/.evi/models/<owner>__<repo>/<file>.gguf`" in _doc("docs/features/runtime.md")


def test_onboarding_does_not_promise_gemini_works_without_a_key():
    # Since Phase 0 batch 2 the gemini backend needs an API key or Vertex AI.
    assert "GEMINI_API_KEY" in _doc("evi/llm/gemini_agent.py")
    row = next(line for line in _doc("docs/features/onboarding.md").splitlines()
               if line.startswith("| 1 |"))
    assert "No key" not in row
    assert "Gemini CLI needs a Gemini API key or Vertex AI" in row
