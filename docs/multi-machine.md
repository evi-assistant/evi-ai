# Multi-machine setup

How to wire eVi across a workstation, a laptop, and a headless AI server
on the same LAN — what most people actually want once they have a
"server class" GPU.

## The setup

```
                    ┌──────────────────────────────┐
                    │      AI server (P40 / 24GB)  │
                    │  Ollama   :11434             │
                    │  ComfyUI  :8188              │
                    │  evi web  :8000 (0.0.0.0)    │
                    └────────────┬─────────────────┘
                                 │  LAN
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
   ┌────▼─────┐            ┌─────▼──────┐           ┌─────▼────┐
   │ Laptop   │            │ Desktop    │           │ Phone /  │
   │ thin     │            │ thick-and- │           │ Tablet   │
   │ client   │            │ thin both  │           │ browser  │
   └──────────┘            └────────────┘           └──────────┘
```

## 1. AI server (the P40 box)

Install Ollama if you haven't. It has the cleanest model-management API
and works fine on Pascal-era cards using integer quants:

```bash
# Linux (server)
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:32b-instruct-q4_K_M     # ~22 GB, the P40 sweet spot
```

Install eVi:

```bash
git clone <repo> /opt/evi && cd /opt/evi
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[web,mcp,scheduler]'
```

Tell eVi to use Ollama and start the web server bound to your LAN:

```bash
evi models backend ollama                       # auto-bumps base_url to :11434/v1
evi models use qwen2.5:32b-instruct-q4_K_M
evi web --host 0.0.0.0 --port 8000              # listens on the LAN
```

For "always on", run it as a systemd service:

```ini
# /etc/systemd/system/evi-web.service
[Unit]
Description=eVi web UI
After=network.target

[Service]
Type=simple
User=evi
WorkingDirectory=/opt/evi
Environment="PATH=/opt/evi/.venv/bin"
ExecStart=/opt/evi/.venv/bin/evi web --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now evi-web
```

The scheduler boots automatically inside the FastAPI lifespan; any
`evi schedule add` tasks fire from this machine.

## 2. Desktop (the 16 GB workstation)

Same install as the server. Pick a backend you have running locally:

```bash
# Option A: keep using your existing LM Studio install
evi models backend lmstudio
evi models recommend                            # confirms 14B fits

# Option B: install Ollama locally for symmetry with the server
evi models backend ollama
evi models pull qwen2.5:14b-instruct-q4_K_M
evi models use qwen2.5:14b-instruct-q4_K_M
```

Use the CLI directly (`evi chat`) or browse to `http://localhost:8000/`
after `evi web`.

You can also have *this* machine talk to the AI server when the desktop
isn't doing the heavy lifting:

```bash
evi profile add server --backend openai_compat \
    --base-url http://ai-server.local:8000/v1 \
    --model qwen2.5:32b-instruct-q4_K_M

evi --profile server chat
```

## 3. Laptop (the 940MX thin client)

The 940MX is too small to be useful as an LLM host (see the discussion in
the model-selector chat for details). Treat it as a pure thin client.

Install + create two profiles:

```bash
git clone <repo> ~/evi && cd ~/evi
python3 -m venv .venv && .venv/Scripts/activate
pip install -e .

evi profile add home --backend openai_compat \
    --base-url http://ai-server.local:8000/v1 \
    --model qwen2.5:32b-instruct-q4_K_M

evi profile add away --backend lmstudio \
    --model llama3.2:1b-instruct-q4_K_M
```

Use them:

```bash
evi --profile home chat        # at home: full power via the server
evi --profile away chat        # off-LAN: fall back to a tiny local model
```

### Desktop (Tauri) on the laptop

The Tauri desktop app can run in **remote mode**, skipping the Python
spawn entirely. The webview points at the AI server directly:

```bash
EVI_REMOTE_URL=http://ai-server.local:8000 evi-desktop.exe
```

That's the laptop's nicest experience: no Python required for chat, just
a webview against your home server.

## DNS / discovery notes

Replace `ai-server.local` with whatever your network resolves. Options:

- **mDNS / Bonjour**: most modern OSes resolve `<hostname>.local` out of
  the box.
- **`/etc/hosts` or Windows hosts file**: hard-code the LAN IP if mDNS
  isn't working.
- **Tailscale / wireguard**: works off-LAN too. Profile points at the
  Tailscale magic-DNS hostname, and you're "home" anywhere.

If you go the Tailscale route, you can drop the `--profile away`
fallback altogether and just always use the server.

## Security

`evi web --host 0.0.0.0` binds to **every** interface. If your AI server
is on a network you don't control, you should:

- Bind to a specific interface (`--host 10.0.0.5`) or only localhost
  (`--host 127.0.0.1`) and tunnel via SSH or Tailscale.
- Put it behind a reverse proxy that does auth (nginx + basic auth, or
  Cloudflare Access).
- Or just keep it on a LAN you trust.

eVi has no built-in auth on the web UI by design — it assumes a trusted
network.

## Syncing your state across machines (`evi sync`)

The three machines above can share the *knowledge* that should follow you —
memory, skills, profiles, saved commands, routes, the MCP server list, and
hooks — via a git remote (any private repo works):

```bash
# On the first machine: point at an (empty) private git repo + push.
evi sync init git@github.com:you/evi-home.git
evi sync push

# On each other machine: same remote, then pull to adopt the synced state.
evi sync init git@github.com:you/evi-home.git
evi sync pull
```

Day to day: `evi sync push` after you teach eVi something, `evi sync pull` when
you sit down at another machine. `evi sync status` shows the remote + what's
changed.

**What travels:** `memory/ skills/ profiles/ commands/ routes.json mcp.json
hooks.toml`.

**What stays local** (and why): `config.toml` (backend URLs + secrets differ per
machine — your server runs Ollama, your laptop points at it), `tokens/` (OAuth
secrets), `models/` + `indices/` (large, and rebuildable), and
`logs/ images/ screenshots/ uploads/ transcripts/ scheduled/` (machine-local).
A managed `.gitignore` enforces this, so even new files default to *not* synced.

> First `evi sync pull` on a machine adopts the remote state (it force-checks-out
> the synced paths). Anything you'd created locally under those paths first
> should be pushed from there before pulling elsewhere.
