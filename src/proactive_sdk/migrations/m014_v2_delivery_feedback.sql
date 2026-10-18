-- Migration 014: W6 delivery-outcome feedback (separate from preference feedback).
--
-- The existing ``feedback`` table records user *preferences* (mute/handled/
-- not-useful). W6 records what happened *after delivery* — replied / ignored /
-- dismissed / complained — which is a different fact and must not be mixed
-- with the preference ledger. ``source`` distinguishes a trusted-UI report
-- from the host-side ``ignored_24h`` backfill (a weak signal, never authored
-- by the model).
CREATE TABLE delivery_feedback (
 feedback_id TEXT PRIMARY KEY,
 message_id TEXT NOT NULL REFERENCES outbox(message_id),
 signal TEXT NOT NULL CHECK(signal IN ('replied','ignored_24h','dismissed','complained')),
 source TEXT NOT NULL CHECK(source IN ('user','host')),
 authenticated_actor TEXT NOT NULL,
 created_at_ms INTEGER NOT NULL,
 UNIQUE(message_id, signal)
);
CREATE INDEX delivery_feedback_message ON delivery_feedback(message_id);

-- Source attribution for proposals (used by the W6 per-source report). NULL
-- for proposals that cite facts outside the selected candidate set.
ALTER TABLE run_proposals ADD COLUMN source_id TEXT;
