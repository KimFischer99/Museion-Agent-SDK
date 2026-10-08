from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from examples import release_app  # noqa: E402
from proactive_sdk import ErrorCode, Job, OpenAICompatibleModel, PASError
from proactive_sdk.config import PasConfig
from proactive_sdk.host_bridge import HostBridge
from proactive_sdk.host_drivers import HermesHostDriver, PiHostDriver
from proactive_sdk.gws import SubprocessGwsConnector
from proactive_sdk.research import NewsSearchSource


class ReleaseAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_reminder_uses_local_inbox_without_model_call(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            agent = release_app.build_agent(
                PasConfig(
                    state_dir=tmp, timezone="UTC", profile="release-test", locale="zh-CN"
                )
            )
            with patch.object(
                release_app._MissingModel,
                "generate",
                new=AsyncMock(side_effect=AssertionError("model called")),
            ):
                try:
                    grant = agent.grant("notify.self")
                    due_at = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
                    agent.jobs_upsert(
                        Job(
                            id="release-reminder",
                            mode="reminder",
                            schedule={"kind": "runonce", "at": due_at},
                            grant_refs=(grant.grant_id,),
                            delivery_policy={"timezone": "UTC"},
                            reminder={"title": "Reminder", "body": "It is time", "timezone": "UTC"},
                        ),
                        idempotency_key="release-reminder-v1",
                    )
                    await agent.start()
                    for _ in range(100):
                        if agent.inbox_list():
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(agent.inbox_list()[0]["body"], "It is time")
                    self.assertEqual(agent.store.list_runs(), [])
                    self.assertTrue((Path(tmp) / "health.json").is_file())
                    await agent.stop()
                finally:
                    await agent.close()

    async def test_partial_model_environment_fails_closed_without_echoing_secret(self):
        secret = "test-secret-never-report-this"
        with patch.dict(
            os.environ,
            {"PAS_MODEL_API_KEY": secret},
            clear=True,
        ):
            with self.assertRaises(PASError) as ctx:
                release_app.build_agent(
                    PasConfig(
                        state_dir="unused", timezone="UTC", profile="test", locale="zh-CN"
                    )
                )
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)
        self.assertIn("PAS_MODEL_BASE_URL", ctx.exception.safe_message)
        self.assertIn("PAS_MODEL_NAME", ctx.exception.safe_message)
        self.assertNotIn(secret, ctx.exception.safe_message)

    async def test_full_model_configuration_builds_agent_without_network(self):
        env = {
            "PAS_MODEL_BASE_URL": "https://model.invalid/v1",
            "PAS_MODEL_API_KEY": "test-secret",
            "PAS_MODEL_NAME": "test-model",
        }
        with patch.dict(os.environ, env, clear=True):
            fake_agent = SimpleNamespace(registry=_Registry())
            with patch.object(release_app, "ProactiveAgent", return_value=fake_agent) as constructor:
                release_app.build_agent()

        kwargs = constructor.call_args.kwargs
        self.assertEqual(
            kwargs["state_dir"], Path(release_app.__file__).resolve().parent / "state"
        )
        self.assertEqual(
            (kwargs["timezone"], kwargs["profile"], kwargs["locale"]),
            ("UTC", "personal", "zh-CN"),
        )
        self.assertIsInstance(kwargs["model"], OpenAICompatibleModel)
        self.assertEqual(kwargs["model"].base_url, "https://model.invalid/v1")
        self.assertEqual(kwargs["model"].model_name, "test-model")
        self.assertIsNone(kwargs["config"])

    async def test_default_app_wires_public_research_and_controller_without_consent(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            agent = release_app.build_agent(PasConfig(
                state_dir=tmp, timezone="UTC", profile="release-test", locale="zh-CN"
            ))
            try:
                sources = {entry.source_id: entry for entry in agent.registry.entries()}
                self.assertIsInstance(sources["news-search"].source, NewsSearchSource)
                self.assertEqual(sources["news-search"].required_capability, "public.read")
                self.assertIs(agent.proactive.agent, agent)
                self.assertEqual(agent.proactive.public_topics(), ())
                self.assertEqual(agent.grants_list(), [])
                self.assertEqual(agent.jobs_list(), [])

                result = await agent.proactive.handle_input("我对火星感兴趣")
                self.assertEqual(result["applied_actions"][0]["kind"], "interest_saved")
                self.assertEqual(agent.grants_list(), [])
                self.assertEqual(agent.jobs_list(), [])
            finally:
                await agent.close()

    async def test_host_executor_modes_are_built_without_connecting(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {
                "PAS_EXECUTOR": "hermes",
                "PAS_HERMES_URL": "https://hermes.invalid",
                "PAS_HERMES_TOKEN": "test-token",
            }, clear=True):
                hermes = release_app.build_agent(PasConfig(
                    state_dir=Path(tmp) / "hermes", timezone="UTC", profile="h-test", locale="en"
                ))
            try:
                self.assertIsInstance(hermes.executor, HostBridge)
                self.assertIsInstance(hermes.executor.driver, HermesHostDriver)
                self.assertEqual((await hermes.executor.context()).capabilities, frozenset())
                await hermes.proactive.handle_input("我关注NASA公开新闻")
                hermes.proactive.enable()
                self.assertIn("public.read", (await hermes.executor.context()).capabilities)
                with patch.object(hermes.executor.driver, "close", new=AsyncMock()) as close:
                    await hermes.close()
                    await hermes.close()
                    close.assert_awaited_once()
            finally:
                await hermes.close()

            with patch.dict(os.environ, {
                "PAS_EXECUTOR": "pi",
                "PAS_PI_COMMAND": "node /tmp/pi_worker.ts",
                "PAS_PI_ENTRY": "/tmp/pi/index.js",
                "PAS_PI_CWD": tmp,
            }, clear=True):
                pi = release_app.build_agent(PasConfig(
                    state_dir=Path(tmp) / "pi-state", timezone="UTC", profile="p-test", locale="en"
                ))
            try:
                self.assertIsInstance(pi.executor, HostBridge)
                self.assertIsInstance(pi.executor.driver, PiHostDriver)
                self.assertEqual(pi.executor.driver.executor._config.allowed_tools, ())
            finally:
                await pi.close()

    async def test_trusted_webhook_and_optional_gmail_source_are_bound_from_env(self):
        env = {
            "PAS_NOTIFY_URL": "https://hooks.invalid/personal",
            "PAS_NOTIFY_CHANNEL": "push:release",
            "PAS_NOTIFY_HOST": "hooks.invalid",
            "PAS_GWS_COMMAND": json.dumps(["/usr/bin/hatch_gws_cli"]),
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            agent = release_app.build_agent(PasConfig(
                state_dir=tmp, timezone="UTC", profile="release-test", locale="zh-CN"
            ))
            try:
                source = next(item for item in agent.registry.entries() if item.source_id == "gmail")
                self.assertEqual(source.required_capability, "gmail.read")
                self.assertEqual(source.account_ref, "account:primary")
                self.assertIsInstance(source.source._adapter._connector, SubprocessGwsConnector)
                self.assertEqual(source.source._adapter._connector.command, ("/usr/bin/hatch_gws_cli",))
                self.assertEqual(agent.grants_list(), [])
                self.assertEqual(agent.policy.config.notification_profile, "push:release")
                channel = agent.store.get_owner_channel("push:release")
                self.assertEqual(channel["endpoint"], {"url": env["PAS_NOTIFY_URL"]})
                self.assertIsInstance(agent.dispatcher._sinks["webhook"], release_app.WebhookNotificationSink)
            finally:
                await agent.close()


class _Registry:
    def __init__(self):
        self.entries_seen = []

    def register(self, **kwargs):
        self.entries_seen.append(kwargs)


if __name__ == "__main__":
    unittest.main()
