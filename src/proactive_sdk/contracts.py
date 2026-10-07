"""Unified contracts for PAS (SPEC §4).

This module owns: the unified error vocabulary, canonical-JSON hashing,
RFC 3339 timestamp rules, cross-field semantic validators that the JSON
Schemas deliberately do not express, the single-profile boundary, and the
core dataclasses / Protocols that P1–P5 implement against.

Nothing here performs I/O or starts background work.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Protocol, runtime_checkable

PAS_PROTOCOL_VERSION = "1.0"

__all__ = [
    "PAS_PROTOCOL_VERSION",
    "ErrorCode",
    "PASError",
    "error_for_code",
    "canonical_json",
    "content_hash",
    "require_utc_timestamp",
    "validate_schedule",
    "validate_decision",
    "validate_context_pack",
    "validate_reminder",
    "REMINDER_KEYS",
    "OBLIGATIONS",
    "NOTIFY_SELF_CAPABILITY",
    "TOOL_AUTHORITY_PAS_BROKER",
    "TOOL_AUTHORITY_HOST",
    "TOOL_AUTHORITY_UNKNOWN",
    "TOOL_AUTHORITIES",
    "default_obligation",
    "JobSpec",
    "ProfileConfig",
    "RuntimeConfig",
    "assert_single_profile",
    "AgentExecutor",
    "Source",
    "ToolBroker",
    "DeliverySink",
    "ModelPort",
    "ModelToolCall",
    "ModelRequest",
    "ModelResponse",
    "ExecutorCapabilities",
    "RunBudget",
    "RunRequest",
    "ContextPack",
    "ContextSource",
    "RecentNotification",
    "SourceRequest",
    "SourceItem",
    "SourceBatch",
    "MemoryEntry",
    "ActionProposal",
    "Decision",
]


# --------------------------------------------------------------------------- #
# Unified errors (SPEC §4.4)
# --------------------------------------------------------------------------- #


class ErrorCode(str, Enum):
    INVALID_CONFIG = "invalid_config"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    DEPENDENCY_MISSING = "dependency_missing"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    STALE_CONTEXT = "stale_context"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CONFLICT = "conflict"
    EFFECT_UNKNOWN = "effect_unknown"
    INTERNAL_ERROR = "internal_error"


# Codes that must never be retried automatically against other accounts or
# credentials (SPEC §4.4: 授权失败、明确不可重试配额失败、参数冲突不得自动转其他
# 账户或其他凭据重试).
NOT_AUTOMATICALLY_RETRYABLE = frozenset(
    {
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.AUTH_REQUIRED,
        ErrorCode.APPROVAL_REQUIRED,
        ErrorCode.CONFLICT,
        ErrorCode.BUDGET_EXCEEDED,
    }
)


class PASError(Exception):
    """Unified error with safe fields only.

    ``safe_message`` must never contain tokens, mail bodies or unredacted
    HTTP bodies; ``to_dict`` is the only wire representation.
    """

    def __init__(
        self,
        code: ErrorCode,
        safe_message: str,
        *,
        retryable: bool = False,
        retry_after_s: int | None = None,
        scope: str = "request",
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(f"[{code.value}] {safe_message}")
        if len(safe_message) < 1 or len(safe_message) > 500:
            raise ValueError("safe_message must be 1..500 chars")
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.scope = scope
        self.correlation_id = correlation_id or uuid.uuid4().hex

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "protocol_version": PAS_PROTOCOL_VERSION,
            "code": self.code.value,
            "safe_message": self.safe_message,
            "retryable": self.retryable,
            "correlation_id": self.correlation_id,
        }
        if self.retry_after_s is not None:
            out["retry_after_s"] = self.retry_after_s
        if self.scope != "request":
            out["scope"] = self.scope
        return out


def error_for_code(code: ErrorCode, safe_message: str, **kw: Any) -> PASError:
    return PASError(code, safe_message, **kw)


# --------------------------------------------------------------------------- #
# Canonical JSON, hashing, timestamps
# --------------------------------------------------------------------------- #


def canonical_json(obj: Any) -> str:
    """Deterministic JSON text: sorted keys, compact separators, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj: Any) -> str:
    """SHA-256 hex digest of the canonical JSON of ``obj``."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)


def require_utc_timestamp(value: str) -> str:
    """Validate an external timestamp: RFC 3339 with Z or explicit offset.

    Returns the value unchanged on success; raises ``ValueError`` otherwise.
    Naive local times are rejected — the profile timezone never silently
    applies to wire timestamps (SPEC §5.1).
    """
    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        raise ValueError(f"not an RFC 3339 timestamp with timezone: {value!r}")
    from datetime import datetime

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(f"missing timezone offset: {value!r}")
    return value


# --------------------------------------------------------------------------- #
# Cross-field semantic validators (schemas/README.md table)
# --------------------------------------------------------------------------- #

_KIND_REQUIRED: dict[str, tuple[str, ...]] = {
    "interval": ("anchor", "every_seconds"),
    "daily": ("local_time", "timezone"),
    "weekly": ("weekdays", "local_time", "timezone"),
    "monthly": ("day_of_month", "local_time", "timezone"),
    "runonce": ("at",),
}


def validate_schedule(schedule: dict[str, Any]) -> list[str]:
    """Per-kind required-field rules for Schedule (SPEC §5.1)."""
    errors: list[str] = []
    kind = schedule.get("kind")
    if kind not in _KIND_REQUIRED:
        return [f"schedule.kind {kind!r} not in {sorted(_KIND_REQUIRED)}"]
    for field_name in _KIND_REQUIRED[kind]:
        value = schedule.get(field_name)
        if value is None or (isinstance(value, list) and not value):
            errors.append(f"schedule.kind={kind} requires {field_name!r}")
    if kind == "interval":
        anchor = schedule.get("anchor")
        if anchor is not None:
            try:
                require_utc_timestamp(anchor)
            except ValueError as exc:
                errors.append(f"schedule.anchor: {exc}")
    if kind == "runonce":
        at = schedule.get("at")
        if at is not None:
            try:
                require_utc_timestamp(at)
            except ValueError as exc:
                errors.append(f"schedule.at: {exc}")
    if kind in ("daily", "weekly", "monthly"):
        tz = schedule.get("timezone")
        if tz is not None:
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(tz)
            except Exception:
                errors.append(f"schedule.timezone {tz!r} is not a valid IANA zone")
        local_time = schedule.get("local_time")
        if local_time is not None and not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", local_time):
            errors.append(f"schedule.local_time {local_time!r} must be HH:MM")
        # DST fall-back repetition policy; persists per job revision (SPEC §5.1).
        # Spring-forward gaps are always skipped; this only chooses the fold.
        fold_policy = schedule.get("fold_policy", "earliest")
        if fold_policy not in ("earliest", "latest"):
            errors.append("schedule.fold_policy must be 'earliest' or 'latest'")
    if kind == "weekly":
        weekdays = schedule.get("weekdays") or []
        if any(not isinstance(w, int) or not 1 <= w <= 7 for w in weekdays):
            errors.append("schedule.weekdays must be ISO weekday numbers 1–7")
    return errors


_NOTIFY_SELF_REQUIRED = ("evidence_refs", "expires_at")


# --- direct reminders (SPEC §21.1 step 2) ---------------------------------- #
#
# A direct reminder is a *frozen user-authored message* plus a schedule.
# Nothing in it is model-derived: the body, the timezone, the owner
# channel and the notification obligation all come from the trusted
# ``jobs_upsert`` entry point, and ``validate_reminder`` is the single
# place that decides whether such a definition is well formed.

# The capability a profile grants before owner-addressed notifications may
# be produced without a per-message approval (§9.1). Shared vocabulary: the
# store's deterministic reminder path and the policy engine must agree.
NOTIFY_SELF_CAPABILITY = "notify.self"

#: Which authority actually governed a run's side effects (SPEC §22.1 item 5).
#: Shared vocabulary: the executor declares it, the store records it per run,
#: and the CLI/RPC report it. AGENTS.md is explicit that raw shell, network
#: and credentials can bypass the broker, so "we did not declare it" must be
#: representable and must never read as "PAS covered it".
TOOL_AUTHORITY_PAS_BROKER = "pas_broker"
TOOL_AUTHORITY_HOST = "host"
TOOL_AUTHORITY_UNKNOWN = "unknown"
TOOL_AUTHORITIES = (
    TOOL_AUTHORITY_PAS_BROKER,
    TOOL_AUTHORITY_HOST,
    TOOL_AUTHORITY_UNKNOWN,
)

OBLIGATIONS = frozenset({"due", "opportunistic"})
OBLIGATION_DUE = "due"
OBLIGATION_OPPORTUNISTIC = "opportunistic"

REMINDER_KEYS = frozenset(
    {
        "title",
        "body",
        "timezone",
        "destination",
        "topic",
        "refresh_sources",
        "fact_refs",
        "artifact_refs",
    }
)

_REMINDER_BODY_MAX = 4000
_REMINDER_TITLE_MAX = 200
_REMINDER_REFRESH_MAX = 16
_REMINDER_FACTS_MAX = 16
_REMINDER_ARTIFACTS_MAX = 32
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def default_obligation(mode: str) -> str:
    """Notification obligation implied by the job's mode when the trusted
    configuration does not state one.

    A direct reminder owes its message at the scheduled instant; an
    opportunistic heartbeat or an explicit task only reaches out when the
    analysis says so (SPEC §21.1 step 3).
    """
    return OBLIGATION_DUE if mode == "reminder" else OBLIGATION_OPPORTUNISTIC


def _check_iana_zone(value: Any, where: str, errors: list[str]) -> None:
    if value is None:
        errors.append(f"{where} is required")
        return
    if not isinstance(value, str) or not value:
        errors.append(f"{where} must be a non-empty IANA zone name")
        return
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
    except Exception:
        errors.append(f"{where} {value!r} is not a valid IANA zone")


def validate_reminder(reminder: Any, *, schedule: dict[str, Any] | None = None) -> list[str]:
    """Structural rules for the frozen ``reminder`` block.

    Unknown keys are rejected so a trusted configuration can never smuggle
    later semantics (a receiver, a priority, an escalation) into the
    reminder payload without a matching contract change.
    """
    errors: list[str] = []
    if not isinstance(reminder, dict):
        return ["reminder must be an object"]
    unknown = sorted(set(reminder) - REMINDER_KEYS)
    if unknown:
        errors.append(f"reminder has unknown keys {unknown}")

    body = reminder.get("body")
    if not isinstance(body, str) or not 1 <= len(body) <= _REMINDER_BODY_MAX:
        errors.append(f"reminder.body must be 1..{_REMINDER_BODY_MAX} chars")
    elif not body.strip():
        errors.append("reminder.body must not be blank")

    title = reminder.get("title")
    if title is not None and (
        not isinstance(title, str) or not 1 <= len(title) <= _REMINDER_TITLE_MAX
    ):
        errors.append(f"reminder.title must be 1..{_REMINDER_TITLE_MAX} chars")

    _check_iana_zone(reminder.get("timezone"), "reminder.timezone", errors)
    if (
        isinstance(schedule, dict)
        and isinstance(schedule.get("timezone"), str)
        and isinstance(reminder.get("timezone"), str)
        and schedule["timezone"] != reminder["timezone"]
    ):
        errors.append(
            "reminder.timezone must match schedule.timezone when the schedule declares one"
        )

    destination = reminder.get("destination")
    if destination is not None and (
        not isinstance(destination, str) or not 1 <= len(destination) <= 256
    ):
        errors.append("reminder.destination must be 1..256 chars")

    topic = reminder.get("topic")
    if topic is not None and (not isinstance(topic, str) or not 1 <= len(topic) <= 128):
        errors.append("reminder.topic must be 1..128 chars")

    refresh = reminder.get("refresh_sources")
    if refresh is not None:
        if not isinstance(refresh, (list, tuple)) or not 1 <= len(refresh) <= _REMINDER_REFRESH_MAX:
            errors.append(f"reminder.refresh_sources must list 1..{_REMINDER_REFRESH_MAX} ids")
        elif any(not isinstance(item, str) or not _SOURCE_ID_RE.fullmatch(item) for item in refresh):
            errors.append("reminder.refresh_sources entries must be source ids")
        elif len(set(refresh)) != len(refresh):
            errors.append("reminder.refresh_sources must not repeat an id")

    facts = reminder.get("fact_refs")
    if facts is not None:
        if (
            not isinstance(facts, (list, tuple))
            or not 1 <= len(facts) <= _REMINDER_FACTS_MAX
        ):
            errors.append(f"reminder.fact_refs must list 1..{_REMINDER_FACTS_MAX} ids")
        elif any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in facts):
            errors.append("reminder.fact_refs entries must be 1..256 char strings")
        elif len(set(facts)) != len(facts):
            errors.append("reminder.fact_refs must not repeat an id")

    artifacts = reminder.get("artifact_refs")
    if artifacts is not None:
        if (
            not isinstance(artifacts, (list, tuple))
            or not 1 <= len(artifacts) <= _REMINDER_ARTIFACTS_MAX
        ):
            errors.append(
                f"reminder.artifact_refs must list 1..{_REMINDER_ARTIFACTS_MAX} refs"
            )
        else:
            from .artifacts import ArtifactRefError, missing_artifact_refs, normalize_artifact_ref

            normalized: list[str] = []
            for item in artifacts:
                try:
                    normalized.append(normalize_artifact_ref(item))
                except ArtifactRefError as exc:
                    errors.append(f"reminder.artifact_refs: {exc.safe_message}")
            if not errors and isinstance(body, str):
                # An artifact the owner cannot open from the body is not a
                # reference; the frozen text must carry it (SPEC §21.1 step 8).
                missing = missing_artifact_refs(body, normalized)
                if missing:
                    errors.append(
                        "reminder.body must contain every artifact_refs entry;"
                        f" missing {sorted(missing)}"
                    )
    return errors


def validate_decision(decision: dict[str, Any]) -> list[str]:
    """Cross-field rules for Decision (SPEC §8.1)."""
    errors: list[str] = []
    kind = decision.get("decision")
    proposals = decision.get("proposals")
    if kind == "silent" and proposals:
        errors.append("decision=silent requires empty proposals")
    if kind == "propose":
        if not isinstance(proposals, list) or len(proposals) < 1:
            errors.append("decision=propose requires at least one proposal")
        else:
            for idx, proposal in enumerate(proposals):
                if proposal.get("kind") == "notify_self":
                    for field_name in _NOTIFY_SELF_REQUIRED:
                        if not proposal.get(field_name):
                            errors.append(
                                f"proposals[{idx}]: notify_self requires {field_name!r}"
                                " (SPEC §8.1)"
                            )
    return errors


# --------------------------------------------------------------------------- #
# Single-profile boundary (AGENTS.md; SPEC §3.1)
# --------------------------------------------------------------------------- #

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LOCALE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


@dataclass(frozen=True)
class ProfileConfig:
    """One profile: one state dir, one DB, one run identity (SPEC §3.1)."""

    profile_id: str
    state_dir: str
    timezone: str
    locale: str

    def __post_init__(self) -> None:
        if not _PROFILE_ID_RE.fullmatch(self.profile_id):
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"profile_id {self.profile_id!r} fails naming rule"
            )
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(self.timezone)
        except Exception:
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"timezone {self.timezone!r} is not a valid IANA zone"
            ) from None
        if not _LOCALE_RE.fullmatch(self.locale):
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"locale {self.locale!r} fails BCP-47-ish rule"
            )


@dataclass(frozen=True)
class RuntimeConfig:
    """Process-level config for exactly one profile (SPEC §14.3 defaults)."""

    profile: ProfileConfig
    max_concurrent_agent_runs: int = 1
    shutdown_grace_seconds: int = 20
    event_retention_days: int = 30

    def __post_init__(self) -> None:
        if self.max_concurrent_agent_runs < 1:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "max_concurrent_agent_runs must be >= 1"
            )
        if self.event_retention_days < 1:
            raise PASError(ErrorCode.INVALID_CONFIG, "event_retention_days must be >= 1")


def assert_single_profile(profiles: list[ProfileConfig]) -> ProfileConfig:
    """Control-plane entry guard: exactly one profile, never a fleet."""
    if len(profiles) != 1:
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            f"exactly one profile is supported in v0.1, got {len(profiles)}",
            scope="profile",
        )
    return profiles[0]


# --------------------------------------------------------------------------- #
# JobSpec (SPEC §4.1) — implemented for real in P1 (store + scheduler)
# --------------------------------------------------------------------------- #

_JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MISFIRE_POLICIES = frozenset({"coalesce_latest", "grace_once", "expire"})
_SCHEDULER_OWNERS = frozenset({"pas", "host"})
_JOB_MODES = frozenset({"heartbeat", "task", "reminder"})


@dataclass(frozen=True)
class JobSpec:
    """Typed JobSpec. Structural shape is ``schemas/v1/job_spec.json``;
    the checks here are the cross-field rules the schema deliberately does
    not express (mirrors ``contracts.validate_schedule``)."""

    job_id: str
    mode: str
    schedule: dict[str, Any]
    task: dict[str, Any]
    owner: str = "pas"
    revision: int = 1
    grant_refs: tuple[str, ...] = ()
    delivery_policy: dict[str, Any] = field(default_factory=dict)
    misfire_policy: str | None = None
    deadline: str | None = None
    enabled: bool = True
    reminder: dict[str, Any] | None = None
    obligation: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not _JOB_ID_RE.fullmatch(self.job_id):
            raise PASError(ErrorCode.INVALID_CONFIG, f"job_id {self.job_id!r} fails naming rule")
        if self.mode not in _JOB_MODES:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"mode {self.mode!r} must be heartbeat|task|reminder",
            )
        if self.owner not in _SCHEDULER_OWNERS:
            raise PASError(ErrorCode.INVALID_CONFIG, f"owner {self.owner!r} must be pas|host")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise PASError(ErrorCode.INVALID_CONFIG, "revision must be an integer >= 1")
        if not isinstance(self.schedule, dict):
            raise PASError(ErrorCode.INVALID_CONFIG, "schedule must be an object")
        problems = validate_schedule(self.schedule)
        if problems:
            raise PASError(ErrorCode.INVALID_CONFIG, "; ".join(problems))
        if not isinstance(self.task, dict):
            raise PASError(ErrorCode.INVALID_CONFIG, "task must be an object")
        instruction = self.task.get("instruction")
        if self.mode == "reminder":
            # A reminder carries no agent instruction at all: it must not be
            # able to reach the model even by accident (SPEC §21.1 step 2).
            if instruction is not None:
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    "mode=reminder must not carry task.instruction",
                )
            if self.reminder is None:
                raise PASError(ErrorCode.INVALID_CONFIG, "mode=reminder requires reminder")
            reminder_problems = validate_reminder(self.reminder, schedule=self.schedule)
            if reminder_problems:
                raise PASError(ErrorCode.INVALID_CONFIG, "; ".join(reminder_problems))
        else:
            if not isinstance(instruction, str) or not 1 <= len(instruction) <= 10000:
                raise PASError(ErrorCode.INVALID_CONFIG, "task.instruction must be 1..10000 chars")
            if self.reminder is not None:
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    "reminder is only valid with mode=reminder",
                )
            refresh_ids = self.task.get("refresh_source_ids")
            if refresh_ids is not None:
                if (
                    not isinstance(refresh_ids, (list, tuple))
                    or not 1 <= len(refresh_ids) <= _REMINDER_REFRESH_MAX
                ):
                    raise PASError(
                        ErrorCode.INVALID_CONFIG,
                        f"task.refresh_source_ids must list 1..{_REMINDER_REFRESH_MAX} ids",
                    )
                if any(
                    not isinstance(item, str) or not _SOURCE_ID_RE.fullmatch(item)
                    for item in refresh_ids
                ):
                    raise PASError(
                        ErrorCode.INVALID_CONFIG,
                        "task.refresh_source_ids entries must be source ids",
                    )
        if self.obligation is not None and self.obligation not in OBLIGATIONS:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"obligation {self.obligation!r} must be one of {sorted(OBLIGATIONS)}",
            )
        if self.misfire_policy is not None and self.misfire_policy not in _MISFIRE_POLICIES:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"misfire_policy {self.misfire_policy!r} not in {sorted(_MISFIRE_POLICIES)}",
            )
        if self.deadline is not None:
            try:
                require_utc_timestamp(self.deadline)
            except ValueError as exc:
                raise PASError(ErrorCode.INVALID_CONFIG, f"deadline: {exc}") from None
        if not isinstance(self.enabled, bool):
            raise PASError(ErrorCode.INVALID_CONFIG, "enabled must be boolean")
        if any(not isinstance(g, str) or not g for g in self.grant_refs):
            raise PASError(ErrorCode.INVALID_CONFIG, "grant_refs must be non-empty strings")
        if not isinstance(self.delivery_policy, dict):
            raise PASError(ErrorCode.INVALID_CONFIG, "delivery_policy must be an object")

    @property
    def effective_obligation(self) -> str:
        return self.obligation or default_obligation(self.mode)

    def to_dict(self) -> dict[str, Any]:
        """Canonical dict shape; the idempotency hash input for job upserts.

        Fields that did not exist in v0.1.0 are emitted only when set, so a
        replay of a pre-upgrade ``jobs_upsert`` still hashes identically
        (SPEC §21.2: 旧 v0.1.0 数据可迁移).
        """
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "revision": self.revision,
            "owner": self.owner,
            "mode": self.mode,
            "schedule": self.schedule,
            "task": self.task,
            "grant_refs": list(self.grant_refs),
            "delivery_policy": self.delivery_policy,
            "misfire_policy": self.misfire_policy,
            "deadline": self.deadline,
            "enabled": self.enabled,
        }
        if self.reminder is not None:
            out["reminder"] = self.reminder
        if self.obligation is not None:
            out["obligation"] = self.obligation
        return out


# --------------------------------------------------------------------------- #
# Core ports (SPEC §4.2, §8.2). Protocol stubs for P1–P5 implementations.
# --------------------------------------------------------------------------- #


@runtime_checkable
class ModelPort(Protocol):
    """Model access: messages + authorized tool schemas in, content/tool
    calls/usage out. Provider-specific parameters stay in an adapter
    namespace and never leak into the public protocol (SPEC §8.2)."""

    async def generate(self, request: "ModelRequest") -> "ModelResponse": ...


@runtime_checkable
class AgentExecutor(Protocol):
    async def capabilities(self) -> Any: ...
    async def start(self, request: Any) -> Any: ...
    async def events(self, handle: Any, after_seq: int = 0) -> AsyncIterator[Any]: ...
    async def status(self, handle: Any) -> Any: ...
    async def cancel(self, handle: Any) -> Any: ...
    async def close(self) -> None: ...


@runtime_checkable
class Source(Protocol):
    async def fetch_delta(self, request: Any) -> Any: ...


@runtime_checkable
class ToolBroker(Protocol):
    """Authorized tool calls only: the broker signs calls from current
    grants; model output can never be deserialized into one directly
    (SPEC §4.2)."""

    async def call(self, request: Any) -> Any: ...


@runtime_checkable
class DeliverySink(Protocol):
    async def send(self, request: Any) -> Any: ...
    async def reconcile(self, request: Any) -> Any: ...


# Lightweight message records for ModelPort (SPEC §4.2, §8.2). The message
# dicts follow one internal convention regardless of provider:
#   {"role": "system"|"user", "content": str}
#   {"role": "assistant", "content": str|None,
#    "tool_calls": [{"id": str, "name": str, "arguments": dict}, ...]}
#   {"role": "tool", "call_id": str, "content": str}
# Model adapters translate this convention to their provider's wire format;
# reasoning traces are never part of it (EXEC-01: output/reasoning 分离).


@dataclass(frozen=True)
class ModelToolCall:
    """One tool invocation requested by the model.

    This is *model output* — an attempt, never an authorization. Only the
    ToolBroker turns a validated attempt into an AuthorizedToolCall
    (SPEC §4.2); it must not be possible to deserialize model text into
    the authorized form."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not 1 <= len(self.call_id) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "tool call_id must be 1..128 chars")
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "tool name must be 1..128 chars")
        if not isinstance(self.arguments, dict):
            raise PASError(ErrorCode.INVALID_CONFIG, "tool arguments must be an object")


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict[str, Any], ...]
    tool_schemas: tuple[dict[str, Any], ...] = ()
    deadline: str | None = None
    adapter_namespace: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    """One model turn. ``content`` carries the (candidate) final text;
    ``reasoning`` is provider-internal and must never be parsed for
    actions or persisted (SPEC §8.2). Usage fields follow the Usage
    object: unmeasurable values stay ``None``, never zero."""

    content: str | None
    tool_calls: tuple[ModelToolCall, ...] = ()
    usage: dict[str, Any] | None = None
    reasoning: str | None = None


