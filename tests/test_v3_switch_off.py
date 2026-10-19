"""V3 switches default off: a default agent keeps the pre-v3 path.

The runtime e2e exercises the switches on; these two cases guard the other
half of the contract (plan §8): conversation/goal injection, staged handoff
and memory consolidation must not happen unless enabled, and an explicitly
enabled resume must fail closed when the executor has no stable identity.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # p3_fixtures
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from p3_fixtures import ScriptedModel, T0, make_broker, make_executor  # noqa: E402

from proactive_sdk import (  # noqa: E402
    CoordinatorConfig,
    FakeClock,
    Job,
    MemoryEntry,
    ProactiveAgent,
)


class NeverReadConversation:
    def __init__(self) -> None:
        self.calls = 0

    async def recent_turns(self, since_ms, limit):
        self.calls += 1
        raise AssertionError("conversation was read while conversation_context is off")


def _job(*, handoff: bool = True) -> Job:
    return Job(
        id="switch-off-job",
        mode="task",
        schedule={
            "kind": "interval",
            "anchor": "2025-10-09T00:00:00Z",
            "every_seconds": 1800,
        },
        instruction="Record a note.",
        goal_id="goal-1",
        handoff=handoff,
    )


def _internal_record() -> str:
    return json.dumps(
        {
            "decision": "propose",
            "summary": "internal note",
            "proposals": [
                {
                    "kind": "internal_record",
                    "fact_id": "fact-1",
                    "revision": "rev-1",
                    "body": "note",
                    "evidence_refs": ["memory:m1"],
                    "memory_candidates": [
                        {"content": "candidate", "evidence_refs": ["memory:m1"]}
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )


class SwitchOffTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_agent_uses_one_plain_turn_and_no_v3_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(wall_ms=T0)
            model = ScriptedModel([{"content": _internal_record()}])
            executor = make_executor(model, broker=make_broker(capabilities=("memory.read",)))
            conversation = NeverReadConversation()
            agent = ProactiveAgent(
                state_dir=Path(tmp),
                executor=executor,
                clock=clock,
                timezone="UTC",
                locale="en",
                profile="switch-off",
                conversation=conversation,
            )
            try:
                agent.goals_create("goal-1", title="不应被注入的目标")
                agent.store.remember_memory(
                    MemoryEntry("memory:m1", "用户关注这件事", "user"), now_ms=T0
                )
                agent.jobs_upsert(_job())
                agent.trigger_job("switch-off-job", reason="switch-off")
                report = await agent.tick(max_runs=1, run_hooks=False)
                run = report["runs"][0]

                self.assertEqual(run["outcome"], "proposed")
                self.assertEqual(run["model_turns"], 1, "handoff must stay off by default")
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(conversation.calls, 0)
                kinds = {event["kind"] for event in agent.run_events(run["run_id"])}
                for disabled in ("conversation_context", "goal_context",
                                 "worker_started", "compose_started", "run_resumed"):
                    self.assertNotIn(disabled, kinds)
                self.assertEqual(
                    agent.store.db.execute(
                        "SELECT COUNT(*) AS n FROM memory_candidates"
                    ).fetchone()["n"],
                    0,
                    "consolidation candidates must not be collected while off",
                )
                self.assertEqual(
                    agent.store.db.execute(
                        "SELECT COUNT(*) AS n FROM memory_entries"
                        " WHERE entry_json LIKE '%memory_consolidation%'"
                    ).fetchone()["n"],
                    0,
                )
            finally:
                await agent.close()

    async def test_enabled_resume_without_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(wall_ms=T0)
            model = ScriptedModel([])
            executor = make_executor(model, broker=make_broker(capabilities=("memory.read",)))
            agent = ProactiveAgent(
                state_dir=Path(tmp),
                executor=executor,
                clock=clock,
                timezone="UTC",
                locale="en",
                profile="resume-fail-closed",
                coordinator_config=CoordinatorConfig(handoff=True, run_resume=True),
            )
            try:
                agent.store.remember_memory(
                    MemoryEntry("memory:m1", "用户关注这件事", "user"), now_ms=T0
                )
                agent.jobs_upsert(_job())
                agent.trigger_job("switch-off-job", reason="resume")
                report = await agent.tick(max_runs=1, run_hooks=False)
                run = report["runs"][0]
                stored = agent.runs_list()[0]
                self.assertEqual(run["outcome"], "failed")
                self.assertEqual(stored["error_class"], "invalid_config")
                self.assertEqual(model.calls, [])
            finally:
                await agent.close()


if __name__ == "__main__":
    unittest.main()
