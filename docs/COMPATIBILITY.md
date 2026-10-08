# Compatibility matrix (P7 / SPEC §17.3.3)

This document records the supported scope, the verified host versions and the unverified items.
Host versions are not read or pinned automatically by the SDK; a deployer that upgrades a host
must re-verify compatibility.

## Supported scope (v0.1 boundary)

| Dimension | Supported | Notes |
|---|---|---|
| profile | single profile | one state directory, one DB, one owner destination (not a multi-tenant product) |
| host | single host | the control plane defaults to a Unix socket; remote TLS HTTP is not implemented and is left to the deployer's own approach |
| Python | >= 3.11 | measured on 3.14.4 (macOS); the 3.11/3.12 matrix has not been run, recorded as such |
| runtime dependencies | standard library only | optional extra: `jsonschema` (for conformance tooling, does not affect runtime) |

## External hosts (verified versions)

| Host | Verified version | Method | Result |
|---|---|---|---|
| Hermes Runs gateway | 0.21.5+8493.g9b38eb1 (2026.9.24) | P5 real-service probes 1-5 (capabilities / submit / idempotency replay / cancel / key conflict) | 5/5 PASS |
| @earendil-works/pi-coding-agent | 1.0.4 | P5 real-service probes 6-8 (initialize / run envelope / cancel semantics) | 3/3 PASS |

Host upgrade rule: after an upgrade, re-run the probe set listed above and update the verified
versions in this document only once it passes. On a protocol major-version mismatch the control
plane refuses immediately at `system.hello` (fail closed).

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
