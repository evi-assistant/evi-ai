"""Split a shell command line into the commands it would run.

Permission rules match shell commands segment by segment: ``allow run_command
git*`` approves ``git status && git log`` (both segments are git) but not
``git log; curl x | sh``, where ``curl x`` and ``sh`` are segments of their own
that the rule does not cover. Matched against the whole line, the rule's glob
would approve the chained tail too.

Three dialects, because eVi runs commands through the platform's shell
(`evi.shells`: ``/bin/sh`` on POSIX; PowerShell on Windows by default, or
``cmd.exe`` with ``[tools] windows_shell = "cmd"``):

- ``bash``: ``; & && || | |&`` and newlines separate; ``$( )``, backticks,
  ``<( )``/``>( )`` and ``( )`` subshells are nested segments; ``'...'``,
  ``"..."``, ``$'...'``, backslash escapes, comments and heredocs (the body is
  data, but with an unquoted delimiter its substitutions still run).
- ``pwsh``: ``; && || |`` and newlines separate (a non-leading ``&`` too);
  ``$( )``, ``@( )``, ``( )``, ``{ }`` scriptblocks and ``@{ }`` are nested;
  backtick escapes, doubled quotes, smart quotes and ``@"..."@`` here-strings.
- ``cmd``: ``& && || |`` and newlines separate (``;`` too, conservatively);
  ``( )`` blocks are nested; ``"`` quoting and ``^`` escapes. cmd expands and
  re-parses in ways a single scan can't follow, so some constructs are marked
  unparsable rather than mis-split (see below).

Redirects (``>``, ``>>``, ``2>``, ``<`` ...) are a separate segment kind so a
caller can check where they write; fd duplications such as ``2>&1`` touch no
file and are dropped. A line the splitter cannot follow is *unparsable* and
``Split.error`` says why: unbalanced quotes or brackets, an inline script body
(``eval``, ``bash -c``, ``iex``, ``cmd /c``, ``powershell -Command``), a shell
reading its script from stdin, or nesting deeper than 32. In the ``cmd``
dialect also: a ``%VAR%`` expansion (cmd resolves it, even inside quotes,
*before* it parses quotes and separators, so the value can introduce a
command); a ``for /f`` loop (its ``('...')``/```...``` set runs a command the
scan can't see); a ``^&`` or ``^|`` anywhere a ``|`` pipe appears (cmd re-parses
each pipeline stage in a child ``cmd``, stripping the caret so the escaped
separator goes live); and a ``^`` before a bare carriage return (the same
re-escape). Callers must never
let a specific allow rule approve an unparsable line. Where a construct is
ambiguous the splitter splits, because more segments is always the safe
direction: each one has to be covered on its own.

Pure and side-effect free, but for :func:`default_dialect`, which reads
``[tools] windows_shell`` on Windows.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

DIALECTS = ("bash", "pwsh", "cmd")
# Longer lines are not parsed at all (unparsable → ask); keeps the check cheap.
MAX_LENGTH = 100_000
_MAX_DEPTH = 32


def default_dialect() -> str:
    """The dialect of the shell a command line runs in here (`evi.shells`):
    bash on POSIX; on Windows pwsh, or cmd when ``[tools] windows_shell`` says so."""
    if os.name != "nt":
        return "bash"
    from evi import shells

    return shells.dialect()


@dataclass(frozen=True)
class Segment:
    """One command, or one redirect, of a split command line."""

    kind: str  # "command" | "redirect"
    # command: its words as written, joined by single spaces, redirects left
    # out. redirect: the target, unquoted.
    text: str
    start: int  # source span in the original line (redirects and nested
    end: int  # substitutions included)
    depth: int = 0  # 0 = top level; >0 = inside $( ), ` `, ( ), { } ...
    words: tuple[str, ...] = ()  # command: the unquoted words
    op: str = ""  # redirect: the operator as written ('>', '2>>', '<', '&>', '*>' ...)
    dynamic: bool = False  # redirect: the target holds an expansion, so its path is unknown


_CHDIR = frozenset(
    {"cd", "chdir", "pushd", "popd", "set-location", "sl", "push-location", "pop-location"}
)
_DRIVE = re.compile(r"[A-Za-z]:\Z")


@dataclass(frozen=True)
class Split:
    """The segments of a command line, and why it is unparsable (if it is).

    An unparsable split still carries the segments read before the problem,
    so a deny rule can match them; it must never satisfy an allow rule.
    """

    dialect: str
    segments: tuple[Segment, ...]
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def commands(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.segments if s.kind == "command")

    @property
    def redirects(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.segments if s.kind == "redirect")

    @property
    def changes_dir(self) -> bool:
        """True if a segment changes directory (``cd``, ``pushd``, ``C:`` ...),
        so a relative redirect target can't be resolved against the start dir."""
        for seg in self.commands:
            ci = _command_index(seg.words)
            if ci is None:
                continue
            word = seg.words[ci]
            if _base(word) in _CHDIR or _DRIVE.match(word):
                return True
        return False


