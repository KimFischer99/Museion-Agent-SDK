"""Migration 012: a v1.1 database gains v2 reason/silence columns in place.

The upgrade must be additive and idempotent: old ``run_proposals`` rows are
backfilled to ``unspecified`` (no guessing), old ``runs`` rows keep a NULL
``silence_reason``, and reopening applies nothing further.
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
V11_MIGRATIONS = [m for m in MIGRATIONS if m[0] <= 11]


def build_v11_database(path: Path) -> None:
    """Create a schema-11 database with one old run and one old proposal."""
    db = sqlite3.connect(str(path), isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        for version, sql in V11_MIGRATIONS:
            for statement in _split_sql_statements(sql):
                db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at_ms) VALUES (?,?,?)",
                (version, "v11-placeholder", T0),
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
                                policy_version, created_at_ms, updated_at_ms)
               VALUES ('run-legacy', 'evt-legacy', 'proposed', 1, 1, ?, 1, ?, ?)""",
            (T0 + 300_000, T0, T0),
        )
        db.execute(
            """INSERT INTO run_proposals(proposal_id, run_id, seq, kind, fact_id,
                                         revision, body, evidence_refs_json, created_at_ms)
               VALUES ('prop-legacy', 'run-legacy', 1, 'notify_self', 'fact-1',
                       'rev-1', '旧正文', '[]', ?)""",
            (T0,),
        )
        db.execute("COMMIT")
    finally:
        db.close()


class MigrationV2ColumnsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "v11.db"
        build_v11_database(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _open(self) -> Store:
        return Store(
            str(self.db_path),
            profile="demo",
            owner_destination="local-inbox:demo",
            clock=FakeClock(wall_ms=T0),
        )

    def test_upgrade_backfills_old_proposals_and_keeps_runs_null(self):
        store = self._open()
        try:
            row = store.db.execute(
                "SELECT reason FROM run_proposals WHERE proposal_id='prop-legacy'"
            ).fetchone()
            self.assertEqual(row["reason"], "unspecified")
            run = store.db.execute(
                "SELECT silence_reason FROM runs WHERE run_id='run-legacy'"
            ).fetchone()
            self.assertIsNone(run["silence_reason"])
            applied = [
                r["version"]
                for r in store.db.execute("SELECT version FROM schema_migrations ORDER BY version")
            ]
            self.assertEqual(applied, list(range(1, len(MIGRATIONS) + 1)))
        finally:
            store.close()

    def test_reopening_applies_nothing_and_keeps_content(self):
        first = self._open()
        rows_before = first.db.execute(
            "SELECT proposal_id, reason FROM run_proposals"
        ).fetchall()
        first.close()

        second = self._open()
        try:
            rows_after = second.db.execute(
                "SELECT proposal_id, reason FROM run_proposals"
            ).fetchall()
            self.assertEqual([dict(r) for r in rows_before], [dict(r) for r in rows_after])
            latest = second.db.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()["v"]
            self.assertEqual(latest, len(MIGRATIONS))
        finally:
            second.close()

    def test_new_writes_use_the_new_columns(self):
        store = self._open()
        try:
            with store.transaction():
                store.db.execute(
                    "UPDATE run_proposals SET reason='changed' WHERE proposal_id='prop-legacy'"
                )
                store.db.execute(
                    "UPDATE runs SET silence_reason='no_source_change' WHERE run_id='run-legacy'"
                )
            run = store.get_run("run-legacy")
            self.assertEqual(run["silence_reason"], "no_source_change")
            proposal = store.run_proposals("run-legacy")[0]
            self.assertEqual(proposal["reason"], "changed")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
