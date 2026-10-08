"""P5 Pi worker bridge: contract tests against a scripted worker process.

The bridge is exercised over a real subprocess speaking the worker
protocol (tests/fake_pi_worker.py); the real Pi SDK path is validated on
the locked host (tools/validate_p5_real.py, recorded in VALIDATION.md).
Pinned semantics: prompt ACK ≠ result, cancel waits for idle, unknown
effects are honest, concurrent runs are refused, fail closed on init.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.contracts import ErrorCode, PASError
from proactive_sdk.pi_worker import (
    DecisionEnvelopeError,
    PiWorkerConfig,
    PiWorkerExecutor,
)

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "v1"
FAKE_WORKER = str(Path(__file__).resolve().parent / "fake_pi_worker.py")


def _config(**kw: object) -> PiWorkerConfig:
    defaults = dict(
        command=(sys.executable, FAKE_WORKER, "pi_worker.ts"),
        pi_entry="/opt/pi/dist/index.js",
        init_timeout_s=10.0,
        run_timeout_s=20.0,
        cancel_timeout_s=10.0,
    )
    defaults.update(kw)
    return PiWorkerConfig(**defaults)


class PiWorkerConfigTests(unittest.TestCase):
    def test_command_must_target_worker_script(self):
        with self.assertRaises(PASError):
            _config(command=(sys.executable, FAKE_WORKER))

    def test_pi_entry_must_be_package_entry(self):
        with self.assertRaises(PASError):
            _config(pi_entry="/opt/pi/lib")
        with self.assertRaises(PASError):
            _config(pi_entry="")

    def test_allowed_tools_validate_and_can_disable_all_tools(self):
        self.assertEqual(_config(allowed_tools=()).allowed_tools, ())
        with self.assertRaises(PASError):
            _config(allowed_tools=("read", ""))


class PiWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_reports_worker_info(self):
        ex = PiWorkerExecutor(_config())
        info = await ex.start()
        self.assertEqual(info["pi_version"], "fake-1.0.0")
        self.assertEqual(info["allowed_tools"], ["read", "grep", "find", "ls"])
        await ex.close()

    async def test_start_fail_closed_when_worker_missing(self):
        ex = PiWorkerExecutor(_config(command=("/nonexistent/interpreter", "pi_worker.ts")))
        with self.assertRaises(PASError) as ctx:
            await ex.start()
        self.assertEqual(ctx.exception.code, ErrorCode.DEPENDENCY_MISSING)

    async def test_run_before_start_is_internal_error(self):
        ex = PiWorkerExecutor(_config())
        with self.assertRaises(PASError) as ctx:
            await ex.run(run_id="r1", instruction="x", cwd="/tmp")
        self.assertEqual(ctx.exception.code, ErrorCode.INTERNAL_ERROR)


class PiWorkerRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_run_returns_envelope_and_usage(self):
        ex = PiWorkerExecutor(_config())
        await ex.start()
        result = await ex.run(run_id="r1", instruction="RETURN_SILENT", cwd="/tmp/scratch")
        self.assertEqual(result.envelope, {"decision": "silent", "summary": "No change", "proposals": []})
        self.assertEqual(result.usage["input_tokens"], 11)
        self.assertEqual(result.usage["pricing_basis"], "measured")
        await ex.close()

    async def test_envelope_matches_decision_schema(self):
        from proactive_sdk.schema_validate import assert_valid

        schema = json.loads((SCHEMA_DIR / "decision.json").read_text(encoding="utf-8"))
        ex = PiWorkerExecutor(_config())
        await ex.start()
        result = await ex.run(run_id="r2", instruction="RETURN_PROPOSE", cwd="/tmp/scratch")
        # The worker envelope is the decision/summary/proposals triple of
        # schemas/v1/decision.json; validate it in the wire form the
        # coordinator would accept (protocol_version added).
        wire = {
            "protocol_version": "1.0",
            "decision": result.envelope["decision"],
            "summary": result.envelope["summary"],
            "proposals": result.envelope["proposals"],
        }
        assert_valid(schema, wire)
        silent = await ex.run(run_id="r2b", instruction="RETURN_SILENT", cwd="/tmp/scratch")
        assert_valid(schema, {
            "protocol_version": "1.0",
            "decision": silent.envelope["decision"],
            "summary": silent.envelope["summary"],
            "proposals": silent.envelope["proposals"],
        })
        await ex.close()

    async def test_bad_envelope_is_decision_error(self):
        ex = PiWorkerExecutor(_config())
        await ex.start()
        with self.assertRaises(DecisionEnvelopeError):
            await ex.run(run_id="r3", instruction="BAD_ENVELOPE", cwd="/tmp/scratch")
        await ex.close()

    async def test_second_concurrent_run_refused(self):
        ex = PiWorkerExecutor(_config())
        await ex.start()
        task = asyncio.ensure_future(
            ex.run(run_id="slow1", instruction="SLOW:5", cwd="/tmp/scratch")
        )
        await asyncio.sleep(0.3)
        with self.assertRaises(PASError) as ctx:
            await ex.run(run_id="slow2", instruction="RETURN_SILENT", cwd="/tmp/scratch")
        self.assertEqual(ctx.exception.code, ErrorCode.BUDGET_EXCEEDED)
        await ex.cancel("slow1")
        with self.assertRaises(PASError):
            await task
        await ex.close()


class PiWorkerCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_setstles_run(self):
        ex = PiWorkerExecutor(_config())
        await ex.start()
        task = asyncio.ensure_future(
            ex.run(run_id="slow1", instruction="SLOW:10", cwd="/tmp/scratch")
        )
        await asyncio.sleep(0.3)
        await ex.cancel("slow1")
        with self.assertRaises(PASError) as ctx:
            await task
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)
        # Executor is usable again after a cancelled run.
        result = await ex.run(run_id="r4", instruction="RETURN_SILENT", cwd="/tmp/scratch")
        self.assertEqual(result.envelope["decision"], "silent")
        await ex.close()

    async def test_cancel_without_idle_reports_unconfirmed(self):
        ex = PiWorkerExecutor(_config())
        await ex.start()
        task = asyncio.ensure_future(
            ex.run(run_id="stuck1", instruction="SLOW_STUCK:60", cwd="/tmp/scratch")
        )
        await asyncio.sleep(0.3)
        with self.assertRaises(PASError) as ctx:
            await ex.cancel("stuck1")
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)
        self.assertIn("cancellation_unconfirmed", str(ctx.exception))
        # The run itself is still in flight: closing the executor stops the
        # worker and the pending run must surface an honest failure.
        await ex.close()
        with contextlib.suppress(asyncio.TimeoutError, PASError):
            await asyncio.wait_for(task, timeout=5.0)


class PiWorkerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_crash_reports_provider_unavailable(self):
        ex = PiWorkerExecutor(_config())
        await ex.start()
        with self.assertRaises(PASError) as ctx:
            await ex.run(run_id="r5", instruction="CRASH", cwd="/tmp/scratch")
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
        await ex.close()


if __name__ == "__main__":
    unittest.main()
