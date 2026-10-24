import os
import re
import time

from evi.tool_output import cap_chars, clip, spill, prune
from evi.redact import refresh


def test_cap_chars():
    # AC1: cap_chars(0) == 16000; cap_chars(1000) == 2000 (the floor);
    # cap_chars(8192) == 4915; cap_chars(32768) == 19660;
    # cap_chars(1_000_000) == 60000 (the ceiling).
    assert cap_chars(0) == 16000
    assert cap_chars(1000) == 2000
    assert cap_chars(8192) == 4915
    assert cap_chars(32768) == 19660
    assert cap_chars(1_000_000) == 60000


def test_clip_tail_biased():
    # AC2: A 2.4 MB text of 40 000 numbered lines clipped with cap_chars(8192)
    # and tail_biased=True comes out at most 4915 characters, starts with
    # 'line 0:', ends with the last line ('line 39999: ...\n'), and has one
    # line '[... N characters cut. <note> ...]' with N the exact count cut
    # (formatted with thousands separators) and the note given.
    lines = [f"line {i}: some text\n" for i in range(40000)]
    text = "".join(lines)
    cap = cap_chars(8192)
    note = "see spill file"
    result = clip(text, cap, note, tail_biased=True)

    assert len(result) <= cap
    assert result.startswith("line 0:")
    assert result.endswith("line 39999: some text\n")

    # Verify the cut line and the count
    match = re.search(r"\[\.\.\. (\d{1,3}(?:,\d{3})*) characters cut\. " + re.escape(note) + r" \.\.\.\]", result)
    assert match is not None

    # Verify N is exact
    n_str = match.group(1).replace(",", "")
    n_val = int(n_str)
    # N is what lies between the kept head and tail.
    start, end = match.span()
    head = result[:start-1] # -1 for the \n
    tail = result[end+1:] # +1 for the \n
    assert n_val == len(text) - len(head) - len(tail)


def test_clip_head_biased_and_unchanged():
    # AC3: Without tail_biased the head is the larger part (about 3/4 of what's kept);
    # text within the cap comes back unchanged.
    text = "A" * 10000
    cap = 5000
    note = "note"
    result = clip(text, cap, note, tail_biased=False)

    # Find the cut line to separate head and tail
    match = re.search(r"\[\.\.\. .*? \.\.\.\]", result)
    start, end = match.span()
    head = result[:start-1]
    tail = result[end+1:]

    # About 3/4 of what is kept is head.
    assert len(head) > len(tail)
    assert len(head) > 3000

    # Text within cap comes back unchanged
    short_text = "Short"
    assert clip(short_text, cap, note) == short_text


def test_spill(tmp_path, monkeypatch):
    # AC4: spill(tmp_path, 'sess:1/x', 'call/0?', text) writes
    # tmp_path/tool-output/sess_1_x/call_0_.txt and returns that path;
    # a secret value from the environment in the text is written as [REDACTED:...] instead.
    secret_val = "Zq8vN2kLp4Xr7Tw1Ys5M-probe"
    monkeypatch.setenv("ACME_API_KEY", secret_val)
    refresh()
    try:
        text = f"My key is {secret_val} and some other text."
        path = spill(tmp_path, "sess:1/x", "call/0?", text)

        expected_path = tmp_path / "tool-output" / "sess_1_x" / "call_0_.txt"
        assert path == expected_path
        assert path.exists()

        content = path.read_text(encoding="utf-8")
        assert secret_val not in content
        assert "[REDACTED:ACME_API_KEY]" in content
    finally:
        # Clean up env to avoid leaking into other tests
        monkeypatch.delenv("ACME_API_KEY", raising=False)
        refresh()


def test_prune(tmp_path):
    # AC5: prune(tmp_path, 14) deletes a session folder whose newest file is 30 days old
    # and keeps one written now, returning 1; prune(tmp_path, 0) deletes nothing and returns 0;
    # prune on a home with no tool-output folder returns 0.

    # No folder
    assert prune(tmp_path, 14) == 0

    # Setup folders
    old_sess = tmp_path / "tool-output" / "old_sess"
    old_sess.mkdir(parents=True)
    old_file = old_sess / "call.txt"
    old_file.write_text("old")

    new_sess = tmp_path / "tool-output" / "new_sess"
    new_sess.mkdir(parents=True)
    new_file = new_sess / "call.txt"
    new_file.write_text("new")

    # Age the old one (30 days = 30 * 86400 seconds)
    past = time.time() - (30 * 86400)
    os.utime(old_file, (past, past))
    os.utime(old_sess, (past, past))

    assert prune(tmp_path, 14) == 1
    assert not old_sess.exists()
    assert new_sess.exists()

    # prune(tmp_path, 0) deletes nothing
    # Create another old one
    old_sess2 = tmp_path / "tool-output" / "old_sess2"
    old_sess2.mkdir(parents=True)
    old_file2 = old_sess2 / "call.txt"
    old_file2.write_text("old")
    os.utime(old_file2, (past, past))
    os.utime(old_sess2, (past, past))

    assert prune(tmp_path, 0) == 0
    assert old_sess2.exists()
