"""Configuration loading and paths.

Config lives at %USERPROFILE%/.evi/config.toml. First run writes defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import tomllib


def _home_dir() -> Path:
    return Path(os.environ.get("EVI_HOME") or (Path.home() / ".evi"))


HOME = _home_dir()
CONFIG_PATH = HOME / "config.toml"
TOKEN_DIR = HOME / "tokens"
IMAGE_DIR = HOME / "images"
LOG_DIR = HOME / "logs"
MCP_CONFIG_PATH = HOME / "mcp.json"
SKILL_DIR = HOME / "skills"
SCHEDULED_DIR = HOME / "scheduled"
SCHEDULED_LOG_DIR = LOG_DIR / "scheduled"
HOOKS_CONFIG_PATH = HOME / "hooks.toml"
KEYBINDINGS_PATH = HOME / "keybindings.toml"
AGENTS_CONFIG_PATH = HOME / "agents.toml"  # user-defined subagent profiles
MARKETPLACE_PATH = HOME / "marketplace.json"
PEERS_PATH = HOME / "peers.json"
BACKENDS_PATH = HOME / "backends.json"  # multi-backend registry (see evi/backends/registry.py)
USERS_PATH = HOME / "users.json"
TRANSCRIPTS_DIR = HOME / "transcripts"
DREAM_LOG_DIR = LOG_DIR / "dreams"
SCREENSHOT_DIR = HOME / "screenshots"
UPLOADS_DIR = HOME / "uploads"


@dataclass
class LLMSettings:
    backend: str = "lmstudio"   # lmstudio | ollama | llamacpp | openai_compat
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"  # LM Studio ignores this but the SDK requires a value
    # Which OpenAI API shape the agent loop uses. "chat" (Chat Completions) is
    # the default and the ONLY one local backends (LM Studio/Ollama/llama.cpp)
    # support. "responses" opts into the newer Responses API — only for
    # endpoints that implement it (e.g. OpenAI cloud). Env: EVI_LLM_API.
    api: str = "chat"           # "chat" | "responses"
    # When api="responses", also enable these OpenAI server-side built-in tools
    # (executed by the model host, results folded into the reply): e.g.
    # ["web_search", "code_interpreter", "file_search"]. Ignored on chat.
    responses_tools: list[str] = field(default_factory=list)
    model: str = "qwen2.5-7b-instruct"
    temperature: float = 0.7
    max_tokens: int = 4096
    request_timeout: float = 120.0
    # Conversation grows boundless without compaction. When non-zero, the
    # Agent summarises oldest turns into a single system note once the
    # in-memory history exceeds this many messages. 0 = disabled.
    compact_after_messages: int = 40
    compact_keep_recent: int = 10  # leave this many turns un-summarised
    # Approximate token ceiling for the active model. Used for the
    # "X / Y" usage display and as the trigger for pre-emptive compaction
    # at `compact_when_pct` capacity. 0 = unknown.
    context_size: int = 32768
    compact_when_pct: int = 85  # compact when usage exceeds this % of context
    # Embeddings used for semantic file search. Default values are
    # Ollama's stock; LM Studio users should override.
    embed_model: str = "nomic-embed-text"
    embed_dimensions: int = 768
    # Reasoning effort knob, mirroring the OpenAI o-series + many local
    # reasoning models (DeepSeek-R1, Qwen3 with `enable_thinking`, …). One
    # of "off" | "low" | "medium" | "high" | "max". "off" (alias "none")
    # suppresses thinking entirely — nothing is sent, matching Claude Code's
    # ability to turn extended thinking off. Passed via `extra_body` so
    # backends that ignore it just drop it on the floor.
    reasoning_effort: str = "medium"
    # Model fallback chain. When the primary `model` request fails with a
    # retryable backend error (timeout, 5xx, connection refused), the agent
    # retries the turn against each model here in order before giving up.
    # Mirrors Claude Code's `fallbackModel`. Empty = no fallback.
    fallback_models: list[str] = field(default_factory=list)
    # Fast mode — when on, swap to `fast_model` if set. Common pattern:
    # main model is a 14B for daily work, fast_model is a 3B-7B for
    # boilerplate. Empty fast_model = fast_mode is a no-op.
    fast_mode: bool = False
    fast_model: str = ""
    # Sampling knobs. Defaults match OpenAI's "no-op" values; agent code
    # only forwards them when they deviate.
    top_p: float = 1.0
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    # 0 = unset; non-zero seeds the backend's RNG for reproducible runs.
    seed: int = 0
    # Optional hard-stop generation tokens. Empty list = no override.
    stop_sequences: list[str] = field(default_factory=list)
    # When False, the model may only request ONE tool per assistant turn
    # (OpenAI `parallel_tool_calls=false`). Default True = let the model
    # batch independent calls. Only forwarded when False AND tools are in
    # play, so local backends that don't speak the flag never see it.
    parallel_tool_calls: bool = True
    # Reasoning models (o-series, some local R1 builds) reject `max_tokens`
    # and want `max_completion_tokens` instead — it counts hidden reasoning
    # tokens plus visible output. 0 = unset; when >0 we send THIS instead of
    # max_tokens. Leave 0 for ordinary chat models.
    max_completion_tokens: int = 0
    # Token-id bias map as a JSON string (our flat TOML writer can't nest a
    # dict). Shape: '{"123": -100, "456": 5}'. Values clamp to [-100, 100].
    # Empty = no bias. You need the model's tokenizer to find ids, so this
    # is mostly for programmatic / power use.
    logit_bias: str = ""
    # KV-cache prompt reuse hint. llama.cpp's server honours a `cache_prompt`
    # request field to keep the shared prefix warm across turns; other
    # backends ignore the unknown key. We forward it via extra_body when
    # True. vLLM does the same thing server-side via --enable-prefix-caching.
    cache_prompt: bool = False
    # Per-token log probabilities. When `logprobs` is True we ask the backend
    # for them and surface a confidence summary. `top_logprobs` (0-20) also
    # requests the N most-likely alternates per position. Off by default —
    # adds response weight and not every backend supports it.
    logprobs: bool = False
    top_logprobs: int = 0
    # Multi-model routing — see evi/routing.py. When `router_enabled` is
    # true, each user turn is matched against routes in `~/.evi/routes.json`
    # and may swap `model` for that turn. `router_model` is an optional
    # tiny classifier used when no keyword rule matches; empty disables
    # the LLM fallback (keyword-only routing).
    router_enabled: bool = False
    router_model: str = ""
    # Output style (response persona) layered onto the system prompt — one of the
    # built-ins (concise/explanatory/teacher) or a ~/.evi/styles/<name>.md file.
    # Empty = eVi's default voice. See evi/styles.py.
    output_style: str = ""


@dataclass
class ComfySettings:
    base_url: str = "http://localhost:8188"
    default_checkpoint: str = "sd_xl_base_1.0.safetensors"
    default_steps: int = 25
    default_width: int = 1024
    default_height: int = 1024


@dataclass
class ObsidianSettings:
    """Optional Obsidian-vault sync target. Empty `vault_path` = disabled."""

    vault_path: str = ""
    subdir: str = "eVi"


@dataclass
class GoogleSettings:
    client_secrets_path: str = ""  # path to OAuth desktop client JSON
    scopes: list[str] = field(
        default_factory=lambda: ["https://www.googleapis.com/auth/gmail.readonly"]
    )


@dataclass
class MicrosoftSettings:
    client_id: str = ""
    tenant_id: str = "common"
    scopes: list[str] = field(default_factory=lambda: ["Mail.Read", "User.Read"])


@dataclass
class WebSettings:
    """Web frontend security + behaviour.

    `auth_token` empty disables auth entirely (current behaviour — open
    access, fine for localhost-only). When set, every `/api/*` route
    requires either an `Authorization: Bearer <token>` header OR a
    `?token=<token>` query parameter (the latter is needed for `<img>`
    src and streaming endpoints that can't carry custom headers).

    Generate a token with `evi web token rotate`.

    `multi_user` (opt-in) lets a team each log in with their own revocable token
    from `~/.evi/users.json` instead of sharing `auth_token`. Each user gets an
    isolated workspace — their web sessions, transcripts, and memory live under
    `~/.evi/users/<name>/` and are not visible to other users.
    """

    auth_token: str = ""
    multi_user: bool = False


@dataclass
class TelemetrySettings:
    """Opt-in crash/error reporting. **OFF by default.**

    Reports are sent only when `crash_reports` is true AND a `dsn` is set, to a
    Sentry-compatible endpoint (self-hosted GlitchTip or hosted Sentry). Every
    event is heavily scrubbed first (home/user paths, env, API keys, and —
    crucially for an AI app — exception messages + frame locals that may carry
    prompt text). Nothing is sent without a DSN, so this is inert until you opt
    in. Env overrides: `EVI_CRASH_REPORTS` (0/1), `EVI_TELEMETRY_DSN`.
    """

    crash_reports: bool = False
    dsn: str = ""
    backend: str = "sentry"   # "sentry" | "none"
    # OpenTelemetry traces/metrics (Phase 89). OFF by default; nothing is
    # exported without an `otlp_endpoint` (or EVI_OTLP_ENDPOINT) and the
    # `evi-assistant[otel]` deps. Separate from crash reporting above.
    traces: bool = False
    metrics: bool = False
    otlp_endpoint: str = ""   # base OTLP/HTTP URL, e.g. http://localhost:4318
    service_name: str = "evi"


@dataclass
class StatusLineSettings:
    """Customizable chat-REPL status line (off by default). See evi/statusline.py.

    `format` tokens: {model} {used} {ceiling} {pct} {branch} {goal} {effort}
    {fast}. `command` (optional) runs a shell command with the state as JSON on
    stdin and uses its stdout, overriding `format`.
    """

    enabled: bool = False
    format: str = "{model} · {pct}% ctx · {branch}{goal}{fast}"
    command: str = ""


@dataclass
class FederationSettings:
    """eVi↔eVi federation. `serve` opts this instance in to answering
    `POST /api/federate` (run a delegated task for a trusted peer). OFF by
    default — you choose to be a peer. The peers you delegate *to* live in
    `~/.evi/peers.json` (so per-peer tokens stay out of synced config)."""

    serve: bool = False
    # Expose an A2A (Agent2Agent, https://a2a-protocol.org) JSON-RPC endpoint at
    # POST /a2a so ANY standards-compliant agent (not just trusted eVi peers) can
    # delegate a task here. OFF by default — a broader surface than eVi↔eVi
    # federation, so it's a separate opt-in. Still bearer-token-gated + run
    # non-interactively. The Agent Card at /.well-known/agent-card.json is served
    # regardless (discovery is harmless); only task execution needs this flag.
    a2a: bool = False
    # When true, the DESKTOP app binds its bundled server to 0.0.0.0 (LAN) so
    # this machine can be reached as a federation peer — otherwise it's loopback
    # only and unreachable from other boxes. Read by the Tauri shell at launch.
    # SECURITY: exposes the server to the local network — set [web] auth_token.
    bind_lan: bool = False


@dataclass
class UltracodeSettings:
    """Defaults for the ultracode pipeline (`evi ultracode` / `/ultra`).

    A fixed decompose → fan-out solvers → adversarial verify → synthesize pass.
    `breadth` is the number of parallel solver angles (1 disables fan-out);
    `rounds` is verify→refine cycles (0 skips critique — the weakest-model
    escape hatch). `angles` optionally names specific angles from
    `ultracode.SOLVER_ANGLES` (empty = the first `breadth`). `auto_tune`
    downshifts breadth/rounds for tiny / short-context models.
    """

    breadth: int = 3
    rounds: int = 1
    mode: str = "code"
    angles: list[str] = field(default_factory=list)
    max_workers: int = 4
    auto_tune: bool = True
    # Cheaper fan-out: run the parallel SOLVER stage on [llm] fast_model while
    # decompose / the adversarial critic / synthesize stay on the main model.
    # No-op unless fast_model is set. Per-stage overrides are CLI-only flags.
    cheap_fanout: bool = False
    # Multi-backend fan-out: spread the parallel SOLVER angles across every model
    # on backends flagged `fanout` in the registry (round-robin) — so one run can
    # use several providers at once (a big cloud model + locals). No-op if no
    # backend is fanout-flagged. Overrides cheap_fanout for the solve stage.
    fanout: bool = False


@dataclass
class PluginsSettings:
    """Plugin marketplace (lighter/later item). `index_urls` are extra remote
    plugin-index JSON files merged with the local `~/.evi/marketplace.json` for
    `evi plugin search` / `evi plugin install <name>`."""

    index_urls: list[str] = field(default_factory=list)


@dataclass
class VoiceSettings:
    """TTS engine selection (Phase 91).

    `engine` picks how speech is synthesised:
      - "system" — the zero-dep platform voice (Windows SAPI / macOS say / espeak)
      - "coqui"  — Coqui XTTS v2 (multilingual, voice cloning from a sample)
      - "f5"     — F5-TTS (fast zero-shot cloning)
      - "piper"  — Piper (lightweight local neural voices; no cloning)

    The neural engines are optional heavyweight installs (torch etc.); eVi
    lazy-imports them and falls back to a clear error if the deps aren't
    present. `clone_sample` is a reference WAV for the cloning engines;
    `model` is an engine-specific model id/path (e.g. a Piper `.onnx`).
    """

    engine: str = "system"       # system | coqui | f5 | piper
    model: str = ""              # engine-specific model id / path
    clone_sample: str = ""       # reference audio for voice cloning (coqui/f5)
    language: str = "en"


@dataclass
class ToolToggles:
    fs: bool = True
    code: bool = True
    shell: bool = False
    gmail: bool = False
    outlook: bool = False
    image: bool = False
    memory: bool = True
    subagent: bool = False
    mcp: bool = False
    skills: bool = True
    web: bool = False        # network access — opt in
    voice: bool = False      # local TTS — opt in
    computer: bool = False   # mouse/keyboard control — never default
    transcripts: bool = True # write session logs to ~/.evi/transcripts/
    pdf: bool = False        # local PDF extraction
    sqlite: bool = False     # read-only SQLite queries
    index: bool = False      # semantic project search (needs embed model)
    git: bool = False        # git read-only inspection tools
    federation: bool = False # delegate_peer — call a trusted peer eVi (network)
    ocr: bool = False        # tesseract OCR — needs the binary installed
    calendar: bool = False   # iCal / CalDAV calendar reading
    ask: bool = True         # ask_user — clarifying questions (no-op when non-interactive)
    vision: bool = True      # describe_image — caption/inspect images via a VLM specialty
    # When true, run_python executes under an OS sandbox (read-only FS except a
    # temp workdir, no network) where one is available (bwrap / sandbox-exec).
    # Falls back to unsandboxed if no sandboxer is present. See evi/sandbox.py.
    sandbox: bool = False
    # Consume-side MCP allowlist: when non-empty, only these server names (from
    # mcp.json) load — lets a shared/synced mcp.json be gated per machine.
    mcp_allow: list[str] = field(default_factory=list)
    # Tool-search-at-scale: when on AND the enabled toolset exceeds
    # `tool_search_threshold`, defer the long tail behind a `search_tools` meta
    # tool instead of sending every schema each turn (keeps context small with
    # many MCP tools). Core categories (fs, memory) stay always-loaded.
    tool_search: bool = False
    tool_search_threshold: int = 30
    # Cap on how many characters a single MCP tool result is truncated to
    # before being handed back to the model (keeps a chatty server from
    # blowing the context window). 0 = no cap. Mirrors Claude Code's
    # `--max-mcp-output-tokens` (we measure characters, not tokens).
    mcp_max_output_chars: int = 0
    # Transcript retention: delete stored sessions older than this many days
    # on startup. 0 = keep forever. Mirrors Claude Code's `cleanupPeriodDays`.
    cleanup_period_days: int = 0
    # When True, auto-run a locally-installed formatter (ruff/black/prettier/
    # gofmt/rustfmt, by extension) after write_file/edit_file/apply_patch.
    # No-op when the formatter isn't installed. Mirrors opencode's format-on-edit.
    format_on_edit: bool = False
    # When True, run the linter after a write and fold any diagnostics into the
    # tool result so the model sees errors it just introduced (cheap LSP-lite
    # feedback). No-op when the linter isn't installed / no findings.
    check_on_edit: bool = False
    # web_search backend: "ddg" (DuckDuckGo, keyless, default), "searxng" (a
    # self-hosted SearXNG instance — set searxng_url), or "ollama" (Ollama's
    # web search API — needs OLLAMA_API_KEY). All keep search local-first.
    search_backend: str = "ddg"
    searxng_url: str = ""  # e.g. "http://localhost:8888" for the searxng backend


@dataclass
class AutoSettings:
    """Permission policy. Categories listed here run without prompting.

    `subagent` and `shell` deliberately default to NOT auto-approved — they
    spawn things you may want to see before they happen.

    `mode` layers on top (see evi/permissions.py): "ask" (default) prompts for
    non-approved tools, "accept_edits" auto-allows file edits, "plan" denies all
    tools, "yolo" allows everything. `rules` is a first-match allow/deny list of
    `<allow|deny> <tool-glob> [arg-glob]` strings (e.g. "deny shell rm*").
    """

    auto_approve: list[str] = field(
        default_factory=lambda: ["fs", "code", "memory", "skills", "image"]
    )
    mode: str = "ask"  # ask | accept_edits | plan | yolo
    rules: list[str] = field(default_factory=list)
    # Always-deny rules, evaluated BEFORE everything (even yolo / allow rules) —
    # an unconditional block an allow can't override. Same syntax as `rules`
    # (the action is forced to deny), e.g. "shell rm -rf*" or "fs *.pem".
    hard_deny: list[str] = field(default_factory=list)
    # Paths that force a prompt even under accept_edits / trusted_dirs — writing
    # code-executing or secret files should never be silently auto-approved.
    # Matched (fnmatch) against the path and its basename. Edit to taste.
    protected_paths: list[str] = field(default_factory=lambda: [
        ".env", "*.env", ".env.*", ".npmrc", ".gitconfig", ".pypirc",
        ".bashrc", ".zshrc", ".bash_profile", ".profile",
        "*.pem", "id_rsa", "id_ed25519",
    ])
    # Auto-approve fs/code tools whose path is under one of these dirs, and
    # web fetches to one of these hosts — without listing the whole category.
    trusted_dirs: list[str] = field(default_factory=list)
    trusted_domains: list[str] = field(default_factory=list)
    # Session flag set by `/effort ultracode`: auto-run each substantive REPL
    # turn through the ultracode pipeline (the eVi analogue of Claude Code's
    # `/effort ultracode`). Off by default; cleared by the other effort levels.
    ultracode: bool = False


@dataclass
class SpecialtyModels:
    """Per-task SLM (small specialty models), distinct from the main
    instruct/coder model — so a small dedicated model can handle OCR / vision /
    speech without swapping the chat model. Empty = today's behavior (main
    model / tesseract / faster-whisper / [voice] engine).

    ``ocr`` and ``vision`` are chat-VLM ids served over the OpenAI image schema
    on eVi's backends (Ollama / LM Studio / llama.cpp / openai_compat). By
    default they use the ``[llm]`` backend + base_url; set ``*_base_url`` (and
    optionally ``*_backend``) to point a specialty at a SEPARATE local server
    (e.g. a vLLM OCR endpoint, or a dedicated Ollama). ``stt`` is a
    faster-whisper model id consumed by ``[voice]`` (e.g. ``large-v3-turbo``);
    ``tts`` names an engine-specific voice/model where applicable.

    Examples: ocr = "glm-ocr" (Ollama), vision = "moondream",
    stt = "large-v3-turbo".
    """

    ocr: str = ""
    ocr_base_url: str = ""
    ocr_backend: str = ""
    vision: str = ""
    vision_base_url: str = ""
    vision_backend: str = ""
    stt: str = ""
    tts: str = ""
    # Dedicated safety-guard model for the [[guard]] guardrail layer
    # (Llama Guard / ShieldGemma, e.g. "llama-guard3"). See evi/guardmodel.py.
    guard: str = ""
    guard_base_url: str = ""
    guard_backend: str = ""
    # Speaker diarization + document-layout/OCR specialty models (heavy, lazy
    # optional extras — evi/diarize.py [diarize] and evi/doclayout.py [doc]).
    diarize: str = ""
    doc_layout: str = ""


@dataclass
class WorktreeSettings:
    """`evi worktree` defaults. `base_ref` is the branch/commit new worktrees
    fork from when `--base` isn't given (e.g. "main", so feature worktrees
    always branch off main rather than whatever HEAD happens to be). Empty =
    HEAD. Mirrors Claude Code's worktree baseRef."""

    base_ref: str = ""


