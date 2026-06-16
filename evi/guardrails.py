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

    [[classifier]]               # offline ML moderation model
    name = "toxicity"
    model = "unitary/toxic-bert" # any HF text-classification model ("" = default)
    labels = ["toxic", "threat", "insult"]   # labels that block ([] = any)
    threshold = 0.7
    applies_to = "both"

Three rule kinds layer together (regex → judge → classifier):
- `[[rule]]` regex — fast, deterministic, can block OR redact.
- `[[judge]]` — an LLM classifies the text against `policy` (needs a `judge_fn`,
  supplied by the agent's own model). Block-only.
- `[[classifier]]` — a local HuggingFace text-classification model scores the
  text; blocks when a `labels` score crosses `threshold` (needs a `classify_fn`;
  install `evi-assistant[moderation]`). Block-only, fully offline.

The semantic kinds only run when their injected fn is provided, so this module
stays model-free. Both fail *open* (a missing/flaky model skips the rule, not
the turn).

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
from dataclasses import dataclass, field
from pathlib import Path

from evi.config import HOME

import tomllib


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
class ClassifierRule:
    """A rule scored by a local ML text-classification model."""

    name: str
    model: str = ""                          # HF model id ("" → default)
    labels: list[str] = field(default_factory=list)  # block labels ([] = any)
    threshold: float = 0.5
    applies_to: str = "both"

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
        classifier_rules: list[ClassifierRule] | None = None,
        enabled: bool = True,
    ) -> None:
        self.rules = rules
        self.judge_rules = judge_rules or []
        self.classifier_rules = classifier_rules or []
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

        classifier_rules: list[ClassifierRule] = []
        for raw in data.get("classifier", []):
            name = (raw.get("name") or "classifier").strip()
            model = str(raw.get("model", "")).strip()
            raw_labels = raw.get("labels") or []
            labels = [str(x).lower() for x in raw_labels] if isinstance(raw_labels, list) else []
            try:
                threshold = float(raw.get("threshold", 0.5))
            except (TypeError, ValueError):
                threshold = 0.5
            applies_to = (raw.get("applies_to") or "both").strip().lower()
            if applies_to not in ("input", "output", "both"):
                applies_to = "both"
            classifier_rules.append(
                ClassifierRule(name=name, model=model, labels=labels,
                               threshold=threshold, applies_to=applies_to)
            )

        enabled = bool(data.get("enabled", True)) and bool(
            rules or judge_rules or classifier_rules
        )
        return cls(
            rules=rules,
            judge_rules=judge_rules,
            classifier_rules=classifier_rules,
            enabled=enabled,
        )

    def check(self, text: str, direction: str, judge_fn=None, classify_fn=None) -> GuardrailResult:
        """Apply all rules covering `direction` ('input' or 'output').

        Layers, in order, stopping at the first block: regex `[[rule]]`
        (block/redact) → `[[judge]]` (needs `judge_fn(policy, text) ->
        (allowed, reason)`) → `[[classifier]]` (needs `classify_fn(model, text)
        -> {label: score}`). The semantic layers only run when their fn is
        supplied; either failing raises are swallowed (fail *open*) so a flaky
        model skips the rule, not the turn.
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

        # Offline-classifier layer — same gating (only if nothing blocked yet).
        if classify_fn is not None and not blocked:
            for cr in self.classifier_rules:
                if not cr.covers(direction):
                    continue
                try:
                    scores = classify_fn(cr.model, out) or {}
                except Exception:
                    continue  # fail open
                pairs = [
                    (lbl, sc) for lbl, sc in scores.items()
                    if not cr.labels or str(lbl).lower() in cr.labels
                ]
                if not pairs:
                    continue
                lbl, top = max(pairs, key=lambda kv: kv[1])
                if top >= cr.threshold:
                    blocked.append(cr.name)
                    notes.append(f"{cr.name}: {lbl} {top:.2f}")

        return GuardrailResult(
            allowed=not blocked,
            text=out,
            blocked_by=blocked,
            redacted_by=redacted,
            notes=notes,
        )


# --- editor helpers (the Web guardrails editor reads/validates/writes raw) ---


def read_raw(path: Path | None = None) -> str:
    """The raw guardrails.toml text ('' when the file is absent)."""
    p = path or GUARDRAILS_PATH
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except OSError:
        return ""


def validate(text: str) -> str | None:
    """Validate guardrails TOML. Returns an error string, or None if valid
    (parses + every regex rule compiles + judge rules have a policy)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return f"invalid TOML: {exc}"
    if not isinstance(data, dict):
        return "guardrails.toml must be a table"
    for r in data.get("rule", []) or []:
        pat = (r.get("pattern") or "").strip() if isinstance(r, dict) else ""
        if not pat:
            return "a [[rule]] is missing a non-empty pattern"
        try:
            re.compile(pat)
        except re.error as exc:
            return f"bad regex in rule {r.get('name', pat)!r}: {exc}"
    for j in data.get("judge", []) or []:
        if not (isinstance(j, dict) and str(j.get("policy", "")).strip()):
            return "a [[judge]] rule is missing a non-empty policy"
    return None


def write_raw(text: str, path: Path | None = None) -> None:
    """Persist guardrails.toml (caller should validate() first)."""
    p = path or GUARDRAILS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
