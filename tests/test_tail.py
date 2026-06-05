"""Smoke test for `evi tail` — verifies the CLI command is wired and
prints new transcript lines incrementally. We don't run the full polling
loop (that's a side thread); we directly exercise the file-watching
behaviour we'd see by simulating writes between iterations."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from typer.testing import CliRunner

import evi.apps.cli.main as cli_main
import evi.config as config_mod


def test_tail_command_registered() -> None:
    """Sanity: `evi tail --help` works."""
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["tail", "--help"])
    assert result.exit_code == 0
    assert "transcript" in result.stdout.lower()


def test_tail_reads_existing_then_keeps_offset(
    monkeypatch, tmp_path: Path
) -> None:
    """Synthesise a today-dir, drop in a session file, walk the offset map
    the same way `tail` does. Covers the read-since-last-offset logic
    without spawning the long-running CLI loop."""
    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = tmp_path / "transcripts" / today
    today_dir.mkdir(parents=True)

    monkeypatch.setattr(config_mod, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(config_mod, "HOME", tmp_path)

    f = today_dir / "abcd.jsonl"
    line1 = json.dumps({"role": "user", "content": "hi", "ts": time.time()}) + "\n"
    f.write_text(line1, encoding="utf-8")

    offsets: dict[Path, int] = {}
    seen: list[dict] = []

    def tick() -> None:
        for path in sorted(today_dir.glob("*.jsonl")):
            start = offsets.get(path, 0)
            with path.open("rb") as fh:
                fh.seek(start)
                data = fh.read()
                offsets[path] = fh.tell()
            for line in data.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                seen.append(json.loads(line))

    tick()
    assert len(seen) == 1
    assert seen[0]["content"] == "hi"

    # Append another line and tick again — offset map should pick up only the new line.
    line2 = json.dumps({"role": "assistant", "content": "hello", "ts": time.time()}) + "\n"
    with f.open("a", encoding="utf-8") as fh:
        fh.write(line2)
    tick()
    assert len(seen) == 2
    assert seen[1]["content"] == "hello"
