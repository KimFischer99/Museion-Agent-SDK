# PAS proactive plugin for Hermes

SPEC §12.1 approach B: an existing host (Hermes) consumes PAS proactive capabilities. The
plugin exposes five tools through Hermes's official registration entry point
(`register(ctx)` → `ctx.register_tool`); every operation reaches the PAS control plane over
JSON-RPC 2.0. The plugin holds no schedules, no grants and no approval state.

| Tool | PAS method | Notes |
|---|---|---|
| `proactive_schedule` | `jobs.create` | Proposes a persistent schedule; PAS accepting it is not the same as having notified |
| `proactive_status` | `jobs.list` | Reads schedules and their next due |
| `proactive_pause` / `proactive_resume` | `jobs.pause` / `jobs.resume` | Pause/resume |
| `proactive_skills_inspect` | `skills.explain` | Read-only compatibility status; import/audit are never exposed to the model |

## Configuration

Environment variables (profile-scoped, injected through the profile's `.env`):

- `PAS_RPC_URL` — PAS control-plane URL; only HTTPS or a literal loopback HTTP is accepted.
- `PAS_RPC_TOKEN` — bearer token; never handed to the model and never written to session records.

When unconfigured, tool calls return an explicit `ok:false` (fail closed) and plugin loading is
unaffected.

## Boundaries (must be preserved)

- This plugin does not expose `grants.create` / `approvals.resolve` (§14.3: trusted user UI only).
- The model cannot approve any action or rewrite a grant through this plugin.
- If the host takes over scheduler ownership, the PAS timer for the same task must be stopped
  explicitly (SPEC §3).

## Install and verify

- Contract tests: `python3 -m unittest tests.test_pas_plugin` (stub ctx plus a scripted PAS RPC
  server, no network, no model calls).
- Official Hermes entry-point validation: `hermes plugins validate examples/pas_hermes_plugin`.
- Installing into a running Hermes is the operator's decision (it needs a live PAS daemon, P7).
