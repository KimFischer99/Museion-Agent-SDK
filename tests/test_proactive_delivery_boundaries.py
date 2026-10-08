from __future__ import annotations

import hashlib
import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from p4_fixtures import make_stack, notify_proposal  # noqa: E402
from proactive_sdk import ContextPack, ContextSource, FeedbackManager, JobSpec, OutboxDispatcher  # noqa: E402
from proactive_sdk.contracts import canonical_json  # noqa: E402

T0 = 1_760_000_000_000


def _rfc3339(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _news_snapshot(store, *, fresh_until_ms: int) -> tuple[str, str, str]:
    fields = {
        "description": "A public Mars research update.",
        "published_at": "2025-10-09T00:00:00Z",
        "title": "Mars update",
        "url": "https://news.example/mars-update",
    }
    content = canonical_json({**fields, "candidate_topics": ["mars"]})
    fact_id = "news:" + hashlib.sha256(fields["url"].encode("utf-8")).hexdigest()[:24]
    revision = "sha256:" + hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()[:32]
    snapshot = store.put_snapshot(
        "news-search",
        "account:public",
        content=content,
        observed_at_ms=T0,
        fresh_until_ms=fresh_until_ms,
        sensitivity="public",
    )
    return fact_id, revision, f"snapshot:news-search:{snapshot.snapshot_id}"


def _proposed_news_run(store, clock, job, fact_id: str, revision: str, snapshot_ref: str, fresh_until_ms: int) -> str:
    now = clock.wall_now_ms()
    store.admit_event(
        f"job:{job.job_id}:{job.revision}:{now}:news-boundary",
        origin="scheduler",
        payload={"job_id": job.job_id, "mode": job.mode},
        observed_at_ms=now,
        expires_at_ms=now + 7 * 24 * 3600 * 1000,
        job_id=job.job_id,
        job_revision=job.revision,
    )
    lease = store.claim_run(now_ms=now, ttl_ms=60_000)
    assert lease is not None
    pack = ContextPack(
        task_goal_id=job.job_id,
        task_scope=f"job:{job.job_id}",
        locale="zh-CN",
        timezone="UTC",
        preferences_ref="prefs",
        sources=(
            ContextSource(
                source_id="news-search",
                account_ref="account:public",
                snapshot_ref=snapshot_ref,
                observed_at=_rfc3339(now),
                fresh_until=_rfc3339(fresh_until_ms),
                sensitivity="public",
            ),
        ),
    ).to_dict()
    proposal = notify_proposal(
        fact_id,
        revision=revision,
        arguments={"topic": "mars"},
        evidence_refs=[snapshot_ref],
    )
    store.record_run_decision(
        lease,
        context_pack=pack,
        decision={
            "protocol_version": "1.0",
            "decision": "propose",
            "summary": "Mars update",
            "proposals": [],
        },
        proposals=[proposal],
        usage=None,
        now_ms=now,
    )
    return lease.run_id


class ProactiveDeliveryBoundaryTests(unittest.TestCase):
    def _dispatch(self, store, clock, policy):
        return asyncio.run(OutboxDispatcher(store, policy=policy).dispatch_due(now_ms=clock.wall_now_ms()))

    def test_topic_muted_after_queue_is_suppressed_before_dispatch(self):
        store, clock, _, policy, job = make_stack()
        self.addCleanup(store.close)
        fact_id, revision, ref = _news_snapshot(store, fresh_until_ms=T0 + 60_000)
        run_id = _proposed_news_run(store, clock, job, fact_id, revision, ref, T0 + 60_000)

        self.assertEqual(policy.apply_to_run(run_id, now_ms=T0).queued, 1)
        FeedbackManager(store).record(
            kind="mute_topic", scope={"topic": "mars"}, actor="owner", now_ms=T0 + 1
        )

        reports = self._dispatch(store, clock, policy)

        self.assertEqual([report.state for report in reports], ["suppressed"])
        self.assertEqual(store.list_inbox(), [])

    def test_news_snapshot_freshness_is_bound_when_queued_and_blocks_late_dispatch(self):
        store, clock, _, policy, job = make_stack()
        self.addCleanup(store.close)
        fresh_until = T0 + 60_000
        fact_id, revision, ref = _news_snapshot(store, fresh_until_ms=fresh_until)
        run_id = _proposed_news_run(store, clock, job, fact_id, revision, ref, fresh_until)

        report = policy.apply_to_run(run_id, now_ms=T0)
        self.assertEqual(report.queued, 1)
        action = store.list_actions(state="queued")[0]
        request = store.action_delivery_context(action["action_id"])["request"]
        self.assertEqual(
            request["news_search_snapshot"],
            {"source_id": "news-search", "snapshot_ref": ref, "fresh_until_ms": fresh_until},
        )

        clock.set_wall(fresh_until + 1)
        reports = self._dispatch(store, clock, policy)

        self.assertEqual([report.state for report in reports], ["suppressed"])
        self.assertEqual(store.list_inbox(), [])

    def test_stale_news_snapshot_is_rejected_before_queue(self):
        store, clock, _, policy, job = make_stack()
        self.addCleanup(store.close)
        fact_id, revision, ref = _news_snapshot(store, fresh_until_ms=T0 - 1)
        run_id = _proposed_news_run(store, clock, job, fact_id, revision, ref, T0 - 1)

        report = policy.apply_to_run(run_id, now_ms=T0)

        self.assertEqual(report.queued, 0)
        self.assertTrue(any("news_snapshot_stale" in reason for reason in report.reasons))
        self.assertEqual(store.list_outbox(), [])

    def test_due_obligation_still_bypasses_soft_global_disable(self):
        store, clock, _, policy, job = make_stack()
        self.addCleanup(store.close)
        due_job = JobSpec(
            job_id="due-notify",
            mode="heartbeat",
            schedule=job.schedule,
            task={"instruction": "Send the due notice."},
            grant_refs=job.grant_refs,
            obligation="due",
        )
        store.upsert_job(due_job, idempotency_key="due-notify-v1")
        store.set_proactive_preferences({"enabled": False, "allowed_topics": []}, now_ms=T0)
        from p4_fixtures import make_proposed_run

        run_id = make_proposed_run(
            store,
            clock,
            due_job,
            proposals=[notify_proposal("fact-due", arguments={"topic": "space"})],
        )
        self.assertEqual(policy.apply_to_run(run_id, now_ms=T0).queued, 1)

        reports = self._dispatch(store, clock, policy)

        self.assertEqual([report.state for report in reports], ["stored_in_inbox"])
        self.assertEqual(len(store.list_inbox()), 1)


if __name__ == "__main__":
    unittest.main()
