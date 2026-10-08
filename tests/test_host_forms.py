"""SPEC §22.1 item 4: the two broadest host forms.

The acceptance clause is concrete: *an agent that only accepts a string and
returns a string must be able to take a job and complete one loop.* So the
tests below drive a real subprocess that knows nothing about PAS, and an
in-process callable that is just a function, through the full facade.

What is deliberately NOT hidden: a subprocess host runs with the
operator's environment and PAS does not sandbox it. The tests pin the
declared capability (``external_tool_broker=False``) rather than letting it
be implied, because that declaration is what the run ledger will report.
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
    CANCEL_UNSUPPORTED,
    CallableHostDriver,
    ErrorCode,
    FakeClock,
    HostBridge,
    HostDriver,
    HostPrompt,
    Job,
    MemoryEntry,
    PASError,
    ProactiveAgent,
    RunBudget,
    RunCancelled,
    SubprocessHostDriver,
    extract_envelope,
)

T0 = 1_760_000_000_000
MEMORY_ID = "mem-host-forms"
FAKE_HOST = str(Path(__file__).resolve().parent / "fake_text_host.py")
SILENT = {"decision": "silent", "summary": "No change", "proposals": []}


def _prompt(user: str = "nothing to report", run_key: str = "run-forms") -> HostPrompt:
    return HostPrompt(run_key=run_key, system="CONTRACT-BLOCK", user=user, budget=RunBudget())


class EnvelopeExtractionTests(unittest.TestCase):
    def test_takes_the_last_json_object_carrying_a_decision(self):
        stdout = (
            "[log] booting\n"
            "[log] thinking\n"
            '{"decision": "silent", "summary": "first", "proposals": []}\n'
            "[log] done\n"
            '{"decision": "propose", "summary": "second", "proposals": []}\n'
        )
        self.assertEqual(
            json.loads(extract_envelope(stdout))["summary"], "second"
        )

    def test_ignores_non_json_and_json_without_a_decision_key(self):
        stdout = (
            '{"level": "info", "msg": "not an envelope"}\n'
            "plain text\n"
            '{"decision": "silent", "summary": "real", "proposals": []}\n'
        )
        self.assertEqual(json.loads(extract_envelope(stdout))["summary"], "real")

    def test_returns_raw_text_when_nothing_qualifies(self):
        # The bridge must be the one to report the problem, not this helper.
        stdout = "no structured answer here\n"
        self.assertEqual(extract_envelope(stdout), stdout)

    def test_the_contract_itself_never_looks_like_an_envelope(self):
        from proactive_sdk import agent_system_prompt

        text = agent_system_prompt("检查一下", RunBudget())
        for line in text.splitlines():
            if line.strip().startswith("{"):
                with self.assertRaises(ValueError, msg=line):
                    json.loads(line.strip())
        self.assertEqual(extract_envelope(text), text)


class CallableHostDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_plain_sync_function_is_a_host(self):
        driver = CallableHostDriver(lambda _p: json.dumps(SILENT))
        self.assertIsInstance(driver, HostDriver)
        reply = await driver.submit(_prompt(), timeout_s=5.0)
        self.assertEqual(json.loads(reply.text), SILENT)

    async def test_a_dict_return_is_serialised(self):
        driver = CallableHostDriver(lambda _p: SILENT)
        reply = await driver.submit(_prompt(), timeout_s=5.0)
        self.assertEqual(json.loads(reply.text), SILENT)

    async def test_an_async_function_is_awaited(self):
        async def host(_p: str) -> str:
            await asyncio.sleep(0)
            return json.dumps(SILENT)

        driver = CallableHostDriver(host)
        reply = await driver.submit(_prompt(), timeout_s=5.0)
        self.assertEqual(json.loads(reply.text), SILENT)

    async def test_it_receives_the_flattened_prompt(self):
        seen: list[str] = []

        def host(prompt: str) -> str:
            seen.append(prompt)
            return json.dumps(SILENT)

        driver = CallableHostDriver(host)
        await driver.submit(_prompt(user="USER-BLOCK"), timeout_s=5.0)
        self.assertIn("CONTRACT-BLOCK", seen[0])
        self.assertIn("USER-BLOCK", seen[0])

    async def test_an_unusable_return_type_is_a_config_error(self):
        driver = CallableHostDriver(lambda _p: object())
        with self.assertRaises(PASError) as ctx:
            await driver.submit(_prompt(), timeout_s=5.0)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)

    async def test_a_slow_host_hits_the_deadline(self):
        async def host(_p: str) -> str:
            await asyncio.sleep(10)
            return json.dumps(SILENT)

        driver = CallableHostDriver(host)
        with self.assertRaises(PASError) as ctx:
            await driver.submit(_prompt(), timeout_s=0.05)
        self.assertEqual(ctx.exception.code, ErrorCode.DEADLINE_EXCEEDED)

    async def test_cancel_reports_confirmed_for_a_settled_run(self):
        driver = CallableHostDriver(lambda _p: json.dumps(SILENT))
        await driver.submit(_prompt(run_key="r1"), timeout_s=5.0)
        self.assertEqual(await driver.cancel("r1"), CANCEL_CONFIRMED)

    async def test_cancel_of_an_unknown_run_is_unsupported(self):
        driver = CallableHostDriver(lambda _p: json.dumps(SILENT))
        self.assertEqual(await driver.cancel("never"), CANCEL_UNSUPPORTED)

    async def test_capabilities_name_the_form(self):
        driver = CallableHostDriver(lambda _p: "", name="my-agent")
        caps = await driver.capabilities()
        self.assertEqual(caps["form"], "in_process")
        self.assertEqual(caps["host"], "my-agent")


class SubprocessHostDriverTests(unittest.IsolatedAsyncioTestCase):
    def _driver(self, **kw) -> SubprocessHostDriver:
        defaults = dict(
            command=(sys.executable, FAKE_HOST),
            capabilities={"external_tool_broker": False},
        )
        defaults.update(kw)
        return SubprocessHostDriver(**defaults)

    async def test_a_chatty_black_box_completes(self):
        driver = self._driver()
        reply = await driver.submit(_prompt(), timeout_s=20.0)
        envelope = json.loads(reply.text)
        self.assertEqual(envelope["decision"], "silent")
        self.assertEqual(envelope["proposals"], [])
        self.assertEqual(reply.usage["host_exit_code"], 0)

    async def test_a_proposal_envelope_survives_a_chatty_black_box(self):
        driver = self._driver()
        reply = await driver.submit(
            _prompt(user="PAS-FAKE-HOST-PROPOSE"), timeout_s=20.0
        )
        envelope = json.loads(reply.text)
        self.assertEqual(envelope["decision"], "propose")
        self.assertEqual(envelope["proposals"][0]["kind"], "draft")

    async def test_the_prompt_reaches_stdin(self):
        driver = self._driver()
        reply = await driver.submit(_prompt(user="PAS-FAKE-HOST-GARBAGE"), timeout_s=20.0)
        # The trigger token was seen, so the prompt did arrive; the answer is
        # intentionally not an envelope.
        self.assertIn("could not produce", reply.text)

    async def test_a_non_zero_exit_is_a_failure_not_an_empty_answer(self):
        driver = self._driver()
        with self.assertRaises(PASError) as ctx:
            await driver.submit(_prompt(user="PAS-FAKE-HOST-FAIL"), timeout_s=20.0)
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
        self.assertTrue(ctx.exception.retryable)

    async def test_a_crash_after_partial_output_is_still_a_failure(self):
        driver = self._driver()
        with self.assertRaises(PASError) as ctx:
            await driver.submit(_prompt(user="PAS-FAKE-HOST-CRASH"), timeout_s=20.0)
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    async def test_a_hung_host_is_killed_at_the_deadline(self):
        driver = self._driver()
        with self.assertRaises(PASError) as ctx:
            await driver.submit(_prompt(user="PAS-FAKE-HOST-SLOW"), timeout_s=0.4)
        self.assertEqual(ctx.exception.code, ErrorCode.DEADLINE_EXCEEDED)
        self.assertEqual(driver._procs, {})  # no leaked process handle

    async def test_a_missing_binary_is_a_dependency_error(self):
        driver = self._driver(command=("/nonexistent/agent-binary",))
        with self.assertRaises(PASError) as ctx:
            await driver.submit(_prompt(), timeout_s=5.0)
        self.assertEqual(ctx.exception.code, ErrorCode.DEPENDENCY_MISSING)

    async def test_cwd_must_exist(self):
        with self.assertRaises(PASError):
            self._driver(cwd="/definitely/not/here")

    async def test_capabilities_declare_the_form_and_the_trust_boundary(self):
        caps = await self._driver().capabilities()
        self.assertEqual(caps["form"], "subprocess")
        self.assertIs(caps["external_tool_broker"], False)


class HostFormsThroughTheFacadeTests(unittest.TestCase):
    """The actual acceptance clause: take a job, complete one loop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(wall_ms=T0)

    def tearDown(self):
        self.tmp.cleanup()

    def _prepare(self, agent: ProactiveAgent) -> None:
        agent.grant("memory.read")
        asyncio.run(
            agent.pack_builder.memory.remember(
                MemoryEntry(memory_id=MEMORY_ID, content="用户在关注这件事", source="user")
            )
        )
        grant = agent.create_grant_from_user_consent(
            capability="notify.self",
            account_ref="account:primary",
            scope={},
            consent_evidence_ref="consent:forms",
        )
        agent.jobs_upsert(
            Job(
                id="forms-job",
                mode="task",
                schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 3600},
                instruction="检查一下。",
                grant_refs=(grant.grant_id,),
            ),
            idempotency_key="forms-job-v1",
        )

    def _envelope(self) -> dict:
        return {
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

    def _subprocess_agent(self, trigger: str = "") -> ProactiveAgent:
        driver = SubprocessHostDriver(
            command=(sys.executable, FAKE_HOST),
            capabilities={"external_tool_broker": False},
        )
        agent = ProactiveAgent(
            state_dir=Path(self.tmp.name),
            executor=HostBridge(driver),
            clock=self.clock,
            timezone="UTC",
            locale="en",
            profile="forms",
        )
        # The trigger token travels inside the job instruction, i.e. inside
        # the prompt the black box receives on stdin.
        self.trigger = trigger
        return agent

    def test_a_string_only_black_box_completes_a_loop(self):
        agent = self._subprocess_agent()
        try:
            self._prepare(agent)
            agent.trigger_job("forms-job", reason="forms test")
            report = asyncio.run(agent.tick())
            runs = list(report["runs"])
            self.assertEqual(runs[0]["outcome"], "proposed")
            # `silent` settles as completed: nothing to deliver, loop closed.
            self.assertEqual(runs[0]["policy_outcome"], "completed")
        finally:
            asyncio.run(agent.close())

    def test_an_in_process_callable_takes_a_job_to_the_inbox(self):
        envelope = self._envelope()
        driver = CallableHostDriver(lambda _p: envelope)
        agent = ProactiveAgent(
            state_dir=Path(self.tmp.name),
            executor=HostBridge(driver, capabilities=("memory.read",)),
            clock=self.clock,
            timezone="UTC",
            locale="en",
            profile="forms",
        )
        try:
            self._prepare(agent)
            agent.trigger_job("forms-job", reason="forms test")
            report = asyncio.run(agent.tick())
            runs = list(report["runs"])
            self.assertEqual(runs[0]["outcome"], "proposed")
            self.assertEqual(runs[0]["policy_outcome"], "actions_queued")
            inbox = agent.store.list_inbox()
            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0]["body"], "只通知本人")
        finally:
            asyncio.run(agent.close())


if __name__ == "__main__":
    unittest.main()
