# Managed local runtime (llama.cpp)

## Overview

eVi can run a local model for you with nothing else installed: no Ollama, no LM
Studio, no account and no administrator rights. It downloads a prebuilt
`llama-server` from a pinned llama.cpp release and a small GGUF model from a
curated catalog, starts the server on `127.0.0.1`, points `[llm]` at it and
keeps it running while eVi runs. This is the **managed runtime**.

- **Zero-config first run** — the web and desktop first-run screen offers it
  as *Download a model*; `evi runtime setup` does the same from a terminal.
- **A model catalog** — download, switch and remove local models from
  Settings → Model & Backend → Local models, or with `evi model`.
- **GPU acceleration** — one click (or `evi runtime gpu`) fetches the CUDA build
  that matches your NVIDIA GPU and offloads the model to it.
- **Bring your own llama.cpp** — point eVi at a `llama-server` you already have
  and it supervises that one instead of downloading.
- **A context window sized to the machine** — eVi starts the server with the
  largest window that fits and keeps `[llm] context_size` in step.

Ollama, LM Studio and every other backend still work; the managed runtime is
just the one that needs nothing from you.

## How it works

### What setup puts on your machine

Before anything downloads, the setup card lists each item with its size and
destination, says the server is loopback-only and needs no administrator
access, and **Show the exact files & commands** reveals the literal download
URLs. Items already on disk are marked as such and are not downloaded again.
The same list is `GET /api/runtime/plan` (pass `?model_id=<id>` for a model
other than the starter).

| Item | Where |
|---|---|
| llama.cpp runtime (CPU build) | `~/.evi/runtime/llama/<release>/` |
| GPU runtime (CUDA build, on request) | under the same release folder |
| Models (GGUF) | `~/.evi/models/<owner>__<repo>/<file>.gguf` (the Hugging Face repo, `/` written as `__`) |
| Server log | `~/.evi/runtime/logs/llama-server.log` |
| Context-size record | `~/.evi/runtime/llm_context.json` |

