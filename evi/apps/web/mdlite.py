"""Minimal, dependency-free Markdown → HTML for the in-app docs viewer.

Not a full CommonMark implementation — just the subset our ``docs/`` use
(headings, fenced + inline code, bold/italic, links, ordered/unordered
nested lists, blockquotes, GFM tables, horizontal rules, paragraphs). The
point is offline rendering inside the desktop app with no extra runtime
dependency: the chat bubbles render Markdown client-side via a CDN copy of
``marked``, which is unavailable when the machine is offline — docs must not
depend on that.

All text is HTML-escaped; only the structural markup this module emits is
trusted. See tests/test_mdlite.py.
"""

from __future__ import annotations

import re

_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))


def _esc(text: str) -> str:
    for a, b in _ESCAPES:
        text = text.replace(a, b)
    return text


_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
# Single * or _ for emphasis, but not when glued to word chars (so `base_url`
# and `max_tokens` survive) and not a leftover bold marker.
_ITALIC_RE = re.compile(r"(?<![\*\w])[*_]([^*_\n]+)[*_](?![\*\w])")
_CODE_RE = re.compile(r"`([^`]+)`")
_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^(\*{3,}|-{3,}|_{3,})$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _inline(text: str) -> str:
    """Render inline spans. Inline code is stashed first so its contents
    don't get mangled by the link/emphasis passes, then restored last."""
    spans: list[str] = []

    def _stash(m: "re.Match[str]") -> str:
        spans.append("<code>" + _esc(m.group(1)) + "</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE_RE.sub(_stash, text)
    text = _esc(text)

    def _link(m: "re.Match[str]") -> str:
        label, url = m.group(1), m.group(2)
        url = url.replace('"', "%22")
        return f'<a href="{url}" target="_blank" rel="noopener">{label}</a>'

    text = _LINK_RE.sub(_link, text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)
    return text


def _is_block_start(line: str, lines: list[str], i: int, n: int) -> bool:
    s = line.strip()
    if not s:
        return True
    if s.startswith("```") or s.startswith(">"):
        return True
    if _HEADING_RE.match(s) or _HR_RE.match(s) or _ITEM_RE.match(line):
        return True
    if (
        "|" in line
        and i + 1 < n
        and "-" in lines[i + 1]
        and _TABLE_SEP_RE.match(lines[i + 1])
    ):
        return True
    return False


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _parse_table(lines: list[str], i: int, n: int) -> tuple[str, int]:
    header = _split_row(lines[i])
    i += 2  # header + separator
    body: list[list[str]] = []
    while i < n and lines[i].strip() and "|" in lines[i]:
        body.append(_split_row(lines[i]))
        i += 1
    out = ["<table><thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in header]
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out), i


def _parse_list(lines: list[str], i: int, n: int) -> tuple[str, int]:
    first = lines[i]
    indent = len(first) - len(first.lstrip())
    ordered = bool(re.match(r"^\s*\d+\.\s+", first))
    tag = "ol" if ordered else "ul"
    items: list[str] = []
    while i < n:
        line = lines[i]
        if not line.strip():
            # Continue across a blank line only if another item at this indent
            # follows; otherwise the list is done.
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if (
                j < n
                and (len(lines[j]) - len(lines[j].lstrip())) >= indent
                and _ITEM_RE.match(lines[j])
            ):
                i = j
                continue
            break
        cur = len(line) - len(line.lstrip())
        if cur < indent:
            break
        m = _ITEM_RE.match(line)
        if cur == indent and m:
            items.append("<li>" + _inline(m.group(3)) + "</li>")
            i += 1
        elif cur > indent:
            if m:
                nested, i = _parse_list(lines, i, n)
                if items:
                    items[-1] = items[-1][:-5] + nested + "</li>"
                else:
                    items.append("<li>" + nested + "</li>")
            else:
                # Indented non-item line = continuation of the current item.
                if items:
                    items[-1] = items[-1][:-5] + " " + _inline(line.strip()) + "</li>"
                i += 1
        else:
            break
    return f"<{tag}>" + "".join(items) + f"</{tag}>", i


def render(md: str) -> str:
    """Render a Markdown document to an HTML fragment."""
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    html: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            cls = f' class="language-{_esc(lang)}"' if lang else ""
            html.append(f"<pre><code{cls}>" + _esc("\n".join(buf)) + "</code></pre>")
            continue

        m = _HEADING_RE.match(stripped)
        if m:
            level = len(m.group(1))
            html.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            i += 1
            continue

        if _HR_RE.match(stripped):
            html.append("<hr>")
            i += 1
            continue

        if (
            "|" in line
            and i + 1 < n
            and "-" in lines[i + 1]
            and _TABLE_SEP_RE.match(lines[i + 1])
        ):
            tbl, i = _parse_table(lines, i, n)
            html.append(tbl)
            continue

        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            html.append("<blockquote>" + render("\n".join(buf)) + "</blockquote>")
            continue

        if _ITEM_RE.match(line):
            lst, i = _parse_list(lines, i, n)
            html.append(lst)
            continue

        buf = []
        while i < n and lines[i].strip() and not _is_block_start(lines[i], lines, i, n):
            buf.append(lines[i].strip())
            i += 1
        html.append("<p>" + _inline(" ".join(buf)) + "</p>")
    return "\n".join(html)
