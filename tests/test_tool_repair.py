import json

import evi.tools.fs  # noqa: F401  registers read_file and write_file
import evi.tools.shell  # noqa: F401  registers run_command
from evi.tools.base import REGISTRY
from evi.tools.repair import close_truncated, fit_arguments, loads_tolerant, parse_arguments


def test_truncated_repair():
    # AC1: Truncated object repair
    assert parse_arguments('{"path": "a.py"', allow_truncated=True) == (
        {'path': 'a.py'}, ['closed arguments that were cut off']
    )
    assert parse_arguments('{"path": "a.py"', allow_truncated=False) == (None, [])


def test_strict_json():
    # AC2: Strict JSON and empty string
    assert parse_arguments('{"path": "a.py"}', allow_truncated=False) == ({'path': 'a.py'}, [])
    assert parse_arguments('', allow_truncated=False) == ({}, [])


def test_tolerant_json():
    # AC3: Python-style literals
    assert parse_arguments("{'path': 'a.py', 'all': true}", allow_truncated=False) == (
        {'path': 'a.py', 'all': True}, ['read arguments that were not strict JSON']
    )


def test_close_truncated():
    # AC4: Closing logic
    assert close_truncated('{"path": "a.p') == '{"path": "a.p"}'
    assert close_truncated('{"a": [1, 2') == '{"a": [1, 2]}'
    assert close_truncated('{"path": "a.py",') == '{"path": "a.py"}'
    assert close_truncated('{"path": "x\\') == '{"path": "x"}'
    assert close_truncated('{"path": "a.py"}') is None


def test_invalid_arguments():
    # AC5: Non-objects or unrepairable
    assert parse_arguments('[1, 2]', allow_truncated=True) == (None, [])
    assert parse_arguments('"x"', allow_truncated=True) == (None, [])
    assert parse_arguments('{"path"', allow_truncated=True) == (None, [])
    assert parse_arguments('{"path":', allow_truncated=True) == (None, [])
    assert parse_arguments('not json', allow_truncated=True) == (None, [])
    assert parse_arguments("{'a': {1, 2}}", allow_truncated=True) == (None, [])


def test_json_words_inside_strings_are_left_alone():
    # Only bare true/false/null are respelled; a value that says them keeps its text.
    assert loads_tolerant("{'a': 'it is true', 'b': false}") == {"a": "it is true", "b": False}
    assert loads_tolerant("{'a': \"null or 'x'\", 'b': null, 'c': 'don\\'t true'}") == {
        "a": "null or 'x'", "b": None, "c": "don't true"}


def test_tool_prepare_coercion():
    # AC1: Coerce offset string to int
    res_args, notes, err = REGISTRY['read_file'].prepare('{"path": "a.py", "offset": "5"}')
    assert json.loads(res_args) == {'path': 'a.py', 'offset': 5}
    assert notes == ['offset: "5" -> 5']
    assert err is None


def test_tool_prepare_aliases():
    # AC2: Aliases
    # read_file: file_path -> path
    res_args, notes, err = REGISTRY['read_file'].prepare('{"file_path": "a.py"}')
    assert json.loads(res_args) == {'path': 'a.py'}
    assert 'file_path -> path' in notes

    # run_command: cmd -> command
    res_args, notes, err = REGISTRY['run_command'].prepare('{"cmd": "ls"}')
    assert json.loads(res_args) == {'command': 'ls'}
    assert 'cmd -> command' in notes

    # When path is given too, file_path is dropped
    res_args, notes, err = REGISTRY['read_file'].prepare('{"path": "a.py", "file_path": "b.py"}')
    assert json.loads(res_args) == {'path': 'a.py'}
    assert 'dropped unknown argument file_path' in notes


def test_tool_prepare_unknown_args():
    # AC3: Unknown arguments and ctx dropped
    res_args, notes, err = REGISTRY['read_file'].prepare('{"path": "a.py", "junk": 1, "ctx": {}}')
    assert json.loads(res_args) == {'path': 'a.py'}
    assert 'dropped unknown argument junk' in notes
    assert 'dropped unknown argument ctx' in notes


