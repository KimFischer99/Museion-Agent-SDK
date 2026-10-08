-- Migration 011: durable user memory and proactive preferences.
CREATE TABLE memory_entries (
 memory_id TEXT PRIMARY KEY,
 entry_json TEXT NOT NULL CHECK(json_valid(entry_json)),
 updated_at_ms INTEGER NOT NULL
);
CREATE TABLE proactive_preferences (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 allowed_topics_json TEXT CHECK(allowed_topics_json IS NULL OR json_valid(allowed_topics_json)),
 updated_at_ms INTEGER NOT NULL
);
