-- Migration 001: baseline DDL, byte-for-byte the table set of
-- examples/schema.sql (SPEC §13.1 "生产表起点"). One database per
-- authenticated profile; timestamps are UTC epoch ms.
--
-- Connection-level PRAGMAs (foreign_keys, busy_timeout, journal_mode,
-- synchronous) are set by the store, never here. Each migration runs
-- inside one BEGIN IMMEDIATE transaction and is recorded with its
-- sha256 checksum in schema_migrations.
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE schema_migrations (
 version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at_ms INTEGER NOT NULL
);
CREATE TABLE grants (
 grant_id TEXT PRIMARY KEY, account_ref TEXT NOT NULL, capability TEXT NOT NULL,
 scope_json TEXT NOT NULL CHECK(json_valid(scope_json)), version INTEGER NOT NULL,
 expires_at_ms INTEGER, revoked_at_ms INTEGER, consent_evidence_ref TEXT NOT NULL
);
CREATE TABLE jobs (
 job_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 mode TEXT NOT NULL CHECK(mode IN ('heartbeat','task')),
 scheduler_owner TEXT NOT NULL CHECK(scheduler_owner IN ('pas','host')),
 schedule_json TEXT NOT NULL CHECK(json_valid(schedule_json)),
 task_json TEXT NOT NULL CHECK(json_valid(task_json)),
 next_due_ms INTEGER, updated_at_ms INTEGER NOT NULL
);
CREATE INDEX jobs_due ON jobs(enabled,next_due_ms);
CREATE TABLE job_grants (
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 grant_id TEXT NOT NULL REFERENCES grants(grant_id),
 PRIMARY KEY(job_id,grant_id)
);
CREATE TABLE hooks (
 hook_id TEXT PRIMARY KEY, definition_hash TEXT NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 state_version INTEGER NOT NULL DEFAULT 0,
 state_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(state_json)),
 lease_fence INTEGER NOT NULL DEFAULT 0, lease_until_ms INTEGER NOT NULL DEFAULT 0,
 next_due_ms INTEGER
);
CREATE TABLE events (
 event_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
 origin TEXT NOT NULL, job_id TEXT REFERENCES jobs(job_id), job_revision INTEGER,
 occurrence_id TEXT, payload_hash TEXT NOT NULL, payload_ref TEXT NOT NULL,
 observed_at_ms INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL
);
CREATE TABLE hook_invocations (
 invocation_id TEXT PRIMARY KEY, hook_id TEXT NOT NULL REFERENCES hooks(hook_id),
 request_hash TEXT NOT NULL, state_version INTEGER NOT NULL,
 event_id TEXT REFERENCES events(event_id), committed_at_ms INTEGER NOT NULL
);
CREATE TABLE runs (
 run_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
 state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
 fence INTEGER NOT NULL DEFAULT 0, lease_until_ms INTEGER NOT NULL DEFAULT 0,
 deadline_ms INTEGER NOT NULL, policy_version INTEGER NOT NULL,
 context_ref TEXT, host_handle_json TEXT CHECK(host_handle_json IS NULL OR json_valid(host_handle_json)),
 error_class TEXT, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
);
CREATE INDEX runs_dispatch ON runs(state,lease_until_ms);
CREATE TABLE run_events (
 run_id TEXT NOT NULL REFERENCES runs(run_id), seq INTEGER NOT NULL,
 kind TEXT NOT NULL, safe_summary TEXT, detail_ref TEXT,
 created_at_ms INTEGER NOT NULL, PRIMARY KEY(run_id,seq)
);
CREATE TABLE source_state (
 source_id TEXT NOT NULL, account_ref TEXT NOT NULL,
 cursor_ref TEXT, detected_watermark TEXT, version INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL, PRIMARY KEY(source_id,account_ref)
);
CREATE TABLE snapshots (
 snapshot_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, account_ref TEXT NOT NULL,
 content_ref TEXT NOT NULL, content_hash TEXT NOT NULL,
 observed_at_ms INTEGER NOT NULL, fresh_until_ms INTEGER NOT NULL,
 sensitivity TEXT NOT NULL, tombstone INTEGER NOT NULL DEFAULT 0 CHECK(tombstone IN (0,1))
);
CREATE TABLE approvals (
 approval_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
 grant_id TEXT NOT NULL REFERENCES grants(grant_id), grant_version INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','approved','denied','expired','revoked')),
 expires_at_ms INTEGER NOT NULL, resolved_by TEXT, resolved_at_ms INTEGER
);
CREATE TABLE actions (
 action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
 business_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 request_json TEXT NOT NULL CHECK(json_valid(request_json)), request_hash TEXT NOT NULL,
 grant_id TEXT NOT NULL REFERENCES grants(grant_id), grant_version INTEGER NOT NULL,
 approval_id TEXT REFERENCES approvals(approval_id), policy_version INTEGER NOT NULL,
 state TEXT NOT NULL, expires_at_ms INTEGER NOT NULL
);
CREATE TABLE outbox (
 message_id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES actions(action_id),
 delivery_key TEXT NOT NULL UNIQUE, destination_ref TEXT NOT NULL,
 payload_ref TEXT NOT NULL, state TEXT NOT NULL,
 not_before_ms INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL,
 fence INTEGER NOT NULL DEFAULT 0, lease_until_ms INTEGER NOT NULL DEFAULT 0,
 receipt_ref TEXT
);
CREATE INDEX outbox_dispatch ON outbox(state,not_before_ms);
CREATE TABLE delivery_attempts (
 attempt_id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES outbox(message_id),
 fence INTEGER NOT NULL, provider_key TEXT NOT NULL,
 state TEXT NOT NULL, started_at_ms INTEGER NOT NULL, completed_at_ms INTEGER,
 receipt_ref TEXT, error_class TEXT, UNIQUE(message_id,fence)
);
CREATE TABLE skill_installs (
 install_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE,
 source_hash TEXT NOT NULL, manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
 technical_status TEXT NOT NULL, distribution_status TEXT NOT NULL,
 updated_at_ms INTEGER NOT NULL
);
CREATE TABLE feedback (
 feedback_id TEXT PRIMARY KEY, message_id TEXT REFERENCES outbox(message_id),
 kind TEXT NOT NULL, scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
 authenticated_actor TEXT NOT NULL, created_at_ms INTEGER NOT NULL
);
