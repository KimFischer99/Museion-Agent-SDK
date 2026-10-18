-- Migration 012: v2 decision reasons and silence reasons (decision contract 1.1).
--
-- Additive only, no table rebuilds:
--   run_proposals.reason   why an interrupting proposal (notify_self/draft)
--                          is worth the owner's attention;
--   runs.silence_reason    semantic reason a run stayed quiet (host verdict
--                          or the model's own silent-decision reason).
--
-- Existing rows are backfilled to 'unspecified': the old data never carried
-- a reason, and guessing one here would fabricate a quality metric.
ALTER TABLE run_proposals ADD COLUMN reason TEXT
 CHECK(reason IS NULL OR reason IN
   ('time_sensitive','changed','requested','ready_for_decision','new_opportunity','unspecified'));
UPDATE run_proposals SET reason='unspecified' WHERE reason IS NULL;

ALTER TABLE runs ADD COLUMN silence_reason TEXT
 CHECK(silence_reason IS NULL OR silence_reason IN
   ('no_source_change','user_active','quota_exhausted','quiet_window','gate_error',
    'moot','already_handled','unconfirmed','not_useful_now','reason_none','unspecified'));
