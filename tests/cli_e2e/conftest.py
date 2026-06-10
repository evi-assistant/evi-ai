"""CLI end-to-end harness.

Drives the *real* `evi` CLI (`python -m evi …`) as a subprocess against an
isolated ``EVI_HOME``, so these tests exercise the whole entrypoint —
argument parsing, config wiring, file I/O, output formatting — the way a user
does. Unit tests call functions directly; these catch CLI-wiring regressions
unit tests can't see.

No browser/Playwright needed (unlike ``tests/e2e/``), so this dir has its own
conftest. Tests are marked ``e2e`` so they're excluded from the default
``pytest`` run and selected with ``pytest tests/cli_e2e -m e2e``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class CliResult:
    code: int
    stdout: str
    stderr: str

    @property
    def out(self) -> str:
        """stdout + stderr combined (Rich sometimes writes to either)."""
        return self.stdout + self.stderr

    def json(self):
        return json.loads(self.stdout)


@pytest.fixture
def evi_cli(tmp_path):
    """Return a callable that runs `python -m evi <args>` against an isolated
    home. The callable exposes ``.home`` (the EVI_HOME path) and ``.workdir``
    (a scratch dir for building source plugins/skills to import)."""
    home = tmp_path / "evihome"
    home.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()

    def run(*args: str, check: bool = True, input_text: str | None = None,
            timeout: int = 90) -> CliResult:
        proc = subprocess.run(
            [sys.executable, "-m", "evi", *args],
            env={
                **os.environ,
                "EVI_HOME": str(home),
                "NO_COLOR": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            },
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            input=input_text,
            timeout=timeout,
        )
        res = CliResult(proc.returncode, proc.stdout or "", proc.stderr or "")
        if check:
            assert proc.returncode == 0, (
                f"`evi {' '.join(args)}` exited {proc.returncode}\n"
                f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
            )
        return res

    run.home = home          # type: ignore[attr-defined]
    run.workdir = workdir     # type: ignore[attr-defined]
    return run
