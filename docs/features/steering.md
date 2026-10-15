# Steering a running turn

## Overview

You can type while eVi is still working. In the web and desktop app a message
sent during a turn is **queued and applied at the turn's next step**, after
the tool eVi is running finishes, instead of interrupting the work or being
lost behind a disabled Send button. "Correct it while it works" replaces "wait,
or stop and start over".

- While a turn runs, the **Send** button reads **Queue**.
- Your message appears in the conversation at once, tagged *queued — applies
  at the next step*, and the tag changes to *applied* when it lands.
- **Nothing is lost.** If the turn finishes before there is a next step (the
  model just answered, or the turn ended on an error or its step limit), the
  queued message is sent as a normal message instead.

## How it works

A turn is a loop: the model replies, eVi runs the tools it asked for, and the
model sees the results in the next round. A steering message waits in a queue
on the session's agent. At the top of each round, before the model is called
again, eVi appends everything queued to the conversation as your messages,
writes them to the transcript and emits a `Steering` event (`applied: true`)
on the turn's event stream; the model reads them together with the tool
results.

When the turn ends, anything still queued is handed back as `Steering` events
with `applied: false` — always before the turn's closing `Done` (or `Error`)
event, so `Done` is the last frame — and the web UI resends each one as a
normal message. A re-roll hands queued follow-ups back the same way.

Steering never cancels anything. A long tool (a big download, a slow test run)
finishes first; the correction applies right after it. Stopping a turn from
every surface is on the roadmap ([roadmap.md](../roadmap.md), Phase 4).

## Usage

### Web and desktop

Type and press Enter (or click **Queue**) while a turn is running. Use it for
corrections ("not that file, the one in `src/`"), extra constraints ("and keep
the public API unchanged") or answers to a question the model asked in passing.

### API

```http
POST /api/session/<session_id>/steer
Content-Type: application/json

{"text": "use the staging config instead"}
```

| Response | Meaning |
|---|---|
| `{"queued": true, "pending": 1}` | Queued; applies at the next step (`pending` counts what is waiting) |
| `{"queued": false, "reason": "not busy"}` | No turn is running: send it as a normal message (`POST /api/chat`) |

An empty `text` is a 400. The `Steering` events arrive on the stream of the
`POST /api/chat` request that runs the turn: `{"kind": "Steering", "text":
"...", "applied": true}`.

## Notes / limits

- **Web and desktop only.** The terminal REPL and `evi run` have no steering.
- **Applied between steps, not mid-step.** A model that is still writing its
  answer finishes it first; if it then makes no tool call the turn ends, and
  your message is sent as the next turn.
- **Text only.** A message with an attached image, or a `/command`, typed
  during a turn is not queued as steering.

## Relevant source files

- `evi/llm/agent.py`: `Agent.steer`, `take_pending_steering`, the `Steering` event
- `evi/apps/web/server.py`: `POST /api/session/{session_id}/steer`, leftovers at turn end
- `evi/apps/web/static/index.html`: the Queue button and the queued/applied tags
