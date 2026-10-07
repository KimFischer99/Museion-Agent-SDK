"""SPEC §22.1 item 7: suggestions, and the confirmation that turns them into work.

`suggest_watch` was a declared proposal kind with no consumer: policy filed
it under `_LOCAL_KINDS` and the run_events note was the end of it. The user
never saw it and it never became a task.

The property that matters most is a *negative* one, so it gets its own
tests: **a suggestion must never become a task on its own.** Everything
else — freezing, actor recording, refusing twice — exists to make that
negative property checkable rather than merely intended.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import (  # noqa: E402
    NOTIFY_SELF_CAPABILITY,
    CallableHostDriver,
    ErrorCode,
    FakeClock,
    HostBridge,
    Job,
    PASError,
    ProactiveAgent,
)

T0 = 1_760_000_000_000
SCHEDULE = {"kind": "interval", "anchor": "2026-01-01T00:00:00Z", "every_seconds": 3600}
INSTRUCTION = "检查 PR #1234 的状态，有进展只通知本人。"


def _proposal(**over) -> dict:
    body = {
        "kind": "suggest_watch",
        "fact_id": "fact-1",
        "revision": "1",
        "arguments": {
            "schedule": dict(SCHEDULE),
            "instruction": INSTRUCTION,
            "reason": "用户在跟进这个 PR，值得持续观察",
        },
    }
    body.update(over)
    return body


def _envelope(*proposals: dict) -> dict:
    return {
        "decision": "propose",
        "summary": "建议建立跟踪",
        "proposals": list(proposals) or [_proposal()],
    }


class WatchSuggestionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(wall_ms=T0)
        self.envelope = _envelope()
        self.host = CallableHostDriver(lambda _p: self.envelope)
        self.agent = ProactiveAgent(
            state_dir=Path(self.tmp.name),
            executor=HostBridge(self.host),
            clock=self.clock,
            timezone="UTC",
            locale="en",
            profile="personal",  # the CLI resolves this default from config
        )
        self.grant = self.agent.create_grant_from_user_consent(
            capability=NOTIFY_SELF_CAPABILITY,
            account_ref="account:primary",
            scope={},
            consent_evidence_ref="consent:watch",
        )
        self.agent.jobs_upsert(
            Job(
                id="origin",
                mode="task",
                schedule=SCHEDULE,
                instruction="看看有什么值得跟踪的。",
                grant_refs=(self.grant.grant_id,),
            ),
            idempotency_key="origin-v1",
        )

    def tearDown(self):
        asyncio.run(self.agent.close())
        self.tmp.cleanup()

    def _run(self) -> dict:
        self.agent.trigger_job("origin", reason="watch test")
        return asyncio.run(self.agent.tick())

    def _pending(self) -> list[dict]:
        return self.agent.suggestions_list(state="pending")


class SuggestionIsInertTests(WatchSuggestionCase):
    def test_a_suggestion_is_recorded_rather_than_filed_and_forgotten(self):
        report = self._run()
        entry = list(report["runs"])[0]
        self.assertEqual(entry["outcome"], "proposed")
        pending = self._pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "pending")
        self.assertEqual(pending[0]["instruction"], INSTRUCTION)
        self.assertEqual(pending[0]["schedule"], SCHEDULE)

    def test_a_suggestion_does_not_create_a_job_by_itself(self):
        self._run()
        # The decisive negative: exactly the origin job exists, nothing else.
        self.assertEqual([j.job_id for j in self.agent.jobs_list()], ["origin"])
        self.assertIsNone(self.agent.store.get_job(self._pending()[0]["job_name"]))
        self.assertIsNone(self._pending()[0]["created_job_id"])

    def test_a_suggestion_delivers_nothing(self):
        self._run()
        self.assertEqual(self.agent.store.list_outbox(), [])
        self.assertEqual(self.agent.store.list_inbox(), [])

    def test_the_verdict_says_it_is_pending_confirmation(self):
        self._run()
        run_id = self.agent.store.list_runs()[0]["run_id"]
        proposal = self.agent.store.run_proposals(run_id)[0]
        self.assertEqual(proposal["policy"]["outcome"], "watch_suggested")
        self.assertEqual(proposal["policy"]["reason"], "pending_confirmation")


class ConfirmationTests(WatchSuggestionCase):
    def test_accepting_creates_the_frozen_job(self):
        self._run()
        suggestion = self._pending()[0]
        resolved = self.agent.suggestions_resolve(
            suggestion["suggestion_id"], accept=True, actor="cli:test"
        )
        self.assertEqual(resolved["state"], "accepted")
        self.assertEqual(resolved["resolved_by"], "cli:test")
        created = self.agent.store.get_job(suggestion["job_name"])
        self.assertIsNotNone(created)
        # Exactly the frozen parameters — nothing re-read model output.
        self.assertEqual(created.schedule, SCHEDULE)
        self.assertEqual(created.task["instruction"], INSTRUCTION)
        self.assertEqual(created.grant_refs, (self.grant.grant_id,))
        self.assertEqual(resolved["created_job_id"], created.job_id)

    def test_declining_is_recorded_with_the_same_weight(self):
        self._run()
        suggestion = self._pending()[0]
        resolved = self.agent.suggestions_resolve(
            suggestion["suggestion_id"], accept=False, actor="cli:test"
        )
        self.assertEqual(resolved["state"], "declined")
        self.assertEqual(resolved["resolved_by"], "cli:test")
        self.assertIsNone(self.agent.store.get_job(suggestion["job_name"]))
        self.assertEqual(self.agent.suggestions_list(state="declined")[0]["suggestion_id"],
                         suggestion["suggestion_id"])

    def test_resolving_twice_is_a_conflict_not_an_overwrite(self):
        self._run()
        suggestion = self._pending()[0]
        self.agent.suggestions_resolve(suggestion["suggestion_id"], accept=False, actor="a")
        with self.assertRaises(PASError) as ctx:
            self.agent.suggestions_resolve(suggestion["suggestion_id"], accept=True, actor="b")
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)
        self.assertIsNone(self.agent.store.get_job(suggestion["job_name"]))

    def test_an_actor_is_required(self):
        self._run()
        for bad in ("", None):
            with self.assertRaises(PASError):
                self.agent.suggestions_resolve(
                    self._pending()[0]["suggestion_id"], accept=True, actor=bad
                )

    def test_an_unknown_suggestion_is_rejected(self):
        with self.assertRaises(PASError):
            self.agent.suggestions_resolve("ws-nope", accept=True, actor="a")

    def test_an_accepted_job_starts_tracking(self):
        self._run()
        suggestion = self._pending()[0]
        self.agent.suggestions_resolve(suggestion["suggestion_id"], accept=True, actor="a")
        job = self.agent.store.get_job(suggestion["job_name"])
        self.assertIsNotNone(job.next_due_ms)
        self.assertTrue(job.enabled)


class SuggestionValidationTests(WatchSuggestionCase):
    def _reject(self, **arguments) -> str:
        self.envelope = _envelope(_proposal(arguments=arguments))
        self._run()
        run_id = self.agent.store.list_runs()[0]["run_id"]
        proposal = self.agent.store.run_proposals(run_id)[0]
        return proposal["policy"]["reason"]

    def test_a_missing_schedule_is_refused(self):
        self.assertEqual(self._reject(instruction=INSTRUCTION, reason="r"),
                         "watch_schedule_missing")

    def test_an_invalid_schedule_is_refused_without_freezing_anything(self):
        reason = self._reject(
            schedule={"kind": "daily", "local_time": "09:00"},  # no timezone
            instruction=INSTRUCTION,
            reason="r",
        )
        self.assertTrue(reason.startswith("watch_schedule_invalid"), reason)
        self.assertEqual(self._pending(), [])

    def test_a_missing_instruction_is_refused(self):
        self.assertEqual(self._reject(schedule=SCHEDULE, reason="r"),
                         "watch_instruction_invalid")

    def test_a_missing_reason_is_refused(self):
        self.assertEqual(self._reject(schedule=SCHEDULE, instruction=INSTRUCTION),
                         "watch_reason_missing")

    def test_nothing_is_frozen_when_validation_fails(self):
        self._reject(schedule="not-an-object", instruction=INSTRUCTION, reason="r")
        self.assertEqual(self.agent.suggestions_list(), [])


class SuggestionIdentityTests(WatchSuggestionCase):
    def test_the_frozen_name_is_derived_and_collision_free(self):
        self.envelope = _envelope(_proposal(arguments={
            "schedule": dict(SCHEDULE), "instruction": INSTRUCTION, "reason": "r",
        }))
        self._run()
        name = self._pending()[0]["job_name"]
        self.assertRegex(name, r"^[a-z0-9][a-z0-9._-]{0,127}$")
        self.assertNotIn(name, {j.job_id for j in self.agent.jobs_list()})

    def test_a_name_hint_is_used_when_it_is_usable(self):
        self.envelope = _envelope(_proposal(arguments={
            "schedule": dict(SCHEDULE), "instruction": INSTRUCTION, "reason": "r",
            "name": "pr-1234-watch",
        }))
        self._run()
        self.assertEqual(self._pending()[0]["job_name"], "pr-1234-watch")

    def test_a_taken_name_is_de_collided_before_the_user_sees_it(self):
        self.agent.jobs_upsert(
            Job(id="pr-1234-watch", mode="task", schedule=SCHEDULE, instruction="existing"),
            idempotency_key="taken-v1",
        )
        self.envelope = _envelope(_proposal(arguments={
            "schedule": dict(SCHEDULE), "instruction": INSTRUCTION, "reason": "r",
            "name": "pr-1234-watch",
        }))
        self._run()
        name = self._pending()[0]["job_name"]
        self.assertNotEqual(name, "pr-1234-watch")
        self.assertTrue(name.startswith("pr-1234-watch-"))

    def test_re_evaluating_the_same_proposal_freezes_one_suggestion(self):
        from proactive_sdk.policy import PolicyConfig, PolicyEngine
        from proactive_sdk import OwnerChannelRegistry

        self._run()
        run_id = self.agent.store.list_runs()[0]["run_id"]
        policy = PolicyEngine(
            self.agent.store,
            channels=OwnerChannelRegistry(self.agent.store),
            config=PolicyConfig(),
        )
        # A retry of phase A must not produce a second suggestion.
        store = self.agent.store
        first = store.list_watch_suggestions()
        record = store.create_watch_suggestion(
            run_id=run_id,
            proposal_id=store.run_proposals(run_id)[0]["proposal_id"],
            job_id=None,
            job_name="ignored-name",
            schedule=SCHEDULE,
            instruction=INSTRUCTION,
            grant_refs=(),
            delivery_policy={},
            misfire_policy=None,
            reason="r",
            now_ms=T0,
        )
        self.assertEqual(len(store.list_watch_suggestions()), len(first))
        self.assertEqual(record["suggestion_id"], first[0]["suggestion_id"])
        self.assertIsNotNone(policy)


class SuggestionSurfacesTests(WatchSuggestionCase):
    def test_the_cli_lists_and_accepts(self):
        import contextlib
        import io

        self._run()
        suggestion_id = self._pending()[0]["suggestion_id"]
        state_dir = str(Path(self.tmp.name))

        from proactive_sdk.service import main

        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["--state-dir", state_dir, "suggestions", "list", "--json"])
        self.assertEqual(code, 0)
        pending = json.loads(out.getvalue())["suggestions"]
        self.assertEqual(pending[0]["suggestion_id"], suggestion_id)

        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main([
                "--state-dir", state_dir, "suggestions", "accept", suggestion_id,
                "--actor", "cli:operator", "--json",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["state"], "accepted")
        self.assertEqual(payload["resolved_by"], "cli:operator")

    def test_resolving_requires_a_known_suggestion(self):
        import contextlib
        import io

        from proactive_sdk.service import main

        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = main([
                "--state-dir", str(Path(self.tmp.name)),
                "suggestions", "decline", "ws-nope",
            ])
        self.assertEqual(code, 2)
        self.assertIn("unknown suggestion", err.getvalue())


if __name__ == "__main__":
    unittest.main()
