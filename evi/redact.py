"""Secret redaction for what eVi writes down or shows: transcripts, crash
reports, and (as later items adopt it) run reports, evidence bundles, exports
and tool-call displays.

A secret becomes a marker that says what was there, never what it was:
``[REDACTED:<kind>]``. Two things are redacted:

- Token formats with a distinctive shape (``PATTERNS``): ``sk-`` API keys
  (OpenAI, Anthropic, DeepSeek, OpenRouter), GitHub (``ghp_`` and co.,
  ``github_pat_``), GitLab, AWS access key ids (``AKIA``/``ASIA``), Slack
  (``xox?-``), Google (``AIza``), Hugging Face, npm, PyPI, Stripe, other
  provider keys (``gsk_``, ``xai-``, ``pplx-``), JWTs, PEM private-key blocks
  and HTTP ``Authorization``/``Bearer`` credentials. The shapes are tight on
  purpose: a word boundary, a long random body, and for the loose prefixes a
  digit in it, so prose like "task-runner" or "Bearer authentication" stays.
- Known values: the secrets this machine actually has, each labelled by where
  it came from (``[REDACTED:OPENAI_API_KEY]``, ``[REDACTED:llm.api_key]``).
  They are read from the environment (names ending ``_KEY``, ``_TOKEN``,
  ``_SECRET``, ``_PASSWORD``), from secret-named fields (``api_key``,
  ``*_token``, ``*_secret``, ``*_password``, ``Authorization``) of
  config.toml, the active profile, backends.json, peers.json, users.json,
  mcp.json and routines.json, and from any source another module registers
  (``register_source``). A value shorter than ``MIN_KNOWN_LEN`` is never
  redacted (a short value matches ordinary text), nor is a placeholder
  (``sk-no-key-required``, ``************``), an ``env:NAME`` reference, a
  value with whitespace in it or a path (of a file that exists, or a network
  path); ``max_tokens`` and ``client_secrets_path`` are not secret names. The
  set is built once and rebuilt when the environment or one of those files
  changes (a ``Config.save`` from any surface), so nothing has to remember to
  refresh it. A source that can't be read is skipped, never fatal.

None of those files is a keyring. A module that keeps a secret somewhere this
one can't see (the OS keyring, a key file of its own) registers it with
``register_source`` and calls ``refresh()`` when it changes. One such secret
is built in: the director's attestation key (`evi.attest`), redacted as
``[REDACTED:director_key]`` in the forms it is kept or printed in (hex either
case, base64, the key file's bytes as base64). It is read only through
`evi.attest`, so only where that module itself would read it (never in a
process an eVi agent may have started), at most once per change of the key
file, and kept once read: ``prime()`` reads it before a process marks itself
an interactive session (the REPL), after which `evi.attest` no longer would.

A pattern finds a token wherever it sits, including right after an escape
(``\\n``, ``\\t``) in JSON or repr text or after a terminal colour code
(``grep --color=always``), and what it replaces never holds a quote or half
an escape, so redacting JSON as text leaves it JSON.

API (stable; later items import these):

- ``redact(text, extra=()) -> str``: ``text`` with every secret replaced.
- ``redact_obj(obj, extra=()) -> obj``: a copy of a JSON-like value (dicts,
  lists, tuples) with every string in it redacted; keys and non-strings are
  left alone.
- ``Redactor(extra=(), *, known=True)``: the same, for many strings at once
  (the known-value set is looked up once) with a tally: call it on a string,
  ``.obj(value)`` on a structure, then read ``.counts`` (kind -> how many) and
  ``.total``, e.g. to say in a run report how much was redacted.
  ``.args(text)`` redacts tool-call arguments as the JSON they are and as
  text (always JSON again, untouched when clean) and ``.tool_calls(calls)``
  an assistant message's ``tool_calls`` (ids and names kept). Use these for
  arguments.
- ``extra``: further secret values a caller knows of, redacted as
  ``[REDACTED:secret]``, under the same minimum length.
- ``refresh()``: drop the known-value set so the next call rebuilds it (and
  reads the director key again).
- ``prime()``: build the known-value set now, reading what this process may
  read only now (the director key, before the REPL marks itself interactive).
- ``register_source(name, fn)``: add a known-value source; ``fn()`` returns
  (kind, value) pairs (``None`` removes it).
- ``marker(kind)`` builds a marker, ``MARKER_RE`` finds them (to count what
  an artifact had redacted); ``MIN_KNOWN_LEN`` is the minimum length.

Redact first, then truncate: a known value cut in half no longer matches, and
neither does a token shorter than its pattern's minimum.

Where it applies: every transcript line at write (evi.transcripts), every
crash report (evi.reporting), and the tool-call displays: the web UI's
ToolCall / ToolResult / PermissionRequest events and the history it redraws
(evi.apps.web.server), the CLI's tool-call lines, results and permission
prompts (evi.apps.cli.main), `evi run`'s tool outputs (evi.headless), each
scheduled task's log and recorded status (evi.scheduler) and the tool trace a
subagent hands back (evi.llm.subagent, cut short, so redacted first). Not to
the conversation the model sees: the live history keeps what the tools
returned (redacting the model's own context is opt-in and not built here).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import tomllib

import evi.config as _config

logger = logging.getLogger(__name__)

# A known value shorter than this is never redacted: short values (a port, a
# word, "lm-studio") turn up in ordinary text. Real API keys and the tokens
# eVi mints are 20+ characters.
MIN_KNOWN_LEN = 12

MARKER_RE = re.compile(r"\[REDACTED:[^\]\s]+\]")


def marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


# Characters a token body is made of; a boundary is "not one of these".
_B = r"A-Za-z0-9_\-"


# A terminal colour / erase code (CSI, `ESC [ 0 1 ; 3 1 m`) right before a
# token: `grep --color=always` output, or a tool forcing colour. Its final
# letter would otherwise read as part of the token. The ESC itself may be raw
# or escaped as JSON (\u001b) or repr (\x1b) writes it; each lookbehind is
# fixed-width, and the parameters and final letter are consumed (the
# substitution keeps whatever precedes the `s` group).
_CSI = (r"(?<=\[)(?:(?<=\x1b\[)|(?<=\\u001[bB]\[)|(?<=\\x1[bB]\[)|(?<=\\033\[))"
        r"[0-9;?]*[A-Za-z]")


def _edge(first: str, body: str = _B) -> str:
    """Where a token starting with one of `first` may start: after a
    character that can't be part of it, right after an escape (``\\n``,
    ``\\t``, ``\\u00e9``) in JSON or repr text, whose last character would
    otherwise read as part of the token, or right after a terminal colour
    code (`_CSI`). Each branch opens with a one-character check (the `first`
    lookahead, the `[` lookbehind) that rejects nearly every position in one
    step, so the scan stays fast."""
    return (rf"(?:(?=[{first}])(?:(?<![{body}])|(?<=\\[nrtfbv])|(?<=\\u[0-9A-Fa-f]{{4}}))"
            rf"|{_CSI}(?=[{first}]))")


# PEM private keys. A line break may be escaped once or twice (the key inside
# JSON, a repr, or source code shown as JSON), lines may be indented (a key in
# YAML, a Python string, a Markdown code block) and blank (PGP armour always
# has one after its headers), and a key flattened onto one line (`echo $KEY`)
# has spaces for breaks. The body may hold only what a PEM body holds: base64
# lines and header fields (`Proc-Type: 4,ENCRYPTED`, `Comment: any text`). So
# a match stops at the first other character: it never runs through code or
# prose between a BEGIN and an END, never eats a quote or half an escape
# (JSON stays JSON; an escaped tab or \u in a header value goes whole, but a
# header value with a quote in it ends the match), and takes time linear in
# the text however many BEGIN lines it has (a header value stops before
# "-----"). A bare header (a constant in code) is not a key: the first body
# line must be a header field or 16+ base64 characters.
_PEM_BEGIN = r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
_PEM_END = r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
_PEM_NL = r"[ \t]*(?:\r?\n|(?:\\{1,2}r)?\\{1,2}n)(?:[ \t]|\\{1,2}t)*"
_PEM_BREAK = rf"(?:{_PEM_NL}|[ \t]+)"
_PEM_FIELD = r"[A-Za-z][A-Za-z\-]*: (?:(?!-----)(?:[^\r\n\\\"]|\\{1,2}(?:t|u[0-9A-Fa-f]{4})))*"
_PEM_LINE = rf"(?:{_PEM_FIELD}|[A-Za-z0-9+/=]*)"

# (kind, pattern). Each pattern's `s` group is the secret; text around it in
# the match (a header name, the auth scheme) is kept. Applied in this order,
# so a JWT or an sk- key inside an Authorization header gets its own kind.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(rx)) for kind, rx in (
        # The whole block; or one cut off before its END line (truncated
        # tool output), up to where its base64 lines stop.
        ("private_key",
         rf"(?P<s>{_PEM_BEGIN}(?=(?:{_PEM_BREAK})++(?:{_PEM_FIELD}|[A-Za-z0-9+/=]{{16}}))"
         rf"(?:(?:{_PEM_BREAK}{_PEM_LINE})*+{_PEM_END}"
         rf"|(?:{_PEM_NL}{_PEM_LINE}|[ \t]+[A-Za-z0-9+/=]{{16,}})*+))"),
        ("jwt",
         rf"{_edge('e')}(?P<s>eyJ[{_B}]{{8,}}\.eyJ[{_B}]{{8,}}\.[{_B}]{{8,}})"),
        # sk- keys: OpenAI (sk-, sk-proj-), Anthropic (sk-ant-), DeepSeek,
        # OpenRouter (sk-or-). A digit in the body keeps words like
        # "sk-learn-pipeline-runner" out, and an @ after it SSH key types
        # ("sk-ecdsa-sha2-nistp256@openssh.com").
        ("api_key",
         rf"{_edge('s')}(?P<s>sk-(?=[{_B}]*[0-9])[{_B}]{{16,}}+)(?!@)"),
        ("api_key",
         rf"{_edge('gxp')}(?P<s>(?:gsk_|xai-|pplx-)[A-Za-z0-9]{{32,}})"),
        ("stripe_key",
         rf"{_edge('sr')}(?P<s>(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{{20,}})"),
        ("github_token",
         rf"{_edge('g')}(?P<s>gh[pousr]_[A-Za-z0-9]{{20,}}|github_pat_[A-Za-z0-9_]{{22,}})"),
        ("gitlab_token",
         rf"{_edge('g')}(?P<s>glpat-[A-Za-z0-9_\-]{{20,}})"),
        ("aws_access_key",
         rf"{_edge('A', 'A-Za-z0-9')}(?P<s>(?:AKIA|ASIA)[0-9A-Z]{{16}})(?![A-Za-z0-9])"),
        ("slack_token",
         rf"{_edge('x')}(?P<s>xox[abeprs]-(?=[A-Za-z0-9\-]*[0-9])[A-Za-z0-9\-]{{10,}})"),
        ("google_api_key",
         rf"{_edge('A')}(?P<s>AIza[0-9A-Za-z_\-]{{35}})(?![A-Za-z0-9_\-])"),
        ("hf_token",
         rf"{_edge('h')}(?P<s>hf_[A-Za-z0-9]{{30,}})"),
        ("npm_token",
         rf"{_edge('n')}(?P<s>npm_[A-Za-z0-9]{{36,}})"),
        ("pypi_token",
         rf"{_edge('p')}(?P<s>pypi-AgE[A-Za-z0-9_\-]{{50,}})"),
        # The credential of an Authorization header, whatever its shape
        # (`Authorization: Bearer x`, `"Authorization": "Basic x"`). Not an
        # f-string or a $VAR placeholder: { and $ end the credential.
        ("bearer",
         r"(?i:authorization)[\"']?\s*[:=]\s*[\"']?(?i:bearer|basic|token)\s+"
         r"(?P<s>[A-Za-z0-9._~+/\-]{8,}=*)"),
        # A bare `Bearer <token>`: long, and with a digit, so prose isn't.
        ("bearer",
         rf"{_edge('B', 'A-Za-z0-9_')}Bearer\s+"
         r"(?P<s>(?=[A-Za-z0-9._~+/\-]*[0-9])[A-Za-z0-9._~+/\-]{16,}=*)"),
    )
)

# Names whose value is a secret: env vars and secret-named fields in eVi's
# files. `max_tokens` and `client_secrets_path` are not.
_SECRET_NAME = re.compile(
    r"(?:^|[_\-])(?:api_?key|key|token|secret|password|passwd|authorization)$",
    re.IGNORECASE,
)

# Values that sit in a secret field without being one.
_PLACEHOLDERS = frozenset({
    "sk-no-key-required", "no-key-required", "not-required", "not-needed",
    "your-api-key", "your_api_key", "your-token-here", "placeholder", "changeme",
})

_SCHEME = re.compile(r"(?i)(?:bearer|basic|token)\s+(\S+)")


# ---- known values ------------------------------------------------------------


@dataclass(frozen=True)
class _Known:
    rx: re.Pattern[str] | None
    kinds: dict[str, str]  # text that matches -> kind


_lock = threading.Lock()
_cache: tuple[str, _Known] | None = None
# Sources other modules add (register_source): name -> callable.
_sources: dict[str, Callable[[], Iterable[tuple[str, str]]]] = {}
# The director key's forms this process has read (`_director_values`), kept
# once read; and the key file's stat at the last read (`_UNREAD`: read again).
# Guarded by _lock (only _build, which holds it, and refresh touch them).
_UNREAD = object()
_director_forms: set[str] = set()
_director_read_at: object = _UNREAD


def refresh() -> None:
    """Forget the known-value set; the next redaction rebuilds it, and reads
    the director key again."""
    global _cache, _director_read_at
    with _lock:
        _cache = None
        _director_read_at = _UNREAD


def prime() -> None:
    """Build the known-value set now, if it isn't built. A process where
    `evi.attest` reads the director key now but won't later (the REPL, before
    it sets EVI_INTERACTIVE=1 for the tools it runs) calls this first, so the
    key is redacted for the rest of the process."""
    try:
        _known()
    except Exception:  # noqa: BLE001 — redaction never fails a caller
        logger.debug("redaction: prime failed", exc_info=True)


def register_source(name: str, source: Callable[[], Iterable[tuple[str, str]]] | None) -> None:
    """Add a source of known values under `name` (replacing one of that name),
    or remove it with None. `source()` returns (kind, value) pairs, each
    redacted as ``[REDACTED:<kind>]`` under the same rules as the built-in
    sources. It is called when the set is rebuilt, under a lock, so it must not
    call back into this module; if it raises, it is skipped. Nothing here can
    see its values change (a rotated keyring entry, say): call refresh() then."""
    global _cache
    with _lock:
        if source is None:
            _sources.pop(name, None)
        else:
            _sources[name] = source
        _cache = None


def _candidate(value: Any) -> str | None:
    """`value` as a secret worth redacting, or None (see the module doc)."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    m = _SCHEME.fullmatch(v)
    if m:
        v = m.group(1)
    if len(v) < MIN_KNOWN_LEN or any(c.isspace() for c in v):
        return None
    if v.lower() in _PLACEHOLDERS or v.startswith("env:") or len(set(v)) < 5:
        return None
    if v.startswith(("//", "\\\\")):
        return None  # a network path; never stat it (that can block)
    if "/" in v or "\\" in v:
        try:
            if os.path.exists(os.path.expanduser(v)):
                return None  # a key FILE's path, not the key
        except (OSError, ValueError):
            pass
    return v