@dataclass(frozen=True)
class ExecutorCapabilities:
    """Honest self-declaration (SPEC §4.2: 声明不是证明). Conformance
    tests and deployment-side checks remain mandatory."""

    streaming: bool = False
    cancellation: bool = False
    resumption: bool = False
    external_tool_broker: bool = False
    read_only_enforcement: bool = False
    usage_reporting: bool = False

    def __post_init__(self) -> None:
        for name in (
            "streaming",
            "cancellation",
            "resumption",
            "external_tool_broker",
            "read_only_enforcement",
            "usage_reporting",
        ):
            if not isinstance(getattr(self, name), bool):
                raise PASError(ErrorCode.INVALID_CONFIG, f"{name} must be boolean")


# --------------------------------------------------------------------------- #
# Run envelope (SPEC §4.1 RunRequest; schemas/v1/run_request.json)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunBudget:
    """Per-run resource bounds (SPEC §8.2 suggested starting values)."""

    max_model_turns: int = 8
    max_tool_calls: int = 12
    wall_time_s: int = 120
    max_proposals: int = 8

    def __post_init__(self) -> None:
        bounds = (
            ("max_model_turns", 1, 64),
            ("max_tool_calls", 0, 256),
            ("wall_time_s", 1, 3600),
            ("max_proposals", 0, 64),
        )
        for name, low, high in bounds:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise PASError(
                    ErrorCode.INVALID_CONFIG, f"budget.{name} must be an integer in {low}..{high}"
                )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_model_turns": self.max_model_turns,
            "max_tool_calls": self.max_tool_calls,
            "wall_time_s": self.wall_time_s,
            "max_proposals": self.max_proposals,
        }


