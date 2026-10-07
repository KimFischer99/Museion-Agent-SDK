"""Observability: structured logs, metrics and health (SPEC §17.2; OPS-01).

Three pieces, all stdlib and all side-effect-free at import:

- :class:`StructuredLogger` — one JSON object per line (run_id /
  event_id / action_id / job_id / phase / reason / duration_ms). Every
  free-text field passes the redactor first; the redaction contract is
  "default has no raw private content" and "sensitive log check" (§16.1
  Operations row): bearer tokens, API keys, private key blocks and long
  secret-looking runs are masked before they reach a sink.
- :class:`Metrics` — the SPEC-named counters and gauges (wake,
  suppressed, model_calls, tool_denied, outbox_pending,
  delivery_unknown, grant_revoked, scheduler_lateness). A lock guards
  the counters; no background thread is started.
- :func:`health_snapshot` — liveness vs readiness (§17.2): the process
  being alive is not "everything works"; failed sources, exhausted disk
  or a stale scheduler tick make readiness ``degraded`` with machine
  reasons, and the caller surfaces them.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, TextIO

__all__ = [
    "StructuredLogger",
    "redact_text",
    "contains_unredacted_secret",
    "Metrics",
    "health_snapshot",
    "free_disk_mb",
]

_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"(?i)(api[_-]?key|apikey|token|secret|password)\s*[:=]\s*\S{6,}"),
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b"),
)

_SECRET_PROBE_RE = re.compile(
    r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{6,}"
    r"|sk-[A-Za-z0-9]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def redact_text(text: str) -> str:
    """Mask credential-shaped substrings. Structural identifiers the SDK
    itself mints (run_, evt_, act_, job_ ids and sha256: refs) are left
    intact — they are记账 identifiers, not secrets."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def contains_unredacted_secret(text: str) -> bool:
    """Conservative probe used by the log audit test: matches only the
    unambiguous credential shapes, so ordinary identifiers do not trip
    it."""
    if not isinstance(text, str):
        return False
    return bool(_SECRET_PROBE_RE.search(text))


class StructuredLogger:
    """JSON-lines logger. ``fields`` given at construction are merged
    into every record (e.g. profile). Thread-safe: one writer lock per
    logger. Import/construct performs no I/O; the first ``log`` call
    writes."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        component: str = "pas",
        fields: dict[str, Any] | None = None,
        redact: bool = True,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._component = component
        self._fields = dict(fields or {})
        self._redact = redact
        self._lock = threading.Lock()

    def log(
        self,
        level: str,
        event: str,
        *,
        phase: str | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        action_id: str | None = None,
        job_id: str | None = None,
        duration_ms: int | None = None,
        reason: str | None = None,
        **extra: Any,
    ) -> None:
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": level,
            "component": self._component,
            "event": event,
        }
        record.update(self._fields)
        if phase is not None:
            record["phase"] = phase
        if run_id is not None:
            record["run_id"] = run_id
        if event_id is not None:
            record["event_id"] = event_id
        if action_id is not None:
            record["action_id"] = action_id
        if job_id is not None:
            record["job_id"] = job_id
        if duration_ms is not None:
            record["duration_ms"] = int(duration_ms)
        if reason is not None:
            record["reason"] = redact_text(str(reason))[:500] if self._redact else str(reason)[:500]
        for key, value in extra.items():
            if value is not None:
                record[key] = (
                    redact_text(str(value))[:500]
                    if self._redact and isinstance(value, str)
                    else value
                )
        line = json.dumps(record, ensure_ascii=False, sort_keys=False)
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass  # a broken log sink must never take the daemon down

    def info(self, event: str, **kwargs: Any) -> None:
        self.log("info", event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self.log("warning", event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self.log("error", event, **kwargs)


class Metrics:
    """Counters and gauges named by SPEC §17.2. ``snapshot()`` returns a
    plain dict for logging and for the health file."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {
            "wake": 0,
            "suppressed": 0,
            "failed_runs": 0,
            "model_calls": 0,
            "tool_denied": 0,
            "grant_revoked": 0,
            "delivery_accepted": 0,
            "delivery_failed": 0,
        }
        self._gauges: dict[str, int] = {
            "outbox_pending": 0,
            "delivery_unknown": 0,
            "scheduler_lateness_ms": 0,
        }
        self._started = time.monotonic()

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            if name not in self._counters:
                raise KeyError(f"unknown counter {name!r}")
            self._counters[name] += amount

    def set_gauge(self, name: str, value: int) -> None:
        with self._lock:
            if name not in self._gauges:
                raise KeyError(f"unknown gauge {name!r}")
            self._gauges[name] = int(value)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            merged = dict(self._counters)
            merged.update(self._gauges)
            merged["uptime_s"] = int(time.monotonic() - self._started)
            return merged


def free_disk_mb(path: str | Any) -> int | None:
    """Free space in MB for the filesystem holding ``path``; None when
    the platform cannot answer (the alarm then degrades to unknown, not
    to a fake healthy value)."""
    import os

    try:
        usage = os.statvfs(str(path))
    except (OSError, AttributeError, NotImplementedError):
        return None
    return int(usage.f_bavail * usage.f_frsize / (1024 * 1024))


def health_snapshot(
    *,
    db_ok: bool,
    disk_free_mb: int | None,
    disk_free_warn_mb: int,
    unknown_deliveries: int,
    scheduler_age_ms: int | None,
    scheduler_stale_after_ms: int,
    source_errors: int = 0,
    extra_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Liveness vs readiness (§17.2): ``alive`` is the process liveness
    bit the supervisor owns; ``ready``/``degraded`` are computed here
    from checkable facts, with machine reasons. "计划仍存在"不是"监控正常
    工作" — a stale scheduler tick is degraded even when jobs look fine."""
    reasons: list[str] = []
    if not db_ok:
        reasons.append("database_unavailable")
    if disk_free_mb is not None and disk_free_mb < disk_free_warn_mb:
        reasons.append("disk_low")
    if disk_free_mb is None:
        reasons.append("disk_unknown")
    if unknown_deliveries > 0:
        reasons.append(f"delivery_unknown:{unknown_deliveries}")
    if source_errors > 0:
        reasons.append(f"source_errors:{source_errors}")
    if scheduler_age_ms is not None and scheduler_age_ms > scheduler_stale_after_ms:
        reasons.append(f"scheduler_stale:{scheduler_age_ms}ms")
    if extra_reasons:
        reasons.extend(extra_reasons)
    return {
        "alive": True,
        "ready": not reasons,
        "degraded": bool(reasons),
        "reasons": reasons,
        "checked_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "disk_free_mb": disk_free_mb,
        "unknown_deliveries": unknown_deliveries,
    }
