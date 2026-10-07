"""SPEC §22.1 item 6: a user-originated wake and its authorization.

The bug this closes, measured before the change: a wake with no job reaches
policy with ``grant_refs = ()``, so *every* proposal it produces is
suppressed with ``grant_missing``. "The user said something, so do something
about it" was structurally dead.

What these tests are really guarding is the boundary, not the feature:

* **the generic path must stay unable to mint authority.** Being able to
  write a manual event must not be the same as being able to authorize what
  that event produces — so a plain ``admit_event`` is asserted to still fail.
* **binding is not widening.** The wake binds grants that already exist;
  scope, mutes, quiet hours, quota and dedup still apply on top.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import (  # noqa: E402
    NOTIFY_SELF_CAPABILITY,
    CallableHostDriver,
    ErrorCode,
    FakeClock,
    HostBridge,
    Job,
    MemoryEntry,
    PASError,
    ProactiveAgent,
    validate_decision,  # noqa: F401  (import check)
)

T0 = 1_760_000_000_000
MEMORY_ID = "mem-user-wake"


def _envelope(**over) -> dict:
    body = {
        "decision": "propose",
        "summary": "有一件要跟进的事",
        "proposals": [
            {
                "kind": "notify_self",
                "fact_id": "fact-1",
                "revision": "1",
                "body": "帮你盯着这件事",
                "evidence_refs": [MEMORY_ID],
                "expires_at": "2030-01-01T00:00:00Z",
            }
        ],
    }
    body.update(over)
    return body


class UserWakeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(wall_ms=T0)
        self.host = CallableHostDriver(lambda _p: self.envelope())
        self.envelope = lambda: _envelope()
        self.agent = ProactiveAgent(
            state_dir=Path(self.tmp.name),
            executor=HostBridge(self.host),
            clock=self.clock,
            timezone="UTC",
            locale="en",
            profile="wake",
        )
        asyncio.run(
            self.agent.pack_builder.memory.remember(
                MemoryEntry(memory_id=MEMORY_ID, content="用户在跟这件事", source="user")
            )
        )
        self.grant = self.agent.create_grant_from_user_consent(
            capability=NOTIFY_SELF_CAPABILITY,
            account_ref="account:primary",
            scope={},
            consent_evidence_ref="consent:wake",
        )

    def tearDown(self):
        asyncio.run(self.agent.close())
        self.tmp.cleanup()

    def _tick(self) -> dict:
        return asyncio.run(self.agent.tick())


class GenericPathCannotAuthorizeTests(UserWakeCase):
    def test_a_plain_manual_event_still_has_no_authority(self):
        """The hole is only closed if the *ordinary* entry stays closed."""
        self.agent.store.admit_event(
            "manual-without-authority",
            origin="manual",
            payload={"reason": "帮我盯着这个 PR"},
            observed_at_ms=T0,
            expires_at_ms=T0 + 3_600_000,
        )
        report = self._tick()
        entry = list(report["runs"])[0]
        # The wake ran, but policy refused everything it proposed.
        self.assertEqual(entry["outcome"], "proposed")
        run = self.agent.store.list_runs()[0]
        verdicts = [
            p["policy"]
            for p in self.agent.store.run_proposals(run["run_id"])
            if p.get("policy")
        ]
        self.assertTrue(verdicts)
        self.assertEqual(verdicts[0]["reason"], "grant_missing")
        self.assertEqual(self.agent.store.list_outbox(), [])

    def test_the_event_carries_no_authorization_record_at_all(self):
        event_id = self.agent.store.admit_event(
            "manual-plain-2",
            origin="manual",
            payload={"reason": "x"},
            observed_at_ms=T0,
            expires_at_ms=T0 + 3_600_000,
        )
        self.assertIsNone(self.agent.store.get_event(event_id).authorization)


class TrustedEntryAuthorizesTests(UserWakeCase):
    def test_a_note_from_the_trusted_side_reaches_the_outbox(self):
        event_id = self.agent.note_user_input(
            "帮我盯着这个 PR", grant_refs=(self.grant.grant_id,)
        )
        report = self._tick()
        entry = list(report["runs"])[0]
        self.assertEqual(entry["outcome"], "proposed")
        self.assertEqual(entry["policy_outcome"], "actions_queued")
        queued = self.agent.store.list_outbox()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["destination_ref"], self.agent.owner_destination)
        self.assertIsNotNone(self.agent.store.get_event(event_id).authorization)

    def test_the_ledger_records_where_the_authority_came_from(self):
        self.agent.note_user_input("帮我盯着这个 PR", grant_refs=(self.grant.grant_id,))
        self._tick()
        run_id = self.agent.store.list_runs()[0]["run_id"]
        kinds = {e["kind"]: e["safe_summary"] for e in self.agent.store.run_events(run_id)}
        self.assertIn("wake_authorization", kinds)
        self.assertIn("event-scoped", kinds["wake_authorization"])

    def test_a_repeated_note_with_the_same_key_is_one_wake(self):
        first = self.agent.note_user_input(
            "同一句话", grant_refs=(self.grant.grant_id,), idempotency_key="note-1"
        )
        second = self.agent.note_user_input(
            "同一句话", grant_refs=(self.grant.grant_id,), idempotency_key="note-1"
        )
        self.assertEqual(first, second)


class TrustedEntryValidationTests(UserWakeCase):
    def test_at_least_one_grant_is_required(self):
        with self.assertRaises(PASError) as ctx:
            self.agent.note_user_input("没有任何授权的一句话", grant_refs=())
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)
        self.assertIn("at least one grant_ref", ctx.exception.safe_message)

    def test_a_revoked_grant_cannot_be_bound(self):
        self.agent.revoke_grant(self.grant.grant_id)
        with self.assertRaises(PASError) as ctx:
            self.agent.note_user_input("x", grant_refs=(self.grant.grant_id,))
        self.assertIn("not active", ctx.exception.safe_message)

    def test_an_unknown_grant_cannot_be_bound(self):
        with self.assertRaises(PASError):
            self.agent.note_user_input("x", grant_refs=("grant-does-not-exist",))

    def test_an_unknown_destination_cannot_be_bound(self):
        with self.assertRaises(PASError) as ctx:
            self.agent.note_user_input(
                "x", grant_refs=(self.grant.grant_id,), destination="push:not-registered"
            )
        self.assertIn("not a registered owner channel", ctx.exception.safe_message)

    def test_text_must_be_usable(self):
        for bad in ("", None, "x" * 10001):
            with self.assertRaises(PASError):
                self.agent.note_user_input(bad, grant_refs=(self.grant.grant_id,))

    def test_the_store_refuses_to_bind_a_grant_that_does_not_exist(self):
        """Defence in depth: the store never trusts the caller's list."""
        with self.assertRaises(PASError) as ctx:
            self.agent.store.admit_user_wake(
                "user:sneaky",
                text="x",
                grant_refs=("invented-grant",),
                destination_ref=self.agent.owner_destination,
                observed_at_ms=T0,
                expires_at_ms=T0 + 1000,
            )
        self.assertIn("cannot create authority", ctx.exception.safe_message)

    def test_the_store_refuses_a_wake_with_no_grants(self):
        with self.assertRaises(PASError):
            self.agent.store.admit_user_wake(
                "user:no-grants",
                text="x",
                grant_refs=(),
                destination_ref=self.agent.owner_destination,
                observed_at_ms=T0,
                expires_at_ms=T0 + 1000,
            )


