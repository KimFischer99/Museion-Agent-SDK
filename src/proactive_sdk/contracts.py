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
    "ProfileConfig",
    "RuntimeConfig",
    "assert_single_profile",
    "AgentExecutor",
    "Source",
    "ToolBroker",
    "DeliverySink",
    "ModelPort",
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
    if kind == "weekly":
        weekdays = schedule.get("weekdays") or []
        if any(not isinstance(w, int) or not 1 <= w <= 7 for w in weekdays):
            errors.append("schedule.weekdays must be ISO weekday numbers 1–7")
    return errors


_NOTIFY_SELF_REQUIRED = ("evidence_refs", "expires_at")


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


# Lightweight message records for ModelPort (structure only in P0; schema
# files arrive with the executor work in P3).


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict[str, Any], ...]
    tool_schemas: tuple[dict[str, Any], ...] = ()
    deadline: str | None = None
    adapter_namespace: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    content: str | None
    tool_calls: tuple[dict[str, Any], ...] = ()
    usage: dict[str, Any] | None = None
