"""W2: the user-presence (idle) gate.

The gate must spend zero model calls while the user is present, must fail
open when a host probe is broken, must never swallow a reminder, and must
never swallow the wake the user just raised.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # p3_fixtures / coordinator helpers
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from p3_fixtures import (  # noqa: E402
    ScriptedModel,
    ScriptedSource,
    T0,
    make_broker,
    make_executor,
    make_pack_builder,
    rfc3339,
    static_batch,
)
from test_coordinator import (  # noqa: E402
    admit_job_event,
    make_job,
    make_store,
)

from proactive_sdk import (  # noqa: E402
    NOTIFY_SELF_CAPABILITY,
    CoordinatorConfig,
    FakeClock,
    Job,
    ProactiveAgent,
    ProactiveCoordinator,
    SourceRegistry,
)

MINUTE = 60_000


def rfc3339_at_now_plus(seconds: int) -> str:
    """RFC 3339 for wall-clock now + seconds (real-clock regression only)."""
    from datetime import datetime, timedelta, timezone

    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class FakeProbe:
    """Deterministic ActivityProbe double with call accounting."""

    def __init__(self, last_ms: int | None, *, error: Exception | None = None):
        self.last_ms = last_ms
        self.error = error
        self.calls = 0

    def last_user_activity_at_ms(self) -> int | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.last_ms


class QuietAnalysisGateTests(unittest.TestCase):
    def test_due_task_runs_during_quiet_hours_and_user_activity(self):
        now_ms = int(datetime.fromisoformat("2026-10-20T23:30:00+08:00").timestamp() * 1000)
        store, clock = make_store()
        clock.set_wall(now_ms)
        self.addCleanup(store.close)
        job = replace(make_job(mode="task", task={"instruction": "Check", "always_run": True}),
                      obligation="due", delivery_policy={
                          "quiet_hours": {"start": "23:00", "end": "08:00", "timezone": "Asia/Shanghai"},
                      })
        store.upsert_job(job, idempotency_key="due-gate")
        admit_job_event(store, job, observed_ms=now_ms)
        model = ScriptedModel([silent_turn()])
        coordinator = make_gated_coordinator(store, model, None, probe=FakeProbe(now_ms))
        report = asyncio.run(coordinator.process_pending_run(now_ms=now_ms))
        self.assertEqual(report.model_turns, 1)
        self.assertNotEqual(report.silence_reason, "quiet_window")

    def test_night_job_is_suppressed_before_fetch_and_model(self):
        now_ms = int(datetime.fromisoformat("2026-10-20T23:30:00+08:00").timestamp() * 1000)
        store, clock = make_store()
        clock.set_wall(now_ms)
        self.addCleanup(store.close)
        job = replace(make_job(), delivery_policy={
            "quiet_hours": {"start": "23:00", "end": "08:00", "timezone": "Asia/Shanghai"},
        })
        store.upsert_job(job, idempotency_key="quiet-gate")
        admit_job_event(store, job, observed_ms=now_ms)
        model = ScriptedModel([])
        source = ScriptedSource([])
        coordinator = make_gated_coordinator(store, model, source)
        report = asyncio.run(coordinator.process_pending_run(now_ms=now_ms))
        self.assertEqual((report.outcome, report.silence_reason, report.model_turns),
                         ("suppressed", "quiet_window", 0))
        self.assertEqual(model.calls, [])
        self.assertEqual(source.requests, [])


def make_gated_coordinator(
    store,
    model,
    source,
    *,
    probe=None,
    idle_gate: bool = True,
    idle_min_minutes: int = 60,
):
    registry = SourceRegistry()
    if source is not None:
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
        config=CoordinatorConfig(idle_gate=idle_gate, idle_min_minutes=idle_min_minutes),
        activity_probe=probe,
    )


def silent_turn() -> dict:
    return {"content": json.dumps({"decision": "silent", "summary": "无变化", "proposals": []})}


class GateUnitTests(unittest.TestCase):
    def _heartbeat(self, *, probe, changed=True, idle_gate=True):
        store, _ = make_store()
        job = make_job()
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel([silent_turn()] if changed else [])
        source = ScriptedSource(
            [static_batch(items=[("item-1", "rev-1", "hello")] if changed else [])]
        )
        coordinator = make_gated_coordinator(
            store, model, source, probe=probe, idle_gate=idle_gate
        )
        report = asyncio.run(coordinator.process_pending_run())
        return store, model, report

    def test_recent_activity_suppresses_with_zero_model_calls(self):
        probe = FakeProbe(T0 - 5 * MINUTE)
        store, model, report = self._heartbeat(probe=probe)
        self.assertEqual(report.outcome, "suppressed")
        self.assertEqual(report.reason, "user_active")
        self.assertEqual(report.silence_reason, "user_active")
        self.assertEqual(report.model_turns, 0)
        self.assertEqual(model.calls, [])
        run = store.list_runs(state="suppressed")[0]
        self.assertEqual(run["silence_reason"], "user_active")
        activity = store.job_activity("hb-1", phase="analysis")
        self.assertEqual(activity[0]["reason"], "user_active")

    def test_exactly_the_idle_window_is_allowed(self):
        # `now - last >= idle_min_minutes` passes: 恰好 60 分钟放行.
        probe = FakeProbe(T0 - 60 * MINUTE)
        _, model, report = self._heartbeat(probe=probe)
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)

    def test_long_idle_window_runs_normally(self):
        probe = FakeProbe(T0 - 120 * MINUTE)
        _, model, report = self._heartbeat(probe=probe)
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)

    def test_unknown_probe_result_runs_normally(self):
        probe = FakeProbe(None)
        _, model, report = self._heartbeat(probe=probe)
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)

    def test_broken_probe_fails_open_and_records_a_warning(self):
        probe = FakeProbe(None, error=RuntimeError("host session store unavailable"))
        with self.assertLogs("proactive_sdk.coordinator", level="WARNING"):
            store, model, report = self._heartbeat(probe=probe)
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)
        kinds = [event["kind"] for event in store.run_events(report.run_id)]
        self.assertIn("activity_probe_failed", kinds)

    def test_idle_gate_switch_off_ignores_the_probe(self):
        probe = FakeProbe(T0 - 1 * MINUTE)
        _, model, report = self._heartbeat(probe=probe, idle_gate=False)
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)

    def test_manual_wake_is_exempt_even_when_the_user_is_active(self):
        store, _ = make_store()
        job = make_job()
        store.upsert_job(job, idempotency_key="k1")
        store.admit_event(
            "manual-1",
            origin="manual",
            payload={"reason": "user asked for this"},
            observed_at_ms=T0,
            expires_at_ms=T0 + 3_600_000,
            job_id=job.job_id,
            job_revision=job.revision,
        )
        model = ScriptedModel([silent_turn()])
        source = ScriptedSource([static_batch(items=[("item-1", "rev-1", "hello")])])
        probe = FakeProbe(T0 - 1_000)  # the user spoke one second ago
        coordinator = make_gated_coordinator(store, model, source, probe=probe)
        report = asyncio.run(coordinator.process_pending_run())
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)

    def test_the_latest_of_pas_and_host_activity_wins(self):
        # PAS's own store knows about a note 10 minutes ago; the host probe
        # knows the user spoke 1 minute ago. The later one must suppress.
        store, _ = make_store()
        store.admit_event(
            "note-1",
            origin="manual",
            payload={"reason": "earlier note"},
            observed_at_ms=T0 - 10 * MINUTE,
            expires_at_ms=T0 + 3_600_000,
            # A past note is presence evidence, not a queued run.
            create_run=False,
        )
        job = make_job()
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel([silent_turn()])
        source = ScriptedSource([static_batch(items=[("item-1", "rev-1", "hello")])])
        coordinator = make_gated_coordinator(
            store, model, source, probe=FakeProbe(T0 - 1 * MINUTE)
        )
        report = asyncio.run(coordinator.process_pending_run())
        self.assertEqual(report.outcome, "suppressed")
        self.assertEqual(model.calls, [])


class ReminderRegressionTests(unittest.TestCase):
    def test_idle_gate_never_suppresses_a_reminder(self):
        clock = FakeClock(wall_ms=T0)
        probe = FakeProbe(T0 - 1_000)  # user "just spoke"
        model = ScriptedModel([])  # any model call would be an error
        with tempfile.TemporaryDirectory() as tmp:
            agent = ProactiveAgent(
                state_dir=tmp,
                model=model,
                clock=clock,
                activity_probe=probe,
            )

            async def scenario():
                try:
                    grant = agent.grant(NOTIFY_SELF_CAPABILITY)
                    agent.jobs_upsert(
                        Job(
                            id="idle-reminder",
                            mode="reminder",
                            schedule={"kind": "runonce", "at": rfc3339(T0 + MINUTE)},
                            grant_refs=(grant.grant_id,),
                            delivery_policy={"timezone": "UTC"},
                            reminder={"body": "带伞", "timezone": "UTC"},
                        ),
                        idempotency_key="idle-reminder-v1",
                    )
                    clock.advance_wall(MINUTE + 1_000)
                    report = await agent.tick()
                    body = agent.inbox_list()[0]["body"]
                finally:
                    await agent.close()
                return report, body

            report, body = asyncio.run(scenario())
            self.assertEqual(report["dispatched"][0]["state"], "stored_in_inbox")
            self.assertEqual(body, "带伞")
            # Reminders never enter the coordinator: zero model calls and the
            # presence probe is never even consulted.
            self.assertEqual(model.calls, [])
            self.assertEqual(probe.calls, 0)

    def test_real_clock_reminder_is_still_on_time_with_the_gate_on(self):
        """The reminder promise is kept even while the user is "present"."""
        from proactive_sdk import ProactiveAgent
        from p3_fixtures import ScriptedModel

        model = ScriptedModel([])  # any model call is a test failure
        with tempfile.TemporaryDirectory() as tmp:
            agent = ProactiveAgent(
                state_dir=tmp,
                model=model,
                activity_probe=FakeProbe(0),  # "active" whenever asked
            )  # real SystemClock on purpose

            async def scenario():
                try:
                    grant = agent.grant(NOTIFY_SELF_CAPABILITY)
                    agent.jobs_upsert(
                        Job(
                            id="real-clock-reminder",
                            mode="reminder",
                            schedule={"kind": "runonce", "at": rfc3339_at_now_plus(1)},
                            grant_refs=(grant.grant_id,),
                            delivery_policy={"timezone": "UTC"},
                            reminder={"body": "到点了", "timezone": "UTC"},
                        ),
                        idempotency_key="real-clock-reminder-v1",
                    )
                    for _ in range(200):
                        await agent.tick()
                        if agent.inbox_list():
                            break
                        await asyncio.sleep(0.01)
                    body = agent.inbox_list()[0]["body"] if agent.inbox_list() else None
                finally:
                    await agent.close()
                return body

            body = asyncio.run(scenario())
            self.assertEqual(body, "到点了")
            self.assertEqual(model.calls, [])


CONFIG_TEXT = """config_version: "1"
profile: demo
state_dir: /tmp/pas-demo
timezone: UTC
locale: en
policy:
  idle_gate: false
  idle_min_minutes: 30