@dataclass(frozen=True)
class RunRequest:
    """Typed RunRequest; ``to_dict`` validates against
    ``schemas/v1/run_request.json`` in the contract tests."""

    run_id: str
    attempt: int
    fence: int
    context_ref: str
    budget: RunBudget
    deadline: str
    policy_version: str = "1"
    skill_refs: tuple[str, ...] = ()
    tool_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not 8 <= len(self.run_id) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "run_id must be 8..128 chars")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 1:
            raise PASError(ErrorCode.INVALID_CONFIG, "attempt must be an integer >= 1")
        if not isinstance(self.fence, int) or isinstance(self.fence, bool) or self.fence < 0:
            raise PASError(ErrorCode.INVALID_CONFIG, "fence must be an integer >= 0")
        if not isinstance(self.context_ref, str) or not 1 <= len(self.context_ref) <= 256:
            raise PASError(ErrorCode.INVALID_CONFIG, "context_ref must be 1..256 chars")
        if not isinstance(self.budget, RunBudget):
            raise PASError(ErrorCode.INVALID_CONFIG, "budget must be RunBudget")
        try:
            require_utc_timestamp(self.deadline)
        except ValueError as exc:
            raise PASError(ErrorCode.INVALID_CONFIG, f"deadline: {exc}") from None
        if not isinstance(self.policy_version, str) or not 1 <= len(self.policy_version) <= 64:
            raise PASError(ErrorCode.INVALID_CONFIG, "policy_version must be 1..64 chars")
        if any(not isinstance(s, str) or not 1 <= len(s) <= 128 for s in self.skill_refs):
            raise PASError(ErrorCode.INVALID_CONFIG, "skill_refs must be 1..128-char strings")
        if any(not isinstance(s, str) or not 1 <= len(s) <= 128 for s in self.tool_allowlist):
            raise PASError(ErrorCode.INVALID_CONFIG, "tool_allowlist must be 1..128-char strings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PAS_PROTOCOL_VERSION,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "fence": self.fence,
            "context_ref": self.context_ref,
            "skill_refs": list(self.skill_refs),
            "tool_allowlist": list(self.tool_allowlist),
            "budget": self.budget.to_dict(),
            "deadline": self.deadline,
            "policy_version": self.policy_version,
        }