def _env_items() -> list[tuple[str, str]]:
    return sorted((k, v) for k, v in os.environ.items() if _SECRET_NAME.search(k))


def _profile_path() -> Path | None:
    try:
        from evi.profiles import active_profile_name, profile_path, valid_profile_name

        name = active_profile_name()
        return profile_path(name) if name and valid_profile_name(name) else None
    except Exception:  # noqa: BLE001 — redaction never fails on a source
        return None


def _files() -> list[tuple[str, Path]]:
    """(label, path) of eVi's files that hold secrets. TOML ones are labelled
    by dotted field path, JSON ones by `<stem>.<field>`."""
    out = [("toml", _config.CONFIG_PATH)]
    prof = _profile_path()
    if prof is not None:
        out.append(("toml", prof))
    out += [("json", p) for p in (
        _config.BACKENDS_PATH, _config.PEERS_PATH, _config.USERS_PATH, _config.MCP_CONFIG_PATH,
        Path(_config.HOME) / "routines.json",  # evi.routines: each token is a capability
    )]
    return out


# A file written less than this long ago is told apart by its bytes too: a
# same-size rewrite (a rotated token of fixed length) within one tick of the
# file system's clock leaves its modification time and size as they were.
_RECENT_NS = 2_000_000_000


def _stat(path: Path) -> tuple[Any, ...] | None:
    """What tells one version of `path` from the next: its modification time
    and size, plus a hash of its bytes while it is recent (`_RECENT_NS`)."""
    try:
        st = path.stat()
    except OSError:
        return None
    stamp: tuple[Any, ...] = (st.st_mtime_ns, st.st_size)
    if time.time_ns() - st.st_mtime_ns < _RECENT_NS:
        try:
            stamp += (hashlib.sha256(path.read_bytes()).hexdigest(),)
        except OSError:
            pass
    return stamp


