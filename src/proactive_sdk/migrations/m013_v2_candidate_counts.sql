-- Migration 013: W5 per-run cost accounting.
--
-- The two-stage path has to be auditable after the fact: how many
-- candidates actually entered the analysis, and how many were cut by
-- ``policy.max_candidates``. Additive only; old rows keep NULL (unknown,
-- not zero — a v2-era count on a legacy run would be a fabricated fact).
ALTER TABLE runs ADD COLUMN candidates_count INTEGER
 CHECK(candidates_count IS NULL OR candidates_count >= 0);
ALTER TABLE runs ADD COLUMN truncated_count INTEGER
 CHECK(truncated_count IS NULL OR truncated_count >= 0);
