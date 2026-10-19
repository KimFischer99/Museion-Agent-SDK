CREATE TABLE goals (
 goal_id TEXT PRIMARY KEY,
 title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
 description TEXT NOT NULL DEFAULT '' CHECK(length(description) <= 2000),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','archived')),
 created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL
);
CREATE TABLE goal_entries (
 entry_id TEXT PRIMARY KEY,
 goal_id TEXT NOT NULL REFERENCES goals(goal_id),
 kind TEXT NOT NULL CHECK(kind IN ('created','updated','progress','status')),
 text TEXT NOT NULL CHECK(length(text) <= 2000),
 evidence_refs_json TEXT NOT NULL CHECK(json_valid(evidence_refs_json)),
 at_ms INTEGER NOT NULL
);
CREATE INDEX goal_entries_goal ON goal_entries(goal_id, at_ms);
