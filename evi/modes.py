"""Session modes — Chat / Cowork / Code presets.

A *mode* is a curated set of tool categories the session is allowed to use,
surfaced as a segmented control in the web/desktop UI (modelled on Claude
Desktop's Chat / Cowork / Code switcher). Switching a session's mode hot-swaps
the agent's tool list; new sessions inherit the last-picked mode.

Modes intentionally only gate *tools* — model, reasoning effort, etc. stay under
the model picker / settings so the two concerns don't fight. `memory` and
`skills` are in every mode (they're how eVi stays useful + personalised).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    name: str
    label: str
    categories: tuple[str, ...]
    blurb: str


# Order matters — it's the left-to-right order of the switcher.
MODES: dict[str, Mode] = {
    "chat": Mode(
        name="chat",
        label="Chat",
        categories=("memory", "skills"),
        blurb="Plain conversation — no file, code, or web tools.",
    ),
    "cowork": Mode(
        name="cowork",
        label="Cowork",
        categories=("memory", "skills", "fs", "web", "image", "pdf", "calendar", "index"),
        blurb="Everyday assistant — read files, search the web, calendar, images.",
    ),
    "code": Mode(
        name="code",
        label="Code",
        categories=("memory", "skills", "fs", "code", "shell", "git", "index", "subagent"),
        blurb="Software work — code, shell, git, semantic search, subagents.",
    ),
}

DEFAULT_MODE = "chat"


def resolve(name: str | None) -> Mode:
    """Return the named mode, falling back to the default for unknown names."""
    return MODES.get(name or "", MODES[DEFAULT_MODE])


def mode_tools(name: str | None) -> list:
    """Tools enabled for a mode — every registered tool whose category is in the
    mode's set, regardless of the global per-category toggles (the mode IS the
    toggle while it's active)."""
    from evi.tools.base import REGISTRY

    cats = set(resolve(name).categories)
    return [t for t in REGISTRY.values() if t.category in cats]
