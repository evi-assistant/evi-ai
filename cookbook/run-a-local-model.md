# Run a model on your own hardware

Point eVi at a model running on your own machine — no API key, nothing leaves
your disk — and have it recommend one that actually fits your hardware.

> **Uses:** CLI · **You'll need:** [Ollama](https://ollama.com) or
> [LM Studio](https://lmstudio.ai) (or any OpenAI-compatible server), and eVi
> (`pip install evi-assistant`).

## 1. Start a local backend

eVi talks to whatever local server you already run. Two common choices:

- **Ollama** — serves on `http://localhost:11434`. `ollama serve`, then pull a
  model, e.g. `ollama pull qwen2.5-coder:7b`.
- **LM Studio** — start its local server (default `http://localhost:1234`) and
  load a model in the UI.

eVi auto-detects LM Studio, Ollama, and llama.cpp on their default ports. You do
not have to configure anything yet.

## 2. Let eVi pick a model for your machine

`evi models recommend` inspects your hardware (RAM, and GPU VRAM if present) and
suggests a model that will fit, plus a smaller "fast" model for quick turns:

```bash
evi models recommend
```

If you like a suggestion, make it the active model:

```bash
evi models use qwen2.5-coder:7b
```

`evi models list` shows every model your backends expose, tagged by source, and
`evi models active` shows the one in use.

## 3. Chat

```bash
evi chat
```

That opens the terminal REPL against your local model. Ask it something, let it
use tools, `/help` lists the slash commands. The same core also powers `evi web`
(a browser UI) and the desktop app — all three share `~/.evi/config.toml`.

## Pointing at a specific backend or an OpenAI-compatible server

To be explicit rather than relying on auto-detection, register a backend:

```bash
# a local Ollama endpoint
evi backend add local --kind ollama --base-url http://localhost:11434/v1

# any OpenAI-compatible server (vLLM, llama.cpp, a hosted endpoint)
evi backend add mybox --kind openai_compat --base-url http://192.168.1.50:8000/v1
```

Then `evi backend use local`. The full list of backend kinds and config keys is
in [Configuration](https://github.com/evi-assistant/evi-ai/blob/main/docs/configuration.md).

## Where to go next

- [Build an agent with the SDK](agent-sdk-quickstart.md) — drive the same model
  from Python.
- [Guard an agent's inputs and outputs](guardrails.md) — add safety layers.
