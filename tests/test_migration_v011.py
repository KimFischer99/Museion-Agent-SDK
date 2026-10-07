"""SPEC §21.2: a v0.1.0 database must upgrade in place.

The upgrade is interesting because migration 007 *relaxes* two baseline
constraints (``jobs.mode`` gains ``reminder``; ``actions.run_id`` becomes
nullable) by rebuilding those tables. This test builds a genuine v0.1.0
database — migrations 1–6 only, with real rows in every child table — and
then opens it with the v0.1.1 binary.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import FakeClock, JobSpec, Store  # noqa: E402
from proactive_sdk.store import MIGRATIONS, _split_sql_statements  # noqa: E402

T0 = 1_760_000_000_000
V010_MIGRATIONS = [m for m in MIGRATIONS if m[0] <= 6]


def build_v010_database(path: Path) -> None:
    """Create a schema-6 database with representative rows."""
    db = sqlite3.connect(str(path), isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        for version, sql in V010_MIGRATIONS:
            for statement in _split_sql_statements(sql):
                db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at_ms) VALUES (?,?,?)",
                (version, "v010-placeholder", T0),
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
               VALUES ('legacy','3',1,'task','pas','{"kind":"interval","anchor":"2025-10-09T00:00:00Z","every_seconds":1800}',
                       '{"instruction":"legacy job"}', NULL, ?, ?, 'grace_once','["g1"]','{}')""",
            (T0, T0),
        )
        db.execute(
            """INSERT INTO grants(grant_id, account_ref, capability, scope_json, version,
                                   consent_evidence_ref, created_at_ms)
               VALUES ('g1','account:primary','notify.self','{}',1,'consent:legacy',?)""",
            (T0,),
        )
        db.execute(
            """INSERT INTO events(event_id, idempotency_key, origin, job_id, job_revision,
                                   occurrence_id, payload_hash, payload_ref, payload_json,
                                   observed_at_ms, expires_at_ms)
               VALUES ('evt1','occ-1','scheduler','legacy',3,'occ-1','h','inline','{}',?,?)""",
            (T0, T0 + 1000),
        )
        db.execute(
            """INSERT INTO runs(run_id, event_id, state, deadline_ms, policy_version,
                                 created_at_ms, updated_at_ms)
               VALUES ('run1','evt1','completed',?,1,?,?)""",
            (T0 + 1000, T0, T0),
        )
        db.execute(
            """INSERT INTO actions(action_id, run_id, business_key, kind, request_json,
                                    request_hash, grant_id, grant_version, policy_version,
                                    state, expires_at_ms)
               VALUES ('act1','run1','biz-legacy','notify_self','{}','h1','g1',1,1,'completed',?)""",
            (T0 + 1000,),
        )
        db.execute(
            """INSERT INTO outbox(message_id, action_id, delivery_key, destination_ref,
                                   payload_ref, state, not_before_ms, expires_at_ms,
                                   provider_key, payload_json, created_at_ms)
               VALUES ('msg1','act1','biz-legacy','local-inbox:demo','blob:x',
                       'stored_in_inbox',?,?, 'pas-act1','{"title":"legacy"}',?)""",
            (T0, T0 + 1000, T0),
        )
        db.execute(
            """INSERT INTO job_occurrences(occurrence_id, job_id, job_revision, kind,
                                           slot_ms, state, reason, event_id, recorded_at_ms)
               VALUES ('occ-1','legacy',3,'interval',?, 'admitted', NULL, 'evt1', ?)""",
            (T0, T0),
        )
        db.execute(
            """INSERT INTO inbox(inbox_id, message_id, title, body, delivered_at_ms)
               VALUES ('inb1','msg1','legacy','old body',?)""",
            (T0,),
        )
        db.commit()
    finally:
        db.close()


class MigrationV011Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "pas.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_v010_database_upgrades_in_place_without_losing_rows(self):
        build_v010_database(self.db)
        store = Store(
            str(self.db), profile="demo", owner_destination="local-inbox:demo",
            clock=FakeClock(wall_ms=T0),
        )
        try:
            self.assertEqual(store.schema_version(), len(MIGRATIONS))
            self.assertEqual(store.integrity_check(), "ok")

            # The legacy job keeps its revision, task text and default policy.
            job = store.get_job("legacy")
            self.assertIsNotNone(job)
            self.assertEqual(job.revision, 3)
            self.assertEqual(job.mode, "task")
            self.assertEqual(job.task["instruction"], "legacy job")
            self.assertIsNone(job.reminder)
            # v0.1.0 rows have no stated obligation; nothing is retroactively
            # promoted to "due".
            self.assertEqual(job.obligation, "opportunistic")
            self.assertIsNone(job.stopped_at_ms)

            # Every ledger row survived the table rebuild.
            self.assertEqual(len(store.occurrences("legacy")), 1)
            self.assertEqual(len(store.list_runs()), 1)
            self.assertEqual(len(store.list_actions()), 1)
            self.assertEqual(len(store.list_outbox()), 1)
            self.assertEqual(len(store.list_inbox()), 1)

            # Foreign keys are enforced again after the migration pass.
            self.assertEqual(store.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(store.db.execute("PRAGMA foreign_key_check").fetchall(), [])

            # The rebuilt tables carry the relaxed constraints.
            store.upsert_job(
                JobSpec(
                    job_id="new-reminder",
                    mode="reminder",
                    schedule={"kind": "runonce", "at": "2026-01-01T09:00:00Z"},
                    task={},
                    reminder={"body": "x", "timezone": "UTC"},
                ),
                idempotency_key="k-reminder",
            )
            self.assertEqual(store.get_job("new-reminder").mode, "reminder")
            with store.transaction():
                store.db.execute(
                    """INSERT INTO actions(action_id, run_id, business_key, kind, request_json,
                                           request_hash, grant_id, grant_version, policy_version,
                                           state, expires_at_ms, source)
                       VALUES ('act2', NULL, 'biz-2', 'direct_reminder', '{}', 'h2', 'g1', 1, 1,
                               'queued', ?, 'reminder')""",
                    (T0 + 1000,),
                )
        finally:
            store.close()

    def test_reopening_an_upgraded_database_applies_nothing_further(self):
        build_v010_database(self.db)
        first = Store(str(self.db), profile="demo", owner_destination="local-inbox:demo",
                      clock=FakeClock(wall_ms=T0))
        applied = [
            row["version"]
            for row in first.db.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        first.close()
        second = Store(str(self.db), profile="demo", owner_destination="local-inbox:demo",
                       clock=FakeClock(wall_ms=T0))
        try:
            again = [
                row["version"]
                for row in second.db.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            self.assertEqual(applied, again)
            self.assertEqual(again, list(range(1, len(MIGRATIONS) + 1)))
        finally:
            second.close()

    def test_child_references_still_point_at_the_rebuilt_tables(self):
        build_v010_database(self.db)
        store = Store(str(self.db), profile="demo", owner_destination="local-inbox:demo",
                      clock=FakeClock(wall_ms=T0))
        try:
            def target(table: str) -> str:
                fks = store.db.execute(f"PRAGMA foreign_key_list({table})").fetchall()
                return {row["table"] for row in fks}

            for child in ("events", "job_occurrences", "job_activity", "job_grants"):
                self.assertIn("jobs", target(child) or {"jobs"})
            self.assertIn("actions", target("outbox"))
            self.assertIn("runs", target("actions"))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
