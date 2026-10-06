-- Migration 003: P2 hook runtime additions (SPEC §6, §15.1 P2, HOOK-01).
--
-- Baseline hooks table (m001) already carries definition_hash, enabled,
-- state_version, state_json, lease_fence/lease_until_ms and next_due_ms.
-- Everything here is additive: error accounting with its own cooldown
-- (SPEC §6.1 "错误次数累计与管理员诊断有单独冷却"), the hook's poll
-- interval, and run bookkeeping. Columns are added, never dropped or
-- retyped, so 002->003 upgrade and rollback-by-transaction stay testable.

ALTER TABLE hooks ADD COLUMN poll_interval_ms INTEGER
 CHECK(poll_interval_ms IS NULL OR poll_interval_ms > 0);
ALTER TABLE hooks ADD COLUMN timeout_ms INTEGER
 CHECK(timeout_ms IS NULL OR timeout_ms > 0);
-- The runnable definition itself (canonical HookSpec JSON). definition_hash
-- (m001) stays the integrity anchor: the runner re-hashes this column and
-- refuses to execute on mismatch.
ALTER TABLE hooks ADD COLUMN definition_json TEXT
 CHECK(definition_json IS NULL OR json_valid(definition_json));
ALTER TABLE hooks ADD COLUMN created_at_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hooks ADD COLUMN updated_at_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hooks ADD COLUMN last_run_at_ms INTEGER;
ALTER TABLE hooks ADD COLUMN last_decision TEXT
 CHECK(last_decision IS NULL OR last_decision IN ('silent','wake'));
ALTER TABLE hooks ADD COLUMN consecutive_errors INTEGER NOT NULL DEFAULT 0
 CHECK(consecutive_errors >= 0);
ALTER TABLE hooks ADD COLUMN last_error_class TEXT;
ALTER TABLE hooks ADD COLUMN last_error_at_ms INTEGER;
ALTER TABLE hooks ADD COLUMN error_backoff_until_ms INTEGER NOT NULL DEFAULT 0;

-- Due scan for the hook coordinator, mirroring jobs_due.
CREATE INDEX hooks_due ON hooks(enabled, next_due_ms);
