# Voice (TTS engines, STT, AutoSpeaker)

## Overview

eVi can both **speak** (text-to-speech, TTS) and **listen** (speech-to-text, STT) entirely on your own machine. Like the rest of eVi, voice is local-first: the default TTS engine uses whatever speech synthesizer ships with your OS (no Python packages, no network), and STT runs a local Whisper model — your audio never leaves the box.

Use voice when you want eVi to:

- Read replies aloud sentence-by-sentence as they stream (the `AutoSpeaker`).
- Run hands-free as an always-on voice assistant that listens, transcribes, chats, and answers aloud (`evi voice loop`).
- Transcribe a one-off mic recording or an existing audio file into text.
- Let the agent itself speak text or transcribe the mic via tools (`speak_text`, `transcribe_microphone`) during a conversation.

There are four selectable TTS engines (one zero-dependency, three optional neural ones) and an optional Whisper-based STT stack.

## How it works

### TTS — four engines

The engine is chosen by `[voice] engine` in your config. The four values are `system`, `coqui`, `f5`, and `piper`.

- **`system`** (default, zero-dep) — uses the platform's built-in voice, spawned as a subprocess:
  - **Windows** — PowerShell `System.Speech.Synthesis.SpeechSynthesizer`.
  - **macOS** — the `say` command.
  - **Linux** — `espeak-ng` if installed, else `espeak`, else an error.
  - `detect_backend()` resolves this to one of `windows`, `macos`, `espeak-ng`, `espeak`, or `none`. The `rate` option (words-per-minute-ish, engine-dependent) only applies to the system engine.
- **`coqui`** — Coqui XTTS v2: multilingual, can clone a voice from a reference WAV (`clone_sample`). Lazy-imports the `TTS` package; the model is loaded (and downloaded on first use) and cached per model id.
- **`f5`** — F5-TTS: fast zero-shot cloning. Driven through its `f5-tts_infer-cli` binary.
- **`piper`** — Piper: lightweight local neural voices, **no cloning**. Requires the `piper` binary and a voice `.onnx` file in `[voice] model`.

The neural engines synthesize to a temporary WAV in your temp dir and then play it with whatever the platform provides — `winsound` on Windows, `afplay` on macOS, and `paplay`/`aplay`/`ffplay` on Linux (in that order of preference). All three neural engines are lazy-imported, so eVi starts instantly and only pays the heavy import cost when you actually select one; a missing dependency raises a clear `VoiceError` with an install hint rather than crashing.

### AutoSpeaker — streaming speech

`AutoSpeaker` is what makes replies sound natural while they stream. It buffers incoming text deltas and, whenever it sees a sentence terminator (`.`, `!`, `?` followed by whitespace or end-of-input), it hands that completed sentence to a **background worker thread** that speaks it via the configured engine. Speaking is blocking *within* that thread so sentences don't overlap audibly, while your main loop stays responsive. Before speaking, each chunk is cleaned: fenced code blocks become "[code block]", inline code becomes "code", URLs become "link", and newlines collapse into sentence breaks — so eVi never reads raw code or URLs aloud. If the TTS backend disappears mid-stream, the worker fails open: it stops trying silently instead of erroring.

### STT — Whisper

Speech recognition uses `faster-whisper` (a CTranslate2 build of Whisper) plus `sounddevice` for mic capture. Both are optional — install the `stt` extra to enable them. The model is downloaded to the Hugging Face cache on first use (~75 MB for the default `tiny.en`); larger models are more accurate but slower. Models are cached in memory across calls so you only pay the 1–3 s load once. Available models, fastest → most accurate: `tiny.en`, `base.en`, `small.en`, `medium.en`, `large-v3` (the `.en` variants are English-only, smaller, and faster).

### AutoListener — always-on listening

The `evi voice loop` command runs an `AutoListener`: a background thread opens a mic input stream and runs energy-based **voice activity detection** (VAD) on 30 ms frames. Roughly 180 ms of loud audio starts an utterance; ~750 ms of silence ends it (or a 30 s hard cap). The clip is transcribed with Whisper and passed to a callback that chats with the agent and speaks the reply. While eVi is talking, the listener is **paused** so it doesn't transcribe its own voice. An optional **wake phrase** gates utterances: only speech containing the phrase (case-insensitive substring) is acted on, and the wake phrase is stripped before the request reaches the agent.

## Setup

### Config — `[voice]` in `~/.evi/config.toml`

Voice settings live in the `[voice]` section of your main config file at `~/.evi/config.toml` (on Windows, `%USERPROFILE%\.evi\config.toml`; overridable with the `EVI_HOME` environment variable). First run writes the defaults shown here:

