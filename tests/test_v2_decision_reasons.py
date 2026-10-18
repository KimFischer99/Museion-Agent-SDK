"""v2 decision contract 1.1: structured reasons and semantic silence.

Covers W1 (``reason`` on interrupting proposals) and W3 (``silence_reason``
for runs that stay quiet), including the gray-release switch, the
migration-independent parser behaviour and the ledger read path.

The contract tests in ``test_decision_contract.py`` prove the *prompt*
matches the layers; this module proves the concrete v2 behaviours end to
end (model text → typed decision → store → run/activity read path).
"""

from __future__ import annotations

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
    static_batch,
)
from test_coordinator import (  # noqa: E402
    admit_job_event,
    make_coordinator,
    make_job,
    make_store,
)

from proactive_sdk import (  # noqa: E402
    HOST_SILENCE_REASONS,
    MODEL_SILENCE_REASONS,
    REASON_ENUM,
    SILENCE_UNSPECIFIED,
    ErrorCode,
    ExecutorConfig,
    PASError,
    ToolLoopExecutor,
    validate_decision,
)
from proactive_sdk.decision_parse import parse_decision_text  # noqa: E402

EVIDENCE = frozenset({"snapshot:item-1:rev-1"})


def _proposal(**over: object) -> dict:
    body: dict = {
        "kind": "draft",
        "fact_id": "item-1",
        "revision": "rev-1",
        "reason": "changed",
    }
    body.update(over)
    return body


def _document(**over: object) -> dict:
    body: dict = {
        "decision": "propose",
        "summary": "来源出现新修订",
        "proposals": [_proposal()],
    }
    body.update(over)
    return body


def _parse(document: dict, **kwargs: object):
    return parse_decision_text(
        json.dumps(document, ensure_ascii=False),
        evidence=EVIDENCE,
        max_proposals=8,
        **kwargs,
    )


class ReasonContractTests(unittest.TestCase):
    def test_every_reason_parses_and_survives_the_wire(self):
        for reason in REASON_ENUM:
            with self.subTest(reason=reason):
                decision = _parse(_document(proposals=[_proposal(reason=reason)]))
                self.assertEqual(decision.proposals[0].reason, reason)
                wire = decision.to_wire_dict()
                self.assertEqual(wire["proposals"][0]["reason"], reason)

    def test_reason_is_required_for_notify_self_and_draft(self):
        for kind in ("notify_self", "draft"):
            with self.subTest(kind=kind):
                proposal = {k: v for k, v in _proposal(kind=kind).items() if k != "reason"}
                if kind == "notify_self":
                    proposal["evidence_refs"] = ["snapshot:item-1:rev-1"]
                    proposal["expires_at"] = "2030-01-01T00:00:00Z"
                with self.assertRaises(PASError) as ctx:
                    _parse(_document(proposals=[proposal]))
                self.assertIn("'reason'", ctx.exception.safe_message)
                self.assertIn(kind, ctx.exception.safe_message)

    def test_illegal_reason_is_rejected_not_defaulted(self):
        with self.assertRaises(PASError) as ctx:
            _parse(_document(proposals=[_proposal(reason="because_i_said_so")]))
        self.assertIn("not in enum", ctx.exception.safe_message)
        self.assertIn("reason", ctx.exception.safe_message)

    def test_gray_release_accepts_missing_reason(self):
        proposal = {k: v for k, v in _proposal().items() if k != "reason"}
        decision = _parse(_document(proposals=[proposal]), require_reason=False)
        self.assertIsNone(decision.proposals[0].reason)

    def test_gray_release_never_accepts_an_illegal_reason(self):
        with self.assertRaises(PASError):
            _parse(
                _document(proposals=[_proposal(reason="because_i_said_so")]),
                require_reason=False,
            )

    def test_validate_decision_honours_the_gray_switch(self):
        proposal = {"kind": "draft", "fact_id": "f", "revision": "1"}
        document = {"decision": "propose", "summary": "s", "proposals": [proposal]}
        self.assertTrue(validate_decision(document))
        self.assertEqual(validate_decision(document, require_reason=False), [])

    def test_record_kinds_do_not_need_a_reason(self):
        for kind in ("internal_record", "suggest_watch", "request_external_action"):
            with self.subTest(kind=kind):
                decision = _parse(
                    _document(
                        proposals=[{"kind": kind, "fact_id": "f", "revision": "1"}]
                    )
                )
                self.assertIsNone(decision.proposals[0].reason)


