#!/bin/sh
# Entrypoint of the eVi image (Dockerfile): runs its command, `evi web --host
# 0.0.0.0 --port 8000` by default, after making sure that server has an access
# token.
#
# A published port reaches the container from the Docker bridge, not from
# loopback, and without a token eVi answers only loopback clients — so even the
# host's own browser would be refused (docs/security.md, "Containers"). When
# neither EVI_WEB_AUTH_TOKEN nor `[web] auth_token` is set, this generates a
# 256-bit token once, keeps it in the eVi home (the state volume) as
# `container-token` so restarts reuse it, and passes it to `evi web` as
# EVI_WEB_AUTH_TOKEN. Like the desktop app's LAN token it gates other machines
# only, so the image's HEALTHCHECK and `docker exec … evi …` keep working on
# loopback. The token is never logged: `evi web-config token show` prints it.
set -eu

if [ "${1:-}" = "evi" ] && [ "${2:-}" = "web" ]; then
    # Prints the token to pass, or nothing when the environment or config.toml
    # already has one. Notes for the log go to stderr.
    token=$(python - <<'PY'
import os
import re
import secrets
import sys
from pathlib import Path


def note(msg: str) -> None:
    print(f"eVi: {msg}", file=sys.stderr)


if os.environ.get("EVI_WEB_AUTH_TOKEN", "").strip():
    sys.exit(0)
try:
    from evi.config import Config

    configured = (Config.load().web.auth_token or "").strip()
except Exception as exc:  # the server will report a broken config itself
    note(f"could not read config.toml ({exc}); generating an access token anyway")
    configured = ""
if configured:
    sys.exit(0)

home = Path(os.environ.get("EVI_HOME") or Path.home() / ".evi")
path = home / "container-token"
try:
    token = path.read_text(encoding="utf-8").strip()
except (OSError, UnicodeDecodeError):
    token = ""
if re.fullmatch(r"[0-9A-Fa-f]{64}", token):
    kept = f"kept in {path}"
else:  # only a token this script could have written is reused
    token = secrets.token_hex(32)
    try:
        home.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.replace(tmp, path)
        kept = f"generated and kept in {path}"
    except OSError as exc:
        kept = f"generated, but not saved ({exc}), so it changes on every restart"

note(f"no access token was set, so this container uses its own: {kept}.")
note("Other machines, your browser through the published port included, sign in "
     "with it. Show it with:")
note("  docker exec <container> evi web-config token show")
note("To choose your own, set EVI_WEB_AUTH_TOKEN or [web] auth_token "
     "(evi web-config token rotate).")
sys.stdout.write(token)
PY
    ) || token=""
    if [ -n "$token" ]; then
        EVI_WEB_AUTH_TOKEN=$token
        export EVI_WEB_AUTH_TOKEN
    fi
fi

exec "$@"
