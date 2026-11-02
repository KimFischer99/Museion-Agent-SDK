"""Stage D: task authorization provenance must be atomic and honest.

One behavioral case covers the complete increment required by the plan:
old databases stay readable with an explicit "not recorded" status, new
job revisions append source records in the same transaction, idempotent
replays do not double-book, and read-only history never revives a revoked
grant.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import FakeClock, JobSpec, PASError, ProactiveAgent  # noqa: E402
from proactive_sdk.contracts import content_hash  # noqa: E402
from proactive_sdk.store import MIGRATIONS, JobWriteSource, _split_sql_statements  # noqa: E402

T0 = 1_760_000_000_000
V18 = [migration for migration in MIGRATIONS if migration[0] <= 18]


class _NoopExecutor:
    async def context(self):
        raise AssertionError("provenance test must not execute a model")

    async def execute(self, *_args, **_kwargs):
        raise AssertionError("provenance test must not execute a model")


def _build_v18_database(path: Path) -> None:
    db = sqlite3.connect(str(path), isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        for version, sql in V18:
            for statement in _split_sql_statements(sql):
                db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at_ms) VALUES (?,?,?)",
                (version, "stage-d-v18-placeholder", T0),
            )
        db.execute(
            "INSERT INTO meta(key, value) VALUES ('identity', ?)",
            ('{"owner":"local-inbox:demo","profile":"demo"}',),
        )
        db.execute(
            """INSERT INTO jobs(job_id, revision, enabled, mode, scheduler_owner,
                                schedule_json, task_json, next_due_ms, updated_at_ms,
                                created_at_ms, misfire_policy, grant_refs_json,
                                delivery_policy_json)
               VALUES ('legacy', 3, 1, 'task', 'pas',
                       '{"kind":"interval","anchor":"2025-10-09T00:00:00Z","every_seconds":1800}',
                       '{"instruction":"legacy task"}', NULL, ?, ?,
                       'grace_once', '["grant-legacy"]', '{}')""",
            (T0, T0),
        )
        db.execute("COMMIT")
    finally:
        db.close()


def _spec(job_id: str, revision: int, instruction: str, grant_refs=()) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        revision=revision,
        mode="task",
        schedule={"kind": "runonce", "at": "2026-11-02T00:00:00Z"},
        task={"instruction": instruction},
        grant_refs=tuple(grant_refs),
    )


class JobProvenanceTests(unittest.TestCase):
    def test_job_provenance_is_atomic_idempotent_and_legacy_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pas.sqlite3"
            _build_v18_database(db_path)
            clock = FakeClock(wall_ms=T0)
            agent = ProactiveAgent(
                state_dir=tmp,
                executor=_NoopExecutor(),
                profile="demo",
                owner_destination="local-inbox:demo",
                clock=clock,
            )
            try:
                # An upgraded v18 job remains readable. Its missing source
                # history is reported explicitly, never inferred from the
                # current task or current grants.
                self.assertEqual(agent.store.schema_version(), len(MIGRATIONS))
                legacy = agent.jobs_get("legacy")
                self.assertIsNotNone(legacy)
                self.assertEqual(legacy.revision, 3)
                legacy_source = agent.jobs_provenance("legacy")
                self.assertEqual(legacy_source["source_status"], "not_recorded")
                self.assertEqual(legacy_source["current"]["source_label"], "来源未记录")
                self.assertFalse(legacy_source["current"]["recorded"])
                self.assertEqual(legacy_source["revisions"], [])

                # A provenance-write failure rolls back the jobs row and
                # idempotency row together: a failed write cannot leave a
                # half-recorded task or a false source history.
                with patch.object(
                    agent.store,
                    "_write_job_provenance_tx",
                    side_effect=RuntimeError("injected provenance failure"),
                ):
                    with self.assertRaises(RuntimeError):
                        agent.jobs_upsert(
                            _spec("atomic", 1, "must not survive"),
                            idempotency_key="atomic-key",
                            provenance=JobWriteSource(
                                entry_kind="rpc", actor="owner-local", request_id="atomic-req"
                            ),
                        )
                self.assertIsNone(agent.jobs_get("atomic"))
                self.assertEqual(agent.store.list_job_provenance("atomic"), [])
                self.assertIsNone(
                    agent.store.db.execute(
                        "SELECT 1 FROM jobs_idempotency WHERE idempotency_key='atomic-key'"
                    ).fetchone()
                )

                # Revoked authority is historical evidence only. Recording
                # the ref in provenance must not make it active again.
                grant = agent.grant("notify.self", evidence="consent:stage-d-test")
                agent.store.revoke_grant(grant.grant_id, now_ms=T0 + 1)
                raw_input = "a raw chat sentence that must not be stored"
                first_source = JobWriteSource(
                    entry_kind="rpc",
                    actor="owner-local",
                    request_id="request-1",
                    host_ref="client:test",
                    input_hash=content_hash({"text": raw_input}),
                )
                first = agent.jobs_upsert(
                    _spec("fresh", 1, "first version", grant_refs=(grant.grant_id,)),
                    idempotency_key="fresh-key-1",
                    provenance=first_source,
                )
                self.assertEqual(first.revision, 1)
                self.assertEqual(len(agent.store.list_job_provenance("fresh")), 1)

                # Exact replay returns the same record and adds no second
                # provenance entry.
                replay = agent.jobs_upsert(
                    _spec("fresh", 1, "first version", grant_refs=(grant.grant_id,)),
                    idempotency_key="fresh-key-1",
                    provenance=first_source,
                )
                self.assertEqual(replay.revision, 1)
                self.assertEqual(len(agent.store.list_job_provenance("fresh")), 1)

                # A real edit appends a new version. The first snapshot keeps
                # the revoked grant ref; no later state rewrites it.
                agent.jobs_upsert(
                    _spec("fresh", 2, "second version"),
                    idempotency_key="fresh-key-2",
                    provenance=JobWriteSource(
                        entry_kind="rpc",
                        actor="owner-local",
                        request_id="request-2",
                        host_ref="client:test",
                    ),
                )
                history = agent.store.list_job_provenance("fresh")
                self.assertEqual([row.revision for row in history], [1, 2])
                self.assertEqual(history[0].grant_refs, (grant.grant_id,))
                self.assertEqual(history[0].task["instruction"], "first version")
                self.assertEqual(history[0].entry_kind, "rpc")
                self.assertEqual(history[0].authenticated_actor, "owner-local")
                self.assertEqual(history[0].request_id, "request-1")
                self.assertEqual(history[0].host_ref, "client:test")
                self.assertIs(agent.store.get_grant(grant.grant_id).is_active(T0 + 2), False)
                current_view = agent.jobs_provenance("fresh")
                self.assertTrue(current_view["recorded"])
                self.assertEqual(current_view["source_label"], "来源已记录")
                # The digest is retained; the raw sentence is not.
                self.assertNotIn(raw_input, json.dumps(current_view, ensure_ascii=False))
                self.assertEqual(len(history[0].input_hash), 64)
            finally:
                asyncio.run(agent.close())


if __name__ == "__main__":
    unittest.main()
