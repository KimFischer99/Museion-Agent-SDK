"""Bounded handoff records and checkpoint identities; no scheduling or delivery."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .artifacts import normalize_artifact_ref
from .context import evidence_universe
from .contracts import ContextPack, ErrorCode, PASError, RunBudget, canonical_json, content_hash, validate_worker_bindings
from .decision_parse import parse_decision_text
from .executor import ExecutorOutcome
from .pathsafe import PathSafetyError, safe_join


def validate_outcome(outcome: ExecutorOutcome, pack: ContextPack, *, require_reason: bool, budget: RunBudget | None = None) -> None:
    """Custom executors pass the same decision/evidence boundary as built-in ones."""
    if budget is not None and (
        type(outcome.model_turns) is not int or not 0 <= outcome.model_turns <= budget.max_model_turns
        or type(outcome.tool_calls) is not int or not 0 <= outcome.tool_calls <= budget.max_tool_calls
        or len(outcome.decision.proposals) > budget.max_proposals
    ):
        raise PASError(ErrorCode.BUDGET_EXCEEDED, "executor exceeded the stage budget")
    wire = outcome.decision.to_wire_dict()
    wire.pop("protocol_version", None)
    parse_decision_text(canonical_json(wire), evidence=evidence_universe(pack, list(outcome.tool_evidence_refs)),
                        max_proposals=8, require_reason=require_reason)


def worker_report(outcome: ExecutorOutcome, pack: ContextPack, *, require_reason: bool, budget: RunBudget | None = None) -> dict[str, Any]:
    validate_outcome(outcome, pack, require_reason=require_reason, budget=budget)
    if any(p.kind != "internal_record" for p in outcome.decision.proposals):
        raise PASError(ErrorCode.PERMISSION_DENIED, "worker may only return internal records")
    bindings = list(outcome.tool_evidence_bindings)
    validate_worker_bindings(bindings)
    if set(outcome.tool_evidence_refs) != {b["ref"] for b in bindings}:
        raise PASError(ErrorCode.INVALID_CONFIG, "worker tool evidence lacks provenance")
    report = {"summary": outcome.decision.summary,
              "records": [p.to_dict() for p in outcome.decision.proposals], "evidence_bindings": bindings}
    if len(canonical_json(report)) > 32_000:
        raise PASError(ErrorCode.BUDGET_EXCEEDED, "worker report exceeds 32000 chars")
    return report


def artifact_hashes(report: dict[str, Any], root: Path | None) -> dict[str, str]:
    refs: set[str] = set()
    for record in report["records"]:
        values = (record.get("arguments") or {}).get("artifact_refs", [])
        if not isinstance(values, list) or len(values) > 32:
            raise PASError(ErrorCode.INVALID_CONFIG, "worker artifact_refs must contain at most 32 refs")
        refs.update(normalize_artifact_ref(ref) for ref in values)
    if len(refs) > 32:
        raise PASError(ErrorCode.BUDGET_EXCEEDED, "report exceeds 32 artifact refs")
    if refs and root is None:
        raise PASError(ErrorCode.UNSUPPORTED_CAPABILITY, "worker artifact root is not configured")
    result: dict[str, str] = {}
    for ref in sorted(refs):
        try:
            path = safe_join(root, ref.removeprefix("artifact:"))
            if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                raise PASError(ErrorCode.STALE_CONTEXT, "worker artifact missing or exceeds 16 MiB")
            result[ref] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, PathSafetyError):
            raise PASError(ErrorCode.STALE_CONTEXT, "worker artifact is unavailable") from None
    return result


def checkpoint_input_hash(stage: str, data: dict[str, Any]) -> str:
    if stage in ("probed", "analyzed"):
        return data["base_hash"]
    if stage == "composed":
        return content_hash({"base_hash": data["base_hash"], "worker": data["worker"], "artifacts": data["artifacts"]})
    raise PASError(ErrorCode.CONFLICT, "checkpoint stage is not resumable")


def outcome_dict(outcome: ExecutorOutcome) -> dict[str, Any]:
    decision = outcome.decision.to_wire_dict()
    decision.pop("protocol_version", None)
    return {"decision": decision, "usage": outcome.usage,
            "model_turns": outcome.model_turns, "tool_calls": outcome.tool_calls,
            "tool_denied": outcome.tool_denied, "repair_used": outcome.repair_used,
            "tool_evidence_refs": list(outcome.tool_evidence_refs),
            "tool_evidence_bindings": list(outcome.tool_evidence_bindings)}


def restore_outcome(data: dict[str, Any], pack: ContextPack, *, require_reason: bool) -> ExecutorOutcome:
    decision = parse_decision_text(canonical_json(data["decision"]),
        evidence=evidence_universe(pack, data["tool_evidence_refs"]), max_proposals=8, require_reason=require_reason)
    return ExecutorOutcome(decision=decision, usage=data["usage"], model_turns=data["model_turns"],
        tool_calls=data["tool_calls"], tool_denied=data["tool_denied"], repair_used=data["repair_used"],
        tool_evidence_refs=tuple(data["tool_evidence_refs"]), tool_evidence_bindings=tuple(data["tool_evidence_bindings"]))


def stage_input_hash(*, pack: ContextPack, source_records: list[dict[str, Any]], instruction: str,
                     source_states: list[dict[str, Any]],
                     job: Any, event: Any, context: Any, request: Any, config: Any,
                     executor: Any, grants: list[Any], policy_config: Any) -> str:
    # Includes runtime conversation text only in the hash, never in checkpoint JSON.
    return content_hash({"pack": pack.to_dict(), "sources": source_records, "source_states": source_states, "instruction": instruction,
        "job": asdict(job) if job is not None else None,
        "event": asdict(event), "capabilities": sorted(context.capabilities),
        "tools": list(request.tool_allowlist), "budget": request.budget.to_dict(),
        "deadline": request.deadline, "tool_authority": context.tool_authority,
        "policy_version": request.policy_version, "config": asdict(config),
        "policy_config": asdict(policy_config) if policy_config is not None else None,
        "executor": {"type": f"{type(executor).__module__}.{type(executor).__qualname__}",
                     "identity": getattr(executor, "checkpoint_identity", None)},
        "grants": [asdict(grant) for grant in grants],
    })