# --------------------------------------------------------------------------- #
# ContextPack (SPEC §4.1, §7.1; schemas/v1/context_pack.json)
# --------------------------------------------------------------------------- #


_SENSITIVITIES = frozenset({"public", "private", "sensitive"})


@dataclass(frozen=True)
class ContextSource:
    """One immutable source snapshot reference inside a ContextPack."""

    source_id: str
    account_ref: str
    snapshot_ref: str
    observed_at: str
    fresh_until: str
    sensitivity: str = "private"

    def __post_init__(self) -> None:
        for name in ("source_id", "account_ref", "snapshot_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not 1 <= len(value) <= 256:
                raise PASError(ErrorCode.INVALID_CONFIG, f"source.{name} must be 1..256 chars")
        for name in ("observed_at", "fresh_until"):
            try:
                require_utc_timestamp(getattr(self, name))
            except ValueError as exc:
                raise PASError(ErrorCode.INVALID_CONFIG, f"source.{name}: {exc}") from None
        if self.sensitivity not in _SENSITIVITIES:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"source.sensitivity {self.sensitivity!r} must be public|private|sensitive",
            )


# How many already-sent notifications may travel into one ContextPack.
# Bounded on purpose: the summary exists so the model can compare topics,
# openings and fact deltas, not so an entire history can be replayed into
# the prompt (SPEC §21.1 step 6).
MAX_RECENT_NOTIFICATIONS = 20
NOTIFICATION_WINDOW_MS = 24 * 3600 * 1000
_RECENT_TITLE_MAX = 120


