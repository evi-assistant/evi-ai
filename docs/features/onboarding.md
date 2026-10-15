# First run and connecting a backend

## Overview

A fresh eVi has no model to talk to. The web and desktop app handle that with
a **first-run screen**, shown whenever nothing can answer yet. It offers what
this machine can already use, cheapest first, and only calls a backend
"connected" once it has actually replied.

- **It looks before it downloads.** Setup checks for AI coding CLIs you already
  have (Claude Code, Codex, Gemini CLI, Amp, Qwen Code, GitHub Copilot CLI),
  local servers already running (LM Studio, Ollama, llama.cpp) and API keys in
  the environment, then offers a model download last.
- **Connected means it answered.** Picking an option sends a real one-token
  completion and a second one with a tool attached, and reports both:
  *Chat ✓ · Tools ✓*. If the backend doesn't answer, **the switch is rolled
  back** and your previous backend is left exactly as it was.
- **A first prompt to try.** An empty chat offers *Say hi* · *What can you
  do?* · *Give me a quick tour*; any reply proves the model, tools and settings
  work together.

## How it works

### What the first-run screen offers

`GET /api/backend/status` probes everything in parallel (a CLI agent counts if
its command is on `PATH`; a server counts if its port answers; llama.cpp is
looked for on ports 8080 to 8090) and returns `suggestions`, ranked by how
fast each reaches a first reply:

| Rank | Option | Badge | Why it ranks there |
|---|---|---|---|
| 1 | **Use Claude Code / Codex / …**: a CLI agent installed here | installed | No download; uses the CLI's own login (Gemini CLI needs a Gemini API key or Vertex AI) |
| 2 | **Use LM Studio / Ollama / llama.cpp**: a server already running | running | Nothing to download |
| 3 | **Use your Anthropic / OpenAI API key**: found in the environment | key found | A key is already set |
| 4 | **Download a model** (or **Start local AI** once downloaded) | key-free | Works with no account at all, but downloads about 1 GB |

The screen shows the top four and folds the rest under *Other options found*.
**Set this up later** closes it; Settings → Model & Backend has every option.

Option 4 is eVi's own managed llama.cpp runtime. Its card lists what it will
download, how big it is and where it goes before anything starts, then shows
named steps (*Prepare runtime · Download model · Start the model · Ready*)
with **Run diagnostics** and **View log** on failure. See
[runtime.md](runtime.md).

### The verified switch

Choosing options 1 to 3 calls `POST /api/backend/use` with `"verify": true`:

1. eVi remembers the current `[llm]` backend, URL, key and model.
2. It switches `[llm]` to the chosen backend. With no model given, it picks one
   the backend actually has (the hardware-recommended one when present).
3. It sends `Reply with the single word: ok` with `max_tokens = 1`. If that
   fails, it **restores the previous backend** and returns `"ok": false` with
   the error. The screen says *It didn't answer — nothing was changed*.
4. It sends a second one-token request offering one dummy tool. Success sets
   `tools: true`. A backend can chat and still reject tools; eVi then reports
   *Chat ✓ · Tools ✗*, because most of eVi's features (files, search, images)
   are tools.
5. On success every open chat switches to the new backend at once.

`POST /api/backend/verify` runs steps 3 and 4 against the backend you already
have, without switching. The answer looks like:

```json
{"model": "qwen2.5:7b-instruct", "backend": "ollama", "chat": true, "tools": true,
 "error": "", "latency_ms": 412}
```

Before 1.0.49 "connected" meant a port was open, so a CLI whose login had
expired, a missing model or a wrong key only showed up at the first message.

### Ollama, the older path

The Ollama route from earlier releases is still there: **Install Ollama**
(`POST /api/backend/install`: winget on Windows, Homebrew on macOS, the
official install script on Linux, or the download page when none of those is
available), **Start Ollama** (`POST /api/backend/start`) and a model pull with
progress (`GET /api/backend/pull`, server-sent events). Prefer the managed
runtime if you don't already use Ollama: it needs no installer and no service.

## Usage

### Web and desktop

1. Open eVi. If nothing answers, the first-run screen appears.
2. Click the first option (or any other).
3. Read the verdict. *Chat ✓ · Tools ✓* means you're done; type in the chat
   or click a starter prompt.

Settings → Model & Backend shows a live banner for the active backend (red
when nothing answers at the configured URL or CLI); see
[web-desktop.md](web-desktop.md#settings-groups-explainers-and-health-banners).

### CLI

The terminal has no first-run screen. Pick a backend yourself:

```text
evi runtime setup                      # eVi's own llama.cpp + a small model; it runs inside `evi web` (runtime.md)
evi backend add claude --kind claude_agent   # a CLI agent you already use, then:
evi backend use claude                 # (configuration.md covers each CLI agent)
evi models recommend                   # what fits this machine, for Ollama / LM Studio
evi doctor                             # checks the configured backend, CLI agents and API keys
```

### API

| Route | What it does |
|---|---|
| `GET /api/backend/status` | Probe the configured backend, local servers, CLI agents and keys; ranked `suggestions` |
| `POST /api/backend/use` | `{"kind", "model"?, "base_url"?, "verify"?}`: switch; with `verify`, roll back unless it answers |
| `POST /api/backend/verify` | Prove the configured backend answers (chat, then tools) |
| `POST /api/backend/install` | Unattended Ollama install where the OS allows it |
| `POST /api/backend/start` | Start `ollama serve` |
| `GET /api/backend/pull` | Pull an Ollama model, streaming progress |

## Notes / limits

- **A CLI agent counts as found when its command is on `PATH`.** Whether it is
  signed in only shows at the verify step, which is the point of verifying.
- **The verify step costs two tiny requests** on a metered API key.
- **API keys are only looked for in `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`.**
  Other providers are set up in Settings → Model & Backend or with
  `evi backend add`.
- **The first-run screen is web and desktop only.** `evi chat` with no working
  backend fails at the first message; `evi doctor` says why.

## Relevant source files

- `evi/apps/web/server.py`: `/api/backend/status`, `_first_run_suggestions`, the verified switch, `_verify_llm`
- `evi/apps/web/static/index.html`: the first-run screen, the verdict, starter prompts
- `evi/firstrun.py`: the Ollama install plan and the recommended Ollama model
