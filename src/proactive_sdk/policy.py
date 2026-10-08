"""Policy layer: grants, frozen-parameter approvals, and the pre-delivery
policy engine (SPEC §9, §10.1, §10.4; POLICY-01).

Trust model (§9.1, §14.1): grants and owner channels are created only by
the trusted setup path (``GrantManager`` / ``OwnerChannelRegistry`` are
wired by the composition root). Model output — ActionProposal fields and
arguments — is *data*: it can never create a grant, pick a destination,
or extend a scope. ``notify_self`` proposals whose arguments even mention
a ``destination`` are suppressed, not redirected (本人目标不可替换).

Evaluation is hard-policy-first and runs in two phases matching §4.3:

- phase A (``apply_to_run``): every run proposal gets a machine verdict;
  one transaction moves the run proposed → policy_evaluated.
- phase B (same call): queueable entries become actions + outbox rows in
  ONE transaction; approval-needing actions freeze a canonical request
  (hash-bound); the rest settle the run actions_queued/completed.

Deferral, never violation: quiet hours and the daily quota defer a
message to the next allowed moment (or suppress it if it would arrive
stale) — the hard gate is "no send inside the quiet window", not "send
later at any cost". The dispatcher re-checks revocation inside its claim
transaction and topic mutes right before the effect (§10.4 投递前复验).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .artifacts import ArtifactRefError, missing_artifact_refs, normalize_artifact_ref
from .contracts import (
    NOTIFY_SELF_CAPABILITY,
    validate_schedule,
    OBLIGATION_DUE,
    ErrorCode,
    PASError,
    canonical_json,
    content_hash,
)
from .store import Store
from .windows import local_day_end_ms, local_day_start_ms, quiet_end_ms

__all__ = [
    "POLICY_VERSION",
    "NOTIFY_SELF_CAPABILITY",
    "PolicyConfig",
    "PolicyRunReport",
    "GrantManager",
    "OwnerChannelRegistry",
    "ApprovalManager",
    "PolicyEngine",
]

POLICY_VERSION = "1.0"

# Proposal kinds that only produce local artifacts (no delivery, no
# approval) — §9.1 "创建本地草稿、记录证据" tier.
#
# `suggest_watch` used to sit here, which meant policy filed it as a
# `run_events` note and stopped: the user never saw it and it never became
# a task (SPEC §22.1 item 7). It now has its own path that freezes a
# pending suggestion the user can accept or decline.
_LOCAL_KINDS = frozenset({"draft", "internal_record"})

_WATCH_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: Host-declared proactive pacing. Only ever a *soft* pacing preference:
#: it can never raise a job's obligation, bypass a quiet window, a mute, a
#: grant check or the business-key dedup.
_CADENCES = frozenset({"warm", "balanced", "gentle"})


@dataclass(frozen=True)
class PolicyConfig:
    """Policy defaults. Per-job ``delivery_policy`` overrides quotas and
    quiet hours; hard rules (grant, receiver binding) are not configurable
    per job."""

    notification_profile: str = "owner-default"
    default_message_ttl_ms: int = 7 * 24 * 3600 * 1000
    approval_ttl_ms: int = 3 * 24 * 3600 * 1000
    max_per_day: int | None = None
    # SPEC §21.1 step 8: a host-declared proactive cadence. The *name*
    # routes product behaviour; the numbers are the host's, never an SDK
    # default — with no numbers configured the cadence adds no gate
    # (不把参考产品的小时数写成 SDK 默认值).
    cadence: str = "balanced"
    cadence_min_gap_seconds: int | None = None
    cadence_max_per_day: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.notification_profile, str) or not self.notification_profile:
            raise PASError(ErrorCode.INVALID_CONFIG, "notification_profile must be a non-empty string")
        positive = ("default_message_ttl_ms", "approval_ttl_ms")
        for name in positive:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PASError(ErrorCode.INVALID_CONFIG, f"{name} must be a positive integer")
        if self.max_per_day is not None and (
            not isinstance(self.max_per_day, int) or isinstance(self.max_per_day, bool) or self.max_per_day < 1
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "max_per_day must be a positive integer or None")
        if self.cadence not in _CADENCES:
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"cadence must be one of {sorted(_CADENCES)}"
            )
        for name in ("cadence_min_gap_seconds", "cadence_max_per_day"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 1
            ):
                raise PASError(
                    ErrorCode.INVALID_CONFIG, f"{name} must be a positive integer or None"
                )


@dataclass(frozen=True)
class PolicyRunReport:
    """What policy evaluation actually did to one run (§4.3)."""

    run_id: str
    outcome: str  # completed | actions_queued | waiting_for_approval
    queued: int = 0
    deferred: int = 0
    approval_pending: int = 0
    local_records: int = 0
    suppressed: int = 0
    duplicates: int = 0
    #: Pending watch suggestions frozen during this run (SPEC §22.1 item 7).
    watch_suggestions: int = 0
    reasons: tuple[str, ...] = ()


class GrantManager:
    """Typed wrapper over the store's grant rows. This is the trusted
    entry point for authorization data — model-facing code only ever
    reads capability snapshots."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def create(
        self,
        *,
        capability: str,
        account_ref: str,
        scope: dict[str, Any] | None = None,
        consent_evidence_ref: str,
        expires_at_ms: int | None = None,
        now_ms: int | None = None,
    ):
        return self.store.create_grant(
            capability=capability,
            account_ref=account_ref,
            scope=scope or {},
            consent_evidence_ref=consent_evidence_ref,
            expires_at_ms=expires_at_ms,
            now_ms=self.store.clock.wall_now_ms() if now_ms is None else now_ms,
        )

    def revoke(self, grant_id: str, *, now_ms: int | None = None) -> int:
        return self.store.revoke_grant(
            grant_id, now_ms=self.store.clock.wall_now_ms() if now_ms is None else now_ms
        )

    def get(self, grant_id: str):
        return self.store.get_grant(grant_id)

    def active(self, *, now_ms: int) -> list:
        return [g for g in self.store.list_grants() if g.is_active(now_ms)]

    def active_capabilities(self, *, now_ms: int) -> frozenset[str]:
        return self.store.active_capabilities(now_ms=now_ms)