@dataclass(frozen=True)
class RecentNotification:
    """One *already sent* notification, reduced to what is safe to show.

    Redaction is structural, not best-effort: there is no body field at
    all, the destination collapses to its channel kind, the title is
    truncated, and ``fact_id`` travels only as a short digest so two
    messages about the same fact can be compared without handing the model
    a join key into the delivery ledger.
    """

    fact_digest: str
    channel_kind: str
    topic: str | None
    title: str
    sent_at_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.fact_digest, str) or not 1 <= len(self.fact_digest) <= 32:
            raise PASError(ErrorCode.INVALID_CONFIG, "fact_digest must be 1..32 chars")
        if self.channel_kind not in ("local_inbox", "webhook"):
            raise PASError(ErrorCode.INVALID_CONFIG, "channel_kind must be local_inbox|webhook")
        if self.topic is not None and (
            not isinstance(self.topic, str) or not 1 <= len(self.topic) <= 128
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "topic must be 1..128 chars or None")
        if not isinstance(self.title, str) or not 1 <= len(self.title) <= _RECENT_TITLE_MAX:
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"title must be 1..{_RECENT_TITLE_MAX} chars"
            )
        if not isinstance(self.sent_at_ms, int) or isinstance(self.sent_at_ms, bool):
            raise PASError(ErrorCode.INVALID_CONFIG, "sent_at_ms must be an integer")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "fact_digest": self.fact_digest,
            "channel_kind": self.channel_kind,
            "title": self.title,
            "sent_at_ms": self.sent_at_ms,
        }
        if self.topic is not None:
            out["topic"] = self.topic
        return out

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "RecentNotification":
        """Build from a store summary row, applying the redaction rules."""
        raw_fact = row.get("fact_id") or row.get("business_key") or ""
        digest = content_hash(str(raw_fact))[:16] if raw_fact else "unknown"
        title = (row.get("title") or "notification")[:_RECENT_TITLE_MAX]
        topic = row.get("topic")
        if isinstance(topic, str) and not 1 <= len(topic) <= 128:
            topic = topic[:128]
        kind = row.get("channel_kind") or "local_inbox"
        if kind not in ("local_inbox", "webhook"):
            kind = "local_inbox"
        return cls(
            fact_digest=digest,
            channel_kind=kind,
            topic=topic if isinstance(topic, str) and topic else None,
            title=title or "notification",
            sent_at_ms=int(row.get("sent_at_ms") or 0),
        )


