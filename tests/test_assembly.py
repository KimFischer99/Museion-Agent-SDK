"""SPEC §22.1 item 9: taking the SDK and using it.

"Out of the box" is a claim about a stranger's first ten minutes, so these
tests are written from that angle: does `models=`/`model=` alone produce a
working agent, does a plain function become a usable tool, and does the
capability check still mean something once everything is assembled for you?

That last one is the reason this file exists. Convenience wiring is exactly
where an authorization check goes quietly vacuous: if the auto-assembled
broker were handed every capability its tools declare, then
`required_capability` would look enforced while enforcing nothing. The
tests below pin the opposite — a fresh agent *cannot* call its own memory
tool until the user grants it, and can the moment they do.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from proactive_sdk import (  # noqa: E402
    CAPABILITY_MEMORY_READ,
    CAPABILITY_STATE_READ,
    NOTIFY_SELF_CAPABILITY,
    BrokerCallContext,
    ErrorCode,
    FakeClock,
    Job,
    LocalToolBroker,
    PASError,
    ProactiveAgent,
    ToolCallAttempt,
    builtin_tools,
    register_tools,
    register_tools as _register_tools,
    tool,
)

T0 = 1_760_000_000_000


class OfflineModel:
    async def generate(self, request):
        from proactive_sdk import ModelResponse

        return ModelResponse(
            content=json.dumps({"decision": "silent", "summary": "n", "proposals": []}),
            usage={"turns": 1},
        )


class ToolDecoratorTests(unittest.TestCase):
    def test_a_plain_function_becomes_a_declared_tool(self):
        @tool
        def lookup(query: str, limit: int = 5) -> list[str]:
            """Find things.

            A longer explanation that is not part of the one-line description.
            """
            return [query] * limit

        self.assertEqual(lookup.spec.name, "lookup")
        self.assertEqual(lookup.spec.description, "Find things.")
        self.assertEqual(
            lookup.spec.parameters,
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )
        self.assertTrue(lookup.spec.read_only)

    def test_it_calls_through_and_serialises_the_result(self):
        @tool
        def structured(a: int) -> dict:
            """Return structured data."""
            return {"a": a}

        self.assertEqual(json.loads(asyncio.run(structured._handler({"a": 1}))), {"a": 1})

    def test_an_async_function_is_awaited(self):
        @tool
        async def slow(x: str) -> str:
            """Async."""
            await asyncio.sleep(0)
            return f"got {x}"

        self.assertEqual(asyncio.run(slow._handler({"x": "y"})), "got y")

    def test_a_missing_type_hint_is_an_error_not_a_guess(self):
        with self.assertRaises(PASError) as ctx:

            @tool
            def unhinted(x):
                """No hint."""
                return x

        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)
        self.assertIn("type hint", ctx.exception.safe_message)

    def test_an_unmappable_type_is_an_error(self):
        with self.assertRaises(PASError) as ctx:

            @tool
            def exotic(x: complex) -> str:
                """Cannot be expressed."""
                return str(x)

        self.assertIn("cannot map", ctx.exception.safe_message)

    def test_varargs_are_refused_because_the_schema_could_not_be_exact(self):
        with self.assertRaises(PASError):
            tool(lambda *args: "x", name="variadic")

    def test_optional_parameters_become_optional_properties(self):
        @tool
        def maybe(required: str, optional: str | None = None) -> str:
            """Mixed."""
            return required

        self.assertEqual(maybe.spec.parameters["required"], ["required"])
        self.assertEqual(maybe.spec.parameters["properties"]["optional"], {"type": "string"})

    def test_a_capability_is_carried_onto_the_spec(self):
        @tool(capability="calendar.read")
        def guarded(x: str) -> str:
            """Guarded."""
            return x

        self.assertEqual(guarded.spec.required_capability, "calendar.read")


class AssemblyTests(unittest.TestCase):
    def _agent(self, tmp: str, **kw) -> ProactiveAgent:
        return ProactiveAgent(
            state_dir=Path(tmp) / "s",
            model=OfflineModel(),
            clock=FakeClock(wall_ms=T0),
            timezone="UTC",
            profile="assembly",
            **kw,
        )

    def test_a_model_alone_produces_a_working_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                self.assertEqual(type(agent.executor).__name__, "ToolLoopExecutor")
                self.assertEqual(
                    agent.tool_names,
                    ("current_time", "list_recent_activity", "recall_memory"),
                )
                # and it really runs
                report = asyncio.run(agent.tick())
                self.assertEqual(report["runs"], [])
            finally:
                asyncio.run(agent.close())

    def test_passing_both_a_model_and_an_executor_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PASError) as ctx:
                self._agent(tmp, executor=object())
            self.assertIn("not both", ctx.exception.safe_message)

    def test_passing_neither_explains_the_two_ways(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PASError) as ctx:
                ProactiveAgent(state_dir=Path(tmp) / "s", clock=FakeClock(wall_ms=T0))
            message = ctx.exception.safe_message
            self.assertIn("model=", message)
            self.assertIn("executor=", message)

    def test_builtin_tools_can_be_turned_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp, include_builtin_tools=False)
            try:
                self.assertEqual(agent.tool_names, ())
            finally:
                asyncio.run(agent.close())

    def test_a_users_own_tool_is_registered_alongside_the_builtins(self):
        @tool
        def mine(x: str) -> str:
            """Mine."""
            return x

        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp, tools=(mine,))
            try:
                self.assertIn("mine", agent.tool_names)
                self.assertIn("current_time", agent.tool_names)
            finally:
                asyncio.run(agent.close())

    def test_an_explicit_executor_still_wins_the_old_way(self):
        """The pre-v0.1.2 call shape must keep working unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            from proactive_sdk import HostBridge

            class Host:
                async def capabilities(self):
                    return {}

                async def submit(self, prompt, *, timeout_s):
                    from proactive_sdk import HostReply

                    return HostReply(text=json.dumps(
                        {"decision": "silent", "summary": "n", "proposals": []}))

                async def cancel(self, run_key):
                    return "unsupported"

                async def close(self):
                    return None

            agent = ProactiveAgent(
                state_dir=Path(tmp) / "s", executor=HostBridge(Host()),
                clock=FakeClock(wall_ms=T0), profile="assembly",
            )
            try:
                self.assertIsInstance(agent.executor, HostBridge)
                self.assertFalse(hasattr(agent, "tool_names"))
            finally:
                asyncio.run(agent.close())


