"""P7 productization tests, part 1: config, observability, backup,
facade lifecycle (SPEC §14 / §17; OPS-01)."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from p3_fixtures import T0, make_executor, rfc3339  # noqa: E402

from proactive_sdk import (  # noqa: E402
    FakeClock,
    Job,
    PASError,
    ProactiveAgent,
    build_config,
    config_to_yaml_subset,
    contains_unredacted_secret,
    load_config,
    parse_config_text,
    redact_text,
)
from proactive_sdk.backup import (  # noqa: E402
    BackupError,
    create_backup,
    read_backup_meta,
    restore_backup,
)
from proactive_sdk.config import ConfigError  # noqa: E402
from proactive_sdk.observability import (  # noqa: E402
    Metrics,
    StructuredLogger,
    health_snapshot,
)


class _FakeSource:
    source_id = "cal"
    account_ref = "account:primary"
    required_capability = "calendar.read"

    async def fetch_delta(self, request):
        from proactive_sdk.contracts import SourceBatch

        return SourceBatch(
            source_id=request.source_id,
            account_ref=request.account_ref,
            observed_at=rfc3339(T0),
            cursor_ref=request.cursor_ref or "c1",
            items=(),
        )


def _make_agent(tmp: str, *, profile: str = "p7a", clock=None) -> ProactiveAgent:
    agent = ProactiveAgent(
        state_dir=tmp,
        executor=make_executor(_SilentModel(), clock=clock),
        sources=(_FakeSource(),),
        timezone="Europe/Berlin",
        locale="zh-CN",
        profile=profile,
        clock=clock,
    )
    agent.grants.create(
        capability="calendar.read",
        account_ref="account:primary",
        scope={"resource_ids": ["primary"]},
        consent_evidence_ref="consent:p7",
        now_ms=clock.wall_now_ms() if clock is not None else T0,
    )
    return agent


class _SilentModel:
    provider = "fake"

    async def generate(self, *_a, **_k):
        from proactive_sdk.contracts import ModelResponse

        return ModelResponse(
            content=json.dumps(
                {"decision": "silent", "summary": "p7 test silent", "proposals": []}
            ),
            tool_calls=[],
            usage=None,
        )


def _clock_at_T0() -> FakeClock:
    return FakeClock(wall_ms=T0)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


class ConfigTests(unittest.TestCase):
    BASE = (
        'config_version: "1"\n'
        "profile: demo\n"
        "timezone: Europe/Berlin\n"
        "locale: zh-CN\n"
        "runtime:\n"
        "  max_concurrent_agent_runs: 1\n"
        "  shutdown_grace_seconds: 20\n"
        "heartbeat:\n"
        "  enabled: true\n"
        "  every_seconds: 1800\n"
        "  misfire: coalesce_latest\n"
        'policy:\n'
        '  notification_window: {start: "09:00", end: "21:30"}\n'
        "  allow_untrusted_shell_skills: false\n"
    )

    def test_full_config_loads_with_defaults(self):
        config = build_config(parse_config_text(self.BASE))
        self.assertEqual(config.profile, "demo")
        self.assertEqual(config.timezone, "Europe/Berlin")
        self.assertTrue(config.heartbeat.enabled)
        self.assertEqual(config.policy.notification_window, ("09:00", "21:30"))
        self.assertEqual(config.runtime.loop_interval_seconds, 5.0)
        self.assertTrue(config.control_plane.enabled)

    def test_unknown_top_level_key_rejected(self):
        raw = parse_config_text(self.BASE + "\nbogus: 1\n")
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("bogus", str(ctx.exception))

    def test_unknown_nested_key_rejected(self):
        raw = parse_config_text(self.BASE + "  also_bogus: 2\n")
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("policy", str(ctx.exception))
        self.assertIn("also_bogus", str(ctx.exception))

    def test_bad_timezone_rejected(self):
        raw = parse_config_text(self.BASE.replace("Europe/Berlin", "Mars/Olympus"))
        with self.assertRaises(ConfigError):
            build_config(raw)

    def test_bad_notification_window_rejected(self):
        raw = parse_config_text(
            self.BASE.replace('{start: "09:00", end: "21:30"}',
                              '{start: "9am", end: "21:30"}')
        )
        with self.assertRaises(ConfigError):
            build_config(raw)

    def test_bad_misfire_rejected(self):
        raw = parse_config_text(self.BASE.replace("coalesce_latest", "yolo"))
        with self.assertRaises(ConfigError):
            build_config(raw)

    def test_duplicate_key_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("profile: a\nprofile: b\n")

    def test_tabs_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("runtime:\n\tmax: 1\n")

    def test_redaction_hides_paths_and_secret_keys(self):
        raw = parse_config_text(
            self.BASE.replace("state_dir", "state_dir")  # keep absent; add explicitly below
        )
        # rebuild with a home path and a token_file
        text = self.BASE + "control_plane:\n  token_file: /Users/someone/.pas/token.txt\n"
        config = build_config(parse_config_text(text))
        view = json.loads(json.dumps(config.redacted()))
        flat = json.dumps(view)
        self.assertNotIn("/Users/someone", flat)
        self.assertIn("<redacted>", flat)

    def test_roundtrip_yaml_subset(self):
        config = build_config(parse_config_text(self.BASE))
        rendered = config_to_yaml_subset(config)
        reparsed = build_config(parse_config_text(rendered))
        self.assertEqual(reparsed.profile, config.profile)
        self.assertEqual(reparsed.policy.notification_window, config.policy.notification_window)

    def test_load_config_missing_file_is_error_not_guess(self):
        with self.assertRaises(ConfigError):
            load_config("/nonexistent/pas.yaml")


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #


class ObservabilityTests(unittest.TestCase):
    def test_log_line_is_structured_and_redacted(self):
        stream = io.StringIO()
        logger = StructuredLogger(stream=stream, component="pas.test", fields={"profile": "p"})
        logger.info(
            "run_finished", run_id="run1", event_id="evt1", action_id="act1",
            job_id="job1", phase="proposed", duration_ms=12,
            reason="auth with Authorization: Bearer abc123.def456",
        )
        record = json.loads(stream.getvalue().splitlines()[-1])
        self.assertEqual(record["run_id"], "run1")
        self.assertEqual(record["event_id"], "evt1")
        self.assertEqual(record["action_id"], "act1")
        self.assertEqual(record["phase"], "proposed")
        self.assertIn("[REDACTED]", record["reason"])
        self.assertFalse(contains_unredacted_secret(record["reason"]))

    def test_raw_reason_never_hits_disk_unredacted(self):
        stream = io.StringIO()
        logger = StructuredLogger(stream=stream)
        logger.warning("leak_probe", reason="sk-abcdefghijklmnop1234")
        out = stream.getvalue()
        self.assertNotIn("sk-abcdefghijklmnop1234", out)
        self.assertFalse(contains_unredacted_secret(out))

    def test_metrics_names_match_spec(self):
        metrics = Metrics()
        metrics.inc("wake", 3)
        metrics.inc("suppressed")
        metrics.inc("model_calls", 2)
        metrics.inc("tool_denied")
        metrics.inc("grant_revoked")
        metrics.set_gauge("outbox_pending", 4)
        metrics.set_gauge("delivery_unknown", 1)
        metrics.set_gauge("scheduler_lateness_ms", 900)
        snap = metrics.snapshot()
        for name in ("wake", "suppressed", "model_calls", "tool_denied",
                     "outbox_pending", "delivery_unknown", "grant_revoked",
                     "scheduler_lateness_ms"):
            self.assertIn(name, snap)
        self.assertEqual(snap["wake"], 3)
        self.assertEqual(snap["outbox_pending"], 4)

    def test_health_snapshot_degraded_reasons(self):
        healthy = health_snapshot(
            db_ok=True, disk_free_mb=1000, disk_free_warn_mb=50,
            unknown_deliveries=0, scheduler_age_ms=100, scheduler_stale_after_ms=60000,
        )
        self.assertTrue(healthy["ready"])
        degraded = health_snapshot(
            db_ok=True, disk_free_mb=10, disk_free_warn_mb=50,
            unknown_deliveries=2, scheduler_age_ms=999_999, scheduler_stale_after_ms=60_000,
        )
        self.assertFalse(degraded["ready"])
        self.assertTrue(degraded["degraded"])
        self.assertTrue(any("disk_low" in r for r in degraded["reasons"]))
        self.assertTrue(any("delivery_unknown" in r for r in degraded["reasons"]))
        self.assertTrue(any("scheduler_stale" in r for r in degraded["reasons"]))


# --------------------------------------------------------------------------- #
# Facade lifecycle
# --------------------------------------------------------------------------- #


class FacadeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_tick_job_runs_and_status_is_honest(self):
        async def scenario():
            clock = _clock_at_T0()
            agent = _make_agent(self.tmp, clock=clock)
            try:
                agent.jobs_upsert(
                    Job(
                        id="hb",
                        mode="heartbeat",
                        schedule={
                            "kind": "interval",
                            "anchor": "2025-10-09T00:00:00Z",
                            "every_seconds": 1800,
                        },
                        instruction="检查日历变化",
                    ),
                    idempotency_key="p7-tick-1",
                )
                # First tick at T0: the job's first slot may be up to one
                # interval away — admit nothing is a legal outcome. Jump
                # one interval and tick again: now exactly one run.
                clock.advance_wall(1_800_000)
                report = await agent.tick()
                self.assertEqual(len(report["runs"]), 1, report)
                self.assertEqual(report["runs"][0]["outcome"], "proposed")
                status = agent.status()
                self.assertTrue(status["health"]["alive"])
                # second tick at the same instant: no new slot, zero runs
                report2 = await agent.tick()
                self.assertEqual(report2["runs"], [])
            finally:
                await agent.close()

        asyncio.run(scenario())

    def test_pause_resume_and_manual_trigger(self):
        async def scenario():
            agent = _make_agent(self.tmp)
            try:
                agent.jobs_upsert(
                    Job(id="t1", mode="task",
                        schedule={"kind": "runonce", "at": rfc3339(T0 + 3_600_000)},
                        instruction="一次任务"),
                    idempotency_key="p7-manual-1",
                )
                record = agent.jobs_pause("t1")
                self.assertFalse(record.enabled)
                with self.assertRaises(PASError):
                    agent.trigger_job("t1")  # paused jobs refuse manual runs
                agent.jobs_resume("t1")
                event_id = agent.trigger_job("t1", reason="manual test")
                self.assertTrue(event_id.startswith("job:t1:manual:"))
            finally:
                await agent.close()

        asyncio.run(scenario())

    def test_cancel_queued_run_is_resolved_not_faked(self):
        async def scenario():
            agent = _make_agent(self.tmp)
            try:
                agent.jobs_upsert(
                    Job(id="t2", mode="task",
                        schedule={"kind": "runonce", "at": rfc3339(T0 + 3_600_000)},
                        instruction="将被取消"),
                    idempotency_key="p7-cancel-1",
                )
                agent.trigger_job("t2")
                run_id = agent.runs_list(state="queued")[0]["run_id"]
                outcome = agent.runs_cancel(run_id)
                self.assertEqual(outcome["outcome"], "cancelled")
                run = agent.runs_get(run_id)
                self.assertEqual(run["state"], "suppressed")
                self.assertTrue(run["cancel_requested"])
                # cancel of a terminal run refuses honestly
                outcome2 = agent.runs_cancel(run_id)
                self.assertEqual(outcome2["outcome"], "not_cancellable")
            finally:
                await agent.close()

        asyncio.run(scenario())

    def test_export_and_delete_data(self):
        async def scenario():
            agent = _make_agent(self.tmp)
            try:
                agent.jobs_upsert(
                    Job(id="t3", mode="heartbeat",
                        schedule={"kind": "interval",
                                  "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
                        instruction="导出测试"),
                    idempotency_key="p7-export-1",
                )
                await agent.tick()
                out_path = Path(self.tmp) / "export.json"
                summary = agent.export_data(out_path)
                self.assertGreater(summary["tables"]["jobs"], 0)
                dumped = json.loads(out_path.read_text())
                self.assertIn("jobs", dumped)
                with self.assertRaises(PASError):
                    agent.delete_data(confirm="yes")  # wrong phrase refused
                deleted = agent.delete_data(confirm="DELETE PROFILE DATA")
                self.assertGreaterEqual(deleted.get("jobs", 0), 1)
                self.assertEqual(len(agent.jobs_list()), 0)
            finally:
                await agent.close()

        asyncio.run(scenario())

    def test_context_manager_closes_store(self):
        async def scenario():
            tmp = tempfile.mkdtemp()
            async with _make_agent(tmp) as agent:
                agent.jobs_upsert(
                    Job(id="cm", mode="heartbeat",
                        schedule={"kind": "interval",
                                  "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
                        instruction="ctx"),
                    idempotency_key="p7-ctx-1",
                )
            # store closed: touching it must fail, not hang
            with self.assertRaises(Exception):
                agent.store.db.execute("SELECT 1")

        asyncio.run(scenario())

    def test_reopen_same_state_dir_is_idempotent(self):
        async def scenario():
            tmp = tempfile.mkdtemp()
            async with _make_agent(tmp, profile="reopen") as agent:
                agent.jobs_upsert(
                    Job(id="j", mode="heartbeat",
                        schedule={"kind": "interval",
                                  "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
                        instruction="reopen"),
                    idempotency_key="p7-reopen-1",
                )
            async with _make_agent(tmp, profile="reopen") as agent2:
                self.assertEqual(len(agent2.jobs_list()), 1)
                self.assertIsNotNone(agent2.store.get_owner_channel("local-inbox:reopen"))

        asyncio.run(scenario())

    def test_profile_mismatch_refused_on_reopen(self):
        async def scenario():
            tmp = tempfile.mkdtemp()
            async with _make_agent(tmp, profile="alpha"):
                pass
            with self.assertRaises(PASError):
                _make_agent(tmp, profile="beta")  # store identity binding refuses

        asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Backup / restore
# --------------------------------------------------------------------------- #


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.agent = _make_agent(str(self.tmp), profile="bk")
        self.agent.jobs_upsert(
            Job(id="bk-job", mode="heartbeat",
                schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z",
                          "every_seconds": 1800},
                instruction="备份源数据"),
            idempotency_key="p7-bk-1",
        )
        asyncio.get_event_loop_policy()
        asyncio.run(self.agent.tick())

    def tearDown(self):
        self.agent.store.close()

    def test_backup_roundtrip_preserves_rows(self):
        backup_path = self.tmp / "b.bin"
        meta = create_backup(self.agent.store, backup_path)
        self.assertEqual(meta["kind"], "pas-backup")
        self.assertEqual(meta["profile"], "bk")
        self.assertEqual(meta["schema_version"], self.agent.store.schema_version())
        on_disk = read_backup_meta(backup_path)
        self.assertEqual(on_disk["sha256"], meta["sha256"])
        self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)

        # capture row counts, restore, compare
        counts_before = {
            t: len(rows) for t, rows in self.agent.store.export_profile_data().items()
        }
        result = restore_backup(
            self.tmp / "pas.sqlite3", backup_path,
            expect_profile="bk", expect_owner_destination="local-inbox:bk",
        )
        self.assertEqual(result["profile"], "bk")
        verify = self.agent.store.export_profile_data()
        counts_after = {t: len(rows) for t, rows in verify.items()}
        self.assertEqual(counts_before, counts_after)

    def test_restore_refuses_identity_mismatch(self):
        backup_path = self.tmp / "b2.bin"
        create_backup(self.agent.store, backup_path)
        with self.assertRaises(BackupError):
            restore_backup(
                self.tmp / "pas.sqlite3", backup_path,
                expect_profile="other-profile",
            )

    def test_restore_refuses_daemon_lock(self):
        backup_path = self.tmp / "b3.bin"
        create_backup(self.agent.store, backup_path)
        (self.tmp / "daemon.lock").write_text(f"pid={1_000_000_000}\n")
        try:
            with self.assertRaises(BackupError):
                restore_backup(self.tmp / "pas.sqlite3", backup_path)
        finally:
            (self.tmp / "daemon.lock").unlink()

    def test_restore_refuses_newer_schema(self):
        backup_path = self.tmp / "b4.bin"
        meta = create_backup(self.agent.store, backup_path)
        meta_path = backup_path.with_name(backup_path.name + ".meta.json")
        forged = dict(meta)
        forged["schema_version"] = 999
        meta_path.write_text(json.dumps(forged))
        with self.assertRaises(BackupError):
            restore_backup(self.tmp / "pas.sqlite3", backup_path)

    def test_restore_refuses_tampered_payload(self):
        backup_path = self.tmp / "b5.bin"
        create_backup(self.agent.store, backup_path)
        payload = bytearray(backup_path.read_bytes())
        payload[200] ^= 0xFF
        backup_path.write_bytes(bytes(payload))
        with self.assertRaises(BackupError):
            restore_backup(self.tmp / "pas.sqlite3", backup_path)


if __name__ == "__main__":
    unittest.main()
