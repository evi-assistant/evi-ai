# Observability (OpenTelemetry, stats, crash reports)

eVi gives you three independent ways to see what your assistant is doing — **OpenTelemetry traces/metrics** for tool calls, **local usage stats** computed from your transcripts, and **opt-in crash reporting** to a Sentry-compatible endpoint. All three are built around eVi's local-first, privacy-first posture: nothing leaves your machine unless you explicitly opt in and point it somewhere.

## Overview

| Pillar | What it does | Default | Data leaves machine? |
|--------|--------------|---------|----------------------|
| **OpenTelemetry** | Emits spans + metrics around every tool call (counts, durations) to an OTLP collector you run | OFF | Only if you set an endpoint |
| **Local stats** | `evi stats` aggregates sessions, messages, tools, busy days, and rough token volume from your transcripts | Always available (reads local files) | Never |
| **Crash reports** | Captures uncaught exceptions, scrubs them aggressively, and sends them to your Sentry/GlitchTip DSN | OFF | Only if you opt in *and* set a DSN |

Use **OpenTelemetry** when you want live dashboards/traces for tool latency and call volume (e.g. in Grafana/Jaeger/Tempo). Use **stats** for a quick local "how have I been using eVi?" summary with zero setup. Use **crash reports** when you want to know about failures in your own self-hosted error tracker without those reports carrying prompt text, file paths, or API keys.

## How it works

### OpenTelemetry (`evi/otel.py`)

- At CLI startup, `otel.init_telemetry()` runs. It reads the `[telemetry]` config and is **a no-op unless** `traces` and/or `metrics` is enabled.
- Tool calls in the agent loop are wrapped automatically. Each call runs inside a span named `evi.tool` (with a `tool.name` attribute), and after it finishes eVi records two metrics:
  - `evi.tool.calls` — a counter, tagged with `tool` and `ok` (`true`/`false`).
  - `evi.tool.duration` — a histogram in **ms**, tagged with `tool`.
- Export uses **OTLP over HTTP**. Traces go to `<endpoint>/v1/traces`, metrics to `<endpoint>/v1/metrics`. eVi never starts an exporter on its own — **without an endpoint, nothing leaves the process** (the provider is created but has no exporter attached).
- Everything degrades to a no-op: if the OTel packages aren't installed, if the feature is off, or if init fails, the `span(...)` context manager and `record_tool(...)` simply do nothing. Hot paths are safe to wrap unconditionally.
- The service name reported in the OTel `Resource` defaults to `evi` and is configurable.

### Local stats (`evi/stats.py`)

- `compute_stats()` reads your JSONL transcripts from `~/.evi/transcripts/`, iterates session files, and aggregates:
  - **sessions** and **messages** counts,
  - a **role breakdown** (user/assistant/tool/...),
  - **top tools** (counted from `tool`-role entries that carry a `tool_name`),
  - **busiest days** (top 7),
  - **approx_tokens** — a rough estimate using `chars / 4` (this is *not* a real tokenizer),
  - first/last timestamps for the date span.
- Everything stays on disk and is printed locally. This is the deliberate local counterpart to a cloud analytics dashboard, which eVi does not provide.

### Crash reports (`evi/reporting.py`)

- At CLI startup, `init_reporting()` builds a reporter and `install_excepthook()` chains it into `sys.excepthook`. Uncaught CLI errors get captured (scrubbed) and then the original traceback is still printed normally — reporting never masks the real error.
- A reporter is only "active" when **all** of these hold: reporting is enabled (config or `EVI_CRASH_REPORTS`), a DSN is present (config or `EVI_TELEMETRY_DSN`), the backend isn't `none`, and `sentry-sdk` imports successfully. Otherwise you get a no-op `NullReporter`.
- The active `SentryReporter` initializes `sentry-sdk` with `send_default_pii=False`, `server_name="evi"`, and a **scrubber** as `before_send`. The scrubber is applied to every event and is deliberately aggressive because in an AI assistant, exception messages and stack-frame locals can carry prompt text, usernames in file paths, and secrets. It:
  - drops the values of risky keys wholesale (`vars`, `env`/`environ`, `headers`, `cookies`, `authorization`, `data`, `request`, `extra`, `api_key`, `token`, `password`, `secret`, `dsn`),
  - rewrites your home directory to `<HOME>` and your username to `<USER>`,
  - redacts API-key/token patterns (OpenAI `sk-…`, GitHub `ghp_…`, Slack `xox…`, `Bearer …`) to `<redacted>`,
  - sets `server_name` to `evi` and removes the `user` (IP/id) block.
