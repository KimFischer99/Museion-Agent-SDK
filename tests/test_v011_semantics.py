"""SPEC §21.1 steps 3, 5, 6, 7, 8: the v0.1.1 semantics added on top of
the reminder path.

Each section states the rule it pins, because the interesting failures
here are not crashes but *silent* ones: a deferral that looks like a
loss, a stale snapshot sent as if it were current, a duplicate nobody can
explain, a paused job that "did something" with no record of why.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from p3_fixtures import P3TestCase, ScriptedSource, T0, static_batch  # noqa: E402
from p4_fixtures import make_proposed_run, make_stack, notify_proposal  # noqa: E402
from proactive_sdk import (  # noqa: E402
    NOTIFY_SELF_CAPABILITY,
    ContextPackBuilder,
    DispatchConfig,
    EphemeralMemoryPort,
    ErrorCode,
    FakeClock,
    GrantManager,
    JobSpec,
    OwnerChannelRegistry,
    OutboxDispatcher,
    PASError,
    PolicyConfig,
    PolicyEngine,
    SourceRegistry,
    Store,
)
from proactive_sdk.artifacts import ArtifactRefError, normalize_artifact_ref  # noqa: E402
from proactive_sdk.policy import PolicyEngine as _PolicyEngine  # noqa: E402,F401
from proactive_sdk.scheduler import Scheduler  # noqa: E402
from proactive_sdk.windows import local_day_end_ms, local_day_start_ms  # noqa: E402

MINUTE = 60_000
OWNER = "local-inbox:demo"


def rfc3339(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def reminder_stack(
    *,
    reminder: dict | None = None,
    delivery_policy: dict | None = None,
    at_ms: int | None = None,
    channels: list[dict] | None = None,
    clock: FakeClock | None = None,
):
    clock = clock or FakeClock(wall_ms=T0)
    store = Store(":memory:", profile="demo", owner_destination=OWNER, clock=clock)
    registry = OwnerChannelRegistry(store)
    registry.register(
        channel_ref=OWNER, kind="local_inbox", push_summary_only=False, now_ms=clock.wall_now_ms()
    )
    for channel in channels or []:
        registry.register(now_ms=clock.wall_now_ms(), **channel)
    grant = GrantManager(store).create(
        capability=NOTIFY_SELF_CAPABILITY,
        account_ref="account:primary",
        scope={},
        consent_evidence_ref="consent:test",
        now_ms=clock.wall_now_ms(),
    )
    spec = JobSpec(
        job_id="rem-1",
        mode="reminder",
        schedule={"kind": "runonce", "at": rfc3339(at_ms if at_ms is not None else T0 + MINUTE)},
        task={},
        grant_refs=(grant.grant_id,),
        delivery_policy=delivery_policy if delivery_policy is not None else {"timezone": "UTC"},
        reminder=reminder if reminder is not None else {"body": "带伞", "timezone": "UTC"},
    )
    scheduler = Scheduler(store, clock)
    scheduler.register_job(spec, idempotency_key="k1")
    return store, clock, scheduler, spec, registry


# --------------------------------------------------------------------------- #
# Step 3: obligation is trusted configuration, never a model claim
# --------------------------------------------------------------------------- #


class ObligationTests(P3TestCase):
    def test_mode_decides_the_default_obligation(self):
        heartbeat = JobSpec(
            job_id="hb",
            mode="heartbeat",
            schedule={"kind": "interval", "anchor": "2026-01-01T00:00:00Z", "every_seconds": 60},
            task={"instruction": "x"},
        )
        reminder = JobSpec(
            job_id="rm",
            mode="reminder",
            schedule={"kind": "runonce", "at": "2026-01-01T09:00:00Z"},
            task={},
            reminder={"body": "x", "timezone": "UTC"},
        )
        self.assertEqual(heartbeat.effective_obligation, "opportunistic")
        self.assertEqual(reminder.effective_obligation, "due")

    def test_invalid_obligation_is_rejected(self):
        with self.assertRaises(PASError):
            JobSpec(
                job_id="hb",
                mode="heartbeat",
                schedule={"kind": "interval", "anchor": "2026-01-01T00:00:00Z", "every_seconds": 60},
                task={"instruction": "x"},
                obligation="urgent",
            )

    def test_a_proposal_cannot_raise_the_jobs_obligation(self):
        # The job is opportunistic; a proposal (model output) that tries to
        # declare itself owed must not change the evaluated obligation.
        store, clock, registry, policy, job = make_stack(delivery_policy={"timezone": "UTC"})
        run_id = make_proposed_run(
            store,
            clock,
            job,
            proposals=[
                notify_proposal(
                    "f1",
                    arguments={"obligation": "due", "priority": "urgent"},
                )
            ],
        )
        self.assertEqual(store.get_job(job.job_id).obligation, "opportunistic")
        verdict = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertIn(verdict.outcome, ("actions_queued", "completed"))
        # The declared obligation is stored as data and changes nothing:
        # no cadence bypass, no fabricated duty.
        action = store.list_actions()[0]
        self.assertEqual(action["kind"], "notify_self")

    def test_due_obligation_is_exempt_from_the_cadence_preference(self):
        # A host cadence gap must never be what loses a reminder the user
        # scheduled themselves.
        store, clock, scheduler, _, _ = reminder_stack(
            delivery_policy={"timezone": "UTC", "cadence_min_gap_seconds": 3600}
        )
        clock.advance_wall(MINUTE + 1000)
        report = scheduler.admit_due()
        self.assertEqual(len(report.admitted), 1)
        message = store.list_outbox()[0]
        self.assertEqual(message["state"], "pending")
        self.assertLessEqual(message["not_before_ms"], clock.wall_now_ms())


# --------------------------------------------------------------------------- #
# Step 3/4: the local day boundary is DST-safe
# --------------------------------------------------------------------------- #


class LocalDayBoundaryTests(unittest.TestCase):
    def test_local_day_end_follows_the_calendar_not_a_fixed_offset(self):
        berlin = ZoneInfo("Europe/Berlin")
        # 2026-10-25 is the DST fall-back day in Europe/Berlin: the local day
        # is 25 hours long, so day_end - day_start is not 86_400_000.
        noon = datetime(2026, 10, 25, 12, 0, tzinfo=berlin)
        now_ms = int(noon.timestamp() * 1000)
        start = local_day_start_ms("Europe/Berlin", now_ms)
        end = local_day_end_ms("Europe/Berlin", now_ms)
        self.assertEqual(end - start, 25 * 3600 * 1000)
        # And the ordinary day stays 24 hours.
        plain = int(datetime(2026, 10, 20, 12, 0, tzinfo=berlin).timestamp() * 1000)
        self.assertEqual(
            local_day_end_ms("Europe/Berlin", plain) - local_day_start_ms("Europe/Berlin", plain),
            24 * 3600 * 1000,
        )

    def test_a_quota_deferral_lands_on_the_next_local_day_start(self):
        store, clock, scheduler, _, _ = reminder_stack(
            delivery_policy={
                "timezone": "Europe/Berlin",
                "max_per_day": 1,
                # Quiet hours at night keep the first reminder pending so the
                # second one hits the quota.
                "quiet_hours": {"start": "22:00", "end": "08:00", "timezone": "Europe/Berlin"},
            },
            at_ms=int(datetime(2026, 10, 25, 23, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp() * 1000),
        )
        clock.set_wall(
            int(datetime(2026, 10, 25, 23, 1, tzinfo=ZoneInfo("Europe/Berlin")).timestamp() * 1000)
        )
        scheduler.admit_due()
        second = JobSpec(
            job_id="rem-2",
            mode="reminder",
            schedule={"kind": "runonce", "at": rfc3339(clock.wall_now_ms())},
            task={},
            grant_refs=store.get_job("rem-1").grant_refs,
            delivery_policy=store.get_job("rem-1").delivery_policy,
            reminder={"body": "第二件事", "timezone": "Europe/Berlin"},
        )
        scheduler.register_job(second, idempotency_key="k2")
        clock.advance_wall(1000)
        scheduler.admit_due()
        messages = {m["payload"]["job_id"]: m for m in store.list_outbox()}
        self.assertEqual(len(messages), 2)
        # Both are deferred past midnight; neither is silently lost.
        expected = local_day_end_ms("Europe/Berlin", clock.wall_now_ms())
        for message in messages.values():
            self.assertGreaterEqual(message["not_before_ms"], expected)


# --------------------------------------------------------------------------- #
# Step 5: pre-delivery source refresh
# --------------------------------------------------------------------------- #


class PreDeliveryRefreshTests(P3TestCase):
    def _dispatchable(self, reminder: dict, source, *, source_ids=("calendar",)):
        store, clock, scheduler, _, registry = reminder_stack(
            reminder=reminder,
            channels=[
                {
                    "channel_ref": "push:ext",
                    "kind": "webhook",
                    "push_summary_only": False,
                    "endpoint": {"url": "http://127.0.0.1:1/nope"},
                }
            ],
        )
        sources = SourceRegistry()
        if source is not None:
            sources.register(
                source_id="calendar",
                account_ref="account:primary",
                source=source,
                required_capability="calendar.read",
            )
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        return store, clock, sources

    def test_source_failure_defers_the_message_instead_of_sending_stale_data(self):
        source = ScriptedSource([PASError(ErrorCode.PROVIDER_UNAVAILABLE, "down")])
        store, clock, sources = self._dispatchable(
            {
                "body": "看 artifact:reports/a.md",
                "timezone": "UTC",
                "destination": "push:ext",
                "refresh_sources": ["calendar"],
                "artifact_refs": ["artifact:reports/a.md"],
            },
            source,
        )
        before = len(source.requests)
        dispatcher = OutboxDispatcher(
            store, config=DispatchConfig(), sources=sources
        )
        reports = self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual(len(source.requests), before + 1)  # a real re-read happened
        self.assertEqual(reports[0].state, "pending")  # retryable, not sent
        message = store.get_outbox_message(reports[0].message_id)
        self.assertEqual(message["state"], "pending")
        self.assertIn("source_unavailable", message["reason"])
        states = {row["phase"]: row["state"] for row in store.job_activity("rem-1")}
        self.assertEqual(states.get("delivery"), "queued")

    def test_unauthorized_source_suppresses_instead_of_sending(self):
        source = ScriptedSource([PASError(ErrorCode.PERMISSION_DENIED, "revoked")])
        store, clock, sources = self._dispatchable(
            {
                "body": "看 calendar",
                "timezone": "UTC",
                "destination": "push:ext",
                "refresh_sources": ["calendar"],
            },
            source,
        )
        dispatcher = OutboxDispatcher(store, config=DispatchConfig(), sources=sources)
        reports = self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual(reports[0].state, "suppressed")
        message = store.get_outbox_message(reports[0].message_id)
        self.assertIn("source_unauthorized", message["reason"])

    def test_cancelled_fact_suppresses_the_reminder(self):
        from proactive_sdk import SourceBatch, SourceItem

        batch = SourceBatch(
            source_id="calendar",
            account_ref="account:primary",
            observed_at=rfc3339(T0 + MINUTE),
            cursor_ref="c2",
            items=(
                SourceItem(
                    fact_id="f1",
                    revision="2",
                    content="",
                    observed_at=rfc3339(T0 + MINUTE),
                    tombstone=True,
                ),
            ),
        )
        store, clock, sources = self._dispatchable(
            {
                "body": "看 calendar",
                "timezone": "UTC",
                "destination": "push:ext",
                "refresh_sources": ["calendar"],
                "fact_refs": ["f1"],
            },
            ScriptedSource([batch]),
        )
        dispatcher = OutboxDispatcher(store, config=DispatchConfig(), sources=sources)
        reports = self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual(reports[0].state, "suppressed")
        message = store.get_outbox_message(reports[0].message_id)
        self.assertIn("source_fact_cancelled", message["reason"])

    def test_a_frozen_reminder_makes_no_source_request_at_all(self):
        store, clock, sources = self._dispatchable(
            {"body": "带伞", "timezone": "UTC"},
            ScriptedSource([]),
        )
        # No refresh_sources declared → nothing to re-read, and the scripted
        # source (which raises when drained) is never called.
        dispatcher = OutboxDispatcher(store, config=DispatchConfig(), sources=sources)
        reports = self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual(reports[0].state, "stored_in_inbox")
        self.assertEqual(sources.entries()[0].source.requests, [])


# --------------------------------------------------------------------------- #
# Step 6: the bounded 24 h sent-notification summary
# --------------------------------------------------------------------------- #


class QuietWindowParsingTests(unittest.TestCase):
    """The profile-level notification window must actually take effect.

    ``facade._with_policy_defaults`` writes the profile default as
    ``quiet_hours_start`` / ``quiet_hours_end`` / ``quiet_hours_timezone``.
    Before v0.1.1 the parser only looked at a bare ``timezone`` key, so a
    job that declared no timezone of its own silently had no quiet hours at
    all — the default was accepted and then ignored.
    """

    def test_inline_window_uses_quiet_hours_timezone(self):
        policy = {
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "08:00",
            "quiet_hours_timezone": "Europe/Berlin",
        }
        from proactive_sdk.windows import quiet_end_ms, quiet_window

        parsed = quiet_window(policy)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], (1320, 480))
        self.assertEqual(str(parsed[1]), "Europe/Berlin")
        # 2026-10-25T23:00 Berlin is inside the window.
        night = int(
            datetime(2026, 10, 25, 23, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp() * 1000
        )
        self.assertIsNotNone(quiet_end_ms(policy, night))

    def test_nested_window_still_wins_over_the_inline_form(self):
        from proactive_sdk.windows import quiet_window

        parsed = quiet_window(
            {
                "quiet_hours_start": "01:00",
                "quiet_hours_end": "02:00",
                "quiet_hours_timezone": "UTC",
                "quiet_hours": {"start": "22:00", "end": "08:00", "timezone": "Europe/Berlin"},
            }
        )
        self.assertEqual(parsed[0], (1320, 480))
        self.assertEqual(str(parsed[1]), "Europe/Berlin")

    def test_no_window_configured_returns_none(self):
        from proactive_sdk.windows import quiet_window

        self.assertIsNone(quiet_window({}))
        self.assertIsNone(quiet_window({"quiet_hours_start": "22:00"}))


class RecentNotificationTests(P3TestCase):
    def _store_with_sent(self, *, sent_at_offsets: list[int]):
        store = Store(":memory:", profile="demo", owner_destination=OWNER, clock=FakeClock(wall_ms=T0))
        channels = OwnerChannelRegistry(store)
        channels.register(channel_ref=OWNER, kind="local_inbox", push_summary_only=False, now_ms=T0)
        grant = GrantManager(store).create(
            capability=NOTIFY_SELF_CAPABILITY,
            account_ref="account:primary",
            scope={},
            consent_evidence_ref="consent:test",
            now_ms=T0,
        )
        policy = PolicyEngine(store, channels=channels, config=PolicyConfig())
        job = JobSpec(
            job_id="hb-1",
            mode="heartbeat",
            schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
            task={"instruction": "检查来源变化，只通知本人。"},
            grant_refs=(grant.grant_id,),
            delivery_policy={"timezone": "UTC"},
        )
        store.upsert_job(job, idempotency_key="k1")
        clock = store.clock
        for index, offset in enumerate(sent_at_offsets):
            clock.set_wall(T0 + offset)
            run_id = make_proposed_run(
                store,
                clock,
                job,
                proposals=[notify_proposal(f"f{index}", body="这次要说的内容" * 40)],
            )
            policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
            dispatcher = OutboxDispatcher(store, config=DispatchConfig())
            self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        clock.set_wall(T0)
        return store

    def test_summary_is_redacted_bounded_and_windowed(self):
        store = self._store_with_sent(sent_at_offsets=[-2 * 3600 * 1000, -30 * 3600 * 1000])
        rows = store.recent_sent_notifications(now_ms=T0, window_ms=24 * 3600 * 1000, limit=20)
        self.assertEqual(len(rows), 1)  # the 30 h old message is outside the window
        self.assertNotIn("body", rows[0])

    def test_window_boundary_is_half_open_on_the_past_side(self):
        store = self._store_with_sent(sent_at_offsets=[-24 * 3600 * 1000])
        # Exactly 24 h old at T0 -> outside. A millisecond earlier in "now"
        # puts it back inside, which is what "the last 24 hours" means.
        self.assertEqual(
            store.recent_sent_notifications(now_ms=T0, window_ms=24 * 3600 * 1000), []
        )
        self.assertEqual(
            len(store.recent_sent_notifications(now_ms=T0 - 1, window_ms=24 * 3600 * 1000)), 1
        )

    def test_pack_carries_a_redacted_summary_and_never_a_body(self):
        store = self._store_with_sent(sent_at_offsets=[-3600 * 1000])
        rows = store.recent_sent_notifications(now_ms=T0)
        builder = ContextPackBuilder(
            locale="zh-CN", timezone="Europe/Berlin", memory=EphemeralMemoryPort()
        )
        pack = self.run_async(
            builder.build(
                goal_id="g", scope="s", source_records=[], now_ms=T0,
                recent_notifications=rows,
            )
        )
        wire = pack.to_dict()
        self.assertEqual(len(wire["recent_notifications"]), 1)
        entry = wire["recent_notifications"][0]
        self.assertEqual(set(entry), {"fact_digest", "channel_kind", "title", "sent_at_ms"})
        self.assertNotIn("body", json.dumps(wire, ensure_ascii=False))
        self.assertLessEqual(len(entry["title"]), 120)

    def test_pack_refuses_an_unbounded_or_untyped_summary(self):
        from proactive_sdk.contracts import ContextPack, MAX_RECENT_NOTIFICATIONS

        with self.assertRaises(PASError):
            ContextPack(
                task_goal_id="g",
                task_scope="s",
                locale="zh-CN",
                timezone="UTC",
                preferences_ref="p",
                recent_notifications=({"title": "x", "body": "leaked"},),  # type: ignore[arg-type]
            )
        self.assertGreaterEqual(MAX_RECENT_NOTIFICATIONS, 1)

    def test_a_queued_but_unsent_message_is_not_in_the_summary(self):
        store = Store(":memory:", profile="demo", owner_destination=OWNER, clock=FakeClock(wall_ms=T0))
        channels = OwnerChannelRegistry(store)
        channels.register(channel_ref=OWNER, kind="local_inbox", push_summary_only=False, now_ms=T0)
        grant = GrantManager(store).create(
            capability=NOTIFY_SELF_CAPABILITY, account_ref="account:primary", scope={},
            consent_evidence_ref="c", now_ms=T0,
        )
        job = JobSpec(
            job_id="hb-1",
            mode="heartbeat",
            schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
            task={"instruction": "x"},
            grant_refs=(grant.grant_id,),
            delivery_policy={"timezone": "UTC"},
        )
        store.upsert_job(job, idempotency_key="k1")
        policy = PolicyEngine(store, channels=channels, config=PolicyConfig())
        run_id = make_proposed_run(store, store.clock, job, proposals=[notify_proposal("f1")])
        policy.apply_to_run(run_id, now_ms=T0)
        # Queued, never dispatched → the owner saw nothing → not in the summary.
        self.assertEqual(store.recent_sent_notifications(now_ms=T0), [])


# --------------------------------------------------------------------------- #
# Step 7: job management and the visible activity projection
# --------------------------------------------------------------------------- #


class ActivityProjectionTests(P3TestCase):
    def test_agent_runs_project_their_analysis_outcome(self):
        """A heartbeat that stayed quiet shows *why*, on every job type."""
        from p3_fixtures import ContextPackBuilder, ScriptedModel, make_broker, make_executor
        from proactive_sdk import EphemeralMemoryPort, ProactiveCoordinator, SourceRegistry

        store = Store(
            ":memory:", profile="demo", owner_destination=OWNER, clock=FakeClock(wall_ms=T0)
        )
        job = JobSpec(
            job_id="hb-1",
            mode="heartbeat",
            schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
            task={"instruction": "检查来源变化。"},
        )
        store.upsert_job(job, idempotency_key="k1")
        store.admit_event(
            "wake-1",
            origin="scheduler",
            payload={"job_id": "hb-1", "mode": "heartbeat"},
            observed_at_ms=T0,
            expires_at_ms=T0 + 7 * 24 * 3600 * 1000,
            job_id="hb-1",
            job_revision=1,
        )
        # No registered source -> the L0 gate suppresses before any model call.
        coordinator = ProactiveCoordinator(
            store,
            registry=SourceRegistry(),
            pack_builder=ContextPackBuilder(
                locale="zh-CN", timezone="UTC", memory=EphemeralMemoryPort()
            ),
            executor=make_executor(ScriptedModel([]), broker=make_broker()),
        )
        report = self.run_async(coordinator.process_pending_run())
        self.assertEqual(report.outcome, "suppressed")
        rows = store.job_activity("hb-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phase"], "analysis")
        self.assertEqual(rows[0]["state"], "suppressed")
        self.assertEqual(rows[0]["reason"], "l0_nothing_to_check")
        self.assertEqual(rows[0]["run_id"], report.run_id)
        self.assertEqual(rows[0]["obligation"], "opportunistic")

    def test_realer_action_projects_a_delivery_row_for_agent_runs(self):
        store, clock, registry, policy, job = make_stack(delivery_policy={"timezone": "UTC"})
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("f1")])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        dispatcher = OutboxDispatcher(store, config=DispatchConfig())
        reports = self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual(reports[0].state, "stored_in_inbox")
        rows = store.job_activity(job.job_id, phase="delivery")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "delivered")
        self.assertEqual(rows[0]["run_id"], run_id)

    def test_activity_separates_execution_from_notification(self):
        store, clock, scheduler, _, _ = reminder_stack()
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        dispatcher = OutboxDispatcher(store, config=DispatchConfig())
        self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        rows = store.job_activity("rem-1")
        phases = {row["phase"] for row in rows}
        self.assertEqual(phases, {"action", "delivery"})
        by_phase = {row["phase"]: row for row in rows}
        self.assertEqual(by_phase["action"]["state"], "queued")
        self.assertEqual(by_phase["delivery"]["state"], "delivered")
        self.assertEqual(by_phase["delivery"]["message_id"], by_phase["action"]["message_id"])
        self.assertEqual(by_phase["action"]["obligation"], "due")

    def test_a_quiet_job_still_shows_why_it_stayed_quiet(self):
        store, clock, scheduler, _, _ = reminder_stack(
            reminder={"body": "带伞", "timezone": "UTC", "topic": "weather"}
        )
        store.mute_topic("weather", reason="user", now_ms=clock.wall_now_ms())
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        rows = store.job_activity("rem-1")
        self.assertEqual(rows[0]["state"], "missed")
        self.assertEqual(rows[0]["reason"], "topic_muted")
        self.assertEqual(
            [row["phase"] for row in store.job_activity("rem-1", phase="missed")], ["missed"]
        )

    def test_activity_projection_is_idempotent_per_phase(self):
        store, clock, scheduler, _, _ = reminder_stack()
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        scheduler.admit_due()
        self.assertEqual(len(store.job_activity("rem-1")), 1)

    def test_unknown_outcome_is_recorded_as_retryable_unknown(self):
        store, clock, scheduler, _, _ = reminder_stack(
            reminder={"body": "带伞", "timezone": "UTC", "destination": "push:ext"},
            channels=[
                {
                    "channel_ref": "push:ext",
                    "kind": "webhook",
                    "push_summary_only": False,
                    "endpoint": {"url": "http://127.0.0.1:1/nope"},
                }
            ],
        )
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        dispatcher = OutboxDispatcher(store, config=DispatchConfig())
        self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        rows = store.job_activity("rem-1")
        delivery = [row for row in rows if row["phase"] == "delivery"]
        self.assertEqual(delivery[0]["state"], "unknown")
        self.assertEqual(delivery[0]["retryable"], 1)


# --------------------------------------------------------------------------- #
# Step 8: cadence preference and artifact references
# --------------------------------------------------------------------------- #


class CadenceTests(P3TestCase):
    def test_cadence_name_is_validated(self):
        with self.assertRaises(PASError):
            PolicyConfig(cadence="frantic")

    def test_cadence_gap_defers_opportunistic_reach_out(self):
        store, clock, registry, policy, job = make_stack(delivery_policy={"timezone": "UTC"})
        policy.config = PolicyConfig(cadence="gentle", cadence_min_gap_seconds=3600)
        first = make_proposed_run(store, clock, job, proposals=[notify_proposal("f1")])
        policy.apply_to_run(first, now_ms=clock.wall_now_ms())
        dispatcher = OutboxDispatcher(store, config=DispatchConfig())
        self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))

        clock.advance_wall(60_000)
        second = make_proposed_run(store, clock, job, proposals=[notify_proposal("f2")])
        verdict = policy.apply_to_run(second, now_ms=clock.wall_now_ms())
        self.assertEqual(verdict.deferred, 1)
        pending = [m for m in store.list_outbox() if m["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertGreater(pending[0]["not_before_ms"], clock.wall_now_ms())
        self.assertEqual(pending[0]["reason"], "cadence_gap")

    def test_no_cadence_numbers_means_no_extra_gate(self):
        store, clock, registry, policy, job = make_stack(delivery_policy={"timezone": "UTC"})
        self.assertIsNone(policy.config.cadence_min_gap_seconds)
        self.assertIsNone(policy.config.cadence_max_per_day)
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("f1")])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        messages = store.list_outbox()
        self.assertEqual(messages[0]["state"], "pending")
        self.assertLessEqual(messages[0]["not_before_ms"], clock.wall_now_ms())


class ArtifactRefTests(P3TestCase):
    def test_reference_grammar_rejects_unsafe_forms(self):
        self.assertEqual(
            normalize_artifact_ref("artifact:reports/a.md"), "artifact:reports/a.md"
        )
        for bad in (
            "/Users/kim/report.md",
            "artifact:/etc/passwd",
            "artifact:../secrets.txt",
            "file:///tmp/x",
            "artifact:",
            "artifact:a/../../b",
            "artifact:C:/x",
            "",
        ):
            with self.assertRaises(ArtifactRefError, msg=bad):
                normalize_artifact_ref(bad)

    def test_proposal_referencing_an_unopenable_artifact_is_rejected(self):
        store, clock, registry, policy, job = make_stack(delivery_policy={"timezone": "UTC"})
        run_id = make_proposed_run(
            store,
            clock,
            job,
            proposals=[
                notify_proposal(
                    "f1",
                    body="打开 /Users/kim/report.md 查看",
                    arguments={"artifact_refs": ["/Users/kim/report.md"]},
                )
            ],
        )
        verdict = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(verdict.suppressed, 1)
        self.assertTrue(any("artifact_ref_unsafe" in r for r in verdict.reasons))
        self.assertEqual(store.list_outbox(), [])

    def test_proposal_that_never_mentions_the_artifact_in_the_body_is_rejected(self):
        store, clock, registry, policy, job = make_stack(delivery_policy={"timezone": "UTC"})
        run_id = make_proposed_run(
            store,
            clock,
            job,
            proposals=[
                notify_proposal(
                    "f1",
                    body="有一份新报告，你打开看看",
                    arguments={"artifact_refs": ["artifact:reports/a.md"]},
                )
            ],
        )
        verdict = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(verdict.suppressed, 1)
        self.assertTrue(any("artifact_ref_not_in_body" in r for r in verdict.reasons))

    def test_valid_artifact_reference_travels_with_the_payload(self):
        store, clock, registry, policy, job = make_stack(delivery_policy={"timezone": "UTC"})
        ref = "artifact:reports/a.md"
        run_id = make_proposed_run(
            store,
            clock,
            job,
            proposals=[
                notify_proposal(
                    "f1",
                    body=f"报告已生成：{ref}",
                    arguments={"artifact_refs": [ref]},
                )
            ],
        )
        verdict = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(verdict.outcome, "actions_queued")
        message = store.list_outbox()[0]
        self.assertEqual(message["payload"]["artifact_refs"], [ref])


# --------------------------------------------------------------------------- #
# Data lifecycle: a new ledger table must be exported and wiped like the rest
# --------------------------------------------------------------------------- #


class DataLifecycleTests(P3TestCase):
    def _populated_store(self):
        store, clock, scheduler, _, _ = reminder_stack()
        clock.advance_wall(MINUTE + 1000)
        scheduler.admit_due()
        dispatcher = OutboxDispatcher(store, config=DispatchConfig())
        self.run_async(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        return store, clock

    def test_export_includes_the_new_ledgers(self):
        store, _ = self._populated_store()
        dump = store.export_profile_data()
        self.assertIn("job_occurrences", dump)
        self.assertIn("job_activity", dump)
        self.assertTrue(dump["job_activity"])

    def test_wipe_removes_every_ledger_row_including_events_and_jobs(self):
        store, clock = self._populated_store()
        # A manual wake gives the profile an events row (which jobs and
        # job_occurrences reference) alongside the reminder's own ledgers.
        store.admit_event(
            "manual-wake",
            origin="manual",
            payload={"reason": "test"},
            observed_at_ms=clock.wall_now_ms(),
            expires_at_ms=clock.wall_now_ms() + 3_600_000,
        )
        self.assertTrue(store.list_jobs())
        self.assertTrue(store.db.execute("SELECT count(*) FROM events").fetchone()[0])
        deleted = store.wipe_profile_data()
        # The wipe must not leave a parent row behind because a child still
        # referenced it (both were previously skipped silently).
        self.assertIn("events", deleted)
        self.assertIn("jobs", deleted)
        self.assertIn("job_activity", deleted)
        self.assertEqual(store.list_jobs(), [])
        self.assertEqual(store.job_activity(), [])
        self.assertEqual(store.list_outbox(), [])
        self.assertEqual(store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        self.assertEqual(store.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_wipe_is_atomic_and_reports_real_row_counts(self):
        store, _ = self._populated_store()
        before = store.db.execute("SELECT count(*) FROM job_activity").fetchone()[0]
        self.assertGreater(before, 0)
        deleted = store.wipe_profile_data()
        self.assertEqual(deleted["job_activity"], before)


if __name__ == "__main__":
    unittest.main()
