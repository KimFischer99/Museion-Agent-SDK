# Compatibility matrix (P7 / SPEC §17.3.3)

## v0.1.6 verification

On macOS with Python 3.14, the offline unit/contract suite passed 999 tests. Scripted
checks cover trusted request classification and source selection, scheduled reminders,
unchanged-source research across restart, note follow-up deduplication and forgetting,
quiet-window report deferral, source failure feedback, and the Hermes input hook's owner
and unauthorized paths. The Hermes hook tests use a stub host; they do not establish
compatibility with a deployed Hermes gateway. An isolated local run fetched the NASA RSS
feed and delivered two manually triggered reports to the local inbox using a scripted
executor; it did not verify a real model, real scheduled wall-clock execution, or an
external notification provider. TypeScript 5.8.3 compilation and wheel install smoke
passed. No database migration was introduced for these changes.

This document records the supported scope, the verified host versions and the unverified items.
Host versions are not read or pinned automatically by the SDK; a deployer that upgrades a host
must re-verify compatibility.

## Supported scope (v0.1 boundary)

| Dimension | Supported | Notes |
|---|---|---|
| profile | single profile | one state directory, one DB, one owner destination (not a multi-tenant product) |
| host | single host | the control plane defaults to a Unix socket; remote TLS HTTP is not implemented and is left to the deployer's own approach |
| Python | >= 3.11 | 3.14.4 (macOS): full suite 966 tests, only environment-class errors (hook sandbox unavailable in the recording shell); 3.12.3 (Linux test host): full suite 966 tests, 20 environment errors (no bubblewrap) + 9 skips + 1 environment-sensitive performance guard on a slow shared host; 3.11.16: targeted 259 tests + wheel install smoke. A fully green matrix on all three runtimes is not claimed. v0.1.4 (v3): 3.12.3 Linux fresh venv, 978 tests — 20 environment errors (no bubblewrap) + 9 skips + 1 slow-host performance guard (`test_thousand_jobs_scan_is_bounded`, 41 s > 30 s), no other failures; macOS 3.14.4 targeted checks and release build/install smoke passed. Python 3.11 has not run the v3 suite. |
| runtime dependencies | standard library only | optional extra: `jsonschema` (for conformance tooling, does not affect runtime) |

## External hosts (verified versions)

| Host | Verified version | Method | Result |
|---|---|---|---|
| Hermes Runs gateway | 0.21.5+8493.g9b38eb1 (2026.9.24) | P5 real-service probes 1-5 (capabilities / submit / idempotency replay / cancel / key conflict) | 5/5 PASS |
| @earendil-works/pi-coding-agent | 1.0.4 | P5 real-service probes 6-8 (initialize / run envelope / cancel semantics) | 3/3 PASS |

Host upgrade rule: after an upgrade, re-run the probe set listed above and update the verified
versions in this document only once it passes. On a protocol major-version mismatch the control
plane refuses immediately at `system.hello` (fail closed).

v2 real-host gate (2026-10-18, isolated Linux test host, Python 3.12.3, real model replies with
`pricing_basis=measured`): Hermes Runs gateway 8/8 probes passed — config, capabilities, bridge
envelope, contract injection, and **cancel-while-running** (the run was observed in `running`
before the cancel; the host confirmed it and the run settled `cancelled`, not completed); Pi
1.0.4 3/3 probes passed — initialize, bridge envelope (v2 `silence_reason` accepted with the real
response shape), and in-flight cancel (request level `requested`, settled `errored/conflict`,
honestly reported rather than claimed confirmed). The host-side activity probe and the W7 host
executor remain unverified (see below).