```toml
[voice]
engine       = "system"   # system | coqui | f5 | piper
model        = ""         # engine-specific model id / path
clone_sample = ""         # reference WAV for the cloning engines (coqui / f5)
language     = "en"
```

| Key | Default | Meaning |
| --- | --- | --- |
| `engine` | `"system"` | Which TTS engine to use. |
| `model` | `""` | Engine-specific model id or path. For Coqui, an XTTS model id (defaults to `tts_models/multilingual/multi-dataset/xtts_v2` when blank); for Piper, the **required** path to a voice `.onnx`. |
| `clone_sample` | `""` | Reference audio WAV for voice cloning (used by `coqui` and `f5`). |
| `language` | `"en"` | ISO language hint for the neural engines. |

### The `voice` tool toggle

The agent-facing tools (`speak_text`, `transcribe_microphone`) are **opt-in**. Enable them under `[tools]`:

```toml
[tools]
voice = true   # local TTS / STT tools for the agent — opt in (default false)
```

This toggle does **not** affect the `evi voice ...` CLI commands or the `/speak` REPL command — those work regardless. It only controls whether the LLM agent can call the voice tools itself.

### Optional pip extras

- **STT** (`evi voice listen`, `transcribe`, `loop`, the web transcribe endpoint, and the `transcribe_microphone` tool):

  ```bash
  pip install 'evi-assistant[stt]'
  ```

  This pulls in `faster-whisper`, `sounddevice`, and `numpy`.

- **Neural TTS engines** are *not* bundled and have no single eVi extra — install each engine's own package/binary:
  - **Coqui:** `pip install coqui-tts`
  - **F5-TTS:** `pip install f5-tts` (provides `f5-tts_infer-cli`)
  - **Piper:** install the `piper` binary (or `pip install piper-tts`) and point `[voice] model` at a voice `.onnx`.

  > Note: the Coqui error message suggests `pip install 'evi-assistant[voice-clone]'`, but no such extra is currently declared — install `coqui-tts` directly.

The `system` engine needs nothing on Windows and macOS; on Linux it needs `espeak-ng` (preferred) or `espeak` on your `PATH`, plus a WAV player (`pulseaudio-utils` / `alsa-utils` / `ffmpeg`) if you use a neural engine.

## Usage

### CLI — `evi voice ...`

```text
evi voice speak       Speak text aloud via the configured engine.
evi voice backend     Show which platform TTS backend the 'system' engine will use.
evi voice engines     List all engines and whether each is installed (* = active).
evi voice listen      Record from the mic and print the transcription.
evi voice transcribe  Transcribe an existing audio file.
evi voice loop        Always-on voice assistant (listen → chat → speak). Ctrl-C to stop.
```

Key options:

- `evi voice speak <text> [--rate N] [--engine system|coqui|f5|piper]` — `--engine` overrides the config for this one call; `--rate` only affects the system engine.
- `evi voice listen [--duration 5.0] [--model tiny.en] [--device cpu|cuda]`
- `evi voice transcribe <path> [--model tiny.en] [--device cpu|cuda]`
- `evi voice loop [--wake "evi"] [--model tiny.en] [--device cpu|cuda] [--no-speak] [--rms-threshold 0.015] [--debug]` — `--wake` sets a wake phrase (empty = respond to everything), `--no-speak` prints replies instead of speaking them, `--rms-threshold` raises the VAD sensitivity floor for noisy rooms, `--debug` prints VAD diagnostics.

### REPL — `/speak`

Inside the interactive REPL, toggle sentence-by-sentence auto-speaking of assistant replies:

```text
/speak on      turn auto-speak ON
/speak off     turn auto-speak OFF
/speak         show current state
```

Turning it on with no TTS backend present prints an error and leaves it off. When on, each streamed reply is fed to an `AutoSpeaker` and spoken as complete sentences arrive.

### Web UI

The web UI exposes transcription, not playback. Drop an audio file (`.wav`, `.mp3`, `.m4a`, `.ogg`) onto the chat; it's uploaded to `~/.evi/uploads/<session>/` and run through Whisper via the `POST /api/transcribe` endpoint, which returns the recognized text. This requires the `stt` extra. TTS engine selection is exposed in the desktop app's **Settings → Voice** screen (or just edit `[voice]` in the config).

### Agent tools

With `[tools] voice = true`, the agent can call:

