"""W6: delivery-outcome feedback, ignored_24h backfill and `pas stats`.

Preference feedback (mute/handled) and delivery feedback (replied/dismissed/
complained/ignored_24h) are different facts and live in different tables; the
report never adjusts thresholds by itself.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # p3/p4 fixtures
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from p3_fixtures import FakeClock, T0, ScriptedModel, ScriptedSource, make_broker, make_executor, make_pack_builder, static_batch
from p4_fixtures import make_proposed_run, make_stack, notify_proposal
from test_coordinator import admit_job_event, make_job, make_store

from proactive_sdk import (  # noqa: E402
    CoordinatorConfig,
    PASError,
    ProactiveCoordinator,
    SourceRegistry,
    Store,
)
from proactive_sdk.contracts import ErrorCode  # noqa: E402
from proactive_sdk.rpc import PROACTIVE_RPC_METHODS  # noqa: E402
from proactive_sdk.stats import aggregate_delivery_stats, parse_since  # noqa: E402

MINUTE = 60_000
HOUR = 3_600_000
DAY = 24 * HOUR


def _run(coro):
    return asyncio.run(coro)


_DELIVERY_SEQ = {"n": 0}


def _deliver(stack, proposals=None):
    """Drive one notification to the local inbox; returns its message_id."""
    from proactive_sdk import OutboxDispatcher

    store, clock, _channels, policy, job = stack
    _DELIVERY_SEQ["n"] += 1
    fact = f"fact-{_DELIVERY_SEQ['n']}"
    before = {row["inbox_id"] for row in store.list_inbox()}
    run_id = make_proposed_run(
        store, clock, job, proposals=proposals or [notify_proposal(fact)]
    )
    policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
    _run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
    fresh = [row for row in store.list_inbox() if row["inbox_id"] not in before]
    assert len(fresh) == 1, f"fixture delivery must add exactly one inbox row, got {fresh}"
    return fresh[0]["message_id"], run_id


class SinceParsingTests(unittest.TestCase):
    def test_relative_windows(self):
        now = 1_760_000_000_000
        self.assertEqual(parse_since("7d", now_ms=now), now - 7 * DAY)
        self.assertEqual(parse_since("24h", now_ms=now), now - 24 * HOUR)
        self.assertEqual(parse_since("30m", now_ms=now), now - 30 * MINUTE)
        self.assertEqual(parse_since(None, now_ms=now), now - 7 * DAY)

    def test_iso_timestamp(self):
        now = 1_760_000_000_000
        self.assertEqual(parse_since("2025-10-09T00:00:00Z", now_ms=now), 1_759_968_000_000)

    def test_invalid_windows_are_errors(self):
        for bad in ("", "7", "0d", "seven days", "d7"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_since(bad, now_ms=T0)


class AggregationTests(unittest.TestCase):
    def _samples(self):
        messages = [
            {"message_id": "m1", "reason": "changed", "source_id": "calendar"},
            {"message_id": "m2", "reason": "changed", "source_id": "calendar"},
            {"message_id": "m3", "reason": "changed", "source_id": "calendar"},
            {"message_id": "m4", "reason": "time_sensitive", "source_id": "rss"},
            {"message_id": "m5", "reason": "time_sensitive", "source_id": "rss"},
            {"message_id": "m6", "reason": "time_sensitive", "source_id": "rss"},
            {"message_id": "m7", "reason": "requested", "source_id": None},
            {"message_id": "m8", "reason": "requested", "source_id": None},
            {"message_id": "m9", "reason": None, "source_id": "calendar"},
            {"message_id": "m10", "reason": None, "source_id": "calendar"},
        ]
        feedback = [
            {"message_id": "m1", "signal": "replied", "source": "user"},
            {"message_id": "m2", "signal": "replied", "source": "user"},
            {"message_id": "m3", "signal": "replied", "source": "user"},
            {"message_id": "m4", "signal": "dismissed", "source": "user"},
            {"message_id": "m5", "signal": "complained", "source": "user"},
            {"message_id": "m6", "signal": "ignored_24h", "source": "host"},
            {"message_id": "m7", "signal": "ignored_24h", "source": "host"},
            {"message_id": "m8", "signal": "dismissed", "source": "user"},
        ]
        return messages, feedback

    def test_totals_and_rates_are_exact(self):
        report = aggregate_delivery_stats(*self._samples())
        totals = report["totals"]
        self.assertEqual(totals["sent"], 10)
        self.assertEqual(totals["accepted"], 3)
        self.assertEqual(totals["negative"], 3)
        self.assertEqual(totals["weak_ignored"], 2)
        self.assertEqual(totals["pending"], 2)
        self.assertEqual(totals["acceptance_rate"], 0.3)

    def test_reason_rows_are_grouped_and_unknown_labels_are_explicit(self):
        report = aggregate_delivery_stats(*self._samples())
        by_reason = {row["reason"]: row for row in report["by_reason"]}
        self.assertEqual(by_reason["changed"]["sent"], 3)
        self.assertEqual(by_reason["changed"]["accepted"], 3)
        self.assertEqual(by_reason["changed"]["acceptance_rate"], 1.0)
        self.assertEqual(by_reason["time_sensitive"]["negative"], 2)
        self.assertEqual(by_reason["time_sensitive"]["weak_ignored"], 1)
        self.assertEqual(by_reason["unspecified"]["sent"], 2)

    def test_source_rows_are_grouped_and_unknown_source_is_named(self):
        report = aggregate_delivery_stats(*self._samples())
        by_source = {row["source_id"]: row for row in report["by_source"]}
        self.assertEqual(by_source["calendar"]["sent"], 5)
        self.assertEqual(by_source["calendar"]["accepted"], 3)
        self.assertEqual(by_source["rss"]["negative"], 2)
        self.assertEqual(by_source["unknown"]["sent"], 2)
        self.assertEqual(by_source["unknown"]["negative"], 1)
        self.assertEqual(by_source["unknown"]["weak_ignored"], 1)

    def test_empty_report_is_well_formed(self):
        report = aggregate_delivery_stats([], [])
        self.assertEqual(report["totals"]["sent"], 0)
        self.assertEqual(report["totals"]["acceptance_rate"], 0.0)
        self.assertEqual(report["by_reason"], [])
        self.assertEqual(report["by_source"], [])


class DeliveryFeedbackStoreTests(unittest.TestCase):
    def test_record_validate_and_idempotency(self):
        stack = make_stack()
        message_id, _run_id = _deliver(stack)
        store = stack[0]
        row = store.record_delivery_feedback(
            message_id=message_id, signal="replied", actor="ui", now_ms=T0 + 1
        )
        self.assertFalse(row["duplicate"])
        self.assertEqual(row["source"], "user")
        again = store.record_delivery_feedback(
            message_id=message_id, signal="replied", actor="ui", now_ms=T0 + 2
        )
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(store.delivery_feedback_for(message_id)), 1)

    def test_invalid_inputs_are_refused(self):
        stack = make_stack()
        message_id, _ = _deliver(stack)
        store = stack[0]
        cases = [
            dict(message_id=message_id, signal="maybe", actor="ui"),
            dict(message_id=message_id, signal="ignored_24h", actor="ui"),  # user cannot
            dict(message_id="missing", signal="replied", actor="ui"),
            dict(message_id=message_id, signal="replied", actor=""),
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PASError):
                    store.record_delivery_feedback(now_ms=T0 + 1, **kwargs)
        # The host-side verdict is allowed through the store API.
        row = store.record_delivery_feedback(
            message_id=message_id,
            signal="ignored_24h",
            actor="pas:backfill",
            source="host",
            now_ms=T0 + 1,
        )
        self.assertEqual(row["source"], "host")

    def test_backfill_marks_only_silent_messages_after_24h(self):
        stack = make_stack()
        store, clock, _c, policy, job = stack
        first_id, _ = _deliver(stack)
        second_id, _ = _deliver(stack)
        store.record_delivery_feedback(
            message_id=first_id, signal="replied", actor="ui", now_ms=clock.wall_now_ms()
        )
        self.assertEqual(store.backfill_ignored_24h(now_ms=clock.wall_now_ms()), 0)  # too early
        clock.advance_wall(DAY + MINUTE)
        self.assertEqual(store.backfill_ignored_24h(now_ms=clock.wall_now_ms()), 1)
        marked = store.delivery_feedback_for(second_id)
        self.assertEqual([row["signal"] for row in marked], ["ignored_24h"])
        self.assertEqual(store.delivery_feedback_for(first_id)[0]["signal"], "replied")
        # Idempotent: nothing new on the next cycle.
        self.assertEqual(store.backfill_ignored_24h(now_ms=clock.wall_now_ms()), 0)

    def test_sent_messages_carry_reason_and_source(self):
        stack = make_stack()
        store, clock, _c, _policy, _job = stack
        message_id, run_id = _deliver(stack)
        with store.transaction():
            store.db.execute(
                "UPDATE run_proposals SET reason='changed', source_id='calendar' WHERE run_id=?",
                (run_id,),
            )
        rows = store.sent_messages_since(
            since_ms=clock.wall_now_ms() - HOUR, now_ms=clock.wall_now_ms()
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], message_id)
        self.assertEqual(rows[0]["reason"], "changed")
        self.assertEqual(rows[0]["source_id"], "calendar")

    def test_export_and_wipe_include_delivery_feedback(self):
        stack = make_stack()
        message_id, _ = _deliver(stack)
        store, clock, _c, _p, _j = stack
        store.record_delivery_feedback(
            message_id=message_id, signal="dismissed", actor="ui", now_ms=clock.wall_now_ms()
        )
        exported = store.export_profile_data()
        self.assertEqual(len(exported["delivery_feedback"]), 1)
        store.wipe_profile_data()
        self.assertEqual(store.export_profile_data()["delivery_feedback"], [])


class CoordinatorSourceAttributionTests(unittest.TestCase):
    def test_proposal_records_the_candidate_source(self):
        store, _ = make_store()
        job = make_job(mode="task", task={"instruction": "检查", "always_run": True})
        store.upsert_job(job, idempotency_key="k1")
        admit_job_event(store, job)
        model = ScriptedModel(
            [
                {
                    "content": json.dumps(
                        {
                            "decision": "propose",
                            "summary": "新内容",
                            "proposals": [
                                {
                                    "kind": "draft",
                                    "fact_id": "item-1",
                                    "revision": "rev-1",
                                    "reason": "changed",
                                }
                            ],
                        }
                    )
                }
            ]
        )
        registry = SourceRegistry()
        registry.register(
            source_id="calendar",
            account_ref="account:primary",
            source=ScriptedSource([static_batch([("item-1", "rev-1", "hello")])]),
            required_capability="calendar.read",
        )
        coordinator = ProactiveCoordinator(
            store,
            registry=registry,
            pack_builder=make_pack_builder(),
            executor=make_executor(model, broker=make_broker()),
            config=CoordinatorConfig(),
        )
        _run(coordinator.process_pending_run())
        run = store.list_runs(state="proposed")[0]
        proposal = store.run_proposals(run["run_id"])[0]
        self.assertEqual(proposal["source_id"], "calendar")
        self.assertEqual(proposal["reason"], "changed")


class FeedbackBoundaryTests(unittest.TestCase):
    def test_rpc_method_is_registered_and_trusted_only(self):
        self.assertIn("feedback.record", PROACTIVE_RPC_METHODS)
        # The model/plugin tool surface is unchanged: the plugin's exact tool
        # list is asserted in test_pas_plugin; built-in model tools stay 3.
        from proactive_sdk.builtin_tools import builtin_tools

        class _Agent:
            store = type("S", (), {"clock": FakeClock(wall_ms=T0)})()
            timezone = "UTC"

        self.assertEqual(len(builtin_tools(_Agent())), 3)

    def test_config_switch_for_host_backfill(self):
        from proactive_sdk.config import ConfigError, build_config, config_to_yaml_subset, parse_config_text

        base = 'config_version: "1"\nprofile: demo\nstate_dir: /tmp/x\ntimezone: UTC\nlocale: en\n'
        config = build_config(parse_config_text(base))
        self.assertFalse(config.policy.feedback_ignored_backfill)
        enabled = build_config(
            parse_config_text(base + "policy:\n  feedback_ignored_backfill: true\n")
        )
        self.assertTrue(enabled.policy.feedback_ignored_backfill)
        self.assertIn("feedback_ignored_backfill: true", config_to_yaml_subset(enabled))
        with self.assertRaises(ConfigError):
            build_config(
                parse_config_text(base + "policy:\n  feedback_ignored_backfill: yes\n")
            )


class StatsCliTests(unittest.TestCase):
    def test_parser_and_empty_report(self):
        import contextlib
        import io

        from proactive_sdk.service import _render_stats_table, build_parser, main

        args = build_parser().parse_args(["stats"])
        self.assertEqual(args.since, "7d")
        self.assertEqual(build_parser().parse_args(["stats", "--since", "24h"]).since, "24h")
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = main(["--state-dir", tmp, "stats", "--json"])
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["totals"]["sent"], 0)
        rendered = _render_stats_table(
            {
                "since": "7d",
                "since_ms": 0,
                "now_ms": 1,
                "totals": {"sent": 0, "accepted": 0, "negative": 0, "weak_ignored": 0,
                           "pending": 0, "acceptance_rate": 0.0},
                "by_reason": [],
                "by_source": [],
            }
        )
        self.assertIn("by reason", rendered)
        self.assertIn("by source", rendered)

    def test_facade_report_is_well_formed_on_an_empty_profile(self):
        from proactive_sdk import ProactiveAgent

        with tempfile.TemporaryDirectory() as tmp:
            agent = ProactiveAgent(
                state_dir=tmp, model=ScriptedModel([]), clock=FakeClock(wall_ms=T0)
            )
            try:
                report = agent.delivery_stats(since="24h")
            finally:
                _run(agent.close())
        self.assertEqual(report["totals"]["sent"], 0)
        self.assertEqual(report["by_reason"], [])
        self.assertEqual(report["by_source"], [])
        self.assertEqual(report["since"], "24h")
        self.assertEqual(report["since_ms"], T0 - DAY)


if __name__ == "__main__":
    unittest.main()
