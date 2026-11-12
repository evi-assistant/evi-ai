"""A pytest check that names a test file in files_in_scope fails when that
file collected no tests (evi.checks.expected_tests / empty_tests).

SK14-2's attempt 2 was accepted with a new test file of imports and comments
only: its required check named that file and two others, pytest ran the
others' tests and exited 0. These tests run real pytest in a temporary
folder; none touches eVi's home outside the fixtures.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from evi import checks, evidence, tasks
from tests.conftest import llm_call, llm_text
from tests.test_evidence import _agent, _cli, _record, _use_agents
from tests.test_evidence import home, repo  # noqa: F401  fixtures

PY = Path(sys.executable).as_posix()
PLACEHOLDER = "import pytest  # tests go here\nimport os\n"
REAL = "def test_one():\n    assert 1 + 1 == 2\n\n\ndef test_two():\n    assert True\n"
SKIPPED = "import pytest\n\n\n@pytest.mark.skip(reason='not here')\ndef test_later():\n    pass\n"
MODULE_SKIPPED = "import pytest\n\npytest.skip('not here', allow_module_level=True)\n"


def _pytest(*args: str) -> tuple[str, ...]:
    return (PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args)


@pytest.fixture
def folder(tmp_path) -> Path:
    """A folder with tests/test_old.py (two tests) and tests/test_new.py, a
    placeholder like SK14-2's."""
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_old.py").write_text(REAL, encoding="utf-8")
    (root / "tests" / "test_new.py").write_text(PLACEHOLDER, encoding="utf-8")
    return root


def _run(root: Path, argv: tuple[str, ...], scope=("tests/test_new.py",)) -> checks.CheckRun:
    check = checks.Check("python", argv, "test", "task",
                         expect_tests=checks.expected_tests(argv, scope))
    return checks.run_check(check, root, timeout=120)


# ---- which files a check must collect tests from -------------------------------------


def test_the_named_test_files_in_scope_are_expected():
    argv = _pytest("tests/test_new.py", "tests/test_old.py", "./tests/test_b.py::test_x")
    assert checks.expected_tests(argv, ["tests/test_new.py", "tests/test_b.py"]) == (
        "tests/test_new.py", "tests/test_b.py")
    # A glob in scope covers them too; a file out of scope is not the run's.
    assert checks.expected_tests(argv, ["tests/*.py"]) == (
        "tests/test_new.py", "tests/test_old.py", "tests/test_b.py")
    assert checks.expected_tests(argv, ["evi/x.py"]) == ()
    assert checks.expected_tests(argv, []) == ()


def test_a_folder_a_filter_an_option_value_or_another_tool_expects_nothing():
    scope = ["tests/test_new.py", "tests"]
    # A folder is not a file the check names.
    assert checks.expected_tests(_pytest("tests"), scope) == ()
    # -k / -m / --deselect may leave a file's tests out on purpose.
    for flt in (("-k", "new"), ("-kfoo",), ("-m", "slow"), ("--deselect", "tests/test_x.py::t")):
        assert checks.expected_tests(_pytest(*flt, "tests/test_new.py"), scope) == ()
    # An option's value is not a path to test.
    assert checks.expected_tests(_pytest("--ignore", "tests/test_new.py", "tests"), scope) == ()
    # Not a test file by name (a module for --doctest-modules, say).
    assert checks.expected_tests(_pytest("tests/helpers.py"), ["tests/helpers.py"]) == ()
    # Not pytest at all.
    assert checks.expected_tests((PY, "-m", "ruff", "check", "tests/test_new.py"), scope) == ()
    assert checks.expected_tests(("npm", "test", "tests/test_new.py"), scope) == ()
    # After "--" every word is a path.
    assert checks.expected_tests(_pytest("--", "tests/test_new.py"), scope) == (
        "tests/test_new.py",)


def test_junit_testcases_are_matched_to_their_files():
    junit = ('<testsuites><testsuite tests="3">'
             '<testcase classname="tests.test_old" name="test_one"/>'
             '<testcase classname="tests.test_cls.TestA" name="test_a[1.5]"/>'
             '<testcase classname="" name="tests.test_mod"><skipped/></testcase>'
             '<testcase classname="pkg.sub.tests.test_deep" name="t"/>'
             '<testcase classname="test_low" name="t"/>'
             '</testsuite></testsuites>')
    files = ["tests/test_old.py", "tests/test_cls.py", "tests/test_mod.py",
             "sub/tests/test_deep.py", "here/test_low.py", "tests/test_new.py",
             "tests/test_old_more.py"]
    # A class's tests, a module skipped whole, a rootdir above the check's
    # folder or below it all count; a longer name sharing a prefix does not.
    assert checks.empty_tests(junit, files) == ("tests/test_new.py", "tests/test_old_more.py")
    assert checks.empty_tests("not xml", files) == ()


# ---- running the check ------------------------------------------------------------


