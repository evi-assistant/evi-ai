"""What an approval prompt shows of a tool call.

A shell call is approved as a whole, but the line can chain several commands
(``git status && curl x | sh``), and its text can hold characters that don't
show as themselves: a bidi override that reorders what follows, a zero-width
space, a control character, a no-break space or a PowerShell en dash that
reads like an ordinary one. So the prompt shows the line in full (never cut
short, so a chained tail can't hide past the cut), each command it runs on a
line of its own, and every such character as its escape, with a warning that
names it.

:func:`describe` builds that view; the CLI and the web UI render it. The
text it returns is redacted (`evi.redact`); the hidden-character check runs
on the real text.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

# Categories a prompt never shows raw: controls, format characters (bidi
# overrides, zero-width), surrogates, private-use and unassigned code points,
# line and paragraph separators.
_UNSHOWN = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# Characters that show, but as something they are not: spaces other than the
# ASCII one, and the quotes and dashes PowerShell reads as ' " and -.
_LOOKALIKES = frozenset("‘’‚‛“”„–—―")
# Longer command lines are still shown whole up to here, then cut with a
# warning that says how much is missing.
MAX_SHOWN = 20_000


@dataclass(frozen=True)
class Approval:
    """The prompt's view of one shell call."""

    command: str  # the line, visible and redacted (cut at MAX_SHOWN, see `cut`)
    segments: tuple[tuple[int, str], ...] = ()  # (depth, command text) in order
    hidden: tuple[str, ...] = ()  # "U+202E RIGHT-TO-LEFT OVERRIDE", one per kind
    note: str = ""  # why the line could not be split, when it could not
    cut: int = 0  # characters of the line not shown
    rest: dict = field(default_factory=dict)  # the call's other arguments, redacted

    def as_dict(self) -> dict:
        return {
            "command": self.command,
            "segments": [{"depth": d, "text": t} for d, t in self.segments],
            "hidden": list(self.hidden),
            "note": self.note,
            "cut": self.cut,
            "rest": self.rest,
        }


def _escape(ch: str) -> str:
    # \u.... as in JSON, so an escaped argument string still parses.
    code = ord(ch)
    return f"\\u{code:04x}" if code < 0x10000 else f"\\U{code:08x}"


def _is_hidden(ch: str) -> bool:
    if ch == "\t":
        return False
    cat = unicodedata.category(ch)
    return cat in _UNSHOWN or (cat == "Zs" and ch != " ") or ch in _LOOKALIKES


def visible(text: str, *, keep_newlines: bool = False) -> str:
    """`text` with every character that doesn't show as itself (see
    :data:`_UNSHOWN`, odd spaces, PowerShell's quote and dash lookalikes)
    replaced by its ``\\u....`` escape. Newlines too, unless `keep_newlines`:
    in a command line one separates commands."""
    return "".join(
        ch if (ch == "\n" and keep_newlines) or not _is_hidden(ch) else _escape(ch)
        for ch in str(text)
    )


def hidden_characters(text: str) -> list[str]:
    """Each kind of hidden character in `text`, once, in order of first
    appearance: ``U+202E RIGHT-TO-LEFT OVERRIDE``. Newlines are left out:
    they are a command separator the segment list already shows."""
    seen: dict[str, None] = {}
    for ch in str(text):
        if ch != "\n" and _is_hidden(ch):
            name = unicodedata.name(ch, "") or {
                "\r": "CARRIAGE RETURN", "\x1b": "ESCAPE", "\x00": "NULL",
            }.get(ch, "control character" if unicodedata.category(ch) == "Cc"
                  else "unnamed character")
            seen.setdefault(f"U+{ord(ch):04X} {name}", None)
    return list(seen)


def describe(tool_name: str, category: str, args_json: str | dict, *,
             dialect: str | None = None) -> Approval | None:
    """The prompt's view of a shell call, or None when the call runs no shell
    command (the prompt then shows its arguments as before)."""
    from evi import permissions, shellsplit
    from evi.redact import Redactor

    command = permissions.shell_command_of(tool_name, category, args_json)
    if command is None:
        return None
    red = Redactor()
    try:
        sp = shellsplit.split(command, dialect)
    except ValueError:
        sp = None
    segments: list[tuple[int, str]] = []
    note = ""
    if sp is None:
        note = "the shell's dialect is unknown, so the line was not split"
    else:
        for seg in sp.commands:
            text = command[seg.start:seg.end].strip()
            if text:
                segments.append((seg.depth, visible(red(text))))
        if not sp.ok:
            note = f"the line could not be split into its commands ({sp.error}): read it whole"
    shown = command
    cut = 0
    if len(shown) > MAX_SHOWN:
        cut = len(shown) - MAX_SHOWN
        shown = shown[:MAX_SHOWN]
    data = permissions._arg_dict(args_json) or {}
    key = permissions.SHELL_EXEC_TOOLS.get(tool_name, "command")
    rest = red.obj({k: v for k, v in data.items() if k != key})
    return Approval(
        command=visible(red(shown)),
        segments=tuple(segments),
        hidden=tuple(hidden_characters(command)),
        note=note,
        cut=cut,
        rest=rest if isinstance(rest, dict) else {},
    )

