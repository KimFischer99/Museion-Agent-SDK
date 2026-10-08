"""PAS Hermes plugin contract tests (SPEC §12.1).

The plugin is exercised through its real register(ctx) entry against a
stub ctx, and its handlers against a scripted PAS JSON-RPC server on a
real loopback HTTP socket — no external network, no model calls.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))

import pas_hermes_plugin as plugin  # noqa: E402
from pas_hermes_plugin.pas_client import PasRpcClient, PluginRpcError  # noqa: E402


class StubCtx:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, *, name, toolset, schema, handler, **_kw):
        self.tools.append({"name": name, "toolset": toolset, "schema": schema,
                           "handler": handler})


class _RpcServer(threading.Thread):
    """Loopback JSON-RPC server with scripted results and request capture."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.requests: list[dict] = []
        self.fail_after: int | None = None

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                rpc = self.server.rpc_owner  # type: ignore[attr-defined]
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                rpc.requests.append(body)
                if rpc.fail_after is not None and len(rpc.requests) > rpc.fail_after:
                    self.send_response(503)
                    self.end_headers()
                    return
                method = body.get("method")
                if method == "system.hello":
                    result = {"protocol_version": "1.0", "server": "pas-test",
                              "methods": ["system.hello", "jobs.create", "jobs.list",
                                          "jobs.pause", "jobs.resume", "skills.explain"]}
                elif method == "jobs.create":
                    result = {"job_id": body["params"]["job"]["id"], "state": "scheduled"}
                elif method == "jobs.list":
                    result = {"jobs": [{"job_id": "daily-agenda"}]}
                elif method in ("jobs.pause", "jobs.resume"):
                    result = {"job_id": body["params"]["job_id"], "state": "paused"}
                elif method == "skills.explain":
                    result = {"skill_ref": body["params"].get("skill_ref"), "status": "compatible"}
                else:
                    payload = json.dumps({
                        "jsonrpc": "2.0", "id": body["id"],
                        "error": {"code": -32601, "message": "unknown method"}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.rpc_owner = self  # type: ignore[attr-defined]
        self.httpd = server
        self.url = f"http://127.0.0.1:{server.server_port}"

    def run(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.httpd.shutdown()


class PluginRegistrationTests(unittest.TestCase):
    def test_register_exposes_proactive_tools_via_official_entry(self):
        ctx = StubCtx()
        plugin.register(ctx)
        names = [tool["name"] for tool in ctx.tools]
        self.assertEqual(
            names,
            ["proactive_schedule", "proactive_status", "proactive_pause",
             "proactive_resume", "proactive_skills_inspect"],
        )
        for tool in ctx.tools:
            self.assertEqual(tool["toolset"], "proactive")
            schema = tool["schema"]
            self.assertEqual(schema["name"], tool["name"])
            self.assertEqual(schema["parameters"]["type"], "object")
        schedule = next(t for t in ctx.tools if t["name"] == "proactive_schedule")
        self.assertEqual(
            set(schedule["schema"]["parameters"]["required"]),
            {"job_id", "mode", "schedule", "instruction"},
        )


class PluginHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _RpcServer()
        self.server.start()
        self.client = PasRpcClient(self.server.url, "test-token")
        self.client.hello()
        # Rebind the module-level cached client to the scripted server.
        plugin.tools._client.cache_clear()  # type: ignore[attr-defined]
        original = plugin.tools._client  # type: ignore[attr-defined]
        plugin.tools._client = lambda: self.client  # type: ignore[attr-defined]
        self._original = original
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        plugin.tools._client = self._original  # type: ignore[attr-defined]
        self.server.stop()

    def test_schedule_lands_in_pas(self):
        reply = json.loads(plugin.handle_proactive_schedule({
            "job_id": "daily-agenda",
            "mode": "task",
            "schedule": {"kind": "daily", "local_time": "09:00", "timezone": "Europe/Berlin"},
            "instruction": "summarize the day",
        }))
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["job"]["state"], "scheduled")
        created = [r for r in self.server.requests if r.get("method") == "jobs.create"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["params"]["job"]["id"], "daily-agenda")
        # jobs.create requires an idempotency_key (SPEC §14.2); sending it is
        # what makes a replay idempotent instead of an error.
        self.assertTrue(created[0]["params"]["idempotency_key"].startswith("plugin-"))

    def test_bad_job_id_refused_locally(self):
        reply = json.loads(plugin.handle_proactive_schedule({
            "job_id": "BAD ID", "mode": "task",
            "schedule": {"kind": "interval", "every_seconds": 1800},
            "instruction": "x",
        }))
        self.assertFalse(reply["ok"])
        # Nothing reached PAS.
        self.assertFalse(any(r.get("method") == "jobs.create" for r in self.server.requests))

    def test_status_pause_resume_inspect(self):
        status = json.loads(plugin.handle_proactive_status({}))
        self.assertTrue(status["ok"] and status["jobs"][0]["job_id"] == "daily-agenda")
        paused = json.loads(plugin.handle_proactive_pause({"job_id": "daily-agenda"}))
        self.assertTrue(paused["ok"])
        resumed = json.loads(plugin.handle_proactive_resume({"job_id": "daily-agenda"}))
        self.assertTrue(resumed["ok"])
        inspected = json.loads(plugin.handle_proactive_skills_inspect({"skill_ref": "gmail"}))
        self.assertTrue(inspected["ok"] and inspected["skill"]["status"] == "compatible")

    def test_non_loopback_http_refused(self):
        with self.assertRaises(PluginRpcError):
            PasRpcClient("http://example.org", "token")
        with self.assertRaises(PluginRpcError):
            PasRpcClient("https://user:pass@example.org", "token")
        with self.assertRaises(PluginRpcError):
            PasRpcClient("http://127.0.0.1:1", "")  # missing token


if __name__ == "__main__":
    unittest.main()