@dataclass(frozen=True)
class ContextPack:
    """Immutable per-run context snapshot (SPEC §7.1).

    ``untrusted_content_policy`` is the constant ``data_only``: source and
    tool content is data, never instructions; the policy is enforced
    structurally (tool allowlist, evidence closure, kind allowlist), not
    by prompt text (AGENTS.md)."""

    task_goal_id: str
    task_scope: str
    locale: str
    timezone: str
    preferences_ref: str
    sources: tuple[ContextSource, ...] = ()
    pending_refs: tuple[str, ...] = ()
    sent_fact_refs: tuple[str, ...] = ()
    memory_refs: tuple[str, ...] = ()
    untrusted_content_policy: str = "data_only"
    recent_notifications: tuple["RecentNotification", ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.task_goal_id, str) or not 1 <= len(self.task_goal_id) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "task.goal_id must be 1..128 chars")
        if not isinstance(self.task_scope, str) or not 1 <= len(self.task_scope) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "task.scope must be 1..128 chars")
        if not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*", self.locale or ""):
            raise PASError(ErrorCode.INVALID_CONFIG, f"locale {self.locale!r} fails BCP-47-ish rule")
        if not isinstance(self.timezone, str) or not 1 <= len(self.timezone) <= 64:
            raise PASError(ErrorCode.INVALID_CONFIG, "timezone must be 1..64 chars")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(self.timezone)
        except Exception:
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"timezone {self.timezone!r} is not a valid IANA zone"
            ) from None
        if not isinstance(self.preferences_ref, str) or not 1 <= len(self.preferences_ref) <= 256:
            raise PASError(ErrorCode.INVALID_CONFIG, "preferences_ref must be 1..256 chars")
        if len(self.sources) > 64:
            raise PASError(ErrorCode.INVALID_CONFIG, "context pack allows at most 64 sources")
        for name in ("pending_refs", "sent_fact_refs", "memory_refs"):
            refs = getattr(self, name)
            if len(refs) > 256:
                raise PASError(ErrorCode.INVALID_CONFIG, f"{name} allows at most 256 refs")
            if any(not isinstance(r, str) or not 1 <= len(r) <= 256 for r in refs):
                raise PASError(ErrorCode.INVALID_CONFIG, f"{name} entries must be 1..256 chars")
        if self.untrusted_content_policy != "data_only":
            raise PASError(
                ErrorCode.INVALID_CONFIG, "untrusted_content_policy must be 'data_only'"
            )
        if len(self.recent_notifications) > MAX_RECENT_NOTIFICATIONS:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"recent_notifications allows at most {MAX_RECENT_NOTIFICATIONS} entries",
            )
        for entry in self.recent_notifications:
            if not isinstance(entry, RecentNotification):
                raise PASError(
                    ErrorCode.INVALID_CONFIG, "recent_notifications entries must be typed"
                )

    @property
    def evidence_refs(self) -> frozenset[str]:
        """The evidence universe proposals may cite: snapshot refs and
        memory refs. Tool-result evidence joins at executor level."""
        return frozenset(
            {source.snapshot_ref for source in self.sources} | set(self.memory_refs)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "task": {"goal_id": self.task_goal_id, "scope": self.task_scope},
            "locale": self.locale,
            "timezone": self.timezone,
            "preferences_ref": self.preferences_ref,
            "sources": [
                {
                    "source_id": s.source_id,
                    "account_ref": s.account_ref,
                    "snapshot_ref": s.snapshot_ref,
                    "observed_at": s.observed_at,
                    "fresh_until": s.fresh_until,
                    "sensitivity": s.sensitivity,
                }
                for s in self.sources
            ],
            "pending_refs": list(self.pending_refs),
            "sent_fact_refs": list(self.sent_fact_refs),
            "memory_refs": list(self.memory_refs),
            "recent_notifications": [entry.to_dict() for entry in self.recent_notifications],
            "untrusted_content_policy": self.untrusted_content_policy,
        }


