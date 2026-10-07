"""SPEC §22.1 item 1: the executor seam.

What this module pins: **the coordinator depends on ``executor.RunExecutor``
and nothing else.** Before v0.1.2 it read ``.broker`` / ``.config`` off a
concrete ``ToolLoopExecutor``, so a third party could implement the
documented protocol perfectly and still crash on the first run.

The decisive test is ``test_minimal_executor_drives_a_full_closed_loop``:
a class that implements *only* ``context()`` and ``execute()`` — no
broker, no config, no inheritance — drives L0 → L1 → policy → outbox.
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
    ActionProposal,
    Decision,
    ErrorCode,
    ExecutorContext,
    ExecutorOutcome,
    FakeClock,
    Job,
    LocalToolBroker,
    PASError,
    ProactiveAgent,
    RunBudget,
    RunExecutor,
    ToolLoopExecutor,
)

T0 = 1_760_000_000_000


class MinimalExecutor:
    """Implements ONLY the documented seam. No broker, no config, no base class."""

    def __init__(
        self,
        *,
        decision: Decision | None = None,
        capabilities: tuple[str, ...] = (),
        tools: tuple[str, ...] = (),
        budget: RunBudget | None = None,
        fail_context: bool = False,
    ) -> None:
        self._decision = decision
        self._caps = frozenset(capabilities)
        self._tools = tuple(tools)
        self._budget = budget or RunBudget()
        self._fail_context = fail_context
        self.context_calls = 0
        self.execute_calls = 0
        self.seen_instruction: str | None = None

    async def context(self) -> ExecutorContext:
        self.context_calls += 1
        if self._fail_context:
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "host is unreachable")
        return ExecutorContext(
            capabilities=self._caps, tool_names=self._tools, budget=self._budget
        )

    async def execute(self, request, pack, source_records, *, instruction, cancel_event=None):
        self.execute_calls += 1
        self.seen_instruction = instruction
        assert self._decision is not None, "test bug: no scripted decision"
        return ExecutorOutcome(
            decision=self._decision,
            usage={"protocol_version": "1.0", "model_turns": 0, "tool_calls": 0},
            model_turns=0,
        )


def _notify_decision() -> Decision:
    return Decision(
        decision="propose",
        summary="有一个需要告知的新情况",
        proposals=(
            ActionProposal(
                kind="notify_self",
                fact_id="fact-1",
                revision="1",
                body="只通知本人",
                arguments={},
                evidence_refs=("snapshot:fixture",),
                expires_at="2030-01-01T00:00:00Z",
            ),
        ),
    )


class MinimalExecutorSeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(wall_ms=T0)

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, executor) -> ProactiveAgent:
        return ProactiveAgent(
            state_dir=Path(self.tmp.name),
            executor=executor,
            clock=self.clock,
            timezone="UTC",
            locale="en",
            profile="seam",
        )

    def _prepare(self, agent: ProactiveAgent) -> None:
        grant = agent.create_grant_from_user_consent(
            capability=NOTIFY_SELF_CAPABILITY,
            account_ref="account:primary",
            scope={},
            consent_evidence_ref="consent:seam",
        )
        agent.jobs_upsert(
            Job(
                id="seam-job",
                mode="task",
                schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 3600},
                instruction="检查一下，只通知本人。",
                grant_refs=(grant.grant_id,),
            ),
            idempotency_key="seam-job-v1",
        )

    def test_minimal_executor_drives_a_full_closed_loop(self):
        executor = MinimalExecutor(decision=_notify_decision())
        agent = self._agent(executor)
        try:
            self._prepare(agent)
            agent.trigger_job("seam-job", reason="seam test")
            report = asyncio.run(agent.tick())

            runs = list(report["runs"])
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["outcome"], "proposed")
            self.assertEqual(runs[0]["policy_outcome"], "actions_queued")
            self.assertEqual(executor.context_calls, 1)
            self.assertEqual(executor.execute_calls, 1)
            self.assertEqual(executor.seen_instruction, "检查一下，只通知本人。")

            queued = agent.store.list_outbox()
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0]["destination_ref"], "local-inbox:seam")
        finally:
            asyncio.run(agent.close())

    def test_context_failure_fails_the_run_before_any_analysis(self):
        executor = MinimalExecutor(fail_context=True)
        agent = self._agent(executor)
        try:
            self._prepare(agent)
            agent.trigger_job("seam-job", reason="seam test")
            report = asyncio.run(agent.tick())
            runs = report["runs"]
            self.assertEqual(runs[0]["outcome"], "failed")
            self.assertEqual(executor.execute_calls, 0)  # analysis never started
            self.assertEqual(agent.store.list_outbox(), [])
        finally:
            asyncio.run(agent.close())

    def test_declared_capabilities_and_tools_come_from_the_executor(self):
        seen: dict[str, object] = {}

        class Recording(MinimalExecutor):
            async def execute(self, request, pack, source_records, *, instruction, cancel_event=None):
                seen["budget"] = request.budget
                seen["allowlist"] = request.tool_allowlist
                return await super().execute(
                    request, pack, source_records, instruction=instruction, cancel_event=cancel_event
                )

        executor = Recording(
            decision=Decision(decision="silent", summary="无事"),
            tools=("read_evidence", "fetch_page"),
            budget=RunBudget(max_model_turns=3),
        )
        agent = self._agent(executor)
        try:
            self._prepare(agent)
            agent.trigger_job("seam-job", reason="seam test")
            asyncio.run(agent.tick())
        finally:
            asyncio.run(agent.close())
        self.assertEqual(seen["allowlist"], ("read_evidence", "fetch_page"))
        self.assertEqual(seen["budget"].max_model_turns, 3)

    def test_an_operator_allowlist_still_narrows_what_the_executor_offers(self):
        seen: dict[str, object] = {}

        class Recording(MinimalExecutor):
            async def execute(self, request, pack, source_records, *, instruction, cancel_event=None):
                seen["allowlist"] = request.tool_allowlist
                return await super().execute(
                    request, pack, source_records, instruction=instruction, cancel_event=cancel_event
                )

        executor = Recording(
            decision=Decision(decision="silent", summary="无事"),
            tools=("read_evidence", "fetch_page"),
        )
        agent = ProactiveAgent(
            state_dir=Path(self.tmp.name),
            executor=executor,
            clock=self.clock,
            timezone="UTC",
            locale="en",
            profile="seam",
            tool_allowlist=("read_evidence",),
        )
        try:
            self._prepare(agent)
            agent.trigger_job("seam-job", reason="seam test")
            asyncio.run(agent.tick())
        finally:
            asyncio.run(agent.close())
        self.assertEqual(seen["allowlist"], ("read_evidence",))


class SeamConformanceTests(unittest.TestCase):
    """`RunExecutor` must describe what the coordinator really consumes."""

    def test_builtin_executor_satisfies_the_seam(self):
        executor = ToolLoopExecutor(
            model=type("M", (), {"generate": staticmethod(lambda *a, **k: None)})(),
            broker=LocalToolBroker(capabilities=frozenset({"calendar.read"})),
        )
        self.assertIsInstance(executor, RunExecutor)

    def test_a_minimal_implementation_satisfies_the_seam(self):
        self.assertIsInstance(MinimalExecutor(), RunExecutor)

    def test_an_object_without_the_seam_does_not(self):
        self.assertNotIsInstance(object(), RunExecutor)

    def test_the_seam_is_narrower_than_the_agent_executor_port(self):
        """A host session port is not a run executor — one does not imply the other."""
        from proactive_sdk.contracts import AgentExecutor

        class HostSession:  # implements contracts.AgentExecutor only
            async def capabilities(self): ...
            async def start(self, request): ...
            def events(self, handle, after_seq=0): ...
            async def status(self, handle): ...
            async def cancel(self, handle): ...
            async def close(self): ...

        self.assertIsInstance(HostSession(), AgentExecutor)
        self.assertNotIsInstance(HostSession(), RunExecutor)

    def test_builtin_executor_accepts_a_duck_typed_broker(self):
        class ForeignBroker:
            capabilities = frozenset({"calendar.read"})

            def tool_names(self):
                return ("only_tool",)

            def tool_schemas(self, allowlist):
                return ({"name": "only_tool"},)

            async def call(self, attempt, *, context):  # pragma: no cover - not exercised
                raise NotImplementedError

        executor = ToolLoopExecutor(
            model=type("M", (), {"generate": staticmethod(lambda *a, **k: None)})(),
            broker=ForeignBroker(),
        )
        ctx = asyncio.run(executor.context())
        self.assertEqual(ctx.capabilities, frozenset({"calendar.read"}))
        self.assertEqual(ctx.tool_names, ("only_tool",))

    def test_a_broker_missing_members_is_still_rejected(self):
        with self.assertRaises(PASError) as ctx:
            ToolLoopExecutor(
                model=type("M", (), {"generate": staticmethod(lambda *a, **k: None)})(),
                broker=object(),
            )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)
        self.assertIn("broker must provide", ctx.exception.safe_message)


class ExecutorContextTests(unittest.TestCase):
    def test_normalizes_sequences_and_sets(self):
        ctx = ExecutorContext(capabilities=["a", "b"], tool_names=["t"])
        self.assertEqual(ctx.capabilities, frozenset({"a", "b"}))
        self.assertEqual(ctx.tool_names, ("t",))

    def test_defaults_are_empty_and_budgeted(self):
        ctx = ExecutorContext()
        self.assertEqual(ctx.capabilities, frozenset())
        self.assertEqual(ctx.tool_names, ())
        self.assertIsInstance(ctx.budget, RunBudget)

    def test_rejects_a_bare_string(self):
        # "abc" must not silently become three capabilities.
        with self.assertRaises(PASError):
            ExecutorContext(capabilities="abc")
        with self.assertRaises(PASError):
            ExecutorContext(tool_names="abc")

    def test_rejects_blank_names_and_a_foreign_budget(self):
        with self.assertRaises(PASError):
            ExecutorContext(capabilities=[""])
        with self.assertRaises(PASError):
            ExecutorContext(budget={"max_model_turns": 1})


if __name__ == "__main__":
    unittest.main()
