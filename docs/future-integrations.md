# Future integrations — backlog

A scratch list of third-party app/service integrations eVi could grow.
Nothing here is committed; the point is to capture ideas as they come
up so we don't lose them. When we pick one up, it gets its own phase
entry in `CHANGELOG.md` and the project memory.

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
| **Slack** | Read DMs/channels, search history, send messages | `slack_sdk` bot token or user token. Tools: `slack_search(q)`, `slack_read_channel(id, n)`, `slack_send(channel, text)`. Permission-gated send. |
| **VS Code** | Open files, see what user is editing, run tasks | Two paths: (a) eVi MCP server consumed by Cline/Continue, (b) VS Code extension that posts cursor + active-file context to eVi. Probably (a). |
| **JetBrains** | Same as VS Code | JetBrains has Beta MCP support; (a) above covers it. |

## Other candidates (not yet asked, but reasonable)

These came up implicitly while listing the above. Capture them while
they're fresh.

| App | Use case | Sketch |
|---|---|---|
| **Discord** | DMs + server messages | `discord.py` (bot user) + opt-in audit log. Read-only tools first; send under permission. |
| **Telegram** | DMs + bots | `python-telegram-bot`. Bot token only — user-account TG is more invasive. |
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
| **Obsidian** | Already shipped via sync. Possibly add a live-vault tool for in-conversation lookups. |
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
| **GitHub Copilot / Cursor / Cody** | AI-to-AI bridge | If you have a paid Copilot/Cursor seat, the bridge is mostly novelty — you already have eVi. |
| **Vault / 1Password CLI / Bitwarden CLI** | Secret retrieval | `op` / `bw` CLIs. Permission-gated heavily. Useful for hooks that need credentials. |
| **Browser history** | What did I look at yesterday | SQLite reads against Chromium / Firefox profiles. Privacy considerations. |
| **Browser bookmarks** | Search saved links | Same shape as history. |
| **Shell command palette** | Recent commands + frequency | `~/.zsh_history` / `~/.bash_history` parse. |
| **`fzf` / `ripgrep` / `fd`** | Better local search than `find_in_project` | Subprocess wrappers; faster for non-semantic queries. |

## Bridges (eVi as MCP server)

Rather than building N integrations one at a time, we could publish an
eVi MCP server that exposes eVi's tools (memory, index, calendar, etc.)
so that *other* agents — Claude Desktop, Cline, Continue, Cursor — can
consume them. This flips the integration story: you don't bridge into
eVi from each editor, the editor's agent reaches out to eVi's tool
surface.

Tracked separately as a possible Phase 31-ish item.

## How to add to this list

Just edit this file and open a PR (or paste in chat — eVi will edit it).
The table is plain markdown; new rows go anywhere.
