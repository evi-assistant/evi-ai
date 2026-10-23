"""User-defined slash commands — saved markdown prompt templates.

Drop a file at ``~/.evi/commands/<name>.md``; typing ``/<name>`` in the REPL or
web chat sends its (expanded) content as the next user message. Modelled on
Claude Code's custom commands:

- **Frontmatter** (optional YAML-ish block at the top)::

      ---
      description: Draft a conventional-commit message
      argument-hint: [scope]
      model: qwen2.5-coder:14b-instruct-q4_K_M
      ---

  `description` is shown in `/help`; `argument-hint` documents expected args;
  `model` is surfaced for callers that want a per-command model override.
- **Arguments**: ``$ARGUMENTS`` (everything after the name), positional
  ``$1``..``$9`` (shlex-split; an unbalanced quote falls back to a whitespace
  split), and the legacy ``{args}`` (== ``$ARGUMENTS``). Arguments are always
  substituted as plain text.
- **File references**: an ``@path/to/file`` written in the template inlines
  that file's contents (fenced, first 16k chars) if it is a regular UTF-8 file
  inside the working folder or a trusted dir that matches neither a protected
  path nor a key/credential file (``_SECRET_PATHS``) — otherwise the token is
  left untouched, with a logged warning when it names a file that exists. Only
  the template's own refs are read: an ``@path`` that arrives in the arguments
  (typed after ``/name``, sent by an MCP client, or spliced in by ``@$1``)
  stays text. MCP-served prompts never inline.
- **Namespacing**: subdirectories become ``:`` names, e.g.
  ``commands/git/commit.md`` → ``/git:commit``. A command file that isn't
  readable UTF-8 is listed as ``(unreadable: ...)`` and fails on its own,
  without breaking the other commands.

We deliberately do **not** execute ``!bash`` blocks (Claude Code gates those
behind allowed-tools; auto-running shell on expansion is too sharp an edge for
eVi's permission model). If you want triggering metadata or tool gating, that's
a Skill (`evi/skills.py`).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from evi import workdir as _workdir
from evi.config import COMMANDS_DIR, AutoSettings
from evi.permissions import _is_protected

logger = logging.getLogger(__name__)

# Each path segment must be a safe identifier (also blocks traversal).
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# Argument placeholders; group 1 is the positional digit.
_PLACEHOLDER_RE = re.compile(r"\$ARGUMENTS|\{args\}|\$([1-9])")
# One pass over a template: the placeholders, plus `@path` refs (group 2) not
# preceded by a non-space char (so emails like a@b aren't matched). Each match
# is replaced once and never rescanned, so argument text can't become an @ref
# and inlined file text is left verbatim.
_TOKEN_RE = re.compile(_PLACEHOLDER_RE.pattern + r"|(?<!\S)@(\S+)")
_MAX_INLINE_BYTES = 16_000
# Key and credential files a template ref never inlines, whatever `[auto]
# protected_paths` says (that list is aimed at writes, and a user may thin it
# out); these have no business being pasted into a prompt. Matched (ignoring
# case) against each spelling of the file and its basename (`_RefScope.denies`);
# the `*/` twin of a `dir/...` pattern catches that dir nested below a root or
# in an absolute path.
_SECRET_PATHS = (
    # env files, keys and certificates (id_* = every SSH key type, _sk and .pub)
    ".env", "*.env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".ssh/*", "*/.ssh/*", ".gnupg/*", "*/.gnupg/*",
    # login / token stores of common tools
    ".netrc", "_netrc", ".git-credentials", "*/git/credentials", ".pgpass",
    ".my.cnf", ".npmrc", ".yarnrc.yml", ".pypirc", ".vault-token",
    ".cargo/credentials", ".cargo/credentials.toml", "*/.cargo/credentials*",
    ".gem/credentials", "*/.gem/credentials",
    ".docker/config.json", "*/.docker/config.json",
    ".kube/config", "*/.kube/config", "kubeconfig",
    ".config/gh/hosts.yml", "*/.config/gh/hosts.yml",
    # cloud CLIs: AWS, Google Cloud, Azure; Terraform state / vars hold secrets too
    ".aws/credentials", "*/.aws/credentials", ".aws/sso/cache/*", "*/.aws/sso/cache/*",
    ".config/gcloud/*", "*/.config/gcloud/*", "application_default_credentials.json",
    "*service-account*.json", ".azure/*", "*/.azure/*",
    ".terraform.d/credentials.tfrc.json", "*.tfvars", "*.tfstate", "*.tfstate.*",
    # eVi's own config (API keys, channel tokens)
    ".evi/config.toml", "*/.evi/config.toml",
)


class CommandFileError(ValueError):
    """A command's own template file can't be read as UTF-8 text."""


@dataclass(frozen=True)
class SlashCommandEntry:
    name: str            # "commit" or namespaced "git:commit"
    path: Path
    summary: str         # for /help — frontmatter description, else first line
    description: str = ""
    argument_hint: str = ""
    model: str = ""


def safe_mode_store() -> "CommandStore":
    """The store safe mode uses: ``commands/`` inside a fresh private temp
    folder, so no user or plugin command loads. Not the temp folder itself:
    the store scans its root's parent for ``plugins/``, and the system temp
    folder's ``plugins/`` (``/tmp/plugins``) is anyone's to create."""
    import tempfile

    return CommandStore(root=Path(tempfile.mkdtemp(prefix="evi-safe-")) / "commands")


class CommandStore:
    """Loader for ``~/.evi/commands/**/*.md``. Stateless; every call rescans."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else COMMANDS_DIR

    def _scan_roots(self) -> list[tuple[str, Path]]:
        """[(name-prefix, dir)] — the user's commands dir plus each installed
        plugin's commands/ (~/.evi/plugins/<name>/commands/, exposed as
        `<plugin>:<cmd>`)."""
        roots: list[tuple[str, Path]] = []
        if self.root.is_dir():
            roots.append(("", self.root))
        from evi.plugins import plugin_dirs
        for pd in plugin_dirs(self.root.parent):
            cdir = pd / "commands"
            if cdir.is_dir() and _NAME_RE.match(pd.name):
                roots.append((pd.name + ":", cdir))
        return roots

    def list(self) -> list[SlashCommandEntry]:
        out: list[SlashCommandEntry] = []
        for prefix, root in self._scan_roots():
            for p in sorted(root.rglob("*.md")):
                rel = p.relative_to(root).with_suffix("")
                if not all(_NAME_RE.match(part) for part in rel.parts):
                    continue
                if not p.is_file():  # e.g. a directory named `x.md`
                    continue
                out.append(self._entry(prefix + ":".join(rel.parts), p))
        return out

    def get(self, name: str) -> SlashCommandEntry | None:
        # Linear scan over a small set; also keeps path traversal impossible
        # (only files surfaced by list() — already name-validated — can match).
        for entry in self.list():
            if entry.name == name:
                return entry
        return None

    def expand(
        self,
        name: str,
        args: str = "",
        *,
        inline: bool = True,
        workdir: str | Path | None = None,
        trusted_dirs: Iterable[str] = (),
        protected_paths: Iterable[str] | None = None,
    ) -> str | None:
        """Return the command body (frontmatter stripped) with arguments and
        file references substituted, or None if the command doesn't exist.

        Only `@path` refs written in the template are inlined, and only files
        inside `workdir` (default: the session working folder) or one of
        `trusted_dirs` that don't match `protected_paths` (default: the stock
        `[auto] protected_paths`). `inline=False` leaves every ref as text —
        for callers serving the expansion to someone else (MCP prompts/get).

        Raises CommandFileError if the command's own file can't be read."""
        entry = self.get(name)
        if entry is None:
            return None
        try:
            text = entry.path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise CommandFileError(f"{entry.path.name}: {_unreadable(exc)}") from exc
        _, body = _split_frontmatter(text)
        return _substitute(
            body, args, inline=inline, workdir=workdir,
            trusted_dirs=trusted_dirs, protected_paths=protected_paths,
        ).strip()

    def expand_for_agent(self, agent: Any, name: str, args: str = "") -> str | None:
        """`expand` scoped to an agent session: template refs resolve in its
        working folder (`agent.cwd`, else the session working folder) and obey
        its `[auto] trusted_dirs` (which `/add-dir` extends) and
        `protected_paths`. Chat surfaces (web, CLI) expand through this so they
        agree on what a template may read."""
        auto = getattr(getattr(agent, "config", None), "auto", None)
        return self.expand(
            name,
            args,
            workdir=getattr(agent, "cwd", "") or None,
            trusted_dirs=getattr(auto, "trusted_dirs", None) or (),
            protected_paths=getattr(auto, "protected_paths", None),
        )

    # --- internals -------------------------------------------------------

    def _entry(self, name: str, path: Path) -> SlashCommandEntry:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            # Listed (so /help shows why it doesn't work) but no longer able to
            # break every other command's lookup.
            return SlashCommandEntry(
                name=name, path=path, summary=f"(unreadable: {_unreadable(exc)})"
            )
        meta, body = _split_frontmatter(text)
        desc = meta.get("description", "")
        return SlashCommandEntry(
            name=name,
            path=path,
            summary=desc or _first_line(body),
            description=desc,
            argument_hint=meta.get("argument-hint", ""),
            model=meta.get("model", ""),
        )


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter dict, body). No frontmatter → ({}, text)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip().lower()] = val.strip().strip("'\"")
    return meta, text[m.end():]


