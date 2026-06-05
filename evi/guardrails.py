"""Guardrails — a lightweight, local content-filter layer.

Borrowed in spirit from Bedrock Guardrails / Gemini safety settings, but
regex-first and fully local. Off by default. Rules live in
`~/.evi/guardrails.toml`:

    enabled = true

    [[rule]]
    name = "block-secrets"
    pattern = "(?i)(api[_-]?key|secret)\\\\s*[:=]"
    action = "block"        # block | redact
    applies_to = "input"    # input | output | both

    [[rule]]
    name = "redact-emails"
    pattern = "[\\\\w.+-]+@[\\\\w-]+\\\\.[\\\\w.-]+"
    action = "redact"
    applies_to = "both"

Semantics:
- **input** rules run on the user's message BEFORE it hits the LLM. A
  `block` match refuses the turn (no LLM call); a `redact` match replaces
  the matched spans with `[REDACTED]` and proceeds.
- **output** rules run on the assistant's final text AFTER streaming. We
  can't un-stream, so a `block` match replaces the stored history content
  and flags it; `redact` rewrites the stored content. Either way the
  caller is told so the UI can surface a warning.

This is a guardrail, not airtight security — a determined model or user
can phrase around regexes. It's meant for shared/kiosk installs and
"don't paste my API keys into the model" hygiene.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from evi.config import HOME

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


GUARDRAILS_PATH = HOME / "guardrails.toml"

_REDACTION = "[REDACTED]"


@dataclass
class GuardrailRule:
    name: str
    pattern: str
    action: str = "block"        # "block" | "redact"
    applies_to: str = "both"     # "input" | "output" | "both"
    _compiled: re.Pattern | None = field(default=None, repr=False, compare=False)

    def compiled(self) -> re.Pattern:
        if self._compiled is None:
            self._compiled = re.compile(self.pattern)
        return self._compiled

    def covers(self, direction: str) -> bool:
        return self.applies_to in (direction, "both")


@dataclass
class GuardrailResult:
    allowed: bool                 # False => a block rule matched
    text: str                     # possibly-redacted text
    blocked_by: list[str] = field(default_factory=list)   # rule names
    redacted_by: list[str] = field(default_factory=list)  # rule names

    @property
    def changed(self) -> bool:
        return bool(self.blocked_by or self.redacted_by)


class Guardrails:
    """Holds the rule set and applies it to text in a direction."""

    def __init__(self, rules: list[GuardrailRule], *, enabled: bool = True) -> None:
        self.rules = rules
        self.enabled = enabled

    @classmethod
    def load(cls, path: Path | None = None) -> "Guardrails":
        """Load from `~/.evi/guardrails.toml`. Missing file → disabled."""
        p = path or GUARDRAILS_PATH
        if not p.is_file():
            return cls(rules=[], enabled=False)
        try:
            with p.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            return cls(rules=[], enabled=False)
        rules: list[GuardrailRule] = []
        for raw in data.get("rule", []):
            pattern = (raw.get("pattern") or "").strip()
            if not pattern:
                continue
            name = (raw.get("name") or pattern[:24]).strip()
            action = (raw.get("action") or "block").strip().lower()
            if action not in ("block", "redact"):
                action = "block"
            applies_to = (raw.get("applies_to") or "both").strip().lower()
            if applies_to not in ("input", "output", "both"):
                applies_to = "both"
            rule = GuardrailRule(name=name, pattern=pattern, action=action, applies_to=applies_to)
            try:
                rule.compiled()  # validate regex now; skip bad ones
            except re.error:
                continue
            rules.append(rule)
        return cls(rules=rules, enabled=bool(data.get("enabled", True)) and bool(rules))

    def check(self, text: str, direction: str) -> GuardrailResult:
        """Apply all rules covering `direction` ('input' or 'output')."""
        if not self.enabled or not text:
            return GuardrailResult(allowed=True, text=text)
        out = text
        blocked: list[str] = []
        redacted: list[str] = []
        for rule in self.rules:
            if not rule.covers(direction):
                continue
            rx = rule.compiled()
            if not rx.search(out):
                continue
            if rule.action == "block":
                blocked.append(rule.name)
            else:  # redact
                out = rx.sub(_REDACTION, out)
                redacted.append(rule.name)
        return GuardrailResult(
            allowed=not blocked,
            text=out,
            blocked_by=blocked,
            redacted_by=redacted,
        )