class CapabilitiesStayMeaningfulTests(unittest.TestCase):
    """Convenience must not turn `required_capability` into decoration."""

    def _agent(self, tmp: str) -> ProactiveAgent:
        return ProactiveAgent(
            state_dir=Path(tmp) / "s", model=OfflineModel(),
            clock=FakeClock(wall_ms=T0), timezone="UTC", profile="caps",
        )

    def test_a_fresh_agent_holds_no_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                self.assertEqual(agent.executor.broker.capability_snapshot(), frozenset())
            finally:
                asyncio.run(agent.close())

    def test_the_snapshot_is_live_rather_than_frozen_at_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                self.assertEqual(asyncio.run(agent.executor.context()).capabilities, frozenset())
                agent.grant(CAPABILITY_MEMORY_READ)
                # Read fresh on every run: a grant added later is not ignored.
                self.assertEqual(
                    asyncio.run(agent.executor.context()).capabilities,
                    frozenset({CAPABILITY_MEMORY_READ}),
                )
            finally:
                asyncio.run(agent.close())

    def test_a_tool_needing_a_grant_is_refused_without_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                broker = agent.executor.broker
                result = asyncio.run(
                    broker.call(
                        ToolCallAttempt(call_id="c1", name="recall_memory", arguments={}),
                        context=BrokerCallContext(
                            run_id="r1", fence=1,
                            allowlist=frozenset(broker.tool_names()),
                            capabilities=broker.capability_snapshot(),
                            tool_calls_remaining=3,
                        ),
                    )
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, ErrorCode.PERMISSION_DENIED.value)
                self.assertIn(CAPABILITY_MEMORY_READ, result.safe_error)
            finally:
                asyncio.run(agent.close())

    def test_the_same_tool_works_once_the_user_grants_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                agent.grant(CAPABILITY_MEMORY_READ)
                broker = agent.executor.broker
                result = asyncio.run(
                    broker.call(
                        ToolCallAttempt(call_id="c1", name="recall_memory", arguments={}),
                        context=BrokerCallContext(
                            run_id="r1", fence=1,
                            allowlist=frozenset(broker.tool_names()),
                            capabilities=broker.capability_snapshot(),
                            tool_calls_remaining=3,
                        ),
                    )
                )
                self.assertTrue(result.ok, result.safe_error)
                self.assertEqual(json.loads(result.output)["count"], 0)
            finally:
                asyncio.run(agent.close())

    def test_a_tool_outside_the_allowlist_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                agent.grant(CAPABILITY_MEMORY_READ)
                broker = agent.executor.broker
                result = asyncio.run(
                    broker.call(
                        ToolCallAttempt(call_id="c1", name="recall_memory", arguments={}),
                        context=BrokerCallContext(
                            run_id="r1", fence=1, allowlist=frozenset({"current_time"}),
                            capabilities=broker.capability_snapshot(),
                            tool_calls_remaining=3,
                        ),
                    )
                )
                self.assertFalse(result.ok)
                self.assertIn("allowlist", result.safe_error)
            finally:
                asyncio.run(agent.close())

    def test_revoking_the_grant_takes_the_tool_away_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                grant = agent.grant(CAPABILITY_MEMORY_READ)
                self.assertIn(
                    CAPABILITY_MEMORY_READ, agent.executor.broker.capability_snapshot()
                )
                agent.revoke_grant(grant.grant_id)
                self.assertNotIn(
                    CAPABILITY_MEMORY_READ, agent.executor.broker.capability_snapshot()
                )
            finally:
                asyncio.run(agent.close())

    def test_the_builtin_set_declares_the_capabilities_it_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                specs = {t.spec.name: t.spec for t in builtin_tools(agent)}
                self.assertIsNone(specs["current_time"].required_capability)
                self.assertEqual(specs["recall_memory"].required_capability, CAPABILITY_MEMORY_READ)
                self.assertEqual(
                    specs["list_recent_activity"].required_capability, CAPABILITY_STATE_READ
                )
                for spec in specs.values():
                    self.assertTrue(spec.read_only)
            finally:
                asyncio.run(agent.close())


