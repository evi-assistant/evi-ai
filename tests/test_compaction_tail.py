"""tail_within: the compaction cut moved forward until the verbatim tail fits a token budget."""

from evi.history import tail_within


# U(n), A(n), T(n): a user, assistant or tool message of n tokens by size().
def U(n): return {"role": "user", "content": "u" * (4 * n)}
def A(n): return {"role": "assistant", "content": "a" * (4 * n)}
def T(n): return {"role": "tool", "content": "t" * (4 * n)}
def S(): return {"role": "system", "content": ""}


def size(m):
    return len(m.get("content") or "") // 4


def test_tail_within():
    # the tail from index 3 is 545 tokens
    h = [S(), U(10), A(10), U(10), A(5), T(500), A(10), U(10), A(10)]
    assert tail_within(h, 3, 1000, size) == 3

    # past the 500-token tool result, to the next assistant message
    assert tail_within(h, 3, 100, size) == 6

    # 7's tail (20) fits 25; nothing fits 5, so the newest step stays verbatim
    assert tail_within(h, 3, 25, size) == 7
    assert tail_within(h, 3, 5, size) == 8

    # no budget: the cut as it was
    assert tail_within(h, 3, 0, size) == 3
    assert tail_within(h, 3, -10, size) == 3

    # never a tool message, even when nothing fits
    h2 = [S(), U(10), A(1), T(500)]
    assert tail_within(h2, 1, 50, size) == 2
