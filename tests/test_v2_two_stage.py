"""W5: two-stage decision path (probe → candidates → run).

A model-free candidate stage decides whether an analysis is worth paying
for. Preserved semantics: source-less tasks still run (pure reasoning),
``task.always_run=true`` keeps the pre-v2 every-slot behaviour, and wakes
the user raised manually or through a hook are explicit signals that run.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
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
    static_batch,
)
from test_coordinator import (  # noqa: E402
    admit_hook_event,
    admit_job_event,
    make_job,
    make_store,
)

from proactive_sdk import (  # noqa: E402
    CoordinatorConfig,
    ErrorCode,
    JobSpec,
    PASError,
    ProactiveCoordinator,
    SourceRegistry,
)


def silent_turn() -> dict:
    return {"content": json.dumps({"decision": "silent", "summary": "无变化", "proposals": []})}


def make_two_stage(
    store,
    model,
    source,
    *,
    max_candidates: int = 10,
    capabilities=("calendar.read",),
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
        config=CoordinatorConfig(max_candidates=max_candidates),
    )


def task_job(*, always_run: bool = False):
    task: dict = {"instruction": "检查跟踪的来源"}
    if always_run:
        task["always_run"] = True
    return make_job(job_id="daily-agenda", mode="task", task=task)


class TwoStageGateTests(unittest.TestCase):
    def test_task_without_candidates_is_suppressed_with_zero_model_calls(self):
        store, _ = make_store()
        job = task_job()
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel([])
        source = ScriptedSource([static_batch(items=[], cursor="cursor-1")])
        report = asyncio.run(make_two_stage(store, model, source).process_pending_run())
        self.assertEqual(report.outcome, "suppressed")
        self.assertEqual(report.reason, "l0_no_source_change")
        self.assertEqual(report.silence_reason, "no_source_change")
        self.assertEqual(model.calls, [])
        run = store.list_runs(state="suppressed")[0]
        self.assertEqual(run["candidates_count"], 0)
        self.assertEqual(run["truncated_count"], 0)

    def test_task_with_a_candidate_runs_once(self):
        store, _ = make_store()
        job = task_job()
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel([silent_turn()])
        source = ScriptedSource([static_batch(items=[("item-1", "rev-1", "hello")])])
        report = asyncio.run(make_two_stage(store, model, source).process_pending_run())
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(report.candidates, 1)
        self.assertEqual(report.truncated, 0)

    def test_always_run_keeps_the_pre_v2_semantics(self):
        store, _ = make_store()
        job = task_job(always_run=True)
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel([silent_turn()])
        source = ScriptedSource([static_batch(items=[], cursor="cursor-1")])
        report = asyncio.run(make_two_stage(store, model, source).process_pending_run())
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)

    def test_source_less_task_is_still_pure_reasoning(self):
        store, _ = make_store()
        job = task_job()
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel([silent_turn()])
        report = asyncio.run(make_two_stage(store, model, None).process_pending_run())
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)

    def test_manual_trigger_is_exempt_from_the_candidate_gate(self):
        store, _ = make_store()
        job = task_job()
        store.upsert_job(job, idempotency_key="k1")
        store.admit_event(
            "manual-trigger-1",
            origin="manual",
            payload={"reason": "manual trigger"},
            observed_at_ms=T0,
            expires_at_ms=T0 + 3_600_000,
            job_id=job.job_id,
            job_revision=job.revision,
        )
        model = ScriptedModel([silent_turn()])
        source = ScriptedSource([static_batch(items=[], cursor="cursor-1")])
        report = asyncio.run(make_two_stage(store, model, source).process_pending_run())
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)

    def test_hook_wake_is_an_explicit_signal(self):
        store, _ = make_store()
        admit_hook_event(store, reason="hook saw something")
        model = ScriptedModel([silent_turn()])
        source = ScriptedSource([static_batch(items=[], cursor="cursor-1")])
        report = asyncio.run(make_two_stage(store, model, source).process_pending_run())
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)

    def test_heartbeat_unchanged_still_suppresses(self):
        store, _ = make_store()
        job = make_job()
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel([])
        source = ScriptedSource([static_batch(items=[], cursor="cursor-1")])
        report = asyncio.run(make_two_stage(store, model, source).process_pending_run())
        self.assertEqual(report.outcome, "suppressed")
        self.assertEqual(report.reason, "l0_no_source_change")


class CandidateBudgetTests(unittest.TestCase):
    def _run_with_items(self, items, *, max_candidates=10, cursor="cursor-1"):
        store, _ = make_store()
        job = make_job()
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel([silent_turn()])
        source = ScriptedSource([static_batch(items=items, cursor=cursor)])
        coordinator = make_two_stage(store, model, source, max_candidates=max_candidates)
        report = asyncio.run(coordinator.process_pending_run())
        run = store.list_runs(state="proposed")[0]
        return store, model, report, run

    def test_candidates_are_capped_and_the_overflow_is_counted(self):
        items = [(f"item-{i}", "rev-1", f"content-{i}") for i in range(12)]
        store, model, report, run = self._run_with_items(items, max_candidates=10)
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(run["candidates_count"], 10)
        self.assertEqual(run["truncated_count"], 2)
        pack = store.get_context_pack(run["context_ref"])
        self.assertEqual(len(pack["sources"]), 10)
        snapshots = store.db.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]
        self.assertEqual(snapshots, 10)

    def test_duplicate_items_are_deduplicated_not_truncated(self):
        items = [
            ("item-1", "rev-1", "same content"),
            ("item-1", "rev-1", "same content"),
            ("item-2", "rev-1", "other content"),
        ]
        _, _, report, run = self._run_with_items(items, max_candidates=10)
        self.assertEqual(report.candidates, 2)
        self.assertEqual(report.truncated, 0)
        self.assertEqual(run["candidates_count"], 2)
        self.assertEqual(run["truncated_count"], 0)

    def test_a_new_revision_of_a_known_fact_is_its_own_candidate(self):
        items = [
            ("item-1", "rev-1", "first"),
            ("item-1", "rev-2", "second"),
        ]
        _, _, report, _ = self._run_with_items(items)
        self.assertEqual(report.candidates, 2)

    def test_twenty_four_empty_ticks_cost_one_model_call_total(self):
        store, clock = make_store()
        job = make_job()
        store.upsert_job(job, idempotency_key="k1")
        model = ScriptedModel([silent_turn()])
        # First tick: one changed item; the following 23 ticks: same cursor,
        # no items (a quiet source).
        batches = [static_batch(items=[("item-1", "rev-1", "hello")], cursor="cursor-1")]
        batches += [
            static_batch(items=[], cursor="cursor-1") for _ in range(23)
        ]
        source = ScriptedSource(batches)
        coordinator = make_two_stage(store, model, source)
        for tick in range(24):
            admit_job_event(store, job, observed_ms=T0 + tick * 1_800_000)
            asyncio.run(coordinator.process_pending_run())
        # Only the changed tick paid for a model call.
        self.assertEqual(len(model.calls), 1)
        suppressed = store.list_runs(state="suppressed")
        self.assertEqual(len(suppressed), 23)


class JobSurfaceTests(unittest.TestCase):
    def test_job_spec_accepts_and_validates_always_run(self):
        spec = JobSpec(
            job_id="j1",
            mode="task",
            schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 60},
            task={"instruction": "x", "always_run": True},
        )
        self.assertIs(spec.task["always_run"], True)
        with self.assertRaises(PASError) as ctx:
            JobSpec(
                job_id="j2",
                mode="task",
                schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 60},
                task={"instruction": "x", "always_run": "yes"},
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)

    def test_job_spec_schema_carries_always_run(self):
        from proactive_sdk.schema_validate import validate

        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas" / "v1" / "job_spec.json").read_text()
        )
        task = schema["properties"]["task"]
        self.assertFalse(task["additionalProperties"])
        self.assertEqual(task["properties"]["always_run"]["type"], "boolean")

    def test_cli_create_accepts_always_run(self):
        from proactive_sdk.service import build_parser

        args = build_parser().parse_args(
            [
                "jobs",
                "create",
                "j1",
                "--mode",
                "task",
                "--schedule-json",
                '{"kind":"interval","anchor":"2025-10-09T00:00:00Z","every_seconds":60}',
                "--instruction",
                "x",
                "--always-run",
            ]
        )
        self.assertTrue(args.always_run)

    def test_config_max_candidates_reaches_the_coordinator(self):
        from proactive_sdk import FakeClock, ProactiveAgent
        from proactive_sdk.config import build_config, config_to_yaml_subset, parse_config_text
        from p3_fixtures import ScriptedModel
        import tempfile

        text = (
            'config_version: "1"\nprofile: demo\nstate_dir: /tmp/x\n'
            "timezone: UTC\nlocale: en\npolicyless: 0\n"
        )
        text = (
            'config_version: "1"\nprofile: demo\nstate_dir: /tmp/x\n'
            "timezone: UTC\nlocale: en\npolicy:\n  max_candidates: 5\n"
        )
        config = build_config(parse_config_text(text))
        self.assertEqual(config.policy.max_candidates, 5)
        self.assertIn("max_candidates: 5", config_to_yaml_subset(config))
        with tempfile.TemporaryDirectory() as tmp:
            agent = ProactiveAgent(
                state_dir=tmp,
                model=ScriptedModel([]),
                clock=FakeClock(wall_ms=T0),
                config=config,
            )
            try:
                self.assertEqual(agent.coordinator.config.max_candidates, 5)
            finally:
                asyncio.run(agent.close())

    def test_facade_job_carries_always_run_into_the_task(self):
        from proactive_sdk import FakeClock, Job, ProactiveAgent
        from p3_fixtures import ScriptedModel
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            agent = ProactiveAgent(
                state_dir=tmp, model=ScriptedModel([]), clock=FakeClock(wall_ms=T0)
            )

            async def scenario():
                try:
                    agent.jobs_upsert(
                        Job(
                            id="always-run-job",
                            mode="task",
                            schedule={
                                "kind": "interval",
                                "anchor": "2025-10-09T00:00:00Z",
                                "every_seconds": 60,
                            },
                            instruction="x",
                            always_run=True,
                        )
                    )
                    return agent.jobs_get("always-run-job").task
                finally:
                    await agent.close()

            task = asyncio.run(scenario())
            self.assertIs(task.get("always_run"), True)


if __name__ == "__main__":
    unittest.main()
