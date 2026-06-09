# Sessions, Resume, Handoff, Checkpoints

## Overview

eVi keeps a durable record of your conversations and the file changes it makes, so you can pick up where you left off and undo mistakes.

- **Sessions / transcripts** — every chat is written to disk as a JSONL file, one file per session per day. You can list, view, export, and reopen them.
- **Resume / continue / fork** — reopen a past session and keep talking in the same file, jump back into the most recent session, or branch a copy into a brand-new session while leaving the original untouched.
- **Handoff** — print the exact command + URL to pick a session up on another device after syncing.
- **Checkpoints / rewind** — every file eVi writes via its `write_file` tool is journalled with the file's prior contents, so a bad edit can be rolled back.

Why you'd use it: nothing is lost when you close the terminal or browser, you can move a conversation between machines, you keep an auditable record of what was said, and you can undo file writes eVi made without reaching for `git`.

## How it works

### Transcripts

When the `transcripts` tool toggle is on (the default), each turn is appended to:

```
~/.evi/transcripts/<YYYY-MM-DD>/<session_id>.jsonl
```

Each line is a JSON object for one message (`role` = `user` / `assistant` / `tool` / `system`, plus `content`, a `ts` timestamp, and — for tool turns — `tool_name`, or `tool_calls` on assistant turns). Because writes happen per turn, the on-disk copy is always current as of the last completed turn.

Listing scans the day directories newest-first and summarizes each file: message count, first user message (the injected `[ongoing goal: …]` prefix is stripped from the summary), and start/end timestamps derived from the `ts` fields.

