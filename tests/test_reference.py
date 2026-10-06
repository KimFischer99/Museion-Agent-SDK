from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from reference_core import (ContractError, Conflict, HookResult, Ledger, SelfNotification,
                            coalesce_interval, delivery_allowed, normalized_skill_name,
                            parse_hook_output)


class ProtocolTests(unittest.TestCase):
    def parse(self, data: str):
        return parse_hook_output(data.encode(), 0)

    def test_wake_payload(self):
        x = self.parse('debug\nHATCH_HOOK_RESULT:{"decision":"wake","reason":"changed","payload":[1],"disable_after_run":true}\n')
        self.assertEqual(x.payload, [1])
        self.assertTrue(x.disable_after_run)

    def test_silent(self):
        self.assertEqual(self.parse('HATCH_HOOK_RESULT:{"decision":"silent","reason":"unchanged"}').decision, "silent")

    def test_missing_terminal(self):
        with self.assertRaises(ContractError): self.parse("no result")

    def test_two_terminal_records(self):
        line = 'HATCH_HOOK_RESULT:{"decision":"wake","reason":"x"}\n'
        with self.assertRaises(ContractError): self.parse(line + line)

    def test_output_after_result(self):
        with self.assertRaises(ContractError):
            self.parse('HATCH_HOOK_RESULT:{"decision":"wake","reason":"x"}\nafter')

    def test_duplicate_json_key(self):
        with self.assertRaises(ContractError):
            self.parse('HATCH_HOOK_RESULT:{"decision":"silent","decision":"wake","reason":"x"}')

    def test_nonfinite_json(self):
        with self.assertRaises(ContractError):
            self.parse('HATCH_HOOK_RESULT:{"decision":"wake","reason":"x","payload":NaN}')

    def test_non_boolean_disable(self):
        with self.assertRaises(ContractError):
            self.parse('HATCH_HOOK_RESULT:{"decision":"wake","reason":"x","disable_after_run":1}')

    def test_nonzero_exit_never_wakes(self):
        with self.assertRaises(ContractError): parse_hook_output(b'HATCH_HOOK_RESULT:{"decision":"wake","reason":"x"}', 1)

    def test_output_limit(self):
        with self.assertRaises(ContractError): parse_hook_output(b"x" * 5, 0, max_bytes=4)

    def test_utf8_required(self):
        with self.assertRaises(ContractError): parse_hook_output(b"\xff", 0)

    def test_skill_normalization(self):
        self.assertEqual(normalized_skill_name("google_calendar"), "google-calendar")
        with self.assertRaises(ContractError): normalized_skill_name("../escape")


