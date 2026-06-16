#!/usr/bin/env bash
# eVi install script — Linux / macOS
#
# Usage:
#   ./scripts/install.sh                       # core install
#   ./scripts/install.sh --all                 # all optional extras
#   ./scripts/install.sh --extras web,mcp,scheduler

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
extras="dev,web,mcp,scheduler"
all=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all) all=1; shift ;;
        --extras) extras="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ "$all" == "1" ]]; then
    extras="dev,web,mcp,scheduler,downloads,web-tools,stt,computer"
fi

echo "==> eVi install"
echo "    repo:    $repo_root"
echo "    extras:  $extras"

# Pick a python — prefer 3.13, then 3.14, then `python3`.
python=""
for candidate in python3.13 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        v=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        major=${v%%.*}; minor=${v##*.}
        if [[ "$major" -gt 3 || ( "$major" == "3" && "$minor" -ge 13 ) ]]; then
            python="$candidate"; break
        fi
    fi
done
if [[ -z "$python" ]]; then
    echo "error: Python 3.13+ required" >&2
    exit 1
fi
echo "    python:  $python ($("$python" --version))"

# Create venv.
venv="$repo_root/.venv"
if [[ ! -d "$venv" ]]; then
    echo "==> Creating venv at $venv"
    "$python" -m venv "$venv"
fi
venv_python="$venv/bin/python"

echo "==> Upgrading pip"
"$venv_python" -m pip install --quiet --upgrade pip

echo "==> Installing evi with extras [$extras]"
"$venv_python" -m pip install --quiet -e "$repo_root[$extras]"

echo "==> Smoke test"
"$venv_python" -m pytest -q --timeout=15

cat <<EOF

==> Done.
Activate the venv with:
    source $venv/bin/activate
Then try:
    evi models recommend
    evi chat
EOF