class SilenceReasonTests(unittest.TestCase):
    def test_silent_without_a_reason_becomes_unspecified(self):
        decision = _parse({"decision": "silent", "summary": "无变化", "proposals": []})
        self.assertEqual(decision.silence_reason, SILENCE_UNSPECIFIED)
        self.assertEqual(decision.to_wire_dict()["silence_reason"], SILENCE_UNSPECIFIED)

    def test_every_model_silence_reason_is_accepted(self):
        for reason in MODEL_SILENCE_REASONS:
            with self.subTest(reason=reason):
                decision = _parse(
                    {
                        "decision": "silent",
                        "summary": "无变化",
                        "proposals": [],
                        "silence_reason": reason,
                    }
                )
                self.assertEqual(decision.silence_reason, reason)

    def test_unknown_silence_reason_is_normalised_with_a_warning(self):
        warnings: list[str] = []
        decision = _parse(
            {
                "decision": "silent",
                "summary": "无变化",
                "proposals": [],
                "silence_reason": "bored",
            },
            on_unknown_silence_reason=warnings.append,
        )
        self.assertEqual(decision.silence_reason, SILENCE_UNSPECIFIED)
        self.assertEqual(len(warnings), 1)
        self.assertIn("bored", warnings[0])

    def test_silence_reason_is_rejected_outside_silent(self):
        with self.assertRaises(PASError) as ctx:
            _parse(_document(silence_reason="moot"))
        self.assertIn("only decision=silent", ctx.exception.safe_message)

    def test_host_silence_vocabulary_is_closed(self):
        # The host-side enum exists for the ledger/stats surface; every
        # value must be expressible in the run row's CHECK constraint.
        self.assertEqual(
            HOST_SILENCE_REASONS,
            ("no_source_change", "user_active", "quota_exhausted", "quiet_window", "gate_error"),
        )


class ExecutorReasonTests(unittest.TestCase):
    def setUp(self) -> None:
        from p3_fixtures import make_pack_builder, make_run_request

        self.pack_builder = make_pack_builder()
        self.request = make_run_request()

    def _run(self, executor: ToolLoopExecutor, pack):
        import asyncio

        return asyncio.run(
            executor.execute(self.request, pack, [], instruction="检查变化")
        )

    def _pack(self):
        import asyncio

        return asyncio.run(
            self.pack_builder.build(
                goal_id="g", scope="s", source_records=[], now_ms=T0
            )
        )

    def test_missing_reason_gets_exactly_one_repair(self):
        missing = {
            "decision": "propose",
            "summary": "s",
            "proposals": [
                {k: v for k, v in _proposal().items() if k != "reason"}
            ],
        }
        valid = _document()
        model = ScriptedModel(
            [
                {"content": json.dumps(missing, ensure_ascii=False)},
                {"content": json.dumps(valid, ensure_ascii=False)},
            ]
        )
        executor = make_executor(model)
        outcome = self._run(executor, self._pack())
        self.assertEqual(outcome.decision.proposals[0].reason, "changed")
        self.assertTrue(outcome.repair_used)
        self.assertEqual(len(model.calls), 2)

    def test_missing_reason_twice_fails_closed(self):
        missing = {
            "decision": "propose",
            "summary": "s",
            "proposals": [{"kind": "draft", "fact_id": "f", "revision": "1"}],
        }
        model = ScriptedModel(
            [
                {"content": json.dumps(missing, ensure_ascii=False)},
                {"content": json.dumps(missing, ensure_ascii=False)},
            ]
        )
        executor = make_executor(model)
        with self.assertRaises(PASError) as ctx:
            self._run(executor, self._pack())
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)
        self.assertIn("'reason'", ctx.exception.safe_message)

    def test_gray_switch_builds_an_executor_that_accepts_absence(self):
        missing = {
            "decision": "propose",
            "summary": "s",
            "proposals": [{"kind": "draft", "fact_id": "f", "revision": "1"}],
        }
        model = ScriptedModel([{"content": json.dumps(missing, ensure_ascii=False)}])
        from proactive_sdk import FakeClock

        executor = ToolLoopExecutor(
            model=model,
            broker=make_broker(),
            config=ExecutorConfig(require_reason=False),
            clock=FakeClock(wall_ms=T0),
        )
        outcome = self._run(executor, self._pack())
        self.assertIsNone(outcome.decision.proposals[0].reason)

    def test_unknown_silence_reason_is_an_event_not_a_crash(self):
        model = ScriptedModel(
            [
                {
                    "content": json.dumps(
                        {
                            "decision": "silent",
                            "summary": "无变化",
                            "proposals": [],
                            "silence_reason": "bored",
                        },
                        ensure_ascii=False,
                    )
                }
            ]
        )
        executor = make_executor(model)
        outcome = self._run(executor, self._pack())
        self.assertEqual(outcome.decision.silence_reason, SILENCE_UNSPECIFIED)
        kinds = [event.kind for event in outcome.events]
        self.assertIn("silence_reason_unknown", kinds)


