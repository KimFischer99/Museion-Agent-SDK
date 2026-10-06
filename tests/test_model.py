"""P3 ModelPort tests: the OpenAI-compatible adapter against a local
scripted HTTP server (transport-level contract), plus usage normalization
and error mapping (SPEC §4.2, §8.2; EXEC-01).

Boundary (AGENTS.md): these tests prove the adapter's request/response
logic over real HTTP loopback. They do NOT prove compatibility with any
deployed provider release — live-provider validation stays an open gate
and must not be claimed from this file.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import ErrorCode, ModelRequest, PASError
from proactive_sdk.model import OpenAICompatibleModel

SECRET = "test-key-not-a-real-credential"


def chat_completion_body(*, content=None, tool_calls=None, usage=None):
    message: dict = {}
    if content is not None:
        message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {"choices": [{"message": message}]}
    if usage is not None:
        body["usage"] = usage
    return body


class _Handler(BaseHTTPRequestHandler):
    server_version = "ScriptedP3/1"

    def respond(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        self.send_response(status)
        for key, value in (headers or {"Content-Type": "application/json"}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        self.server.requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)}
        )
        script = self.server.script
        if isinstance(script, Exception):
            raise script
        status, payload, headers = script
        self.respond(status, json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload, headers)

    def log_message(self, *args):  # silence test output
        pass


class ScriptedServer:
    """Local loopback server scripted with one response."""

    def __init__(self, script):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.script = script
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def make_model(server: ScriptedServer) -> OpenAICompatibleModel:
    return OpenAICompatibleModel(
        base_url=server.url,
        model="fixture-model-1",
        api_key_provider=lambda: SECRET,
        provider="openai-compatible-fixture",
    )


class AdapterContractTests(unittest.TestCase):
    def setUp(self):
        self._servers: list[ScriptedServer] = []

    def tearDown(self):
        for server in self._servers:
            server.stop()

    def scripted(self, script) -> ScriptedServer:
        server = ScriptedServer(script).start()
        self._servers.append(server)
        return server

    def run_async(self, coro):
        import asyncio

        return asyncio.run(coro)

    def gen(self, model, request=None):
        import asyncio

        request = request or ModelRequest(messages=({"role": "user", "content": "hi"},))
        return asyncio.run(model.generate(request))

    def test_request_shape_and_response_mapping(self):
        usage = {
            "prompt_tokens": 12,
            "completion_tokens": 34,
            "prompt_tokens_details": {"cached_tokens": 5},
        }
        server = self.scripted(
            (
                200,
                chat_completion_body(
                    content="ok",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_evidence", "arguments": '{"fact_id": "item-1"}'},
                        }
                    ],
                    usage=usage,
                ),
                None,
            )
        )
        model = make_model(server)
        request = ModelRequest(
            messages=(
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ),
            tool_schemas=[{"name": "read_evidence", "description": "d", "parameters": {}}],
        )
        response = self.gen(model, request)
        self.assertEqual(response.content, "ok")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].call_id, "call_1")
        self.assertEqual(response.tool_calls[0].arguments, {"fact_id": "item-1"})
        self.assertEqual(response.usage["input_tokens"], 12)
        self.assertEqual(response.usage["output_tokens"], 34)
        self.assertEqual(response.usage["cache_read_tokens"], 5)
        self.assertEqual(response.usage["pricing_basis"], "measured")
        sent = server.server.requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {SECRET}")
        self.assertEqual(sent["body"]["model"], "fixture-model-1")
        self.assertEqual(sent["body"]["tools"][0]["function"]["name"], "read_evidence")
        self.assertEqual(sent["body"]["messages"][0]["role"], "system")

    def test_missing_usage_stays_unknown_never_zero(self):
        server = self.scripted((200, chat_completion_body(content="ok"), None))
        response = self.gen(make_model(server))
        self.assertIsNone(response.usage["input_tokens"])
        self.assertIsNone(response.usage["output_tokens"])
        self.assertEqual(response.usage["pricing_basis"], "unknown")

    def test_error_mapping_401_429_500(self):
        cases = [
            (401, ErrorCode.AUTH_REQUIRED, False),
            (429, ErrorCode.RATE_LIMITED, True),
            (500, ErrorCode.PROVIDER_UNAVAILABLE, True),
            (400, ErrorCode.INVALID_CONFIG, False),
        ]
        for status, expected_code, expected_retryable in cases:
            with self.subTest(status=status):
                headers = {"Retry-After": "7"} if status == 429 else None
                server = self.scripted((status, {"error": {"message": "boom"}}, headers))
                with self.assertRaises(PASError) as caught:
                    self.gen(make_model(server))
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(caught.exception.retryable, expected_retryable)
                if status == 429:
                    self.assertEqual(caught.exception.retry_after_s, 7)
                # Bodies are dropped, not forwarded into safe messages.
                self.assertNotIn("boom", caught.exception.safe_message)
                self.assertNotIn(SECRET, caught.exception.safe_message)

    def test_non_json_and_bad_tool_arguments_map_to_provider_unavailable(self):
        server = self.scripted((200, b"not json at all", {"Content-Type": "text/plain"}))
        with self.assertRaises(PASError) as caught:
            self.gen(make_model(server))
        self.assertEqual(caught.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

        server2 = self.scripted(
            (
                200,
                chat_completion_body(
                    tool_calls=[
                        {"id": "c1", "function": {"name": "t", "arguments": "{not json"}}
                    ]
                ),
                None,
            )
        )
        with self.assertRaises(PASError) as caught:
            self.gen(make_model(server2))
        self.assertEqual(caught.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_connection_refused_maps_to_provider_unavailable(self):
        # Port 1 on loopback is closed in any sane test environment.
        model = OpenAICompatibleModel(
            base_url="http://127.0.0.1:1/v1",
            model="m",
            api_key_provider=lambda: SECRET,
            timeout_s=1.0,
        )
        with self.assertRaises(PASError) as caught:
            self.gen(model)
        self.assertEqual(caught.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
        self.assertTrue(caught.exception.retryable)

    def test_adapter_namespace_stays_in_request(self):
        server = self.scripted((200, chat_completion_body(content="ok"), None))
        model = make_model(server)
        request = ModelRequest(
            messages=({"role": "user", "content": "hi"},),
            adapter_namespace={"openai": {"temperature": 0.2}},
        )
        self.gen(model, request)
        self.assertEqual(server.server.requests[0]["body"].get("temperature"), 0.2)

    def test_reasoning_field_is_dropped(self):
        payload = chat_completion_body(content="ok")
        payload["choices"][0]["message"]["reasoning_content"] = "SECRET-CHAIN"
        server = self.scripted((200, payload, None))
        response = self.gen(make_model(server))
        self.assertIsNone(response.reasoning)
        self.assertNotIn("SECRET-CHAIN", repr(response))

    def test_config_validation(self):
        for kwargs in (
            {"base_url": "", "model": "m", "api_key_provider": lambda: "k"},
            {"base_url": "http://x", "model": "", "api_key_provider": lambda: "k"},
            {"base_url": "http://x", "model": "m", "api_key_provider": None},
        ):
            with self.assertRaises(PASError):
                OpenAICompatibleModel(**kwargs)


class FakeModelHonestyTests(unittest.TestCase):
    """The scripted fixture must never pass itself off as a real provider."""

    def test_scripted_fixture_labels_are_honest(self):
        import asyncio

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from p3_fixtures import ScriptedModel

        model = ScriptedModel([{"content": '{"decision":"silent","summary":"s","proposals":[]}'}])
        self.assertEqual(model.provider, "fake")
        self.assertEqual(model.model_name, "scripted-fixture")
        response = asyncio.run(
            model.generate(ModelRequest(messages=({"role": "user", "content": "x"},)))
        )
        self.assertIsNone(response.usage)  # fixture has no measured usage


if __name__ == "__main__":
    unittest.main()
