# eVi and other coding agents

This page compares eVi with the other coding agents and the local-model tools
around it. It was researched on 2026-11-19 from each product's first-party
changelogs, release notes and docs (the [sources](#sources) are listed at the
end). "New" means shipped between 2026-10-08 and 2026-11-19. Some product
groups were researched in less depth, and their older features were not
fact-checked against current docs; claims from those groups are marked
"(unconfirmed)". Products change every few days, so treat any single cell as a
snapshot.

The detailed eVi-versus-Claude-Code table is in
[claude-code-comparison.md](claude-code-comparison.md). What eVi builds next
is in [roadmap.md](roadmap.md), and what eVi does today is in
[features.md](features.md).

eVi is local-first and single-user: it targets small local models (8K to 32K
windows) on one GPU, and anything that reaches a cloud service is opt-in.
Several features below (cloud sessions, team admin, hosted review) are out of
scope for eVi by design rather than missing.

## The landscape

**Full-featured agents** (the columns of the comparison matrix below):

| Product | What it is | Latest version (date) | Licence | Local models |
|---|---|---|---|---|
| Claude Code | Anthropic's agent: CLI, Desktop Code tab, VS Code and JetBrains, cloud sessions on web and mobile, Agent SDK | CLI 2.1.328 (2026-11-19); Desktop v6.6395.1 (2026-11-18) | Proprietary (Python Agent SDK: MIT) | Not first-party: third-party providers and gateways only; Ollama's `ollama launch` routes it to Ollama models (Ollama 0.40.15, 2026-11-03) |
| Codex | OpenAI's agent: Rust CLI, Codex in the ChatGPT desktop app, IDE extension, Codex Cloud, mobile | CLI 0.170.0 (2026-11-18); app 26.1111 (2026-11-12) | CLI Apache-2.0; app and cloud proprietary | Yes: `--oss` with Ollama or LM Studio |
| OpenCode | Open-source agent with a TUI, desktop and web clients over one server | v2.3.2 (2026-11-18) | MIT (not re-verified) | Yes, through its provider catalog |
| OpenClaw | Self-hosted personal assistant and agent gateway: 25+ chat channels, Control UI, desktop and phone apps | 2026.11.6 stable; 2026.11.10-beta.1 (2026-11-19) | MIT | Yes: Ollama, managed llama.cpp, LM Studio, vLLM, SGLang and more |
| Hermes Agent | Nous Research's Python agent: CLI, TUI, desktop, webapp, one per-machine gateway for 21+ messaging platforms | 0.23.2 (2026-11-16) | MIT | Yes: one-click managed llama.cpp, Ollama, LM Studio, vLLM |
| Pi | Minimal, extensible terminal harness with print, JSON and RPC modes and a TypeScript SDK | 1.5.2 (2026-11-10) | MIT | Yes: llama.cpp router integration and OpenAI-compatible endpoints |
| Gemini CLI / Antigravity | Google's terminal agent, and its agent-first app plus CLI and SDK (Antigravity CLI replaced Gemini CLI for unpaid users) | Gemini CLI v0.70.0 (2026-11-19); Antigravity v2.33.0 (2026-11-17) | Not recorded | Not recorded |
| Cursor | IDE, CLI, cloud agents and Bugbot review | No version numbers; latest note 2026-11-06 | Proprietary | Not recorded |
| GitHub Copilot | Copilot CLI, agent mode in VS Code, review and agents on GitHub | CLI 1.0.114, VS Code 1.147 (both 2026-11-18) | Proprietary | Not recorded |

**Other terminal and desktop agents:**

| Product | What it is | Latest version (date) | Licence | Local models |
|---|---|---|---|---|
| oh-my-pi | Batteries-included fork of Pi with a Rust core, LSP, debugger, browser, voice and the Tern companion UI | 19.0.1 (2026-11-19) | MIT | Yes |
| Goose | Open-source Rust agent: desktop, CLI, MCP, recipes | v1.60.1 (2026-11-18) | Apache-2.0 | Yes: Ollama and Lemonade providers |
| Crush | Charm's terminal agent | v0.102.0 (2026-11-19) | Not recorded | Not recorded |
| Qwen Code | Qwen's terminal agent, merging with Qoder in 1.0 | v0.28.0 / 1.0.0-preview.1 (2026-11-17) | Not recorded | Custom endpoints |
| Kimi Code CLI | Moonshot's agent (the legacy kimi-cli is archived) | 2.7.0 (2026-11-19) | Not recorded | Not recorded |
| Mistral Vibe | Mistral's coding agent, now a Rust CLI plus desktop app | 3.1.3 (2026-11-17) | Not recorded | Not recorded |
| Amp | Thread-based agent with cloud "orbs" and runners | No versions (latest note 2026-11-17) | Proprietary | Not recorded |
| Factory Droid | Factory's agent CLI and app | CLI 1.15.0 (2026-11-19) | Proprietary | Not recorded |
| Augment Auggie | Augment's CLI, rewritten on the Pi agent in 1.x | 1.2.0 (2026-11-10) | Proprietary | Not recorded |
| Letta Code | Memory-first coding agent CLI | 0.34.66 (2026-11-19) | Not recorded | Has a local backend mode |
| Aider | Terminal pair programmer; no release since February | 0.86.2 (2026-02-12) | Apache-2.0 | Yes, through LiteLLM (unconfirmed) |
| Open Interpreter | Now a Codex-compatible app server | 0.0.61 (2026-11-11) | Not recorded | Not recorded |

**Editor agents and IDEs:**

| Product | What it is | Latest version (date) | Licence | Local models |
|---|---|---|---|---|
| Cline | VS Code agent plus CLI, desktop and SDK | ext v4.1.26, desktop-v0.0.49 (2026-10-26) | Apache-2.0 | Yes: a Local Models page manages Ollama and LM Studio |
| Kilo Code | VS Code, JetBrains and CLI agent on the OpenCode core | v7.8.31 (2026-11-19) | Apache-2.0 | Not recorded |
| Roo Code | VS Code extension, shut down 2026-05-15 | v3.54.0 (2026-05-15) | Apache-2.0 | n/a |
| Zed | Editor that hosts external agents over ACP | 1.29.2 (2026-11-18) | Not recorded | Bring-your-own-key models |
| Kiro | Spec-driven IDE and CLI | IDE 1.4.236 / CLI 3.5.0 (2026-11-18) | Not recorded | Not recorded |
| Windsurf, now Devin Desktop | IDE renamed into Devin's desktop app | 3.14.30825 (2026-11-18) | Proprietary | Not recorded |

**Cloud and autonomous agents:** Devin (latest note 2026-11-16, proprietary),
OpenHands (1.42.0, 2026-11-18, works with hosted and local models) and Jules
(winding down: read-only after 2026-11-11, closes 2026-12-31).

**Local model apps and servers:** Ollama (0.40.16, 2026-11-09), LM Studio
(1.2.1, 2026-11-16, now branded "Bionic" in its changelog), vLLM (v0.34.0,
2026-11-16), llama.cpp (b12554, 2026-11-19), Jan (0.8.8, 2026-10-28, ships
llama.cpp), AnythingLLM (1.20.0, 2026-11-17, built-in LLM) and Open WebUI
(0.13.4, 2026-11-17, now an agent workspace).

**Protocols:** MCP (spec 2026-07-28: stateless protocol, tasks moved to an
extension), ACP (v1 stable, v2 draft; latest stabilization 2026-11-06), A2A,
AGENTS.md and Agent Skills (the last three were not verified in this pass).

Warp and JetBrains Junie were not researched.

## What changed since October 2026

Themes that several products shipped between 2026-10-08 and 2026-11-19.

**Goal loops.** A goal that keeps the agent working across turns until a
condition is met became common. OpenCode added `/goal` that runs until a
reviewer confirms (v2.2.1, 2026-10-29), Open Interpreter added managed goal
creation (0.0.60, 2026-11-09) and Mistral Vibe added `/goal` with
edit/pause/resume and a token budget (3.1.3, 2026-11-17). Claude Code, Codex,
OpenClaw, Hermes and Antigravity already had one. Codex's plan mode gained
"clear context and implement" (0.169.0, 2026-11-13).

**Background commands and processes.** Claude Code keeps a timed-out shell
command running in the background (2.1.311, 2026-10-14). Hermes
auto-backgrounds long commands (0.22.0, 2026-10-13) and notifies on exit
(0.23.0, 2026-11-05). OpenCode can inspect background shells (v2.2.1,
2026-10-29) and moves timed-out commands to the background (v2.3.1,
2026-11-18). oh-my-pi wakes the agent on matching background output (19.0.0,
2026-11-18), and OpenClaw keeps background commands across restarts
(2026.11.10-beta.1, 2026-11-19).

**Background and durable subagents.** Claude Code runs main-conversation
subagents in the background (2.1.302, 2026-10-16) and can reopen finished ones
(2.1.319, 2026-11-06). Crush (v0.100.0, 2026-11-05) and OpenCode (v2.3.0,
2026-11-13, on by default) added background subagents; Hermes made them
survive stops (0.23.0) and durable (0.23.2, 2026-11-16); Goose can resume a
finished delegate with a follow-up (v1.59.0, 2026-11-10). Antigravity added
`/swarm` (v2.31.0, 2026-11-09) and Dynamic Workflows (v2.32.0, 2026-11-13).

**Code Mode.** The model writes a short script that calls several tools in
one step. Claude Code added it as an experiment (2.1.323, 2026-11-12); Codex
improved its helpers (0.162.0, 2026-10-08); OpenClaw added persistence
(2026.10.1, 2026-10-10); Pi refined its codemode declarations (1.2.0, 2026-10-12).

**Compaction and prompt caching.** Claude Code compacts idle sessions before
the cache expires (2.1.299-2.1.314, from 2026-10-14) and retries an empty
summary (2.1.311). Hermes prepares the next summary in the background (0.22.6,
2026-11-02) and marks uncertain claims `UNVERIFIED` (0.23.0, 2026-11-05).
OpenCode added a percentage buffer (v2.2.5, 2026-10-30) and background
compaction (v2.3.1, 2026-11-18). OpenClaw caps summary length (2026.11.5,
2026-11-17). Pi (1.5.1, 2026-11-09) and oh-my-pi (19.0.0, 2026-11-18) warm the
provider's prompt cache while idle.

**Steering and side questions.** Codex steers a running turn immediately
(0.163.0, 2026-10-15); Kimi Code injects Enter into the running turn (2.7.0,
2026-11-19); OpenClaw lets you edit queued messages (2026.11.10-beta.1). Side
questions that stay out of the main history spread: Claude Code Desktop's side
chat pane (2026-10-15), Devin side chats (2026-11-06), Factory `/side`
(1.6.0, 2026-11-07), Qwen Code `/btw` (v0.27.2, 2026-11-10), Codex side-chat
tabs (0.168.0, 2026-11-13) and Antigravity `/btw` tabs (v2.32.2, 2026-11-16).

**Permissions.** Claude Code added `denyInsteadOfPrompting` (2.1.319,
2026-11-06), blocks writes outside the working folders (2.1.314, 2026-10-30),
names what a folder will load in its trust prompt (2.1.326, 2026-11-16),
ignores broad wildcard allow rules in auto mode (2.1.326-2.1.327, 2026-11-17)
and starts in auto mode by default (2.1.328, 2026-11-19). Mistral Vibe denies
access outside the working folder (3.0.3, 2026-10-26) and asks before shell
reads of protected files (3.1.3, 2026-11-17). Codex requires explicit `--trust`
for non-interactive runs (0.169.0, 2026-11-13). LLM approval reviewers spread:
Qwen Code hardened its Auto Mode (v0.27.4, 2026-11-15) and VS Code added
custom instructions for its approval reviewer (1.147, 2026-11-18).

**Secrets.** oh-my-pi reworked redaction with stable placeholders (18.20.0,
2026-11-12); Hermes widened secret masking (0.23.0); OpenClaw redacts across
stream chunk boundaries (2026.10.2, 2026-10-12); Codex masks credentials in
remote commands (0.164.0, 2026-10-23); Claude Code tells the model not to
commit likely secret files (2.1.327); GitHub Copilot added on-demand secret
scanning for agents (2026-11-17).

**Sandboxes and remote machines.** Factory Droid shipped a Windows command
sandbox (1.11.0, 2026-11-13); Hermes added bubblewrap and Singularity plugins
(0.22.4, 2026-10-21); Claude Code hardened its sandbox (from 2.1.305,
2026-10-20). Antigravity runs remote agents (v2.29.0, 2026-11-03) and connects
over SSH (v2.33.0); Kimi Code added SSH hosts (2.7.0); Cline started cloud
sessions (v4.1.25, 2026-10-16).

**Hooks.** Hooks can now change what happens, not only veto it: Claude Code's
fail-closed `onFailure: block` (from 2.1.295, 2026-10-08), Kiro CLI hooks that
rewrite tool input and answer approvals (3.4.0, 2026-11-16), Mistral Vibe's
`pre_llm`/`post_llm` hooks (3.1.3), Hermes's blocking `pre_llm_call` (0.22.6)
and Crush's run-wide hooks that can redact output (v0.99.0, 2026-10-29).

**Chat channels.** Claude Tag posts private Allow/Deny cards in Slack threads
(2026-10-14); ChatGPT dots take texts over iMessage (2026-10-28); Devin added
Teams (2026-10-19) and reworked Slack (2026-11-12); @GitHub in Slack and Teams
went GA (2026-11-06); Crush added MCP channels (v0.102.0, 2026-11-19); Qwen
Code added WeChat, QQ, email and DingTalk adapters (v0.27.2, v0.28.0).

**Phone and remote control.** Claude Code's Remote Control streams replies to
the phone (2.1.326, 2026-11-16); Codex on mobile added a background command
pill (2026-10-22) and a picker for Cloud or a paired computer (2026-11-18); LM
Studio added remote control over LM Link (1.2.1, 2026-11-16); OpenCode added a
mobile composer (v2.2.6, 2026-11-06).

**Voice.** Codex added realtime voice calls in the TUI (0.170.0, 2026-11-18);
Cursor added voice chat with agents (2026-11-06); Antigravity added a voice
mode (v2.29.0, 2026-11-03); Hermes added live dictation (0.22.0); Goose
(v1.59.0) and oh-my-pi (18.22.1) made live voice configurable; Ollama added
speech generation (0.40.11, 2026-10-24).

**Browser use.** Copilot CLI drives the user's browser (1.0.113, 2026-11-17);
Hermes added an in-app browser with a real profile (0.23.1, 2026-11-09);
Antigravity added a built-in browser (v2.28.0, 2026-10-27).

**Review, CI, git and evals.** Claude Code's managed review got
Quick/Standard/Deep levels (2026-10-19) and `/code-review` severity labels
(2.1.328). oh-my-pi reviews GitHub PR references (18.16.0), watches Actions
runs (18.19.0) and commits only the agent's changes (18.20.1, 2026-11-14);
Letta Code watches PRs (0.34.65); Zed writes commit messages in the repo's
style (1.29.1). GitHub Copilot Evals entered public preview (2026-11-17), and
Gemini CLI added extension validation with eval tests (v0.68.0, 2026-11-10).

**Local models.** Hermes installs and manages llama.cpp with hardware ratings
(0.22.0, 2026-10-13); OpenClaw added recommended-model pickers (2026.10.5);
Jan bundles llama.cpp (0.8.5, 2026-10-08); AnythingLLM ships a built-in LLM
(1.19.0, 2026-10-26). Ollama added concurrent generation and YaRN scaling
(0.40.15, 2026-11-03) and new coding models; vLLM 0.34.0 made the extensible
KV cache the default (2026-11-16).

**Memory and automation.** Codex made memory v2 the default and imports other
agents' memories (0.165.0, 2026-10-27); Devin added nightly "dreaming"
(2026-10-09); oh-my-pi added a recall relevance filter (18.20.3). Hermes made
cron self-healing (0.22.0) with a missed-run policy (0.22.6); Mistral Vibe
added a `/loop` manager (3.1.3); OpenClaw added experimental MCP-event
triggers (2026.10.10); Devin sets reminders for itself (2026-11-11).

## Comparison matrix

**Legend:** ✅ ships · ⚠️ partial, preview or limited · ❌ not shipped ·
🚫 eVi only: decided against this cycle (see roadmap.md's
[Not doing](roadmap.md#not-doing-this-cycle) list) · — not recorded by this
research. The Claude Code column covers the CLI, Desktop and cloud sessions;
Codex covers the CLI, app and Cloud; Copilot covers Copilot CLI, VS Code agent
mode and GitHub. eVi's column describes eVi 1.1.0 plus the unreleased
phase-1 work.

### Agent loop and tools

| Capability | eVi | Claude Code | Codex | OpenCode | OpenClaw | Hermes | Pi | Gemini / Antigravity | Cursor | Copilot |
|---|---|---|---|---|---|---|---|---|---|---|
| Local models as a first-class backend | ✅ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — |
| Managed local runtime (download and run llama.cpp) | ✅ | ❌ | ❌ | — | ✅ | ✅ | ⚠️ | — | — | — |
| Goal that auto-continues across turns | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ | — | — |
| Read-only plan mode with approval | ⚠️ | ✅ | ✅ | ✅ | — | — | ❌ | ✅ | — | — |
| Todo / task list tool | ❌ | ✅ | — | ✅ | — | ✅ | ❌ | ✅ | — | — |
| Steer or queue while a turn runs | ⚠️ | ✅ | ✅ | — | ✅ | ✅ | ✅ | — | — | — |
| Retry and model fallback on provider failure | ⚠️ | ✅ | — | — | ✅ | ✅ | ✅ | — | — | — |
| Background shell commands | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — | — |
| Code Mode (script that calls tools) | 🚫 | ⚠️ | ⚠️ | — | ✅ | ✅ | ✅ | — | — | — |
| LSP diagnostics and navigation | ❌ | ✅ | — | ✅ | — | ✅ | — | — | — | — |
| Deferred tool search | ✅ | ✅ | ✅ | — | ✅ | ✅ | ✅ | — | — | — |
| Browser automation | 🚫 | ✅ | ✅ | — | ✅ | ✅ | — | ✅ | — | ✅ |
| Computer use | ✅ | ✅ | ✅ | — | ✅ | ✅ | — | — | — | — |

eVi notes: `/goal` is a prompt prefix and plan mode refuses every tool
([loop-tools-goal-loop and plan-mode-and-todo-core](roadmap.md#phase-3--safe-autonomy-effects-policy-approvals-plans-and-goals)
are planned); steering works in the web and desktop app only; fallback models
cover request setup, not a stream that breaks mid-turn; tool search is opt-in.

### Context and memory

| Capability | eVi | Claude Code | Codex | OpenCode | OpenClaw | Hermes | Pi | Gemini / Antigravity | Cursor | Copilot |
|---|---|---|---|---|---|---|---|---|---|---|
| Automatic compaction | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ |
| Background or idle compaction | ❌ | ✅ | — | ✅ | — | ⚠️ | — | — | — | — |
| Persistent memory across sessions | ✅ | ✅ | ✅ | — | ✅ | ✅ | ❌ | ✅ | — | — |
| Reads other tools' instruction files (AGENTS.md, CLAUDE.md) | ⚠️ | ✅ | ✅ | — | — | ✅ | ✅ | — | — | — |
| Session search the model can call | ❌ | — | — | — | ✅ | ✅ | — | — | — | — |

eVi notes: compaction is structured and incremental and fits small windows;
eVi reads EVI.md and AGENTS.md only; session search is the `evi sessions
search` CLI, not a model tool.

### Permissions and safety

| Capability | eVi | Claude Code | Codex | OpenCode | OpenClaw | Hermes | Pi | Gemini / Antigravity | Cursor | Copilot |
|---|---|---|---|---|---|---|---|---|---|---|
| Allow / ask / deny rules | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | — | ✅ |
| "Always allow" grants from the prompt | ❌ | ✅ | — | — | ✅ | ✅ | ❌ | — | — | — |
| LLM or classifier approval mode | 🚫 | ✅ | ✅ | — | ✅ | ✅ | ❌ | — | — | ✅ |
| Folder trust before loading project config | ✅ | ✅ | ✅ | — | — | — | ✅ | ✅ | — | — |
| OS sandbox for shell commands | ⚠️ | ✅ | ✅ | — | ✅ | ✅ | ❌ | ✅ | — | ⚠️ |
| Container, cloud or remote execution | 🚫 | ✅ | ✅ | — | ✅ | ✅ | — | ✅ | ✅ | — |
| Secret redaction in transcripts and logs | ✅ | ✅ | ✅ | — | ✅ | ✅ | — | — | — | — |

eVi notes: eVi's sandbox (bubblewrap, Seatbelt) does nothing on Windows and is
coarse; durable grants are
[approvals-v2](roadmap.md#phase-3--safe-autonomy-effects-policy-approvals-plans-and-goals);
eVi does not redact what goes to the model.

### Surfaces

| Capability | eVi | Claude Code | Codex | OpenCode | OpenClaw | Hermes | Pi | Gemini / Antigravity | Cursor | Copilot |
|---|---|---|---|---|---|---|---|---|---|---|
| Rewind conversation and code in the terminal | ⚠️ | ✅ | ✅ | ✅ | — | ✅ | ✅ | ✅ | — | — |
| Desktop app | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| Web UI | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | — |
| Editor integration (extension or ACP) | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| Turns that survive a closed tab (reattach) | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | — | — | — |
| Drive a session from a phone | ⚠️ | ✅ | ✅ | — | ✅ | ✅ | ❌ | ✅ | ✅ | — |
| Chat channels (Telegram, Slack and others) | ⚠️ | ✅ | ✅ | — | ✅ | ✅ | ❌ | — | — | ✅ |
| Voice | ⚠️ | ✅ | ✅ | — | ✅ | ✅ | ❌ | ✅ | ✅ | — |
| Side questions outside the main history (`/btw`) | ❌ | ✅ | ✅ | — | ✅ | ✅ | — | ✅ | — | — |

eVi notes: the terminal REPL has no `/rewind` (the web app has edit, branch
and re-roll, and `evi rewind` restores files); the VS Code extension is
completion plus a chat panel; Telegram is one message per turn with no
approvals; voice is the CLI voice loop and file transcription, with no
microphone in the web or desktop app.

### Extensibility, automation and review

| Capability | eVi | Claude Code | Codex | OpenCode | OpenClaw | Hermes | Pi | Gemini / Antigravity | Cursor | Copilot |
|---|---|---|---|---|---|---|---|---|---|---|
| MCP client | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ |
| Acts as an MCP server | ✅ | — | ❌ | — | ✅ | ✅ | — | — | — | — |
| Hooks | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | — |
| Skills | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ |
| Plugins and a marketplace | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Subagents | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | — | ✅ |
| Background subagents | 🚫 | ✅ | — | ✅ | — | ✅ | ❌ | ✅ | — | — |
| Scheduled tasks | ✅ | ✅ | ✅ | — | ✅ | ✅ | ❌ | ✅ | ✅ | ⚠️ |
| Webhook or event triggers | ⚠️ | ✅ | ✅ | — | ✅ | ✅ | ❌ | — | ✅ | — |
| Git worktree isolation | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ | — | — |
| Local multi-agent code review | ✅ | ✅ | ✅ | — | ✅ | ✅ | — | ⚠️ | — | ✅ |
| PR review on GitHub | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | ✅ |
| Headless / CI mode | ✅ | ✅ | ✅ | ✅ | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| Evals | ✅ | ⚠️ | — | — | ✅ | — | ✅ | ⚠️ | — | ⚠️ |
| OpenTelemetry export | ⚠️ | ✅ | ✅ | — | ✅ | — | — | — | — | — |

eVi notes: hooks observe and veto but cannot rewrite input or answer
approvals; plugins install from user-supplied index URLs without pinning;
triggers are cron schedules plus one webhook-to-recipe path; GitHub support is
a composite action that runs one prompt; OpenTelemetry covers tool calls only.
Claude Code's evals are plugin evals; Gemini CLI's and Copilot's evals cover
extensions and customizations.

## Where eVi is ahead

- **Local models are the main path, not an add-on.** eVi reads the true
  context window from Ollama, llama-server and LM Studio and budgets every
  round against it, sizes the managed llama.cpp runtime to the machine with
  hash-pinned downloads, and verifies a backend switch with a real completion
  and tool call before keeping it ([runtime.md](features/runtime.md),
  [onboarding.md](features/onboarding.md)).
- **Small-model tool-call repair.** Lenient JSON, aliases, recovery of tool
  calls written as text or XML, near-miss edit recovery and read-before-write
  keep 8K-32K models working where stricter harnesses fail.
- **Tool-result pipeline.** Results over about 15% of the window are cut to
  head and tail with a redacted spill file, stale results are masked, and
  web, MCP and calendar output is wrapped as untrusted
  ([memory-context.md](features/memory-context.md)).
- **Segment-aware shell permissions.** eVi splits a command line the way sh,
  PowerShell and cmd.exe would and needs every command to be allowed; lines it
  cannot follow (`bash -c`, `eval`, `iex`) are never approved by a specific
  rule. Most competitors match prefixes
  ([permissions-and-sandbox.md](features/permissions-and-sandbox.md)).
- **Fail-closed unattended runs.** Runs nobody watches deny by default and
  name their policy; remote-triggered runs (peers, A2A, webhooks, channel
  pushes) are pinned to deny; a named implementer policy confines writes to a
  worktree with a test-command allowlist and no network.
- **Tighten-only project config.** A repo's `.evi.toml` loads only in a
  trusted folder, can only tighten security and is pinned by hash; trust is
  per repo, so worktrees inherit it.
- **Verified work.** Director attestation signs task verdicts with a key the
  agent cannot read, and evidence bundles and provenance trailers record who
  implemented and who verified a change. No surveyed product has an
  agent-proof verification chain ([tasks.md](features/tasks.md),
  [evidence.md](features/evidence.md)).
- **Eval-gated changes.** Behavioural evals run against a recorded baseline on
  a reference local model, and `evi models recommend --verify` qualifies a
  local model on the real tool-calling suite ([evals.md](features/evals.md)).
- **Guardrails and redaction.** Four local guardrail layers (regex, LLM judge,
  classifier, guard model) on input and output, and secret redaction across
  transcripts, events, reports and evidence
  ([guardrails.md](features/guardrails.md),
  [secret-redaction.md](features/secret-redaction.md)).
- **Breadth for a local tool.** CLI-agent backends (Claude Code, Codex, Gemini
  CLI, Amp, Qwen Code, Copilot CLI) as providers, MCP in both directions,
  federation and A2A across LAN peers, ultracode, workflows, agent teams, the
  Companion proactive watcher, and memory with dream and reflect
  consolidation.
- **Web server hardening.** Origin and CSRF checks, loopback-or-token access, a
  strict CSP with a sanitizer, and a desktop shell whose IPC is narrowed to a
  few commands ([web-desktop.md](features/web-desktop.md)).

## Where eVi is behind

Gaps that several competitors shipped, with the roadmap item that covers each.

| Gap | Seen in | eVi plan |
|---|---|---|
| A broken or stalled stream ends the turn; no bounded retry or watchdog | Hermes, Cline, oh-my-pi, Claude Code | [`stream-loop-resilience-reasoning`](roadmap.md#phase-2--context-economy-and-small-model-reliability) |
| `/goal` does not auto-continue | Claude Code, Codex, OpenCode, OpenClaw, Hermes, Mistral Vibe | [`loop-tools-goal-loop`](roadmap.md#phase-3--safe-autonomy-effects-policy-approvals-plans-and-goals) |
| No background commands or process manager | Claude Code, Hermes, OpenCode, OpenClaw, oh-my-pi, Codex | [`loop-tools-background-processes`](roadmap.md#phase-3--safe-autonomy-effects-policy-approvals-plans-and-goals) |
| Plan mode cannot read files; no todo tool | Claude Code, Codex, Gemini CLI, OpenCode | [`plan-mode-and-todo-core`](roadmap.md#phase-3--safe-autonomy-effects-policy-approvals-plans-and-goals), [`plan-todo-ui`](roadmap.md#phase-3--safe-autonomy-effects-policy-approvals-plans-and-goals) |
| No "always allow this" from the prompt, no deny-with-guidance | Claude Code, Hermes, OpenClaw, Kilo Code | [`approvals-v2`](roadmap.md#phase-3--safe-autonomy-effects-policy-approvals-plans-and-goals) |
| Only two fixed named policies; no read-only or workspace profiles | Codex, OpenClaw, Claude Code | [`agent-policy-and-profiles`](roadmap.md#phase-3--safe-autonomy-effects-policy-approvals-plans-and-goals) |
| Steering is web/desktop only; no queue in the REPL or headless runs | Codex, Kimi Code, OpenClaw, Pi, Hermes | [`steer-stop-surfaces`](roadmap.md#phase-4--live-sessions-phone-and-review-surfaces) |
| A turn is tied to one browser tab; no reattach | Claude Code, Hermes, OpenClaw, Codex | [`server-owned-turns-and-input`](roadmap.md#phase-4--live-sessions-phone-and-review-surfaces) |
| No ACP agent for Zed, JetBrains or Neovim | Hermes, OpenCode, Goose, Mistral Vibe, Gemini CLI | [`acp-server`](roadmap.md#phase-4--live-sessions-phone-and-review-surfaces) |
| No per-turn diff review with revert | Cline, Claude Code Desktop, Devin, Codex | [`ux-surfaces-changes-panel`](roadmap.md#phase-4--live-sessions-phone-and-review-surfaces) |
| Telegram is one message per turn with no approvals or `/stop` | Codex mobile, Claude Remote Control, Claude Tag, Hermes, OpenClaw | [`channel-runtime-core`](roadmap.md#phase-4--live-sessions-phone-and-review-surfaces), [`channel-approvals-core`](roadmap.md#phase-4--live-sessions-phone-and-review-surfaces) |
| `ask_user` works only in the terminal | Claude Code, OpenClaw, Hermes | [`server-owned-turns-and-input`](roadmap.md#phase-4--live-sessions-phone-and-review-surfaces) |
| No session search tool or resume recap | Hermes, Claude Code, Codex | [`faithful-resume-and-session-recall`](roadmap.md#phase-5--memory-skills-and-ecosystem-behind-trust-and-review) |
| Memory has no review inbox, provenance or relevance filter | Devin, OpenClaw, oh-my-pi, Codex, Hermes | [`memory-learning-inbox`](roadmap.md#phase-5--memory-skills-and-ecosystem-behind-trust-and-review) |
| No CLAUDE.md, `@import` or path-scoped rules | Claude Code, Hermes, Codex, Pi | [`instructions-and-skills-interop`](roadmap.md#phase-5--memory-skills-and-ecosystem-behind-trust-and-review) |
| Plugins are unpinned, with no update or lockfile | Hermes, Pi, Claude Code, Mistral Vibe | [`ecosystem-extension-supply-chain`](roadmap.md#phase-5--memory-skills-and-ecosystem-behind-trust-and-review) |
| MCP has no OAuth; secrets sit in plain config | Pi, Hermes, OpenClaw | [`mcp-oauth-and-secrets`](roadmap.md#phase-5--memory-skills-and-ecosystem-behind-trust-and-review) |
| No byte-stable prefix or idle cache warming | Hermes, Pi, oh-my-pi, OpenClaw | [`prompt-prefix-and-kv`](roadmap.md#phase-2--context-economy-and-small-model-reliability) |

Not on the roadmap yet (candidates for the next cycle): LSP diagnostics and
navigation, background or idle compaction, a ripgrep-backed search with
context lines, hooks that can rewrite input and answer approvals, a scheduler
with one-shot runs and run history, review findings that are re-verified and
carry severity, a secret scan before commits, workspace-only file confinement
in every mode, a REPL `/rewind` and `@file` completion, `/btw` side questions,
and shell completion for `evi`.

On the [Not doing](roadmap.md#not-doing-this-cycle) list, with new competitor
evidence since the decision: Code Mode (six products), background subagents
(six products), an LLM approval reviewer (seven products), a Windows command
sandbox (Factory Droid) and container backends, voice dictation in the web
composer, QR pairing and Tailscale access from a phone, and a refreshed 2026
model catalog for the managed runtime. The reasons recorded there (small local
models, a single GPU slot, injection surface) still apply; the evidence
suggests revisiting them once the foundations they wait on have shipped.

## Products

### Claude Code

Anthropic's agent in the terminal, a Desktop Code tab, VS Code and JetBrains,
and cloud sessions on the web and phone, plus the Agent SDK. It ships every one
to three days; the CLI went from 2.1.294 to 2.1.328 in the window.

- Code Mode: the model writes JavaScript that makes several tool calls in one
  step (experimental, 2.1.323, 2026-11-12).
- Auto mode starts by default when no mode is set, works on Bedrock, Google
  Cloud and Foundry, and ignores broad wildcard allow rules (2.1.326-2.1.328,
  2026-11-17 to 2026-11-19).
- `permissions.denyInsteadOfPrompting` (2.1.319, 2026-11-06) and
  `blockWritesOutsideWorkingDirectories` (2.1.314, 2026-10-30).
- Idle compaction (from 2.1.299, 2026-10-14); timed-out shell commands keep
  running in the background (2.1.311).
- `ultrathink` raises one turn to the highest effort (2.1.311, 2026-10-26).
- Desktop: side chat pane, session rail with a Changes card and PR monitor,
  saved environments (v2.42304.0, 2026-10-15); HTML/PDF/video previews
  (2026-11-05); release channels (2026-11-17).
- Remote Control streams replies live to phone and browser (2.1.326,
  2026-11-16); `/code-review` severity labels and suggested fixes (2.1.328).

### Codex

OpenAI's open-source Rust CLI plus Codex inside the ChatGPT desktop app, an
IDE extension, Codex Cloud and the ChatGPT mobile app. Docs moved to
learn.chatgpt.com and are shared with ChatGPT Work.

- Immediate steering of a running turn (0.163.0, 2026-10-15).
- Memory v2 on by default and import of other agents' memories (0.165.0,
  2026-10-27).
- Plan handoff "Yes, clear context and implement" (0.169.0, 2026-11-13).
- Realtime voice calls in the TUI (0.170.0, 2026-11-18) and audio attachments
  (0.168.0).
- Terminal workspace with side-chat tabs (experimental, 0.168.0-0.169.0) and a
  multi-backend session picker with Cloud (0.167.0, 2026-11-06).
- App: crash and quit recovery that resumes local chats (26.1027-26.1106,
  2026-11-10) and composer predictions (beta, 2026-10-09).
- Mobile: background command pill (2026-10-22) and a project picker for Cloud
  or a paired computer (2026-11-18).
- `codex doctor --network-only` and a local diagnostic ZIP (0.166.0,
  2026-10-29).

### OpenCode

An open-source agent with a TUI, desktop and web clients over one server; the
repository moved from sst to anomalyco. Research used only GitHub release
notes, so older features are (unconfirmed).

- `/goal` that runs until a reviewer confirms (v2.2.1, 2026-10-29).
- Background subagents by default (v2.3.0, 2026-11-13).
- Background compaction and timed-out shell commands moved to the background
  (v2.3.1, 2026-11-18); percentage compaction buffer (v2.2.5, 2026-10-30).
- Configurable prompt cache TTLs (v2.2.0, 2026-10-27).
- `opencode run --worktree` (v2.2.6, 2026-11-06).
- Desktop plugins with themes and slots (experimental, v2.2.0-v2.2.6) and plugin
  updates from settings (v2.3.1).
- Experimental HTML artifacts (v2.2.4, 2026-10-30).

### OpenClaw

A self-hosted personal assistant and agent gateway in TypeScript: one Gateway
process connects chat channels, a browser Control UI, CLI, desktop and phone
apps, an embedded agent runtime and external harnesses.

- Durable background commands, edit queued messages and browser handoff
  (2026.11.10-beta.1, 2026-11-19).
- Advisor that periodically reviews a run (Labs, 2026.10.10, 2026-11-03).
- Knowledge Scribes and memory maintenance flows (2026.11.5-beta.1,
  2026-11-04).
- Event-triggered automations through MCP Events (experimental, 2026.10.10)
  and automation self-repair (2026.10.5, 2026-10-18).
- Managed llama.cpp OCR and vision (2026.10.10) and recommended-model pickers
  with live catalogs (2026.10.5).
- Processes and Subagents panels with targeted Stop (2026.10.5).
- Deferred MCP tools with input hints and a usage-ranked tool index
  (2026.11.5-beta.3, 2026.11.10-beta.1).
- Scheduled automatic updates with a pre-update rehearsal (2026.11.10-beta.1).

### Hermes Agent

Nous Research's open-source Python agent with a CLI, TUI, desktop app and
webapp attached to one per-machine gateway that also serves messaging
platforms, cron, kanban boards and ACP editors.

- One-click managed llama.cpp with hardware ratings and resumable GGUF
  downloads (0.22.0, 2026-10-13); long local turns on laptops (0.22.2).
- Bounded outage recovery, a turn liveness watchdog (0.22.0) and a stream
  lifetime cap (0.23.0, 2026-11-05).
- Prepared compaction (0.22.6, 2026-11-02) and `UNVERIFIED` lines in summaries
  (0.23.0).
- `/rewind` with Esc backtrack (0.23.0, 0.23.1).
- Plugin catalog of 1,050+ entries with a verified tier and auto-update
  (0.23.0).
- Approval gate additions: credential uploads, invisible Unicode, cloud
  metadata fetches (0.22.0); MCP tool costs with a spend limit (0.23.0).
- Live dictation and full-duplex voice; full i18n in 16 languages (0.22.0).
- Guided first-run onboarding chat (0.22.0).

### Pi and oh-my-pi

Pi is a minimal terminal harness (now earendil-works/pi) that leaves subagents,
plan mode, permissions and similar to extensions. oh-my-pi is a
batteries-included fork with a Rust core.

- Pi: streaming read and ripgrep-backed grep with context lines (1.5.1,
  2026-11-09).
- Pi: idle prompt cache warming and Anthropic message threads (1.5.1); an empty
  tools array is no longer sent, fixing errors on vLLM 0.20+.
- Pi: tightened system prompt (1.5.0, 2026-11-02) and cache-preserving thinking
  changes (1.3.0, 2026-10-15).
- Pi: MCP OAuth with a Client ID Metadata Document and extension update
  changelogs (1.5.0).
- Pi: published JSON schemas for its config files (1.2.0, 2026-10-12).
- oh-my-pi: `/review` from GitHub PR references (18.16.0), Actions watch
  (18.19.0) and `/commit` for the agent's own changes (18.20.1).
- oh-my-pi: secret redaction with stable placeholders (18.20.0) and path-scoped
  bash allow rules (18.20.3, 2026-11-16).
- oh-my-pi: single per-user daemon, bash notify and cache-warming strategies
  (19.0.0, 2026-11-18).

### Gemini CLI and Antigravity

Google's open-source terminal agent and its agent-first app with a CLI and SDK.
Antigravity CLI replaced Gemini CLI for unpaid users. Jules, Google's async
agent, is winding down (closes 2026-12-31). Gemini CLI's feature list was
researched thinly.

- Antigravity: remote agents and voice mode (v2.29.0, 2026-11-03).
- Antigravity: `/swarm` (v2.31.0, 2026-11-09) and Dynamic Workflows (v2.32.0,
  2026-11-13).
- Antigravity: `/btw` side-question tabs (v2.32.2, 2026-11-16) and SSH to
  remote Linux (v2.33.0, 2026-11-17).
- Antigravity: built-in browser and PDF viewer (v2.28.0, 2026-10-27) and a
  partner plugins marketplace (v2.28.2, 2026-10-29).
- Gemini CLI: atomic file operations with backup recovery (v0.64.0, 2026-10-13)
  and extension validation with eval tests (v0.68.0, 2026-11-10).
- Gemini CLI already had ACP and A2A servers, hooks, plan mode, sandboxing and
  checkpointing (unconfirmed).

### Cursor

An IDE with a CLI, cloud agents and Bugbot review. Its changelog carries no
version numbers, and features before the window are (unconfirmed).

- Origin app marketplace with 20+ integrations (2026-10-13).
- Cloud agent canvases and an Environments API (beta, 2026-10-16).
- Agent SDK GA: agents as code folders with GitHub, Slack, webhook and cron
  triggers (2026-10-20).
- Build Live Apps with hosting and security checks before publish
  (2026-11-05).
- Voice chat with agents in the IDE, web and iOS (2026-11-06).

### GitHub Copilot

Copilot CLI, agent mode in VS Code and Copilot on GitHub. Only features new in
the window were gathered.

- Browser Use in Copilot CLI (1.0.113, 2026-11-17).
- On-demand secret scanning for developers and agents, and code review with
  adaptive effort (2026-11-17).
- Copilot Evals public preview (2026-11-17).
- VS Code 1.147 (2026-11-18): assisted permissions with an LLM reviewer, skills
  over MCP, model routers (experimental) and Send Later.
- Regex in enterprise shell permission rules (CLI 1.0.111, 2026-11-13);
  subagents honour `--max-ai-credits` (1.0.112).
- @GitHub in Slack and Teams GA (2026-11-06).

### Other products

**Kiro** (IDE 1.4.236, CLI 3.5.0). CLI hooks can rewrite tool input and answer
approvals (3.4.0, 2026-11-16); chat forking from any turn (2026-11-16); Crew
Conductor mode (2026-11-19); enterprise sandbox controls and OpenTelemetry
(preview, 2026-11-17).

**Zed** (1.29.2). External agents get compaction and session notices
(1.25.2-1.29.1); the Git panel compares branches and writes commit messages in
the repo's style (1.29.1, 2026-11-18); Bedrock and bring-your-own-key models.

**Windsurf, now Devin Desktop** (3.14.30825). Agent Command Center with up to
eight side-by-side sessions (3.13.4, 2026-11-17); Devin Bots (2026-11-17); MCP
2026-07-28 support (3.10.67, 2026-10-12).

**Devin.** Memory with nightly dreaming (2026-10-09); Oncall GA (2026-10-19);
plugins and a Customize page (2026-10-30); side chats (2026-11-06); reminders
to itself (2026-11-11); review redesign with merge trains (2026-11-12); Slack
rework (2026-11-12).

**Amp.** Use a Claude plan in Amp (2026-10-10); Linear assignment
(2026-10-19); self-hosted orbs and macOS satellites (2026-10-22 to 10-29);
Spaces (2026-11-06); Boxes, always-on computers (2026-11-10); Windows desktop
(2026-11-17).

**Factory Droid** (CLI 1.15.0). Unified 1.x versioning (2026-10-29); org audit
API (1.1.0); `/side` chat (1.6.0, 2026-11-07); Windows command sandbox (1.11.0,
2026-11-13); subagent sessions nested in the picker (1.15.0, 2026-11-19).

**Crush** (v0.102.0). OSC 7501 status (2026-10-08); run-wide hooks (v0.99.0,
2026-10-29); background subagents (v0.100.0, 2026-11-05); skill toggles
(v0.101.0); MCP channels and replaceable built-in prompts (v0.102.0,
2026-11-19).

**Qwen Code** (v0.28.0, 1.0.0-preview.1). Qwen Code 1.0 preview merges Qoder
with native packages (2026-11-11); `/btw` and recaps (v0.27.2); Auto Mode
hardening (v0.27.4, 2026-11-15); WeChat, QQ, email and DingTalk channels
(v0.27.2, v0.28.0); a desktop app (2026-11-17).

**Kimi Code CLI** (2.7.0, 2026-11-19). Steering by default, `/tasks`, a
headless `kimi server`, SSH hosts, Bedrock and experimental next-prompt
predictions, all in 2.7.0; Desktop 1.4.0 hands off sessions from other agents.

**Mistral Vibe** (3.1.3). Desktop app and Rust CLI (3.0.0, 2026-10-15);
outside-workdir deny (3.0.3); `/context` (3.1.0); vim keys and lifecycle hooks
(3.1.1); `/skills` manager with version pinning (3.1.2); `/goal`, `/loop`,
`/trusted-folders` and protected-file read approval (3.1.3, 2026-11-17).

**Letta Code** (0.34.66). Model adapters through pi-ai (0.34.57); local
backend self-compaction (0.34.61); channels (0.34.63); task workers that report
PRs and blockers (0.34.64); `watch-pr` (0.34.65, 2026-11-17).

**Goose** (v1.60.1). Allowlist hardening (v1.57.0, 2026-10-28); context window
viewer (v1.58.0); resumable subagent delegates, deferred MCP tools and live
voice settings (v1.59.0, 2026-11-10); skills as a first-class operation and
steering (v1.60.0, 2026-11-17).

**Cline** (v4.1.26). Cloud sessions (preview, v4.1.25, 2026-10-16); retry
tuning (SDK v0.2.0); desktop Changes pane, PR status and Local Models page
(desktop-v0.0.49, 2026-10-26); Hub attach to running tasks (cli-v3.0.74).

**Kilo Code** (v7.8.31). Origin-scoped browser permissions (v7.8.15); Agent
Manager fans out worktree sessions (v7.8.20, 2026-10-27) and continues them in
the cloud (v7.8.26); Codestral autocomplete by default (v7.8.30).

**Roo Code and Aider.** Roo Code shut down on 2026-05-15. Aider's last release
is 0.86.2 (2026-02-12). **Augment Auggie** 1.2.0 (2026-11-10) is a rewrite on
the Pi agent with reasoning effort control and session worktrees.

**OpenHands** (1.42.0). Agent Canvas with a per-conversation Docker runtime
(1.34.0, 2026-10-27); automations explained with diagrams (1.37.0);
conversation budget meters (1.38.0); a hooks dialog (1.41.0, 2026-11-16).

**Open WebUI** (0.13.4). Lazy skill loading (0.12.4); a tabbed workspace
(0.13.0, 2026-11-03); Claude Code, Codex and Pi as models in the picker
(0.13.1); agents in automations (0.13.3); Tether to expose a local machine's
models and tools (0.13.4, 2026-11-17).

**Local model apps and servers.** Jan bundles llama.cpp with a first-run
wizard (0.8.5, 2026-10-08), added built-in subagents (0.8.7) and Trusted Hosts
for its local API (0.8.8). AnythingLLM ships a built-in LLM in Docker (1.19.0,
2026-10-26). LM Studio added an oMLX engine and in-app terminal (1.1.10),
approvals that carry over to forks (1.2.0) and remote control over LM Link
(1.2.1, 2026-11-16). Ollama added coding models such as Flint (0.40.8,
2026-10-21), speech generation (0.40.11), concurrent generation, YaRN and
`ollama launch` for coding CLIs (0.40.15). vLLM tightened its default CORS
(v0.32.0) and made the extensible KV cache the default (v0.34.0, 2026-11-16).
Open Interpreter is now a Codex-compatible app server with attested Linux
builds (0.0.61, 2026-11-11).

**Adjacent Anthropic and OpenAI products.** Claude Tag (Claude in Slack) with
private approval cards (2026-10-14); Cowork scheduled tasks and computer use;
Claude Duet, a proactive agent app (research preview, 2026-10-21); ChatGPT
dots, always-on agents that can start Codex tasks (2026-10-09) and take texts
over iMessage (2026-10-28).

**Protocols.** ACP lets clients append to the agent's system prompt
(2026-11-06). MCP's 2026-07-28 revision is stateless and moves tasks to an
extension; Claude Code (2.1.319, 2026-11-06) and Windsurf adopted parts of it.

## Sources

All fetched on 2026-11-19.

- Claude Code: [changelog](https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md), [releases](https://github.com/anthropics/claude-code/releases), [docs index](https://code.claude.com/docs/llms.txt), [Desktop docs](https://code.claude.com/docs/en/desktop), [Claude Desktop changelog](https://claude.com/docs/cowork/changelog), [release notes](https://support.claude.com/en/articles/12138966-release-notes), [Agent SDK TypeScript changelog](https://raw.githubusercontent.com/anthropics/claude-agent-sdk-typescript/main/CHANGELOG.md), [Agent SDK Python changelog](https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/main/CHANGELOG.md)
- Codex: [changelog](https://learn.chatgpt.com/docs/changelog), [what's new](https://learn.chatgpt.com/docs/whats-new.md), [docs index](https://learn.chatgpt.com/llms.txt), [CLI releases](https://github.com/openai/codex/releases)
- OpenCode: [releases](https://github.com/anomalyco/opencode/releases)
- OpenClaw: [changelog](https://raw.githubusercontent.com/openclaw/openclaw/main/CHANGELOG.md), [releases](https://github.com/openclaw/openclaw/releases), [docs index](https://docs.openclaw.ai/llms.txt)
- Hermes Agent: [releases](https://github.com/NousResearch/hermes-agent/releases), [docs index](https://hermes-agent.nousresearch.com/docs/llms.txt)
- Pi and oh-my-pi: [Pi changelog](https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/coding-agent/CHANGELOG.md), [Pi docs](https://github.com/earendil-works/pi/tree/main/packages/coding-agent/docs), [oh-my-pi changelog](https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/coding-agent/CHANGELOG.md)
- Google: [Gemini CLI releases](https://github.com/google-gemini/gemini-cli/releases), [Antigravity changelog](https://antigravity.google/docs/changelog), [Jules changelog](https://jules.google/docs/changelog)
- Cursor: [changelog](https://cursor.com/changelog)
- GitHub Copilot and editors: [GitHub changelog](https://github.blog/changelog/label/copilot/), [Copilot CLI releases](https://github.com/github/copilot-cli/releases), [VS Code updates](https://code.visualstudio.com/updates), [Zed releases](https://zed.dev/releases), [Kiro changelog](https://kiro.dev/changelog), [Devin Desktop changelog](https://docs.devin.ai/desktop/changelog)
- Terminal agents: [Crush](https://github.com/charmbracelet/crush/releases), [Qwen Code](https://github.com/QwenLM/qwen-code/releases), [Kimi CLI](https://raw.githubusercontent.com/MoonshotAI/kimi-cli/main/CHANGELOG.md), [Kimi Code](https://github.com/MoonshotAI/kimi-code/releases), [Mistral Vibe](https://github.com/mistralai/mistral-vibe/blob/main/CHANGELOG.md), [Amp news](https://ampcode.com/news), [Factory](https://docs.factory.com/changelog/release-notes), [Auggie 1.2.0](https://www.augmentcode.com/changelog/auggie-cli-1-2-0-release-notes)
- Editor and open-source agents: [Goose](https://github.com/block/goose/releases), [Cline](https://github.com/cline/cline/releases), [Kilo Code](https://github.com/Kilo-Org/kilocode/releases), [Aider history](https://aider.chat/HISTORY.html), [Roo Code README](https://raw.githubusercontent.com/RooCodeInc/Roo-Code/main/README.md)
- Autonomous agents and local apps: [OpenHands](https://github.com/OpenHands/OpenHands/releases), [Devin](https://docs.devin.ai/release-notes/overview), [Letta Code](https://github.com/letta-ai/letta-code/releases), [Open WebUI](https://github.com/open-webui/open-webui/releases), [Jan](https://github.com/janhq/jan/releases), [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm/releases), [Ollama](https://github.com/ollama/ollama/releases), [Open Interpreter](https://github.com/openinterpreter/open-interpreter/releases), [LM Studio](https://lmstudio.ai/changelog)
- Servers and protocols: [vLLM](https://github.com/vllm-project/vllm/releases), [llama.cpp](https://github.com/ggml-org/llama.cpp/releases), [MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog), [ACP updates](https://agentclientprotocol.com/updates.md)