Every download is checked against a SHA-256 pinned in eVi before it is
extracted or run, models come from a pinned Hugging Face commit, archives can't
write outside their folder, and a failed install leaves nothing half-installed.
A catalog model already on disk counts as installed only if it matches its
pin. Developers re-pin with `scripts/pin_runtime_hashes.py` (see
[development.md](../development.md#bumping-llamacpp-or-the-model-catalog)).

### Setup steps

Setup runs in the background and reports named steps: **Prepare runtime ·
Download model · Start the model · Ready**. Every failure offers **Run
diagnostics** and **View log**; the log is served over the API
(`GET /api/runtime/log?lines=200`), so it is readable from a browser too.
Progress is `GET /api/runtime/status` (`stage`, `pct`, `message`, `error`,
plus `server_running`, `active_model_id`, the GPU fields and `ctx`).

### The server

The managed `llama-server` listens on `127.0.0.1` only, on the first free port
from 8080. eVi starts it with the model (`-m`), the context window (`-c`) and
the GPU offload (`-ngl`), waits until it answers, and writes
`backend = "llamacpp"`, its `base_url` and the model name into `config.toml`.
Switching models restarts the server on the same port with the new model.

The server lives inside an eVi process:

- `evi web` and the desktop app relaunch it on start when `[llm]` points at the
  managed runtime and nothing is answering, on the configured model (or the
  first installed one).
- It stops when that eVi process exits. On Windows it is also tied to eVi's
  process lifetime, so a hard kill of the app takes the server with it. On
  macOS and Linux a server left behind by a hard kill is reused on the next
  start.
- The terminal REPL (`evi chat`, `evi run`) talks to the server while the web
  or desktop app is running; it does not start one itself.

### Context window

eVi owns `[llm] context_size` while you use the managed runtime: it picks
32768, 16384 or 8192 tokens, whatever fits next to the model in VRAM (on the
GPU) or in about 70% of RAM, and gives your own value back when you switch to
another backend. Details: [configuration.md](../configuration.md#context_size-and-the-managed-llamacpp-runtime).

### GPU

When a supported GPU is found, Settings → Model & Backend → Local models shows
**Use your GPU (*name*)**. It downloads the matching CUDA build once and
restarts the current model with full offload when the model fits in VRAM.

| Platform | What you get |
|---|---|
| Windows + NVIDIA | CUDA build chosen by compute capability: the CUDA 13.3 build for Blackwell (RTX 50-series), the 12.4 build for older GPUs |
| macOS | Metal, in the base build: nothing extra to download |
| Linux | CPU (this llama.cpp release ships no prebuilt CUDA build) |

`GET /api/runtime/status` reports `gpu_available`, `gpu_installed`, `on_gpu`
and `gpu_name`. Later starts prefer the GPU build when the model fits.

### Your own llama.cpp

If you already have a `llama-server` (your own build, a GPU build eVi doesn't
ship), the setup card offers it: `GET /api/runtime/detect` lists binaries on
`PATH` and in common install folders, and `POST /api/runtime/locate` with
`{"path": "...", "model_id": "..."}` validates one, saves it as
`[runtime] server_path` and starts it on a model. eVi then supervises your
binary and skips the runtime download. A path that later disappears or stops
being a runnable `llama-server` is cleared, and eVi falls back to the managed
download.

```toml
[runtime]
server_path = "C:/llama.cpp/llama-server.exe"   # empty = eVi's own download
```

## Usage

### CLI

```text
evi runtime setup      # download the CPU runtime + the starter model, start it, point eVi at it
evi runtime status     # runtime installed? server running? the current setup stage
evi runtime gpu        # download the CUDA build if needed and rerun the current model on the GPU
evi runtime stop       # stop the server this process started
evi model list         # the catalog: ● downloaded · ★ best fit for this machine · ▶ active
evi model use <id>     # download if needed, then switch the server to that model
evi model remove <id>  # delete a downloaded model (not the active one)
```

`evi runtime …` and `evi model use` run the server inside their own process,
so it stops when the command exits; the downloads and the config stay, and
`evi web` or the desktop app starts the server again. Use them while the app
is closed, or use Settings → Model & Backend in the app while it is open.
`evi models` (plural) is the older command group for Ollama, LM Studio and
`hf:` pulls; `evi model` (singular) is the managed catalog.

### Web and desktop

- **First run:** *Download a model* (or *Start local AI* once it is
  downloaded) on the first-run screen; see [onboarding.md](onboarding.md).
- **Settings → Model & Backend → Local models:** the catalog with size and
  license per model, **Use** (downloads with progress, then switches),
  **Remove**, and **Use your GPU**.

### API

| Route | What it does |
|---|---|
| `GET /api/runtime/plan` | What setup would download and where, before it does |
| `POST /api/runtime/setup` | Start first-run setup in the background |
| `GET /api/runtime/status` | Progress, server state, active model, GPU fields, `ctx` |
| `GET /api/runtime/log` | Tail of the server log (`?lines=`, at most 2000) |
| `POST /api/runtime/stop` | Stop the managed server |
| `GET /api/runtime/catalog` | Catalog entries with `installed`, the recommendation, the active model |
| `POST /api/runtime/model/use` | `{"id": ...}`: download if needed and switch |
| `POST /api/runtime/model/remove` | `{"id": ...}`: delete a downloaded model (409 for the active one) |
| `POST /api/runtime/gpu` | Download the CUDA build if needed and rerun on the GPU |
| `GET /api/runtime/detect` | `llama-server` binaries already on this machine |
| `POST /api/runtime/locate` | Use an existing binary (400 if it isn't a runnable `llama-server`) |

The long-running calls return at once; poll `GET /api/runtime/status`.

## The catalog

A short list of small instruction models (Qwen2.5 0.5B to 14B, Llama 3.2 1B
and 3B, Qwen2.5 Coder 7B), all Q4_K_M GGUFs that download without a Hugging
Face account. `evi model list` (or the Local models list) shows the current
entries with size and license. Licenses differ: some are Apache-2.0, the
Llama models use the Llama 3.2 Community license, and Qwen2.5 3B has a
non-commercial research license. Check the license before you build on a model.

★ marks the largest general model that fits your machine: in VRAM when there
is a GPU, else in about 70% of RAM. First-run setup uses the 1.5B starter
(about 1 GB) so the first reply comes quickly; switch up once it works.

## Notes / limits

- **Downloads happen after install.** The installer stays small; the runtime
  and a model are fetched the first time you ask for them.
- **One managed server per eVi process.** A model switch is a restart, so the
  first reply after a switch waits for the model to load.
- **Removing the active model is refused.** Switch to another one first (on
  Windows the running server holds the file open).
- **Linux has no GPU build here.** Point `[runtime] server_path` at your own
  CUDA or Vulkan `llama-server` to use the GPU.
- **The server is loopback-only.** It is not reachable from your network. To
  reach eVi from another machine, see [multi-machine.md](../multi-machine.md).

## Relevant source files

- `evi/runtime/setup.py`: setup, model switch, GPU switch, autostart, context window
- `evi/runtime/llamacpp_runtime.py`: runtime download, pins, CUDA build choice, detection
- `evi/runtime/catalog.py`: the model catalog, downloads, recommendation, window sizing
- `evi/runtime/llama_server.py`, `evi/runtime/reap.py`: the server process and its lifetime
- `evi/data/runtime_models.json`: catalog data
