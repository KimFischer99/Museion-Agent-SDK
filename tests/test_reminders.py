"""SPEC §21.1 steps 2–4: deterministic direct reminders.

The central claim under test is that a reminder reaches the owner outbox
at its scheduled instant with **zero model calls**, exactly once per
occurrence, and that every occurrence which could not be delivered leaves
a queryable reason instead of disappearing.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from p3_fixtures import P3TestCase  # noqa: E402
from proactive_sdk import (  # noqa: E402
    NOTIFY_SELF_CAPABILITY,
    ErrorCode,
    FakeClock,
    GrantManager,
    JobSpec,
    OwnerChannelRegistry,
    PASError,
    PolicyEngine,
    Store,
)
from proactive_sdk.policy import PolicyConfig  # noqa: E402
from proactive_sdk.scheduler import Scheduler  # noqa: E402

T0 = 1_760_000_000_000  # 2025-10-09T08:53:20Z
MINUTE = 60_000
OWNER = "local-inbox:demo"
GRACE_MS = 900_000


def rfc3339(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class ReminderCase(P3TestCase):
    def make(
        self,
        *,
        at_ms: int | None = None,
        reminder: dict | None = None,
        delivery_policy: dict | None = None,
        with_grant: bool = True,
        misfire_policy: str | None = None,
        clock: FakeClock | None = None,
    ):
        clock = clock or FakeClock(wall_ms=T0)
        store = Store(":memory:", profile="demo", owner_destination=OWNER, clock=clock)
        channels = OwnerChannelRegistry(store)
        channels.register(channel_ref=OWNER, kind="local_inbox", push_summary_only=False,
                          now_ms=clock.wall_now_ms())
        grant_ids: list[str] = []
        if with_grant:
            grant = GrantManager(store).create(
                capability=NOTIFY_SELF_CAPABILITY,
                account_ref="account:primary",
                scope={},
                consent_evidence_ref="consent:test",
                now_ms=clock.wall_now_ms(),
            )
            grant_ids.append(grant.grant_id)
        spec = JobSpec(
            job_id="rem-1",
            mode="reminder",
            schedule={"kind": "runonce", "at": rfc3339(at_ms if at_ms is not None else T0 + MINUTE)},
            task={},
            grant_refs=tuple(grant_ids),
            delivery_policy=delivery_policy if delivery_policy is not None else {"timezone": "UTC"},
            reminder=reminder if reminder is not None else {"body": "带伞", "timezone": "UTC"},
            misfire_policy=misfire_policy,
        )
        scheduler = Scheduler(store, clock)
        scheduler.register_job(spec, idempotency_key="k1")
        return store, clock, scheduler, spec


class ReminderAdmissionTests(ReminderCase):
    def test_reminder_reaches_outbox_with_zero_model_calls_and_no_run(self):
        store, clock, scheduler, _ = self.make()
        clock.advance_wall(MINUTE + 1000)
        report = scheduler.admit_due()
        self.assertEqual(len(report.admitted), 1)
        messages = store.list_outbox()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["state"], "pending")
        self.assertEqual(messages[0]["destination_ref"], OWNER)
        self.assertEqual(messages[0]["payload"]["body"], "带伞")
        self.assertEqual(messages[0]["payload"]["semantic"], "direct_reminder")
        # No analysis happened: the run ledger stays empty (四本账分离).
        self.assertEqual(store.list_runs(), [])
        self.assertEqual(store.run_state_counts().get("queued", 0), 0)
        # The wake is still accounted for on its own.
        self.assertEqual(
            [o["state"] for o in store.occurrences("rem-1")], ["admitted"]
        )

    def test_repeat_admission_of_same_occurrence_never_double_sends(self):
        store, clock, scheduler, _ = self.make()
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        # A replayed tick (restart recovery) and a PAS/host race both call
        # the same admission again; neither may produce a second message.
        scheduler.admit_due()
        scheduler.admit_due()
        self.assertEqual(len(store.list_outbox()), 1)
        self.assertEqual(len(store.occurrences("rem-1")), 1)

    def test_reminder_requires_an_active_notify_grant(self):
        store, clock, scheduler, _ = self.make(with_grant=False)
        clock.advance_wall(MINUTE + 1000)
        report = scheduler.admit_due()
        self.assertEqual(report.admitted, [])
        self.assertIn("grant_missing", [reason for _, reason in report.expired])
        self.assertEqual(store.list_outbox(), [])
        # The refusal is visible on the activity projection, not silent.
        activity = store.job_activity("rem-1")
        self.assertEqual([row["state"] for row in activity], ["missed"])

    def test_revoked_grant_stops_the_reminder(self):
        store, clock, scheduler, spec = self.make()
        GrantManager(store).revoke(spec.grant_refs[0], now_ms=clock.wall_now_ms())
        clock.advance_wall(MINUTE + 1000)
        report = scheduler.admit_due()
        self.assertEqual(report.admitted, [])
        self.assertEqual(store.list_outbox(), [])
        self.assertIn("grant_missing", [reason for _, reason in report.expired])

    def test_paused_job_is_not_scheduled(self):
        store, clock, scheduler, _ = self.make()
        store.set_job_enabled("rem-1", expected_revision=1, enabled=False)
        clock.advance_wall(MINUTE + 1000)
        self.assertEqual(store.due_job_ids(clock.wall_now_ms()), [])
        self.assertEqual(store.list_outbox(), [])

    def test_stopped_job_is_never_scheduled_but_keeps_its_ledger(self):
        store, clock, scheduler, _ = self.make()
        store.stop_job("rem-1", reason="user_stop", now_ms=clock.wall_now_ms())
        clock.advance_wall(MINUTE + 1000)
        self.assertEqual(store.due_job_ids(clock.wall_now_ms()), [])
        self.assertEqual(store.list_outbox(), [])
        self.assertIsNotNone(store.get_job("rem-1"))  # audit preserved

    def test_deleted_job_without_history_is_removed_for_real(self):
        store, clock, scheduler, _ = self.make()
        store.delete_job("rem-1")
        self.assertIsNone(store.get_job("rem-1"))

    def test_delete_job_with_history_is_refused_by_the_store(self):
        store, clock, scheduler, _ = self.make()
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        with self.assertRaises(PASError) as ctx:
            store.delete_job("rem-1")
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)

    def test_late_occurrence_is_born_as_a_miss_not_a_silent_park(self):
        # The job is registered *after* its target instant: grace_once
        # admits it as a catch-up within the grace window.
        clock = FakeClock(wall_ms=T0)
        store, clock, scheduler, _ = self.make(at_ms=T0 - 60_000, clock=clock)
        clock.advance_wall(30_000)  # 90 s late, well inside the grace window
        report = scheduler.admit_due()
        self.assertEqual(len(report.admitted), 1)
        message = store.list_outbox()[0]
        self.assertEqual(message["payload"]["late_reason"], "catch_up_within_grace")
        activity = store.job_activity("rem-1")
        # Registered at T0 for a T0-60s instant, admitted at T0+30s.
        self.assertEqual(activity[0]["lateness_ms"], 90_000)
        self.assertEqual(activity[0]["planned_at_ms"], T0 - 60_000)

    def test_occurrence_beyond_grace_is_reported_as_missed(self):
        clock = FakeClock(wall_ms=T0)
        store, clock, scheduler, _ = self.make(at_ms=T0 - 60_000, clock=clock)
        clock.advance_wall(GRACE_MS + 60_000)
        report = scheduler.admit_due()
        self.assertEqual(report.admitted, [])
        self.assertEqual(store.list_outbox(), [])
        self.assertEqual(len(report.expired), 1)
        _, reason = report.expired[0]
        self.assertTrue(reason.startswith("missed_beyond_grace"), reason)
        occurrence = store.occurrences("rem-1")[0]
        self.assertEqual(occurrence["state"], "expired")
        self.assertIn("missed_beyond_grace", occurrence["reason"])
        activity = store.job_activity("rem-1")
        self.assertEqual(activity[0]["phase"], "missed")
        self.assertEqual(activity[0]["state"], "missed")
        self.assertEqual(activity[0]["obligation"], "due")


class ReminderGateTests(ReminderCase):
    def test_quiet_hours_defers_and_stays_visible(self):
        policy = {
            "timezone": "UTC",
            "quiet_hours": {"start": "00:00", "end": "09:30", "timezone": "UTC"},
        }
        store, clock, scheduler, _ = self.make(delivery_policy=policy)
        clock.advance_wall(MINUTE + 1000)
        report = scheduler.admit_due()
        self.assertEqual(len(report.admitted), 1)
        message = store.list_outbox()[0]
        self.assertEqual(message["payload"]["body"], "带伞")
        # Deferred, not dropped: the message waits for the window to close.
        row = store.get_outbox_message(message["message_id"])
        self.assertGreater(row["not_before_ms"], clock.wall_now_ms())
        activity = store.job_activity("rem-1")
        self.assertEqual(activity[0]["state"], "deferred")
        self.assertEqual(activity[0]["reason"], "quiet_hours")
        self.assertEqual(activity[0]["obligation"], "due")

    def test_daily_quota_defers_the_reminder_rather_than_losing_it(self):
        store, clock, scheduler, _ = self.make(
            delivery_policy={"timezone": "UTC", "max_per_day": 1}
        )
        # A second reminder for the same instant: the first consumes the
        # day's quota, the second is deferred with a visible reason instead
        # of being dropped.
        second = JobSpec(
            job_id="rem-2",
            mode="reminder",
            schedule={"kind": "runonce", "at": rfc3339(T0 + MINUTE)},
            task={},
            grant_refs=store.get_job("rem-1").grant_refs,
            delivery_policy={"timezone": "UTC", "max_per_day": 1},
            reminder={"body": "第二件事", "timezone": "UTC"},
        )
        scheduler.register_job(second, idempotency_key="k2")
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        messages = store.list_outbox()
        self.assertEqual(len(messages), 2)
        by_job = {m["payload"]["job_id"]: m for m in messages}
        self.assertEqual(by_job["rem-1"]["state"], "pending")
        self.assertLessEqual(by_job["rem-1"]["not_before_ms"], clock.wall_now_ms())
        self.assertEqual(by_job["rem-2"]["state"], "pending")
        self.assertGreater(by_job["rem-2"]["not_before_ms"], clock.wall_now_ms())
        deferred = store.job_activity("rem-2")
        self.assertEqual(deferred[0]["state"], "deferred")
        self.assertEqual(deferred[0]["reason"], "daily_quota")

    def test_muted_topic_is_a_hard_gate_with_a_visible_reason(self):
        store, clock, scheduler, _ = self.make(
            reminder={"body": "带伞", "timezone": "UTC", "topic": "weather"}
        )
        store.mute_topic("weather", until_ms=None, reason="user", now_ms=clock.wall_now_ms())
        clock.advance_wall(MINUTE + 1000)
        report = scheduler.admit_due()
        self.assertEqual(report.admitted, [])
        self.assertEqual(store.list_outbox(), [])
        self.assertIn("topic_muted", [reason for _, reason in report.expired])
        self.assertEqual(store.occurrences("rem-1")[0]["reason"], "topic_muted")

    def test_reminder_expiry_shorter_than_the_quiet_window_becomes_a_miss(self):
        policy = {
            "timezone": "UTC",
            "quiet_hours": {"start": "00:00", "end": "23:00", "timezone": "UTC"},
            "expire_after_s": 60,
        }
        store, clock, scheduler, _ = self.make(delivery_policy=policy)
        clock.advance_wall(MINUTE + 1000)
        report = scheduler.admit_due()
        self.assertEqual(report.admitted, [])
        self.assertEqual(store.list_outbox(), [])
        self.assertIn(
            "expired_before_window_open:quiet_hours", [reason for _, reason in report.expired]
        )


class ReminderDeliveryTests(ReminderCase):
    def test_local_inbox_receives_the_frozen_body(self):
        store, clock, scheduler, _ = self.make()
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        from proactive_sdk import DispatchConfig, OutboxDispatcher

        dispatcher = OutboxDispatcher(store, config=DispatchConfig())
        reports = self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual([r.state for r in reports], ["stored_in_inbox"])
        inbox = store.list_inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["body"], "带伞")
        # Delivery outcome is its own activity phase, separate from the action.
        states = {row["phase"]: row["state"] for row in store.job_activity("rem-1")}
        self.assertEqual(states.get("action"), "queued")
        self.assertEqual(states.get("delivery"), "delivered")

    def test_unknown_delivery_is_not_retried_blindly(self):
        store, clock, scheduler, _ = self.make(
            reminder={"body": "带伞", "timezone": "UTC", "destination": "push:ext"}
        )
        channels = OwnerChannelRegistry(store)
        channels.register(
            channel_ref="push:ext", kind="webhook", push_summary_only=False,
            endpoint={"url": "http://127.0.0.1:1/nope"}, now_ms=clock.wall_now_ms(),
        )
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        from proactive_sdk import DispatchConfig, OutboxDispatcher

        dispatcher = OutboxDispatcher(store, config=DispatchConfig())
        reports = self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual(reports[0].state, "delivery_unknown")
        again = self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms() + 60_000))
        self.assertEqual(again, [])  # delivery_unknown is never auto-retried
        states = {row["phase"]: row["state"] for row in store.job_activity("rem-1")}
        self.assertEqual(states.get("delivery"), "unknown")


class ReminderValidationTests(unittest.TestCase):
    def test_reminder_rejects_task_instruction(self):
        with self.assertRaises(PASError) as ctx:
            JobSpec(
                job_id="r",
                mode="reminder",
                schedule={"kind": "runonce", "at": "2025-10-09T09:00:00Z"},
                task={"instruction": "not allowed"},
                reminder={"body": "x", "timezone": "UTC"},
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)

    def test_non_reminder_mode_rejects_a_reminder_block(self):
        with self.assertRaises(PASError):
            JobSpec(
                job_id="r",
                mode="task",
                schedule={"kind": "runonce", "at": "2025-10-09T09:00:00Z"},
                task={"instruction": "do it"},
                reminder={"body": "x", "timezone": "UTC"},
            )

    def test_reminder_requires_body_and_timezone(self):
        with self.assertRaises(PASError):
            JobSpec(job_id="r", mode="reminder",
                    schedule={"kind": "runonce", "at": "2025-10-09T09:00:00Z"},
                    task={}, reminder={"body": "x"})
        with self.assertRaises(PASError):
            JobSpec(job_id="r", mode="reminder",
                    schedule={"kind": "runonce", "at": "2025-10-09T09:00:00Z"},
                    task={}, reminder={"timezone": "UTC"})

    def test_reminder_timezone_must_match_the_schedule(self):
        with self.assertRaises(PASError):
            JobSpec(
                job_id="r",
                mode="reminder",
                schedule={"kind": "daily", "local_time": "09:00", "timezone": "Europe/Berlin"},
                task={},
                reminder={"body": "x", "timezone": "UTC"},
            )

    def test_reminder_rejects_unknown_keys(self):
        with self.assertRaises(PASError) as ctx:
            JobSpec(
                job_id="r",
                mode="reminder",
                schedule={"kind": "runonce", "at": "2025-10-09T09:00:00Z"},
                task={},
                reminder={"body": "x", "timezone": "UTC", "priority": "urgent"},
            )
        self.assertIn("unknown keys", ctx.exception.safe_message)


if __name__ == "__main__":
    unittest.main()
