"""P5 control-plane RPC envelope tests (SPEC §14.2)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.contracts import ErrorCode
from proactive_sdk.rpc import (
    MAX_MESSAGE_BYTES,
    PROACTIVE_RPC_METHODS,
    RPC_METHOD_NOT_FOUND,
    RPC_PARSE_ERROR,
    RpcProtocolError,
    RpcDispatcher,
    RpcSession,
    notification_message,
    parse_frame,
    request_message,
)


def _dispatcher() -> RpcDispatcher:
    dispatcher = RpcDispatcher(server_name="pas-test")
    calls: list[tuple[str, dict]] = []

    async def jobs_list(params: dict, session: RpcSession) -> dict:
        calls.append(("jobs.list", params))
        return {"jobs": []}

    dispatcher.register("jobs.list", jobs_list)
    dispatcher._calls = calls  # type: ignore[attr-defined]
    return dispatcher


class FrameTests(unittest.TestCase):
    def test_parse_request(self):
        frame = parse_frame(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "jobs.list", "params": {}}))
        self.assertEqual(frame["method"], "jobs.list")

    def test_reject_garbage(self):
        for raw in (b"", b"not json", b"[]", b'{"jsonrpc":"1.0","id":1,"method":"m"}',
                    b'{"jsonrpc":"2.0","id":1.5,"method":"m"}'):
            with self.assertRaises(RpcProtocolError):
                parse_frame(raw)

    def test_reject_oversized(self):
        big = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "m", "params": {"x": "y" * (MAX_MESSAGE_BYTES + 10)}})
        with self.assertRaises(RpcProtocolError):
            parse_frame(big.encode())

    def test_notification_has_no_id(self):
        frame = parse_frame(json.dumps(notification_message("system.health")))
        self.assertNotIn("id", frame)


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_hello_negotiates_and_lists_methods(self):
        dispatcher = _dispatcher()
        session = RpcSession()
        reply = await dispatcher.handle_frame(
            request_message(1, "system.hello", {"protocol_version": "1.0", "client": "t"}), session
        )
        assert reply is not None
        self.assertIn("jobs.list", reply["result"]["methods"])
        self.assertTrue(session.negotiated)

    async def test_methods_require_negotiation(self):
        dispatcher = _dispatcher()
        session = RpcSession()
        reply = await dispatcher.handle_frame(request_message(1, "jobs.list", {}), session)
        assert reply is not None
        self.assertEqual(reply["error"]["data"]["code"], ErrorCode.AUTH_REQUIRED)

    async def test_major_version_mismatch_fails_closed(self):
        dispatcher = _dispatcher()
        reply = await dispatcher.handle_frame(
            request_message(1, "system.hello", {"protocol_version": "2.0"}), RpcSession()
        )
        assert reply is not None
        self.assertEqual(reply["error"]["data"]["code"], ErrorCode.UNSUPPORTED_CAPABILITY)

    async def test_registered_method_roundtrip(self):
        dispatcher = _dispatcher()
        session = RpcSession()
        await dispatcher.handle_frame(
            request_message(1, "system.hello", {"protocol_version": "1.0"}), session
        )
        reply = await dispatcher.handle_frame(request_message(2, "jobs.list", {}), session)
        assert reply is not None
        self.assertEqual(reply["result"], {"jobs": []})
        self.assertEqual(dispatcher._calls, [("jobs.list", {})])  # type: ignore[attr-defined]

    async def test_unknown_method(self):
        dispatcher = _dispatcher()
        session = RpcSession()
        await dispatcher.handle_frame(
            request_message(1, "system.hello", {"protocol_version": "1.0"}), session
        )
        reply = await dispatcher.handle_frame(request_message(2, "grants.create", {}), session)
        assert reply is not None
        self.assertEqual(reply["error"]["code"], RPC_METHOD_NOT_FOUND)

    async def test_notification_yields_none(self):
        dispatcher = _dispatcher()
        session = RpcSession()
        reply = await dispatcher.handle_frame(
            notification_message("jobs.list"), session
        )
        self.assertIsNone(reply)

    async def test_handler_exception_becomes_internal_error(self):
        dispatcher = RpcDispatcher()

        async def boom(params: dict, session: RpcSession) -> dict:
            raise ValueError("secret detail should not leak")

        dispatcher.register("jobs.list", boom)
        session = RpcSession()
        session.negotiated = True
        reply = await dispatcher.handle_frame(request_message(9, "jobs.list", {}), session)
        assert reply is not None
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertNotIn("secret", reply["error"]["message"])

    def test_register_rejects_non_frozen_method(self):
        dispatcher = RpcDispatcher()
        with self.assertRaises(Exception):
            dispatcher.register("grants.create", _dispatcher)  # type: ignore[arg-type]

    def test_frozen_method_set_matches_spec(self):
        # SPEC §14.2 method list — the generated TS client is checked
        # against the same tuple in test_client_ts.py.
        self.assertEqual(
            len(PROACTIVE_RPC_METHODS), 20
        )


if __name__ == "__main__":
    unittest.main()
