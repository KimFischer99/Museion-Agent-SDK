"""PAS Hermes plugin contract tests (SPEC §12.1).

The plugin is exercised through its real register(ctx) entry against a
stub ctx, and its handlers against a scripted PAS JSON-RPC server on a
real loopback HTTP socket — no external network, no model calls.
"""

from __future__ import annotations

import json
import asyncio
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

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


class TrustedInputHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_owner_message_reaches_trusted_input_and_reply(self):
        env = {"PAS_HERMES_PLATFORM": "telegram", "PAS_HERMES_USER": "owner",
               "PAS_HERMES_CHAT": "dm-1", "PAS_HERMES_PROFILE": "demo"}
        source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), user_id="owner",
                                 chat_id="dm-1", profile="demo", chat_type="dm")
        event = SimpleNamespace(source=source, message_id="message-1", text="每天 09:00 提醒我：喝水")
        done = asyncio.Event()

        async def send(chat_id, reply):
            done.set()

        adapter = SimpleNamespace(send=AsyncMock(side_effect=send))
        gateway = SimpleNamespace(_is_user_authorized=MagicMock(return_value=True),
                                  _adapter_for_source=MagicMock(return_value=adapter))
        client = SimpleNamespace(proactive_input=MagicMock(return_value={"reply": "已安排"}))
        with patch.dict(os.environ, env, clear=True), patch.object(plugin.tools, "_client", return_value=client):
            result = plugin._trusted_input(event=event, gateway=gateway)
            await asyncio.wait_for(done.wait(), timeout=2)
        self.assertEqual(result["action"], "skip")
        self.assertEqual(client.proactive_input.call_args.args[0], event.text)
        self.assertEqual(len(client.proactive_input.call_args.args[1]), 64)
        adapter.send.assert_awaited_once_with("dm-1", "已安排")

    async def test_gateway_rejected_sender_never_reaches_pas(self):
        env = {"PAS_HERMES_PLATFORM": "telegram", "PAS_HERMES_USER": "owner",
               "PAS_HERMES_CHAT": "dm-1", "PAS_HERMES_PROFILE": "demo"}
        source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), user_id="owner",
                                 chat_id="dm-1", profile="demo", chat_type="dm")
        event = SimpleNamespace(source=source, message_id="message-1", text="每天 09:00 提醒我：喝水")
        gateway = SimpleNamespace(_is_user_authorized=MagicMock(return_value=False),
                                  _adapter_for_source=MagicMock())
        with patch.dict(os.environ, env, clear=True), patch.object(plugin.tools, "_client") as client:
            self.assertIsNone(plugin._trusted_input(event=event, gateway=gateway))
        gateway._adapter_for_source.assert_not_called()
        client.assert_not_called()


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
        self.assertEqual(created[0]["params"]["job"]["instruction"], "summarize the day")
        self.assertFalse(created[0]["params"]["job"]["always_run"])
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


class NativeUnixPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_against_real_daemon_survives_restart(self):
        from examples.release_app import build_agent
        from proactive_sdk.config import PasConfig
        from proactive_sdk.rpc_server import ControlPlaneServer

        with tempfile.TemporaryDirectory(dir="/tmp") as tmp, patch.dict(os.environ, {}, clear=True):
            agent = build_agent(PasConfig(state_dir=tmp, profile="plugin-test", timezone="UTC", locale="en"))
            server = ControlPlaneServer(agent, socket_path=Path(tmp) / "pas.sock", token="test-token")
            await server.start()
            client = PasRpcClient(f"unix://{server.socket_path}", "test-token")
            try:
                with patch("proactive_sdk.rpc_server.peer_uid", return_value=None), patch.object(
                    plugin.tools, "_client", return_value=client,
                ):
                    await asyncio.to_thread(client.hello)
                    reply = json.loads(await asyncio.to_thread(plugin.handle_proactive_schedule, {
                        "job_id": "native-schedule", "mode": "task",
                        "schedule": {"kind": "interval", "anchor": "2026-10-19T00:00:00Z", "every_seconds": 3600},
                        "instruction": "stay silent", "always_run": True,
                    }))
                    self.assertTrue(reply["ok"], reply)
                    job = agent.jobs_get("native-schedule")
                    self.assertEqual(job.task["instruction"], "stay silent")
                    self.assertTrue(job.task["always_run"])
                    await server.close()
                    await server.start()
                    listed = await asyncio.to_thread(client.jobs_list)
                    self.assertEqual(listed["jobs"][0]["job_id"], "native-schedule")
            finally:
                await server.close()
                await agent.close()


if __name__ == "__main__":
    unittest.main()
