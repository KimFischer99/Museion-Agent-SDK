# JSON Schemas (PAS Interoperability Profile v1)

This directory holds the JSON Schema 2020-12 definitions of the unified contracts in SPEC
section 4. They are the **carrier of the frozen contracts**: the Python / TypeScript
implementations and the fixtures derive from them, and a schema change must land in all three
places (see docs/CONTRIBUTING.md).

## Conventions

- Every cross-process object must carry `protocol_version` (`^\d+\.\d+$`). Currently `1.0`.
- External timestamps are always RFC 3339 with `Z` or an explicit offset; internal integer
  millisecond fields end in `_ms`.
- Mutating requests use `idempotency_key`; the same key with different canonical content
  returns `conflict`.
- Canonical JSON: sorted keys, compact separators (see `proactive_sdk.contracts.canonical_json`);
  hashes are always lowercase hexadecimal SHA-256.

## Validator coverage

`proactive_sdk.schema_validate` implements the subset of 2020-12 keywords this repository's
schemas actually use: `type` (including `["x","null"]`), `properties`, `required`,
`additionalProperties:false`, `enum`, `const`, `items`, `minItems`/`maxItems`,
`minLength`/`maxLength`, `minimum`/`maximum`, `pattern`, `format: date-time`, `$defs`/internal
`$ref`. Annotation keywords (`title`/`description`/`$id`/`$schema`) are ignored.

**Cross-field rules are not expressed in the schemas.** They are implemented by the semantic
validators in `contracts.py` and have dedicated tests:

| Rule | Location |
|---|---|
| `Schedule` required fields per kind (interval⇒anchor+every_seconds; daily/monthly⇒local_time+timezone; weekly⇒weekdays+local_time+timezone; runonce⇒at) | `contracts.validate_schedule` |
| `Schedule.fold_policy` allows only `earliest`/`latest` (default `earliest`; it selects which of the two repeated instants to use when DST falls back, and the spring-forward gap is always skipped. Schema and implementation have supported this in step since P1) | `contracts.validate_schedule` |
| `Decision.decision == "silent"` ⇒ `proposals` is empty; `"propose"` ⇒ at least 1 | `contracts.validate_decision` |
| A `notify_self` proposal must carry `evidence_refs` and `expires_at` | `contracts.validate_decision` |
| `mode="reminder"` requires `reminder` and must not carry `task.instruction`; other modes must not carry `reminder`; `reminder.timezone` must match `schedule.timezone` | `contracts.validate_reminder` (JSON Schema expresses the mode/reminder coupling with `allOf`) |
| Unknown keys in `reminder` are rejected; every entry in `artifact_refs` must be openable and must appear verbatim in `body` | `proactive_sdk.artifacts` + `contracts.validate_reminder` |
| `obligation` is only `due`/`opportunistic`, and only trusted task configuration may supply it | `contracts.JobSpec` |
| `ContextPack.recent_notifications` is bounded and has no `body` field; `channel_kind` has only two values | `contracts.validate_context_pack` |

If the `jsonschema` library is introduced later for conformance (P7), these rules should be
written as `if/then` as well, or kept as code-level checks — both routes must produce identical
test results.

## Generated artifacts stay in sync

`packages/client-ts/src/schema_types.ts` is generated from this directory by
`tools/gen_client_ts.py`, and `tests/test_client_ts_drift.py` fails when the generated result
differs from what is committed; the method table in `packages/client-ts/src/rpc.ts` is compared
item by item against `proactive_sdk.rpc.PROACTIVE_RPC_METHODS`. A change to a public object must
land in Python, this directory, the generated types and the fixtures together.

## Files

| Schema | SPEC object |
|---|---|
| `error.json` | §4.4 unified error |
| `job_spec.json` | §4.1 JobSpec (includes Schedule) |
| `wake_event.json` | §4.1 WakeEvent |
| `run_request.json` / `run_handle.json` | §4.1 RunRequest / RunHandle |
| `context_pack.json` | §4.1 + §7.1 ContextPack |
| `decision.json` | §4.1 Decision + ActionProposal |
| `action_record.json` | §4.1 ActionRecord |
| `delivery_attempt.json` | §4.1 DeliveryAttempt (includes the outbox state machine §10.2) |
| `usage.json` | §4.1 Usage |

Example values live in `tests/test_schemas.py` and match the examples in SPEC §7.1 / §8.1
(fake data).
