"""Delivery: outbox dispatcher, real notification sinks, feedback
(SPEC §10.2, §10.3, §10.4; SEND-01).

The dispatcher turns queued outbox messages into actual deliveries with
the §10.2 state machine:

    pending → sending → provider_accepted / stored_in_inbox
                      ↘ failed_retryable / failed_terminal / delivery_unknown
    pending → deferred / suppressed / expired
    delivery_unknown → reconciled_delivered | retry after authoritative not-delivered

Rules this module enforces mechanically:

- Network I/O happens OUTSIDE any store transaction (§13.2: 不持锁等待网络).
  claim (fence+1, grant re-check) is one transaction; the result journal +
  message transition is another.
- Every attempt is journaled in ``delivery_attempts`` keyed by the
  message fence — including attempts whose message was revoked/superseded
  mid-flight (the ledger keeps what actually happened, §10.3).
- The provider idempotency key is per MESSAGE, not per attempt: retries
  reuse the identical key (§10.2 重试需 provider 同一幂等 key).
- ``delivery_unknown`` is never auto-retried. Only an authoritative
  provider answer (reconcile) or a real receipt moves it (ACK 丢失不盲发);
  "the provider probably didn't get it" is not an answer.
- The local inbox path writes the inbox row and the stored_in_inbox state
  in the SAME transaction — reliable once-inbox for the owner channel.

``WebhookNotificationSink`` is the real notification sink: loopback-tested
HTTP transport (no in-process mock), bounded responses, no redirect
following, TLS verification on, idempotency header set.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .contracts import ErrorCode, PASError, canonical_json
from .policy import PolicyEngine
from .store import OutboxLease, Store

__all__ = [
    "DeliveryRequest",
    "ReconcileRequest",
    "SinkResult",
    "ReconcileAnswer",
    "WebhookNotificationSink",
    "DispatchConfig",
    "DispatchReport",
    "OutboxDispatcher",
    "FeedbackManager",
]

# HTTP status → §10.2 outcome mapping for the webhook sink.
_RETRYABLE_STATUS = frozenset({408, 429})


@dataclass(frozen=True)
class DeliveryRequest:
    """One send attempt handed to a sink. Carries the message identity so
    the provider can deduplicate (idempotency key = provider_key)."""

    message_id: str
    provider_key: str
    destination_ref: str
    endpoint: dict[str, Any]
    payload: dict[str, Any]


@dataclass(frozen=True)
class ReconcileRequest:
    """One authoritative-status query for an unknown message."""

    message_id: str
    provider_key: str
    endpoint: dict[str, Any]


@dataclass(frozen=True)
class SinkResult:
    """What a send attempt actually produced. ``delivery_unknown`` means
    the request MAY have been accepted — it is a distinct outcome, never
    folded into failure."""

    state: str  # provider_accepted | failed_retryable | failed_terminal | delivery_unknown
    receipt: dict[str, Any] | None = None
    error_class: str | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class ReconcileAnswer:
    """``delivered=None`` = the provider could not answer authoritatively
    (the message must stay delivery_unknown)."""

    delivered: bool | None
    receipt: dict[str, Any] | None = None


# Sinks are duck-typed against ``contracts.DeliverySink`` (§4.2): an
# implementation provides ``send(DeliveryRequest) -> SinkResult`` and
# ``reconcile(ReconcileRequest) -> ReconcileAnswer``. The dispatcher is
# the only caller; authorization already happened in policy.


class WebhookNotificationSink:
    """Real HTTP webhook notification channel (P4's one real sink).

    POST {url} with JSON body, ``Idempotency-Key`` header, bounded
    response read, no redirect following, default TLS verification.
    Transport layer only — authorization happened in policy; the sink
    never decides who may receive what.

    Status mapping: 2xx → provider_accepted; 408/429/5xx →
    failed_retryable; other 4xx → failed_terminal; connection errors,
    timeouts and resets → delivery_unknown (the server may have accepted
    the request before the connection died — the ACK-lost case).
    """

    def __init__(self, *, timeout_s: float = 10.0, max_response_bytes: int = 65536) -> None:
        if timeout_s <= 0 or timeout_s > 120:
            raise PASError(ErrorCode.INVALID_CONFIG, "timeout_s must be in (0, 120]", scope="sink")
        if not 1 <= max_response_bytes <= 1_048_576:
            raise PASError(ErrorCode.INVALID_CONFIG, "max_response_bytes out of range", scope="sink")
        self.timeout_s = timeout_s
        self.max_response_bytes = max_response_bytes

    async def send(self, request: DeliveryRequest) -> SinkResult:
        return await asyncio.to_thread(self._send_sync, request)

    async def reconcile(self, request: ReconcileRequest) -> ReconcileAnswer:
        return await asyncio.to_thread(self._reconcile_sync, request)

    # -- synchronous transport (runs in a worker thread) ----------------- #

    def _opener(self) -> urllib.request.OpenerDirector:
        """Opener that refuses redirects: following one would silently
        replay the POST against a different endpoint (SSRF/rebind surface)."""

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
                return None

        return urllib.request.build_opener(_NoRedirect)

    def _send_sync(self, request: DeliveryRequest) -> SinkResult:
        url = request.endpoint.get("url")
        if not isinstance(url, str) or not url:
            return SinkResult("failed_terminal", error_class="endpoint_missing")
        body = canonical_json(request.payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": request.provider_key,
        }
        extra = request.endpoint.get("headers")
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
        http = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with self._opener().open(http, timeout=self.timeout_s) as response:
                status = response.status
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status in _RETRYABLE_STATUS or 500 <= status <= 599:
                return SinkResult("failed_retryable", error_class=f"http_{status}", http_status=status)
            return SinkResult("failed_terminal", error_class=f"http_{status}", http_status=status)
        except (urllib.error.URLError, TimeoutError, OSError, ConnectionError):
            # Ambiguous: TCP may have delivered the request before dying.
            return SinkResult("delivery_unknown", error_class="transport_unknown")
        if 200 <= status <= 299:
            receipt = self._parse_json_object(raw)
            return SinkResult(
                "provider_accepted",
                receipt={"external_id": (receipt or {}).get("external_id")},
                http_status=status,
            )
        return SinkResult("failed_terminal", error_class=f"http_{status}", http_status=status)

    def _reconcile_sync(self, request: ReconcileRequest) -> ReconcileAnswer:
        """Only an explicit provider statement counts. A 404 or any other
        "didn't find it" signal is NOT authoritative (§10.2: 普通搜索"没找到
        消息"不足以作为权威未发送证明)."""
        status_url = request.endpoint.get("status_url")
        if not isinstance(status_url, str) or not status_url:
            return ReconcileAnswer(None)
        query = urllib.parse.urlencode({"provider_key": request.provider_key})
        separator = "&" if urllib.parse.urlparse(status_url).query else "?"
        http = urllib.request.Request(f"{status_url}{separator}{query}", method="GET")
        try:
            with self._opener().open(http, timeout=self.timeout_s) as response:
                if response.status != 200:
                    return ReconcileAnswer(None)
                body = self._parse_json_object(response.read(self.max_response_bytes + 1))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ConnectionError):
            return ReconcileAnswer(None)
        if not body or not isinstance(body.get("delivered"), bool):
            return ReconcileAnswer(None)
        receipt = {"external_id": body.get("external_id")} if body.get("external_id") else None
        return ReconcileAnswer(body["delivered"], receipt)

    def _parse_json_object(self, raw: bytes) -> dict[str, Any] | None:
        try:
            parsed = json.loads(raw[: self.max_response_bytes].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class DispatchConfig:
    lease_ttl_ms: int = 30_000
    max_attempts: int = 5
    backoff_base_ms: int = 60_000
    backoff_cap_ms: int = 3_600_000

    def __post_init__(self) -> None:
        for name in ("lease_ttl_ms", "max_attempts", "backoff_base_ms", "backoff_cap_ms"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PASError(ErrorCode.INVALID_CONFIG, f"{name} must be a positive integer")


@dataclass(frozen=True)
class DispatchReport:
    """One dispatch outcome — honest per-message accounting."""

    message_id: str
    state: str
    reason: str | None = None
    http_status: int | None = None


class OutboxDispatcher:
    """Drives the §10.2 outbox state machine against real sinks."""

    def __init__(
        self,
        store: Store,
        *,
        sinks: dict[str, Any] | None = None,
        policy: PolicyEngine | None = None,
        config: DispatchConfig | None = None,
    ) -> None:
        self.store = store
        self.config = config if config is not None else DispatchConfig()
        self.policy = policy
        self._sinks: dict[str, Any] = dict(sinks or {})
        if "webhook" not in self._sinks:
            self._sinks["webhook"] = WebhookNotificationSink()

    def register_sink(self, channel_kind: str, sink: Any) -> None:
        if channel_kind in self._sinks:
            raise PASError(ErrorCode.CONFLICT, f"sink for {channel_kind!r} already registered", scope="delivery")
        self._sinks[channel_kind] = sink

    # ------------------------------------------------------------------ #
    # Dispatch loop
    # ------------------------------------------------------------------ #

    async def dispatch_due(self, *, limit: int = 10, now_ms: int | None = None) -> list[DispatchReport]:
        """Promote due deferrals, then claim-and-send until quiet or the
        limit. One message at a time; each message's network call happens
        between transactions."""
        now = self.store.clock.wall_now_ms() if now_ms is None else now_ms
        self.store.expire_due_messages(now_ms=now)
        self.store.promote_deferred(now_ms=now)
        reports: list[DispatchReport] = []
        for _ in range(int(limit)):
            lease = self.store.claim_outbox_message(now_ms=now, ttl_ms=self.config.lease_ttl_ms)
            if lease is None:
                break
            reports.append(await self._dispatch_one(lease, now_ms=now))
        return reports

    async def _dispatch_one(self, lease: OutboxLease, *, now_ms: int) -> DispatchReport:
        started_at = self.store.clock.wall_now_ms()
        # §10.3: the lease proves DB ownership, not that reality stood
        # still — re-read immediately before the effect.
        message = self.store.get_outbox_message(lease.message_id)
        if message is None or message["state"] != "sending" or message["fence"] != lease.fence:
            return DispatchReport(lease.message_id, "aborted_precheck", "message no longer claimable")
        # §10.4 投递前复验: policy state may have changed since queueing.
        if self.policy is not None:
            reason = self.policy.pre_dispatch_recheck(lease, now_ms=now_ms)
            if reason:
                final = self.store.finish_outbox_attempt(
                    lease,
                    outcome="suppressed",
                    started_at_ms=started_at,
                    now_ms=now_ms,
                    error_class=reason,
                )
                return DispatchReport(lease.message_id, final, reason)
        if lease.channel_kind == "local_inbox":
            final = self.store.finish_outbox_attempt(
                lease,
                outcome="stored_in_inbox",
                started_at_ms=started_at,
                now_ms=now_ms,
                inbox_message={
                    "title": lease.payload.get("title"),
                    "body": lease.payload.get("body"),
                },
            )
            return DispatchReport(lease.message_id, final)
        sink = self._sinks.get(lease.channel_kind)
        if sink is None:
            final = self.store.finish_outbox_attempt(
                lease,
                outcome="failed_terminal",
                started_at_ms=started_at,
                now_ms=now_ms,
                error_class="no_sink_for_channel",
            )
            return DispatchReport(lease.message_id, final, "no_sink_for_channel")
        result = await sink.send(
            DeliveryRequest(
                message_id=lease.message_id,
                provider_key=lease.provider_key,
                destination_ref=lease.destination_ref,
                endpoint=self._endpoint_for(lease.destination_ref),
                payload=lease.payload,
            )
        )
        if result.state == "provider_accepted":
            final = self.store.finish_outbox_attempt(
                lease,
                outcome="provider_accepted",
                started_at_ms=started_at,
                now_ms=now_ms,
                receipt=result.receipt,
            )
        elif result.state == "failed_retryable":
            final = self.store.finish_outbox_attempt(
                lease,
                outcome="failed_retryable",
                started_at_ms=started_at,
                now_ms=now_ms,
                receipt=result.receipt,
                error_class=result.error_class,
                retry_not_before_ms=self._backoff_not_before(lease.message_id, now_ms),
                max_attempts=self.config.max_attempts,
            )
        elif result.state == "delivery_unknown":
            final = self.store.finish_outbox_attempt(
                lease,
                outcome="delivery_unknown",
                started_at_ms=started_at,
                now_ms=now_ms,
                receipt=result.receipt,
                error_class=result.error_class,
            )
        else:  # failed_terminal
            final = self.store.finish_outbox_attempt(
                lease,
                outcome="failed_terminal",
                started_at_ms=started_at,
                now_ms=now_ms,
                receipt=result.receipt,
                error_class=result.error_class,
            )
        return DispatchReport(lease.message_id, final, result.error_class, result.http_status)

    def _endpoint_for(self, destination_ref: str) -> dict[str, Any]:
        channel = self.store.get_owner_channel(destination_ref)
        if channel is None:
            return {}
        return channel.get("endpoint") or {}

    def _channel_kind_for(self, destination_ref: str) -> str:
        channel = self.store.get_owner_channel(destination_ref)
        return (channel or {}).get("kind") or "unknown"

    def _backoff_not_before(self, message_id: str, now_ms: int) -> int:
        message = self.store.get_outbox_message(message_id)
        attempts = (message or {}).get("attempts") or 0
        delay = min(self.config.backoff_base_ms * (2**attempts), self.config.backoff_cap_ms)
        return now_ms + delay

    # ------------------------------------------------------------------ #
    # Unknown reconciliation and receipts (§10.2, §10.3)
    # ------------------------------------------------------------------ #

    async def reconcile_unknowns(self, *, limit: int = 10, now_ms: int | None = None) -> list[DispatchReport]:
        """Ask the provider authoritatively about every delivery_unknown
        message. Auto-retry this is not: only an explicit answer moves the
        message; anything ambiguous keeps it parked."""
        now = self.store.clock.wall_now_ms() if now_ms is None else now_ms
        reports: list[DispatchReport] = []
        for message in self.store.list_outbox(state="delivery_unknown", limit=limit):
            sink = self._sinks.get(self._channel_kind_for(message["destination_ref"]))
            if sink is None:
                reports.append(DispatchReport(message["message_id"], "delivery_unknown", "no_sink_for_channel"))
                continue
            answer = await sink.reconcile(
                ReconcileRequest(
                    message_id=message["message_id"],
                    provider_key=message.get("provider_key") or f"pas-{message['action_id']}",
                    endpoint=self._endpoint_for(message["destination_ref"]),
                )
            )
            state = self.store.reconcile_outbox_message(
                message["message_id"],
                delivered=answer.delivered,
                receipt=answer.receipt,
                now_ms=now,
            )
            reports.append(DispatchReport(message["message_id"], state))
        return reports

    def apply_receipt(self, message_id: str, *, receipt: dict[str, Any], now_ms: int | None = None) -> str:
        """Record a provider receipt that arrived out-of-band (webhook
        callback, duplicate callback, late receipt). Idempotent: a
        duplicate changes nothing (§10.3 晚到回执应对账保留)."""
        now = self.store.clock.wall_now_ms() if now_ms is None else now_ms
        return self.store.reconcile_outbox_message(
            message_id, delivered=True, receipt=receipt, now_ms=now
        )


class FeedbackManager:
    """Authenticated user feedback (§14.2 notifications.feedback). Topic
    mutes feed straight back into policy evaluation; handled-fact feedback
    suppresses further notifications about the same fact (§10.1)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def record(
        self,
        *,
        kind: str,
        scope: dict[str, Any] | None = None,
        actor: str,
        message_id: str | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        return self.store.record_feedback(
            kind=kind,
            scope=scope or {},
            actor=actor,
            message_id=message_id,
            now_ms=self.store.clock.wall_now_ms() if now_ms is None else now_ms,
        )

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_feedback(limit=limit)
