"""SPEC §21.1 step 1: freeze the semantic matrix and pin v0.1.0 behaviour.

This module is the regression anchor for the v0.1.1 work: everything in
``SemanticMatrixBaselineTests`` documents behaviour that already existed
in v0.1.0 and must not change while the new reminder / obligation /
freshness / activity / cadence semantics are added.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from p3_fixtures import (  # noqa: E402
    P3TestCase,
    ScriptedModel,
    ScriptedSource,
    T0,
    make_broker,
    make_executor,
    static_batch,
)
from proactive_sdk import (  # noqa: E402
    ContextPackBuilder,
    EphemeralMemoryPort,
    ErrorCode,
    FakeClock,
    JobSpec,
    PASError,
    ProactiveCoordinator,
    SourceRegistry,
    Store,
)

INSTRUCTION = "检查跟踪的来源是否有变化，有新修订时只通知本人。"


def _store():
    clock = FakeClock(wall_ms=T0)
    return Store(
        ":memory:", profile="demo", owner_destination="local-inbox:demo", clock=clock
    ), clock


def _job(job_id="hb-1", *, mode="heartbeat", owner="pas", task=None, schedule=None):
    return JobSpec(
        job_id=job_id,
        mode=mode,
        owner=owner,
        schedule=schedule
        or {"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
        task=task or {"instruction": INSTRUCTION},
    )


def _admit(store, job, *, observed_ms=T0):
    return store.admit_event(
        f"job:{job.job_id}:{job.revision}:{observed_ms}",
        origin="scheduler",
        payload={"job_id": job.job_id, "mode": job.mode},
        observed_at_ms=observed_ms,
        expires_at_ms=observed_ms + 7 * 24 * 3600 * 1000,
        job_id=job.job_id,
        job_revision=job.revision,
    )


def _coordinator(store, model, source, *, capabilities=("calendar.read",), register=True):
    broker = make_broker(capabilities=capabilities)
    executor = make_executor(model, broker=broker)
    registry = SourceRegistry()
    if register:
        registry.register(
            source_id="calendar",
            account_ref="account:primary",
            source=source if source is not None else ScriptedSource([]),
            required_capability="calendar.read",
        )
    builder = ContextPackBuilder(
        locale="zh-CN", timezone="Europe/Berlin", memory=EphemeralMemoryPort()
    )
    return ProactiveCoordinator(
        store, registry=registry, pack_builder=builder, executor=executor
    )


def _silent_turn(summary="无实质变化"):
    return {
        "content": json.dumps(
            {"decision": "silent", "summary": summary, "proposals": []}, ensure_ascii=False
        )
    }


class SemanticMatrixBaselineTests(P3TestCase):
    """SPEC §21.1 step 1: v0.1.0 behaviour that must survive v0.1.1."""

    # -- 零模型调用硬门禁 ------------------------------------------------ #

    def test_heartbeat_without_sources_is_suppressed_with_zero_model_calls(self):
        store, _ = _store()
        job = _job()
        store.upsert_job(job, idempotency_key="k1")
        _admit(store, job)
        model = ScriptedModel([])
        coordinator = _coordinator(store, model, None, register=False)
        report = self.run_async(coordinator.process_pending_run())
        self.assertEqual(report.outcome, "suppressed")
        self.assertEqual(report.reason, "l0_nothing_to_check")
        self.assertEqual(report.model_turns, 0)
        self.assertEqual(model.calls, [])

    def test_heartbeat_without_change_is_suppressed_with_zero_model_calls(self):
        store, _ = _store()
        job = _job()
        store.upsert_job(job, idempotency_key="k1")
        _admit(store, job)
        model = ScriptedModel([_silent_turn()])
        source = ScriptedSource(
            [
                static_batch(items=[("i1", "r1", "hello")], cursor="c1"),
                static_batch(items=[], cursor="c1"),
            ]
        )
        coordinator = _coordinator(store, model, source)
        first = self.run_async(coordinator.process_pending_run())
        self.assertEqual(first.outcome, "proposed")
        _admit(store, job, observed_ms=T0 + 1_800_000)
        second = self.run_async(coordinator.process_pending_run())
        self.assertEqual(second.outcome, "suppressed")
        self.assertEqual(second.reason, "l0_no_source_change")
        self.assertEqual(second.model_turns, 0)
        self.assertEqual(len(model.calls), 1)

    def test_heartbeat_with_unauthorized_source_is_suppressed(self):
        store, _ = _store()
        job = _job()
        store.upsert_job(job, idempotency_key="k1")
        _admit(store, job)
        model = ScriptedModel([])
        # broker without the calendar.read capability
        coordinator = _coordinator(store, model, ScriptedSource([]), capabilities=())
        report = self.run_async(coordinator.process_pending_run())
        self.assertEqual(report.outcome, "suppressed")
        self.assertEqual(report.reason, "l0_source_unauthorized")
        self.assertEqual(model.calls, [])

    # -- 显式任务语义 ---------------------------------------------------- #

    def test_explicit_task_runs_without_sources(self):
        store, _ = _store()
        job = _job(job_id="oneshot", mode="task")
        store.upsert_job(job, idempotency_key="k1")
        _admit(store, job)
        model = ScriptedModel([_silent_turn("今日无安排")])
        coordinator = _coordinator(store, model, None, register=False)
        report = self.run_async(coordinator.process_pending_run())
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)

    def test_explicit_task_with_all_sources_failing_fails_visibly(self):
        store, _ = _store()

        class Boom:
            async def fetch_delta(self, request):
                raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "source down")

        job = _job(job_id="oneshot", mode="task")
        store.upsert_job(job, idempotency_key="k1")
        _admit(store, job)
        model = ScriptedModel([])
        coordinator = _coordinator(store, model, Boom())
        report = self.run_async(coordinator.process_pending_run())
        self.assertEqual(report.outcome, "failed")
        self.assertEqual(report.error_code, ErrorCode.PROVIDER_UNAVAILABLE.value)
        self.assertEqual(model.calls, [])

    # -- 单 owner --------------------------------------------------------- #

    def test_host_owned_job_is_never_admitted_by_pas(self):
        store, clock = _store()
        job = _job(job_id="host-owned", owner="host")
        store.upsert_job(job, idempotency_key="k1", initial_next_due_ms=clock.wall_now_ms() - 1000)
        self.assertEqual(store.due_job_ids(clock.wall_now_ms()), [])
        self.assertEqual(store.occurrences("host-owned"), [])

    def test_pas_owned_due_job_is_scanned(self):
        store, clock = _store()
        job = _job(job_id="pas-owned")
        store.upsert_job(job, idempotency_key="k1", initial_next_due_ms=clock.wall_now_ms() - 1000)
        self.assertEqual(store.due_job_ids(clock.wall_now_ms()), ["pas-owned"])

    # -- 默认 misfire policy --------------------------------------------- #

    def test_default_misfire_policy_is_mode_derived(self):
        store, _ = _store()
        store.upsert_job(_job(job_id="hb"), idempotency_key="k-hb")
        store.upsert_job(_job(job_id="tk", mode="task"), idempotency_key="k-tk")
        self.assertEqual(store.get_job("hb").misfire_policy, "coalesce_latest")
        self.assertEqual(store.get_job("tk").misfire_policy, "grace_once")

    # -- occurrence 幂等 -------------------------------------------------- #

    def test_same_occurrence_admission_is_idempotent(self):
        store, clock = _store()
        job = _job(job_id="hb")
        store.upsert_job(job, idempotency_key="k1")
        now = clock.wall_now_ms()
        first = store.admit_job_occurrence(
            "hb", expected_revision=1, kind="interval",
            slot_ms=now, next_due_ms=now + 1_800_000, now_ms=now,
        )
        second = store.admit_job_occurrence(
            "hb", expected_revision=1, kind="interval",
            slot_ms=now, next_due_ms=now + 1_800_000, now_ms=now,
        )
        self.assertEqual(first, "admitted")
        self.assertEqual(second, "already_admitted")
        self.assertEqual(
            len([o for o in store.occurrences("hb") if o["state"] == "admitted"]), 1
        )

    def test_paused_job_admission_is_skipped_not_admitted(self):
        store, clock = _store()
        job = _job(job_id="hb")
        store.upsert_job(job, idempotency_key="k1")
        paused = store.set_job_enabled("hb", expected_revision=1, enabled=False)
        now = clock.wall_now_ms()
        outcome = store.admit_job_occurrence(
            "hb", expected_revision=paused.revision, kind="interval",
            slot_ms=now, next_due_ms=now + 1_000, now_ms=now,
        )
        self.assertEqual(outcome, "skipped_disabled")
        self.assertEqual(store.occurrences("hb"), [])

    # -- 业务键硬去重 ----------------------------------------------------- #

    def test_business_key_is_profile_goal_fact_revision_destination_kind(self):
        from proactive_sdk.policy import PolicyEngine
        from p4_fixtures import make_stack, notify_proposal, make_proposed_run

        store, clock, registry, policy, job = make_stack(delivery_policy={"timezone": "UTC"})
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("f1")])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        queued = store.list_outbox(state="pending")
        self.assertEqual(len(queued), 1)
        first_key = queued[0]["delivery_key"]

        # Same fact + revision + destination + kind → duplicate, not a new message.
        run2 = make_proposed_run(store, clock, job, proposals=[notify_proposal("f1")])
        policy.apply_to_run(run2, now_ms=clock.wall_now_ms())
        self.assertEqual(len(store.list_outbox(state="pending")), 1)
        self.assertEqual(store.list_outbox(state="pending")[0]["delivery_key"], first_key)

        # A rephrased body is still the same fact (never a body hash).
        run3 = make_proposed_run(
            store, clock, job, proposals=[notify_proposal("f1", body="换一种说法的同一事实")]
        )
        policy.apply_to_run(run3, now_ms=clock.wall_now_ms())
        self.assertEqual(len(store.list_outbox(state="pending")), 1)


if __name__ == "__main__":
    unittest.main()
