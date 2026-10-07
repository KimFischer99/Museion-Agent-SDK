"""SPEC §22.1 item 3: Hermes and Pi as host-bridge drivers.

These pin what the drivers must NOT get wrong, because each has a way of
looking successful while lying:

* Hermes ``start`` returns *acceptance*. A driver that returns there would
  report an answer before one existed, so the tests drive a run whose first
  status is ``started`` and only then ``completed``.
* Pi takes one flat string. If the driver forgot to flatten the prompt, the
  contract never reaches the worker and the failure surfaces much later as
  an unparseable envelope — so the test asserts what the worker actually
  received.

The transport layer itself (submit/status/cancel/unknown) is the existing
P5 adapters' territory and stays covered by test_hermes_executor.py and
test_pi_worker.py.
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
    ErrorCode,
    HostBridge,
    HostDriver,
    HostPrompt,
    PASError,
    RunBudget,
    RunCancelled,
)
from proactive_sdk.hermes import (  # noqa: E402
    CancellationUnconfirmed,
    HermesAcceptanceUnknown,
    HermesRunRequest,
    HermesRunsExecutor,
)
from proactive_sdk.host_drivers import HermesHostDriver, PiHostDriver  # noqa: E402
from proactive_sdk.hermes import HermesRunHandle  # noqa: E402
from proactive_sdk.decision_contract import (  # noqa: E402
    agent_system_prompt,
    decision_contract,
)
from proactive_sdk.pi_worker import PiWorkerConfig, PiWorkerExecutor  # noqa: E402

FAKE_WORKER = str(Path(__file__).resolve().parent / "fake_pi_worker.py")
ENVELOPE = {
    "decision": "silent",
    "summary": "No change",
    "proposals": [],
}


def _caps() -> dict:
    return {
        "object": "hermes.api_server.capabilities",
        "features": {
            "run_submission": True,
            "run_status": True,
            "run_stop": True,
            "runs_idempotency": {"supported": True, "durable": True, "retention_seconds": 86400},
        },
    }


class ScriptedHermesClient:
    """Same surface as HermesRunsClient with canned replies."""

    def __init__(self, *, default_status: dict | None = None) -> None:
        self.submit_replies: list[object] = []
        self.status_replies: list[object] = []
        self.bodies: list[tuple[dict, str]] = []
        self.stop_calls = 0
        #: Returned once status_replies is exhausted (cancel polls run long).
        self.default_status = default_status or {"status": "running"}

    def capabilities(self) -> dict:
        return _caps()

    def submit(self, body: dict, operation_key: str) -> dict:
        self.bodies.append((json.loads(json.dumps(body)), operation_key))
        item = self.submit_replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def status(self, run_id: str) -> dict:
        if not self.status_replies:
            return dict(self.default_status)
        item = self.status_replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stop(self, run_id: str) -> dict:
        self.stop_calls += 1
        return {"status": "stopping"}

    def events(self, run_id: str) -> dict:
        return {"events": []}


def _hermes_executor(client: ScriptedHermesClient) -> HermesRunsExecutor:
    return HermesRunsExecutor(
        "https://127.0.0.1:1", "tok", client=client,
        poll_interval_s=0.001, cancel_timeout_s=0.1,
    )


def _prompt(run_key: str = "run-x") -> HostPrompt:
    return HostPrompt(
        run_key=run_key,
        system="SYSTEM-CONTRACT-BLOCK",
        user="USER-CONTEXT-BLOCK",
        budget=RunBudget(),
    )


class HermesDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_acceptance_is_not_completion(self):
        client = ScriptedHermesClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        # First poll still running, second terminal: the driver must not
        # return on the first.
        client.status_replies = [
            {"status": "running"},
            {"status": "completed", "output": json.dumps(ENVELOPE)},
        ]
        driver = HermesHostDriver(_hermes_executor(client))
        reply = await driver.submit(_prompt(), timeout_s=5.0)
        self.assertEqual(json.loads(reply.text), ENVELOPE)
        self.assertEqual(reply.host_run_id, "run_a")

    async def test_the_contract_travels_in_instructions_for_hermes(self):
        client = ScriptedHermesClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        client.status_replies = [{"status": "completed", "output": json.dumps(ENVELOPE)}]
        driver = HermesHostDriver(_hermes_executor(client))
        # A realistic prompt, i.e. the one HostBridge actually hands over.
        real = HostPrompt(
            run_key="run-key-1",
            system=agent_system_prompt("检查一下。", RunBudget()),
            user="USER-CONTEXT-BLOCK",
        )
        await driver.submit(real, timeout_s=5.0)

        body, operation_key = client.bodies[0]
        self.assertEqual(operation_key, "run-key-1")  # stable idempotency key
        self.assertEqual(body["input"], "USER-CONTEXT-BLOCK")
        # The whole contract reached the host, in Hermes's own instruction slot.
        first_contract_line = decision_contract().splitlines()[0]
        self.assertIn(first_contract_line, body["instructions"])
        self.assertIn("检查一下。", body["instructions"])

    async def test_interrupted_run_is_a_cancellation_not_an_answer(self):
        client = ScriptedHermesClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        client.status_replies = [{"status": "interrupted", "output": "partial text"}]
        driver = HermesHostDriver(_hermes_executor(client))
        with self.assertRaises(RunCancelled):
            await driver.submit(_prompt(), timeout_s=5.0)

    async def test_failed_run_is_retryable_not_a_decision(self):
        client = ScriptedHermesClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        client.status_replies = [{"status": "failed"}]
        driver = HermesHostDriver(_hermes_executor(client))
        with self.assertRaises(PASError) as ctx:
            await driver.submit(_prompt(), timeout_s=5.0)
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
        self.assertTrue(ctx.exception.retryable)

    async def test_completed_without_output_is_a_protocol_error(self):
        client = ScriptedHermesClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        client.status_replies = [{"status": "completed"}]
        driver = HermesHostDriver(_hermes_executor(client))
        with self.assertRaises(PASError) as ctx:
            await driver.submit(_prompt(), timeout_s=5.0)
        self.assertEqual(ctx.exception.code, ErrorCode.INTERNAL_ERROR)

    async def test_lost_submit_reply_is_unknown_not_a_retry(self):
        client = ScriptedHermesClient()
        client.submit_replies = [HermesAcceptanceUnknown("k", "hash")]
        driver = HermesHostDriver(_hermes_executor(client))
        with self.assertRaises(PASError) as ctx:
            await driver.submit(_prompt(), timeout_s=5.0)
        self.assertEqual(ctx.exception.code, ErrorCode.EFFECT_UNKNOWN)
        self.assertTrue(ctx.exception.retryable)

    async def test_cancel_of_a_running_run_reaches_confirmed(self):
        client = ScriptedHermesClient()
        client.status_replies = [{"status": "cancelled"}]
        executor = _hermes_executor(client)
        driver = HermesHostDriver(executor)
        driver._handles["run-c"] = HermesRunHandle(
            executor_id="hermes-runs", host_run_id="run_a", state="running"
        )
        self.assertEqual(await driver.cancel("run-c"), CANCEL_CONFIRMED)
        self.assertEqual(client.stop_calls, 1)

    async def test_cancel_of_an_already_settled_run_is_confirmed_without_asking(self):
        client = ScriptedHermesClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        client.status_replies = [{"status": "cancelled"}]
        driver = HermesHostDriver(_hermes_executor(client))
        with self.assertRaises(RunCancelled):
            await driver.submit(_prompt("run-c"), timeout_s=5.0)
        # The driver observed the terminal state itself, so it does not need
        # to re-ask the host to learn the run is not running.
        self.assertEqual(await driver.cancel("run-c"), CANCEL_CONFIRMED)
        self.assertEqual(client.stop_calls, 0)

    async def test_cancel_of_an_unknown_run_is_unsupported_not_success(self):
        driver = HermesHostDriver(_hermes_executor(ScriptedHermesClient()))
        self.assertEqual(await driver.cancel("never-submitted"), CANCEL_UNSUPPORTED)

    async def test_unconfirmed_cancellation_is_reported_as_requested(self):
        # The host never settles, so the cancel times out unconfirmed.
        client = ScriptedHermesClient(default_status={"status": "running"})
        executor = HermesRunsExecutor(
            "https://127.0.0.1:1", "tok", client=client,
            poll_interval_s=0.001, cancel_timeout_s=0.01,
        )
        driver = HermesHostDriver(executor)
        driver._handles["run-u"] = HermesRunHandle(
            executor_id="hermes-runs", host_run_id="run_a", state="running"
        )
        self.assertEqual(await driver.cancel("run-u"), CANCEL_REQUESTED)


class HermesDriverThroughBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_real_envelope_flows_through_the_bridge(self):
        client = ScriptedHermesClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        client.status_replies = [{"status": "completed", "output": json.dumps(ENVELOPE)}]
        driver = HermesHostDriver(_hermes_executor(client))
        bridge = HostBridge(driver, capabilities=frozenset({"calendar.read"}))
        self.assertIsInstance(driver, HostDriver)

        from test_host_bridge import _pack, _request

        outcome = await bridge.execute(_request(), _pack(), [], instruction="检查一下。")
        self.assertEqual(outcome.decision.decision, "silent")
        self.assertEqual(outcome.usage["pricing_basis"], "unknown")


class PiDriverTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, **kw) -> PiWorkerConfig:
        defaults = dict(
            command=(sys.executable, FAKE_WORKER, "pi_worker.ts"),
            pi_entry="/opt/pi/dist/index.js",
            init_timeout_s=10.0,
            run_timeout_s=20.0,
            cancel_timeout_s=10.0,
        )
        defaults.update(kw)
        return PiWorkerConfig(**defaults)

    async def test_submit_flattens_the_whole_prompt_into_the_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = PiWorkerExecutor(self._config())
            driver = PiHostDriver(executor, cwd=tmp)
            try:
                prompt = HostPrompt(
                    run_key="run-pi-1",
                    system="SYSTEM-CONTRACT-BLOCK",
                    user="USER-CONTEXT-BLOCK RETURN_PROPOSE",
                    budget=RunBudget(),
                )
                reply = await driver.submit(prompt, timeout_s=10.0)
            finally:
                await driver.close()
        envelope = json.loads(reply.text)
        # The worker answered because the trigger token travelled inside the
        # flattened prompt — proof the contract+context reached it.
        self.assertEqual(envelope["decision"], "propose")
        self.assertEqual(envelope["proposals"][0]["kind"], "draft")

    async def test_worker_starts_lazily_and_reports_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = PiWorkerExecutor(self._config())
            self.assertFalse(executor.is_started)
            driver = PiHostDriver(executor, cwd=tmp)
            try:
                await driver.submit(
                    HostPrompt(run_key="run-1", system="s", user="RETURN_SILENT"),
                    timeout_s=10.0,
                )
                self.assertTrue(executor.is_started)
                self.assertIsInstance(await driver.capabilities(), dict)
            finally:
                await driver.close()

    async def test_capabilities_connects_instead_of_reporting_nothing(self):
        """An empty answer would read as "this host can do nothing"."""
        with tempfile.TemporaryDirectory() as tmp:
            executor = PiWorkerExecutor(self._config())
            driver = PiHostDriver(executor, cwd=tmp)
            try:
                info = await driver.capabilities()
                self.assertTrue(executor.is_started)
                self.assertEqual(info.get("pi_version"), "fake-1.0.0")
            finally:
                await driver.close()

    async def test_invalid_envelope_from_the_worker_is_a_protocol_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = PiWorkerExecutor(self._config())
            driver = PiHostDriver(executor, cwd=tmp)
            try:
                with self.assertRaises(PASError):
                    await driver.submit(
                        HostPrompt(run_key="run-2", system="s", user="BAD_ENVELOPE"),
                        timeout_s=10.0,
                    )
            finally:
                await driver.close()

    async def test_cwd_must_exist(self):
        executor = PiWorkerExecutor(self._config())
        with self.assertRaises(PASError):
            PiHostDriver(executor, cwd="/definitely/not/here")

    async def test_cancel_is_confirmed_when_the_worker_settles(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = PiWorkerExecutor(self._config())
            driver = PiHostDriver(executor, cwd=tmp)
            try:
                await driver.submit(
                    HostPrompt(run_key="run-3", system="s", user="RETURN_SILENT"),
                    timeout_s=10.0,
                )
                self.assertEqual(await driver.cancel("run-3"), CANCEL_CONFIRMED)
            finally:
                await driver.close()


class PiDriverThroughBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_real_worker_envelope_flows_through_the_bridge(self):
        from test_host_bridge import _pack, _request

        with tempfile.TemporaryDirectory() as tmp:
            executor = PiWorkerExecutor(
                PiWorkerConfig(
                    command=(sys.executable, FAKE_WORKER, "pi_worker.ts"),
                    pi_entry="/opt/pi/dist/index.js",
                    init_timeout_s=10.0,
                    run_timeout_s=20.0,
                    cancel_timeout_s=10.0,
                )
            )
            driver = PiHostDriver(executor, cwd=tmp)
            bridge = HostBridge(driver)
            try:
                outcome = await bridge.execute(_request(), _pack(), [], instruction="RETURN_SILENT")
                self.assertEqual(outcome.decision.decision, "silent")
            finally:
                await driver.close()


if __name__ == "__main__":
    unittest.main()
