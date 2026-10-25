"""A5-derived follow-up work: run-level next_eligible_at_ms (P2) and the
delivery-feedback context injection (P3b).

P2: only the two suppression reasons with an honest wall-clock answer
(quiet_window, user_active) record an eligible point; every other
suppression stays NULL rather than being guessed (migration 018).
P3b: while ``policy.feedback_context`` is on, the newest delivery-outcome
signals enter the run's context pack as a redacted projection — digest,
topic, signal — with no message body and no ledger join key.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # shared fixtures
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from p3_fixtures import (  # noqa: E402
    ScriptedModel,
    ScriptedSource,
    T0,
    make_broker,
    make_executor,
    make_pack_builder,
    static_batch,
)
from p4_fixtures import make_stack  # noqa: E402
from test_coordinator import admit_job_event, make_job, make_store  # noqa: E402
from test_v2_activity_gate import FakeProbe  # noqa: E402
from test_v2_feedback import _deliver  # noqa: E402

from proactive_sdk import (  # noqa: E402
    CoordinatorConfig,
    ProactiveCoordinator,
    SourceRegistry,
)
from proactive_sdk.contracts import validate_v3_context  # noqa: E402

MINUTE = 60_000


def _coordinator(store, model, source, *, probe=None, feedback_context=False):
    registry = SourceRegistry()
    registry.register(
        source_id="calendar",
        account_ref="account:primary",
        source=source,
        required_capability="calendar.read",
    )
    return ProactiveCoordinator(
        store,
        registry=registry,
        pack_builder=make_pack_builder(),
        executor=make_executor(model, broker=make_broker()),
        config=CoordinatorConfig(feedback_context=feedback_context),
        activity_probe=probe,
    )


def _silent_turn() -> dict:
    return {"content": json.dumps({"decision": "silent", "summary": "无变化", "proposals": []})}


class NextEligibleTests(unittest.TestCase):
    def test_quiet_window_records_the_window_end(self):
        now_ms = int(datetime.fromisoformat("2026-10-20T23:30:00+08:00").timestamp() * 1000)
        quiet_end = int(datetime.fromisoformat("2026-10-21T08:00:00+08:00").timestamp() * 1000)
        store, clock = make_store()
        clock.set_wall(now_ms)
        self.addCleanup(store.close)
        job = replace(make_job(), delivery_policy={
            "quiet_hours": {"start": "23:00", "end": "08:00", "timezone": "Asia/Shanghai"},
        })
        store.upsert_job(job, idempotency_key="quiet-eligible")
        admit_job_event(store, job, observed_ms=now_ms)
        coordinator = _coordinator(store, ScriptedModel([]), ScriptedSource([]))
        report = asyncio.run(coordinator.process_pending_run(now_ms=now_ms))
        self.assertEqual((report.outcome, report.silence_reason), ("suppressed", "quiet_window"))
        self.assertEqual(report.next_eligible_at_ms, quiet_end)
        run = store.get_run(report.run_id)
        self.assertEqual(run["next_eligible_at_ms"], quiet_end)

    def test_user_active_records_last_activity_plus_idle_window(self):
        store, _ = make_store()
        self.addCleanup(store.close)
        job = make_job()
        store.upsert_job(job, idempotency_key="idle-eligible")
        admit_job_event(store, job)
        probe = FakeProbe(T0 - 5 * MINUTE)  # idle gate default: 60 minutes
        coordinator = _coordinator(store, ScriptedModel([]), ScriptedSource([]), probe=probe)
        report = asyncio.run(coordinator.process_pending_run())
        self.assertEqual((report.outcome, report.silence_reason), ("suppressed", "user_active"))
        self.assertEqual(report.next_eligible_at_ms, T0 + 55 * MINUTE)
        run = store.get_run(report.run_id)
        self.assertEqual(run["next_eligible_at_ms"], T0 + 55 * MINUTE)

    def test_reasons_without_a_clock_point_stay_null(self):
        store, _ = make_store()
        self.addCleanup(store.close)
        job = make_job()
        store.upsert_job(job, idempotency_key="no-change-null")
        admit_job_event(store, job)
        # Probe unknown -> gate fails open; empty delta -> L0 no-source-change.
        coordinator = _coordinator(
            store, ScriptedModel([]), ScriptedSource([static_batch(items=[])]), probe=FakeProbe(None)
        )
        report = asyncio.run(coordinator.process_pending_run())
        self.assertEqual((report.outcome, report.silence_reason), ("suppressed", "no_source_change"))
        self.assertIsNone(report.next_eligible_at_ms)
        run = store.get_run(report.run_id)
        self.assertIsNone(run["next_eligible_at_ms"])


class FeedbackContextTests(unittest.TestCase):
    def _delivered_stack(self):
        stack = make_stack()
        self.addCleanup(stack[0].close)
        message_id, _run_id = _deliver(stack)
        store, clock = stack[0], stack[1]
        store.record_delivery_feedback(
            message_id=message_id, signal="replied", actor="user-1",
            now_ms=clock.wall_now_ms(),
        )
        return store, clock, message_id

    def _feedback_run(self, store, *, feedback_context: bool, observed_ms: int):
        job = replace(make_job(), job_id="fb-run-1")
        store.upsert_job(job, idempotency_key="fb-run")
        admit_job_event(store, job, observed_ms=observed_ms)
        model = ScriptedModel([_silent_turn()])
        source = ScriptedSource([static_batch(items=[("item-1", "rev-1", "hello")])])
        coordinator = _coordinator(store, model, source, feedback_context=feedback_context)
        report = asyncio.run(coordinator.process_pending_run())
        prompt = "\n".join(
            str(part) for call in model.calls for part in call["messages"]
        )
        return report, prompt

    def test_injection_is_redacted_and_flag_gated(self):
        store, clock, message_id = self._delivered_stack()
        rows = store.recent_delivery_feedback(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"fact_digest", "topic", "signal", "at_ms"})
        self.assertEqual(rows[0]["signal"], "replied")
        self.assertNotIn(message_id, json.dumps(rows, default=str))

        report, prompt = self._feedback_run(store, feedback_context=True, observed_ms=T0 - MINUTE)
        self.assertIn("delivery_feedback", prompt)
        self.assertIn("replied", prompt)
        self.assertNotIn(message_id, prompt)
        kinds = [event["kind"] for event in store.run_events(report.run_id)]
        self.assertIn("feedback_context", kinds)

        report_off, prompt_off = self._feedback_run(
            store, feedback_context=False, observed_ms=T0 - 2 * MINUTE
        )
        self.assertNotIn("delivery_feedback", prompt_off)
        kinds_off = [event["kind"] for event in store.run_events(report_off.run_id)]
        self.assertNotIn("feedback_context", kinds_off)


class FeedbackContractTests(unittest.TestCase):
    GOOD = {"fact_digest": "0123456789abcdef", "topic": "t", "signal": "replied", "at_ms": 1}

    def test_delivery_feedback_bounds_and_redaction(self):
        self.assertEqual(validate_v3_context({"delivery_feedback": [self.GOOD]}), [])
        six = [dict(self.GOOD, at_ms=i) for i in range(6)]
        self.assertIn(
            "delivery_feedback allows at most 5 entries",
            validate_v3_context({"delivery_feedback": six}),
        )
        bad_signal = dict(self.GOOD, signal="accepted")  # A5 vocabulary, not ours
        self.assertIn(
            "invalid delivery feedback entry",
            validate_v3_context({"delivery_feedback": [bad_signal]}),
        )
        with_join_key = dict(self.GOOD, message_id="msg-1")
        self.assertIn(
            "invalid delivery feedback entry",
            validate_v3_context({"delivery_feedback": [with_join_key]}),
        )

    def test_pending_notification_allows_optional_not_before(self):
        entry = {
            "fact_digest": "0123456789abcdef", "topic": "t",
            "state": "deferred", "at_ms": 1, "not_before_ms": 2,
        }
        self.assertEqual(validate_v3_context({"pending_notifications": [entry]}), [])


if __name__ == "__main__":
    unittest.main()
