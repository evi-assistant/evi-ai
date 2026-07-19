"""Safe mode — a clean boot with every user customization disabled.

`evi --safe-mode …` (or ``EVI_SAFE_MODE=1``) starts a session with project
context, memory, skills, hooks, guardrails, user/plugin slash commands, plugin
bin/ PATH wiring, MCP servers and transcripts all switched off, so a broken one
can be isolated. Mirrors Claude Code's ``--safe-mode``.

The flag is carried in the environment rather than the config so it propagates
uniformly to `Config.load()`, subprocesses, and the desktop sidecar — and so it
can never be persisted to disk by accident.

Enforcement is centralised in :func:`evi.sdk.builder.build_agent` (the single
source of truth for Agent assembly), so every surface — CLI, web, desktop, MCP,
scheduler, subagents — honours it without each caller remembering to.
"""

from __future__ import annotations

import os

ENV_VAR = "EVI_SAFE_MODE"
_TRUTHY = ("1", "true", "yes", "on")


def enabled() -> bool:
    """True when ``--safe-mode`` / ``EVI_SAFE_MODE`` is active."""
    return os.environ.get(ENV_VAR, "").strip().lower() in _TRUTHY


def activate() -> None:
    """Turn safe mode on for this process *and* anything it spawns."""
    os.environ[ENV_VAR] = "1"


def banner() -> str:
    """One-line notice for surfaces that want to tell the user why their
    customizations are missing."""
    return (
        "safe mode: project context, memory, skills, hooks, guardrails, "
        "commands, plugins and MCP are disabled"
    )
