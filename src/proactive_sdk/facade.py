"""Public facade (SPEC §14.1; P7).

:class:`ProactiveAgent` is the typed public API the SPEC's design
example targets. It wires the P1–P6 kernels (store, scheduler, hook
runner, coordinator, policy, dispatcher) behind one object and owns the
rules that were previously the demo scripts' responsibility:

- one profile / one state dir / one DB — the store identity binding and
  the daemon lock (daemon module) enforce it, never conventions;
- grants and owner channels are created only through the trusted-UI
  entry points here; model-facing code only reads capability snapshots;
- ``tick()`` is the full admission → analysis → policy → delivery pass
  a caller can drive by hand (embedded mode); ``serve()``/``start()``
  hand the loop to :class:`proactive_sdk.daemon.PasDaemon` (daemon
  mode). Both cannot own the same state directory at the same time —
  the daemon lock arbitrates;
- stop distinguishes graceful drain (in-flight run may finish within
  the grace budget) from force stop (in-flight run is cancelled; its
  run keeps a live lease and is recovered by the next instance — never
  silently rewritten to completed).

Importing or constructing the facade starts no threads and no I/O
beyond creating the state directory and opening the store the caller
asked for.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backup import create_backup, restore_backup
from .contracts import (
    ErrorCode,
    JobSpec,
    MemoryEntry,
    PASError,
    ProfileConfig,
    RuntimeConfig,
    content_hash,
)
from .context import ContextPackBuilder, MemoryPort, SQLiteMemoryPort, SourceRegistry
from .coordinator import CoordinatorConfig, ProactiveCoordinator
from .delivery import DispatchConfig, FeedbackManager, OutboxDispatcher, WebhookNotificationSink
from .hooks import HookRunner, HookSpec, HookSandbox, platform_sandbox
from .observability import Metrics, StructuredLogger, free_disk_mb, health_snapshot
from .policy import (
    NOTIFY_SELF_CAPABILITY,
    ApprovalManager,
    GrantManager,
    OwnerChannelRegistry,
    PolicyConfig,
    PolicyEngine,
)
from .scheduler import Scheduler
from .skills import LegacySkillImporter
from .store import Store

__all__ = ["ProactiveAgent", "Job", "ChannelSink", "DB_FILENAME", "LOCK_FILENAME", "SOCKET_FILENAME"]

DB_FILENAME = "pas.sqlite3"
LOCK_FILENAME = "daemon.lock"
SOCKET_FILENAME = "pas.sock"
HEALTH_FILENAME = "health.json"


@dataclass(frozen=True)
class Job:
    """SPEC §14.1 job shape. ``instruction`` maps to ``task``;
    ``notification_profile`` rides in ``delivery_policy``.

    ``mode="reminder"`` is the deterministic direct reminder: ``instruction``
    is then empty and ``reminder`` carries the frozen user text instead
    (SPEC §21.1 step 2). ``obligation`` lets trusted configuration state
    whether the job owes a notification at its scheduled instant; when it
    is left unset the mode decides (reminder → ``due``, else
    ``opportunistic``).
    """

    id: str
    mode: str
    schedule: dict[str, Any]
    instruction: str = ""
    grant_refs: tuple[str, ...] = ()
    notification_profile: str = "owner-default"
    misfire_policy: str | None = None
    deadline: str | None = None
    enabled: bool = True
    delivery_policy: dict[str, Any] = field(default_factory=dict)
    reminder: dict[str, Any] | None = None
    obligation: str | None = None
    #: Explicit version. Changing a job's behaviour means bumping this:
    #: every policy decision (dedup keys, grants, quiet hours) is keyed on
    #: the revision, so an edit that kept the old number would look like a
    #: no-op to everything downstream. Without this field the convenience
    #: type could not express an edit at all -- callers had to drop to
    #: JobSpec to change one line of an instruction (SPEC §22.1 item 9).
    revision: int = 1

    def to_spec(self) -> JobSpec:
        delivery_policy = dict(self.delivery_policy)
        delivery_policy.setdefault("notification_profile", self.notification_profile)
        task: dict[str, Any] = {}
        if self.mode != "reminder":
            task["instruction"] = self.instruction
        return JobSpec(
            job_id=self.id,
            revision=self.revision,
            mode=self.mode,
            schedule=dict(self.schedule),
            task=task,
            grant_refs=tuple(self.grant_refs),
            delivery_policy=delivery_policy,
            misfire_policy=self.misfire_policy,
            deadline=self.deadline,
            enabled=self.enabled,
            reminder=dict(self.reminder) if self.reminder is not None else None,
            obligation=self.obligation,
        )


@dataclass(frozen=True)
class ChannelSink:
    """One personal notification channel (trusted setup path). ``sink``
    is the transport for external channels; the built-in local inbox
    needs no transport. ``register`` fails on a local_inbox ref that is
    not the store's bound owner destination (本人目标不可替换)."""

    channel_ref: str
    kind: str
    endpoint: dict[str, Any] | None = None
    push_summary_only: bool = True
    sink: Any | None = None


