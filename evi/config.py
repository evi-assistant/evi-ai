"""Configuration loading and paths.

Config lives at %USERPROFILE%/.evi/config.toml. First run writes defaults.

`Config.load()` layers, lowest first: config.toml (the USER layer), the
active profile (~/.evi/profiles/<name>.toml), then the project `.evi.toml` —
only in a trusted folder and only through an allowlist (evi/project.py,
evi/trust.py). `Config.save()` writes back only the user layer plus the
fields the caller changed, so a profile's or a repository's values never end
up in config.toml.
"""

from __future__ import annotations

import copy
import datetime as _dt
import logging
import os
import re
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any

import tomllib

logger = logging.getLogger(__name__)


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
    # A delegate CLI-agent backend (codex, gemini, amp, qwen, copilot) runs its
    # own tools outside eVi's permission checks, so unattended runs (scheduled
    # tasks, routines, channels, federation, workflows, subagents, `evi run`)
    # refuse it unless this is true (evi/llm/cli_agent.py `unattended`).
    allow_cli_agent_unattended: bool = False


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
    """Web frontend security + behaviour (docs/security.md).

    `auth_token` empty, with no launch token (EVI_WEB_AUTH_TOKEN) and no
    users, means no-token mode: the server answers only this machine
    (loopback clients). When set, every `/api/*` route requires either an
    `Authorization: Bearer <token>` header OR a `?token=<token>` query
    parameter (the latter is needed for `<img>` src and streaming endpoints
    that can't carry custom headers), from this machine too.

    Generate a token with `evi web-config token rotate`.

    `multi_user` (opt-in) lets a team each log in with their own revocable token
    from `~/.evi/users.json` instead of sharing `auth_token`. Each user gets an
    isolated workspace — their web sessions, transcripts, and memory live under
    `~/.evi/users/<name>/` and are not visible to other users.

    `decision_timeout_s`: how long a permission prompt waits for an answer
    before it denies. EVI_DECISION_TIMEOUT_S overrides it; anything that isn't
    a positive number means the default.
    """

    auth_token: str = ""
    multi_user: bool = False
    decision_timeout_s: float = 300.0


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

    `subagent`, `shell` and `code` deliberately default to NOT auto-approved —
    they spawn things you may want to see before they happen. `code` is
    running code (run_python, a linter that runs project code); `code_read`
    (python_symbols, check_file with ruff, monitor tailing a file) only reads
    it, and is in the default. A call is matched by what it does
    (`Tool.category_for`): monitor kind=command is `shell`, and a sandboxed
    run_python is `code_read`. In a run a remote trigger drives (federation,
    A2A, a routine, a channel push) running code is never auto-approved
    (evi/permissions.py `decide`); a local unattended run goes by this list.

    `mode` layers on top (see evi/permissions.py): "ask" (default) prompts for
    non-approved tools, "accept_edits" auto-allows file edits, "plan" denies all
    tools, "yolo" allows everything. `rules` is a first-match allow/deny list of
    `<allow|deny> <tool-glob> [arg-glob]` strings (e.g. "deny shell rm*").
    """

    # Lists saved with the old default ("code" for "code_read") keep running
    # code unasked; `evi lint` / `evi doctor` flag them with the migrated list.
    auto_approve: list[str] = field(
        default_factory=lambda: ["fs", "code_read", "memory", "skills", "image"]
    )
    mode: str = "ask"  # ask | accept_edits | plan | yolo
    # First-match "allow|deny <tool-name-or-category-glob> [arg-glob]"
    # (evi/permissions.py); "shell" also matches monitor kind=command.
    # A shell line is matched per command: an allow rule with an arg-glob
    # approves it only if every command is allowed, the line parses and its
    # redirects stay in the working folder; a deny matches any one command.
    rules: list[str] = field(default_factory=list)
    # Always-deny rules, evaluated BEFORE everything (even yolo / allow rules) —
    # an unconditional block an allow can't override. Same syntax as `rules`
    # (the action is forced to deny), e.g. "shell rm -rf*" or "fs *.pem".
    hard_deny: list[str] = field(default_factory=list)
    # Secret and shell start-up files: writing one asks in every mode (yolo
    # included) and reading one asks even with fs auto-approved. Matched
    # (fnmatch, ignoring case) on the path, its name and its real path. Edit to
    # taste; eVi's built-in write-protected paths (EVI.md, AGENTS.md,
    # .evi.toml, .evi/**, ~/.evi/**, .git/hooks/**, .github/workflows/** ...;
    # evi.permissions.WRITE_PROTECTED_PATHS) always apply on top of it.
    protected_paths: list[str] = field(default_factory=lambda: [
        ".env", "*.env", ".env.*", ".npmrc", ".gitconfig", ".pypirc",
        ".bashrc", ".zshrc", ".bash_profile", ".profile",
        "*.pem", "id_rsa", "id_ed25519",
    ])
    # Entries to un-protect, by their exact text: a built-in write-protected
    # path (e.g. "AGENTS.md") that would otherwise be added back on every load,
    # or one of the list above. Only config.toml or a profile can set this; a
    # project's .evi.toml can't.
    protected_paths_remove: list[str] = field(default_factory=list)
    # Auto-approve fs/code tools whose path is under one of these dirs, and
    # web fetches to one of these hosts — without listing the whole category.
    trusted_dirs: list[str] = field(default_factory=list)
    trusted_domains: list[str] = field(default_factory=list)
    # Session flag set by `/effort ultracode`: auto-run each substantive REPL
    # turn through the ultracode pipeline (the eVi analogue of Claude Code's
    # `/effort ultracode`). Off by default; cleared by the other effort levels.
    ultracode: bool = False
    # Curated destructive-shell-command guard (evi/shell_guard.py). A matched
    # command (rm -rf ~, git reset --hard, disk format, force-push, secret
    # exfil, IaC destroy, …) can never run silently: it forces a confirmation
    # prompt when a UI exists and is DENIED when there's none (headless /
    # scheduler). Overrides yolo / accept_edits / auto-approve. On by default.
    block_destructive: bool = True
    # fnmatch globs that EXEMPT a command from the guard, matched against each
    # command of the line (evi/shellsplit.py), never the whole line: a hit is
    # exempt only if every command it touches matches, and never on a line
    # that can't be split. E.g. ["git push --force-with-lease*", "git stash drop"].
    destructive_allow: list[str] = field(default_factory=list)
    # Builtin rule ids to silence without disabling the whole guard, e.g.
    # ["git-commit-amend", "system-power"]. See evi/shell_guard.RULE_IDS.
    destructive_disable_rules: list[str] = field(default_factory=list)
    # What a tool call that would ask does when nobody can be asked: a
    # scheduled task, a subagent or dream run, a workflow step, an SDK embed
    # with no permission callback. "deny" (the default, and what any other
    # value means) refuses it; "allow" runs it, the explicit escape hatch.
    # Auto-approved categories run either way; protected paths and the
    # destructive guard deny either way.
    unattended_policy: str = "deny"


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
class DesktopSettings:
    """Desktop-app (Tauri shell) preferences. The shell reads these from
    config.toml at launch; the web UI edits them like any other setting."""

    # Background-update the frozen Python core (the `evi-server` sidecar) from
    # the signed sidecar-latest channel, applied on next launch. Off pins the
    # core to the installed version. The `EVI_SIDECAR_UPDATE=0` env var still
    # forces it off regardless of this setting.
    sidecar_auto_update: bool = True


@dataclass
class ChannelSettings:
    """Inbound chat channels — reach eVi from a phone without a mobile app.

    Off by default and deliberately narrow: a channel drives a tool-capable
    agent, so an unknown sender is gated by a pairing code (approved locally with
    `evi channel approve`) and an approved one runs with `telegram_tools`, NOT
    with whatever the desktop user has enabled, under the channel's own policy
    (evi/channels/policy.py), never `[auto]`. Keep `shell`/`fs`/`code`/`computer`
    out of these lists unless you fully trust everyone you pair with.
    """

    telegram_enabled: bool = False
    telegram_token: str = ""
    # Tool CATEGORIES an approved sender may use. Chosen explicitly rather than
    # inherited, so enabling shell locally never widens the bot. [] = no tools.
    telegram_tools: list[str] = field(default_factory=lambda: ["memory", "skills"])
    # Categories (of telegram_tools) that run without asking. Nobody is at the
    # desk to answer a prompt, so every other call is denied. [auto] never
    # applies to the bot: not its auto_approve, mode (yolo / accept_edits),
    # allow rules or trusted dirs. Listing `code`, `fs`, `shell` or `computer`
    # lets every paired sender run code or change files on this machine.
    telegram_auto_approve: list[str] = field(default_factory=list)
    telegram_max_turns: int = 6
    # Answer paired senders in group chats too. Off: only direct (private)
    # chats are answered, and a pairing code is only ever sent in one.
    allow_groups: bool = False
    # What a channel may do with your long-term memory: "recall" (default:
    # read and search it), "staged" (also remember, but each note waits in
    # ~/.evi/inbox/channel/ as untrusted until you apply it) or "full" (read,
    # write and forget it directly). Any other value means "recall".
    memory_mode: str = "recall"


@dataclass
class CheckSettings:
    """Verify-before-done (evi/checks.py): the project checks a turn runs once
    it has changed files, before it may end.

    `verify`: "auto" (on for a run under the implementer policy, `evi run
    --policy implementer`; off in an interactive session), "always" (also an
    interactive Code-mode session, each check offered through the normal
    permission flow first) or "off". A failing round is fed back to the model;
    after `max_rounds` failing rounds the turn ends with stop_reason
    "checks_failed". `timeout` is seconds per command, `tail_lines` how much
    of a check's output is kept. `detect` = false runs only `commands`: your
    own command lines (or argv arrays), run whenever files changed. A
    project's .evi.toml [checks] commands count only in a trusted folder, and
    no check runs at all in an untrusted one."""

    verify: str = "auto"  # auto | always | off
    max_rounds: int = 2
    timeout: int = 300
    tail_lines: int = 40
    detect: bool = True
    commands: list[str] = field(default_factory=list)


@dataclass
class HookSettings:
    """Hook behaviour (evi/hooks.py; the hooks themselves are in hooks.toml).
    `max_blocks`: how many times, per user turn, `protocol = "json"` stop hooks
    may send the model back to work; a block after that ends the turn with
    stop_reason "stop_hook_exhausted"."""

    max_blocks: int = 3


@dataclass
class RuntimeSettings:
    """Managed llama.cpp runtime. Normally eVi downloads + supervises its own
    `llama-server` under ~/.evi/runtime. Set `server_path` to point eVi at an
    EXISTING `llama-server` binary instead (your own install / GPU build) — eVi
    then supervises that one and skips the download (the "locate existing" path)."""

    server_path: str = ""


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
    desktop: DesktopSettings = field(default_factory=DesktopSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    channels: ChannelSettings = field(default_factory=ChannelSettings)
    checks: CheckSettings = field(default_factory=CheckSettings)
    hooks: HookSettings = field(default_factory=HookSettings)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
        # The user layer as written — save() patches the caller's changes
        # into this, never the merged result below.
        user = copy.deepcopy(data)
        # Safe mode means STOCK config: skip both the profile overlay and the
        # per-project `.evi.toml`. Either can re-introduce exactly what a clean
        # boot is trying to rule out (a pinned model, an output_style pointing at
        # a broken style file, tool toggles, permission rules), so gating here —
        # inside Config.load — covers every entrypoint at once (CLI, web/desktop,
        # subagents, dream, scheduler).
        from evi import safemode

        if not safemode.enabled():
            # Profile overlay (cheap when none active — returns {}).
            # Imported lazily to avoid the circular import at module load.
            from evi.profiles import load_profile_overlay, merge_overlay

            overlay = load_profile_overlay()
            if overlay:
                data = merge_overlay(data, overlay)
            # Per-project `.evi.toml` (walked up from cwd to the git top
            # level). It ships with the repo, so it is UNTRUSTED: it applies
            # only in a folder the user trusted, and only the version they
            # trusted (evi/trust.py) — never prompted for here, so evi run,
            # web and the scheduler fail closed. Even then only allowlisted
            # preferences (model, sampling) apply as-is, and [tools]/[auto]
            # can only TIGHTEN the user + profile values above. Whatever isn't
            # applied is reported once per process. Never raises: a file that
            # can't be parsed or applied is ignored whole (fail closed). See
            # evi/project.py.
            from evi.project import apply_project_config

            data = apply_project_config(data)
        cfg, unknown = cls._from_dict(data)
        _warn_unknown_keys(unknown)
        cfg._user_raw = user
        cfg._snapshot = asdict(cfg)
        return cfg

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> tuple["Config", list[str]]:
        """Build a Config from merged TOML data. Keys this version doesn't
        know (a typo, or a setting from a newer eVi) are left out and
        returned as dotted names rather than crashing the load; save() keeps
        them in config.toml. Every section is read back — a section missing
        here would be write-only (the old [desktop] bug)."""
        unknown: list[str] = []
        sections: dict[str, Any] = {}
        for f in fields(cls):
            section_cls = f.default_factory  # type: ignore[misc]
            body = data.get(f.name, {})
            if not isinstance(body, dict):
                unknown.append(f.name)
                body = {}
            known = {g.name for g in fields(section_cls)}
            sections[f.name] = section_cls(**{k: v for k, v in body.items() if k in known})
            unknown.extend(f"{f.name}.{k}" for k in body if k not in known)
        names = set(sections)
        unknown.extend(str(k) for k in data if k not in names)
        return cls(**sections), sorted(unknown)

    def save(self) -> None:
        """Write config.toml — the USER layer only.

        For a Config from load(), that is the file as it is on disk now (or as
        it was at load, if it has since become unreadable) with the fields the
        caller changed since load() (or the last save) patched in. Profile and
        project values are never written: before, saving any setting inside a
        repo (/model, POST /api/config, …) copied the repo's `.evi.toml` and
        the active profile into config.toml for good.

        What a changed field writes is worked out against the two fixed
        references load() kept — the merged values it returned and the user's
        own config.toml — so it doesn't matter how often the caller saves:
        - set back to the value load() returned (an undo, e.g. a failed
          backend switch rolling back): the user's own value is restored, or
          the key dropped if config.toml never had it;
        - a list that a profile or a trusted repo contributed to (e.g. a
          profile's auto_approve, the repo's hard_deny union): only the
          entries the caller added or removed are applied to the user's own
          list, never the inherited ones;
        - anything else: the new value.
        A field set to the value it already had isn't a change, so setting a
        value you only inherit (from a profile or `.evi.toml`) doesn't copy it
        into config.toml — set it in config.toml itself to keep it.

        A Config built in code (`Config()`) has no layers and is written
        whole, as before."""
        ensure_dirs()
        current = asdict(self)
        loaded: dict[str, Any] | None = getattr(self, "_snapshot", None)
        if loaded is None:
            out = current
        else:
            last: dict[str, Any] = getattr(self, "_last", None) or loaded
            user: dict[str, Any] = getattr(self, "_user_raw", None) or {}
            defaults = asdict(type(self)())
            out = self._user_layer_on_disk()
            for section, body in current.items():
                for key, value in body.items():
                    if (last.get(section) or {}).get(key, _MISSING) == value:
                        continue  # not changed since load() / the last save
                    _patch_user_layer(
                        out, user, section, key, value,
                        at_load=(loaded.get(section) or {}).get(key, _MISSING),
                        default=(defaults.get(section) or {}).get(key, _MISSING),
                    )
        CONFIG_PATH.write_text(_to_toml(out), encoding="utf-8")
        self._last = current

    def _user_layer_on_disk(self) -> dict[str, Any]:
        """config.toml as it is now — another surface may have saved since
        load() — else the copy load() kept."""
        try:
            with CONFIG_PATH.open("rb") as f:
                return tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return copy.deepcopy(getattr(self, "_user_raw", None) or {})


_MISSING: Any = object()


def _patch_user_layer(
    out: dict[str, Any],
    user: dict[str, Any],
    section: str,
    key: str,
    value: Any,
    *,
    at_load: Any,
    default: Any,
) -> None:
    """Apply one changed field to the user layer `out` (see Config.save).
    `user` is config.toml as load() read it, `at_load` the merged value
    load() returned, `default` the field's default."""
    user_body = user.get(section)
    has_own = isinstance(user_body, dict) and key in user_body
    own = user_body[key] if has_own else default  # the user layer's own value
    if value == at_load:
        new = own  # an undo: back to what the user layer had
    elif (
        isinstance(value, list)
        and isinstance(at_load, list)
        and isinstance(own, list)
        and at_load != own
    ):
        # The loaded list was (partly) inherited — a profile's list, or a
        # trusted repo's entries unioned in. Carry over the caller's edits
        # only, so the inherited entries stay where they came from.
        removed = [x for x in at_load if x not in value]
        added = [x for x in value if x not in at_load]
        new = [x for x in own if x not in removed] + [x for x in added if x not in own]
    else:
        new = value
    target = out.get(section)
    if new == own and not has_own:
        # Nothing of the user's own to write: leave the key (and a section
        # config.toml didn't have) out rather than pinning the default.
        if isinstance(target, dict):
            target.pop(key, None)
            if not target and not isinstance(user_body, dict):
                del out[section]
        return
    if not isinstance(target, dict):
        target = out[section] = {}
    target[key] = copy.deepcopy(user_body[key] if new == own else new)


