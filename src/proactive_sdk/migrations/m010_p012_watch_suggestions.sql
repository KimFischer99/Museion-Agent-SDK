-- Migration 010: pending watch suggestions (SPEC §22.1 item 7).
--
-- `suggest_watch` was one of the five declared proposal kinds but had no
-- consumer: policy filed it under `_LOCAL_KINDS`, which writes a
-- `run_events` note and stops there. The user never saw it and it never
-- became a task.
--
-- A suggestion is a *frozen* record of what would be created. Freezing
-- matters: the model proposes once, and the user then confirms exactly what
-- was proposed. There is no path from here back to model output, so a
-- confirmation can never create something different from what was shown.
--
-- Authority does not travel with the suggestion. The grants recorded here
-- are the ones the originating job or wake already held; confirming reuses
-- them and cannot widen them.
CREATE TABLE watch_suggestions (
 suggestion_id TEXT PRIMARY KEY,
 run_id TEXT REFERENCES runs(run_id),
 proposal_id TEXT,
 job_id TEXT,
 state TEXT NOT NULL CHECK(state IN ('pending','accepted','declined')),
 -- Frozen parameters, exactly as shown to the user.
 job_name TEXT NOT NULL,
 schedule_json TEXT NOT NULL CHECK(json_valid(schedule_json)),
 instruction TEXT NOT NULL,
 grant_refs_json TEXT NOT NULL CHECK(json_valid(grant_refs_json)),
 delivery_policy_json TEXT NOT NULL CHECK(json_valid(delivery_policy_json)),
 misfire_policy TEXT
  CHECK(misfire_policy IS NULL OR misfire_policy IN ('coalesce_latest','grace_once','expire')),
 -- Why the analysis thought this was worth watching; user-visible.
 reason TEXT NOT NULL,
 created_at_ms INTEGER NOT NULL,
 resolved_at_ms INTEGER,
 resolved_by TEXT,
 created_job_id TEXT
);
CREATE INDEX watch_suggestions_state ON watch_suggestions(state, created_at_ms);
CREATE INDEX watch_suggestions_run ON watch_suggestions(run_id);
