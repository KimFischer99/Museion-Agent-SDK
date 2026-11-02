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
sys.path.insert(0, str(ROOT / "tests"))
from examples.release_app import build_agent
from proactive_sdk import ErrorCode, FakeClock, JobSpec, PASError, ProactiveAgent
from proactive_sdk.config import PasConfig
from proactive_sdk.policy import PolicyConfig
from proactive_sdk.proactive import ProactiveController
from proactive_sdk.launcher import (
    _configure, collect_settings, load_settings, main, readiness, save_settings,
)
from p3_fixtures import ScriptedModel, ScriptedSource, T0, static_batch
from test_coordinator import admit_job_event, make_coordinator


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
                first_source = agent.jobs_provenance("first-demo")["current"]
                self.assertEqual(first_source["entry_kind"], "launcher")
                self.assertEqual(first_source["authenticated_actor"], "launcher")
                await _configure(agent, [], False, True)
                self.assertEqual(agent.jobs_get("first-demo").revision, 2)
                self.assertEqual(
                    [row["revision"] for row in agent.jobs_provenance("first-demo")["revisions"]],
                    [1, 2],
                )
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


class StageAManualTodoTests(unittest.IsolatedAsyncioTestCase):
    async def test_todos_escalate_recover_and_resolve_per_item(self):
        with tempfile.TemporaryDirectory() as root:
            clock = FakeClock(wall_ms=T0)
            agent = ProactiveAgent(
                state_dir=root,
                executor=object(),
                clock=clock,
                timezone="UTC",
                locale="zh-CN",
            )
            agent.proactive = ProactiveController(agent)
            try:
                def job_spec(job_id: str, obligation: str = "opportunistic") -> JobSpec:
                    return JobSpec(
                        job_id=job_id,
                        mode="heartbeat",
                        schedule={
                            "kind": "interval",
                            "anchor": "2025-10-09T00:00:00Z",
                            "every_seconds": 1800,
                        },
                        task={"instruction": "检查测试来源。"},
                        obligation=obligation,
                    )

                watched = job_spec("hb-a")
                agent.jobs_upsert(watched, idempotency_key="stage-a-hb-a")
                other = job_spec("hb-b")
                agent.jobs_upsert(other, idempotency_key="stage-a-hb-b")
                due = job_spec("due-a", obligation="due")
                agent.jobs_upsert(due, idempotency_key="stage-a-due-a")

                failures = [
                    PASError(ErrorCode.PROVIDER_UNAVAILABLE, f"source down {i}", retryable=True)
                    for i in range(3)
                ]
                source = ScriptedSource(failures + [static_batch(items=[], cursor="cursor-ok")])
                agent.coordinator = make_coordinator(
                    agent.store, ScriptedModel([]), source
                )

                def current_categories() -> list[str]:
                    return [
                        item["category"]
                        for item in agent.current_issues()["items"]
                    ]

                # One or two retryable source faults are diagnostics, not a
                # human todo.  Another job's success must not clear them.
                for index in range(2):
                    now = T0 + index * 1_800_000
                    admit_job_event(agent.store, watched, observed_ms=now)
                    report = await agent.coordinator.process_pending_run(now_ms=now)
                    self.assertEqual(report.outcome, "suppressed")
                    self.assertTrue(report.retryable)
                    self.assertNotIn("source_unavailable", current_categories())
                agent.store.record_job_activity(
                    job_id="hb-b",
                    job_revision=1,
                    phase="analysis",
                    state="proposed",
                    obligation="opportunistic",
                    reason="unrelated success",
                    dedupe_suffix="analysis:success",
                    now_ms=T0 + 2_000_000,
                )
                self.assertNotIn("source_unavailable", current_categories())

                # The third consecutive real source failure is promoted.
                now = T0 + 2 * 1_800_000
                admit_job_event(agent.store, watched, observed_ms=now)
                third = await agent.coordinator.process_pending_run(now_ms=now)
                self.assertEqual(third.outcome, "suppressed")
                source_item = next(
                    item for item in agent.current_issues()["items"]
                    if item["category"] == "source_unavailable"
                )
                self.assertEqual(source_item["job_id"], "hb-a")
                failed_run_id = source_item["run_id"]

                # A due-obligation failure is visible immediately.
                agent.store.record_job_activity(
                    job_id="due-a",
                    job_revision=1,
                    phase="analysis",
                    state="failed",
                    obligation="due",
                    reason="按约任务失败",
                    retryable=True,
                    dedupe_suffix="analysis:due-failed",
                    now_ms=T0 + 2_100_000,
                )
                self.assertIn("job_failed", current_categories())

                # A valid approval, an expired-but-blocking approval, and
                # an unknown delivery are three independent items.
                grant = agent.grant("calendar.write", evidence="consent:stage-a")
                valid = agent.store.create_approval(
                    request={"action": "valid"},
                    grant_id=grant.grant_id,
                    ttl_ms=60_000,
                    now_ms=T0,
                )
                expired = agent.store.create_approval(
                    request={"action": "expired"},
                    grant_id=grant.grant_id,
                    ttl_ms=1,
                    now_ms=T0 - 10,
                )
                event_id = agent.store.admit_event(
                    "stage-a-approval-run",
                    origin="manual",
                    payload={"job_id": "hb-a", "reason": "status fixture"},
                    observed_at_ms=T0,
                    expires_at_ms=T0 + 100_000,
                    job_id="hb-a",
                    job_revision=1,
                )
                run_id = next(
                    run["run_id"]
                    for run in agent.store.list_runs()
                    if run["event_id"] == event_id
                )
                # The fixture is only here to give the action a real run
                # link; settle its queued run so it cannot be mistaken for
                # the recovery pass below.
                fixture_lease = agent.store.claim_run(now_ms=T0, ttl_ms=60_000)
                self.assertIsNotNone(fixture_lease)
                agent.store.fail_run(
                    fixture_lease, error_class="fixture_cleanup", now_ms=T0
                )
                agent.store.db.execute(
                    """INSERT INTO actions(
                           action_id, run_id, business_key, kind, request_json,
                           request_hash, grant_id, grant_version, approval_id,
                           policy_version, state, expires_at_ms, source,
                           obligation, created_at_ms)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "act-stage-a-expired", run_id, "bk-stage-a-expired",
                        "request_external_action", "{}", "hash-expired",
                        grant.grant_id, grant.version, expired.approval_id,
                        1, "planned", T0 + 100_000, "agent", "opportunistic", T0,
                    ),
                )
                agent.store.db.execute(
                    """INSERT INTO actions(
                           action_id, run_id, business_key, kind, request_json,
                           request_hash, grant_id, grant_version, approval_id,
                           policy_version, state, expires_at_ms, source,
                           obligation, created_at_ms)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "act-stage-a-unknown", run_id, "bk-stage-a-unknown",
                        "notify_self", "{}", "hash-unknown",
                        grant.grant_id, grant.version, None,
                        1, "queued", T0 + 100_000, "agent", "opportunistic", T0,
                    ),
                )
                agent.store.db.execute(
                    """INSERT INTO outbox(
                           message_id, action_id, delivery_key, destination_ref,
                           payload_ref, state, not_before_ms, expires_at_ms,
                           created_at_ms, reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "msg-stage-a-unknown", "act-stage-a-unknown",
                        "dk-stage-a-unknown", "local-inbox:personal",
                        "blob:stage-a", "delivery_unknown", T0,
                        T0 + 100_000, T0, "provider_timeout",
                    ),
                )

                outbox_before = len(agent.store.list_outbox())
                inbox_before = len(agent.inbox_list())
                status = agent.status()
                self.assertEqual(len(agent.store.list_outbox()), outbox_before)
                self.assertEqual(len(agent.inbox_list()), inbox_before)
                todo_items = status["manual_todos"]["items"]
                categories = [item["category"] for item in todo_items]
                bounded = agent.store.current_issues(now_ms=T0, limit=1)
                self.assertEqual(bounded["returned"], 1)
                self.assertEqual(bounded["limit"], 1)
                self.assertTrue(bounded["truncated"])
                self.assertIn("approval_pending", categories)
                self.assertIn("delivery_unknown", categories)
                self.assertIn("approval_blocked", categories)
                self.assertIn("source_unavailable", categories)
                self.assertIn("job_failed", categories)
                self.assertNotIn(
                    expired.approval_id,
                    [
                        item.get("approval_id")
                        for item in todo_items
                        if item["category"] == "approval_pending"
                    ],
                )
                self.assertIn(
                    expired.approval_id,
                    [
                        item.get("approval_id")
                        for item in todo_items
                        if item["category"] == "approval_blocked"
                    ],
                )
                self.assertEqual(
                    [approval.approval_id for approval in agent.approvals_pending()],
                    [valid.approval_id],
                )
                # The launcher consumes the same bounded projection.
                readiness_report = readiness(agent, {"environment": {}})
                self.assertEqual(readiness_report["state"], "attention")
                self.assertEqual(
                    [item["object_ref"] for item in readiness_report["manual_todos"]["items"]],
                    [item["object_ref"] for item in todo_items],
                )

                # An unrelated job's success and elapsed time do not clear
                # an unresolved delivery-unknown item.
                agent.store.record_job_activity(
                    job_id="hb-b",
                    job_revision=1,
                    phase="analysis",
                    state="proposed",
                    obligation="opportunistic",
                    reason="another unrelated success",
                    dedupe_suffix="analysis:success-2",
                    now_ms=T0 + 3_100_000,
                )
                self.assertIn("delivery_unknown", current_categories())
                self.assertIn(
                    "delivery_unknown",
                    [
                        item["category"]
                        for item in agent.store.current_issues(
                            now_ms=T0 + 365 * 24 * 3600 * 1000
                        )["items"]
                    ],
                )

                # Resolving the valid approval removes exactly that item.
                resolved = agent.approvals_resolve(
                    valid.approval_id, approve=True, actor="stage-a-test"
                )
                self.assertEqual(resolved.state, "approved")
                self.assertNotIn(
                    valid.approval_id,
                    [
                        item.get("approval_id")
                        for item in agent.current_issues()["items"]
                        if item["category"] == "approval_pending"
                    ],
                )

                # An authoritative delivery result clears only that item.
                self.assertEqual(
                    agent.store.reconcile_outbox_message(
                        "msg-stage-a-unknown", delivered=True, now_ms=T0 + 3_000_000
                    ),
                    "reconciled_delivered",
                )
                self.assertNotIn("delivery_unknown", current_categories())

                # A real successful source check clears the same job's
                # failure streak; old rows remain queryable as history.
                now = T0 + 3 * 1_800_000
                admit_job_event(agent.store, watched, observed_ms=now)
                recovered = await agent.coordinator.process_pending_run(now_ms=now)
                self.assertEqual(recovered.outcome, "suppressed")
                self.assertEqual(recovered.reason, "l0_no_source_change")
                self.assertNotIn("source_unavailable", current_categories())
                self.assertTrue(
                    any(
                        row["state"] == "suppressed" and row["retryable"]
                        for row in agent.store.job_activity("hb-a", phase="analysis")
                    )
                )
                self.assertIn(
                    "source_check_retryable_failure",
                    [row["kind"] for row in agent.store.run_events(failed_run_id)],
                )
                self.assertIn("approval_blocked", current_categories())

                # Authorization loss is actionable immediately, not after a
                # three-failure threshold.
                unauthorized_job = job_spec("unauth-a")
                agent.jobs_upsert(
                    unauthorized_job, idempotency_key="stage-a-unauth-a"
                )
                agent.coordinator = make_coordinator(
                    agent.store,
                    ScriptedModel([]),
                    ScriptedSource([
                        PASError(
                            ErrorCode.PERMISSION_DENIED,
                            "source authorization missing",
                        )
                    ]),
                )
                now = T0 + 4 * 1_800_000
                admit_job_event(agent.store, unauthorized_job, observed_ms=now)
                unauthorized = await agent.coordinator.process_pending_run(now_ms=now)
                self.assertEqual(unauthorized.outcome, "suppressed")
                self.assertEqual(unauthorized.reason, "l0_source_unauthorized")
                self.assertIn("source_unauthorized", current_categories())
            finally:
                await agent.close()


if __name__ == "__main__":
    unittest.main()