class BindingIsNotWideningTests(UserWakeCase):
    def test_policy_still_suppresses_inside_the_bound_scope(self):
        """Binding a grant does not exempt the wake from policy."""
        self.agent.store.mute_topic("pr-tracking", reason="user", now_ms=T0)
        self.envelope = lambda: _envelope(
            proposals=[
                {
                    "kind": "notify_self",
                    "fact_id": "fact-1",
                    "revision": "1",
                    "body": "帮你盯着这件事",
                    "arguments": {"topic": "pr-tracking"},
                    "evidence_refs": [MEMORY_ID],
                    "expires_at": "2030-01-01T00:00:00Z",
                }
            ]
        )
        self.agent.note_user_input("帮我盯着这个 PR", grant_refs=(self.grant.grant_id,))
        self._tick()
        self.assertEqual(self.agent.store.list_outbox(), [])

    def test_a_grant_of_another_capability_does_not_authorize_a_notification(self):
        other = self.agent.create_grant_from_user_consent(
            capability="calendar.read",
            account_ref="account:primary",
            scope={},
            consent_evidence_ref="consent:other",
        )
        self.agent.note_user_input("x", grant_refs=(other.grant_id,))
        self._tick()
        self.assertEqual(self.agent.store.list_outbox(), [])

    def test_business_key_dedup_still_applies_inside_one_wake(self):
        """Binding a grant does not bypass the hard dedup layer."""
        # The same proposal, twice, inside one run: the business key and the
        # outbox delivery key still collapse it to a single message.
        self.envelope = lambda: _envelope(
            proposals=[_envelope()["proposals"][0], _envelope()["proposals"][0]]
        )
        self.agent.note_user_input("帮我盯着这个 PR", grant_refs=(self.grant.grant_id,))
        self._tick()
        self.assertEqual(len(self.agent.store.list_outbox()), 1)

    def test_two_separate_notes_are_two_goals_by_design(self):
        """Not a dedup failure: each note is its own goal in the business key.

        SPEC §10.1 binds dedup to profile+goal+fact+revision+destination+kind,
        and a free-standing wake's goal is the event itself. Saying the same
        thing twice really is two separate asks.
        """
        self.envelope = lambda: _envelope()
        self.agent.note_user_input("第一次", grant_refs=(self.grant.grant_id,))
        self._tick()
        self.agent.note_user_input("第二次", grant_refs=(self.grant.grant_id,))
        self._tick()
        keys = {m["delivery_key"] for m in self.agent.store.list_outbox()}
        self.assertEqual(len(keys), 2)


class NoteToAJobScopedDestinationTests(UserWakeCase):
    def test_a_job_can_still_be_used_normally(self):
        """The new path must not disturb the standing job path."""
        self.agent.jobs_upsert(
            Job(
                id="standing",
                mode="task",
                schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 3600},
                instruction="检查一下。",
                grant_refs=(self.grant.grant_id,),
            ),
            idempotency_key="standing-v1",
        )
        self.agent.trigger_job("standing", reason="t")
        report = self._tick()
        self.assertEqual(list(report["runs"])[0]["policy_outcome"], "actions_queued")


if __name__ == "__main__":
    unittest.main()
