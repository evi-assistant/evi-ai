# Content Guardrails

## Overview

Content Guardrails are a lightweight, **fully local** content-filter layer that
sits between you and the model. They let you block or scrub specific content on
the way *in* (your message, before it reaches the LLM) and on the way *out* (the
assistant's reply, after it streams). Think of it as a regex-first, locally
graded counterpart to hosted moderation features like Bedrock Guardrails or
Gemini safety settings — but nothing leaves your machine.

Typical reasons to turn this on:

- **Secret hygiene** — stop yourself from pasting API keys, tokens, or
  passwords into the model ("don't send my secrets to the backend").
- **PII redaction** — automatically mask emails, phone numbers, etc. in both
  directions.
- **Shared / kiosk installs** — apply a basic safety policy (self-harm,
  toxicity) when other people use the assistant.

Guardrails are **off by default**. They only activate when you create
`~/.evi/guardrails.toml` with `enabled = true` and at least one rule.

> This is a guardrail, not airtight security. A determined model or user can
> phrase around a regex. It is meant for hygiene and shared-machine policy, not
> as a hard security boundary.

## How it works

Guardrails load from a single TOML file and apply to text in a **direction**:

- **input** — runs on your raw message *before* it hits the LLM. A `block`
  match refuses the turn entirely (no LLM call). A `redact` match replaces the
  matched spans with `[REDACTED]` and proceeds.
- **output** — runs on the assistant's final text *after* streaming finishes.
  Because the text has already streamed to the screen, eVi can't un-stream it —
  but it cleans the **stored** copy so it can't poison later turns or the
  transcript, and flags the turn so the UI can show a warning. A `block` match
  replaces the stored content with `[output blocked by guardrail: …]`; a
  `redact` match rewrites the stored content.

Three rule kinds **layer together** and run in a fixed order, **stopping at the
first block**:

1. **`[[rule]]` — regex.** Fast, deterministic, fully local. Can `block` or
   `redact`. Regexes are compiled and validated at load time; invalid ones are
   silently skipped.
2. **`[[judge]]` — semantic, LLM-graded.** eVi's *own* model classifies the
   text against a plain-English `policy` you write and blocks on a match. It
   runs as a single, separate, non-streaming model round-trip per turn
   (temperature 0, no tools), so it stays local. **Block-only** (no redact).
3. **`[[classifier]]` — offline ML model.** A local HuggingFace
   text-classification model scores the text; eVi blocks when a score for one of
   your `labels` crosses `threshold`. Fully offline after the first download.
   **Block-only.**

Important behavior of the two semantic layers (`judge` and `classifier`):

- They **only run if the previous layers didn't already block** — once
  something blocks, eVi stops.
- They **fail open**: if the grader model is missing, errors, or is flaky, that
  rule is *skipped* — it never wedges your turn. (A missing classifier
  dependency, a model that won't load, or an exception all just skip the rule.)

## Setup

### Config file

All guardrail config lives in one file:

- **Path:** `~/.evi/guardrails.toml` (Windows: `%USERPROFILE%\.evi\guardrails.toml`)
- If the file is missing or unparseable, guardrails are **disabled**.
- `evi guardrails path` prints the exact path on your system.

Top-level key:

| Key       | Type | Default | Notes |
|-----------|------|---------|-------|
| `enabled` | bool | `true`  | Master switch. Even with `enabled = true`, guardrails stay off unless at least one rule/judge/classifier is defined. |

### `[[rule]]` — regex rules

| Key          | Default          | Notes |
|--------------|------------------|-------|
| `pattern`    | (required)       | A Python `re` regex. Empty/invalid patterns are skipped. |
| `name`       | first 24 chars of pattern | Label shown in listings/warnings. |
| `action`     | `"block"`        | `block` or `redact`. Anything else falls back to `block`. |
| `applies_to` | `"both"`         | `input`, `output`, or `both`. Anything else falls back to `both`. |

### `[[judge]]` — semantic LLM rules

| Key          | Default          | Notes |
|--------------|------------------|-------|
| `policy`     | (required)       | Plain-English description of what's disallowed. Empty → rule skipped. |
| `name`       | first 24 chars of policy | Label. |
| `applies_to` | `"both"`         | `input`, `output`, or `both`. Block-only. |

`[[judge]]` rules use your configured chat model (`[llm] model` in
`config.toml`) as the grader — no extra setup, but they add one model
round-trip per turn they run on.

### `[[classifier]]` — offline ML rules

| Key          | Default               | Notes |
|--------------|-----------------------|-------|
| `model`      | `""` → `unitary/toxic-bert` | Any HuggingFace text-classification model id. |
| `name`       | `"classifier"`        | Label. |
| `labels`     | `[]` (any label)      | Lower-cased; block when any listed label crosses threshold. `[]` = block on any label. |
| `threshold`  | `0.5`                 | Score (0–1) at/above which the rule blocks. |
| `applies_to` | `"both"`              | `input`, `output`, or `both`. Block-only. |

**Optional dependency.** `[[classifier]]` rules need `transformers` + `torch`:

```bash
pip install 'evi-assistant[moderation]'
```

The model loads lazily (first use downloads weights; cached afterward) and runs
fully offline thereafter. If the deps or model aren't available, classifier
rules simply fail open and are skipped. Downloaded model weights live under
`~/.evi/models/`.

## Usage

### CLI — `evi guardrails`

There are three subcommands for inspecting and dry-running your config (none of
them edit the file — you hand-edit `guardrails.toml`):

```bash
evi guardrails path                       # print the config file path
evi guardrails list                       # list loaded rules + enabled state
evi guardrails test "<text>"              # dry-run text through the rules
evi guardrails test "<text>" --direction input    # input | output | both (default both)
```

`evi guardrails test` shows the verdict (`allowed` / `BLOCKED`), which rules
blocked or redacted, and the redacted result text. Note that `test` exercises
the **regex layer only** — it does not invoke the `[[judge]]` or
`[[classifier]]` graders.

### In chat (CLI and Web)

Guardrails are applied automatically whenever they're enabled — there's no
per-turn flag to toggle. Both the interactive CLI chat and the web UI load
`~/.evi/guardrails.toml` at session start and wire it into the agent.

- **CLI chat / REPL:** if a rule fires, you'll see the guardrail event inline;
  a blocked input ends the turn before any model call.
- **Web UI:** guardrail events render as a warning bubble, e.g.
  `⚠ guardrail (input): …`, describing the direction and what fired.

There is **no slash command** to enable/disable guardrails — control them by
editing the config file (and `enabled`).

### Web / Desktop — the Guardrails editor

The web and desktop apps have a **Settings → Guardrails** panel that shows the
enabled state and a rule-count summary (regex / judge / classifier), plus a
`guardrails.toml` editor. **Save** validates the TOML server-side (each
`[[rule]]` regex must compile and each `[[judge]]` must have a `policy`) and
reports the error inline rather than writing a broken file. It is backed by
`GET`/`POST /api/guardrails`.

## Examples

### Example 1 — Block secrets on input, redact emails both ways

`~/.evi/guardrails.toml`:

```toml
enabled = true

[[rule]]
name = "block-secrets"
pattern = "(?i)(api[_-]?key|secret|password)\\s*[:=]"
action = "block"          # refuse the whole turn — never send it to the model
applies_to = "input"

[[rule]]
name = "redact-emails"
pattern = "[\\w.+-]+@[\\w-]+\\.[\\w.-]+"
action = "redact"         # mask the match, then continue
applies_to = "both"
```

Verify it loaded and dry-run some text:

```bash
$ evi guardrails list
guardrails: enabled

  block-secrets block (input) — /(?i)(api[_-]?key|secret|password)\s*[:=]/
  redact-emails redact (both) — /[\w.+-]+@[\w-]+\.[\w.-]+/

$ evi guardrails test "my api_key= sk-12345 and email is me@example.com"
input: BLOCKED
  blocked by: block-secrets

$ evi guardrails test "ping me@example.com about the docs" --direction output
output: allowed
  redacted by: redact-emails
  result: ping [REDACTED] about the docs
```

### Example 2 — Semantic judge + offline toxicity classifier

`~/.evi/guardrails.toml`:

```toml
enabled = true

[[judge]]                  # graded by your local LLM, no extra install
name = "no-self-harm"
policy = "Requests for, or content encouraging, self-harm or suicide."
applies_to = "both"

[[classifier]]             # offline HF model; needs evi-assistant[moderation]
name = "toxicity"
model = "unitary/toxic-bert"            # "" also resolves to this default
labels = ["toxic", "threat", "insult"]  # [] would block on ANY returned label
threshold = 0.7
applies_to = "both"
```

Install the optional classifier deps, then confirm the rules are loaded:

```bash
$ pip install 'evi-assistant[moderation]'

$ evi guardrails list
guardrails: enabled

  no-self-harm judge (both) — Requests for, or content encouraging, self-harm or…
  toxicity classifier (both) — unitary/toxic-bert >= 0.7 on toxic, insult, threat
```

These two layers run live during chat (`evi chat` or the web UI). Remember that
`evi guardrails test` only checks regex rules, so it will report `allowed` for
text that the judge or classifier would catch at runtime — exercise those in an
actual chat turn.

## Notes / limits

- **Off by default.** No `guardrails.toml`, or `enabled = false`, or zero
  rules → guardrails do nothing. A malformed TOML file also disables them
  (rather than erroring).
- **Fail-open semantic layers.** `[[judge]]` and `[[classifier]]` never block a
  turn due to their *own* failure — a missing model, missing
  `[moderation]` deps, a load error, or a flaky response all cause the rule to
  be skipped, not the turn refused. The deterministic `[[rule]]` regex layer
  does *not* fail open: a matching block rule always blocks.
- **First-block-wins ordering.** Layers run regex → judge → classifier and stop
  at the first block, so a later layer won't double-flag content an earlier one
  already blocked. Redactions from the regex layer still apply before the
  semantic layers run.
- **Output is post-stream.** Output guardrails can't retract text already shown
  on screen; they sanitize the *stored* history/transcript and flag the turn.
  Treat output rules as "keep the bad text out of memory and warn me," not as a
  hard pre-display filter.
- **Block-only for semantic kinds.** `[[judge]]` and `[[classifier]]` can only
  block; only `[[rule]]` regexes can `redact`.
- **Judge cost.** Each `[[judge]]` rule that runs adds a separate model
  round-trip (temperature 0, ~120 tokens) per turn — cheap locally, but not
  free.
- **Classifier input is capped** at the first 2000 characters of the text
  before scoring; long inputs are truncated for classification.
- **`test` is regex-only.** `evi guardrails test` is a dry run of the regex
  layer; it does not call the judge or classifier graders.
- **Not airtight.** Regexes can be evaded by rephrasing, and the semantic layers
  are only as good as your local model / classifier. Use guardrails for hygiene
  and shared-machine policy, not as a security boundary.
