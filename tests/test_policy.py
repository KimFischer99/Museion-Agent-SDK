"""P4 policy tests: grants, frozen-parameter approvals, quiet hours,
receiver binding, dedup and feedback (SPEC §9, §10.1, §10.4; §15.1 P4
acceptance: 夜间不发 / 撤销立刻生效 / 冻结参数审批 / 本人目标不可替换).

Boundary: policy evaluation and store transactions only — the scripted
run fixtures bypass the L1 loop (P3's tests cover it) and no real
notification provider participates in this file.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from proactive_sdk import (
    ApprovalManager,
    ErrorCode,
    FakeClock,
    GrantManager,
    NOTIFY_SELF_CAPABILITY,
    OwnerChannelRegistry,
    PASError,
    PolicyConfig,
    PolicyEngine,
    Store,
)
from p4_fixtures import (
    OWNER_CHANNEL,
    P4TestCase,
    berlin_ms,
    make_proposed_run,
    make_stack,
    make_store,
    notify_proposal,
    rfc3339,
)
from p4_fixtures import ScriptedWebhookServer, webhook_channel

NIGHT = berlin_ms("2026-10-20", 2)      # 02:00 Europe/Berlin — inside 22:00–08:00
QUIET_END = berlin_ms("2026-10-20", 8)  # 08:00 Europe/Berlin
DAY = berlin_ms("2026-10-20", 14)       # 14:00 Europe/Berlin — outside quiet hours

QUIET_POLICY = {
    "quiet_hours": {"start": "22:00", "end": "08:00", "timezone": "Europe/Berlin"},
}


def quiet_job_policy(**extra):
    policy = dict(QUIET_POLICY)
    policy.update(extra)
    return policy


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


# --------------------------------------------------------------------------- #
# 夜间不发 — quiet hours defer or suppress, never violate
# --------------------------------------------------------------------------- #


class QuietHoursTests(P4TestCase):
    def _queued_message(self, store):
        messages = store.list_outbox()
        assert len(messages) == 1, f"expected exactly one queued message, got {messages}"
        return messages[0]

    def test_night_notification_is_deferred_to_quiet_end(self):
        store, clock, _, policy, job = make_stack(delivery_policy=quiet_job_policy())
        clock.set_wall(NIGHT)
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(report.outcome, "actions_queued")
        self.assertEqual(report.queued, 1)
        message = self._queued_message(store)
        self.assertEqual(message["state"], "pending")
        self.assertEqual(message["not_before_ms"], QUIET_END)
        self.assertEqual(message["reason"], "quiet_hours")

    def test_deferred_message_is_not_sent_before_quiet_end(self):
        from proactive_sdk import OutboxDispatcher

        store, clock, _, policy, job = make_stack(delivery_policy=quiet_job_policy())
        clock.set_wall(NIGHT)
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        dispatcher = OutboxDispatcher(store)
        reports = _run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        # 02:00 local: nothing dispatchable, nothing delivered.
        self.assertEqual(reports, [])
        self.assertEqual(store.list_inbox(), [])
        clock.set_wall(QUIET_END + 60_000)
        reports = _run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual([r.state for r in reports], ["stored_in_inbox"])
        self.assertEqual(len(store.list_inbox()), 1)

    def test_proposal_expiring_inside_quiet_hours_is_suppressed(self):
        store, clock, _, policy, job = make_stack(delivery_policy=quiet_job_policy())
        clock.set_wall(NIGHT)
        # Expires 07:00 — before the 08:00 quiet end: sending it later
        # would deliver a stale message (§10.4).
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-1", expires_at=rfc3339(berlin_ms("2026-10-20", 7)))],
        )
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(report.suppressed, 1)
        self.assertIn("expired_in_quiet_hours", report.reasons[0])
        self.assertEqual(report.outcome, "completed")
        self.assertEqual(store.list_outbox(), [])

    def test_daytime_notification_is_not_deferred(self):
        store, clock, _, policy, job = make_stack(delivery_policy=quiet_job_policy())
        clock.set_wall(DAY)
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(report.queued, 1)
        message = self._queued_message(store)
        self.assertEqual(message["not_before_ms"], DAY)

    def test_quiet_hours_config_errors_are_invalid_config(self):
        from proactive_sdk.policy import quiet_end_ms

        with self.assertRaises(PASError):
            quiet_end_ms({"quiet_hours": {"start": "25:00", "end": "08:00", "timezone": "Europe/Berlin"}}, NIGHT)
        with self.assertRaises(PASError):
            quiet_end_ms({"quiet_hours": {"start": "22:00", "end": "08:00", "timezone": "Mars/Olympus"}}, NIGHT)


# --------------------------------------------------------------------------- #
# 本人目标不可替换 — receiver binding
# --------------------------------------------------------------------------- #


class ReceiverBindingTests(P4TestCase):
    def test_model_named_receiver_is_suppressed_not_redirected(self):
        store, clock, _, policy, job = make_stack(delivery_policy=quiet_job_policy())
        clock.set_wall(DAY)
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-1", arguments={"destination": "attacker@evil.example"})],
        )
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(report.suppressed, 1)
        self.assertIn("receiver_forbidden", report.reasons[0])
        self.assertEqual(report.outcome, "completed")
        self.assertEqual(store.list_outbox(), [])

    def test_notify_self_lands_only_in_the_bound_owner_channel(self):
        from proactive_sdk import OutboxDispatcher

        store, clock, _, policy, job = make_stack(delivery_policy=quiet_job_policy())
        clock.set_wall(DAY)
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        report = _run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual([r.state for r in report], ["stored_in_inbox"])
        inbox = store.list_inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["message_id"], store.list_outbox()[0]["message_id"])

    def test_local_inbox_channel_must_equal_owner_destination(self):
        store, _ = make_store()
        registry = OwnerChannelRegistry(store)
        with self.assertRaises(PASError) as caught:
            registry.register(channel_ref="local-inbox:someone-else", kind="local_inbox")
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_CONFIG)

    def test_unknown_notification_profile_is_rejected(self):
        store, clock, _, policy, job = make_stack(
            delivery_policy={**{"notification_profile": "push:ghost"}}
        )
        clock.set_wall(DAY)
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(report.suppressed, 1)
        self.assertIn("channel_unavailable", report.reasons[0])


# --------------------------------------------------------------------------- #
# 撤销立刻生效 — revocation cascade
# --------------------------------------------------------------------------- #


class RevocationTests(P4TestCase):
    def test_revocation_suppresses_queued_message_and_pending_approvals(self):
        from proactive_sdk import OutboxDispatcher

        target = webhook_channel()
        store, clock, registry, policy, job = make_stack(
            grant_capabilities=[NOTIFY_SELF_CAPABILITY, "calendar.write"],
            channels=[target],
            delivery_policy={"external_action_target": "push:demo"},
        )
        clock.set_wall(DAY)
        # Queue a notification…
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        # …and a pending approval for an external action.
        ext_run = make_proposed_run(
            store, clock, job,
            proposals=[{
                "kind": "request_external_action",
                "fact_id": "fact-ext-1",
                "revision": "1",
                "arguments": {"capability": "calendar.write", "action": "create_event",
                              "resource_id": "cal-a"},
                "evidence_refs": ["snapshot:snap-fixture"],
            }],
        )
        policy.apply_to_run(ext_run, now_ms=clock.wall_now_ms())
        approvals = store.list_approvals(state="pending")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(store.list_outbox(state="pending").__len__(), 1)

        grants = GrantManager(store)
        for grant in grants.active(now_ms=clock.wall_now_ms()):
            grants.revoke(grant.grant_id, now_ms=clock.wall_now_ms())
        # Immediate effect, same instant:
        self.assertEqual(store.list_approvals(state="pending"), [])
        self.assertEqual(store.list_approvals(state="revoked").__len__(), 1)
        messages = store.list_outbox()
        self.assertTrue(all(m["state"] == "suppressed" for m in messages))
        self.assertTrue(all(m["reason"] == "grant_revoked" for m in messages))
        report = _run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual(report, [])

    def test_revocation_during_flight_journals_attempt_but_aborts_commit(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        lease = store.claim_outbox_message(now_ms=clock.wall_now_ms(), ttl_ms=30_000)
        self.assertIsNotNone(lease)
        # Revocation lands while the worker is mid-flight (lease held).
        grants = GrantManager(store)
        grants.revoke(grants.active(now_ms=clock.wall_now_ms())[0].grant_id,
                      now_ms=clock.wall_now_ms())
        # The provider *accepted* before the worker noticed — the attempt
        # is journaled, but the message is NOT marked delivered.
        final = store.finish_outbox_attempt(
            lease, outcome="provider_accepted",
            started_at_ms=clock.wall_now_ms(), now_ms=clock.wall_now_ms(),
            receipt={"external_id": "ext-9"},
        )
        self.assertEqual(final, "aborted")
        message = store.list_outbox()[0]
        self.assertEqual(message["state"], "suppressed")
        attempts = store.delivery_attempts_for(message["message_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["state"], "provider_accepted")

    def test_revoked_capability_leaves_active_capabilities(self):
        store, clock, _, _, _ = make_stack()
        grants = GrantManager(store)
        grant = grants.active(now_ms=clock.wall_now_ms())[0]
        self.assertIn(NOTIFY_SELF_CAPABILITY, grants.active_capabilities(now_ms=clock.wall_now_ms()))
        grants.revoke(grant.grant_id, now_ms=clock.wall_now_ms())
        self.assertEqual(grants.active_capabilities(now_ms=clock.wall_now_ms()), frozenset())

    def test_revocation_is_idempotent(self):
        store, clock, _, _, _ = make_stack()
        grants = GrantManager(store)
        grant_id = grants.active(now_ms=clock.wall_now_ms())[0].grant_id
        first = grants.revoke(grant_id, now_ms=clock.wall_now_ms())
        second = grants.revoke(grant_id, now_ms=clock.wall_now_ms())
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- #
# 冻结参数审批 — approvals bind the canonical request hash
# --------------------------------------------------------------------------- #


class ApprovalFreezeTests(P4TestCase):
    def _external_stack(self, *, endpoint: dict | None = None):
        target = webhook_channel()
        if endpoint is not None:
            target["endpoint"] = endpoint
        store, clock, registry, policy, job = make_stack(
            grant_capabilities=["calendar.write"],
            channels=[target],
            delivery_policy={"external_action_target": "push:demo"},
        )
        clock.set_wall(DAY)
        return store, clock, registry, policy, job

    def _external_proposal(self, *, arguments=None):
        return {
            "kind": "request_external_action",
            "fact_id": "fact-ext-1",
            "revision": "1",
            "arguments": arguments or {"capability": "calendar.write", "action": "create_event",
                                       "resource_id": "cal-a"},
            "evidence_refs": ["snapshot:snap-fixture"],
        }

    def test_external_action_waits_for_frozen_approval_then_delivers(self):
        server = ScriptedWebhookServer().start()
        store, clock, registry, policy, job = self._external_stack(
            endpoint={"url": server.url, "status_url": server.status_url},
        )
        try:
            run_id = make_proposed_run(store, clock, job, proposals=[self._external_proposal()])
            report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
            self.assertEqual(report.outcome, "waiting_for_approval")
            self.assertEqual(report.approval_pending, 1)
            pending = store.list_approvals(state="pending")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].grant_version, 1)
            # Model-facing dispatch must NOT send anything while pending.
            from proactive_sdk import OutboxDispatcher

            _run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
            self.assertEqual(server.requests, [])
            # The authenticated user approves via the control plane.
            approvals = ApprovalManager(store)
            resolved = approvals.resolve(
                pending[0].approval_id, approve=True, actor="ui-admin", now_ms=clock.wall_now_ms()
            )
            self.assertEqual(resolved.state, "approved")
            self.assertEqual(resolved.resolved_by, "ui-admin")
            queued = policy.promote_approved(now_ms=clock.wall_now_ms())
            self.assertEqual(queued, 1)
            reports = _run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
            self.assertEqual([r.state for r in reports], ["provider_accepted"])
            self.assertEqual(len(server.requests), 1)
        finally:
            server.close()

    def test_parameter_change_after_approval_requires_new_approval(self):
        store, clock, registry, policy, job = self._external_stack()
        approvals = ApprovalManager(store)
        request_v1 = {
            "kind": "request_external_action", "account_ref": "account:primary",
            "destination_ref": "push:demo", "arguments": {"action": "create_event"},
            "attachment_hashes": [], "evidence_refs": [], "payload": {"title": "v1"},
        }
        approval = approvals.request(
            grant_id=store.list_grants()[0].grant_id, request=request_v1, now_ms=clock.wall_now_ms()
        )
        self.assertEqual(approval.state, "pending")
        # Same frozen content → same approval, no parallel record.
        again = approvals.request(
            grant_id=store.list_grants()[0].grant_id, request=request_v1, now_ms=clock.wall_now_ms()
        )
        self.assertEqual(again.approval_id, approval.approval_id)
        # Any parameter change (§9.2: 提交前参数或附件变化则重新审批) is a new
        # request with a different hash — the old approval does not cover it.
        request_v2 = dict(request_v1, arguments={"action": "create_event", "guests": ["x"]})
        approval_v2 = approvals.request(
            grant_id=store.list_grants()[0].grant_id, request=request_v2, now_ms=clock.wall_now_ms()
        )
        self.assertNotEqual(approval_v2.approval_id, approval.approval_id)
        self.assertNotEqual(approval_v2.request_hash, approval.request_hash)

    def test_tampered_request_hash_is_refused_at_promotion(self):
        store, clock, registry, policy, job = self._external_stack()
        run_id = make_proposed_run(store, clock, job, proposals=[self._external_proposal()])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        approval = store.list_approvals(state="pending")[0]
        ApprovalManager(store).resolve(
            approval.approval_id, approve=True, actor="ui-admin", now_ms=clock.wall_now_ms()
        )
        # Simulate a binding mismatch (bug/tamper): the action's request
        # hash no longer matches the approval's frozen hash (§9.2).
        action = store.list_actions(state="planned")[0]
        store.db.execute(
            "UPDATE actions SET request_hash='0' || substr(request_hash, 2) WHERE action_id=?",
            (action["action_id"],),
        )
        queued = policy.promote_approved(now_ms=clock.wall_now_ms())
        self.assertEqual(queued, 0)
        self.assertEqual(store.list_actions(state="failed").__len__(), 1)
        self.assertEqual(store.list_outbox(), [])

    def test_denial_cancels_action_and_settles_run(self):
        store, clock, registry, policy, job = self._external_stack()
        run_id = make_proposed_run(store, clock, job, proposals=[self._external_proposal()])
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(report.outcome, "waiting_for_approval")
        approval = store.list_approvals(state="pending")[0]
        ApprovalManager(store).resolve(
            approval.approval_id, approve=False, actor="ui-admin", now_ms=clock.wall_now_ms()
        )
        self.assertEqual(store.list_actions(state="cancelled").__len__(), 1)
        self.assertEqual(store.get_run(run_id)["state"], "completed")

    def test_resolution_requires_authenticated_actor(self):
        store, clock, registry, policy, job = self._external_stack()
        run_id = make_proposed_run(store, clock, job, proposals=[self._external_proposal()])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        approval = store.list_approvals(state="pending")[0]
        with self.assertRaises(PASError) as caught:
            ApprovalManager(store).resolve(
                approval.approval_id, approve=True, actor="  ", now_ms=clock.wall_now_ms()
            )
        self.assertEqual(caught.exception.code, ErrorCode.AUTH_REQUIRED)

    def test_expired_approval_cannot_be_resolved(self):
        store, clock, registry, policy, job = self._external_stack()
        run_id = make_proposed_run(store, clock, job, proposals=[self._external_proposal()])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        approval = store.list_approvals(state="pending")[0]
        clock.set_wall(approval.expires_at_ms + 1)
        with self.assertRaises(PASError) as caught:
            ApprovalManager(store).resolve(
                approval.approval_id, approve=True, actor="ui-admin", now_ms=clock.wall_now_ms()
            )
        self.assertEqual(caught.exception.code, ErrorCode.CONFLICT)
        self.assertEqual(store.get_approval(approval.approval_id).state, "expired")


# --------------------------------------------------------------------------- #
# 账户切换 / scope 扩大 — grant scope binds
# --------------------------------------------------------------------------- #


class GrantScopeTests(P4TestCase):
    def _stack_with_write_grant(self):
        target = webhook_channel()
        store, clock, registry, policy, job = make_stack(
            grant_capabilities=["calendar.write"],
            channels=[target],
            delivery_policy={"external_action_target": "push:demo"},
        )
        clock.set_wall(DAY)
        return store, clock, policy, job

    def _external_proposal(self, arguments):
        return {
            "kind": "request_external_action", "fact_id": "fact-ext", "revision": "1",
            "arguments": arguments, "evidence_refs": ["snapshot:snap-fixture"],
        }

    def test_account_switch_attack_is_suppressed(self):
        store, clock, policy, job = self._stack_with_write_grant()
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[self._external_proposal({"capability": "calendar.write",
                                                "account_ref": "account:other"})],
        )
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertIn("account_mismatch", report.reasons[0])
        self.assertEqual(store.list_outbox(), [])

    def test_scope_expansion_is_suppressed(self):
        store, clock, policy, job = self._stack_with_write_grant()
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[self._external_proposal({"capability": "calendar.write",
                                                "resource_id": "cal-b"})],
        )
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertIn("scope_exceeded", report.reasons[0])

    def test_action_outside_granted_action_list_is_suppressed(self):
        store, clock, registry, policy, job = make_stack(
            grant_capabilities=["calendar.write"],
            channels=[webhook_channel()],
            delivery_policy={"external_action_target": "push:demo"},
        )
        grant = store.list_grants()[0]
        store.db.execute(
            "UPDATE grants SET scope_json=? WHERE grant_id=?",
            ('{"resource_ids":["cal-a"],"actions":["create_event"]}', grant.grant_id),
        )
        clock.set_wall(DAY)
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[self._external_proposal({"capability": "calendar.write",
                                                "action": "delete_event",
                                                "resource_id": "cal-a"})],
        )
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertIn("scope_exceeded", report.reasons[0])


# --------------------------------------------------------------------------- #
# 两层去重的业务层 / 反馈抑制 / 配额
# --------------------------------------------------------------------------- #


class DedupFeedbackQuotaTests(P4TestCase):
    def test_same_fact_revision_is_not_delivered_twice(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        first = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        policy.apply_to_run(first, now_ms=clock.wall_now_ms())
        clock.advance_wall(3600_000)
        second = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        report = policy.apply_to_run(second, now_ms=clock.wall_now_ms())
        self.assertIn("duplicate_business_key", report.reasons[0])
        self.assertEqual(store.list_outbox().__len__(), 1)

    def test_revision_bump_is_a_new_fact_version(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        first = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1", revision="1")])
        policy.apply_to_run(first, now_ms=clock.wall_now_ms())
        clock.advance_wall(3600_000)
        second = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1", revision="2")])
        report = policy.apply_to_run(second, now_ms=clock.wall_now_ms())
        self.assertEqual(report.queued, 1)
        self.assertEqual(store.list_outbox().__len__(), 2)

    def test_topic_mute_via_feedback_suppresses_and_unmute_restores(self):
        from proactive_sdk import FeedbackManager

        store, clock, _, policy, job = make_stack()
        feedback = FeedbackManager(store)
        feedback.record(kind="mute_topic", scope={"topic": "politics"},
                        actor="ui-admin", now_ms=clock.wall_now_ms())
        clock.set_wall(DAY)
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-1", arguments={"topic": "politics"})],
        )
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertIn("topic_muted", report.reasons[0])
        feedback.record(kind="unmute_topic", scope={"topic": "politics"},
                        actor="ui-admin", now_ms=clock.wall_now_ms())
        clock.advance_wall(60_000)
        run_id = make_proposed_run(store, clock, job,
                                   proposals=[notify_proposal("fact-1", arguments={"topic": "politics"})])
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(report.queued, 1)

    def test_config_muted_topics_are_hard_policy(self):
        store, clock, _, policy, job = make_stack(
            delivery_policy={"muted_topics": ["sports"]},
        )
        clock.set_wall(DAY)
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-1", arguments={"topic": "sports"})],
        )
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertIn("topic_muted", report.reasons[0])

    def test_handled_fact_is_suppressed(self):
        from proactive_sdk import FeedbackManager

        store, clock, _, policy, job = make_stack()
        FeedbackManager(store).record(
            kind="handled", scope={"fact_id": "fact-1"}, actor="ui-admin", now_ms=clock.wall_now_ms()
        )
        clock.set_wall(DAY)
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertIn("already_handled", report.reasons[0])

    def test_daily_quota_defers_not_drops(self):
        store, clock, _, policy, job = make_stack(
            delivery_policy={"max_per_day": 1, "timezone": "Europe/Berlin"}
        )
        clock.set_wall(DAY)
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-1"), notify_proposal("fact-2")],
        )
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        # `queued` counts everything queued into the outbox (immediate +
        # deferred); `deferred` is the subset parked for later (§10.2).
        self.assertEqual(report.queued, 2)
        self.assertEqual(report.deferred, 1)
        messages = {m["delivery_key"]: m for m in store.list_outbox()}
        states = {m["state"] for m in messages.values()}
        self.assertEqual(states, {"pending"})
        reasons = {m["reason"] for m in messages.values()}
        self.assertIn("daily_quota", reasons)

    def test_expired_proposal_is_suppressed(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-1", expires_at=rfc3339(DAY - 1000))],
        )
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertIn("expired", report.reasons[0])

    def test_expired_message_never_sent_after_validity(self):
        from proactive_sdk import OutboxDispatcher

        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[notify_proposal("fact-1", expires_at=rfc3339(DAY + 60_000))],
        )
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        clock.advance_wall(120_000)
        reports = _run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual(reports, [])
        self.assertEqual(store.list_outbox()[0]["state"], "expired")

    def test_local_record_kinds_stay_local(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        run_id = make_proposed_run(
            store, clock, job,
            proposals=[
                {"kind": "draft", "fact_id": "fact-d1", "revision": "1", "body": "草稿"},
                {"kind": "internal_record", "fact_id": "fact-d2", "revision": "1"},
            ],
        )
        report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        self.assertEqual(report.local_records, 2)
        self.assertEqual(report.outcome, "completed")
        self.assertEqual(store.list_outbox(), [])
        self.assertEqual(store.list_actions(), [])
        kinds = {e["kind"] for e in store.run_events(run_id)}
        self.assertIn("local_record", kinds)

    def test_feedback_requires_actor_and_known_message(self):
        from proactive_sdk import FeedbackManager

        store, clock, _, _, _ = make_stack()
        feedback = FeedbackManager(store)
        with self.assertRaises(PASError):
            feedback.record(kind="not_useful", scope={}, actor="", now_ms=clock.wall_now_ms())
        with self.assertRaises(PASError):
            feedback.record(kind="not_useful", scope={}, actor="ui", message_id="msg-none",
                            now_ms=clock.wall_now_ms())
        record = feedback.record(kind="not_useful", scope={"message": "x"}, actor="ui",
                                 now_ms=clock.wall_now_ms())
        self.assertEqual(record["actor"], "ui")


# --------------------------------------------------------------------------- #
# 锁屏去敏 — summary-only push payloads
# --------------------------------------------------------------------------- #


class PayloadSensitivityTests(P4TestCase):
    def test_push_channel_payload_is_summary_only(self):
        store, clock, registry, policy, job = make_stack(
            channels=[webhook_channel()], delivery_policy={"notification_profile": "push:demo"},
        )
        clock.set_wall(DAY)
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        payload = store.list_outbox()[0]["payload"]
        self.assertNotIn("body", payload)
        self.assertEqual(payload["title"], "有新的安排")

    def test_local_inbox_payload_keeps_the_body(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-1")])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        payload = store.list_outbox()[0]["payload"]
        self.assertIn("body", payload)
        self.assertEqual(payload["body"], "明早与后端团队的评审提前到 10:00。")


# --------------------------------------------------------------------------- #
# Store primitives: grants, blobs, channels
# --------------------------------------------------------------------------- #


class StorePrimitiveTests(P4TestCase):
    def test_grant_create_is_content_idempotent(self):
        store, clock, _, _, _ = make_stack()
        grants = GrantManager(store)
        now = clock.wall_now_ms()
        one = grants.create(capability="calendar.read", account_ref="account:primary",
                            consent_evidence_ref="consent:v1", now_ms=now)
        two = grants.create(capability="calendar.read", account_ref="account:primary",
                            consent_evidence_ref="consent:v1", now_ms=now)
        self.assertEqual(one.grant_id, two.grant_id)

    def test_channel_re_registration_conflicts_on_change(self):
        store, clock, registry, _, _ = make_stack()
        registry.register(channel_ref="push:demo", kind="webhook", endpoint={"url": "http://x"},
                          now_ms=clock.wall_now_ms())
        registry.register(channel_ref="push:demo", kind="webhook", endpoint={"url": "http://x"},
                          now_ms=clock.wall_now_ms())
        with self.assertRaises(PASError):
            registry.register(channel_ref="push:demo", kind="webhook", endpoint={"url": "http://y"},
                              now_ms=clock.wall_now_ms())

    def test_blob_store_roundtrip_and_snapshot_bodies(self):
        store, clock, _, _, _ = make_stack()
        ref = store.put_blob("通知正文", now_ms=clock.wall_now_ms())
        self.assertEqual(store.blob_content(ref), "通知正文")
        snapshot = store.put_snapshot(
            "calendar", "account:primary", content="会议正文", observed_at_ms=clock.wall_now_ms(),
            fresh_until_ms=clock.wall_now_ms() + 1000, sensitivity="private",
        )
        self.assertEqual(snapshot.content_ref, f"blob:{snapshot.content_hash}")
        self.assertEqual(store.snapshot_content(snapshot.snapshot_id), "会议正文")

    def test_notifications_today_counts_queue_time(self):
        store, clock, _, _, _ = make_stack()
        clock.set_wall(DAY)
        self.assertEqual(store.notifications_today(day_start_ms=berlin_ms("2026-10-20", 0),
                                                   now_ms=clock.wall_now_ms()), 0)

    def test_a_silent_decision_completes_instead_of_erroring(self):
        """The most common proactive outcome must not look like a failure.

        A run whose decision is `silent` has zero proposals. That is a valid
        run, so it has to move proposed → policy_evaluated → completed. Until
        v0.1.2 `record_policy_verdicts` rejected the empty verdict map, so
        every silent decision wired to a PolicyEngine was reported as
        `policy_error` and left retryable — but nothing tested that
        combination, so it stayed invisible.
        """
        store, clock, registry, policy, job = make_stack(delivery_policy={"timezone": "UTC"})
        run_id = make_proposed_run(store, clock, job, proposals=[], summary="无实质变化")
        self.assertEqual(store.get_run(run_id)["state"], "proposed")

        verdict = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())

        self.assertEqual(verdict.outcome, "completed")
        self.assertEqual(verdict.queued, 0)
        self.assertEqual(verdict.approval_pending, 0)
        self.assertEqual(store.get_run(run_id)["state"], "completed")
        self.assertEqual(store.list_outbox(), [])

    def test_migrations_include_p5_tables(self):
        store, _, _, _, _ = make_stack()
        tables = {
            row[0]
            for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for expected in ("inbox", "owner_channels", "topic_mutes", "blobs", "outbox"):
            self.assertIn(expected, tables)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# End-to-end: L0 → L1 → policy → outbox → local inbox (coordinator wired)
# --------------------------------------------------------------------------- #


class CoordinatorPolicyIntegrationTests(P4TestCase):
    def test_proposed_run_flows_through_policy_into_inbox(self):
        from p3_fixtures import (
            P3TestCase,
            ScriptedModel,
            ScriptedSource,
            make_executor,
            make_pack_builder,
            make_registry,
            static_batch,
        )

        store, clock, registry_channels, policy, job = make_stack()
        clock.set_wall(DAY)
        model = ScriptedModel(turns=[
            {"tool_calls": [("c1", "read_evidence", {"fact_id": "fact-e2e"})]},
            {"content": (
                '{"decision": "propose", "summary": "日程有更新", "proposals":'
                ' [{"kind": "notify_self", "fact_id": "fact-e2e", "revision": "1",'
                '  "body": "下午 3 点评审", "evidence_refs":'
                '  ["tool:read_evidence:c1"], "expires_at": "'
                + rfc3339(DAY + 3600_000) + '"}]}'
            ), "usage": {"model_calls": 1}},
        ])
        source = ScriptedSource(batches=[static_batch([("fact-e2e", "1", "下午 3 点评审")])])
        executor = make_executor(model, broker=None, clock=clock) if False else None
        # make_executor signature: (model, broker=None, *, budget, clock…)
        from p3_fixtures import make_broker

        executor = make_executor(model, broker=make_broker(capabilities=("calendar.read",)), clock=clock)
        registry = make_registry(source=source)
        from proactive_sdk import ProactiveCoordinator

        coordinator = ProactiveCoordinator(
            store,
            registry=registry,
            pack_builder=make_pack_builder(clock=clock),
            executor=executor,
            policy_engine=policy,
        )
        store.admit_event(
            f"job:{job.job_id}:{job.revision}:{clock.wall_now_ms()}",
            origin="scheduler",
            payload={"job_id": job.job_id, "mode": job.mode},
            observed_at_ms=clock.wall_now_ms(),
            expires_at_ms=clock.wall_now_ms() + 3_600_000,
            job_id=job.job_id,
            job_revision=job.revision,
        )
        report = P3TestCase.run_async_wrapper(coordinator) if False else None
        import asyncio

        report = asyncio.run(coordinator.process_pending_run(now_ms=clock.wall_now_ms()))
        self.assertIsNotNone(report)
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(report.policy_outcome, "actions_queued")
        self.assertEqual(report.queued, 1)
        # Delivery pass: local inbox write in one transaction.
        from proactive_sdk import OutboxDispatcher

        dispatch = asyncio.run(
            OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms())
        )
        self.assertEqual([r.state for r in dispatch], ["stored_in_inbox"])
        inbox = store.list_inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["fact_id"], "fact-e2e")
        self.assertEqual(inbox[0]["body"], "下午 3 点评审")
        self.assertEqual(store.get_run(report.run_id)["state"], "actions_queued")

    def test_topic_muted_proposal_is_stopped_by_policy_end_to_end(self):
        from p3_fixtures import (
            P3TestCase,
            ScriptedModel,
            ScriptedSource,
            make_broker,
            make_executor,
            make_pack_builder,
            make_registry,
            static_batch,
        )
        import asyncio

        from proactive_sdk import OutboxDispatcher, ProactiveCoordinator

        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        # A muted topic passes the P3 Decision parse (topics are data) and
        # must be stopped by the P4 policy layer, zero deliveries.
        from proactive_sdk import FeedbackManager

        FeedbackManager(store).record(
            kind="mute_topic", scope={"topic": "gossip"}, actor="ui-admin",
            now_ms=clock.wall_now_ms(),
        )
        model = ScriptedModel(turns=[
            {"tool_calls": [("c1", "read_evidence", {"fact_id": "fact-evil"})]},
            {"content": (
                '{"decision": "propose", "summary": "转发八卦", "proposals":'
                ' [{"kind": "notify_self", "fact_id": "fact-evil", "revision": "1",'
                '  "body": "x", "arguments": {"topic": "gossip", "urgency": "urgent"},'
                '  "evidence_refs": ["tool:read_evidence:c1"], "expires_at": "'
                + rfc3339(DAY + 3600_000) + '"}]}'
            ), "usage": {"model_calls": 1}},
        ])
        source = ScriptedSource(batches=[static_batch([("fact-evil", "1", "x")])])
        executor = make_executor(model, broker=make_broker(capabilities=("calendar.read",)), clock=clock)
        coordinator = ProactiveCoordinator(
            store,
            registry=make_registry(source=source),
            pack_builder=make_pack_builder(clock=clock),
            executor=executor,
            policy_engine=policy,
        )
        store.admit_event(
            f"job:{job.job_id}:{job.revision}:{clock.wall_now_ms()}",
            origin="scheduler",
            payload={"job_id": job.job_id, "mode": job.mode},
            observed_at_ms=clock.wall_now_ms(),
            expires_at_ms=clock.wall_now_ms() + 3_600_000,
            job_id=job.job_id,
            job_revision=job.revision,
        )
        report = asyncio.run(coordinator.process_pending_run(now_ms=clock.wall_now_ms()))
        self.assertEqual(report.outcome, "proposed")
        self.assertEqual(report.policy_outcome, "completed")
        self.assertEqual(report.suppressed_by_policy, 1)
        # Urgency ("urgent") did not bypass the user's explicit mute
        # (§10.1: 提高优先级不能绕过用户的明确禁区).
        dispatch = asyncio.run(
            OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms())
        )
        self.assertEqual(dispatch, [])
        self.assertEqual(store.list_inbox(), [])
        self.assertEqual(store.list_outbox(), [])