def validate_context_pack(pack: dict[str, Any]) -> list[str]:
    """Cross-field checks for ContextPack documents beyond the JSON Schema
    (mirrors ``schemas/v1/context_pack.json`` bounds so typed and wire
    validation agree)."""
    errors: list[str] = []
    if pack.get("schema_version") != "1.0":
        errors.append("schema_version must be '1.0'")
    if pack.get("untrusted_content_policy") != "data_only":
        errors.append("untrusted_content_policy must be 'data_only'")
    task = pack.get("task")
    if not isinstance(task, dict) or not task.get("goal_id") or not task.get("scope"):
        errors.append("task requires goal_id and scope")
    sources = pack.get("sources", [])
    if len(sources) > 64:
        errors.append("sources exceeds 64 items")
    for name in ("pending_refs", "sent_fact_refs", "memory_refs"):
        if len(pack.get(name, [])) > 256:
            errors.append(f"{name} exceeds 256 items")
    recent = pack.get("recent_notifications", [])
    if len(recent) > MAX_RECENT_NOTIFICATIONS:
        errors.append(f"recent_notifications exceeds {MAX_RECENT_NOTIFICATIONS} items")
    for idx, entry in enumerate(recent):
        if not isinstance(entry, dict):
            errors.append(f"recent_notifications[{idx}] must be an object")
            continue
        if set(entry) - {"fact_digest", "channel_kind", "topic", "title", "sent_at_ms"}:
            errors.append(f"recent_notifications[{idx}] has unknown keys")
        if "body" in entry:
            errors.append(f"recent_notifications[{idx}] must never carry a body")
        if entry.get("channel_kind") not in ("local_inbox", "webhook"):
            errors.append(f"recent_notifications[{idx}].channel_kind invalid")
        title = entry.get("title")
        if not isinstance(title, str) or not 1 <= len(title) <= _RECENT_TITLE_MAX:
            errors.append(f"recent_notifications[{idx}].title out of range")
    return errors


# --------------------------------------------------------------------------- #
# Source port records (SPEC §4.2, §7.1)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceRequest:
    """A bounded, account-bound delta fetch (SPEC §4.2: 绑定具体账户、
    最小读取范围和 deadline). Paging cursors bind to one account and are
    never reused across accounts."""

    source_id: str
    account_ref: str
    deadline: str
    scope: dict[str, Any] = field(default_factory=dict)
    cursor_ref: str | None = None
    max_items: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not 1 <= len(self.source_id) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "source_id must be 1..128 chars")
        if not isinstance(self.account_ref, str) or not 1 <= len(self.account_ref) <= 256:
            raise PASError(ErrorCode.INVALID_CONFIG, "account_ref must be 1..256 chars")
        try:
            require_utc_timestamp(self.deadline)
        except ValueError as exc:
            raise PASError(ErrorCode.INVALID_CONFIG, f"deadline: {exc}") from None
        if not isinstance(self.scope, dict):
            raise PASError(ErrorCode.INVALID_CONFIG, "scope must be an object")
        if self.cursor_ref is not None and (
            not isinstance(self.cursor_ref, str) or not 1 <= len(self.cursor_ref) <= 256
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "cursor_ref must be 1..256 chars or None")
        if not isinstance(self.max_items, int) or isinstance(self.max_items, bool) or not (
            1 <= self.max_items <= 1000
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "max_items must be an integer in 1..1000")


@dataclass(frozen=True)
class SourceItem:
    """One fact observed in a source delta."""

    fact_id: str
    revision: str
    content: str
    observed_at: str
    sensitivity: str = "private"
    tombstone: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.fact_id, str) or not 1 <= len(self.fact_id) <= 256:
            raise PASError(ErrorCode.INVALID_CONFIG, "fact_id must be 1..256 chars")
        if not isinstance(self.revision, str) or not 1 <= len(self.revision) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "revision must be 1..128 chars")
        if not isinstance(self.content, str) or len(self.content) > 100_000:
            raise PASError(ErrorCode.INVALID_CONFIG, "content must be a string of at most 100k chars")
        try:
            require_utc_timestamp(self.observed_at)
        except ValueError as exc:
            raise PASError(ErrorCode.INVALID_CONFIG, f"observed_at: {exc}") from None
        if self.sensitivity not in _SENSITIVITIES:
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"sensitivity {self.sensitivity!r} must be public|private|sensitive"
            )
        if not isinstance(self.tombstone, bool):
            raise PASError(ErrorCode.INVALID_CONFIG, "tombstone must be boolean")


