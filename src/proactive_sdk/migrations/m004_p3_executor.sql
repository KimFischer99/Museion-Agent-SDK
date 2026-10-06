-- Migration 004: P3 executor additions (SPEC §8, §15.1 P3, EXEC-01).
--
-- Everything here is additive: columns are added, never dropped or
-- retyped, so 003->004 upgrade and rollback-by-transaction stay testable.
-- runs.state keeps no CHECK (baseline m001), so the §4.3 states
-- 'proposed' and 'suppressed' need no schema change; the reference
-- 'planned' state from P1 stays valid for the reference path.

-- Run accounting: decision summary (bounded like Decision.summary),
-- proposal count, and the normalized Usage object (§4.1; unmeasurable
-- values are null inside the JSON, never zero).
ALTER TABLE runs ADD COLUMN decision_summary TEXT
 CHECK(decision_summary IS NULL OR length(decision_summary) <= 500);
ALTER TABLE runs ADD COLUMN proposal_count INTEGER
 CHECK(proposal_count IS NULL OR proposal_count >= 0);
ALTER TABLE runs ADD COLUMN usage_json TEXT
 CHECK(usage_json IS NULL OR json_valid(usage_json));

-- Immutable ContextPack snapshots (§7.1). One row per run; the pack body
-- is canonical JSON verified against content_hash on read. Large source
-- bodies do not go here: they live in snapshots via content_ref.
CREATE TABLE context_packs (
 context_ref TEXT PRIMARY KEY,
 run_id TEXT NOT NULL REFERENCES runs(run_id),
 event_id TEXT,
 pack_json TEXT NOT NULL CHECK(json_valid(pack_json)),
 content_hash TEXT NOT NULL,
 created_at_ms INTEGER NOT NULL
);
CREATE INDEX context_packs_run ON context_packs(run_id);

-- Accepted ActionProposals (§8.1) awaiting the P4 policy/outbox path.
-- These are not ActionRecords yet: no grant/approval/policy fields —
-- policy evaluation and the actions table stay P4 (§15.1 P4 row).
CREATE TABLE run_proposals (
 proposal_id TEXT PRIMARY KEY,
 run_id TEXT NOT NULL REFERENCES runs(run_id),
 seq INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN
   ('notify_self','draft','internal_record','suggest_watch','request_external_action')),
 fact_id TEXT NOT NULL,
 revision TEXT,
 body TEXT,
 arguments_json TEXT CHECK(arguments_json IS NULL OR json_valid(arguments_json)),
 evidence_refs_json TEXT NOT NULL CHECK(json_valid(evidence_refs_json)),
 expires_at_ms INTEGER,
 created_at_ms INTEGER NOT NULL,
 UNIQUE(run_id, seq)
);
CREATE INDEX run_proposals_run ON run_proposals(run_id);