class ProactiveAgent:
    """The single entry object for one profile."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        executor: Any | None = None,
        model: Any | None = None,
        tools: tuple[Any, ...] | list[Any] = (),
        include_builtin_tools: bool = True,
        sources: tuple[Any, ...] = (),
        memory: MemoryPort | None = None,
        sinks: tuple[Any, ...] | list[Any] = (),
        timezone: str = "UTC",
        locale: str = "en",
        profile: str = "personal",
        owner_destination: str | None = None,
        config: Any | None = None,
        clock: Any | None = None,
        coordinator_config: CoordinatorConfig | None = None,
        policy_config: PolicyConfig | None = None,
        dispatch_config: DispatchConfig | None = None,
        tool_allowlist: tuple[str, ...] | None = None,
        hook_sandbox: HookSandbox | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.timezone = timezone
        self.locale = locale
        self.profile = profile
        # `executor` and `model` are two ways to say the same thing, so
        # supplying both would silently pick one. Refuse instead.
        if executor is not None and model is not None:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "pass either executor= (a host/bridge) or model= (let the built-in"
                " executor be assembled), not both",
                scope="facade",
            )
        if executor is None and model is None:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "an agent needs something to think with: pass model=<OpenAICompatible"
                " or any object with generate()> to use the built-in tool loop, or"
                " executor=<HostBridge/HermesRunsExecutor/...> to hand analysis to a host",
                scope="facade",
            )
        self.requested_tools = tuple(tools)
        self.include_builtin_tools = bool(include_builtin_tools)
        self.executor = executor
        self.config = config
        self.clock = clock
        self.metrics = Metrics()
        self.logger = StructuredLogger(component="pas.facade", fields={"profile": profile})

        self.owner_destination = owner_destination or f"local-inbox:{profile}"
        profile_cfg = ProfileConfig(
            profile_id=profile, state_dir=str(self.state_dir), timezone=timezone, locale=locale
        )
        runtime_cfg = RuntimeConfig(profile=profile_cfg)
        if config is not None:
            runtime_cfg = RuntimeConfig(
                profile=profile_cfg,
                max_concurrent_agent_runs=config.runtime.max_concurrent_agent_runs,
                shutdown_grace_seconds=config.runtime.shutdown_grace_seconds,
                event_retention_days=config.runtime.event_retention_days,
            )
        self.runtime_config = runtime_cfg

        self.store = Store(
            str(self.state_dir / DB_FILENAME),
            profile=profile,
            owner_destination=self.owner_destination,
            clock=clock,
        )

        # Trusted setup entries -------------------------------------------
        self.grants = GrantManager(self.store)
        self.channels = OwnerChannelRegistry(self.store)
        self.approvals = ApprovalManager(self.store)
        self.feedback = FeedbackManager(self.store)

        # Ensure the owner's local inbox channel exists (the bound
        # destination must be registered; register() validates the ref
        # and is idempotent on identical content — a content conflict
        # means the binding changed outside this facade and must fail).
        self.channels.register(
            channel_ref=self.owner_destination, kind="local_inbox", push_summary_only=False
        )

        # Sources ----------------------------------------------------------
        self.registry = SourceRegistry()
        for entry in sources:
            self.registry.register(
                source_id=entry.source_id,
                account_ref=entry.account_ref,
                source=entry,
                required_capability=entry.required_capability,
            )

        # Policy + delivery -------------------------------------------------
        resolved_policy_config = policy_config or self._policy_config_from_agent_config()
        self.policy = PolicyEngine(self.store, channels=self.channels, config=resolved_policy_config)
        self.dispatcher = OutboxDispatcher(
            self.store,
            policy=self.policy,
            config=dispatch_config or DispatchConfig(),
            # SPEC §21.1 step 5: a reminder that declares time-sensitive
            # sources is re-read right before the effect. The registry is
            # the only thing that can answer, and it is optional — without
            # it, a message is sent exactly as the policy froze it.
            sources=self.registry,
        )
        # local_inbox is delivered by the dispatcher itself (stored_in_inbox);
        # only external channels register transports here.
        for sink_spec in sinks:
            self._attach_sink(sink_spec)

        # Coordinator --------------------------------------------------------
        self.pack_builder = ContextPackBuilder(locale=locale, timezone=timezone,
                                             memory=memory if memory is not None else SQLiteMemoryPort(self.store))
        if self.executor is None:
            self.executor = self._assemble_builtin_executor(model, include_builtin_tools)
        self.coordinator = ProactiveCoordinator(
            self.store,
            registry=self.registry,
            pack_builder=self.pack_builder,
            # self.executor, not the local `executor`: when model= was used
            # the executor is assembled just above, and passing the original
            # parameter here would hand the coordinator a None.
            executor=self.executor,
            config=coordinator_config,
            tool_allowlist=tool_allowlist,
            policy_engine=self.policy,
        )
        self.scheduler = Scheduler(
            self.store,
            self.store.clock,
            default_max_per_day=resolved_policy_config.max_per_day,
        )
        self._hook_runner: HookRunner | None = None
        self._hook_sandbox = hook_sandbox
        self._owns_lock = False
        self._daemon: Any | None = None
        self._closed = False

    # ------------------------------------------------------------------ #
    # Lifecycle (SPEC §14.1: start/stop/close, context manager, serve)
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "ProactiveAgent":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def serve(self) -> None:
        """Foreground blocking serve (daemon mode owns the event loop).
        Ctrl-C / SIGTERM drain through the daemon's signal handling."""
        from .daemon import PasDaemon

        daemon = PasDaemon(self)
        self._daemon = daemon
        try:
            await daemon.serve_forever()
        finally:
            self._daemon = None

    async def start(self, *, grace_s: float | None = None) -> "Any":
        """Embedded mode: acquire the single-instance lock and run the
        daemon loop as a task on the caller's event loop. Returns the
        PasDaemon; ``await agent.stop()`` drains and releases."""
        from .daemon import PasDaemon

        if self._daemon is not None:
            raise PASError(ErrorCode.INVALID_CONFIG, "agent already started", scope="facade")
        daemon = PasDaemon(self)
        await daemon.start(grace_s=grace_s)
        self._daemon = daemon
        return daemon

    async def stop(self, *, drain: bool = True, grace_s: float | None = None) -> dict[str, Any]:
        """Stop the embedded daemon. ``drain=True`` waits up to the grace
        budget for the in-flight run(s); ``drain=False`` cancels them
        now — the risk is explicit: their leases stay live and the next
        instance recovers the runs (attempt+1); nothing is marked
        complete that did not complete."""
        if self._daemon is None:
            return {"stopped": False, "reason": "not_started"}
        report = await self._daemon.stop(drain=drain, grace_s=grace_s)
        self._daemon = None
        return report

    async def close(self) -> None:
        if self._daemon is not None:
            await self.stop(drain=False)
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self.executor, "close", None)
            if close is not None:
                await close()
        finally:
            self.store.close()

    def _close_quietly(self) -> None:
        try:
            self.store.close()
        except Exception:  # pragma: no cover - defensive
            pass

    # ------------------------------------------------------------------ #
    # Tick: one full pass, caller-driven (embedded / tests / cron)
    # ------------------------------------------------------------------ #

    async def tick(
        self,
        *,
        max_runs: int | None = None,
        run_hooks: bool = True,
        dispatch: bool = True,
    ) -> dict[str, Any]:
        """One admission → analysis → policy → delivery pass. With no
        due jobs and no queued runs this performs zero model calls — the
        heartbeat L0 gate lives inside the coordinator, not here."""
        if self._closed:
            raise PASError(ErrorCode.INVALID_CONFIG, "agent is closed", scope="facade")
        now = self.store.clock.wall_now_ms()
        report: dict[str, Any] = {"admitted": [], "expired": [], "runs": [], "hooks": {}, "dispatched": []}

        admission = self.scheduler.admit_due(now_ms=now)
        report["admitted"] = list(admission.admitted)
        report["expired"] = [occ for occ, _ in admission.expired]
        if admission.admitted:
            self.metrics.inc("wake", len(admission.admitted))

        hook_summary: dict[str, int] = {}
        if run_hooks:
            runner = self._hooks()
            if runner is not None:
                # The store connection is bound to the creating thread;
                # hooks run inline (the runner is single-flight anyway).
                summary = runner.run_due_hooks(now_ms=now)
                counts: dict[str, int] = {}
                for hook_report in summary.reports:
                    counts[hook_report.outcome] = counts.get(hook_report.outcome, 0) + 1
                hook_summary = counts

        limit = max_runs if max_runs is not None else self.runtime_config.max_concurrent_agent_runs
        for _ in range(max(1, limit)):
            run_report = await self.coordinator.process_pending_run(now_ms=now)
            if run_report is None:
                break
            entry = {
                "run_id": run_report.run_id,
                "outcome": run_report.outcome,
                "reason": run_report.reason,
                "model_turns": run_report.model_turns,
                "policy_outcome": run_report.policy_outcome,
            }
            report["runs"].append(entry)
            if run_report.outcome == "suppressed":
                self.metrics.inc("suppressed")
            elif run_report.outcome == "failed":
                self.metrics.inc("failed_runs")

        if dispatch:
            dispatch_reports = await self.dispatcher.dispatch_due(now_ms=now)
            report["dispatched"] = [
                {"message_id": r.message_id, "state": r.state} for r in dispatch_reports
            ]
            await self.dispatcher.reconcile_unknowns(now_ms=now)
            self.store.promote_deferred(now_ms=now)
            self.store.expire_due_messages(now_ms=now)
            counts = self.store.outbox_state_counts()
            self.metrics.set_gauge("outbox_pending", counts.get("queued", 0) + counts.get("deferred", 0))
            self.metrics.set_gauge("delivery_unknown", counts.get("unknown", 0))
        report["hooks"] = hook_summary
        return report

    # ------------------------------------------------------------------ #
    # Jobs CRUD (upsert / pause / resume / delete / trigger)
    # ------------------------------------------------------------------ #

    def jobs_upsert(
        self, job: Job | JobSpec, *, idempotency_key: str | None = None
    ) -> Any:
        """Create or revise a job (trusted entry point).

        Every authorization-bearing field is checked here, before the job
        exists: a reminder's destination must be the bound owner channel or
        a registered owner channel, and its grant must currently be active.
        The store re-checks the same gates at every occurrence, because a
        grant revoked tomorrow must stop tomorrow's reminder.

        ``idempotency_key`` defaults to a hash of the job's own content,
        which is the right answer for the common case ("make sure this job
        looks like this"): re-submitting identical content is a no-op, and
        changing it is a new revision. Pass an explicit key when you want
        two *different* jobs to share one revision history.
        """
        spec = job.to_spec() if isinstance(job, Job) else job
        spec = self._with_policy_defaults(spec)
        self._validate_trusted_job(spec)
        if idempotency_key is None:
            idempotency_key = f"job:{content_hash(dataclasses.asdict(spec))[:32]}"
        return self.scheduler.register_job(spec, idempotency_key=idempotency_key)

    def grant(
        self,
        capability: str,
        *,
        account_ref: str = "account:primary",
        scope: dict[str, Any] | None = None,
        evidence: str | None = None,
        expires_at_ms: int | None = None,
    ) -> Any:
        """Record the user's consent for one capability, in one line.

        **Trusted entry only.** The caller of this method must be the user's
        own code — a CLI invocation the user typed, a settings screen they
        clicked. Model output, Skill text, a webhook body or any remote
        request must never reach it; that is what
        :meth:`create_grant_from_user_consent` documents, and this is only
        the same operation with a shorter signature.

        ``evidence`` is the audit pointer to the consent artifact. It
        defaults to a locally-generated ref because the Caller *is* the
        user's own process — pass a real artifact ref when consent came
        from somewhere else.
        """
        return self.create_grant_from_user_consent(
            capability=capability,
            account_ref=account_ref,
            scope=scope if scope is not None else {},
            consent_evidence_ref=evidence or f"consent:local-call:{uuid.uuid4().hex[:16]}",
            expires_at_ms=expires_at_ms,
        )

    def _validate_trusted_job(self, spec: JobSpec) -> None:
        if spec.mode != "reminder":
            return
        reminder = spec.reminder or {}
        destination = reminder.get("destination") or self.owner_destination
        if destination != self.owner_destination:
            channel = self.store.get_owner_channel(destination)
            if channel is None or not channel.get("enabled"):
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    f"reminder destination {destination!r} is not a registered owner channel",
                    scope="jobs",
                )
        if self._active_notify_grant(spec.grant_refs) is None:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "a direct reminder requires an active notify.self grant"
                " (create it through the trusted consent path)",
                scope="jobs",
            )

    def _active_notify_grant(self, grant_refs: tuple[str, ...]):
        now = self.store.clock.wall_now_ms()
        for grant_id in grant_refs:
            grant = self.store.get_grant(grant_id)
            if grant is None:
                continue
            if grant.capability == NOTIFY_SELF_CAPABILITY and grant.is_active(now):
                return grant
        return None

    def _with_policy_defaults(self, spec: JobSpec) -> JobSpec:
        """Profile-level notification window becomes the default quiet
        hours of every job that does not set its own (SPEC §14.3
        policy.notification_window). Explicit per-job policy wins."""
        if self.config is None:
            return spec
        window = self.config.policy.notification_window
        if window is None:
            return spec
        policy = dict(spec.delivery_policy)
        if not any(k in policy for k in ("quiet_hours_start", "quiet_hours_end", "quiet_hours")):
            policy["quiet_hours_start"], policy["quiet_hours_end"] = window
            policy.setdefault("quiet_hours_timezone", self.config.timezone)
        return JobSpec(
            job_id=spec.job_id, mode=spec.mode, schedule=spec.schedule, task=spec.task,
            owner=spec.owner, revision=spec.revision, grant_refs=spec.grant_refs,
            delivery_policy=policy, misfire_policy=spec.misfire_policy,
            deadline=spec.deadline, enabled=spec.enabled,
            reminder=spec.reminder, obligation=spec.obligation,
        )

    @property
    def cadence(self) -> str:
        """The host-declared proactive cadence (`warm`/`balanced`/`gentle`).

        A preference, not an authorization: it paces opportunistic
        reach-out only and can never suppress a due reminder, bypass a
        quiet window, a mute or the business-key dedup (SPEC §21.1 step 8).
        """
        return self.policy.config.cadence

    def note_user_input(
        self,
        text: str,
        *,
        grant_refs: tuple[str, ...] | list[str],
        destination: str | None = None,
        delivery_policy: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Trusted entry: the user said something worth acting on.

        This is the *only* way a wake that is not attached to a job obtains
        an authorization context, and it is deliberately a named method on
        the trusted side rather than an option on the generic event path:
        being able to raise a wake must not be the same thing as being able
        to authorize what it produces.

        The caller is the product's trusted side (an authenticated UI, a CLI
        invocation, the control plane). Model output, Skill text and source
        content must never reach it — nothing here reads a proposal.

        What it does NOT do: it cannot create authority. Every
        ``grant_refs`` entry must already exist and be active *now*, the
        destination must already be a bound or registered owner channel, and
        the policy layer still applies the grant scope, mutes, quiet hours,
        quota and dedup on top of this binding.
        """
        now = self.store.clock.wall_now_ms()
        refs = tuple(grant_refs or ())
        active = self._active_notify_grants(refs)
        if len(active) != len(refs):
            missing = [ref for ref in refs if ref not in active]
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"a user wake can only bind active grants; not active: {sorted(missing)}",
                scope="facade",
            )
        resolved = self._resolve_user_destination(destination)
        event_id = self.store.admit_user_wake(
            f"user:{idempotency_key or uuid.uuid4().hex}",
            text=text,
            grant_refs=refs,
            destination_ref=resolved,
            delivery_policy=delivery_policy,
            observed_at_ms=now,
            expires_at_ms=now + 24 * 3600 * 1000,
        )
        self.metrics.inc("wake")
        return event_id

    def _active_notify_grants(self, grant_refs: tuple[str, ...]) -> set[str]:
        now = self.store.clock.wall_now_ms()
        found: set[str] = set()
        for grant_id in grant_refs:
            grant = self.store.get_grant(grant_id)
            if grant is not None and grant.is_active(now):
                found.add(grant_id)
        return found

    def _resolve_user_destination(self, destination: str | None) -> str:
        """The bound owner destination by default; anything else must already
        be a registered, enabled owner channel (本人目标不可替换)."""
        if destination is None:
            return self.owner_destination
        if not isinstance(destination, str) or not destination:
            raise PASError(ErrorCode.INVALID_CONFIG, "destination must be a string", scope="facade")
        if destination == self.owner_destination:
            return destination
        channel = self.store.get_owner_channel(destination)
        if channel is None or not channel.get("enabled"):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"destination {destination!r} is not a registered owner channel",
                scope="facade",
            )
        return destination

    def suggestions_list(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Pending (or already resolved) watch suggestions, oldest first.

        These are the "you might want to watch this" proposals the analysis
        produced and that nobody has answered yet. A suggestion is inert
        until a trusted caller resolves it.
        """
        return self.store.list_watch_suggestions(state=state, limit=limit)

    def suggestions_resolve(
        self, suggestion_id: str, *, accept: bool, actor: str
    ) -> dict[str, Any]:
        """Accept or decline a frozen suggestion (SPEC §22.1 item 7).

        Accepting creates the job *from the frozen row*, through the same
        trusted ``jobs_upsert`` path as any other job, and only then records
        the acceptance — so a failure to create leaves the suggestion
        pending rather than claiming a task exists.

        Declining is recorded with the same weight as accepting: a refusal
        the user made is a fact about the ledger too.

        ``actor`` is the authenticated principal from the control plane.
        This is a trusted-UI action; the model has no path here, and the
        parameters cannot change between proposal and confirmation because
        nothing re-reads model output.
        """
        now = self.store.clock.wall_now_ms()
        if not accept:
            return self.store.decline_watch_suggestion(suggestion_id, actor=actor, now_ms=now)
        # Claim BEFORE creating anything: if the user already declined this
        # suggestion, the conflict must fire while no job exists yet.
        claimed = self.store.claim_watch_suggestion(suggestion_id, actor=actor, now_ms=now)
        if claimed.get("created_job_id"):
            return claimed
        frozen = Job(
            id=claimed["job_name"],
            mode="task",
            schedule=dict(claimed["schedule"]),
            instruction=claimed["instruction"],
            grant_refs=tuple(claimed["grant_refs"]),
            delivery_policy=dict(claimed["delivery_policy"]),
            misfire_policy=claimed["misfire_policy"],
        )
        created = self.jobs_upsert(frozen, idempotency_key=f"suggestion:{suggestion_id}")
        return self.store.attach_watch_job(
            suggestion_id, created_job_id=created.job_id, now_ms=now
        )

    def jobs_get(self, job_id: str) -> Any:
        return self.store.get_job(job_id)

    def jobs_list(self, *, enabled: bool | None = None) -> list[Any]:
        return self.store.list_jobs(enabled=enabled)

    def jobs_pause(self, job_id: str) -> Any:
        job = self.store.get_job(job_id)
        if job is None:
            raise PASError(ErrorCode.INVALID_CONFIG, f"unknown job {job_id!r}", scope="jobs")
        return self.store.set_job_enabled(job_id, enabled=False, expected_revision=job.revision)

    def jobs_resume(self, job_id: str) -> Any:
        job = self.store.get_job(job_id)
        if job is None:
            raise PASError(ErrorCode.INVALID_CONFIG, f"unknown job {job_id!r}", scope="jobs")
        return self.store.set_job_enabled(job_id, enabled=True, expected_revision=job.revision)

    def jobs_delete(self, job_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """The user's "delete" action, with the audit trail preserved.

        A job that never admitted anything is removed for real. A job with
        history cannot be erased — the ledger is not rewritten — so it is
        *stopped* instead: no further scheduling, everything still
        queryable. The returned dict says which of the two happened, so a
        caller never has to guess (SPEC §21.1 step 7).
        """
        job = self.store.get_job(job_id)
        if job is None:
            raise PASError(ErrorCode.INVALID_CONFIG, f"unknown job {job_id!r}", scope="jobs")
        try:
            self.store.delete_job(job_id)
        except PASError as exc:
            if exc.code is not ErrorCode.CONFLICT:
                raise
            record = self.store.stop_job(job_id, reason=reason or "user_delete")
            return {
                "job_id": job_id,
                "action": "stopped",
                "tracking": "stopped",
                "revision": record.revision,
                "reason": record.stop_reason,
                "audit_preserved": True,
            }
        return {"job_id": job_id, "action": "deleted", "tracking": "removed", "audit_preserved": False}

    def jobs_stop(self, job_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Stop tracking a job without deleting anything it produced."""
        record = self.store.stop_job(job_id, reason=reason or "user_stop")
        return {
            "job_id": record.job_id,
            "action": "stopped",
            "tracking": "stopped",
            "revision": record.revision,
            "reason": record.stop_reason,
            "audit_preserved": True,
        }

    def activity_list(
        self, job_id: str | None = None, *, phase: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """User-visible activity projection (SPEC §21.1 step 7).

        Execution outcome and notification outcome are separate rows; a
        quiet job still shows *why* it stayed quiet instead of nothing.
        """
        return self.store.job_activity(job_id, phase=phase, limit=limit)

    def trigger_job(self, job_id: str, *, reason: str = "manual trigger") -> str:
        """Manual run: admit an immediate event for an existing job. The
        run keeps the job's grants and instruction; the manual origin is
        visible in run events (manual run ≠ scheduler admission)."""
        job = self.store.get_job(job_id)
        if job is None:
            raise PASError(ErrorCode.INVALID_CONFIG, f"unknown job {job_id!r}", scope="jobs")
        if job.mode == "reminder":
            # A direct reminder has a frozen instant and no agent task; a
            # manual trigger would have nothing to run.
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"job {job_id!r} is a direct reminder; reminders fire at their"
                " scheduled instant and are not manually triggered",
                scope="jobs",
            )
        if job.stopped_at_ms is not None:
            raise PASError(ErrorCode.CONFLICT, f"job {job_id!r} is stopped", scope="jobs")
        if not job.enabled:
            raise PASError(ErrorCode.INVALID_CONFIG, f"job {job_id!r} is paused", scope="jobs")
        now = self.store.clock.wall_now_ms()
        import uuid

        occurrence = f"manual:{uuid.uuid4().hex}"
        event_id = f"job:{job.job_id}:manual:{occurrence}"
        self.store.admit_event(
            event_id,
            origin="manual",
            payload={"job_id": job.job_id, "mode": "task", "reason": reason},
            observed_at_ms=now,
            expires_at_ms=now + 24 * 3600 * 1000,
            job_id=job.job_id,
            job_revision=job.revision,
        )
        self.metrics.inc("wake")
        return event_id

    # ------------------------------------------------------------------ #
    # Runs (status / events / cancel)
    # ------------------------------------------------------------------ #

    def runs_get(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if run is None:
            return None
        run = dict(run)
        cancel = self.store.db.execute(
            "SELECT cancel_requested, cancel_requested_ms FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        run["cancel_requested"] = bool(cancel["cancel_requested"]) if cancel is not None else False
        return run

    def runs_list(self, *, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_runs(state=state, limit=limit)

    def run_events(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.run_events(run_id)

    def runs_cancel(self, run_id: str) -> dict[str, Any]:
        """Durable cancel request (§14.2). The returned ``outcome`` is
        the truth: ``cancel_requested`` for an in-flight run means the
        worker observed the request only when it actually stops."""
        outcome = self.store.request_run_cancel(run_id, now_ms=self.store.clock.wall_now_ms())
        if self._daemon is not None:
            self._daemon.request_cancel(run_id)
        return outcome

    # ------------------------------------------------------------------ #
    # Grants / approvals / notifications (trusted-UI entries)
    # ------------------------------------------------------------------ #

    def create_grant_from_user_consent(
        self,
        *,
        capability: str,
        account_ref: str,
        scope: dict[str, Any] | None = None,
        consent_evidence_ref: str,
        expires_at_ms: int | None = None,
    ) -> Any:
        """Grant creation is a trusted-UI action (§14.2); it must never
        be exposed to the model or a Skill. ``consent_evidence_ref`` is
        the audit pointer to the user's consent artifact."""
        grant = self.grants.create(
            capability=capability,
            account_ref=account_ref,
            scope=scope,
            consent_evidence_ref=consent_evidence_ref,
            expires_at_ms=expires_at_ms,
        )
        return grant

    def revoke_grant(self, grant_id: str) -> int:
        revoked = self.grants.revoke(grant_id)
        self.metrics.inc("grant_revoked")
        return revoked

    async def remember_user_context(self, entry: MemoryEntry) -> None:
        """Trusted user-side memory write. Memory never creates tool or notification authority."""
        await self.pack_builder.memory.remember(entry)

    def proactive_preferences(self) -> dict[str, Any]:
        return self.store.get_proactive_preferences()

    def set_proactive_preferences(self, preferences: dict[str, Any]) -> dict[str, Any]:
        """Apply explicit user preferences from the trusted UI or CLI."""
        return self.store.set_proactive_preferences(preferences)

    def grants_list(self, *, include_revoked: bool = False) -> list[Any]:
        return self.store.list_grants(include_revoked=include_revoked)

    def approvals_pending(self) -> list[Any]:
        return self.approvals.pending()

    def approvals_get(self, approval_id: str) -> Any:
        return self.store.get_approval(approval_id)

    def approvals_resolve(self, approval_id: str, *, approve: bool, actor: str) -> Any:
        """``actor`` is the authenticated principal string supplied by
        the control plane (recorded on the approval row); it is never
        accepted from model-facing callers."""
        result = self.approvals.resolve(
            approval_id, approve=approve, actor=actor, now_ms=self.store.clock.wall_now_ms()
        )
        return result

    def inbox_list(self, *, unread_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_inbox(unread_only=unread_only, limit=limit)

    def inbox_mark_read(self, inbox_id: str) -> bool:
        return self.store.mark_inbox_read(inbox_id, now_ms=self.store.clock.wall_now_ms())

    def notifications_feedback(
        self, *, kind: str, actor: str, message_id: str | None = None, scope: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.feedback.record(kind=kind, scope=scope or {}, actor=actor, message_id=message_id)

    def feedback_from_inbox(
        self, inbox_id: str, *, kind: str, actor: str, scope: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Resolve a displayed inbox item to its owned message and fact before feedback."""
        row = self.store.db.execute(
            "SELECT message_id, fact_id FROM inbox WHERE inbox_id=?", (inbox_id,)
        ).fetchone()
        if row is None:
            raise PASError(ErrorCode.INVALID_CONFIG, "unknown inbox item", scope="feedback")
        resolved = dict(scope or {})
        if kind == "handled":
            if not row["fact_id"]:
                raise PASError(ErrorCode.INVALID_CONFIG, "this inbox item has no fact; use read or stop its job", scope="feedback")
            if resolved.get("fact_id") not in (None, row["fact_id"]):
                raise PASError(ErrorCode.INVALID_CONFIG, "feedback fact must match the inbox item", scope="feedback")
            resolved["fact_id"] = row["fact_id"]
        return self.notifications_feedback(kind=kind, actor=actor, message_id=row["message_id"], scope=resolved)

    # ------------------------------------------------------------------ #
    # Skills (audit / import / explain — §11, §14.1)
    # ------------------------------------------------------------------ #

    def skills_audit(self, *, source: str | Path | None = None) -> dict[str, Any]:
        root = self._skills_root(source)
        importer = LegacySkillImporter(root=root)
        skills = importer.scan()
        report = importer.report(skills)
        report["source"] = str(root)
        report["stored_installs"] = len(self.store.list_skill_installs())
        return report

    def skills_import(self, *, source: str | Path | None = None) -> dict[str, Any]:
        """Scan and record installs. ``technical_status`` stays at the
        honest floor (``parsed``); capability e2e status is a separate,
        later gate — import does not fake authorization."""
        root = self._skills_root(source)
        importer = LegacySkillImporter(root=root)
        skills = importer.scan()
        now = self.store.clock.wall_now_ms()
        installed = 0
        for skill in skills:
            self.store.record_skill_install(
                canonical_name=skill.canonical_name,
                source_hash=skill.sha256,
                sidecar=skill.sidecar(),
                technical_status="parsed",
                distribution_status="permission_unverified",
                now_ms=now,
            )
            installed += 1
        report = importer.report(skills)
        return {"source": str(root), "scanned": report["count"], "installed": installed,
                "issue_counts": report["issue_counts"]}

    def skills_explain(self, canonical_name: str) -> dict[str, Any]:
        """Explain one skill's state honestly: what parsed, what is
        missing technically, what authorization it needs and whether
        that grant exists (SKILL-01: 缺失二进制与授权可解释)."""
        install = self.store.get_skill_install(canonical_name)
        if install is None:
            raise PASError(ErrorCode.INVALID_CONFIG, f"skill {canonical_name!r} is not installed", scope="skills")
        sidecar = install["sidecar"]
        requirements = sidecar.get("requirements", {})
        needed_capabilities = list(requirements.get("capabilities", []))
        needed_grants = list(requirements.get("grants", []))
        now = self.store.clock.wall_now_ms()
        active = self.store.active_capabilities(now_ms=now)
        return {
            "canonical_name": canonical_name,
            "technical_status": install["technical_status"],
            "distribution_status": install["distribution_status"],
            "capabilities": needed_capabilities,
            "capabilities_granted": [c for c in needed_capabilities if c in active],
            "capabilities_missing": [c for c in needed_capabilities if c not in active],
            "grants_required": needed_grants,
            "binaries": list(requirements.get("binaries", [])),
            "notes": (
                "technical_status is parsed; capability e2e verification is a separate gate"
                if install["technical_status"] == "parsed"
                else f"technical_status={install['technical_status']}"
            ),
        }

    def _skills_root(self, source: str | Path | None) -> Path:
        if source is not None:
            return Path(source).expanduser()
        if self.config is not None and self.config.skills.source:
            return Path(self.config.skills.source).expanduser()
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            "no skills source configured (pass source= or set skills.source in config)",
            scope="skills",
        )

    # ------------------------------------------------------------------ #
    # Hooks
    # ------------------------------------------------------------------ #

    def register_hook(self, spec: HookSpec) -> Any:
        return self._hooks().register_hook(spec)

    def hooks_list(self, *, enabled: bool | None = None) -> list[Any]:
        return self.store.list_hooks(enabled=enabled)

    def hooks_run_due(self) -> dict[str, int]:
        runner = self._hooks()
        summary = runner.run_due_hooks()
        counts: dict[str, int] = {}
        for hook_report in summary.reports:
            counts[hook_report.outcome] = counts.get(hook_report.outcome, 0) + 1
        return counts

    def _hooks(self) -> HookRunner:
        if self._hook_runner is None:
            self._hook_runner = HookRunner(
                self.store,
                self.state_dir / "hooks-staging",
                sandbox=self._hook_sandbox if self._hook_sandbox is not None else platform_sandbox(),
            )
        return self._hook_runner

    # ------------------------------------------------------------------ #
    # Status, health, backup/restore, export/delete (§14.1 / §17.2)
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        now = self.store.clock.wall_now_ms()
        jobs = self.store.list_jobs()
        runs = self.store.run_state_counts()
        outbox = self.store.outbox_state_counts()
        disk_free = free_disk_mb(self.state_dir)
        health = health_snapshot(
            db_ok=True,
            disk_free_mb=disk_free,
            disk_free_warn_mb=self._disk_warn_mb(),
            unknown_deliveries=outbox.get("unknown", 0),
            scheduler_age_ms=None,
            scheduler_stale_after_ms=10**12,
        )
        return {
            "profile": self.profile,
            "state_dir": str(self.state_dir),
            "owner_destination": self.owner_destination,
            "jobs": {"total": len(jobs), "enabled": sum(1 for j in jobs if j.enabled)},
            "runs": runs,
            # Not a metric to optimise: a run recorded as `host` means PAS
            # did not constrain its side effects, and the operator should be
            # able to see that without opening the ledger.
            "run_tool_authority": self.store.run_tool_authority_counts(),
            "outbox": outbox,
            "inbox_unread": len(self.store.list_inbox(unread_only=True, limit=1000)),
            "grants_active": len(self.store.active_capabilities(now_ms=now)),
            "health": health,
            "metrics": self.metrics.snapshot(),
            "daemon_running": self._daemon is not None,
        }

    def write_health_file(self, *, scheduler_age_ms: int | None = None, db_ok: bool = True,
                          source_errors: int = 0) -> dict[str, Any]:
        outbox = self.store.outbox_state_counts()
        health = health_snapshot(
            db_ok=db_ok,
            disk_free_mb=free_disk_mb(self.state_dir),
            disk_free_warn_mb=self._disk_warn_mb(),
            unknown_deliveries=outbox.get("unknown", 0),
            scheduler_age_ms=scheduler_age_ms,
            scheduler_stale_after_ms=max(10 * 60 * 1000, self._loop_interval_ms() * 10),
            source_errors=source_errors,
        )
        health["metrics"] = self.metrics.snapshot()
        tmp = self.state_dir / (HEALTH_FILENAME + ".tmp")
        tmp.write_text(json.dumps(health, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_dir / HEALTH_FILENAME)
        return health

    def backup(self, path: str | Path) -> dict[str, Any]:
        return create_backup(self.store, path)

    def restore(self, backup_path: str | Path) -> dict[str, Any]:
        """Destructive: replaces this profile's database file from a
        backup. Refuses while a daemon lock exists; the caller must
        confirm in its own UI before calling."""
        if self._daemon is not None:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "stop the running daemon before restore", scope="backup"
            )
        result = restore_backup(
            self.state_dir / DB_FILENAME,
            backup_path,
            expect_profile=self.profile,
            expect_owner_destination=self.owner_destination,
        )
        self._close_quietly()
        self.store = Store(
            str(self.state_dir / DB_FILENAME),
            profile=self.profile,
            owner_destination=self.owner_destination,
            clock=self.clock,
        )
        self._rebind_store()
        return result

    def export_data(self, path: str | Path) -> dict[str, Any]:
        dump = self.store.export_profile_data()
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"path": str(target), "tables": {k: len(v) for k, v in dump.items()}}

    def delete_data(self, *, confirm: str) -> dict[str, int]:
        """Delete all profile data rows. Requires the exact confirmation
        phrase — this is the §14.1 delete entry and it is irreversible."""
        if confirm != "DELETE PROFILE DATA":
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                'delete_data requires confirm="DELETE PROFILE DATA"',
                scope="facade",
            )
        return self.store.wipe_profile_data()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _policy_config_from_agent_config(self) -> PolicyConfig:
        if self.config is None:
            return PolicyConfig()
        policy = self.config.policy
        return PolicyConfig(
            max_per_day=policy.max_unsolicited_notifications_per_day,
            cadence=policy.cadence,
            cadence_min_gap_seconds=policy.cadence_min_gap_seconds,
            cadence_max_per_day=policy.cadence_max_per_day,
        )

    def _assemble_builtin_executor(self, model: Any, include_builtin: bool) -> Any:
        """Wire `model=` into a working analysis loop, tools included.

        The capability snapshot is a *callable*, evaluated when each run
        starts, so it reflects the grants that exist then. Freezing it here
        would mean a grant the user adds later is silently ignored — the
        tool would keep being refused with `permission_denied` and nothing
        would explain why.
        """
        from .builtin_tools import builtin_tools
        from .executor import ToolLoopExecutor
        from .toolkit import register_tools
        from .tools import LocalToolBroker

        broker = LocalToolBroker(
            capabilities=lambda: self.store.active_capabilities(
                now_ms=self.store.clock.wall_now_ms()
            )
        )
        available: list[Any] = list(self.requested_tools)
        if include_builtin:
            available = list(builtin_tools(self)) + available
        register_tools(broker, available)
        self.tool_names = broker.tool_names()
        return ToolLoopExecutor(model=model, broker=broker, clock=self.store.clock)

    def _attach_sink(self, sink_spec: Any) -> None:
        if isinstance(sink_spec, ChannelSink):
            self.channels.register(
                channel_ref=sink_spec.channel_ref,
                kind=sink_spec.kind,
                endpoint=sink_spec.endpoint,
                push_summary_only=sink_spec.push_summary_only,
            )
            if sink_spec.sink is not None:
                self.dispatcher.register_sink(sink_spec.kind, sink_spec.sink)
            elif sink_spec.kind not in ("local_inbox",) and sink_spec.sink is None:
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    f"channel {sink_spec.channel_ref!r} of kind {sink_spec.kind!r} needs a sink transport",
                    scope="channels",
                )
            return
        # P5-style bare webhook sink: register under its own kind
        if isinstance(sink_spec, WebhookNotificationSink):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "wrap WebhookNotificationSink in ChannelSink(channel_ref=..., kind=..., sink=...)",
                scope="channels",
            )
        raise PASError(ErrorCode.INVALID_CONFIG, f"unsupported sink spec {type(sink_spec).__name__}", scope="channels")

    def _disk_warn_mb(self) -> int:
        return self.config.runtime.disk_free_warn_mb if self.config is not None else 50

    def _loop_interval_ms(self) -> int:
        seconds = self.config.runtime.loop_interval_seconds if self.config is not None else 5.0
        return int(seconds * 1000)

    def _rebind_store(self) -> None:
        """After a restore the Store object must be rebuilt; repoint the
        kernels that hold a store reference."""
        if isinstance(self.pack_builder.memory, SQLiteMemoryPort):
            self.pack_builder.memory.store = self.store
        self.grants = GrantManager(self.store)
        self.channels = OwnerChannelRegistry(self.store)
        self.approvals = ApprovalManager(self.store)
        self.feedback = FeedbackManager(self.store)
        self.policy = PolicyEngine(
            self.store, channels=self.channels, config=self._policy_config_from_agent_config()
        )
        self.dispatcher = OutboxDispatcher(self.store, policy=self.policy, sources=None)
        self.coordinator = ProactiveCoordinator(
            self.store,
            registry=self.registry,
            pack_builder=self.pack_builder,
            executor=self.executor,
            config=self.coordinator.config if hasattr(self.coordinator, "config") else None,
            tool_allowlist=self.coordinator.tool_allowlist,
            policy_engine=self.policy,
        )
        self.scheduler = Scheduler(self.store, self.store.clock)
        self._hook_runner = None
