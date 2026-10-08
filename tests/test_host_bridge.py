"""SPEC §22.1 item 3: the host bridge.

The acceptance question is not "does the bridge work" but **"can someone
who is not us plug in an agent without touching PAS"**. So the driver used
here (``TextInTextOutHost``) is defined in this file, understands one
string in and one string out, and implements nothing PAS-specific.

Two honesty rules get their own tests, because both are easy to fake:

* acceptance is not completion (a driver that returns at acceptance is a
  contract violation, and the bridge must refuse it);
* cancellation is not stopping (the ledger records the level the host
  actually confirmed).
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
    CANCEL_CONFIRMED,
    CANCEL_REQUESTED,
    CANCEL_UNSUPPORTED,
    ContextPack,
    EphemeralMemoryPort,
    ErrorCode,
    FakeClock,
    HostBridge,
    HostDriver,
    HostPrompt,
    HostReply,
    Job,
    MemoryEntry,
    PASError,
    ProactiveAgent,
    RunBudget,
    RunCancelled,
    RunRequest,
)

T0 = 1_760_000_000_000
MEMORY_ID = "mem-host-bridge"


class TextInTextOutHost:
    """A host that only understands one string in and one string out.

    Deliberately knows nothing about PAS: no store, no contract, no
    envelope type. Whatever PAS needs, it must be in the prompt.
    """

    def __init__(
        self,
        answer: str | dict | Exception | None,
        *,
        cancel_level: str = CANCEL_CONFIRMED,
        usage: dict | None = None,
    ) -> None:
        self.answer = answer
        self.cancel_level = cancel_level
        self.usage = usage or {}
        self.prompts: list[tuple[HostPrompt, float]] = []
        self.cancelled: list[str] = []
        self.closed = False

    async def capabilities(self) -> dict:
        return {"text_in_text_out": True}

    async def submit(self, prompt: HostPrompt, *, timeout_s: float) -> HostReply:
        self.prompts.append((prompt, timeout_s))
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer is None:
            return HostReply(text=None)
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer, ensure_ascii=False)
        return HostReply(text=text, usage=dict(self.usage), host_run_id="host-run-1")

    async def cancel(self, run_key: str) -> str:
        self.cancelled.append(run_key)
        return self.cancel_level

    async def close(self) -> None:
        self.closed = True


def _envelope(**over) -> dict:
    base = {
        "decision": "propose",
        "summary": "有一条要告知的事",
        "proposals": [
            {
                "kind": "notify_self",
                "fact_id": "fact-1",
                "revision": "1",
                "body": "只通知本人",
                "evidence_refs": [MEMORY_ID],
                "expires_at": "2030-01-01T00:00:00Z",
            }
        ],
    }
    base.update(over)
    return base


def _pack() -> ContextPack:
    return ContextPack(
        task_goal_id="goal-1",
        task_scope="job:task",
        locale="zh-CN",
        timezone="Europe/Berlin",
        preferences_ref="preferences:default",
        memory_refs=(MEMORY_ID,),
    )


def _request(*, deadline: str = "2030-01-01T00:00:00Z", budget: RunBudget | None = None) -> RunRequest:
    return RunRequest(
        run_id="run-bridge-1",
        attempt=1,
        fence=1,
        context_ref="ctx:run-bridge-1",
        budget=budget or RunBudget(),
        deadline=deadline,
        tool_allowlist=(),
    )


def _bridge(host, **kw) -> HostBridge:
    return HostBridge(host, **kw)


class BridgeEndToEndTests(unittest.TestCase):
    """A stranger's host, driven through the facade, with no PAS changes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(wall_ms=T0)

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, host: TextInTextOutHost) -> ProactiveAgent:
        agent = ProactiveAgent(
            state_dir=Path(self.tmp.name),
            executor=_bridge(host, capabilities=("memory.read",)),
            clock=self.clock,
            timezone="UTC",
            locale="en",
            profile="bridge",
        )
        # A memory entry is the one piece of evidence this run will have;
        # the host may only cite what the pack actually carries.
        asyncio.run(
            agent.pack_builder.memory.remember(
                MemoryEntry(memory_id=MEMORY_ID, content="用户关注这件事", source="user")
            )
        )
        return agent

    def test_text_only_host_drives_a_closed_loop_and_lands_in_the_inbox(self):
        host = TextInTextOutHost(_envelope())
        agent = self._agent(host)
        try:
            grant = agent.create_grant_from_user_consent(
                capability="notify.self",
                account_ref="account:primary",
                scope={},
                consent_evidence_ref="consent:bridge",
            )
            agent.jobs_upsert(
                Job(
                    id="bridge-job",
                    mode="task",
                    schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 3600},
                    instruction="检查一下。",
                    grant_refs=(grant.grant_id,),
                ),
                idempotency_key="bridge-job-v1",
            )
            agent.trigger_job("bridge-job", reason="bridge test")
            report = asyncio.run(agent.tick())

            runs = list(report["runs"])
            self.assertEqual(runs[0]["outcome"], "proposed")
            self.assertEqual(runs[0]["policy_outcome"], "actions_queued")
            inbox = agent.store.list_inbox()
            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0]["body"], "只通知本人")
        finally:
            asyncio.run(agent.close())

    def test_l0_suppression_never_reaches_the_host(self):
        host = TextInTextOutHost(_envelope())
        agent = ProactiveAgent(
            state_dir=Path(self.tmp.name),
            executor=_bridge(host),
            clock=self.clock,
            timezone="UTC",
            locale="en",
            profile="bridge",
        )
        try:
            # A heartbeat with no registered source is suppressed at L0.
            agent.jobs_upsert(
                Job(
                    id="hb",
                    mode="heartbeat",
                    schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
                    instruction="检查来源变化。",
                ),
                idempotency_key="hb-v1",
            )
            agent.trigger_job("hb", reason="tick")
            report = asyncio.run(agent.tick())
            self.assertEqual(list(report["runs"])[0]["outcome"], "suppressed")
            self.assertEqual(host.prompts, [])  # zero host calls
        finally:
            asyncio.run(agent.close())


