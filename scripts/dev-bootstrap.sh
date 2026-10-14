#!/usr/bin/env bash
# dev-bootstrap.sh - set up or repair an eVi development environment.
#
#   bash scripts/dev-bootstrap.sh [options]
#
#   --venv DIR         the venv to use (default: $EVI_DEV_VENV, else
#                      ~/.venvs/evi-<version>, e.g. ~/.venvs/evi-313). Keep it
#                      outside the checkout: a venv records one machine's
#                      interpreter path, so a checkout shared between machines
#                      must not carry it.
#   --python VER       Python for a new venv (default 3.13; 3.12+ required)
#   --extras LIST      extras to install
#                      (default dev,web,mcp,scheduler,downloads,web-tools,index,e2e)
#   --recreate         delete and recreate a venv that does not start although
#                      its interpreter exists (broken, older than 3.12), or a
#                      foreign one inside a git checkout
#   --reinstall        reinstall eVi + pytest-xdist into an existing venv
#   --playwright       also download Chromium for tests/e2e
#   --worktree BRANCH  create (or reuse) <repo>/.worktrees/BRANCH and check it
#   --base REF         start point for a new worktree branch (default HEAD)
#   --repo DIR         the checkout (default: the one holding this script)
#   --dry-run          print what would happen and change nothing
#
# A working venv is reused. One that is plainly foreign (the interpreter its
# pyvenv.cfg names does not exist on this machine) and outside any git
# checkout is moved aside, never deleted, and recreated with uv, else with
# python -m venv. Any other venv that does not start, or is older than 3.12,
# and any foreign venv inside a git checkout is replaced only with --recreate.
# The script ends with `python -m evi doctor --dev --strict`. git
# safe.directory and line-ending problems are reported with their fix command,
# never applied.
set -euo pipefail

say() { echo "dev-bootstrap: $*"; }
die() { echo "dev-bootstrap: $*" >&2; exit 2; }
run() {  # echo a step, then run it unless --dry-run
  echo "+ $*"
  if [ "$DRY_RUN" = 0 ]; then "$@"; fi
}

PYVER=3.13
EXTRAS="dev,web,mcp,scheduler,downloads,web-tools,index,e2e"
VENV="${EVI_DEV_VENV:-}"
RECREATE=0
REINSTALL=0
PLAYWRIGHT=0
DRY_RUN=0
WORKTREE=""
BASE="HEAD"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SAFE_DIR_FIX=""

# An option's value: present, and not the next option. Otherwise
# `--extras --dry-run` would take "--dry-run" as the list and run for real.
need() {
  case "${2-}" in
    ""|-*) die "$1 needs a value${2:+, not the option $2} (see --help)" ;;
  esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    --venv) need "$1" "${2-}"; VENV="$2"; shift 2 ;;
    --python) need "$1" "${2-}"; PYVER="$2"; shift 2 ;;
    --extras) need "$1" "${2-}"; EXTRAS="$2"; shift 2 ;;
    --recreate) RECREATE=1; shift ;;
    --reinstall) REINSTALL=1; shift ;;
    --playwright) PLAYWRIGHT=1; shift ;;
    --worktree) need "$1" "${2-}"; WORKTREE="$2"; shift 2 ;;
    --base) need "$1" "${2-}"; BASE="$2"; shift 2 ;;
    --repo) need "$1" "${2-}"; REPO="$(cd "$2" && pwd)"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# --- git safe.directory --------------------------------------------------
