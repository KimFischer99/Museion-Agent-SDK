ALTER TABLE runs ADD COLUMN stage TEXT CHECK(stage IN ('probed','analyzed','composed','proposed'));
ALTER TABLE runs ADD COLUMN checkpoint_json TEXT CHECK(checkpoint_json IS NULL OR json_valid(checkpoint_json));
ALTER TABLE runs ADD COLUMN checkpoint_hash TEXT;
ALTER TABLE runs ADD COLUMN input_hash TEXT;
ALTER TABLE runs ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0 CHECK(resume_count >= 0);
-- Old decisions were already committed and must never become claimable checkpoints.
UPDATE runs SET stage='proposed' WHERE context_ref IS NOT NULL;