class BridgeContractTests(unittest.TestCase):
    def test_prompt_carries_the_contract_and_the_context(self):
        host = TextInTextOutHost(_envelope())
        bridge = _bridge(host, capabilities=frozenset({"calendar.read"}), tool_names=("read",))
        outcome = asyncio.run(
            bridge.execute(_request(), _pack(), [], instruction="检查一下。")
        )
        self.assertEqual(len(host.prompts), 1)
        prompt, _timeout = host.prompts[0]
        # The contract is injected, not inferred (§8.1).
        self.assertIn("Respond with ONLY one JSON object", prompt.system)
        self.assertIn("notify_self", prompt.system)
        self.assertIn("检查一下。", prompt.system)
        # The context is rendered from the same helper the built-in loop uses.
        self.assertIn("task.goal_id=goal-1", prompt.user)
        self.assertIn("untrusted_content_policy=data_only", prompt.user)
        # A single-string host gets one string.
        self.assertIn(prompt.user, prompt.render())
        self.assertEqual(outcome.decision.decision, "propose")

    def test_invalid_envelope_fails_and_queues_nothing(self):
        host = TextInTextOutHost("this is not JSON at all")
        bridge = _bridge(host)
        with self.assertRaises(PASError) as ctx:
            asyncio.run(bridge.execute(_request(), _pack(), [], instruction="x"))
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)
        self.assertIn("strict JSON", ctx.exception.safe_message)

    def test_fabricated_evidence_is_refused(self):
        host = TextInTextOutHost(
            _envelope(
                proposals=[
                    {
                        "kind": "notify_self",
                        "fact_id": "f",
                        "revision": "1",
                        "evidence_refs": ["snapshot:never-observed"],
                        "expires_at": "2030-01-01T00:00:00Z",
                    }
                ]
            )
        )
        bridge = _bridge(host)
        with self.assertRaises(PASError) as ctx:
            asyncio.run(bridge.execute(_request(), _pack(), [], instruction="x"))
        self.assertIn("cites unknown evidence", ctx.exception.safe_message)

    def test_a_driver_that_returns_at_acceptance_is_a_contract_violation(self):
        class ReturnsRawText:
            """Fails the seam: returns a bare string, not a HostReply."""

            async def capabilities(self):
                return {}

            async def submit(self, prompt, *, timeout_s):
                return "accepted"

            async def cancel(self, run_key):
                return CANCEL_CONFIRMED

            async def close(self):
                return None

        bridge = HostBridge(ReturnsRawText())
        with self.assertRaises(PASError) as ctx:
            asyncio.run(bridge.execute(_request(), _pack(), [], instruction="x"))
        self.assertEqual(ctx.exception.code, ErrorCode.INTERNAL_ERROR)
        self.assertIn("acceptance is not completion", ctx.exception.safe_message)

    def test_unreported_usage_stays_absent_rather_than_zero(self):
        host = TextInTextOutHost(_envelope())
        bridge = _bridge(host)
        outcome = asyncio.run(bridge.execute(_request(), _pack(), [], instruction="x"))
        self.assertNotIn("input_tokens", outcome.usage)
        self.assertEqual(outcome.usage["pricing_basis"], "unknown")
        self.assertEqual(outcome.usage["host_turns"], 1)

    def test_reported_usage_is_carried_through(self):
        host = TextInTextOutHost(
            _envelope(),
            usage={"input_tokens": 120, "output_tokens": 30, "pricing_basis": "measured"},
        )
        bridge = _bridge(host)
        outcome = asyncio.run(bridge.execute(_request(), _pack(), [], instruction="x"))
        self.assertEqual(outcome.usage["input_tokens"], 120)
        self.assertEqual(outcome.usage["pricing_basis"], "measured")

    def test_a_transport_failure_is_retryable_not_a_decision(self):
        host = TextInTextOutHost(
            PASError(ErrorCode.PROVIDER_UNAVAILABLE, "host down", retryable=True)
        )
        bridge = _bridge(host)
        with self.assertRaises(PASError) as ctx:
            asyncio.run(bridge.execute(_request(), _pack(), [], instruction="x"))
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_the_host_timeout_is_bounded_by_the_run_deadline(self):
        host = TextInTextOutHost(_envelope())
        bridge = _bridge(host, host_timeout_s=3600.0)
        # A deadline in the past must not let the host run for an hour.
        asyncio.run(
            bridge.execute(_request(deadline="2020-01-01T00:00:00Z"), _pack(), [], instruction="x")
        )
        _prompt, timeout_s = host.prompts[0]
        self.assertLess(timeout_s, 3600.0)
        self.assertGreaterEqual(timeout_s, 1.0)


