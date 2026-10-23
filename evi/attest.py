"""Director attestation: only the director marks a task verified or rejected.

eVi never marks its own work verified (docs/roadmap.md, Phase 1). A task's
verdict counts only when it carries an *attestation*: an HMAC-SHA256, under a
key only the director holds, over the task record, the branch head, the
evidence bundle's hash, the verdict, the review rounds and the review gate's
result. A task whose TOML says ``status = "verified"`` without one, or one
edited (or whose evidence changed) after it was signed, is reported as
unverified (`status_label`), and no Verified-by trailer is rendered for it
(`trailers`).

The director credential
    32 random bytes, made once by `init_key`. Kept in the OS keyring (service
    ``evi-director``) when the optional ``keyring`` package is importable and
    its backend works, else in ``<eVi home>/director/director.key``: a
    user-only file (0600 in a 0700 folder on POSIX; on Windows its ACL is cut
    down to the current user, best effort) whose first byte is not UTF-8, so
    text tools (``search_files``, ``grep``) skip it as binary. A home other
    than ``~/.evi`` (``EVI_HOME``) gets a keyring entry of its own. It is
    never an environment variable: ``EVI_DIRECTOR`` means nothing to eVi.

Who may sign (`authorize`)
    `sign` and `init_key` go ahead only when no eVi agent may have started
    the process (`agent_parent`): no ``EVI_AGENT`` in its environment (what
    eVi sets for the processes its agents run), no ``EVI_INTERACTIVE=1``
    (inherited from an ``evi chat`` REPL), and no eVi process above it past
    the launchers of this same command (``evi chat``, ``evi serve``, the
    dev uvicorn sidecar, the desktop app's frozen ``evi-server`` and
    ``evi-desktop`` ...); and when the process itself is director
    tooling (`agent_context`: its own command line is ``evi task
    verify|reject`` or ``evi attest ...`` as
    `evi.shell_guard.is_director_argv` reads it, so this module called from
    ``python -c`` or ``run_python`` is refused). Then the director confirms on
    a terminal, or passes ``yes=True`` (``--yes``) from their own shell.

    The key itself is never read in a process `agent_parent` objects to:
    there `verify` reports `NO_KEY` (the task shows as unverified) and
    `key_id` is None. The director checks from their own shell. (Tests that
    sign or verify stub `_process_chain` and clear ``EVI_AGENT`` /
    ``EVI_INTERACTIVE``, as tests/test_attest.py does, so they also pass when
    an eVi agent runs the suite.)

Defence in depth for agents
    `evi.permissions.decide` denies, in every mode (yolo and ``/auto on``
    included) and whatever rule matches, any tool call whose path argument is
    in the credential folder (or is a device path that may reach it), and any
    shell line or ``run_python`` code that names the credential
    (``evi-director``, ``.evi/director``, ``director.key``), sets or clears
    ``EVI_DIRECTOR`` / ``EVI_AGENT``, imports or patches this module
    (``from evi import attest``, ``_process_chain`` ...), or runs director
    tooling, as a command or as what another program is handed to run
    (``schtasks /tr "..."``, ``ssh host ...``, ``find -exec``, a git ``!``
    alias; `evi.shell_guard.director_hit`). A mention that is only data (a
    grep pattern, a commit message, an ``echo``, a heredoc body nothing
    runs) is left alone. The task files themselves (``.evi/tasks/**``) are
    write-protected like the rest of ``.evi/**``, and the signature is what
    makes a hand-edit show: an agent can write any TOML it likes, it can't
    sign it.

Limits
    An agent runs as the same OS user as the director, so code it runs (a
    script it writes and then runs, or code the checks above don't
    recognise) can do what this module does: pop ``EVI_AGENT``, stub
    `_process_chain` or `_key_or_reason`, or read the key file or keyring
    entry directly, and then sign a "verified" attestation that verifies.
    The checks above stop the direct routes and keep the rest from
    happening by accident or unnoticed; they don't stop an agent set on it.
    Full isolation needs the agent under a separate OS account (or a
    director key that needs a secret the agent never sees to sign with).

Contract for the task queue, the evidence bundle and their CLI (evi/tasks.py
and ``evi task ...``; later evi/evidence.py and ``evi evidence ...``)

- A task is a TOML table with at least ``id`` and ``status``. The signature
  covers EVERY field of the record except the ``attestation`` table, so
  anything that may change after verification must live outside the record
  (or be re-signed). Values must survive the TOML round trip (str, int,
  float, bool, lists, tables, dates).
- Evidence bundle paths are absolute (or resolved against the repository
  by the caller): `evidence_hash` reads a relative one against the current
  folder. ``""`` (no evidence) hashes to ``""``.
- ``evi task verify T1`` (and ``reject``): ``head = branch_head(...)``,
  ``ev = evidence_hash(<bundle>)``, for a verify
  ``res = review_gate(base, cwd=...)`` (``evi review --multi --exit-code`` on
  the branch diff), then ``signed = sign(record, head=head,
  evidence_hash=ev, verdict="verified", rounds=n, review=res,
  override_reason=reason, signer=..., yes=yes, confirm=...)`` and write
  ``signed`` back as is (it is a copy with ``status`` set to the verdict and
  the ``attestation`` table added). `sign` raises `ReviewBlocked` when the
  review reports blocking findings and no override reason was given; the
  reason, when given, is recorded in the signed block.
- ``evi task show``: ``status_label(record, evidence_hash=ev,
  head=branch_head(record["branch"]))`` (or `verify` for the details).
  ``head`` is required: a branch that moved on after signing (or an older
  verified TOML restored from git history) reads ``unverified (stale ...)``.
  Pass ``head=None`` only when the task's branch no longer exists (merged
  and deleted). A status that reads as "verified" / "rejected" (any case,
  look-alike letters, invisible characters) without a valid signature reads
  ``unverified (unsigned)``; a status that isn't plain ASCII reads
  ``unverified (unrecognised status ...)``.
- ``evi task list`` and other views over many tasks: one `Verifier` for the
  whole list (it reads the key, and walks the process tree, once).
- Any transition out of "verified" / "rejected" (reopening a task for
  rework) writes ``drop_attestation(record)``: the old signature no longer
  applies. A task that still carries one reads ``<status> (an earlier
  attestation no longer applies)``.
- ``evi evidence commit-body``: ``trailers(record, evidence_hash=ev,
  head=branch_head(record["branch"]))`` gives the Verified-by,
  Review-rounds and Attestation lines, or none without a valid "verified"
  attestation at that head (``head`` is required: a moved branch gives
  none).
- ``evi stats --provenance``: ``verify_trailers(commit_message)`` checks the
  signed Attestation trailer against this machine's key without the task
  TOML or the evidence bundle (bundles expire); only a `VALID` one counts.
  It is an HMAC, so it can only be checked where the director key is (not
  in CI), and rotating the key voids old trailers too. The trailer names
  the task and the head it was verified at; whether that head is in the
  commit's history is the caller's to check.
- Director commands must be spelled ``evi task verify|reject ...`` or
  ``evi attest ...`` (e.g. a key-setup ``evi attest init``): those are the
  forms the shell guard refuses in agent shells and the only command lines
  `authorize` signs from.
- Processes eVi's agents run carry ``EVI_AGENT=1`` (`AGENT_ENV`): evi/proc.py
  sets it for what it spawns (the shell / code / monitor tools, the CLI-agent
  drivers), the spawners that build an environment of their own (hooks, MCP
  stdio servers, the linter and OCR runners, the Claude Agent SDK) set it
  too, and eVi's agent-hosting entry points set it in their own process at
  startup (`evi.proc.mark_agent_process`), so everything else they start
  inherits it. Director tooling never sets it. A process that drops it and
  is detached from eVi's tree escapes both this and the process-tree check.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, unquote

VERSION = 1
# The OS keyring entry, and the file fallback under eVi's home.
KEYRING_SERVICE = "evi-director"
KEYRING_USER = "attestation-key"
DIRECTOR_DIRNAME = "director"
KEY_FILENAME = "director.key"
# A non-secret marker (the key id) written next to where the key file would be
# when the key lives in the keyring, so code that only needs to know whether a
# director key exists (evi.redact) never queries the OS keyring otherwise: on
# macOS that query can raise a Keychain prompt for users who never made a key.
KEYRING_MARKER = "in-keyring"
# Set (to "1") in the environment of every process an eVi agent runs; its
# presence alone makes `authorize` refuse.
AGENT_ENV = "EVI_AGENT"
# Never read: the director is whoever holds the key, not whoever sets a flag.
# Named here so the shell guard and the docs can refer to it.
DIRECTOR_ENV = "EVI_DIRECTOR"
ATTESTATION_KEY = "attestation"
VERDICTS = ("verified", "rejected")

# `verify` states.
VALID = "valid"
UNSIGNED = "unsigned"
INVALID = "invalid"
NO_KEY = "no-key"
STALE = "stale"

_KEY_BYTES = 32
# 0xFF never occurs in UTF-8, so a text reader gives up on the file.
_FILE_MAGIC = b"\xffEVI-DIRECTOR-KEY-1\n"
_MAC_DOMAIN = b"evi-attest-v1\n"
_TRAILER_DOMAIN = b"evi-attest-trailer-v1\n"
_KEY_ID_DOMAIN = b"evi-attest-key-id\n"
# The signed fields of an attestation table, besides "v" and "sig".
_FIELDS = (
    "task", "digest", "head", "evidence", "verdict", "rounds", "review",
    "signer", "signed_at", "key",
)
_STR_FIELDS = ("task", "digest", "head", "evidence", "verdict", "signer", "signed_at", "key")
_HEX_SIG = re.compile(r"[0-9a-f]{64}\Z")
_HEX_KEY_ID = re.compile(r"[0-9a-f]{16}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# A task status shown as it is: plain ASCII, as the task queue writes them
# ("open", "needs_review" ...). Anything else could hide a look-alike.
_PLAIN_STATUS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
# A module path (`evi.apps.web.server:app`, `-m evi.apps.cli.main`) on an
# ancestor's command line: an eVi process run other than as `evi ...`.
_EVI_MODULE = re.compile(r"evi\.[\w.]+(?::[\w.]+)?\Z")
# eVi's frozen binaries (`shell_guard` base names): the desktop app's sidecar
# server (`<resources>/evi-server/evi-server.exe --host H --port P`) and the
# desktop shell itself.
_EVI_BINARIES = frozenset({"evi-server", "evi-desktop"})
_MAX_ANCESTORS = 32
# The commit trailers `trailers` renders and `verify_trailers` reads.
_TRAILER_LINE = re.compile(r"(Verified-by|Review-rounds|Attestation):[ \t]*(.*?)[ \t]*\Z", re.I)
_VERIFIED_BY = re.compile(r"(.+?)\s*\(director key ([0-9a-f]{16})\)\Z")


class AttestError(RuntimeError):
    """Signing, verifying setup or key management can't go ahead."""


