#!/usr/bin/env python3
"""Write the throwaway eVi config the evi-run action runs with.

The backend settings come from the environment (EVI_CFG_BACKEND,
EVI_CFG_BASE_URL, EVI_CFG_API_KEY, EVI_CFG_MODEL). When neither a backend nor a
base URL is given, nothing is written and the caller's own $EVI_HOME is used.
Otherwise `<home>/config.toml` is written owner-only, and `home=<home>` goes to
$GITHUB_OUTPUT for the run step.

The config always says `[auto] auto_approve = []`. eVi's default pre-approves
the fs, code, memory, skills and image tool categories, so over untrusted text
(a PR diff, an issue body) the model could run Python and read or write any
file, this config and its API key included. With the list empty, every tool
call needs approval, and `evi run` without `--yes` denies what needs approval:
the run fails closed. The action's `auto_approve: "true"` passes `--yes`,
which approves everything regardless.

    python evi_run_config.py "$RUNNER_TEMP/evi-ci"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_KEYS = (
    ("backend", "EVI_CFG_BACKEND"),
    ("base_url", "EVI_CFG_BASE_URL"),
    ("api_key", "EVI_CFG_API_KEY"),
    ("model", "EVI_CFG_MODEL"),
)


def toml_string(value: str) -> str:
    """A TOML basic string: quotes, backslashes and control characters escaped."""
    out = []
    for ch in value:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif (ch < " " and ch != "\t") or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def render(llm: dict[str, str]) -> str:
    lines = ["[llm]"]
    lines += [f"{key} = {toml_string(value)}" for key, value in llm.items() if value]
    lines += [
        "",
        "# Fail closed: no tool category runs without approval, and `evi run`",
        "# without --yes denies whatever needs approval.",
        "[auto]",
        "auto_approve = []",
    ]
    return "\n".join(lines) + "\n"


def write_config(home: Path, llm: dict[str, str]) -> Path:
    """Write `home/config.toml` (owner-only) and return its path."""
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    path = home / "config.toml"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(llm))
    return path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: evi_run_config.py <home>", file=sys.stderr)
        return 2
    llm = {key: os.environ.get(env, "") for key, env in ENV_KEYS}
    if not (llm["backend"] or llm["base_url"]):
        return 0  # no backend given: the caller's own $EVI_HOME config applies
    home = Path(args[0])
    write_config(home, llm)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"home={home}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
