"""Tests for scripts/check_docs_freshness.py — new CLI commands and web routes
must come with a docs/ change (or a `docs-skip` label).

Everything runs on synthetic diffs; the end-to-end test builds a throwaway git
repo under tmp_path and runs the script the way the docs-freshness workflow
does (`--base <ref>`).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "check_docs_freshness.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_docs_freshness", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


cdf = _load()


def _file_diff(path: str, added: list[str], removed: list[str] = (), *, new: bool = False) -> str:
    """A one-hunk unified diff for `path` adding/removing the given lines."""
    old_header = "/dev/null" if new else f"a/{path}"
    body = [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    start = 0 if new else 10
    return "\n".join([
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- {old_header}",
        f"+++ b/{path}",
        f"@@ -{start},{len(removed)} +{start or 1},{len(added)} @@",
        *body,
    ]) + "\n"


ROUTE = [
    '@app.get("/api/widgets")',
    "def list_widgets():",
    "    return []",
]
COMMAND = [
    '@widget_app.command("list")',
    "def widget_list():",
    "    pass",
]
DOC = ["## Widgets", "", "`GET /api/widgets` lists widgets."]


# ---- detection -------------------------------------------------------------

def test_route_without_docs_fails():
    ok, report = cdf.check(_file_diff("evi/apps/web/server.py", ROUTE))
    assert not ok
    text = "\n".join(report)
    assert "web route" in text and "/api/widgets" in text
    assert "docs-skip" in text  # the failure names the escape hatch


def test_finding_carries_the_new_file_line_number():
    scan = cdf.scan_diff(_file_diff("evi/apps/web/server.py", ROUTE))
    assert [(f.path, f.line, f.kind) for f in scan.findings] == [
        ("evi/apps/web/server.py", 10, "web route")
    ]


@pytest.mark.parametrize("decorator", [
    '@app.post("/api/widgets")',
    '@app.delete("/api/widgets/{wid}")',
    '@app.patch("/api/widgets/{wid}")',
    '@app.websocket("/ws/widgets")',
    '@router.api_route("/api/widgets", methods=["GET"])',
    '    @app.put("/api/widgets/{wid}")',  # nested (inside a factory function)
])
def test_every_http_decorator_counts_as_a_route(decorator):
    scan = cdf.scan_diff(_file_diff("evi/apps/web/server.py", [decorator, "def f(): ..."]))
    assert [f.kind for f in scan.findings] == ["web route"]


def test_add_api_route_call_counts_as_a_route():
    diff = _file_diff("evi/a2a.py", ['app.add_api_route("/a2a", handler, methods=["POST"])'])
    assert [f.kind for f in cdf.scan_diff(diff).findings] == ["web route"]


def test_typer_command_without_docs_fails():
    ok, report = cdf.check(_file_diff("evi/apps/cli/main.py", COMMAND))
    assert not ok
    assert "CLI command" in "\n".join(report)


def test_typer_group_counts_as_a_command():
    diff = _file_diff("evi/apps/cli/main.py",
                      ['widget_app = typer.Typer(help="Widgets.")',
                       'app.add_typer(widget_app, name="widget")'])
    assert [f.kind for f in cdf.scan_diff(diff).findings] == ["CLI command group"]


def test_bare_app_command_decorator_counts():
    diff = _file_diff("evi/apps/cli/main.py", ["@app.command()", "def widgets(): ..."])
    assert [f.kind for f in cdf.scan_diff(diff).findings] == ["CLI command"]


# ---- what passes -----------------------------------------------------------

def test_route_with_a_docs_change_passes():
    diff = (_file_diff("evi/apps/web/server.py", ROUTE)
            + _file_diff("docs/features/widgets.md", DOC, new=True))
    ok, report = cdf.check(diff)
    assert ok
    assert "comes with a docs/ change" in report[0]


def test_docs_skip_label_passes_and_says_so():
    ok, report = cdf.check(_file_diff("evi/apps/web/server.py", ROUTE), {"docs-skip"})
    assert ok
    assert "docs-skip" in report[0]


def test_other_labels_do_not_skip():
    ok, _ = cdf.check(_file_diff("evi/apps/web/server.py", ROUTE), {"enhancement", "docs"})
    assert not ok


def test_routes_outside_the_package_are_ignored():
    # Tests and scripts spin up throwaway apps all the time.
    diff = (_file_diff("tests/test_widgets.py", ROUTE)
            + _file_diff("scripts/fake_server.py", COMMAND))
    assert cdf.check(diff)[0]


def test_non_python_files_are_ignored():
    diff = _file_diff("evi/apps/web/static/index.html", ['@app.get("/x") in a comment'])
    assert cdf.check(diff)[0]


def test_removed_route_passes():
    diff = _file_diff("evi/apps/web/server.py", [], removed=ROUTE)
    assert cdf.check(diff)[0]


def test_moved_route_is_not_new_surface():
    # The same decorator removed from one module and added to another (a file
    # split) is a move.
    diff = (_file_diff("evi/apps/web/server.py", [], removed=ROUTE)
            + _file_diff("evi/apps/web/widgets.py", ROUTE, new=True))
    assert cdf.scan_diff(diff).findings == []


def test_moving_one_command_and_adding_another_still_fails():
    diff = (_file_diff("evi/apps/cli/main.py", ["@app.command()", "def old(): ..."],
                       removed=["@app.command()", "def old(): ..."])
            + _file_diff("evi/apps/cli/main.py", ["@app.command()", "def new(): ..."]))
    assert len(cdf.scan_diff(diff).findings) == 1


def test_removed_line_starting_with_dashes_is_not_a_file_header():
    # "-- note" removed reads as "--- note" in a diff; counting hunk lines keeps
    # it from being taken for the next file's header.
    diff = "\n".join([
        "diff --git a/evi/apps/web/server.py b/evi/apps/web/server.py",
        "--- a/evi/apps/web/server.py",
        "+++ b/evi/apps/web/server.py",
        "@@ -1,2 +1,2 @@",
        "--- note",
        '+@app.get("/api/widgets")',
        " def f(): ...",
    ]) + "\n"
    scan = cdf.scan_diff(diff)
    assert [(f.path, f.line) for f in scan.findings] == [("evi/apps/web/server.py", 1)]


def test_empty_diff_passes():
    assert cdf.check("") == (True, ["docs freshness: no new CLI commands or web routes."])


@pytest.mark.parametrize("separator", [" ", " ", "\x85", "\x0b", "\x0c", "\x1c"])
def test_unicode_line_separators_do_not_hide_a_route(separator):
    # str.splitlines() breaks on these too; the extra "lines" used to run the
    # hunk counter out early, so the route below them went unseen.
    diff = _file_diff("evi/apps/web/server.py",
                      [f'BANNER = "{separator * 6}"', *ROUTE])
    scan = cdf.scan_diff(diff)
    assert [(f.line, f.kind) for f in scan.findings] == [(11, "web route")]


def test_carriage_return_inside_a_line_does_not_hide_a_route(tmp_path):
    # Read as text, a bare "\r" would become a line break (universal newlines).
    diff = tmp_path / "change.diff"
    diff.write_bytes(_file_diff("evi/apps/web/server.py",
                                ['NOTE = "a\rb\rc\rd"', *ROUTE]).encode("utf-8"))
    proc = _run(["--diff-file", str(diff)], tmp_path)
    assert proc.returncode == 1, proc.stdout
    assert "evi/apps/web/server.py:11: web route" in proc.stdout


def test_crlf_diff_lines_still_parse():
    diff = _file_diff("evi/apps/web/server.py", ROUTE).replace("\n", "\r\n")
    assert [f.line for f in cdf.scan_diff(diff).findings] == [10]


# ---- moves: bare decorators are matched with the function they decorate ----

def test_replacing_one_bare_command_with_another_is_new_surface():
    # 20 of the CLI's top-level commands are a bare `@app.command()`; deleting
    # one and adding a different one is not a move.
    diff = _file_diff("evi/apps/cli/main.py",
                      ["@app.command()", "def brand_new(): ..."],
                      removed=["@app.command()", "def retired(): ..."])
    findings = cdf.scan_diff(diff).findings
    assert [(f.text, f.func) for f in findings] == [("@app.command()", "brand_new")]
    assert not cdf.check(diff)[0]


def test_moving_a_bare_command_to_another_file_is_a_move():
    diff = (_file_diff("evi/apps/cli/main.py", [],
                       removed=["@app.command()", "def widgets(): ..."])
            + _file_diff("evi/apps/cli/widgets.py",
                         ["@app.command()", "def widgets(): ..."], new=True))
    assert cdf.scan_diff(diff).findings == []


def test_a_bare_decorator_takes_its_name_from_a_context_def():
    # A decorator added above an existing function: the `def` is a context line.
    diff = "\n".join([
        "diff --git a/evi/apps/cli/main.py b/evi/apps/cli/main.py",
        "--- a/evi/apps/cli/main.py",
        "+++ b/evi/apps/cli/main.py",
        "@@ -5,2 +5,4 @@",
        "+@app.command()",
        "+@some_wrapper",
        " def existing():",
        "     pass",
    ]) + "\n"
    (finding,) = cdf.scan_diff(diff).findings
    assert (finding.line, finding.func) == (5, "existing")


def test_named_decorators_move_regardless_of_the_handler_name():
    # A route or explicitly named command is identified by its path/name.
    diff = (_file_diff("evi/apps/web/server.py", [],
                       removed=['@app.get("/api/widgets")', "def old_handler(): ..."])
            + _file_diff("evi/apps/web/widgets.py",
                         ['@app.get("/api/widgets")', "def new_handler(): ..."], new=True))
    assert cdf.scan_diff(diff).findings == []


def test_call_form_command_registration_counts():
    diff = _file_diff("evi/apps/cli/main.py", ['app.command("secret")(secret_cmd)'])
    assert [f.kind for f in cdf.scan_diff(diff).findings] == ["CLI command"]


# ---- the Docs-skip commit trailer --------------------------------------------

@pytest.mark.parametrize("messages, expected", [
    ("feat: x\n\nDocs-skip: internal route\n", "internal route"),
    ("feat: x\n\ndocs-skip:   follow-up PR\nCo-Authored-By: a <b@c>\n", "follow-up PR"),
    ("feat: x\n\nDocs-skip:\n", None),                 # a reason is required
    ("feat: x mentions Docs-skip: inline\n", None),   # only a line of its own
    ("", None),
])
def test_skip_trailer(messages, expected):
    assert cdf.skip_trailer(messages) == expected


def test_trailer_skips_and_says_so():
    ok, report = cdf.check(_file_diff("evi/apps/web/server.py", ROUTE), trailer="internal")
    assert ok
    assert "Docs-skip: internal" in report[0]


# ---- labels ----------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("", set()),
    ("docs-skip", {"docs-skip"}),
    ("bug, docs-skip ,", {"bug", "docs-skip"}),
    ('[\n  "docs-skip",\n  "ci"\n]', {"docs-skip", "ci"}),  # toJSON(labels.*.name)
    ("[]", set()),
])
def test_parse_labels(raw, expected):
    assert cdf.parse_labels(raw) == expected


# ---- the script, end to end ------------------------------------------------

def _run(args, cwd, **env):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=cwd, capture_output=True, text=True, timeout=120,
        env={**os.environ, "GITHUB_ACTIONS": "", "EVI_PR_LABELS": "", **env},
    )


def test_cli_diff_file_exit_codes(tmp_path):
    diff = tmp_path / "change.diff"
    diff.write_text(_file_diff("evi/apps/web/server.py", ROUTE), encoding="utf-8")
    assert _run(["--diff-file", str(diff)], tmp_path).returncode == 1
    assert _run(["--diff-file", str(diff), "--labels", "docs-skip"], tmp_path).returncode == 0
    missing = _run(["--diff-file", str(tmp_path / "nope.diff")], tmp_path)
    assert missing.returncode == 2


def test_cli_reads_labels_from_env(tmp_path):
    diff = tmp_path / "change.diff"
    diff.write_text(_file_diff("evi/apps/web/server.py", ROUTE), encoding="utf-8")
    proc = _run(["--diff-file", str(diff)], tmp_path, EVI_PR_LABELS='["docs-skip"]')
    assert proc.returncode == 0


def test_cli_emits_github_annotations(tmp_path):
    diff = tmp_path / "change.diff"
    diff.write_text(_file_diff("evi/apps/web/server.py", ROUTE), encoding="utf-8")
    proc = _run(["--diff-file", str(diff)], tmp_path, GITHUB_ACTIONS="true")
    assert proc.returncode == 1
    assert "::error file=evi/apps/web/server.py,line=10::" in proc.stdout


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        cwd=repo, check=True, capture_output=True, timeout=60,
    )


def test_cli_against_a_real_git_history(tmp_path):
    repo = tmp_path / "repo"
    (repo / "evi" / "apps" / "web").mkdir(parents=True)
    (repo / "docs").mkdir()
    server = repo / "evi" / "apps" / "web" / "server.py"
    server.write_text("app = object()\n", encoding="utf-8")
    (repo / "docs" / "index.md").write_text("# Docs\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")

    server.write_text("app = object()\n\n" + "\n".join(ROUTE) + "\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "add a route")
    proc = _run(["--base", "main"], repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "evi/apps/web/server.py:3: web route" in proc.stdout

    assert _run(["--base", "main", "--labels", "docs-skip"], repo).returncode == 0

    (repo / "docs" / "index.md").write_text("# Docs\n\nGET /api/widgets\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "document it")
    proc = _run(["--base", "main"], repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    assert _run(["--base", "no-such-ref"], repo).returncode == 2


def test_workflow_checks_pull_requests_and_pushes_to_main():
    # Releases have mostly landed as direct pushes to main; a PR-only trigger
    # never saw them.
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "docs-freshness.yml")
                        .read_text(encoding="utf-8"))
    triggers = wf.get("on", wf.get(True))
    assert triggers["pull_request"]["branches"] == ["main"]
    assert {"labeled", "unlabeled"} <= set(triggers["pull_request"]["types"])
    assert triggers["push"]["branches"] == ["main"]
    (step,) = [s for s in wf["jobs"]["docs-freshness"]["steps"]
               if "check_docs_freshness.py" in str(s.get("run", ""))]
    assert step["env"]["PUSH_BEFORE"] == "${{ github.event.before }}"
    assert 'base="$PUSH_BEFORE"' in step["run"]
    assert '--base "$base"' in step["run"]
    assert "continue-on-error" not in step


def test_cli_push_range_with_a_docs_skip_trailer(tmp_path):
    # What docs-freshness.yml does on a push to main: --base <event.before>.
    repo = tmp_path / "repo"
    (repo / "evi").mkdir(parents=True)
    module = repo / "evi" / "cli.py"
    module.write_text("app = object()\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                            text=True, check=True).stdout.strip()

    module.write_text("app = object()\n\n@app.command()\ndef secret(): ...\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "add a command")
    assert _run(["--base", before], repo).returncode == 1

    _git(repo, "commit", "-q", "--allow-empty", "-m",
         "chore: note it\n\nDocs-skip: internal debugging command")
    proc = _run(["--base", before], repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Docs-skip: internal debugging command" in proc.stdout