**Resume is a best-effort reconstruction.** eVi rebuilds an in-memory history from the JSONL in order and re-emits user/assistant/tool messages (synthesizing `tool_call_id`s where the original wasn't logged). It does **not** restore the backend's KV cache (the next message is a fresh prompt to the model) nor transient flags like an active goal or one-shot plan mode — re-set those if you need them.

- **resume** and **continue** reuse the same `session_id`, so new messages append to the **same** transcript file.
- **fork** loads the history into a **fresh** agent with a new auto-generated `session_id`, so the original file is left intact and new turns go to a new file.

In the web UI, a session id arriving via `/?session=<id>` is revived from disk on first use: if a transcript for that id exists, its history is loaded into a fresh `Agent` before the next message.

### Checkpoints

Every `write_file` operation records the file's **prior** state first, under:

```
~/.evi/checkpoints/journal.jsonl     # one entry per write: {seq, ts, path, op, blob?, size?}
~/.evi/checkpoints/blobs/<sha256>    # prior contents, content-addressed (dedup'd)
```

The `op` is one of:

- `create` — the file didn't exist before, so undo **deletes** it.
- `modify` — prior bytes were saved as a blob, so undo **restores** them.
- `skip` — the file was larger than 5 MiB, so it was **not** snapshotted and cannot be restored.

`rewind` walks entries with `seq >= target` newest-first, restoring/deleting each, then trims those entries from the journal — so a second rewind continues further back. Checkpointing is best-effort: a checkpoint failure never blocks the actual write.

## Setup

Config lives in `~/.evi/config.toml`. Transcripts are controlled by the `transcripts` key under the `[tools]` section:

```toml
[tools]
transcripts = true     # write session JSONL under ~/.evi/transcripts/
```

- **Default:** `transcripts = true`. If you turn it off, there are no session files to list, show, resume, fork, continue, or hand off.
- **No extra pip extras** are required for sessions or checkpoints — they are plain JSONL files in your eVi home.
- **eVi home** defaults to `~/.evi` and can be relocated with the `EVI_HOME` environment variable; transcripts and checkpoints then live under that directory.
- **Checkpoints** have no config toggle of their own — they are recorded automatically whenever eVi uses its `write_file` tool. Snapshots are capped at 5 MiB per file (larger writes are journalled as `skip`).

Relevant paths:

| Data | Path |
|------|------|
| Session transcripts | `~/.evi/transcripts/<YYYY-MM-DD>/<session_id>.jsonl` |
| Checkpoint journal | `~/.evi/checkpoints/journal.jsonl` |
| Checkpoint blobs | `~/.evi/checkpoints/blobs/<sha256>` |

## Usage

### CLI — `evi sessions`

```text
evi sessions list [--days 7] [--limit 20]    # recent sessions, newest first
evi sessions show <session_id>               # print the full transcript
evi sessions export <session_id> [-f md|html|json] [-o PATH]
evi sessions resume <session_id>             # reopen and continue in the SAME file
evi sessions continue                        # resume the most recently active session
evi sessions fork <session_id>               # branch into a NEW session, original intact
evi sessions handoff [session_id]            # print resume cmd + URL for another device
evi sessions title <session_id>              # LLM-generated short title for a session
```

- `list` defaults to the last **7** days and **20** entries; `export` defaults to `md` and prints to stdout unless you pass `--out/-o`.
- `handoff` defaults to the most recent session when no id is given.

### CLI — `evi rewind` (checkpoints)

```text
evi rewind --list        # (or -l) list recent file checkpoints with their seq numbers
evi rewind               # undo just the latest write
evi rewind <seq>         # undo all writes with seq >= <seq>
```

`rewind` with no seq (or `0`) undoes only the most recent write.

### REPL slash command

Inside an interactive chat (`evi chat`), `/recent [n]` lists recent sessions (read-only; default 8, scans the last 30 days). It prints short ids and reminds you to resume with `evi sessions resume <id>` after `/exit`, or via the `evi link <id>` deep link.

### Web UI

- Open a specific session directly with `/?session=<session_id>` (the desktop app also handles the `evi://session/<id>` deep link). Its history is revived from the on-disk transcript.
- The web chat surfaces a **rewind** dialog to undo file writes (the browser equivalent of `evi rewind`).

## Examples

### Example 1 — list, peek, resume, and hand off to a laptop

```console
$ evi sessions list --days 14 --limit 5
a1b2c3d4  2026-06-08 21:14   17 msgs  refactor the auth middleware
9f8e7d6c  2026-06-07 09:02    6 msgs  draft a release note for v0.31.0

$ evi sessions show a1b2c3d4 | head            # inspect before reopening

$ evi sessions resume a1b2c3d4
resumed a1b2c3d4 (16 messages restored)
> …continue the conversation here; new turns append to the same file…
```

To continue it later on another machine:

```console
$ evi sessions handoff a1b2c3d4
handoff a1b2c3d4 (17 messages)
  1. sync this device:   evi sync push
  2. on the other device: evi sync pull, then either
       evi sessions resume a1b2c3d4
       or open http:///?session=a1b2c3d4 in the web UI
```

Run `evi sync push` here, `evi sync pull` on the other device, then either resume command works because the transcript file travels with the sync.

### Example 2 — fork a session, export it, then undo a bad file write

Branch a copy so the original stays clean, and save a readable archive:

```console
$ evi sessions fork a1b2c3d4
forked a1b2c3d4 → new session b7c8d9e0 (16 messages copied)

$ evi sessions export a1b2c3d4 --format md --out ~/notes/auth-chat.md
wrote /home/you/notes/auth-chat.md
```

If eVi made a file edit you want to undo:

```console
$ evi rewind --list
     7 21:40:11 modify /home/you/project/app.py
     8 21:41:02 create /home/you/project/new_helper.py

$ evi rewind 7        # undo seq 7 and everything after it (7 and 8)
✓ deleted (was newly created) — /home/you/project/new_helper.py
✓ restored — /home/you/project/app.py
```

`evi rewind` alone would have undone only seq 8; passing `7` rolls back from that point onward.

### Example 3 — make sure transcripts are enabled

If `evi sessions list` says no sessions are found, confirm the toggle:

```toml
# ~/.evi/config.toml
[tools]
transcripts = true
```

## Notes / limits

- **Resume is not perfect.** The KV cache is not restored (the next prompt is fresh), and transient flags like an active goal or one-shot plan mode are dropped — re-set them if needed. Tool-call ids are synthesized on resume when the original wasn't logged.
- **Transcripts must be on.** With `transcripts = false` there is nothing to list, show, resume, fork, continue, or hand off. The CLI hints at this when the list is empty.
- **resume/continue write back to the original file; fork does not.** Use `fork` when you want to explore a branch without altering the source session.
- **Handoff relies on sync.** The on-disk copy is only as current as the last completed turn, and the other device only sees it after `evi sync pull`. Always `evi sync push` from the source first.
- **Checkpoints are best-effort and fail-open.** A checkpoint failure never blocks the actual write — so in rare cases a write may not be undoable.
- **5 MiB snapshot cap.** Writes over 5 MiB are journalled as `skip` and **cannot** be rewound (the listing shows them, but rewind reports "could not restore (not snapshotted)").
- **Rewind is destructive and not itself undoable.** It overwrites current file contents with the saved prior bytes (or deletes newly created files) and trims the journal, so undone entries are gone; there is no "redo."
- **Scope of checkpoints.** Only files eVi writes through its own `write_file` tool are journalled — edits you make by hand, or changes from shell commands, are not captured here. Use git for general version control.
- **Privacy.** Transcripts and checkpoint blobs are plain files under your eVi home (`~/.evi`, or `$EVI_HOME`). They contain your full conversation text and prior file contents in the clear; treat that directory as sensitive, and remember that `export` (md/html/json) produces an unencrypted copy wherever you write it.
