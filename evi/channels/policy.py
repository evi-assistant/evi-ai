"""A chat channel's own permission policy: the ceiling on what a paired sender
can make eVi do.

It is built from `[channels]` and never from `[auto]`, because `[auto]` is the
desktop user's policy for a session they are watching. A channel turn has
nobody at the desk, and the person typing is whoever the owner paired with.

What a channel agent gets:

- **Tools**: the categories in the channel's tool list and nothing else.
  `subagent` is never offered, because a subagent loads the desktop config and
  so would run under the desktop policy. The `memory` category is cut down to
  what `memory_mode` allows (`MEMORY_TOOLS`). The project bug-ledger tools
  (`record_fix`, `search_fixes`) are left out because a channel has no project.
  In safe mode, memory, skills and MCP tools are dropped, as `build_agent` does
  for its own toggles.
- **Permission**: mode "ask" with nobody to ask, so a call runs only when its
  category is in the channel's auto-approve list or it is a channel-safe tool:
  the memory tools `memory_mode` exposes (they cannot write the owner's memory
  unless the mode is "full") and the read-only skill tools. The user's
  `hard_deny`, deny rules and protected paths still apply. The
  destructive-command guard is always on and has no exemptions. Nothing else
  carries over from `[auto]`: not its `auto_approve`, mode (yolo /
  accept_edits), allow rules, trusted dirs or domains, guard exemptions,
  `protected_paths_remove` or unattended policy.
- **Backend**: never a delegate CLI agent (codex, gemini, amp, qwen, copilot),
  which runs its own tools outside this policy. `[llm]
  allow_cli_agent_unattended` is the desktop owner's opt-in for their own
  unattended runs (scheduled tasks, `evi run`) and does not cover a channel.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Iterable

from evi.config import AutoSettings, Config

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evi.tools.base import Tool

logger = logging.getLogger(__name__)

MEMORY_MODES = ("recall", "staged", "full")
# The memory tools each memory_mode offers. recall reads and searches; staged
# adds remember, which a StagedMemoryStore turns into an inbox note
# (evi/channels/inbox.py); full writes and forgets the owner's memory directly.
MEMORY_TOOLS: dict[str, tuple[str, ...]] = {
    "recall": ("recall", "list_memories", "recall_by_tag"),
    "staged": ("recall", "list_memories", "recall_by_tag", "remember"),
    "full": ("recall", "list_memories", "recall_by_tag", "remember", "forget"),
}
# The skill tools only read eVi's own installed skills.
SKILL_TOOLS = ("list_skills", "invoke_skill")
# Categories a channel never gets, whatever its tool list says.
UNCONTAINED = frozenset({"subagent"})
_SAFE_MODE_DROPS = frozenset({"memory", "skills", "mcp"})


def _names(items: Any) -> list[str]:
    """Clean a config list of category names: stripped, lower-case, deduped.
    A bare string counts as one name, and anything else as none."""
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, (list, tuple, set, frozenset)):
        return []
    seen: dict[str, None] = {}
    for item in items:
        if isinstance(item, str) and item.strip():
            seen.setdefault(item.strip().lower(), None)
    return list(seen)


@dataclass(frozen=True)
class ChannelPolicy:
    """What one channel's agent may do. Build it with `make_policy`."""

    channel: str
    tools: tuple[str, ...]          # tool categories offered
    auto_approve: tuple[str, ...]   # categories that run without asking
    memory_mode: str                # one of MEMORY_MODES
    allow_groups: bool              # answer paired senders in group chats

    @property
    def safe_tools(self) -> tuple[str, ...]:
        """Tool names that run without asking, beyond `auto_approve`."""
        out: list[str] = []
        if "memory" in self.tools:
            out += MEMORY_TOOLS[self.memory_mode]
        if "skills" in self.tools:
            out += SKILL_TOOLS
        return tuple(out)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": "ask",
            "tools": list(self.tools),
            "auto_approve": list(self.auto_approve),
            "memory_mode": self.memory_mode,
            "allow_groups": self.allow_groups,
        }

    def describe(self) -> str:
        """One line for the per-turn log."""
        return (
            f"mode=ask tools=[{','.join(self.tools)}] "
            f"auto_approve=[{','.join(self.auto_approve)}] "
            f"memory={self.memory_mode} groups={'on' if self.allow_groups else 'off'}"
        )


def make_policy(
    channel: str,
    *,
    tools: Iterable[str],
    auto_approve: Iterable[str] = (),
    memory_mode: str = "recall",
    allow_groups: bool = False,
) -> ChannelPolicy:
    """The policy for `channel` from its config values. Fails closed: an
    unknown memory_mode means "recall" and a malformed list means none."""
    from evi import safemode

    cats = _names(tools)
    dropped = [c for c in cats if c in UNCONTAINED]
    if dropped:
        logger.warning("%s: never offering %s to a channel (a subagent runs under "
                       "the desktop policy)", channel, ", ".join(dropped))
    cats = [c for c in cats if c not in UNCONTAINED]
    if safemode.enabled():
        cats = [c for c in cats if c not in _SAFE_MODE_DROPS]
    mode = memory_mode.strip().lower() if isinstance(memory_mode, str) else ""
    if mode not in MEMORY_MODES:
        logger.warning("%s: unknown memory_mode %r, using \"recall\"", channel, memory_mode)
        mode = "recall"
    return ChannelPolicy(
        channel=channel,
        tools=tuple(cats),
        auto_approve=tuple(_names(auto_approve)),
        memory_mode=mode,
        allow_groups=allow_groups is True,
    )


def channel_tools(policy: ChannelPolicy) -> list[Tool]:
    """The tools a channel agent is built with, in registry order."""
    from evi.tools import register_builtin_tools
    from evi.tools.base import REGISTRY

    register_builtin_tools()
    memory = set(MEMORY_TOOLS[policy.memory_mode])
    return [
        t for t in REGISTRY.values()
        if t.category in policy.tools and (t.category != "memory" or t.name in memory)
    ]


def channel_config(cfg: Config, policy: ChannelPolicy) -> Config:
    """A copy of `cfg` whose `[auto]` is the channel's policy and whose `[llm]`
    never opts in to a delegate CLI agent. The `[auto]` starts from a stock
    AutoSettings, so a widening setting it doesn't name (including any added
    later) is at its default, and keeps only what tightens."""
    user = cfg.auto
    deny_rules = [
        r for r in (getattr(user, "rules", None) or [])
        if isinstance(r, str) and r.split(None, 1)[:1] and r.split(None, 1)[0].lower() == "deny"
    ]
    stock = AutoSettings()
    protected = list(dict.fromkeys(
        [*(getattr(user, "protected_paths", None) or []), *stock.protected_paths]))
    auto = AutoSettings(
        auto_approve=list(policy.auto_approve),
        mode="ask",
        # The user's deny rules first: first match wins, so they still block a
        # channel-safe tool.
        rules=deny_rules + [f"allow {name}" for name in policy.safe_tools],
        hard_deny=list(getattr(user, "hard_deny", None) or []),
        protected_paths=protected,
    )
    # cli_agent.unattended reads this opt-in: off, a delegate backend is refused
    # up front and every delegate client built or called in the turn refuses.
    llm = copy.copy(cfg.llm)
    llm.allow_cli_agent_unattended = False
    return replace(cfg, auto=auto, llm=llm)