class AgentRefused(AttestError):
    """Director tooling was run from an agent (or can't be told apart from one)."""


class NotConfirmed(AttestError):
    """The director did not confirm (or there was no terminal to confirm on)."""


class ReviewBlocked(AttestError):
    """`evi review --multi --exit-code` reported blocking findings and no
    override reason was given."""


# ---- the review gate --------------------------------------------------------


@dataclass(frozen=True)
class ReviewResult:
    """What ``evi review --multi --exit-code`` said about a branch diff."""

    exit_code: int
    base: str
    report: str = ""

    @property
    def blocking(self) -> bool:
        return self.exit_code != 0

    def summary(self, override_reason: str = "") -> dict[str, Any]:
        """The signed record of this result (the report itself by hash)."""
        out: dict[str, Any] = {
            "base": self.base,
            "exit_code": int(self.exit_code),
            "report_sha256": hashlib.sha256(self.report.encode("utf-8")).hexdigest(),
        }
        if override_reason:
            out["override"] = override_reason
        return out


def review_gate(
    base: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    reviewer: Callable[..., str] | None = None,
) -> ReviewResult:
    """Run ``evi review --branch <base> --multi --exit-code`` in-process: the
    multi-lens review (`evi.review.multi_review`, with the repo's review
    context) of ``<base>...HEAD`` in `cwd`, gated by `evi.review.review_exit_code`.
    No changes is a clean result. `reviewer` replaces `multi_review` (tests).
    Raises `evi.review.ReviewError` when the diff can't be had."""
    from evi import review

    diff = review.get_diff(branch=base, cwd=cwd)
    if not diff.strip():
        return ReviewResult(0, base, "(no changes to review)")
    context = review.load_review_context(Path(cwd) if cwd else None)
    if reviewer is not None:
        report = reviewer(diff, context=context)
    else:  # the reviewers read the tree the diff is of, not the session's folder
        report = review.multi_review(diff, context=context, cwd=cwd)
    return ReviewResult(review.review_exit_code(report), base, report)


