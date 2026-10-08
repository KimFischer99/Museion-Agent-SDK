from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import FakeClock, MemoryEntry, PASError, Store

T0 = 1_760_000_000_000


class PersistentContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "profile.db"
        self.store = self.open_store()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def open_store(self):
        return Store(
            str(self.path), profile="demo", owner_destination="local-inbox:demo",
            clock=FakeClock(wall_ms=T0),
        )

    def test_memory_and_preferences_survive_reopen_and_upsert_by_id(self):
        self.assertEqual(self.store.schema_version(), 11)
        first = MemoryEntry("m1", "old", "user", evidence_refs=("ref:1",))
        self.store.remember_memory(first, now_ms=T0)
        self.store.remember_memory(
            MemoryEntry("m1", "new", "user", evidence_refs=("ref:2",)), now_ms=T0 + 1
        )
        self.assertEqual(
            self.store.set_proactive_preferences(
                {"enabled": False, "allowed_topics": ["  Work ", "work", "Health"]},
                now_ms=T0 + 2,
            ),
            {"enabled": False, "allowed_topics": ["work", "health"]},
        )
        self.store.close()
        self.store = self.open_store()
        self.assertEqual(self.store.recall_memory(), (
            MemoryEntry("m1", "new", "user", evidence_refs=("ref:2",)),
        ))
        self.assertEqual(self.store.get_proactive_preferences(), {
            "enabled": False, "allowed_topics": ["work", "health"]
        })

    def test_expiry_bounds_defaults_and_forget(self):
        self.assertEqual(self.store.get_proactive_preferences(), {
            "enabled": True, "allowed_topics": None
        })
        self.store.remember_memory(
            MemoryEntry("old", "expired", "user", expires_at="2025-10-09T08:53:20Z"),
            now_ms=T0 - 1,
        )
        self.store.remember_memory(MemoryEntry("new", "live", "user"), now_ms=T0)
        self.assertEqual([e.memory_id for e in self.store.recall_memory(now_ms=T0)], ["new"])
        self.assertTrue(self.store.forget_memory("new"))
        self.assertFalse(self.store.forget_memory("new"))
        for i in range(3):
            self.store.remember_memory(
                MemoryEntry(f"cap{i}", f"entry {i}", "user"),
                now_ms=T0 + i, max_entries=2,
            )
        self.assertEqual(
            [e.memory_id for e in self.store.recall_memory(limit=256)], ["cap1", "cap2"]
        )
        for invalid in (0, 257, True):
            with self.assertRaises(PASError):
                self.store.recall_memory(limit=invalid)
        with self.assertRaises(PASError):
            self.store.set_proactive_preferences({"enabled": 1, "allowed_topics": None})

    def test_export_and_wipe_include_completed_run_context_copy(self):
        self.store.upsert_job(_heartbeat(), idempotency_key="setup")
        self.store.admit_job_occurrence(
            "hb-1", expected_revision=1, kind="interval", slot_ms=T0,
            next_due_ms=T0 + 1000, now_ms=T0,
        )
        lease = self.store.claim_run(now_ms=T0, ttl_ms=60_000)
        self.store.complete_run(lease, now_ms=T0 + 1)
        self.store.remember_memory(MemoryEntry("m1", "copied private memory", "user"), now_ms=T0)
        self.store.set_proactive_preferences(
            {"enabled": True, "allowed_topics": []}, now_ms=T0
        )
        pack = {
            "schema_version": "1.0", "task": {"goal_id": "g", "scope": "s"},
            "locale": "en", "timezone": "UTC", "preferences_ref": "prefs:1",
            "sources": [], "pending_refs": [], "sent_fact_refs": [],
            "memory_refs": ["copied private memory"], "untrusted_content_policy": "data_only",
        }
        pack_json = json.dumps(pack, separators=(",", ":"), sort_keys=True)
        self.store.db.execute(
            """INSERT INTO context_packs(context_ref, run_id, event_id, pack_json,
               content_hash, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)""",
            ("ctx:full", lease.run_id, lease.event_id, pack_json,
             hashlib.sha256(pack_json.encode()).hexdigest(), T0),
        )
        dump = self.store.export_profile_data()
        self.assertIn("context_packs", dump)
        self.assertIn("memory_entries", dump)
        self.assertIn("proactive_preferences", dump)
        self.assertEqual(len(dump["context_packs"]), 1)
        deleted = self.store.wipe_profile_data()
        self.assertGreater(deleted["context_packs"], 0)
        self.assertGreater(deleted["memory_entries"], 0)
        self.assertGreater(deleted["proactive_preferences"], 0)
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        for table in ("context_packs", "memory_entries", "proactive_preferences"):
            self.assertEqual(self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)


def _heartbeat():
    from proactive_sdk import JobSpec

    return JobSpec(
        job_id="hb-1", mode="heartbeat",
        schedule={"kind": "interval", "anchor": "2025-10-09T08:53:20Z", "every_seconds": 60},
        task={"instruction": "check"}, revision=1,
    )


if __name__ == "__main__":
    unittest.main()
