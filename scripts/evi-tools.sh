#!/usr/bin/env bash
# Thin wrapper: forwards to scripts/evi_tools.py.
#   ./scripts/evi-tools.sh list
#   ./scripts/evi-tools.sh install ffmpeg
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="python3"
if [ -x "$here/../.venv/bin/python" ]; then py="$here/../.venv/bin/python"; fi
exec "$py" "$here/evi_tools.py" "$@"
