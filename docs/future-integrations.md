# Future integrations — backlog

A scratch list of third-party app/service integrations eVi could grow.
Nothing here is committed; the point is to capture ideas as they come
up so we don't lose them. When we pick one up, it becomes an item in
[roadmap.md](roadmap.md) and ships with a `CHANGELOG.md` entry.

## Shipped since this list started

| App | What shipped |
|---|---|
| **Telegram** | A chat channel (1.0.51): message eVi from your phone through your own bot, with pairing and a narrow toolset. Stdlib Bot API client, no `python-telegram-bot`. See [channels.md](features/channels.md). |
| **VS Code** | A VS Code extension (`editors/vscode/`): local ghost-text completion through `/api/complete` and a chat panel through `/api/chat`. |
| **eVi as an MCP server** | `evi mcp serve` (stdio and streamable HTTP) publishes curated tools, memory resources and command prompts to Claude Desktop, Cline, Continue, Cursor and JetBrains' MCP client. See [mcp.md](features/mcp.md). |
| **Obsidian** | `evi obsidian` syncs memory with a vault. |
| **Claude Code / Codex history** | `evi sessions import` (1.0.51). See [sessions.md](features/sessions.md). |
| **GitHub Copilot, Claude Code, Codex, Gemini CLI, Amp, Qwen Code** | CLI-agent backends (1.0.3 to 1.0.5): eVi routes a turn through the CLI you already use. See [configuration.md](configuration.md#the-cli-agent-backends-at-a-glance). |

## Chat channels: what decides the next one

The roadmap adds **no second chat channel** until the channel runtime (per-chat
sessions, a durable inbox, in-chat approvals) has shipped and a channel is
chosen (an open question in [roadmap.md](roadmap.md)). Slack and Discord need
WebSocket client stacks that break the stdlib-only desktop sidecar, and email
and forge notifications are untrusted text arriving at a tool-capable agent.
Matrix, Signal (through a user-run `signal-cli`) and IMAP/SMTP email are the
candidates on that question.

Each entry has a one-line sketch of the integration shape: which eVi
surfaces would expand (tool category + maybe a CLI subcommand) and what
sort of dependency it'd pull in.

## Deferred (asked-and-pushed-out)

These were mentioned in the 2026-05-27 planning session but the user
chose to defer in favour of calendar reading first.

| App | Use case | Sketch |
|---|---|---|
| **Home Assistant** | Control lights / climate / locks, query sensors | REST API + long-lived token. Tools: `homeassistant_state(entity)`, `homeassistant_service(domain, service, data)`. Existing `homeassistant` Python client, or just `httpx` direct. |
| **Notion** | Read pages + databases, append notes | Official `notion-client`. OAuth or integration token. Tools: `notion_search(query)`, `notion_get_page(id)`, `notion_append(parent, content)`. |
| **Spotify** | Now-playing, search, queue + playback control | `spotipy` + OAuth (refresh-token flow). Tools: `spotify_now_playing()`, `spotify_search(q, type)`, `spotify_play(uri)`, `spotify_queue(uri)`. |
| **Plex** | Library search, what's playing, history | `plexapi`. Server URL + token (Plex.tv). Tools: `plex_search(q)`, `plex_recently_added(days)`, `plex_playback_state()`. |
| **Slack** | Read DMs/channels, search history, send messages | `slack_sdk` bot token or user token. Tools: `slack_search(q)`, `slack_read_channel(id, n)`, `slack_send(channel, text)`. Permission-gated send. As a chat channel, see "Chat channels" above. |
| **JetBrains** | Use eVi from the IDE | `evi mcp serve` through JetBrains' MCP client covers tools today; editors with ACP support get eVi as an agent once `evi acp` ships ([roadmap.md](roadmap.md), Phase 4). |

## Other candidates (not yet asked, but reasonable)

These came up implicitly while listing the above. Capture them while
they're fresh.

| App | Use case | Sketch |
|---|---|---|
| **Discord** | DMs + server messages | `discord.py` (bot user) + opt-in audit log. Read-only tools first; send under permission. As a chat channel, see "Chat channels" above. |
| **GitHub** | Issues, PRs, code search, releases | `PyGithub` or raw REST + PAT. Already covered partially by MCP github server — could ship a native tool too. |
| **GitLab** | Same as GitHub | `python-gitlab`. |
| **Linear** | Issue tracking | REST + API key. Tools: `linear_search`, `linear_issue(id)`, `linear_create_issue(...)`. |
| **Jira / Confluence** | Issue tracking + wiki | `atlassian-python-api`. Beware: enterprise auth flows. |
| **Trello** | Lightweight cards | REST + API key. Niche. |
| **Asana** | Tasks | Official Python client. Same shape as Linear. |
| **Email — IMAP/SMTP** | Read + send without provider-specific OAuth | `imaplib` + `smtplib` stdlib. Generic, beats per-provider integrations for users on Fastmail/Posteo/etc. (We deliberately deferred Phase 2 Gmail/M365; IMAP is the user-friendly fallback.) |
| **RSS / Atom** | News feeds, blog updates | `feedparser`. Read-only. Pairs well with dream/memory. |
| **YouTube transcripts** | Summarize videos | `youtube-transcript-api`. No API key, just URL → transcript. |
| **Reddit** | Read subs, comment threads | `praw` + OAuth (read-only is easier). |
| **Mastodon / Bluesky** | Same shape as Twitter | Mastodon: `Mastodon.py`. Bluesky: `atproto`. |
| **Twitter / X** | API now restrictive | Skip unless user has paid API tier. |
| **Hacker News** | Read top stories, comments | Free API; no auth. Pairs well with morning briefings. |
| **Anki** | Spaced repetition for memorisation | `anki-connect` (HTTP plugin) or raw `.anki2` files. |
| **Obsidian** | Live-vault lookups | Sync already shipped (`evi obsidian`). A read tool for in-conversation lookups would be a thin addition. |
| **Logseq** | Same shape as Obsidian, different file layout. |
| **iMessage / SMS** | Read recent messages | macOS only: `~/Library/Messages/chat.db`. SQLite read tool already exists — would be a thin wrapper. |
| **Apple Reminders** | Read + create tasks | macOS only: AppleScript bridge via `osascript`. |
| **Things 3** | Tasks | macOS+iOS. URL scheme for create; SQLite read for `~/Library/Containers/com.culturedcode.ThingsMac/...`. |
| **Todoist** | Cross-platform tasks | Official Python SDK; REST + API token. |
| **TickTick** | Cross-platform tasks | Unofficial REST clients. |
| **OmniFocus** | Tasks | macOS AppleScript or URL scheme. |
| **Pocket** | Read-it-later | Pocket REST API. Mostly defunct since Mozilla's pivot. |
| **Instapaper / Readwise** | Highlights export | Readwise has a clean REST API. Useful for dream/memory feedback. |
| **Drafts** | macOS quick capture | URL scheme + iCloud Drive. |
| **OneDrive / Dropbox / Google Drive** | File access | Each has an OAuth flow; would beat IMAP-like generic-WebDAV approach. |
| **WebDAV** | Generic file access | `webdavclient3`. Covers Nextcloud, ownCloud, kDrive. |
| **Slack Tracker (Sherlock-style)** | Local stand-up summaries | Reads from Slack tool above + git tool. Composes a daily report. Could be a `recipe` instead of a tool. |
| **Postgres / MySQL / SQLite via DB URL** | Generic read-only DB queries beyond the current sqlite tool | `sqlalchemy` or per-DB drivers; broaden the existing SQLite tool. |
| **Redis** | Cache/inspect | `redis-py`. Mostly dev-ops use. |
| **AWS / GCP / Azure CLIs** | Cloud ops | Wrappers around `aws`/`gcloud`/`az`. Permission-gated. |
| **Kubernetes** | Cluster ops | `kubernetes` client. Read-only by default; tools like `kubectl_get_pods` etc. |
| **Docker** | Local containers | Docker SDK or shell. List, logs, exec. |
| **Tailscale** | Status, peer list | `tailscale` CLI subprocess. |
| **Wireguard** | Status | `wg show` parse. |
| **Calibre** | E-book library | `calibre` CLI or `calibredb` script. |
| **Apple Notes** | Read | macOS only — AppleScript bridge. |
| **Goodreads / StoryGraph** | Reading list | StoryGraph has no API; Goodreads' API is sunset. Skip. |
| **Letterboxd** | Watched films | No API. Skip until they ship one. |
| **Last.fm** | Music history | REST + API key. Pairs with Spotify. |
| **Strava** | Workouts | OAuth + REST. |
| **Apple Health / Google Fit / Garmin Connect** | Body data | Apple Health export → XML (via Shortcuts); Garmin via `garminconnect`. |
| **Weather** | Conditions / forecast | open-meteo (no key), or NWS, or OpenWeatherMap. Tool: `weather(location, when)`. |
| **News headlines** | NewsAPI / GDELT | API key needed for most. RSS is the no-key fallback. |
| **Wikipedia** | Article excerpts | `wikipedia-api`. No key. Useful retrieval target. |
| **arXiv / Semantic Scholar** | Paper search + abstracts | Both have free REST APIs. |
| **Cursor / Cody** | AI-to-AI bridge | Copilot is already a backend (`copilot`); for the rest the bridge is mostly novelty: you already have eVi. |
| **Vault / 1Password CLI / Bitwarden CLI** | Secret retrieval | `op` / `bw` CLIs. Permission-gated heavily. Useful for hooks that need credentials. |
| **Browser history** | What did I look at yesterday | SQLite reads against Chromium / Firefox profiles. Privacy considerations. |
| **Browser bookmarks** | Search saved links | Same shape as history. |
| **Shell command palette** | Recent commands + frequency | `~/.zsh_history` / `~/.bash_history` parse. |
| **`fzf` / `ripgrep` / `fd`** | Better local search than `find_in_project` | Subprocess wrappers; faster for non-semantic queries. |

## Bridges (eVi as MCP server)

Shipped: `evi mcp serve` publishes eVi's curated tools, memory and command
prompts so that *other* agents (Claude Desktop, Cline, Continue, Cursor) can
use them, which covers many "integrate with editor X" requests without a
plugin per editor. Next on that path is `evi acp`, eVi as an Agent Client
Protocol agent for editors ([roadmap.md](roadmap.md), Phase 4).

## How to add to this list

Just edit this file and open a PR (or paste in chat — eVi will edit it).
The table is plain markdown; new rows go anywhere.
