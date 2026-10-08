"""The host bridge: drive any agent that answers a prompt (SPEC §22.1 item 3).

Before v0.1.2, "using another agent" meant using one of two hand-written
adapters, each covering half the job. The bridge is the missing layer:

    (RunRequest, ContextPack, source_records, instruction)
        -> render the context + inject the decision contract
        -> hand the prompt to a HostDriver
        -> parse and validate the envelope against this run's evidence
        -> ExecutorOutcome

A driver only has to answer a prompt. That is the whole contract, and it is
what makes "text in, text out" hosts usable — see
``tests/test_host_bridge.py::TextInTextOutHost``.

Two honesty rules the bridge exists to enforce:

* **Acceptance is not completion.** ``HostDriver.submit`` must return only
  once the host reached a *terminal* state. A driver that returns at
  acceptance would make PAS report an answer that had not been produced
  yet, so the bridge treats a driver contract violation as a failure.
* **Cancellation is not stopping.** ``HostDriver.cancel`` reports what the
  host actually confirmed; the bridge records that level in the run ledger
  rather than claiming the run stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from .context import evidence_universe, render_context_message
from .contracts import ErrorCode, PASError, RunBudget, RunRequest
from .decision_contract import agent_system_prompt
from .decision_parse import parse_decision_text
from .executor import (
    ExecutorContext,
    ExecutorOutcome,
    RunCancelled,
    RunEventRecord,
)

__all__ = [
    "CANCEL_CONFIRMED",
    "CANCEL_REQUESTED",
    "CANCEL_UNSUPPORTED",
    "HOST_CANCEL_LEVELS",
    "HostPrompt",
    "HostReply",
    "HostDriver",
    "HostBridge",
]

#: What a host's cancel acknowledgement actually means.
CANCEL_CONFIRMED = "confirmed"      # the host states the run is terminal
CANCEL_REQUESTED = "requested"      # accepted, outcome not yet known
CANCEL_UNSUPPORTED = "unsupported"  # the host cannot cancel at all
HOST_CANCEL_LEVELS = (CANCEL_CONFIRMED, CANCEL_REQUESTED, CANCEL_UNSUPPORTED)

_DEFAULT_HOST_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class HostPrompt:
    """One question for a host.

    ``system`` carries the decision contract verbatim; a host that can only
    take a single string uses :meth:`render`.
    """

    run_key: str
    system: str
    user: str
    budget: RunBudget = field(default_factory=RunBudget)

    def render(self) -> str:
        """Flatten to the single string a black-box host accepts."""
        return f"{self.system}\n\n{self.user}"


@dataclass(frozen=True)
class HostReply:
    """What a host produced, before PAS validates it.

    ``usage`` is whatever the host could measure; anything it did not report
    stays absent, and the bridge marks the pricing basis ``unknown`` rather
    than inventing a zero.
    """

    text: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    host_run_id: str | None = None


@runtime_checkable
class HostDriver(Protocol):
    """A host that can answer one prompt at a time.

    ``submit`` returns **only on a terminal state**; returning at acceptance
    is a contract violation. Recoverable transport problems raise a
    retryable ``PASError``; a run the host reports as interrupted raises
    :class:`~proactive_sdk.executor.RunCancelled`.
    """

    async def capabilities(self) -> dict[str, Any]: ...

    async def submit(self, prompt: HostPrompt, *, timeout_s: float) -> HostReply: ...

    async def cancel(self, run_key: str) -> str: ...

    async def close(self) -> None: ...


class HostBridge:
    """``executor.RunExecutor`` over any :class:`HostDriver`.

    The bridge owns everything that must not vary between hosts: prompt
    assembly, contract injection, envelope parsing, evidence closure and the
    run-ledger events. A driver owns only transport.
    """

    def __init__(
        self,
        driver: HostDriver,
        *,
        capabilities: frozenset[str] | set[str] | tuple[str, ...] | Callable[[], Any] = (),
        tool_names: tuple[str, ...] = (),
        budget: RunBudget | None = None,
        host_timeout_s: float = _DEFAULT_HOST_TIMEOUT_S,
        usage_translator: Any | None = None,
        external_tool_broker: bool = False,
    ) -> None:
        if not isinstance(driver, HostDriver):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "driver must implement HostDriver (capabilities/submit/cancel/close)",
                scope="host-bridge",
            )
        if not isinstance(host_timeout_s, (int, float)) or host_timeout_s <= 0:
            raise PASError(ErrorCode.INVALID_CONFIG, "host_timeout_s must be positive")
        self.driver = driver
        self._capabilities = capabilities if callable(capabilities) else frozenset(capabilities)
        self._tool_names = tuple(tool_names)
        self._budget = budget or RunBudget()
        self.host_timeout_s = float(host_timeout_s)
        #: Optional ``dict -> dict`` normaliser for a host's usage shape.
        self._usage_translator = usage_translator
        # A host is a black box until proven otherwise: claiming PAS authority
        # without the host actually routing its tool calls through PAS's
        # broker would put a false statement in the run ledger.
        self.external_tool_broker = bool(external_tool_broker)

    # ------------------------------------------------------------------ #
    # executor.RunExecutor
    # ------------------------------------------------------------------ #

    async def context(self) -> ExecutorContext:
        return ExecutorContext(
            capabilities=frozenset(self._capabilities() if callable(self._capabilities) else self._capabilities),
            tool_names=self._tool_names,
            budget=self._budget,
            external_tool_broker=self.external_tool_broker,
        )

    async def close(self) -> None:
        await self.driver.close()

    async def execute(
        self,
        request: RunRequest,
        pack: Any,
        source_records: list[dict[str, Any]],
        *,
        instruction: str,
        cancel_event: Any | None = None,
    ) -> ExecutorOutcome:
        events: list[RunEventRecord] = []
        prompt = HostPrompt(
            run_key=request.run_id,
            system=agent_system_prompt(instruction, request.budget),
            user=render_context_message(pack, source_records),
            budget=request.budget,
        )
        if _cancelled(cancel_event):
            await self._cancel_and_record(prompt.run_key, events, scope="before_submit")
            raise RunCancelled()

        timeout_s = self._bounded_timeout(request, events)
        events.append(
            RunEventRecord(kind="host_submitted", safe_summary=f"driver={type(self.driver).__name__}")
        )
        try:
            reply = await self.driver.submit(prompt, timeout_s=timeout_s)
        except RunCancelled:
            events.append(RunEventRecord(kind="host_cancelled", safe_summary="host reported interrupted"))
            raise
        except PASError as exc:
            events.append(
                RunEventRecord(kind="host_failed", safe_summary=f"{exc.code.value}:{exc.retryable}")
            )
            raise
        if not isinstance(reply, HostReply):
            raise PASError(
                ErrorCode.INTERNAL_ERROR,
                "driver returned a non-HostReply (acceptance is not completion)",
                scope="host-bridge",
            )
        if _cancelled(cancel_event):
            # The host may have finished while the cancel request arrived;
            # what came back is still validated, but the run is not reported
            # as a clean completion.
            await self._cancel_and_record(prompt.run_key, events, scope="after_reply")
            raise RunCancelled()

        decision = parse_decision_text(
            reply.text,
            evidence=evidence_universe(pack, []),
            max_proposals=request.budget.max_proposals,
        )
        events.append(
            RunEventRecord(
                kind="host_replied",
                safe_summary=f"decision={decision.decision} proposals={len(decision.proposals)}",
            )
        )
        usage = self._usage(reply.usage)
        return ExecutorOutcome(
            decision=decision,
            usage=usage,
            model_turns=int(usage.get("host_turns") or 1),
            events=tuple(events),
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _bounded_timeout(self, request: RunRequest, events: list[RunEventRecord]) -> float:
        """The smaller of the configured host timeout and the run deadline."""
        remaining_s = self.host_timeout_s
        try:
            deadline = datetime.fromisoformat(
                str(request.deadline).replace("Z", "+00:00").replace("z", "+00:00")
            )
            if deadline.tzinfo is not None:
                now = datetime.now(timezone.utc)
                remaining_s = min(remaining_s, max(1.0, (deadline - now).total_seconds()))
        except (TypeError, ValueError):
            pass  # an unparseable deadline is the coordinator's problem, not a reason to hang
        return remaining_s

    async def _cancel_and_record(
        self, run_key: str, events: list[RunEventRecord], *, scope: str
    ) -> None:
        """Ask the host to stop and record *what it confirmed*, not what we
        hoped. A cancel request is never recorded as a stopped run."""
        try:
            level = await self.driver.cancel(run_key)
        except PASError:
            level = CANCEL_UNSUPPORTED
        if level not in HOST_CANCEL_LEVELS:
            level = CANCEL_REQUESTED
        events.append(
            RunEventRecord(kind="host_cancel", safe_summary=f"level={level} scope={scope}")
        )

    def _usage(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Normalise a host's usage report.

        Everything the host did not report is left absent rather than set to
        zero, and the pricing basis is ``unknown`` unless the host measured
        it — a fabricated ``0`` token count would read as "no cost".
        """
        if self._usage_translator is not None:
            translated = self._usage_translator(dict(raw))
            if isinstance(translated, dict):
                raw = translated
        usage: dict[str, Any] = {
            "protocol_version": "1.0",
            "host_turns": 1,
            "pricing_basis": raw.get("pricing_basis") or "unknown",
        }
        for key in (
            "provider",
            "model",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "tool_calls",
            "elapsed_ms",
        ):
            if key in raw:
                usage[key] = raw[key]
        return usage


def _cancelled(cancel_event: Any | None) -> bool:
    return cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)()
