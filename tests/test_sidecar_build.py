"""The frozen desktop sidecar carries the scheduler, and its self-check proves
it: without APScheduler the app's scheduler never started, so scheduled tasks
and the Companion's check-ins and brief silently never ran on the desktop."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_both_sidecar_builds_install_and_collect_the_scheduler():
    for name in ("build-sidecar.ps1", "build-sidecar.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        extras = re.search(r"\[([a-z,-]+)\]\"", text)
        assert extras and "scheduler" in extras.group(1).split(","), name
        assert "--collect-submodules apscheduler" in text, name
        assert "--copy-metadata apscheduler" in text, name


def test_the_self_check_imports_the_scheduler():
    text = (ROOT / "scripts" / "sidecar_entry.py").read_text(encoding="utf-8")
    assert '"apscheduler.schedulers.background"' in text
    assert '"apscheduler.triggers.cron"' in text
