#!/usr/bin/env python3
"""Set the desktop app version across the four Tauri files, in place.

Used by `desktop-release.yml` to sync the desktop version to the release tag, so
the built app + updater `latest.json` always report the right version WITHOUT a
human remembering to bump four files. Safe to run locally too:

    python scripts/set-desktop-version.py 1.0.5

Edits (first/relevant occurrence only, formatting otherwise preserved):
  * desktop/src-tauri/tauri.conf.json   -> top-level "version"
  * desktop/src-tauri/Cargo.toml        -> [package] version
  * desktop/package.json                -> "version"
  * desktop/src-tauri/Cargo.lock        -> the evi-desktop [[package]] entry
"""

from __future__ import annotations

import pathlib
import re
import sys

_SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+].+)?$")


def _sub_once(path: pathlib.Path, pattern: str, repl: str, *, flags=0) -> None:
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n != 1:
        raise SystemExit(f"{path}: expected exactly one match for /{pattern}/, got {n}")
    # Force LF (the repo is `eol=lf`) so this never introduces CRLF noise on Windows.
    path.write_text(new, encoding="utf-8", newline="\n")


def set_version(root: pathlib.Path, version: str) -> None:
    # tauri.conf.json: the first "version": "..." (top-level app version).
    _sub_once(root / "desktop/src-tauri/tauri.conf.json",
              r'"version":\s*"[^"]+"', f'"version": "{version}"')
    # Cargo.toml: the [package] version is the first bare `version = "..."`.
    _sub_once(root / "desktop/src-tauri/Cargo.toml",
              r'(?m)^version = "[^"]+"', f'version = "{version}"')
    # package.json: the first "version": "..." (top-level).
    _sub_once(root / "desktop/package.json",
              r'"version":\s*"[^"]+"', f'"version": "{version}"')
    # Cargo.lock: only the evi-desktop package entry (name line then version line).
    _sub_once(root / "desktop/src-tauri/Cargo.lock",
              r'(name = "evi-desktop"\nversion = ")[^"]+(")',
              rf'\g<1>{version}\g<2>')


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not _SEMVER.match(argv[1]):
        print("usage: set-desktop-version.py X.Y.Z", file=sys.stderr)
        return 2
    version = argv[1]
    root = pathlib.Path(__file__).resolve().parent.parent
    set_version(root, version)
    print(f"desktop version set to {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