- The DSN is a write-only ingest key, so it is safe to keep in config. The backend is a swappable seam (`sentry` today; `none` disables it), selected by config rather than code.

## Setup

All three features are configured under a single `[telemetry]` section in **`~/.evi/config.toml`** (on Windows, `%USERPROFILE%\.evi\config.toml`). The home directory can be relocated with the `EVI_HOME` environment variable. Defaults (everything off) are:

```toml
[telemetry]
# Crash reporting (Sentry/GlitchTip)
crash_reports = false
dsn           = ""
backend       = "sentry"   # "sentry" | "none"

# OpenTelemetry traces/metrics
traces        = false
metrics       = false
otlp_endpoint = ""         # base OTLP/HTTP URL, e.g. http://localhost:4318
service_name  = "evi"
```

### Optional pip extras

Both exporting features need optional dependencies (eVi ships without them by default):

```bash
# OpenTelemetry traces + metrics
pip install 'evi-assistant[otel]'

# Crash reporting (sentry-sdk)
pip install 'evi-assistant[telemetry]'
```

If the extras aren't installed, the features stay inert (no-op) even if you enable them in config — they fail open rather than crashing eVi.

### Environment-variable overrides

| Variable | Effect |
|----------|--------|
| `EVI_OTLP_ENDPOINT` | Overrides `otlp_endpoint` (base OTLP/HTTP URL). Trailing slashes are stripped. |
| `EVI_CRASH_REPORTS` | Truthy (`1`, `true`, `yes`, …) / falsy (``, `0`, `false`, `no`) override for `crash_reports`. |
| `EVI_TELEMETRY_DSN` | Overrides the crash-report `dsn`. |

### Stats

No setup beyond having transcripts. Stats read `~/.evi/transcripts/`, which is populated when transcript logging is on (`[tools] transcripts = true`, the default). If transcripts are disabled, `evi stats` will report nothing.

## Usage

### `evi stats` — local usage analytics

```text
evi stats [--days N] [--json]
```

- `--days N` — only look at the last N days (`0`, the default, means all history).
- `--json` — print the raw aggregated dict as JSON instead of the formatted summary.

The human-readable output shows session/message counts, an approximate token total (in thousands), the date span, the role breakdown, the top 8 tools, and the busiest days. If there are no transcripts it prints a hint that `tools.transcripts` may be off.

### OpenTelemetry — automatic once enabled

There is no separate command. Once `traces`/`metrics` are enabled, an `otlp_endpoint` is set, and the `[otel]` extra is installed, eVi instruments tool calls automatically every time you run a chat (CLI or web). Spans and metrics flow to your collector in the background.

### Crash reports — automatic once enabled

Also no separate command. Once enabled with a DSN, eVi installs a chained `sys.excepthook` at startup; any uncaught CLI exception is scrubbed and sent, then the traceback prints as usual.

### Web UI

The web server initializes OpenTelemetry the same way as the CLI on startup, so enabling `[telemetry] traces/metrics` instruments web-driven tool calls too. (The Settings → Model & Backend page surfaces hardware/OS stats, which is separate from `evi stats` usage analytics.)

## Examples

### Example 1 — Local OTLP collector, then watch tool metrics

Install the extra, point eVi at a local OTLP/HTTP collector (the default OTLP HTTP port is `4318`), and run a chat. Spans/metrics for each tool call are exported automatically.

```toml
# ~/.evi/config.toml
[telemetry]
traces        = true
metrics       = true
otlp_endpoint = "http://localhost:4318"
service_name  = "evi"
```

