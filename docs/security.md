# Security: who can reach eVi's web server

`evi web` and the desktop app serve the same FastAPI app. Behind it sit tools
that read and write files and run commands, so every request is checked before
it reaches a handler. This page is the reference for those checks: who gets in,
what a browser has to send, how the UI keeps model output inert, how the
desktop app's LAN token and window are locked down, and what changed for
existing setups.

## The request matrix

| Request | No access token | Access token set |
|---|---|---|
| From another machine (LAN, container network) | **403**, whatever `Host` it claims | Needs the token (401 without) |
| From this machine, `Host` names another site (DNS rebinding) | **403** | Needs the token |
| Browser page on another site (`Origin` / `Sec-Fetch-Site` say so) | **403** for anything that changes state | **403** for anything that changes state |
| Browser, state-changing `/api` call without `X-Evi-Client` | **403** | Allowed when it carries the token |
| `GET` / `HEAD /api/backend/pull` (starts a model download) without `X-Evi-Client`, from any client | **403** | Allowed when it carries the token |
| Non-browser client on this machine (CLI, VS Code extension, curl, peers on loopback) | Allowed | Needs the token |

"No access token" means `[web] auth_token` is empty, no launch token was
passed (`EVI_WEB_AUTH_TOKEN`, below), and multi-user mode has no users. In that
mode eVi answers **only this machine**: the TCP peer must be a loopback
address. The `Host` header alone proves nothing, because curl or any script on
the LAN can send `Host: localhost`.

