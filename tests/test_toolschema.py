import re

from evi.toolschema import safe_name, wire_names

SAFE_PATTERN = re.compile(r'[a-zA-Z0-9_-]{1,64}')


def test_safe_name_basic():
    # AC1: safe name kept, MCP tool dot becomes double underscore
    assert safe_name('read_file') == 'read_file'
    assert safe_name('github.search') == 'github__search'


def test_safe_name_unsafe_chars():
    # AC2: spaces and special chars become underscores
    assert safe_name('my server.do thing!') == 'my_server__do_thing_'
    assert safe_name('über.tool') == '_ber__tool'


def test_safe_name_length_and_empty():
    # AC3: over 64 chars are hashed and cut to 64; empty name is hashed
    long_name = 'x' * 70
    res1 = safe_name(long_name)
    assert len(res1) == 64
    assert '_' in res1 and len(res1.split('_')[-1]) == 8 # ends with _ + 8 hex
    assert SAFE_PATTERN.fullmatch(res1)

    long_mcp = 's.' + 'y' * 80
    res2 = safe_name(long_mcp)
    assert len(res2) == 64
    assert SAFE_PATTERN.fullmatch(res2)

    res_empty = safe_name('')
    assert SAFE_PATTERN.fullmatch(res_empty)


def test_safe_name_consistency():
    # AC4: results match pattern and are deterministic
    names = ['read_file', 'github.search', 'über.tool', 'x' * 100, '']
    for n in names:
        res = safe_name(n)
        assert SAFE_PATTERN.fullmatch(res), f"Failed for {n}"
        assert res == safe_name(n), f"Non-deterministic for {n}"


def test_wire_names_collisions():
    # AC5: distinct names, stable map regardless of order
    input_names = ['a.b', 'a__b', 'read_file', 'github.search']
    res1 = wire_names(input_names)
    res2 = wire_names(input_names[::-1])

    assert res1 == res2
    assert len(set(res1.values())) == 4
    assert res1['a__b'] == 'a__b'
    assert res1['read_file'] == 'read_file'
    assert res1['github.search'] == 'github__search'
    # 'a.b' should be hashed because 'a__b' is already safe and takes priority
    assert res1['a.b'] != 'a__b'
    assert SAFE_PATTERN.fullmatch(res1['a.b'])


def test_wire_names_long_collisions():
    # AC6: different long names with same prefix get different wire names
    prefix = 'a' * 55
    name1 = prefix + '1' * 20
    name2 = prefix + '2' * 20

    res = wire_names([name1, name2])
    assert res[name1] != res[name2]
    assert len(res[name1]) == 64
    assert len(res[name2]) == 64
