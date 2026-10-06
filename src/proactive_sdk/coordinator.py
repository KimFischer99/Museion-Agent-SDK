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
     committed in one transaction under the lease fence (§13.2). The run
     ends ``proposed``; policy evaluation, actions and delivery stay P4.

Accounting honesty (AGENTS.md): ``suppressed`` / ``failed`` / ``proposed``
are three different outcomes and the report names which one happened,
plus how many model turns were actually spent. Analysis completion is
recorded; nothing here claims a notification was delivered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .context import ContextPackBuilder, SnapshotMaterializer, SourceRegistry
from .contracts import ErrorCode, PASError, content_hash
from .executor import ExecutorOutcome, ToolLoopExecutor
from .store import RunLease, Store

__all__ = ["CoordinatorConfig", "RunReport", "ProactiveCoordinator"]

_MAX_INSTRUCTION_CHARS = 10000


@dataclass(frozen=True)
class CoordinatorConfig:
    run_lease_ttl_ms: int = 60_000
    source_fetch_deadline_s: int = 10
    snapshot_default_ttl_ms: int = 30 * 60 * 1000

    def __post_init__(self) -> None:
        positive = ("run_lease_ttl_ms", "source_fetch_deadline_s", "snapshot_default_ttl_ms")
        for name in positive:
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise PASError(ErrorCode.INVALID_CONFIG, f"{name} must be a positive integer")


@dataclass(frozen=True)
class RunReport:
    """What actually happened to one run — the caller-visible truth."""

    run_id: str
    outcome: str  # proposed | suppressed | failed
    reason: str | None = None
    error_code: str | None = None
    model_turns: int = 0
    proposals: int = 0
    context_ref: str | None = None


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
        executor: ToolLoopExecutor,
        config: CoordinatorConfig | None = None,
        tool_allowlist: tuple[str, ...] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.pack_builder = pack_builder
        self.executor = executor
        self.config = config if config is not None else CoordinatorConfig()
        self.tool_allowlist = tool_allowlist

    async def process_pending_run(
        self, *, now_ms: int | None = None, cancel_event: Any | None = None
    ) -> RunReport | None:
        now = self.store.clock.wall_now_ms() if now_ms is None else now_ms
        lease = self.store.claim_run(now_ms=now, ttl_ms=self.config.run_lease_ttl_ms)
        if lease is None:
            return None
        try:
            return await self._process(lease, now_ms=now, cancel_event=cancel_event)
        except PASError as exc:
            self._fail(lease, error_class=exc.code.value, now_ms=now)
            return RunReport(
                run_id=lease.run_id, outcome="failed", error_code=exc.code.value,
                reason=exc.safe_message,
            )

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

        # ---- L0: eligibility and change detection, zero model calls ----
        entries = self.registry.entries()
        fetched: list[tuple[Any, Any]] = []  # (SourceEntry, SourceBatch)
        unauthorized: list[str] = []
        source_errors: list[str] = []
        previous_cursors: dict[tuple[str, str], str | None] = {}
        for entry in entries:
            if entry.required_capability not in self.executor.broker.capabilities:
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
        try:
            pack = await self.pack_builder.build(
                goal_id=goal_id,
                scope=scope_label,
                source_records=source_records,
                now_ms=now_ms,
                allow_stale=not require_fresh,
            )
        except PASError as exc:
            if exc.code == ErrorCode.STALE_CONTEXT and mode == "heartbeat":
                return self._suppress(lease, "l0_context_stale", now_ms)
            raise

        run_row = self.store.get_run(lease.run_id)
        run_request = self._run_request(lease, run_row, now_ms)
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
        return RunReport(
            run_id=lease.run_id,
            outcome="proposed",
            reason=outcome.decision.summary,
            model_turns=outcome.model_turns,
            proposals=len(outcome.decision.proposals),
            context_ref=context_ref,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

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
        return SourceRequest(
            source_id=entry.source_id,
            account_ref=entry.account_ref,
            deadline=deadline,
            scope=dict(entry.scope),
        )

    @staticmethod
    def _watermark(batch: Any) -> str | None:
        if not batch.items:
            return None
        return content_hash([(item.fact_id, item.revision) for item in batch.items])[:32]

    def _run_request(self, lease: RunLease, run_row: dict[str, Any] | None, now_ms: int) -> Any:
        from .contracts import RunRequest

        deadline_ms = (run_row or {}).get("deadline_ms") or now_ms + 300_000
        budget = self.executor.config.budget
        allowlist = self.tool_allowlist
        if allowlist is None:
            allowlist = self.executor.broker.tool_names()
        return RunRequest(
            run_id=lease.run_id,
            attempt=(run_row or {}).get("attempt") or 1,
            fence=lease.fence,
            context_ref=f"ctx:{lease.run_id}",
            budget=budget,
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
