"""Bounded ToolLoopExecutor (SPEC §8; P3 / EXEC-01).

Two-stage decisioning (§8.1): deterministic eligibility happens before
this executor runs (coordinator L0); inside the run, the model reasons
with a budget and produces a structured Decision, and every proposal is
re-validated deterministically afterwards. Permissions, dedupe and the
ledger are never delegated to the model.

Loop order per turn (§8.2):

    deadline / cancel / budget checks
      → ModelPort.generate
      → validate tool-call shape (call_id, name, arguments)
      → ToolBroker re-checks allowlist / capability / budget, executes
      → bounded tool results appended as data-only messages
      → until a parseable Decision or a budget limit

Failure rules: model parse failure gets exactly one bounded repair
attempt, then the run fails — prose is never mined for an action. A
proposal is accepted only when its evidence refs all belong to this
run's evidence universe (source snapshots + memory + broker-produced
tool evidence); fabricated evidence invalidates the whole Decision.
Budgets (turns, tool calls, wall time, proposals) and the wall-clock
deadline are enforced; wall-time measurement uses the injected Clock's
monotonic side — infrastructure time, never profile logical time.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from .contracts import (
    TOOL_AUTHORITY_HOST,
    TOOL_AUTHORITY_PAS_BROKER,
    TOOL_AUTHORITIES,
    TOOL_AUTHORITY_UNKNOWN,
    ActionProposal,
    Decision,
    ErrorCode,
    ExecutorCapabilities,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    PASError,
    RunBudget,
    RunRequest,
    ContextPack,
    validate_decision,
)
from .context import evidence_universe, render_context_message
from .decision_contract import agent_system_prompt
from .decision_parse import parse_decision_text
from .tools import BrokerCallContext, LocalToolBroker, ToolCallAttempt

__all__ = [
    "TOOL_AUTHORITY_PAS_BROKER",
    "TOOL_AUTHORITY_HOST",
    "TOOL_AUTHORITY_UNKNOWN",
    "TOOL_AUTHORITIES",
    "ExecutorContext",
    "RunExecutor",
    "ExecutorConfig",
    "RunEventRecord",
    "ExecutorOutcome",
    "RunCancelled",
    "ToolLoopExecutor",
]

_DECISION_KINDS = ("notify_self", "draft", "internal_record", "suggest_watch", "request_external_action")


@dataclass(frozen=True)
class ExecutorConfig:
    budget: RunBudget = field(default_factory=RunBudget)
    tool_call_timeout_s: float = 10.0
    repair_attempts: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.budget, RunBudget):
            raise PASError(ErrorCode.INVALID_CONFIG, "budget must be RunBudget")
        if not isinstance(self.tool_call_timeout_s, (int, float)) or self.tool_call_timeout_s <= 0:
            raise PASError(ErrorCode.INVALID_CONFIG, "tool_call_timeout_s must be positive")
        if not isinstance(self.repair_attempts, int) or not 0 <= self.repair_attempts <= 3:
            raise PASError(ErrorCode.INVALID_CONFIG, "repair_attempts must be 0..3")


@dataclass(frozen=True)
class RunEventRecord:
    """One observation event destined for the run ledger. Summaries carry
    counts/refs/reasons only — never content or reasoning (§7.3)."""

    kind: str
    safe_summary: str | None = None


@dataclass(frozen=True)
class ExecutorOutcome:
    decision: Decision
    usage: dict[str, Any]
    source_records: list[dict[str, Any]] = field(default_factory=list)
    model_turns: int = 0
    tool_calls: int = 0
    tool_denied: int = 0
    repair_used: bool = False
    tool_evidence_refs: tuple[str, ...] = ()
    events: tuple[RunEventRecord, ...] = ()


@dataclass(frozen=True)
class ExecutorContext:
    """Everything the coordinator knows about an executor *before* it runs.

    This is deliberately the whole of it: which source capabilities are
    currently available, which tools may be offered to the model, and the
    budget the run must stay inside. A built-in loop answers it from its
    broker and config; a host adapter answers it from its own state (a
    Hermes/Pi adapter may have to ask the host). Nothing here reveals *how*
    the analysis will be produced, which is what makes the executor
    replaceable (SPEC §22.1 item 1).

    Note the distinction from ``ToolLoopExecutor.capabilities`` (an
    ``ExecutorCapabilities`` record of booleans like ``streaming``): the
    ``capabilities`` here are the *strings* a source binding requires, e.g.
    ``calendar.read``.
    """

    capabilities: frozenset[str] = frozenset()
    tool_names: tuple[str, ...] = ()
    budget: RunBudget = field(default_factory=RunBudget)
    #: True only when the executor routes its tool calls through PAS's
    #: broker. Defaults to False because a host that says nothing is a
    #: host PAS cannot vouch for, and the ledger must say so.
    external_tool_broker: bool = False

    @property
    def tool_authority(self) -> str:
        """Which authority governed this run's side effects."""
        return (
            TOOL_AUTHORITY_PAS_BROKER
            if self.external_tool_broker
            else TOOL_AUTHORITY_HOST
        )

    def __post_init__(self) -> None:
        raw = self.capabilities
        if isinstance(raw, str) or not isinstance(raw, (frozenset, set, tuple, list)):
            raise PASError(
                ErrorCode.INVALID_CONFIG, "capabilities must be a set of strings"
            )
        names = tuple(raw)
        for name in names:
            if not isinstance(name, str) or not name:
                raise PASError(
                    ErrorCode.INVALID_CONFIG, "capability names must be non-empty strings"
                )
        object.__setattr__(self, "capabilities", frozenset(names))

        tools = self.tool_names
        if isinstance(tools, str) or not isinstance(tools, (tuple, list)):
            raise PASError(ErrorCode.INVALID_CONFIG, "tool_names must be a sequence of strings")
        tools = tuple(tools)
        for name in tools:
            if not isinstance(name, str) or not name:
                raise PASError(
                    ErrorCode.INVALID_CONFIG, "tool names must be non-empty strings"
                )
        object.__setattr__(self, "tool_names", tools)

        if not isinstance(self.budget, RunBudget):
            raise PASError(ErrorCode.INVALID_CONFIG, "budget must be RunBudget")
        if not isinstance(self.external_tool_broker, bool):
            raise PASError(
                ErrorCode.INVALID_CONFIG, "external_tool_broker must be a boolean"
            )