def _fingerprint(env: list[tuple[str, str]], paths: list[Path]) -> str:
    h = hashlib.sha256()
    h.update(repr(env).encode("utf-8", "surrogatepass"))
    h.update(repr([(str(p), _stat(p)) for p in paths]).encode("utf-8", "surrogatepass"))
    return h.hexdigest()


# ---- the director's attestation key ---------------------------------------------


def _director_key_file() -> Path | None:
    """Where `evi.attest` keeps the director key when there is no keyring."""
    try:
        from evi import attest

        return attest.key_path()
    except Exception:  # noqa: BLE001 — redaction never fails on a source
        return None


def _director_key() -> bytes | None:
    """The director key, read through `evi.attest` and so only where it reads
    it: never in a process an eVi agent may have started, nor in an
    interactive session. None when no key can exist (no key file and no
    keyring marker): then neither the OS keyring nor the process tree is
    touched, so a user who never made a director key never sees a keychain
    prompt from redaction."""
    try:
        from evi import attest

        if not attest.key_may_exist():
            return None
        return attest._load_key()
    except Exception:  # noqa: BLE001
        logger.debug("redaction: director key unreadable", exc_info=True)
        return None


def _director_values() -> list[tuple[str, str]]:
    """(kind, value) for every form of the director key this process has read.
    Read again when the key file changed since the last read (or after
    refresh()); kept once read, so a process that can no longer read it (the
    REPL, once it marks itself interactive) still redacts it. Caller holds
    _lock."""
    global _director_read_at
    path = _director_key_file()
    stamp = _stat(path) if path is not None else None
    if _director_read_at is _UNREAD or stamp != _director_read_at:
        _director_read_at = stamp
        key = _director_key()
        if key:
            hexed = key.hex()
            _director_forms.update({
                hexed, hexed.upper(),  # as the keyring holds it, and either case printed
                base64.b64encode(key).decode("ascii"),
                base64.urlsafe_b64encode(key).decode("ascii").rstrip("="),
            })
            try:  # `base64 director.key`: the file's own bytes
                from evi import attest

                _director_forms.add(base64.b64encode(attest._FILE_MAGIC + key).decode("ascii"))
            except Exception:  # noqa: BLE001
                pass
    return [("director_key", v) for v in sorted(_director_forms)]


