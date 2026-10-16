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
- **Direct messages only.** A message in a group gets no reply, and an unknown
  sender in a group gets no pairing code, unless you set `allow_groups`.
  Senders that stand for "someone" rather than one person (an anonymous group
  admin, a post forwarded from a channel) are never answered.
- **A policy of its own.** Approved senders get the tool categories listed in
  `[channels] telegram_tools` (memory and skills by default), never your own
  toolset, and run under the channel's own permission policy, never `[auto]`.
  Turning on `shell`, auto-approving `code` or switching to yolo for yourself
  does not widen what the bot can do.
- **Nobody to ask, so the answer is no.** A tool call that would show a
  permission prompt is denied, because nobody is at the desk to answer it.
  Only the categories you list in `telegram_auto_approve` (none by default)
  and the channel's own memory and skill reads run.
- **Recall-only memory.** By default the bot can read and search your
  long-term memory but not change it (`memory_mode`).
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

1. **Shared sender** (an anonymous group admin, Telegram's service account
   777000, a message sent on behalf of a channel, or another bot) → ignored.
2. **Group chat** → ignored, unless `allow_groups` is on and the sender is
   paired. Telegram's own `private` chat type is the only thing that counts
   as a direct message.
3. **Unknown sender in a direct chat** → eVi records a pairing request and
   replies with a code. Nothing else happens. Messaging again returns the same
   code; a code expires after an hour.
4. **Paired sender** → eVi runs one agent turn under the channel's policy, with
   at most `telegram_max_turns` model steps, and sends the reply back (split
   into several messages past Telegram's 4096-character limit). The policy of
   every turn is appended to `~/.evi/logs/channels/telegram.log` (`turn for
   user … in chat …: policy mode=ask tools=[…] auto_approve=[…] memory=recall
   groups=off`); past 1 MB the log rolls over to `telegram.log.1`.

The first two checks need only the message, so they hold even when
`config.toml` can't be read (say, mid-edit). When handling a message fails,
the error text goes back only to a paired sender in a direct chat; anywhere
else it is only logged.

Each message is answered by a fresh turn: the bot does not see your earlier
messages. With `memory_mode = "staged"` or `"full"` you can ask it to remember
something (the `memory` tool) so it carries over.

Pairing state (who is paired, who is waiting) lives in
`~/.evi/channels/telegram.json`, not in `config.toml`, so saving settings can
never overwrite an approval list.

A bad or revoked token doesn't spin: the loop backs off (up to five minutes
between attempts) and shows the error in Settings → Channels and
`evi channel status`. Applying a change (**Save & apply**) restarts the poller
without running a message twice: the new poller picks up from the last update
the old one took, even while that one is still answering it, and an update one
poller took is skipped by any other on the same token. Starting the channel
again always stops the poller already running, so two restarts at once still
leave one.

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
telegram_enabled      = false             # off by default
telegram_token        = ""                # from @BotFather; shown as ******** in Settings
telegram_tools        = ["memory", "skills"]  # tool CATEGORIES an approved sender may use; [] = none
telegram_auto_approve = []                # categories that run without asking (none by default)
telegram_max_turns    = 6                 # model steps per message
allow_groups          = false             # answer paired senders in group chats too
memory_mode           = "recall"          # recall | staged | full
```

`telegram_tools` takes the same category names as `[tools]` (`memory`,
`skills`, `web`, `fs`, `shell`, …); an empty list gives the bot no tools at
all. `subagent` is never offered, because a subagent runs under your desktop
policy. `allow_groups` opens groups only when it is the TOML boolean `true`:
a quoted `"false"`, or any other value, keeps them closed. A repository's
`.evi.toml` can't set any `[channels]` key.

### The channel's policy

The bot runs in mode `ask` with nobody to ask: each turn is an unattended run
under the `deny` policy, and a denied call tells the model that nobody could
approve it. That denial names `[auto] auto_approve` and `[auto]
unattended_policy`; in a channel turn the first is your `telegram_auto_approve`
list and the second is always `deny`. A tool call runs only if:

- its category is in `telegram_auto_approve`, or
- it is one of the channel's own safe tools: the memory tools `memory_mode`
  offers and the read-only skill tools (`list_skills`, `invoke_skill`).

Everything else is denied. Nothing in `[auto]` widens this: not its
`auto_approve` list, its mode (yolo, accept_edits), its allow rules, its
trusted dirs and domains, its destructive-guard exemptions or its unattended
policy. What tightens still applies: your `hard_deny` list, your deny rules,
the protected paths (the stock list is always included) and the
destructive-command guard.

Listing `code`, `fs`, `shell` or `computer` in `telegram_auto_approve` lets
every paired sender run code or change files on this machine without asking.
Do it only if you fully trust everyone you pair with. With `web` in
`telegram_tools` but not in `telegram_auto_approve`, fetches are denied.

A delegate CLI-agent backend (`codex`, `gemini`, `amp`, `qwen`, `copilot`)
runs its own tools outside eVi's permission checks, so the bot never uses one,
as `[llm] backend` or as any `[models] *_backend`, and says why. `[llm]
allow_cli_agent_unattended` doesn't change this: it is your opt-in for your
own unattended runs (scheduled tasks, `evi run`), not for whoever you paired.

### Memory modes

`memory_mode` decides what the bot may do with your long-term memory:

| Mode | Tools offered | Effect |
|---|---|---|
| `recall` (default) | `recall`, `list_memories`, `recall_by_tag` | Reads and searches your memory. Nothing can change it. |
| `staged` | the above plus `remember` | A `remember` is staged, not saved: it waits in `~/.evi/inbox/channel/` as one JSON file with `source = "channel:telegram:<chat>"` and `trust = "untrusted"` until you apply it. |
| `full` | the above plus `forget` | Writes and forgets your memory directly. Whatever a paired sender stores shows up in every desktop session's memory index. |

The project bug-ledger tools (`record_fix`, `search_fixes`) are never offered,
because the bot has no project. An unknown value means `recall`.

Staged notes sit outside the memory folder, so no session lists or recalls
them. Review them in Python with `evi.channels.inbox`: `list_staged()` lists
them, `apply(id)` writes one into your memory, and `discard(id)` drops it. At
most 200 notes wait at once. A review command and the Memory inbox panel are
on the roadmap.

## Usage

### CLI

```text
evi channel status            # on/off, running, token set, the effective toolset, who is paired or waiting
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
| `GET /api/channels/telegram` | State, toolset, `policy` (`mode`, `tools`, `auto_approve`, `memory_mode`, `allow_groups`), paired and pending senders (never the token) |
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
- **Relying on the phone to remember things?** Memory is recall-only by
  default, so the bot can't save anything you tell it. Set
  `memory_mode = "staged"` and apply the staged notes, or `"full"` if you
  trust everyone you pair with.
- **Coming next.** The roadmap ([roadmap.md](../roadmap.md)) brings a channel
  runtime with per-chat sessions, in-chat approvals, `/stop`, progress and
  media (Phase 4), and the Memory inbox for staged notes (Phase 5). A second
  channel waits until that runtime exists.

## Relevant source files

- `evi/channels/telegram.py`: the polling loop, the direct-chat and pairing gates, the turn per message
- `evi/channels/policy.py`: the channel's own policy (tools, permission, memory mode)
- `evi/channels/inbox.py`: staged memories and how the owner applies them
- `evi/channels/pairing.py`: pairing codes, approvals, revocation
- `evi/channels/telegram_api.py`: the stdlib Bot API client (chat type, shared senders)
- `evi/memory.py`: `MemoryStore(read_only=True)`, the recall-only store
- `evi/config.py`: `ChannelSettings`
