-- Migration 019: append-only task authorization provenance (stage D).
--
-- Each job revision gets one immutable source record written in the same
-- transaction as the jobs row.  The record stores only a digest of the
-- trusted input, never the raw chat text; a digest is an audit correlation
-- value and deliberately cannot reconstruct the request.  Historical grant
-- references are snapshots for display/audit only and grant no authority:
-- every execution still re-checks the current grants.
--
-- There is intentionally no FK to jobs: an explicit profile wipe or a
-- never-run job deletion must not silently rewrite this audit ledger.  The
-- update trigger makes application-level history immutability enforceable.
CREATE TABLE job_provenance (
    job_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    entry_kind TEXT NOT NULL,
    authenticated_actor TEXT NOT NULL,
    request_id TEXT,
    host_ref TEXT,
    input_hash TEXT NOT NULL,
    recorded_at_ms INTEGER NOT NULL,
    task_json TEXT NOT NULL CHECK(json_valid(task_json)),
    grant_refs_json TEXT NOT NULL CHECK(json_valid(grant_refs_json)),
    job_spec_json TEXT NOT NULL CHECK(json_valid(job_spec_json)),
    PRIMARY KEY(job_id, revision)
);

CREATE TRIGGER job_provenance_no_update
BEFORE UPDATE ON job_provenance
BEGIN
    SELECT RAISE(ABORT, 'job_provenance is append-only');
END;