def split(command: str, dialect: str | None = None) -> Split:
    """Split `command` as `dialect` (default: :func:`default_dialect`) would."""
    d = dialect or default_dialect()
    if d not in DIALECTS:
        raise ValueError(f"unknown shell dialect: {dialect!r}")
    src = command or ""
    if len(src) > MAX_LENGTH:
        return Split(d, (), f"command line longer than {MAX_LENGTH} characters")
    out: list[Segment] = []
    reasons: list[str] = []
    error = ""
    try:
        _Parser(src, d, out, reasons, 0, len(src), 0).run()
    except _Unparsable as exc:
        error = str(exc)
    except RecursionError:
        error = "nesting too deep"
    if not error and reasons:
        error = reasons[0]
    if not error and d == "cmd":
        error = _cmd_hazard(src)
    out.sort(key=lambda seg: (seg.start, seg.depth))
    return Split(d, tuple(out), error)


# --- inline script bodies ----------------------------------------------------

_POSIX_SHELLS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh"}
)
_PWSH_EXES = frozenset({"powershell", "pwsh", "powershell_ise"})
_EVALUATORS = frozenset({"eval", "iex", "invoke-expression"})
# Commands that run the command named by their (later) arguments.
_WRAPPERS = frozenset({
    "sudo", "doas", "env", "nohup", "nice", "ionice", "time", "timeout", "stdbuf",
    "setsid", "exec", "command", "builtin", "xargs", "chrt", "taskset", "unbuffer",
    "start", "call", "&",
})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# A wrapper's own non-dash arguments: counts/durations and cmd-style /switches.
_WRAPPER_ARG = re.compile(r"(?:\d+(?:\.\d+)?[smhd]?|/[A-Za-z?][\w:-]*)\Z")
# sh -c / -lc / -ec ..., -s (script on stdin), a lone "-" (stdin), fish --command.
_POSIX_INLINE = re.compile(r"-[A-Za-z]*[cs][A-Za-z]*\Z|-\Z|--command\Z")
_DASHES = "-/\u2013\u2014\u2015"  # PowerShell also takes en/em dashes and '/'


def _base(word: str) -> str:
    """Lower-cased program name of a word: no directory, no ``.exe``."""
    b = re.split(r"[\\/]", word)[-1].lower()
    return b[:-4] if b.endswith(".exe") else b


def _command_index(words: tuple[str, ...] | list[str]) -> int | None:
    """Index of the word naming the program a segment runs, skipping leading
    ``NAME=value`` assignments and wrappers (``sudo -u x``, ``timeout 5``, ...)."""
    i, n = 0, len(words)
    while i < n:
        w = words[i]
        if _ASSIGNMENT.match(w):
            i += 1
            continue
        if _base(w) in _WRAPPERS:
            i += 1
            while i < n and (
                not words[i]
                or words[i].startswith("-")
                or _ASSIGNMENT.match(words[i])
                or _WRAPPER_ARG.match(words[i])
            ):
                i += 1
            continue
        return i
    return None


def _pwsh_flag(arg: str, names: tuple[str, ...], exact: tuple[str, ...] = ()) -> bool:
    """True if `arg` is a PowerShell CLI switch naming one of `names` (which
    accept any unambiguous prefix: -c, -com, -e, -enc ...) or one of `exact`."""
    if not arg or arg[0] not in _DASHES:
        return False
    key = arg.lstrip(_DASHES).split(":", 1)[0].lower()
    return bool(key) and (key in exact or any(n.startswith(key) for n in names))


