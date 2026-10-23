"""Tests for multi-agent code review (Phase 70)."""

from __future__ import annotations

import evi.review as review


def test_lenses_present():
    assert {"correctness", "security", "performance", "tests"} == set(review.REVIEW_LENSES)


def test_multi_review_combines_per_lens(monkeypatch):
    captured = {}

    def fake_parallel(tasks, *, system_prompt, tool_categories=(), tools=None,
                      unattended_policy=None, cwd=None):
        captured["cwd"] = cwd
        captured["tasks"] = tasks
        captured["system_prompt"] = system_prompt
        captured["cats"] = tool_categories
        captured["tools"] = tools
        captured["policy"] = unattended_policy
        # echo a finding tagged by which lens the task mentions
        out = []
        for t in tasks:
            lens = next(k for k in review.REVIEW_LENSES if review.REVIEW_LENSES[k] in t)
            out.append((t, f"finding for {lens}"))
        return out

    monkeypatch.setattr("evi.llm.subagent.run_subagents_parallel", fake_parallel)

    report = review.multi_review("diff --git a b\n+x", tool_categories=("fs",))
    assert report.startswith("# Multi-agent review")
    for lens in review.REVIEW_LENSES:
        assert f"## {lens.title()}" in report
        assert f"finding for {lens}" in report
    # the diff is embedded in every task; the lens drives each one
    assert all("```diff" in t for t in captured["tasks"])
    assert captured["cats"] == ("fs",)
    # The diff is untrusted: reviewers get the read-only fs tools only (never
    # write_file / edit_file / apply_patch), and nothing else to run.
    from evi.tools.base import REGISTRY

    assert all(
        REGISTRY[n].read_only and REGISTRY[n].category == "fs" for n in captured["tools"]
    )
    assert {"read_file", "list_dir", "find_files", "search_files"} <= set(captured["tools"])
    assert not {"write_file", "edit_file", "apply_patch"} & set(captured["tools"])
    assert captured["policy"] == "allow"
    assert captured["cwd"] is None  # the session's folder, as before


def test_multi_review_subset_of_lenses(monkeypatch):
    monkeypatch.setattr(
        "evi.llm.subagent.run_subagents_parallel",
        lambda tasks, *, system_prompt, tool_categories=(), **_: [(t, "ok") for t in tasks],
    )
    report = review.multi_review("d", lenses=["security"])
    assert "## Security" in report
    assert "## Correctness" not in report


def test_the_reviewers_read_the_tree_the_diff_is_of(monkeypatch, tmp_path):
    """The verify gate reviews a task branch in its worktree: the reviewers'
    tools must read that tree, not the session's folder (the main checkout),
    or what they look up contradicts the diff."""
    seen = []
    monkeypatch.setattr(
        "evi.llm.subagent.run_subagents_parallel",
        lambda tasks, *, cwd=None, **_: seen.append(cwd) or [(t, "ok") for t in tasks],
    )
    review.multi_review("d", lenses=["tests"], cwd=tmp_path)
    assert seen == [str(tmp_path)]

    from evi import attest

    monkeypatch.setattr(review, "get_diff", lambda **_: "diff --git a/x b/x\n+x\n")
    monkeypatch.setattr(review, "load_review_context", lambda _p: "")
    gate = attest.review_gate("base", cwd=tmp_path)
    assert gate.exit_code == 0 and seen[-1] == str(tmp_path)