class LedgerProjectionTests(unittest.TestCase):
    """Model text → run ledger: reason and silence reason must be readable."""

    def setUp(self) -> None:
        import asyncio

        self._run_async = asyncio.run

    def test_reason_is_persisted_and_visible_through_run_proposals(self):
        store, _ = make_store()
        job = make_job()
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel(
            [{"content": json.dumps(_document(), ensure_ascii=False)}]
        )
        source = ScriptedSource([static_batch([("item-1", "rev-1", "hello")])])
        coordinator = make_coordinator(store, model, source)
        report = self._run_async(coordinator.process_pending_run())
        self.assertEqual(report.outcome, "proposed")
        run = store.list_runs(state="proposed")[0]
        proposals = store.run_proposals(run["run_id"])
        self.assertEqual(proposals[0]["reason"], "changed")

    def test_suppressed_run_records_the_semantic_silence_reason(self):
        store, _ = make_store()
        job = make_job()
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        # Two heartbeat ticks: the first sees a change, the second does not.
        model = ScriptedModel(
            [
                {"content": json.dumps({"decision": "silent", "summary": "无变化", "proposals": []})}
            ]
        )
        source = ScriptedSource(
            [
                static_batch(items=[("item-1", "rev-1", "hello")], cursor="cursor-1"),
                static_batch(items=[], cursor="cursor-1"),
            ]
        )
        coordinator = make_coordinator(store, model, source)
        first = self._run_async(coordinator.process_pending_run())
        self.assertEqual(first.silence_reason, SILENCE_UNSPECIFIED)

        admit_job_event(store, job, observed_ms=T0 + 1_800_000)
        second = self._run_async(coordinator.process_pending_run())
        self.assertEqual(second.outcome, "suppressed")
        self.assertEqual(second.silence_reason, "no_source_change")

        run = store.list_runs(state="suppressed")[0]
        self.assertEqual(run["silence_reason"], "no_source_change")
        # Raw machine verdict stays in the run events for debugging.
        events = store.run_events(run["run_id"])
        self.assertEqual(events[0]["kind"], "suppressed")
        self.assertEqual(events[0]["safe_summary"], "l0_no_source_change")
        # The user-visible activity projection carries the semantic reason.
        activity = store.job_activity(job.job_id, phase="analysis")
        self.assertEqual(activity[0]["state"], "suppressed")
        self.assertEqual(activity[0]["reason"], "no_source_change")

    def test_model_silence_reason_is_persisted_on_the_run(self):
        store, _ = make_store()
        job = make_job()
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel(
            [
                {
                    "content": json.dumps(
                        {
                            "decision": "silent",
                            "summary": "这事已经处理过",
                            "proposals": [],
                            "silence_reason": "already_handled",
                        },
                        ensure_ascii=False,
                    )
                }
            ]
        )
        source = ScriptedSource([static_batch([("item-1", "rev-1", "hello")])])
        coordinator = make_coordinator(store, model, source)
        report = self._run_async(coordinator.process_pending_run())
        self.assertEqual(report.silence_reason, "already_handled")
        run = store.list_runs(state="proposed")[0]
        self.assertEqual(run["silence_reason"], "already_handled")
        activity = store.job_activity(job.job_id, phase="analysis")
        self.assertEqual(activity[0]["reason"], "already_handled")


if __name__ == "__main__":
    unittest.main()
