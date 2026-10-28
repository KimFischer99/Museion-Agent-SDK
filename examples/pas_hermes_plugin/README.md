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

- `PAS_RPC_URL` — use `unix:///var/lib/pas-demo/pas.sock` for the native daemon.
  HTTPS or literal loopback HTTP is also accepted for deployments with an HTTP bridge.
- `PAS_RPC_TOKEN` — bearer token; never handed to the model and never written to session records.
- `PAS_NOTIFY_GRANT` — comma-separated grant refs of the operator's `notify.self`
  grant (the same ref passed to `pas jobs create --grant-ref`).
  `proactive_schedule` submits them as `grant_refs`. PAS policy refuses a
  notification whose job carries no active `notify.self` grant, so a plan
  created without one is accepted and then can never speak (`grant_missing`).
  This is configuration, not model input: the model may propose a plan, but it
  cannot grant itself the right to notify anyone.

When unconfigured, tool calls return an explicit `ok:false` (fail closed) and plugin loading is
unaffected.

The optional `pre_gateway_dispatch` input hook is separate from model-facing tools. Set
`PAS_HERMES_PLATFORM`, `PAS_HERMES_USER`, `PAS_HERMES_CHAT`, and `PAS_HERMES_PROFILE` to the
exact authorized owner DM. The hook runs before gateway authentication, so it also checks the
gateway's authorization decision itself and requires a host message ID. It routes the original
user text to `proactive.input`; neither a model-supplied actor nor a model tool grants reminder
authority. Without the complete binding it stays inactive.

Unix calls open a fresh connection and authenticate with `system.hello` each time, so a daemon
restart leaves no stale negotiated session. Interrupted calls fail explicitly without automatic
replay. The host must have permission to open the daemon's 0600 socket (normally the same UID).
Set `control_plane.token_file` on the daemon if token authentication is needed.

## Alignment with SPEC §14.2

`jobs.create` requires two fields that this plugin originally omitted, which made
`proactive_schedule` fail every time:

- `idempotency_key` (a required param) — now derived from the SHA-256 of the job
  body, so an identical replay is idempotent while a changed body lands as a new
  revision instead of a conflict.
- the job object's id field is named `id`, not `job_id`.
- `instruction` is a top-level job field on the wire; the daemon maps it into `task.instruction`.
- `always_run` is optional and defaults to false. Set it to true for an every-occurrence task
  that must run even with no changed source candidates; quiet/activity policy still applies.

`mode` accepts only `heartbeat` / `task` (plus `reminder`, created through
`pas jobs create --mode reminder`). The earlier `watch` value is not in the
control plane's enum and is rejected outright.


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
