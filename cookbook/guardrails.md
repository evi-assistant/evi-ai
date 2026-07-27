# Guard an agent's inputs and outputs

Put safety checks in front of every turn — block secrets before they reach the
model, redact PII in both directions, and optionally judge content against a
policy — without touching any code.

> **Uses:** configuration (`~/.evi/guardrails.toml`) · **Applies to:** the CLI,
> web, and desktop, and any agent built with `build_agent()`.

## The three layers

Guardrails run in order, and the first block wins:

1. **`[[rule]]` — regex.** Fast, deterministic, no model. `block` refuses the
   turn; `redact` replaces the match with `[REDACTED]`.
2. **`[[judge]]` — LLM-as-judge.** Sends the text to a model with a policy and
   asks whether it complies. Needs a model; **fails open** (allows) if the model
   is unavailable, so a judge outage never wedges you.
3. **`[[classifier]]` — offline ML.** A local moderation model, no network.
   Needs the `moderation` extra (`pip install evi-assistant[moderation]`); also
   fails open.

Each rule's `applies_to` is `"input"`, `"output"`, or `"both"` (the default).

## A minimal config

Copy [`examples/guardrails.toml`](../examples/guardrails.toml) to
`~/.evi/guardrails.toml` and trim it to what you need. The regex layer alone is
often enough:

```toml
# ~/.evi/guardrails.toml
enabled = true

[[rule]]
name = "api-keys"
pattern = "(api[_-]?key|secret[_-]?key|bearer )\\S+"
action = "block"
applies_to = "input"

[[rule]]
name = "emails"
pattern = "[\\w.+-]+@[\\w-]+\\.[\\w.-]+"
action = "redact"
applies_to = "both"
```

With that in place, a prompt containing an API key is refused before it reaches
the model, and any email address is `[REDACTED]` on the way in and on the way
out.

## Adding a policy judge

When a pattern can't express the rule — "don't give medical advice", "stay on
topic" — add a judge. It costs a model call per checked turn, so scope it with
`applies_to`:

```toml
[[judge]]
name = "on-topic"
policy = "The assistant only answers questions about this codebase. Refuse anything else."
applies_to = "input"
```

If the judge model is down, the turn is **allowed** rather than blocked — safety
layers here degrade toward availability, so verify with the offline regex layer
anything you truly must never let through.

## Verifying it works

`evi doctor` reports whether guardrails loaded and how many rules are active.
Then try to trip a rule from `evi chat` — paste something matching your `block`
pattern and confirm the turn is refused.

The full field reference (every key, the classifier setup, per-rule options) is
in [`docs/features/guardrails.md`](https://github.com/evi-assistant/evi-ai/blob/main/docs/features/guardrails.md).

## Where to go next

- [Review a pull request in CI](headless-ci-review.md) — combine guardrails with
  an unattended run.
