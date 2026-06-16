"""Configurable REPL keybindings (Phase 82).

A small mapping of key sequence → slash command, read from
``~/.evi/keybindings.toml``:

    [keybindings]
    "c-t" = "/tools"          # Ctrl-T runs /tools
    "c-r" = "/reset"          # Ctrl-R clears the conversation
    "f2"  = "/model"          # F2 opens the model picker line
    "escape g" = "/goal"      # a two-key sequence (Esc then g)

When a bound key is pressed in the interactive chat REPL, the line is
replaced with that command and submitted — a one-keystroke shortcut for the
commands you run most. Keys use prompt_toolkit's names (``c-t``, ``f2``,
``escape``, …); a space-separated value is a multi-key sequence. Bindings that
collide with terminal essentials (``c-c`` interrupt, ``c-d`` EOF, ``tab``
completion) are skipped so you can't lock yourself out.

This is a separate file rather than a `config.toml` section because the map is
free-form (arbitrary keys) and the flat-TOML config writer only handles
scalars + string lists — same reason hooks.toml and mcp.json live on their own.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tomllib

from evi.config import KEYBINDINGS_PATH

logger = logging.getLogger(__name__)

# Keys we refuse to rebind — losing these breaks the terminal contract.
_RESERVED = {"c-c", "c-d", "tab", "c-m", "enter", "c-j"}


def load_keybindings(path: Path | None = None) -> dict[str, str]:
    """Parse ``[keybindings]`` from keybindings.toml into {key: command}.

    Missing file / malformed content → {} (never raises). Reserved keys and
    empty/blank commands are dropped with a warning.
    """
    p = path or KEYBINDINGS_PATH
    if not p.is_file():
        return {}
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("keybindings file unreadable (%s); ignoring", exc)
        return {}

    section = data.get("keybindings", data)  # allow a bare top-level table too
    if not isinstance(section, dict):
        return {}
    out: dict[str, str] = {}
    for key, cmd in section.items():
        key = str(key).strip().lower()
        cmd = str(cmd).strip()
        if not key or not cmd:
            continue
        if key in _RESERVED or any(k in _RESERVED for k in key.split()):
            logger.warning("skipping reserved keybinding %r", key)
            continue
        out[key] = cmd
    return out