def _split_args(args: str) -> list[str]:
    """Positional args, shell-style. An unbalanced quote or a trailing backslash
    (`/fix don't panic`) falls back to a whitespace split instead of failing."""
    if not args.strip():
        return []
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


def _substitute(
    body: str,
    args: str,
    *,
    inline: bool = True,
    workdir: str | Path | None = None,
    trusted_dirs: Iterable[str] = (),
    protected_paths: Iterable[str] | None = None,
) -> str:
    parts = _split_args(args)
    scope: _RefScope | None = None  # built on the first template ref

    def arg(m: "re.Match[str]") -> str:
        if m.group(1):  # $1..$9 — missing positional → blank
            i = int(m.group(1))
            return parts[i - 1] if i <= len(parts) else ""
        return args  # $ARGUMENTS / {args}

    def token(m: "re.Match[str]") -> str:
        nonlocal scope
        ref = m.group(2)
        if ref is None:
            return arg(m)
        if _PLACEHOLDER_RE.search(ref):
            # `@$1` and the like: the path would come from the arguments, so
            # splice them in as text and never read it.
            return "@" + _PLACEHOLDER_RE.sub(arg, ref)
        if inline:
            if scope is None:
                scope = _RefScope.build(workdir, trusted_dirs, protected_paths)
            text = _read_ref(ref, scope)
            if text is not None:
                return f"\n```\n{text}\n```\n"
        return m.group(0)  # not an inlinable file → leave the @token as-is

    return _TOKEN_RE.sub(token, body)


