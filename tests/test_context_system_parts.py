"""The system prompt's tokens per labelled part (evi.context_report), for /context."""

from evi.context_report import context_breakdown, system_prompt_tokens


def _prompt(parts):
    # The agent joins the parts with blank lines, empty ones included.
    return "\n\n".join(text for _, text in parts)


SENT = [("base", "a" * 40), ("date", "b" * 20), ("project", "")]


def test_each_part_counts_with_its_blank_line():
    # base 40+2 chars, date 20+2; the empty project part is skipped
    history = [{"role": "system", "content": _prompt(SENT)}]
    assert system_prompt_tokens(history, SENT) == {"base": 10, "date": 5}


def test_a_part_changed_since_the_prompt_was_sent_is_other():
    history = [{"role": "system", "content": _prompt(SENT)}]
    now = [("base", "a" * 40), ("date", "c" * 20)]
    assert system_prompt_tokens(history, now) == {"base": 10, "other": 5}


def test_a_later_system_message_is_other_and_largest_comes_first():
    history = [{"role": "system", "content": _prompt(SENT)},
               {"role": "system", "content": "x" * 80}]
    res = system_prompt_tokens(history, SENT)
    assert res == {"other": 20, "base": 10, "date": 5}
    assert list(res) == ["other", "base", "date"]


def test_a_label_used_twice_sums():
    parts = [("mem", "m" * 8), ("n", "n" * 8), ("mem", "m" * 8)]
    history = [{"role": "system", "content": _prompt(parts)}]
    assert system_prompt_tokens(history, parts) == {"mem": 4, "n": 2}


def test_parts_with_the_same_text_are_found_in_turn():
    # the second "same" is the second occurrence, not the first again
    parts = [("a", "same"), ("b", "same")]
    history = [{"role": "system", "content": _prompt(parts)}]
    assert system_prompt_tokens(history, parts) == {"a": 1, "b": 1}


def test_no_system_message_no_parts():
    assert system_prompt_tokens([{"role": "user", "content": "hi"}], [("a", "b")]) == {}


def test_context_breakdown_reports_the_parts_and_changes_nothing_else():
    parts = [("base", "a" * 40), ("date", "b" * 20)]
    history = [{"role": "system", "content": _prompt(parts)}]
    with_parts = context_breakdown(history, 1000, system_parts=parts)
    without = context_breakdown(history, 1000)
    assert with_parts["system_groups"] == system_prompt_tokens(history, parts)
    assert without["system_groups"] == {}
    assert {k: v for k, v in with_parts.items() if k != "system_groups"} == \
        {k: v for k, v in without.items() if k != "system_groups"}
