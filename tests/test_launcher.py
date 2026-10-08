from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from examples.release_app import build_agent
from proactive_sdk import PASError, FakeClock
from proactive_sdk.config import PasConfig
from proactive_sdk.policy import PolicyConfig
from proactive_sdk.launcher import (
    _configure, collect_settings, load_settings, main, readiness, save_settings,
)


class LauncherTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_controls_survive_restart_and_errors_remain_visible(self):
        environment = {"PAS_MODEL_BASE_URL": "https://model.invalid/v1",
                       "PAS_MODEL_API_KEY": "credential-never-print", "PAS_MODEL_NAME": "test"}
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, environment, clear=True):
            settings = {"state_dir": root, "timezone": "UTC", "environment": environment}
            agent = build_agent(PasConfig(state_dir=root, profile="personal", timezone="UTC", locale="zh-CN"))
            try:
                self.assertEqual(readiness(agent, settings)["state"], "waiting_interests")
                await _configure(agent, ["nasa"], False, False)
                self.assertEqual(readiness(agent, settings)["state"], "disabled")
                await agent.proactive.handle_input("启用主动通知")
                report = readiness(agent, settings)
                self.assertTrue(report["proactive_ready"])
                self.assertEqual(report["backend"]["connection"], "not_checked")
                self.assertNotIn(environment["PAS_MODEL_API_KEY"], json.dumps(report))
                with patch.object(agent, "activity_list", return_value=[
                    {"state": "suppressed", "reason": "l0_no_source_change"}
                ]):
                    self.assertIn("保持静默", readiness(agent, settings)["recent_status"])
                with patch.object(agent, "activity_list", return_value=[
                    {"state": "suppressed", "reason": "l0_source_error:provider_unavailable"}
                ]):
                    failed = readiness(agent, settings)
                    self.assertEqual(failed["state"], "attention")
                    self.assertFalse(failed["proactive_ready"])
                agent.jobs_pause("proactive-research")
                self.assertEqual(readiness(agent, settings)["state"], "paused")
                await agent.proactive.handle_input("以后别主动发消息")
            finally:
                await agent.close()
            reopened = build_agent(PasConfig(state_dir=root, profile="personal", timezone="UTC", locale="zh-CN"))
            try:
                self.assertEqual(readiness(reopened, settings)["state"], "disabled")
                self.assertFalse(reopened.jobs_get("proactive-research").enabled)
            finally:
                await reopened.close()

    async def test_demo_reminder_without_model_and_repeated_setup_revision(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            agent = build_agent(PasConfig(state_dir=root, profile="personal", timezone="UTC", locale="zh-CN"))
            try:
                await _configure(agent, [], False, True)
                self.assertEqual(agent.jobs_get("first-demo").revision, 1)
                await _configure(agent, [], False, True)
                self.assertEqual(agent.jobs_get("first-demo").revision, 2)
                with patch.object(agent.store, "clock", FakeClock(wall_ms=agent.store.clock.wall_now_ms()+6000)):
                    await agent.tick()
                self.assertIn("体验提醒已送达", agent.inbox_list()[0]["body"])
                self.assertEqual(agent.store.list_runs(), [])
                self.assertEqual(readiness(agent, {"environment": {}})["state"], "reminder_only")
            finally:
                await agent.close()

    async def test_pi_missing_files_and_long_socket_paths_block_startup(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            agent = build_agent(PasConfig(state_dir=root, profile="personal", timezone="UTC", locale="zh-CN"))
            try:
                settings = {"environment": {"PAS_EXECUTOR": "pi", "PAS_PI_COMMAND": "missing-pas-node pi_worker.ts",
                                           "PAS_PI_ENTRY": "/missing/pi/index.js"}}
                report = readiness(agent, settings)
                self.assertFalse(report["can_start"])
                self.assertEqual(report["state"], "blocked")
                with patch.object(agent, "state_dir", Path(root) / ("x"*140)):
                    self.assertTrue(any("socket" in problem for problem in readiness(agent, {"environment": {}})["problems"]))
            finally:
                await agent.close()

    async def test_no_implicit_authorization_when_adding_topics(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            agent = build_agent(PasConfig(state_dir=root, profile="personal", timezone="UTC", locale="zh-CN"))
            try:
                await _configure(agent, ["nasa"], False, False)
                self.assertEqual(agent.grants_list(), [])
                self.assertEqual(agent.jobs_list(), [])
            finally:
                await agent.close()

    async def test_reconfigure_updates_notification_and_grants_without_resuming_pause(self):
        environment = {"PAS_MODEL_BASE_URL": "https://model.invalid/v1",
                       "PAS_MODEL_API_KEY": "secret", "PAS_MODEL_NAME": "test"}
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, environment, clear=True):
            agent = build_agent(PasConfig(state_dir=root, profile="personal", timezone="UTC", locale="zh-CN"))
            try:
                await _configure(agent, ["nasa"], True, False)
                previous = agent.jobs_get("proactive-research")
                agent.jobs_pause(previous.job_id)
                agent.revoke_grant(previous.grant_refs[0])
                agent.channels.register(channel_ref="push:owner", kind="webhook", endpoint={"url":"https://hooks.invalid/push"})
                agent.policy.config = PolicyConfig(notification_profile="push:owner")
                await _configure(agent, [], True, False)
                updated = agent.jobs_get(previous.job_id)
                self.assertFalse(updated.enabled)
                self.assertEqual(updated.delivery_policy["notification_profile"], "push:owner")
                self.assertNotEqual(updated.grant_refs, previous.grant_refs)
                self.assertEqual(updated.schedule, previous.schedule)
                self.assertEqual(updated.task, previous.task)
            finally:
                await agent.close()


class SettingsTests(unittest.TestCase):
    def test_secret_config_is_private_atomic_and_unknown_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"settings.json"
            settings = {"state_dir": str(Path(root)/"state"), "timezone": "UTC",
                        "environment": {"PAS_MODEL_API_KEY": "never-echo-this-key"}}
            save_settings(path, settings)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_settings(path), settings)
            settings["environment"]["UNKNOWN_SETTING"] = "value"
            save_settings(path, settings)
            with self.assertRaises(PASError):
                load_settings(path)
            path.chmod(0o644)
            with self.assertRaises(PASError):
                load_settings(path)

    def test_model_setup_hides_credentials_and_requires_explicit_enable(self):
        previous = {"state_dir": "/unused", "timezone": "UTC", "environment": {}}
        output = io.StringIO()
        with patch("builtins.input", side_effect=["2", "https://model.invalid/v1", "test-model", "UTC", "1", "NASA", "N"]), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("getpass.getpass", return_value="hidden-key"), contextlib.redirect_stdout(output):
            settings, topics, enable, demo = collect_settings(previous)
        self.assertEqual(topics, ["nasa"])
        self.assertFalse(enable)
        self.assertFalse(demo)
        self.assertEqual(settings["environment"]["PAS_MODEL_API_KEY"], "hidden-key")
        self.assertNotIn("hidden-key", output.getvalue())

    def test_check_reuses_saved_settings_without_wizard_or_secret_output(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            root = Path(root)
            settings = {"state_dir": str(root/"state"), "timezone": "UTC", "environment": {"PAS_EXECUTOR": "model"}}
            save_settings(root/"settings.json", settings)
            output = io.StringIO()
            with patch("builtins.input", side_effect=AssertionError("wizard repeated")), contextlib.redirect_stdout(output):
                self.assertEqual(main(build_agent, root, ["check", "--json"]), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["state"], "reminder_only")
            self.assertEqual((root/"state").stat().st_mode & 0o777, 0o700)

    def test_setup_keeps_existing_pi_and_webhook_defaults(self):
        previous = {"state_dir": "/unused", "timezone": "UTC", "environment": {
            "PAS_EXECUTOR": "pi", "PAS_PI_COMMAND": "'/opt/my node' pi_worker.ts",
            "PAS_PI_ENTRY": "/opt/pi/dist/index.js", "PAS_NOTIFY_URL": "https://hooks.invalid/private-token",
        }}
        output = io.StringIO()
        with patch("builtins.input", side_effect=["", "", "", "", "", "", "N"]), \
             patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(output):
            settings, topics, enable, demo = collect_settings(previous)
        self.assertEqual(settings["environment"]["PAS_EXECUTOR"], "pi")
        self.assertIn("/opt/my node", settings["environment"]["PAS_PI_COMMAND"])
        self.assertEqual(settings["environment"]["PAS_NOTIFY_URL"], previous["environment"]["PAS_NOTIFY_URL"])
        self.assertNotIn("private-token", output.getvalue())

    def test_running_instance_blocks_setup_before_settings_or_consent_change(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            root = Path(root)
            state = root/"state"
            state.mkdir(mode=0o700)
            (state/"daemon.lock").write_text(f"pid={os.getpid()}\n")
            settings = {"state_dir": str(state), "timezone": "UTC", "environment": {}}
            path = root/"settings.json"
            save_settings(path, settings)
            before = path.read_bytes()
            errors = io.StringIO()
            with patch("proactive_sdk.launcher.collect_settings", return_value=(settings, ["nasa"], True, False)), contextlib.redirect_stderr(errors):
                self.assertEqual(main(build_agent, root, ["setup"]), 2)
            self.assertIn("已有运行实例", errors.getvalue())
            self.assertEqual(path.read_bytes(), before)
            agent = build_agent(PasConfig(state_dir=str(state), profile="personal", timezone="UTC", locale="zh-CN"))
            try:
                self.assertEqual(agent.grants_list(), [])
                self.assertEqual(agent.store.recall_memory(), ())
            finally:
                asyncio.run(agent.close())


if __name__ == "__main__":
    unittest.main()
