"""Schedule math and admission orchestration (SPEC §5; P1 / SCHED-01).

Pure schedule computation plus the admission loop that scans the due
index and commits occurrences transactionally through :class:`Store`.

Time rules (SPEC §5.1/§5.2):

- Interval schedules are anchored in UTC: ``slot = anchor + floor((now -
  anchor) / every) * every``. The occurrence id derives from job_id,
  revision and logical slot, so jitter or late admission never changes
  identity and a retry can never create a second occurrence.
- Daily/weekly/monthly schedules fire at a wall time in an explicit IANA
  zone. Spring-forward gaps (nonexistent local times) are skipped, never
  shifted to a neighbour time. Fall-back repetitions collapse to the
  earliest instant unless the job revision pins ``fold_policy=latest``.
  Resolution converts each fold candidate to UTC and back and keeps only
  wall-time-preserving instants.
- Monthly schedules skip months without the requested day; they never
  silently substitute the month's last day.
- Misfire policies: ``coalesce_latest`` (heartbeat default) admits the
  latest missed slot once; ``grace_once`` (task/reminder default) admits
  the latest missed slot only within the grace window; ``expire`` admits
  only slots reached within the healthy-tick tolerance and expires
  everything else. A missed episode materializes at most one ledger row
  (the latest slot) carrying the episode size as its reason.
- ``mode="reminder"`` jobs take a completely different admission path:
  their occurrence is committed straight into the owner outbox with zero
  model calls and no run row (SPEC §21.1 step 2). The same misfire
  arithmetic decides *whether* the occurrence is still owed, but an
  occurrence that can no longer be delivered is recorded as a queryable
  miss instead of a silent expiry (SPEC §21.1 step 4).

All computation is in UTC epoch milliseconds on integer arithmetic; wall
and monotonic clocks are never mixed (see ``clock.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .clock import Clock
from .contracts import JobSpec, validate_schedule
from .store import Store

__all__ = [
    "Schedule",
    "parse_schedule",
    "interval_slot_ms",
    "resolve_local_wall",
    "latest_due_slot",
    "next_occurrence_after",
    "Scheduler",
    "AdmissionReport",
]

_MS_PER_DAY = 24 * 3600 * 1000
_WEEKDAY_RE = re.compile(r"^[1-7]$")
_MAX_EPISODE_COUNT = 10000
# Latest-slot lookback is bounded well below the episode counter: finding
# the newest missed slot never needs years of scanning.
_LOOKBACK_CAP_DAYS = 400


def _ts_to_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(f"timestamp needs an explicit offset: {value!r}")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


@dataclass(frozen=True)
class Schedule:
    """Parsed schedule. All fields normalized to the units the math uses."""

    kind: str
    anchor_ms: int | None = None
    every_seconds: int | None = None
    local_hh: int | None = None
    local_mm: int | None = None
    timezone: str | None = None
    weekdays: tuple[int, ...] = ()
    day_of_month: int | None = None
    at_ms: int | None = None
    fold_policy: str = "earliest"


def parse_schedule(schedule: dict[str, Any]) -> Schedule:
    """Validate and normalize a schedule dict (schema shape, SPEC §5.1)."""
    problems = validate_schedule(schedule)
    if problems:
        raise ValueError("; ".join(problems))
    kind = schedule["kind"]
    zone: ZoneInfo | None = None
    if schedule.get("timezone") is not None:
        zone = ZoneInfo(schedule["timezone"])
    local_hh = local_mm = None
    if schedule.get("local_time") is not None:
        hh, mm = schedule["local_time"].split(":")
        local_hh, local_mm = int(hh), int(mm)
    return Schedule(
        kind=kind,
        anchor_ms=_ts_to_ms(schedule["anchor"]) if kind == "interval" else None,
        every_seconds=schedule["every_seconds"] if kind == "interval" else None,
        local_hh=local_hh,
        local_mm=local_mm,
        timezone=schedule.get("timezone"),
        weekdays=tuple(schedule.get("weekdays", ())) if kind == "weekly" else (),
        day_of_month=schedule.get("day_of_month") if kind == "monthly" else None,
        at_ms=_ts_to_ms(schedule["at"]) if kind == "runonce" else None,
        fold_policy=schedule.get("fold_policy", "earliest"),
    )


def interval_slot_ms(anchor_ms: int, every_seconds: int, now_ms: int) -> int:
    """Anchored interval slot containing ``now_ms`` (SPEC §5.2).

    Valid for ``now_ms`` before the anchor as well: the result is then a
    virtual pre-anchor slot, which the scheduler only ever uses as a
    fresh-job cursor, never as an occurrence.
    """
    if type(anchor_ms) is not int or type(every_seconds) is not int or type(now_ms) is not int:
        raise TypeError("interval math requires integer milliseconds")
    if every_seconds <= 0:
        raise ValueError("every_seconds must be positive")
    every_ms = every_seconds * 1000
    return anchor_ms + ((now_ms - anchor_ms) // every_ms) * every_ms


def resolve_local_wall(
    day: date, hh: int, mm: int, zone: ZoneInfo, fold_policy: str = "earliest"
) -> list[int]:
    """Resolve one local wall time to candidate UTC epoch-ms instants.

    Returns 0 candidates for spring-forward gaps, 1 for normal times and
    (subject to ``fold_policy``) up to 2 for fall-back repetitions. Only
    instants whose UTC round-trip reproduces the wall time are kept, so a
    nonexistent local time can never masquerade as a valid one.
    """
    if fold_policy not in ("earliest", "latest"):
        raise ValueError(f"unknown fold_policy {fold_policy!r}")
    naive = datetime(day.year, day.month, day.day, hh, mm)
    instants: set[int] = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        back = utc.astimezone(zone)
        if back.replace(tzinfo=None) == naive:
            instants.add(int(utc.timestamp() * 1000))
    ordered = sorted(instants)
    if not ordered:
        return []
    if fold_policy == "latest":
        return [ordered[-1]]
    return ordered[:1]


def _calendar_slots_on(
    sched: Schedule, day: date, zone: ZoneInfo
) -> list[int]:
    if sched.kind == "weekly" and day.isoweekday() not in sched.weekdays:
        return []
    if sched.kind == "monthly":
        # Months without that day simply never reach the resolve step:
        # Feb 30 is not a date, so it is skipped, not clamped (SPEC §5.1).
        if day.day != sched.day_of_month:
            return []
    assert sched.local_hh is not None and sched.local_mm is not None
    return resolve_local_wall(day, sched.local_hh, sched.local_mm, zone, sched.fold_policy)


def _calendar_zone(sched: Schedule) -> ZoneInfo:
    return ZoneInfo(sched.timezone or "UTC")


def latest_due_slot(sched: Schedule, now_ms: int, cursor_ms: int) -> int | None:
    """Latest occurrence slot in ``(cursor_ms, now_ms]``, or None.

    ``cursor_ms`` is the latest materialized slot (admitted or expired) or
    the fresh-job cursor; slots at or before it are already accounted for.
    """
    if type(now_ms) is not int or type(cursor_ms) is not int:
        raise TypeError("slot math requires integer milliseconds")
    if sched.kind == "interval":
        assert sched.anchor_ms is not None and sched.every_seconds is not None
        slot = interval_slot_ms(sched.anchor_ms, sched.every_seconds, now_ms)
        return slot if slot > cursor_ms else None
    if sched.kind == "runonce":
        assert sched.at_ms is not None
        return sched.at_ms if cursor_ms < sched.at_ms <= now_ms else None
    zone = _calendar_zone(sched)
    start_day = datetime.fromtimestamp(cursor_ms / 1000, timezone.utc).astimezone(zone).date()
    end_day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).astimezone(zone).date()
    # Bound the lookback so pathological cursors cannot stall admission.
    end_day_dt = datetime.fromtimestamp(now_ms / 1000, timezone.utc).astimezone(zone)
    if start_day < end_day_dt.date() - timedelta(days=_LOOKBACK_CAP_DAYS):
        start_day = end_day_dt.date() - timedelta(days=_LOOKBACK_CAP_DAYS)
    latest: int | None = None
    day = start_day
    step = timedelta(days=1)
    while day <= end_day:
        for slot in _calendar_slots_on(sched, day, zone):
            if cursor_ms < slot <= now_ms and (latest is None or slot > latest):
                latest = slot
        day += step
    return latest


def next_occurrence_after(
    sched: Schedule, after_ms: int, *, horizon_days: int = 400
) -> int | None:
    """First occurrence strictly after ``after_ms`` (its next-due value).

    Returns None when the schedule has no future occurrence (a completed
    runonce); callers then park the job by storing a NULL ``next_due_ms``.
    """
    if sched.kind == "interval":
        assert sched.anchor_ms is not None and sched.every_seconds is not None
        if after_ms < sched.anchor_ms:
            return sched.anchor_ms
        slot = interval_slot_ms(sched.anchor_ms, sched.every_seconds, after_ms)
        return slot + sched.every_seconds * 1000
    if sched.kind == "runonce":
        assert sched.at_ms is not None
        return sched.at_ms if sched.at_ms > after_ms else None
    zone = _calendar_zone(sched)
    day = datetime.fromtimestamp(after_ms / 1000, timezone.utc).astimezone(zone).date()
    end_day = day + timedelta(days=horizon_days)
    while day <= end_day:
        for slot in _calendar_slots_on(sched, day, zone):
            if slot > after_ms:
                return slot
        day += timedelta(days=1)
    return None


def _fresh_cursor(sched: Schedule, created_at_ms: int) -> int:
    """Cursor for a job that has never materialized an occurrence.

    A new job owes nothing before its creation: interval jobs start at the
    anchored slot containing creation (first full slot afterwards fires),
    calendar jobs at creation, and a runonce created after its target time
    is born as a missed occurrence and goes through its misfire policy.
    """
    if sched.kind == "interval":
        assert sched.anchor_ms is not None and sched.every_seconds is not None
        return interval_slot_ms(sched.anchor_ms, sched.every_seconds, created_at_ms)
    if sched.kind == "runonce":
        assert sched.at_ms is not None
        # A runonce created *after* its target instant is born as a missed
        # occurrence and must still go through its misfire policy, so the
        # cursor sits strictly before ``at``. (v0.1.1 fixes what was a
        # silent park: the slot was invisible to ``latest_due_slot`` and
        # the miss left no queryable reason — SPEC §21.1 step 4.)
        if created_at_ms >= sched.at_ms:
            return sched.at_ms - 1
        return created_at_ms
    return created_at_ms


def _episode_size(sched: Schedule, cursor_ms: int, latest_ms: int, cap: int = _MAX_EPISODE_COUNT) -> int:
    """Number of occurrence slots in ``(cursor_ms, latest_ms]``, capped."""
    if sched.kind == "interval":
        assert sched.anchor_ms is not None and sched.every_seconds is not None
        every_ms = sched.every_seconds * 1000
        return min((latest_ms - cursor_ms) // every_ms, cap)
    if sched.kind == "runonce":
        assert sched.at_ms is not None
        return 1 if cursor_ms < sched.at_ms <= latest_ms else 0
    zone = _calendar_zone(sched)
    day = datetime.fromtimestamp(cursor_ms / 1000, timezone.utc).astimezone(zone).date()
    end_day = datetime.fromtimestamp(latest_ms / 1000, timezone.utc).astimezone(zone).date()
    count = 0
    while day <= end_day and count <= cap:
        for slot in _calendar_slots_on(sched, day, zone):
            if cursor_ms < slot <= latest_ms:
                count += 1
        day += timedelta(days=1)
    return min(count, cap)


@dataclass
class AdmissionReport:
    """What one ``admit_due`` pass decided, per SPEC §5.2 accounting."""

    evaluated: int = 0
    admitted: list[str] = field(default_factory=list)
    already_admitted: list[str] = field(default_factory=list)
    expired: list[tuple[str, str]] = field(default_factory=list)  # (occurrence_id, reason)
    advanced_only: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (job_id, outcome)

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "admitted": list(self.admitted),
            "already_admitted": list(self.already_admitted),
            "expired": [list(item) for item in self.expired],
            "advanced_only": list(self.advanced_only),
            "skipped": [list(item) for item in self.skipped],
        }


class Scheduler:
    """Admission orchestration on top of a Store.

    The scheduler owns schedule math and misfire decisions; the store owns
    transactions and identity. Every store call re-verifies revision and
    enabled inside its transaction, so an edit or pause racing with
    admission can only ever win or lose atomically (SPEC §5.2, §13.2).
    """

    def __init__(
        self,
        store: Store,
        clock: Clock,
        *,
        grace_seconds: int = 900,
        scan_tolerance_seconds: int = 60,
        occurrence_horizon_days: int = 400,
        default_max_per_day: int | None = None,
    ) -> None:
        if grace_seconds < 0 or scan_tolerance_seconds < 0:
            raise ValueError("grace and tolerance must be non-negative")
        if default_max_per_day is not None and (
            not isinstance(default_max_per_day, int)
            or isinstance(default_max_per_day, bool)
            or default_max_per_day < 1
        ):
            raise ValueError("default_max_per_day must be a positive integer or None")
        self.store = store
        self.clock = clock
        self.grace_ms = grace_seconds * 1000
        self.tolerance_ms = scan_tolerance_seconds * 1000
        self.horizon_days = occurrence_horizon_days
        self.default_max_per_day = default_max_per_day

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register_job(self, spec: JobSpec, *, idempotency_key: str) -> Any:
        """Store-level upsert plus a computed initial due time, so a new
        job's first tick lands on its first real occurrence instead of
        rescanning until then."""
        now = self.clock.wall_now_ms()
        sched = parse_schedule(spec.schedule)
        if spec.enabled:
            initial = self._initial_next_due(sched, now)
        else:
            initial = now
        return self.store.upsert_job(
            spec, idempotency_key=idempotency_key, now_ms=now, initial_next_due_ms=initial
        )

    def _initial_next_due(self, sched: Schedule, created_at_ms: int) -> int:
        cursor = _fresh_cursor(sched, created_at_ms)
        nxt = next_occurrence_after(sched, cursor, horizon_days=self.horizon_days)
        # For a runonce created before/at its time the first due is `at`
        # itself; for everything else it is the first slot after creation.
        if sched.kind == "runonce":
            assert sched.at_ms is not None
            return max(sched.at_ms, created_at_ms)
        return nxt if nxt is not None else created_at_ms

    # ------------------------------------------------------------------ #
    # Admission
    # ------------------------------------------------------------------ #

    def admit_due(self, *, now_ms: int | None = None) -> AdmissionReport:
        """Evaluate every due job once, one short transaction per job."""
        now = self.clock.wall_now_ms() if now_ms is None else now_ms
        report = AdmissionReport()
        for job_id in self.store.due_job_ids(now):
            job = self.store.get_job(job_id)
            if job is None:  # deleted between the scan and the read
                continue
            self._admit_job(job, now, report)
        return report

    def _admit_job(self, job: Any, now: int, report: AdmissionReport) -> None:
        sched = parse_schedule(job.schedule)
        report.evaluated += 1
        known = self.store.latest_known_occurrence(job.job_id)
        cursor = _fresh_cursor(sched, job.created_at_ms) if known is None else known[0]
        latest = latest_due_slot(sched, now, cursor)

        if latest is None:
            nxt = next_occurrence_after(sched, max(cursor, now), horizon_days=self.horizon_days)
            if nxt != job.next_due_ms:
                outcome = self.store.update_next_due(
                    job.job_id,
                    expected_revision=job.revision,
                    next_due_ms=nxt,
                    now_ms=now,
                )
                self._record_outcome(job.job_id, outcome, report, occurrence=None)
            else:
                report.evaluated += 0  # nothing changed; counted once above
            return

        decision = self._misfire_decision(job, sched, latest, now, cursor)
        next_due = next_occurrence_after(sched, latest, horizon_days=self.horizon_days)
        if job.mode == "reminder":
            self._admit_reminder(job, sched, latest, next_due, now, decision, report)
            return
        if decision[0] == "admit":
            outcome = self.store.admit_job_occurrence(
                job.job_id,
                expected_revision=job.revision,
                kind=sched.kind,
                slot_ms=latest,
                next_due_ms=next_due,
                now_ms=now,
            )
            self._record_outcome(
                job.job_id,
                outcome,
                report,
                occurrence=self._occurrence_label(job, latest),
            )
        else:
            _, reason = decision
            outcome = self.store.record_missed_occurrence(
                job.job_id,
                expected_revision=job.revision,
                kind=sched.kind,
                slot_ms=latest,
                reason=reason,
                next_due_ms=next_due,
                now_ms=now,
            )
            self._record_outcome(
                job.job_id,
                outcome,
                report,
                occurrence=self._occurrence_label(job, latest),
                expired_reason=reason,
            )

    def _admit_reminder(
        self,
        job: Any,
        sched: Schedule,
        latest: int,
        next_due: int | None,
        now: int,
        decision: tuple[str, str | None],
        report: AdmissionReport,
    ) -> None:
        """Deterministic reminder admission: no run, no model (SPEC §21.1).

        The store owns the whole transaction (occurrence + gates + action +
        outbox), so a restart, a replayed tick or a PAS/host race can only
        ever produce one message for one occurrence.
        """
        occurrence = self._occurrence_label(job, latest)
        if decision[0] != "admit":
            _, reason = decision
            self.store.record_missed_reminder(
                job.job_id,
                expected_revision=job.revision,
                kind=sched.kind,
                slot_ms=latest,
                reason=reason,
                next_due_ms=next_due,
                now_ms=now,
            )
            report.expired.append((occurrence, reason))
            return
        late_reason = None
        if now - latest > self.tolerance_ms:
            # Only the observable fact is recorded: this occurrence was
            # admitted after its planned instant as a graceful catch-up.
            late_reason = "catch_up_within_grace"
        result = self.store.admit_reminder_occurrence(
            job.job_id,
            expected_revision=job.revision,
            kind=sched.kind,
            slot_ms=latest,
            next_due_ms=next_due,
            now_ms=now,
            default_max_per_day=self.default_max_per_day,
            late_reason=late_reason,
        )
        if result.outcome in ("queued", "deferred"):
            report.admitted.append(occurrence)
        elif result.outcome == "already":
            report.already_admitted.append(occurrence)
        elif result.outcome == "skipped_stopped":
            report.skipped.append((job.job_id, "skipped_stopped"))
        elif result.outcome == "skipped_disabled":
            report.skipped.append((job.job_id, "skipped_disabled"))
        elif result.outcome == "skipped_revision":
            report.skipped.append((job.job_id, "skipped_revision"))
        elif result.outcome == "skipped_missing":
            report.skipped.append((job.job_id, "skipped_missing"))
        else:
            # suppressed / missed: the occurrence is accounted for and the
            # reason stays queryable on the job's activity projection.
            report.expired.append((occurrence, result.reason or result.outcome))

    def _misfire_decision(
        self, job: Any, sched: Schedule, latest: int, now: int, cursor: int
    ) -> tuple[str, str | None]:
        """Return ("admit", None) or ("expire", reason) for the latest slot."""
        if job.deadline_ms is not None and now > job.deadline_ms:
            return ("expire", "past_job_deadline")
        if job.misfire_policy == "coalesce_latest":
            return ("admit", None)
        if job.misfire_policy == "grace_once":
            if now - latest <= self.grace_ms:
                return ("admit", None)
            return (
                "expire",
                f"missed_beyond_grace episode_slots={_episode_size(sched, cursor, latest)}",
            )
        # expire: only slots reached by a healthy tick are admitted.
        if now - latest <= self.tolerance_ms:
            return ("admit", None)
        return (
            "expire",
            f"missed_beyond_catchup_tolerance episode_slots={_episode_size(sched, cursor, latest)}",
        )

    @staticmethod
    def _occurrence_label(job: Any, slot_ms: int) -> str:
        return f"{job.job_id}:{job.revision}:{slot_ms}"

    @staticmethod
    def _record_outcome(
        job_id: str,
        outcome: str,
        report: AdmissionReport,
        *,
        occurrence: str | None,
        expired_reason: str | None = None,
    ) -> None:
        if outcome == "admitted" and occurrence:
            report.admitted.append(occurrence)
        elif outcome == "already_admitted" and occurrence:
            report.already_admitted.append(occurrence)
        elif outcome == "recorded" and occurrence and expired_reason:
            report.expired.append((occurrence, expired_reason))
        elif outcome == "advanced" and occurrence is None:
            report.advanced_only.append(job_id)
        elif outcome.startswith("skipped_"):
            report.skipped.append((job_id, outcome))
