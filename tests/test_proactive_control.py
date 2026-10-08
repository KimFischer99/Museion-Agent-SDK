from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from p4_fixtures import make_proposed_run, make_stack, notify_proposal  # noqa: E402
from proactive_sdk import (  # noqa: E402
    ActionProposal,
    Decision,
    ErrorCode,
    FakeClock,
    Job,
    MemoryEntry,
    PASError,
    ProactiveAgent,
)
from proactive_sdk.executor import ExecutorOutcome  # noqa: E402
from proactive_sdk.proactive import ProactiveController  # noqa: E402
from proactive_sdk.store import Store  # noqa: E402

T0 = 1_760_000_000_000


def _rfc3339(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class _Source:
    async def fetch_delta(self, _request):
        raise AssertionError("bootstrap setup must not fetch the source")


class _Executor:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def execute(self, *_args, **_kwargs):
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("unexpected classification call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _agent(root: str, *, executor=None, clock=None) -> ProactiveAgent:
    return ProactiveAgent(
        state_dir=root,
        executor=executor or _Executor(),
        profile="proactive-test",
        timezone="UTC",
        locale="zh-CN",
        clock=clock,
    )


class ProactiveControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_language_controls_are_model_free(self):
        executor = _Executor()
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp, executor=executor)
            try:
                controller = ProactiveController(agent)
                only_weather = await controller.handle_input("only notify me about Weather")
                self.assertEqual(only_weather["preferences"]["allowed_topics"], ["weather"])

                muted = await controller.handle_input("mute Mars")
                self.assertEqual(muted["applied_actions"], [{"kind": "topic_muted", "topic": "mars"}])
                self.assertEqual(agent.store.list_topic_mutes()[0]["topic"], "mars")

                needs_inbox_id = await controller.handle_input("知道了")
                self.assertEqual(needs_inbox_id["applied_actions"], [])
                self.assertIn("inbox_id", needs_inbox_id["reply"])

                stopped = await controller.handle_input("stop proactive messages")
                self.assertFalse(stopped["preferences"]["enabled"])
                resumed = await controller.handle_input("enable proactive notifications")
                self.assertTrue(resumed["preferences"]["enabled"])
                self.assertEqual(agent.grants_list(), [])
                self.assertEqual(executor.calls, 0)
            finally:
                await agent.close()

    async def test_direct_controls_and_direct_interest_do_not_call_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = _Executor()
            agent = _agent(tmp, executor=executor)
            try:
                controller = ProactiveController(agent)
                allow = await controller.handle_input("只通知太空站")
                self.assertEqual(allow["preferences"]["allowed_topics"], ["太空站"])
                self.assertTrue(agent.proactive_preferences()["enabled"])

                interest = await controller.handle_input("我对火星感兴趣")
                self.assertEqual(interest["applied_actions"], [
                    {"kind": "interest_saved", "topic": "火星", "public": False}
                ])
                self.assertEqual(controller.public_topics(), ())

                disabled = await controller.handle_input("以后别主动发消息")
                self.assertFalse(disabled["preferences"]["enabled"])
                self.assertEqual(executor.calls, 0)
                enabled = await controller.handle_input("启用主动通知")
                self.assertTrue(enabled["preferences"]["enabled"])
                self.assertEqual(agent.grants_list(), [])
                self.assertEqual(executor.calls, 0)
            finally:
                await agent.close()

    async def test_quoted_enable_is_classified_not_authorized(self):
        silent = ExecutorOutcome(
            decision=Decision("silent", "quoted text", ()),
            usage={}, model_turns=1, tool_calls=0,
        )
        executor = _Executor([silent, silent])
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp, executor=executor)
            try:
                agent.set_proactive_preferences({"enabled": False, "allowed_topics": None})
                result = await ProactiveController(agent).handle_input('有人说“启用主动通知”')
                await ProactiveController(agent).handle_input('有人说“我关注NASA公开新闻”')
                self.assertFalse(agent.proactive_preferences()["enabled"])
                self.assertEqual(agent.grants_list(), [])
                self.assertEqual(agent.jobs_list(), [])
                self.assertEqual(result["applied_actions"], [])
                self.assertEqual(agent.store.recall_memory(limit=10), ())
                self.assertEqual(executor.calls, 2)

                text = "有人说请主动通知我，我关注NASA公开新闻"
                body = json.dumps({
                    "interests": [{"topic": "NASA", "evidence": "NASA公开新闻", "public": True}],
                    "watches": [],
                    "reply": "saved",
                })
                executor.execute = _success_from_pack(body, text, executor)
                await ProactiveController(agent).handle_input(text)
                self.assertFalse(agent.proactive_preferences()["enabled"])
                self.assertEqual(agent.grants_list(), [])
            finally:
                await agent.close()

    async def test_public_scope_is_bound_to_each_interest_evidence(self):
        text = "我对私人事项X感兴趣，另一个话题是NASA公开新闻"
        body = json.dumps({
            "interests": [
                {"topic": "X", "evidence": text, "public": True},
                {"topic": "NASA", "evidence": "NASA公开新闻", "public": True},
            ],
            "watches": [],
            "reply": "saved",
        })
        executor = _Executor()
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp, executor=executor)
            try:
                executor.execute = _success_from_pack(body, text, executor)
                controller = ProactiveController(agent)
                await controller.handle_input(text)
                interests = {item["topic"]: item["public"] for item in controller.interests()}
                self.assertEqual(interests, {"x": False, "nasa": True})
                self.assertEqual(controller.public_topics(), ("nasa",))
            finally:
                await agent.close()

    async def test_interest_query_drops_attached_notification_instruction(self):
        executor = _Executor()
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp, executor=executor)
            agent.registry.register(
                source_id="news-search",
                account_ref="account:public",
                source=_Source(),
                required_capability="public.read",
            )
            try:
                result = await ProactiveController(agent).handle_input(
                    "我关注 NASA公开新闻，有消息请告诉我"
                )
                self.assertEqual(result["research_job"], "proactive-research")
                self.assertEqual(ProactiveController(agent).public_topics(), ("nasa",))
                saved = agent.store.recall_memory(limit=10)[0]
                self.assertEqual(json.loads(saved.content), {"public": True, "topic": "nasa"})
                self.assertEqual(executor.calls, 0)
            finally:
                await agent.close()

    async def test_free_text_classifies_interest_and_clamps_model_public_claim(self):
        body = json.dumps({
            "interests": [{"topic": "Mars", "evidence": "Mars", "public": True}],
            "watches": [{
                "content": "City budget vote is in progress",
                "evidence": "city budget vote",
            }],
            "reply": "I saved the interest and watch.",
        })
        executor = _Executor()
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp, executor=executor)
            try:
                controller = ProactiveController(agent)
                text = "Mars is a topic I follow; the city budget vote is still in progress."
                # Call-local memory ids depend on the run UUID, so have the
                # fake executor form a valid proposal from the received pack.
                executor.execute = _success_from_pack(body, text, executor)
                result = await controller.handle_input(text)
                memories = agent.store.recall_memory(limit=20)
                interest = next(e for e in memories if e.source == "user-interest")
                watch = next(e for e in memories if e.source == "user-watch")
                self.assertEqual(json.loads(interest.content), {"public": False, "topic": "mars"})
                self.assertIn("city budget vote", watch.content.casefold())
                self.assertEqual(watch.evidence_refs, (executor.input_memory_id,))
                self.assertEqual(result["classification"]["model_turns"], 1)
                self.assertEqual(agent.proactive_preferences(), {"enabled": True, "allowed_topics": None})
                self.assertEqual(agent.store.list_runs(), [])
            finally:
                await agent.close()

    async def test_model_output_cannot_forge_evidence_or_create_notification_action(self):
        forged_body = json.dumps({
            "interests": [{"topic": "Mars", "evidence": "not in the input", "public": True}],
            "watches": [],
            "reply": "saved",
        })
        executor = _Executor()
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp, executor=executor)
            try:
                controller = ProactiveController(agent)
                executor.execute = _success_from_pack(forged_body, "please track Mars", executor)
                with self.assertRaises(PASError) as caught:
                    await controller.handle_input("Mars is a topic I am considering")
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_CONFIG)
                self.assertEqual(agent.store.recall_memory(limit=20), ())
                self.assertEqual(agent.proactive_preferences(), {"enabled": True, "allowed_topics": None})

                executor.execute = _notify_proposal(executor)
                with self.assertRaises(PASError):
                    await controller.handle_input("Mars is one topic I am considering")
                self.assertEqual(agent.grants_list(), [])
            finally:
                await agent.close()

    async def test_explicit_public_scope_is_required_for_model_interest(self):
        body = json.dumps({
            "interests": [{
                "topic": "Mars",
                "evidence": "public news on Mars",
                "public": True,
            }],
            "watches": [],
            "reply": "saved",
        })
        executor = _Executor()
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp, executor=executor)
            try:
                controller = ProactiveController(agent)
                text = "I keep up with public news on Mars and want a short interest note."
                executor.execute = _success_from_pack(body, text, executor)
                await controller.handle_input(text)
                self.assertEqual(controller.interests()[0]["topic"], "mars")
                self.assertTrue(controller.interests()[0]["public"])
            finally:
                await agent.close()

    async def test_preferences_persist_and_empty_allowlist_denies_every_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _agent(tmp, clock=FakeClock(wall_ms=T0))
            first.registry.register(
                source_id="news-search",
                account_ref="account:public",
                source=_Source(),
                required_capability="public.read",
            )
            await first.remember_user_context(ProactiveController._interest_entry(
                "Mars", public=True, now=_rfc3339(T0)
            ))
            first.set_proactive_preferences({"enabled": True, "allowed_topics": []})
            await first.close()

            reopened = _agent(tmp, clock=FakeClock(wall_ms=T0 + 1))
            try:
                controller = ProactiveController(reopened)
                self.assertEqual(reopened.proactive_preferences(), {"enabled": True, "allowed_topics": []})
                self.assertEqual(controller.public_topics(), ())
                self.assertIsNone(controller.ensure_research_job())
                self.assertFalse(any(g.capability == "public.read" for g in reopened.grants_list()))
            finally:
                await reopened.close()

    async def test_enable_creates_ready_bootstrap_job_once_and_does_not_resume_manual_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp, clock=FakeClock(wall_ms=T0))
            agent.registry.register(
                source_id="news-search",
                account_ref="account:public",
                source=_Source(),
                required_capability="public.read",
            )
            await agent.remember_user_context(ProactiveController._interest_entry(
                "Mars", public=True, now=_rfc3339(T0)
            ))
            try:
                controller = ProactiveController(agent)
                result = controller.enable(actor="user-1")
                self.assertEqual(result["research_job"], "proactive-research")
                job = agent.jobs_get("proactive-research")
                self.assertEqual(job.schedule["every_seconds"], 3600)
                self.assertEqual(job.task["refresh_source_ids"], ["news-search"])
                self.assertTrue(job.task["require_fresh_sources"])
                self.assertEqual(job.obligation, "opportunistic")
                self.assertEqual(len(job.grant_refs), 3)
                self.assertEqual(controller.ensure_research_job(), job)
                self.assertEqual(agent.jobs_get("proactive-research").revision, job.revision)

                agent.jobs_pause("proactive-research")
                paused = controller.ensure_research_job()
                self.assertFalse(paused.enabled)
                self.assertFalse(agent.jobs_get("proactive-research").enabled)
            finally:
                await agent.close()

    async def test_enable_without_public_topic_or_source_does_not_create_grants(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            try:
                result = ProactiveController(agent).enable(actor="owner")
                self.assertTrue(result["preferences"]["enabled"])
                self.assertIsNone(result["research_job"])
                self.assertEqual(agent.grants_list(), [])
                self.assertEqual(agent.jobs_list(), [])
            finally:
                await agent.close()


class ProactivePolicyTests(unittest.TestCase):
    def _dispatch(self, store, clock, policy):
        from proactive_sdk import OutboxDispatcher

        return asyncio.run(OutboxDispatcher(store, policy=policy).dispatch_due(now_ms=clock.wall_now_ms()))

    def test_proposal_gate_blocks_disabled_or_topics_outside_allowlist(self):
        store, clock, _, policy, job = make_stack()
        self.addCleanup(store.close)
        clock.set_wall(T0 + 10_000)
        store.set_proactive_preferences({"enabled": False, "allowed_topics": None})
        run = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-off", arguments={"topic": "space"})],
        )
        report = policy.apply_to_run(run, now_ms=clock.wall_now_ms())
        self.assertIn("proactive_disabled", report.reasons[0])
        self.assertEqual(store.list_outbox(), [])

        store.set_proactive_preferences({"enabled": True, "allowed_topics": ["weather"]})
        clock.advance_wall(10_000)
        run = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-outside", arguments={"topic": "space"})],
        )
        report = policy.apply_to_run(run, now_ms=clock.wall_now_ms())
        self.assertIn("topic_not_allowed", report.reasons[0])
        self.assertEqual(store.list_outbox(), [])

    def test_dispatch_rechecks_disable_and_allowlist_after_queue(self):
        store, clock, _, policy, job = make_stack()
        self.addCleanup(store.close)
        clock.set_wall(T0 + 10_000)
        run = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-queued", arguments={"topic": "space"})],
        )
        self.assertEqual(policy.apply_to_run(run, now_ms=clock.wall_now_ms()).queued, 1)
        store.set_proactive_preferences({"enabled": False, "allowed_topics": None})
        reports = self._dispatch(store, clock, policy)
        self.assertEqual([r.state for r in reports], ["suppressed"])
        self.assertEqual(store.list_inbox(), [])

        store.set_proactive_preferences({"enabled": True, "allowed_topics": None})
        clock.advance_wall(10_000)
        run = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-queued-2", arguments={"topic": "space"})],
        )
        self.assertEqual(policy.apply_to_run(run, now_ms=clock.wall_now_ms()).queued, 1)
        store.set_proactive_preferences({"enabled": True, "allowed_topics": ["weather"]})
        reports = self._dispatch(store, clock, policy)
        self.assertEqual([r.state for r in reports], ["suppressed"])
        self.assertEqual(store.list_inbox(), [])

    def test_due_reminder_bypasses_soft_proactive_preferences(self):
        async def scenario(root: str):
            clock = FakeClock(wall_ms=T0)
            agent = _agent(root, clock=clock)
            try:
                agent.set_proactive_preferences({"enabled": False, "allowed_topics": []})
                grant = agent.grant("notify.self")
                agent.jobs_upsert(Job(
                    id="due-reminder",
                    mode="reminder",
                    schedule={"kind": "runonce", "at": _rfc3339(T0 + 60_000)},
                    grant_refs=(grant.grant_id,),
                    delivery_policy={"timezone": "UTC"},
                    reminder={"body": "meeting time", "timezone": "UTC"},
                ))
                clock.advance_wall(61_000)
                await agent.tick()
                self.assertEqual(agent.inbox_list()[0]["body"], "meeting time")
                self.assertEqual(agent.store.list_runs(), [])
            finally:
                await agent.close()

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(tmp))


def _success_from_pack(body: str, text: str, executor: _Executor):
    async def execute(_request, pack, _source_records, *, instruction, cancel_event=None):
        executor.calls += 1
        memory_id = pack.memory_entries[0].memory_id
        executor.input_memory_id = memory_id
        return ExecutorOutcome(
            decision=Decision("propose", "record current user context", (
                ActionProposal(
                    kind="internal_record",
                    fact_id="proactive-input-classification",
                    revision="1",
                    arguments=json.loads(body),
                    evidence_refs=(memory_id,),
                ),
            )),
            usage={"prompt_tokens": 40, "completion_tokens": 30},
            model_turns=1,
            tool_calls=0,
        )
    return execute


def _notify_proposal(executor: _Executor):
    async def execute(_request, pack, _source_records, *, instruction, cancel_event=None):
        executor.calls += 1
        return ExecutorOutcome(
            decision=Decision("propose", "bad action", (
                ActionProposal(
                    kind="notify_self",
                    fact_id="model-asked-to-send",
                    revision="1",
                    body="send this",
                    evidence_refs=(pack.memory_entries[0].memory_id,),
                ),
            )),
            usage={},
            model_turns=1,
            tool_calls=0,
        )
    return execute