"""


class ConfigSurfaceTests(unittest.TestCase):
    def test_policy_keys_parse_emit_and_roundtrip(self):
        from proactive_sdk.config import build_config, config_to_yaml_subset, parse_config_text

        config = build_config(parse_config_text(CONFIG_TEXT))
        self.assertFalse(config.policy.idle_gate)
        self.assertEqual(config.policy.idle_min_minutes, 30)
        rendered = config_to_yaml_subset(config)
        self.assertIn("idle_gate: false", rendered)
        self.assertIn("idle_min_minutes: 30", rendered)
        reparsed = build_config(parse_config_text(rendered))
        self.assertEqual(reparsed.policy.idle_gate, False)
        self.assertEqual(reparsed.policy.idle_min_minutes, 30)

    def test_defaults_are_on(self):
        from proactive_sdk.config import build_config, parse_config_text

        config = build_config(
            parse_config_text(
                'config_version: "1"\nprofile: demo\nstate_dir: /tmp/x\n'
                "timezone: UTC\nlocale: en\n"
            )
        )
        self.assertTrue(config.policy.idle_gate)
        self.assertEqual(config.policy.idle_min_minutes, 60)

    def test_config_reaches_the_coordinator(self):
        from proactive_sdk.config import build_config, parse_config_text
        from proactive_sdk import FakeClock, ProactiveAgent
        from p3_fixtures import ScriptedModel

        config = build_config(parse_config_text(CONFIG_TEXT))
        with tempfile.TemporaryDirectory() as tmp:
            agent = ProactiveAgent(
                state_dir=tmp,
                model=ScriptedModel([]),
                clock=FakeClock(wall_ms=T0),
                config=config,
            )
            try:
                self.assertFalse(agent.coordinator.config.idle_gate)
                self.assertEqual(agent.coordinator.config.idle_min_minutes, 30)
            finally:
                import asyncio

                asyncio.run(agent.close())

    def test_invalid_values_are_rejected(self):
        from proactive_sdk.config import ConfigError, build_config, parse_config_text

        for bad in (
            "policy:\n  idle_gate: yes\n",
            "policy:\n  idle_gate: 1\n",
            "policy:\n  idle_min_minutes: 0\n",
            "policy:\n  idle_min_minutes: 1.5\n",
        ):
            with self.subTest(bad=bad):
                text = (
                    'config_version: "1"\nprofile: demo\nstate_dir: /tmp/x\n'
                    "timezone: UTC\nlocale: en\n" + bad
                )
                with self.assertRaises(ConfigError):
                    build_config(parse_config_text(text))


if __name__ == "__main__":
    unittest.main()