When the launch token is the only credential (the desktop app's LAN token, a
container's), it gates other machines only. Requests from this machine are
handled as in the "No access token" column, so clients that predate the token,
such as an older VS Code extension, keep working on loopback. A
`[web] auth_token` or multi-user logins gate this machine too.

A request that a proxy forwarded (it carries `X-Forwarded-For`, `Forwarded`
or `X-Real-IP`) never counts as coming from this machine, even when the proxy
runs here. See [Serving the network](#serving-the-network).

A request counts as coming from a browser when it has an `Origin` header, or a
`Sec-Fetch-Site` header naming the page that sent it (any value but `none`).
Every current browser sends one of them on the requests this matters for.
`Sec-Fetch-Mode` alone doesn't count: Node's `fetch`, which the VS Code
extension host uses, sends `Sec-Fetch-Mode: cors` and nothing else. curl, the
CLI and peers send none of these headers.

### Browser requests and `X-Evi-Client`

A page on another site can make your browser send requests to
`127.0.0.1`. Two rules stop it from driving eVi:

1. **Origin check.** A state-changing request (POST/PUT/PATCH/DELETE, and the
   one side-effecting GET, `/api/backend/pull`) is refused unless its `Origin`
   is eVi's own, a loopback alias of the same port, or the desktop shell's
   loading page. `EVI_ALLOW_CROSS_ORIGIN=1` turns this check off. Behind a
   reverse proxy that rewrites `Host`, have it send `X-Forwarded-Host` and
   start eVi with `EVI_TRUSTED_PROXY=1`.
2. **Client header.** A browser's state-changing `/api` request must also carry
   an `X-Evi-Client` header (any value), unless it authenticates with the
   access token. eVi's own pages add the header to every request. Another
   page can't add a custom header to a cross-origin request without a CORS
   preflight, and eVi never answers one.

A script that calls the API from a browser page must send `X-Evi-Client: 1`
or the access token. A `mode: 'no-cors'` fetch can't carry either, so it is
always refused. Routine webhooks (`/api/routine/<token>`) are exempt: their
path token is the credential.

The side-effecting GET, `/api/backend/pull`, is stricter. Without the access
token, `GET` and `HEAD` of it need an `X-Evi-Client` header (any value) from
**every** caller: a browser whatever its `Sec-Fetch-Site` (`same-origin` and
`none` included), and curl or a script too. Without the header the answer is
403, and its detail names the header. A GET is what an `<img>`, `<link>`,
`<iframe>` or a clicked link sends, and none of them can add a header, so
markup on another site, or in eVi's own page, can't start a download. eVi's
UI reads the progress stream with `fetch`, which sends it. One that the
browser labels `Sec-Fetch-Site: cross-site` or `same-site` (another site, or
another port on this machine) is refused even with the header, and even with
`EVI_ALLOW_CROSS_ORIGIN=1`. With the token (`Authorization` header or
`?token=`) no `X-Evi-Client` is needed. From a script:

```sh
curl -N -H 'X-Evi-Client: 1' 'http://127.0.0.1:8473/api/backend/pull?model=<name>'
```

### Session ids

A session id is a single path component: 1–96 letters, digits, `_` or `-`, and
not a Windows device name. Every route that takes one checks it: in a body, in
the `?session=` link parameter, and in `/api/session/<id>/…`. Anything else
gets 400 before a handler runs.

## Access tokens

eVi accepts these as the owner's token:

- **`[web] auth_token`** in `config.toml`. Set it with
  `evi web-config token rotate`. The server re-reads it on every request, so a
  rotation takes effect at once, with no restart: the previous token stops
  working and signed-in browsers ask for the new one.
- **The launch token**: the `EVI_WEB_AUTH_TOKEN` environment variable of the
  server process. The desktop app passes its generated LAN token this way (see
  below), and so does the Docker image (see [Containers](#containers)); a
  service can do the same. The server reads it once at startup and removes it
  from its environment, so the agent's tool subprocesses never inherit it. It
  stays valid until that server restarts, even after you rotate
  `[web] auth_token`. On its own it gates other machines only (see above); to
  require a token from this machine too, set `[web] auth_token`.

`evi web-config token show` prints the token a server on this machine signs
clients in with: `[web] auth_token` if set, else `EVI_WEB_AUTH_TOKEN`, else
the token a launcher generated and keeps in the eVi home (`container-token`
from the Docker image, then the desktop app's `desktop-token`). Only the
token goes to stdout, so `$(evi web-config token show)` works in a script;
where it came from goes to stderr. With no token at all it prints nothing.

Clients send the token as `Authorization: Bearer <token>`. EventSource and
`<img>` can't set headers, so they use `?token=<token>`. Once the token is
configured, every `/api` route requires it except `/api/health`,
`/api/auth/check`, `/api/backend/status`, the agent card, and routine webhooks.

### Serving the network

Set a token before you serve the network, then share it with whoever needs
access: peers that delegate tasks here, the VS Code extension on another
machine, a phone's browser.

`evi web` with a non-loopback `--host` (`0.0.0.0`, a LAN address) and no
token refuses to start: it prints how to set one and exits with status 1.
`[web] auth_token`, `EVI_WEB_AUTH_TOKEN` or multi-user logins all count. A
server started some other way (`python -m uvicorn evi.apps.web.server:app
--host 0.0.0.0`, say) still starts, but answers only this machine, and the 403
it gives everyone else says how to fix it. When it starts, `evi web` prints a
`web auth:` line saying who it will answer.

Behind a reverse proxy, even one **on the same machine**, clients count as
remote. A request that carries `X-Forwarded-For`, `Forwarded` or `X-Real-IP` is
never treated as this machine's, and uvicorn (which runs eVi) also replaces
the client address with the `X-Forwarded-For` one when the proxy runs on this
machine. So give eVi a token when you put a proxy in front of it. A proxy that
sends none of those headers relays every client as this machine, so don't
expose one without a token.

This also means `FORWARDED_ALLOW_IPS=*` (uvicorn's setting for trusting proxy
headers from any address) can't make a LAN client look local in no-token mode:
the `X-Forwarded-For` it would use for that gets the request refused.

### Containers

A published port reaches the container from the Docker bridge, not from
loopback, so even your own browser on the host counts as another machine and
needs the token. The image runs `evi web --host 0.0.0.0` through an
entrypoint (`scripts/docker-entrypoint.sh`) that makes sure it has one:

- If neither `EVI_WEB_AUTH_TOKEN` nor `[web] auth_token` is set, the
  entrypoint generates a random 256-bit token on first start, keeps it in the
  state volume as `/root/.evi/container-token` (mode 0600), and passes it to
  `evi web` as `EVI_WEB_AUTH_TOKEN`. Restarts reuse it. The log says where it
  is and how to show it, never the token itself:

  ```sh
  docker exec <container> evi web-config token show
  docker compose exec evi evi web-config token show   # with docker-compose.yml
  ```

- To choose your own, pass `-e EVI_WEB_AUTH_TOKEN=<token>` to `docker run`, or
  set `EVI_WEB_AUTH_TOKEN` under the `evi` service's `environment:` in
  `docker-compose.yml`; the entrypoint then generates nothing.
- `docker compose exec evi evi web-config token rotate` sets
  `[web] auth_token` in the state volume. It takes effect at once, and unlike
  the generated token it gates the container's own loopback too. The
  generated token keeps working until the container restarts; after that the
  entrypoint sees `[web] auth_token` and passes no launch token.

Like the desktop app's LAN token, the generated token gates other machines
only. The image's `HEALTHCHECK` calls `/api/health` over the container's
loopback, which works with any token setup (`/api/health` is public). Sign in
to the web UI with the token.

## The web UI

### Rendering model output

A reply is untrusted text. A web page the agent fetched, a file in a
repository, an MCP server's output or another session's message can get the
model to write any HTML. The UI (`evi/apps/web/static/app.js`) renders it
like this:

- Markdown goes through marked, then DOMPurify with a profile for chat, and
  the result is added to the page as DOM nodes, never as an HTML string. If
  DOMPurify didn't load, the reply is shown as escaped text, never as
  unsanitized HTML.
- Nothing that runs or reaches the UI survives: no scripts, event handlers,
  SVG, frames, objects, forms, buttons or other form controls, media
  elements, `<style>` or `style=`; no `id`, `name` or `data-*` attributes; and
  no attribute that points at an element by id (`for`, `form`, `list`,
  `headers`, `aria-controls`, `aria-labelledby` and the other ARIA
  references). Rendered markdown has no ids, so those could only point at the
  UI's own controls: a `<label for="reset">` would make a click on the reply
  press Reset. Classes are kept only when they name a code language
  (`language-*`).
- **Images** load only from inline `data:image/…` or from eVi's own generated
  images, `/images/<file name>`. The name must be bare (no `/`, `\`, `%`,
  `?`, `#` or leading dot), and it is checked again as the browser resolves
  it, so `/images/../api/…` doesn't pass. Any other image becomes the text
  `[image: <alt>]` (a link when its address is http(s)). A reply therefore
  can't make the page fetch a URL on its own, which could leak the
  conversation in its query or set off one of eVi's own endpoints.
- **Links**: an http(s) link to another site opens in a new tab with
  `rel="noopener noreferrer"`. `mailto:`, `#fragment` and `/images/<name>`
  links are kept. Any other link loses its target, including a relative path
  and an absolute link back to eVi's own address.
- Inputs survive only as disabled checkboxes, for task lists.

**Diagrams.** A `mermaid` code block is the model's code too, and laying it
out could make the page fetch a URL the diagram chose. So mermaid loads only
when a finished reply has a diagram, and it runs in a hidden frame of its own
whose policy lets nothing load (`default-src 'none'; script-src 'self';
style-src 'unsafe-inline'; img-src data:`). A diagram that tries to load
anything there (an image shape, a `url()` in a `classDef` or `style`, a C4
colour) is not drawn, and its source stays visible. Mermaid runs with
`securityLevel: 'strict'`, and a diagram's `%%{init}%%` or front matter can't
set `themeCSS`, `themeVariables`, `fontFamily`, `altFontFamily`,
`dompurifyConfig` or `arrowMarkerAbsolute` (`theme`, `look` and flowchart
options still work). The SVG that comes back is sanitized again and refused
if it carries a `url()` other than an in-document `#ref`, an `@import` or
`@font-face`, or a CSS rule not scoped to its own diagram.

**Libraries.** marked 14.1.4, DOMPurify 3.4.16, highlight.js 11.12.0 and
mermaid 11.17.2 ship inside eVi under `static/vendor/`, and the page loads
each with a sha384 `integrity` pin. The UI renders offline, nothing loads
from a CDN, and the browser refuses a vendored file that changed. See
[development.md](development.md#the-web-ui-and-its-libraries).

### The page's Content-Security-Policy

The policy has one source: the
`<meta http-equiv="Content-Security-Policy">` tag in `static/index.html`.
Today it is:

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' ipc: http://ipc.localhost; object-src 'none'; base-uri 'none'; form-action 'self'
```

Script runs only from files eVi serves (`app.js` and the vendored libraries):
no inline script, inline handler, `eval` or other origin. Markup that got
past the sanitizer still can't run script, load anything from another site,
or post a form elsewhere. `ipc:` and `http://ipc.localhost` are the desktop
shell's IPC transport.

### Response headers

- Every response carries `X-Content-Type-Options: nosniff`. The server pins
  `.js` to `text/javascript`, `.css` to `text/css`, `.html` to `text/html` and
  `.json` to `application/json`, so a Windows registry mapping (`.js` as
  `text/plain`) can't block the UI.
- `GET` and `HEAD` of `/` and `/static/index.html` carry
  `Content-Security-Policy`, the meta policy above plus
  `; frame-ancestors 'none'`, and `X-Frame-Options: DENY`. The server re-reads
  the meta whenever `index.html` changes (`page_csp` in `server.py`), and
  ignores a `frame-ancestors` written there. If it can't read a meta policy,
  the page gets `default-src 'none'; frame-ancestors 'none'` (a blank page,
  never an unprotected one) and an error is logged.
- Any other HTML page (FastAPI's `/docs` and `/redoc`) gets
  `Content-Security-Policy: frame-ancestors 'none'`, unless it sets a policy
  of its own, and `X-Frame-Options: DENY`.
- `/images/<name>` and `/uploads/<session>/<name>` get
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`,
  so an HTML or SVG file opened directly runs nothing, in an opaque origin.

### Framing

`frame-ancestors 'none'` and `X-Frame-Options: DENY` stop the browser from
loading eVi inside another page's frame, where a click meant for that page
could land on Approve or Settings (clickjacking). A `<meta>` policy can't
carry `frame-ancestors`, which is why it is only in the header. A copy
served without these headers (an older server, a proxy that strips them)
falls back on `app.js`: when `window.top !== window.self` it blanks the page
and stops, before it makes any `/api` call. That applies to every frame,
eVi's own origin included.

## The desktop app

### LAN access (`[federation] bind_lan`)

With LAN access off, the desktop app's server binds `127.0.0.1` and runs
without a token. With it on, the server binds `0.0.0.0` and always has a
token:

- If `[web] auth_token` is set, that token is used.
- Otherwise the app generates a random 256-bit token the first time it needs
  one. It is stored in `~/.evi/desktop-token`: mode 0600 on macOS and Linux,
  and on Windows the user profile's permissions already restrict it to you.
  The app passes the token to its server as `EVI_WEB_AUTH_TOKEN`. The same
  token is reused on every launch, so peers keep working.
- The app injects the token into its own window, only for the server's origin,
  so the UI signs itself in with no prompt. The UI uses the injected token
  only when it has none stored, or when the stored one stops working. The
  injected value is read at launch, so after you rotate `[web] auth_token`
  while the app runs, the new token you sign in with is kept.
- If no token can be created (for example, the eVi home isn't writable), the
  app stays on loopback instead of opening the LAN without one.

Peers on other machines, and the VS Code extension pointed at this machine
from elsewhere, need that token. Clients on this machine don't, because the
generated token gates other machines only. The VS Code extension still sends
`~/.evi/desktop-token` to a server on this machine when its `evi.authToken`
setting is empty.

Only the desktop app's own server uses `~/.evi/desktop-token`.
`evi web --host 0.0.0.0` needs `[web] auth_token` or `EVI_WEB_AUTH_TOKEN` of
its own, or it refuses to start, and the Docker image generates its own (see
[Containers](#containers)). `evi web-config token show` prints the desktop
token when no other token is set, so you can sign in from another machine,
and notes when LAN access is off and the app isn't using it.

### Thin-client mode (`EVI_REMOTE_URL`)

`EVI_REMOTE_URL=http://ai-server:8000` points the app at another machine's
server instead of starting one. If that server needs a token, set
`EVI_REMOTE_TOKEN`. Without it, the app uses the token `~/.evi/peers.json`
keeps for a peer at the same address (scheme, host and port), and without
either the UI shows its sign-in prompt. The app never sends this machine's own
`[web] auth_token` to another server. The token is injected only for the
remote server's origin.

Set `EVI_REMOTE_URL` to the address the server finally answers on. The
window stays on that origin (see [Shell isolation](#shell-isolation)), so it
doesn't follow a redirect to another one, such as an `http://` to `https://`
upgrade or a sign-in proxy (Cloudflare Access, oauth2-proxy) on another host.
Pointed at an eVi server older than this release, the chat works, but the
desktop actions in the page (update prompts, Open Logs, links in the system
browser) don't: that server's page looks for `window.__TAURI__`, which the
app no longer provides.

### Shell isolation

The window shows model output and holds the access token, so the shell limits
what the page can reach:

- **No Tauri JavaScript API.** `withGlobalTauri` is off, so
  `window.__TAURI__` is undefined. The page calls the shell only through
  Tauri's IPC primitive, and only for nine commands: the update check,
  install, restart, status, dismiss and staged-update commands, Open Logs,
  and opening an external link.
- **IPC for the server's origin only.** At launch the app grants those
  commands to exactly the eVi server's origin: `http://127.0.0.1:<port>` and
  `http://localhost:<port>` for its own server, or the `EVI_REMOTE_URL`
  origin. Tauri's built-in commands aren't granted, and the app's bundled
  loading page gets no IPC. Earlier versions granted the commands to every
  `localhost` and `127.0.0.1` port, so any local web page the window reached
  could call them.
- **Navigation guard.** The window may show only the eVi server (the same
  origins), the app's bundled loading page (in a development build, the dev
  server that serves it), and `blob:` URLs on the server's origin (Export
  Chat). Any other navigation, whether a link, a redirect or a
  script, is refused, and the shell writes
  `evi: refused to navigate the window to <origin>` to stderr.
- **Links.** An http(s) link in a reply opens in the system browser
  (`open_external_cmd`, which accepts only http(s) URLs with no credentials
  in them), never in the window.
- The bundled loading page has a CSP of its own (`tauri.conf.json`).

### Shell and core versions (`SHELL_ABI` 2)

The desktop shell and its Python core (the sidecar) update separately. The
token handshake changed how the shell launches the core, so it carries shell
ABI 2, and every core published from this change on declares
`min_shell_abi = 2` in its update manifest: that is the default of
`scripts/build-sidecar-manifest.py`, which the release workflow runs without
`--min-shell-abi`. A shell only stages a core whose `min_shell_abi` is at most
its own ABI, so an older shell keeps its bundled core until a full app update
installs the new shell. `tests/test_web_hardening.py` fails if the manifest
the script builds would reach an ABI-1 shell, or would be refused by the
current one. **Release notes for this change must say that a full desktop
update is needed.**

### Opening links and folders

On Windows the app opens links with `rundll32.exe url.dll,FileProtocolHandler`
and folders with `explorer.exe`, both by absolute path under `%SystemRoot%`.
A copy of either program placed next to `evi-desktop.exe` is never run.

## One turn per session

A session runs one turn at a time. While a turn runs:

- Another `/api/chat` for that session gets **409** and a pointer to
  `POST /api/session/<id>/steer`. A follow-up sent there joins the running turn
  at its next step. The web UI does this for you: after a reload or from a
  second window, a message to a busy session is queued as a follow-up.
- Commands that rewrite history or settings wait for the turn to finish:
  `/reset`, `/compact`, `/reload`, `/model <id>`, `/goal <text>`, `/auto on`,
  `/plan`, user commands that start a turn, and the reset, truncate, edit and
  re-roll routes. Read-only commands (`/help`, `/tools`, `/model`, `/goal`,
  `/auto` with no argument) still answer.
- A channel push with `run: true` falls back to a note.

The VS Code extension uses one session per window. Earlier versions shared a
single `vscode` session between all windows.

## Permission prompts

When a tool needs approval, the turn sends a `PermissionRequest` event and
waits for `POST /api/decide`. The wait has a deadline:

- After **300 seconds** with no answer, the request is **denied**, and the
  stream says so with a `PermissionExpired` event. Set the deadline in seconds
  with `[web] decision_timeout_s` in `config.toml` (read for each prompt, so
  no restart is needed), or with `EVI_DECISION_TIMEOUT_S`, which overrides it.
  A value that isn't a positive number means the default, so a prompt always
  has a deadline. The `PermissionRequest` event carries the deadline as
  `timeout_s`, and the UI closes its prompt when it passes.
- A dropped connection (a closed or reloaded tab) does **not** deny. The
  decision stays open until the deadline, and any of your tabs can answer it:
  `/api/decide` looks for the decision id in all of your own sessions, never
  another user's.
- When no client is watching a session (between turns, or during a channel
  `run: true`), a tool that would ask is denied immediately.

The VS Code extension shows the prompt with Approve and Deny buttons.

## Multi-user mode

With `[web] multi_user = true`, each login in `~/.evi/users.json` gets its own
sessions, transcripts and memory. Routes that act on the **host** are
owner-only. Only the owner's token (`[web] auth_token` or the launch token) can
use them, and a users.json login gets 403:

- settings (`/api/config`), hooks, plugins, guardrails, model routes;
- channels (Telegram pairing), peers (including LAN access), the managed
  runtime, MCP servers and MCP serving, backends (`/api/backends`,
  `/api/backend/*` except status), and switching the model (`POST
  /api/model-picker`);
- file checkpoints and rewind (`/api/checkpoints`, `/api/rewind`). One journal
  records everyone's file writes, so only the owner can list or undo them;
- folder trust (`POST /api/project/trust`);
- host-level runs: workflows, ultracode, recipes and evals. These use the
  host's own config and data directories, outside per-user isolation;
- delegated tasks from peers and A2A agents (`/api/federate`, `/a2a`), which
  run on the host's config and memory the same way. Peers send the owner's
  token.

Each login's data lives in `users/<folder>` under the eVi home. The folder
name is the login's name with every character outside `A-Z`, `a-z`, `0-9`,
`_`, `.` and `-` replaced by `_`, and leading and trailing `.`, `_` and `-`
stripped (`user` when nothing is left). The memory tools (`remember`,
`recall`, `forget`, `list_memories`, `recall_by_tag`) work in the login's
`memory` folder, the same notes its system prompt lists, and so do the
subagents its chats start. Notes saved before this release
through those tools went to the shared `~/.evi/memory` and are not moved.

The owner's own sessions, transcripts and memory live under the name `owner`.
A users.json login whose name would map there (`owner` in any case, or a name
like `owner.` or `-owner-` whose folder name is `owner`) can't sign in at all,
so it never sees the owner's sessions. The host-level runs above use the
host's `~/.evi/memory`, not `users/owner/memory`.

Two or more logins with different names whose folders match, ignoring case,
would read each other's chats and memory, so **none of them can sign in**
until all but one are renamed or removed. For example, `bob smith`,
`bob_smith` and `bob/smith` are all `users/bob_smith`; `Alice` and `alice`
are one folder on Windows and macOS; `.` and `user` are both `users/user`.
Each such login gets 403 with:

> The login '<name>' can't sign in: another login in users.json maps to the
> same data folder (users/<folder>), so they would share chats and memory.
> Ask the owner of this eVi to rename one of them.

The other login isn't named. `/api/auth/check` answers
`{"ok": false, "required": true, "error": "<that text>"}`, and the web
sign-in overlay shows it, for a token typed in and for one already stored.
The server logs one warning per group, once per process, naming every login
in it: `users.json: the logins … all map to the data folder users/<folder>;
none of them can sign in until all but one are renamed or removed`. One name
listed twice (two tokens) is one login, not a collision, and logins with
different folders (`bob-smith` and `bob_smith`) are unaffected.

`evi doctor` warns when multi-user logins exist but no owner token is set,
because then nobody can reach these routes. It also flags a login that can't
sign in because of its name. It doesn't report colliding logins yet: look for
the warning in the server log.

## Uploads

`/api/upload` and `/api/transcribe` stream to `~/.evi/uploads/<session>/` and
stop at 32 MB with a 413. Nothing is left on disk.

## What changed for existing setups

- **No token means this machine only.** Other machines, and anything a
  reverse proxy forwards, need a token. `EVI_ALLOW_ANY_HOST=1` no longer
  opens a token-less server to them.
- **`evi web --host 0.0.0.0` needs a token** (`evi web-config token rotate`)
  or it refuses to start. A systemd unit or script that runs it that way
  needs one before the upgrade.
- **The Docker image generates a token** when you set none. Sign in with
  `docker exec <container> evi web-config token show`.
- **Desktop LAN access** (`[federation] bind_lan`) now runs with a token, and
  peers on other machines must put it in their `peers.json` entry. This needs
  a full desktop update; an older app keeps its bundled core.
- **Thin-client mode** no longer sends this machine's `[web] auth_token` to
  the remote server; set `EVI_REMOTE_TOKEN` or a `peers.json` entry.
- **Browser scripts** that call the API must send `X-Evi-Client` or the
  token.
- **`GET /api/backend/pull`** needs `X-Evi-Client` (or the token) from every
  caller: add `-H 'X-Evi-Client: 1'` to a curl or script that starts a model
  download without a token.
- **The web UI can't be framed** by another page, and its code is
  `static/app.js` under a `script-src 'self'` policy with no inline script.
  Replies keep less raw HTML: no inline styles, ids, forms or buttons, and
  images from other sites show as `[image: …]` links.
- **Desktop thin-client mode** needs `EVI_REMOTE_URL` to be the server's final
  address, and a server older than this release loses the in-page desktop
  actions.
- **One turn per session**, a **300-second deadline** on permission prompts,
  and a channel `run: true` that denies tools that would ask.
- **Multi-user logins** lose settings, checkpoints and host-level runs, which
  are owner-only now, and a login named `owner` can't sign in. Logins whose
  data folders collide can't sign in either, and each login's memory tools
  now use its own folder, so notes they saved in the shared `~/.evi/memory`
  stay there.

## Checking your setup

`evi doctor` reports the web access posture: loopback-only, token set, LAN
access with the desktop's token, or LAN access that has no token yet. It
prints the fix for each warning.

`evi web` prints a `web auth:` line when it starts: a token is required (from
every client, or from other machines only for a launch token), users.json
logins, or no token, in which case only this machine can connect. With a
non-loopback `--host` and no token it doesn't start at all (see
[Serving the network](#serving-the-network)). The Docker image gives its
server a token of its own (see [Containers](#containers)).

`evi web-config token show` prints the token clients need (see
[Access tokens](#access-tokens)).

## Environment variables

| Variable | Effect |
|---|---|
| `EVI_WEB_AUTH_TOKEN` | Launch token for the server process. The desktop app sets it when LAN access is on, and the Docker image's entrypoint when no token is configured. |
| `EVI_REMOTE_TOKEN` | Desktop thin-client mode: the token for the `EVI_REMOTE_URL` server (else the matching `peers.json` entry's). |
| `EVI_DECISION_TIMEOUT_S` | Permission-prompt deadline in seconds; overrides `[web] decision_timeout_s` (default 300). |
| `EVI_ALLOW_ANY_HOST=1` | No-token mode: accept any `Host` header (still loopback clients only). |
| `EVI_ALLOW_CROSS_ORIGIN=1` | Turn off the cross-site `Origin` check. |
| `EVI_TRUSTED_PROXY=1` | Trust `X-Forwarded-Host` / `X-Forwarded-Port` from a reverse proxy. |