@dataclass(frozen=True)
class SourceBatch:
    """One delta result (SPEC §7.1: cursor、版本、tombstone、观察时间、
    分页)。``fresh_until`` is None when the source cannot promise
    freshness; the pack then carries the observed time and freshness
    enforcement is the caller's job."""

    source_id: str
    account_ref: str
    observed_at: str
    items: tuple[SourceItem, ...] = ()
    cursor_ref: str | None = None
    has_more: bool = False
    fresh_until: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not 1 <= len(self.source_id) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "source_id must be 1..128 chars")
        if not isinstance(self.account_ref, str) or not 1 <= len(self.account_ref) <= 256:
            raise PASError(ErrorCode.INVALID_CONFIG, "account_ref must be 1..256 chars")
        try:
            require_utc_timestamp(self.observed_at)
        except ValueError as exc:
            raise PASError(ErrorCode.INVALID_CONFIG, f"observed_at: {exc}") from None
        if len(self.items) > 1000:
            raise PASError(ErrorCode.INVALID_CONFIG, "batch allows at most 1000 items")
        if self.cursor_ref is not None and (
            not isinstance(self.cursor_ref, str) or not 1 <= len(self.cursor_ref) <= 256
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "cursor_ref must be 1..256 chars or None")
        if not isinstance(self.has_more, bool):
            raise PASError(ErrorCode.INVALID_CONFIG, "has_more must be boolean")
        if self.fresh_until is not None:
            try:
                require_utc_timestamp(self.fresh_until)
            except ValueError as exc:
                raise PASError(ErrorCode.INVALID_CONFIG, f"fresh_until: {exc}") from None


# --------------------------------------------------------------------------- #
# Memory (SPEC §7.3: structured preferences + short evidenced entries;
# no embedding/vector DB required)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MemoryEntry:
    """One short memory entry with its evidence trail.

    Inferred preferences never upgrade into tool authorization (SPEC
    §7.3); ``content`` is bounded display text, not a policy document."""

    memory_id: str
    content: str
    source: str
    evidence_refs: tuple[str, ...] = ()
    confidence: str | None = None
    last_confirmed_at: str | None = None
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.memory_id, str) or not 1 <= len(self.memory_id) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "memory_id must be 1..128 chars")
        if not isinstance(self.content, str) or not 1 <= len(self.content) <= 2000:
            raise PASError(ErrorCode.INVALID_CONFIG, "memory content must be 1..2000 chars")
        if not isinstance(self.source, str) or not 1 <= len(self.source) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "memory source must be 1..128 chars")
        if any(not isinstance(r, str) or not 1 <= len(r) <= 256 for r in self.evidence_refs):
            raise PASError(ErrorCode.INVALID_CONFIG, "evidence_refs must be 1..256-char strings")
        if self.confidence is not None and self.confidence not in (
            "inferred",
            "user_confirmed",
        ):
            raise PASError(
                ErrorCode.INVALID_CONFIG, "confidence must be 'inferred' or 'user_confirmed'"
            )
        for name in ("last_confirmed_at", "expires_at"):
            value = getattr(self, name)
            if value is not None:
                try:
                    require_utc_timestamp(value)
                except ValueError as exc:
                    raise PASError(ErrorCode.INVALID_CONFIG, f"{name}: {exc}") from None


# --------------------------------------------------------------------------- #
# Decision (SPEC §4.1, §8.1; schemas/v1/decision.json)
# --------------------------------------------------------------------------- #


_PROPOSAL_KINDS = frozenset(
    {"notify_self", "draft", "internal_record", "suggest_watch", "request_external_action"}
)


@dataclass(frozen=True)
class ActionProposal:
    """One proposed action. ``kind`` is from the closed enum; the model
    never names external receivers (SPEC §4.1) — destinations bind to
    owner channels in P4."""

    kind: str
    fact_id: str
    revision: str
    body: str | None = None
    arguments: dict[str, Any] | None = None
    evidence_refs: tuple[str, ...] = ()
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _PROPOSAL_KINDS:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"proposal kind {self.kind!r} not in {sorted(_PROPOSAL_KINDS)}",
            )
        if not isinstance(self.fact_id, str) or not 1 <= len(self.fact_id) <= 256:
            raise PASError(ErrorCode.INVALID_CONFIG, "fact_id must be 1..256 chars")
        if not isinstance(self.revision, str) or not 1 <= len(self.revision) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "revision must be 1..128 chars")
        if self.body is not None and (not isinstance(self.body, str) or len(self.body) > 20000):
            raise PASError(ErrorCode.INVALID_CONFIG, "body must be a string of at most 20000 chars")
        if self.arguments is not None and not isinstance(self.arguments, dict):
            raise PASError(ErrorCode.INVALID_CONFIG, "arguments must be an object")
        if len(self.evidence_refs) > 32:
            raise PASError(ErrorCode.INVALID_CONFIG, "evidence_refs allows at most 32 entries")
        if any(not isinstance(r, str) or not 1 <= len(r) <= 256 for r in self.evidence_refs):
            raise PASError(ErrorCode.INVALID_CONFIG, "evidence_refs must be 1..256-char strings")
        if self.expires_at is not None:
            try:
                require_utc_timestamp(self.expires_at)
            except ValueError as exc:
                raise PASError(ErrorCode.INVALID_CONFIG, f"expires_at: {exc}") from None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "fact_id": self.fact_id,
            "revision": self.revision,
            "evidence_refs": list(self.evidence_refs),
        }
        if self.body is not None:
            out["body"] = self.body
        if self.arguments is not None:
            out["arguments"] = self.arguments
        if self.expires_at is not None:
            out["expires_at"] = self.expires_at
        return out


@dataclass(frozen=True)
class Decision:
    """Typed Decision. ``to_wire_dict`` adds ``protocol_version`` — the
    model itself never emits one; the control plane stamps it."""

    decision: str
    summary: str
    proposals: tuple[ActionProposal, ...] = ()

    def __post_init__(self) -> None:
        if self.decision not in ("propose", "silent"):
            raise PASError(ErrorCode.INVALID_CONFIG, "decision must be 'propose' or 'silent'")
        if not isinstance(self.summary, str) or not 1 <= len(self.summary) <= 500:
            raise PASError(ErrorCode.INVALID_CONFIG, "summary must be 1..500 chars")
        if self.decision == "silent" and self.proposals:
            raise PASError(ErrorCode.INVALID_CONFIG, "silent requires empty proposals")
        if self.decision == "propose" and not self.proposals:
            raise PASError(ErrorCode.INVALID_CONFIG, "propose requires at least one proposal")

    def to_wire_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PAS_PROTOCOL_VERSION,
            "decision": self.decision,
            "summary": self.summary,
            "proposals": [proposal.to_dict() for proposal in self.proposals],
        }
