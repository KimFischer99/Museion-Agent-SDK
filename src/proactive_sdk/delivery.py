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

from datetime import datetime, timezone

from .contracts import ErrorCode, PASError, SourceRequest, canonical_json
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


def _rfc3339_to_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if parsed.tzinfo is None:
        raise PASError(ErrorCode.INVALID_CONFIG, f"timestamp needs an offset: {value!r}")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _ms_to_rfc3339(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


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
        sources: Any | None = None,
        source_deadline_s: int = 10,
    ) -> None:
        self.store = store
        self.config = config if config is not None else DispatchConfig()
        self.policy = policy
        # Optional: when wired, a message whose action declares
        # time-sensitive sources is re-read right before the effect
        # (SPEC §21.1 step 5). Without it, only the frozen message is sent.
        self.sources = sources
        self.source_deadline_s = int(source_deadline_s)
        self._sinks: dict[str, Any] = dict(sinks or {})
        # Which registered sinks are merely the library's convenience
        # default. A caller-supplied transport may replace those, but two
        # caller-supplied transports for one kind remain a conflict.
        self._default_sinks: set[str] = set()
        for kind, sink in self._sinks.items():
            if isinstance(sink, WebhookNotificationSink):
                self._default_sinks.add(kind)
        if "webhook" not in self._sinks:
            self._sinks["webhook"] = WebhookNotificationSink()
            self._default_sinks.add("webhook")

    def register_sink(self, channel_kind: str, sink: Any) -> None:
        """Attach the transport for one channel kind.

        A caller may replace the pre-registered default (that is what the
        ``sinks`` argument is for — before v0.1.2 the default was
        pre-registered *and* un-replaceable, so supplying your own webhook
        transport always raised a conflict). Two caller-supplied transports
        for the same kind are still refused: silently keeping one of them
        would send through a transport the caller did not choose.
        """
        if self._sinks.get(channel_kind) is sink:
            # Registering the same transport again is idempotent: two
            # channels of one kind may legitimately share it, and their
            # difference lives in the per-channel endpoint.
            return
        if channel_kind in self._sinks and channel_kind not in self._default_sinks:
            raise PASError(
                ErrorCode.CONFLICT,
                f"sink for {channel_kind!r} already registered",
                scope="delivery",
            )
        self._sinks[channel_kind] = sink
        self._default_sinks.discard(channel_kind)

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

    #: Outbox state → (activity state, retryable) for the user-visible
    #: delivery phase of the job activity projection (SPEC §21.1 step 7).
    _ACTIVITY_STATES: dict[str, tuple[str, bool]] = {
        "provider_accepted": ("accepted", False),
        "reconciled_delivered": ("delivered", False),
        "stored_in_inbox": ("delivered", False),
        "delivery_unknown": ("unknown", True),
        "failed_retryable": ("queued", True),
        # ``finish_outbox_attempt`` reports a not-yet-exhausted retry as
        # 'pending' (the message stays claimable), which is exactly what the
        # user-visible projection calls 'queued'.
        "pending": ("queued", True),
        "aborted": ("skipped", False),
        "failed_terminal": ("failed", False),
        "suppressed": ("suppressed", False),
        "deferred": ("deferred", True),
        "expired": ("missed", False),
    }

    def _project_delivery(
        self, message_id: str, *, state: str, now_ms: int
    ) -> None:
        """Mirror one delivery outcome onto the job activity projection.

        Best-effort by design: the projection is derived, the outbox and
        the attempt ledger stay authoritative, and a projection failure
        must never turn a successful delivery into a failed one. Both a
        deterministic reminder and an analysis run project here, so
        ``activity_list`` shows execution and notification outcome as
        separate rows for every job that produced a message.
        """
        try:
            message = self.store.get_outbox_message(message_id)
            if message is None:
                return
            action = self.store.action_delivery_context(message["action_id"])
            if action is None:
                return
            request = action.get("request") or {}
            if action.get("source") == "reminder":
                job_id = request.get("job_id")
            else:
                # Analysis-sourced action: the owning job comes from the
                # run → event chain, not from payload text.
                job_id = self.store.job_id_for_action(message["action_id"])
            if not isinstance(job_id, str) or not job_id:
                return
            job = self.store.get_job(job_id)
            if job is None:
                return
            mapped, retryable = self._ACTIVITY_STATES.get(state, (state, False))
            self.store.record_job_activity(
                job_id=job_id,
                job_revision=job.revision,
                phase="delivery",
                state=mapped,
                obligation=action.get("obligation") or job.obligation,
                occurrence_id=action.get("occurrence_id"),
                slot_ms=request.get("planned_at_ms"),
                reason=(message.get("reason") or None),
                planned_at_ms=request.get("planned_at_ms"),
                actual_at_ms=now_ms,
                destination_ref=message["destination_ref"],
                message_id=message_id,
                run_id=action.get("run_id"),
                retryable=retryable,
                # Keyed by message as well as state: a job that produced two
                # messages must show two rows, while a replayed attempt for
                # the *same* message and outcome stays one row.
                dedupe_suffix=f"delivery:{mapped}:{message_id}",
                now_ms=now_ms,
            )
        except Exception:  # noqa: BLE001 - the projection never breaks delivery
            return

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
                self._project_delivery(lease.message_id, state=final, now_ms=now_ms)
                return DispatchReport(lease.message_id, final, reason)
        # §10.4 投递前复验, second half (SPEC §21.1 step 5): a message whose
        # action declares time-sensitive sources is re-read right before the
        # effect, on every channel — "the fact was cancelled" is not a
        # channel-specific fact, and a stale snapshot is never sent as if it
        # were current.
        refresh_action, refresh_reason = await self._pre_delivery_refresh(lease, now_ms=now_ms)
        if refresh_action == "retry":
            final = self.store.finish_outbox_attempt(
                lease,
                outcome="failed_retryable",
                started_at_ms=started_at,
                now_ms=now_ms,
                error_class=refresh_reason,
                retry_not_before_ms=self._backoff_not_before(lease.message_id, now_ms),
                max_attempts=self.config.max_attempts,
            )
            self._project_delivery(lease.message_id, state=final, now_ms=now_ms)
            return DispatchReport(lease.message_id, final, refresh_reason)
        if refresh_action == "suppress":
            final = self.store.finish_outbox_attempt(
                lease,
                outcome="suppressed",
                started_at_ms=started_at,
                now_ms=now_ms,
                error_class=refresh_reason,
            )
            self._project_delivery(lease.message_id, state=final, now_ms=now_ms)
            return DispatchReport(lease.message_id, final, refresh_reason)
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
            self._project_delivery(lease.message_id, state=final, now_ms=now_ms)
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
            self._project_delivery(lease.message_id, state=final, now_ms=now_ms)
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
        self._project_delivery(lease.message_id, state=final, now_ms=now_ms)
        return DispatchReport(lease.message_id, final, result.error_class, result.http_status)

    async def _pre_delivery_refresh(
        self, lease: OutboxLease, *, now_ms: int
    ) -> tuple[str, str | None]:
        """Re-read the sources a message declares as time-sensitive.

        Returns ``("send", None)`` when nothing has to be re-verified or
        everything checks out, ``("retry", reason)`` for a recoverable
        failure (the message keeps its identity, backoff and provider key)
        and ``("suppress", reason)`` when the fact is gone for good.

        The rule that matters: a stale snapshot is never sent "as if" it
        were current. A source that cannot answer defers the message
        instead of letting old facts through (SPEC §21.1 step 5).
        """
        if self.sources is None:
            return ("send", None)
        action = self.store.action_delivery_context(lease.action_id)
        if action is None:
            return ("send", None)
        plan = (action.get("request") or {}).get("refresh")
        if not isinstance(plan, dict):
            return ("send", None)
        source_ids = plan.get("sources") or []
        account_ref = plan.get("account_ref")
        if not source_ids or not isinstance(account_ref, str):
            return ("send", None)
        wanted_facts = set(plan.get("facts") or [])
        deadline = _ms_to_rfc3339(now_ms + self.source_deadline_s * 1000)
        for source_id in source_ids:
            previous = plan.get("watermarks", {}).get(source_id)
            try:
                batch = await self.sources.fetch_delta(
                    SourceRequest(
                        source_id=source_id,
                        account_ref=account_ref,
                        deadline=deadline,
                        cursor_ref=previous,
                    )
                )
            except PASError as exc:
                if exc.code in (ErrorCode.PERMISSION_DENIED, ErrorCode.AUTH_REQUIRED):
                    # 授权撤销: never send what the user may no longer see.
                    return ("suppress", f"source_unauthorized:{source_id}")
                return (
                    "retry",
                    f"source_unavailable:{source_id}:{exc.code.value}",
                )
            fresh_until = batch.fresh_until
            if fresh_until is not None and _rfc3339_to_ms(fresh_until) < now_ms:
                return ("retry", f"source_stale:{source_id}")
            cancelled = [
                item.fact_id
                for item in batch.items
                if item.tombstone and (not wanted_facts or item.fact_id in wanted_facts)
            ]
            if cancelled:
                return ("suppress", f"source_fact_cancelled:{source_id}")
        return ("send", None)

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
            self._project_delivery(message["message_id"], state=state, now_ms=now)
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
