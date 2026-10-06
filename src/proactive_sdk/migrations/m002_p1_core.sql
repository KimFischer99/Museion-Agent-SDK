-- Migration 002: P1 persistence additions (SPEC §15.1 P1, SCHED-01/STORE-01).
--
-- Baseline tables stay untouched where possible; columns are added, never
-- dropped or retyped. Everything here is additive so 001->002 upgrade and
-- rollback-by-transaction are testable.

-- Job bookkeeping beyond the baseline columns: creation time (fresh-job
-- cursor), resolved misfire policy, job-level deadline, and the grant /
-- delivery-policy parts of JobSpec §4.1 that P4 will consume.
ALTER TABLE jobs ADD COLUMN created_at_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN misfire_policy TEXT
 CHECK(misfire_policy IS NULL OR misfire_policy IN ('coalesce_latest','grace_once','expire'));
ALTER TABLE jobs ADD COLUMN deadline_ms INTEGER;
ALTER TABLE jobs ADD COLUMN grant_refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(grant_refs_json));
ALTER TABLE jobs ADD COLUMN delivery_policy_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(delivery_policy_json));

-- Event payloads are small hook/scheduler envelopes (<=64 KiB, guarded at
-- admission); large bodies still go to a controlled blob store in P3, the
-- DB keeps hash + ref. Column stays nullable: baseline rows predate it.
ALTER TABLE events ADD COLUMN payload_json TEXT
 CHECK(payload_json IS NULL OR json_valid(payload_json));

-- Materialized occurrence ledger. A slot exists here only once per
-- (job, revision, kind, slot): re-admission of the same occurrence is an
-- INSERT OR IGNORE, so a retry can never enqueue a duplicate event
-- (SPEC §5.2 "同 occurrence 单入队"). 'expired' rows keep the queryable
-- non-execution reason required for missed runs (SPEC §5.2).
CREATE TABLE job_occurrences (
 occurrence_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
 job_revision INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('interval','daily','weekly','monthly','runonce')),
 slot_ms INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('admitted','expired')),
 reason TEXT,
 event_id TEXT REFERENCES events(event_id),
 recorded_at_ms INTEGER NOT NULL,
 UNIQUE(job_id, job_revision, kind, slot_ms)
);
CREATE INDEX job_occurrences_job ON job_occurrences(job_id, slot_ms);

-- Write-path idempotency for mutation requests (SPEC §4.1: all mutation
-- requests carry idempotency_key; same key + different normalized content
-- is a conflict). Event admission dedupes through events.idempotency_key.
CREATE TABLE jobs_idempotency (
 idempotency_key TEXT PRIMARY KEY,
 request_hash TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
 created_at_ms INTEGER NOT NULL
);
