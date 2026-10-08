"""Long-running daemon (SPEC §17.1; OPS-01; P7).

Startup sequence (§17.1 启动): acquire the single-instance lock → open
the store (schema check via migrations) → clean hook staging leftovers
→ resolve cancel flags orphaned by a crash → sweep expired deliveries →
recompute due jobs (admit) → serve. Recovery is deliberately
conservative: running runs whose worker died keep their lease and are
reclaimed with attempt+1 by the normal fencing path — the daemon never
rewrites ``running`` to ``pending`` on exit, because a running run may
have external side effects it cannot describe.

Shutdown (§17.1 停止): stop admitting → request worker cancel/drain →
wait up to ``shutdown_grace_seconds`` → close connections. A second
signal forces immediately. Drain and force are distinct outcomes and
the stop report says which one happened.

Supervision belongs to the platform (systemd/container/launchd in
``deploy/``): this process stays in the foreground, exits non-zero on
fatal startup errors, and writes ``health.json`` each cycle so a
supervisor or human can check readiness without owning the RPC client.

Disk-full (OPS-01 磁盘满报警): free space below ``disk_free_warn_mb``
degrades health and logs once per crossing; below ``disk_free_stop_mb``
the daemon stops admitting new work (fail closed) but keeps serving
reads so the operator can clean up.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path
from typing import Any

from .contracts import ErrorCode, PASError
from .observability import Metrics, StructuredLogger, free_disk_mb
from .rpc_server import ControlPlaneServer

__all__ = ["PasDaemon", "DaemonLock", "LOCK_FILENAME"]

LOCK_FILENAME = "daemon.lock"


class DaemonLock:
    """Cooperative single-instance lock for one state directory.

    ``daemon.lock`` holds the owning PID. A lock whose PID is provably
    dead (os.kill(pid, 0) raises ESRCH) is stale and may be taken over;
    anything else — alive owner, unreadable file, foreign format — is a
    refusal. The lock file is removed only by the holder. This is
    cooperative discipline for one trusted user's machine (单主机边界),
    not multi-tenant security.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / LOCK_FILENAME
        self.held = False

    def acquire(self) -> bool:
        if self.held:
            return True
        if self.path.exists():
            if self._owner_alive():
                return False
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode())
        finally:
            os.close(fd)
        self.held = True
        return True

    def _owner_alive(self) -> bool:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return True  # unreadable → assume alive (fail closed)
        owner = None
        for part in text.split():
            if part.startswith("pid="):
                owner = part[4:]
        if owner is None or not owner.isdigit():
            return True
        try:
            os.kill(int(owner), 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


class PasDaemon:
    """Drives the facade's kernels on an interval until stopped."""

    def __init__(
        self,
        agent: Any,
        *,
        logger: StructuredLogger | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.agent = agent
        self.lock = DaemonLock(agent.state_dir)
        self.logger = logger or StructuredLogger(
            component="pas.daemon", fields={"profile": agent.profile}
        )
        self.metrics = metrics if metrics is not None else agent.metrics
        self.loop_interval_s = (
            agent.config.runtime.loop_interval_seconds if agent.config is not None else 5.0
        )
        self.grace_s = (
            float(agent.config.runtime.shutdown_grace_seconds)
            if agent.config is not None
            else 20.0
        )
        self.disk_warn_mb = agent.config.runtime.disk_free_warn_mb if agent.config is not None else 50
        self.disk_stop_mb = agent.config.runtime.disk_free_stop_mb if agent.config is not None else 5
        self._inflight: dict[str, tuple[asyncio.Task[Any], asyncio.Event]] = {}
        self._stop_event = asyncio.Event()
        self._force_event = asyncio.Event()
        self._rpc_server: ControlPlaneServer | None = None
        self._disk_alerted = False
        self._disk_stopped = False
        self._started = False
        self._last_admit_ms: int | None = None
        self._loop_task: asyncio.Task[Any] | None = None

    # ------------------------------------------------------------------ #
    # Startup / shutdown
    # ------------------------------------------------------------------ #

    async def start(self, *, grace_s: float | None = None) -> None:
        """Acquire the lock and run the recovery pass; the loop then
        runs as a task on the caller's loop (embedded mode)."""
        if self._started:
            raise PASError(ErrorCode.INVALID_CONFIG, "daemon already started", scope="daemon")
        if not self.lock.acquire():
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "another daemon owns this state directory; use `pas doctor` to inspect it",
                scope="daemon",
            )
        self._started = True
        if grace_s is not None:
            self.grace_s = float(grace_s)
        try:
            self._recover()
        except BaseException:
            self.lock.release()
            self._started = False
            raise
        try:
            await self._start_control_plane()
        except BaseException:
            self.lock.release()
            self._started = False
            raise
        self._stop_event.clear()
        self._force_event.clear()
        self._loop_task = asyncio.create_task(self._loop(), name="pas-daemon-loop")
        self.logger.info("daemon_started", extra_state=str(self.agent.state_dir))

    async def serve_forever(self) -> None:
        """Foreground mode: SIGINT/SIGTERM request a drain, a second
        signal forces. Returns when the shutdown pass completed."""
        loop = asyncio.get_running_loop()
        signal_count = {"n": 0}

        def _on_signal() -> None:
            signal_count["n"] += 1
            if signal_count["n"] == 1:
                self.logger.info("signal_stop_requested", signal="TERM/INT")
                self._stop_event.set()
            else:
                self.logger.warning("force_stop_requested", signal_count=signal_count["n"])
                self._force_event.set()
                self._stop_event.set()

        installed: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover — non-main loop
                pass

        await self.start()
        try:
            await self._stop_event.wait()
            await self._shutdown(drain=not self._force_event.is_set())
        finally:
            for sig in installed:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):  # pragma: no cover
                    pass

    async def stop(self, *, drain: bool = True, grace_s: float | None = None) -> dict[str, Any]:
        """Embedded-mode stop; returns what actually happened."""
        if not self._started:
            return {"stopped": False, "reason": "not_started"}
        if grace_s is not None:
            self.grace_s = float(grace_s)
        self._stop_event.set()
        report = await self._shutdown(drain=drain)
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        self._started = False
        return report

    def request_stop(self) -> None:
        self._stop_event.set()

    def request_cancel(self, run_id: str) -> None:
        entry = self._inflight.get(run_id)
        if entry is not None:
            entry[1].set()

    async def _shutdown(self, *, drain: bool) -> dict[str, Any]:
        self._stop_event.set()
        inflight_ids = list(self._inflight)
        interrupted: list[str] = []
        drained = 0
        if self._inflight:
            if drain:
                pending = [task for task, _ in self._inflight.values()]
                done, still_running = await asyncio.wait(pending, timeout=self.grace_s)
                drained = len(done)
                for task in still_running:
                    interrupted.append(task.get_name())
                    task.cancel()
                if still_running:
                    await asyncio.gather(*still_running, return_exceptions=True)
                    self.logger.warning(
                        "drain_grace_exceeded",
                        reason=f"{len(still_running)} run(s) past grace; leases stay live for recovery",
                    )
            else:
                for run_id, (task, cancel_event) in self._inflight.items():
                    interrupted.append(run_id)
                    cancel_event.set()
                    task.cancel()
                await asyncio.gather(
                    *(task for task, _ in self._inflight.values()), return_exceptions=True
                )
        self._inflight.clear()
        if self._rpc_server is not None:
            await self._rpc_server.close()
            self._rpc_server = None
        self.lock.release()
        report = {
            "drain": drain,
            "inflight": inflight_ids,
            "drained": drained,
            "interrupted": interrupted,
            "grace_s": self.grace_s,
        }
        self.logger.info("daemon_stopped", **{k: v for k, v in report.items()})
        return report

    # ------------------------------------------------------------------ #
    # Recovery (§17.1 启动)
    # ------------------------------------------------------------------ #

    def _recover(self) -> None:
        now = self.agent.store.clock.wall_now_ms()
        resolved = self.agent.store.resolve_cancel_requested(now_ms=now)
        expired = self.agent.store.expire_due_messages(now_ms=now)
        promoted = self.agent.store.promote_deferred(now_ms=now)
        staging_cleaned = self._clean_hook_staging()
        admission = self.agent.scheduler.admit_due(now_ms=now)
        self._last_admit_ms = now
        self.logger.info(
            "recovery_complete",
            cancels_resolved=resolved,
            deliveries_expired=expired,
            deferred_promoted=promoted,
            staging_removed=staging_cleaned,
            jobs_admitted=len(admission.admitted),
        )

    def _clean_hook_staging(self) -> int:
        staging = self.agent.state_dir / "hooks-staging"
        if not staging.is_dir():
            return 0
        removed = 0
        for entry in staging.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    async def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._cycle()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.loop_interval_s)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a dead loop must be visible
            self.logger.error("loop_crashed", reason=f"{type(exc).__name__}: {exc}")
            raise

    async def _cycle(self) -> None:
        agent = self.agent
        now = agent.store.clock.wall_now_ms()

        # -- disk alarm (OPS-01) ------------------------------------------
        free = free_disk_mb(agent.state_dir)
        if free is not None:
            if free < self.disk_stop_mb:
                if not self._disk_stopped:
                    self.logger.error("disk_space_critical", free_mb=free, action="stop_admitting")
                    self._disk_stopped = True
            elif free < self.disk_warn_mb:
                if not self._disk_alerted:
                    self.logger.warning("disk_space_low", free_mb=free, warn_mb=self.disk_warn_mb)
                    self._disk_alerted = True
            else:
                self._disk_alerted = False
                self._disk_stopped = False

        if not self._disk_stopped:
            admission = agent.scheduler.admit_due(now_ms=now)
            if admission.admitted:
                self.metrics.inc("wake", len(admission.admitted))
                self.logger.info("jobs_admitted", count=len(admission.admitted))
            agent.store.resolve_cancel_requested(now_ms=now)

        await self._run_hooks(now)
        await self._pump_runs()
        await self._pump_delivery(now)

        health = agent.write_health_file(
            scheduler_age_ms=(now - self._last_admit_ms) if self._last_admit_ms else None,
            db_ok=True,
        )
        if health["degraded"]:
            self.logger.warning("health_degraded", reason=",".join(health["reasons"]))

    async def _run_hooks(self, now: int) -> None:
        due = self.agent.store.due_hook_ids(now)
        if not due:
            return
        try:
            runner = self.agent._hooks()
        except PASError as exc:
            self.logger.error("hooks_unavailable", reason=exc.safe_message)
            return
        try:
            # Inline, not to_thread: the store connection is bound to its
            # creating thread, and the hook runner is single-flight anyway.
            summary = runner.run_due_hooks(now_ms=now)
            for report in summary.reports:
                self.logger.info("hook_ran", extra_hook=report.hook_id, phase=report.outcome)
        except PASError as exc:
            self.logger.error("hook_failed", reason=exc.safe_message)

    async def _pump_runs(self) -> None:
        agent = self.agent
        max_concurrent = agent.runtime_config.max_concurrent_agent_runs
        # Reap finished tasks first.
        for run_id in [rid for rid, (task, _) in self._inflight.items() if task.done()]:
            task, _ = self._inflight.pop(run_id)
            try:
                report = task.result()
                if report is not None:
                    self.logger.info(
                        "run_finished",
                        run_id=run_id,
                        phase=report.outcome,
                        reason=report.reason,
                        model_turns=report.model_turns,
                    )
                    if report.outcome == "suppressed":
                        self.metrics.inc("suppressed")
                    elif report.outcome == "failed":
                        self.metrics.inc("failed_runs")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the run failed; the loop continues
                self.logger.error("run_crashed", run_id=run_id, reason=type(exc).__name__)

        while len(self._inflight) < max_concurrent and not self._stop_event.is_set():
            known = set(self._inflight)
            cancel_event = asyncio.Event()
            task = asyncio.create_task(
                agent.coordinator.process_pending_run(cancel_event=cancel_event),
                name="pas-run",
            )
            await asyncio.sleep(0)  # let the task reach its claim (synchronous before first await)
            if task.done():
                try:
                    report = task.result()
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("run_crashed", reason=type(exc).__name__)
                    report = None
                if report is None:
                    break  # nothing claimable this cycle — stop pumping
                self.logger.info(
                    "run_finished", run_id=report.run_id, phase=report.outcome,
                    reason=report.reason,
                )
                if report.outcome == "suppressed":
                    self.metrics.inc("suppressed")
                elif report.outcome == "failed":
                    self.metrics.inc("failed_runs")
                continue
            claimed = self._running_runs() - known
            if not claimed:
                # The claim did not land under this task's name (raced
                # with another claimant): wait it out, then stop pumping.
                try:
                    report = await asyncio.wait_for(task, timeout=1.0)
                    if report is not None:
                        self.logger.info(
                            "run_finished", run_id=report.run_id, phase=report.outcome
                        )
                except asyncio.TimeoutError:
                    task.cancel()
                    self.logger.error("run_claim_unresolved", reason="no running run matched")
                break
            run_id = sorted(claimed)[0]
            self._inflight[run_id] = (task, cancel_event)

    def _running_runs(self) -> set[str]:
        rows = self.agent.store.db.execute(
            "SELECT run_id FROM runs WHERE state='running'"
        ).fetchall()
        return {row["run_id"] for row in rows}

    async def _pump_delivery(self, now: int) -> None:
        agent = self.agent
        try:
            reports = await agent.dispatcher.dispatch_due(now_ms=now)
            for report in reports:
                self.logger.info(
                    "delivery_attempted",
                    extra_action=report.message_id,
                    phase=report.state,
                    reason=report.reason,
                )
                if report.state == "provider_accepted":
                    self.metrics.inc("delivery_accepted")
                elif report.state in ("failed_retryable", "failed_terminal"):
                    self.metrics.inc("delivery_failed")
            reconciled = await agent.dispatcher.reconcile_unknowns(now_ms=now)
            for report in reconciled:
                self.logger.info(
                    "delivery_reconciled", extra_action=report.message_id, phase=report.state
                )
            agent.store.promote_deferred(now_ms=now)
            agent.store.expire_due_messages(now_ms=now)
            counts = agent.store.outbox_state_counts()
            self.metrics.set_gauge(
                "outbox_pending", counts.get("queued", 0) + counts.get("deferred", 0)
            )
            self.metrics.set_gauge("delivery_unknown", counts.get("unknown", 0))
        except PASError as exc:
            self.logger.error("delivery_cycle_failed", reason=exc.safe_message)

    # ------------------------------------------------------------------ #
    # Control plane
    # ------------------------------------------------------------------ #

    async def _start_control_plane(self) -> None:
        agent = self.agent
        enabled = agent.config.control_plane.enabled if agent.config is not None else True
        if not enabled:
            self.logger.info("control_plane_disabled")
            return
        socket_path = (
            Path(agent.config.control_plane.socket)
            if agent.config is not None and agent.config.control_plane.socket
            else agent.state_dir / "pas.sock"
        )
        token = self._load_token()
        self._rpc_server = ControlPlaneServer(
            agent, socket_path=socket_path, token=token, logger=self.logger, metrics=self.metrics
        )
        await self._rpc_server.start()
        self.logger.info("control_plane_listening", extra_socket=str(socket_path))

    def _load_token(self) -> str | None:
        agent = self.agent
        token_file = agent.config.control_plane.token_file if agent.config is not None else None
        if not token_file:
            return None
        path = Path(token_file).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            self.logger.error("token_file_unreadable", reason="configured token file cannot be read")
            return None
        return token or None
