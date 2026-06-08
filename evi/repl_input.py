"""REPL input — prompt_toolkit-backed line editor with completion + history.

This is a thin shim over `prompt_toolkit.PromptSession`. The goal is to
keep the rest of the CLI unchanged: callers ask `read(prompt)` and get a
string back, just like `input()` or `console.input()`.

What we add over plain input:
- ⏎  Slash-command tab completion (`/help`, `/effort` …).
- ⏎  Argument completion: `/effort <Tab>` → low/medium/high/max,
        `/model <Tab>` → backend's model ids,
        `/forcetool <Tab>` → enabled tool names,
        `/image <Tab>` / `/audio <Tab>` → filesystem paths.
- ⏎  Persistent history at `~/.evi/repl_history`, with up-arrow recall.
- ⏎  Multi-line input via Shift+Enter (Esc-Enter on terminals that don't
        forward Shift-Enter — most do).
- ⏎  Graceful fallback to `console.input()` when prompt_toolkit is
        missing (e.g. on a stripped-down container) so the user can still
        chat.

Completion data is gathered lazily — model lists in particular cost a
network call so we cache them for the lifetime of the REPL.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, TYPE_CHECKING

from evi.config import HOME

if TYPE_CHECKING:  # avoid heavy import at module load
    from evi.llm.agent import Agent


HISTORY_PATH = HOME / "repl_history"

# Effort knob — must mirror evi.config.LLMSettings.reasoning_effort.
_EFFORT_LEVELS = ("low", "medium", "high", "max")

# Built-in slash commands. Kept in sync with evi/apps/cli/main.py:_BUILTINS;
# we duplicate here so this module doesn't depend on the CLI module.
_BUILTIN_COMMANDS = (
    "help", "reset", "exit", "quit", "tools", "model", "goal", "plan",
    "auto", "compact", "context", "ctx", "image", "img", "effort", "fast",
    "json", "notools", "forcetool", "reload", "audio", "speak", "predict",
)


def _strip_rich(text: str) -> str:
    """Remove Rich markup tags so prompt_toolkit doesn't print them
    literally. Best-effort — Rich's grammar is fuzzy.

    Permissive enough to strip closing tags with empty bodies (`[/]`) which
    Rich treats as "close the most recent tag".
    """
    return re.sub(r"\[/?[A-Za-z0-9_# .,-]*\]", "", text)


# --- completion ---------------------------------------------------------


def _completer_class():
    """Return our completer class, defined lazily so the module imports
    even when prompt_toolkit is missing."""
    from prompt_toolkit.completion import (
        Completer,
        Completion,
        PathCompleter,
    )

    class _EviCompleter(Completer):
        """Slash-command-aware completer.

        Behaviour depends on the parsed line:

        - empty / non-slash input → no suggestions (we don't want to
          interrupt freeform chat)
        - leading `/` with no space → match against the command name
        - `/<cmd> <args>`           → delegate to per-command logic

        Argument completions try not to be silently empty: when a model
        list can't be fetched, we just yield nothing rather than erroring.
        """

        def __init__(self, agent: "Agent") -> None:
            self.agent = agent
            self._path = PathCompleter(expanduser=True, only_directories=False)
            self._models_cache: list[str] | None = None

        # --- per-command resolvers --------------------------------------

        def _model_ids(self) -> list[str]:
            if self._models_cache is not None:
                return self._models_cache
            try:
                from evi.backends import get_backend

                models = [m.id for m in get_backend(self.agent.config.llm).list_models()]
            except Exception:
                models = []
            # Always include the active id so /model <Tab> shows something
            # even when the backend is unreachable.
            active = self.agent.config.llm.model
            if active and active not in models:
                models = [active, *models]
            self._models_cache = models
            return models

        # --- public API -------------------------------------------------

        def get_completions(self, document, complete_event) -> Iterable["Completion"]:
            text = document.text_before_cursor
            if not text.startswith("/"):
                return  # type: ignore[return-value]
            stripped = text[1:]
            if " " not in stripped:
                # Completing the command name itself.
                prefix = stripped
                user_cmds = self._user_commands()
                all_cmds = sorted(set(_BUILTIN_COMMANDS) | set(user_cmds))
                for name in all_cmds:
                    if name.startswith(prefix):
                        yield Completion(
                            name,
                            start_position=-len(prefix),
                            display=name,
                        )
                return

            # `/cmd <args...>` — pick by command.
            cmd, _, rest = stripped.partition(" ")
            cmd = cmd.lower()
            arg = rest  # may be empty
            # `current` is the token we're completing (last whitespace-split).
            current = arg.rsplit(" ", 1)[-1]
            yield from self._complete_arg(cmd, current, document, complete_event)

        def _complete_arg(
            self,
            cmd: str,
            current: str,
            document,
            complete_event,
        ) -> Iterable["Completion"]:
            if cmd == "model":
                for m in self._model_ids():
                    if m.startswith(current):
                        yield Completion(m, start_position=-len(current))
            elif cmd == "effort":
                for lvl in _EFFORT_LEVELS:
                    if lvl.startswith(current.lower()):
                        yield Completion(lvl, start_position=-len(current))
            elif cmd in ("auto", "speak"):
                for opt in ("on", "off"):
                    if opt.startswith(current.lower()):
                        yield Completion(opt, start_position=-len(current))
            elif cmd == "fast":
                # `on/off` first; user can also type a model id.
                for opt in ("on", "off"):
                    if opt.startswith(current.lower()):
                        yield Completion(opt, start_position=-len(current))
                for m in self._model_ids():
                    if m.startswith(current):
                        yield Completion(m, start_position=-len(current))
            elif cmd == "forcetool":
                # `/forcetool <tool> <prompt>` — only complete the first arg.
                if " " not in document.text_before_cursor[len("/forcetool "):]:
                    for tname in sorted(self.agent.tools):
                        if tname.startswith(current):
                            yield Completion(tname, start_position=-len(current))
            elif cmd == "goal":
                for opt in ("clear",):
                    if opt.startswith(current.lower()):
                        yield Completion(opt, start_position=-len(current))
            elif cmd == "predict":
                # First arg slot: "clear" or "file" subverbs.
                if " " not in document.text_before_cursor[len("/predict "):]:
                    for opt in ("clear", "file"):
                        if opt.startswith(current.lower()):
                            yield Completion(opt, start_position=-len(current))
                else:
                    # `/predict file <path>` — filesystem completion for the path.
                    from prompt_toolkit.document import Document
                    sub = Document(text=current, cursor_position=len(current))
                    yield from self._path.get_completions(sub, complete_event)
            elif cmd in ("image", "img", "audio"):
                # Filesystem completion for paths. We delegate to
                # PathCompleter but rewrite the document so it sees the
                # path token rather than the whole `/image <path>` line.
                from prompt_toolkit.document import Document

                # Compute where the path arg starts so PathCompleter's
                # `start_position` lines up with the editing cursor.
                #
                # The user types `/image partial`, cursor at end. We hand
                # PathCompleter a document of just `partial` so its
                # negative start_position refers to chars inside the path
                # token, not the whole REPL line.
                sub = Document(text=current, cursor_position=len(current))
                yield from self._path.get_completions(sub, complete_event)

        def _user_commands(self) -> list[str]:
            try:
                from evi.commands import CommandStore

                return [e.name for e in CommandStore().list()]
            except Exception:
                return []

    return _EviCompleter


# --- keybindings ---------------------------------------------------------


def _build_key_bindings(bindings: dict[str, str] | None = None):
    """Build a prompt_toolkit `KeyBindings` from ~/.evi/keybindings.toml.

    Each bound key replaces the current line with its command and submits it.
    A binding that prompt_toolkit rejects (bad key name) is skipped so one
    typo can't take down the editor. Returns None when there are no bindings.
    """
    if bindings is None:
        from evi.keybindings import load_keybindings

        bindings = load_keybindings()
    if not bindings:
        return None

    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    def _make(command: str):
        def handler(event) -> None:
            buf = event.app.current_buffer
            buf.text = command
            buf.cursor_position = len(command)
            buf.validate_and_handle()  # submit, as if the user pressed Enter

        return handler

    import logging

    log = logging.getLogger(__name__)
    for key, command in bindings.items():
        try:
            kb.add(*key.split())(_make(command))
        except Exception as exc:  # invalid key name → skip, keep the rest
            log.warning("skipping keybinding %r (%s)", key, exc)
    return kb


# --- ReplInput facade ----------------------------------------------------


class ReplInput:
    """Wraps a prompt_toolkit `PromptSession`, with a degraded fallback.

    Usage:

        repl = ReplInput(agent)
        line = repl.read("you > ")   # blocks until \\n or Ctrl-D
    """

    def __init__(self, agent: "Agent", history_path: Path | None = None) -> None:
        self.agent = agent
        self._fallback = False
        self._session = None
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        except ImportError:
            self._fallback = True
            return

        hist_path = Path(history_path) if history_path is not None else HISTORY_PATH
        # File creation is deferred until first write, but the parent dir
        # has to exist for FileHistory to open it.
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        completer_cls = _completer_class()
        self._session = PromptSession(
            history=FileHistory(str(hist_path)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer_cls(agent),
            complete_while_typing=False,  # only on Tab — don't pop a menu mid-chat
            key_bindings=_build_key_bindings(),
        )

    def read(self, prompt: str) -> str:
        """Read one line from the user, blocking. Raises KeyboardInterrupt
        on Ctrl-C and EOFError on Ctrl-D, like `input()`.

        `prompt` is the rendered Rich-tagged string the REPL was passing
        to `console.input()`. We strip the Rich tags before handing it to
        prompt_toolkit — the styled rendering is gone, but the
        functionality is preserved.
        """
        if self._fallback or self._session is None:
            # Caller already prints Rich-coloured prompts via console.input
            # elsewhere; matching that here lets the fallback look identical.
            from rich.console import Console

            return Console().input(prompt)
        return self._session.prompt(_strip_rich(prompt))
