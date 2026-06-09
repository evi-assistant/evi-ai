"""`evi` CLI entry point.

    evi chat            interactive REPL
    evi config show     print resolved config
    evi config path     print config file path
    evi tools           list registered tools
"""

from __future__ import annotations

import atexit
import os
import sys
from dataclasses import asdict
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from evi.backends import default_base_url, get_backend, KNOWN_BACKENDS
from evi.commands import CommandStore
from evi.config import CONFIG_PATH, MCP_CONFIG_PATH, Config, ensure_dirs
from evi.hooks import load_hooks
from evi.llm.agent import (
    Agent,
    Done,
    Error,
    Guardrail,
    LogProbs,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolProgress,
    ToolResult,
    UsageStats,
)
from evi.llm.client import make_client
from evi.mcp import MCPManager, filter_allowed, load_servers
from evi.profiles import (
    ENV_VAR as PROFILE_ENV_VAR,
    PROFILES_DIR,
    list_profiles,
    load_profile_overlay,
    profile_path,
)
from evi.project import load_project_context
from evi.scheduled import TaskStore
from evi.transcripts import TranscriptStore

# Register built-in tools by importing for side effects.
from evi.tools import REGISTRY  # noqa: F401
import evi.tools.fs  # noqa: F401
import evi.tools.code  # noqa: F401
import evi.tools.image_comfy  # noqa: F401
import evi.tools.memory  # noqa: F401
import evi.tools.skills  # noqa: F401
import evi.tools.subagent  # noqa: F401
import evi.tools.websearch  # noqa: F401
import evi.tools.voice  # noqa: F401
import evi.tools.computer  # noqa: F401
import evi.tools.pdf  # noqa: F401
import evi.tools.sqlite  # noqa: F401
import evi.tools.index  # noqa: F401
import evi.tools.git  # noqa: F401
import evi.tools.ocr  # noqa: F401
import evi.tools.calendar  # noqa: F401
import evi.tools.rerank  # noqa: F401
from evi.memory import MemoryStore
from evi.skills import SkillStore
from evi.tools.base import get_enabled_tools


_mcp_manager: MCPManager | None = None


def _ensure_mcp(config: Config) -> MCPManager | None:
    """Start MCP once per process if enabled. Idempotent."""
    global _mcp_manager
    if not config.tools.mcp:
        return None
    if _mcp_manager is not None:
        return _mcp_manager
    servers = filter_allowed(load_servers(), config.tools.mcp_allow)
    if not servers:
        return None
    try:
        manager = MCPManager(servers)
        manager.start()
    except ImportError:
        console.print(
            "[yellow]MCP enabled but `mcp` package not installed — "
            "run: pip install 'evi-assistant[mcp]'[/yellow]"
        )
        return None
    _mcp_manager = manager
    atexit.register(manager.stop)
    return manager


app = typer.Typer(add_completion=False, no_args_is_help=True, help="eVi — personal AI assistant.")
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        from evi import __version__
        console.print(f"evi {__version__}")
        raise typer.Exit()


@app.callback()
def _global_options(
    profile: str | None = typer.Option(
        None,
        "--profile",
        "-p",
        envvar=PROFILE_ENV_VAR,
        help="Activate a profile from ~/.evi/profiles/<name>.toml. "
             "Overrides parts of the base config for this invocation.",
    ),
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        "-d",
        envvar="EVI_DEBUG",
        help="Print LLM requests + tool calls to stderr.",
    ),
) -> None:
    """Top-level options consumed before any subcommand runs."""
    if profile:
        # Push into env so downstream Config.load() / subprocess children
        # pick it up uniformly.
        os.environ[PROFILE_ENV_VAR] = profile
    if debug:
        from evi.debug import set_enabled
        set_enabled(True)
    # Opt-in crash reporting — a no-op NullReporter unless [telemetry]
    # crash_reports is true AND a dsn is set (or the env overrides). Installs a
    # chained sys.excepthook so uncaught CLI errors are reported (scrubbed),
    # then still printed normally.
    from evi.reporting import init_reporting, install_excepthook
    install_excepthook(init_reporting())
    # Opt-in OpenTelemetry traces/metrics (no-op unless [telemetry] traces/metrics
    # + an endpoint are set and the otel deps are installed).
    from evi import otel
    otel.init_telemetry()


def _cli_permission_prompt(name: str, args: str, category: str) -> bool:
    """Ask the user whether to allow a tool call. Default-deny on empty input."""
    preview = args if len(args) <= 200 else args[:200] + "…"
    console.print(
        f"\n[yellow]permission:[/yellow] "
        f"[bold]{name}[/bold] [dim]({category})[/dim] "
        f"args={preview}"
    )
    answer = console.input(
        "  approve? [bold]y[/bold]/n/a (allow all this session): "
    ).strip().lower()
    if answer in ("a", "all"):
        # Caller (the Agent) checks auto_all after this returns; mutate via
        # the global-ish reference we stash below in _build_agent.
        _AUTO_STATE["agent"].enable_auto_all()
        return True
    return answer in ("y", "yes")


def _cli_permission_prompt_batch(calls: list[tuple[str, str, str]]) -> list[bool]:
    """Prompt ONCE for a whole multi-tool turn. Returns a parallel bool list.

    Input: list of (tool_name, args_json, category) for the calls that need
    a decision (pre-approved ones never reach here). The user can approve
    all, deny all, allow-all-this-session, or pick specific 1-based indices.
    """
    console.print(f"\n[yellow]permission:[/yellow] {len(calls)} tool calls requested")
    for i, (name, args, category) in enumerate(calls, 1):
        preview = args if len(args) <= 160 else args[:160] + "…"
        console.print(
            f"  [bold]{i}.[/bold] [bold]{name}[/bold] [dim]({category})[/dim] args={preview}"
        )
    answer = console.input(
        "  approve? [bold]a[/bold]ll / [bold]n[/bold]one / "
        "indices (e.g. 1,3) / [bold]s[/bold] (allow all this session): "
    ).strip().lower()
    if answer in ("s", "session"):
        _AUTO_STATE["agent"].enable_auto_all()
        return [True] * len(calls)
    if answer in ("a", "all", "y", "yes"):
        return [True] * len(calls)
    if answer in ("", "n", "no", "none"):
        return [False] * len(calls)
    # Parse comma/space-separated 1-based indices to allow.
    allowed: set[int] = set()
    for tok in answer.replace(",", " ").split():
        if tok.isdigit():
            allowed.add(int(tok))
    return [(i + 1) in allowed for i in range(len(calls))]


# Stash the active agent so the permission prompt can flip auto_all.
_AUTO_STATE: dict[str, Agent] = {}


def _build_agent() -> Agent:
    ensure_dirs()
    config = Config.load()
    _ensure_mcp(config)  # registers MCP tools before we read REGISTRY
    client = make_client(config.llm)
    toggles = asdict(config.tools)
    tools = get_enabled_tools(toggles)
    memory = MemoryStore() if toggles.get("memory") else None
    skills = SkillStore() if toggles.get("skills") else None
    project = load_project_context()
    hooks = load_hooks()
    transcripts = TranscriptStore() if toggles.get("transcripts") else None
    from evi.guardrails import Guardrails

    guardrails = Guardrails.load()
    agent = Agent(
        client=client,
        config=config,
        tools=tools,
        memory=memory,
        skills=skills,
        project=project,
        hooks=hooks,
        permission_callback=_cli_permission_prompt,
        permission_batch_callback=_cli_permission_prompt_batch,
        transcripts=transcripts,
        guardrails=guardrails if guardrails.enabled else None,
    )
    _AUTO_STATE["agent"] = agent
    return agent


# --- slash command dispatch -------------------------------------------------

# Result codes for handlers:
#   "continue"  — handled in-REPL, ask user for the next message
#   "exit"      — leave the REPL
#   message     — string to forward to the LLM as the user's turn
SlashResult = str  # "continue" | "exit" | <expanded prompt>


