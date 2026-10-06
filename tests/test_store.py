"""P1 store tests: migrations, identity, jobs API, event admission,
run claim/fencing — including real-process kill and two-connection
concurrency (SPEC §16.1 Transactions/Operations rows, STORE-01)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import ErrorCode, FakeClock, JobSpec, PASError, Store
from proactive_sdk.store import MIGRATION_COUNT, MIGRATIONS

T0 = 1_760_000_000_000  # 2025-10-09T08:53:20Z; arbitrary fixed wall time


def make_store(path, *, clock=None, profile="demo", owner="local-inbox:demo"):
    return Store(
        str(path), profile=profile, owner_destination=owner,
        clock=clock or FakeClock(wall_ms=T0),
    )


def heartbeat_spec(job_id="hb-1", *, revision=1, enabled=True, anchor_s=0):
    return JobSpec(
        job_id=job_id,
        mode="heartbeat",
        schedule={
            "kind": "interval",
            "anchor": f"2025-10-09T0{anchor_s}:00:00Z",
            "every_seconds": 1800,
        },
        task={"instruction": "检查跟踪的来源是否有变化。"},
        revision=revision,
        enabled=enabled,
    )


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "p.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_database_applies_all_migrations_with_checksums(self):
        store = make_store(self.db)
        try:
            rows = store.db.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual([r["version"] for r in rows], list(range(1, MIGRATION_COUNT + 1)))
            import hashlib

            for row, (_, sql) in zip(rows, MIGRATIONS):
                self.assertEqual(row["checksum"], hashlib.sha256(sql.encode()).hexdigest())
            tables = {
                r["name"]
                for r in store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for expected in ("jobs", "events", "runs", "job_occurrences", "jobs_idempotency",
                             "outbox", "delivery_attempts", "grants", "approvals"):
                self.assertIn(expected, tables)
        finally:
            store.close()

    def test_reopen_is_idempotent(self):
        store = make_store(self.db)
        store.close()
        store = make_store(self.db)
        try:
            count = store.db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(count, MIGRATION_COUNT)
        finally:
            store.close()

    def test_newer_schema_refuses_old_binary(self):
        store = make_store(self.db)
        try:
            store.db.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at_ms) VALUES (99,'x',1)"
            )
        finally:
            store.close()
        with self.assertRaises(PASError) as ctx:
            make_store(self.db)
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)
        self.assertIn("newer", ctx.exception.safe_message)

    def test_failed_migration_rolls_back_completely(self):
        store = make_store(self.db)
        try:
            broken = "CREATE TABLE trixie(a INTEGER); THIS IS NOT SQL;"
            with self.assertRaises(sqlite3.OperationalError):
                with store.transaction():
                    store._apply_migration(MIGRATION_COUNT + 1, broken)
            tables = {
                r["name"]
                for r in store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertNotIn("trixie", tables)
            versions = [
                r[0]
                for r in store.db.execute("SELECT version FROM schema_migrations").fetchall()
            ]
            self.assertNotIn(MIGRATION_COUNT + 1, versions)
        finally:
            store.close()

    def test_migrations_carry_additive_p1_columns(self):
        store = make_store(self.db)
        try:
            job_cols = {
                r["name"]
                for r in store.db.execute("PRAGMA table_info(jobs)").fetchall()
            }
            for col in ("created_at_ms", "misfire_policy", "deadline_ms",
                        "grant_refs_json", "delivery_policy_json"):
                self.assertIn(col, job_cols)
            event_cols = {
                r["name"] for r in store.db.execute("PRAGMA table_info(events)").fetchall()
            }
            self.assertIn("payload_json", event_cols)
        finally:
            store.close()


class IdentityAndPragmaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "p.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_profile_identity_is_enforced(self):
        store = make_store(self.db)
        try:
            with self.assertRaises(PASError) as ctx:
                make_store(self.db, profile="other")
            self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)
            with self.assertRaises(PASError):
                make_store(self.db, owner="local-inbox:someone-else")
        finally:
            store.close()

    def test_sqlite_mode_is_delete_journal_full_sync(self):
        store = make_store(self.db)
        try:
            mode = store.db.execute("PRAGMA journal_mode").fetchone()[0]
            sync = store.db.execute("PRAGMA synchronous").fetchone()[0]
            fk = store.db.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(mode, "delete")
            self.assertEqual(sync, 2)  # FULL
            self.assertEqual(fk, 1)
        finally:
            store.close()

    def test_foreign_keys_are_enforced(self):
        import sqlite3

        store = make_store(self.db)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                with store.transaction():
                    store.db.execute(
                        "INSERT INTO runs(run_id, event_id, state, deadline_ms,"
                        " policy_version, created_at_ms, updated_at_ms)"
                        " VALUES ('r','missing-event','queued',1,1,1,1)"
                    )
        finally:
            store.close()


class JobApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "p.db"
        self.store = make_store(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_create_and_read_roundtrip(self):
        spec = JobSpec(
            job_id="daily-agenda",
            mode="task",
            schedule={"kind": "daily", "local_time": "09:00", "timezone": "Europe/Berlin"},
            task={"instruction": "总结今日日程", "goal_id": "agenda"},
            grant_refs=("grant:cal",),
            delivery_policy={"notification_profile": "owner-default"},
            deadline="2026-12-31T23:00:00Z",
        )
        rec = self.store.upsert_job(spec, idempotency_key="setup-1")
        self.assertEqual(rec.revision, 1)
        self.assertTrue(rec.enabled)
        self.assertEqual(rec.misfire_policy, "grace_once")  # task default
        self.assertIsNotNone(rec.deadline_ms)
        self.assertEqual(rec.grant_refs, ("grant:cal",))
        self.assertEqual(rec.delivery_policy["notification_profile"], "owner-default")
        again = self.store.get_job("daily-agenda")
        self.assertEqual(again.schedule["timezone"], "Europe/Berlin")
        self.assertEqual(again.task["goal_id"], "agenda")

    def test_heartbeat_default_misfire_is_coalesce_latest(self):
        rec = self.store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        self.assertEqual(rec.misfire_policy, "coalesce_latest")

    def test_idempotent_replay_returns_same_record(self):
        spec = heartbeat_spec()
        first = self.store.upsert_job(spec, idempotency_key="setup-hb")
        second = self.store.upsert_job(spec, idempotency_key="setup-hb")
        self.assertEqual(first.revision, second.revision)
        self.assertEqual(first.updated_at_ms, second.updated_at_ms)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1
        )

    def test_idempotency_key_reuse_with_different_content_conflicts(self):
        self.store.upsert_job(heartbeat_spec(), idempotency_key="k")
        with self.assertRaises(PASError) as ctx:
            self.store.upsert_job(
                heartbeat_spec(job_id="hb-1", revision=2, anchor_s=1),
                idempotency_key="k",
            )
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)

    def test_optimistic_revision_requires_exact_increment(self):
        self.store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        with self.assertRaises(PASError) as ctx:
            self.store.upsert_job(heartbeat_spec(revision=3), idempotency_key="k2")
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)
        rec = self.store.upsert_job(heartbeat_spec(revision=2), idempotency_key="k3")
        self.assertEqual(rec.revision, 2)

    def test_set_job_enabled_revisions_and_conflicts(self):
        self.store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        paused = self.store.set_job_enabled("hb-1", expected_revision=1, enabled=False)
        self.assertEqual(paused.revision, 2)
        self.assertFalse(paused.enabled)
        with self.assertRaises(PASError) as ctx:
            self.store.set_job_enabled("hb-1", expected_revision=1, enabled=True)
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)
        resumed = self.store.set_job_enabled("hb-1", expected_revision=2, enabled=True)
        self.assertEqual(resumed.revision, 3)
        self.assertTrue(resumed.enabled)

    def test_delete_job_with_history_is_refused(self):
        self.store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        self.store.admit_job_occurrence(
            "hb-1", expected_revision=1, kind="interval", slot_ms=T0,
            next_due_ms=T0 + 1, now_ms=T0,
        )
        with self.assertRaises(PASError) as ctx:
            self.store.delete_job("hb-1")
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)
        # A job without admitted occurrences deletes cleanly.
        self.store.upsert_job(heartbeat_spec(job_id="hb-2"), idempotency_key="k2")
        self.store.delete_job("hb-2")
        self.assertIsNone(self.store.get_job("hb-2"))


class EventAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "p.db"
        self.store = make_store(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_same_dedupe_key_same_payload_is_single_event(self):
        e1 = self.store.admit_event(
            "hook:h1:inv-1", origin="hook", payload={"id": "x"},
            observed_at_ms=T0, expires_at_ms=T0 + 1000,
        )
        e2 = self.store.admit_event(
            "hook:h1:inv-1", origin="hook", payload={"id": "x"},
            observed_at_ms=T0 + 5, expires_at_ms=T0 + 1000,
        )
        self.assertEqual(e1, e2)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1
        )

    def test_same_dedupe_key_different_payload_conflicts(self):
        self.store.admit_event(
            "k", origin="manual", payload={"a": 1},
            observed_at_ms=T0, expires_at_ms=T0 + 1,
        )
        with self.assertRaises(PASError) as ctx:
            self.store.admit_event(
                "k", origin="manual", payload={"a": 2},
                observed_at_ms=T0, expires_at_ms=T0 + 1,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)

    def test_payload_over_budget_rejected(self):
        with self.assertRaises(PASError) as ctx:
            self.store.admit_event(
                "big", origin="manual", payload={"blob": "x" * 70_000},
                observed_at_ms=T0, expires_at_ms=T0 + 1,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)

    def test_unknown_origin_rejected(self):
        with self.assertRaises(PASError):
            self.store.admit_event(
                "k", origin="model", payload={}, observed_at_ms=T0, expires_at_ms=T0 + 1,
            )

    def test_create_run_optional(self):
        with_run = self.store.admit_event(
            "k1", origin="manual", payload={}, observed_at_ms=T0, expires_at_ms=T0 + 1,
        )
        no_run = self.store.admit_event(
            "k2", origin="manual", payload={}, observed_at_ms=T0,
            expires_at_ms=T0 + 1, create_run=False,
        )
        runs = self.store.db.execute(
            "SELECT event_id FROM runs"
        ).fetchall()
        self.assertEqual([r["event_id"] for r in runs], [with_run])
        self.assertNotIn(no_run, [r["event_id"] for r in runs])

    def test_admit_job_occurrence_dedupes_across_retries(self):
        self.store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        first = self.store.admit_job_occurrence(
            "hb-1", expected_revision=1, kind="interval", slot_ms=T0,
            next_due_ms=T0 + 1800_000, now_ms=T0,
        )
        second = self.store.admit_job_occurrence(
            "hb-1", expected_revision=1, kind="interval", slot_ms=T0,
            next_due_ms=T0 + 1800_000, now_ms=T0 + 5,
        )
        self.assertEqual(first, "admitted")
        self.assertEqual(second, "already_admitted")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM job_occurrences").fetchone()[0], 1)

    def test_admission_verifies_enabled_and_revision_inside_transaction(self):
        self.store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        self.assertEqual(
            self.store.admit_job_occurrence(
                "hb-1", expected_revision=9, kind="interval", slot_ms=T0,
                next_due_ms=T0, now_ms=T0,
            ),
            "skipped_revision",
        )
        self.store.set_job_enabled("hb-1", expected_revision=1, enabled=False)
        self.assertEqual(
            self.store.admit_job_occurrence(
                "hb-1", expected_revision=2, kind="interval", slot_ms=T0,
                next_due_ms=T0, now_ms=T0,
            ),
            "skipped_disabled",
        )
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)

    def test_event_payload_is_stored_canonical(self):
        event_id = self.store.admit_event(
            "k", origin="manual", payload={"b": 2, "a": 1},
            observed_at_ms=T0, expires_at_ms=T0 + 1,
        )
        row = self.store.db.execute(
            "SELECT payload_json, payload_ref, payload_hash FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self.assertEqual(json.loads(row["payload_json"]), {"a": 1, "b": 2})
        self.assertEqual(row["payload_ref"], "inline")
        import hashlib

        self.assertEqual(
            row["payload_hash"],
            hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest(),
        )


class ClaimFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "p.db"
        self.store = make_store(self.db)
        self.store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        self.store.admit_job_occurrence(
            "hb-1", expected_revision=1, kind="interval", slot_ms=T0,
            next_due_ms=T0 + 1800_000, now_ms=T0,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_claim_sets_running_with_increased_fence(self):
        lease = self.store.claim_run(now_ms=T0, ttl_ms=60_000)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.fence, 1)
        self.assertEqual(lease.lease_until_ms, T0 + 60_000)
        self.assertIsNone(self.store.claim_run(now_ms=T0 + 1, ttl_ms=60_000))

    def test_expired_lease_is_reclaimable_with_new_fence(self):
        first = self.store.claim_run(now_ms=T0, ttl_ms=60_000)
        second = self.store.claim_run(now_ms=T0 + 61_000, ttl_ms=60_000)
        self.assertIsNotNone(second)
        self.assertEqual(second.fence, first.fence + 1)

    def test_stale_fence_cannot_commit(self):
        stale = self.store.claim_run(now_ms=T0, ttl_ms=60_000)
        self.store.claim_run(now_ms=T0 + 61_000, ttl_ms=60_000)  # fence 2
        with self.assertRaises(PASError) as ctx:
            self.store.complete_run(stale, now_ms=T0 + 62_000)
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)

    def test_complete_requires_current_fence_and_live_lease(self):
        lease = self.store.claim_run(now_ms=T0, ttl_ms=60_000)
        with self.assertRaises(PASError):
            self.store.complete_run(lease, now_ms=T0 + 61_000)  # lease expired
        fresh = self.store.claim_run(now_ms=T0 + 62_000, ttl_ms=60_000)
        self.store.complete_run(fresh, now_ms=T0 + 63_000)
        state = self.store.db.execute(
            "SELECT state FROM runs WHERE run_id=?", (fresh.run_id,)
        ).fetchone()[0]
        self.assertEqual(state, "planned")
        with self.assertRaises(PASError):
            self.store.complete_run(fresh, now_ms=T0 + 64_000)  # already planned

    def test_fail_run_records_error_class_with_fence(self):
        lease = self.store.claim_run(now_ms=T0, ttl_ms=60_000)
        self.store.fail_run(lease, error_class="provider_unavailable", now_ms=T0 + 1)
        row = self.store.db.execute(
            "SELECT state, error_class FROM runs WHERE run_id=?", (lease.run_id,)
        ).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_class"], "provider_unavailable")

    def test_claim_specific_run_id_misses_unknown_id(self):
        lease = self.store.claim_run(now_ms=T0, ttl_ms=60_000, run_id="run" + "0" * 28)
        self.assertIsNone(lease)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "p.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_survives_reopen(self):
        clock = FakeClock(wall_ms=T0)
        store = make_store(self.db, clock=clock)
        store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        store.admit_job_occurrence(
            "hb-1", expected_revision=1, kind="interval", slot_ms=T0,
            next_due_ms=T0 + 1800_000, now_ms=T0,
        )
        lease = store.claim_run(now_ms=T0, ttl_ms=60_000)
        store.close()

        clock.advance_wall(120_000)  # lease now expired
        reopened = make_store(self.db, clock=clock)
        try:
            self.assertIsNotNone(reopened.get_job("hb-1"))
            renewed = reopened.claim_run(now_ms=clock.wall_now_ms(), ttl_ms=60_000)
            self.assertIsNotNone(renewed)
            self.assertEqual(renewed.fence, lease.fence + 1)
        finally:
            reopened.close()


CHILD_SCRIPT = """
import json, os, sys
sys.path.insert(0, {src!r})
from proactive_sdk import Store

