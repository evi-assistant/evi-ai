#!/usr/bin/env python3
"""Fail on GitHub Actions expressions pasted into scripts (template injection).

Actions substitutes every `${{ ... }}` into a `run:` script (or an
actions/github-script `script:`) as raw text *before* the shell or JS runs it.
When the value is attacker-influenced — a PR title, a branch name, or a step
output carrying model text — that is code injection: a backtick, quote or `$(`
in the value becomes code. The fix is always the same: pass the value through
`env:` and read `$VAR` / `process.env.VAR`.

Script sinks checked: every step's `run:`, the inputs of well-known actions
that run their value as code (`SCRIPT_INPUTS`: github-script's `script`,
nick-fields/retry's `command`), and a `docker://` step's `args` / `entrypoint`
(the container's command line). Action names compare case-insensitively, as
GitHub resolves them.

This check is deliberately strict and deterministic: ANY expression inside a
script fails, even a harmless-looking `${{ runner.os }}` (use `$RUNNER_OS`), so
there is nothing to judge and nothing to allowlist. With `--zizmor` it also
runs zizmor (pedantic persona, `json-v1` output) and fails on its
template-injection findings, printing everything else zizmor reports as
advisory notes.

    python workflow_guard.py                       # the repo's workflows + actions
    python workflow_guard.py --zizmor examples/github/*.yml

Exit codes: 0 clean, 1 findings, 2 usage error (no files, bad YAML, zizmor
missing, failing or producing output this script does not recognise).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATTERNS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".github/actions/*/action.yml",
    ".github/actions/*/action.yaml",
)
# zizmor's audit id for code injection via template expansion.
INJECTION_AUDIT = "template-injection"
# Inputs of well-known actions whose value runs as code (keyed by the
# lower-cased `owner/repo` of `uses:`).
SCRIPT_INPUTS: dict[str, tuple[str, ...]] = {
    "actions/github-script": ("script",),
    "nick-fields/retry": ("command",),
}
# A `docker://image` step runs `entrypoint` with `args` as its command line.
DOCKER_SCRIPT_INPUTS = ("entrypoint", "args")


@dataclass
class Finding:
    path: str
    line: int
    where: str
    message: str


def _get(mapping: yaml.Node | None, key: str) -> yaml.Node | None:
    if not isinstance(mapping, yaml.MappingNode):
        return None
    for k, v in mapping.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def _items(node: yaml.Node | None) -> list[tuple[str, yaml.Node]]:
    if not isinstance(node, yaml.MappingNode):
        return []
    return [(k.value, v) for k, v in node.value if isinstance(k, yaml.ScalarNode)]


def _expression_line(node: yaml.ScalarNode) -> int:
    """1-based line of the first `${{` in a scalar (block scalars start one
    line below their `|` / `>` indicator)."""
    before = node.value[: node.value.index("${{")]
    if node.style in ("|", ">"):
        return node.start_mark.line + 2 + before.count("\n")
    return node.start_mark.line + 1


def _steps(root: yaml.Node) -> list[tuple[str, yaml.Node]]:
    """(label, step node) for every step of a workflow or composite action."""
    found: list[tuple[str, yaml.Node]] = []
    for job_id, job in _items(_get(root, "jobs")):
        steps = _get(job, "steps")
        for i, step in enumerate(steps.value if isinstance(steps, yaml.SequenceNode) else []):
            found.append((f"jobs.{job_id}.steps[{i}]", step))
    steps = _get(_get(root, "runs"), "steps")
    for i, step in enumerate(steps.value if isinstance(steps, yaml.SequenceNode) else []):
        found.append((f"runs.steps[{i}]", step))
    return found


def _label(where: str, step: yaml.Node) -> str:
    name = _get(step, "name") or _get(step, "id")
    return f"{where} ({name.value})" if isinstance(name, yaml.ScalarNode) else where


def _script_inputs(uses: yaml.Node | None) -> tuple[str, ...]:
    """The `with:` inputs of the action in `uses:` that run their value as code."""
    if not isinstance(uses, yaml.ScalarNode):
        return ()
    ref = uses.value.strip().lower()
    if ref.startswith("docker://"):
        return DOCKER_SCRIPT_INPUTS
    # owner/repo[/path]@ref -> owner/repo
    action = "/".join(ref.split("@", 1)[0].split("/")[:2])
    return SCRIPT_INPUTS.get(action, ())


def scan_text(text: str, path: str) -> list[Finding]:
    """Every `${{` inside a `run:` or another script sink in one file."""
    findings: list[Finding] = []
    for root in yaml.compose_all(text):
        if root is None:
            continue
        for where, step in _steps(root):
            scripts = [("run", _get(step, "run"))]
            with_ = _get(step, "with")
            for name in _script_inputs(_get(step, "uses")):
                scripts.append((f"with.{name}", _get(with_, name)))
            for key, node in scripts:
                if isinstance(node, yaml.ScalarNode) and "${{" in node.value:
                    findings.append(Finding(
                        path, _expression_line(node), f"{_label(where, step)}.{key}",
                        "expression interpolated into a script; pass it through env: "
                        "and read $VAR / process.env.VAR instead",
                    ))
    return findings


def expand(patterns: list[str]) -> list[str]:
    files: list[str] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern)) if glob.has_magic(pattern) else [pattern]
        for match in matches:
            norm = Path(match).as_posix()
            if norm not in files and Path(match).is_file():
                files.append(norm)
    return files


# `json-v1` is zizmor's frozen JSON schema (zizmor >= 1.6; plain `json` is an
# alias that moves on a major release). `--no-exit-codes` keeps findings from
# setting the exit code, so any non-zero exit is an error, not a finding.
ZIZMOR_ARGS = ("--format=json-v1", "--no-exit-codes", "--persona=pedantic")


def run_zizmor(files: list[str]) -> tuple[list[Finding], list[str]]:
    """(template-injection findings, advisory notes) from zizmor's JSON output."""
    exe = shutil.which("zizmor")
    if exe is None:
        raise RuntimeError("zizmor is not installed (pip install zizmor)")
    proc = subprocess.run(
        [exe, *ZIZMOR_ARGS, *files],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"zizmor exited {proc.returncode}:\n{proc.stderr.strip()}")
    try:
        results = json.loads(proc.stdout)
    except ValueError:
        results = None
    if not isinstance(results, list):
        raise RuntimeError(
            f"zizmor printed no JSON list of findings:\n{proc.stdout.strip()[:500]}\n"
            f"{proc.stderr.strip()}"
        )
    return _split_zizmor(results)


