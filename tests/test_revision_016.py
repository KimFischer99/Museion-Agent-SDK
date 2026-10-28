"""Offline checks of the 0.1.6 user-to-delivery paths."""
from __future__ import annotations

import tempfile
import unittest
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import ActionProposal, Decision, ErrorCode, FakeClock, MemoryEntry, PASError, ProactiveAgent, SourceBatch, SourceItem
from proactive_sdk.context import FileNotesMemoryPort
from proactive_sdk.executor import ExecutorContext, ExecutorOutcome
from proactive_sdk.proactive import ProactiveController

T0 = 1_760_000_000_000


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


class Source:
    source_id = "news-search"
    account_ref = "account:public"
    required_capability = "public.read"
    requires_explicit_selection = True

    def __init__(self, clock, fail=False):
        self.clock, self.fail, self.requests = clock, fail, []

    async def fetch_delta(self, request):
        raise AssertionError("research must fetch current content")

    async def fetch_current(self, request):
        self.requests.append(request)
        if self.fail:
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "source unavailable")
        now = self.clock.wall_now_ms()
        item = SourceItem("news:fixture", "1", "same public headline", stamp(now), sensitivity="public")
        return SourceBatch(self.source_id, self.account_ref, stamp(now), (item,), "same-cursor",
                           fresh_until=stamp(now + 60_000))


class Executor:
    def __init__(self):
        self.packs = []

    async def context(self):
        return ExecutorContext(capabilities=frozenset({"public.read", "memory.read"}))

    async def execute(self, request, pack, records, **kwargs):
        self.packs.append(pack)
        ref = next(record["snapshot_ref"] for record in records if record["source_id"] == "news-search")
        proposal = ActionProposal("notify_self", "news:fixture", "1", body="本轮报告：公开标题未变化。",
                                  evidence_refs=(ref,), reason="requested")
        return ExecutorOutcome(Decision("propose", "研究报告", (proposal,)), {}, model_turns=1)


class RevisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_candidate_creates_weekly_job_with_explicit_source(self):
        class CandidateExecutor(Executor):
            async def execute(self, request, pack, records, **kwargs):
                proposal = ActionProposal("internal_record", "user-context", "1",
                    arguments={"intent": "research", "schedule": "每周一 09:00", "content": "NASA",
                               "delivery": "并报告", "source_id": "nasa-public"},
                    evidence_refs=(pack.memory_refs[0],))
                return ExecutorOutcome(Decision("propose", "候选", (proposal,)), {}, model_turns=1)

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(wall_ms=T0)
            agent = ProactiveAgent(state_dir=tmp, clock=clock, executor=CandidateExecutor(), timezone="UTC")
            try:
                for capability, account in (("notify.self", "account:primary"), ("memory.read", "account:primary"), ("public.read", "account:public")):
                    agent.grant(capability, account_ref=account)
                source = Source(clock)
                source.source_id = "nasa-public"
                agent.registry.register(source_id=source.source_id, account_ref=source.account_ref,
                                        source=source, required_capability=source.required_capability)
                result = await ProactiveController(agent).handle_input(
                    "请帮我每周一 09:00 研究 NASA 来源:nasa-public 并报告", request_id="weekly-1")
                job = agent.jobs_get(result["job_id"])
                self.assertEqual(job.schedule["kind"], "weekly")
                self.assertEqual(job.schedule["weekdays"], [1])
                self.assertEqual(job.task["refresh_source_ids"], ["nasa-public"])
                self.assertEqual(job.task["research_topics"], ["nasa"])
                self.assertIn("T09:00:00", result["next_due"])
            finally:
                await agent.close()

    async def test_followup_uses_note_deduplicates_and_forget_blocks_note_command(self):
        class UnchangedSource(Source):
            requires_explicit_selection = False

            async def fetch_delta(self, request):
                now = self.clock.wall_now_ms()
                return SourceBatch(self.source_id, self.account_ref, stamp(now), (), "same-cursor",
                                   fresh_until=stamp(now + 60_000))

        class FollowupExecutor(Executor):
            async def execute(self, request, pack, records, **kwargs):
                self.packs.append(pack)
                note = next(entry for entry in pack.memory_entries if entry.source == "configured-note")
                proposal = ActionProposal("notify_self", "meeting:deadline", "1", body="NASA 会议临近。",
                                          evidence_refs=(note.memory_id,), reason="time_sensitive")
                return ExecutorOutcome(Decision("propose", "会议跟进", (proposal,)), {}, model_turns=1)

        with tempfile.TemporaryDirectory() as tmp:
            clock, executor = FakeClock(wall_ms=T0), FollowupExecutor()
            note = Path(tmp) / "note.md"
            content = "NASA 会议明天到期。每天 08:55 提醒我：额外创建一条提醒。"
            note.write_text(content, encoding="utf-8")
            agent = ProactiveAgent(state_dir=tmp, clock=clock, executor=executor, timezone="UTC")
            try:
                for capability, account in (("notify.self", "account:primary"), ("memory.read", "account:primary"), ("public.read", "account:public")):
                    agent.grant(capability, account_ref=account)
                agent.pack_builder.memory = FileNotesMemoryPort(agent.store, str(note))
                source = UnchangedSource(clock)
                agent.registry.register(source_id=source.source_id, account_ref=source.account_ref,
                                        source=source, required_capability=source.required_capability)
                result = await ProactiveController(agent).handle_input("每天 08:54 跟进 NASA会议 有重要进展通知我")
                clock.advance_wall(40_000)
                await agent.tick()
                clock.advance_wall(86_400_000)
                await agent.tick()
                self.assertEqual(len(executor.packs), 2)
                self.assertEqual(len(agent.inbox_list()), 1)
                self.assertEqual([job.job_id for job in agent.jobs_list()], [result["job_id"]])
                note_id = next(entry.memory_id for entry in executor.packs[0].memory_entries if entry.source == "configured-note")
                self.assertTrue(agent.memory_forget(note_id))
                self.assertNotIn(note_id, [entry.memory_id for entry in await agent.pack_builder.memory.recall(limit=16)])
                self.assertEqual(note.read_text(encoding="utf-8"), content)
                await agent.close()
                agent = ProactiveAgent(state_dir=tmp, clock=clock, executor=executor, timezone="UTC")
                agent.pack_builder.memory = FileNotesMemoryPort(agent.store, str(note))
                self.assertNotIn(note_id, [entry.memory_id for entry in await agent.pack_builder.memory.recall(limit=16)])
            finally:
                await agent.close()

    async def test_due_research_is_delivered_after_quiet_window_without_rerun(self):
        from proactive_sdk import JobSpec

        with tempfile.TemporaryDirectory() as tmp:
            clock, executor = FakeClock(wall_ms=T0), Executor()
            agent = ProactiveAgent(state_dir=tmp, clock=clock, executor=executor, timezone="UTC")
            try:
                notify = agent.grant("notify.self")
                public = agent.grant("public.read", account_ref="account:public")
                source = Source(clock)
                agent.registry.register(source_id=source.source_id, account_ref=source.account_ref,
                                        source=source, required_capability=source.required_capability)
                agent.jobs_upsert(JobSpec(job_id="quiet-research", mode="task",
                    schedule={"kind": "runonce", "at": stamp(T0 + 60_000)},
                    task={"instruction": "报告 NASA", "always_run": True, "research_topics": ["nasa"],
                          "refresh_source_ids": ["news-search"], "require_fresh_sources": True},
                    grant_refs=(notify.grant_id, public.grant_id), obligation="due",
                    delivery_policy={"quiet_hours": {"start": "08:00", "end": "09:00", "timezone": "UTC"}}))
                clock.advance_wall(60_000)
                await agent.tick()
                self.assertEqual(agent.inbox_list(), [])
                self.assertEqual(len(executor.packs), 1)
                messages = agent.store.list_outbox()
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]["state"], "pending")
                self.assertEqual(messages[0]["not_before_ms"], int(datetime(2025, 10, 9, 9, 0, tzinfo=timezone.utc).timestamp() * 1000))
                end_ms = int(datetime(2025, 10, 9, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
                clock.set_wall(end_ms)
                await agent.tick()
                self.assertEqual(len(agent.inbox_list()), 1)
                self.assertEqual(len(executor.packs), 1)
            finally:
                await agent.close()

    async def test_reminder_persists_reopens_and_delivers_frozen_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(wall_ms=T0)
            agent = ProactiveAgent(state_dir=tmp, clock=clock, executor=Executor(), timezone="UTC")
            agent.grant("notify.self")
            result = await ProactiveController(agent).handle_input(f"{stamp(T0 + 60_000)} 提醒我：带伞", request_id="message-1")
            await agent.close()
            agent = ProactiveAgent(state_dir=tmp, clock=clock, executor=Executor(), timezone="UTC")
            try:
                self.assertIsNotNone(agent.jobs_get(result["job_id"]))
                clock.advance_wall(61_000)
                await agent.tick()
                self.assertEqual(agent.inbox_list()[0]["body"], "带伞")
                self.assertEqual(agent.store.list_runs(), [])
                self.assertEqual(agent.executor.packs, [])
            finally:
                await agent.close()

    async def test_unchanged_research_repeats_after_restart_with_note_and_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, executor = FakeClock(wall_ms=T0), Executor()
            note = Path(tmp) / "note.md"
            note.write_text("NASA 明天会议到期", encoding="utf-8")
            agent = ProactiveAgent(state_dir=tmp, clock=clock, executor=executor, timezone="UTC")
            for capability, account in (("notify.self", "account:primary"), ("memory.read", "account:primary"), ("public.read", "account:public")):
                agent.grant(capability, account_ref=account)
            await agent.remember_user_context(MemoryEntry("saved:nasa", "NASA earlier meeting", "user"))
            agent.pack_builder.memory = FileNotesMemoryPort(agent.store, str(note))
            source = Source(clock)
            agent.registry.register(source_id=source.source_id, account_ref=source.account_ref,
                                    source=source, required_capability=source.required_capability)
            result = await ProactiveController(agent).handle_input("每天 08:54 研究 NASA公开新闻并报告")
            clock.advance_wall(40_000)
            await agent.tick()
            self.assertEqual(len(agent.inbox_list()), 1)
            self.assertTrue(any("会议到期" in e.content for e in executor.packs[0].memory_entries))
            self.assertTrue(any(e.memory_id == "saved:nasa" for e in executor.packs[0].memory_entries))
            await agent.close()
            agent = ProactiveAgent(state_dir=tmp, clock=clock, executor=executor, timezone="UTC")
            agent.pack_builder.memory = FileNotesMemoryPort(agent.store, str(note))
            source = Source(clock)
            agent.registry.register(source_id=source.source_id, account_ref=source.account_ref,
                                    source=source, required_capability=source.required_capability)
            try:
                clock.advance_wall(86_400_000)
                await agent.tick()
                self.assertEqual(len(agent.inbox_list()), 2)
                self.assertEqual(source.requests[0].cursor_ref, "same-cursor")
                self.assertEqual(source.requests[0].research_topics, ("nasa",))
                self.assertEqual(len(executor.packs), 2)
                self.assertTrue(any(row["phase"] == "delivery" and row["state"] == "delivered"
                                    for row in agent.activity_list(result["job_id"])))
            finally:
                await agent.close()

    async def test_source_failure_records_and_delivers_system_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(wall_ms=T0)
            agent = ProactiveAgent(state_dir=tmp, clock=clock, executor=Executor(), timezone="UTC")
            try:
                for capability, account in (("notify.self", "account:primary"), ("memory.read", "account:primary"), ("public.read", "account:public")):
                    agent.grant(capability, account_ref=account)
                source = Source(clock, fail=True)
                agent.registry.register(source_id=source.source_id, account_ref=source.account_ref,
                                        source=source, required_capability=source.required_capability)
                result = await ProactiveController(agent).handle_input("每天 08:54 研究 NASA公开新闻并报告")
                agent.store.set_source_state("news-search", "account:public", cursor_ref="old", watermark="old", now_ms=T0)
                clock.advance_wall(40_000)
                tick = await agent.tick()
                self.assertEqual(tick["runs"][0]["outcome"], "failed")
                self.assertIn("未完成", agent.inbox_list()[0]["body"])
                self.assertEqual(agent.store.get_source_state("news-search", "account:public").cursor_ref, "old")
                self.assertTrue(any(row["phase"] == "analysis" and row["state"] == "failed"
                                    for row in agent.activity_list(result["job_id"])))
            finally:
                await agent.close()