@dataclass(frozen=True)
class _RefScope:
    """Where a template's `@refs` may read from, built once per expansion."""

    root: Path  # relative refs resolve here
    lexical: tuple[Path, ...]  # the working folder + trusted dirs, absolute
    resolved: tuple[Path, ...]  # ...and with symlinks followed
    deny: tuple[str, ...]  # lower-cased protected + secret patterns

    @classmethod
    def build(
        cls,
        workdir: str | Path | None,
        trusted_dirs: Iterable[str],
        protected_paths: Iterable[str] | None,
    ) -> _RefScope:
        root = Path(workdir) if workdir else _workdir.get_cwd()
        lexical: list[Path] = []
        resolved: list[Path] = []
        for d in (root, *(t for t in trusted_dirs if t)):
            try:
                lex = Path(os.path.abspath(Path(d).expanduser()))
                resolved.append(lex.resolve())
                lexical.append(lex)
            except (OSError, ValueError, RuntimeError):
                continue
        pats = AutoSettings().protected_paths if protected_paths is None else protected_paths
        deny: list[str] = []
        for pat in (*pats, *_SECRET_PATHS):
            if pat:
                deny.append(pat.lower())
                if pat.startswith("~"):  # `~/.evi/**` names an absolute path
                    deny.append(os.path.expanduser(pat).lower())
        return cls(root, tuple(lexical), tuple(resolved), tuple(deny))

    def contains(self, real: Path) -> bool:
        return any(real == r or real.is_relative_to(r) for r in self.resolved)

    def denies(self, ref: str, lex: Path, real: Path) -> bool:
        """Match every spelling of the file — as written, normalised, absolute,
        and relative to each root — case-insensitively (a case-insensitive disk
        opens `ID_ED25519` as `id_ed25519`), so `./x`, `a/../x` and absolute
        refs can't slip past a `dir/*` pattern."""
        names = [ref, os.path.normpath(ref), str(lex), str(real)]
        for base in (*self.lexical, *self.resolved):
            for path in (lex, real):
                if path == base or path.is_relative_to(base):
                    names.append(path.relative_to(base).as_posix())
        return _is_protected([n.lower() for n in names], self.deny)


