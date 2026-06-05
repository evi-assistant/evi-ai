"""Tests for the hook loader + runner."""

from __future__ import annotations

import sys
from pathlib import Path


from evi.hooks import Hook, HookRegistry, _run_hook, load_hooks


# ---- loader -------------------------------------------------------------


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    assert load_hooks(tmp_path / "no.toml").hooks == []


def test_load_parses_both_events(tmp_path: Path) -> None:
    p = tmp_path / "hooks.toml"
    p.write_text(
        """
[[before_tool_call]]
name = "audit"
match = "*"
command = ["true"]

[[before_tool_call]]
name = "block"
match = "write_file"
command = ["false"]
veto_on_nonzero = true

[[after_tool_call]]
name = "notify"
match = "generate_image"
command = "echo done"
""",
        encoding="utf-8",
    )
    reg = load_hooks(p)
    assert [h.name for h in reg.hooks] == ["audit", "block", "notify"]
    assert reg.hooks[1].veto_on_nonzero is True
    # Single-string command form is allowed.
    assert reg.hooks[2].command == ["echo done"]


def test_load_skips_malformed_entries(tmp_path: Path) -> None:
    p = tmp_path / "hooks.toml"
    p.write_text(
        """
[[before_tool_call]]
name = "no-command"
# command missing

[[before_tool_call]]
name = "ok"
command = ["true"]
""",
        encoding="utf-8",
    )
    reg = load_hooks(p)
    assert [h.name for h in reg.hooks] == ["ok"]


# ---- matching -----------------------------------------------------------


def test_applies_to_glob() -> None:
    h = Hook(name="x", event="before_tool_call", match="fs.*", command=["true"])
    assert h.applies_to("fs.read_file") is True
    assert h.applies_to("write_file") is False


def test_for_event_filters() -> None:
    reg = HookRegistry(
        hooks=[
            Hook("a", "before_tool_call", "*", ["true"]),
            Hook("b", "after_tool_call", "*", ["true"]),
        ]
    )
    assert [h.name for h in reg.for_event("before_tool_call", "x")] == ["a"]
    assert [h.name for h in reg.for_event("after_tool_call", "x")] == ["b"]


# ---- runner -------------------------------------------------------------


def test_run_hook_passes_env_vars(tmp_path: Path) -> None:
    out_file = tmp_path / "env.txt"
    # Use the running Python so this works cross-platform without bash.
    cmd = [
        sys.executable,
        "-c",
        (
            "import os\n"
            f"open(r'{out_file}', 'w').write("
            "os.environ.get('EVI_HOOK_TOOL','') + '\\n' + "
            "os.environ.get('EVI_HOOK_ARGS_JSON','') + '\\n' + "
            "os.environ.get('EVI_HOOK_EVENT',''))\n"
        ),
    ]
    hook = Hook("env-probe", "before_tool_call", "*", cmd, timeout=10)
    res = _run_hook(hook, "fs.read_file", '{"path":"x"}', result_output=None)
    assert res.exit_code == 0
    contents = out_file.read_text().splitlines()
    assert contents[0] == "fs.read_file"
    assert contents[1] == '{"path":"x"}'
    assert contents[2] == "before_tool_call"


def test_run_before_vetoes_on_nonzero() -> None:
    bad = Hook(
        "block",
        "before_tool_call",
        "*",
        [sys.executable, "-c", "import sys; sys.stderr.write('nope'); sys.exit(3)"],
        timeout=10,
        veto_on_nonzero=True,
    )
    reg = HookRegistry(hooks=[bad])
    _, veto = reg.run_before("any", "{}")
    assert veto is not None
    assert veto.exit_code == 3
    assert "nope" in veto.stderr


def test_run_before_nonzero_without_veto_does_not_block() -> None:
    noisy = Hook(
        "warn",
        "before_tool_call",
        "*",
        [sys.executable, "-c", "import sys; sys.exit(1)"],
        timeout=10,
        veto_on_nonzero=False,
    )
    reg = HookRegistry(hooks=[noisy])
    _, veto = reg.run_before("any", "{}")
    assert veto is None


def test_after_hook_receives_result(tmp_path: Path) -> None:
    out_file = tmp_path / "result.txt"
    cmd = [
        sys.executable,
        "-c",
        (
            "import os\n"
            f"open(r'{out_file}', 'w').write(os.environ.get('EVI_HOOK_RESULT',''))\n"
        ),
    ]
    hook = Hook("capture", "after_tool_call", "*", cmd, timeout=10)
    reg = HookRegistry(hooks=[hook])
    reg.run_after("t", "{}", tool_output="hello world")
    assert out_file.read_text() == "hello world"


def test_run_hook_timeout() -> None:
    slow = Hook(
        "slow",
        "before_tool_call",
        "*",
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=0.5,
    )
    res = _run_hook(slow, "x", "{}", result_output=None)
    assert res.timed_out is True
    assert res.exit_code == 124
