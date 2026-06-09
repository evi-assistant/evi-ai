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

    [[judge]]                    # semantic — graded by the LLM, not a regex
    name = "no-self-harm"
    policy = "Requests for, or content encouraging, self-harm or suicide."
    applies_to = "both"

`[[judge]]` rules are the semantic layer: instead of a regex, an LLM classifies
the text against the `policy` and blocks on a match. They only run when the
caller passes a `judge_fn` (the agent supplies one backed by its own model), so
the filter stays local + model-free here. Judge rules are block-only (you can't
redact a span the regex didn't find); a judge error fails *open* (skips the
rule) so a flaky model can't wedge the chat.

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
class JudgeRule:
    """A semantic rule graded by an LLM rather than a regex."""

    name: str
    policy: str                  # description of what's disallowed
    applies_to: str = "both"     # "input" | "output" | "both"

    def covers(self, direction: str) -> bool:
        return self.applies_to in (direction, "both")


@dataclass
class GuardrailResult:
    allowed: bool                 # False => a block rule matched
    text: str                     # possibly-redacted text
    blocked_by: list[str] = field(default_factory=list)   # rule names
    redacted_by: list[str] = field(default_factory=list)  # rule names
    notes: list[str] = field(default_factory=list)        # judge reasons, etc.

    @property
    def changed(self) -> bool:
        return bool(self.blocked_by or self.redacted_by)


class Guardrails:
    """Holds the rule set and applies it to text in a direction."""

    def __init__(
        self,
        rules: list[GuardrailRule],
        *,
        judge_rules: list[JudgeRule] | None = None,
        enabled: bool = True,
    ) -> None:
        self.rules = rules
        self.judge_rules = judge_rules or []
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

        judge_rules: list[JudgeRule] = []
        for raw in data.get("judge", []):
            policy = (raw.get("policy") or "").strip()
            if not policy:
                continue
            name = (raw.get("name") or policy[:24]).strip()
            applies_to = (raw.get("applies_to") or "both").strip().lower()
            if applies_to not in ("input", "output", "both"):
                applies_to = "both"
            judge_rules.append(JudgeRule(name=name, policy=policy, applies_to=applies_to))

        enabled = bool(data.get("enabled", True)) and bool(rules or judge_rules)
        return cls(rules=rules, judge_rules=judge_rules, enabled=enabled)

    def check(self, text: str, direction: str, judge_fn=None) -> GuardrailResult:
        """Apply all rules covering `direction` ('input' or 'output').

        Regex rules run first (block/redact). Then, if `judge_fn(policy, text)
        -> (allowed, reason)` is supplied and there are `[[judge]]` rules for
        this direction (and nothing already blocked), the semantic layer runs.
        A judge that raises fails *open* — the rule is skipped, not the turn.
        """
        if not self.enabled or not text:
            return GuardrailResult(allowed=True, text=text)
        out = text
        blocked: list[str] = []
        redacted: list[str] = []
        notes: list[str] = []
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

        # Semantic layer — only when a grader is supplied and regex didn't block.
        if judge_fn is not None and not blocked:
            for jr in self.judge_rules:
                if not jr.covers(direction):
                    continue
                try:
                    allowed, reason = judge_fn(jr.policy, out)
                except Exception:
                    continue  # fail open: a flaky grader can't wedge the turn
                if not allowed:
                    blocked.append(jr.name)
                    if reason:
                        notes.append(f"{jr.name}: {reason}")

        return GuardrailResult(
            allowed=not blocked,
            text=out,
            blocked_by=blocked,
            redacted_by=redacted,
            notes=notes,
        )