# Unknown config keys already reported (once per process per key set).
_WARNED_UNKNOWN: set[tuple[str, ...]] = set()


def _warn_unknown_keys(unknown: list[str]) -> None:
    if not unknown:
        return
    key = tuple(unknown)
    if key in _WARNED_UNKNOWN:
        return
    _WARNED_UNKNOWN.add(key)
    from evi.project import format_key_list

    logger.warning(
        "eVi: unknown settings in %s (or the active profile): %s. They are "
        "ignored but kept in the file — a newer eVi may use them; otherwise "
        "check for a typo.",
        CONFIG_PATH,
        format_key_list(unknown),
    )


MODELS_DIR = HOME / "models"
PROFILES_DIR = HOME / "profiles"
COMMANDS_DIR = HOME / "commands"
INDICES_DIR = HOME / "indices"
RECIPES_DIR = HOME / "recipes"
STYLES_DIR = HOME / "styles"
RUNTIME_DIR = HOME / "runtime"   # managed llama.cpp runtime (downloaded binaries)


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
        RUNTIME_DIR,
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
    """Small TOML writer for config.toml: one `[section]` per table. Since
    save() writes the user's own file back, it also round-trips what our
    flat sections don't use — top-level keys, nested tables (`[a.b]`),
    inline tables, dates and odd key names."""
    lines = [f"{_key(k)} = {_fmt(v)}" for k, v in d.items() if not isinstance(v, dict)]
    if lines:
        lines.append("")
    for section, body in d.items():
        if isinstance(body, dict):
            _table(lines, [section], body)
    return "\n".join(lines)


def _table(lines: list[str], path: list[Any], body: dict[str, Any]) -> None:
    scalars = [(k, v) for k, v in body.items() if not isinstance(v, dict)]
    if scalars or len(scalars) == len(body):  # no bare `[a]` above `[a.b]`
        lines.append("[" + ".".join(_key(p) for p in path) + "]")
        lines.extend(f"{_key(k)} = {_fmt(v)}" for k, v in scalars)
        lines.append("")
    for k, v in body.items():
        if isinstance(v, dict):
            _table(lines, [*path, k], v)


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n",
            "\f": "\\f", "\r": "\\r"}


def _key(k: Any) -> str:
    k = str(k)
    return k if _BARE_KEY.fullmatch(k) else _quote(k)


def _quote(s: str) -> str:
    """A TOML basic string. Control characters are escaped: written raw, a
    newline in a value (e.g. a stop sequence) made the file unloadable."""
    return '"' + "".join(
        _ESCAPES.get(c) or (f"\\u{ord(c):04x}" if ord(c) < 0x20 or ord(c) == 0x7F else c)
        for c in s
    ) + '"'


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_key(k)} = {_fmt(x)}" for k, x in v.items()) + "}"
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    return _quote(str(v))
