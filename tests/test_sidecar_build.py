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


def test_the_desktop_builds_stamp_the_package_version():
    # The checked-in Tauri version is a placeholder; an app built with it looks
    # older than every release, and its updater then "upgrades" it to one.
    for name in ("build-desktop.ps1", "build-desktop.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "pyproject.toml" in text and "version.conf.json" in text, name
        assert text.index("version.conf.json") < text.index("npm run tauri build"), name
