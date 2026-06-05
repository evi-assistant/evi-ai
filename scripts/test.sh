#!/usr/bin/env bash
# Run the test suite. Plain `pytest` works too — this just bundles the
# common flags and uses the venv if present.

set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python="python"
if [[ -x ".venv/bin/python" ]]; then
    python=".venv/bin/python"
elif [[ -x ".venv/Scripts/python.exe" ]]; then
    python=".venv/Scripts/python.exe"
fi

"$python" -m pytest --timeout=15 "$@"
