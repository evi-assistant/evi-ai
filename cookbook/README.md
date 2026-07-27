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

## Automation

- [Review a pull request in CI](headless-ci-review.md) — run eVi headless in
  GitHub Actions and fail the build on real findings. *(CLI + SDK)*
- [Guard an agent's inputs and outputs](guardrails.md) — layer regex, an
  LLM judge, and an offline classifier over every turn. *(config)*

## Contributing a recipe

A recipe is one Markdown file in this directory. Keep it to a single task, lead
with a one-line summary and a **Uses:** line (CLI / SDK / config), and link to a
runnable script in `examples/` rather than pasting a long program inline. Add it
to the list above under the right heading — the site build asserts every recipe
is linked, so an orphaned file fails CI.

On merge to `main`, recipes render to
[evi-ai.dev/cookbook](https://evi-ai.dev/cookbook/) automatically. (The GitHub
wiki mirrors `docs/` only; the cookbook lives on the site.)
