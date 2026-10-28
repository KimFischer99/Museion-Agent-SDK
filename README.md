# Museion Agent SDK v0.1.6

Museion Agent SDK is developed based on Muse. It provides a proactive agent runtime for scheduled reminders, opt-in news research, persistent interests, and notification delivery.

The Python package is `proactive-sdk`, imported as `proactive_sdk`. The core runtime uses the Python standard library and supports model APIs, Pi, and Hermes as analysis backends.

## Getting started

Use Python 3.11+ and `venv`/`ensurepip` available. Download and extract the archive from [Releases](https://github.com/KimFischer99/Museion-Agent-SDK/releases), then run from its `runtime/` directory:

```bash
python3 app.py
```

The first run installs the bundled wheel into a local `.venv` and guides you through configuration. Choose reminder-only mode to run without a model, or configure a backend for research. Notifications go to the local inbox or your own HTTPS webhook.

Keep the terminal open while the agent runs. Press Ctrl-C to stop it. In another terminal, use:

```bash
python3 app.py check     # Check configuration without calling the model
python3 app.py status    # View execution and delivery status
python3 app.py inbox     # Read notifications
python3 app.py setup     # Reconfigure after stopping the agent
```

## Public interests

Add an interest and enable research:

```bash
python3 app.py say "I follow NASA public news"
python3 app.py say "Enable proactive notifications"
```

Research checks Bing News RSS hourly and analyzes supplied headlines and summaries. Interests and preferences are saved across restarts. To turn proactive notifications off:

```bash
python3 app.py say "Stop proactive notifications"
```

Configuration is saved in `settings.json`. State defaults to `~/.local/state/museion`, or an existing runtime `state/` directory. Keep credentials and runtime data out of Git and shared release copies.

For a direct scheduled request, use the trusted input with an explicit time and content:

```bash
python3 app.py say "2027-01-05T09:00:00-08:00 提醒我：带伞"
python3 app.py say "每天 09:00 研究 NASA公开新闻并报告"
python3 app.py say "每周一 09:00 研究 NASA 来源:nasa-public 并报告"
python3 app.py say "每天 09:00 跟进 NASA会议 有重要进展通知我"
```

Daily, weekly, and monthly times use the configured profile timezone; interval forms such as
`每隔30分钟` start from creation. Explicit source IDs must already be registered. These bounded forms require existing notification,
memory and, for public research, public-read grants as applicable; unsupported time expressions need
clarification. A successful response gives a job ID and next due time. Inspect the job and its activity
with `pas jobs show <job-id>` and `pas jobs activity <job-id>`, and use `app.py inbox` to see actual
local delivery. A report covers the configured public source's headlines and summaries, not full
articles. `examples/deployment/` describes explicitly configured notes and conversation files.

## Proactive layer v2 (this branch)

The `feat/pas-v2` branch extends the proactive runtime without changing the existing evidence
closure, delivery state machine or trusted-entry boundary:

- every interrupting proposal carries a structured `reason` (decision contract 1.1), and quiet runs
  record why they stayed quiet; `policy.require_reason` and `policy.idle_gate` are the gray
  releases for these behaviours;
- jobs run through a model-free two-stage path (fetch → candidates → analysis); `task.always_run`
  restores the older every-slot semantics, and the ledger records per-run `candidates`/`truncated`
  counters;
- new public source connectors — `RssSource`, `SitemapDiffSource`, `PageHashSource` — are
  registered programmatically in the deployment's app module (see `examples/custom_sources_app.py`),
  with HTTPS-only, explicit host allowlists and default-off explicit selection;
- delivered notifications can receive delivery-outcome feedback (`replied` / `dismissed` /
  `complained`; `ignored_24h` is host-backfill only), and `pas stats --since 7d` reports acceptance
  per decision reason and per source. Thresholds are never adjusted automatically.

`IMPLEMENTATION-v2.md` (kept out of the product tree) records per-item commands and real outputs.

## Building and extending

Build a local release from the repository root using an environment with `setuptools>=77`:

```bash
python3 tools/build_release.py
```

Use `--output <new-directory>` if `release/` already exists. The build creates separate `runtime/` and `skills/` directories. Skills are reference material and require explicit import and service configuration.

The product bundle also includes `examples/deployment/`: a Linux/systemd deployment with
file-based public sources, explicit job selection, grants and verification steps. See the
[deployment example](https://github.com/KimFischer99/Museion-Agent-SDK/tree/main/examples/deployment).
The optional Hermes plugin is shipped alongside it in `examples/pas_hermes_plugin/`.

See [examples](https://github.com/KimFischer99/Museion-Agent-SDK/tree/main/examples) for SDK integration and [compatibility notes](https://github.com/KimFischer99/Museion-Agent-SDK/blob/main/docs/COMPATIBILITY.md) for supported adapters.

## License

Original SDK code is licensed under [MIT](https://github.com/KimFischer99/Museion-Agent-SDK/blob/main/LICENSE). Muse-derived Skill references retain their source notices and licensing status. Third-party redistribution permissions have not been fully verified; see the [license inventory](https://github.com/KimFischer99/Museion-Agent-SDK/blob/main/docs/LICENSES.md).
