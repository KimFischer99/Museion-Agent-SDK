"""SPEC §22.1 item 5: tool authority is recorded, not implied.

AGENTS.md is blunt about the boundary: raw shell, network and credentials
can bypass the broker, so a host that keeps its own tools means PAS did not
constrain what that run did. The acceptance clause is "run details must
show, machine-readably, whether the run was constrained" — so the tests
below check the *ledger*, not just the object model.

Two properties matter most:

* nothing declared must read as ``host``, never as ``pas_broker``;
* rows written before v0.1.2 keep ``unknown`` forever — a schema migration
  must not retroactively upgrade the claim.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import (  # noqa: E402
    TOOL_AUTHORITIES,
    TOOL_AUTHORITY_HOST,
    TOOL_AUTHORITY_PAS_BROKER,
    TOOL_AUTHORITY_UNKNOWN,
    ErrorCode,
    ExecutorContext,
    FakeClock,
    HostBridge,
    HostReply,
    Job,
    LocalToolBroker,
    PASError,
    ProactiveAgent,
    RunLease,
    ToolLoopExecutor,
)
from proactive_sdk.store import MIGRATIONS, _split_sql_statements  # noqa: E402

T0 = 1_760_000_000_000
SILENT = {"decision": "silent", "summary": "nothing to act on", "proposals": []}


class SilentHost:
    async def capabilities(self) -> dict:
        return {}

    async def submit(self, prompt, *, timeout_s):
        return HostReply(text=json.dumps(SILENT))

    async def cancel(self, run_key: str) -> str:
        return "unsupported"

    async def close(self) -> None:
        return None


def _builtin_executor():
    return ToolLoopExecutor(
        model=type("M", (), {"generate": staticmethod(lambda *a, **k: None)})(),
        broker=LocalToolBroker(capabilities=frozenset()),
    )


class AuthorityVocabularyTests(unittest.TestCase):
    def test_the_three_values_are_the_whole_set(self):
        self.assertEqual(
            set(TOOL_AUTHORITIES),
            {TOOL_AUTHORITY_PAS_BROKER, TOOL_AUTHORITY_HOST, TOOL_AUTHORITY_UNKNOWN},
        )

    def test_an_undeclared_context_is_a_host_context(self):
        # Silence must never be read as coverage.
        self.assertEqual(ExecutorContext().tool_authority, TOOL_AUTHORITY_HOST)

    def test_a_declared_context_is_a_broker_context(self):
        ctx = ExecutorContext(external_tool_broker=True)
        self.assertEqual(ctx.tool_authority, TOOL_AUTHORITY_PAS_BROKER)

    def test_non_boolean_declarations_are_rejected(self):
        for bad in ("true", 1, None):
            with self.assertRaises(PASError):
                ExecutorContext(external_tool_broker=bad)

    def test_the_builtin_loop_reports_the_broker_as_a_fact(self):
        ctx = asyncio.run(_builtin_executor().context())
        self.assertEqual(ctx.tool_authority, TOOL_AUTHORITY_PAS_BROKER)

    def test_a_host_bridge_defaults_to_host_and_can_be_told_otherwise(self):
        default = asyncio.run(HostBridge(SilentHost()).context())
        self.assertEqual(default.tool_authority, TOOL_AUTHORITY_HOST)
        declared = asyncio.run(
            HostBridge(SilentHost(), external_tool_broker=True).context()
        )
        self.assertEqual(declared.tool_authority, TOOL_AUTHORITY_PAS_BROKER)


class AuthorityLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(wall_ms=T0)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_once(self, executor, profile: str) -> ProactiveAgent:
        agent = ProactiveAgent(
            state_dir=Path(self.tmp.name) / profile,
            executor=executor,
            clock=self.clock,
            timezone="UTC",
            locale="en",
            profile=profile,
        )
        grant = agent.create_grant_from_user_consent(
            capability="notify.self", account_ref="a", scope={}, consent_evidence_ref="c"
        )
        agent.jobs_upsert(
            Job(
                id="j",
                mode="task",
                schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 3600},
                instruction="check",
                grant_refs=(grant.grant_id,),
            ),
            idempotency_key="k",
        )
        agent.trigger_job("j", reason="t")
        asyncio.run(agent.tick())
        return agent

    def test_an_unconstrained_host_run_says_so_in_the_ledger(self):
        agent = self._run_once(HostBridge(SilentHost()), "unconstrained")
        try:
            run = agent.store.list_runs()[0]
            self.assertEqual(run["tool_authority"], TOOL_AUTHORITY_HOST)
            self.assertIn("not covered by PAS authorization", run["tool_authority_reason"])
            # and the same fact is on the detail read
            detail = agent.store.get_run(run["run_id"])
            self.assertEqual(detail["tool_authority"], TOOL_AUTHORITY_HOST)
        finally:
            asyncio.run(agent.close())

    def test_a_brokered_run_says_that_instead(self):
        agent = self._run_once(HostBridge(SilentHost()), "brokered-host")
        # Re-run the same shape through the built-in loop for the contrast.
        agent2 = self._run_once(_builtin_executor(), "brokered-builtin")
        try:
            builtin = agent2.store.list_runs()[0]
            self.assertEqual(builtin["tool_authority"], TOOL_AUTHORITY_PAS_BROKER)
            self.assertIn("PAS broker", builtin["tool_authority_reason"])
        finally:
            asyncio.run(agent2.close())
            asyncio.run(agent.close())

    def test_status_reports_the_rollup(self):
        agent = self._run_once(HostBridge(SilentHost()), "rollup")
        try:
            self.assertEqual(
                agent.status()["run_tool_authority"], {TOOL_AUTHORITY_HOST: 1}
            )
            self.assertEqual(
                agent.store.run_tool_authority_counts(), {TOOL_AUTHORITY_HOST: 1}
            )
        finally:
            asyncio.run(agent.close())

    def test_an_unknown_authority_is_refused(self):
        agent = self._run_once(HostBridge(SilentHost()), "bad-authority")
        try:
            run_id = agent.store.list_runs()[0]["run_id"]
            with self.assertRaises(PASError) as ctx:
                agent.store.record_run_tool_authority(
                    _lease_for(agent, run_id),
                    authority="root",
                    reason=None,
                    now_ms=self.clock.wall_now_ms(),
                )
            self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)
        finally:
            asyncio.run(agent.close())

    def test_a_stale_fence_cannot_rewrite_the_authority(self):
        agent = self._run_once(HostBridge(SilentHost()), "stale-fence")
        try:
            run_id = agent.store.list_runs()[0]["run_id"]
            lease = _lease_for(agent, run_id, fence=7)
            stale = RunLease(
                run_id=lease.run_id,
                event_id=lease.event_id,
                fence=lease.fence - 1,
                lease_until_ms=lease.lease_until_ms,
            )
            with self.assertRaises(PASError) as ctx:
                agent.store.record_run_tool_authority(
                    stale, authority=TOOL_AUTHORITY_PAS_BROKER, reason=None,
                    now_ms=self.clock.wall_now_ms(),
                )
            self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)
            # The recorded fact is unchanged.
            self.assertEqual(
                agent.store.get_run(run_id)["tool_authority"], TOOL_AUTHORITY_HOST
            )
        finally:
            asyncio.run(agent.close())


class AuthoritySurfacingTests(unittest.TestCase):
    def test_runs_show_prints_the_authority(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(wall_ms=T0)
            agent = ProactiveAgent(
                state_dir=tmp, executor=HostBridge(SilentHost()), clock=clock,
                timezone="UTC", locale="en", profile="personal",
            )
            grant = agent.create_grant_from_user_consent(
                capability="notify.self", account_ref="a", scope={}, consent_evidence_ref="c"
            )
            agent.jobs_upsert(
                Job(id="j", mode="task", schedule={"kind": "interval",
                    "anchor": "2025-10-09T00:00:00Z", "every_seconds": 3600},
                    instruction="check", grant_refs=(grant.grant_id,)),
                idempotency_key="k",
            )
            agent.trigger_job("j", reason="t")
            asyncio.run(agent.tick())
            run_id = agent.store.list_runs()[0]["run_id"]
            asyncio.run(agent.close())

            from proactive_sdk.service import main

            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = main(["--state-dir", tmp, "runs", "show", run_id, "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["tool_authority"], TOOL_AUTHORITY_HOST)


def _lease_for(agent: ProactiveAgent, run_id: str, *, fence: int = 7) -> RunLease:
    """Present a live fence for a finished run.

    ``claim_run`` only picks up queued runs, and these tests need a run that
    the coordinator has already finished with — so the lease is put back by
    hand rather than by replaying the whole claim path.
    """
    run = agent.store.get_run(run_id)
    assert run is not None
    now = agent.store.clock.wall_now_ms()
    lease_until = now + 60_000
    with agent.store.transaction():
        agent.store.db.execute(
            "UPDATE runs SET state='running', fence=?, lease_until_ms=? WHERE run_id=?",
            (fence, lease_until, run_id),
        )
    return RunLease(
        run_id=run_id, event_id=run["event_id"], fence=fence, lease_until_ms=lease_until
    )


class LegacyRowsStayUnknownTests(unittest.TestCase):
    """A migration must not retroactively upgrade the claim."""

    def test_a_v011_database_migrates_with_unknown_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            conn = sqlite3.connect(str(db_path), isolation_level=None)
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                for version, sql in MIGRATIONS:
                    if version > 7:  # everything up to v0.1.1
                        break
                    for statement in _split_sql_statements(sql):
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, checksum, applied_at_ms)"
                        " VALUES (?,?,?)",
                        (version, "legacy", T0),
                    )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('identity', ?)",
                    ('{"owner":"local-inbox:legacy","profile":"legacy"}',),
                )
                conn.execute(
                    """INSERT INTO events(event_id, idempotency_key, origin, payload_hash,
                                           payload_ref, observed_at_ms, expires_at_ms)
                       VALUES ('evt1','k1','manual','h','inline',?,?)""",
                    (T0, T0 + 1000),
                )
                conn.execute(
                    """INSERT INTO runs(run_id, event_id, state, deadline_ms, policy_version,
                                         created_at_ms, updated_at_ms)
                       VALUES ('run1','evt1','completed',?,1,?,?)""",
                    (T0 + 1000, T0, T0),
                )
                conn.commit()
            finally:
                conn.close()

            from proactive_sdk import Store

            store = Store(
                str(db_path), profile="legacy", owner_destination="local-inbox:legacy",
                clock=FakeClock(wall_ms=T0),
            )
            try:
                run = store.get_run("run1")
                self.assertEqual(run["tool_authority"], TOOL_AUTHORITY_UNKNOWN)
                self.assertIsNone(run["tool_authority_reason"])
                self.assertEqual(store.run_tool_authority_counts(), {TOOL_AUTHORITY_UNKNOWN: 1})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