def test_sk14_2_a_placeholder_among_named_files_fails_the_check(folder):
    run = _run(folder, _pytest("tests/test_new.py", "tests/test_old.py"))
    assert run.exit == 0  # what pytest said: the other file's tests passed
    assert not run.ok and run.empty == ("tests/test_new.py",)
    assert run.status == "exit 0, but tests/test_new.py collected no tests"
    assert "tests/test_new.py is named by this check but collected no tests" in run.tail
    assert "evi-junit-" not in run.tail  # eVi's own report path stays out of it
    note = checks.failure_note([run], 1, 2)
    assert "tests/test_new.py collected no tests" in note
    summary = tasks._summary(None, "checks_failed", [run])
    assert summary.startswith("required check failed:") and "tests/test_new.py" in summary


def test_a_file_with_real_tests_passes(folder):
    (folder / "tests" / "test_new.py").write_text(REAL, encoding="utf-8")
    run = _run(folder, _pytest("tests/test_new.py", "tests/test_old.py"))
    assert run.ok and run.empty == () and "collected no tests" not in run.tail


@pytest.mark.parametrize("body", [SKIPPED, MODULE_SKIPPED])
def test_a_file_whose_tests_are_all_skipped_passes(folder, body):
    """A skipped test is still a test, and the junit totals show the skip."""
    (folder / "tests" / "test_new.py").write_text(body, encoding="utf-8")
    run = _run(folder, _pytest("tests/test_new.py", "tests/test_old.py"))
    assert run.ok, run.tail


def test_a_check_naming_a_folder_is_left_as_pytest_says(folder):
    run = _run(folder, _pytest("tests"), scope=("tests/test_new.py", "tests"))
    assert run.check.expect_tests == () and run.ok


def test_a_check_that_isnt_pytest_is_untouched(folder):
    argv = (PY, "-c", "print('fine')", "tests/test_new.py")
    run = _run(folder, argv)
    assert run.check.expect_tests == () and run.ok and run.exit == 0


def test_a_check_without_expected_files_runs_as_before(folder):
    """What the eval runner and an unconfined run use: no junit, pytest's exit."""
    check = checks.Check("python", _pytest("tests/test_new.py", "tests/test_old.py"), "test",
                         "config")
    run = checks.run_check(check, folder, timeout=120)
    assert run.ok and run.empty == ()


# ---- a task run ---------------------------------------------------------------------


def test_a_task_whose_new_test_file_is_a_placeholder_is_not_implemented(
        repo, monkeypatch, fake_llm):  # noqa: F811  the fixture
    check = _pytest("test_new.py", "test_calc.py")
    res = _cli("task", "new", "--title", "Make x two", "--why", "the answer is two",
               "--acceptance", "calc.x == 2, shown by test_new.py", "--scope", "calc.py",
               "--scope", "test_new.py", "--check", shlex.join(check))
    assert res.exit_code == 0, res.output
    client = fake_llm([
        llm_call(("read_file", {"path": "calc.py"})),
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})),
        llm_call(("write_file", {"path": "test_new.py", "content": PLACEHOLDER})),
        llm_text("Done: each criterion is confirmed by the test."),
        llm_text("Still done."),
    ])
    _use_agents(monkeypatch, _agent(client))
    _cli("task", "start", "T1")
    record = _record(repo)
    assert record["status"] == "open"
    assert record["last_result"]["stop_reason"] == "checks_failed"
    # The model was told, in its turn, which file collected nothing.
    told = [m.get("content") or "" for m in client.requests[-1]["messages"]]
    assert any("test_new.py is named by this check but collected no tests" in str(t)
               for t in told)
    # The evidence records the failed check and why.
    report = evidence.read_report(record["evidence"])
    [ran] = report["checks"]
    assert ran["exit"] == 0 and ran["ok"] is False
    assert ran["note"] == "test_new.py collected no tests"
    assert record["evidence_summary"]["checks_failed"] == 1


def test_a_task_whose_new_test_file_holds_tests_is_implemented(
        repo, monkeypatch, fake_llm):  # noqa: F811  the fixture
    check = _pytest("test_new.py", "test_calc.py")
    res = _cli("task", "new", "--title", "Make x two", "--why", "the answer is two",
               "--acceptance", "calc.x == 2, shown by test_new.py", "--scope", "calc.py",
               "--scope", "test_new.py", "--check", shlex.join(check))
    assert res.exit_code == 0, res.output
    _use_agents(monkeypatch, _agent(fake_llm([
        llm_call(("read_file", {"path": "calc.py"})),
        llm_call(("write_file", {"path": "calc.py", "content": "x = 2\n"})),
        llm_call(("write_file", {"path": "test_new.py", "content": REAL})),
        llm_text("Done."),
    ])))
    _cli("task", "start", "T1")
    record = _record(repo)
    assert record["status"] == "implemented", record["last_result"]
    assert record["evidence_summary"]["checks_failed"] == 0