def _secret_fields(data: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    """(key path, value) of every secret-named field in parsed TOML/JSON."""
    if isinstance(data, dict):
        for k, v in data.items():
            key = (*path, str(k))
            if isinstance(v, (dict, list)):
                yield from _secret_fields(v, key)
            elif _SECRET_NAME.search(str(k)):
                yield key, v
    elif isinstance(data, list):
        for v in data:
            yield from _secret_fields(v, path)


def _read(kind: str, path: Path) -> Any:
    try:
        if kind == "toml":
            with path.open("rb") as f:
                return tomllib.load(f)
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing, unreadable, malformed, too deep: skip
        return None


def _build(
    env: list[tuple[str, str]],
    files: list[tuple[str, Path]],
    sources: list[tuple[str, Callable[[], Iterable[tuple[str, str]]]]],
) -> _Known:
    found: dict[str, str] = {}  # secret -> kind; the first source names it

    def add(value: Any, kind: str) -> None:
        v = _candidate(value)
        if v is not None:
            found.setdefault(v, kind)

    for name, value in env:
        add(value, name)
    # One bad source never costs the others (or the redaction) anything.
    for kind, path in files:
        try:
            for key, value in _secret_fields(_read(kind, path)):
                add(value, ".".join(key) if kind == "toml" else f"{path.stem}.{key[-1]}")
        except Exception:  # noqa: BLE001
            logger.debug("redaction: skipped %s", path.name, exc_info=True)
    for name, source in [("director", _director_values), *sources]:
        try:
            for kind, value in source():
                add(value, str(kind))
        except Exception:  # noqa: BLE001
            logger.debug("redaction: source %r failed", name, exc_info=True)
    return _compile(found)


def _compile(found: dict[str, str]) -> _Known:
    kinds: dict[str, str] = {}
    for value, kind in found.items():
        # Also as it reads inside a JSON string (tool-call arguments), so a
        # value with a quote, backslash or non-ASCII character still matches.
        for form in (value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]):
            kinds.setdefault(form, kind)
    if not kinds:
        return _Known(None, {})
    # Longest first: an alternation takes the first branch that matches.
    alts = sorted(kinds, key=len, reverse=True)
    return _Known(re.compile("|".join(map(re.escape, alts))), kinds)


