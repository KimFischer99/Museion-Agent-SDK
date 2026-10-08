"""P5 Hermes Runs executor: contract tests against a scripted transport.

All network behavior is fake here; the real-service validation was run
separately against a locked Hermes version (see docs/COMPATIBILITY.md).
These tests pin the adapter semantics that SPEC §12.1 and
§15.1 (P5 row) require: 提交≠完成、取消≠已停、幂等对账、fail closed。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proactive_sdk.contracts import ErrorCode, PASError
from proactive_sdk.hermes import (
    CancellationUnconfirmed,
    HermesAcceptanceUnknown,
    HermesProtocolError,
    HermesRunHandle,
    HermesRunRequest,
    HermesRunsExecutor,
    HermesVersionUnsupported,
)

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "v1"


def _caps(**feature_overrides: object) -> dict:
    features = {
        "run_submission": True,
        "run_status": True,
        "run_stop": True,
        "runs_idempotency": {"supported": True, "durable": True, "retention_seconds": 86400},
    }
    features.update(feature_overrides)
    return {"object": "hermes.api_server.capabilities", "features": features}


class FakeClient:
    """Same surface as HermesRunsClient with canned replies."""

    def __init__(self, caps: dict | None = None) -> None:
        self.caps = caps if caps is not None else _caps()
        self.submit_replies: list[object] = []  # dict replies or Exception instances
        self.status_replies: list[object] = []
        self.stop_calls = 0
        self.submit_bodies: list[tuple[dict, str]] = []

    def capabilities(self) -> dict:
        if isinstance(self.caps, Exception):
            raise self.caps
        return self.caps

    def submit(self, body: dict, operation_key: str) -> dict:
        self.submit_bodies.append((json.loads(json.dumps(body)), operation_key))
        item = self.submit_replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def status(self, run_id: str) -> dict:
        item = self.status_replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stop(self, run_id: str) -> dict:
        self.stop_calls += 1
        return {"object": "hermes.run", "run_id": run_id, "status": "stopping"}

    def events(self, run_id: str) -> dict:
        return {"events": [{"event": "run.started"}, {"event": "run.completed"}]}


class _HTTPError409(Exception):
    pass


def _executor(client: FakeClient, **kw: object) -> HermesRunsExecutor:
    defaults = dict(poll_interval_s=0.001, cancel_timeout_s=1.0)
    defaults.update(kw)
    return HermesRunsExecutor("https://127.0.0.1:1", "tok", client=client, **defaults)


def _request(**kw: object) -> HermesRunRequest:
    defaults = dict(operation_key="op-key-1", prompt="hello", instructions="reply")
    defaults.update(kw)
    return HermesRunRequest(**defaults)


class HermesRequestTests(unittest.TestCase):
    def test_operation_key_and_hash_stable(self):
        a, b = _request(), _request()
        self.assertEqual(a.body_hash, b.body_hash)
        self.assertNotEqual(a.body_hash, _request(prompt="other").body_hash)

    def test_rejects_bad_operation_key(self):
        with self.assertRaises(PASError):
            _request(operation_key="")
        with self.assertRaises(PASError):
            _request(operation_key="bad key\n")

    def test_rejects_empty_prompt(self):
        with self.assertRaises(PASError):
            _request(prompt="")
        with self.assertRaises(PASError):
            _request(instructions="")


class HermesCapabilitiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_fail_closed_on_missing_feature(self):
        client = FakeClient(_caps(run_stop=False))
        with self.assertRaises(HermesVersionUnsupported) as ctx:
            await _executor(client).capabilities()
        self.assertEqual(ctx.exception.missing, ["run_stop"])

    async def test_fail_closed_on_unsupported_idempotency(self):
        client = FakeClient(_caps(runs_idempotency={"supported": False}))
        with self.assertRaises(HermesVersionUnsupported):
            await _executor(client).capabilities()

    async def test_fail_closed_without_features_object(self):
        client = FakeClient({"object": "hermes.api_server.capabilities"})
        with self.assertRaises(HermesProtocolError):
            await _executor(client).capabilities()

    async def test_capabilities_cached_after_success(self):
        client = FakeClient()
        ex = _executor(client)
        await ex.capabilities()
        client.caps = _caps(run_stop=False)  # a later downgrade must not matter
        await ex.capabilities()


class HermesStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_is_acceptance_not_completion(self):
        client = FakeClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        handle = await _executor(client).start(_request())
        self.assertEqual(handle.state, "accepted")
        self.assertEqual(handle.host_run_id, "run_a")
        # 提交≠完成: no output may exist at acceptance time.
        self.assertNotEqual(handle.state, "completed")

    async def test_start_sends_idempotency_key_and_documented_body(self):
        client = FakeClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        req = _request(operation_key="op-xyz", prompt="P", instructions="I")
        await _executor(client).start(req)
        body, key = client.submit_bodies[0]
        self.assertEqual(key, "op-xyz")
        self.assertEqual(body, {"input": "P", "instructions": "I"})

    async def test_transport_failure_is_acceptance_unknown(self):
        client = FakeClient()
        client.submit_replies = [HermesAcceptanceUnknown("k", "h")]
        req = _request()
        with self.assertRaises(HermesAcceptanceUnknown) as ctx:
            await _executor(client).start(req)
        self.assertEqual(ctx.exception.operation_key, req.operation_key)
        self.assertEqual(ctx.exception.body_hash, req.body_hash)

    async def test_key_conflict_is_conflict_not_rewrite(self):
        class _Conflict(Exception):
            pass

        from proactive_sdk.hermes import _HermesKeyConflict

        client = FakeClient()
        client.submit_replies = [_HermesKeyConflict()]
        with self.assertRaises(PASError) as ctx:
            await _executor(client).start(_request())
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)

    async def test_submit_without_run_id_is_unknown(self):
        client = FakeClient()
        client.submit_replies = [{"status": "started"}]
        with self.assertRaises(HermesAcceptanceUnknown):
            await _executor(client).start(_request())


class HermesStatusTests(unittest.IsolatedAsyncioTestCase):
    async def _result(self, status_reply: dict):
        client = FakeClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        ex = _executor(client)
        handle = await ex.start(_request())
        client.status_replies = [status_reply]
        return await ex.status(handle)

    async def test_started_maps_accepted(self):
        result = await self._result({"status": "started"})
        self.assertEqual(result.handle.state, "accepted")

    async def test_stopping_is_not_terminal(self):
        result = await self._result({"status": "stopping"})
        self.assertEqual(result.handle.state, "running")

    async def test_waiting_for_approval_is_not_completion(self):
        result = await self._result({"status": "waiting_for_approval"})
        self.assertEqual(result.handle.state, "waiting_for_approval")
        self.assertIsNone(result.output)

    async def test_completed_requires_string_output(self):
        result = await self._result(
            {"status": "completed", "output": "OK",
             "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        )
        self.assertEqual(result.handle.state, "completed")
        self.assertEqual(result.output, "OK")
        self.assertEqual(result.usage["pricing_basis"], "measured")
        self.assertEqual(result.usage["provider"], "hermes")

    async def test_completed_without_usage_reports_unknown_not_zero(self):
        result = await self._result({"status": "completed", "output": "OK"})
        self.assertEqual(result.usage["pricing_basis"], "unknown")
        self.assertIsNone(result.usage["input_tokens"])

    async def test_completed_without_output_is_protocol_error(self):
        client = FakeClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        ex = _executor(client)
        handle = await ex.start(_request())
        client.status_replies = [{"status": "completed"}]
        with self.assertRaises(HermesProtocolError):
            await ex.status(handle)

    async def test_interrupted_maps_cancelled_without_output_claim(self):
        result = await self._result(
            {"status": "interrupted", "output": "half", "interrupted": True}
        )
        self.assertEqual(result.handle.state, "cancelled")
        self.assertTrue(result.interrupted)

    async def test_unknown_status_fails_closed(self):
        with self.assertRaises(HermesProtocolError):
            await self._result({"status": "bloobed"})

    async def test_usage_schema_shape(self):
        result = await self._result(
            {
                "status": "completed",
                "output": "OK",
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                "runtime": {"model": "glm-5.3-flash", "provider": "custom"},
                "created_at": 100.0,
                "updated_at": 101.5,
            }
        )
        usage = result.usage
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertIsNone(usage["tool_calls"])
        self.assertEqual(usage["elapsed_ms"], 1500)
        self.assertEqual(usage["model"], "glm-5.3-flash")
        schema = json.loads((SCHEMA_DIR / "usage.json").read_text(encoding="utf-8"))
        from proactive_sdk.schema_validate import assert_valid

        assert_valid(schema, usage)


class HermesWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_polls_until_terminal(self):
        client = FakeClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        ex = _executor(client)
        handle = await ex.start(_request())
        client.status_replies = [
            {"status": "running"},
            {"status": "running"},
            {"status": "completed", "output": "done"},
        ]
        result = await ex.wait(handle)
        self.assertEqual(result.handle.state, "completed")
        self.assertEqual(result.output, "done")

    async def test_wait_timeout_leaves_run_unclaimed(self):
        client = FakeClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        ex = _executor(client)
        handle = await ex.start(_request())
        client.status_replies = [{"status": "running"}] * 100
        with self.assertRaises(PASError) as ctx:
            await ex.wait(handle, timeout_s=0.05)
        self.assertEqual(ctx.exception.code, ErrorCode.DEADLINE_EXCEEDED)


class HermesCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_host_confirmed_terminal(self):
        client = FakeClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        ex = _executor(client)
        handle = await ex.start(_request())
        client.status_replies = [
            {"status": "stopping"},
            {"status": "cancelled", "interrupted": True},
        ]
        result = await ex.cancel(handle)
        self.assertEqual(client.stop_calls, 1)
        self.assertEqual(result.handle.state, "cancelled")
        # 取消≠已停: only the host-confirmed terminal state is reported.
        self.assertNotEqual(result.handle.state, "running")

    async def test_cancel_timeout_reports_unconfirmed(self):
        client = FakeClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        ex = _executor(client)
        handle = await ex.start(_request())
        client.status_replies = [{"status": "running"}] * 100
        with self.assertRaises(CancellationUnconfirmed) as ctx:
            await ex.cancel(handle, timeout_s=0.05)
        self.assertEqual(ctx.exception.host_run_id, "run_a")

    async def test_cancel_completed_run_is_noop_terminal(self):
        client = FakeClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        ex = _executor(client)
        handle = await ex.start(_request())
        client.status_replies = [{"status": "completed", "output": "OK"}]
        result = await ex.cancel(handle)
        self.assertEqual(result.handle.state, "completed")


class HermesReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_returns_original_run(self):
        client = FakeClient()
        client.submit_replies = [
            {"run_id": "run_1", "status": "started", "replayed": False},
            {"run_id": "run_1", "status": "completed", "replayed": True},
        ]
        ex = _executor(client)
        await ex.start(_request())
        handle = await ex.reconcile(_request())
        self.assertEqual(handle.host_run_id, "run_1")
        body, key = client.submit_bodies[1]
        self.assertEqual(key, "op-key-1")
        self.assertEqual(body, client.submit_bodies[0][0])

    async def test_conflict_never_rewrites(self):
        from proactive_sdk.hermes import _HermesKeyConflict

        client = FakeClient()
        client.submit_replies = [{"run_id": "run_1", "status": "started"}, _HermesKeyConflict()]
        ex = _executor(client)
        await ex.start(_request())
        with self.assertRaises(PASError) as ctx:
            await ex.reconcile(_request())
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)


class HermesEventsTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_are_safe_summaries_only(self):
        client = FakeClient()
        client.submit_replies = [{"run_id": "run_a", "status": "started"}]
        ex = _executor(client)
        handle = await ex.start(_request())
        seen = [item async for item in ex.events(handle, after_seq=1)]
        self.assertEqual(seen, [{"seq": 2, "type": "run.completed"}])
        for item in seen:
            self.assertEqual(set(item), {"seq", "type"})


class HermesHandleSchemaTests(unittest.TestCase):
    def test_handle_matches_schema(self):
        from proactive_sdk.schema_validate import assert_valid

        schema = json.loads((SCHEMA_DIR / "run_handle.json").read_text(encoding="utf-8"))
        for state in ("accepted", "running", "waiting_for_approval", "completed"):
            handle = HermesRunHandle(executor_id="hermes-runs", host_run_id="run_x", state=state)
            assert_valid(schema, handle.to_dict())
        with self.assertRaises(PASError):
            HermesRunHandle(executor_id="hermes-runs", host_run_id="run_x", state="pending")


if __name__ == "__main__":
    unittest.main()