@runtime_checkable
class RunExecutor(Protocol):
    """The single seam the coordinator drives for one run.

    A built-in bounded loop (``ToolLoopExecutor``), a Hermes/Pi host driven
    through a bridge, and a third-party agent all satisfy this same shape,
    so the coordinator never depends on how the analysis is produced.

    This is intentionally NOT ``contracts.AgentExecutor``: that port
    (SPEC §4.2) describes a *long-lived host session* — ``start`` /
    ``events`` / ``status`` / ``cancel``. A host adapter implements
    ``AgentExecutor`` and exposes ``RunExecutor`` on top of it; the
    coordinator only ever sees this narrower one. Before v0.1.2 the
    coordinator read ``.broker`` / ``.config`` off a concrete
    ``ToolLoopExecutor``, which made every third-party executor unusable
    even though the documented protocol said otherwise.
    """

    async def context(self) -> ExecutorContext: ...

    async def execute(
        self,
        request: Any,
        pack: Any,
        source_records: list[dict[str, Any]],
        *,
        instruction: str,
        cancel_event: Any | None = None,
    ) -> "ExecutorOutcome": ...


class RunCancelled(PASError):
    """The cancel event fired; the run stops without a decision."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CONFLICT, "run cancelled before completion", scope="executor"
        )


def _deadline_to_ms(deadline: str) -> int:
    parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00").replace("z", "+00:00"))
    if parsed.tzinfo is None:
        raise PASError(ErrorCode.INVALID_CONFIG, "deadline needs an explicit offset")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


class ToolLoopExecutor:
    """Runs one bounded analysis against a ModelPort and ToolBroker.

    The executor owns no store: the coordinator passes the effective
    allowlist/capabilities and persists the outcome under its lease
    fence. ``clock`` needs ``monotonic_ms`` (wall-time budget) and
    ``wall_now_ms`` (absolute run deadline); tests inject FakeClock."""

    def __init__(
        self,
        *,
        model: Any,
        broker: LocalToolBroker,
        capabilities: ExecutorCapabilities | None = None,
        config: ExecutorConfig | None = None,
        clock: Any | None = None,
    ) -> None:
        if not callable(getattr(model, "generate", None)):
            raise PASError(ErrorCode.INVALID_CONFIG, "model must provide generate()")
        # Duck-typed rather than ``isinstance(broker, LocalToolBroker)``: the
        # loop only ever uses these four members, and the hard check made
        # every third-party broker unusable while §4.2 documents ToolBroker
        # as a replaceable port (SPEC §22.1 item 1).
        for member in ("call", "tool_schemas", "tool_names", "capabilities"):
            if not hasattr(broker, member):
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    f"broker must provide {member!r} to be driven by the built-in executor",
                )
        self.model = model
        self.broker = broker
        self.capabilities = capabilities if capabilities is not None else ExecutorCapabilities(
            external_tool_broker=True, read_only_enforcement=True, usage_reporting=True
        )
        self.config = config if config is not None else ExecutorConfig()
        if clock is None:
            from .clock import SystemClock

            clock = SystemClock()
        self.clock = clock

    def _capabilities_snapshot(self) -> frozenset[str]:
        """The run's effective authorization, read fresh for this run.

        A broker may expose ``capabilities`` as a set or as a callable; the
        callable form is how an auto-assembled agent stays honest when the
        user grants something after construction (SPEC §22.1 item 9).
        """
        source = getattr(self.broker, "capabilities", frozenset())
        if callable(source):
            source = source()
        return frozenset(source)

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    async def context(self) -> ExecutorContext:
        """The coordinator's whole view of this executor."""
        return ExecutorContext(
            capabilities=self._capabilities_snapshot(),
            tool_names=tuple(self.broker.tool_names()),
            budget=self.config.budget,
            # The built-in loop can only reach a tool through its broker, so
            # this is a fact about the code path, not a declaration.
            external_tool_broker=True,
        )

    async def execute(
        self,
        request: RunRequest,
        pack: ContextPack,
        source_records: list[dict[str, Any]],
        *,
        instruction: str,
        cancel_event: asyncio.Event | None = None,
    ) -> ExecutorOutcome:
        budget = request.budget
        deadline_ms = _deadline_to_ms(request.deadline)
        start_mono_ms = self.clock.monotonic_ms()
        wall_budget_ms = budget.wall_time_s * 1000
        # Explicit allowlist only: an empty RunRequest.tool_allowlist means
        # "no tools this run", never "everything registered".
        tool_allowlist = frozenset(request.tool_allowlist)
        tool_schemas = self.broker.tool_schemas(tool_allowlist)

        events: list[RunEventRecord] = []
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(instruction, budget)},
            {"role": "user", "content": self._context_message(pack, source_records)},
        ]
        tool_evidence: list[str] = []
        turns = 0
        tool_calls = 0
        tool_denied = 0
        repairs_left = self.config.repair_attempts
        usage_total = self._empty_usage()

        while True:
            self._check_cancel(cancel_event)
            self._check_deadline(deadline_ms, start_mono_ms, wall_budget_ms)
            if turns >= budget.max_model_turns:
                raise PASError(
                    ErrorCode.BUDGET_EXCEEDED,
                    f"model turn budget exhausted after {turns} turns",
                    scope="executor",
                )
            model_request = ModelRequest(
                messages=tuple(messages),
                tool_schemas=tool_schemas,
                deadline=request.deadline,
            )
            response = await self._generate_bounded(model_request, start_mono_ms, wall_budget_ms)
            # Post-call re-check: the turn just consumed may have run past
            # the wall-time budget; overshoot is rejected, not accepted.
            self._check_deadline(deadline_ms, start_mono_ms, wall_budget_ms)
            turns += 1
            self._accumulate_usage(usage_total, response)
            events.append(RunEventRecord("model_turn", f"turn {turns}/{budget.max_model_turns}"))

            if response.tool_calls:
                if turns >= budget.max_model_turns:
                    # A tool turn that consumed the final turn can never
                    # produce a Decision afterwards; fail on budget now.
                    raise PASError(
                        ErrorCode.BUDGET_EXCEEDED,
                        "tool call on the final model turn leaves no turn for the decision",
                        scope="executor",
                    )
                messages.append(self._assistant_message(response))
                for call in response.tool_calls:
                    self._check_deadline(deadline_ms, start_mono_ms, wall_budget_ms)
                    if tool_calls >= budget.max_tool_calls:
                        raise PASError(
                            ErrorCode.BUDGET_EXCEEDED,
                            f"tool call budget exhausted after {tool_calls} calls",
                            scope="executor",
                        )
                    result = await self.broker.call(
                        ToolCallAttempt(
                            call_id=call.call_id, name=call.name, arguments=call.arguments
                        ),
                        context=BrokerCallContext(
                            run_id=request.run_id,
                            fence=request.fence,
                            allowlist=tool_allowlist,
                            capabilities=self._capabilities_snapshot(),
                            tool_calls_remaining=budget.max_tool_calls - tool_calls,
                            tool_call_timeout_s=self.config.tool_call_timeout_s,
                        ),
                    )
                    tool_calls += 1
                    if result.ok:
                        if result.evidence_ref:
                            tool_evidence.append(result.evidence_ref)
                    else:
                        tool_denied += 1
                        events.append(
                            RunEventRecord(
                                "tool_denied",
                                f"tool {call.name!r} denied: {result.error_code}",
                            )
                        )
                    messages.append(result.to_message())
                events.append(
                    RunEventRecord("tool_calls", f"{len(response.tool_calls)} attempted this turn")
                )
                continue

            # No tool calls: the content must be the final Decision.
            try:
                decision = self._parse_decision(
                    response.content,
                    evidence=evidence_universe(pack, tool_evidence),
                    max_proposals=min(budget.max_proposals, 8),
                )
            except PASError as exc:
                if repairs_left > 0:
                    repairs_left -= 1
                    messages.append(self._assistant_message(response))
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous output was not a valid Decision:"
                                f" {exc.safe_message}. Reply again with ONLY the"
                                " Decision JSON object, no prose."
                            ),
                        }
                    )
                    events.append(RunEventRecord("decision_repair", "invalid decision, one repair issued"))
                    continue
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    f"model produced an invalid decision: {exc.safe_message}",
                    scope="executor",
                ) from exc
            usage_total["tool_calls"] = tool_calls
            usage_total["elapsed_ms"] = self.clock.monotonic_ms() - start_mono_ms
            return ExecutorOutcome(
                decision=decision,
                usage=usage_total,
                source_records=source_records,
                model_turns=turns,
                tool_calls=tool_calls,
                tool_denied=tool_denied,
                repair_used=self.config.repair_attempts - repairs_left > 0,
                tool_evidence_refs=tuple(tool_evidence),
                events=tuple(events),
            )

    # ------------------------------------------------------------------ #
    # Checks
    # ------------------------------------------------------------------ #

    def _check_cancel(self, cancel_event: asyncio.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RunCancelled()

    def _check_deadline(self, deadline_ms: int, start_mono_ms: int, wall_budget_ms: int) -> None:
        if self.clock.wall_now_ms() >= deadline_ms:
            raise PASError(
                ErrorCode.DEADLINE_EXCEEDED,
                "run deadline exceeded",
                scope="executor",
            )
        if self.clock.monotonic_ms() - start_mono_ms > wall_budget_ms:
            raise PASError(
                ErrorCode.BUDGET_EXCEEDED,
                "wall-time budget exhausted",
                scope="executor",
            )

    async def _generate_bounded(
        self, request: ModelRequest, start_mono_ms: int, wall_budget_ms: int
    ) -> ModelResponse:
        """Generate with a hard asyncio timeout so a hung provider call
        cannot hold the run past its wall-time budget."""
        remaining_ms = wall_budget_ms - (self.clock.monotonic_ms() - start_mono_ms)
        timeout_s = max(remaining_ms / 1000, 0.001)
        try:
            return await asyncio.wait_for(self.model.generate(request), timeout_s)
        except asyncio.TimeoutError as exc:
            raise PASError(
                ErrorCode.BUDGET_EXCEEDED,
                "model call exceeded the wall-time budget",
                scope="executor",
            ) from exc

    # ------------------------------------------------------------------ #
    # Decision parsing (§8.1: strict schema, one bounded repair)
    # ------------------------------------------------------------------ #

    def _parse_decision(
        self, content: str | None, *, evidence: frozenset[str], max_proposals: int
    ) -> Decision:
        """Shared with every other producer of a decision (see
        ``decision_parse.parse_decision_text``)."""
        return parse_decision_text(
            content, evidence=evidence, max_proposals=max_proposals
        )

    # ------------------------------------------------------------------ #
    # Prompt assembly and usage accounting
    # ------------------------------------------------------------------ #

    def _system_prompt(self, instruction: str, budget: RunBudget) -> str:
        """The built-in loop is just one consumer of the decision contract.

        The text lives in ``decision_contract`` so a host adapter can inject
        the identical block instead of re-deriving it (SPEC §22.1 item 2).
        """
        return agent_system_prompt(instruction, budget)

    def _context_message(self, pack: ContextPack, source_records: list[dict[str, Any]]) -> str:
        """Shared with host bridges so both render the same context."""
        return render_context_message(pack, source_records)

    @staticmethod
    def _assistant_message(response: ModelResponse) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [
                {"id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in response.tool_calls
            ],
        }

    @staticmethod
    def _empty_usage() -> dict[str, Any]:
        """Start from the unknown-basis Usage: measured values replace
        None as turns report them; anything unreported stays None (never
        zero — §4.1, §8.3)."""
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "tool_calls": 0,
            "elapsed_ms": None,
            "pricing_basis": "unknown",
        }

    def _accumulate_usage(self, total: dict[str, Any], response: ModelResponse) -> None:
        usage = response.usage or {}
        if usage.get("pricing_basis") != "measured":
            # One unmeasured turn makes the whole aggregate conservative:
            # partial token sums would understate cost (§8.3), so every
            # untrustable field goes back to None, never a fake number.
            for field_name in ("input_tokens", "output_tokens", "cache_read_tokens"):
                total[field_name] = None
            total["pricing_basis"] = "unknown"
            return
        total["pricing_basis"] = "measured"
        for field_name in ("input_tokens", "output_tokens", "cache_read_tokens"):
            value = usage.get(field_name)
            if isinstance(value, int) and value >= 0:
                total[field_name] = (total[field_name] or 0) + value
            # Absent optional field (e.g. cache tokens): leave the running
            # value as-is (None if never measured) — absence of a subset
            # does not invalidate the measured basis.