db_path, profile, owner, ttl = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
store = Store(db_path, profile=profile, owner_destination=owner)
lease = store.claim_run(now_ms=0, ttl_ms=ttl)
print(json.dumps({{
    "run_id": lease.run_id if lease else None,
    "fence": lease.fence if lease else None,
}}), flush=True)
# Simulate a hard crash mid-work: the claim exists, nothing is completed.
os._exit(1)
"""


class KilledWorkerTests(unittest.TestCase):
    """SPEC §16.1: atomicity tests need a real process kill, not a mocked
    exception. The child claims a run and dies without completing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "p.db")
        self.src = str(Path(__file__).resolve().parents[1] / "src")

    def tearDown(self):
        self.tmp.cleanup()

    def _spawn_child(self, ttl_ms: int) -> dict:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                CHILD_SCRIPT.format(src=self.src),
                self.db,
                "demo",
                "local-inbox:demo",
                str(ttl_ms),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)  # crashed on purpose
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_killed_worker_lease_is_reclaimed_not_resumed(self):
        clock = FakeClock(wall_ms=0)
        store = Store(self.db, profile="demo", owner_destination="local-inbox:demo", clock=clock)
        store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        store.admit_job_occurrence(
            "hb-1", expected_revision=1, kind="interval", slot_ms=0,
            next_due_ms=1800_000, now_ms=0,
        )
        store.close()

        child = self._spawn_child(ttl_ms=60_000)
        self.assertIsNotNone(child["run_id"])

        # Parent restarts with a fresh connection after the lease expires.
        clock.set_wall(120_000)
        parent = Store(self.db, profile="demo", owner_destination="local-inbox:demo", clock=clock)
        try:
            from proactive_sdk.store import RunLease

            old = RunLease(child["run_id"], _event_of(parent, child["run_id"]), child["fence"], 60_000)
            with self.assertRaises(PASError) as ctx:
                parent.complete_run(old, now_ms=clock.wall_now_ms())
            self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)

            renewed = parent.claim_run(now_ms=clock.wall_now_ms(), ttl_ms=60_000)
            self.assertIsNotNone(renewed)
            self.assertEqual(renewed.run_id, child["run_id"])
            self.assertEqual(renewed.fence, child["fence"] + 1)
        finally:
            parent.close()

    def test_two_processes_only_one_holds_the_lease(self):
        # Process A claims and keeps the lease; process B (independent
        # connection) must see no claimable run while the lease is alive.
        clock = FakeClock(wall_ms=0)
        store = Store(self.db, profile="demo", owner_destination="local-inbox:demo", clock=clock)
        store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        store.admit_job_occurrence(
            "hb-1", expected_revision=1, kind="interval", slot_ms=0,
            next_due_ms=1800_000, now_ms=0,
        )
        lease = store.claim_run(now_ms=0, ttl_ms=120_000)
        store.close()
        self.assertIsNotNone(lease)

        child = self._spawn_child(ttl_ms=60_000)  # claims nothing while A holds
        self.assertIsNone(child["run_id"])


