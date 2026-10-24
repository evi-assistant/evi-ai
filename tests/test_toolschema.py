import re

from evi.toolschema import safe_name, wire_names, normalize_schema, MAX_DEPTH

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


def test_normalize_schema_refs():
    # AC1: $ref resolved inline, $defs and $ref removed
    schema = {
        '$defs': {
            'Addr': {'type': 'object', 'properties': {'city': {'type': 'string'}}, 'required': ['city']}
        },
        'type': 'object',
        'properties': {
            'home': {'$ref': '#/$defs/Addr'},
            'work': {'anyOf': [{'$ref': '#/$defs/Addr'}, {'type': 'null'}]}
        }
    }
    res = normalize_schema(schema)
    addr = {'type': 'object', 'properties': {'city': {'type': 'string'}}, 'required': ['city']}
    assert res['properties']['home'] == addr
    assert res['properties']['work'] == addr
    assert '$defs' not in res
    assert not any('$ref' in str(v) for v in res.values())


def test_normalize_schema_nulls():
    # AC2: "X or null" becomes plain X; type list with null simplified
    # anyOf with null branch
    s1 = {'anyOf': [{'type': 'string', 'default': 'hi'}, {'type': 'null'}]}
    assert normalize_schema(s1) == {'type': 'string', 'default': 'hi'}
    # type list with null
    s2 = {'type': ['string', 'null']}
    assert normalize_schema(s2) == {'type': 'string'}
    # anyOf with multiple non-null branches
    s3 = {'anyOf': [{'type': 'string'}, {'type': 'integer'}, {'type': 'null'}]}
    assert normalize_schema(s3) == {'anyOf': [{'type': 'string'}, {'type': 'integer'}]}


def test_normalize_schema_allof():
    # AC3: allOf merged into one schema
    s = {
        'allOf': [
            {'properties': {'a': {'type': 'string'}}, 'required': ['a']},
            {'properties': {'b': {'type': 'integer'}}}
        ]
    }
    res = normalize_schema(s)
    assert 'allOf' not in res
    assert res['properties'] == {'a': {'type': 'string'}, 'b': {'type': 'integer'}}
    assert res['required'] == ['a']


def test_normalize_schema_limits_and_drops():
    # AC4: recursive/unresolvable refs become {}, depth limit, dropped keywords
    # Recursive ref
    s_rec = {'properties': {'node': {'$ref': '#/'}}}
    assert normalize_schema(s_rec)['properties']['node'] == {}
    # Unresolvable ref
    s_un = {'properties': {'node': {'$ref': '#/nope'}}}
    assert normalize_schema(s_un)['properties']['node'] == {}
    # Depth limit
    s_deep = {}
    curr = s_deep
    for _ in range(12):
        curr['p'] = {}
        curr = curr['p']
    res_deep = normalize_schema(s_deep)
    # Check depth 9 is {}
    node = res_deep
    for _ in range(MAX_DEPTH + 4):
        node = node.get('p', {})
    assert node == {}
    # Dropped keywords
    s_drop = {'$schema': 'v7', '$id': 'id', 'examples': [1], 'type': 'string'}
    assert normalize_schema(s_drop) == {'type': 'string'}


def test_normalize_schema_identity_and_edge():
    # AC5: identity for simple schemas, input unchanged, None handling
    s = {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}
    s_copy = s.copy()
    assert normalize_schema(s) == s
    assert s == s_copy
    assert normalize_schema(None) == {'type': 'object', 'properties': {}}