def _known() -> _Known:
    global _cache
    env, files = _env_items(), _files()
    director = _director_key_file()
    fp = _fingerprint(env, [p for _, p in files] + ([director] if director is not None else []))
    with _lock:
        if _cache is not None and _cache[0] == fp:
            return _cache[1]
        try:
            known = _build(env, files, list(_sources.items()))
        except Exception:  # noqa: BLE001 — redaction never fails on a source
            logger.warning("redaction: no known values; token formats only", exc_info=True)
            known = _Known(None, {})
        _cache = (fp, known)
        return known


# ---- redaction ---------------------------------------------------------------


class Redactor:
    """Redacts strings and JSON-like values, tallying what it replaced.

    The known-value set is fixed when the Redactor is made, so make one per
    batch of work (a transcript line, a report), not one per process."""

    def __init__(self, extra: Iterable[str] = (), *, known: bool = True) -> None:
        self.counts: dict[str, int] = {}
        if isinstance(extra, str):
            extra = (extra,)
        found = {v: "secret" for v in map(_candidate, extra or ()) if v is not None}
        self._extra = _compile(found) if found else None
        self._known = _known() if known else None

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def _hit(self, kind: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return marker(kind)

    def __call__(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        before = self.total
        # Known values first, so one that is also a caller's extra or has a
        # token's shape is named for where it came from.
        for table in (self._known, self._extra):
            if table is not None and table.rx is not None:
                text = table.rx.sub(lambda m, t=table: self._hit(t.kinds[m.group(0)]), text)
        for kind, rx in PATTERNS:
            text = rx.sub(lambda m, k=kind: (
                m.string[m.start():m.start("s")] + self._hit(k) + m.string[m.end("s"):m.end()]
            ), text)
        if self.total != before:
            logger.debug("redacted %d secret(s)", self.total - before)
        return text

    def obj(self, value: Any) -> Any:
        """A copy of `value` with every string in it redacted. Dict keys and
        anything that isn't a str, dict, list or tuple are kept as they are."""
        if isinstance(value, str):
            return self(value)
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.obj(v) for v in value)
        return value

    def args(self, text: str) -> str:
        """Tool-call arguments (a JSON string) with every secret replaced.

        Redacted as the value they encode, not as text, so a secret is found
        however JSON escaped it (a token on its own line follows ``\\n``) and
        the result is always JSON again. Keys are redacted too (the model
        chose them), and so is a number holding a known value (it becomes the
        marker string). Then as text as well, for what only the text shows:
        a duplicate key's earlier value (parsing keeps the last one alone)
        and a header beside its credential (``"Authorization": "Bearer x"``).
        Arguments with nothing to redact come back as the very same string,
        byte for byte; ones that aren't JSON (a model's broken call) are
        redacted as text."""
        if not isinstance(text, str) or not text:
            return text
        counts = dict(self.counts)
        try:
            value = self._json(json.loads(text))
        except (ValueError, RecursionError):
            self.counts = counts
            return self(text)
        out = text if self.counts == counts else json.dumps(value, ensure_ascii=False)
        shown = self(out)
        if shown == out:
            return out
        try:
            json.loads(shown)
            return shown
        except (ValueError, RecursionError):
            # The text pass replaced something outside a string (a known value
            # straddling JSON syntax): still JSON, as one string, never the value.
            return json.dumps(shown, ensure_ascii=False)

    def _json(self, value: Any) -> Any:
        if isinstance(value, str):
            return self(value)
        if isinstance(value, dict):
            return {self(k): self._json(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._json(v) for v in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # A PIN or account number sent as a JSON number: as text it would
            # match, so it does here (a marker is a string).
            digits = str(value)
            shown = self(digits)
            return value if shown == digits else shown
        return value

    def tool_calls(self, calls: Any) -> Any:
        """A copy of OpenAI-style ``tool_calls`` with each call's arguments
        redacted (``args``). Ids, types and function names are kept, so a
        call still pairs with its result."""
        if not isinstance(calls, list):
            return self.obj(calls)
        out: list[Any] = []
        for call in calls:
            if not isinstance(call, dict):
                out.append(self.obj(call))
                continue
            copy: dict[str, Any] = {}
            for k, v in call.items():
                if k in ("id", "type", "index"):
                    copy[k] = v
                elif k == "function" and isinstance(v, dict):
                    copy[k] = {
                        fk: (fv if fk == "name"
                             else self.obj(fv) if fk != "arguments"
                             else self.args(fv) if isinstance(fv, str)
                             else self._json(fv))  # already parsed
                        for fk, fv in v.items()
                    }
                else:
                    copy[k] = self.obj(v)
            out.append(copy)
        return out


def redact(text: str, extra: Iterable[str] = ()) -> str:
    """`text` with every secret replaced by ``[REDACTED:<kind>]``."""
    return Redactor(extra)(text)


def redact_obj(obj: Any, extra: Iterable[str] = ()) -> Any:
    """A copy of JSON-like `obj` with every string in it redacted."""
    return Redactor(extra).obj(obj)