def _inline_reason(words: tuple[str, ...]) -> str:
    """Why this command runs a script body the splitter can't see, or ""."""
    ci = _command_index(words)
    if ci is None:
        return ""
    if _base(words[ci]) in _EVALUATORS:
        return f"{words[ci]} runs a string as a command line"
    # One backward pass (linear in the word count): the flags say what the
    # words AFTER position k hold; the earliest offending word sets `reason`.
    sh_inline = ps_inline = ps_file = cmd_inline = non_dash = False
    reason = ""
    for k in range(len(words) - 1, ci - 1, -1):
        word = words[k]
        b = _base(word)
        if "scriptblock]::create" in word.lower():
            reason = "[scriptblock]::Create runs a string as code"
        elif b in _POSIX_SHELLS:
            if sh_inline:
                reason = f"{b} -c runs an inline script"
            elif k == ci and not non_dash:
                reason = f"{b} reads its script from stdin"
        elif b in _PWSH_EXES:
            if ps_inline:
                reason = f"{b} -Command runs an inline script"
            elif k == ci and not ps_file:
                reason = f"{b} without -File runs a command line or reads stdin"
        elif b == "cmd":
            if cmd_inline:
                reason = "cmd /c runs an inline command line"
            elif k == ci:
                reason = "cmd without /c reads commands from stdin"
        sh_inline = sh_inline or bool(_POSIX_INLINE.match(word))
        ps_inline = ps_inline or _pwsh_flag(
            word, ("command", "encodedcommand", "commandwithargs"), ("ec", "cwa")
        )
        ps_file = ps_file or _pwsh_flag(word, ("file",))
        cmd_inline = cmd_inline or word.lower().startswith(("/c", "/k", "/r"))
        non_dash = non_dash or not word.startswith("-")
    return reason


# --- the parser ----------------------------------------------------------------

_BLANKS = " \t\r\f\v"
_BREAK = {
    "bash": frozenset(" \t\r\f\v\n;&|<>()"),
    "pwsh": frozenset(" \t\r\f\v\n;&|<>(){}"),
    "cmd": frozenset(" \t\r\f\v\n;&|<>()"),
}
_ESCAPE = {"bash": "\\", "pwsh": "`", "cmd": "^"}
_SEPARATORS = {
    "bash": (";;&", ";;", ";&", "&&", "||", "|&", ";", "|", "&"),
    "pwsh": ("&&", "||", ";", "|", "&"),
    "cmd": ("&&", "||", ";", "|", "&"),
}
_REDIRECT_OPS = {
    "bash": ("<<<", "<<-", "<<", "<>", "<&", "<", ">>", ">|", ">&", ">"),
    "pwsh": (">>", ">&", ">", "<"),
    "cmd": (">>", ">&", "<&", ">", "<"),
}
_FD_DUP = re.compile(r"\d*-?\Z")
# cmd resolves `%VAR%` (and `%VAR:~a,b%`, `%VAR:x=y%`, `%CMDCMDLINE%` ...) before
# it parses quotes or separators, and it does so inside quotes too, so a pair of
# `%` with at least one character between them makes what runs unknowable. `%%`
# (adjacent, a batch escape) is not a pair and is left alone.
_CMD_PCT = re.compile(r"%[^%\r\n]+%")
# `for /f %x in ('cmd')` / ``(`cmd`)`` runs a command inside the (...) set.
_CMD_FOR_F = re.compile(r"\bfor\s+/f\b", re.IGNORECASE)


def _cmd_hazard(src: str) -> str:
    """Why a cmd line can't be followed by a single scan (expansions and
    command sets cmd resolves before/around parsing), or "" if it can."""
    if _CMD_PCT.search(src):
        return "a cmd %VAR% expansion is resolved before the line is parsed"
    if _CMD_FOR_F.search(src):
        return "for /f runs the command in its (...) set"
    return ""
_PWSH_SQ = "'\u2018\u2019\u201a\u201b"
_PWSH_DQ = '"\u201c\u201d\u201e'
_PWSH_DQ_CLOSE = re.compile("\n[" + _PWSH_DQ + "]@")
_PWSH_SQ_CLOSE = re.compile("\n[" + _PWSH_SQ + "]@")


class _Unparsable(Exception):
    """The line can't be followed safely; the message says why."""