class OneLineGrantTests(unittest.TestCase):
    def test_the_short_form_records_real_consent_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ProactiveAgent(
                state_dir=Path(tmp) / "s", model=OfflineModel(),
                clock=FakeClock(wall_ms=T0), profile="grant",
            )
            try:
                grant = agent.grant(NOTIFY_SELF_CAPABILITY)
                self.assertEqual(grant.capability, NOTIFY_SELF_CAPABILITY)
                self.assertTrue(grant.consent_evidence_ref.startswith("consent:local-call:"))
                self.assertTrue(grant.is_active(T0))
                self.assertIn(NOTIFY_SELF_CAPABILITY, agent.store.active_capabilities(now_ms=T0))
            finally:
                asyncio.run(agent.close())

    def test_an_explicit_evidence_ref_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ProactiveAgent(
                state_dir=Path(tmp) / "s", model=OfflineModel(),
                clock=FakeClock(wall_ms=T0), profile="grant2",
            )
            try:
                grant = agent.grant(NOTIFY_SELF_CAPABILITY, evidence="consent:settings-screen:42")
                self.assertEqual(grant.consent_evidence_ref, "consent:settings-screen:42")
            finally:
                asyncio.run(agent.close())


class JobIdempotencyTests(unittest.TestCase):
    def _agent(self, tmp: str) -> ProactiveAgent:
        return ProactiveAgent(
            state_dir=Path(tmp) / "s", model=OfflineModel(),
            clock=FakeClock(wall_ms=T0), timezone="UTC", profile="jobs",
        )

    def _job(self, grant_id: str, instruction: str = "看一遍。") -> Job:
        return Job(
            id="daily",
            mode="task",
            schedule={"kind": "daily", "local_time": "09:00", "timezone": "UTC"},
            instruction=instruction,
            grant_refs=(grant_id,),
            delivery_policy={"timezone": "UTC"},
        )

    def test_the_same_job_submitted_twice_is_one_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                grant = agent.grant(NOTIFY_SELF_CAPABILITY)
                first = agent.jobs_upsert(self._job(grant.grant_id))
                second = agent.jobs_upsert(self._job(grant.grant_id))
                self.assertEqual(first.revision, second.revision)
            finally:
                asyncio.run(agent.close())

    def test_changed_content_without_a_revision_bump_is_refused(self):
        """Revisions are explicit; content is not a version.

        A derived idempotency key must not turn "I edited the instruction"
        into a silent in-place mutation — every behaviour that a change
        could affect is keyed on the revision, so the bump has to be the
        caller's deliberate act.
        """
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                grant = agent.grant(NOTIFY_SELF_CAPABILITY)
                agent.jobs_upsert(self._job(grant.grant_id))
                with self.assertRaises(PASError) as ctx:
                    agent.jobs_upsert(self._job(grant.grant_id, "换个说法。"))
                self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)
            finally:
                asyncio.run(agent.close())

    def test_a_revision_bump_is_how_you_change_a_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            try:
                grant = agent.grant(NOTIFY_SELF_CAPABILITY)
                first = agent.jobs_upsert(self._job(grant.grant_id))
                changed = Job(
                    id="daily",
                    mode="task",
                    schedule={"kind": "daily", "local_time": "09:00", "timezone": "UTC"},
                    instruction="换个说法。",
                    grant_refs=(grant.grant_id,),
                    delivery_policy={"timezone": "UTC"},
                    revision=first.revision + 1,
                )
                second = agent.jobs_upsert(changed)
                self.assertGreater(second.revision, first.revision)
            finally:
                asyncio.run(agent.close())


