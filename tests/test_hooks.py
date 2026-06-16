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


# ---- http hooks (Phase 81) ----------------------------------------------


def test_load_parses_url_hook(tmp_path: Path) -> None:
    p = tmp_path / "hooks.toml"
    p.write_text(
        '[[after_tool_call]]\nname = "wh"\nmatch = "*"\nurl = "https://x/y"\n',
        encoding="utf-8",
    )
    h = load_hooks(p).hooks[0]
    assert h.url == "https://x/y" and h.command == []


def test_url_hook_posts_and_succeeds() -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            received.update(json.loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):  # silence
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/hook"

    hook = Hook(name="wh", event="after_tool_call", match="*", command=[], url=url, timeout=5)
    res = _run_hook(hook, "write_file", '{"path":"x"}', result_output="done")
    srv.server_close()

    assert res.exit_code == 0 and not res.vetoed
    assert received["tool"] == "write_file" and received["result"] == "done"


def test_url_hook_non_2xx_vetoes() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(403)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/hook"

    hook = Hook(name="gate", event="before_tool_call", match="*", command=[], url=url,
                veto_on_nonzero=True, timeout=5)
    reg = HookRegistry(hooks=[hook])
    _, veto = reg.run_before("write_file", "{}")
    srv.server_close()
    assert veto is not None and veto.exit_code == 403


# ---- lifecycle hooks (expanded events) ----------------------------------


def test_load_lifecycle_events(tmp_path: Path) -> None:
    p = tmp_path / "hooks.toml"
    p.write_text(
        '[[user_prompt_submit]]\nname="u"\ncommand=["true"]\n'
        '[[before_compact]]\nname="c"\ncommand=["true"]\n'
        '[[stop]]\nname="s"\ncommand=["true"]\n',
        encoding="utf-8",
    )
    reg = load_hooks(p)
    assert {h.event for h in reg.hooks} == {"user_prompt_submit", "before_compact", "stop"}


def test_lifecycle_veto_blocks() -> None:
    hook = Hook(
        name="block", event="user_prompt_submit", match="*",
        command=[sys.executable, "-c", "import sys; sys.exit(1)"],
        veto_on_nonzero=True, timeout=10,
    )
    _results, veto = HookRegistry(hooks=[hook]).run_lifecycle("user_prompt_submit", payload="hi")
    assert veto is not None and veto.vetoed


def test_lifecycle_zero_exit_no_veto() -> None:
    hook = Hook(
        name="ok", event="user_prompt_submit", match="*",
        command=[sys.executable, "-c", "pass"], veto_on_nonzero=True, timeout=10,
    )
    _results, veto = HookRegistry(hooks=[hook]).run_lifecycle("user_prompt_submit", payload="hi")
    assert veto is None


def test_lifecycle_payload_in_env(tmp_path: Path) -> None:
    out = tmp_path / "p.txt"
    code = (
        "import os, pathlib; "
        f"pathlib.Path({out.as_posix()!r}).write_text(os.environ.get('EVI_HOOK_ARGS_JSON',''))"
    )
    hook = Hook(name="probe", event="stop", match="*",
                command=[sys.executable, "-c", code], timeout=10)
    HookRegistry(hooks=[hook]).run_lifecycle("stop", payload="the-prompt")
    assert out.read_text() == "the-prompt"


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


def test_command_hook_sees_effort(monkeypatch, tmp_path: Path) -> None:
    # A command hook gets the active reasoning effort as $EVI_EFFORT.
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    from evi.config import Config

    cfg = Config()
    cfg.llm.reasoning_effort = "high"
    cfg.save()
    hook = Hook(
        name="e", event="before_tool_call", match="*",
        command=[sys.executable, "-c", "import os; print(os.environ.get('EVI_EFFORT', ''))"],
        timeout=10,
    )
    res = _run_hook(hook, "read_file", "{}", result_output=None)
    assert res.stdout.strip() == "high"


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


# ---- editor helpers (read_raw / validate / write_raw) ----------------------


def test_editor_helpers_roundtrip(tmp_path) -> None:
    from evi import hooks as hooks_mod

    p = tmp_path / "hooks.toml"
    assert hooks_mod.read_raw(p) == ""  # absent -> empty
    text = (
        '[[before_tool_call]]\nname = "audit"\nmatch = "*"\n'
        'command = ["echo", "hi"]\n'
    )
    assert hooks_mod.validate(text) is None
    hooks_mod.write_raw(text, p)
    assert hooks_mod.read_raw(p) == text
    loaded = [h.name for h in hooks_mod._parse_hook_file(p)]
    assert loaded == ["audit"]


def test_validate_rejects_bad_toml() -> None:
    from evi import hooks as hooks_mod

    assert hooks_mod.validate("this = = bad") is not None


def test_validate_rejects_unknown_event() -> None:
    from evi import hooks as hooks_mod

    # an event-name typo: the runtime loader would silently never fire this
    err = hooks_mod.validate('[[before_toolcall]]\nname = "x"\ncommand = ["echo"]\n')
    assert err is not None and "before_toolcall" in err


def test_validate_rejects_entry_without_command_or_url() -> None:
    from evi import hooks as hooks_mod

    err = hooks_mod.validate('[[stop]]\nname = "x"\n')
    assert err is not None and "stop" in err
