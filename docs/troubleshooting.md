# Troubleshooting

Common problems, in roughly the order new users hit them.

## Safe mode — is a customization breaking things?

If eVi behaves oddly and you suspect a project `EVI.md`, skill, plugin, hook,
MCP server, memory, guardrail, or custom command, run with **`--safe-mode`** (or
`EVI_SAFE_MODE=1`) to load **none** of them:

```bash
evi --safe-mode chat        # clean baseline — no customizations loaded
evi --safe-mode web         # same clean boot for the web / desktop UI
```

The REPL header shows `SAFE MODE (customizations off)`. If the problem disappears
in safe mode, re-enable customizations one at a time to find the culprit.

Safe mode is carried in the environment (`EVI_SAFE_MODE=1`), so it applies to the
whole process tree — including the desktop app's sidecar and any subagents — and
is never written to your config. It switches off:

| | |
|---|---|
| Project context | `EVI.md` / `AGENTS.md` — including on a later `/cd` |
| Config overlays | your profile **and** a repo's `.evi.toml` — safe mode is *stock config* |
| Memory | the prompt-side index **and** the `remember`/`recall` tools |
| Skills | the catalog **and** `invoke_skill` / `list_skills` |
| Hooks | `~/.evi/hooks.toml` + every plugin's hooks |
| Guardrails | `~/.evi/guardrails.toml` |
| Commands | user + plugin slash commands (and their tab-completion) |
| Plugins | `bin/` PATH wiring |
| MCP | external servers are never spawned |
| Output style | a user `~/.evi/styles/<name>.md` can override a builtin — skipped |
| Keybindings | `~/.evi/keybindings.toml` — stock keys |
| Transcripts | not written |

Inspection commands still work normally, so you can find and fix the culprit —
`evi plugin list`, `evi skill list` and `evi config show` are unaffected.

## "evi: command not found" after `pip install -e .`

The venv isn't on PATH. Either activate it
(`.venv\Scripts\Activate.ps1` on Windows,
`source .venv/bin/activate` on Linux/macOS) or invoke directly:

```bash
.venv\Scripts\evi.exe --help
```

## "LLM request failed: APIConnectionError"

eVi can't reach the backend. Check, in order:

1. Is the backend actually running?
   - LM Studio: open the app, Developer tab, **Start Server**.
   - Ollama: `ollama serve` or check `sudo systemctl status ollama`.
   - llama.cpp: `llama-server -m <path>` must be running.
2. Is `[llm] base_url` correct? Check with `evi config show`.
3. Can you hit it manually? `curl http://localhost:1234/v1/models` for LM Studio,
   `curl http://localhost:11434/api/tags` for Ollama.
4. Are you behind a proxy / VPN that's blocking localhost? Check
   `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`.

## "LLM stream failed mid-reply: …"

The connection to the backend broke while the reply was streaming (the server
dropped it, restarted or sent an error event). eVi keeps what arrived, marked
`(interrupted) The reply was cut off here: the model's stream failed.`, and a
tool call that was cut off is not run. The chat carries on: send your next
message, or re-roll the reply. If nothing of the reply had arrived, the error
reads `LLM request failed: …` instead and nothing is added to the chat; check
the backend as in the section above.

## Stopping a turn: Ctrl+C, Stop, and `evi run` exiting 130

- **`evi chat`:** Ctrl+C while a turn runs stops the turn, prints
  `interrupted` and brings the prompt back with the session intact; an open
  permission prompt is answered no. A second Ctrl+C within 2 seconds quits
  (`bye.`), and so does Ctrl+C at the idle prompt.
- **Web and desktop:** **Stop** next to Send (or **Stop turn** on an open
  permission prompt). The chat then says `Stopped — the turn was cancelled.`
- **`evi run`:** Ctrl+C or a SIGINT stops the turn and still prints a result:
  `--format json` gives the run report with `"stop_reason": "interrupted"`,
  `--format stream-json` still ends with its `RunResult` line, and text mode
  prints what streamed so far and `interrupted: the run was cancelled
  (Ctrl+C / SIGINT)` on stderr. The exit code is **130** in every format, so
  a script can tell a stopped run from a finished one. A second Ctrl+C stops
  waiting for the turn to wind down. A Ctrl+C after the turn, while eVi
  lists the files it changed, skips that list (`files_changed` is `null`)
  and keeps the report, with the stop reason and exit code the turn ended
  with.