v3 real-host gate (2026-10-19, isolated Linux test host, Python 3.12.3, real model replies with
`pricing_basis=measured`, fresh schema 17): the two-stage runtime e2e passed 16/16 assertions on
both Hermes Runs gateway and Pi 1.0.4 — two real model calls per backend, scoped conversation,
memory, goal context and handoff enabled, policy queued one notification, and the outbox was
delivered on the following tick (the first tick's `dispatched` stayed empty by design). One
intermediate Pi attempt was rejected by decision validation because the model cited `atlas-turn`
instead of `conversation:atlas-turn`; the same script passed on rerun. The v14→v17 upgrade, the
v3 switches being off by default, and resume-without-identity failing closed are covered by
`tests/test_migration_v014.py` and `tests/test_v3_switch_off.py`. Cross-process resume-on-success,
switch-on fault injection and the host-side activity probe remain unverified.

## Connectors and notification channels

| Component | Status | `e2e_verified` |
|---|---|---|
| Gmail (restricted GWS grammar) | Transport-layer contract tests (scripted stand-in plus the real adapter code path) | ❌ |
| Google Calendar (same) | Transport-layer contract tests | ❌ |
| Webhook notification sink | Loopback HTTP contract (idempotency key / unknown reconciliation / no blind send on a lost ACK) | ❌ (no external provider bound) |
| Public material tracking | Loopback TLS (self-signed CA + IP pinning) verifying transport and gates | ❌ |

There are no authorized credentials, so the e2e column stays ❌ - the README is in sync with this
and does not pretend an integration has been run.

## Skills compatibility layer

- The product bundles 88 Skill entry points plus their directory assets (377 source files) as
  passive deployment reference; they are not loaded or executed by default.
- The installed directory is `proactive_sdk/_deployment_reference/skills/`; a deployer may opt in
  to a compatibility import explicitly.
- Audit: 88 entry points, reproducible with `tools/reproduce_audit.py`.
- Import pipeline: scan -> sidecar -> install agrees with the audit (P6, 88/88 field-level
  agreement).
- End-to-end capability verification: **0**. `technical_status` stops at `parsed`; `e2e_verified`
  needs real authorization and item-by-item integration, and the gap is recorded transparently
  rather than claiming "all 88 capabilities implemented".

## Operations matrix

| Platform | Status |
|---|---|
| macOS (darwin 27 arm64) | Full test suite green; sandbox-exec available (deprecated by Apple; a failed probe fails closed) |
| Linux (Ubuntu 24.04) | The host where the P5 real-service verification ran; Bubblewrap argv/probe tested, but the hook end-to-end suite was not re-run in full on that machine |

## Data and upgrade rules

- store migrations are additive only; an old binary refuses to open a new database; backups carry
  a `schema_version` and restore refuses when the backup is newer than the binary.
- A major-version change of the control-plane protocol (`PAS_PROTOCOL_VERSION=1.0`) breaks
  client-ts clients - a failed hello negotiation stops the connection.
- After this document or an external document changes, run the tests before updating the matrix
  (SPEC §17.3.6).

## Proactive layer v2 additions (branch `feat/pas-v2`, unreleased)

Verified by unit/integration tests on the Python 3.14.4 development machine (the 3.11/3.12
matrix remains unrun, as stated above). Items that need a real external service stay ❌ until
someone runs them against their own authorized environment — this document never upgrades a row
on the strength of mocks.

| Capability | Verification | Status |
|---|---|---|
| Decision contract 1.1 (structured `reason`; `policy.require_reason` gray release) | Unit + contract-drift tests; schema/TS regenerated together | ✅ offline |
| Semantic silence reasons (`silence_reason`, host + model vocabularies) | Unit + migration tests (012) | ✅ offline |
| User-presence idle gate (`policy.idle_gate`; reminder/manual exempt; fail open) | Unit + fake-clock/real-clock reminder regression | ✅ offline |
| Two-stage path: probe → candidates → run (`policy.max_candidates`, `task.always_run`) | Unit + 24-tick cost simulation | ✅ offline |
| Generic source connectors: RSS/Atom, sitemap diff, page hash | Unit + pinned loopback TLS fixture site; real internet ❌ (manual only) | ✅ offline |
| Host-provided activity probe | Protocol + fake probe only; host adapter implementation is deployment-side | ❌ unverified |
| Delivery feedback (`feedback.record`, `delivery_feedback`, `pas stats --since`) | Unit + aggregation tests; real reply channel is host-side | ✅ offline |
| Host bridge real-service e2e (Hermes Runs gateway + Pi 1.0.4) | 8/8 probes on an isolated host with real model replies; cancel-while-running proven for Hermes | ✅ offline |
| Host executor replacing the built-in loop (W7) | Gated: requires 7 days of ledger data and host-side measurements first | ❌ not started |

Operational notes for v2:

- migrations 012–014 are additive (reason/silence columns, candidate counters, delivery_feedback,
  proposal source attribution); an old binary still refuses a newer database and backups reject a
  newer `schema_version`, so rollback is "restore a backup", not "downgrade in place".
- `task.always_run: true` restores the pre-v2 "run every slot" semantics for a job; without it a
  job-driven task is silent when no source candidate changed.
- new network sources are registered programmatically and are default-off for untargeted
  heartbeats; a job opts in with `task.refresh_source_ids`.
