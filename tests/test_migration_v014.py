"""Migrations 015-017: a schema-14 database gains the v3 tables in place.

The upgrade must be additive and idempotent. Legacy runs get a NULL
checkpoint and ``resume_count`` 0; a run whose decision was already committed
(``context_ref`` set) becomes ``stage='proposed'`` so it can never be claimed
as a resumable checkpoint. Reopening applies nothing further.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import FakeClock, Store  # noqa: E402
from proactive_sdk.store import MIGRATIONS, _split_sql_statements  # noqa: E402

T0 = 1_760_000_000_000
V14_MIGRATIONS = [m for m in MIGRATIONS if m[0] <= 14]
V3_TABLES = ("memory_candidates", "memory_forget_blocks", "goals", "goal_entries")


def build_v14_database(path: Path) -> None:
    """Create a schema-14 database with one committed and one open run."""
    db = sqlite3.connect(str(path), isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        for version, sql in V14_MIGRATIONS:
            for statement in _split_sql_statements(sql):
                db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at_ms) VALUES (?,?,?)",
                (version, "v14-placeholder", T0),
            )
        db.execute(
            "INSERT INTO meta(key, value) VALUES ('identity', ?)",
            ('{"owner":"local-inbox:demo","profile":"demo"}',),
        )
        db.execute(
            """INSERT INTO events(event_id, idempotency_key, origin, payload_hash,
                                  payload_ref, observed_at_ms, expires_at_ms)
               VALUES ('evt-legacy', 'idem-legacy', 'scheduler', 'ph',
                       'pref:legacy', ?, ?)""",
            (T0, T0 + 3_600_000),
        )
        db.execute(
            """INSERT INTO runs(run_id, event_id, state, attempt, fence, deadline_ms,
                                policy_version, context_ref, created_at_ms, updated_at_ms)
               VALUES ('run-legacy-committed', 'evt-legacy', 'actions_queued', 1, 1, ?,
                       1, 'ctx:legacy', ?, ?)""",
            (T0 + 300_000, T0, T0),
        )
        db.execute(
            """INSERT INTO events(event_id, idempotency_key, origin, payload_hash,
                                  payload_ref, observed_at_ms, expires_at_ms)
               VALUES ('evt-legacy-open', 'idem-legacy-open', 'scheduler', 'ph',
                       'pref:legacy-open', ?, ?)""",
            (T0, T0 + 3_600_000),
        )
        db.execute(
            """INSERT INTO runs(run_id, event_id, state, attempt, fence, deadline_ms,
                                policy_version, created_at_ms, updated_at_ms)
               VALUES ('run-legacy-open', 'evt-legacy-open', 'completed', 1, 1, ?,
                       1, ?, ?)""",
            (T0 + 300_000, T0, T0),
        )
        db.execute("COMMIT")
    finally:
        db.close()


class MigrationV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "v14.db"
        build_v14_database(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _open(self) -> Store:
        return Store(
            str(self.db_path),
            profile="demo",
            owner_destination="local-inbox:demo",
            clock=FakeClock(wall_ms=T0),
        )

    def test_upgrade_adds_v3_state_without_touching_legacy_rows(self):
        store = self._open()
        try:
            self.assertEqual(store.schema_version(), 17)
            committed = store.db.execute(
                "SELECT state, stage, checkpoint_json, checkpoint_hash, input_hash,"
                " resume_count FROM runs WHERE run_id='run-legacy-committed'"
            ).fetchone()
            # A committed decision must never become a resumable checkpoint.
            self.assertEqual(committed["stage"], "proposed")
            self.assertEqual(committed["state"], "actions_queued")
            self.assertIsNone(committed["checkpoint_json"])
            self.assertIsNone(committed["checkpoint_hash"])
            self.assertIsNone(committed["input_hash"])
            self.assertEqual(committed["resume_count"], 0)
            open_run = store.db.execute(
                "SELECT stage FROM runs WHERE run_id='run-legacy-open'"
            ).fetchone()
            self.assertIsNone(open_run["stage"])
            applied = [
                r["version"]
                for r in store.db.execute("SELECT version FROM schema_migrations ORDER BY version")
            ]
            self.assertEqual(applied, list(range(1, len(MIGRATIONS) + 1)))
            present = {
                r["name"]
                for r in store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                if r["name"] in V3_TABLES
            }
            self.assertEqual(present, set(V3_TABLES))
        finally:
            store.close()

    def test_reopening_applies_nothing_and_v3_writes_work(self):
        first = self._open()
        try:
            goal = first.create_goal("goal-legacy", title="迁移后的目标", now_ms=T0)
            self.assertEqual(goal["status"], "active")
        finally:
            first.close()

        second = self._open()
        try:
            latest = second.db.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()["v"]
            self.assertEqual(latest, len(MIGRATIONS))
            self.assertEqual(
                second.db.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"],
                len(MIGRATIONS),
            )
            self.assertEqual(second.get_goal("goal-legacy", now_ms=T0)["status"], "active")
            self.assertEqual(
                second.db.execute(
                    "SELECT COUNT(*) AS n FROM goal_entries WHERE goal_id='goal-legacy'"
                ).fetchone()["n"],
                1,
            )
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main()