- `speak_text(text)` — speak text aloud (non-blocking, so it doesn't stall the agent loop). Returns `"ok"` or an error string.
- `transcribe_microphone(duration=5.0, model="tiny.en")` — record from the default mic and transcribe. First call downloads the model.

## Examples

### Example 1 — Speak a line, then transcribe a recording (zero extra setup for TTS)

```bash
# Check what the system engine resolves to on this machine
evi voice backend
# -> windows   (or macos / espeak-ng / espeak / none)

# Speak a sentence with the built-in voice, a little slower
evi voice speak "Build finished. All tests passed." --rate 140

# See which engines are installed and which one is active
evi voice engines
#   system  installed (active)
#   coqui   not installed
#   f5      not installed
#   piper   not installed

# Transcribe 6 seconds from the mic (needs: pip install 'evi-assistant[stt]')
evi voice listen --duration 6 --model base.en
```

### Example 2 — Always-on voice assistant with a wake phrase

```bash
# Hands-free loop: only acts on utterances containing "evi", speaks replies aloud.
# Ctrl-C to stop. (STT needs the [stt] extra; TTS uses the system engine here.)
evi voice loop --wake "evi"

# Quiet testing variant: print replies instead of speaking, with VAD diagnostics
evi voice loop --no-speak --debug
```

### Example 3 — Configure Piper as the TTS engine

Piper gives nicer neural voices than the OS default but needs the binary and a voice model. Edit `~/.evi/config.toml`:

```toml
[voice]
engine = "piper"
model  = "/home/me/voices/en_US-amy-medium.onnx"   # required for Piper
```

```bash
pip install piper-tts          # or install the standalone piper binary
evi voice speak "Now using Piper for synthesis."
```

### Example 4 — Coqui XTTS voice cloning

```toml
[voice]
engine       = "coqui"
clone_sample = "/home/me/voices/my-voice-10s.wav"   # reference audio to clone
language     = "en"
# model left blank -> defaults to tts_models/multilingual/multi-dataset/xtts_v2
```

```bash
pip install coqui-tts
evi voice speak "This should sound like the reference sample." --engine coqui
```

## Notes / limits

- **Fail-open by design.** The `AutoSpeaker` worker swallows a `VoiceError` and simply stops speaking rather than crashing your session; the `AutoListener` likewise logs and skips a failed transcription (only visible with `--debug`). Missing TTS/STT dependencies surface as clear `VoiceError` messages with install hints, not stack traces.
- **`--rate` is system-engine only.** It's ignored by the coqui/f5/piper engines.
- **Piper requires `model`.** Without `[voice] model` set to a `.onnx` path, the Piper engine raises an error. Coqui falls back to the default XTTS model id when `model` is blank.
- **No `voice-clone` extra exists yet.** Despite the hint in Coqui's error message, install `coqui-tts` (and `f5-tts` / `piper-tts`) directly.
- **First STT call is slow.** It downloads the Whisper model (~75 MB for `tiny.en`) and loads it (1–3 s). Subsequent calls reuse the cached model. Use `--device cuda` with a CUDA build of CTranslate2 for speed.
- **VAD is energy-based.** It works well in a quiet room; in noisy environments raise `--rms-threshold`. The wake-phrase gate is the main defense against false triggers from background chatter.
- **The loop pauses the mic while speaking** so eVi doesn't transcribe its own TTS output and loop on itself — but a `--no-speak` loop has no such concern.
- **Privacy.** With the `system` engine and local Whisper, no audio or text is sent off the machine. The neural TTS engines may download model weights on first use (Coqui via the HF cache); after that they run locally.
- **Linux needs binaries.** The `system` engine needs `espeak-ng`/`espeak`; neural engines also need a WAV player (`paplay`/`aplay`/`ffplay`). Without any of these, `evi voice backend` reports `none` and TTS commands exit with an error telling you to install `espeak-ng`.

### Relevant source files

- `C:\evi\evi\voice.py` — `speak`, `synthesize`, `listen`, `transcribe_wav`, `AutoSpeaker`, `AutoListener`, `detect_backend`, `available_engines`.
- `C:\evi\evi\config.py` — `VoiceSettings` (the `[voice]` keys) and `ToolToggles.voice`.
- `C:\evi\evi\tools\voice.py` — the `speak_text` / `transcribe_microphone` agent tools.
- `C:\evi\evi\apps\cli\main.py` — the `evi voice` subcommands and the REPL `/speak` handler.
- `C:\evi\evi\apps\web\server.py` — the `POST /api/transcribe` endpoint.
- `C:\evi\pyproject.toml` — the `[stt]` extra.
