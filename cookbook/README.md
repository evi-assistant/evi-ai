# eVi Cookbook

Task-focused recipes for [eVi](https://github.com/evi-assistant/evi-ai) — the
local-first personal AI assistant. Each recipe is a short, self-contained guide
that solves one real problem, with commands and code you can copy and run.

Unlike an API cookbook, eVi's recipes span three surfaces: the **CLI**, the
**Python SDK** (`evi.sdk`), and **configuration** (`~/.evi/config.toml` plus a
few drop-in files). Each recipe says up front which one it uses.

Every code sample here is checked against the current release in CI — the build
fails if a recipe links to a page or script that does not exist — so what you
read matches the eVi you installed. The runnable scripts live in
[`examples/`](../examples/); recipes point at them rather than duplicating code.

## Getting started

- [Run a model on your own hardware](run-a-local-model.md) — point eVi at a
  local backend, let it pick a model that fits your machine, and chat. *(CLI)*
- [Build an agent with the SDK](agent-sdk-quickstart.md) — construct an agent in
  a few lines, give it a custom tool, and run a prompt to completion. *(SDK)*

## Give the agent more to work with

- [Package and use a skill](package-and-use-a-skill.md) — write a reusable
  instruction packet the agent loads on demand. *(CLI + config)*
- [Give the agent persistent memory](persistent-memory.md) — remember facts
  across sessions and load per-project context. *(config + CLI)*
- [Get structured JSON output](structured-json-output.md) — constrain replies to
  a schema a script can parse. *(SDK + CLI)*
- [Connect to MCP servers (and expose eVi over MCP)](connect-mcp-servers.md) —
  borrow another server's tools, or hand eVi's tools and memory to a desktop AI
  app. *(CLI + config)*

## Automation

- [Review a pull request in CI](headless-ci-review.md) — run eVi headless in
  GitHub Actions and fail the build on real findings. *(CLI + SDK)*
- [Automate with hooks](automate-with-hooks.md) — run your own command or
  webhook on lifecycle events, and veto tool calls. *(config)*
- [Guard an agent's inputs and outputs](guardrails.md) — layer regex, an
  LLM judge, and an offline classifier over every turn. *(config)*

## Scale it out

- [Fan work out across subagents](multi-agent-with-ultracode.md) — decompose,
  solve in parallel, verify, and synthesize with ultracode. *(CLI + SDK)*
- [Delegate to another machine on your LAN](delegate-to-another-machine.md) —
  keep a small local model and borrow a GPU box for heavy turns. *(config + CLI)*

## Contributing a recipe

A recipe is one Markdown file in this directory. Keep it to a single task, lead
with a one-line summary and a **Uses:** line (CLI / SDK / config), and link to a
runnable script in `examples/` rather than pasting a long program inline. Add it
to the list above under the right heading — the site build asserts every recipe
is linked, so an orphaned file fails CI.

On merge to `main`, recipes render to
[evi-ai.dev/cookbook](https://evi-ai.dev/cookbook/) automatically. (The GitHub
wiki mirrors `docs/` only; the cookbook lives on the site.)
