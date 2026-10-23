from evi.tools.repair import close_truncated, parse_arguments


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
