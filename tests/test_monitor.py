"""Tests for the monitor tool (bounded file-tail / command watch)."""

from __future__ import annotations

import json
import threading
import time

import evi.tools.monitor  # noqa: F401  registers the tool
from evi.tools.base import REGISTRY


def _call(**kwargs) -> str:
    return REGISTRY["monitor"].call(json.dumps(kwargs))


def test_tail_file_collects_new_lines(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("old line\n", encoding="utf-8")  # pre-existing — must NOT appear

    def _writer():
        time.sleep(0.3)
        with log.open("a", encoding="utf-8") as f:
            f.write("hello world\n")
            f.flush()

    threading.Thread(target=_writer, daemon=True).start()
    out = _call(target=str(log), kind="file", until="hello", timeout=5)
    assert "hello world" in out
    assert "old line" not in out  # only NEW lines after open
    assert "matched" in out


def test_tail_file_times_out_quietly(tmp_path):
    log = tmp_path / "quiet.log"
    log.write_text("x\n", encoding="utf-8")
    out = _call(target=str(log), kind="file", timeout=1)
    assert "no new lines" in out or "timed out" in out


def test_tail_missing_file_errors(tmp_path):
    out = _call(target=str(tmp_path / "nope.log"), kind="file", timeout=1)
    assert out.startswith("ERROR: not a file")


def test_command_collects_output_and_exit(tmp_path):
    import sys

    out = _call(
        target=f'{sys.executable} -c "print(\'build ok\')"',
        kind="command", timeout=15,
    )
    assert "build ok" in out
    assert "exited 0" in out


def test_command_until_matches_early(tmp_path):
    import sys

    # prints READY then would sleep; until=READY returns immediately
    code = "import time;print('READY',flush=True);time.sleep(30)"
    out = _call(target=f'{sys.executable} -c "{code}"', kind="command",
                until="READY", timeout=20)
    assert "READY" in out and "matched" in out


def test_bad_until_regex():
    out = _call(target="whatever", kind="file", until="(", timeout=1)
    assert out.startswith("ERROR: bad until regex")
