# Secret redaction

## Overview

An API key a tool printed, or the model repeated, never reaches a transcript,
a log or a tool-call display. Wherever eVi writes something down or shows a
tool call, a secret becomes a marker that says what was there, never what it
was: `[REDACTED:<kind>]`, e.g. `[REDACTED:OPENAI_API_KEY]` or
`[REDACTED:github_token]`.

The model's own conversation is left alone: the live history it works from
keeps what the tools returned, so a turn that needs a value still has it.

## How it works

Two kinds of secret are redacted:

- **Token formats with a distinctive shape**: `sk-` API keys (OpenAI,
  Anthropic, DeepSeek, OpenRouter), GitHub, GitLab, AWS access key ids, Slack,
  Google, Hugging Face, npm, PyPI and Stripe tokens, other provider keys
  (`gsk_`, `xai-`, `pplx-`), JWTs, PEM private-key blocks and the credential
  of an `Authorization` / `Bearer` header. The shapes are tight, so prose like
  "task-runner" or "Bearer authentication" stays.
- **Known values**: the secrets this machine actually has, each named for
  where it came from. They are read from environment variables whose name
  ends in `_KEY`, `_TOKEN`, `_SECRET` or `_PASSWORD` (`[REDACTED:ACME_API_KEY]`),
  and from secret-named fields (`api_key`, `*_token`, `*_secret`,
  `*_password`, `Authorization`) of `config.toml`, the active profile,
  `backends.json`, `peers.json`, `users.json`, `mcp.json` and
  `routines.json` (`[REDACTED:llm.api_key]`). The director's attestation key
  is one too (`[REDACTED:director_key]`, in hex and base64).

A known value shorter than 12 characters is never redacted (a short value
turns up in ordinary text), nor is a placeholder (`sk-no-key-required`), an
`env:NAME` reference, a value with a space in it or a path to a key file. The
set follows your changes on its own: a new key in the environment or a saved
setting is redacted from the next line on.

A secret is found wherever it sits: inside JSON (escaped, after `\n`), after
a terminal colour code (`grep --color=always`), or as a tool call's
arguments, which are redacted as the JSON they are (a duplicate key or a
number holding a known value included) and stay valid JSON. Anything cut
short (a 200-character preview) is redacted first, so no prefix of a secret
shows either.

## Where it applies

- **Transcripts**: every line, as it is written (`~/.evi/transcripts/…`),
  so what `evi sessions show` and an export read from them too. Lines written
  by an earlier version stay as they were.
- **Crash reports.**
- **Web and desktop**: the `ToolCall`, `ToolResult` and `PermissionRequest`
  events, and the tool calls and results `GET /api/session/<id>/history`
  redraws.
- **Terminal**: the tool-call and result lines and the permission prompts of
  `evi chat`, and the tool lines of the other commands that stream a turn
  (`evi recipe run`, `evi review`, the voice loop).
- **`evi run`**: the tool outputs in `--format json`.
- **Scheduled tasks**: the whole log (`~/.evi/logs/scheduled/…`: the
  prompt, the reply and the tool trace) and the status `evi schedule list`
  shows.
- **Subagents**: the tool trace a subagent hands back to the agent that
  started it.

## Notes / limits

- **The model's context is not redacted.** What a tool returned goes back to
  the model as it was, and a reply the model streams is shown as it writes it
  (its transcript line is still redacted).
- **Only what eVi can see.** A secret kept somewhere eVi doesn't read (another
  tool's config, the OS keyring) is caught only by its shape. A value with no
  recognisable shape that isn't in the environment or eVi's files shows as it
  is.
- **Processes eVi's agents start** (`run_command`, `run_python`, `monitor`,
  delegate CLI agents) carry `EVI_AGENT=1` in their environment. Director
  tooling refuses to run, and never reads the director key, in such a process
  or anything it starts; the shell guard refuses a command that sets or clears
  it.

## Relevant source files

- `evi/redact.py`: the token formats, the known-value set, `Redactor` (and its
  `args` / `tool_calls` for tool-call arguments)
- `evi/transcripts.py`, `evi/reporting.py`: transcripts and crash reports
- `evi/apps/web/server.py`, `evi/apps/cli/main.py`, `evi/headless.py`: the
  tool-call displays
- `evi/scheduler.py`, `evi/llm/subagent.py`: scheduled-task logs, subagent
  traces
- `evi/proc.py`: `EVI_AGENT` on every process an agent starts
