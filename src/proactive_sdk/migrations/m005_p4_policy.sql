-- Migration 005: P4 policy & delivery additions (SPEC §9, §10; §15.1 P4,
-- POLICY-01 / SEND-01).
--
-- Everything here is additive: columns are added, never dropped or
-- retyped, so 004->005 upgrade and rollback-by-transaction stay testable.
-- The baseline grants/approvals/actions/outbox/delivery_attempts/feedback
-- tables (m001) carry the frozen identity/hash/state columns; this
-- migration adds the bookkeeping the P4 write paths need.

-- Grant audit: creation time (revocation bookkeeping and consent chain).
ALTER TABLE grants ADD COLUMN created_at_ms INTEGER NOT NULL DEFAULT 0;

-- Approvals freeze the full canonical request (§9.2): request_hash (m001)
-- stays the binding anchor; request_json keeps the frozen content for
-- re-approval comparison and audit display.
ALTER TABLE approvals ADD COLUMN request_json TEXT
 CHECK(request_json IS NULL OR json_valid(request_json));
ALTER TABLE approvals ADD COLUMN created_at_ms INTEGER NOT NULL DEFAULT 0;

-- Per-proposal policy verdicts from the §4.3 phase
-- proposed → policy_evaluated; the queueing phase reads them back.
ALTER TABLE run_proposals ADD COLUMN policy_json TEXT
 CHECK(policy_json IS NULL OR json_valid(policy_json));

-- Outbox delivery bookkeeping (§10.2): the stable provider idempotency
-- key (identical across retries of one message), bounded inline payload,
-- attempt counter, machine reason, creation time.
ALTER TABLE outbox ADD COLUMN provider_key TEXT;
ALTER TABLE outbox ADD COLUMN payload_json TEXT
 CHECK(payload_json IS NULL OR json_valid(payload_json));
ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0
 CHECK(attempts >= 0);
ALTER TABLE outbox ADD COLUMN reason TEXT;
ALTER TABLE outbox ADD COLUMN created_at_ms INTEGER NOT NULL DEFAULT 0;
CREATE INDEX outbox_provider_key ON outbox(provider_key);

-- Local inbox (§10.2): rows are written in the SAME transaction that
-- marks the outbox message stored_in_inbox, giving the reliable
-- once-inbox property for the owner's local channel.
CREATE TABLE inbox (
 inbox_id TEXT PRIMARY KEY,
 message_id TEXT NOT NULL UNIQUE REFERENCES outbox(message_id),
 title TEXT NOT NULL,
 body TEXT,
 fact_id TEXT,
 revision TEXT,
 run_id TEXT REFERENCES runs(run_id),
 delivered_at_ms INTEGER NOT NULL,
 read_at_ms INTEGER
);
CREATE INDEX inbox_delivered ON inbox(delivered_at_ms);

-- Owner channels (§9.1): the trusted-config binding of personal
-- notification channels. Rows are created only by the composition root /
-- trusted setup; model output can never insert or select a channel.
CREATE TABLE owner_channels (
 channel_ref TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('local_inbox','webhook')),
 endpoint_json TEXT NOT NULL CHECK(json_valid(endpoint_json)),
 push_summary_only INTEGER NOT NULL DEFAULT 1 CHECK(push_summary_only IN (0,1)),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 created_at_ms INTEGER NOT NULL
);

-- Topic mutes (§10.1 已静音话题): feedback-driven or config-driven;
-- muted_until_ms NULL means until explicitly unmuted.
CREATE TABLE topic_mutes (
 topic TEXT PRIMARY KEY,
 muted_until_ms INTEGER,
 reason TEXT,
 created_at_ms INTEGER NOT NULL
);

-- Handled-fact suppression lookup (§10.1 已由用户处理…都应抑制).
CREATE INDEX feedback_handled_fact
 ON feedback(json_extract(scope_json,'$.fact_id')) WHERE kind='handled';

-- Shared content blobs (§13.1: large bodies live in a controlled local
-- store, the DB keeps hash + ref). Used by outbox payloads and snapshots;
-- size is bounded at admission.
CREATE TABLE blobs (
 blob_hash TEXT PRIMARY KEY,
 content TEXT NOT NULL,
 size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
 created_at_ms INTEGER NOT NULL
);