A stopped reply keeps its text, marked `(interrupted) The reply was cut off
here: …`; running `run_command`, `run_python` and `monitor` commands have
their whole process tree killed and keep their output so far; calls that
never ran read `(cancelled) Not run: …`. The stop lands within about a tenth
of a second. A tool that ignores it, such as an MCP tool call, is given 2
seconds, then answered `(cancelled)` and left behind to finish on its own
timeout. With a delegate CLI-agent backend as the model, the CLI's own process
also runs on until its timeout (or until eVi exits). See [Stopping a
turn](features/steering.md#stopping-a-turn).

## `evi run` exited non-zero: what the exit code means

`evi run` exits with a code that says why the run stopped, the same in the
`text`, `json` and `stream-json` formats. With `--format json` (or the last
`stream-json` line) the report's `stop_reason` says the same in words, and
its `error` says what went wrong.

| Exit | `stop_reason` | What happened | What to do |
|---|---|---|---|
| 0 | `done` | The model finished. | Nothing; but see `denied_tools` below. |
| 1 | `error` | The backend failed (`LLM request failed: …`), the turn raised, or the agent couldn't be built (an MCP server that won't start, a delegate CLI-agent backend refused). | Read `error` (or the `error: …` line on stderr in text format); check the backend as in the sections above. |
| 2 | — | A usage error: no run happened. No prompt, an unknown `--format`, `--max-turns` below 1, `--resume` with `--continue`, an unknown session id or no session to continue, a `--backend` name that isn't saved (`evi backend list`), a `--cwd` that isn't a folder, a bad `--schema`, an unknown `--policy`, `--policy` with `-y`, `--scope` without `--policy` or a `--scope` entry outside the folder. | Fix the command line; the reason is on stderr. |
| 3 | `max_turns` | The run used all its model round trips (`--max-turns`, default 12) before the model finished. The text so far is still printed. | Raise `--max-turns`, or split the task. |
| 4 | `guardrail` | An input guardrail or a `user_prompt_submit` hook refused the prompt, or the backend's content filter ended the reply. | Check `~/.evi/guardrails.toml` and your hooks. |
| 5 | `checks_failed` | The project checks still failed after `[checks] max_rounds` failing rounds. | See [below](#a-run-ended-checks_failed-exit-5-or-stop_hook_exhausted-exit-6). |
| 6 | `stop_hook_exhausted` | A blocking stop hook (`protocol = "json"`) kept blocking past `[hooks] max_blocks`. | See [below](#a-run-ended-checks_failed-exit-5-or-stop_hook_exhausted-exit-6). |
| 130 | `interrupted` | Ctrl+C or a SIGINT stopped the run (above). | — |

`step_budget` and `loop_detected` (exit 3) and `permission_denied` (4) are
reserved for the task loop; no run stops with them yet.

**Tool calls were denied but the run exited 0.** That is by design: the agent
carries on past a denied call and the model answers without it, so the run is
`done`. The report lists those tools in `denied_tools` (each call has status
`denied` in `tool_calls`), and the text format prints `denied: <tools> (tool
calls the permission checks refused; …)` on stderr. A job that needs its tools
should check `denied_tools`, and approve them as the next sections describe.

**It used to exit 0.** Before the run report, `--format json` exited 0 even
when the run failed (the error was only in the JSON), and a run that ran out
of turns exited 0 in every format. A script that treated any non-zero exit as
"eVi crashed" should read `stop_reason` instead.

## A run ended `checks_failed` (exit 5) or `stop_hook_exhausted` (exit 6)

Both come from the gate a turn passes before it may end ([Verify before
done](configuration.md#verify-before-done--checks-in-configtoml)). The work
the run did stays where it did it (in its worktree, with `--worktree`).

**`checks_failed`**: the project checks for the files the turn changed (ruff
on the changed files, the targeted tests, `npm test`, `cargo check`, your
`[checks] commands`) still failed after `[checks] max_rounds` failing rounds
(default 2, so the model had one try at fixing them). `evi run` printed a
`check <name> (round N) exit N …` line on stderr for each, and the report's
`checks` lists every run with its `cmd`, `exit` and the end of its output
(`tail`).

- Read the last round's `tail`, or run the same `cmd` yourself for the whole
  output.
- `exit: null` means the check didn't finish, and its `tail` says why: it
  timed out (raise `[checks] timeout`, 300 seconds by default) or couldn't
  start (a `[checks] commands` program that isn't installed, say); both
  count as failing. A check the policy or the permission checks refused is
  recorded the same way but doesn't count. A detected tool eVi can't find
  (pytest in the folder's `.venv`, in eVi's own Python or on `PATH`) isn't
  run at all.
- To give the model more tries, raise `[checks] max_rounds`; to turn the
  checks off, set `[checks] verify = "off"`.

**`stop_hook_exhausted`**: a [blocking stop
hook](features/hooks.md#blocking-stop-hooks-protocol--json) sent the model
back `[hooks] max_blocks` times (3 by default) and then blocked once more.
The report's `stop_blocks` lists the blocks that went back to the model, each
with its reason; the last block, the one that ended the run, isn't listed. A
hook that errors or times out never blocks, so this hook really answered
"block" each time. Run it by hand with an event on stdin to see what it
objects to:

```bash
echo '{"event": "stop", "session_id": "x", "cwd": ".", "stop_hook_active": true, "iteration": 2, "files_changed": ["calc.py"]}' | python3 /path/to/gate.py
```

A gate that can never pass holds every turn until the cap: fix the hook, or
raise `[hooks] max_blocks` if the model just needs more rounds.

## `run_command` timed out, or "left a process running in the background"

`ERROR: command timed out after <N>s; its process tree was killed. Output so
far: …` means the command outlived its `timeout` (60 s by default, at most
600). eVi killed it and everything it started, and kept what it printed. Ask
for a longer `timeout`, or run a long job in the background and watch it with
`monitor`.

`(it left a process running in the background that still holds its output;
eVi stopped reading and left that process running)` means the command itself
exited, but something it started (a dev server, a build daemon) kept its
output open. eVi returns 2 seconds after the exit instead of waiting on it,
and leaves that process running on purpose. Stop it yourself when you are done
with it; a timeout or a stop would have killed it.

Commands run with no stdin, so one that waits for input reads end-of-file at
once: pass the input as an argument or a file (`--yes`, `< answers.txt`).

## The backend rejects the chat: tool call ids, roles that must alternate, a system message

Strict OpenAI-compatible servers reject a tool result that doesn't answer a
call, and Gemma or Mistral templates (llama.cpp `--jinja`, LM Studio) reject a
`system` message after the first or two user messages in a row. eVi keeps its
history within those rules and repairs each request on its way out, resumed
sessions included (see [History a strict backend
accepts](features/sessions.md#history-a-strict-backend-accepts)). If a backend
still refuses a chat, run with `EVI_DEBUG=1` (or `evi -d chat`): a
`llm.history_invalid` line names what is wrong in the stored history.

## "The model can't seem to call tools — it just describes them"

The model isn't tool-capable, or the temperature is too high.

- Use a tool-tuned model: Qwen2.5 series (especially **14B+**), Hermes-3
  on Llama 3.1, Command-R. See [docs/models.md] in the README quick
  reference.
- Avoid Gemma 2/3 and Phi-4 — their tool calling is hit-or-miss.
- Lower `[llm] temperature` to 0.3–0.4. The default 0.7 makes
  tool-call JSON noisy.
- Watch the model's chat template: some GGUF builds have the tool prompt
  baked wrong. Try a different build from a known-good repacker
  (bartowski / unsloth on HF).

## "Permission denied" for every tool call (CLI)

Auto-approval might be off and you're sitting at an `approve? y/a (this call)
/ n / s (allow all this session)` prompt that hasn't been answered. Type `y`
(or `a`) to allow that call, or `s` and then `y` to allow every call for the
rest of the session. Then revisit `auto.auto_approve` in `config.toml` for a
lasting fix.

## `run_python` asks first (or `evi doctor` says it runs unasked)

Reading code and running it are separate categories. The default
`auto_approve` holds `code_read` (`python_symbols`, `check_file` on Python,
`monitor` tailing a file), so `run_python`, `check_file` on a JavaScript,
TypeScript, Go or Rust file and `monitor` commands ask. To let `run_python`
run without asking, either:

- turn the OS sandbox on (`[tools] sandbox = true`, with `bwrap` on Linux or
  `sandbox-exec` on macOS): a sandboxed `run_python` counts as `code_read`;
- or add `code` to `auto_approve` (it covers `code_read` too), or write an
  `allow run_python` rule.

A `config.toml` saved before `code_read` existed still lists `code`, so there
`run_python` runs unasked. `evi lint` and `evi doctor` (its `code execution
approval` check) warn about it and print the fix, your list with `code_read` in
place of `code`; Settings → Permissions offers the same switch as a button. To
keep running code unasked on purpose, list `code_read` beside `code` and the
warning stops. A peer, a routine webhook or a channel push never runs code by
category either way. See [What a call counts
as](features/permissions-and-sandbox.md#what-a-call-counts-as).

## `monitor` says "this session has no shell tools"

`monitor` with `kind=command` runs a shell command, so it runs only where
`run_command` is offered: `[tools] shell = true`, or code mode (`evi chat -m
code`, the web mode switcher). With `[tools] tool_search` on, `run_command`
may still be in the deferred catalog: once the model looks it up with
`search_tools`, `monitor` commands work too. Tailing a file (`kind=file`)
needs neither.

## "PERMISSION DENIED: … This run is unattended …"

A call that would have asked was refused because nothing could ask you: a
scheduled task, `evi run` or `evi batch` without `-y`, `evi team run`,
`evi dream`, an ultracode stage, a subagent with no chat to ask through, a
workflow or recipe the web app runs, or an SDK agent built without a
`permission_callback`. Such a run denies every call that would ask unless you
opted in (`[auto] unattended_policy = "deny"` is the default). For a scheduled
task, `evi schedule list` shows the refusal as `denied: <tools> (policy deny)`
and the log's header says `# policy: deny`. For `evi run` and `evi batch`,
the run report names the policy (`"policy": "deny"`) and lists the refused
tools in `denied_tools`; `evi run`'s text format prints them on stderr. The
run still exits 0 when the model finishes without them (see the exit-code
section above).

To let the call run, from narrowest to widest:

- **Pre-approve that kind of call**: an `allow` rule for the tool
  (`rules = ["allow web_fetch"]`), a `trusted_dirs` / `trusted_domains` entry,
  or the call's category in `[auto] auto_approve` (`code` for `run_python`,
  `shell` for a `monitor` command). Your own sessions then stop asking for it
  too.
- **For one run**: `evi run -y` and `evi batch -y` approve every call of that
  run, and a routine added with `--yes` does for its webhook.
- **For every unattended run you start**: set `unattended_policy = "allow"`
  under `[auto]` in `config.toml`.
- **In your own Python code**: pass `permission_callback=` or
  `unattended_policy="allow"` to `build_agent` (see "Permissions" in
  [sdk.md](sdk.md)).

None of these opens a remote-triggered run beyond what you pre-approved: a
federated or A2A task, a routine webhook without `yes` and a channel push with
`run: true` deny the call whatever `unattended_policy` or `[auto] mode` says.
There, running code takes more: `run_python`, `run_command` and `monitor`
commands run only under an explicit allow rule (`allow run_python`), not
because `code` or `shell` is in `auto_approve`.
A Telegram turn follows `[channels] telegram_auto_approve` (the denial names
`[auto] auto_approve` because the bot's own policy fills that list). Protected
writes, secret reads and commands the destructive-command guard flags stay
denied in every unattended run; see the next section. Background: [Unattended
runs](features/permissions-and-sandbox.md#unattended-runs).

## "PERMISSION DENIED: … (the implementer policy allows …)"

The run is under `evi run --policy implementer` (or is a task's run, `evi task
start`), which allows a fixed set of calls and denies everything else without
asking. `-y` can't be combined with it, and `[auto] unattended_policy`,
`allow` rules and `auto_approve` don't widen it. The reason after `was not run:` says which rule refused the call;
with `--format json` it is each denied call's `reason` in `tool_calls`, and
the text format prints `denied: <tools> (the implementer policy refused them;
…)` on stderr. The run still ends `done` if the model finishes without the
call.

| The reason says | Why | What to do |
|---|---|---|
| `… is not on the allowlist (…)`, `only npm test`, `git … is not a read-only git command` | That command (in a line of several, the reason quotes the one that failed) isn't on the shell allowlist. | Use an allowlisted form (`python -m pytest …`, `ruff check …`, `git diff`). A project check command of your own can go in `[checks] commands` in `config.toml`: in a trusted folder it is allowed exactly as written. Installing packages, the network or a deploy script are not part of an implementer run: do them yourself. |
| `its argument … is outside the run's folder`, `… is not allowed (it writes a file of its choosing, …)` | An argument reaches outside the run's folder, or the option writes a file of its choosing, reaches the network or sets config that runs a program. | Keep paths inside the folder and drop the option. |
| `pytest @… is not allowed: pytest reads more arguments from that file, which the policy can't check` (or `ruff @…`) | pytest and ruff read more arguments from an `@file`, which the policy can't see. | Name the options and files on the command line. |
| `'…' is not in the task's files_in_scope: change only those files, and list this one as a concern in your hand-off instead (files_in_scope: …)` | A [confined run](features/permissions-and-sandbox.md#confined-runs) (every `evi task start`, or `evi run --confine`) may write only the files its scope covers, and this write, redirect or rewriting command names another. `the task names no files_in_scope, so it may change no file` means the scope is empty. | The model is told to leave the file and report it; that is the intended outcome. If the work needs the file, add it to the task's `files_in_scope` (or `--scope`) and run again. An entry is a path, a folder, or a glob where `*` crosses folders ([What a task may change](features/tasks.md#what-a-task-may-change)). |
| `it rewrites every file in '.', a folder: name the files instead`, `… names none, so it would rewrite every file in the folder it runs in`, `it rewrites every file '…' matches` | In a confined run, `ruff format` and a fixing `ruff check` (`--fix`, `--add-noqa`, a `--config` …) must name the files they rewrite, each in scope. | Run it on the files in scope (`ruff format calc.py`), or use `ruff check` / `ruff format --check` to lint without rewriting. A configured check of this kind is refused too, and doesn't fail the round. |
| `cargo --fix rewrites files across the whole crate, so the task's files_in_scope can't hold it` | `cargo check`, `cargo test` or `cargo clippy` with `--fix` can't be held to a list of files. | The model can make each fix with `edit_file`; a crate-wide `--fix` is one to run yourself and review. |
| `… sets an environment variable before the command`, `… holds a shell expansion` | `X=1 pytest`, `$VAR`, a command substitution or subshell. | Write the command out plainly. |
| `… is a protected file the task's files_in_scope doesn't name` | `EVI.md`, `AGENTS.md`, `.evi.toml`, `.evi/…`, `.github/workflows/…` or `.vscode/tasks.json`. | Name the file with `--scope`, exactly (`--scope .github/workflows/ci.yml`, or `--scope '.github/workflows/*.yml'`); `**` and other broad globs grant nothing. |
| `… is protected and no task may write it` | `.evi/tasks/**`, the eval baseline `evals/self/baseline.json`, `.git`, eVi's home, a secret file or another `[auto] protected_paths` entry. | No `--scope` grants it; do that part yourself. The baseline is recorded only with `evi eval baseline record`, from your own shell ([Evals](features/evals.md#recording-the-baseline-evi-eval-baseline-record)). |
| `… is not a trusted folder, so the implementer policy runs no commands there …` | No command runs in an untrusted folder: tests, git and even `python -m ruff` can run its code. | If you trust the repository's code, `evi trust` it from your own terminal (its worktrees share the trust) and run again. |
| `… is not a tool the implementer policy allows …` | Network, MCP, `run_python`, memory, skills and subagents are outside the policy. | By design; use a normal run for that work. |
| `the destructive-command guard: …` | The guard is always on under the policy; its opt-outs don't apply. | Do that step yourself. |

See [Implementer policy](features/permissions-and-sandbox.md#implementer-policy).

## "checks skipped: … is not a trusted folder"

The turn changed files, but eVi ran none of the project's checks: a check runs
the folder's own code (a test, a `conftest.py`, an npm script), and you
haven't [trusted](configuration.md#trusting-a-folder) this folder. `evi run`
prints the line on stderr, the report's `checks_skipped` holds the reason and
`checks` is empty. Under `--policy implementer` the same rule means the model
could run no commands at all there. If you trust the repository's code, run
`evi trust` in your own terminal and run again; a worktree shares its
repository's trust. A repository's own `[checks] commands` (in its
`.evi.toml`) also need that version of the file approved: after an edit to
`.evi.toml`, trust the folder again.

## "PERMISSION DENIED: … writes the protected path …"

The call would write a protected path: an instruction, config, hook, task or CI
file (`EVI.md`, `AGENTS.md`, `.evi.toml`, `.evi/**`, `~/.evi/**`,
`.git/hooks/**`, `.github/workflows/**`, …), the eval baseline
(`evals/self/baseline.json`, which `evi eval baseline record` writes) or a
secret file. That asks in
every mode, `yolo` and `/auto on` included, and is denied when nothing can
answer (the scheduler, `evi run -y` in CI or a script with no terminal). If an
unattended run really should write one, add its exact entry to
`[auto] protected_paths_remove`, e.g. `protected_paths_remove = ["AGENTS.md"]`;
`evi doctor` lists the protected paths in force. A reason ending in `(after a cd eVi can't follow, so where it
lands is unknown)` means the shell line `cd`s somewhere eVi can't work out
(`cd $DIR`, a wildcard, a drive switch) before writing a relative path: retry
with an absolute path or without the `cd`. See [Permissions & sandbox →
Protected paths](features/permissions-and-sandbox.md#protected-paths).

`… reads the protected path …` is the same check for a secret (`.env`,
`*.pem`, an SSH key). Unattended, the read is denied even under `-y`, `yolo`
or `[auto] unattended_policy = "allow"`; only an explicit allow rule that names
it, such as `allow read_file *.env.example`, lets it through.

## "BLOCKED … director tooling" or a denied call that names the director key

The call reached what only the director may touch: the attestation key folder
(`~/.evi/director`), the `evi-director` keyring entry, `EVI_AGENT` /
`EVI_DIRECTOR`, `evi.attest`, or director tooling (`evi task verify|reject`,
`evi attest`). eVi never marks its own work verified, so it denies such a call
in every mode, without asking, and nothing in `[auto]` (an allow rule,
`destructive_allow`, `destructive_disable_rules`, `block_destructive = false`,
`protected_paths_remove`) changes that. Run director tooling from your own
terminal, not from inside `evi chat` or through the agent. See [The director's
ground](features/permissions-and-sandbox.md#the-directors-ground).

A mention that is only data is allowed: a `grep` pattern, an `echo`, a commit
message in **double** quotes. eVi reads a shell line as cmd would too, where
single quotes don't quote, so `git commit -m 'docs: evi attest'` is denied;
write `git commit -m "docs: evi attest"`. In `run_python` code every mention
counts. On Windows, a path argument that is a device path naming no drive
(`\\?\Volume{…}`, `\\?\GLOBALROOT\…`) is denied for the same reason: use the
drive path.

## `[REDACTED:…]` in a transcript, an export or a tool's output

eVi replaced a secret with a marker naming where it came from:
`[REDACTED:OPENAI_API_KEY]` (an environment variable), `[REDACTED:llm.api_key]`
(a `config.toml` field), `[REDACTED:github_token]` (a token's shape). It does
this in transcripts, tool-call displays, `evi run`'s run report and
`stream-json` events, scheduled-task logs and crash reports; the model itself
still got the real value (and the model's own answer, `text` in the report,
is not redacted). A session resumed or revived from its transcript has the
marker in place of the value, so if the model needs it again it has to read
it again. See [Secret
redaction](features/secret-redaction.md).

If ordinary text is redacted, it is the value of an environment variable whose
name ends in `_KEY`, `_TOKEN`, `_SECRET` or `_PASSWORD` (or of a secret-named
field in eVi's config files): every such value of 12 or more characters, with
no spaces, is treated as a secret wherever it appears (placeholders such as
`sk-no-key-required`, `env:` references and paths to files that exist
aside). The marker names the variable; rename or unset it if its value isn't
secret. Transcripts written by an eVi without redaction are not rewritten.

## A command can't see my API key or token

In a run nobody is watching (a scheduled task, `evi run`, `evi batch`,
`evi workflow run`, a remote-triggered run such as a webhook, a channel push or
a federated task) and in a sandboxed run, `run_command`, `run_python` and
`monitor` commands get no environment variable whose name ends in `_KEY`,
`_TOKEN` or `_SECRET`. Use the tool's own stored login instead (`gh auth
login`, a credential helper, a config file the tool reads), or run the job
from your own chat, where commands keep the full environment. See [What a
command's environment
holds](features/permissions-and-sandbox.md#what-a-commands-environment-holds).

## "The … backend is a CLI agent that runs its own tools … won't use it for an unattended … run"

`[llm] backend` (or a `[models] *_backend`) is `codex`, `gemini`, `amp`,
`qwen` or `copilot`. Those CLIs run their own shell, file and web tools outside
eVi's permission checks, so a run nobody is watching (a scheduled task,
`evi run`, `evi batch`, `evi workflow run`, `evi team run`, a subagent with no
chat to ask, the web app's workflow, recipe, routine, channel-push, ultracode
and federation runs) refuses them; the web app answers `409`. Point the
setting the message names at a local or API backend, or set
`allow_cli_agent_unattended = true` under `[llm]` to accept the risk. The
Telegram channel refuses them even then.

## A repository's `.evi.toml` settings aren't applied

eVi applies a `.evi.toml` only in a folder you have trusted, and only the
version you trusted. The log says which case it is: `eVi: not applying project
config <path>: this folder is not trusted` or `… it is new or changed since you
trusted this folder`. Answer `y` at the prompt `evi chat` shows, or run
`evi trust` in that folder from your own terminal; `evi trust --status` shows
the state. `evi run`, `evi batch`, the web UI and the scheduler never ask. In
the web UI the settings come from the folder the server was started in, not a
session's working folder. The file is looked for only up to the repository's
top level, so a `.evi.toml`, `EVI.md` or `AGENTS.md` in a folder above the
repository doesn't apply inside it. See [Trusting a
folder](configuration.md#trusting-a-folder).

## `--worktree` refuses a branch: "checked out at …" or "differs … only in case"

An isolated run (`evi run --worktree`, see [the run
report](features/structured-and-batch.md#cli-single-headless-run)) never
edits a checkout someone may be working in, so it refuses a branch that is
checked out anywhere but its own worktree under `<repo>/.worktrees/`: your
main checkout's branch above all (`branch main is checked out at …`). Use
`--worktree auto` for a fresh `fix-<n>` branch, or another name; `--base main`
starts the new branch from `main`. `branch Main differs from the existing
branch main only in case` means the name would be the same branch on Windows
and macOS, whose file systems ignore case, so a worktree on it would move
your checkout's branch: pick another name. The same check catches a checkout
whose HEAD spells its branch in another case (after `git checkout MAIN` on
Windows); `git checkout main` there puts it right.

## A worktree "can't be locked for this run"

An isolated run locks its worktree while it runs (`git worktree list` shows
it `locked`; the reason is `eVi isolated run (pid N on HOST)`, or git's own
words on git older than 2.36), so prune leaves it alone and no second run
shares it. This message names the worktree another run still holds.

When a run is killed outright (a crash, `kill -9`, a power cut), it can't take
its lock off. `evi task start` handles that itself: the next start on the same
machine takes over a lock whose reason names this machine and a process that
no longer exists, and carries on. Its claim goes through at once when the dead
start was yours (the same holder, `<user>@<host>` by default); anyone else
waits for the lease to run out, or passes `--force`. Everything else is
refused: a live run's lock, one taken on another machine (a repository on a
shared drive), one in the older `eVi isolated run (pid N)` form, a lock you
put on yourself, and any lock in the way of `evi run --worktree`, a team task
or a workflow step. Once you are sure no run uses the worktree, unlock it with
`git worktree unlock <path>`, the command the message prints. eVi only ever
removes the lock its own runs took.

## `evi task start` exits 2

Nothing ran, and the task is exactly as it was. The `error:` line says why:

- `… is not a trusted folder, so a task can't run there`: the implementer
  policy runs no commands in an untrusted folder, so nothing could prove the
  task. Run `evi trust` in the repository from your own terminal. It refuses
  inside an eVi session and in any process an eVi command started
  (`refusing to trust a folder from inside an eVi session`).
- `task T1 is claimed by … (until …)`: someone else's claim or start holds a
  live lease. Wait for it to run out, or pass `--force` if you know that
  claim is dead.
- `task T1 is 'implemented': only an open task (or a claimed one whose lease
  ran out) can be claimed`: `evi task reopen T1` first to run it again.
- `can't open the task's worktree: …`: `branch task/T1 is checked out at …`
  means another checkout (your main one, say) has the branch: switch that one
  to another branch. `… can't be locked for this run` is [another run's
  lock](#a-worktree-cant-be-locked-for-this-run). `… is missing` means the
  worktree folder was deleted: `git worktree prune` drops its record.
- `the … backend is a CLI agent that runs its own tools …`, or
  `[models] … = '…' is a CLI agent …`: a task runs only on a local or API
  backend, whatever `[llm] allow_cli_agent_unattended` says. Pass
  `--backend` with one, or clear that `[models]` setting.
- `no saved backend named …`: see `evi backend list`.
- `task T1 names no files_in_scope, and a start may change only the files
  they cover`: a run confined to no file could change nothing. Add
  `files_in_scope` to the task file (`files_in_scope = ["evi/parser.py"]`),
  or file the task again with `--scope`. Over MCP, the same task is queued
  and its run ends in `error` with this reason.
- `required check … doesn't parse`, `files_in_scope: …` or `… can't be read
  as a task`: fix the task file.
- `another eVi is changing the task queue`: another command held the queue's
  lock for 15 seconds. Try again.

Any other exit code means the run happened and the task is back to `open`
(or `implemented`, exit 0); it means what it means for [`evi
run`](#evi-run-exited-non-zero-what-the-exit-code-means). A start that ran
but couldn't write its outcome exits at least 1 with `note: the outcome was
not recorded …`, and `evi task release T1` puts the task back to `open`.

## `evi task verify` refuses

Exit 2 means nothing was written and no review ran:

- `refusing to verify task T1: …`: the command runs where an eVi agent may
  have started it (`EVI_AGENT is set`, `inside an eVi chat session`, `an eVi
  process runs above this one`), or not as `evi task verify` itself. Run it
  in your own terminal, not through eVi. `evi task reject` and
  `evi attest init` refuse the same way.
- `only a task in needs_review can be verified`: `evi task submit T1` first,
  or `evi task reopen` a task that already has a verdict.
- `has no branch and base to review (start it first)`: the task was never
  started.
- `no worktree has task/T1 checked out`: the task's worktree was removed.
  Check the branch out again in one, for example
  `git worktree add .worktrees/task__T1 task/T1`.
- `the worktree … is not at the head of task/T1`: switch that worktree back
  to the branch (`git -C <worktree> switch task/T1`).
- `the worktree … has uncommitted changes (…): commit the work on task/T1
  first`: the implementer never commits. Review its changes, commit them on
  the branch in that worktree (or delete what shouldn't be there), then
  verify. The signature covers the commit at the branch head, so it has to
  hold the work you reviewed.
- `the task's base … names no commit here, so there is nothing to review the
  branch against`: the commit in the task's `base` field isn't in this
  repository (the field was edited, or the commit is gone). Set `base` to the
  commit `task/T1` started from; `git merge-base <the branch it started
  from> task/T1` finds it for a branch that hasn't been rebased.
- `the task's base is the head of task/T1 (…): there is no work to review`:
  the branch has no commits since its base. Commit the work on `task/T1`
  first.
- `the task's base … is not an ancestor of task/T1 at …, so a review against
  it would not cover the branch's work`: the branch was rebased onto
  something else, or the field was edited. Set `base` to the commit the
  branch now starts from.
- `task T1: task/T1 changed files outside its files_in_scope (…): undo them
  on the branch, or pass --allow-out-of-scope REASON to sign anyway`: the
  branch changes a file no `files_in_scope` entry covers, often one a
  command wrote by itself (a test's output) or one you committed along with
  the work. Verify checks the branch it signs, not the list `evi task show`
  printed after the run. Remove the file from the branch and commit, add it
  to the task's `files_in_scope` if it belongs to the task, or sign with
  `--allow-out-of-scope "…"`: the reason and the files go into the reviewer
  notes and are signed. See [What a task may
  change](features/tasks.md#what-a-task-may-change).
- `its evidence path … is not an evidence bundle eVi wrote for this
  repository …, so it is not read`: the task file names a folder that isn't
  one of eVi's bundles ([which folders a task may
  name](features/evidence.md#which-folders-a-task-may-name)). Run
  `evi evidence collect T1` to collect a bundle and record its path.
- `its evidence bundle changed since it was collected (… then, … now); collect
  it again`: something changed a file in the bundle after its hash was
  recorded. `evi evidence collect T1` collects a fresh one (it runs the
  required checks again), then verify.
- `task T1 changed meanwhile; run the command again`: the task file changed
  while the review ran.

Exit 1 means the checks above passed but nothing was signed:

- `the review gate could not run: …`: the review (`evi review --multi`)
  needs a working backend and a diff git can produce.
- The review report on stderr, then `` `evi review --multi --exit-code`
  reported blocking findings ``: fix the findings and commit, or pass
  `--reason "…"` to sign anyway; the reason is recorded in the attestation.
- `there is no director key on this machine`: run `evi attest init`.
- `there is no terminal to confirm on`: run it in a terminal, or pass
  `--yes` from your own shell.

## A task shows `unverified (…)`

`evi task list` and `evi task show` show a verdict only with a valid director
attestation at the branch's current head. `unverified (unsigned)` means the
file says `verified` or `rejected` but nobody signed it; `stale` means the
branch moved on after it was signed; `invalid signature: …` means the task
file or its evidence changed after signing, or another key signed it;
`no director key here …` means this machine has no director key, or the
command ran where an agent may have started it. The full list is in [Task
queue](features/tasks.md#how-a-tasks-status-reads). A verdict can't be
refreshed in place: `evi task reopen` sends the task back to `open`, and it
reaches `needs_review` again only through another `evi task start`.

## `evi eval run` exits 1 or 2

**Exit 2: nothing ran**, and no report was written. The `error:` line says
why:

- `no eval suite named '…' (looked in …)`: the suite isn't in
  `~/.evi/evals/`; `evi eval list` shows what is.
- `… case N (…): unknown key '…' (a typo would silently pass)`, `two cases
  are named …`, `bad … regex`: suites are parsed strictly; fix the file
  ([every case key](features/evals.md#every-case-key)).
- `skipped: … is not a trusted folder, so its evals/self suites are skipped
  …`: the repository's `self` suite runs commands the repository provides.
  If you trust its code, `evi trust` it from your own terminal.
- `not inside a git repository, so there is no evals/self suite`, `no eval
  suites in …`: run it from the repository, or pass `--repo DIR`.
- `no saved backend named …`: see `evi backend list`, or use `--backend
  fake`.
- `no case left to run after --tag / --skip-tag`: the filters left out
  every case.
- `--partial only applies with --baseline`: `--partial` says which baseline
  cases a filtered run is excused from; without `--baseline` there are none.
- `… is not an eval baseline: …`: the `--baseline` file doesn't match the
  baseline schema; record it with `evi eval baseline record`.
- `--baseline holds …'s pass rates (backend …); a --backend fake run
  replays scripts and can't be held to them`: drop `--baseline`, or run on
  the real backend.
- `the … backend is a CLI agent that runs its own tools, outside the
  implementer policy an eval runs under`, or `… won't use it for an
  unattended eval run. Point [models] … at a local or API backend`: an eval
  runs only on a local or API backend. Pass `--backend` with one, or clear
  that `[models]` setting.

**Exit 1: a case failed, or the baseline didn't hold.** Each failing row
lists its runs' reasons:

- `the workspace could not be made: git … failed in the workspace: …`: a
  fixture workspace is a git repository, so git must be installed and work.
- `the run stopped with error: …`: the backend failed (unreachable, a model
  it doesn't serve). `stopped with 'max_turns', expected 'done'`: the model
  ran out of turns; raise the case's `max_turns` if it needs more.
- `expected a call to … (… called, but the run doesn't offer it)`: an eval
  run gets only the case `mode`'s tools that the implementer policy allows
  (no git tools, `run_python`, memory, subagents or MCP tools); see [What the
  agent may do](features/evals.md#what-the-agent-may-do).
- ``command_succeeds `…` is refused by the implementer policy: …``: the
  command isn't on the policy's allowlist and never ran. Use an allowlisted
  form (`python -m pytest …`, `ruff check …`).
- ``command_succeeds `…`: exit 1`` with the output's last lines: the command
  failed after the run. `No module named pytest` means eVi's own interpreter
  lacks pytest: a bare `python` in `command_succeeds` is the one running eVi.
- `changed …, outside …`: the run changed a file `diff_touches_only` doesn't
  allow.
- `the fake script ran out after N turn(s)` or `the fake script calls …,
  which this run doesn't offer`: under `--backend fake` the case and its
  `fake` script disagree; fix the script.
- `pass rate 0.60 is below the baseline 0.80 minus 0.10`, `skipped (…), but
  the baseline holds it at …`, or `below baseline not in this run (deleted
  or renamed?)`: `--baseline` holds every case it lists, an `expected_fail`
  one too. See [The baseline gate](features/evals.md#the-baseline-gate).
- `below baseline left out by --tag / --skip-tag: …` (in `--format json`,
  `baseline cases left out by --tag / --skip-tag count as missing: …` on
  stderr): a baseline holds the whole suite, so a filter doesn't excuse a
  case it lists. Run the whole suite, or add `--partial` to hold only the
  cases that ran: the others are then listed under `baseline.excluded` and
  stderr warns `warning: --partial: not held to the baseline …`. A partial
  run proves less than a full one; gate a release on the full suite.

**A suite that passed before fails now.** `evi eval run` used to run cases in
the folder you started it in with every tool approved; each run now gets an
empty temporary git repository under the implementer policy. A case that
reads your project needs `workspace = "repo"` (a trusted repository, at
`HEAD`), or its files in `files` / `fixture`. See [Upgrading an existing
suite](features/evals.md#upgrading-an-existing-suite).

**A case shows `SKIP`.** It wasn't run, and doesn't fail the run (unless a
baseline holds it): `no fake script` or `a judge rubric needs a model` under
`--backend fake`; `workspace = "repo" needs a git repository` outside one;
`… is not a trusted folder, so no worktree of it is made` for a `workspace =
"repo"` case in an untrusted repository.

**The web panel's Run button shows `Error: …`.** It runs the suite as `evi
eval run` does, on the configured `[llm]` backend: `[llm] backend = 'fake'
is not a backend` means the configured backend is the scripted one, which
only `evi eval run --backend fake` runs; a CLI-agent backend is refused as
for `evi eval run` (above); `the suite ran, but its report could not be kept
in …` means the reports folder couldn't be written. A suite named
`self.toml` can't run from the panel at all: rename the file.

**A scheduled eval shows `fail (P/T, N skipped)`.** A scheduled run that
skipped any case fails, even when every case that ran passed. A `workspace =
"repo"` case is skipped when the scheduler's process isn't in a trusted git
repository (`evi web` and `evi scheduler` use the folder they were started
in): start it from the repository, `evi trust` that repository, or move the
case to a suite you run with `evi eval run --repo`. The log under
`~/.evi/logs/scheduled/` gives each `SKIP` reason.

**`evi eval baseline record` says `refusing to record an eval baseline: …`.**
It is director tooling: run it from your own shell, not from inside `evi chat`
or a command eVi started.

**`evi eval baseline record` says `a baseline is the whole suite, …`.** It
records every case of the suite. With `--tag` / `--skip-tag` it refuses:
record the whole suite, and hold a subset to it later with `evi eval run
--baseline … --tag … --partial`. With `self/<file>` (one file of
`evals/self`) it refuses to replace `evals/self/baseline.json`: record
`self`, or pass `--out` to write that file's baseline elsewhere.

**`evi models recommend --verify` exits 2** with
`… lives in eVi's source repository, not in an installed eVi`: run it in a
trusted checkout of eVi, point `--repo` at one, or pass `--suite` with one of
your own suites; it exits 1 when the backend serves none of the recommended
models (`evi models pull <id>` one first).

## `evi worktree prune` keeps a worktree

Prune removes only a worktree whose work is safe elsewhere, and prints why it
kept each other one:

- `locked`: a run is using it, a killed run left its lock
  ([above](#a-worktree-cant-be-locked-for-this-run)), or you locked it;
- `N uncommitted or untracked file(s)`: commit the work there, or delete it;
- `branch fix-1 is not merged into HEAD`: merge the branch first, or pass
  `--into BRANCH` for the branch it was merged into;
- `detached HEAD`: there is no branch to keep its commits, so check one out
  there (`git switch -c <name>`) or remove the worktree yourself;
- `it holds another worktree (…)`: removing it would delete that one too;
- `its directory is missing`: `git worktree prune` drops the leftover record;
- `git did not remove it: …`: git's own check refused (prune never passes
  `--force`), or a file in it is in use (Windows).

`--into` defaults to `HEAD` of the folder you run prune in, so run it from the
main checkout: from inside a worktree, that worktree's own branch counts as
merged. `evi worktree prune --dry-run` lists what it would remove.

## An isolated workflow step, team task or subagent can't use `monitor`, git or MCP tools

That's by design. These runs share eVi's process with other work, and
`monitor`, the git tools and MCP servers work in the folder eVi was started
in, not the run's worktree, so they would reach the checkout the run is kept
out of. eVi leaves them out of such runs. Run the task with `evi run
--worktree` instead, which moves the whole process into the worktree and keeps
every tool. For the same reason a delegate CLI-agent backend (`codex`,
`gemini`, `amp`, `qwen`, `copilot`) is refused there with `the … backend is a
CLI agent that runs its own tools in eVi's process folder, so it can't be held
to a worktree here`. See [Agents → Isolated
runs](features/agents.md#isolated-runs-git-worktrees).

## The web app won't run a workflow: "… ask for isolate = "worktree", which this runner can't honour"

A step of that workflow says `isolate = "worktree"`, and for now only
`evi workflow run` can give a step a worktree of its own. Dispatch answers
`400` before any step runs rather than run it in the server's folder. Run the
workflow with `evi workflow run <name>`, or drop `isolate` from the step.

## "eVi: unknown settings in … config.toml"

A key in `config.toml` (or the active profile) isn't one this eVi knows: a
typo, or a setting from a newer version. It is ignored but kept in the file,
and eVi starts normally. Fix the spelling, or leave it if a newer eVi on
another machine shares the file.

## "Your settings file couldn't be read" / "config.toml couldn't be read"

`config.toml` is there but isn't valid TOML (a typo, a missing `]` or quote,
a half-finished edit) or can't be read. eVi starts anyway, on its **default
settings**: the safe baseline (tools ask first, unattended runs are denied, the
web server stays on this machine, no channel runs). The web and desktop app show
a red banner with the reason and the line, and the CLI prints a warning.

Your own settings (model, backends, rules, tokens) aren't applied until the
file is fixed, and eVi **won't save over it**: a change in Settings answers
"the change wasn't saved" (HTTP 409), `evi …` commands that save print one
`error:` line and exit 1. Nothing in the file is lost. Fix the line the error
names (or move the file aside to start fresh), then reload the app or rerun
the command.

## Chats compact early (or overflow) after switching models

eVi compacts a conversation at `compact_when_pct` of `[llm] context_size`.
The managed llama.cpp runtime sets that to its server's window (32K, 16K or
8K), and gives your own value back when you switch to another backend from the
web or desktop app. After a switch made with the CLI (`evi backend use`, …),
the old window stays until the next `evi web` or desktop start; start one, or
set `context_size` yourself. See [`context_size` and the managed llama.cpp
runtime](configuration.md#context_size-and-the-managed-llamacpp-runtime).

## Gemini backend: "Login with Google" no longer works

Since 2026-06-18 Google doesn't serve Gemini CLI to free, Google AI Pro or
Google AI Ultra "Login with Google" accounts. Set `GEMINI_API_KEY` (a key from
<https://aistudio.google.com/apikey>) or Vertex AI, then run `evi doctor`; see
the Gemini section of [configuration.md](configuration.md). A Gemini Code
Assist Standard / Enterprise login still works.

## ComfyUI image generation hangs or times out

The default 5-minute poll ceiling is generous, but a stuck workflow won't
get unstuck. Check:

1. ComfyUI itself is responsive: `curl http://localhost:8188/system_stats`.
2. The checkpoint named in `[comfy] default_checkpoint` exists in
   ComfyUI's models directory. If not, the workflow fails immediately —
   tail ComfyUI's terminal for the actual error.
3. The sampler/scheduler combo is valid (`euler` + `normal` is safe).

## `evi mcp serve` refuses the agent category

`evi mcp serve` and `evi mcp serve-config` exit 2 with one `error:` line:

- `refusing to serve the agent category over HTTP without --token …`: over
  `--http` the task API needs a bearer token, even on 127.0.0.1. Pass
  `--token`, or serve it over stdio (what a desktop client spawns).
- `refusing to serve the agent category beside git, …: while a task runs, this
  server works in its worktree …`: only `memory`, `calendar` and `web` may be
  served with `agent`. Serve the other categories from a second
  `evi mcp serve` (a second `mcpServers` entry with another name).
- `the agent category serves a repository's task queue: the task queue lives
  in a git repository: …`: the server started outside a repository. Pass
  `--repo DIR`, or make the snippet with `evi mcp serve-config --categories
  …,agent` inside the repository, which adds `--repo` for you.

Settings → MCP → Serve in the web or desktop app answers ``could not build
MCP server: the agent category is served only by `evi mcp serve --categories
...,agent` …`` (and its snippet request `the 'agent' category isn't offered
here …`): the app never serves it. Use `evi mcp serve-config` in the
repository.

Once it runs, a `task_start` the server can't take comes back as an
`ERROR: …` result, and nothing is queued:

- `… is not a trusted folder, so a task can't run there`: run `evi trust` in
  the repository from your own terminal.
- `task T1 is claimed by … until …`: another start or server holds a live
  lease. A server that died lets go within 10 minutes.
- `task T1 is 'implemented': only an open task (or a claimed one whose lease
  ran out) can be started`: review it, or `evi task reopen T1` from your own
  shell to run it again.
- `task T1 is already running as run …` (or `queued`): follow that run with
  `run_status`.
- `the run queue is full (16 runs wait)`: start it once one has run.
- `no saved backend named …`: see `evi backend list`.

A run that ends `cancelled` with `cancel_reason` `the run went past
[tasks] max_run_minutes (60)` took longer than that setting allows; raise it
in `config.toml` ([Task runs over
MCP](configuration.md#task-runs-over-mcp--tasks-in-configtoml)).

## MCP server says "spawned but no tools registered"

The server probably failed to start. Tail eVi's stderr or check
`~/.evi/logs/` for `MCP server <name> failed to start: …`. The reason names
the actual error, such as `HTTP 401 Unauthorized` (a remote server rejected
the `headers` you gave it) or `ConnectError: All connection attempts failed`
(nothing is listening at the `url`):

- `npx` is missing → install Node.js.
- The MCP server package isn't installable → try
  `npx -y <package>` manually to see the error.
- A remote (`transport: http`) server fails on every connect → upgrade eVi:
  older versions passed its headers in a way current mcp SDKs reject. The
  `[mcp]` extra now needs mcp 1.30 or newer.
- Server start needs a long handshake → bump
  `MCPBridge.run(timeout=…)` if you're hacking on the code.

Confirm what *did* register: `evi mcp list-tools`.

## Tauri desktop window stays on the loading shim

If the server process exits while starting, the window says so ("eVi's server
stopped while starting"), with the exit code, where its log is
(`<eVi home>/logs/desktop-server.log`, which follows `EVI_HOME`) and the log's
last lines. If it keeps spinning instead, the server is still starting or
hung. Common reasons:

- `py -3.13` not on PATH. Override with `EVI_PYTHON=python3.13`.
- `EVI_REPO_ROOT` couldn't be detected (binary not next to the repo).
  Set it explicitly.
- The web extras aren't installed: `pip install -e '.[web]'`.
- For thin-client mode, set `EVI_REMOTE_URL=http://server:8000` to skip
  the spawn entirely.

Tail the Tauri stderr to see what `py -3.13 -m uvicorn …` actually did.

## The Companion is quiet, skips check-ins or refuses a proposal

Start with `evi companion status` (or Settings → Companion): it says whether
the Companion is on, when it last checked in and when the next one is due.
Every check-in, notification and refusal is also a line in
`~/.evi/companion/audit.jsonl`. See [the Companion](features/companion.md).

**Nothing runs at all.**

- It is off: `evi companion on`. Only `enabled = true` in your own
  `config.toml` counts; an active profile can switch it off, and a
  repository's `.evi.toml` is ignored for `[companion]`.
- No scheduler runs: start `evi scheduler`, or keep `evi web` / the desktop
  app open (see [Scheduler / cron tasks aren't
  firing](#scheduler--cron-tasks-arent-firing)). The panel says so when the
  server it talks to has none.
- You just switched it on with `evi companion on` (or edited `config.toml`)
  while `evi web` or the desktop app was running: that server picks the
  change up within a minute. The switch in Settings → Companion applies at
  once.
- Safe mode is on: nothing of the Companion runs there.

**A check-in was skipped** (`evi companion check` prints why; the panel
shows it under Check now):

| Says | Meaning | What to do |
|---|---|---|
| `unchanged` | Its sources read the same as last time and your instructions didn't change, so no model was asked. | Nothing: that is the saving. Change `instructions.md` or wait for a change. |
| `busy` | Another check-in or brief was running (`evi scheduler` and `evi web` share one lock). | Try again in a minute. A lock left by a process that died is taken over after two hours. |
| `no sources` | None of `[companion] sources` is available here: no calendar set up (`evi calendar`), the MCP server isn't running in this process. | Set the source up, or list another. An entry that is no source at all (a write or shell tool) is an error in the check-in's result. |
| `disabled` | Off, or safe mode. | As above. |

**Items reach the feed but no notification arrives.** Each feed item says why
it wasn't notified (`not notified: …`):

- `below high`: under `notify_min_priority`. Lower it (`medium`) if you want
  more pings.
- `quiet hours`: inside `quiet_hours` (default `22:00-07:00`); it is
  **held** and goes out at the first check-in or brief after them (up to two
  days). An urgent item waits too unless `quiet_hours_urgent = true`.
- `daily limit reached`: `max_notifications_per_day` were sent today; held
  until tomorrow the same way.
- A duplicate (same dedupe key within `dedupe_days`) never reaches the feed.
- Notified but nothing showed: the channels are `[notify]`'s (sound,
  desktop, `url`; on Windows the desktop toast is the web app's), and
  Telegram needs `telegram = true` plus a paired owner (`evi channel status`).
  A failed send is a `notify_failed` line in the audit log.

**A proposal was refused.** The reason is on the feed item (`proposal
refused`) or, at approval, in the error (`409` in the web app, exit 2 from
`evi companion approve`):

- `… is not in [companion] allowed_actions`: list the tool, or leave it as a
  suggestion.
- `looks like a payment, transfer, trade or deletion tool`, `runs a shell
  line`, `runs a model, code or the screen`: never proposed, by design.
- `its path argument … is not absolute`, `the destructive-command guard`,
  `it writes the protected path …`, `an [auto] hard_deny or deny rule refuses
  it`, `it would overwrite the existing file …`: the checks in [What is always
  refused](features/companion.md#what-is-always-refused).
- `has expired`: older than `proposal_ttl_minutes`; it is not run.
- `its arguments changed since it was proposed`: `proposals.json` was edited
  after the proposal was made; it won't run.
- `BLOCKED BY HOOK …`: one of your `before_tool_call` hooks vetoed it.
- `refusing to approve …: … an eVi agent started this process`: approve from
  your own terminal or the web app, not from a shell eVi runs.

## Scheduler / cron tasks aren't firing

- `apscheduler` not installed: `pip install 'evi-assistant[scheduler]'`.
- The scheduler isn't running anywhere. Pick one:
  - foreground daemon: `evi scheduler`
  - alongside web: `evi web` (lifespan starts the scheduler; not in safe mode)
- The cron expression is invalid: check
  `~/.evi/logs/` — failed jobs get logged with the parse error.
- The task is disabled: `evi schedule list` shows status; flip with
  `evi schedule enable <id>`.
- It fires but its status is `denied: …`: a tool it needed would have asked,
  and a scheduled run denies that by default; see ["PERMISSION DENIED: … This
  run is unattended …"](#permission-denied--this-run-is-unattended-).

## `evi dream` reports zero transcripts to review

You ran chat sessions before `tools.transcripts` was on, or the day-dir
under `~/.evi/transcripts/` doesn't exist yet. Confirm:

```bash
evi config show | grep transcripts
ls ~/.evi/transcripts/
```

Run a quick `evi chat`, exit, then `ls ~/.evi/transcripts/<today>/`.

## STT: "no input device" / "PortAudioError"

`sounddevice` couldn't open the default mic.

- Linux: install PortAudio (`sudo apt install libportaudio2`).
- Windows: confirm the mic isn't disabled in System → Sound. Privacy
  settings → Microphone → "Allow desktop apps" on.
- macOS: grant terminal Microphone permission in System Settings → Privacy.

## Computer-use clicks miss the target

Multi-monitor or HiDPI scaling. Get a known-good baseline:

```bash
# In eVi chat:
> take a screenshot and read the file path back to me
> use screen_size to tell me the resolution
```

Coordinates are pixels on the **primary** display. For non-primary
monitors or fractional scaling, you may need to compute offsets manually.

## Tests hang or fail with "git: unknown switch -b"

Your git is <2.28. Upgrade:

```powershell
winget install --id Git.Git -e
```

The worktree tests use `git -c init.defaultBranch=main init` as a
broader-compat fallback, so they should pass on git ≥2.5.

## "ImportError: cannot import name 'X' from 'evi.…'" after pulling new code

You're running the editable install but Python is caching old bytecode.
Either delete `__pycache__` dirs:

```bash
find . -name __pycache__ -exec rm -rf {} +
```

…or just `pip install -e .` again to refresh metadata.

## Where to dig deeper

- The session transcripts: `~/.evi/transcripts/<today>/<session>.jsonl`
- Tool/hook stdout via `~/.evi/logs/`
- Dream audit logs: `~/.evi/logs/dreams/`
- Scheduled task logs: `~/.evi/logs/scheduled/`
- LM Studio / Ollama have their own terminal output — keep them visible
  during dev.
