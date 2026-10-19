"""Parsing and validating a returned decision (SPEC §8.1).

Split out of ``ToolLoopExecutor`` in v0.1.2 (§22.1 item 3) because the same
validation must apply to *any* producer of a decision — the built-in loop
reading a model response, a host bridge reading a Pi/Hermes final message,
or a third-party executor. Two copies of this would drift, and the copy
that drifts is the one that lets fabricated evidence through.

Strictness is deliberate:

* the model's text must be strict JSON — duplicate keys and NaN/Infinity
  are protocol violations, not silent overwrites;
* unknown fields are rejected at both levels;
* ``contracts.validate_decision`` covers the cross-field rules;
* evidence closure is checked against *this run's* evidence universe, so a
  plausible-looking ref that was never observed is refused.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .contracts import (
    MODEL_SILENCE_REASONS,
    SILENCE_UNSPECIFIED,
    ActionProposal,
    Decision,
    ErrorCode,
    PASError,
    validate_decision,
)

__all__ = ["PROPOSAL_KEYS", "DECISION_KEYS", "strict_json", "parse_decision_text"]

DECISION_KEYS = frozenset({"decision", "summary", "proposals", "silence_reason"})
PROPOSAL_KEYS = frozenset(
    {
        "kind",
        "fact_id",
        "revision",
        "body",
        "arguments",
        "evidence_refs",
        "expires_at",
        "reason",
        "memory_candidates",
    }
)
PROPOSAL_KINDS: tuple[str, ...] = (
    "notify_self",
    "draft",
    "internal_record",
    "suggest_watch",
    "request_external_action",
)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def strict_json(text: str) -> Any:
    """Strict JSON for model output: duplicate keys and NaN/Infinity are
    protocol violations, not silent overwrites."""
    return json.loads(
        text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
    )


def parse_decision_text(
    content: str | None,
    *,
    evidence: frozenset[str],
    max_proposals: int,
    require_reason: bool = True,
    on_unknown_silence_reason: Callable[[str], None] | None = None,
) -> Decision:
    """Turn a producer's final text into a validated :class:`Decision`.

    Raises ``PASError`` for anything that does not meet the contract. The
    caller decides whether to spend its single bounded repair attempt; this
    function never guesses an action out of mixed text (§8.1).

    ``require_reason`` is the gray-release switch for decision contract 1.1
    (missing reason accepted → ``unspecified``); an illegal reason is always
    rejected. ``on_unknown_silence_reason`` receives the raw unknown
    ``silence_reason`` value: unlike every other field, an unknown silence
    reason is *normalised* rather than fatal (W3), but the caller must still
    record the warning.
    """
    if content is None or not content.strip():
        raise PASError(ErrorCode.INVALID_CONFIG, "final turn had no content")
    try:
        raw = strict_json(content)
    except ValueError as exc:
        raise PASError(ErrorCode.INVALID_CONFIG, f"content is not strict JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise PASError(ErrorCode.INVALID_CONFIG, "decision must be a JSON object")
    unknown = set(raw) - DECISION_KEYS
    if unknown:
        raise PASError(ErrorCode.INVALID_CONFIG, f"unknown decision fields: {sorted(unknown)}")
    raw_proposals = raw.get("proposals", [])
    if not isinstance(raw_proposals, list):
        raise PASError(ErrorCode.INVALID_CONFIG, "proposals must be a list")
    if len(raw_proposals) > max_proposals:
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            f"{len(raw_proposals)} proposals exceed the budget of {max_proposals}",
        )
    proposals: list[ActionProposal] = []
    for index, raw_proposal in enumerate(raw_proposals):
        if not isinstance(raw_proposal, dict):
            raise PASError(ErrorCode.INVALID_CONFIG, f"proposals[{index}] must be an object")
        unknown = set(raw_proposal) - PROPOSAL_KEYS
        if unknown:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"proposals[{index}] has unknown fields: {sorted(unknown)}",
            )
        kind = raw_proposal.get("kind")
        if kind not in PROPOSAL_KINDS:
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"proposals[{index}].kind {kind!r} unsupported"
            )
        proposals.append(
            ActionProposal(
                kind=kind,
                fact_id=raw_proposal.get("fact_id"),
                revision=raw_proposal.get("revision"),
                body=raw_proposal.get("body"),
                arguments=raw_proposal.get("arguments"),
                evidence_refs=tuple(raw_proposal.get("evidence_refs") or []),
                expires_at=raw_proposal.get("expires_at"),
                reason=raw_proposal.get("reason"),
                memory_candidates=raw_proposal.get("memory_candidates", ()),
            )
        )
    decision_kind = raw.get("decision")
    silence_reason = raw.get("silence_reason")
    if decision_kind != "silent" and silence_reason is not None:
        # Strict JSON: a field that does not belong to this decision shape is
        # an invalid decision, not something to silently drop.
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            "only decision=silent may carry 'silence_reason'",
        )
    if decision_kind == "silent":
        allowed = MODEL_SILENCE_REASONS + (SILENCE_UNSPECIFIED,)
        if silence_reason is None:
            silence_reason = SILENCE_UNSPECIFIED
        elif silence_reason not in allowed:
            if on_unknown_silence_reason is not None:
                on_unknown_silence_reason(str(silence_reason)[:200])
            silence_reason = SILENCE_UNSPECIFIED
    decision = Decision(
        decision=decision_kind,
        summary=raw.get("summary"),
        proposals=tuple(proposals),
        silence_reason=silence_reason if decision_kind == "silent" else None,
    )
    problems = validate_decision(
        decision.to_wire_dict(), require_reason=require_reason
    )
    if problems:
        raise PASError(ErrorCode.INVALID_CONFIG, "; ".join(problems))
    for proposal in decision.proposals:
        refs = list(proposal.evidence_refs) + [ref for candidate in proposal.memory_candidates
                                              for ref in candidate["evidence_refs"]]
        fabricated = [ref for ref in refs if ref not in evidence]
        if fabricated:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"proposal for fact {proposal.fact_id!r} cites unknown evidence"
                f" (first: {fabricated[0]!r})",
            )
    return decision
