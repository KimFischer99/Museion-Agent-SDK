"""P4 delivery tests: outbox dispatcher, attempt journal, idempotency
keys, unknown reconciliation and late receipts (SPEC §10.2, §10.3;
§15.1 P4 acceptance: ACK 丢失不盲发; §16.1 Delivery row).

Transport honesty: the webhook sink is exercised over real loopback HTTP
against a scripted server — no in-process mock transport. That proves the
dispatcher's state machine over real sockets; it does NOT prove
compatibility with any deployed notification provider (P5/P7 gates).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from proactive_sdk import (
    DispatchReport,
    ErrorCode,
    FakeClock,
    OutboxDispatcher,
    OutboxLease,
    PASError,
    PolicyEngine,
    Store,
)
from p4_fixtures import (
    berlin_ms,
    make_stack,
    make_proposed_run,
    notify_proposal,
    rfc3339,
    webhook_channel,
)
from p4_fixtures import ScriptedWebhookServer

DAY = berlin_ms("2026-10-20", 14)


def run(awaitable):
    return asyncio.run(awaitable)


def queue_one_notification(store, clock, policy, job, *, fact_id="fact-1"):
    run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal(fact_id)])
    return policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())


class LocalInboxDispatchTests(unittest.TestCase):
    def test_once_inbox_and_attempt_journal(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        queue_one_notification(store, clock, policy, job)
        dispatcher = OutboxDispatcher(store)
        first = run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual([r.state for r in first], ["stored_in_inbox"])
        message = store.list_outbox()[0]
        self.assertEqual(message["state"], "stored_in_inbox")
        inbox = store.list_inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["title"], "有新的安排")
        self.assertIn("10:00", inbox[0]["body"] or "")
        attempts = store.delivery_attempts_for(message["message_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["state"], "stored_in_inbox")
        self.assertEqual(attempts[0]["fence"], 1)
        # Second dispatch pass: nothing left, no duplicate inbox row.
        second = run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual(second, [])
        self.assertEqual(len(store.list_inbox()), 1)

    def test_inbox_read_marking(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        queue_one_notification(store, clock, policy, job)
        run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
        inbox = store.list_inbox()
        self.assertTrue(store.mark_inbox_read(inbox[0]["inbox_id"], now_ms=clock.wall_now_ms()))
        self.assertFalse(store.mark_inbox_read(inbox[0]["inbox_id"], now_ms=clock.wall_now_ms()))
        self.assertEqual(store.list_inbox(unread_only=True), [])

    def test_stale_fence_finish_is_journeyed_but_not_applied(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        queue_one_notification(store, clock, policy, job)
        lease = store.claim_outbox_message(now_ms=clock.wall_now_ms(), ttl_ms=30_000)
        self.assertIsNotNone(lease)
        stale = OutboxLease(
            message_id=lease.message_id,
            action_id=lease.action_id,
            fence=lease.fence + 5,
            lease_until_ms=lease.lease_until_ms,
            delivery_key=lease.delivery_key,
            destination_ref=lease.destination_ref,
            channel_kind=lease.channel_kind,
            payload=lease.payload,
            provider_key=lease.provider_key,
        )
        final = store.finish_outbox_attempt(
            stale, outcome="stored_in_inbox", started_at_ms=clock.wall_now_ms(),
            now_ms=clock.wall_now_ms(),
        )
        self.assertEqual(final, "aborted")
        self.assertEqual(store.list_outbox()[0]["state"], "sending")
        # The real claimant still finishes successfully.
        final = store.finish_outbox_attempt(
            lease, outcome="stored_in_inbox", started_at_ms=clock.wall_now_ms(),
            now_ms=clock.wall_now_ms(),
        )
        self.assertEqual(final, "stored_in_inbox")


class WebhookSinkTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ScriptedWebhookServer().start()
        from proactive_sdk import WebhookNotificationSink

        self.sink = WebhookNotificationSink(timeout_s=5)

    def tearDown(self):
        self.server.close()

    def _request(self):
        from proactive_sdk import DeliveryRequest

        return DeliveryRequest(
            message_id="msg-transport-1",
            provider_key="pas-action-test",
            destination_ref="push:demo",
            endpoint={"url": self.server.url, "status_url": self.server.status_url},
            payload={"title": "hello", "fact_id": "f1", "revision": "1",
                     "kind": "notify_self", "run_id": "r1", "semantic": "notify_self"},
        )

    def test_200_is_provider_accepted_with_receipt_and_headers(self):
        result = run(self.sink.send(self._request()))
        self.assertEqual(result.state, "provider_accepted")
        self.assertEqual(result.receipt, {"external_id": "ext-1"})
        sent = self.server.requests[0]
        self.assertEqual(sent["headers"]["Idempotency-Key"], "pas-action-test")
        self.assertEqual(sent["headers"]["Content-Type"], "application/json")
        self.assertEqual(sent["body"]["title"], "hello")

    def test_status_mapping(self):
        for status, expected in ((500, "failed_retryable"), (429, "failed_retryable"),
                                 (404, "failed_terminal"), (400, "failed_terminal")):
            self.server.set_post_script({"status": status, "body": {}})
            result = run(self.sink.send(self._request()))
            self.assertEqual(result.state, expected, f"status {status}")
            self.assertEqual(result.http_status, status)

    def test_ack_loss_is_delivery_unknown_not_failure(self):
        self.server.set_post_script({"drop": True})
        result = run(self.sink.send(self._request()))
        self.assertEqual(result.state, "delivery_unknown")
        # The server DID process the request — that is exactly why the
        # outcome is unknown, not failed.
        self.assertEqual(len(self.server.requests), 1)

    def test_redirects_are_not_followed(self):
        self.server.set_post_script({"status": 302, "body": {}})
        result = run(self.sink.send(self._request()))
        self.assertEqual(result.state, "failed_terminal")
        self.assertEqual(len(self.server.requests), 1)

    def test_reconcile_requires_explicit_provider_statement(self):
        from proactive_sdk import ReconcileRequest

        request = ReconcileRequest(
            message_id="msg-1", provider_key="pas-action-test",
            endpoint={"status_url": self.server.status_url},
        )
        # 404 is "didn't find it", NOT authoritative not-delivered (§10.2).
        self.server.set_get_script(None)
        self.assertEqual(run(self.sink.reconcile(request)).delivered, None)
        self.server.set_get_script({"status": 200, "body": {"maybe": True}})
        self.assertEqual(run(self.sink.reconcile(request)).delivered, None)
        self.server.set_get_script({"status": 200, "body": {"delivered": True,
                                                            "external_id": "ext-7"}})
        answer = run(self.sink.reconcile(request))
        self.assertEqual(answer.delivered, True)
        self.assertEqual(answer.receipt, {"external_id": "ext-7"})
        self.server.set_get_script({"status": 200, "body": {"delivered": False}})
        self.assertEqual(run(self.sink.reconcile(request)).delivered, False)


class AckLostNoBlindResendTests(unittest.TestCase):
    """§15.1 P4 acceptance: ACK 丢失不盲发 (§10.2 delivery_unknown)."""

    def test_unknown_is_never_auto_retried(self):
        server = ScriptedWebhookServer().start()
        try:
            store, clock, registry, policy, job = make_stack(
                channels=[webhook_channel(endpoint={"url": server.url, "status_url": server.status_url})],
                delivery_policy={"notification_profile": "push:demo"},
            )
            clock.set_wall(DAY)
            queue_one_notification(store, clock, policy, job)
            dispatcher = OutboxDispatcher(store)
            server.set_post_script({"drop": True})
            reports = run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            self.assertEqual([r.state for r in reports], ["delivery_unknown"])
            message = store.list_outbox()[0]
            self.assertEqual(message["state"], "delivery_unknown")
            self.assertEqual(message["reason"], "ack_missing")
            # Lease expiry alone does NOT re-arm the message: five more
            # dispatch passes with the clock pushed far ahead must not
            # resend (the provider may have accepted the first request).
            for _ in range(5):
                clock.advance_wall(3_600_000)
                reports = run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
                self.assertEqual(reports, [])
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(store.delivery_attempts_for(message["message_id"]).__len__(), 1)
        finally:
            server.close()

    def test_authoritative_not_delivered_requeues_with_same_provider_key(self):
        server = ScriptedWebhookServer().start()
        try:
            store, clock, registry, policy, job = make_stack(
                channels=[webhook_channel(endpoint={"url": server.url, "status_url": server.status_url})],
                delivery_policy={"notification_profile": "push:demo"},
            )
            clock.set_wall(DAY)
            queue_one_notification(store, clock, policy, job)
            dispatcher = OutboxDispatcher(store)
            server.set_post_script({"drop": True})
            run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            provider_key = store.list_outbox()[0]["provider_key"]

            # Reconcile: the provider authoritatively states NOT delivered.
            server.set_get_script({"status": 200, "body": {"delivered": False}})
            reports = run(dispatcher.reconcile_unknowns(now_ms=clock.wall_now_ms()))
            self.assertEqual([r.state for r in reports], ["pending"])
            message = store.list_outbox()[0]
            self.assertEqual(message["state"], "pending")
            self.assertEqual(message["provider_key"], provider_key)

            # Retry with the SAME idempotency key now succeeds.
            server.set_post_script({"status": 200, "body": {"external_id": "ext-2"}})
            reports = run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            self.assertEqual([r.state for r in reports], ["provider_accepted"])
            keys = {r["headers"]["Idempotency-Key"] for r in server.requests}
            self.assertEqual(keys, {provider_key})
        finally:
            server.close()

    def test_unknown_stays_parked_without_authoritative_source(self):
        server = ScriptedWebhookServer().start()
        try:
            store, clock, registry, policy, job = make_stack(
                channels=[webhook_channel(endpoint={"url": server.url})],
                delivery_policy={"notification_profile": "push:demo"},
            )
            # No status_url configured: the provider cannot answer
            # authoritatively, so reconcile must leave the message parked.
            clock.set_wall(DAY)
            queue_one_notification(store, clock, policy, job)
            dispatcher = OutboxDispatcher(store)
            server.set_post_script({"drop": True})
            run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            reports = run(dispatcher.reconcile_unknowns(now_ms=clock.wall_now_ms()))
            self.assertEqual([r.state for r in reports], ["delivery_unknown"])
            self.assertEqual(store.list_outbox()[0]["state"], "delivery_unknown")
        finally:
            server.close()

    def test_authoritative_delivered_closes_unknown(self):
        server = ScriptedWebhookServer().start()
        try:
            store, clock, registry, policy, job = make_stack(
                channels=[webhook_channel(endpoint={"url": server.url, "status_url": server.status_url})],
                delivery_policy={"notification_profile": "push:demo"},
            )
            clock.set_wall(DAY)
            queue_one_notification(store, clock, policy, job)
            dispatcher = OutboxDispatcher(store)
            server.set_post_script({"drop": True})
            run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            server.set_get_script({"status": 200, "body": {"delivered": True,
                                                           "external_id": "ext-9"}})
            reports = run(dispatcher.reconcile_unknowns(now_ms=clock.wall_now_ms()))
            self.assertEqual([r.state for r in reports], ["reconciled_delivered"])
            self.assertEqual(store.list_outbox()[0]["state"], "reconciled_delivered")
        finally:
            server.close()


class ReceiptAndRetryTests(unittest.TestCase):
    def test_retry_backoff_and_stable_idempotency_key(self):
        server = ScriptedWebhookServer().start()
        try:
            store, clock, registry, policy, job = make_stack(
                channels=[webhook_channel(endpoint={"url": server.url, "status_url": server.status_url})],
                delivery_policy={"notification_profile": "push:demo"},
            )
            clock.set_wall(DAY)
            queue_one_notification(store, clock, policy, job)
            dispatcher = OutboxDispatcher(store)
            server.set_post_script({"status": 500, "body": {}})
            reports = run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            self.assertEqual([r.state for r in reports], ["pending"])
            message = store.list_outbox()[0]
            self.assertGreater(message["not_before_ms"], clock.wall_now_ms())
            # Backoff not elapsed: nothing to do.
            more = run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            self.assertEqual(more, [])
            server.set_post_script({"status": 200, "body": {"external_id": "ext-ok"}})
            clock.set_wall(message["not_before_ms"] + 1)
            reports = run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            self.assertEqual([r.state for r in reports], ["provider_accepted"])
            keys = {r["headers"]["Idempotency-Key"] for r in server.requests}
            self.assertEqual(len(keys), 1)
            attempts = store.delivery_attempts_for(message["message_id"])
            self.assertEqual([a["state"] for a in attempts],
                             ["failed_retryable", "provider_accepted"])
        finally:
            server.close()

    def test_retry_budget_exhausts_to_failed_terminal(self):
        server = ScriptedWebhookServer().start()
        try:
            store, clock, registry, policy, job = make_stack(
                channels=[webhook_channel(endpoint={"url": server.url, "status_url": server.status_url})],
                delivery_policy={"notification_profile": "push:demo"},
            )
            clock.set_wall(DAY)
            queue_one_notification(store, clock, policy, job)
            dispatcher = OutboxDispatcher(
                store, config=__import__("proactive_sdk", fromlist=["DispatchConfig"]).DispatchConfig(max_attempts=2)
            )
            server.set_post_script({"status": 500, "body": {}})
            run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            first = store.list_outbox()[0]
            clock.set_wall(first["not_before_ms"] + 1)
            reports = run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            self.assertEqual([r.state for r in reports], ["failed_terminal"])
            self.assertEqual(store.list_outbox()[0]["attempts"], 2)
        finally:
            server.close()

    def test_late_receipt_reconciles_failed_terminal(self):
        server = ScriptedWebhookServer().start()
        try:
            store, clock, registry, policy, job = make_stack(
                channels=[webhook_channel(endpoint={"url": server.url, "status_url": server.status_url})],
                delivery_policy={"notification_profile": "push:demo"},
            )
            clock.set_wall(DAY)
            queue_one_notification(store, clock, policy, job)
            dispatcher = OutboxDispatcher(store)
            server.set_post_script({"status": 400, "body": {}})
            run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            message = store.list_outbox()[0]
            self.assertEqual(message["state"], "failed_terminal")
            # A late real receipt proves our belief wrong (§10.3).
            state = dispatcher.apply_receipt(
                message["message_id"], receipt={"external_id": "ext-late"},
                now_ms=clock.wall_now_ms(),
            )
            self.assertEqual(state, "reconciled_delivered")
            # Duplicate callback: idempotent, nothing changes.
            again = dispatcher.apply_receipt(
                message["message_id"], receipt={"external_id": "ext-late"},
                now_ms=clock.wall_now_ms(),
            )
            self.assertEqual(again, "already_delivered")
            self.assertEqual(store.list_outbox()[0]["state"], "reconciled_delivered")
        finally:
            server.close()

    def test_duplicate_receipt_after_delivery_is_a_noop(self):
        server = ScriptedWebhookServer().start()
        try:
            store, clock, registry, policy, job = make_stack(
                channels=[webhook_channel(endpoint={"url": server.url, "status_url": server.status_url})],
                delivery_policy={"notification_profile": "push:demo"},
            )
            clock.set_wall(DAY)
            queue_one_notification(store, clock, policy, job)
            dispatcher = OutboxDispatcher(store)
            run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
            message = store.list_outbox()[0]
            self.assertEqual(message["state"], "provider_accepted")
            state = dispatcher.apply_receipt(
                message["message_id"], receipt={"external_id": "ext-1"},
                now_ms=clock.wall_now_ms(),
            )
            self.assertEqual(state, "already_delivered")
        finally:
            server.close()

    def test_receipt_before_first_send_is_unsolicited(self):
        server = ScriptedWebhookServer().start()
        try:
            store, clock, registry, policy, job = make_stack(
                channels=[webhook_channel(endpoint={"url": server.url, "status_url": server.status_url})],
                delivery_policy={"notification_profile": "push:demo"},
            )
            clock.set_wall(DAY)
            queue_one_notification(store, clock, policy, job)
            dispatcher = OutboxDispatcher(store)
            message = store.list_outbox()[0]
            # A receipt for something never sent claims the impossible.
            state = dispatcher.apply_receipt(
                message["message_id"], receipt={"external_id": "x"},
                now_ms=clock.wall_now_ms(),
            )
            # Unsolicited: the message stays pending, nothing is marked
            # delivered on the strength of an impossible receipt.
            self.assertEqual(state, "pending")
            self.assertEqual(store.list_outbox()[0]["state"], "pending")
        finally:
            server.close()


class DispatcherGuardTests(unittest.TestCase):
    def test_two_connections_cannot_double_claim(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        queue_one_notification(store, clock, policy, job)
        lease1 = store.claim_outbox_message(now_ms=clock.wall_now_ms(), ttl_ms=30_000)
        self.assertIsNotNone(lease1)
        lease2 = store.claim_outbox_message(now_ms=clock.wall_now_ms(), ttl_ms=30_000)
        self.assertIsNone(lease2)  # the only due message is 'sending' now

    def test_revoked_between_claim_and_finish_aborts_commit(self):
        from proactive_sdk import GrantManager

        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        queue_one_notification(store, clock, policy, job)
        lease = store.claim_outbox_message(now_ms=clock.wall_now_ms(), ttl_ms=30_000)
        for grant in GrantManager(store).active(now_ms=clock.wall_now_ms()):
            GrantManager(store).revoke(grant.grant_id, now_ms=clock.wall_now_ms())
        final = store.finish_outbox_attempt(
            lease, outcome="provider_accepted", started_at_ms=clock.wall_now_ms(),
            now_ms=clock.wall_now_ms(),
        )
        self.assertEqual(final, "aborted")
        self.assertEqual(store.list_outbox()[0]["state"], "suppressed")

    def test_claim_suppresses_message_whose_grant_died_earlier(self):
        from proactive_sdk import GrantManager

        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        queue_one_notification(store, clock, policy, job)
        for grant in GrantManager(store).active(now_ms=clock.wall_now_ms()):
            GrantManager(store).revoke(grant.grant_id, now_ms=clock.wall_now_ms())
        # The message was already suppressed by the revoke cascade; a
        # dispatcher pass finds nothing claimable either way.
        lease = store.claim_outbox_message(now_ms=clock.wall_now_ms(), ttl_ms=30_000)
        self.assertIsNone(lease)

    def test_dispatch_reports_carry_reasons(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        queue_one_notification(store, clock, policy, job)
        reports = run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
        self.assertEqual(len(reports), 1)
        self.assertIsInstance(reports[0], DispatchReport)
        self.assertIsNone(reports[0].reason)

    def test_dispatcher_uses_fake_clock_now(self):
        store, clock, _, policy, job = make_stack()
        clock.set_wall(DAY)
        queue_one_notification(store, clock, policy, job)
        dispatcher = OutboxDispatcher(store)
        run(dispatcher.dispatch_due())  # now_ms=None → FakeClock's wall time
        self.assertEqual(store.list_outbox()[0]["state"], "stored_in_inbox")


if __name__ == "__main__":
    unittest.main()


class SinkRegistrationTests(unittest.TestCase):
    """The `sinks=` argument must actually be usable for webhook channels."""

    def _store(self):
        from proactive_sdk import FakeClock, Store

        return Store(
            ":memory:", profile="sink", owner_destination="local-inbox:sink",
            clock=FakeClock(wall_ms=1_760_000_000_000),
        )

    def test_a_caller_supplied_webhook_transport_replaces_the_default(self):
        from proactive_sdk import OutboxDispatcher, WebhookNotificationSink

        store = self._store()
        mine = WebhookNotificationSink()
        dispatcher = OutboxDispatcher(store, sinks={"webhook": mine})
        self.assertIs(dispatcher._sinks["webhook"], mine)

    def test_registering_a_second_caller_transport_for_one_kind_conflicts(self):
        from proactive_sdk import ErrorCode, OutboxDispatcher, PASError, WebhookNotificationSink

        dispatcher = OutboxDispatcher(self._store())
        dispatcher.register_sink("custom", WebhookNotificationSink())
        with self.assertRaises(PASError) as ctx:
            dispatcher.register_sink("custom", WebhookNotificationSink())
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)

    def test_the_default_can_be_replaced_once_through_register_sink(self):
        from proactive_sdk import OutboxDispatcher, WebhookNotificationSink

        dispatcher = OutboxDispatcher(self._store())
        mine = WebhookNotificationSink()
        dispatcher.register_sink("webhook", mine)
        self.assertIs(dispatcher._sinks["webhook"], mine)

    def test_registering_the_same_transport_twice_is_idempotent(self):
        """Two channels of one kind may share a transport legitimately."""
        from proactive_sdk import OutboxDispatcher, WebhookNotificationSink

        dispatcher = OutboxDispatcher(self._store())
        mine = WebhookNotificationSink()
        dispatcher.register_sink("webhook", mine)
        dispatcher.register_sink("webhook", mine)  # must not raise
        self.assertIs(dispatcher._sinks["webhook"], mine)
