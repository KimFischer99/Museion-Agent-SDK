-- Candidate provenance is retained after promotion; forgetting blocks old evidence.
CREATE TABLE memory_candidates (
 candidate_id TEXT PRIMARY KEY,
 run_id TEXT NOT NULL REFERENCES runs(run_id),
 proposal_seq INTEGER NOT NULL,
 content TEXT CHECK(content IS NULL OR length(content) BETWEEN 1 AND 2000),
 content_hash TEXT NOT NULL,
 evidence_refs_json TEXT NOT NULL CHECK(json_valid(evidence_refs_json)),
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','promoted','expired','forgotten')),
 created_at_ms INTEGER NOT NULL,
 expires_at_ms INTEGER NOT NULL,
 UNIQUE(run_id, proposal_seq, content_hash)
);
CREATE INDEX memory_candidates_hash ON memory_candidates(content_hash, state);
CREATE TABLE memory_forget_blocks (
 content_hash TEXT PRIMARY KEY,
 forgotten_at_ms INTEGER NOT NULL
);
