"""Tests for the offline Markdown renderer behind Help → Documentation.

`evi/apps/web/mdlite.py` renders `docs/*.md` inside the app (and, via
`scripts/build-site-docs.py`, on evi-ai.dev), so a rendering bug is user-visible
in both places. It had no tests until a wrapped **bold** span in docs/roadmap.md
came out bolding the wrong words.
"""

from __future__ import annotations

from evi.apps.web.mdlite import render


# --- the bug this file was created for -------------------------------------


def test_bold_wrapped_across_a_list_item_renders():
    # Editors wrap long bullets; the closing ** lands on the next line.
    md = "- **Native menus** (File/Edit/View) and **system\ntray** with tray icons."
    html = render(md)
    assert "<strong>system tray</strong>" in html
    assert "**" not in html, f"unrendered delimiters survived: {html}"


def test_lazy_continuation_stays_in_the_item():
    # A continuation wrapped to column 0 used to END the list, stranding the
    # rest of the sentence in its own paragraph.
    md = "- first bullet that wraps\nonto the next line\n- second bullet"
    html = render(md)
    assert html.count("<li>") == 2, html
    assert "<p>" not in html, f"continuation escaped the list: {html}"
    assert "first bullet that wraps onto the next line" in html


def test_wrapped_delimiters_do_not_bold_the_wrong_words():
    # The actual regression from docs/roadmap.md:104-105. With the opening **
    # stranded on the previous line, the remaining pairs matched across the
    # wrong spans and produced "tray<strong> with </strong>minimize-to-tray".
    md = (
        "- **Native menus** (File/Edit/View/Help + accelerators) and **system\n"
        "tray** with **minimize-to-tray**; **force-update** via Help.\n"
    )
    html = render(md)
    assert "<strong>system tray</strong>" in html
    assert "<strong>minimize-to-tray</strong>" in html
    assert "<strong>force-update</strong>" in html
    assert "<strong> with </strong>" not in html


def test_code_span_wrapped_across_lines():
    md = "- run `evi\nchat` to start"
    assert "<code>evi chat</code>" in render(md)


def test_link_wrapped_across_lines():
    md = "- see [the\ndocs](https://evi-ai.dev) for more"
    html = render(md)
    assert 'href="https://evi-ai.dev"' in html
    assert "the docs" in html


# --- behaviour that must NOT regress ---------------------------------------


def test_nested_list_still_nests():
    md = "- outer\n  - inner one\n  - inner two\n- outer two"
    html = render(md)
    # Assert the exact shape: counting tags alone passed even on a variant that
    # ejected "outer" from the <li> its sublist hangs off and re-emitted it
    # afterwards, which is why this pins the whole string.
    assert html == (
        "<ul><li>outer<ul><li>inner one</li><li>inner two</li></ul></li>"
        "<li>outer two</li></ul>"
    ), html


def test_list_ends_at_a_table():
    # _is_block_start's table branch became load-bearing for lists in this
    # change (it now gates continuation, not just paragraphs) but nothing
    # covered it.
    html = render("- item\n| a | b |\n| --- | --- |\n| 1 | 2 |")
    assert "<table>" in html
    assert "<li>item</li>" in html
    assert "<table>" not in html.split("</ul>")[0]


def test_list_ends_at_a_horizontal_rule():
    html = render("- item\n---")
    assert "<hr>" in html
    assert html.count("<li>") == 1
    assert "<hr>" not in html.split("</ul>")[0]


def test_indented_continuation_still_joins():
    md = "- item text\n  indented continuation"
    html = render(md)
    assert "item text indented continuation" in html
    assert html.count("<li>") == 1


def test_blank_line_between_items_keeps_one_list():
    md = "- one\n\n- two"
    html = render(md)
    assert html.count("<ul>") == 1
    assert html.count("<li>") == 2


def test_ordered_list_uses_ol():
    html = render("1. first\n2. second")
    assert html.startswith("<ol>") and "</ol>" in html
    assert html.count("<li>") == 2


def test_list_ends_at_a_heading():
    # A real block start must still terminate the list rather than be swallowed
    # as a lazy continuation.
    html = render("- item\n# Heading")
    assert "<h1>Heading</h1>" in html
    assert "<li>item</li>" in html
    assert "Heading" not in html.split("</ul>")[0]


def test_list_ends_at_a_fence():
    html = render("- item\n```\ncode\n```")
    assert "<pre><code>" in html
    assert "code" not in html.split("</ul>")[0]


def test_list_ends_at_a_blockquote():
    html = render("- item\n> quoted")
    assert "<blockquote>" in html
    assert "quoted" not in html.split("</ul>")[0]


def test_list_ends_at_a_blank_line_then_paragraph():
    html = render("- item\n\nA new paragraph.")
    assert "<p>A new paragraph.</p>" in html
    assert html.count("<li>") == 1


# --- general sanity ---------------------------------------------------------


def test_paragraph_joins_wrapped_lines():
    # Paragraphs already did this; pin it so the list fix isn't mistaken for
    # the whole story if someone revisits the renderer.
    html = render("A sentence with **bold\ntext** wrapped.")
    assert "<strong>bold text</strong>" in html


def test_headings_and_hr():
    html = render("## Title\n\n---\n\nbody")
    assert "<h2>Title</h2>" in html
    assert "<hr>" in html


def test_html_is_escaped():
    assert "&lt;script&gt;" in render("a <script> tag")


def test_fenced_code_is_not_inlined():
    html = render("```\n**not bold** and `not code`\n```")
    assert "<strong>" not in html
    assert "**not bold**" in html
