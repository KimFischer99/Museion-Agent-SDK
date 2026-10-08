"""Run coordination: the L0 → L1 closed loop (SPEC §5.3, §8; P3).

``process_pending_run`` claims one queued run (single flight per
profile, §3.1) and drives it through the SPEC §0 core chain up to the
analysis boundary:

L0 — no model (§5.3). Before any LLM call the coordinator checks job
     enabled (store gate), registered sources, per-source authorization
     against the broker's capability snapshot, and — heartbeat mode only
     — whether any authorized source actually changed. No change, no
     sources, unauthorized or failing sources end the run as
     ``suppressed`` with a machine reason; zero model calls (§16.3 hard
     gate). Explicit tasks and hook wakes skip the change gate: their
     semantics say "run" (§5.3 L1), but authorization and freshness
     still apply before the model is called.

L1 — the executor's bounded tool loop produces a Decision; accepted
     decisions, proposals, usage and the immutable ContextPack are
     committed in one transaction under the lease fence (§13.2). When a
     PolicyEngine is wired in, the P4 policy phases run right after the
     decision commit: proposed → policy_evaluated → actions_queued /
     waiting_for_approval / completed (§4.3). A policy failure does NOT
     rewrite the analysis outcome: the run stays ``proposed`` (retryable)
     and the report says so.

Accounting honesty (AGENTS.md): ``suppressed`` / ``failed`` / ``proposed``
are three different outcomes and the report names which one happened,
plus how many model turns were actually spent. Analysis completion is
recorded; nothing here claims a notification was delivered — delivery
state lives in the outbox and is reported by the dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .context import ContextPackBuilder, SnapshotMaterializer, SourceRegistry
from .contracts import (
    TOOL_AUTHORITY_PAS_BROKER,
    ErrorCode,
    PASError,
    content_hash,
)
from .executor import ExecutorContext, ExecutorOutcome, RunExecutor
from .store import RunLease, Store

if TYPE_CHECKING:
    from .policy import PolicyEngine

__all__ = ["CoordinatorConfig", "RunReport", "ProactiveCoordinator"]

_MAX_INSTRUCTION_CHARS = 10000


@dataclass(frozen=True)
class CoordinatorConfig:
    run_lease_ttl_ms: int = 60_000
    source_fetch_deadline_s: int = 10
    snapshot_default_ttl_ms: int = 30 * 60 * 1000
    # SPEC §21.1 step 6: how far back the "what did we already say" summary
    # reaches, and how many entries may travel into one pack.
    recent_notification_window_ms: int = 24 * 3600 * 1000
    recent_notification_limit: int = 20

    def __post_init__(self) -> None:
        positive = (
            "run_lease_ttl_ms",
            "source_fetch_deadline_s",
            "snapshot_default_ttl_ms",
            "recent_notification_window_ms",
            "recent_notification_limit",
        )
        for name in positive:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PASError(ErrorCode.INVALID_CONFIG, f"{name} must be a positive integer")


@dataclass(frozen=True)
class RunReport:
    """What actually happened to one run — the caller-visible truth.
    ``outcome`` is the ANALYSIS outcome (§4.3 dimension one); delivery is
    reported separately (``policy_outcome``) and never folded into it."""

    run_id: str
    outcome: str  # proposed | suppressed | failed
    reason: str | None = None
    error_code: str | None = None
    model_turns: int = 0
    proposals: int = 0
    context_ref: str | None = None
    policy_outcome: str | None = None  # completed | actions_queued | waiting_for_approval | policy_error
    queued: int = 0
    approval_pending: int = 0
    suppressed_by_policy: int = 0


def _ms_to_rfc3339(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class ProactiveCoordinator:
    """Wires store + sources + memory/pack builder + executor into the
    per-run closed loop. Import/construct does no I/O beyond the store
    the caller already owns; work happens in ``process_pending_run``."""

    def __init__(
        self,
        store: Store,
        *,
        registry: SourceRegistry,
        pack_builder: ContextPackBuilder,
        executor: RunExecutor,
        config: CoordinatorConfig | None = None,
        tool_allowlist: tuple[str, ...] | None = None,
        policy_engine: "PolicyEngine | None" = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.pack_builder = pack_builder
        self.executor = executor
        self.config = config if config is not None else CoordinatorConfig()
        self.tool_allowlist = tool_allowlist
        self.policy_engine = policy_engine

    async def process_pending_run(
        self, *, now_ms: int | None = None, cancel_event: Any | None = None
    ) -> RunReport | None:
        now = self.store.clock.wall_now_ms() if now_ms is None else now_ms
        lease = self.store.claim_run(now_ms=now, ttl_ms=self.config.run_lease_ttl_ms)
        if lease is None:
            return None
        try:
            report = await self._process(lease, now_ms=now, cancel_event=cancel_event)
        except PASError as exc:
            self._fail(lease, error_class=exc.code.value, now_ms=now)
            report = RunReport(
                run_id=lease.run_id, outcome="failed", error_code=exc.code.value,
                reason=exc.safe_message,
            )
        self._project_analysis(lease, report, now_ms=now)
        return report

    def _project_analysis(self, lease: RunLease, report: RunReport, *, now_ms: int) -> None:
        """Mirror the analysis outcome onto the job activity projection.

        Separate from the delivery phase on purpose: a run that proposed
        nothing and a run whose proposal never reached the owner are two
        different facts, and the user-visible view must be able to tell
        them apart (SPEC §21.1 step 7). Wakes with no job (hook / manual)
        have no job projection to write.
        """
        try:
            event = self.store.get_event(lease.event_id)
            job = self.store.get_job(event.job_id) if event and event.job_id else None
            if job is None:
                return
            state = report.outcome if report.outcome in ("proposed", "suppressed", "failed") else "skipped"
            reason = report.reason or report.error_code
            self.store.record_job_activity(
                job_id=job.job_id,
                job_revision=job.revision,
                phase="analysis",
                state=state,
                obligation=job.obligation,
                reason=(str(reason)[:500] if reason else None),
                run_id=lease.run_id,
                dedupe_suffix=f"analysis:{state}",
                now_ms=now_ms,
            )
        except PASError:
            # The projection is derived; it must never fail a run that the
            # ledgers already recorded correctly.
            return

    # ------------------------------------------------------------------ #
    # Pipeline
    # ------------------------------------------------------------------ #

    async def _process(
        self, lease: RunLease, *, now_ms: int, cancel_event: Any | None
    ) -> RunReport:
        event = self.store.get_event(lease.event_id)
        if event is None:
            raise PASError(ErrorCode.INTERNAL_ERROR, "run references a missing event", scope="runs")
        job = self.store.get_job(event.job_id) if event.job_id else None
        instruction, goal_id, scope_label, mode = self._task_of(event, job)
        require_fresh = bool(job.task.get("require_fresh_sources")) if job else False

        # The coordinator's entire view of the executor, asked once per run.
        # A host-backed executor may have to ask its host here, so this is
        # the one place that can fail before any analysis starts.
        context = await self.executor.context()
        # Pin, in the run ledger, whether PAS actually governed what this run
        # may do. A host that keeps its own tools is recorded as such; the
        # absence of a declaration is never read as coverage.
        self.store.record_run_tool_authority(
            lease,
            authority=context.tool_authority,
            reason=self._authority_reason(context),
            now_ms=now_ms,
        )

        if event.authorization:
            # Where a wake's authority came from is part of the ledger: an
            # event-scoped binding is a different fact from a job's standing
            # grant_refs, and the audit should not have to infer it.
            self.store.append_run_event(
                lease,
                kind="wake_authorization",
                safe_summary=(
                    f"event-scoped: grants={len(event.authorization.get('grant_refs') or [])}"
                    f" destination={event.authorization.get('destination_ref')}"
                ),
                now_ms=now_ms,
            )

        # ---- L0: eligibility and change detection, zero model calls ----
        entries = self._targeted_entries(job)
        fetched: list[tuple[Any, Any]] = []  # (SourceEntry, SourceBatch)
        unauthorized: list[str] = []
        source_errors: list[str] = []
        previous_cursors: dict[tuple[str, str], str | None] = {}
        for entry in entries:
            if entry.required_capability not in context.capabilities:
                unauthorized.append(f"{entry.source_id}:{entry.account_ref}")
                continue
            previous = self.store.get_source_state(entry.source_id, entry.account_ref)
            previous_cursors[(entry.source_id, entry.account_ref)] = (
                previous.cursor_ref if previous is not None else None
            )
            try:
                batch = await self.registry.fetch_delta(self._source_request(entry, now_ms))
            except PASError as exc:
                if exc.code in (ErrorCode.PERMISSION_DENIED, ErrorCode.AUTH_REQUIRED):
                    unauthorized.append(f"{entry.source_id}:{entry.account_ref}")
                else:
                    source_errors.append(f"{entry.source_id}:{exc.code.value}")
                continue
            self.store.set_source_state(
                entry.source_id,
                entry.account_ref,
                cursor_ref=batch.cursor_ref,
                watermark=self._watermark(batch),
                now_ms=now_ms,
            )
            fetched.append((entry, batch))

        if mode == "heartbeat":
            # Opportunistic heartbeat: nothing checkable or nothing changed
            # ends here — machine reason, zero model calls (§5.3 L0).
            if not entries:
                return self._suppress(lease, "l0_nothing_to_check", now_ms)
            if not fetched:
                if unauthorized:
                    return self._suppress(lease, "l0_source_unauthorized", now_ms)
                return self._suppress(
                    lease, "l0_source_error:" + ",".join(source_errors)[:480], now_ms
                )
            changed = any(
                batch.cursor_ref != previous_cursors[(entry.source_id, entry.account_ref)]
                or bool(batch.items)
                for entry, batch in fetched
            )
            if not changed:
                return self._suppress(lease, "l0_no_source_change", now_ms)
        else:
            # Explicit task / hook wake: authorization and provider
            # failures are real failures the user must see, not silent
            # suppressions — but a task with no sources at all is pure
            # reasoning and still runs.
            if entries and not fetched:
                if unauthorized and not source_errors:
                    raise PASError(
                        ErrorCode.PERMISSION_DENIED,
                        "no authorized source is available for this task",
                        scope="coordinator",
                    )
                raise PASError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "all configured sources failed before the run",
                    scope="coordinator",
                )

        if unauthorized:
            self.store.append_run_event(
                lease,
                kind="sources_skipped",
                safe_summary=f"unauthorized: {','.join(unauthorized)[:480]}",
                now_ms=now_ms,
            )

        # ---- L1 preparation: snapshots + immutable ContextPack ----
        materializer = SnapshotMaterializer(self.store)
        source_records: list[dict[str, Any]] = []
        for entry, batch in fetched:
            source_records.extend(materializer.materialize(batch, now_ms=now_ms))
        recent = self.store.recent_sent_notifications(
            now_ms=now_ms,
            window_ms=self.config.recent_notification_window_ms,
            limit=self.config.recent_notification_limit,
        )
        try:
            pack = await self.pack_builder.build(
                goal_id=goal_id,
                scope=scope_label,
                source_records=source_records,
                now_ms=now_ms,
                allow_stale=not require_fresh,
                recent_notifications=recent,
                include_memory="memory.read" in context.capabilities,
            )
        except PASError as exc:
            if exc.code == ErrorCode.STALE_CONTEXT and mode == "heartbeat":
                return self._suppress(lease, "l0_context_stale", now_ms)
            raise

        run_row = self.store.get_run(lease.run_id)
        run_request = self._run_request(lease, run_row, now_ms, context)
        self.store.append_run_event(
            lease,
            kind="context_built",
            safe_summary=(
                f"sources={len(pack.sources)} items={len(source_records)}"
                f" goal={goal_id}"
            ),
            now_ms=now_ms,
        )

        # ---- L1: bounded agent loop ----
        outcome: ExecutorOutcome = await self.executor.execute(
            run_request, pack, source_records,
            instruction=instruction, cancel_event=cancel_event,
        )
        for record in outcome.events:
            self.store.append_run_event(lease, kind=record.kind, safe_summary=record.safe_summary, now_ms=now_ms)
        context_ref = self.store.record_run_decision(
            lease,
            context_pack=pack.to_dict(),
            decision=outcome.decision.to_wire_dict(),
            proposals=[proposal.to_dict() for proposal in outcome.decision.proposals],
            usage={"protocol_version": "1.0", **outcome.usage},
            now_ms=now_ms,
        )
        report = RunReport(
            run_id=lease.run_id,
            outcome="proposed",
            reason=outcome.decision.summary,
            model_turns=outcome.model_turns,
            proposals=len(outcome.decision.proposals),
            context_ref=context_ref,
        )
        if self.policy_engine is not None:
            report = self._apply_policy(report, now_ms=now_ms)
        return report

    def _apply_policy(self, report: RunReport, *, now_ms: int) -> RunReport:
        """P4 policy phases right after the decision commit. A policy
        failure never rewrites the analysis outcome: the run stays
        ``proposed`` (retryable via ``PolicyEngine.apply_to_run``), the
        report records the error."""
        try:
            verdict = self.policy_engine.apply_to_run(report.run_id, now_ms=now_ms)
        except PASError as exc:
            try:
                self.store.record_run_note(
                    report.run_id,
                    kind="policy_error",
                    summary=f"policy evaluation failed: {exc.code.value}"[:500],
                    now_ms=now_ms,
                )
            except PASError:
                pass
            return RunReport(
                run_id=report.run_id,
                outcome=report.outcome,
                reason=report.reason,
                error_code=report.error_code,
                model_turns=report.model_turns,
                proposals=report.proposals,
                context_ref=report.context_ref,
                policy_outcome="policy_error",
            )
        return RunReport(
            run_id=report.run_id,
            outcome=report.outcome,
            reason=report.reason,
            error_code=report.error_code,
            model_turns=report.model_turns,
            proposals=report.proposals,
            context_ref=report.context_ref,
            policy_outcome=verdict.outcome,
            queued=verdict.queued,
            approval_pending=verdict.approval_pending,
            suppressed_by_policy=verdict.suppressed,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _authority_reason(context: ExecutorContext) -> str:
        if context.tool_authority == TOOL_AUTHORITY_PAS_BROKER:
            return "executor routes tool calls through the PAS broker"
        return (
            "the host executes its own tools (external_tool_broker=false);"
            " this run's side effects are not covered by PAS authorization"
        )

    def _targeted_entries(self, job: Any) -> tuple[Any, ...]:
        """Sources to read this run.

        A job that names ``task.refresh_source_ids`` reads exactly those
        sources by id instead of scanning every registered binding — the
        time-sensitive case (a calendar, a mailbox) should not pay for an
        unrelated source, and a targeted read also keeps the pack small
        (SPEC §21.1 step 5). An id with no registered binding is a
        configuration error the user must see, not a silent no-op.
        """
        all_entries = self.registry.entries()
        default_entries = tuple(
            entry for entry in all_entries
            if not getattr(entry.source, "requires_explicit_selection", False)
        )
        if job is None:
            return default_entries
        wanted = job.task.get("refresh_source_ids")
        if not wanted:
            return default_entries
        by_id = {entry.source_id: entry for entry in all_entries}
        missing = [source_id for source_id in wanted if source_id not in by_id]
        if missing:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"task.refresh_source_ids names unregistered sources: {sorted(missing)}",
                scope="coordinator",
            )
        return tuple(by_id[source_id] for source_id in wanted)

    def _task_of(self, event: Any, job: Any) -> tuple[str, str, str, str]:
        """Resolve (instruction, goal_id, scope, mode) from the event's
        origin. Job events use the stored task; hook/manual wakes carry
        their reason as the task signal (§6.2: hook wake ⇒ explicit
        signal, so mode='task' semantics)."""
        if job is not None:
            instruction = job.task.get("instruction")
            if not isinstance(instruction, str) or not instruction:
                raise PASError(ErrorCode.INTERNAL_ERROR, f"job {job.job_id!r} has no instruction", scope="runs")
            return instruction[:_MAX_INSTRUCTION_CHARS], job.job_id, f"job:{job.mode}", job.mode
        payload = event.payload or {}
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = "review this wake signal"
        hook_id = payload.get("hook_id")
        goal = f"hook:{hook_id}" if isinstance(hook_id, str) and hook_id else f"event:{event.event_id[:16]}"
        return reason[:_MAX_INSTRUCTION_CHARS], goal, f"origin:{event.origin}", "task"

    def _source_request(self, entry: Any, now_ms: int) -> Any:
        from .contracts import SourceRequest

        deadline = _ms_to_rfc3339(now_ms + self.config.source_fetch_deadline_s * 1000)
        previous = self.store.get_source_state(entry.source_id, entry.account_ref)
        return SourceRequest(
            source_id=entry.source_id,
            account_ref=entry.account_ref,
            deadline=deadline,
            scope=dict(entry.scope),
            cursor_ref=previous.cursor_ref if previous is not None else None,
        )

    @staticmethod
    def _watermark(batch: Any) -> str | None:
        if not batch.items:
            return None
        return content_hash([(item.fact_id, item.revision) for item in batch.items])[:32]

    def _run_request(
        self,
        lease: RunLease,
        run_row: dict[str, Any] | None,
        now_ms: int,
        context: ExecutorContext,
    ) -> Any:
        from .contracts import RunRequest

        deadline_ms = (run_row or {}).get("deadline_ms") or now_ms + 300_000
        allowlist = self.tool_allowlist
        if allowlist is None:
            # The executor declares which tools it can actually offer; an
            # explicit operator allowlist still narrows that (never widens it).
            allowlist = context.tool_names
        return RunRequest(
            run_id=lease.run_id,
            attempt=(run_row or {}).get("attempt") or 1,
            fence=lease.fence,
            context_ref=f"ctx:{lease.run_id}",
            budget=context.budget,
            deadline=_ms_to_rfc3339(deadline_ms),
            tool_allowlist=tuple(allowlist),
        )

    def _suppress(self, lease: RunLease, reason: str, now_ms: int) -> RunReport:
        self.store.record_run_suppressed(lease, reason=reason, now_ms=now_ms)
        return RunReport(run_id=lease.run_id, outcome="suppressed", reason=reason, model_turns=0)

    def _fail(self, lease: RunLease, *, error_class: str, now_ms: int) -> None:
        try:
            self.store.fail_run(lease, error_class=error_class, now_ms=now_ms)
        except PASError:
            # The fence was lost (e.g. lease expired mid-run): the run is
            # someone else's now. Nothing to record under this lease.
            pass