def _zizmor_path(key: object) -> str:
    """`symbolic.key` -> a path: {"Local": {"verbatim_path": ...}} (older
    releases said `given_path`), {"Remote": {"path": ...}}."""
    if not isinstance(key, dict):
        return "?"
    local = key.get("Local")
    if isinstance(local, dict):
        return str(local.get("verbatim_path") or local.get("given_path") or "?")
    remote = key.get("Remote")
    if isinstance(remote, dict):
        return str(remote.get("path") or "?")
    return "?"


def _zizmor_location(result: dict) -> tuple[str, int]:
    """(path, 1-based line) of a finding's Primary location (else its first)."""
    locations = [loc for loc in result.get("locations") or [] if isinstance(loc, dict)]
    primary = [loc for loc in locations if (loc.get("symbolic") or {}).get("kind") == "Primary"]
    for loc in primary or locations:
        path = _zizmor_path((loc.get("symbolic") or {}).get("key"))
        point = ((loc.get("concrete") or {}).get("location") or {}).get("start_point") or {}
        row = point.get("row")
        return path, int(row) + 1 if isinstance(row, int) else 0  # json-v1 rows are 0-based
    return "?", 0


def _split_zizmor(results: list) -> tuple[list[Finding], list[str]]:
    errors: list[Finding] = []
    notes: list[str] = []
    for result in results:
        if not isinstance(result, dict) or "ident" not in result:
            # Fail closed: an output shape we don't understand must not pass.
            raise RuntimeError(f"unrecognised zizmor finding: {str(result)[:200]}")
        if result.get("ignored"):
            continue
        path, line = _zizmor_location(result)
        desc = result.get("desc") or result["ident"]
        if result["ident"] == INJECTION_AUDIT:
            errors.append(Finding(path, line, f"zizmor {INJECTION_AUDIT}", desc))
        else:
            determinations = result.get("determinations")
            severity = (determinations if isinstance(determinations, dict) else {}).get(
                "severity", "?")
            notes.append(f"{path}:{line}: zizmor {result['ident']} ({severity}): {desc}")
    return errors, notes


def _report(finding: Finding) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error file={finding.path},line={finding.line}::{finding.where}: "
              f"{finding.message}")
    else:
        print(f"{finding.path}:{finding.line}: {finding.where}: {finding.message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("patterns", nargs="*", help="extra workflow/action files or globs")
    parser.add_argument("--zizmor", action="store_true",
                        help="also run zizmor and fail on its template-injection findings")
    parser.add_argument("--no-defaults", action="store_true",
                        help="check only the given files, not the repo's workflows/actions")
    args = parser.parse_args(argv)

    patterns = list(args.patterns) if args.no_defaults else [*DEFAULT_PATTERNS, *args.patterns]
    files = expand(patterns)
    if not files:
        print("workflow guard: no workflow or action files found", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    for path in files:
        try:
            findings += scan_text(Path(path).read_text(encoding="utf-8"), path)
        except (OSError, yaml.YAMLError) as exc:
            print(f"workflow guard: cannot read {path}: {exc}", file=sys.stderr)
            return 2

    notes: list[str] = []
    if args.zizmor:
        try:
            zizmor_errors, notes = run_zizmor(files)
        except RuntimeError as exc:
            print(f"workflow guard: {exc}", file=sys.stderr)
            return 2
        findings += zizmor_errors

    for finding in findings:
        _report(finding)
    if notes:
        print(f"zizmor: {len(notes)} advisory finding(s), not gating:")
        for note in notes:
            print(f"  {note}")
    checked = ", ".join(files)
    if findings:
        print(f"workflow guard: {len(findings)} script injection finding(s) in {checked}")
        return 1
    print(f"workflow guard: no expressions in scripts ({checked})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
