-- Migration 007: v0.1.1 reminders, obligations and the job activity
-- projection (SPEC §21.1 steps 2, 3, 4, 7).
--
-- Two baseline constraints have to be *relaxed*, which SQLite cannot do
-- with ALTER TABLE:
--   * jobs.mode was CHECK(mode IN ('heartbeat','task')) and needs the new
--     'reminder' value;
--   * actions.run_id was NOT NULL, but a deterministic direct reminder is
--     not an analysis run and must not fabricate one in the run ledger.
-- Both tables are therefore rebuilt with the documented "create new /
-- copy / drop old / rename new" procedure. The store runs migrations with
-- PRAGMA foreign_keys=OFF and validates with PRAGMA foreign_key_check
-- before committing, so child REFERENCES stay pointed at the final table
-- names. No existing row, and no existing *semantic*, changes.

-- --------------------------------------------------------------------- --
-- jobs: reminder payload + notification obligation + stop-tracking stamp
-- --------------------------------------------------------------------- --
CREATE TABLE jobs_p011 (
 job_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 mode TEXT NOT NULL CHECK(mode IN ('heartbeat','task','reminder')),
 scheduler_owner TEXT NOT NULL CHECK(scheduler_owner IN ('pas','host')),
 schedule_json TEXT NOT NULL CHECK(json_valid(schedule_json)),
 task_json TEXT NOT NULL CHECK(json_valid(task_json)),
 next_due_ms INTEGER, updated_at_ms INTEGER NOT NULL,
 created_at_ms INTEGER NOT NULL DEFAULT 0,
 misfire_policy TEXT
  CHECK(misfire_policy IS NULL OR misfire_policy IN ('coalesce_latest','grace_once','expire')),
 deadline_ms INTEGER,
 grant_refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(grant_refs_json)),
 delivery_policy_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(delivery_policy_json)),
 -- Frozen user-authored reminder (mode='reminder' only). The body, time,
 -- timezone, owner channel and obligation are validated on the trusted
 -- jobs_upsert path, never derived from model or source text.
 reminder_json TEXT CHECK(reminder_json IS NULL OR json_valid(reminder_json)),
 -- 'due' = the job owes a notification at the scheduled instant;
 -- 'opportunistic' = reach out only when analysis says so.
 obligation TEXT NOT NULL DEFAULT 'opportunistic'
  CHECK(obligation IN ('due','opportunistic')),
 -- Stop-tracking stamp: a stopped job keeps its full audit history and is
 -- never scheduled again (SPEC §21.1 step 7: 停止追踪并保留审计, never a
 -- cascading historical wipe).
 stopped_at_ms INTEGER,
 stop_reason TEXT
);
INSERT INTO jobs_p011(
    job_id, revision, enabled, mode, scheduler_owner, schedule_json, task_json,
    next_due_ms, updated_at_ms, created_at_ms, misfire_policy, deadline_ms,
    grant_refs_json, delivery_policy_json)
 SELECT job_id, revision, enabled, mode, scheduler_owner, schedule_json, task_json,
        next_due_ms, updated_at_ms, created_at_ms, misfire_policy, deadline_ms,
        grant_refs_json, delivery_policy_json
   FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_p011 RENAME TO jobs;
CREATE INDEX jobs_due ON jobs(enabled,next_due_ms);

-- --------------------------------------------------------------------- --
-- actions: a deterministic reminder is an action without an analysis run
-- --------------------------------------------------------------------- --
CREATE TABLE actions_p011 (
 action_id TEXT PRIMARY KEY,
 run_id TEXT REFERENCES runs(run_id),
 business_key TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL,
 request_json TEXT NOT NULL CHECK(json_valid(request_json)),
 request_hash TEXT NOT NULL,
 grant_id TEXT NOT NULL REFERENCES grants(grant_id),
 grant_version INTEGER NOT NULL,
 approval_id TEXT REFERENCES approvals(approval_id),
 policy_version INTEGER NOT NULL,
 state TEXT NOT NULL,
 expires_at_ms INTEGER NOT NULL,
 -- 'agent' = produced by a model decision; 'reminder' = frozen user text.
 source TEXT NOT NULL DEFAULT 'agent' CHECK(source IN ('agent','reminder')),
 occurrence_id TEXT,
 obligation TEXT NOT NULL DEFAULT 'opportunistic'
  CHECK(obligation IN ('due','opportunistic')),
 created_at_ms INTEGER NOT NULL DEFAULT 0
);
INSERT INTO actions_p011(
    action_id, run_id, business_key, kind, request_json, request_hash, grant_id,
    grant_version, approval_id, policy_version, state, expires_at_ms)
 SELECT action_id, run_id, business_key, kind, request_json, request_hash, grant_id,
        grant_version, approval_id, policy_version, state, expires_at_ms
   FROM actions;
DROP TABLE actions;
ALTER TABLE actions_p011 RENAME TO actions;

-- --------------------------------------------------------------------- --
-- job_activity: the user-visible projection (SPEC §21.1 step 7)
-- --------------------------------------------------------------------- --
-- Machine ledgers (runs / run_events / outbox) are not a user workspace.
-- This table is the separate projection the product reads to answer
-- "what did this job do, and why did it stay quiet": execution outcome and
-- notification outcome are recorded as distinct phases and are never
-- folded into one another.
CREATE TABLE job_activity (
 activity_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 job_revision INTEGER NOT NULL,
 occurrence_id TEXT,
 slot_ms INTEGER,
 phase TEXT NOT NULL CHECK(phase IN ('wake','analysis','action','delivery','missed')),
 state TEXT NOT NULL CHECK(state IN
   ('admitted','queued','deferred','suppressed','skipped','failed','proposed',
    'accepted','unknown','missed','delivered','read')),
 obligation TEXT NOT NULL CHECK(obligation IN ('due','opportunistic','none')),
 reason TEXT,
 -- Planned vs. actual instants and the *only known* lateness cause; a
 -- direct reminder past its window keeps this row as its queryable reason.
 planned_at_ms INTEGER,
 actual_at_ms INTEGER,
 lateness_ms INTEGER,
 destination_ref TEXT,
 message_id TEXT REFERENCES outbox(message_id),
 run_id TEXT REFERENCES runs(run_id),
 retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0,1)),
 -- Idempotency for the projection itself: replaying the same occurrence
 -- phase never writes a second visible row.
 dedupe_key TEXT NOT NULL UNIQUE,
 created_at_ms INTEGER NOT NULL
);
CREATE INDEX job_activity_job ON job_activity(job_id, created_at_ms);
CREATE INDEX job_activity_occurrence ON job_activity(occurrence_id);