@dataclass
class _Word:
    value: str  # unquoted
    start: int
    end: int
    dynamic: bool = False  # holds an expansion ($x, $( ), %x% ...)
    quoted: bool = False  # any quoting or escaping was used


class _Seg:
    """A command being collected: its words and the extent of its source."""

    def __init__(self) -> None:
        self.words: list[_Word] = []
        self.start = -1
        self.end = -1

    def extend(self, a: int, b: int) -> None:
        self.start = a if self.start < 0 else min(self.start, a)
        self.end = max(self.end, b)

    def add(self, word: _Word) -> None:
        self.words.append(word)
        self.extend(word.start, word.end)


class _Parser:
    """Recursive-descent scanner over ``s[start:limit]``. Nested regions that
    must stay bounded (backquote and heredoc bodies) get a sub-parser over the
    same string, so every span is an offset into the original line."""

    def __init__(
        self,
        s: str,
        dialect: str,
        out: list[Segment],
        reasons: list[str],
        start: int,
        limit: int,
        depth: int,
    ) -> None:
        self.s, self.d, self.out, self.reasons = s, dialect, out, reasons
        self.start, self.n, self.depth = start, limit, depth
        # Heredocs seen on the current line: (delimiter, strip_tabs, expands, depth).
        self.heredocs: list[tuple[str, bool, bool, int]] = []
        # cmd only: a `^&`/`^|` escape seen, and a `|` pipe seen. Both together
        # are unparsable — cmd re-parses each pipeline stage, dropping the caret.
        self._cmd_caret_sep = False
        self._cmd_pipe = False

    def _sub(self, a: int, b: int, depth: int) -> _Parser:
        if depth > _MAX_DEPTH:
            raise _Unparsable("nesting too deep")
        return _Parser(self.s, self.d, self.out, self.reasons, a, b, depth)

    def run(self) -> None:
        self._seq(self.start, self.depth, None)
        if self.heredocs:
            raise _Unparsable("unterminated here-document")
        if self.d == "cmd" and self._cmd_caret_sep and self._cmd_pipe:
            raise _Unparsable(
                "cmd re-parses each command in a pipeline, so '^&'/'^|' there "
                "can introduce a command"
            )

    # -- command lists ------------------------------------------------------

    def _seq(self, i: int, depth: int, closer: str | None) -> int:
        """Parse commands from `i` up to `closer` (or the end); return the
        index just past the closer."""
        if depth > _MAX_DEPTH:
            raise _Unparsable("nesting too deep")
        s, n, d = self.s, self.n, self.d
        seg = _Seg()
        while True:
            i = self._blanks(i)
            if i >= n:
                if closer is not None:
                    raise _Unparsable(f"missing closing {closer!r}")
                self._finish(seg, depth)
                return n
            c = s[i]
            if c == closer:
                self._finish(seg, depth)
                return i + 1
            if c == "\n":
                self._finish(seg, depth)
                seg = _Seg()
                i = self._heredoc_bodies(i + 1)
                continue
            if c == "#" and d != "cmd":  # a comment (we are at a word start)
                end = s.find("\n", i, n)
                i = n if end < 0 else end
                continue
            if d == "pwsh" and s.startswith("<#", i, n):
                end = s.find("#>", i + 2, n)
                if end < 0:
                    raise _Unparsable("unterminated <# comment")
                i = end + 2
                continue
            if d == "pwsh" and c == "&" and not seg.words and not s.startswith("&&", i, n):
                seg.add(_Word("&", i, i + 1))  # the call operator, not a separator
                i += 1
                continue
            j = self._redirect(i, seg, depth)
            if j is not None:
                i = j
                continue
            sep = next((p for p in _SEPARATORS[d] if s.startswith(p, i, n)), "")
            if sep:
                if d == "cmd" and sep == "|":
                    self._cmd_pipe = True
                self._finish(seg, depth)
                seg = _Seg()
                i += len(sep)
                continue
            j = self._group(i, seg, depth)
            if j is not None:
                i = j
                continue
            word = self._word(i, depth)
            if word.end == i:  # a stray ')' or '}', or anything else we can't read
                raise _Unparsable(f"unexpected {c!r}")
            seg.add(word)
            i = word.end

    def _finish(self, seg: _Seg, depth: int) -> None:
        if not seg.words:
            return
        text = " ".join(self.s[w.start:w.end] for w in seg.words)
        words = tuple(w.value for w in seg.words)
        self.out.append(Segment("command", text, seg.start, seg.end, depth, words))
        reason = _inline_reason(words)
        if reason:
            self.reasons.append(reason)

    def _blanks(self, i: int) -> int:
        s, n, esc = self.s, self.n, _ESCAPE[self.d]
        while i < n:
            c = s[i]
            if c in _BLANKS:
                i += 1
            elif c == esc and s.startswith("\n", i + 1, n):  # line continuation
                i += 2
            elif c == esc and s.startswith("\r\n", i + 1, n):
                i += 3
            else:
                break
        return i

    def _group(self, i: int, seg: _Seg, depth: int) -> int | None:
        """A nested command list opening at `i`, or None if there is none."""
        s, n, d = self.s, self.n, self.d
        c = s[i]
        if d == "bash":
            if c in "<>" and s.startswith("(", i + 1, n):  # process substitution
                j = self._seq(i + 2, depth + 1, ")")
                seg.add(_Word(s[i:j], i, j, dynamic=True))
                return j
            if c != "(":
                return None
            if seg.words:  # f() {...}, arr=(...), extglob: not followed
                raise _Unparsable("unexpected '(' inside a command")
            j = self._seq(i + 1, depth + 1, ")")
            seg.extend(i, j)
            return j
        if d == "pwsh":
            if c == "@" and i + 1 < n and s[i + 1] in "({":
                opener, body = s[i + 1], i + 2
            elif c in "({":
                opener, body = c, i + 1
            else:
                return None
            j = self._seq(body, depth + 1, ")" if opener == "(" else "}")
        else:
            if c != "(":
                return None
            j = self._seq(i + 1, depth + 1, ")")
        if seg.words:  # an argument: keep it in the command's text
            seg.add(_Word(s[i:j], i, j, dynamic=True))
        else:
            seg.extend(i, j)
        return j

    # -- redirects and heredocs ---------------------------------------------

    def _redirect(self, i: int, seg: _Seg, depth: int) -> int | None:
        s, n, d = self.s, self.n, self.d
        c = s[i]
        if d == "bash" and c == "&" and s.startswith(">", i + 1, n):
            op = "&>>" if s.startswith("&>>", i, n) else "&>"
        elif c in "<>":
            if d == "bash" and s.startswith("(", i + 1, n):
                return None  # process substitution
            op = next(o for o in _REDIRECT_OPS[d] if s.startswith(o, i, n))
        else:
            return None
        start = i
        if seg.words:  # an fd prefix written against the operator: 2> *>
            last = seg.words[-1]
            fd = last.value.isdigit() or (d == "pwsh" and last.value == "*")
            if last.end == i and not last.quoted and fd:
                seg.words.pop()
                start = last.start
        j = self._blanks(i + len(op))
        word = self._word(j, depth) if j < n else None
        if word is None or word.end == j:
            raise _Unparsable(f"redirect {op!r} without a target")
        seg.extend(start, word.end)
        if op in ("<<", "<<-"):
            self.heredocs.append((word.value, op == "<<-", not word.quoted, depth))
            return word.end
        if op == "<<<":  # here-string: inline data, not a file
            return word.end
        if op in (">&", "<&") and word.value and not word.dynamic and _FD_DUP.match(word.value):
            return word.end  # 2>&1, >&2, <&-: no file involved
        self.out.append(Segment(
            "redirect", word.value, start, word.end, depth,
            op=s[start:i] + op, dynamic=word.dynamic,
        ))
        return word.end

    def _heredoc_bodies(self, i: int) -> int:
        """Consume the bodies of the heredocs opened on the line that just
        ended (bodies start at `i`); return the index after the last one."""
        if not self.heredocs:
            return i
        s, n = self.s, self.n
        pending, self.heredocs = self.heredocs, []
        for delim, strip_tabs, expands, depth in pending:
            body = i
            while True:
                if i >= n:
                    raise _Unparsable(f"unterminated here-document ({delim})")
                nl = s.find("\n", i, n)
                stop = n if nl < 0 else nl
                line = s[i:stop].rstrip("\r")
                nxt = n if nl < 0 else nl + 1
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    end, i = i, nxt
                    break
                i = nxt
            if expands:  # unquoted delimiter: $( ) and `...` in the body still run
                self._sub(body, end, depth)._expanding_body()
        return i

    def _expanding_body(self) -> None:
        s, n, k = self.s, self.n, self.start
        while k < n:
            c = s[k]
            if self.d == "bash" and c == "\\":
                k += 2
            elif self.d == "pwsh" and c == "`":
                k += 2
            elif c == "$" and self.d == "bash":
                k = self._dollar(k, self.depth, in_dq=True)[0]
            elif c == "$" and self.d == "pwsh":
                k = self._dollar_pwsh(k, self.depth)[0]
            elif c == "`" and self.d == "bash":
                k = self._backtick(k, self.depth)
            else:
                k += 1
        if self.heredocs:
            raise _Unparsable("here-document inside a here-document")

    # -- words ----------------------------------------------------------------

    def _word(self, i: int, depth: int) -> _Word:
        if self.d == "bash":
            return self._word_bash(i, depth)
        if self.d == "pwsh":
            word = self._word_pwsh(i, depth)
            if word.value == "--%" and not word.quoted:
                raise _Unparsable("PowerShell stop-parsing token (--%)")
            return word
        return self._word_cmd(i)

    def _word_bash(self, i: int, depth: int) -> _Word:
        s, n, brk = self.s, self.n, _BREAK["bash"]
        buf: list[str] = []
        dyn = quoted = False
        j = i
        while j < n:
            c = s[j]
            if c in brk:
                break
            if c == "\\":
                if j + 1 >= n:
                    raise _Unparsable("trailing backslash")
                if s[j + 1] != "\n":  # backslash-newline is a line continuation
                    buf.append(s[j + 1])
                    quoted = True
                j += 2
            elif c == "'":
                k = s.find("'", j + 1, n)
                if k < 0:
                    raise _Unparsable("unbalanced single quote")
                buf.append(s[j + 1:k])
                quoted = True
                j = k + 1
            elif c == '"':
                j, v, dy = self._dquote_bash(j, depth)
                buf.append(v)
                dyn |= dy
                quoted = True
            elif c == "$":
                j, v, dy, q = self._dollar(j, depth, in_dq=False)
                buf.append(v)
                dyn |= dy
                quoted |= q
            elif c == "`":
                k = self._backtick(j, depth)
                buf.append(s[j:k])
                dyn = True
                j = k
            else:
                buf.append(c)
                j += 1
        return _Word("".join(buf), i, j, dyn, quoted)

    def _dquote_bash(self, j: int, depth: int) -> tuple[int, str, bool]:
        s, n = self.s, self.n
        k = j + 1
        buf: list[str] = []
        dyn = False
        while k < n:
            c = s[k]
            if c == '"':
                return k + 1, "".join(buf), dyn
            if c == "\\" and k + 1 < n:
                nx = s[k + 1]
                if nx in '$`"\\':
                    buf.append(nx)
                elif nx != "\n":
                    buf.append(c + nx)
                k += 2
                continue
            if c == "$":
                k, v, dy, _ = self._dollar(k, depth, in_dq=True)
                buf.append(v)
                dyn |= dy
                continue
            if c == "`":
                k2 = self._backtick(k, depth)
                buf.append(s[k:k2])
                dyn = True
                k = k2
                continue
            if c == "\n" and self.heredocs:
                raise _Unparsable("here-document body inside a quoted string")
            buf.append(c)
            k += 1
        raise _Unparsable("unbalanced double quote")

    def _dollar(self, j: int, depth: int, *, in_dq: bool) -> tuple[int, str, bool, bool]:
        """A bash ``$`` construct at `j`: (end, value, dynamic, quoted)."""
        s, n = self.s, self.n
        nx = s[j + 1] if j + 1 < n else ""
        if nx == "(":
            if s.startswith("((", j + 1, n) and self._is_arith(j):
                k = self._arith(j + 3, depth + 1)
            else:
                k = self._seq(j + 2, depth + 1, ")")
            return k, s[j:k], True, False
        if nx == "{":
            k = self._param(j + 2, depth + 1)
            return k, s[j:k], True, False
        if not in_dq and nx == "'":  # $'...' ANSI-C quoting
            k = j + 2
            while k < n and s[k] != "'":
                k += 2 if s[k] == "\\" else 1
            if k >= n:
                raise _Unparsable("unbalanced $'...' quote")
            return k + 1, s[j + 2:k], False, True
        if not in_dq and nx == '"':  # $"..." locale string
            k, v, dy = self._dquote_bash(j + 1, depth)
            return k, v, dy, True
        if nx and (nx.isalnum() or nx in "_@*#?$!-"):
            return j + 1, "$", True, False
        return j + 1, "$", False, False

    def _is_arith(self, j: int) -> bool:
        """``$((`` at `j`: arithmetic if its parens close with an adjacent
        ``))``, else a command substitution of a subshell (``$( (a); b)``)."""
        s, n = self.s, self.n
        k, level = j + 3, 0
        while k < n:
            c = s[k]
            if c == "(":
                level += 1
            elif c == ")":
                if level == 0:
                    return s.startswith(")", k + 1, n)
                level -= 1
            k += 1
        return False

    def _arith(self, k: int, depth: int) -> int:
        if depth > _MAX_DEPTH:
            raise _Unparsable("nesting too deep")
        s, n = self.s, self.n
        level = 0
        while k < n:
            c = s[k]
            if c == "\\":
                k += 2
                continue
            if c == "$":
                k = self._dollar(k, depth, in_dq=True)[0]
                continue
            if c == "`":
                k = self._backtick(k, depth)
                continue
            if c == "(":
                level += 1
            elif c == ")":
                if level == 0:
                    if s.startswith(")", k + 1, n):
                        return k + 2
                    raise _Unparsable("malformed arithmetic expansion")
                level -= 1
            k += 1
        raise _Unparsable("unterminated arithmetic expansion")

    def _param(self, k: int, depth: int) -> int:
        """``${...}`` from just past the brace; may hold nested substitutions."""
        if depth > _MAX_DEPTH:
            raise _Unparsable("nesting too deep")
        s, n = self.s, self.n
        level = 1
        while k < n:
            c = s[k]
            if c == "\\":
                k += 2
                continue
            if c == "'":
                e = s.find("'", k + 1, n)
                if e < 0:
                    raise _Unparsable("unbalanced single quote")
                k = e + 1
                continue
            if c == '"':
                k = self._dquote_bash(k, depth)[0]
                continue
            if c == "$":
                k = self._dollar(k, depth, in_dq=True)[0]
                continue
            if c == "`":
                k = self._backtick(k, depth)
                continue
            if c == "{":
                level += 1
            elif c == "}":
                level -= 1
                if level == 0:
                    return k + 1
            k += 1
        raise _Unparsable("unterminated ${...}")

    def _backtick(self, j: int, depth: int) -> int:
        s, n = self.s, self.n
        k = j + 1
        while k < n and s[k] != "`":
            if s[k] == "\\":  # escaped/nested backquotes change what runs: not followed
                raise _Unparsable("backslash inside backquotes")
            k += 1
        if k >= n:
            raise _Unparsable("unbalanced backquote")
        self._sub(j + 1, k, depth + 1).run()
        return k + 1

    def _word_pwsh(self, i: int, depth: int) -> _Word:
        s, n, brk = self.s, self.n, _BREAK["pwsh"]
        if s.startswith("@", i, n) and i + 1 < n and s[i + 1] in _PWSH_DQ + _PWSH_SQ:
            j, v, dyn = self._herestring(i, depth)
            return _Word(v, i, j, dyn, True)
        buf: list[str] = []
        dyn = quoted = False
        j = i
        while j < n:
            c = s[j]
            if c in brk:
                break
            if c == "`":
                if j + 1 >= n:
                    raise _Unparsable("trailing backtick")
                if s[j + 1] != "\n":
                    buf.append(s[j + 1])
                    quoted = True
                j += 2
            elif c in _PWSH_SQ:
                j, v = self._squote_pwsh(j)
                buf.append(v)
                quoted = True
            elif c in _PWSH_DQ:
                j, v, dy = self._dquote_pwsh(j, depth)
                buf.append(v)
                dyn |= dy
                quoted = True
            elif c == "$":
                j, v, dy = self._dollar_pwsh(j, depth)
                buf.append(v)
                dyn |= dy
            else:
                buf.append(c)
                j += 1
        return _Word("".join(buf), i, j, dyn, quoted)

    def _squote_pwsh(self, j: int) -> tuple[int, str]:
        s, n = self.s, self.n
        k = j + 1
        buf: list[str] = []
        while k < n:
            c = s[k]
            if c in _PWSH_SQ:
                if k + 1 < n and s[k + 1] in _PWSH_SQ:  # '' is a literal quote
                    buf.append("'")
                    k += 2
                    continue
                return k + 1, "".join(buf)
            buf.append(c)
            k += 1
        raise _Unparsable("unbalanced single quote")

    def _dquote_pwsh(self, j: int, depth: int) -> tuple[int, str, bool]:
        s, n = self.s, self.n
        k = j + 1
        buf: list[str] = []
        dyn = False
        while k < n:
            c = s[k]
            if c == "`":
                if k + 1 >= n:
                    break
                buf.append(s[k + 1])
                k += 2
                continue
            if c in _PWSH_DQ:
                if k + 1 < n and s[k + 1] in _PWSH_DQ:  # "" is a literal quote
                    buf.append('"')
                    k += 2
                    continue
                return k + 1, "".join(buf), dyn
            if c == "$":
                k, v, dy = self._dollar_pwsh(k, depth)
                buf.append(v)
                dyn |= dy
                continue
            buf.append(c)
            k += 1
        raise _Unparsable("unbalanced double quote")

    def _dollar_pwsh(self, j: int, depth: int) -> tuple[int, str, bool]:
        s, n = self.s, self.n
        nx = s[j + 1] if j + 1 < n else ""
        if nx == "(":  # subexpression: runs a pipeline
            k = self._seq(j + 2, depth + 1, ")")
            return k, s[j:k], True
        if nx == "{":  # ${name}: a variable, not code
            k = s.find("}", j + 2, n)
            if k < 0:
                raise _Unparsable("unterminated ${...}")
            return k + 1, s[j:k + 1], True
        if nx and (nx.isalnum() or nx in "_?^$:"):
            return j + 1, "$", True
        return j + 1, "$", False

    def _herestring(self, i: int, depth: int) -> tuple[int, str, bool]:
        s, n = self.s, self.n
        expands = s[i + 1] in _PWSH_DQ
        k = i + 2
        while k < n and s[k] in " \t\r":
            k += 1
        if k >= n or s[k] != "\n":
            raise _Unparsable("malformed here-string header")
        m = (_PWSH_DQ_CLOSE if expands else _PWSH_SQ_CLOSE).search(s, k, n)
        if m is None:
            raise _Unparsable("unterminated here-string")
        if expands:
            self._sub(k + 1, m.start(), depth)._expanding_body()
        return m.end(), s[k + 1:m.start()], expands

    def _word_cmd(self, i: int) -> _Word:
        s, n, brk = self.s, self.n, _BREAK["cmd"]
        buf: list[str] = []
        dyn = quoted = False
        j = i
        while j < n:
            c = s[j]
            if c in brk:
                break
            if c == "^":
                if j + 1 >= n:
                    raise _Unparsable("trailing caret")
                nxt = s[j + 1]
                if nxt == "\r" and not s.startswith("\r\n", j + 1, n):
                    # `^` + a lone CR is a caret continuation that re-escapes the
                    # next separator (so `echo a^\r^|md x` runs `md x`); `^\r\n`
                    # (a real Windows line continuation) is fine.
                    raise _Unparsable("caret before a bare carriage return")
                if nxt != "\n":
                    buf.append(nxt)
                    quoted = True
                    if nxt in "&|":
                        self._cmd_caret_sep = True
                j += 2
            elif c == '"':
                k = s.find('"', j + 1, n)
                if k < 0:
                    raise _Unparsable("unbalanced double quote")
                v = s[j + 1:k]
                if "\n" in v:  # cmd ends the line at a newline, quoted or not
                    raise _Unparsable("newline inside a quoted string")
                buf.append(v)
                dyn |= "%" in v or "!" in v
                quoted = True
                j = k + 1
            else:
                dyn |= c in "%!"
                buf.append(c)
                j += 1
        return _Word("".join(buf), i, j, dyn, quoted)