class OwnerChannelRegistry:
    """Trusted binding of personal notification channels (§9.1). The
    owner's channels are registered during setup; ``resolve`` is the only
    path from a notification profile name to a channel, and it never
    consults model output."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def register(
        self,
        *,
        channel_ref: str,
        kind: str,
        endpoint: dict[str, Any] | None = None,
        push_summary_only: bool = True,
        enabled: bool = True,
        now_ms: int | None = None,
    ) -> None:
        if kind == "local_inbox" and channel_ref != self.store.owner_destination:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "the local inbox channel must be the store's bound owner destination",
                scope="channels",
            )
        self.store.register_owner_channel(
            channel_ref=channel_ref,
            kind=kind,
            endpoint=endpoint or {},
            push_summary_only=push_summary_only,
            enabled=enabled,
            now_ms=self.store.clock.wall_now_ms() if now_ms is None else now_ms,
        )

    def resolve(self, profile_or_ref: str, *, now_ms: int) -> dict[str, Any]:
        """Resolve a notification profile name (or explicit channel ref)
        to an enabled channel record. Unknown or disabled → error, never
        a fallback to another channel (本人目标不可替换)."""
        channel = self.store.get_owner_channel(profile_or_ref)
        if channel is None and profile_or_ref == "owner-default":
            channel = self.store.get_owner_channel(self.store.owner_destination)
        if channel is None:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"notification profile {profile_or_ref!r} has no registered owner channel",
                scope="channels",
            )
        if not channel["enabled"]:
            raise PASError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                f"owner channel {profile_or_ref!r} is disabled",
                scope="channels",
            )
        return channel


class ApprovalManager:
    """Frozen-parameter approvals (§9.2). ``request`` freezes the whole
    canonical request — kind, account, receiver, normalized arguments,
    attachment hashes, evidence — and approval binds its hash. Resolution
    requires an authenticated actor string supplied by the control plane;
    there is no model-facing resolve."""

    def __init__(self, store: Store, *, ttl_ms: int | None = None) -> None:
        self.store = store
        self.ttl_ms = ttl_ms if ttl_ms is not None else PolicyConfig().approval_ttl_ms

    def request(self, *, grant_id: str, request: dict[str, Any], now_ms: int) -> Any:
        return self.store.create_approval(
            request=request, grant_id=grant_id, ttl_ms=self.ttl_ms, now_ms=now_ms
        )

    def resolve(self, approval_id: str, *, approve: bool, actor: str, now_ms: int) -> Any:
        return self.store.resolve_approval(
            approval_id, approve=approve, actor=actor, now_ms=now_ms
        )

    def pending(self) -> list:
        return self.store.list_approvals(state="pending")


class PolicyEngine:
    """Evaluates run proposals against grants, mutes, quiet hours, the
    sent ledger and the daily quota, then queues what survives (§10.1,
    §10.4). Pure decision logic + store transactions; no network."""

    def __init__(
        self,
        store: Store,
        *,
        channels: OwnerChannelRegistry,
        config: PolicyConfig | None = None,
    ) -> None:
        self.store = store
        self.channels = channels
        self.config = config if config is not None else PolicyConfig()

    # ------------------------------------------------------------------ #
    # Run pipeline (§4.3 proposed → policy_evaluated → …)
    # ------------------------------------------------------------------ #

    def apply_to_run(self, run_id: str, *, now_ms: int | None = None) -> PolicyRunReport:
        now = self.store.clock.wall_now_ms() if now_ms is None else now_ms
        run = self.store.get_run(run_id)
        if run is None:
            raise PASError(ErrorCode.INVALID_CONFIG, f"unknown run {run_id!r}", scope="policy")
        event = self.store.get_event(run["event_id"])
        if event is None:
            raise PASError(ErrorCode.INTERNAL_ERROR, "run references a missing event", scope="policy")
        job = self.store.get_job(event.job_id) if event.job_id else None
        goal_id = job.job_id if job is not None else f"event:{event.event_id[:16]}"
        if job is not None:
            delivery_policy = dict(job.delivery_policy)
            grant_refs = tuple(job.grant_refs)
            # Trusted task configuration only: a proposal can never claim
            # that it owes the user a notification (SPEC §21.1 step 3).
            obligation = job.obligation
        else:
            # A wake with no job has no standing scope. It only gets one when
            # a trusted entry bound an authorization context to *this*
            # event — the generic manual/hook path never can, so being able
            # to raise a wake is not the same as being able to authorize
            # what it produces (SPEC §22.1 item 6). Anything absent stays
            # absent: no silent fallback to a default profile or channel.
            authorization = event.authorization or {}
            delivery_policy = dict(authorization.get("delivery_policy") or {})
            grant_refs = tuple(authorization.get("grant_refs") or ())
            obligation = "opportunistic"

        proposals = self.store.run_proposals(run_id)
        verdicts: dict[str, dict[str, Any]] = {}
        entries: list[dict[str, Any]] = []
        reasons: list[str] = []
        local_notes: list[str] = []
        counters = {
            "queued": 0,
            "deferred": 0,
            "approval_pending": 0,
            "local_records": 0,
            "suppressed": 0,
            "watch_suggestions": 0,
        }
        queued_in_batch = 0
        for proposal in proposals:
            verdict, entry = self.evaluate_proposal(
                proposal,
                goal_id=goal_id,
                delivery_policy=delivery_policy,
                grant_refs=grant_refs,
                run_id=run_id,
                now_ms=now,
                queued_in_batch=queued_in_batch,
                obligation=obligation,
            )
            verdicts[proposal["proposal_id"]] = verdict
            if entry is not None:
                entries.append(entry)
            outcome = verdict["outcome"]
            if outcome == "queued":
                counters["deferred" if verdict.get("not_before_ms", now) > now else "queued"] += 1
                queued_in_batch += 1
            elif outcome == "approval_required":
                counters["approval_pending"] += 1
            elif outcome == "local_record":
                counters["local_records"] += 1
                local_notes.append(f"{verdict.get('reason', 'local')}:{proposal.get('fact_id')}"[:200])
            elif outcome == "watch_suggested":
                counters["watch_suggestions"] += 1
                local_notes.append(
                    f"watch_suggested:{verdict.get('job_name')}->{verdict.get('suggestion_id')}"[:200]
                )
            elif outcome == "suppressed":
                counters["suppressed"] += 1
                reasons.append(f"{proposal['proposal_id']}:{verdict.get('reason', 'policy')}")
            if verdict.get("reason") and outcome not in ("suppressed", "local_record"):
                reasons.append(f"{proposal['proposal_id']}:{verdict['reason']}")

        self.store.record_policy_verdicts(run_id, verdicts=verdicts, now_ms=now)
        counts = self.store.queue_run_actions(run_id, entries=entries, now_ms=now)
        for note in local_notes:
            self.store.record_run_note(run_id, kind="local_record", summary=note, now_ms=now)
        final_state = self._run_final_state(run_id, counts)
        return PolicyRunReport(
            run_id=run_id,
            outcome=final_state,
            queued=counts["queued"],
            deferred=counters["deferred"],
            approval_pending=counts["approval_pending"],
            local_records=counters["local_records"],
            suppressed=counters["suppressed"],
            watch_suggestions=counters["watch_suggestions"],
            duplicates=counts["duplicates"],
            reasons=tuple(reasons),
        )

    def _run_final_state(self, run_id: str, counts: dict[str, int]) -> str:
        row = self.store.get_run(run_id)
        state = row["state"] if row else "completed"
        # 'waiting_for_approval' wins over 'actions_queued' over 'completed'.
        if state == "waiting_for_approval":
            return state
        if counts["approval_pending"]:
            return "waiting_for_approval"
        if counts["queued"]:
            return "actions_queued"
        return state if state in ("actions_queued", "completed") else "completed"

    # ------------------------------------------------------------------ #
    # Single-proposal evaluation (hard policy first)
    # ------------------------------------------------------------------ #

    def evaluate_proposal(
        self,
        proposal: dict[str, Any],
        *,
        goal_id: str,
        delivery_policy: dict[str, Any],
        grant_refs: tuple[str, ...],
        run_id: str,
        now_ms: int,
        queued_in_batch: int = 0,
        obligation: str = "opportunistic",
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Return (verdict dict, queueing entry or None). The verdict is
        persisted on the proposal row; the entry is what phase B queues."""
        kind = proposal["kind"]
        arguments = proposal.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}

        # -- receiver binding: the model never names receivers (§9.1) ----
        if "destination" in arguments or "receiver" in arguments or "to" in arguments:
            return self._suppressed("receiver_forbidden")

        # -- artifact references (§21.1 step 8) --------------------------
        artifacts, reason = self._artifact_refs(proposal, arguments)
        if reason:
            return self._suppressed(reason)

        if kind == "suggest_watch":
            return self._evaluate_suggest_watch(
                proposal, arguments, goal_id, grant_refs, run_id, now_ms
            )

        if kind in _LOCAL_KINDS:
            # §9.1 "创建本地草稿、记录证据": these never leave the machine
            # and carry no external effect — the run_events ledger IS the
            # local record. No grant, no approval, no outbox row; dedup
            # (business keys) applies to deliveries only.
            return {"outcome": "local_record", "reason": kind}, None

        if kind == "notify_self":
            return self._evaluate_notify_self(
                proposal, arguments, goal_id, delivery_policy, grant_refs, run_id, now_ms,
                queued_in_batch=queued_in_batch, obligation=obligation, artifacts=artifacts,
            )
        if kind == "request_external_action":
            return self._evaluate_external_action(
                proposal, arguments, goal_id, delivery_policy, grant_refs, run_id, now_ms,
                queued_in_batch=queued_in_batch, obligation=obligation, artifacts=artifacts,
            )
        return self._suppressed("kind_not_policy_managed")

    def _evaluate_notify_self(
        self,
        proposal: dict[str, Any],
        arguments: dict[str, Any],
        goal_id: str,
        delivery_policy: dict[str, Any],
        grant_refs: tuple[str, ...],
        run_id: str,
        now_ms: int,
        queued_in_batch: int = 0,
        obligation: str = "opportunistic",
        artifacts: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        # Grant: an active long-lived owner-notification permission.
        grant = self._active_grant_for(grant_refs, NOTIFY_SELF_CAPABILITY)
        if grant is None:
            return self._suppressed("grant_missing")

        # Destination: notification profile → registered owner channel.
        profile_name = delivery_policy.get("notification_profile", self.config.notification_profile)
        try:
            channel = self.channels.resolve(profile_name, now_ms=now_ms)
        except PASError as exc:
            return self._suppressed(f"channel_unavailable:{exc.safe_message[:120]}")
        if channel["kind"] == "local_inbox" and channel["channel_ref"] != self.store.owner_destination:
            # Defense in depth; the registry already enforces this.
            return self._suppressed("channel_not_owner_bound")

        topic = arguments.get("topic")
        preference_reason = self._proactive_preference_reason(topic, obligation)
        if preference_reason:
            return self._suppressed(preference_reason)
        reason = self._suppression_checks(proposal, topic, delivery_policy, now_ms)
        if reason:
            return self._suppressed(reason)
        news_snapshot, reason = self._news_search_snapshot_binding(proposal, run_id, now_ms=now_ms)
        if reason:
            return self._suppressed(reason)

        # Business dedup before anything else is computed.
        business_key = self._business_key(goal_id, proposal["fact_id"], proposal.get("revision") or "0",
                                          channel["channel_ref"], "notify_self")
        if self.store.has_business_key(business_key):
            return self._suppressed("duplicate_business_key")

        not_before, defer_reason = self._timing(
            delivery_policy, proposal.get("expires_at_ms"), now_ms,
            queued_in_batch=queued_in_batch, obligation=obligation,
        )
        if not_before is None:
            return self._suppressed(defer_reason)

        payload = self._notification_payload(proposal, run_id, channel, "notify_self")
        if artifacts:
            payload["artifact_refs"] = list(artifacts)
        request = self._frozen_request(
            kind="notify_self",
            account_ref=grant.account_ref,
            destination_ref=channel["channel_ref"],
            arguments=arguments,
            proposal=proposal,
            payload=payload,
        )
        if news_snapshot is not None:
            request["news_search_snapshot"] = news_snapshot
        request["obligation"] = obligation
        entry = self._entry(
            proposal=proposal,
            kind="notify_self",
            business_key=business_key,
            request=request,
            request_hash=content_hash(request),
            destination_ref=channel["channel_ref"],
            payload=payload,
            not_before_ms=not_before,
            expires_at_ms=self._expires(proposal, now_ms),
            grant_id=grant.grant_id,
            reason=defer_reason,
            now_ms=now_ms,
        )
        verdict = {"outcome": "queued", "destination_ref": channel["channel_ref"]}
        if not_before > now_ms:
            verdict["not_before_ms"] = not_before
            verdict["reason"] = defer_reason or "deferred"
        return verdict, entry

    def _evaluate_external_action(
        self,
        proposal: dict[str, Any],
        arguments: dict[str, Any],
        goal_id: str,
        delivery_policy: dict[str, Any],
        grant_refs: tuple[str, ...],
        run_id: str,
        now_ms: int,
        queued_in_batch: int = 0,
        obligation: str = "opportunistic",
        artifacts: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """§9.1: 向他人发送、改日历、发布内容 → 单次人工审批 with a frozen
        canonical request; the destination comes from trusted job policy,
        never from the proposal."""
        capability = arguments.get("capability")
        if not isinstance(capability, str) or not capability:
            return self._suppressed("capability_missing")
        grant = self._active_grant_for(grant_refs, capability)
        if grant is None:
            return self._suppressed("grant_missing")

        # 账户切换攻击: the proposal cannot hop to another account.
        requested_account = arguments.get("account_ref")
        if requested_account is not None and requested_account != grant.account_ref:
            return self._suppressed("account_mismatch")

        # scope 扩大: resource filters bind.
        resource_ids = grant.scope.get("resource_ids")
        requested_resource = arguments.get("resource_id")
        if isinstance(resource_ids, list) and resource_ids:
            if requested_resource not in resource_ids:
                return self._suppressed("scope_exceeded")
        allowed_actions = grant.scope.get("actions")
        if isinstance(allowed_actions, list) and allowed_actions:
            requested_action = arguments.get("action")
            if requested_action not in allowed_actions:
                return self._suppressed("scope_exceeded")

        topic = arguments.get("topic")
        reason = self._suppression_checks(proposal, topic, delivery_policy, now_ms)
        if reason:
            return self._suppressed(reason)

        target_ref = delivery_policy.get("external_action_target")
        if not isinstance(target_ref, str) or not target_ref:
            return self._suppressed("no_authorized_target")
        try:
            channel = self.channels.resolve(target_ref, now_ms=now_ms)
        except PASError as exc:
            return self._suppressed(f"channel_unavailable:{exc.safe_message[:120]}")

        business_key = self._business_key(goal_id, proposal["fact_id"], proposal.get("revision") or "0",
                                          target_ref, f"request_external_action:{capability}")
        if self.store.has_business_key(business_key):
            return self._suppressed("duplicate_business_key")

        not_before, defer_reason = self._timing(
            delivery_policy, proposal.get("expires_at_ms"), now_ms,
            queued_in_batch=queued_in_batch, obligation=obligation,
        )
        if not_before is None:
            return self._suppressed(defer_reason)

        payload = self._notification_payload(proposal, run_id, channel, "external_action")
        if artifacts:
            payload["artifact_refs"] = list(artifacts)
        request = self._frozen_request(
            kind="request_external_action",
            account_ref=grant.account_ref,
            destination_ref=target_ref,
            arguments=arguments,
            proposal=proposal,
            payload=payload,
        )
        request_hash = content_hash(request)
        entry = self._entry(
            proposal=proposal,
            kind="request_external_action",
            business_key=business_key,
            request=request,
            request_hash=request_hash,
            destination_ref=target_ref,
            payload=payload,
            not_before_ms=not_before,
            expires_at_ms=self._expires(proposal, now_ms),
            grant_id=grant.grant_id,
            now_ms=now_ms,
            approval_request=request,
        )
        return {"outcome": "approval_required", "request_hash": request_hash}, entry

    def _evaluate_suggest_watch(
        self,
        proposal: dict[str, Any],
        arguments: dict[str, Any],
        goal_id: str,
        grant_refs: tuple[str, ...],
        run_id: str,
        now_ms: int,
    ) -> tuple[dict[str, Any], None]:
        """Freeze a "you might want to watch this" suggestion.

        The model proposes *once*; everything the user later confirms is read
        back from the frozen row, so a confirmation can never create
        something different from what was shown. Confirmation itself is a
        trusted-UI action and does not happen here — a suggestion never
        becomes a task on its own.

        Authority is inherited, not granted: the suggestion carries the
        grants the originating run already held, and the resulting job is
        subject to exactly the same policy as any other job.
        """
        schedule = arguments.get("schedule")
        instruction = arguments.get("instruction")
        reason = arguments.get("reason")
        if not isinstance(schedule, dict):
            return self._suppressed("watch_schedule_missing")
        problems = validate_schedule(schedule)
        if problems:
            return self._suppressed(f"watch_schedule_invalid:{problems[0][:120]}")
        if not isinstance(instruction, str) or not 1 <= len(instruction) <= 10000:
            return self._suppressed("watch_instruction_invalid")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
            return self._suppressed("watch_reason_missing")

        digest = content_hash({"r": run_id, "p": proposal["proposal_id"]})[:28]
        name_hint = arguments.get("name")
        job_name = (
            name_hint
            if isinstance(name_hint, str) and _WATCH_NAME_RE.fullmatch(name_hint)
            else f"watch-{digest[:12]}"
        )
        # The name the user sees is the name that will exist; a collision is
        # resolved here, under the model's eyes, not silently at confirm time.
        if self.store.get_job(job_name) is not None:
            job_name = f"{job_name}-{digest[12:16]}"

        try:
            record = self.store.create_watch_suggestion(
                run_id=run_id,
                proposal_id=proposal["proposal_id"],
                job_id=None,
                job_name=job_name,
                schedule=schedule,
                instruction=instruction,
                grant_refs=tuple(grant_refs),
                delivery_policy={"notification_profile": self.config.notification_profile},
                misfire_policy=None,
                reason=reason,
                now_ms=now_ms,
            )
        except PASError as exc:
            return self._suppressed(f"watch_rejected:{exc.code.value}")
        return {
            "outcome": "watch_suggested",
            "reason": "pending_confirmation",
            "suggestion_id": record["suggestion_id"],
            "job_name": record["job_name"],
        }, None

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #

    def _artifact_refs(
        self, proposal: dict[str, Any], arguments: dict[str, Any]
    ) -> tuple[tuple[str, ...], str | None]:
        """Validate the artifact references a proposal declares.

        Two failures are explicit rejections, never warnings: a reference
        that is not provably openable (absolute path, traversal, foreign
        scheme) and a reference that never reaches the message body — the
        owner would be told to open something they cannot open.
        """
        raw = arguments.get("artifact_refs")
        if raw is None:
            return (), None
        if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= 32:
            return (), "artifact_refs_malformed"
        normalized: list[str] = []
        for item in raw:
            try:
                normalized.append(normalize_artifact_ref(item))
            except ArtifactRefError:
                return (), "artifact_ref_unsafe"
        body = proposal.get("body")
        if not isinstance(body, str) or missing_artifact_refs(body, normalized):
            return (), "artifact_ref_not_in_body"
        return tuple(normalized), None

    def _suppression_checks(
        self,
        proposal: dict[str, Any],
        topic: Any,
        delivery_policy: dict[str, Any],
        now_ms: int,
    ) -> str | None:
        """Checks shared by every deliverable kind, in hard-policy order."""
        expires_at_ms = proposal.get("expires_at_ms")
        if expires_at_ms is not None and expires_at_ms <= now_ms:
            return "expired"
        if self.store.fact_handled(proposal.get("fact_id") or ""):
            return "already_handled"
        if isinstance(topic, str) and topic:
            if self.store.topic_is_muted(topic, now_ms=now_ms) or self.store.topic_is_muted(topic.strip().casefold(), now_ms=now_ms):
                return "topic_muted"
            muted_topics = delivery_policy.get("muted_topics")
            if isinstance(muted_topics, list) and topic in muted_topics:
                return "topic_muted"
        return None

    def _proactive_preference_reason(self, topic: Any, obligation: str) -> str | None:
        """Enforce mutable user reach-out preferences for opportunistic notifications."""
        if obligation == OBLIGATION_DUE:
            return None
        preferences = self.store.get_proactive_preferences()
        if not preferences["enabled"]:
            return "proactive_disabled"
        allowed_topics = preferences["allowed_topics"]
        if allowed_topics is not None:
            candidate = topic.strip().casefold() if isinstance(topic, str) else ""
            if candidate not in allowed_topics:
                return "topic_not_allowed"
        return None

    def _news_search_snapshot_binding(
        self, proposal: dict[str, Any], run_id: str, *, now_ms: int
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Bind news notifications to the fresh snapshot they cite.

        News snapshots are special here: their short freshness window must
        survive the queue so dispatch can suppress a delayed headline.
        Other sources continue using the existing delivery policy.
        """
        fact_id = proposal.get("fact_id")
        evidence_refs = proposal.get("evidence_refs") or []
        prefix = "snapshot:news-search:"
        refs = [ref for ref in evidence_refs if isinstance(ref, str) and ref.startswith(prefix)]
        if not (isinstance(fact_id, str) and fact_id.startswith("news:") or refs):
            return None, None
        if not isinstance(fact_id, str) or not fact_id.startswith("news:") or not refs:
            return None, "news_snapshot_unverified"

        run = self.store.get_run(run_id)
        pack = self.store.get_context_pack(run["context_ref"]) if run and run.get("context_ref") else None
        if pack is None:
            return None, "news_snapshot_unverified"
        pack_sources = {
            source.get("snapshot_ref"): source
            for source in pack.get("sources", [])
            if isinstance(source, dict)
            and source.get("source_id") == "news-search"
            and source.get("account_ref") == "account:public"
        }
        snapshots = {
            snapshot.snapshot_id: snapshot
            for snapshot in self.store.snapshots_for("news-search", "account:public", limit=64)
        }
        matching: list[tuple[str, int]] = []
        for ref in refs:
            source = pack_sources.get(ref)
            snapshot_id = ref.removeprefix(prefix)
            snapshot = snapshots.get(snapshot_id)
            if (
                source is None
                or snapshot is None
                or snapshot.sensitivity != "public"
                or snapshot.tombstone
            ):
                continue
            try:
                content = json.loads(self.store.snapshot_content(snapshot_id) or "")
                title = content["title"]
                url = content["url"]
                description = content["description"]
                published_at = content["published_at"]
                if not all(isinstance(value, str) for value in (title, url, description, published_at)):
                    continue
                stable_fields = {
                    "description": description,
                    "published_at": published_at,
                    "title": title,
                    "url": url,
                }
                item_fact_id = "news:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
                revision = "sha256:" + hashlib.sha256(
                    canonical_json(stable_fields).encode("utf-8")
                ).hexdigest()[:32]
                fresh_until_ms = int(datetime.fromisoformat(source["fresh_until"].replace("Z", "+00:00")).timestamp() * 1000)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if item_fact_id == fact_id and revision == proposal.get("revision"):
                matching.append((ref, fresh_until_ms))
        if not matching:
            return None, "news_snapshot_unverified"
        fresh = [(ref, until) for ref, until in matching if until > now_ms]
        if not fresh:
            return None, "news_snapshot_stale"
        ref, fresh_until_ms = max(fresh, key=lambda item: item[1])
        return {
            "source_id": "news-search",
            "snapshot_ref": ref,
            "fresh_until_ms": fresh_until_ms,
        }, None

    def _timing(
        self,
        delivery_policy: dict[str, Any],
        expires_at_ms: int | None,
        now_ms: int,
        *,
        queued_in_batch: int = 0,
        obligation: str = "opportunistic",
    ) -> tuple[int, str | None]:
        """Compute not_before: quiet hours defer, quota defers to tomorrow.
        Returns (not_before_ms, defer_reason) or (None, suppression_reason)
        when the message would arrive stale."""
        not_before = now_ms
        reason = None
        quiet_end = quiet_end_ms(delivery_policy, now_ms)
        if quiet_end is not None:
            not_before = quiet_end
            reason = "quiet_hours"
            if expires_at_ms is not None and expires_at_ms <= quiet_end:
                # 夜间产生的机会到次日已过期：抑制，不机械补发过时消息 (§10.4).
                return None, "expired_in_quiet_hours"
        max_per_day = delivery_policy.get("max_per_day", self.config.max_per_day)
        if isinstance(max_per_day, int) and not isinstance(max_per_day, bool) and max_per_day > 0:
            tzname = delivery_policy.get("timezone")
            block = delivery_policy.get("quiet_hours")
            if isinstance(block, dict):
                tzname = block.get("timezone")
            day_start = local_day_start_ms(tzname, now_ms)
            sent_today = self.store.notifications_today(day_start_ms=day_start, now_ms=now_ms)
            # Phase A evaluates the whole batch before phase B queues it,
            # so entries already accepted in this batch count too.
            sent_today += queued_in_batch
            if sent_today >= max_per_day:
                tomorrow = local_day_end_ms(tzname, now_ms)
                not_before = max(not_before, tomorrow)
                reason = "daily_quota"
                if expires_at_ms is not None and expires_at_ms <= not_before:
                    return None, "expired_in_quota_defer"

        # -- host cadence preference (§21.1 step 8) ----------------------
        # Pacing is a *soft* preference that only ever applies to
        # opportunistic reach-out. A job that carries the ``due``
        # obligation fires: the user asked for it, so a noise-reduction
        # preference must not be what loses it.
        if obligation != OBLIGATION_DUE:
            gap = delivery_policy.get("cadence_min_gap_seconds", self.config.cadence_min_gap_seconds)
            if isinstance(gap, int) and not isinstance(gap, bool) and gap > 0:
                last = self.store.last_sent_notification_ms(now_ms=now_ms)
                if last is not None and now_ms - last < gap * 1000:
                    not_before = max(not_before, last + gap * 1000)
                    reason = "cadence_gap" if reason is None else reason + "+cadence_gap"
                    if expires_at_ms is not None and expires_at_ms <= not_before:
                        return None, "expired_in_cadence_defer"
            cap = delivery_policy.get("cadence_max_per_day", self.config.cadence_max_per_day)
            if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
                tzname = delivery_policy.get("timezone")
                block = delivery_policy.get("quiet_hours")
                if isinstance(block, dict):
                    tzname = block.get("timezone")
                day_start = local_day_start_ms(tzname, now_ms)
                sent_today = (
                    self.store.notifications_today(day_start_ms=day_start, now_ms=now_ms)
                    + queued_in_batch
                )
                if sent_today >= cap:
                    not_before = max(not_before, local_day_end_ms(tzname, now_ms))
                    reason = "cadence_daily" if reason is None else reason + "+cadence_daily"
                    if expires_at_ms is not None and expires_at_ms <= not_before:
                        return None, "expired_in_cadence_defer"
        return not_before, reason

    def _active_grant_for(self, grant_refs: tuple[str, ...], capability: str):
        now = self.store.clock.wall_now_ms()
        for grant_id in grant_refs:
            grant = self.store.get_grant(grant_id)
            if grant is None:
                continue
            if grant.capability == capability and grant.is_active(now):
                return grant
        return None

    def _business_key(
        self, goal_id: str, fact_id: str, revision: str, destination: str, kind: str
    ) -> str:
        """§10.1: profile + goal + fact_id + revision + destination +
        action_kind — never a body hash (a rephrasing is not a new fact)."""
        return f"biz{content_hash({'p': self.store.profile, 'g': goal_id, 'f': fact_id, 'r': revision, 'd': destination, 'k': kind})[:32]}"

    def _frozen_request(
        self,
        *,
        kind: str,
        account_ref: str,
        destination_ref: str,
        arguments: dict[str, Any],
        proposal: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """§9.2 frozen canonical request. The binding hash is
        sha256(canonical_json(request)) — computed identically by the
        store when it inserts the approval and the action, so the two
        bindings can never diverge."""
        attachment_hashes = arguments.get("attachment_hashes")
        if not isinstance(attachment_hashes, list):
            attachment_hashes = []
        request: dict[str, Any] = {
            "kind": kind,
            "account_ref": account_ref,
            "destination_ref": destination_ref,
            "arguments": arguments,
            "attachment_hashes": sorted(str(h) for h in attachment_hashes),
            "evidence_refs": list(proposal.get("evidence_refs") or []),
            "fact_id": proposal.get("fact_id"),
            "revision": proposal.get("revision"),
            "payload": payload,
        }
        return request

    def _notification_payload(
        self,
        proposal: dict[str, Any],
        run_id: str,
        channel: dict[str, Any],
        semantic: str,
    ) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        summary = (run or {}).get("decision_summary") or "notification"
        body = proposal.get("body")
        payload: dict[str, Any] = {
            "semantic": semantic,
            "kind": proposal["kind"],
            "fact_id": proposal.get("fact_id"),
            "revision": proposal.get("revision"),
            "title": summary[:200],
            "run_id": run_id,
        }
        # §10.4 锁屏去敏: push-style channels default to summary-only; the
        # local inbox always keeps the body (it IS the user's local copy).
        keep_body = channel["kind"] == "local_inbox" or not channel["push_summary_only"]
        if keep_body:
            payload["body"] = body
        return payload

    def _entry(
        self,
        *,
        proposal: dict[str, Any],
        kind: str,
        business_key: str,
        request: dict[str, Any],
        request_hash: str,
        destination_ref: str,
        payload: dict[str, Any],
        not_before_ms: int,
        expires_at_ms: int,
        grant_id: str,
        now_ms: int,
        approval_request: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "proposal_id": proposal["proposal_id"],
            "kind": kind,
            "fact_id": proposal.get("fact_id"),
            "revision": proposal.get("revision"),
            "business_key": business_key,
            "request": request,
            "request_hash": request_hash,
            "approval_request": approval_request,
            "approval_expires_at_ms": now_ms + self.config.approval_ttl_ms,
            "destination_ref": destination_ref,
            "payload": payload,
            "not_before_ms": not_before_ms,
            "expires_at_ms": expires_at_ms,
            "policy_version": POLICY_VERSION,
            "grant_id": grant_id,
            "reason": reason,
        }

    def _expires(self, proposal: dict[str, Any], now_ms: int) -> int:
        return proposal.get("expires_at_ms") or (now_ms + self.config.default_message_ttl_ms)

    @staticmethod
    def _suppressed(reason: str) -> tuple[dict[str, Any], None]:
        return {"outcome": "suppressed", "reason": reason}, None

    # ------------------------------------------------------------------ #
    # Approval promotion + dispatch-time recheck (§10.4 投递前复验)
    # ------------------------------------------------------------------ #

    def promote_approved(self, *, now_ms: int | None = None) -> int:
        """Queue approved actions with a FRESH quiet-hours check: approval
        may resolve inside the quiet window, so not_before is recomputed
        at promotion time, not inherited from evaluation time."""
        now = self.store.clock.wall_now_ms() if now_ms is None else now_ms
        pending = self.store.list_actions(state="planned", approval_state="approved")
        items: list[dict[str, Any]] = []
        for action in pending:
            request = action.get("request") or {}
            delivery_policy: dict[str, Any] = {}
            if action.get("run_id"):
                run = self.store.get_run(action["run_id"])
                event = self.store.get_event(run["event_id"]) if run else None
                job = self.store.get_job(event.job_id) if event and event.job_id else None
                delivery_policy = dict(job.delivery_policy) if job else {}
            not_before, _ = self._timing(delivery_policy, request.get("expires_at_ms"), now)
            if not_before is None:
                continue
            items.append(
                {
                    "action_id": action["action_id"],
                    "destination_ref": request.get("destination_ref"),
                    "payload": request.get("payload") or {},
                    "not_before_ms": not_before,
                    "expires_at_ms": action.get("expires_at_ms") or (now + self.config.default_message_ttl_ms),
                }
            )
        if not items:
            return 0
        return self.store.queue_approved_actions(items, now_ms=now)

    def pre_dispatch_recheck(self, lease: Any, *, now_ms: int) -> str | None:
        """Last look before the effect. Returns a suppression reason or
        None. Grant revocation is already re-checked inside the claim
        transaction; this covers policy state that may have changed since
        queueing (topic mutes, handled facts, staleness)."""
        payload = lease.payload or {}
        fact_id = payload.get("fact_id")
        if self.store.fact_handled(fact_id or ""):
            return "already_handled"
        context = self.store.action_delivery_context(lease.action_id)
        request = context.get("request") or {} if context is not None else {}
        if request.get("kind") == "notify_self":
            topic = (request.get("arguments") or {}).get("topic")
            if isinstance(topic, str) and (
                self.store.topic_is_muted(topic, now_ms=now_ms)
                or self.store.topic_is_muted(topic.strip().casefold(), now_ms=now_ms)
            ):
                return "topic_muted"
            preference_reason = self._proactive_preference_reason(
                topic,
                request.get("obligation") or (context.get("obligation") if context is not None else None) or "opportunistic",
            )
            if preference_reason:
                return preference_reason
            fact_id = request.get("fact_id")
            evidence_refs = request.get("evidence_refs") or []
            is_news = (
                isinstance(fact_id, str) and fact_id.startswith("news:")
            ) or any(
                isinstance(ref, str) and ref.startswith("snapshot:news-search:")
                for ref in evidence_refs
            )
            if is_news:
                binding = request.get("news_search_snapshot")
                if (
                    not isinstance(binding, dict)
                    or binding.get("source_id") != "news-search"
                    or binding.get("snapshot_ref") not in evidence_refs
                    or not isinstance(binding.get("fresh_until_ms"), int)
                    or isinstance(binding.get("fresh_until_ms"), bool)
                ):
                    return "news_snapshot_unbound"
                if binding["fresh_until_ms"] <= now_ms:
                    return "news_snapshot_stale"
        return None