def test_tool_prepare_no_repair():
    # AC4: No repair returns same string
    raw = '{"path": "a.py"}'
    res_args, notes, err = REGISTRY['read_file'].prepare(raw)
    assert res_args is raw
    assert notes == []
    assert err is None


def test_tool_prepare_errors():
    # AC5: Missing required and invalid JSON
    # write_file needs content
    res_args, notes, err = REGISTRY['write_file'].prepare('{"path": "a"}')
    assert res_args == '{"path": "a"}'
    assert 'ERROR: write_file needs content' in err
    assert 'Call it as write_file(path: string, content: string)' in err

    # Invalid JSON object
    res_args, notes, err = REGISTRY['read_file'].prepare('[1]')
    assert res_args == '[1]'
    assert 'ERROR: the arguments to read_file are not a JSON object' in err
    assert 'Call it as read_file(path: string, offset?: integer, limit?: integer)' in err


def test_tool_prepare_truncation():
    # AC6: Truncation for read-only vs write
    # read_file is read-only
    res_args, notes, err = REGISTRY['read_file'].prepare('{"path": "a.py"')
    assert json.loads(res_args) == {'path': 'a.py'}
    assert err is None

    # write_file is not read-only
    res_args, notes, err = REGISTRY['write_file'].prepare('{"path": "a.py", "content": "half')
    assert res_args == '{"path": "a.py", "content": "half'
    assert 'ERROR: the arguments to write_file are not a JSON object' in err


def test_fit_arguments_coercion():
    # AC7: fit_arguments detailed coercion
    def f(flag: bool = False, n: float = 0, k: int = 0, items: list | None = None,
          opts: dict | None = None, name: str = '', **kw):
        pass

    schema = {
        "type": "object",
        "properties": {
            "flag": {"type": "boolean"},
            "n": {"type": "number"},
            "k": {"type": "integer"},
            "items": {"type": "array"},
            "opts": {"type": "object"},
            "name": {"type": "string"},
        }
    }

    # Booleans
    assert fit_arguments({"flag": "true"}, schema, f)[0]["flag"] is True
    assert fit_arguments({"flag": "No"}, schema, f)[0]["flag"] is False

    # Numbers
    assert fit_arguments({"n": "1.5"}, schema, f)[0]["n"] == 1.5
    assert fit_arguments({"n": "2"}, schema, f)[0]["n"] == 2
    assert fit_arguments({"k": 3.0}, schema, f)[0]["k"] == 3

    # Strings
    assert fit_arguments({"name": 7}, schema, f)[0]["name"] == "7"

    # Arrays
    assert fit_arguments({"items": '["a"]'}, schema, f)[0]["items"] == ["a"]
    assert fit_arguments({"items": "a"}, schema, f)[0]["items"] == ["a"]
    assert fit_arguments({"items": 5}, schema, f)[0]["items"] == [5]

    # Objects
    assert fit_arguments({"opts": '{"k": 1}'}, schema, f)[0]["opts"] == {"k": 1}

    # Unchanged
    assert fit_arguments({"flag": "maybe"}, schema, f)[0]["flag"] == "maybe"
    assert fit_arguments({"n": "nan"}, schema, f)[0]["n"] == "nan"
    assert fit_arguments({"n": "inf"}, schema, f)[0]["n"] == "inf"
    assert fit_arguments({"k": "x"}, schema, f)[0]["k"] == "x"
    assert fit_arguments({"name": True}, schema, f)[0]["name"] is True

    # No drop for **kwargs
    def f_kwargs(**kw):
        pass

    res, notes = fit_arguments({"junk": 1}, schema, f_kwargs)
    assert "junk" in res
    assert "dropped unknown argument junk" not in notes


def test_a_raw_newline_or_tab_inside_a_string_is_read():
    # Small models often write a file's newlines raw inside a JSON string.
    raw = '{"path": "a.py", "content": "x = 1\ny = 2\t# z"}'  # a real newline and tab
    assert parse_arguments(raw, allow_truncated=False) == (
        {"path": "a.py", "content": "x = 1\ny = 2\t# z"},
        ["read arguments that were not strict JSON"])