def _handle_help(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    builtins = [
        ("/help", "show this list"),
        ("/reset", "clear conversation history"),
        ("/exit, /quit", "leave the REPL"),
        ("/tools", "list active tools"),
        ("/model [id]", "show or switch the active model"),
        ("/goal [text|clear]", "set / clear / show the ongoing goal"),
        ("/plan", "next turn runs in plan-only mode (no tools)"),
        ("/auto [on|off]", "auto-approve every tool call for this session"),
        ("/compact", "summarise older history into one note to free context"),
        ("/context, /ctx", "show where the context window is being spent"),
        ("/recent [n]", "list recent sessions (resume via `evi sessions resume`)"),
        ("/image <path>", "attach an image to the next turn (VLM models)"),
        ("/effort [low|medium|high|max]", "set reasoning effort"),
        ("/fast [on|off|<model-id>]", "toggle fast mode (swap to a smaller model)"),
        ("/json <prompt>", "force JSON-object output for the next turn"),
        ("/schema <file> [prompt]", "constrain the next turn to a JSON Schema"),
        ("/notools <prompt>", "answer the next turn without using any tools"),
        ("/forcetool <name> <prompt>", "force the model to call a specific tool"),
        ("/reload", "re-read config.toml without restarting"),
        ("/audio <path>", "transcribe an audio file and send as the next turn"),
        ("/audioraw <path> [prompt]", "attach raw audio (omni models) / auto-transcribe otherwise"),
        ("/speak [on|off]", "auto-speak assistant replies sentence-by-sentence"),
        ("/predict <text|file <p>|clear>", "set a speculative-decoding hint for the next turn"),
    ]
    console.print("[bold]Built-in commands:[/bold]")
    for cmd, desc in builtins:
        console.print(f"  [cyan]{cmd:<22}[/cyan] {desc}")
    user_cmds = cmd_store.list()
    if user_cmds:
        console.print("\n[bold]User commands:[/bold] [dim](~/.evi/commands/)[/dim]")
        for e in user_cmds:
            console.print(f"  [cyan]/{e.name:<21}[/cyan] {e.summary}")
    return "continue"


def _handle_reset(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    agent.reset()
    console.print("[dim]history cleared.[/dim]")
    return "continue"


def _handle_exit(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    return "exit"


def _handle_tools(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    if not agent.tools:
        console.print("[dim]no tools enabled[/dim]")
        return "continue"
    for tname, t in sorted(agent.tools.items()):
        console.print(f"  [bold]{tname}[/bold] [dim]({t.category})[/dim]")
    return "continue"


def _handle_model(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    if not args.strip():
        console.print(
            f"[bold]{agent.config.llm.model}[/bold] "
            f"[dim]via {agent.config.llm.backend}[/dim]"
        )
        return "continue"
    new_id = args.strip()
    cfg = Config.load()
    cfg.llm.model = new_id
    cfg.save()
    agent.config.llm.model = new_id
    console.print(f"[green]using[/green] {new_id} [dim](persisted)[/dim]")
    return "continue"


def _handle_goal(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    arg = args.strip()
    if not arg:
        if agent.goal:
            console.print(f"[bold]goal:[/bold] {agent.goal}")
        else:
            console.print("[dim]no goal set[/dim]")
        return "continue"
    if arg.lower() == "clear":
        agent.clear_goal()
        console.print("[dim]goal cleared[/dim]")
        return "continue"
    agent.set_goal(arg)
    console.print(f"[green]goal set:[/green] {arg}")
    return "continue"


def _handle_plan(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    agent.enable_plan_mode()
    console.print(
        "[dim]plan-only mode enabled for the next turn. "
        "Type your task.[/dim]"
    )
    if args.strip():
        # If text was passed alongside /plan, treat it as the task.
        return args.strip()
    return "continue"


def _handle_compact(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    collapsed = agent.compact_history()
    if collapsed == 0:
        console.print("[dim]nothing to compact (history is short)[/dim]")
    else:
        console.print(f"[green]compacted[/green] {collapsed} messages into a summary")
    return "continue"


_EFFORT_LEVELS = ("low", "medium", "high", "max")


def _handle_effort(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """Show or set reasoning effort. Persists to config.toml."""
    arg = args.strip().lower()
    if not arg:
        current = (agent.config.llm.reasoning_effort or "medium").lower()
        console.print(
            f"[bold]effort:[/bold] {current}  "
            f"[dim](levels: {', '.join(_EFFORT_LEVELS)})[/dim]"
        )
        return "continue"
    if arg not in _EFFORT_LEVELS:
        console.print(
            f"[red]invalid effort:[/red] {arg}  "
            f"[dim](pick one of {', '.join(_EFFORT_LEVELS)})[/dim]"
        )
        return "continue"
    cfg = Config.load()
    cfg.llm.reasoning_effort = arg
    cfg.save()
    agent.config.llm.reasoning_effort = arg
    console.print(f"[green]effort → {arg}[/green] [dim](persisted)[/dim]")
    return "continue"


def _handle_fast(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """Toggle fast mode. Optional argument: `on` / `off` / a model id."""
    arg = args.strip()
    cfg = Config.load()
    if not arg:
        state = "ON" if cfg.llm.fast_mode else "OFF"
        target = cfg.llm.fast_model or "(unset — set with /fast <model-id>)"
        console.print(f"[bold]fast mode:[/bold] {state} · target: {target}")
        return "continue"
    a = arg.lower()
    if a in ("on", "yes", "1"):
        if not cfg.llm.fast_model:
            console.print(
                "[yellow]fast_model is unset.[/yellow] Set it with "
                "[bold]/fast <model-id>[/bold] first, or edit config.toml."
            )
            return "continue"
        cfg.llm.fast_mode = True
    elif a in ("off", "no", "0"):
        cfg.llm.fast_mode = False
    else:
        # Treat the arg as a fast_model id and turn fast mode on.
        cfg.llm.fast_model = arg
        cfg.llm.fast_mode = True
    cfg.save()
    agent.config.llm.fast_mode = cfg.llm.fast_mode
    agent.config.llm.fast_model = cfg.llm.fast_model
    state = "ON" if cfg.llm.fast_mode else "OFF"
    console.print(f"[green]fast mode → {state}[/green]  target: {cfg.llm.fast_model or '(none)'}")
    return "continue"


def _handle_json(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """`/json <prompt>` — force JSON-object output for the next turn.

    The agent stashes a one-shot `response_format` that the REPL forwards
    into `agent.chat()`. Pair with a prompt that describes the expected
    schema, e.g. `/json extract {name, email} from this signature: ...`.
    """
    prompt = args.strip()
    if not prompt:
        console.print("[yellow]usage:[/yellow] /json <prompt>")
        return "continue"
    agent._pending_response_format = {"type": "json_object"}  # type: ignore[attr-defined]
    return prompt


def _handle_schema(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """`/schema <file|inline-json> [prompt]` — constrain the next turn to a JSON
    Schema (Structured Outputs). With no prompt it arms the schema for your next
    message; `/schema off` clears it."""
    from evi.structured import SchemaError, as_response_format, load_schema

    args = args.strip()
    if not args:
        console.print(
            "[yellow]usage:[/yellow] /schema <file|inline-json> [prompt]"
            "  [dim]·[/dim]  /schema off"
        )
        return "continue"
    if args.lower() == "off":
        agent._pending_response_format = None  # type: ignore[attr-defined]
        console.print("[dim]schema cleared[/dim]")
        return "continue"
    if args.startswith("{"):
        spec, prompt = args, ""
    else:
        spec, _, prompt = args.partition(" ")
    try:
        rf = as_response_format(load_schema(spec))
    except SchemaError as exc:
        console.print(f"[red]{exc}[/red]")
        return "continue"
    agent._pending_response_format = rf  # type: ignore[attr-defined]
    if prompt.strip():
        return prompt.strip()
    console.print("[dim]schema armed for your next message[/dim]")
    return "continue"


def _handle_notools(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """`/notools <prompt>` — answer the next turn without any tools."""
    prompt = args.strip()
    if not prompt:
        console.print("[yellow]usage:[/yellow] /notools <prompt>")
        return "continue"
    agent._pending_tool_choice = "none"  # type: ignore[attr-defined]
    return prompt


def _handle_forcetool(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """`/forcetool <name> <prompt>` — force the model to call a specific tool."""
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        console.print("[yellow]usage:[/yellow] /forcetool <tool-name> <prompt>")
        return "continue"
    tool_name, prompt = parts
    if tool_name not in agent.tools:
        console.print(f"[red]no such tool:[/red] {tool_name}")
        return "continue"
    agent._pending_tool_choice = {  # type: ignore[attr-defined]
        "type": "function",
        "function": {"name": tool_name},
    }
    return prompt


def _handle_speak(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """`/speak on|off` — toggle sentence-by-sentence TTS for assistant replies."""
    arg = args.strip().lower()
    if arg in ("on", "yes", "1"):
        from evi.voice import detect_backend

        if detect_backend() == "none":
            console.print(
                "[red]no TTS backend found[/red] — install espeak-ng on Linux, "
                "use a Mac (`say`) or Windows (PowerShell SAPI)."
            )
            return "continue"
        agent._auto_speak = True  # type: ignore[attr-defined]
        console.print("[green]auto-speak ON[/green]")
    elif arg in ("off", "no", "0"):
        agent._auto_speak = False  # type: ignore[attr-defined]
        console.print("[yellow]auto-speak OFF[/yellow]")
    else:
        state = "ON" if getattr(agent, "_auto_speak", False) else "OFF"
        console.print(f"[bold]auto-speak:[/bold] {state}")
    return "continue"


def _handle_audioraw(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """`/audioraw <path> [prompt]` — attach a raw audio clip to the next turn.

    Omni models (Qwen2.5-Omni, MiniCPM-o) receive the audio directly as an
    `input_audio` part; other models fall back to local Whisper transcription
    (same as `/audio`, but here it's automatic inside the agent)."""
    parts = args.split(None, 1)
    if not parts or not parts[0].strip():
        console.print("[yellow]usage:[/yellow] /audioraw <path> [prompt text]")
        return "continue"
    audio_path = parts[0]
    prompt_text = parts[1] if len(parts) > 1 else "Describe this audio."
    p = Path(audio_path).expanduser()
    if not p.is_file():
        console.print(f"[red]no such file:[/red] {p}")
        return "continue"
    from evi.audio_input import model_supports_audio

    if not model_supports_audio(agent.config.llm.model):
        console.print(
            f"[dim]model {agent.config.llm.model!r} isn't omni-capable — "
            "will transcribe via Whisper and send the text.[/dim]"
        )
    if not getattr(agent, "_pending_audio", None):
        agent._pending_audio = []  # type: ignore[attr-defined]
    agent._pending_audio.append(str(p))  # type: ignore[attr-defined]
    console.print(f"[green]attached audio:[/green] {p}")
    return prompt_text


def _handle_predict(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """`/predict <text>` — set a speculative-decoding hint for the NEXT turn.

    `/predict file <path>` reads the file as the prediction (common for
    edit workflows). `/predict clear` drops a pending prediction.

    Supporting backends (OpenAI, vLLM, some llama.cpp builds) verify the
    prediction token-by-token, which is 3-5× faster than regenerating from
    scratch when the prediction is mostly right.
    """
    arg = args.strip()
    if not arg:
        cur = getattr(agent, "_pending_prediction", None)
        if cur is None:
            console.print("[dim]no pending prediction[/dim]")
        else:
            preview = cur[:120] + ("…" if len(cur) > 120 else "")
            console.print(
                f"[bold]pending prediction ({len(cur)} chars):[/bold] {preview}"
            )
        return "continue"
    if arg.lower() == "clear":
        agent._pending_prediction = None  # type: ignore[attr-defined]
        console.print("[dim]prediction cleared[/dim]")
        return "continue"
    if arg.lower().startswith("file "):
        path_str = arg[5:].strip()
        p = Path(path_str).expanduser()
        if not p.is_file():
            console.print(f"[red]no such file:[/red] {p}")
            return "continue"
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            console.print(f"[red]not utf-8 text:[/red] {p}")
            return "continue"
        agent._pending_prediction = content  # type: ignore[attr-defined]
        console.print(
            f"[green]prediction set:[/green] {p} ({len(content)} chars)"
        )
        return "continue"
    agent._pending_prediction = arg  # type: ignore[attr-defined]
    console.print(f"[green]prediction set:[/green] {len(arg)} chars")
    return "continue"


def _handle_audio(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """`/audio <path>` — transcribe an audio file and use the text as the
    next turn's user message. Requires `evi[stt]`."""
    arg = args.strip()
    if not arg:
        console.print("[yellow]usage:[/yellow] /audio <path>")
        return "continue"
    p = Path(arg).expanduser()
    if not p.is_file():
        console.print(f"[red]no such file:[/red] {p}")
        return "continue"
    try:
        from evi.voice import VoiceError, transcribe_wav

        text = transcribe_wav(p)
    except VoiceError as exc:
        console.print(f"[red]{exc}[/red]")
        return "continue"
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]transcription failed:[/red] {exc}")
        return "continue"
    if not text:
        console.print("[yellow](no speech detected)[/yellow]")
        return "continue"
    console.print(f"[dim]transcribed:[/dim] {text}")
    return text


def _handle_reload(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """Re-read config.toml + refresh memory/skill index without restarting."""
    agent.refresh_config()
    console.print(
        "[green]config reloaded[/green] · "
        f"model={agent.config.llm.model} · "
        f"effort={agent.config.llm.reasoning_effort}"
    )
    return "continue"


def _handle_image(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """`/image <path> [your prompt]` — attach an image to the next turn."""
    from pathlib import Path as _Path
    from evi.vision import model_supports_vision as _mv

    if not args.strip():
        console.print("[yellow]usage:[/yellow] /image <path> [prompt text]")
        return "continue"

    parts = args.split(None, 1)
    image_path = parts[0]
    prompt_text = parts[1] if len(parts) > 1 else "Describe this image."
    p = _Path(image_path).expanduser()
    if not p.is_file():
        console.print(f"[red]no such file:[/red] {p}")
        return "continue"
    if not _mv(agent.config.llm.model):
        console.print(
            f"[yellow]model {agent.config.llm.model!r} doesn't look vision-capable[/yellow]"
        )
        console.print(
            "[dim]sending paths as text; switch to a vision model with `/model` "
            "(e.g. qwen2.5-vl, llava, llama-3.2-vision)[/dim]"
        )
    # Stash on the agent — _run_repl will forward to chat(images=[...]).
    if not hasattr(agent, "_pending_images") or not agent._pending_images:
        agent._pending_images = []  # type: ignore[attr-defined]
    agent._pending_images.append(str(p))  # type: ignore[attr-defined]
    console.print(f"[green]attached:[/green] {p}")
    return prompt_text


def _handle_auto(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    arg = args.strip().lower()
    if arg in ("on", "yes", "all"):
        agent.enable_auto_all()
        console.print("[green]auto mode ON[/green] — all tool calls auto-approved.")
    elif arg in ("off", "no"):
        agent.disable_auto_all()
        console.print("[yellow]auto mode OFF[/yellow] — config defaults apply.")
    else:
        status = "ON (approve everything)" if agent.auto_all else "OFF"
        cats = sorted(agent.auto_approve_categories) or ["(none)"]
        console.print(f"[bold]auto-all:[/bold] {status}")
        console.print(f"[bold]always-allowed categories:[/bold] {', '.join(cats)}")
    return "continue"


def _handle_context(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """Show where the context window is being spent (Phase 88)."""
    from evi.context_report import BUCKETS, context_breakdown

    rep = context_breakdown(agent.history, agent.config.llm.context_size or 0)
    used, ceiling, pct = rep["used"], rep["ceiling"], rep["pct"]
    color = "red" if pct >= 85 else "yellow" if pct >= 70 else "green"
    head = f"[bold]Context[/bold] — {rep['messages']} messages, ~{used:,} tokens"
    if ceiling:
        head += f" of {ceiling:,} ([{color}]{pct}%[/{color}])"
    else:
        head += " [dim](no llm.context_size set)[/dim]"
    console.print(head)

    labels = {"system": "system prompt", "user": "you",
              "assistant": "assistant", "tools": "tools"}
    width = 24
    for b in BUCKETS:
        toks = rep["buckets"][b]
        share = rep["pct_of_used"][b]
        fill = (share * width) // 100
        bar = "#" * fill + "-" * (width - fill)
        console.print(
            f"  [cyan]{labels[b]:<14}[/cyan] [dim]{bar}[/dim] "
            f"{toks:,} [dim]({share}%)[/dim]"
        )
    return "continue"


def _handle_recent(agent: Agent, args: str, cmd_store: CommandStore) -> SlashResult:
    """List recent sessions (read-only). Resume one with /exit then
    `evi sessions resume <id>` (or open evi://session/<id>)."""
    from evi.sessions import fmt_when, list_sessions

    try:
        n = int(args.strip()) if args.strip() else 8
    except ValueError:
        n = 8
    items = list_sessions(days=30, limit=max(1, n))
    if not items:
        console.print("[dim]no past sessions (is tools.transcripts on?)[/dim]")
        return "continue"
    console.print("[bold]Recent sessions:[/bold]")
    for s in items:
        when = fmt_when(s.ended_at or s.started_at)
        console.print(
            f"  [cyan]{s.session_id[:8]}[/cyan] [dim]{when} · "
            f"{s.message_count} msgs[/dim] {s.first_user_message}"
        )
    console.print(
        "[dim]resume: [/dim][cyan]evi sessions resume <id>[/cyan][dim] "
        "(after /exit), or [/dim][cyan]evi link <id>[/cyan]"
    )
    return "continue"


_BUILTINS: dict[str, callable] = {
    "help": _handle_help,
    "context": _handle_context,
    "ctx": _handle_context,
    "recent": _handle_recent,
    "?": _handle_help,
    "reset": _handle_reset,
    "exit": _handle_exit,
    "quit": _handle_exit,
    "tools": _handle_tools,
    "model": _handle_model,
    "goal": _handle_goal,
    "plan": _handle_plan,
    "auto": _handle_auto,
    "compact": _handle_compact,
    "image": _handle_image,
    "img": _handle_image,
    "effort": _handle_effort,
    "fast": _handle_fast,
    "json": _handle_json,
    "schema": _handle_schema,
    "notools": _handle_notools,
    "forcetool": _handle_forcetool,
    "reload": _handle_reload,
    "audio": _handle_audio,
    "audioraw": _handle_audioraw,
    "speak": _handle_speak,
    "predict": _handle_predict,
}


def _dispatch_slash(
    raw: str, agent: Agent, cmd_store: CommandStore
) -> SlashResult:
    """Parse a `/...` input. Returns a continue/exit sentinel or the
    expanded prompt to send to the LLM (for user-defined commands)."""
    body = raw[1:].strip()
    if not body:
        console.print("[red]empty command[/red] — try /help")
        return "continue"
    name, _, args = body.partition(" ")
    handler = _BUILTINS.get(name.lower())
    if handler is not None:
        return handler(agent, args, cmd_store)
    expanded = cmd_store.expand(name, args)
    if expanded is None:
        console.print(f"[red]unknown command:[/red] /{name} [dim](try /help)[/dim]")
        return "continue"
    return expanded


def _run_repl(agent: Agent) -> None:
    """Drive the chat REPL against an existing Agent. Shared by `chat` and
    `sessions resume` so resumed sessions get the same UX."""
    cmd_store = CommandStore()
    from evi.repl_input import ReplInput

    repl_in = ReplInput(agent)

    header_bits = [
        ("eVi ", "bold cyan"),
        (f"· model={agent.config.llm.model} ", "dim"),
        (f"· {len(agent.tools)} tools ", "dim"),
    ]
    if agent.project is not None:
        header_bits.append((f"· project={agent.project.path.name} ", "green"))
    console.print(Panel.fit(Text.assemble(*header_bits), border_style="cyan"))
    console.print(
        "[dim]/help for commands · /exit to quit · Tab to complete commands[/dim]\n"
    )

    while True:
        # Optional customizable status line (off by default).
        from evi.statusline import status_line

        line = status_line(agent, agent.config)
        if line:
            console.print(line, style="dim", markup=False, highlight=False)
        bits: list[str] = ["[bold green]you"]
        # Context usage chip — only when we have a known ceiling.
        used, ceiling = agent.token_usage()
        if ceiling > 0:
            pct = (used * 100) // ceiling
            color = "yellow" if pct >= 70 else "dim"
            if pct >= 85:
                color = "red"
            bits.append(
                f" [{color}]({used // 1000}k/{ceiling // 1000}k)[/{color}]"
            )
        if agent.goal:
            shortened = agent.goal if len(agent.goal) <= 40 else agent.goal[:37] + "…"
            bits.append(f" [yellow](goal: {shortened})[/yellow]")
        if agent.plan_mode_once:
            bits.append(" [magenta][plan][/magenta]")
        effort = (agent.config.llm.reasoning_effort or "medium").lower()
        if effort != "medium":
            bits.append(f" [cyan][{effort}][/cyan]")
        if agent.config.llm.fast_mode:
            bits.append(" [bright_blue][fast][/bright_blue]")
        bits.append("[/bold green] › ")
        prompt = "".join(bits)
        try:
            user_msg = repl_in.read(prompt)
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye.[/dim]")
            return
        if not user_msg.strip():
            continue
        if user_msg.strip().lower() in {"exit", "quit"}:
            return

        if user_msg.strip().startswith("/"):
            result = _dispatch_slash(user_msg.strip(), agent, cmd_store)
            if result == "exit":
                return
            if result == "continue":
                continue
            user_msg = result

        console.print("[bold magenta]evi ›[/bold magenta] ", end="")
        text_acc: list[str] = []
        speaker = None
        if getattr(agent, "_auto_speak", False):
            from evi.voice import AutoSpeaker
            speaker = AutoSpeaker()
        # Pick up any one-shot overrides stashed by slash commands.
        pending_images = getattr(agent, "_pending_images", None) or []
        if pending_images:
            agent._pending_images = []  # type: ignore[attr-defined]
        pending_response_format = getattr(agent, "_pending_response_format", None)
        if pending_response_format is not None:
            agent._pending_response_format = None  # type: ignore[attr-defined]
        pending_tool_choice = getattr(agent, "_pending_tool_choice", None)
        if pending_tool_choice is not None:
            agent._pending_tool_choice = None  # type: ignore[attr-defined]
        pending_prediction = getattr(agent, "_pending_prediction", None)
        if pending_prediction is not None:
            agent._pending_prediction = None  # type: ignore[attr-defined]
        pending_audio = getattr(agent, "_pending_audio", None) or []
        if pending_audio:
            agent._pending_audio = []  # type: ignore[attr-defined]
        in_thinking = False
        for event in agent.chat(
            user_msg,
            images=pending_images or None,
            response_format=pending_response_format,
            tool_choice=pending_tool_choice,
            prediction=pending_prediction,
            audio=pending_audio or None,
        ):
            if isinstance(event, ThinkingDelta):
                if not in_thinking:
                    console.print("[dim italic]", end="")
                    in_thinking = True
                console.print(event.text, end="", soft_wrap=True, highlight=False, style="dim italic")
                continue
            if in_thinking:
                console.print("[/dim italic]", end="")
                in_thinking = False
            if isinstance(event, TextDelta):
                text_acc.append(event.text)
                console.print(event.text, end="", soft_wrap=True, highlight=False)
                if speaker is not None:
                    speaker.feed(event.text)
            elif isinstance(event, ToolCall):
                console.print()
                console.print(f"[yellow]→ tool[/yellow] [bold]{event.name}[/bold] {event.arguments}")
            elif isinstance(event, ToolProgress):
                console.print(
                    f"[dim]… {', '.join(event.names)} running ({event.elapsed:.0f}s)[/dim]"
                )
            elif isinstance(event, ToolResult):
                preview = event.output if len(event.output) < 400 else event.output[:400] + "…"
                console.print(f"[yellow]← result[/yellow] {preview}")
                console.print("[bold magenta]evi ›[/bold magenta] ", end="")
                text_acc = []
            elif isinstance(event, UsageStats):
                console.print(
                    f"\n[dim]tokens: prompt={event.prompt_tokens} · "
                    f"completion={event.completion_tokens} · "
                    f"total={event.total_tokens}[/dim]"
                )
            elif isinstance(event, Guardrail):
                style = "red" if event.blocked else "yellow"
                console.print(f"\n[{style}]⚠ guardrail:[/{style}] {event.message}")
            elif isinstance(event, LogProbs):
                import math

                # exp(avg logprob) ≈ average per-token probability.
                conf = math.exp(event.avg_logprob) * 100
                color = "green" if conf >= 70 else ("yellow" if conf >= 40 else "red")
                console.print(
                    f"[dim]confidence: [{color}]{conf:.0f}%[/{color}] avg · "
                    f"{event.low_count} low-confidence token(s) "
                    f"(< {event.low_threshold})[/dim]"
                )
            elif isinstance(event, Error):
                console.print(f"\n[red]error:[/red] {event.message}")
            elif isinstance(event, Done):
                if speaker is not None:
                    speaker.flush()
                if text_acc:
                    console.print()
                    md = Markdown("".join(text_acc))
                    console.print(md)
                else:
                    console.print()
                break
        console.print()


@app.command()
def chat() -> None:
    """Start an interactive chat session with the local model."""
    _run_repl(_build_agent())


@app.command()
def doctor(
    strict: bool = typer.Option(
        False, "--strict", help="Exit non-zero if any check fails.",
    ),
) -> None:
    """Diagnose the local environment: paths, config, backend, deps, binaries."""
    from rich.markup import escape

    from evi.doctor import run_checks, summarize

    checks = run_checks()
    glyph = {"ok": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "fail": "[red]✗[/red]"}
    for c in checks:
        # Escape name/detail — they can contain `[...]` (e.g. extras like
        # `evi[stt]`) that Rich would otherwise eat as markup tags.
        console.print(
            f"  {glyph.get(c.status, '?')} "
            f"[bold]{escape(c.name)}[/bold] [dim]{escape(c.detail)}[/dim]"
        )
    ok, warn, fail = summarize(checks)
    console.print(
        f"\n[green]{ok} ok[/green] · [yellow]{warn} warn[/yellow] · [red]{fail} fail[/red]"
    )
    if fail and strict:
        raise typer.Exit(1)


guardrails_app = typer.Typer(help="Inspect and test content guardrails.")
app.add_typer(guardrails_app, name="guardrails")


@guardrails_app.command("path")
def guardrails_path() -> None:
    """Print the guardrails config file path."""
    from evi.guardrails import GUARDRAILS_PATH

    console.print(str(GUARDRAILS_PATH))


@guardrails_app.command("list")
def guardrails_list() -> None:
    """List loaded guardrail rules."""
    from evi.guardrails import Guardrails

    g = Guardrails.load()
    if not g.rules:
        console.print("[dim]no guardrail rules[/dim]")
        from evi.guardrails import GUARDRAILS_PATH

        console.print(f"[dim]create:[/dim] {GUARDRAILS_PATH}")
        return
    state = "[green]enabled[/green]" if g.enabled else "[yellow]disabled[/yellow]"
    console.print(f"guardrails: {state}\n")
    for r in g.rules:
        action_color = "red" if r.action == "block" else "yellow"
        console.print(
            f"  [bold]{r.name}[/bold] "
            f"[{action_color}]{r.action}[/{action_color}] "
            f"[dim]({r.applies_to})[/dim] — /{r.pattern}/"
        )


@guardrails_app.command("test")
def guardrails_test(
    text: str,
    direction: str = typer.Option("both", help="input | output | both"),
) -> None:
    """Run a piece of text through the guardrails and show the result."""
    from evi.guardrails import Guardrails

    g = Guardrails.load()
    if not g.enabled:
        console.print("[yellow]guardrails are disabled[/yellow]")
    for d in (["input", "output"] if direction == "both" else [direction]):
        res = g.check(text, d)
        verdict = "[green]allowed[/green]" if res.allowed else "[red]BLOCKED[/red]"
        console.print(f"[bold]{d}:[/bold] {verdict}")
        if res.blocked_by:
            console.print(f"  blocked by: {', '.join(res.blocked_by)}")
        if res.redacted_by:
            console.print(f"  redacted by: {', '.join(res.redacted_by)}")
            console.print(f"  result: {res.text}")


@app.command()
def variants(
    prompt: str = typer.Argument(..., help="The prompt to generate variants for."),
    n: int = typer.Option(3, "-n", "--count", help="How many variants to request."),
    temperature: float = typer.Option(
        0.9, "--temperature", "-t", help="Higher = more diverse variants.",
    ),
) -> None:
    """Generate N independent one-shot variants for a prompt.

    Good for "give me 3 commit messages", subject lines, rewrites, etc.
    Stateless — no tools, no history. Backends that ignore OpenAI's `n`
    parameter (most local ones today) return a single variant; you'll get
    whatever the backend produced.

    Examples:
        evi variants "commit message for adding speculative decoding" -n 5
        evi variants "a punchy tagline for a local AI assistant" -t 1.1
    """
    ensure_dirs()
    config = Config.load()
    client = make_client(config.llm)
    agent = Agent(client=client, config=config, tools=[])
    try:
        results = agent.complete_variants(prompt, n=n, temperature=temperature)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]request failed:[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(1)
    if not results:
        console.print("[yellow](no variants returned)[/yellow]")
        return
    if len(results) < n:
        console.print(
            f"[dim](backend returned {len(results)} of {n} requested — "
            f"it may not support OpenAI's `n` parameter)[/dim]\n"
        )
    for i, text in enumerate(results, 1):
        console.print(f"[bold cyan]{i}.[/bold cyan] {text}\n")


@app.command()
def edit(
    file: str = typer.Argument(..., help="Path to the file to edit."),
    instruction: str = typer.Argument(..., help="What to change. e.g. 'add a docstring to every function'."),
    write: bool = typer.Option(
        False, "--write", "-w",
        help="Write the result back to the file. Default: print to stdout.",
    ),
    diff: bool = typer.Option(
        False, "--diff",
        help="Show a unified diff between original and edited content.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Don't prompt before --write.",
    ),
) -> None:
    """Edit a file with speculative decoding.

    Reads `file`, sends its full content as the `prediction` hint to the
    backend, asks the model to apply `instruction`. Supporting backends
    (OpenAI, vLLM, llama.cpp with speculation) verify the prediction
    token-by-token — usually 3-5× faster than a from-scratch regen when
    the edit is small.

    Examples:
        evi edit foo.py "add type hints to every function"
        evi edit foo.py "rename `get_user` to `fetch_user`" --diff
        evi edit foo.py "fix the off-by-one in the loop" --write --yes
    """
    from difflib import unified_diff

    p = Path(file).expanduser()
    if not p.is_file():
        console.print(f"[red]no such file:[/red] {p}")
        raise typer.Exit(1)
    try:
        original = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        console.print(f"[red]not utf-8 text:[/red] {p}")
        raise typer.Exit(1)

    user_msg = (
        f"Here is the contents of `{p.name}`. {instruction}.\n\n"
        "Output the ENTIRE modified file as plain text — no markdown "
        "fences, no commentary before or after, just the file contents "
        "as they should appear on disk.\n\n"
        f"```\n{original}\n```"
    )

    # Build a scoped agent: no tools, system prompt nudges the model to
    # output the file verbatim with the requested edit applied.
    ensure_dirs()
    config = Config.load()
    client = make_client(config.llm)
    agent = Agent(
        client=client,
        config=config,
        tools=[],
        system_prompt=(
            "You apply small, focused edits to source files. Output the "
            "entire modified file verbatim — no commentary, no markdown "
            "fences, no surrounding text. Preserve existing style, "
            "indentation, and line endings."
        ),
    )
    _AUTO_STATE["agent"] = agent

    console.print(
        Panel.fit(
            Text.assemble(
                ("eVi edit ", "bold cyan"),
                (f"· {p.name} ", "dim"),
                (f"· {len(original)} bytes prediction", "dim"),
            ),
            border_style="cyan",
        )
    )

    text_acc: list[str] = []
    for event in agent.chat(user_msg, prediction=original):
        if isinstance(event, TextDelta):
            text_acc.append(event.text)
        elif isinstance(event, Error):
            console.print(f"[red]error:[/red] {event.message}")
            raise typer.Exit(1)
        elif isinstance(event, Done):
            break

    new_content = "".join(text_acc).strip()
    # Some models still wrap in fences despite instructions; strip a single
    # leading/trailing fence pair if present.
    if new_content.startswith("```"):
        new_content = new_content.split("\n", 1)[-1]
        if new_content.endswith("```"):
            new_content = new_content[: new_content.rfind("```")].rstrip()
    new_content = new_content.rstrip() + "\n"

    if diff:
        lines = list(unified_diff(
            original.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=str(p),
            tofile=str(p) + " (edited)",
            lineterm="",
        ))
        if not lines:
            console.print("[dim](no changes)[/dim]")
        else:
            for line in lines:
                stripped = line.rstrip("\n")
                if stripped.startswith("+++") or stripped.startswith("---"):
                    console.print(f"[bold]{stripped}[/bold]")
                elif stripped.startswith("+"):
                    console.print(f"[green]{stripped}[/green]")
                elif stripped.startswith("-"):
                    console.print(f"[red]{stripped}[/red]")
                elif stripped.startswith("@@"):
                    console.print(f"[cyan]{stripped}[/cyan]")
                else:
                    console.print(stripped)
        if not write:
            return

    if write:
        if not yes:
            if not typer.confirm(f"Overwrite {p}?", default=False):
                console.print("[dim]not written[/dim]")
                return
        p.write_text(new_content, encoding="utf-8")
        console.print(f"[green]wrote[/green] {p}")
        return

    # Default: print the new content to stdout for piping.
    print(new_content, end="")


@app.command()
def review(
    range_arg: str | None = typer.Argument(
        None,
        help="Diff range, e.g. `HEAD~3..HEAD` or `main..feature`. "
             "If omitted, defaults to `git diff HEAD` (working tree vs last commit).",
    ),
    staged: bool = typer.Option(False, "--staged", help="Review `git diff --cached`."),
    branch: str | None = typer.Option(
        None, "--branch",
        help="Review `<branch>...HEAD` — the current branch's commits not on <branch>.",
    ),
    file: str | None = typer.Option(
        None, "--file", help="Review the diff for a single file (vs HEAD).",
    ),
    diff_file: str | None = typer.Option(
        None, "--diff-file", help="Read a saved patch from disk and review it.",
    ),
    no_tools: bool = typer.Option(
        False, "--no-tools",
        help="Run with tools disabled. Default: fs + git + index read-only tools enabled.",
    ),
    multi: bool = typer.Option(
        False, "--multi",
        help="Fan out parallel reviewers (correctness · security · performance · tests) "
             "and combine, instead of one pass.",
    ),
) -> None:
    """Git-aware code review. Streams a focused critique to your terminal.

    Examples:

        evi review                          # working tree vs HEAD
        evi review HEAD~3..HEAD             # last 3 commits
        evi review --staged                 # what's staged for commit
        evi review --branch main            # this branch's PR diff
        evi review --file evi/agent.py      # one file
        evi review --diff-file change.patch # saved patch
    """
    from dataclasses import asdict
    from evi.review import ReviewError, REVIEW_SYSTEM_PROMPT, get_diff, review_prompt

    try:
        diff = get_diff(
            range=range_arg, staged=staged, branch=branch,
            file=file, diff_file=diff_file,
        )
    except ReviewError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if not diff.strip():
        console.print("[dim](no changes to review)[/dim]")
        return

    if multi:
        from evi.review import multi_review

        cats = () if no_tools else ("fs", "git", "index")
        console.print(
            "[dim]running parallel reviewers "
            "(correctness · security · performance · tests)…[/dim]"
        )
        console.print(Markdown(multi_review(diff, tool_categories=cats)))
        return

    # Build a scoped agent: same model + memory + skills, but with a
    # review-focused system prompt and (by default) only read-only tools.
    ensure_dirs()
    config = Config.load()
    _ensure_mcp(config)
    client = make_client(config.llm)
    toggles = asdict(config.tools)
    if no_tools:
        tools = []
    else:
        # Scope to non-destructive read-only categories. `code` (run_python)
        # could be useful for reproducing a bug, but defaults off for review.
        safe = {"fs", "git", "index"}
        tools = [
            t for t in get_enabled_tools(toggles)
            if t.category in safe
        ]
    memory = MemoryStore() if toggles.get("memory") else None
    skills = SkillStore() if toggles.get("skills") else None
    project = load_project_context()
    transcripts = TranscriptStore() if toggles.get("transcripts") else None
    agent = Agent(
        client=client,
        config=config,
        tools=tools,
        system_prompt=REVIEW_SYSTEM_PROMPT,
        memory=memory,
        skills=skills,
        project=project,
        hooks=load_hooks(),
        permission_callback=_cli_permission_prompt,
        transcripts=transcripts,
    )
    _AUTO_STATE["agent"] = agent

    console.print(
        Panel.fit(
            Text.assemble(
                ("eVi review ", "bold cyan"),
                (f"· {len(tools)} tools ", "dim"),
                (f"· {len(diff)} byte diff", "dim"),
            ),
            border_style="cyan",
        )
    )

    prompt = review_prompt(diff)
    text_acc: list[str] = []
    in_thinking = False
    for event in agent.chat(prompt):
        if isinstance(event, ThinkingDelta):
            if not in_thinking:
                console.print("[dim italic]", end="")
                in_thinking = True
            console.print(
                event.text, end="", soft_wrap=True,
                highlight=False, style="dim italic",
            )
            continue
        if in_thinking:
            console.print("[/dim italic]", end="")
            in_thinking = False
        if isinstance(event, TextDelta):
            text_acc.append(event.text)
            console.print(event.text, end="", soft_wrap=True, highlight=False)
        elif isinstance(event, ToolCall):
            console.print()
            console.print(
                f"[yellow]→ tool[/yellow] [bold]{event.name}[/bold] {event.arguments}"
            )
        elif isinstance(event, ToolResult):
            preview = (
                event.output if len(event.output) < 400
                else event.output[:400] + "…"
            )
            console.print(f"[yellow]← result[/yellow] {preview}")
        elif isinstance(event, UsageStats):
            console.print(
                f"\n[dim]tokens: prompt={event.prompt_tokens} · "
                f"completion={event.completion_tokens} · "
                f"total={event.total_tokens}[/dim]"
            )
        elif isinstance(event, Error):
            console.print(f"\n[red]error:[/red] {event.message}")
        elif isinstance(event, Done):
            if text_acc:
                console.print()
                console.print(Markdown("".join(text_acc)))
            else:
                console.print()
            break


config_app = typer.Typer(help="Config commands.")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show() -> None:
    """Print the resolved configuration."""
    cfg = Config.load()
    import json as _json

    console.print_json(_json.dumps(asdict(cfg), default=str))


@config_app.command("path")
def config_path() -> None:
    """Print the config file path."""
    console.print(str(CONFIG_PATH))


voice_app = typer.Typer(help="Voice TTS commands.")
app.add_typer(voice_app, name="voice")


@voice_app.command("speak")
def voice_speak(
    text: str,
    rate: int | None = None,
    engine: str = typer.Option(
        "", "--engine", help="Override the [voice] engine: system|coqui|f5|piper."
    ),
) -> None:
    """Speak `text` aloud via the configured TTS engine."""
    from evi.config import Config
    from evi.voice import VoiceError, detect_backend, speak

    vs = Config.load().voice
    eng = engine or vs.engine or "system"
    if eng == "system" and detect_backend() == "none":
        console.print("[red]no TTS backend found[/red] — install espeak-ng on Linux")
        raise typer.Exit(1)
    try:
        speak(
            text, rate=rate, blocking=True, engine=eng,
            model=vs.model, clone_sample=vs.clone_sample, language=vs.language,
        )
    except VoiceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


@voice_app.command("backend")
def voice_backend() -> None:
    """Show which platform TTS backend will be used (the 'system' engine)."""
    from evi.voice import detect_backend

    console.print(detect_backend())


@voice_app.command("engines")
def voice_engines() -> None:
    """List the TTS engines and whether each is installed."""
    from evi.config import Config
    from evi.voice import available_engines

    active = Config.load().voice.engine or "system"
    for name, ok in available_engines().items():
        mark = "[green]installed[/green]" if ok else "[dim]not installed[/dim]"
        star = " [cyan](active)[/cyan]" if name == active else ""
        console.print(f"  [bold]{name:<7}[/bold] {mark}{star}")
    console.print(
        "[dim]set [/dim][cyan]engine[/cyan][dim] under the voice section of "
        "config.toml (coqui/f5 also take a clone_sample WAV).[/dim]"
    )


@voice_app.command("listen")
def voice_listen(
    duration: float = typer.Option(5.0, help="Seconds to record."),
    model: str = typer.Option("tiny.en", help="Whisper model (tiny.en/base.en/small.en/medium.en/large-v3)."),
    device: str = typer.Option("cpu", help="cpu or cuda."),
) -> None:
    """Record from the mic and print the transcription."""
    from evi.voice import VoiceError, listen

    console.print(f"[dim]listening for {duration}s…[/dim]")
    try:
        text = listen(duration=duration, model=model, device=device)
    except VoiceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if not text:
        console.print("[yellow](no speech detected)[/yellow]")
        return
    console.print(text)


@voice_app.command("transcribe")
def voice_transcribe(
    path: str,
    model: str = typer.Option("tiny.en"),
    device: str = typer.Option("cpu"),
) -> None:
    """Transcribe an existing audio file."""
    from evi.voice import VoiceError, transcribe_wav

    try:
        console.print(transcribe_wav(path, model=model, device=device))
    except VoiceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


@voice_app.command("loop")
def voice_loop(
    wake: str = typer.Option(
        "",
        "--wake",
        help="Optional wake phrase. Utterances must contain it (case-insensitive). "
             "Empty = respond to everything.",
    ),
    model: str = typer.Option("tiny.en", help="Whisper model id."),
    device: str = typer.Option("cpu", help="cpu or cuda for the STT model."),
    no_speak: bool = typer.Option(
        False, "--no-speak",
        help="Skip TTS — print responses only. Useful for testing the loop.",
    ),
    rms_threshold: float = typer.Option(
        0.015,
        help="Volume threshold for voice detection. Raise if you're in a noisy room.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Print VAD diagnostics."),
) -> None:
    """Always-on voice assistant. Listens, transcribes, chats, and speaks.

    Press Ctrl-C to exit.

    Example:
        evi voice loop --wake "evi"
    """
    import threading as _threading

    from evi.voice import AutoListener, AutoSpeaker, VoiceError, detect_backend

    if not no_speak and detect_backend() == "none":
        console.print(
            "[red]no TTS backend found[/red] — pass --no-speak, or install "
            "espeak-ng on Linux."
        )
        raise typer.Exit(1)

    agent = _build_agent()
    if not no_speak:
        agent._auto_speak = True  # type: ignore[attr-defined]

    wake_phrase = wake.strip() or None
    header = (
        f"[bold cyan]eVi voice loop[/bold cyan] · model={agent.config.llm.model}"
        f" · stt={model}"
    )
    if wake_phrase:
        header += f" · wake=[yellow]{wake_phrase!r}[/yellow]"
    else:
        header += " · wake=[dim](none — always-on)[/dim]"
    console.print(Panel.fit(header, border_style="cyan"))
    console.print("[dim]listening… Ctrl-C to stop[/dim]\n")

    # Lock around the chat-handler body so a second utterance arriving
    # mid-stream queues up instead of trampling the in-flight call. (In
    # practice AutoListener is single-threaded so this just guards against
    # subclasses / tests, but it's cheap.)
    busy = _threading.Lock()
    listener_ref: dict[str, AutoListener] = {}  # forward ref so callback can pause

    def _on_utterance(text: str) -> None:
        if not text.strip():
            return
        with busy:
            console.print(f"[bold green]you ›[/bold green] {text}")
            listener = listener_ref.get("l")
            if listener is not None:
                listener.pause()
            speaker = AutoSpeaker() if not no_speak else None
            try:
                console.print("[bold magenta]evi ›[/bold magenta] ", end="")
                text_acc: list[str] = []
                in_thinking = False
                for event in agent.chat(text):
                    if isinstance(event, ThinkingDelta):
                        if not in_thinking:
                            console.print("[dim italic]", end="")
                            in_thinking = True
                        console.print(
                            event.text, end="", soft_wrap=True,
                            highlight=False, style="dim italic",
                        )
                        continue
                    if in_thinking:
                        console.print("[/dim italic]", end="")
                        in_thinking = False
                    if isinstance(event, TextDelta):
                        text_acc.append(event.text)
                        console.print(
                            event.text, end="", soft_wrap=True, highlight=False,
                        )
                        if speaker is not None:
                            speaker.feed(event.text)
                    elif isinstance(event, ToolCall):
                        console.print()
                        console.print(
                            f"[yellow]→ tool[/yellow] [bold]{event.name}[/bold] "
                            f"{event.arguments}"
                        )
                    elif isinstance(event, ToolResult):
                        preview = (
                            event.output if len(event.output) < 400
                            else event.output[:400] + "…"
                        )
                        console.print(f"[yellow]← result[/yellow] {preview}")
                        console.print("[bold magenta]evi ›[/bold magenta] ", end="")
                        text_acc = []
                    elif isinstance(event, Error):
                        console.print(f"\n[red]error:[/red] {event.message}")
                    elif isinstance(event, Done):
                        if speaker is not None:
                            speaker.flush()
                        if text_acc:
                            console.print()
                            md = Markdown("".join(text_acc))
                            console.print(md)
                        else:
                            console.print()
                        break
                console.print()
            finally:
                if speaker is not None:
                    # Wait for any queued sentences to finish before unpausing
                    # the mic so we don't capture our own voice.
                    import time as _time
                    while not speaker._q.empty():  # type: ignore[attr-defined]
                        _time.sleep(0.05)
                    speaker.close()
                if listener is not None:
                    listener.resume()
            console.print("[dim]listening…[/dim]")

    try:
        listener = AutoListener(
            on_utterance=_on_utterance,
            wake_phrase=wake_phrase,
            model=model,
            device=device,
            rms_threshold=rms_threshold,
            debug=debug,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]listener init failed:[/red] {exc}")
        raise typer.Exit(1)

    listener_ref["l"] = listener
    try:
        listener.start()
    except VoiceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    try:
        # Block the main thread until Ctrl-C; the listener runs on its own.
        import threading
        threading.Event().wait()
    except KeyboardInterrupt:
        console.print("\n[dim]stopping…[/dim]")
    finally:
        listener.stop()


update_app = typer.Typer(
    help="Self-update + rollback. Bare `evi update` checks PyPI + prompts to upgrade.",
    invoke_without_command=True,
)
app.add_typer(update_app, name="update")


def _print_install_kind_refusal(kind, allow_force: bool = True) -> None:
    """Render the per-install-kind refusal hint."""
    console.print(f"[yellow]refusing to upgrade:[/yellow] {kind.hint}")
    if allow_force and kind.kind == "locked":
        console.print("[dim]Pass --force to override.[/dim]")


@update_app.callback(invoke_without_command=True)
def update_default(
    ctx: typer.Context,
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Non-interactive — accept the upgrade prompt automatically.",
    ),
    to: str | None = typer.Option(
        None, "--to",
        help="Pin an explicit version (e.g. 0.10.0). Downgrade allowed.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Bypass the locked-env refusal. Editable + pipx still refuse.",
    ),
) -> None:
    """Check PyPI and (optionally) upgrade."""
    if ctx.invoked_subcommand is not None:
        return  # subcommand handles its own flow

    from evi import __version__
    from evi.update import (
        DIST_NAME,
        UpdateError,
        apply_upgrade,
        check_pypi,
        create_snapshot,
        detect_install_kind,
        gc_snapshots,
        verify_install,
    )

    # 1) PyPI check (skipped when --to is set; user knows what they want).
    if to is None:
        try:
            info = check_pypi()
        except UpdateError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        console.print(
            f"[dim]current:[/dim] {info.current}  "
            f"[dim]latest:[/dim] {info.latest}"
        )
        if not info.behind:
            console.print("[green]you're up to date[/green]")
            raise typer.Exit(0)
        target_version = info.latest
        spec = f"{DIST_NAME}=={target_version}"
    else:
        target_version = to.strip()
        spec = f"{DIST_NAME}=={target_version}" if target_version else DIST_NAME

    # 2) Install-kind gate.
    kind = detect_install_kind()
    if kind.kind == "editable":
        _print_install_kind_refusal(kind, allow_force=False)
        raise typer.Exit(1)
    if kind.kind == "pipx":
        _print_install_kind_refusal(kind, allow_force=False)
        raise typer.Exit(1)
    if kind.kind == "locked" and not force:
        _print_install_kind_refusal(kind, allow_force=True)
        raise typer.Exit(1)

    # 3) Confirm.
    if not yes:
        if not typer.confirm(
            f"Upgrade evi {__version__} → {target_version}?", default=True
        ):
            console.print("[dim]cancelled[/dim]")
            raise typer.Exit(0)

    # 4) Snapshot.
    try:
        snap = create_snapshot(__version__, target_version)
    except UpdateError as exc:
        console.print(f"[red]snapshot failed:[/red] {exc}")
        raise typer.Exit(1)
    console.print(f"[dim]snapshot:[/dim] {snap.dir}")

    # 5) Upgrade.
    console.print(f"[cyan]running:[/cyan] pip install --upgrade {spec}")
    result = apply_upgrade(spec)
    if not result.ok:
        console.print(f"[red]pip failed[/red] (exit {result.returncode})")
        if result.stderr:
            console.print(f"[dim]{result.stderr.strip()}[/dim]")
        console.print(
            f"\n[yellow]snapshot kept:[/yellow] {snap.dir}\n"
            f"[dim]rollback with:[/dim] [cyan]evi update rollback[/cyan]"
        )
        raise typer.Exit(1)

    # 6) Verify.
    verify = verify_install()
    if not verify.ok:
        console.print(
            f"[red]post-upgrade verify failed:[/red] {verify.err}\n"
            f"[yellow]rollback with:[/yellow] [cyan]evi update rollback[/cyan]"
        )
        raise typer.Exit(1)
    console.print(
        f"[green]ok[/green] · installed {verify.version}"
    )

    # 7) GC.
    deleted = gc_snapshots()
    if deleted:
        console.print(f"[dim]pruned {len(deleted)} old snapshot(s)[/dim]")


@update_app.command("check")
def update_check() -> None:
    """Just probe PyPI — don't change anything."""
    from evi.update import UpdateError, check_pypi

    try:
        info = check_pypi()
    except UpdateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if info.behind:
        console.print(
            f"[yellow]update available[/yellow] · "
            f"{info.current} → {info.latest}"
        )
    else:
        console.print(f"[green]up to date[/green] · {info.current}")
    if info.summary:
        console.print(f"[dim]{info.summary}[/dim]")
    if info.release_url:
        console.print(f"[dim]{info.release_url}[/dim]")


@update_app.command("history")
def update_history() -> None:
    """List on-disk snapshots, newest first."""
    from evi.update import list_snapshots

    snaps = list_snapshots()
    if not snaps:
        console.print("[dim]no snapshots[/dim]")
        return
    for i, s in enumerate(snaps, 1):
        console.print(
            f"  [{i}] [bold]{s.from_version}[/bold] → "
            f"[bold]{s.to_version}[/bold]  "
            f"[dim]{s.timestamp:%Y-%m-%d %H:%M:%S UTC}[/dim]  "
            f"[dim]{s.dir}[/dim]"
        )


@update_app.command("rollback")
def update_rollback(
    selector: str = typer.Argument(
        "1",
        help="Snapshot to restore: 1=newest, 2=second-newest, or exact dir name. Default newest.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't prompt."),
) -> None:
    """Restore a previous pip freeze snapshot."""
    from evi.update import (
        UpdateError, apply_rollback, resolve_snapshot, verify_install,
    )

    try:
        snap = resolve_snapshot(selector)
    except UpdateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[bold]restore[/bold] {snap.from_version} → {snap.to_version} "
        f"[dim]({snap.timestamp:%Y-%m-%d %H:%M:%S UTC})[/dim]"
    )
    if not yes:
        if not typer.confirm("Run pip install -r requirements.txt from this snapshot?", default=True):
            console.print("[dim]cancelled[/dim]")
            raise typer.Exit(0)

    result = apply_rollback(snap)
    if not result.ok:
        console.print(f"[red]rollback failed[/red] (exit {result.returncode})")
        if result.stderr:
            console.print(f"[dim]{result.stderr.strip()}[/dim]")
        raise typer.Exit(1)

    verify = verify_install()
    if not verify.ok:
        console.print(
            f"[red]post-rollback verify failed:[/red] {verify.err}"
        )
        raise typer.Exit(1)
    console.print(f"[green]rolled back[/green] · installed {verify.version}")


@update_app.command("prune")
def update_prune(
    keep: int = typer.Option(5, "--keep", help="How many newest snapshots to retain."),
) -> None:
    """Drop snapshots older than the most-recent N."""
    from evi.update import gc_snapshots

    deleted = gc_snapshots(keep=keep)
    if not deleted:
        console.print("[dim]nothing to prune[/dim]")
        return
    for s in deleted:
        console.print(f"[yellow]removed[/yellow] {s.dir.name}")


@update_app.command("from-wheel")
def update_from_wheel(
    path: str = typer.Argument(..., help="Path to a local wheel (.whl) or sdist (.tar.gz)."),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Offline install from a local artifact. Snapshots + verifies same as `update`."""
    from pathlib import Path as _Path

    from evi import __version__
    from evi.update import (
        UpdateError, apply_upgrade, create_snapshot, verify_install,
    )

    p = _Path(path).expanduser().resolve()
    if not p.is_file():
        console.print(f"[red]no such file:[/red] {p}")
        raise typer.Exit(1)
    if not yes:
        if not typer.confirm(f"Install from {p.name}?", default=True):
            raise typer.Exit(0)

    try:
        snap = create_snapshot(__version__, f"wheel:{p.name}")
    except UpdateError as exc:
        console.print(f"[red]snapshot failed:[/red] {exc}")
        raise typer.Exit(1)
    console.print(f"[dim]snapshot:[/dim] {snap.dir}")

    result = apply_upgrade(str(p))
    if not result.ok:
        console.print(f"[red]pip failed[/red] (exit {result.returncode})")
        if result.stderr:
            console.print(f"[dim]{result.stderr.strip()}[/dim]")
        raise typer.Exit(1)

    verify = verify_install()
    if not verify.ok:
        console.print(f"[red]verify failed:[/red] {verify.err}")
        raise typer.Exit(1)
    console.print(f"[green]ok[/green] · installed {verify.version}")


@update_app.command("settings")
def update_settings() -> None:
    """Print install kind + snapshot dir + retention."""
    from evi import __version__
    from evi.update import (
        DEFAULT_KEEP, SNAPSHOTS_DIR, detect_install_kind, list_snapshots,
    )

    console.print(f"[bold]evi {__version__}[/bold]")
    kind = detect_install_kind()
    console.print(f"  install kind: [cyan]{kind.kind}[/cyan]")
    console.print(f"  location:     {kind.location}")
    if kind.hint:
        console.print(f"  note:         [dim]{kind.hint}[/dim]")
    console.print(f"  snapshots:    {SNAPSHOTS_DIR}")
    console.print(f"  retention:    keep {DEFAULT_KEEP} newest")
    console.print(f"  on disk:      {len(list_snapshots())}")


calendar_app = typer.Typer(help="Calendar — iCal URL + CalDAV sources.")
app.add_typer(calendar_app, name="calendar")


@calendar_app.command("list")
def calendar_list_sources() -> None:
    """List configured calendar sources."""
    from evi.calendar import CalendarStore

    sources = CalendarStore().load()
    if not sources:
        console.print(
            "[dim]no calendar sources configured. Add one with:[/dim]\n"
            "  [cyan]evi calendar add personal --url <ical-url>[/cyan]\n"
            "or for CalDAV:\n"
            "  [cyan]evi calendar add work --kind caldav --url <url> "
            "--username <u> --password-env EVI_CAL_WORK_PASSWORD[/cyan]"
        )
        return
    for s in sources:
        bits = [f"[bold]{s.name}[/bold]", f"[cyan]{s.kind}[/cyan]"]
        bits.append(s.url)
        if s.username:
            bits.append(f"user={s.username}")
        if s.password_env:
            bits.append(f"pw=${s.password_env}")
        if s.calendar:
            bits.append(f"cal={s.calendar}")
        console.print("  " + " · ".join(bits))


@calendar_app.command("add")
def calendar_add(
    name: str = typer.Argument(..., help="Local name for the source (e.g. 'personal')."),
    url: str = typer.Option(..., "--url", help="iCal URL or CalDAV root URL."),
    kind: str = typer.Option("ical", "--kind", help="ical | caldav"),
    username: str = typer.Option("", "--username", help="CalDAV username."),
    password_env: str = typer.Option(
        "", "--password-env",
        help="Env var holding the CalDAV password (we never write secrets to disk).",
    ),
    calendar_name: str = typer.Option(
        "", "--calendar", help="CalDAV: restrict to this calendar name."
    ),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    """Add a calendar source."""
    from evi.calendar import CalendarStore, Source

    kind_norm = kind.strip().lower()
    if kind_norm not in ("ical", "caldav"):
        console.print(f"[red]bad --kind:[/red] {kind} (use ical or caldav)")
        raise typer.Exit(1)
    if kind_norm == "caldav" and username and not password_env:
        console.print(
            "[yellow]--username given without --password-env[/yellow] — "
            "you'll need to set --password-env=NAME and `export NAME=...` "
            "before reading."
        )
    source = Source(
        name=name,
        kind=kind_norm,
        url=url,
        username=username,
        password_env=password_env,
        calendar=calendar_name,
    )
    if not CalendarStore().add(source, overwrite=overwrite):
        console.print(f"[red]source exists:[/red] {name}. Use --overwrite.")
        raise typer.Exit(1)
    console.print(f"[green]added[/green] {name} ({kind_norm})")


@calendar_app.command("remove")
def calendar_remove(name: str) -> None:
    """Remove a calendar source."""
    from evi.calendar import CalendarStore

    if not CalendarStore().remove(name):
        console.print(f"[red]no such source:[/red] {name}")
        raise typer.Exit(1)
    console.print(f"[yellow]removed[/yellow] {name}")


@calendar_app.command("peek")
def calendar_peek(
    days: int = typer.Option(1, "--days", help="Window length (1 = today only)."),
    source: str = typer.Option("", "--source", help="Restrict to one named source."),
) -> None:
    """Print upcoming events from configured calendars."""
    from evi.calendar import (
        CalendarStore,
        days_window,
        format_events,
        read_all,
    )

    sources = CalendarStore().load()
    if source.strip():
        sources = [s for s in sources if s.name == source.strip()]
    if not sources:
        console.print("[dim]no sources configured (or --source didn't match)[/dim]")
        raise typer.Exit(1)
    start, end = days_window(days)
    events, errors = read_all(sources, start=start, end=end)
    if events:
        console.print(format_events(events))
    else:
        console.print("[dim](no events in this window)[/dim]")
    if errors:
        console.print()
        for err in errors:
            console.print(f"[red]error:[/red] {err}")


route_app = typer.Typer(help="Multi-model routing — pick the right model per turn.")
app.add_typer(route_app, name="route")


@route_app.command("list")
def route_list() -> None:
    """Show all configured routes."""
    from evi.routing import RouterStore

    cfg = Config.load()
    routes = RouterStore().load()
    state = "[green]ON[/green]" if cfg.llm.router_enabled else "[red]OFF[/red]"
    classifier = cfg.llm.router_model or "[dim](none — keyword-only)[/dim]"
    console.print(f"[bold]router:[/bold] {state} · classifier: {classifier}")
    if not routes:
        console.print(
            "[dim]no routes. Add one with:[/dim]\n"
            "  [cyan]evi route add coder --model qwen2.5-coder-14b --keywords code,debug[/cyan]\n"
            "[dim]Or install a preset:[/dim]\n"
            "  [cyan]evi route preset common[/cyan]"
        )
        return
    for r in routes:
        kws = ", ".join(r.match_keywords) or "[dim](classifier-only)[/dim]"
        desc = f" — {r.description}" if r.description else ""
        console.print(
            f"  [bold]{r.name}[/bold] → [cyan]{r.model}[/cyan]{desc}\n"
            f"    [dim]match:[/dim] {kws}"
        )


@route_app.command("add")
def route_add(
    name: str = typer.Argument(..., help="Route name (used in /forceroute later)."),
    model: str = typer.Option(..., "--model", "-m", help="Model id to send matching turns to."),
    keywords: str = typer.Option(
        "", "--keywords", "-k",
        help="Comma-separated keywords; ANY appearing in the user message fires the route.",
    ),
    description: str = typer.Option(
        "", "--description", help="Free-form description (used by the LLM classifier)."
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing route by this name."),
) -> None:
    """Add or replace a route."""
    from evi.routing import Route, RouterStore

    kws = [k.strip() for k in keywords.split(",") if k.strip()]
    route = Route(name=name, model=model, description=description, match_keywords=kws)
    if not RouterStore().add(route, overwrite=overwrite):
        console.print(
            f"[red]route exists:[/red] {name}. Pass --overwrite to replace."
        )
        raise typer.Exit(1)
    console.print(f"[green]added[/green] {name} → {model}")


@route_app.command("remove")
def route_remove(name: str) -> None:
    """Remove a route by name."""
    from evi.routing import RouterStore

    if not RouterStore().remove(name):
        console.print(f"[red]no such route:[/red] {name}")
        raise typer.Exit(1)
    console.print(f"[yellow]removed[/yellow] {name}")


@route_app.command("test")
def route_test(message: str) -> None:
    """Show which route would fire for a sample message."""
    from evi.routing import Router, RouterStore

    cfg = Config.load()
    routes = RouterStore().load()
    if not routes:
        console.print("[dim]no routes configured[/dim]")
        return
    # No classifier client in this command — keyword-only test. The
    # classifier path requires a live LLM and would slow the CLI down.
    router = Router(
        routes, default_model=cfg.llm.model, classifier_model="", client=None
    )
    decision = router.pick(message)
    if decision.route_name == "default":
        console.print(
            f"[yellow]no route matched[/yellow] → using default [bold]{decision.model}[/bold]"
        )
    else:
        console.print(
            f"[green]matched[/green] [bold]{decision.route_name}[/bold]"
            f" → [cyan]{decision.model}[/cyan] [dim]({decision.reason})[/dim]"
        )


@route_app.command("enable")
def route_enable() -> None:
    """Turn router_enabled on in config.toml."""
    cfg = Config.load()
    cfg.llm.router_enabled = True
    cfg.save()
    console.print("[green]router enabled[/green]")


@route_app.command("disable")
def route_disable() -> None:
    """Turn router_enabled off in config.toml."""
    cfg = Config.load()
    cfg.llm.router_enabled = False
    cfg.save()
    console.print("[yellow]router disabled[/yellow]")


@route_app.command("classifier")
def route_classifier(model: str | None = typer.Argument(None)) -> None:
    """Show or set the LLM classifier model (empty = keyword-only)."""
    cfg = Config.load()
    if model is None:
        current = cfg.llm.router_model or "[dim](none)[/dim]"
        console.print(f"[bold]classifier model:[/bold] {current}")
        return
    cfg.llm.router_model = model.strip()
    cfg.save()
    console.print(f"[green]classifier model →[/green] {model}")


@route_app.command("preset")
def route_preset(
    name: str = typer.Argument("common", help="Preset name (only 'common' is shipped)."),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace existing routes with matching names."
    ),
) -> None:
    """Install a named preset of routes."""
    from evi.routing import PRESET_ROUTES, RouterStore

    preset = PRESET_ROUTES.get(name)
    if preset is None:
        console.print(
            f"[red]unknown preset:[/red] {name}. "
            f"Available: {', '.join(PRESET_ROUTES)}"
        )
        raise typer.Exit(1)
    store = RouterStore()
    added = 0
    skipped: list[str] = []
    for r in preset:
        if store.add(r, overwrite=overwrite):
            added += 1
        else:
            skipped.append(r.name)
    console.print(f"[green]installed[/green] {added} routes from preset {name!r}")
    if skipped:
        console.print(
            f"[yellow]skipped (already exist):[/yellow] {', '.join(skipped)}\n"
            "[dim](use --overwrite to replace)[/dim]"
        )


sync_app = typer.Typer(help="Cross-machine sync of portable ~/.evi state (git).")
app.add_typer(sync_app, name="sync")


@sync_app.command("init")
def sync_init(
    remote: str = typer.Argument(
        "", help="Git remote URL (e.g. git@github.com:you/evi-home.git). Optional."
    ),
) -> None:
    """Set up sync in ~/.evi: init the repo + managed .gitignore (and the
    remote, if given). Syncs memory/skills/profiles/commands/routes/mcp/hooks;
    keeps config, secrets, models, and indices local."""
    from evi import sync as sync_mod

    try:
        console.print(sync_mod.init(remote=remote or None))
    except sync_mod.SyncError as exc:
        console.print(f"[red]sync init failed:[/red] {exc}")
        raise typer.Exit(1)


@sync_app.command("push")
def sync_push(
    message: str = typer.Option("", "--message", "-m", help="Commit message."),
) -> None:
    """Commit local changes to the portable state and push to the remote."""
    from evi import sync as sync_mod

    try:
        console.print(sync_mod.push(message=message or None))
    except sync_mod.SyncError as exc:
        console.print(f"[red]sync push failed:[/red] {exc}")
        raise typer.Exit(1)


@sync_app.command("pull")
def sync_pull() -> None:
    """Pull the latest portable state from the remote into ~/.evi."""
    from evi import sync as sync_mod

    try:
        console.print(sync_mod.pull())
    except sync_mod.SyncError as exc:
        console.print(f"[red]sync pull failed:[/red] {exc}")
        raise typer.Exit(1)


@sync_app.command("status")
def sync_status() -> None:
    """Show the sync remote + working-tree status."""
    from evi import sync as sync_mod

    try:
        console.print(sync_mod.status())
    except sync_mod.SyncError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


recipe_app = typer.Typer(help="Saved multi-turn workflows (recipes).")
app.add_typer(recipe_app, name="recipe")


def _print_turn(agent: Agent, prompt: str) -> None:
    """Stream one agent turn to the console (text + tool activity)."""
    in_thinking = False
    for event in agent.chat(prompt):
        if isinstance(event, ThinkingDelta):
            if not in_thinking:
                console.print("[dim italic]", end="")
                in_thinking = True
            console.print(event.text, end="", soft_wrap=True, highlight=False, style="dim italic")
            continue
        if in_thinking:
            console.print("[/dim italic]", end="")
            in_thinking = False
        if isinstance(event, TextDelta):
            console.print(event.text, end="", soft_wrap=True, highlight=False)
        elif isinstance(event, ToolCall):
            console.print(f"\n[yellow]→ tool[/yellow] [bold]{event.name}[/bold] {event.arguments}")
        elif isinstance(event, ToolProgress):
            console.print(
                f"[dim]… {', '.join(event.names)} running ({event.elapsed:.0f}s)[/dim]"
            )
        elif isinstance(event, ToolResult):
            preview = event.output if len(event.output) < 400 else event.output[:400] + "…"
            console.print(f"[yellow]← result[/yellow] {preview}")
        elif isinstance(event, Error):
            console.print(f"\n[red]error:[/red] {event.message}")
        elif isinstance(event, Done):
            console.print()
            break


@recipe_app.command("list")
def recipe_list() -> None:
    """List saved recipes."""
    from evi import recipes

    recs = recipes.list_recipes()
    if not recs:
        console.print(
            "[dim]no recipes. Create one with:[/dim] [cyan]evi recipe new <name>[/cyan]"
        )
        return
    for r in recs:
        desc = f" — {r.description}" if r.description else ""
        console.print(f"  [bold]{r.name}[/bold] [dim]({len(r.steps)} steps)[/dim]{desc}")


@recipe_app.command("show")
def recipe_show(name: str) -> None:
    """Print a recipe's steps."""
    from evi import recipes

    try:
        rec = recipes.load_recipe(name)
    except recipes.RecipeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    head = f"[bold]{rec.name}[/bold]" + (f" — {rec.description}" if rec.description else "")
    console.print(head)
    for i, step in enumerate(rec.steps, 1):
        label = f" [dim]({step.label})[/dim]" if step.label else ""
        console.print(f"  [cyan]{i}.[/cyan]{label} {step.prompt}")


@recipe_app.command("new")
def recipe_new(
    name: str,
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing recipe."),
) -> None:
    """Write a starter recipe template to ~/.evi/recipes/<name>.toml."""
    from evi import recipes

    try:
        path = recipes.create_recipe(name, overwrite=overwrite)
    except recipes.RecipeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]created[/green] {path}\n"
        f"[dim]edit it, then run:[/dim] [cyan]evi recipe run {recipes._slug(name)}[/cyan]"
    )


@recipe_app.command("run")
def recipe_run(
    name: str,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-approve every tool call (unattended run)."
    ),
) -> None:
    """Run a recipe — its steps stream through one shared conversation."""
    from evi import recipes

    try:
        rec = recipes.load_recipe(name)
    except recipes.RecipeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    agent = _build_agent()
    if yes:
        agent.enable_auto_all()
    console.print(
        Panel.fit(
            Text.assemble(
                (f"recipe {rec.name} ", "bold cyan"), (f"· {len(rec.steps)} steps", "dim")
            ),
            border_style="cyan",
        )
    )
    for i, step in enumerate(rec.steps, 1):
        header = f"[bold cyan]Step {i}/{len(rec.steps)}[/bold cyan]"
        if step.label:
            header += f" [dim]· {step.label}[/dim]"
        console.print(f"\n{header}\n[dim]> {step.prompt}[/dim]")
        _print_turn(agent, step.prompt)
    console.print("\n[green]recipe complete[/green]")


@app.command("link")
def link_cmd(
    target: str = typer.Argument(
        None, help="Session id (default: most recent), or 'new'."
    ),
    open_url: str = typer.Option(
        "", "--open", help="Parse an evi:// URL and show the in-app path it routes to."
    ),
) -> None:
    """Make an evi:// deep link (or resolve one with --open).

    The desktop app registers the evi:// scheme; opening a link focuses the app
    on that session/workflow. The same links work in a browser via the web UI's
    /?session= and /?workflow= params.
    """
    from evi import deeplinks

    if open_url:
        try:
            kind, value, _ = deeplinks.parse_link(open_url)
        except deeplinks.DeepLinkError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        console.print(
            f"[dim]{kind}[/dim] {value} -> [cyan]{deeplinks.to_web_path(open_url)}[/cyan]"
        )
        return

    if target == "new":
        console.print(deeplinks.build_link("new"))
        return
    from evi.sessions import most_recent_session_id

    sid = target or most_recent_session_id()
    if sid is None:
        console.print("[dim]no sessions — pass a session id or 'new'[/dim]")
        raise typer.Exit(1)
    console.print(deeplinks.build_link("session", sid))


eval_app = typer.Typer(help="Evals — regression-test prompts/skills/models against assertions.")
app.add_typer(eval_app, name="eval")


@eval_app.command("list")
def eval_list() -> None:
    """List eval suites (~/.evi/evals/)."""
    from evi import evals

    items = evals.list_suites()
    if not items:
        console.print("[dim]no suites.[/dim] create one with [cyan]evi eval new <name>[/cyan]")
        return
    for s in items:
        desc = f" — {s.description}" if s.description else ""
        console.print(f"  [bold]{s.name}[/bold] [dim]({len(s.cases)} cases)[/dim]{desc}")


@eval_app.command("new")
def eval_new(
    name: str,
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace if it exists."),
) -> None:
    """Write a starter eval suite."""
    from evi import evals

    try:
        path = evals.create_suite(name, overwrite=overwrite)
    except evals.EvalError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]created[/green] {path}")


@eval_app.command("run")
def eval_run(
    name: str,
    mode: str = typer.Option("", "--mode", "-m", help="Default tool preset for cases."),
    json_out: bool = typer.Option(False, "--json", help="Print the full report as JSON."),
) -> None:
    """Run an eval suite and report the pass-rate. Exit code is non-zero if any
    case fails (so it gates CI)."""
    from evi import evals
    from evi.headless import run_headless
    from evi.modes import mode_tools

    try:
        suite = evals.load_suite(name)
    except evals.EvalError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    def run_one(case) -> str:
        agent = _build_agent()
        m = case.mode or mode
        if m:
            agent.tools = {t.name: t for t in mode_tools(m)}
        agent.enable_auto_all()
        res = run_headless(agent, case.prompt)
        return res.text or (f"ERROR: {res.error}" if res.error else "")

    def judge_fn(case, output) -> tuple[bool, str]:
        agent = _build_agent()
        agent.tools = {}  # the grader answers from text alone, no tools
        prompt = (
            "Grade the ANSWER against the RUBRIC. Reply with exactly PASS or FAIL "
            "on the first line, then a one-line reason.\n\n"
            f"RUBRIC: {case.judge}\n\nANSWER:\n{output}"
        )
        res = run_headless(agent, prompt)
        text = (res.text or "").strip()
        first = text.splitlines()[0] if text else ""
        return first.strip().upper().startswith("PASS"), (first[:200] or "no judge output")

    report = evals.run_eval(suite, run_one, judge_fn=judge_fn)
    if json_out:
        import json as _json

        print(_json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for c in report["cases"]:
            mark = "[green]PASS[/green]" if c["passed"] else "[red]FAIL[/red]"
            console.print(f"  {mark} [bold]{c['name']}[/bold]")
            for f in c["failures"]:
                console.print(f"       [dim]{f}[/dim]")
        pct = round(report["pass_rate"] * 100)
        color = "green" if report["passed"] == report["total"] else "yellow"
        console.print(
            f"\n[{color}]{report['passed']}/{report['total']} passed "
            f"({pct}%)[/{color}] — [bold]{report['name']}[/bold]"
        )
    if report["passed"] < report["total"]:
        raise typer.Exit(1)


@app.command("agents")
def agents_cmd() -> None:
    """List subagent profiles (built-in + plugin) usable via the `delegate` tool."""
    from evi.llm.subagent import SUBAGENT_PROFILES, all_profiles

    for name, p in all_profiles().items():
        cats = ", ".join(p.get("tool_categories") or ()) or "no tools"  # type: ignore[arg-type]
        origin = "built-in" if name in SUBAGENT_PROFILES else "plugin"
        sp = str(p.get("system_prompt", ""))[:72]
        console.print(f"  [bold]{name}[/bold] [dim]({cats} · {origin})[/dim]")
        console.print(f"    [dim]{sp}…[/dim]")
    console.print(
        "\n[dim]use via the [/dim][cyan]delegate(profile, task)[/cyan][dim] tool, or add your "
        "own in a plugin's [/dim]agents.toml[dim].[/dim]"
    )


workflow_app = typer.Typer(
    help="Dynamic workflows — multi-step, parallel multi-agent orchestration."
)
app.add_typer(workflow_app, name="workflow")


@workflow_app.command("list")
def workflow_list() -> None:
    """List saved workflows (~/.evi/workflows/)."""
    from evi import workflows

    items = workflows.list_workflows()
    if not items:
        console.print(
            "[dim]no workflows.[/dim] create one with "
            "[cyan]evi workflow new <name>[/cyan]"
        )
        return
    for w in items:
        par = sum(1 for s in w.steps if s.parallel)
        extra = f", {par} parallel" if par else ""
        desc = f" — {w.description}" if w.description else ""
        console.print(f"  [bold]{w.name}[/bold] [dim]({len(w.steps)} steps{extra})[/dim]{desc}")


@workflow_app.command("show")
def workflow_show(name: str) -> None:
    """Show a workflow's steps and variables."""
    from evi import workflows

    try:
        w = workflows.load_workflow(name)
    except workflows.WorkflowError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    desc = f" — {w.description}" if w.description else ""
    console.print(f"[bold cyan]{w.name}[/bold cyan]{desc}")
    if w.vars:
        console.print("[dim]vars: " + ", ".join(f"{k}={v}" for k, v in w.vars.items()) + "[/dim]")
    for i, s in enumerate(w.steps, 1):
        tag = " [magenta](parallel)[/magenta]" if s.parallel else ""
        lbl = f" · {s.label}" if s.label else ""
        console.print(f"  [cyan]{i}. {s.id}[/cyan]{tag}{lbl}")
        console.print(f"     [dim]{s.prompt[:120]}[/dim]")


@workflow_app.command("new")
def workflow_new(
    name: str,
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace if it exists."),
) -> None:
    """Write a starter workflow template."""
    from evi import workflows

    try:
        path = workflows.create_workflow(name, overwrite=overwrite)
    except workflows.WorkflowError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]created[/green] {path}")


@workflow_app.command("run")
def workflow_run(
    name: str,
    var: list[str] = typer.Option(
        None, "--var", help="Override a workflow var: k=v (repeatable)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Print {step: output} as JSON."),
) -> None:
    """Run a workflow — each step is its own headless agent; parallel blocks run
    concurrently (tools auto-approved, since runs are unattended)."""
    from evi import workflows
    from evi.headless import run_headless
    from evi.modes import mode_tools

    try:
        w = workflows.load_workflow(name)
    except workflows.WorkflowError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    variables: dict[str, str] = {}
    for kv in var or []:
        if "=" not in kv:
            console.print(f"[red]bad --var {kv!r}[/red] (expected k=v)")
            raise typer.Exit(2)
        k, _, v = kv.partition("=")
        variables[k.strip()] = v

    def run_step(prompt: str, step) -> str:
        agent = _build_agent()
        if step.mode:
            agent.tools = {t.name: t for t in mode_tools(step.mode)}
        agent.enable_auto_all()  # workflows are unattended
        if not json_out:
            tag = " [magenta](parallel)[/magenta]" if step.parallel else ""
            console.print(f"\n[bold cyan]> {step.id}[/bold cyan]{tag}")
        res = run_headless(agent, prompt)
        out = res.text or (f"ERROR: {res.error}" if res.error else "")
        if not json_out:
            console.print(out)
        return out

    if not json_out:
        console.print(
            Panel.fit(
                Text.assemble(
                    (f"workflow {w.name} ", "bold cyan"), (f"· {len(w.steps)} steps", "dim")
                ),
                border_style="cyan",
            )
        )
    try:
        outputs = workflows.run_workflow(w, run_step=run_step, variables=variables)
    except workflows.WorkflowError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if json_out:
        import json as _json

        print(_json.dumps(outputs, ensure_ascii=False, indent=2))
    else:
        console.print("\n[green]workflow complete[/green]")


@app.command()
def run(
    prompt: str = typer.Argument(None, help="The prompt. If omitted, read from stdin."),
    format: str = typer.Option("text", "--format", "-f", help="Output format: text | json."),
    mode: str = typer.Option("", "--mode", "-m", help="Tool preset: chat | cowork | code."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-approve all tool calls (unattended)."
    ),
    schema: str = typer.Option(
        "", "--schema", help="Constrain output to a JSON Schema (file or inline JSON)."
    ),
) -> None:
    """Headless: run a single prompt non-interactively and print the result.

    text → the final answer on stdout. json → {text, tools, usage, error}.
    Without --yes, tools that aren't in your auto-approve list are denied (so a
    scripted run never blocks on a prompt). Reads the prompt from stdin if
    omitted: `echo "summarise README.md" | evi run -m code -y`."""
    import sys as _sys

    from evi import headless as _hl

    text = (prompt if prompt is not None else _sys.stdin.read()).strip()
    if not text:
        console.print("[red]no prompt given[/red]")
        raise typer.Exit(2)

    agent = _build_agent()
    if mode:
        from evi.modes import mode_tools

        agent.tools = {t.name: t for t in mode_tools(mode)}
    if yes:
        agent.enable_auto_all()
    else:
        # Non-interactive: deny tools not already auto-approved rather than
        # block on a permission prompt that no one can answer.
        agent.permission_callback = lambda *a, **k: False
        agent.permission_batch_callback = None

    response_format = None
    if schema:
        from evi.structured import SchemaError, as_response_format, load_schema

        try:
            response_format = as_response_format(load_schema(schema))
        except SchemaError as exc:
            print(f"error: {exc}", file=_sys.stderr)
            raise typer.Exit(2)

    res = _hl.run_headless(agent, text, response_format=response_format)
    if format == "json":
        print(_hl.to_json(res))
        return
    if res.error:
        print(f"error: {res.error}", file=_sys.stderr)
        raise typer.Exit(1)
    print(res.text)


@app.command()
def batch(
    input_file: str = typer.Argument(..., help="Prompts file (.jsonl/.json or one-per-line)."),
    out: str = typer.Option("", "--out", "-o", help="Write JSONL results here (default: stdout)."),
    parallel: int = typer.Option(1, "--parallel", "-j", help="Run N prompts concurrently."),
    mode: str = typer.Option("", "--mode", "-m", help="Default tool preset for items without one."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve all tool calls."),
) -> None:
    """Headless batch: run every prompt in a file, one JSON result per line.

    Each item gets its own agent (per-item `mode`/`schema` override the flags).
    Local analog of a Batch API — good for evals, bulk extraction, translations.
    """
    import sys as _sys

    from evi import batch as _batch
    from evi.headless import run_headless
    from evi.modes import mode_tools
    from evi.structured import as_response_format, load_schema

    try:
        items = _batch.parse_batch_file(input_file)
    except _batch.BatchError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    if not items:
        console.print("[dim]no prompts in input[/dim]")
        raise typer.Exit(1)

    def run_one(item: dict) -> dict:
        agent = _build_agent()
        m = str(item.get("mode") or mode)
        if m:
            agent.tools = {t.name: t for t in mode_tools(m)}
        if yes:
            agent.enable_auto_all()
        else:
            agent.permission_callback = lambda *a, **k: False
            agent.permission_batch_callback = None
        rf = None
        if item.get("schema"):
            rf = as_response_format(load_schema(str(item["schema"])))
        res = run_headless(agent, str(item["prompt"]), response_format=rf)
        return {
            "id": item.get("id"),
            "prompt": item["prompt"],
            "text": res.text,
            "error": res.error,
            "usage": res.usage,
        }

    results = _batch.run_batch(items, run_one, parallel=max(1, parallel))
    payload = _batch.to_jsonl(results)
    if out:
        from pathlib import Path as _Path

        _Path(out).write_text(payload + "\n", encoding="utf-8")
        ok = sum(1 for r in results if not r.get("error"))
        console.print(f"[green]wrote[/green] {len(results)} results ({ok} ok) -> [cyan]{out}[/cyan]")
    else:
        print(payload, file=_sys.stdout)


style_app = typer.Typer(help="Output styles — switchable response personas.")
app.add_typer(style_app, name="style")


@style_app.command("list")
def style_list() -> None:
    """List available output styles (built-ins + ~/.evi/styles/*.md)."""
    from evi import styles

    active = Config.load().llm.output_style
    console.print(f"[bold]active:[/bold] {active or '(default)'}")
    for n in styles.list_styles():
        mark = " [green]✓[/green]" if n == active else ""
        console.print(f"  {n}{mark}")


@style_app.command("show")
def style_show(name: str) -> None:
    """Print a style's instruction text."""
    from evi import styles

    text = styles.style_text(name)
    console.print(text or f"[red]no such style:[/red] {name}")


@style_app.command("use")
def style_use(
    name: str = typer.Argument("", help="Style name, or empty to clear (default voice)."),
) -> None:
    """Set the active output style."""
    from evi import styles

    if name and name not in styles.list_styles():
        console.print(f"[red]no such style:[/red] {name}")
        raise typer.Exit(1)
    cfg = Config.load()
    cfg.llm.output_style = name
    cfg.save()
    console.print(f"[green]output style:[/green] {name or '(default)'}")


routine_app = typer.Typer(help="Routines — trigger a recipe from a webhook.")
app.add_typer(routine_app, name="routine")


@routine_app.command("add")
def routine_add(
    name: str,
    recipe: str = typer.Option(..., "--recipe", "-r", help="The recipe this routine runs."),
    yes: bool = typer.Option(False, "--yes", help="Auto-approve all tools for this routine."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing routine."),
) -> None:
    """Create a webhook-triggered routine bound to a recipe."""
    from evi import recipes, routines

    try:
        recipes.load_recipe(recipe)
    except recipes.RecipeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    try:
        r = routines.add(name, recipe, yes=yes, overwrite=overwrite)
    except routines.RoutineError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]created routine[/green] {r.name} → recipe [cyan]{r.recipe}[/cyan]")
    console.print(
        "[dim]Trigger it against your running `evi web` server:[/dim]\n"
        f"  [cyan]curl -X POST http://localhost:8000/api/routine/{r.token}[/cyan]"
    )


@routine_app.command("list")
def routine_list() -> None:
    """List routines (with their webhook tokens)."""
    from evi import routines

    items = routines.load()
    if not items:
        console.print(
            "[dim]no routines. Add one:[/dim] "
            "[cyan]evi routine add <name> --recipe <recipe>[/cyan]"
        )
        return
    for r in items:
        auto = " [yellow](auto-approve)[/yellow]" if r.yes else ""
        state = "" if r.enabled else " [red](disabled)[/red]"
        console.print(f"  [bold]{r.name}[/bold] → {r.recipe}{auto}{state}")
        console.print(f"    [dim]POST /api/routine/{r.token}[/dim]")


@routine_app.command("remove")
def routine_remove(name: str) -> None:
    """Remove a routine."""
    from evi import routines

    if not routines.remove(name):
        console.print(f"[red]no such routine:[/red] {name}")
        raise typer.Exit(1)
    console.print(f"[yellow]removed[/yellow] {name}")


@routine_app.command("run")
def routine_run(name: str) -> None:
    """Run a routine's recipe locally (the same way the webhook would)."""
    from evi import recipes, routines

    r = routines.get(name)
    if r is None:
        console.print(f"[red]no such routine:[/red] {name}")
        raise typer.Exit(1)
    try:
        recipe = recipes.load_recipe(r.recipe)
    except recipes.RecipeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    agent = _build_agent()
    if r.yes:
        agent.enable_auto_all()
    for res in recipes.run_recipe_headless(agent, recipe):
        console.print(f"\n[bold cyan]{res['label'] or 'step'}[/bold cyan]")
        if res["error"]:
            console.print(f"[red]error:[/red] {res['error']}")
        else:
            console.print(Markdown(res["text"] or "(no output)"))


plugin_app = typer.Typer(help="Plugins — installable bundles of commands, skills, hooks, and MCP servers.")
app.add_typer(plugin_app, name="plugin")


@plugin_app.command("add")
def plugin_add(
    source: str = typer.Argument(..., help="Local directory or git URL."),
    name: str = typer.Option("", "--name", help="Override the plugin name."),
) -> None:
    """Install a plugin from a local directory or a git URL."""
    from evi import plugins

    try:
        pname = plugins.install(source, name=name or None)
    except plugins.PluginError as exc:
        console.print(f"[red]install failed:[/red] {exc}")
        raise typer.Exit(1)
    console.print(
        f"[green]installed[/green] {pname}\n"
        f"[dim]its commands are now[/dim] [cyan]/{pname}:<command>[/cyan] "
        f"[dim](see `evi plugin list`)[/dim]"
    )


@plugin_app.command("list")
def plugin_list() -> None:
    """List installed plugins."""
    from evi import plugins

    items = plugins.list_plugins()
    if not items:
        console.print(
            "[dim]no plugins. Add one with:[/dim] [cyan]evi plugin add <dir|git-url>[/cyan]"
        )
        return
    for p in items:
        ver = f" [dim]v{p.version}[/dim]" if p.version else ""
        desc = f" — {p.description}" if p.description else ""
        parts = [f"{p.commands} cmds"]
        if p.skills:
            parts.append(f"{p.skills} skills")
        if p.hooks:
            parts.append(f"{p.hooks} hooks")
        if p.mcp:
            parts.append(f"{p.mcp} mcp")
        if p.agents:
            parts.append(f"{p.agents} agents")
        counts = ", ".join(parts)
        console.print(f"  [bold]{p.name}[/bold]{ver} [dim]({counts})[/dim]{desc}")


@plugin_app.command("remove")
def plugin_remove(name: str) -> None:
    """Remove an installed plugin."""
    from evi import plugins

    if not plugins.remove(name):
        console.print(f"[red]no such plugin:[/red] {name}")
        raise typer.Exit(1)
    console.print(f"[yellow]removed[/yellow] {name}")


def _load_marketplace():
    from evi import marketplace
    from evi.config import Config

    urls = Config.load().plugins.index_urls
    return marketplace, marketplace.load_index(index_urls=urls)


@plugin_app.command("search")
def plugin_search(query: str = typer.Argument("", help="Filter by name/desc/tag.")) -> None:
    """Search the plugin marketplace index (~/.evi/marketplace.json + index_urls)."""
    marketplace, entries = _load_marketplace()
    hits = marketplace.search(query, entries)
    if not hits:
        console.print(
            "[dim]no matches.[/dim] add an index entry with "
            "[cyan]evi plugin index add <name> <source>[/cyan] "
            "or set [cyan]index_urls[/cyan][dim] under the plugins config[/dim]"
        )
        return
    for e in hits:
        tags = f" [dim]#{' #'.join(e.tags)}[/dim]" if e.tags else ""
        by = f" [dim]· {e.author}[/dim]" if e.author else ""
        console.print(f"  [bold]{e.name}[/bold]{by}{tags}")
        if e.description:
            console.print(f"    [dim]{e.description}[/dim]")
        console.print(f"    [dim]{e.source}[/dim]")


@plugin_app.command("install")
def plugin_install(name: str) -> None:
    """Install a plugin by name, resolved through the marketplace index."""
    from evi import plugins

    marketplace, entries = _load_marketplace()
    entry = marketplace.resolve(name, entries)
    if entry is None:
        console.print(
            f"[red]no plugin named[/red] {name} [dim]in the index "
            "(try `evi plugin search`)[/dim]"
        )
        raise typer.Exit(1)
    try:
        installed = plugins.install(entry.source)
    except plugins.PluginError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]installed[/green] {installed} [dim](from {entry.source})[/dim]"
    )


plugin_index_app = typer.Typer(help="Manage the local plugin index (marketplace.json).")
plugin_app.add_typer(plugin_index_app, name="index")


@plugin_index_app.command("init")
def plugin_index_init(
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace if it exists.")
) -> None:
    """Write a starter ~/.evi/marketplace.json."""
    from evi import marketplace

    try:
        path = marketplace.create_index(overwrite=overwrite)
    except marketplace.MarketplaceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]created[/green] {path}")


@plugin_index_app.command("add")
def plugin_index_add(
    name: str,
    source: str = typer.Argument(..., help="A directory path or git URL."),
    description: str = typer.Option("", "--desc", help="Short description."),
    author: str = typer.Option("", "--author", help="Author."),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
) -> None:
    """Add (or replace) an entry in the local plugin index."""
    from evi import marketplace

    entry = marketplace.MarketplaceEntry(
        name=name,
        source=source,
        description=description,
        author=author,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    marketplace.add_entry(entry)
    console.print(f"[green]indexed[/green] {name} [dim]-> {source}[/dim]")


@app.command()
def rewind(
    seq: int = typer.Argument(
        0, help="Undo file writes from this checkpoint seq onward (0 = just the latest)."
    ),
    list_: bool = typer.Option(
        False, "--list", "-l", help="List recent file checkpoints instead of undoing."
    ),
) -> None:
    """Undo eVi's file writes — file-level rewind (see also `evi rewind --list`)."""
    import datetime as _dt

    from evi import checkpoints

    if list_:
        entries = checkpoints.list_checkpoints()
        if not entries:
            console.print("[dim]no file checkpoints yet.[/dim]")
            return
        for e in entries:
            ts = _dt.datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
            console.print(
                f"  [cyan]{e['seq']:>4}[/cyan] [dim]{ts}[/dim] "
                f"[yellow]{e['op']}[/yellow] {e['path']}"
            )
        console.print("\n[dim]undo from a point:[/dim] [cyan]evi rewind <seq>[/cyan]")
        return
    actions = checkpoints.rewind(seq or None)
    if not actions:
        console.print("[dim]nothing to rewind.[/dim]")
        return
    for path, action in actions:
        console.print(f"[green]✓[/green] {action} — {path}")


@app.command()
def setup() -> None:
    """Interactive first-run wizard. Detects backends, recommends a model,
    optionally pulls it, writes config.toml."""
    import httpx

    from evi.backends import KNOWN_BACKENDS, default_base_url
    from evi.hardware import detect as detect_hw
    from evi.recommend import recommend

    ensure_dirs()
    cfg = Config.load()

    console.print(Panel.fit(
        "[bold cyan]eVi setup[/bold cyan]\n"
        "[dim]Detects local LLM backends, recommends a model for your hardware, "
        "and writes ~/.evi/config.toml.[/dim]",
        border_style="cyan",
    ))

    # --- 1. Probe backends ---------------------------------------------
    candidates = [
        ("lmstudio", default_base_url("lmstudio")),
        ("ollama", default_base_url("ollama")),
        ("llamacpp", default_base_url("llamacpp")),
    ]
    reachable: list[tuple[str, str]] = []
    console.print("\n[bold]1. Probing local backends…[/bold]")
    for kind, base in candidates:
        url = base.rstrip("/") + "/models"
        try:
            r = httpx.get(url, timeout=2.0)
            ok = r.status_code < 500
        except Exception:
            ok = False
        flag = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(f"   {flag} {kind:<10} [dim]{base}[/dim]")
        if ok:
            reachable.append((kind, base))

    if not reachable:
        console.print(
            "\n[yellow]No backends reachable.[/yellow] Start one of "
            "LM Studio (Developer → Start Server), `ollama serve`, or "
            "`llama-server`, then re-run `evi setup`."
        )
        raise typer.Exit(1)

    # --- 2. Pick a backend ---------------------------------------------
    if len(reachable) == 1:
        chosen_kind, chosen_url = reachable[0]
        console.print(f"\n[bold]2. Backend:[/bold] [green]{chosen_kind}[/green] (only one reachable)")
    else:
        console.print("\n[bold]2. Pick a backend:[/bold]")
        for i, (kind, base) in enumerate(reachable, 1):
            console.print(f"   [{i}] {kind} [dim]({base})[/dim]")
        while True:
            choice = console.input("   choice: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(reachable):
                chosen_kind, chosen_url = reachable[int(choice) - 1]
                break
            console.print("   [yellow]invalid — pick a number from the list[/yellow]")

    # --- 3. Recommend a model ------------------------------------------
    console.print("\n[bold]3. Hardware scan & recommendation:[/bold]")
    hw = detect_hw()
    rec = recommend(hw)
    if hw.primary_gpu:
        console.print(
            f"   GPU: [cyan]{hw.primary_gpu.name}[/cyan] "
            f"([cyan]{hw.primary_gpu.vram_total_mb} MB[/cyan])"
        )
    else:
        console.print("   GPU: [dim]none detected[/dim]")
    console.print(f"   RAM: [cyan]{hw.ram_total_gb:.1f} GB[/cyan]")
    for note in rec.notes:
        console.print(f"   [dim]· {note}[/dim]")

    if rec.mode == "remote-only":
        console.print(
            "\n[yellow]Local resources are too tight.[/yellow] "
            "Point at a remote backend instead — see docs/multi-machine.md."
        )
        raise typer.Exit(0)

    if rec.chat is None:
        console.print("\n[red]No suitable model found in registry.[/red]")
        raise typer.Exit(1)

    rec_id = rec.chat.id
    console.print(
        f"\n   Recommendation: [bold green]{rec_id}[/bold green]"
        f" [dim]({rec.chat.parameters}, {rec.chat.quantization}, "
        f"tool-calling={rec.chat.tool_calling})[/dim]"
    )

    # --- 4. Maybe pull (Ollama only) -----------------------------------
    if chosen_kind == "ollama" and typer.confirm(
        f"\n4. Pull {rec_id} via Ollama now?", default=True
    ):
        try:
            backend = KNOWN_BACKENDS["ollama"](base_url=chosen_url)
            with console.status(f"pulling {rec_id}…", spinner="dots"):
                for ev in backend.pull_model(rec_id):
                    if ev.status:
                        console.print(f"   [dim]{ev.status}[/dim]")
            console.print("   [green]done[/green]")
        except Exception as exc:
            console.print(f"   [red]pull failed:[/red] {exc}")
            console.print("   [dim]You can retry with `evi models pull` later.[/dim]")
    elif chosen_kind != "ollama":
        console.print(
            f"\n4. [dim]Load `{rec_id}` in {chosen_kind} manually, or run "
            f"`evi models pull hf:<repo>:<file>` for a direct download.[/dim]"
        )

    # --- 5. Write config -----------------------------------------------
    cfg.llm.backend = chosen_kind
    cfg.llm.base_url = chosen_url
    cfg.llm.model = rec_id
    # Tool-call reliability climbs at lower temps; default to 0.4 for
    # tool-tuned models.
    cfg.llm.temperature = 0.4
    cfg.save()

    console.print(
        f"\n[bold]5. Wrote[/bold] [cyan]{CONFIG_PATH}[/cyan]\n"
        f"   backend={chosen_kind}, model={rec_id}, temperature=0.4"
    )
    console.print(
        "\n[bold green]Setup complete.[/bold green] Try:\n"
        "   [cyan]evi chat[/cyan]"
    )


@app.command()
def search(
    query: str = typer.Argument(..., help="Substring (default) or regex (with --regex)."),
    days: int = typer.Option(90, "--days", help="How many days back to scan."),
    role: str | None = typer.Option(
        None, "--role", help="Filter to one role: user / assistant / tool / system.",
    ),
    session: str | None = typer.Option(
        None, "--session", help="Restrict to one session id.",
    ),
    regex_flag: bool = typer.Option(
        False, "--regex", help="Interpret query as a regex (case-insensitive).",
    ),
    limit: int = typer.Option(100, "--limit", help="Cap results."),
) -> None:
    """Grep across saved chat transcripts.

    Examples:
        evi search "TODO"               # plain substring (case-insensitive)
        evi search --regex "FIXME|XXX"  # regex
        evi search --role user "deploy" # only your own messages
        evi search --days 7 "bug"       # last week only
    """
    from evi.search import collect

    try:
        hits = collect(
            query,
            days=days,
            role=role,
            session=session,
            regex=regex_flag,
            limit=limit,
        )
    except ValueError as exc:  # bad regex
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if not hits:
        console.print("[dim](no matches)[/dim]")
        return

    last_session = None
    for h in hits:
        if h.session != last_session:
            console.print(
                f"\n[bold cyan]{h.session}[/bold cyan]  "
                f"[dim]{h.date}[/dim]"
            )
            last_session = h.session
        role_color = {
            "user": "green", "assistant": "magenta",
            "tool": "yellow", "system": "blue",
        }.get(h.role, "white")
        console.print(
            f"  [dim]L{h.line_no}[/dim] "
            f"[{role_color}]{h.role:<9}[/{role_color}] "
            f"{h.snippet}"
        )
    console.print(
        f"\n[dim]{len(hits)} match(es) · "
        f"`evi sessions show <id>` to read the full transcript[/dim]"
    )


@app.command()
def tail(
    session_id: str | None = typer.Argument(
        None,
        help="Filter to one session id. Omit to follow ALL sessions for today.",
    ),
    interval: float = typer.Option(
        0.5, help="Poll interval in seconds for new transcript lines.",
    ),
) -> None:
    """Live-tail today's transcripts as eVi writes them.

    Useful for watching a scheduled task fire, or observing a long-running
    chat from another terminal. Polls each session's `.jsonl` file for
    new bytes since the last read and prints colour-coded role lines.
    """
    import json
    import time
    from datetime import datetime

    from evi.config import TRANSCRIPTS_DIR

    ensure_dirs()
    today_dir = TRANSCRIPTS_DIR / datetime.now().strftime("%Y-%m-%d")
    if not today_dir.is_dir():
        today_dir.mkdir(parents=True, exist_ok=True)

    console.print(
        f"[dim]tailing {today_dir}"
        + (f" · session={session_id}" if session_id else " · all sessions")
        + "[/dim]"
    )
    offsets: dict[Path, int] = {}
    try:
        while True:
            for f in sorted(today_dir.glob("*.jsonl")):
                sid = f.stem
                if session_id and sid != session_id:
                    continue
                start = offsets.get(f, 0)
                try:
                    with f.open("rb") as fh:
                        fh.seek(start)
                        data = fh.read()
                        offsets[f] = fh.tell()
                except OSError:
                    continue
                if not data:
                    continue
                for line in data.decode("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = entry.get("role", "?")
                    color = {
                        "user": "green", "assistant": "magenta",
                        "tool": "yellow", "system": "blue",
                    }.get(role, "white")
                    content = entry.get("content") or ""
                    if len(content) > 200:
                        content = content[:200] + "…"
                    label = f"[{sid[:8]}/{role}]"
                    if entry.get("tool_name"):
                        label += f"({entry['tool_name']})"
                    console.print(f"[dim]{label}[/dim] [{color}]{content}[/{color}]")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")


@app.command()
def dream(hours: int = 24) -> None:
    """Review the last N hours of transcripts and curate long-term memory."""
    from evi.dream import run_dream

    console.print(
        f"[cyan]dreaming…[/cyan] reviewing last {hours}h of transcripts"
    )
    report = run_dream(hours=hours)
    console.print(
        f"[green]done[/green] · "
        f"[bold]+{len(report.added)}[/bold] / "
        f"[bold]-{len(report.removed)}[/bold] / "
        f"[bold]~{len(report.changed)}[/bold]"
    )
    if report.added:
        console.print(f"[green]added:[/green] {', '.join(report.added)}")
    if report.removed:
        console.print(f"[yellow]removed:[/yellow] {', '.join(report.removed)}")
    if report.changed:
        console.print(f"[cyan]changed:[/cyan] {', '.join(report.changed)}")
    console.print(f"[dim]log: {report.log_path}[/dim]")


@app.command()
def web(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Launch the local web UI (FastAPI + SSE)."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]uvicorn not installed[/red] — run: pip install 'evi-assistant[web]'")
        raise typer.Exit(1)
    cfg = Config.load()
    if cfg.web.auth_token:
        console.print(
            "[cyan]web auth:[/cyan] [green]enabled[/green] "
            "[dim](browser prompts for the token on first load)[/dim]"
        )
    else:
        console.print(
            "[cyan]web auth:[/cyan] [yellow]disabled[/yellow] "
            "[dim](run `evi web token rotate` to require a token)[/dim]"
        )
    console.print(f"[cyan]eVi web →[/cyan] http://{host}:{port}")
    uvicorn.run("evi.apps.web.server:app", host=host, port=port, reload=False)


web_app = typer.Typer(help="Web server auth + helper commands.")
app.add_typer(web_app, name="web-config")
# Nested typer group: `evi web-config token ...`. We use a dedicated
# top-level group rather than nesting under `web` because Typer's `web`
# command above is a plain command, not a group.

token_app = typer.Typer(help="Manage the web UI bearer token.")
web_app.add_typer(token_app, name="token")


@token_app.command("show")
def web_token_show() -> None:
    """Print the current web auth token (or `(unset)` when auth is disabled)."""
    cfg = Config.load()
    if not cfg.web.auth_token:
        console.print("[dim](unset — auth is disabled)[/dim]")
        return
    console.print(cfg.web.auth_token)


@token_app.command("rotate")
def web_token_rotate(
    length: int = typer.Option(32, help="Token byte length (gives 2× hex chars).")
) -> None:
    """Generate a new web auth token and persist it to config.toml.

    Prints the token ONCE. Open the web UI, sign in with this value, and
    your browser caches it in localStorage. Use `evi web token clear` to
    disable auth again.
    """
    import secrets as _secrets

    token = _secrets.token_hex(max(8, int(length)))
    cfg = Config.load()
    cfg.web.auth_token = token
    cfg.save()
    console.print(
        f"[green]rotated[/green]\n[bold]{token}[/bold]\n\n"
        "[dim]Paste this into the browser sign-in form. Restart `evi web` if "
        "it's currently running.[/dim]"
    )


@token_app.command("clear")
def web_token_clear() -> None:
    """Unset the auth token — `evi web` becomes open access again."""
    cfg = Config.load()
    cfg.web.auth_token = ""
    cfg.save()
    console.print("[yellow]cleared[/yellow] — web auth disabled")


models_app = typer.Typer(help="Inspect and switch the active LLM model.")
app.add_typer(models_app, name="models")


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@models_app.command("list")
def models_list() -> None:
    """List models available through the active backend."""
    cfg = Config.load()
    backend = get_backend(cfg.llm)
    models = backend.list_models()
    if not models:
        console.print(
            f"[yellow]no models reported by {backend.name} at {backend.base_url}[/yellow]"
        )
        console.print(
            "[dim]check the backend is running, or change `[llm] backend` in config.toml[/dim]"
        )
        return
    for m in sorted(models, key=lambda x: x.id):
        marker = "[green]●[/green]" if m.id == cfg.llm.model else " "
        bits = []
        if m.parameters:
            bits.append(m.parameters)
        if m.quantization:
            bits.append(m.quantization)
        if m.size_bytes:
            bits.append(_fmt_size(m.size_bytes))
        meta = " · ".join(bits)
        console.print(
            f"{marker} [bold]{m.id}[/bold]"
            + (f" [dim]({meta})[/dim]" if meta else "")
        )


@models_app.command("active")
def models_active() -> None:
    """Print the model currently configured under `[llm] model`."""
    cfg = Config.load()
    console.print(
        f"[bold]{cfg.llm.model}[/bold] [dim]via {cfg.llm.backend} @ {cfg.llm.base_url}[/dim]"
    )


@models_app.command("info")
def models_info(model_id: str) -> None:
    """Show backend-specific info for one model."""
    cfg = Config.load()
    backend = get_backend(cfg.llm)
    info = backend.model_info(model_id)
    if info is None:
        console.print(f"[red]not found:[/red] {model_id}")
        raise typer.Exit(1)
    console.print(f"[bold]{info.display_name()}[/bold]")
    console.print(f"  backend: {info.backend}")
    if info.family:
        console.print(f"  family: {info.family}")
    if info.parameters:
        console.print(f"  parameters: {info.parameters}")
    if info.quantization:
        console.print(f"  quantization: {info.quantization}")
    if info.size_bytes:
        console.print(f"  size: {_fmt_size(info.size_bytes)}")
    from evi.recommend import context_window_for

    win = context_window_for(model_id)
    if win:
        console.print(f"  context window: ~{win // 1024}K tokens [dim](native)[/dim]")


@models_app.command("use")
def models_use(model_id: str) -> None:
    """Set `[llm] model` to this id and save config.toml."""
    cfg = Config.load()
    backend = get_backend(cfg.llm)
    if backend.model_info(model_id) is None and not typer.confirm(
        f"{model_id} not found on {backend.name}. Save anyway?",
        default=False,
    ):
        raise typer.Exit(1)
    cfg.llm.model = model_id
    cfg.save()
    console.print(f"[green]using[/green] {model_id}")

    # Long-context awareness: nudge if the configured context_size doesn't match
    # the model's known native window.
    from evi.recommend import context_window_for

    win = context_window_for(model_id)
    if win:
        cur = cfg.llm.context_size or 0
        if cur > win:
            console.print(
                f"[yellow]note:[/yellow] {model_id} supports ~{win // 1024}K tokens "
                f"but [cyan]llm.context_size[/cyan] is {cur} — lower it to avoid truncation."
            )
        elif cur == 0:
            console.print(
                f"[dim]tip: set [cyan]llm.context_size[/cyan] (~{win} for this model).[/dim]"
            )


@models_app.command("pull")
def models_pull(
    ref: str = typer.Argument(
        ...,
        help=(
            "Model id. Format depends on backend:\n"
            "  • Ollama: a tag like `qwen2.5:14b`\n"
            "  • LM Studio / llama.cpp: `hf:<repo>` or `hf:<repo>:<file.gguf>`\n"
        ),
    ),
) -> None:
    """Download a model. Backend-aware: Ollama tags get pulled into Ollama's
    own store; `hf:...` refs are downloaded to ~/.evi/models/."""
    from evi.downloads import download_gguf, parse_hf_ref
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    hf_ref = parse_hf_ref(ref)
    if hf_ref is not None:
        try:
            path = download_gguf(
                hf_ref,
                on_progress=lambda msg: console.print(f"[dim]{msg}[/dim]"),
            )
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]downloaded:[/green] {path}")
        console.print(
            "[dim]Point your backend at this file: LM Studio → 'Local Models' "
            "folder, or llama-server -m <path>[/dim]"
        )
        return

    # Backend-driven pull (Ollama).
    cfg = Config.load()
    backend = get_backend(cfg.llm)
    if not backend.supports_pull():
        console.print(
            f"[yellow]{backend.name} has no pull API. Use the `hf:<repo>:<file>` "
            "syntax to download a GGUF directly:[/yellow]"
        )
        console.print(
            f"[dim]  evi models pull hf:bartowski/{ref}-GGUF:{ref}-Q4_K_M.gguf[/dim]"
        )
        raise typer.Exit(1)

    progress = Progress(
        TextColumn("[bold]{task.fields[status]}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )
    task_id = None
    with progress:
        try:
            for ev in backend.pull_model(ref):
                if task_id is None and ev.total:
                    task_id = progress.add_task(
                        "pulling", total=ev.total, status=ev.status or "starting"
                    )
                if task_id is not None:
                    progress.update(
                        task_id,
                        completed=ev.downloaded or 0,
                        status=ev.status or "downloading",
                    )
                else:
                    console.print(f"[dim]{ev.status}[/dim]")
        except Exception as exc:
            console.print(f"[red]pull failed:[/red] {exc}")
            raise typer.Exit(1)
    console.print(f"[green]pulled[/green] {ref}")


@models_app.command("recommend")
def models_recommend() -> None:
    """Detect this machine's hardware and recommend a model."""
    from evi.hardware import detect
    from evi.recommend import recommend

    hw = detect()
    rec = recommend(hw)

    console.print(Panel.fit(
        Text.assemble(
            ("Hardware\n", "bold cyan"),
            (f"  RAM: {hw.ram_total_gb:.1f} GB\n", ""),
            *(
                [(f"  GPU: {g.name} ({g.vram_total_mb} MB VRAM, "
                  f"cc {g.compute_capability or '?'})\n", "")
                 for g in hw.gpus]
                or [("  GPU: none detected (NVIDIA-only sensing)\n", "yellow")]
            ),
        ),
        border_style="cyan",
    ))

    for note in rec.notes:
        console.print(f"[dim]· {note}[/dim]")
    console.print()

    if rec.mode == "remote-only":
        console.print(
            "[yellow]Recommendation: point eVi at a remote backend instead "
            "of running local.[/yellow]"
        )
        console.print(
            '[dim]Edit ~/.evi/config.toml → [llm] base_url = "http://ai-server:1234/v1"[/dim]'
        )
        return

    if rec.chat is not None:
        c = rec.chat
        console.print(
            f"[bold]Chat:[/bold]  [green]{c.id}[/green] "
            f"[dim]({c.parameters}, {c.quantization}, tool-calling={c.tool_calling})[/dim]"
        )
        if c.notes:
            console.print(f"        [dim]{c.notes}[/dim]")
    if rec.coder is not None and (rec.chat is None or rec.coder.id != rec.chat.id):
        c = rec.coder
        console.print(
            f"[bold]Coder:[/bold] [green]{c.id}[/green] "
            f"[dim]({c.parameters}, {c.quantization}, tool-calling={c.tool_calling})[/dim]"
        )
        if c.notes:
            console.print(f"        [dim]{c.notes}[/dim]")

    cfg = Config.load()
    if cfg.llm.backend == "ollama" and rec.chat is not None:
        console.print(
            f"\n[dim]Pull with:[/dim] [bold]ollama pull {rec.chat.id}[/bold] "
            f"(or `evi models pull` once 9.4 lands)"
        )
    console.print(
        f"\n[dim]Apply with:[/dim] [bold]evi models use {rec.chat.id if rec.chat else '<id>'}[/bold]"
    )


@models_app.command("backend")
def models_backend(kind: str | None = typer.Argument(None)) -> None:
    """Show or change `[llm] backend`. With no argument: print current.

    Valid kinds: lmstudio, ollama, llamacpp, openai_compat.
    """
    cfg = Config.load()
    if kind is None:
        console.print(
            f"[bold]{cfg.llm.backend}[/bold] @ {cfg.llm.base_url}"
        )
        console.print(f"[dim]available: {', '.join(KNOWN_BACKENDS)}[/dim]")
        return
    kind = kind.strip().lower()
    if kind not in KNOWN_BACKENDS:
        console.print(
            f"[red]unknown backend[/red] — pick one of: {', '.join(KNOWN_BACKENDS)}"
        )
        raise typer.Exit(1)
    if cfg.llm.base_url in (
        "http://localhost:1234/v1",
        "http://localhost:11434/v1",
        "http://localhost:8080/v1",
        "http://localhost:8000/v1",
    ):
        # Move to the new backend's default localhost URL since the old one
        # was clearly an untouched default.
        cfg.llm.base_url = default_base_url(kind)
    cfg.llm.backend = kind
    cfg.save()
    console.print(
        f"[green]backend → {kind}[/green] · base_url={cfg.llm.base_url}"
    )


obsidian_app = typer.Typer(help="Sync eVi memory with an Obsidian vault.")
app.add_typer(obsidian_app, name="obsidian")


def _obsidian_vault() -> tuple[str, str] | None:
    """Read vault settings from config; return None with a clear error if unset."""
    cfg = Config.load()
    vault = cfg.obsidian.vault_path.strip()
    if not vault:
        console.print(
            "[yellow]Obsidian vault not configured.[/yellow] "
            "Set it in ~/.evi/config.toml:\n\n"
            "  [obsidian]\n"
            '  vault_path = "C:/Users/me/Documents/MyVault"\n'
            '  subdir = "eVi"\n'
        )
        return None
    return vault, cfg.obsidian.subdir


@obsidian_app.command("status")
def obsidian_status() -> None:
    """Show what's in memory vs vault without changing anything."""
    from evi.memory import MemoryStore
    from evi.obsidian import status

    cfg = _obsidian_vault()
    if cfg is None:
        raise typer.Exit(1)
    vault, sub = cfg
    info = status(MemoryStore(), vault, sub)
    console.print(f"[dim]vault dir:[/dim] {info['vault_dir'][0]}")
    if info["only_in_memory"]:
        console.print(f"[green]only in memory:[/green] {', '.join(info['only_in_memory'])}")
    if info["only_in_vault"]:
        console.print(f"[yellow]only in vault:[/yellow] {', '.join(info['only_in_vault'])}")
    if info["in_both"]:
        console.print(f"[dim]in both:[/dim] {len(info['in_both'])} entries")


def _print_stats(stats, action: str) -> None:
    console.print(f"[bold]{action}[/bold] · {stats.summary()}")
    if stats.pushed:
        console.print(f"  [green]pushed:[/green] {', '.join(stats.pushed)}")
    if stats.pulled:
        console.print(f"  [cyan]pulled:[/cyan] {', '.join(stats.pulled)}")
    if stats.skipped:
        console.print(f"  [yellow]skipped:[/yellow] {', '.join(stats.skipped)}")
    if stats.errors:
        for err in stats.errors:
            console.print(f"  [red]error:[/red] {err}")


@obsidian_app.command("push")
def obsidian_push(
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't write — just report."),
) -> None:
    """Copy live memory entries into the vault (overwrites)."""
    from evi.memory import MemoryStore
    from evi.obsidian import push

    cfg = _obsidian_vault()
    if cfg is None:
        raise typer.Exit(1)
    vault, sub = cfg
    stats = push(MemoryStore(), vault, sub, dry_run=dry_run)
    _print_stats(stats, "push (dry-run)" if dry_run else "push")


@obsidian_app.command("pull")
def obsidian_pull(
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't write — just report."),
) -> None:
    """Read vault entries into eVi memory (overwrites)."""
    from evi.memory import MemoryStore
    from evi.obsidian import pull

    cfg = _obsidian_vault()
    if cfg is None:
        raise typer.Exit(1)
    vault, sub = cfg
    stats = pull(MemoryStore(), vault, sub, dry_run=dry_run)
    _print_stats(stats, "pull (dry-run)" if dry_run else "pull")


@obsidian_app.command("sync")
def obsidian_sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't write — just report."),
) -> None:
    """Bidirectional sync; newer side wins on a per-entry basis."""
    from evi.memory import MemoryStore
    from evi.obsidian import sync

    cfg = _obsidian_vault()
    if cfg is None:
        raise typer.Exit(1)
    vault, sub = cfg
    stats = sync(MemoryStore(), vault, sub, dry_run=dry_run)
    _print_stats(stats, "sync (dry-run)" if dry_run else "sync")


backup_app = typer.Typer(help="Backup and restore eVi state.")
app.add_typer(backup_app, name="backup")


def _fmt_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


@backup_app.command("create")
def backup_create(
    out: Path | None = typer.Option(None, help="Output archive path. Default: ~/.evi/evi-backup-<stamp>.tar.gz"),
    include: list[str] = typer.Option(
        [],
        help="Override default excludes (e.g. --include models --include transcripts).",
    ),
) -> None:
    """Create a portable backup archive of ~/.evi/."""
    from evi.backup import create_backup

    summary = create_backup(out_path=out, includes=set(include))
    console.print(
        f"[green]wrote[/green] {summary.archive}\n"
        f"  files:   {summary.file_count}\n"
        f"  packed:  {_fmt_bytes(summary.bytes_packed)}\n"
        f"  excluded top-level: {', '.join(summary.excluded_top) or '(none)'}"
    )


@backup_app.command("restore")
def backup_restore(
    archive: Path,
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Merge into an existing non-empty ~/.evi/. Required when there are stray files.",
    ),
) -> None:
    """Restore an archive into ~/.evi/. By default refuses to clobber existing state."""
    from evi.backup import restore_backup

    try:
        summary = restore_backup(archive, overwrite=overwrite)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print("[dim]Pass --overwrite to merge.[/dim]")
        raise typer.Exit(1)
    except FileNotFoundError:
        console.print(f"[red]archive not found:[/red] {archive}")
        raise typer.Exit(1)

    console.print(
        f"[green]restored[/green] {summary.file_count} files into {summary.home}"
    )


finetune_app = typer.Typer(
    help="Fine-tune dataset tools — curate transcripts into training data."
)
app.add_typer(finetune_app, name="finetune")


@finetune_app.command("export")
def finetune_export(
    out: str = typer.Option(
        "evi-finetune.jsonl", "--out", "-o", help="Output JSONL path."
    ),
    days: int = typer.Option(0, "--days", help="Only the last N days (0 = all)."),
    limit: int = typer.Option(10000, "--limit", help="Max sessions to scan."),
    min_turns: int = typer.Option(
        1, "--min-turns", help="Skip sessions with fewer user turns."
    ),
    session: list[str] = typer.Option(
        None, "--session", help="Only these session ids (repeatable)."
    ),
    system: str = typer.Option(
        "", "--system", help="Prepend this system message to every example."
    ),
    include_tools: bool = typer.Option(
        False, "--include-tools", help="Keep tool calls + results (default: drop)."
    ),
) -> None:
    """Export stored sessions to a JSONL fine-tune dataset (one chat per line)."""
    from evi import finetune

    written, seen = finetune.export_dataset(
        out,
        sessions=session or None,
        days=(days or None),
        limit=limit,
        min_user_turns=min_turns,
        system=(system or None),
        include_tools=include_tools,
    )
    console.print(
        f"[green]wrote[/green] {written} examples from {seen} sessions "
        f"→ [cyan]{out}[/cyan]"
    )
    if written == 0:
        console.print(
            "[dim]no examples — check tools.transcripts is on, or widen --days / "
            "try --include-tools[/dim]"
        )


sessions_app = typer.Typer(help="Browse and resume past chat sessions.")
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("list")
def sessions_list(
    days: int = typer.Option(7, help="Days back to scan."),
    limit: int = typer.Option(20, help="Max sessions to list."),
) -> None:
    """List recent sessions, newest first."""
    from evi.sessions import fmt_when, list_sessions

    items = list_sessions(days=days, limit=limit)
    if not items:
        console.print("[dim]no sessions found[/dim]")
        console.print(
            "[dim]Make sure `tools.transcripts = true` in config.toml.[/dim]"
        )
        return
    for s in items:
        console.print(
            f"[bold]{s.session_id}[/bold]  "
            f"[dim]{fmt_when(s.started_at)}[/dim]  "
            f"[cyan]{s.message_count:>3} msgs[/cyan]  "
            f"{s.first_user_message}"
        )


@sessions_app.command("show")
def sessions_show(session_id: str) -> None:
    """Print the full contents of a session."""
    from evi.sessions import find_session, history_from_transcript

    path = find_session(session_id)
    if path is None:
        console.print(f"[red]no session[/red] {session_id}")
        raise typer.Exit(1)
    for msg in history_from_transcript(path):
        role = msg["role"]
        content = msg.get("content", "")
        color = {"user": "green", "assistant": "magenta", "tool": "yellow"}.get(role, "white")
        console.print(f"[bold {color}]{role}:[/bold {color}] {content}")


@sessions_app.command("export")
def sessions_export(
    session_id: str,
    fmt: str = typer.Option("md", "--format", "-f", help="md / html / json"),
    out: Path | None = typer.Option(
        None, "--out", "-o",
        help="Write to this path. Defaults to stdout.",
    ),
) -> None:
    """Export a session's transcript as markdown, HTML, or JSON."""
    from evi.sessions import export_session

    try:
        body = export_session(session_id, fmt=fmt)
    except FileNotFoundError:
        console.print(f"[red]no session[/red] {session_id}")
        raise typer.Exit(1)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if out is None:
        # Print plain to stdout so users can pipe.
        print(body)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")


def _hydrate_agent(agent: Agent, path) -> int:
    """Load a transcript into `agent.history`, keeping its composed system
    prompt at index 0. Returns the number of restored messages."""
    from evi.sessions import history_from_transcript

    history = history_from_transcript(path)
    agent.history = [agent.history[0], *history]
    return len(history)


@sessions_app.command("resume")
def sessions_resume(session_id: str) -> None:
    """Resume a past session — load its history and continue in the same file."""
    from evi.sessions import find_session

    path = find_session(session_id)
    if path is None:
        console.print(f"[red]no session[/red] {session_id}")
        raise typer.Exit(1)
    agent = _build_agent()
    agent.session_id = session_id  # new messages append to the same file
    n = _hydrate_agent(agent, path)
    console.print(f"[cyan]resumed {session_id}[/cyan] [dim]({n} messages restored)[/dim]")
    _run_repl(agent)


@sessions_app.command("continue")
def sessions_continue() -> None:
    """Resume the most recently active session."""
    from evi.sessions import find_session, most_recent_session_id

    sid = most_recent_session_id()
    if sid is None:
        console.print("[dim]no sessions to continue[/dim]")
        raise typer.Exit(1)
    path = find_session(sid)
    agent = _build_agent()
    agent.session_id = sid
    n = _hydrate_agent(agent, path)
    console.print(f"[cyan]continuing {sid}[/cyan] [dim]({n} messages restored)[/dim]")
    _run_repl(agent)


@sessions_app.command("handoff")
def sessions_handoff(
    session_id: str = typer.Argument(
        None, help="Session to hand off (default: the most recent)."
    ),
) -> None:
    """Print how to pick a session up on another device (Phase 87).

    Transcripts persist per-turn, so a session is resumable once it has any
    turns. Sync first (`evi sync`), then on the other device run the resume
    command or open the URL.
    """
    from evi.sessions import handoff_info, most_recent_session_id

    sid = session_id or most_recent_session_id()
    if sid is None:
        console.print("[dim]no sessions to hand off[/dim]")
        raise typer.Exit(1)
    info = handoff_info(sid)
    if info is None:
        console.print(f"[red]no session[/red] {sid}")
        raise typer.Exit(1)
    console.print(f"[cyan]handoff {sid}[/cyan] [dim]({info['messages']} messages)[/dim]")
    console.print("  1. sync this device:   [bold]evi sync push[/bold]")
    console.print("  2. on the other device: [bold]evi sync pull[/bold], then either")
    console.print(f"       [green]{info['resume_cmd']}[/green]")
    console.print(f"       or open [green]{info['resume_url']}[/green] in the web UI")


@sessions_app.command("fork")
def sessions_fork(session_id: str) -> None:
    """Fork a past session into a NEW session — the original is left intact."""
    from evi.sessions import find_session

    path = find_session(session_id)
    if path is None:
        console.print(f"[red]no session[/red] {session_id}")
        raise typer.Exit(1)
    agent = _build_agent()  # fresh agent → new auto-generated session_id
    n = _hydrate_agent(agent, path)
    console.print(
        f"[cyan]forked {session_id}[/cyan] → new session "
        f"[bold]{agent.session_id}[/bold] [dim]({n} messages copied)[/dim]"
    )
    _run_repl(agent)


@sessions_app.command("title")
def sessions_title(session_id: str) -> None:
    """Generate a short LLM-written title for a past session."""
    from evi.sessions import find_session, history_from_transcript

    path = find_session(session_id)
    if path is None:
        console.print(f"[red]no session[/red] {session_id}")
        raise typer.Exit(1)
    agent = _build_agent()
    history = history_from_transcript(path)
    agent.history = [agent.history[0], *history]
    title = agent.suggest_title()
    if not title:
        console.print("[yellow](could not generate a title)[/yellow]")
        raise typer.Exit(1)
    console.print(title)


worktree_app = typer.Typer(help="Git worktree helpers for parallel work.")
app.add_typer(worktree_app, name="worktree")


@worktree_app.command("list")
def worktree_list() -> None:
    """List worktrees for the current repo."""
    from evi.worktree import WorktreeError, list_worktrees

    try:
        entries = list_worktrees()
    except WorktreeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    for e in entries:
        branch = e.branch or "[dim](detached)[/dim]"
        console.print(f"  [bold]{branch:<30}[/bold] [dim]{e.head}[/dim] {e.path}")


@worktree_app.command("create")
def worktree_create(
    branch: str,
    base: str | None = typer.Option(None, help="Branch / commit to fork from."),
    existing: bool = typer.Option(
        False, "--existing", help="Check out an existing branch instead of creating it."
    ),
) -> None:
    """Create a worktree at <repo>/.worktrees/<branch>/ on `branch`."""
    from evi.worktree import WorktreeError, create_worktree

    try:
        path = create_worktree(branch, create_branch=not existing, base=base)
    except WorktreeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]created[/green] {path}")
    console.print(
        f"[dim]start a session there:[/dim] [bold]evi worktree chat {branch}[/bold]"
    )


@worktree_app.command("remove")
def worktree_remove(branch_or_path: str) -> None:
    """Remove a worktree (--force; commits are kept on the branch)."""
    from evi.worktree import WorktreeError, remove_worktree

    try:
        remove_worktree(branch_or_path)
    except WorktreeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[yellow]removed[/yellow] {branch_or_path}")


@worktree_app.command("chat")
def worktree_chat(branch: str) -> None:
    """Open the chat REPL with cwd inside the named worktree."""
    from evi.worktree import WorktreeError, find_worktree_for

    try:
        path = find_worktree_for(branch)
    except WorktreeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if path is None:
        console.print(
            f"[red]no worktree for branch[/red] {branch} "
            f"[dim](create one with `evi worktree create {branch}`)[/dim]"
        )
        raise typer.Exit(1)
    os.chdir(path)
    console.print(f"[dim]cwd → {path}[/dim]")
    # Reuse the regular chat command. It'll pick up EVI.md from the new cwd.
    chat()


profile_app = typer.Typer(help="Manage config profiles for different machines.")
app.add_typer(profile_app, name="profile")


@profile_app.command("list")
def profile_list() -> None:
    """List available profiles."""
    names = list_profiles()
    active = os.environ.get(PROFILE_ENV_VAR, "").strip()
    if not names:
        console.print("[dim]no profiles. Add one:[/dim]")
        console.print(
            "[dim]  evi profile add home --base-url http://ai-server:1234/v1[/dim]"
        )
        return
    for name in names:
        marker = "[green]●[/green]" if name == active else " "
        console.print(f"{marker} [bold]{name}[/bold]")


@profile_app.command("show")
def profile_show(name: str) -> None:
    """Print a profile's TOML contents."""
    path = profile_path(name)
    if not path.is_file():
        console.print(f"[red]no such profile:[/red] {name}")
        raise typer.Exit(1)
    console.print(f"[dim]{path}[/dim]")
    console.print(path.read_text(encoding="utf-8"))


@profile_app.command("path")
def profile_path_cmd(name: str | None = typer.Argument(None)) -> None:
    """Print the profiles dir, or one profile's file path."""
    if name is None:
        console.print(str(PROFILES_DIR))
    else:
        console.print(str(profile_path(name)))


@profile_app.command("add")
def profile_add(
    name: str,
    backend: str | None = typer.Option(None, help="lmstudio | ollama | llamacpp | openai_compat"),
    base_url: str | None = typer.Option(None, help="Override [llm] base_url."),
    model: str | None = typer.Option(None, help="Override [llm] model."),
    force: bool = typer.Option(False, "--force", help="Overwrite if it already exists."),
) -> None:
    """Create a partial profile under ~/.evi/profiles/<name>.toml."""
    ensure_dirs()
    path = profile_path(name)
    if path.is_file() and not force:
        console.print(f"[red]exists:[/red] {path}. Use --force to overwrite.")
        raise typer.Exit(1)
    llm_lines: list[str] = []
    if backend:
        if backend not in KNOWN_BACKENDS:
            console.print(f"[red]unknown backend[/red]: {backend}")
            raise typer.Exit(1)
        llm_lines.append(f'backend = "{backend}"')
    if base_url:
        llm_lines.append(f'base_url = "{base_url}"')
    if model:
        llm_lines.append(f'model = "{model}"')
    if not llm_lines:
        console.print(
            "[yellow]profile is empty[/yellow] — pass --backend / --base-url / --model"
        )
        raise typer.Exit(1)
    body = "[llm]\n" + "\n".join(llm_lines) + "\n"
    path.write_text(body, encoding="utf-8")
    console.print(f"[green]wrote[/green] {path}")
    console.print(f"[dim]activate with:[/dim] evi --profile {name} chat")


@profile_app.command("remove")
def profile_remove(name: str) -> None:
    """Delete a profile."""
    path = profile_path(name)
    if not path.is_file():
        console.print(f"[red]no such profile[/red]: {name}")
        raise typer.Exit(1)
    path.unlink()
    console.print(f"[yellow]removed[/yellow] {path}")


@profile_app.command("active")
def profile_active() -> None:
    """Print the active profile (from $EVI_PROFILE), if any."""
    name = os.environ.get(PROFILE_ENV_VAR, "").strip()
    if not name:
        console.print("[dim](none — using base config.toml)[/dim]")
        return
    console.print(f"[bold]{name}[/bold]")
    overlay = load_profile_overlay(name)
    if not overlay:
        console.print("[yellow]profile file missing or empty[/yellow]")
    else:
        for section, body in overlay.items():
            console.print(f"  [cyan]{section}[/cyan]: {body}")


mcp_app = typer.Typer(help="MCP (Model Context Protocol) commands.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("path")
def mcp_path() -> None:
    """Print the MCP server-list JSON path."""
    console.print(str(MCP_CONFIG_PATH))


@mcp_app.command("list-servers")
def mcp_list_servers() -> None:
    """List configured MCP servers from ~/.evi/mcp.json."""
    servers = load_servers()
    if not servers:
        console.print("[dim]no MCP servers configured[/dim]")
        console.print(f"[dim]edit:[/dim] {MCP_CONFIG_PATH}")
        return
    for s in servers:
        flag = "[green]on [/green]" if s.enabled else "[red]off[/red]"
        argstr = " ".join(s.args)
        console.print(f"{flag} [bold]{s.name}[/bold] — [dim]{s.command} {argstr}[/dim]")


@mcp_app.command("list-tools")
def mcp_list_tools() -> None:
    """Start MCP servers and list every tool they expose."""
    cfg = Config.load()
    if not cfg.tools.mcp:
        console.print("[yellow]tools.mcp = false in config — enable it first[/yellow]")
    servers = load_servers()
    if not servers:
        console.print("[dim]no MCP servers configured[/dim]")
        return
    try:
        manager = MCPManager(servers)
        manager.start()
    except ImportError:
        console.print("[red]`mcp` package not installed — run: pip install 'evi-assistant[mcp]'[/red]")
        raise typer.Exit(1)
    try:
        names = manager.registered_tool_names()
        if not names:
            console.print("[dim]no tools discovered[/dim]")
            return
        for tname in sorted(names):
            t = REGISTRY[tname]
            console.print(f"[bold]{tname}[/bold] — [dim]{t.description}[/dim]")
    finally:
        manager.stop()


@mcp_app.command("serve")
def mcp_serve(
    categories: str = typer.Option(
        "memory,index,calendar,git",
        "--categories",
        "-c",
        help="Comma-separated tool categories to expose to MCP clients.",
    ),
    tools: str = typer.Option(
        "", "--tools",
        help="Optional allow-list of exact tool names (comma-separated) within the categories.",
    ),
    http: bool = typer.Option(
        False, "--http",
        help="Serve over streamable HTTP instead of stdio (for remote clients).",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="HTTP bind host (with --http)."),
    port: int = typer.Option(8765, "--port", help="HTTP bind port (with --http)."),
    token: str = typer.Option(
        "", "--token",
        help="Require this bearer token (with --http). Strongly recommended for non-localhost.",
    ),
) -> None:
    """Run eVi AS an MCP server, exposing eVi's tools + memory resources +
    command prompts to Claude Desktop / Cursor / Cline / Continue. Default
    transport is stdio (spawned by the client); use --http for a remote,
    optionally token-gated, streamable-HTTP server. See `mcp serve-config`."""
    try:
        from evi.mcp.publish import serve
    except ImportError:
        console.print("[red]`mcp` package not installed — run: pip install 'evi-assistant[mcp]'[/red]")
        raise typer.Exit(1)
    cats = tuple(c.strip() for c in categories.split(",") if c.strip())
    allow = tuple(t.strip() for t in tools.split(",") if t.strip()) or None
    if http and not token:
        console.print("[yellow]⚠ --http without --token: the server is unauthenticated. "
                      "Bind to localhost only, or pass --token.[/yellow]")
    serve(cats, allow, http=http, host=host, port=port, token=token)


@mcp_app.command("serve-config")
def mcp_serve_config(
    categories: str = typer.Option(
        "memory,index,calendar,git", "--categories", "-c",
        help="Tool categories the snippet will expose.",
    ),
) -> None:
    """Print an MCP client config snippet (Claude Desktop / Cursor) that runs
    `evi mcp serve`. Paste it into the client's mcpServers config."""
    import json
    import sys

    snippet = {
        "mcpServers": {
            "evi": {
                "command": sys.executable,
                "args": ["-m", "evi", "mcp", "serve", "--categories", categories],
            }
        }
    }
    console.print(json.dumps(snippet, indent=2))


schedule_app = typer.Typer(help="Manage scheduled prompts.")
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("add")
def schedule_add(
    name: str = typer.Option(..., help="Human-friendly task name."),
    cron: str = typer.Option(..., help='Crontab string, e.g. "0 9 * * *".'),
    prompt: str = typer.Option(..., help="The prompt to send to eVi."),
    disabled: bool = typer.Option(False, help="Create the task in disabled state."),
) -> None:
    """Save a new scheduled task."""
    store = TaskStore()
    task = store.add(name=name, cron=cron, prompt=prompt, enabled=not disabled)
    console.print(f"[green]added[/green] {task.id} — {task.name}")


@schedule_app.command("list")
def schedule_list() -> None:
    """List all scheduled tasks."""
    store = TaskStore()
    tasks = store.list()
    if not tasks:
        console.print("[dim]no scheduled tasks[/dim]")
        return
    for t in tasks:
        flag = "[green]on [/green]" if t.enabled else "[red]off[/red]"
        status = t.last_status or "(never run)"
        console.print(
            f"{flag} [bold]{t.id}[/bold] {t.name} "
            f"[dim]cron={t.cron} last={status}[/dim]"
        )


@schedule_app.command("remove")
def schedule_remove(task_id: str) -> None:
    """Delete a scheduled task by id."""
    if TaskStore().remove(task_id):
        console.print(f"[yellow]removed[/yellow] {task_id}")
    else:
        console.print(f"[red]no task[/red] {task_id}")
        raise typer.Exit(1)


@schedule_app.command("enable")
def schedule_enable(task_id: str) -> None:
    """Enable a previously-disabled task."""
    TaskStore().set_enabled(task_id, True)
    console.print(f"[green]enabled[/green] {task_id}")


@schedule_app.command("disable")
def schedule_disable(task_id: str) -> None:
    """Disable a task (kept on disk, just won't fire)."""
    TaskStore().set_enabled(task_id, False)
    console.print(f"[yellow]disabled[/yellow] {task_id}")


@schedule_app.command("run-now")
def schedule_run_now(task_id: str) -> None:
    """Run a task immediately, regardless of its cron."""
    from evi.scheduler import Scheduler

    sched = Scheduler()
    log_path = sched.run_now(task_id)
    console.print(f"[cyan]ran[/cyan] {task_id} → {log_path}")


@app.command()
def scheduler(reload_interval: int = 60) -> None:
    """Run the scheduler in the foreground (Ctrl-C to stop).

    Re-syncs jobs with disk every `--reload-interval` seconds so changes via
    `evi schedule add/remove/...` are picked up without restarting.
    """
    from evi.scheduler import Scheduler

    sched = Scheduler()
    try:
        sched.start()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print("[cyan]eVi scheduler running.[/cyan] Ctrl-C to stop.")
    try:
        import time

        while True:
            time.sleep(reload_interval)
            sched.reload()
    except KeyboardInterrupt:
        console.print("\n[dim]stopping scheduler…[/dim]")
    finally:
        sched.stop()


@app.command("tools")
def tools_list() -> None:
    """List registered tools and whether they are enabled."""
    cfg = Config.load()
    toggles = asdict(cfg.tools)
    for t in REGISTRY.values():
        enabled = toggles.get(t.category, False)
        flag = "[green]on [/green]" if enabled else "[red]off[/red]"
        console.print(f"{flag} [bold]{t.name}[/bold] [dim]({t.category})[/dim] — {t.description}")


if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