@dataclass
class NotifySettings:
    """Completion notifications (off by default). When `enabled`, eVi pings on
    turn-done so you can walk away from a long local turn.
    `sound` = a beep; `desktop` = a native toast (macOS/Linux; Windows visual
    toasts come from the desktop/web UI); `url` = an ntfy topic or webhook POSTed
    so a remote turn can still reach your phone. See evi/notify.py."""

    enabled: bool = False
    sound: bool = True
    desktop: bool = True
    url: str = ""


@dataclass
class Config:
    llm: LLMSettings = field(default_factory=LLMSettings)
    comfy: ComfySettings = field(default_factory=ComfySettings)
    google: GoogleSettings = field(default_factory=GoogleSettings)
    microsoft: MicrosoftSettings = field(default_factory=MicrosoftSettings)
    obsidian: ObsidianSettings = field(default_factory=ObsidianSettings)
    tools: ToolToggles = field(default_factory=ToolToggles)
    auto: AutoSettings = field(default_factory=AutoSettings)
    web: WebSettings = field(default_factory=WebSettings)
    telemetry: TelemetrySettings = field(default_factory=TelemetrySettings)
    statusline: StatusLineSettings = field(default_factory=StatusLineSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    plugins: PluginsSettings = field(default_factory=PluginsSettings)
    federation: FederationSettings = field(default_factory=FederationSettings)
    ultracode: UltracodeSettings = field(default_factory=UltracodeSettings)
    worktree: WorktreeSettings = field(default_factory=WorktreeSettings)
    models: SpecialtyModels = field(default_factory=SpecialtyModels)
    notify: NotifySettings = field(default_factory=NotifySettings)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
        # Profile overlay (cheap when none active — returns {}).
        # Imported lazily to avoid the circular import at module load.
        from evi.profiles import load_profile_overlay, merge_overlay

        overlay = load_profile_overlay()
        if overlay:
            data = merge_overlay(data, overlay)
        # Per-project `.evi.toml` (walked up from cwd) wins over user + profile,
        # so a repo can pin its own model / tools / permissions.
        from evi.project import load_project_config_overlay

        project_overlay = load_project_config_overlay()
        if project_overlay:
            data = merge_overlay(data, project_overlay)
        return cls(
            llm=LLMSettings(**data.get("llm", {})),
            comfy=ComfySettings(**data.get("comfy", {})),
            google=GoogleSettings(**data.get("google", {})),
            microsoft=MicrosoftSettings(**data.get("microsoft", {})),
            obsidian=ObsidianSettings(**data.get("obsidian", {})),
            tools=ToolToggles(**data.get("tools", {})),
            auto=AutoSettings(**data.get("auto", {})),
            web=WebSettings(**data.get("web", {})),
            telemetry=TelemetrySettings(**data.get("telemetry", {})),
            statusline=StatusLineSettings(**data.get("statusline", {})),
            voice=VoiceSettings(**data.get("voice", {})),
            plugins=PluginsSettings(**data.get("plugins", {})),
            federation=FederationSettings(**data.get("federation", {})),
            ultracode=UltracodeSettings(**data.get("ultracode", {})),
            worktree=WorktreeSettings(**data.get("worktree", {})),
            models=SpecialtyModels(**data.get("models", {})),
            notify=NotifySettings(**data.get("notify", {})),
        )

    def save(self) -> None:
        ensure_dirs()
        CONFIG_PATH.write_text(_to_toml(asdict(self)), encoding="utf-8")


MODELS_DIR = HOME / "models"
PROFILES_DIR = HOME / "profiles"
COMMANDS_DIR = HOME / "commands"
INDICES_DIR = HOME / "indices"
RECIPES_DIR = HOME / "recipes"
STYLES_DIR = HOME / "styles"


def ensure_dirs() -> None:
    for d in (
        HOME,
        TOKEN_DIR,
        IMAGE_DIR,
        LOG_DIR,
        SKILL_DIR,
        SCHEDULED_DIR,
        SCHEDULED_LOG_DIR,
        MODELS_DIR,
        PROFILES_DIR,
        COMMANDS_DIR,
        TRANSCRIPTS_DIR,
        DREAM_LOG_DIR,
        SCREENSHOT_DIR,
        UPLOADS_DIR,
        INDICES_DIR,
        RECIPES_DIR,
        STYLES_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def _to_toml(d: dict[str, Any]) -> str:
    """Minimal TOML writer for our flat-section config (no nested tables)."""
    lines: list[str] = []
    for section, body in d.items():
        lines.append(f"[{section}]")
        for k, v in body.items():
            lines.append(f"{k} = {_fmt(v)}")
        lines.append("")
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
