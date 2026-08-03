# Delegate to another machine on your LAN

Keep a small, fast model for everyday chat on your laptop, and hand the heavy
turns to a bigger eVi running on a GPU box down the hall — no cloud, no shared
account, just two eVi instances talking over your LAN.

> **Uses:** config (`[tools] federation`, `[federation] serve`, `~/.evi/peers.json`)
> · CLI (`evi peer …`) · **two machines** on one trusted network.

## How it works

Your laptop POSTs a self-contained task to the peer's `/api/federate` endpoint;
the peer runs it on its own agent and returns the answer text. Transport is plain
HTTP with the peer's existing web bearer token — there's no separate trust model
and no TLS, so keep peers on a LAN or VPN. Federation is **opt-in on both ends**:
the caller enables a tool, the responder opts in to serving.

## On the big box (the one doing the work)

Turn on serving and bind the web server to the LAN so peers can actually reach it:

```toml
# ~/.evi/config.toml
[web]
auth_token = "…"      # you're on the LAN now — require a token (evi web-config token rotate)

[federation]
serve = true          # answer POST /api/federate for trusted peers (403 otherwise)
```

```bash
evi web --host 0.0.0.0 --port 8473   # bind the LAN on the federation port
```

`evi web` defaults to `127.0.0.1:8000`, so both flags matter: `--host 0.0.0.0`
to leave loopback, and `--port 8473` because that's the port peers scan and the
`http://gpu-box:8473` URL below points at. Then **open port `8473` through the
firewall**. A served task runs
non-interactively and is **deny-by-default for tools** — the peer's agent refuses
any tool not already auto-approved, so delegation can't quietly trigger a shell or
network tool on the remote box.

## On the small box (the one delegating)

Enable the `delegate_peer` tool and register the peer:

```toml
# ~/.evi/config.toml
[tools]
federation = true     # enables the delegate_peer tool (off by default)
```

```bash
evi peer add gpu http://gpu-box:8473 --token <big-box web token>
evi peer list         # shows live reachability + the peer's eVi version/model
```

Prefer a file? Copy [`examples/peers.json`](../examples/peers.json) to
`~/.evi/peers.json` and fill in each `name` / `url` / `token` (`token` is optional
if the peer needs no auth).

## Delegate a task

From the CLI, one shot:

```bash
evi peer run gpu "Summarize the architecture of /srv/project and list its top 5 risks."
```

From a chat, with the tool on, the model calls `delegate_peer(peer, task)` itself
(and `list_peers` first, to pick the peer whose model fits the job — e.g. route a
vision task to the peer whose model has vision).

## Discover peers on the network

```bash
evi peer scan            # sweep the local /24 for running eVi instances (~1-2 s)
evi peer scan --port 8000
```

If a scan finds nothing with the firewall open, the peer is almost certainly bound
to `127.0.0.1` only — relaunch it with `evi web --host 0.0.0.0 --port 8473`. `evi peer scan`
warns you when **this** machine has that problem.

## Distribute a whole task list

Once peers are registered, `evi team run --peers` (alias `--distribute`) fans a
shared task list across reachable peers **plus** local — real cross-machine
parallelism instead of teammates serialising on one GPU. A peer that drops mid-run
falls back to running its task locally, so the team never stalls.

The full field reference (serving, A2A, the Peers panel, security notes) is in
[`docs/features/agents.md`](https://github.com/evi-assistant/evi-ai/blob/main/docs/features/agents.md).

## Where to go next

- [Run a local model](run-a-local-model.md) — set up the small, fast model your
  laptop keeps for everyday chat.
- [Guard an agent's inputs and outputs](guardrails.md) — add safety checks before
  work leaves your machine.
