"""Self-update + rollback.

`evi update` checks PyPI for a newer version, snapshots the current
`pip freeze` state to disk, runs `pip install --upgrade`, verifies the
install can import, and offers a rollback path. Snapshots live under
`~/.evi/snapshots/`; retention is the last five.

Critical invariants:

- We always install into `sys.executable`'s site-packages. Using bare
  `pip` from PATH could pick up a different venv.
- We refuse editable installs (`pip install -e .`) — don't clobber dev
  checkouts.
- For locked envs (poetry/uv/pipenv), we refuse and suggest the right
  tool unless `--force` is passed.
- For pipx, we forward to `pipx upgrade evi-ai` because pipx manages its
  own isolated venvs.
- Rollback restores the FULL `pip freeze`, not just Evi. A transitive
  dep bump can break Evi just as easily as a direct one; only restoring
  Evi wouldn't recover.

The module is structured so that the CLI calls into a handful of
top-level functions that all return dataclasses. Nothing here prints
or prompts — the CLI is the UX layer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

from evi.config import HOME, ensure_dirs


# PyPI distribution name (differs from the import package, which is `evi`).
DIST_NAME = "evi-ai"
PYPI_URL = f"https://pypi.org/pypi/{DIST_NAME}/json"
SNAPSHOTS_DIR = HOME / "snapshots"
DEFAULT_KEEP = 5
DEFAULT_PIP_TIMEOUT = 600.0  # seconds — full pip resolve can be slow


class UpdateError(RuntimeError):
    """Raised for unrecoverable update problems (PyPI unreachable, bad
    install kind, malformed snapshot, etc)."""


# --- PyPI probe ---------------------------------------------------------


@dataclass(frozen=True)
class LatestInfo:
    """Result of a PyPI version check."""

    current: str
    latest: str
    behind: bool
    release_url: str = ""
    summary: str = ""


def check_pypi(
    *,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
    current: str | None = None,
) -> LatestInfo:
    """Hit pypi.org for the latest Evi version.

    `current` defaults to `evi.__version__`. Passing it lets tests pin a
    value without monkeypatching the import. Raises `UpdateError` on
    network failure or malformed payload.
    """
    if current is None:
        from evi import __version__ as current

    try:
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=True) as c:
            r = c.get(PYPI_URL)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as exc:
        raise UpdateError(f"PyPI unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UpdateError(f"PyPI returned malformed JSON: {exc}") from exc

    info = data.get("info") or {}
    latest = str(info.get("version") or "").strip()
    if not latest:
        raise UpdateError("PyPI response missing info.version")
    return LatestInfo(
        current=current,
        latest=latest,
        behind=_version_lt(current, latest),
        release_url=str(info.get("project_url") or info.get("home_page") or ""),
        summary=str(info.get("summary") or ""),
    )


def _version_lt(a: str, b: str) -> bool:
    """Return True iff a < b under PEP 440-ish ordering.

    We deliberately avoid `packaging.version.parse` so we don't pull in
    another dep just for this. The grammar is `N.N.N(suffix)?`; pre-
    release suffixes (a/b/rc/dev) sort BEFORE the equivalent release.
    Good enough for our use case — PyPI versions follow this.
    """
    def parts(v: str) -> tuple:
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[\.\-]?(a|b|rc|dev)(\d+))?", v)
        if not m:
            # Unknown shape — fall back to string compare, treating it as max.
            return (10**6, 10**6, 10**6, "z", 10**6)
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        pre = m.group(4) or "z"  # absent pre-release sorts AFTER any pre
        pre_n = int(m.group(5)) if m.group(5) else 0
        return (major, minor, patch, pre, pre_n)
    return parts(a) < parts(b)


# --- install kind detection --------------------------------------------


@dataclass(frozen=True)
class InstallKind:
    """How Evi was installed. Drives whether `update` can run."""

    kind: str   # "venv" | "editable" | "pipx" | "locked" | "unknown"
    location: str = ""
    hint: str = ""

    @property
    def upgradable(self) -> bool:
        return self.kind == "venv"


def detect_install_kind() -> InstallKind:
    """Identify the install layout. Order: editable > pipx > locked > venv.

    `editable` and `pipx` win because they're certain; `locked` is a
    cwd-walk heuristic that the user can override with `--force`.
    """
    # 1) editable — parse `pip show evi-ai`
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "show", DIST_NAME],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if line.lower().startswith("editable project location:"):
                    loc = line.split(":", 1)[1].strip()
                    return InstallKind(
                        kind="editable",
                        location=loc,
                        hint=f"editable install at {loc} — use `git pull` instead.",
                    )
    except (OSError, subprocess.TimeoutExpired):
        pass

    # 2) pipx — env hint or executable path under pipx's venv layout
    pipx_home = os.environ.get("PIPX_HOME", "").strip()
    exe = Path(sys.executable).resolve()
    is_pipx = False
    if pipx_home:
        try:
            exe.relative_to(Path(pipx_home).resolve())
            is_pipx = True
        except ValueError:
            pass
    if not is_pipx:
        # Pipx default layout: ~/.local/pipx/venvs/evi-ai/{bin,Scripts}/python
        for marker in (f"pipx/venvs/{DIST_NAME}", f"pipx\\venvs\\{DIST_NAME}"):
            if marker in str(exe):
                is_pipx = True
                break
    if is_pipx:
        return InstallKind(
            kind="pipx",
            location=str(exe),
            hint=f"pipx-installed — run `pipx upgrade {DIST_NAME}` instead.",
        )

    # 3) locked env — walk cwd up to $HOME looking for a lockfile
    locked = _find_lockfile(Path.cwd())
    if locked is not None:
        return InstallKind(
            kind="locked",
            location=str(locked),
            hint=(
                f"locked-env project ({locked.name}) — use that tool's upgrade "
                f"command (poetry update {DIST_NAME} / uv pip install -U {DIST_NAME} "
                f"/ pipenv update {DIST_NAME}). Pass --force to override."
            ),
        )

    return InstallKind(kind="venv", location=str(exe))


def _find_lockfile(start: Path) -> Path | None:
    """Walk up from `start` (capped at HOME) looking for a lockfile.

    Returns the lockfile path or None. We don't escape the user's
    home — wandering into `/` would find way too many false positives.
    """
    home = Path.home().resolve()
    candidates = ("poetry.lock", "uv.lock", "Pipfile.lock")
    cur = start.resolve()
    while True:
        for name in candidates:
            p = cur / name
            if p.is_file():
                return p
        if cur == home or cur == cur.parent:
            return None
        cur = cur.parent


# --- snapshots ----------------------------------------------------------


_DIR_NAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})_(.+)_to_(.+)$"
)


@dataclass(frozen=True)
class Snapshot:
    """One pre-upgrade snapshot directory."""

    dir: Path
    timestamp: datetime
    from_version: str
    to_version: str

    @property
    def name(self) -> str:
        return self.dir.name


def _timestamp_str(now: datetime | None = None) -> str:
    now = now or datetime.now(tz=timezone.utc)
    return now.strftime("%Y-%m-%dT%H-%M-%S")


def create_snapshot(
    from_version: str,
    to_version: str,
    *,
    root: Path | None = None,
    pip_executable: list[str] | None = None,
    timeout: float = 60.0,
) -> Snapshot:
    """Take a `pip freeze` snapshot and write metadata.

    `root` overrides SNAPSHOTS_DIR for tests. `pip_executable` overrides
    the pip command (default `[sys.executable, '-m', 'pip']`).
    """
    root = root or SNAPSHOTS_DIR
    ensure_dirs()
    root.mkdir(parents=True, exist_ok=True)

    ts = _timestamp_str()
    snap_dir = root / f"{ts}_{from_version}_to_{to_version}"
    snap_dir.mkdir(parents=True, exist_ok=False)

    pip = pip_executable or [sys.executable, "-m", "pip"]
    try:
        proc = subprocess.run(
            [*pip, "freeze"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(snap_dir, ignore_errors=True)
        raise UpdateError(f"pip freeze failed: {exc}") from exc
    if proc.returncode != 0:
        shutil.rmtree(snap_dir, ignore_errors=True)
        raise UpdateError(f"pip freeze exited {proc.returncode}: {proc.stderr}")

    (snap_dir / "requirements.txt").write_text(proc.stdout, encoding="utf-8")
    (snap_dir / "version.txt").write_text(from_version + "\n", encoding="utf-8")
    metadata = {
        "timestamp": ts,
        "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
        "from": from_version,
        "to": to_version,
        "python_executable": sys.executable,
    }
    (snap_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    return Snapshot(
        dir=snap_dir,
        timestamp=datetime.strptime(ts, "%Y-%m-%dT%H-%M-%S").replace(tzinfo=timezone.utc),
        from_version=from_version,
        to_version=to_version,
    )


def list_snapshots(*, root: Path | None = None) -> list[Snapshot]:
    """Return all on-disk snapshots, newest first."""
    root = root or SNAPSHOTS_DIR
    if not root.is_dir():
        return []
    out: list[Snapshot] = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        m = _DIR_NAME_RE.match(p.name)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        out.append(
            Snapshot(
                dir=p,
                timestamp=ts,
                from_version=m.group(2),
                to_version=m.group(3),
            )
        )
    out.sort(key=lambda s: s.timestamp, reverse=True)
    return out


def gc_snapshots(*, keep: int = DEFAULT_KEEP, root: Path | None = None) -> list[Snapshot]:
    """Drop snapshots beyond `keep`. Returns the deleted ones."""
    keep = max(0, int(keep))
    snaps = list_snapshots(root=root)
    if len(snaps) <= keep:
        return []
    extras = snaps[keep:]
    for s in extras:
        shutil.rmtree(s.dir, ignore_errors=True)
    return extras


def resolve_snapshot(
    selector: str | int | None,
    *,
    root: Path | None = None,
) -> Snapshot:
    """Pick a snapshot from a flexible selector.

    - None / 1 / "1" → newest
    - 2 → second newest
    - "<dir name>" → exact match
    """
    snaps = list_snapshots(root=root)
    if not snaps:
        raise UpdateError("no snapshots on disk")
    if selector is None or selector == "" or selector == 1 or selector == "1":
        return snaps[0]
    if isinstance(selector, int) or (isinstance(selector, str) and selector.isdigit()):
        idx = int(selector) - 1
        if idx < 0 or idx >= len(snaps):
            raise UpdateError(f"snapshot index {selector} out of range (1..{len(snaps)})")
        return snaps[idx]
    for s in snaps:
        if s.name == selector:
            return s
    raise UpdateError(f"no snapshot named {selector!r}")


# --- pip operations -----------------------------------------------------


@dataclass(frozen=True)
class UpgradeResult:
    """Outcome of one pip install call."""

    ok: bool
    spec: str
    stdout: str
    stderr: str
    returncode: int


def _pip_run(
    args: Iterable[str],
    *,
    timeout: float = DEFAULT_PIP_TIMEOUT,
    pip_executable: list[str] | None = None,
) -> subprocess.CompletedProcess:
    pip = pip_executable or [sys.executable, "-m", "pip"]
    return subprocess.run(
        [*pip, *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def apply_upgrade(
    spec: str,
    *,
    timeout: float = DEFAULT_PIP_TIMEOUT,
    pip_executable: list[str] | None = None,
) -> UpgradeResult:
    """Run `pip install --upgrade <spec>`.

    `spec` is either `evi-ai`, `evi-ai==X.Y.Z`, or a local path to a wheel /
    sdist. Never raises — pack the failure into the result.
    """
    try:
        proc = _pip_run(
            ["install", "--upgrade", spec],
            timeout=timeout, pip_executable=pip_executable,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return UpgradeResult(
            ok=False, spec=spec, stdout="",
            stderr=f"pip invocation failed: {exc}", returncode=-1,
        )
    return UpgradeResult(
        ok=proc.returncode == 0,
        spec=spec,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )


def apply_rollback(
    snapshot: Snapshot,
    *,
    timeout: float = DEFAULT_PIP_TIMEOUT,
    pip_executable: list[str] | None = None,
) -> UpgradeResult:
    """Restore a snapshot by re-running pip install -r <requirements>.

    See module-level rationale: we restore the FULL freeze, not just
    Evi, so transitive bumps get undone too.
    """
    req = snapshot.dir / "requirements.txt"
    if not req.is_file():
        return UpgradeResult(
            ok=False, spec=str(snapshot.dir),
            stdout="", stderr=f"requirements.txt missing under {snapshot.dir}",
            returncode=-1,
        )
    try:
        proc = _pip_run(
            ["install", "-r", str(req)],
            timeout=timeout, pip_executable=pip_executable,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return UpgradeResult(
            ok=False, spec=str(req), stdout="",
            stderr=f"pip invocation failed: {exc}", returncode=-1,
        )
    return UpgradeResult(
        ok=proc.returncode == 0,
        spec=str(req),
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )


# --- post-install verify -----------------------------------------------


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    version: str
    err: str = ""


def verify_install(*, python_executable: str | None = None,
                   timeout: float = 30.0) -> VerifyResult:
    """Spawn a fresh python and import evi.

    We can't rely on `import evi` in this process: the in-memory module
    is the pre-upgrade version and Python won't pick up the new files
    without a full restart. A subprocess is the only honest check.
    """
    py = python_executable or sys.executable
    try:
        proc = subprocess.run(
            [py, "-c", "import evi, sys; sys.stdout.write(evi.__version__)"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return VerifyResult(ok=False, version="", err=str(exc))
    if proc.returncode != 0:
        return VerifyResult(
            ok=False,
            version="",
            err=(proc.stderr or proc.stdout or "import failed").strip(),
        )
    version = proc.stdout.strip()
    return VerifyResult(ok=bool(version), version=version)
