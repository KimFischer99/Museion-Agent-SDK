-- Migration 008: per-run tool authority (SPEC §22.1 item 5 / §22.2).
--
-- A host agent may execute its own tools. PAS cannot see those calls, and
-- AGENTS.md is explicit that raw shell, network and credentials can bypass
-- the broker — so the honest thing is not to pretend otherwise but to
-- record, per run, whether PAS's authorization actually constrained what
-- happened.
--
--   pas_broker  the executor routed tool calls through PAS's broker
--   host        the host executed its own tools; PAS did not constrain them
--   unknown     never declared (rows written before v0.1.2)
--
-- 'unknown' is the default on purpose: "not declared" must never read as
-- "covered". Additive columns only, so a v0.1.0/v0.1.1 database upgrades in
-- place and an older binary still opens a newer file's other tables.
ALTER TABLE runs ADD COLUMN tool_authority TEXT NOT NULL DEFAULT 'unknown'
 CHECK(tool_authority IN ('pas_broker','host','unknown'));
ALTER TABLE runs ADD COLUMN tool_authority_reason TEXT
 CHECK(tool_authority_reason IS NULL OR length(tool_authority_reason) <= 500);