```bash
pip install 'evi-assistant[otel]'

# Optional: override the endpoint for a single run without editing config
export EVI_OTLP_ENDPOINT="http://localhost:4318"

evi chat "summarize my notes from today"
```

In your backend you'll see the `evi.tool` spans plus the `evi.tool.calls` counter (tagged `tool` + `ok`) and the `evi.tool.duration` histogram (in ms, tagged `tool`), all under service name `evi`.

### Example 2 — Quick local usage report

```bash
# Formatted summary for the last 30 days
evi stats --days 30
```

```text
42 sessions · 318 messages · ~57k tokens  Apr 12 - May 09
  roles: user: 159, assistant: 142, tool: 17
  top tools: fs.read (9), websearch (5), fs.write (3)
  busiest days: 2026-05-07 (6), 2026-05-02 (5), 2026-04-29 (4)
```

For piping into other tooling, ask for raw JSON:

```bash
evi stats --json
```

```json
{
  "sessions": 42,
  "messages": 318,
  "roles": { "user": 159, "assistant": 142, "tool": 17 },
  "tools": { "fs.read": 9, "websearch": 5, "fs.write": 3 },
  "busiest_days": { "2026-05-07": 6, "2026-05-02": 5 },
  "approx_tokens": 14250,
  "first_ts": 1744483200.0,
  "last_ts": 1746748800.0
}
```

### Example 3 — Opt-in crash reporting to self-hosted GlitchTip

```toml
# ~/.evi/config.toml
[telemetry]
crash_reports = true
dsn           = "https://<key>@glitchtip.example.com/1"
backend       = "sentry"   # set to "none" to disable without removing the dsn
```

```bash
pip install 'evi-assistant[telemetry]'
evi chat "..."   # any uncaught error is scrubbed, sent, then printed locally
```

To toggle reporting per-run via environment without touching config:

```bash
EVI_CRASH_REPORTS=0 evi chat "..."          # force-disable for this run
EVI_TELEMETRY_DSN="https://<key>@..." EVI_CRASH_REPORTS=1 evi chat "..."
```

## Notes / limits

- **Off by default, fail-open everywhere.** All three pillars are inert until configured, and if the optional deps are missing or init fails, eVi continues normally — telemetry never crashes the app and crash reporting never hides the real traceback.
- **Nothing is exported without an endpoint/DSN.** OpenTelemetry needs `otlp_endpoint` (or `EVI_OTLP_ENDPOINT`); crash reports need a `dsn` (or `EVI_TELEMETRY_DSN`) *and* `crash_reports = true` *and* `backend != "none"`. Miss any of these and the feature is a no-op.
- **OTLP transport is HTTP only.** Traces are sent to `<endpoint>/v1/traces` and metrics to `<endpoint>/v1/metrics`, so point `otlp_endpoint` at an OTLP/HTTP receiver (typically port `4318`), not a gRPC (`4317`) endpoint. Trailing slashes on the endpoint are stripped automatically.
- **`approx_tokens` is an estimate.** Stats use `chars / 4`, not a real tokenizer — treat it as a rough volume signal, and note it only counts string `content` fields in transcript entries.
- **Stats depend on transcripts.** If `[tools] transcripts` is off, there's nothing to aggregate and `evi stats` will say so.
- **The crash-report scrubber is aggressive by design but not infallible.** It drops known PII-carrying keys, rewrites home/username, and redacts common key/token formats — but it can't guarantee a novel secret format won't appear. The DSN itself is a write-only ingest key, safe to keep in synced config.
- **Top-tools counting** comes from `tool`-role transcript entries that carry a `tool_name`; tools invoked in sessions that predate transcript logging won't appear.

Relevant source files: `C:\evi\evi\otel.py`, `C:\evi\evi\stats.py`, `C:\evi\evi\reporting.py`, `C:\evi\evi\config.py` (TelemetrySettings, lines 188-209), and the CLI wiring in `C:\evi\evi\apps\cli\main.py` (`stats` command ~line 4042; startup init ~lines 153-158).