def _is_network_path(p: Path) -> bool:
    """A UNC (`\\\\host\\share`) or device (`\\\\?\\`, `\\\\.\\`) path — Windows only."""
    return p.drive.startswith(("\\\\", "//"))


def _read_ref(ref: str, scope: _RefScope) -> str | None:
    """The (capped) text of a template's `@ref`, or None unless it is a regular
    UTF-8 file inside the working folder or a trusted dir that matches no
    protected or secret path. Relative refs resolve against the working folder."""
    try:
        p = Path(ref).expanduser()
        if not p.is_absolute():
            p = scope.root / p
        lex = Path(os.path.abspath(p))  # lexical only: no filesystem access yet
    except (OSError, ValueError, RuntimeError):  # bad path / unknown ~user
        return None
    if _is_network_path(lex) and lex.drive.lower() not in {
        r.drive.lower() for r in (*scope.lexical, *scope.resolved)
    }:
        # Refuse before resolve(): resolving would already connect to the host.
        _refused(ref, "a network or device path outside the working folder")
        return None
    try:
        real = p.resolve()  # follow symlinks before the containment check
    except (OSError, ValueError, RuntimeError):
        return None
    if not scope.contains(real):
        if _exists(real):
            _refused(ref, "outside the working folder and [auto] trusted_dirs")
        return None
    if not real.is_file():  # missing, a directory, or a FIFO/device (open could block)
        return None
    if scope.denies(ref, lex, real):
        _refused(ref, "a protected path or a key/credential file")
        return None
    try:
        with real.open(encoding="utf-8") as f:
            return f.read(_MAX_INLINE_BYTES)  # never reads (or decodes) the rest
    except (OSError, ValueError):  # unreadable or not UTF-8 text
        return None


def _exists(p: Path) -> bool:
    try:
        return p.exists()
    except (OSError, ValueError):
        return False


def _refused(ref: str, why: str) -> None:
    logger.warning("command template ref @%s not inlined: %s", ref, why)


def _unreadable(exc: BaseException) -> str:
    if isinstance(exc, UnicodeDecodeError):
        return "not UTF-8 text"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc)


def _first_line(text: str) -> str:
    """First prose line of the body (markdown header as fallback)."""
    header: str | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if header is None:
                header = stripped.lstrip("#").strip()
            continue
        return stripped[:160]
    return header[:160] if header else "(no summary)"
