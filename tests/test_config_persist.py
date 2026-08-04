"""Config round-trip persistence — guards the "section written but never read
back" class of bug.

`Config.load()` builds each nested settings object from `data[<section>]`. If a
section is present on the dataclass but missing from `load()`, that section is
*write-only*: `save()` serialises it (via `asdict`) but `load()` silently drops
it back to defaults — and because `save()` rewrites the whole file, editing any
OTHER section clobbers the orphaned one. That's exactly what happened to
`[desktop]`. These tests round-trip a non-default value through save→load for
the sections the web Settings UI now exposes, so a future orphaned section fails
here instead of shipping as a dead control.
"""
from __future__ import annotations

import evi.config as C
from evi.config import Config


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CONFIG_PATH", tmp_path / "config.toml")


def test_desktop_section_survives_roundtrip(tmp_path, monkeypatch):
    # The original bug: Config.load() never read [desktop], so this reverted to
    # the True default on reload (and any other section's save clobbered it).
    _isolate(tmp_path, monkeypatch)
    cfg = Config()
    cfg.desktop.sidecar_auto_update = False
    cfg.save()
    assert Config.load().desktop.sidecar_auto_update is False


def test_other_saved_section_does_not_clobber_desktop(tmp_path, monkeypatch):
    # Regression for the clobber half of the bug: save a desktop value, then load
    # + save an unrelated change; desktop must persist across the second write.
    _isolate(tmp_path, monkeypatch)
    cfg = Config()
    cfg.desktop.sidecar_auto_update = False
    cfg.save()

    reloaded = Config.load()
    reloaded.llm.temperature = 0.42  # touch an unrelated section
    reloaded.save()

    final = Config.load()
    assert final.desktop.sidecar_auto_update is False
    assert final.llm.temperature == 0.42


def test_ui_exposed_sections_roundtrip(tmp_path, monkeypatch):
    # Every section the Settings UI now writes must survive a round-trip, or the
    # control is a dead end. One distinctive value per newly-exposed section.
    _isolate(tmp_path, monkeypatch)
    cfg = Config()
    cfg.models.vision = "moondream"
    cfg.statusline.enabled = True
    cfg.worktree.base_ref = "main"
    cfg.llm.router_enabled = True
    cfg.llm.seed = 1234
    cfg.tools.tool_search = True
    cfg.auto.block_destructive = False
    cfg.save()

    got = Config.load()
    assert got.models.vision == "moondream"
    assert got.statusline.enabled is True
    assert got.worktree.base_ref == "main"
    assert got.llm.router_enabled is True
    assert got.llm.seed == 1234
    assert got.tools.tool_search is True
    assert got.auto.block_destructive is False