# git refuses a checkout owned by another account (a network share) with
# "dubious ownership". Print git's own fix and trust the path for this run only.
trust_check() {
  local out fix value n
  if out="$(LC_ALL=C git -C "$1" rev-parse --show-toplevel 2>&1)"; then
    return 0
  fi
  case "$out" in
    *"dubious ownership"*) ;;
    *) die "git cannot read $1: $out" ;;
  esac
  fix="$(printf '%s\n' "$out" | grep -o "git config --global --add safe.directory .*" | head -n 1)"
  say "git does not trust $1 (owned by another account; usual on a network share)."
  say "fix it once with:  $fix"
  value="${fix#git config --global --add safe.directory }"
  value="${value#\'}"
  value="${value%\'}"
  n="${GIT_CONFIG_COUNT:-0}"
  export "GIT_CONFIG_KEY_$n=safe.directory" "GIT_CONFIG_VALUE_$n=$value"
  export GIT_CONFIG_COUNT=$((n + 1))
  SAFE_DIR_FIX="${SAFE_DIR_FIX:+$SAFE_DIR_FIX; }$fix"
}
trust_check "$REPO"

# --- the venv ------------------------------------------------------------
abspath() {  # absolute physical path of $1 (which need not exist yet)
  if [ -d "$1" ]; then (cd "$1" && pwd -P); return; fi
  local d
  d="$(dirname "$1")"
  if [ -d "$d" ]; then echo "$(cd "$d" && pwd -P)/$(basename "$1")"; else echo "$1"; fi
}
in_checkout() {  # print the git checkout (a directory holding .git) that holds
  # $1. It walks the path itself instead of comparing it with $REPO as a
  # string, which a different spelling (case, 8.3 name, symlink) slips past.
  local d="$1" parent
  while :; do
    if [ -e "$d/.git" ]; then echo "$d"; return 0; fi
    parent="$(dirname "$d")"
    [ "$parent" != "$d" ] || return 1
    d="$parent"
  done
}
[ -n "$VENV" ] || VENV="$HOME/.venvs/evi-${PYVER//./}"
VENV="$(abspath "${VENV//\\//}")"  # C:\x\venv from a Windows env var -> C:/x/venv

venv_py() {
  if [ "${OS:-}" = "Windows_NT" ]; then echo "$1/Scripts/python.exe"; else echo "$1/bin/python"; fi
}
runs_312() {  # the given command starts and is Python 3.12+ (checked by output,
  # not exit code alone: bash "runs" an empty or non-binary file as a script)
  [ "$("$@" -c 'import sys; print("py-ok" if sys.version_info >= (3, 12) else "py-old")' 2>/dev/null | tr -d '\r')" = "py-ok" ]
}
find_python() {  # a command for Python $PYVER, else any 3.12+
  local c
  for c in "python$PYVER" "py -$PYVER" python3 python; do
    # shellcheck disable=SC2086  # "py -3.13" is two words on purpose
    if command -v ${c%% *} >/dev/null 2>&1 && runs_312 $c; then
      echo "$c"
      return 0
    fi
  done
  return 1
}
have_uv() { command -v uv >/dev/null 2>&1; }
winpath() {  # a path native Windows tools (python, uv, pip) read; as is elsewhere
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s\n' "$1"; fi
}

CREATE=0
if [ -d "$VENV" ]; then
  if runs_312 "$(venv_py "$VENV")"; then
    say "using venv $VENV"
  else
    [ -f "$VENV/pyvenv.cfg" ] || die "$VENV exists but is not a venv (no pyvenv.cfg); refusing to touch it"
    home="$(sed -n 's/^home *= *//p' "$VENV/pyvenv.cfg" | tr -d '\r' | head -n 1)"
    # Replace it unasked only when it is plainly foreign: the interpreter it
    # was made from does not exist here. Anything else needs --recreate.
    if [ -n "$home" ] && [ -d "$home" ]; then
      [ "$RECREATE" = 1 ] || die "venv $VENV does not start (or is older than 3.12) although its interpreter $home exists: rerun with --recreate to replace it"
    else
      say "venv $VENV is foreign: its base interpreter ${home:-?} does not exist on this machine"
    fi
    if checkout="$(in_checkout "$VENV")" && [ "$RECREATE" != 1 ]; then
      die "it is inside the git checkout $checkout and may be another machine's venv: rerun with --recreate to replace it, or use --venv outside the checkout"
    fi
    if [ "$RECREATE" = 1 ]; then
      run rm -rf -- "$VENV"
    else
      # Moved aside, not deleted: "foreign" is a guess from this account's
      # view, and a venv whose interpreter this shell cannot see (another
      # machine, or an app's redirected AppData) may still work there.
      aside="$VENV.foreign-$(date +%Y%m%d-%H%M%S)-$$"
      run mv -- "$VENV" "$aside"
      [ "$DRY_RUN" = 1 ] || say "moved it aside to $aside: delete that once the new venv works, or move it back"
    fi
    CREATE=1
  fi
else
  CREATE=1
fi

if [ "$CREATE" = 1 ]; then
  if have_uv; then
    run uv venv --python "$PYVER" "$(winpath "$VENV")"
  else
    base="$(find_python)" || die "no Python 3.12+ found: install uv (https://docs.astral.sh/uv/) or Python $PYVER"
    # shellcheck disable=SC2086
    run $base -m venv "$(winpath "$VENV")"
  fi
fi
PY="$(venv_py "$VENV")"

if [ "$CREATE" = 1 ] || [ "$REINSTALL" = 1 ]; then
  spec="$(winpath "$REPO")[$EXTRAS]"
  if have_uv; then
    run uv pip install --python "$PY" -e "$spec" pytest-xdist
  else
    run "$PY" -m pip install --upgrade pip
    run "$PY" -m pip install -e "$spec" pytest-xdist
  fi
fi
if [ "$PLAYWRIGHT" = 1 ]; then
  run "$PY" -m playwright install chromium
fi

# --- the worktree --------------------------------------------------------
TARGET="$REPO"
if [ -n "$WORKTREE" ]; then
  git -C "$REPO" check-ref-format --branch "$WORKTREE" >/dev/null 2>&1 \
    || die "not a valid branch name: $WORKTREE"
  TARGET="$REPO/.worktrees/${WORKTREE//\//__}"
  if [ -e "$TARGET/.git" ]; then
    # Reuse it only for the same branch: `a/b` and `a__b` share a directory,
    # and a worktree can have switched branches since it was made.
    trust_check "$TARGET"
    have="$(git -C "$TARGET" symbolic-ref --short -q HEAD || true)"
    [ "$have" = "$WORKTREE" ] \
      || die "$TARGET is the worktree of branch ${have:-(a detached HEAD)}, not $WORKTREE: pick another branch name, or remove that worktree"
    say "reusing worktree $TARGET"
  elif [ -e "$TARGET" ]; then
    die "$TARGET exists but is not a worktree"
  else
    # Keep worktrees out of the main checkout's `git add -A`.
    if ! git -C "$REPO" check-ignore -q --no-index .worktrees/probe; then
      exclude="$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)/info/exclude"
      echo "+ append .worktrees/ to $exclude"
      if [ "$DRY_RUN" = 0 ]; then
        mkdir -p "$(dirname "$exclude")"
        printf '\n.worktrees/\n' >> "$exclude"
      fi
    fi
    if git -C "$REPO" show-ref --verify --quiet "refs/heads/$WORKTREE"; then
      run git -C "$REPO" worktree add "$TARGET" "$WORKTREE"
    else
      run git -C "$REPO" worktree add -b "$WORKTREE" "$TARGET" "$BASE"
    fi
    [ "$DRY_RUN" = 1 ] || trust_check "$TARGET"
  fi
fi

# --- check ---------------------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then
  echo "+ (cd $TARGET && $PY -m evi doctor --dev --repo $(winpath "$TARGET") --venv $(winpath "$VENV") --strict)"
  say "dry run: nothing was changed"
  exit 0
fi
cd "$TARGET"
set +e
"$PY" -m evi doctor --dev --repo "$(winpath "$TARGET")" --venv "$(winpath "$VENV")" --strict
code=$?
set -e
if [ -n "$SAFE_DIR_FIX" ]; then
  say "git trusted the checkout for this run only; make it permanent with: $SAFE_DIR_FIX"
fi
if [ "$code" = 0 ]; then
  say "ready: $PY (work in $TARGET)"
fi
exit "$code"
