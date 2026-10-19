"""Regression cases for the five findings at 908c1a8."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest

from proactive_sdk import FakeClock, OutboxDispatcher, PASError, ErrorCode
from proactive_sdk.daemon import PasDaemon
from p3_fixtures import T0, ScriptedModel, static_batch
from p4_fixtures import make_stack, berlin_ms, webhook_channel
from test_coordinator import make_store, make_job, admit_job_event, make_coordinator
from test_delivery import queue_one_notification
from test_p7_daemon import _agent


class ReviewRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_webhook_is_reconciled_without_resending(self):
        store, clock, _, policy, job = make_stack(
            channels=[webhook_channel(endpoint={"url": "https://notify.example"})],
            delivery_policy={"notification_profile": "push:demo"},
        )
        self.addCleanup(store.close)
        queue_one_notification(store, clock, policy, job)
        lease = store.claim_outbox_message(now_ms=T0, ttl_ms=1000)
        self.assertIsNotNone(lease)
        clock.advance_wall(2000)
        dispatcher = OutboxDispatcher(store, policy=policy)
        self.assertEqual(await dispatcher.dispatch_due(), [])
        message = store.get_outbox_message(lease.message_id)
        self.assertEqual(message["state"], "delivery_unknown")
        self.assertEqual(message["provider_key"], lease.provider_key)
        self.assertEqual(store.reconcile_outbox_message(
            lease.message_id, delivered=True, now_ms=clock.wall_now_ms()
        ), "reconciled_delivered")
        self.assertEqual(await dispatcher.dispatch_due(), [])

    async def test_model_failure_leaves_delta_available_until_success(self):
        store, clock = make_store()
        self.addCleanup(store.close)
        job = make_job()
        store.upsert_job(job, idempotency_key="review")

        class DeltaSource:
            async def fetch_delta(self, request):
                return static_batch(items=[] if request.cursor_ref == "cursor-1"
                                    else [("f", "1", "important update")])

        class RecoveringModel(ScriptedModel):
            fail = True

            async def generate(self, request):
                if self.fail:
                    self.fail = False
                    raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "temporary outage")
                return await super().generate(request)

        model = RecoveringModel([{"content": json.dumps(
            {"decision": "silent", "summary": "checked", "proposals": []}
        )}])
        coordinator = make_coordinator(store, model, DeltaSource())
        admit_job_event(store, job)
        self.assertEqual((await coordinator.process_pending_run()).outcome, "failed")
        self.assertIsNone(store.get_source_state("calendar", "account:primary"))
        admit_job_event(store, job, observed_ms=T0 + 1)
        report = await coordinator.process_pending_run()
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(report.candidates, 1)
        self.assertEqual(store.get_source_state("calendar", "account:primary").cursor_ref, "cursor-1")

    async def test_delayed_notification_waits_until_quiet_window_ends(self):
        clock = FakeClock(wall_ms=berlin_ms("2026-10-20", 21, 59))
        store, clock, _, policy, job = make_stack(clock=clock, delivery_policy={
            "quiet_hours": {"start": "22:00", "end": "08:00", "timezone": "Europe/Berlin"}
        })
        self.addCleanup(store.close)
        queue_one_notification(store, clock, policy, job)
        dispatcher = OutboxDispatcher(store, policy=policy)
        clock.set_wall(berlin_ms("2026-10-20", 22, 1))
        self.assertEqual((await dispatcher.dispatch_due())[0].state, "deferred")
        self.assertEqual(store.list_inbox(), [])
        self.assertEqual(store.list_outbox()[0]["attempts"], 0)
        clock.set_wall(berlin_ms("2026-10-21", 8))
        self.assertEqual((await dispatcher.dispatch_due())[0].state, "stored_in_inbox")
        self.assertEqual(len(store.list_inbox()), 1)

    async def test_successful_scan_keeps_health_ready_after_ten_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(wall_ms=T0)
            async with _agent(tmp, clock=clock) as agent:
                daemon = PasDaemon(agent)
                daemon._recover()
                clock.advance_wall(601000)
                await daemon._cycle()
                health = json.loads((agent.state_dir / "health.json").read_text())
                self.assertTrue(health["ready"], health["reasons"])

    async def test_fatal_cycle_reaches_foreground_and_releases_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _agent(tmp) as agent:
                daemon = PasDaemon(agent)

                async def broken_cycle():
                    raise OSError("health-file write failed")

                daemon._cycle = broken_cycle
                with self.assertRaisesRegex(OSError, "health-file write failed"):
                    await asyncio.wait_for(daemon.serve_forever(), timeout=2)
                self.assertFalse(daemon.lock.path.exists())
                self.assertFalse((agent.state_dir / "pas.sock").exists())
                self.assertFalse(daemon._started)
                self.assertIsNone(daemon._loop_task)