class BridgeCancellationTests(unittest.TestCase):
    """A cancel request is never recorded as a stopped run."""

    def test_cancel_before_submit_asks_the_host_and_records_the_level(self):
        host = TextInTextOutHost(_envelope(), cancel_level=CANCEL_REQUESTED)
        bridge = _bridge(host)
        cancel = asyncio.Event()
        cancel.set()
        with self.assertRaises(RunCancelled):
            asyncio.run(bridge.execute(_request(), _pack(), [], instruction="x", cancel_event=cancel))
        self.assertEqual(host.cancelled, ["run-bridge-1"])
        self.assertEqual(host.prompts, [])  # never submitted

    def test_cancel_after_the_reply_is_not_a_clean_completion(self):
        host = TextInTextOutHost(_envelope(), cancel_level=CANCEL_CONFIRMED)
        bridge = _bridge(host)

        class SetOnSubmit(asyncio.Event):
            pass

        cancel = asyncio.Event()

        original = host.submit

        async def submit_then_cancel(prompt, *, timeout_s):
            reply = await original(prompt, timeout_s=timeout_s)
            cancel.set()
            return reply

        host.submit = submit_then_cancel  # type: ignore[assignment]
        with self.assertRaises(RunCancelled):
            asyncio.run(bridge.execute(_request(), _pack(), [], instruction="x", cancel_event=cancel))
        self.assertEqual(host.cancelled, ["run-bridge-1"])

    def test_an_uncooperative_host_is_still_reported_as_requested(self):
        host = TextInTextOutHost(_envelope(), cancel_level=CANCEL_UNSUPPORTED)
        bridge = _bridge(host)

        async def broken_cancel(run_key):
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "cannot cancel")

        host.cancel = broken_cancel  # type: ignore[assignment]
        cancel = asyncio.Event()
        cancel.set()
        with self.assertRaises(RunCancelled):
            asyncio.run(bridge.execute(_request(), _pack(), [], instruction="x", cancel_event=cancel))
        # The attempt was still made; the failure is not silently swallowed.


class BridgeSeamTests(unittest.TestCase):
    def test_the_bridge_satisfies_run_executor(self):
        from proactive_sdk import RunExecutor

        self.assertIsInstance(_bridge(TextInTextOutHost(_envelope())), RunExecutor)

    def test_a_text_host_satisfies_the_driver_protocol(self):
        self.assertIsInstance(TextInTextOutHost(_envelope()), HostDriver)

    def test_an_object_without_the_driver_surface_is_rejected(self):
        with self.assertRaises(PASError) as ctx:
            HostBridge(object())
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)

    def test_context_reflects_the_declared_surface(self):
        bridge = _bridge(
            TextInTextOutHost(_envelope()),
            capabilities=frozenset({"calendar.read"}),
            tool_names=("read_evidence",),
            budget=RunBudget(max_model_turns=4),
        )
        ctx = asyncio.run(bridge.context())
        self.assertEqual(ctx.capabilities, frozenset({"calendar.read"}))
        self.assertEqual(ctx.tool_names, ("read_evidence",))
        self.assertEqual(ctx.budget.max_model_turns, 4)

    def test_cancel_levels_are_a_closed_set(self):
        from proactive_sdk import HOST_CANCEL_LEVELS

        self.assertEqual(
            set(HOST_CANCEL_LEVELS),
            {CANCEL_CONFIRMED, CANCEL_REQUESTED, CANCEL_UNSUPPORTED},
        )


if __name__ == "__main__":
    unittest.main()