class MissedActivityVisibilityTests(unittest.TestCase):
    """`pas activity` must answer "what was missed?" for every job mode."""

    def test_a_task_jobs_missed_episode_is_user_visible(self):
        from datetime import datetime, timezone

        def at(*args) -> int:
            return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)

        clock = FakeClock(wall_ms=at(2025, 10, 21, 8, 59))
        with tempfile.TemporaryDirectory() as tmp:
            agent = ProactiveAgent(
                state_dir=Path(tmp) / "s", model=OfflineModel(),
                clock=clock, timezone="UTC", profile="outage",
            )
            try:
                grant = agent.grant(NOTIFY_SELF_CAPABILITY)
                agent.jobs_upsert(
                    Job(
                        id="daily", mode="task",
                        schedule={"kind": "daily", "local_time": "09:00", "timezone": "UTC"},
                        instruction="看一遍。", grant_refs=(grant.grant_id,),
                        delivery_policy={"timezone": "UTC"},
                    )
                )
                clock.advance_wall(60_000)
                asyncio.run(agent.tick())
                clock.advance_wall(3 * 24 * 3600 * 1000 + 5 * 3600 * 1000)
                asyncio.run(agent.tick())

                missed = [r for r in agent.store.job_activity() if r["state"] == "missed"]
                self.assertEqual(len(missed), 1, "one episode, one row")
                self.assertIn("episode_slots=3", missed[0]["reason"])
                self.assertEqual(missed[0]["job_id"], "daily")
            finally:
                asyncio.run(agent.close())


class ExamplesRunOutOfTheBoxTests(unittest.TestCase):
    """The quickstart has to work for someone who has just cloned the repo."""

    def _run(self, name: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "examples" / name)],
            capture_output=True, text=True, timeout=180,
            env={**__import__("os").environ, "PAS_MODEL_BASE_URL": "", "PAS_MODEL_API_KEY": ""},
        )

    def test_quickstart_runs_with_no_credentials(self):
        done = self._run("quickstart.py")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("offline-stub", done.stdout)
        self.assertIn("current_time", done.stdout)

    def test_the_outage_demo_runs_and_reports_the_episode(self):
        done = self._run("restart_after_three_days.py")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("episode_slots=3", done.stdout)
        self.assertIn("0 条 inbox", done.stdout)


if __name__ == "__main__":
    unittest.main()
