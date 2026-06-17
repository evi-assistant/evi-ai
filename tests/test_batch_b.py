"""Batch B: models.dev catalog + config linter."""

import json

from evi import modelsdev
from evi.modelsdev import ModelInfo


# --- models.dev catalog -------------------------------------------------------

def test_baked_snapshot_parses():
    raw = json.loads(modelsdev.BAKED_CATALOG.read_text(encoding="utf-8"))
    flat = modelsdev._flatten(raw)
    assert "qwen2.5-coder" in flat
    qc = flat["qwen2.5-coder"]
    assert qc.tool_call is True and qc.context == 32768
    gpt = flat["gpt-4o"]
    assert gpt.vision is True and gpt.tool_call is True and gpt.input_cost == 2.5


def test_flatten_accepts_flat_shape():
    flat = modelsdev._flatten({
        "my-model": {"id": "my-model", "tool_call": True,
                     "modalities": {"input": ["text", "image"]},
                     "limit": {"context": 4096}},
    })
    assert flat["my-model"].vision is True
    assert flat["my-model"].context == 4096


def test_lookup_exact_canonical_and_prefix(monkeypatch):
    cat = {
        "qwen2.5-coder": ModelInfo(id="qwen2.5-coder", context=32768, tool_call=True),
        "gpt-4o": ModelInfo(id="gpt-4o", context=128000, tool_call=True, vision=True),
    }
    monkeypatch.setattr(modelsdev, "load_catalog", lambda: cat)
    assert modelsdev.lookup("gpt-4o").context == 128000             # exact
    assert modelsdev.lookup("openai/gpt-4o").context == 128000      # provider prefix
    assert modelsdev.lookup("qwen2.5-coder:14b-q4").tool_call       # ollama tag
    assert modelsdev.lookup("totally-unknown-model") is None


def test_capabilities_uses_catalog_override(monkeypatch):
    from evi.capabilities import capabilities

    monkeypatch.setattr(
        modelsdev, "lookup",
        lambda mid: ModelInfo(id=mid, vision=True, reasoning=True,
                              tool_call=True, audio=False) if "known" in mid else None,
    )
    caps = capabilities("known-model")
    assert caps["vision"] and caps["reasoning"] and caps["tools"]
    # Unknown model falls back to heuristics (qwen2.5 → tools via heuristic).
    caps2 = capabilities("qwen2.5:7b")
    assert isinstance(caps2["tools"], bool)


def test_context_window_for_prefers_catalog(monkeypatch):
    from evi import recommend

    monkeypatch.setattr(
        modelsdev, "lookup",
        lambda mid: ModelInfo(id=mid, context=999999) if "big" in mid else None,
    )
    assert recommend.context_window_for("big-model") == 999999


# --- config linter ------------------------------------------------------------

def _write_skill(d, name, *, desc="does a thing", body="# hi", extra=""):
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {desc}\n{extra}---\n{body}\n"
    (d / "SKILL.md").write_text(fm, encoding="utf-8")


def test_lint_path_clean(tmp_path):
    from evi import configlint

    _write_skill(tmp_path / "good", "good")
    assert configlint.lint_path(tmp_path) == []


def test_lint_path_flags_missing_description(tmp_path):
    from evi import configlint

    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: bad\n---\n# body\n", encoding="utf-8")
    issues = configlint.lint_path(tmp_path)
    assert any(i.level == "error" and "description" in i.message for i in issues)


def test_lint_path_flags_broken_ref_and_big_body(tmp_path):
    from evi import configlint

    big = "x" * 9000
    _write_skill(tmp_path / "big", "big",
                 body=f"See [helper](helper.py)\n{big}")
    issues = configlint.lint_path(tmp_path)
    assert any("broken file reference" in i.message for i in issues)
    assert any("body is" in i.message for i in issues)


def test_lint_returns_list_without_raising():
    from evi import configlint

    assert isinstance(configlint.lint(), list)
