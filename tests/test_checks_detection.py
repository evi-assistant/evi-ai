"""Which project checks run for a turn's changes, and how one runs
(evi/checks.py; loop-verify-gate).

`detect(root, config, trusted, changed_files)` picks the checks for what the
turn changed: ruff on the changed .py files when there is a ruff config, the
targeted tests only (a changed test file, tests/test_<module>.py), npm test
for JS/TS, cargo check for Rust, and `[checks] commands`. Nothing in an
untrusted folder, and a project's own `[checks]` only when the folder is
trusted and that .evi.toml version approved.

Acceptance criterion covered here:
- A [checks] entry from an untrusted repo is ignored —
  test_a_checks_entry_from_an_untrusted_repo_is_ignored (and the changed
  .evi.toml case after it)

eVi's home and trust list are under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evi import checks, hooks as hooks_mod, redact, trust
from evi.checks import Check, detect, run_check
from evi.config import Config

PY = sys.executable


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config and trust list under tmp_path, no safe mode."""
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", evi_home / "hooks.toml")
    return evi_home


@pytest.fixture
def proj(tmp_path) -> Path:
    """A Python project with a ruff config and a tests/ folder."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    (root / "pkg" / "calc.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg" / "util.py").write_text("y = 2\n", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    return root


def _cfg(**checks_settings) -> SimpleNamespace:
    return SimpleNamespace(checks=SimpleNamespace(**checks_settings))


def _names(found: list[Check]) -> list[tuple[str, str]]:
    return [(c.name, c.source) for c in found]


def test_ruff_on_the_changed_files_and_only_the_targeted_tests(proj):
    found = detect(proj, Config(), True, ["pkg/calc.py", "README.md"])
    assert _names(found) == [("ruff", "pyproject"), ("pytest", "pytest")]
    ruff, pytest_ = found
    assert ruff.argv[-3:] == ("check", "--no-cache", "pkg/calc.py") and ruff.kind == "lint"
    assert pytest_.argv[-1] == "tests/test_calc.py" and pytest_.kind == "test"
    assert "tests" not in pytest_.argv  # never the whole suite
    assert pytest_.ok_codes == (0, 5)


def test_test_detection_beside_module_and_in_tests_subfolder(proj):
    # 1. Changed calc.py with test_calc.py beside it (at root)
    (proj / "calc.py").write_text("pass")
    (proj / "test_calc.py").write_text("pass")
    (proj / "tests" / "test_calc.py").write_text("pass")
    targets = checks._test_targets(proj, ["calc.py"])
    assert targets == ["tests/test_calc.py", "test_calc.py"]

    # 2. Changed pkg/util.py with pkg/test_util.py
    (proj / "pkg" / "test_util.py").write_text("pass")
    targets = checks._test_targets(proj, ["pkg/util.py"])
    assert targets == ["pkg/test_util.py"]

    # 3. Changed pkg/util.py with both pkg/test_util.py and tests/pkg/test_util.py
    (proj / "tests" / "pkg").mkdir(exist_ok=True)
    (proj / "tests" / "pkg" / "test_util.py").write_text("pass")
    targets = checks._test_targets(proj, ["pkg/util.py"])
    assert targets == ["pkg/test_util.py", "tests/pkg/test_util.py"]

    # 4. Changed conftest.py, __init__.py, __main__.py yield nothing
    for name in ("conftest.py", "__init__.py", "__main__.py"):
        (proj / name).write_text("pass")
        targets = checks._test_targets(proj, [name])
        assert targets == []


def test_a_changed_test_file_is_its_own_target(proj):
    found = detect(proj, Config(), True, ["tests/test_calc.py"])
    assert found[-1].argv[-1] == "tests/test_calc.py"


def test_a_module_without_tests_gets_ruff_only_and_non_python_gets_nothing(proj):
    assert _names(detect(proj, Config(), True, ["pkg/util.py"])) == [("ruff", "pyproject")]
    assert detect(proj, Config(), True, ["README.md", "docs/x.txt"]) == []
    assert detect(proj, Config(), True, []) == []


def test_no_ruff_without_a_ruff_config(proj):
    (proj / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    assert _names(detect(proj, Config(), True, ["pkg/calc.py"])) == [("pytest", "pytest")]
    (proj / "ruff.toml").write_text("line-length = 90\n", encoding="utf-8")
    assert _names(detect(proj, Config(), True, ["pkg/calc.py"]))[0] == ("ruff", "ruff.toml")


def test_deleted_files_caches_and_paths_outside_are_left_out(proj):
    assert detect(proj, Config(), True, ["pkg/gone.py"]) == []
    assert detect(proj, Config(), True, [".ruff_cache/x.py", "node_modules/a/b.js",
                                         "pkg/__pycache__/calc.py", "../elsewhere.py"]) == []


def test_nothing_runs_in_an_untrusted_folder(proj):
    assert detect(proj, Config(), False, ["pkg/calc.py"]) == []
    assert checks.allowlist(proj, Config(), False) == []


def test_npm_test_for_js_changes_when_package_json_has_a_real_test(proj, monkeypatch):
    monkeypatch.setattr(checks, "_which", lambda p: f"/bin/{p}")
    (proj / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    assert ("npm test", "package.json") in _names(detect(proj, Config(), True, ["src/a.ts"]))
    assert ("npm test", "package.json") not in _names(detect(proj, Config(), True, ["a.py"]))
    (proj / "package.json").write_text(json.dumps(
        {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
    assert detect(proj, Config(), True, ["src/a.ts"]) == []


def test_cargo_check_for_rust_changes(proj, monkeypatch):
    monkeypatch.setattr(checks, "_which", lambda p: f"/bin/{p}")
    assert detect(proj, Config(), True, ["src/main.rs"]) == []
    (proj / "Cargo.toml").write_text('[package]\nname = "x"\n')
    [cargo] = detect(proj, Config(), True, ["src/main.rs"])
    assert cargo.argv == ("/bin/cargo", "check") and cargo.source == "Cargo.toml"


def test_a_tool_that_is_not_installed_is_skipped(proj, monkeypatch):
    monkeypatch.setattr(checks, "_module_argv", lambda m: None)
    monkeypatch.setattr(checks, "_which", lambda p: None)
    assert detect(proj, Config(), True, ["pkg/calc.py"]) == []


def test_configured_commands_run_when_files_changed(proj):
    cfg = _cfg(commands=["make lint", ["tox", "-e", "py"]])
    found = detect(proj, cfg, True, ["README.md"])
    assert [(c.argv, c.source) for c in found] == [(("make", "lint"), "config"),
                                                   (("tox", "-e", "py"), "config")]
    assert detect(proj, cfg, True, []) == []
    only = detect(proj, _cfg(commands=["make lint"], detect=False), True, ["pkg/calc.py"])
    assert [c.argv for c in only] == [("make", "lint")]


def _project_checks(root: Path, command: str) -> None:
    (root / ".evi.toml").write_text(f'[checks]\ncommands = ["{command}"]\n', encoding="utf-8")


def test_a_checks_entry_from_an_untrusted_repo_is_ignored(home, proj):
    _project_checks(proj, "evil --exfiltrate")
    assert trust.status(proj).state == "untrusted"
    assert checks.project_commands(proj) == []
    # Even a caller that runs detection anyway (trusted=True) gets no project
    # command: the project file itself is only read once the folder is trusted.
    assert all(c.source != "project" for c in detect(proj, Config(), True, ["pkg/calc.py"]))
    assert ("evil", "--exfiltrate") not in checks.allowlist(proj, Config(), True)


def test_a_changed_evi_toml_in_a_trusted_repo_is_ignored_until_approved(home, proj):
    _project_checks(proj, "make check")
    trust.grant(proj)
    assert checks.project_commands(proj) == ["make check"]
    found = detect(proj, Config(), True, ["pkg/calc.py"])
    assert ("make", "check") in [c.argv for c in found if c.source == "project"]
    assert ("make", "check") in checks.allowlist(proj, Config(), True)
    _project_checks(proj, "evil --exfiltrate")  # edited after it was trusted
    assert trust.status(proj).state == "changed"
    assert checks.project_commands(proj) == []
    assert all(c.source != "project" for c in detect(proj, Config(), True, ["pkg/calc.py"]))


def test_the_allowlist_holds_each_detectable_tools_command(proj):
    prefixes = checks.allowlist(proj, _cfg(commands=["make lint"]), True)
    assert (PY, "-m", "ruff", "check") in prefixes
    assert (PY, "-m", "pytest") in prefixes
    assert ("make", "lint") in prefixes


@pytest.mark.parametrize(("raw", "verify", "rounds", "timeout"), [
    ({}, "auto", 2, 300.0),
    ({"verify": "ALWAYS", "max_rounds": 3, "timeout": 5}, "always", 3, 5.0),
    ({"verify": "sometimes", "max_rounds": 0, "timeout": -1}, "auto", 2, 300.0),
    ({"verify": "off", "max_rounds": "x"}, "off", 2, 300.0),
])
def test_settings_are_checked(raw, verify, rounds, timeout):
    s = checks.settings(_cfg(**raw))
    assert (s.verify, s.max_rounds, s.timeout) == (verify, rounds, timeout)
    assert Config().checks.verify == "auto" and Config().hooks.max_blocks == 3


# ---- running one ---------------------------------------------------------------


def _py(code: str, name: str = "probe") -> Check:
    return Check(name, (PY, "-c", code), "test", "config")


def test_a_check_runs_in_the_folder_and_keeps_the_tail(tmp_path):
    run = run_check(_py("import os\nfor i in range(100): print(i)\nprint(os.getcwd())"),
                    tmp_path, tail_lines=3)
    assert run.ok and run.exit == 0 and run.duration_ms >= 0
    assert run.tail.splitlines() == ["98", "99", str(tmp_path)]
    record = run.record()
    assert set(record) == {"name", "cmd", "exit", "duration_ms", "tail", "round", "source"}
    assert record["cmd"] == run.check.cmd and record["round"] == 1


def test_a_failing_check_and_pytests_no_tests_code(tmp_path):
    assert not run_check(_py("raise SystemExit(3)"), tmp_path).ok
    nothing = Check("pytest", (PY, "-c", "raise SystemExit(5)"), "test", "pytest", ok_codes=(0, 5))
    assert run_check(nothing, tmp_path).ok


def test_a_check_past_its_timeout_is_killed_and_fails(tmp_path):
    run = run_check(_py("import time; print('started', flush=True); time.sleep(30)"),
                    tmp_path, timeout=1)
    assert run.exit is None and not run.ok and "timed out after 1s" in run.tail
    assert run.duration_ms < 20_000


def test_a_check_that_cannot_start_is_a_failure_not_a_crash(tmp_path):
    run = run_check(Check("nope", (str(tmp_path / "no-such-program"),), "test", "config"),
                    tmp_path)
    assert run.exit is None and "could not start" in run.tail


def test_a_bare_python_runs_this_interpreter_not_whatever_windows_finds_first(tmp_path):
    # On Windows a venv's python.exe starts the base interpreter, and a bare
    # "python" spawned from there is found in the base install's folder first.
    run = run_check(Check("python", ("python", "-c", "import sys; print(sys.prefix)"),
                          "test", "task"), tmp_path)
    assert run.ok and run.tail.strip() == sys.prefix
    assert run.record()["cmd"].startswith("python ")  # the report keeps what was asked


def _fake_program(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / (name + (".exe" if sys.platform == "win32" else ""))
    exe.write_bytes(b"")
    exe.chmod(0o755)
    return exe


def _same(got: list[str], want: list[str]) -> bool:
    """argv equal, but the program's case as the platform compares it
    (shutil.which on Windows spells the extension as PATHEXT does)."""
    return [os.path.normcase(got[0]), *got[1:]] == [os.path.normcase(want[0]), *want[1:]]


def test_bare_program_names_resolve_to_paths(tmp_path):
    env = {"PATH": str(tmp_path / "bin")}
    tool = _fake_program(tmp_path / "bin", "lintit")
    assert _same(checks.resolve_argv(("lintit", "-q"), tmp_path, env), [str(tool), "-q"])
    assert checks.resolve_argv(("python3", "-V"), tmp_path, env) == [sys.executable, "-V"]
    own = _fake_program(tmp_path / ".venv" / ("Scripts" if sys.platform == "win32" else "bin"),
                        "python")
    assert checks.resolve_argv(("python", "-V"), tmp_path, env) == [str(own), "-V"]
    # A path, or a name found nowhere, is left alone.
    assert checks.resolve_argv(("./x.sh",), tmp_path, env) == ["./x.sh"]
    assert checks.resolve_argv(("no-such-tool",), tmp_path, env) == ["no-such-tool"]


def test_a_frozen_build_looks_python_up_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    env = {"PATH": str(tmp_path / "bin")}
    py = _fake_program(tmp_path / "bin", "python")
    assert _same(checks.resolve_argv(("python", "-V"), tmp_path, env), [str(py), "-V"])


def test_a_check_gets_no_secrets_and_its_output_is_redacted(tmp_path, monkeypatch):
    secret = "Zq8vN2kLp4Xr7Tw1Ys5M-check"
    monkeypatch.setenv("ACME_API_KEY", secret)
    redact.refresh()
    try:
        run = run_check(_py("import os; print(os.environ.get('ACME_API_KEY', 'scrubbed'))\n"
                            f"print({secret!r})"), tmp_path)
        assert run.tail.splitlines()[0] == "scrubbed"
        assert secret not in run.tail
    finally:
        redact.refresh()


def test_the_failure_note_names_each_failing_command(tmp_path):
    bad = run_check(_py("print('E   assert 1 == 2'); raise SystemExit(1)", "pytest"), tmp_path)
    good = run_check(_py("pass", "ruff"), tmp_path)
    note = checks.failure_note([bad, good], 1, 2)
    assert "round 1 of 2" in note and "exit 1" in note and "assert 1 == 2" in note
    assert bad.check.cmd in note and good.check.cmd not in note