class ClockTests(unittest.TestCase):
    def test_coalesce_not_replay_all(self):
        x = coalesce_interval(0, 1800, 86400, 0)
        self.assertEqual((x.due_at, x.next_at), (86400, 88200))

    def test_no_drift(self):
        self.assertEqual(coalesce_interval(0, 1800, 1817, 0).next_at, 3600)

    def test_same_slot_not_twice(self):
        self.assertIsNone(coalesce_interval(0, 1800, 1817, 1800).due_at)

    def test_clock_backwards(self):
        x = coalesce_interval(0, 1800, 500, 1800)
        self.assertEqual((x.due_at, x.next_at), (None, 3600))

    def test_future_anchor(self):
        self.assertEqual(coalesce_interval(1000, 60, 999, None).next_at, 1000)

    def test_invalid_period(self):
        with self.assertRaises(ContractError): coalesce_interval(0, 0, 0, None)

    def test_allowed_window_boundaries(self):
        self.assertTrue(delivery_allowed(datetime(2026, 1, 1, 9, tzinfo=timezone.utc), "UTC", 540, 1290))
        self.assertFalse(delivery_allowed(datetime(2026, 1, 1, 21, 30, tzinfo=timezone.utc), "UTC", 540, 1290))

    def test_cross_midnight_window(self):
        self.assertTrue(delivery_allowed(datetime(2026, 1, 1, 23, tzinfo=timezone.utc), "UTC", 1320, 420))
        self.assertFalse(delivery_allowed(datetime(2026, 1, 1, 12, tzinfo=timezone.utc), "UTC", 1320, 420))

    def test_fall_back_both_folds_same_rule(self):
        # New York repeats 01:30 on Nov 1, 2026. Neither is allowed in 09:00-21:30.
        for hour in (5, 6):
            self.assertFalse(delivery_allowed(datetime(2026, 11, 1, hour, 30, tzinfo=timezone.utc),
                                              "America/New_York", 540, 1290))

    def test_naive_time_rejected(self):
        with self.assertRaises(ContractError): delivery_allowed(datetime(2026, 1, 1), "UTC", 540, 1290)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.l = Ledger(self.path, profile="p1", owner_destination="inbox:p1")
        self.l.register_hook("h1")

    def tearDown(self):
        self.l.close()
        self.tmp.cleanup()

    def event(self, key="event-1"):
        return self.l.admit_event(key, {"id": key}, 100)

    def plan(self, key="event-1", fact="fact-1", revision="v1", expires=1000):
        self.event(key)
        lease = self.l.claim_run(101)
        self.l.complete_run(lease, [SelfNotification(fact, revision, "Update", expires)], 102)
        return lease

    def test_silent_probe_does_not_create_run(self):
        self.l.commit_probe("h1", "i1", 0, {"n": 1}, HookResult("silent", "no change"), 100)
        self.assertEqual(self.l.count("runs"), 0)
        self.assertEqual(self.l.db.execute("SELECT version FROM hooks").fetchone()[0], 1)

    def test_disable_and_admit_are_atomic(self):
        result = HookResult("wake", "done", {"value": 1}, True)
        eid = self.l.commit_probe("h1", "i1", 0, {"n": 1}, result, 100)
        self.assertIsNotNone(eid)
        self.assertEqual(self.l.db.execute("SELECT enabled FROM hooks").fetchone()[0], 0)
        self.assertEqual(self.l.count("runs"), 1)

    def test_probe_replay_is_idempotent(self):
        args = ("h1", "i1", 0, {}, HookResult("wake", "x"), 100)
        self.assertEqual(self.l.commit_probe(*args), self.l.commit_probe(*args))
        self.assertEqual(self.l.count("runs"), 1)

    def test_stale_hook_state_rejected(self):
        self.l.commit_probe("h1", "i1", 0, {}, HookResult("silent", "x"), 100)
        with self.assertRaises(Conflict):
            self.l.commit_probe("h1", "i2", 0, {}, HookResult("wake", "x"), 100)
        self.assertEqual(self.l.count("events"), 0)

    def test_failed_admission_rolls_back_hook_state(self):
        with patch.object(self.l, "_admit_event", side_effect=RuntimeError("injected crash")):
            with self.assertRaises(RuntimeError):
                self.l.commit_probe("h1", "i1", 0, {"n": 1}, HookResult("wake", "x", None, True), 100)
        row = self.l.db.execute("SELECT * FROM hooks").fetchone()
        self.assertEqual((row["version"], row["enabled"], row["state_json"]), (0, 1, "{}"))
        self.assertEqual(self.l.count("probe_commits"), 0)

    def test_dry_run_has_no_persistent_effect(self):
        self.l.commit_probe("h1", "i1", 0, {"n": 1}, HookResult("wake", "x", None, True), 100, dry_run=True)
        self.assertEqual(self.l.count("events"), 0)
        self.assertEqual(self.l.count("probe_commits"), 0)
        self.assertEqual(self.l.db.execute("SELECT version FROM hooks").fetchone()[0], 0)

    def test_event_replay_conflict(self):
        self.event()
        with self.assertRaises(Conflict): self.l.admit_event("event-1", {"changed": True}, 100)
        self.assertEqual(self.l.count("events"), 1)

    def test_restart_preserves_work(self):
        self.event()
        self.l.close()
        self.l = Ledger(self.path, profile="p1", owner_destination="inbox:p1")
        self.assertIsNotNone(self.l.claim_run(101))

    def test_no_second_claim_before_expiry(self):
        self.event()
        self.assertIsNotNone(self.l.claim_run(100, ttl=20))
        self.assertIsNone(self.l.claim_run(119))

    def test_stale_run_cannot_commit(self):
        self.event()
        old = self.l.claim_run(100, ttl=10)
        new = self.l.claim_run(111)
        self.assertGreater(new.token, old.token)
        with self.assertRaises(Conflict): self.l.complete_run(old, [], 112)
        self.l.complete_run(new, [], 112)

    def test_expired_run_cannot_commit_without_reclaim(self):
        self.event()
        old = self.l.claim_run(100, ttl=10)
        with self.assertRaises(Conflict): self.l.complete_run(old, [], 110)

    def test_same_fact_across_runs_not_notified_twice(self):
        self.plan("e1")
        self.plan("e2")
        self.assertEqual(self.l.count("outbox"), 1)

    def test_new_revision_can_notify_again(self):
        self.plan("e1", revision="v1")
        self.plan("e2", revision="v2")
        self.assertEqual(self.l.count("outbox"), 2)

    def test_quiet_time_defers_not_loses(self):
        self.plan()
        self.assertIsNone(self.l.claim_delivery(103, allowed=False))
        self.assertEqual(self.l.count("outbox", "pending"), 1)
        self.assertIsNotNone(self.l.claim_delivery(104, allowed=True))

    def test_expired_notification_is_not_sent(self):
        self.plan(expires=103)
        self.assertIsNone(self.l.claim_delivery(103, allowed=True))
        self.assertEqual(self.l.count("outbox", "expired"), 1)

    def test_delivery_requires_receipt(self):
        self.plan()
        delivery = self.l.claim_delivery(103, allowed=True)
        self.assertEqual(self.l.count("outbox", "delivered"), 0)
        self.l.record_receipt(delivery, "provider:receipt-1")
        self.assertEqual(self.l.count("outbox", "delivered"), 1)

    def test_crash_after_send_does_not_auto_resend(self):
        self.plan()
        self.l.claim_delivery(103, allowed=True, ttl=10)
        self.assertIsNone(self.l.claim_delivery(114, allowed=True))
        self.assertEqual(self.l.count("outbox", "unknown"), 1)

    def test_late_receipt_resolves_unknown(self):
        self.plan()
        d = self.l.claim_delivery(103, allowed=True, ttl=10)
        self.l.claim_delivery(114, allowed=True)
        self.l.record_receipt(d, "provider:late-receipt")
        self.assertEqual(self.l.count("outbox", "delivered"), 1)

    def test_retry_only_after_authoritative_not_sent(self):
        self.plan()
        d = self.l.claim_delivery(103, allowed=True)
        self.l.mark_delivery_unknown(d)
        with self.assertRaises(ContractError): self.l.reconcile(d)
        self.l.reconcile(d, authoritative_not_sent=True)
        new = self.l.claim_delivery(104, allowed=True)
        self.assertGreater(new.token, d.token)
        with self.assertRaises(Conflict): self.l.record_receipt(d, "old-receipt")

    def test_owner_destination_not_model_selected(self):
        self.plan()
        self.assertEqual(self.l.claim_delivery(103, allowed=True).recipient, "inbox:p1")

    def test_other_profile_cannot_open_database(self):
        with self.assertRaises(Conflict):
            Ledger(self.path, profile="p2", owner_destination="inbox:p2")

    def test_atomic_proposal_validation(self):
        self.event()
        lease = self.l.claim_run(101)
        with self.assertRaises(ContractError):
            self.l.complete_run(lease, [SelfNotification("a", "1", "ok", 1000),
                                        SelfNotification("b", "1", "bad", 101)], 102)
        self.assertEqual(self.l.count("outbox"), 0)
        self.assertEqual(self.l.count("runs", "running"), 1)


if __name__ == "__main__":
    unittest.main()
