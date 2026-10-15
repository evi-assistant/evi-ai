# Chat channels (Telegram)

## Overview

A **chat channel** lets you message eVi from your phone. eVi 1.0.51 ships one:
**Telegram**. You create a bot, give eVi its token, and eVi answers messages
sent to that bot, running the model and the tools on the machine eVi runs on.
There is no mobile app to install, no public URL and no port forwarding: eVi
long-polls Telegram, so nothing on your machine is exposed to the internet.

The channel is **off until you turn it on**, and it is narrow by design:

- **Pairing first.** An unknown sender gets a pairing code and **no agent turn
  runs at all** until you approve that code on the machine eVi runs on.
  Finding the bot is not the same as being allowed to use it.
- **A toolset of its own.** Approved senders get the tool categories listed in
  `[channels] telegram_tools` (memory and skills by default), never your own
  toolset. Turning on `shell` for yourself does not widen what the bot can do.
- **Nobody to ask, so the answer is no.** A tool call that would show a
  permission prompt is denied, because nobody is at the desk to answer it.
- **No repository context.** A message from the phone runs without the
  project instructions of the folder eVi was started in, but your `hooks.toml`
  still guards it (off in safe mode).

Not to be confused with **session channels**, which push an alert into a live
web session (`POST /api/session/<id>/channel`); those are covered in
[automation.md](automation.md#channels).

## How it works

`evi web` and the desktop app start the channel at launch when it is enabled
and has a token. A background thread long-polls Telegram's Bot API. For each
plain text message (photos, joins and edits are ignored):

1. **Unknown sender** → eVi records a pairing request and replies with a code.
   Nothing else happens. Messaging again returns the same code; a code expires
   after an hour.
2. **Paired sender** → eVi runs one agent turn with the channel's toolset and
   at most `telegram_max_turns` model steps, and sends the reply back (split
   into several messages past Telegram's 4096-character limit).

Each message is answered by a fresh turn: the bot does not see your earlier
messages. Ask it to remember something (the `memory` tool) if it should carry
over.

Pairing state (who is paired, who is waiting) lives in
`~/.evi/channels/telegram.json`, not in `config.toml`, so saving settings can
never overwrite an approval list.

A bad or revoked token doesn't spin: the loop backs off (up to five minutes
between attempts) and shows the error in Settings → Channels and
`evi channel status`.

## Setup

1. In Telegram, talk to **@BotFather**, create a bot and copy its token.
2. In eVi: **Settings → Channels** (under *Extend*), paste the token, tick
   **Enable the Telegram channel** and click **Save & apply**. The channel
   starts at once; no restart.
3. Message your bot from your phone. It answers with a pairing code.
4. Approve the code: in Settings → Channels (**Waiting for approval →
   Approve**), or on the machine with `evi channel approve <code>`.
5. Message the bot again; eVi answers.

The same settings in `config.toml`:

```toml
[channels]
telegram_enabled   = false             # off by default
telegram_token     = ""                # from @BotFather; shown as ******** in Settings
telegram_tools     = ["memory", "skills"]  # tool CATEGORIES an approved sender may use
telegram_max_turns = 6                 # model steps per message
```

`telegram_tools` takes the same category names as `[tools]` (`memory`,
`skills`, `web`, `fs`, `shell`, …). Keep `shell`, `fs` and `computer` out of it
unless you fully trust everyone you pair with: anyone paired can use whatever
is listed. A repository's `.evi.toml` can't set any `[channels]` key.

## Usage

### CLI

```text
evi channel status            # on/off, running, token set, the toolset, who is paired or waiting
evi channel approve <code>    # let the sender with this pairing code talk to eVi
evi channel revoke <user-id>  # remove a paired sender (ids are listed by status)
evi channel serve             # run the channel in the foreground, without the web app (Ctrl-C stops it)
```

`evi channel serve` is for running the channel on its own; `evi web` and the
desktop app already run it when it is enabled. To set the token from a
terminal, edit `[channels]` in `config.toml` (`evi config path` prints where it
is).

### Web and desktop

**Settings → Channels** shows the channel state (off, running, not running,
needs a bot token) and the last error, the token field and the enable switch,
the toolset approved senders get, the **Waiting for approval** list with
**Approve**, and the **Paired** list with **Revoke**.

### API

| Route | What it does |
|---|---|
| `GET /api/channels/telegram` | State, toolset, paired and pending senders (never the token) |
| `POST /api/channels/telegram/approve` | `{"code": "..."}`: approve a pairing code (404 if unknown or expired) |
| `POST /api/channels/telegram/revoke` | `{"user_id": "..."}`: remove a paired sender |
| `POST /api/channels/telegram/restart` | Apply a token or enable change without restarting eVi |

## Notes / limits

- **Messages travel through Telegram.** The model and tools run on your
  machine, but what you send and what eVi replies passes through Telegram's
  servers. Don't send what you wouldn't put in a Telegram chat.
- **The token is a password for the bot.** Anyone with it can read the bot's
  messages. It is stored in `config.toml` and masked in Settings; revoke it in
  @BotFather if it leaks.
- **Text only.** Photos, voice notes and files are ignored for now.
- **One turn per message, no follow-up questions.** The bot can't ask you to
  approve a tool mid-turn; such calls are denied.
- **Coming next.** The roadmap ([roadmap.md](../roadmap.md)) gives the channel
  its own named policy with direct messages only by default and recall-only
  memory (Phase 0), then a channel runtime with per-chat sessions, in-chat
  approvals, `/stop`, progress and media (Phase 4). A second channel waits
  until that runtime exists.

## Relevant source files

- `evi/channels/telegram.py`: the polling loop, the pairing gate, the turn per message
- `evi/channels/pairing.py`: pairing codes, approvals, revocation
- `evi/channels/telegram_api.py`: the stdlib Bot API client
- `evi/config.py`: `ChannelSettings`