def _event_of(store: Store, run_id: str) -> str:
    return store.db.execute(
        "SELECT event_id FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()[0]


class TwoConnectionContentionTests(unittest.TestCase):
    """Two independent connections racing for the same claim: exactly one
    wins. This is real SQLite serialization, not repeated function calls."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "p.db")
        store = Store(self.db, profile="demo", owner_destination="local-inbox:demo")
        store.upsert_job(heartbeat_spec(), idempotency_key="k1")
        store.admit_job_occurrence(
            "hb-1", expected_revision=1, kind="interval", slot_ms=0,
            next_due_ms=1800_000, now_ms=0,
        )
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_exactly_one_of_two_connections_claims(self):
        results = []

        def worker():
            conn = Store(self.db, profile="demo", owner_destination="local-inbox:demo")
            try:
                results.append(conn.claim_run(now_ms=0, ttl_ms=60_000))
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        leases = [r for r in results if r is not None]
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0].fence, 1)

    def test_distinct_runs_claimed_by_distinct_connections(self):
        store = Store(self.db, profile="demo", owner_destination="local-inbox:demo")
        for i in range(3):
            store.admit_event(
                f"manual:m{i}", origin="manual", payload={"i": i},
                observed_at_ms=0, expires_at_ms=60_000,
            )
        store.close()

        results = []

        def worker():
            conn = Store(self.db, profile="demo", owner_destination="local-inbox:demo")
            try:
                got = []
                while True:
                    lease = conn.claim_run(now_ms=0, ttl_ms=60_000)
                    if lease is None:
                        break
                    got.append(lease)
                    conn.complete_run(lease, now_ms=1)
                results.append(got)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        all_ids = [lease.run_id for group in results for lease in group]
        self.assertEqual(len(all_ids), 4)  # 1 job run + 3 manual runs
        self.assertEqual(len(set(all_ids)), 4)  # no double-claim


if __name__ == "__main__":
    unittest.main()
