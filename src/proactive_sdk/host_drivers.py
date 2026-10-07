"""Hermes and Pi as host-bridge drivers (SPEC §22.1 item 3).

In v0.1.1 these were two standalone adapters, each covering half the job
and neither pluggable into the coordinator. Here they become what they
should always have been: **transport drivers**. Everything that must be
identical across hosts — prompt assembly, contract injection, envelope
parsing, evidence closure, usage honesty, cancel semantics — belongs to
``HostBridge`` and is not repeated below.

The one substantive difference between the two, and the reason the bridge
takes a ``HostPrompt`` rather than a string:

* Hermes accepts a *separate* instruction channel, so the driver uses it:
  the contract goes in ``instructions``, the context in ``input``.
* Pi accepts a single string per turn, so the driver flattens the prompt
  with :meth:`HostPrompt.render`. A black-box host is exactly this shape.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .contracts import ErrorCode, PASError
from .executor import RunCancelled
from .hermes import (
    CancellationUnconfirmed,
    HermesAcceptanceUnknown,
    HermesRunHandle,
    HermesRunRequest,
    HermesRunsExecutor,
)
from .host_bridge import (
    CANCEL_CONFIRMED,
    CANCEL_REQUESTED,
    CANCEL_UNSUPPORTED,
    HostPrompt,
    HostReply,
)
from .pi_worker import PiWorkerExecutor

__all__ = ["HermesHostDriver", "PiHostDriver"]

_HERMES_INSTRUCTIONS = (
    "You are the analysis stage of a personal proactive agent. Answer with the"
    " decision envelope described below and nothing else."
)


class HermesHostDriver:
    """``HostDriver`` over the Hermes Runs surface.

    Acceptance, completion and stopping stay distinct all the way through:
    ``start`` returns acceptance only, ``wait`` owns completion, and a
    cancelled/interrupted run raises ``RunCancelled`` rather than being
    reported as an answer.
    """

    def __init__(
        self,
        executor: HermesRunsExecutor,
        *,
        instructions: str = _HERMES_INSTRUCTIONS,
        session_id: str | None = None,
    ) -> None:
        if not isinstance(executor, HermesRunsExecutor):
            raise PASError(ErrorCode.INVALID_CONFIG, "executor must be a HermesRunsExecutor")
        self.executor = executor
        self.instructions = instructions
        self.session_id = session_id
        self._handles: dict[str, HermesRunHandle] = {}
        #: Run keys this driver has itself observed reaching a terminal state.
        self._settled: set[str] = set()

    async def capabilities(self) -> dict[str, Any]:
        return await self.executor.capabilities()

    async def submit(self, prompt: HostPrompt, *, timeout_s: float) -> HostReply:
        request = HermesRunRequest(
            operation_key=prompt.run_key,
            # Hermes separates system-level guidance from the user turn, so
            # the contract travels in `instructions` and the context in `prompt`.
            instructions=f"{self.instructions}\n\n{prompt.system}",
            prompt=prompt.user,
            session_id=self.session_id,
        )
        try:
            handle = await self.executor.start(request)
        except HermesAcceptanceUnknown as exc:
            # The POST may have been accepted remotely. Never re-submit blind;
            # the bridge surfaces this as a retryable unknown effect.
            raise PASError(
                ErrorCode.EFFECT_UNKNOWN,
                "hermes submit acceptance is unknown; reconcile before retrying",
                retryable=True,
            ) from exc
        self._handles[prompt.run_key] = handle
        result = await self.executor.wait(handle, timeout_s=timeout_s)
        # From here the driver has observed a terminal state itself, which is
        # what lets cancel() answer "already stopped" without re-asking.
        self._settled.add(prompt.run_key)
        if result.interrupted or result.handle.state == "cancelled":
            raise RunCancelled()
        if result.handle.state == "failed":
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"hermes run failed (host status {result.host_status or 'unknown'})",
                retryable=True,
            )
        if not isinstance(result.output, str):
            raise PASError(
                ErrorCode.INTERNAL_ERROR,
                "hermes reported completion without a string output",
            )
        return HostReply(
            text=result.output,
            usage=dict(result.usage or {}),
            host_run_id=result.handle.host_run_id,
        )

    async def cancel(self, run_key: str) -> str:
        # We saw this run reach a terminal state: it is not running, and that
        # is a confirmed fact rather than an unanswered request.
        if run_key in self._settled:
            return CANCEL_CONFIRMED
        handle = self._handles.get(run_key)
        if handle is None:
            return CANCEL_UNSUPPORTED
        try:
            await self.executor.cancel(handle)
        except CancellationUnconfirmed:
            return CANCEL_REQUESTED
        except PASError:
            return CANCEL_UNSUPPORTED
        return CANCEL_CONFIRMED

    async def close(self) -> None:
        await self.executor.close()


class PiHostDriver:
    """``HostDriver`` over one Pi worker session.

    Pi takes a single string per turn, so the whole prompt — contract
    included — is flattened into the ``instruction``. Before v0.1.2 the
    Pi path sent a bare instruction and the contract was simply absent,
    which is why a Pi worker could answer with perfectly reasonable text
    that PAS then had to reject.
    """

    def __init__(
        self,
        executor: PiWorkerExecutor,
        *,
        cwd: str,
        agent_dir: str | None = None,
    ) -> None:
        if not isinstance(executor, PiWorkerExecutor):
            raise PASError(ErrorCode.INVALID_CONFIG, "executor must be a PiWorkerExecutor")
        if not isinstance(cwd, str) or not cwd:
            raise PASError(ErrorCode.INVALID_CONFIG, "cwd must be a non-empty scratch dir")
        if not os.path.isdir(cwd):
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"cwd {cwd!r} is not an existing directory"
            )
        self.executor = executor
        self.cwd = cwd
        self.agent_dir = agent_dir
        self._settled: set[str] = set()

    async def capabilities(self) -> dict[str, Any]:
        """What the worker reports about itself.

        Starts the session if it has not been started yet: the worker is a
        lazily-connected host, and answering before connecting would return
        an empty dict that reads as "this host can do nothing" rather than
        "we have not asked yet".
        """
        await self._ensure_started()
        info = self.executor.worker_info
        return dict(info) if isinstance(info, dict) else {}

    async def submit(self, prompt: HostPrompt, *, timeout_s: float) -> HostReply:
        await self._ensure_started()
        result = await self.executor.run(
            run_id=prompt.run_key,
            instruction=prompt.render(),
            cwd=self.cwd,
            agent_dir=self.agent_dir,
            timeout_s=timeout_s,
        )
        self._settled.add(prompt.run_key)
        # The worker already validated the envelope; it is re-serialised so
        # the bridge performs the *same* validation every other host gets —
        # one validation path, not two.
        return HostReply(
            text=json.dumps(result.envelope, ensure_ascii=False),
            usage=dict(result.usage),
            host_run_id=result.host_session_ref,
        )

    async def cancel(self, run_key: str) -> str:
        # A run this driver already saw complete is not running. Asking the
        # worker would answer "no active run", which is not "unsupported" —
        # it is the outcome cancel was asking for.
        if run_key in self._settled:
            return CANCEL_CONFIRMED
        try:
            await self.executor.cancel(run_key)
        except PASError as exc:
            # cancellation_unconfirmed is CONFLICT: asked, not stopped.
            if exc.code is ErrorCode.CONFLICT:
                return CANCEL_REQUESTED
            return CANCEL_UNSUPPORTED
        return CANCEL_CONFIRMED

    async def close(self) -> None:
        await self.executor.close()

    async def _ensure_started(self) -> None:
        if not self.executor.is_started:
            await self.executor.start()
