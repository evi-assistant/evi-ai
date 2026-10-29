"""pack_blocks: a rendered transcript in chunks a summariser can take (evi.history)."""

from evi.history import pack_blocks


def test_pack_blocks():
    # AC1: Basic chunking
    assert pack_blocks(['aaa', 'bbb', 'ccc'], 8) == ['aaa\n\nbbb', 'ccc']

    # AC2: Blocks longer than limit are split, none dropped
    # Single long block
    assert pack_blocks(['x' * 10], 4) == ['xxxx', 'xxxx', 'xx']
    # Mixed blocks with a long one in the middle
    assert pack_blocks(['ab', 'x' * 10, 'cd'], 4) == ['ab', 'xxxx', 'xxxx', 'xx', 'cd']

    # AC3: Empty input and limit <= 0
    assert pack_blocks([], 10) == []
    assert pack_blocks(['a', 'b'], 0) == ['a\n\nb']
    assert pack_blocks(['a', 'b'], -1) == ['a\n\nb']

    # AC4: Many blocks, no splitting, check integrity
    blocks = ['x' * 21] * 50
    limit = 120
    chunks = pack_blocks(blocks, limit)
    for chunk in chunks:
        assert len(chunk) <= limit
    assert '\n\n'.join(chunks) == '\n\n'.join(blocks)

    # AC5: Custom separator
    assert pack_blocks(['a', 'b'], 10, sep=' | ') == ['a | b']

    # Edge case: block exactly the limit
    assert pack_blocks(['aaaa'], 4) == ['aaaa']
    # Edge case: block slightly over limit
    assert pack_blocks(['aaaaa'], 4) == ['aaaa', 'a']
    # Edge case: block exactly limit + sep
    # 'aaa' (3) + '\n\n' (2) + 'aaa' (3) = 8. Limit 8.
    assert pack_blocks(['aaa', 'aaa'], 8) == ['aaa\n\naaa']
    # 'aaa' (3) + '\n\n' (2) + 'aaaa' (4) = 9. Limit 8.
    assert pack_blocks(['aaa', 'aaaa'], 8) == ['aaa', 'aaaa']