# ---- what is signed ---------------------------------------------------------


def _json_default(value: Any) -> str:
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    raise TypeError(f"can't sign a {type(value).__name__} value")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=True, default=_json_default,
    ).encode("utf-8")


def task_digest(record: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the task record without its ``attestation`` table,
    over its parsed values (so re-formatting the TOML changes nothing, any
    edit to a value does). Raises TypeError for a value TOML can't hold."""
    body = {k: v for k, v in record.items() if k != ATTESTATION_KEY}
    return "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()


def _hash_file(path: Path) -> bytes:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def evidence_hash(path: str | os.PathLike[str] | None) -> str:
    """``sha256:<hex>`` of an evidence bundle: one file's bytes, or every file
    under a folder (its relative path and content; a link by its target,
    never followed). "" for no evidence (None, "", or a path that isn't
    there), so evidence deleted after signing reads as changed. `path` is
    absolute: a relative one is read against the current folder."""
    if path is None or not str(os.fspath(path)).strip():
        return ""  # never Path("") == ".": that would hash the current folder
    root = Path(path)
    if not root.exists() and not root.is_symlink():
        return ""
    h = hashlib.sha256()
    if root.is_file() and not root.is_symlink():
        h.update(b"file\0" + _hash_file(root))
        return "sha256:" + h.hexdigest()
    h.update(b"dir\0")
    entries: list[tuple[str, Path]] = []
    if root.is_symlink():
        entries.append((".", root))
    else:
        for folder, dirs, files in os.walk(root, followlinks=False):
            here = Path(folder)
            for name in [*files, *(d for d in dirs if (here / d).is_symlink())]:
                full = here / name
                entries.append((full.relative_to(root).as_posix(), full))
    for rel, full in sorted(entries):
        h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
        if full.is_symlink():
            h.update(b"link\0" + os.readlink(full).encode("utf-8", "surrogateescape") + b"\0")
        else:
            h.update(b"file\0" + _hash_file(full))
    return "sha256:" + h.hexdigest()


def branch_head(ref: str = "HEAD", *, cwd: str | os.PathLike[str] | None = None) -> str:
    """The commit `ref` names in the repository at `cwd` (full sha), read
    with eVi's hardened git (`evi.gitsafe`)."""
    import subprocess

    from evi import gitsafe

    try:
        safe = gitsafe.validate_ref(ref)
        proc = gitsafe.run_git(
            ["rev-parse", "--verify", "--quiet", safe + "^{commit}"], cwd=cwd, timeout=30,
        )
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise AttestError(f"can't read {ref!r}: {exc}") from exc
    head = proc.stdout.strip()
    if proc.returncode != 0 or not head:
        raise AttestError(f"{ref!r} names no commit here")
    return head


# ---- the key ----------------------------------------------------------------


def _home() -> Path:
    from evi import config

    return Path(config.HOME)


def key_path() -> Path:
    """Where the key file goes when there is no usable keyring."""
    return _home() / DIRECTOR_DIRNAME / KEY_FILENAME


def keyring_marker() -> Path:
    """The marker saying the director key is in the keyring (holds its id)."""
    return _home() / DIRECTOR_DIRNAME / KEYRING_MARKER


def key_may_exist() -> bool:
    """Whether a director key may exist, without touching the OS keyring: a
    key file, or the marker `init_key` leaves when it stores the key there."""
    return key_path().is_file() or keyring_marker().is_file()


def _keyring() -> Any:
    """The ``keyring`` module, or None when it isn't installed (it is an
    optional dependency; eVi never requires it)."""
    try:
        import keyring  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — ImportError, or a backend that fails to load
        return None
    return keyring


def _keyring_user() -> str:
    """The keyring entry for this eVi home: ``attestation-key`` for
    ``~/.evi``, one of its own for any other home (a test's, a second
    install's), so it never reads or replaces the real home's key."""
    home = _home()
    try:
        mine = os.path.normcase(str(home.resolve()))
        default = os.path.normcase(str((Path.home() / ".evi").resolve()))
    except (OSError, RuntimeError):
        mine, default = os.path.normcase(str(home)), ""
    if mine == default:
        return KEYRING_USER
    return f"{KEYRING_USER}@{hashlib.sha256(mine.encode('utf-8')).hexdigest()[:16]}"


def _decode_key(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        key = bytes.fromhex(value.strip())
    except ValueError:
        return None
    return key if len(key) == _KEY_BYTES else None


def _keyring_get() -> bytes | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return _decode_key(kr.get_password(KEYRING_SERVICE, _keyring_user()))
    except Exception:  # noqa: BLE001 — no backend, locked, denied
        return None


def _keyring_set(key: bytes) -> bool:
    """Store `key` in the keyring; True only if it reads back."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(KEYRING_SERVICE, _keyring_user(), key.hex())
        return _keyring_get() == key
    except Exception:  # noqa: BLE001
        return False


def _file_get() -> bytes | None:
    try:
        data = key_path().read_bytes()
    except OSError:
        return None
    if not data.startswith(_FILE_MAGIC) or len(data) != len(_FILE_MAGIC) + _KEY_BYTES:
        return None
    return data[len(_FILE_MAGIC):]


def _restrict(path: Path, *, folder: bool = False) -> None:
    """Make `path` the current user's only: 0700 / 0600 on POSIX; on Windows
    grant the user full control (inherited by what a folder holds), then drop
    the inherited entries, only once the grant worked so the owner never
    loses access. ``icacls`` is run by its full path: the director's working
    folder may be a repository with an ``icacls.exe`` of its own. Best effort."""
    if os.name != "nt":
        os.chmod(path, 0o700 if folder else 0o600)
        return
    import getpass
    import subprocess

    icacls = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        user = getpass.getuser()
        domain = os.environ.get("USERDOMAIN", "")
        who = f"{domain}\\{user}" if domain else user
        grant = f"{who}:(OI)(CI)F" if folder else f"{who}:F"
        granted = subprocess.run(
            [icacls, str(path), "/grant:r", grant],
            capture_output=True, timeout=30, creationflags=flags,
        ).returncode == 0
        if granted:
            subprocess.run(
                [icacls, str(path), "/inheritance:r"],
                capture_output=True, timeout=30, creationflags=flags,
            )
    except Exception:  # noqa: BLE001 — no user name, no icacls, a timeout: leave it
        pass


def _file_set(key: bytes) -> Path:
    path = key_path()
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict(folder, folder=True)
    tmp = folder / f".{KEY_FILENAME}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(_FILE_MAGIC + key)
            fh.flush()
            os.fsync(fh.fileno())
        _restrict(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def _key_or_reason() -> tuple[bytes | None, str]:
    """The director key (the keyring's when it holds one, else the file's),
    or None and why: never in a process an eVi agent may have started
    (`agent_parent`), so ``python -c "from evi import attest; ..."`` run by
    an agent gets nothing to sign with."""
    why = agent_parent()
    if why:
        return None, f"the director key isn't read here: {why}"
    key = _keyring_get() or _file_get()
    if key is None:
        return None, "there is no director key on this machine"
    return key, ""


def _load_key() -> bytes | None:
    return _key_or_reason()[0]


def key_id_of(key: bytes) -> str:
    """A short public name for `key` (it reveals nothing about it)."""
    return hashlib.sha256(_KEY_ID_DOMAIN + key).hexdigest()[:16]


def key_id() -> str | None:
    """This machine's director key id, or None when there is no key (or this
    process may be an agent's)."""
    key = _load_key()
    return key_id_of(key) if key is not None else None


def key_location() -> str:
    """Where the director key lives ("" when there is none, or this process
    may be an agent's)."""
    if agent_parent():
        return ""
    if _keyring_get() is not None:
        return f"OS keyring (service {KEYRING_SERVICE!r}, entry {_keyring_user()!r})"
    if _file_get() is not None:
        return str(key_path())
    return ""


def init_key(
    *,
    rotate: bool = False,
    yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
) -> str:
    """Create the director key (in the keyring when it works, else the
    user-only file) and return its id. Refuses when one exists unless
    `rotate`, which makes every attestation signed so far invalid. Director
    tooling only (`authorize`)."""
    existing = _load_key()
    if existing is not None and not rotate:
        raise AttestError(
            f"a director key already exists (key id {key_id_of(existing)}, in "
            f"{key_location()}); rotating it makes every attestation signed so far invalid"
        )
    authorize(
        "replace the director key (every attestation signed so far becomes invalid)"
        if existing is not None else "create the director key",
        yes=yes, confirm=confirm,
    )
    key = secrets.token_bytes(_KEY_BYTES)
    if _keyring_set(key):
        try:
            key_path().unlink()  # an older file key must not linger
        except OSError:
            pass
        marker = keyring_marker()
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.write_text(key_id_of(key) + "\n", encoding="utf-8")
    elif _keyring_get() is not None:
        raise AttestError(
            "the OS keyring holds a director key eVi can't replace; remove the "
            f"{KEYRING_SERVICE!r} entry there first"
        )
    else:
        _file_set(key)
        try:
            keyring_marker().unlink()  # the key is in the file now
        except OSError:
            pass
    return key_id_of(key)


# ---- who may sign -----------------------------------------------------------


def _process_chain() -> list[tuple[int, list[str]]]:
    """``(pid, command line)`` of this process and its ancestors, nearest
    first; an unreadable command line is ``[]``."""
    import psutil

    out: list[tuple[int, list[str]]] = []
    try:
        proc = psutil.Process()
    except (psutil.Error, OSError):
        return out
    seen: set[int] = set()
    while proc is not None and proc.pid not in seen and len(out) <= _MAX_ANCESTORS:
        seen.add(proc.pid)
        try:
            cmdline = list(proc.cmdline())
        except (psutil.Error, OSError):
            cmdline = []
        out.append((proc.pid, cmdline))
        try:
            proc = proc.parent()  # psutil checks the parent is older (no reused pid)
        except (psutil.Error, OSError):
            break
    return out


def _is_evi_process(cmdline: Sequence[str]) -> bool:
    from evi import shell_guard, shellsplit

    if shell_guard.evi_args(cmdline) is not None:
        return True
    if cmdline and shellsplit._base(cmdline[0]) in _EVI_BINARIES:
        return True
    return any(_EVI_MODULE.match(w) for w in cmdline[1:])


def _shown(cmdline: Sequence[str]) -> str:
    text = " ".join(cmdline) or "(unreadable)"
    return text if len(text) <= 120 else text[:117] + "..."


def _agent_parent(env: Mapping[str, str], chain: list[tuple[int, list[str]]]) -> str | None:
    from evi import shell_guard

    if AGENT_ENV in env:
        return f"{AGENT_ENV} is set, so an eVi agent started this process"
    if env.get("EVI_INTERACTIVE") == "1":
        return "this runs inside an eVi chat session (EVI_INTERACTIVE=1)"
    if not chain or not chain[0][1]:
        return "this process's command line can't be read, so it can't be told apart from an agent's"
    own_args = shell_guard.evi_args(chain[0][1])
    launchers = True
    for pid, cmdline in chain[1:]:
        # The console-script launcher (evi.exe), the venv's python.exe
        # redirector, `uv run evi ...` and the like run this very command:
        # skip them, then nothing above may be eVi.
        if launchers and own_args is not None and shell_guard.evi_args(cmdline) == own_args:
            continue
        launchers = False
        if cmdline and _is_evi_process(cmdline):
            return f"an eVi process runs above this one (pid {pid}: `{_shown(cmdline)}`)"
    return None


def agent_parent(env: Mapping[str, str] | None = None) -> str | None:
    """Why an eVi agent may have started this process, or None: ``EVI_AGENT``
    in its environment, ``EVI_INTERACTIVE=1`` (inherited from, or set by, an
    ``evi chat`` REPL), an unreadable command line, or an eVi process above it
    (past the launchers of this same command). Such a process never reads the
    director key."""
    return _agent_parent(os.environ if env is None else env, _process_chain())


def agent_context(env: Mapping[str, str] | None = None) -> str | None:
    """Why director tooling must refuse to run in this process, or None:
    `agent_parent`, or the process itself isn't director tooling (its own
    command line is not ``evi task verify|reject`` / ``evi attest ...``)."""
    from evi import shell_guard

    chain = _process_chain()
    why = _agent_parent(os.environ if env is None else env, chain)
    if why:
        return why
    own = chain[0][1]
    if not shell_guard.is_director_argv(own):
        return (
            "only director tooling signs (`evi task verify|reject`, `evi attest ...`); "
            f"this process runs `{_shown(own)}`"
        )
    return None


def _has_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):  # replaced or closed streams
        return False


def _tty_confirm(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def authorize(
    action: str,
    *,
    yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Raise unless the director may `action` here: never in an agent's
    process (`agent_context`, `AgentRefused`), and only after a confirmation
    on a terminal (`confirm`, default a y/N prompt) or with `yes` (``--yes``,
    from the director's own shell) — else `NotConfirmed`."""
    why = agent_context(env)
    if why:
        raise AgentRefused(f"refusing to {action}: {why}. Only the director, from their own shell, may.")
    if yes:
        return
    if not _has_tty():
        raise NotConfirmed(
            f"refusing to {action}: there is no terminal to confirm on; run it in a "
            "terminal, or pass --yes from the director's own shell"
        )
    try:
        ok = bool((confirm or _tty_confirm)(f"{action[:1].upper()}{action[1:]}?"))
    except (EOFError, KeyboardInterrupt):
        ok = False
    if not ok:
        raise NotConfirmed(f"not confirmed: {action}")


# ---- sign / verify ----------------------------------------------------------


def _mac(key: bytes, fields: Mapping[str, Any]) -> str:
    body = {"v": VERSION, **{f: fields[f] for f in _FIELDS}}
    return hmac.new(key, _MAC_DOMAIN + _canonical(body), hashlib.sha256).hexdigest()


def _one_line(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AttestError(f"{what} is empty")
    if _CONTROL.search(text):
        raise AttestError(f"{what} holds a control character (a newline?)")
    return text


def sign(
    record: Mapping[str, Any],
    *,
    head: str,
    evidence_hash: str,
    verdict: str,
    rounds: int,
    review: ReviewResult | None = None,
    override_reason: str = "",
    signer: str = "director",
    yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Sign `verdict` ("verified" / "rejected") for the task `record`, and
    return a copy of it with ``status`` set to the verdict and an
    ``attestation`` table added, to be written back as is.

    The HMAC covers the task id, the digest of every other field of the
    record (`task_digest`), the branch `head`, the `evidence_hash`, the
    verdict, the review `rounds`, the review gate's result (`review`, with
    `override_reason`), the `signer`, the time and the key id. A verify needs
    `review` (`review_gate`): when it is blocking, `ReviewBlocked` is raised
    unless `override_reason` says why to sign anyway, and that reason is
    signed with it. Director tooling only (`authorize`); raises `AttestError`
    without a director key."""
    task_id = _one_line(record.get("id"), "the task id")
    if verdict not in VERDICTS:
        raise AttestError(f"unknown verdict {verdict!r} (one of {', '.join(VERDICTS)})")
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 0:
        raise AttestError(f"review rounds must be a whole number >= 0, not {rounds!r}")
    head = _one_line(head, "the branch head")
    if not isinstance(evidence_hash, str) or _CONTROL.search(evidence_hash):
        raise AttestError("the evidence hash must be a single-line string")
    signer = _one_line(signer, "the signer")
    override = str(override_reason or "").strip()
    if _CONTROL.search(override.replace("\n", " ").replace("\t", " ")):
        raise AttestError("the override reason holds a control character")
    if verdict == "verified":
        if review is None:
            raise AttestError(
                "a verify needs the review gate's result (`review_gate`: "
                "`evi review --multi --exit-code` on the branch diff)"
            )
        if review.blocking and not override:
            raise ReviewBlocked(
                f"`evi review --multi --exit-code` reported blocking findings (exit "
                f"{review.exit_code}) on the diff against {review.base!r}; fix them, or "
                "give an override reason (it is recorded in the attestation)"
            )
    body = {k: v for k, v in record.items() if k != ATTESTATION_KEY}
    body["status"] = verdict
    try:
        digest = task_digest(body)
    except (TypeError, ValueError) as exc:
        raise AttestError(f"the task record can't be signed: {exc}") from exc
    gate = (
        f"review exit {review.exit_code} against {review.base!r}" if review is not None
        else "no review"
    )
    authorize(
        f"sign task {task_id} as {verdict} at {head[:12]} (evidence "
        f"{evidence_hash[:19] or 'none'}, {rounds} review round(s), {gate}"
        + (f", override: {override}" if override else "") + ")",
        yes=yes, confirm=confirm,
    )
    key, why = _key_or_reason()
    if key is None:
        raise AttestError(f"{why} (`init_key` creates one)")
    when = (now or _dt.datetime.now(_dt.timezone.utc)).astimezone(_dt.timezone.utc)
    if review is not None:
        gate_record = review.summary(override)
    else:
        gate_record = {"override": override} if override else {}
    block: dict[str, Any] = {
        "v": VERSION,
        "task": task_id,
        "digest": digest,
        "head": head,
        "evidence": evidence_hash,
        "verdict": verdict,
        "rounds": rounds,
        "review": gate_record,
        "signer": signer,
        "signed_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "key": key_id_of(key),
    }
    block["sig"] = _mac(key, block)
    body[ATTESTATION_KEY] = block
    return body



def drop_attestation(record: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the task `record` without its ``attestation`` table: what a
    transition out of "verified" / "rejected" (reopening the task for
    rework) writes, as the old signature no longer applies to it."""
    return {k: v for k, v in record.items() if k != ATTESTATION_KEY}


@dataclass(frozen=True)
class Check:
    """What `verify` found. `ok` only for a valid signature over the record
    as it is now, its evidence and (when given) the current head."""

    state: str  # VALID | UNSIGNED | INVALID | NO_KEY | STALE
    reason: str = ""
    verdict: str = ""
    signer: str = ""
    rounds: int = 0
    key: str = ""
    head: str = ""  # the head it was signed at

    @property
    def ok(self) -> bool:
        return self.state == VALID


@dataclass(frozen=True)
class TrailerCheck:
    """What `verify_trailers` found in a commit message. `ok` only for a
    signed Attestation trailer that matches the Verified-by and
    Review-rounds lines beside it, under this machine's director key."""

    state: str  # VALID | UNSIGNED | INVALID | NO_KEY
    reason: str = ""
    task: str = ""
    head: str = ""
    signer: str = ""
    rounds: int = 0
    key: str = ""

    @property
    def ok(self) -> bool:
        return self.state == VALID


def _parse_block(block: Any) -> dict[str, Any] | str:
    """The signed fields of an attestation table, or why it is malformed."""
    if not isinstance(block, Mapping):
        return "the attestation is not a table"
    extra = set(block) - {*_FIELDS, "v", "sig"}
    if extra:
        return f"the attestation has fields nothing signed: {', '.join(sorted(map(str, extra)))}"
    if block.get("v") != VERSION or isinstance(block.get("v"), bool):
        return f"unknown attestation version {block.get('v')!r}"
    out: dict[str, Any] = {}
    for f in _STR_FIELDS:
        value = block.get(f)
        if not isinstance(value, str):
            return f"attestation field {f!r} is missing or not a string"
        out[f] = value
    rounds = block.get("rounds")
    if not isinstance(rounds, int) or isinstance(rounds, bool):
        return "attestation field 'rounds' is missing or not a number"
    out["rounds"] = rounds
    review = block.get("review", {})
    if not isinstance(review, Mapping):
        return "attestation field 'review' is not a table"
    out["review"] = dict(review)
    if out["verdict"] not in VERDICTS:
        return f"unknown verdict {out['verdict']!r}"
    sig = block.get("sig")
    if not isinstance(sig, str) or not _HEX_SIG.match(sig):
        return "the signature is missing or malformed"
    out["sig"] = sig
    return out


def _status_key(status: str) -> str:
    """`status` as a reader takes it in: NFKC-folded (full-width and other
    compatibility forms), case-folded, with everything but letters and
    digits dropped (zero-width and other format characters, spaces,
    punctuation)."""
    folded = unicodedata.normalize("NFKC", status).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _within_two_edits(a: str, b: str) -> bool:
    """True if `a` becomes `b` with at most two one-character edits."""
    if abs(len(a) - len(b)) > 2:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > 2:
            return False
        prev = cur
    return prev[-1] <= 2


def claims_verdict(status: Any) -> bool:
    """True if a task status reads as a verdict: "verified" / "rejected" in
    any case or spacing, with look-alike, full-width or invisible
    characters, inside a longer word ("unverified", "verified-by-director")
    or a letter or two off ("verifed", "verif1ed"). Such a status counts
    only with a valid attestation (`status_label`)."""
    key = _status_key(str(status if status is not None else ""))
    return any(v in key or _within_two_edits(key, v) for v in VERDICTS)


def _trailer_mac(key: bytes, task: str, head: str, rounds: int, signer: str, key_id: str) -> str:
    body = {"v": VERSION, "task": task, "head": head, "verdict": "verified",
            "rounds": rounds, "signer": signer, "key": key_id}
    return hmac.new(key, _TRAILER_DOMAIN + _canonical(body), hashlib.sha256).hexdigest()


def _parse_attestation_trailer(value: str) -> tuple[str, str, str, str] | str:
    """``(task, head, key id, sig)`` of an Attestation trailer's value
    (``v1 task=<id> head=<sha> key=<key id> sig=<hmac>``, the task and head
    percent-encoded), or why it is malformed."""
    parts = value.split()
    if len(parts) != 5 or parts[0] != f"v{VERSION}":
        return "the Attestation trailer is not a v1 one"
    fields: dict[str, str] = {}
    for part, name in zip(parts[1:], ("task", "head", "key", "sig")):
        got, sep, text = part.partition("=")
        if got != name or not sep or not text:
            return f"the Attestation trailer's {name!r} field is missing or out of place"
        fields[name] = text
    task, head = unquote(fields["task"]), unquote(fields["head"])
    if _CONTROL.search(task) or _CONTROL.search(head) or not task.strip() or not head.strip():
        return "the Attestation trailer's task or head is malformed"
    if not _HEX_KEY_ID.match(fields["key"]) or not _HEX_SIG.match(fields["sig"]):
        return "the Attestation trailer's key id or signature is malformed"
    return task, head, fields["key"], fields["sig"]


class Verifier:
    """Checks task attestations and commit trailers against this machine's
    director key, reading it (and walking the process tree, `agent_parent`)
    once, when first needed: one for a whole ``evi task list`` or ``evi
    stats --provenance``, not one per task. The module's `verify`,
    `status_label`, `trailers` and `verify_trailers` each use a new one."""

    def __init__(self) -> None:
        self._loaded = False
        self._key: bytes | None = None
        self._why = ""

    def _key_or_reason(self) -> tuple[bytes | None, str]:
        if not self._loaded:
            self._key, self._why = _key_or_reason()
            self._loaded = True
        return self._key, self._why

    def verify(
        self, record: Mapping[str, Any], *, evidence_hash: str, head: str | None = None
    ) -> Check:
        """See the module's `verify`."""
        if ATTESTATION_KEY not in record:
            return Check(UNSIGNED, "no attestation")
        fields = _parse_block(record.get(ATTESTATION_KEY))
        if isinstance(fields, str):
            return Check(INVALID, fields)
        key, why = self._key_or_reason()
        if key is None:
            return Check(NO_KEY, why)
        if fields["key"] != key_id_of(key):
            return Check(INVALID, f"signed with another director key ({fields['key']}), "
                                  f"not this machine's ({key_id_of(key)})")
        try:
            expected = _mac(key, fields)
        except (TypeError, ValueError):
            return Check(INVALID, "the attestation holds a value that can't be signed")
        if not hmac.compare_digest(expected, fields["sig"]):
            return Check(INVALID, "the signature does not match the attestation")
        if fields["task"] != str(record.get("id") or "").strip():
            return Check(INVALID, f"the attestation is for task {fields['task']!r}, not this one")
        try:
            digest = task_digest(record)
        except (TypeError, ValueError):
            return Check(INVALID, "the task record holds a value that can't be signed")
        if not hmac.compare_digest(digest, fields["digest"]):
            return Check(INVALID, "the task record changed after it was signed")
        if not isinstance(evidence_hash, str) or evidence_hash != fields["evidence"]:
            return Check(INVALID, "the evidence changed after it was signed")
        found = dict(verdict=fields["verdict"], signer=fields["signer"], rounds=fields["rounds"],
                     key=fields["key"], head=fields["head"])
        if head is not None and head.strip() != fields["head"]:
            return Check(STALE, f"signed at {fields['head'][:12]}, the branch is now at "
                                f"{head.strip()[:12]}", **found)
        return Check(VALID, "", **found)

    def status_label(
        self, record: Mapping[str, Any], *, evidence_hash: str, head: str | None
    ) -> str:
        """See the module's `status_label`."""
        raw = record.get("status")
        status = str(raw if raw is not None else "").strip()
        if claims_verdict(status):
            check = self.verify(record, evidence_hash=evidence_hash, head=head)
            if check.ok:
                return check.verdict
            if check.state == UNSIGNED:
                return "unverified (unsigned)"
            if check.state == NO_KEY:
                return "unverified (no director key here to check the signature)"
            if check.state == STALE:
                return f"unverified (stale: {check.reason})"
            return f"unverified (invalid signature: {check.reason})"
        if not status:
            return ""
        if not _PLAIN_STATUS.match(status):
            shown = ascii(status[:64]) + ("..." if len(status) > 64 else "")
            return f"unverified (unrecognised status {shown})"
        if ATTESTATION_KEY in record:
            return f"{status} (an earlier attestation no longer applies)"
        return status

    def trailers(
        self, record: Mapping[str, Any], *, evidence_hash: str, head: str
    ) -> list[str]:
        """See the module's `trailers`."""
        if not isinstance(head, str) or not head.strip():
            raise AttestError(
                "commit trailers need the task branch's current head (`branch_head`): "
                "an attestation for a branch that moved on gives none"
            )
        check = self.verify(record, evidence_hash=evidence_hash, head=head)
        if not check.ok or check.verdict != "verified":
            return []
        key, _ = self._key_or_reason()
        if key is None:  # can't be: the attestation just verified under it
            return []
        task = str(record.get("id") or "").strip()
        signer = _CONTROL.sub(" ", check.signer).strip()
        sig = _trailer_mac(key, task, check.head, check.rounds, signer, check.key)
        return [
            f"Verified-by: {signer} (director key {check.key})",
            f"Review-rounds: {check.rounds}",
            f"Attestation: v{VERSION} task={quote(task, safe='')} "
            f"head={quote(check.head, safe='')} key={check.key} sig={sig}",
        ]

    def verify_trailers(self, message: str) -> TrailerCheck:
        """See the module's `verify_trailers`."""
        found: dict[str, list[str]] = {}
        for line in str(message or "").splitlines():
            m = _TRAILER_LINE.match(line.strip())
            if m:
                found.setdefault(m.group(1).lower(), []).append(m.group(2))
        if not found:
            return TrailerCheck(UNSIGNED, "no Verified-by trailer")
        if "attestation" not in found:
            return TrailerCheck(UNSIGNED, "no Attestation trailer: a bare Verified-by line "
                                          "is self-reported")
        for name, values in found.items():
            if len(values) > 1:
                return TrailerCheck(INVALID, f"more than one {name} trailer")
        if "verified-by" not in found or "review-rounds" not in found:
            return TrailerCheck(INVALID, "an Attestation trailer without its Verified-by and "
                                         "Review-rounds lines")
        parsed = _parse_attestation_trailer(found["attestation"][0])
        if isinstance(parsed, str):
            return TrailerCheck(INVALID, parsed)
        task, head, key_id, sig = parsed
        by = _VERIFIED_BY.match(found["verified-by"][0])
        if by is None:
            return TrailerCheck(INVALID, "the Verified-by trailer is malformed")
        signer = by.group(1).strip()
        if by.group(2) != key_id:
            return TrailerCheck(INVALID, "the Verified-by and Attestation trailers name "
                                         "different keys")
        if not re.fullmatch(r"[0-9]{1,9}", found["review-rounds"][0]):
            return TrailerCheck(INVALID, "the Review-rounds trailer is not a number")
        info = dict(task=task, head=head, signer=signer,
                    rounds=int(found["review-rounds"][0]), key=key_id)
        key, why = self._key_or_reason()
        if key is None:
            return TrailerCheck(NO_KEY, why, **info)
        if key_id != key_id_of(key):
            return TrailerCheck(INVALID, f"signed with another director key ({key_id}), not "
                                         f"this machine's ({key_id_of(key)})", **info)
        expected = _trailer_mac(key, task, head, info["rounds"], signer, key_id)
        if not hmac.compare_digest(expected, sig):
            return TrailerCheck(INVALID, "the Attestation signature does not match the "
                                         "trailers", **info)
        return TrailerCheck(VALID, "", **info)


def verify(
    record: Mapping[str, Any], *, evidence_hash: str, head: str | None = None
) -> Check:
    """Check the task `record`'s attestation against this machine's director
    key, the record as it is now and `evidence_hash` (`evidence_hash()` of the
    bundle now; "" for none). With `head`, a branch that has moved on since
    signing is `STALE` (`status_label` and `trailers` require it). Never
    raises for a bad record: it is `INVALID`."""
    return Verifier().verify(record, evidence_hash=evidence_hash, head=head)


def status_label(
    record: Mapping[str, Any], *, evidence_hash: str, head: str | None
) -> str:
    """The task status to show:

    - the signed verdict when the attestation is valid at `head` (the task
      branch's current head; None only when that branch no longer exists);
    - ``unverified (...)`` when the status reads as "verified" / "rejected"
      (`claims_verdict`) without one: unsigned, invalid, stale, or no key
      here to check it;
    - ``unverified (unrecognised status ...)`` for a status that isn't plain
      ASCII, as it could hide a look-alike;
    - else the record's own status (noting an attestation that no longer
      applies, see `drop_attestation`)."""
    return Verifier().status_label(record, evidence_hash=evidence_hash, head=head)


def trailers(
    record: Mapping[str, Any], *, evidence_hash: str, head: str
) -> list[str]:
    """The ``Verified-by``, ``Review-rounds`` and ``Attestation`` commit
    trailers for the task, from a valid "verified" attestation at `head` (the
    task branch's current head, required) only; [] otherwise (unsigned,
    tampered, rejected, stale, or no key to check with). The Attestation
    line signs the other two with the task id and head (`verify_trailers`)."""
    return Verifier().trailers(record, evidence_hash=evidence_hash, head=head)


def verify_trailers(message: str) -> TrailerCheck:
    """Check the director trailers in a commit `message` against this
    machine's key, without the task record or its evidence: `VALID` only for
    a signed Attestation trailer matching its Verified-by and Review-rounds
    lines (one task per commit); `UNSIGNED` for none, or a Verified-by line
    without one (self-reported); else `INVALID` or `NO_KEY`. The result
    names the task and the head it was verified at."""
    return Verifier().verify_trailers(message)
