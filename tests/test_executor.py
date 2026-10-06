"""P3 executor tests: bounded tool loop, decision validation, evidence
closure, budgets, deadline, cancellation, usage honesty (SPEC §8,
§16.1 Execution row, EXEC-01)."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # p3_fixtures
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from p3_fixtures import (
    P3TestCase,
    ScriptedModel,
    T0,
    make_broker,
    make_executor,
    make_pack_builder,
    make_run_request,
    rfc3339,
)
from proactive_sdk import (
    ErrorCode,
    FakeClock,
    PASError,
    RunBudget,
    RunCancelled,
    SourceBatch,
    ToolSpec,
)
from proactive_sdk.executor import ToolLoopExecutor

PROPOSE = {
    "decision": "propose",
    "summary": "跟踪的来源出现新修订。",
    "proposals": [
        {
            "kind": "notify_self",
            "fact_id": "item-1",
            "revision": "rev-2",
            "body": "发现一条与你跟踪目标相关的新内容。",
            "evidence_refs": ["snapshot:calendar:snap1"],
            "expires_at": rfc3339(T0 + 3_600_000),
        }
    ],
}


SILENT = {"decision": "silent", "summary": "s", "proposals": []}


def turn(decision_body: dict | None = None, **kwargs) -> dict:
    """Wrap a decision body dict as a scripted model turn."""
    if decision_body is not None:
        kwargs["content"] = json.dumps(decision_body, ensure_ascii=False)
    return kwargs


async def build_pack(executor=None):
    builder = make_pack_builder()
    return await builder.build(
        goal_id="watch-x",
        scope="job:heartbeat",
        source_records=[
            {
                "fact_id": "item-1",
                "revision": "rev-2",
                "content": "source says: new revision",
                "sensitivity": "private",
                "tombstone": False,
                "snapshot_ref": "snapshot:calendar:snap1",
                "source_id": "calendar",
                "account_ref": "account:primary",
                "observed_at_ms": T0,
                "fresh_until_ms": T0 + 1_800_000,
            }
        ],
        now_ms=T0,
    )


class DecisionValidationTests(P3TestCase):
    def test_propose_decision_with_valid_evidence_is_accepted(self):
        model = ScriptedModel([turn(PROPOSE)])
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        outcome = self.run_async(
            executor.execute(make_run_request(), pack, [], instruction="检查变化")
        )
        self.assertEqual(outcome.decision.decision, "propose")
        self.assertEqual(len(outcome.decision.proposals), 1)
        self.assertEqual(outcome.model_turns, 1)
        self.assertEqual(outcome.repair_used, False)

    def test_silent_with_empty_proposals_is_accepted(self):
        model = ScriptedModel(
            [{"content": '{"decision":"silent","summary":"无变化","proposals":[]}'}]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        outcome = self.run_async(
            executor.execute(make_run_request(), pack, [], instruction="检查变化")
        )
        self.assertEqual(outcome.decision.decision, "silent")

    def test_silent_with_proposals_is_rejected_after_single_repair(self):
        model = ScriptedModel(
            [
                {"content": '{"decision":"silent","summary":"x","proposals":[{"kind":"draft","fact_id":"f","revision":"r"}]}'},
                {"content": "still not JSON"},
            ]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        with self.assertRaises(PASError) as caught:
            self.run_async(executor.execute(make_run_request(), pack, [], instruction="检查变化"))
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_CONFIG)
        # The repair prompt reached the model on the second call.
        self.assertTrue(model.calls[1]["messages"][-1]["content"].startswith("Your previous output"))

    def test_fabricated_evidence_is_rejected(self):
        malicious = dict(PROPOSE)
        malicious["proposals"] = [
            {
                "kind": "notify_self",
                "fact_id": "item-1",
                "revision": "rev-2",
                "evidence_refs": ["snapshot:calendar:INVENTED"],
                "expires_at": rfc3339(T0 + 3_600_000),
            }
        ]
        model = ScriptedModel([turn(malicious), turn(malicious)])
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        with self.assertRaises(PASError) as caught:
            self.run_async(executor.execute(make_run_request(), pack, [], instruction="检查变化"))
        self.assertIn("unknown evidence", caught.exception.safe_message)

    def test_unknown_kind_and_extra_fields_are_rejected(self):
        for bad in (
            '{"decision":"propose","summary":"s","proposals":[{"kind":"send_email","fact_id":"f","revision":"r"}]}',
            '{"decision":"propose","summary":"s","proposals":[{"kind":"draft","fact_id":"f","revision":"r","to":"attacker@example.com"}]}',
            '{"decision":"propose","summary":"s","proposals":[],"priority":"high"}',
        ):
            model = ScriptedModel([{"content": bad}, {"content": bad}])
            executor = make_executor(model)
            pack = self.run_async(build_pack())
            with self.assertRaises(PASError):
                self.run_async(executor.execute(make_run_request(), pack, [], instruction="检查变化"))

    def test_proposals_over_budget_are_rejected(self):
        many = {
            "decision": "propose",
            "summary": "s",
            "proposals": [
                {"kind": "internal_record", "fact_id": f"f{i}", "revision": "r"} for i in range(4)
            ],
        }
        model = ScriptedModel([turn(many), turn(many)])
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        request = make_run_request(budget=RunBudget(max_proposals=3))
        with self.assertRaises(PASError) as caught:
            self.run_async(executor.execute(request, pack, [], instruction="检查变化"))
        self.assertIn("exceed the budget", caught.exception.safe_message)

    def test_duplicate_json_keys_rejected(self):
        bad = '{"decision":"propose","decision":"silent","summary":"s","proposals":[]}'
        model = ScriptedModel([{"content": bad}, {"content": bad}])
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        with self.assertRaises(PASError):
            self.run_async(executor.execute(make_run_request(), pack, [], instruction="检查变化"))


class ToolLoopTests(P3TestCase):
    def test_tool_call_round_trip_and_evidence_closure(self):
        model = ScriptedModel(
            [
                {"tool_calls": [("c1", "read_evidence", {"fact_id": "item-1"})]},
                turn(
                    {
                        "decision": "propose",
                        "summary": "s",
                        "proposals": [
                            {
                                "kind": "internal_record",
                                "fact_id": "item-1",
                                "revision": "rev-2",
                                "evidence_refs": ["tool:read_evidence:c1"],
                            }
                        ],
                    }
                ),
            ]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        outcome = self.run_async(
            executor.execute(make_run_request(), pack, [], instruction="检查变化")
        )
        self.assertEqual(outcome.tool_calls, 1)
        self.assertEqual(outcome.tool_denied, 0)
        self.assertEqual(outcome.tool_evidence_refs, ("tool:read_evidence:c1",))
        self.assertEqual(outcome.decision.proposals[0].evidence_refs, ("tool:read_evidence:c1",))
        # The tool result reached the model as a data-only message.
        tool_messages = [
            m for m in model.calls[1]["messages"] if m.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("untrusted data", tool_messages[0]["content"])
        self.assertIn("evidence for item-1", tool_messages[0]["content"])

    def test_unregistered_tool_is_denied_and_counted(self):
        model = ScriptedModel(
            [
                {"tool_calls": [("c1", "delete_everything", {})]},
                turn({"decision": "silent", "summary": "无可用工具", "proposals": []}),
            ]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        outcome = self.run_async(
            executor.execute(make_run_request(), pack, [], instruction="检查变化")
        )
        self.assertEqual(outcome.tool_denied, 1)
        self.assertEqual(outcome.tool_calls, 1)
        self.assertIn("permission_denied", model.calls[1]["messages"][-1]["content"])

    def test_empty_allowlist_denies_every_tool(self):
        model = ScriptedModel(
            [
                {"tool_calls": [("c1", "read_evidence", {"fact_id": "x"})]},
                turn(SILENT),
            ]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        outcome = self.run_async(
            executor.execute(make_run_request(tool_allowlist=()), pack, [], instruction="检查变化")
        )
        self.assertEqual(outcome.tool_denied, 1)

    def test_tool_arguments_failing_schema_are_denied(self):
        model = ScriptedModel(
            [
                {"tool_calls": [("c1", "read_evidence", {"fact_id": 12345})]},
                turn(SILENT),
            ]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        outcome = self.run_async(
            executor.execute(make_run_request(), pack, [], instruction="检查变化")
        )
        self.assertEqual(outcome.tool_denied, 1)
        self.assertIn("invalid_config", model.calls[1]["messages"][-1]["content"])

    def test_capability_not_granted_denies_tool(self):
        model = ScriptedModel(
            [
                {"tool_calls": [("c1", "read_evidence", {"fact_id": "x"})]},
                turn(SILENT),
            ]
        )
        # Broker without the calendar.read capability.
        executor = make_executor(model, broker=make_broker(capabilities=(), with_evidence_tool=True))
        pack = self.run_async(build_pack())
        outcome = self.run_async(
            executor.execute(make_run_request(), pack, [], instruction="检查变化")
        )
        self.assertEqual(outcome.tool_denied, 1)
        self.assertIn("not granted", model.calls[1]["messages"][-1]["content"])


class BudgetAndDeadlineTests(P3TestCase):
    def test_model_turn_budget_exhaustion(self):
        endless_tool_call = {"tool_calls": [("c1", "read_evidence", {"fact_id": "x"})]}
        model = ScriptedModel([endless_tool_call] * 5)
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        request = make_run_request(budget=RunBudget(max_model_turns=2, max_tool_calls=10))
        with self.assertRaises(PASError) as caught:
            self.run_async(executor.execute(request, pack, [], instruction="检查变化"))
        self.assertEqual(caught.exception.code, ErrorCode.BUDGET_EXCEEDED)

    def test_tool_call_on_final_turn_fails_budget(self):
        model = ScriptedModel(
            [
                {"tool_calls": [("c1", "read_evidence", {"fact_id": "x"})]},
            ]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        request = make_run_request(budget=RunBudget(max_model_turns=1, max_tool_calls=5))
        with self.assertRaises(PASError) as caught:
            self.run_async(executor.execute(request, pack, [], instruction="检查变化"))
        self.assertEqual(caught.exception.code, ErrorCode.BUDGET_EXCEEDED)

    def test_tool_call_budget_exhaustion(self):
        model = ScriptedModel(
            [
                {"tool_calls": [(f"c{i}", "read_evidence", {"fact_id": "x"}) for i in range(3)]},
                turn(SILENT),
            ]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        request = make_run_request(budget=RunBudget(max_model_turns=4, max_tool_calls=2))
        with self.assertRaises(PASError) as caught:
            self.run_async(executor.execute(request, pack, [], instruction="检查变化"))
        self.assertEqual(caught.exception.code, ErrorCode.BUDGET_EXCEEDED)

    def test_wall_time_budget_uses_monotonic_infrastructure_time(self):
        class AdvancingModel(ScriptedModel):
            def __init__(self, turns, clock, step_ms):
                super().__init__(turns)
                self._clock = clock
                self._step_ms = step_ms

            async def generate(self, request):
                self._clock.advance_mono(self._step_ms)
                return await super().generate(request)

        clock = FakeClock(wall_ms=T0, mono_ms=0)
        model = AdvancingModel(
            [
                {"tool_calls": [("c1", "read_evidence", {"fact_id": "x"})]},
                turn(SILENT),
            ],
            clock,
            step_ms=2000,
        )
        executor = make_executor(model, clock=clock)
        pack = self.run_async(build_pack())
        request = make_run_request(budget=RunBudget(wall_time_s=3))
        with self.assertRaises(PASError) as caught:
            self.run_async(executor.execute(request, pack, [], instruction="检查变化"))
        self.assertEqual(caught.exception.code, ErrorCode.BUDGET_EXCEEDED)

    def test_run_wall_deadline_exceeded(self):
        model = ScriptedModel([turn(SILENT)])
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        request = make_run_request(deadline=rfc3339(T0 - 1))
        with self.assertRaises(PASError) as caught:
            self.run_async(executor.execute(request, pack, [], instruction="检查变化"))
        self.assertEqual(caught.exception.code, ErrorCode.DEADLINE_EXCEEDED)
        self.assertEqual(model.calls, [])  # never called the model

    def test_cancel_event_stops_run(self):
        model = ScriptedModel([turn(SILENT)])
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        cancel = asyncio.Event()
        cancel.set()
        with self.assertRaises(RunCancelled):
            self.run_async(
                executor.execute(make_run_request(), pack, [], instruction="检查变化", cancel_event=cancel)
            )
        self.assertEqual(model.calls, [])


class UsageHonestyTests(P3TestCase):
    def test_measured_usage_is_summed(self):
        model = ScriptedModel(
            [
                {"usage": {"input_tokens": 100, "output_tokens": 20, "pricing_basis": "measured"}},
                {"tool_calls": [("c1", "read_evidence", {"fact_id": "x"})],
                 "usage": {"input_tokens": 150, "output_tokens": 30, "pricing_basis": "measured"}},
                turn(SILENT, usage={"input_tokens": 200, "output_tokens": 10, "pricing_basis": "measured"}),
            ]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        outcome = self.run_async(
            executor.execute(make_run_request(), pack, [], instruction="检查变化")
        )
        self.assertEqual(outcome.usage["input_tokens"], 450)
        self.assertEqual(outcome.usage["output_tokens"], 60)
        self.assertEqual(outcome.usage["tool_calls"], 1)
        self.assertEqual(outcome.usage["pricing_basis"], "measured")

    def test_unknown_turn_keeps_unknown_basis_and_null_fields(self):
        model = ScriptedModel(
            [
                {"usage": {"input_tokens": 10, "output_tokens": 5, "pricing_basis": "measured"}},
                turn(SILENT),  # no usage
            ]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        outcome = self.run_async(
            executor.execute(make_run_request(), pack, [], instruction="检查变化")
        )
        self.assertEqual(outcome.usage["pricing_basis"], "unknown")
        self.assertIsNone(outcome.usage["input_tokens"])
        self.assertIsNone(outcome.usage["output_tokens"])

    def test_reasoning_never_reaches_messages(self):
        model = ScriptedModel(
            [
                turn(SILENT, reasoning="SECRET-CHAIN-TEXT"),
            ]
        )
        executor = make_executor(model)
        pack = self.run_async(build_pack())
        self.run_async(executor.execute(make_run_request(), pack, [], instruction="检查变化"))
        for call in model.calls:
            blob = repr(call["messages"])
            self.assertNotIn("SECRET-CHAIN-TEXT", blob)


class ToolSpecTests(P3TestCase):
    def test_write_tools_are_refused_in_p3(self):
        with self.assertRaises(PASError) as caught:
            ToolSpec(name="send_money", description="x", read_only=False)
        self.assertEqual(caught.exception.code, ErrorCode.UNSUPPORTED_CAPABILITY)

    def test_bad_name_refused(self):
        with self.assertRaises(PASError):
            ToolSpec(name="Send-Mail", description="x")


if __name__ == "__main__":
    unittest.main()
