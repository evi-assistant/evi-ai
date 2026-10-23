import time
from evi.ignore import parse_rules, IgnoreRules

def test_redos_catastrophic_backtracking():
    # AC 1: '*a' * 9 + '*b' on 'a' * 40
    pattern = '*a' * 9 + '*b'
    rules = IgnoreRules(parse_rules(pattern))

    start = time.perf_counter()
    res = rules.ignored('a' * 40, False)
    duration = time.perf_counter() - start

    assert res is False
    assert duration < 0.1, f"Too slow: {duration:.4f}s"

    # Should still match if it ends in 'b'
    assert rules.ignored('a' * 40 + 'b', False) is True

def test_redos_large_scale():
    # AC 2: '*a' * 30 + '*b' on 'a' * 200
    pattern = '*a' * 30 + '*b'
    rules = IgnoreRules(parse_rules(pattern))

    start = time.perf_counter()
    res = rules.ignored('a' * 200, False)
    duration = time.perf_counter() - start

    assert res is False
    assert duration < 0.5, f"Too slow: {duration:.4f}s"

def test_ordinary_patterns():
    # AC 3: Ordinary patterns still behave as before
    cases = [
        ('*.log', 'test.log', True),
        ('*.log', 'test.txt', False),
        ('a*b*c', 'axbyc', True),
        ('a*b*c', 'axbyd', False),
        ('*.py[cod]', 'main.pyc', True),
        ('*.py[cod]', 'main.py', False),
        ('docs/*.md', 'docs/readme.md', True),
        ('docs/*.md', 'docs/sub/readme.md', False),
        ('**/tmp', 'tmp', True),
        ('**/tmp', 'src/tmp', True),
        ('**/tmp', 'src/tmp/file', True), # tmp is a folder here, but the rule is **/tmp
        ('logs/**', 'logs/a.txt', True),
        ('logs/**', 'src/logs/a.txt', False),  # anchored: it has a '/'
        ('logs/**', 'src/a.txt', False),
    ]

    for pattern, path, expected in cases:
        rules = IgnoreRules(parse_rules(pattern))
        # For '**/tmp', we test as dir if it's 'tmp'
        is_dir = True if path.endswith('tmp') else False
        res = rules.ignored(path, is_dir)
        assert res == expected, f"Pattern '{pattern}' on '{path}' expected {expected}, got {res}"


def test_a_middle_double_star_keeps_its_slash():
    rules = IgnoreRules(parse_rules("a/**/b"))
    assert rules.ignored("a/b", False) is True
    assert rules.ignored("a/x/y/b", False) is True
    assert rules.ignored("ab", False) is False
    assert rules.ignored("axb", False) is False
