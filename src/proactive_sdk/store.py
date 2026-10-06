"""Durable store for exactly one PAS profile (SPEC §13; P1 / STORE-01).

Owns: SQLite connection setup (pragmas), schema migrations with recorded
checksums, the profile identity binding, the typed jobs API (idempotency
keys + optimistic revisions), event admission with occurrence dedupe, and
the run ledger with claim leases and monotonic fences.

Transaction rules (SPEC §13.2): short ``BEGIN IMMEDIATE`` transactions
only, no network, no awaits. Admission verifies job revision and enabled
*inside* the same transaction that inserts the occurrence and event and
advances ``next_due_ms``. Business transactions commit before callers are
told a write succeeded; a failed commit is reported as failed.

Time: every persisted timestamp is UTC epoch milliseconds (``_ms``).
Leases use the injected Clock's wall time so they survive restarts; the
monotonic clock is reserved for in-process waits and is never persisted.

One Store per profile per process; the connection is not thread-shareable.
Concurrency is exercised through independent connections (see tests).
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .clock import Clock, SystemClock
from .contracts import (
    ErrorCode,
    JobSpec,
    PASError,
    canonical_json,
    content_hash,
)

__all__ = [
    "Store",
    "JobRecord",
    "RunLease",
    "MIGRATION_COUNT",
]

_EVENT_PAYLOAD_MAX_BYTES = 65536
_EVENT_TTL_MS = 7 * 24 * 3600 * 1000
_RUN_DEFAULT_DEADLINE_MS = 5 * 60 * 1000
_POLICY_VERSION = 1

_MIGRATION_NAME_RE = re.compile(r"m(\d+)_[a-z0-9_]+\.sql")


def _load_migrations() -> tuple[tuple[int, str], ...]:
    files = importlib.resources.files("proactive_sdk.migrations")
    found: list[tuple[int, str]] = []
    for entry in files.iterdir():
        match = _MIGRATION_NAME_RE.fullmatch(entry.name)
        if match:
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    found.sort()
    for expected, (version, _) in enumerate(found, start=1):
        if version != expected:
            raise RuntimeError(f"migration versions must be contiguous 1..n, got {found}")
    return tuple(found)


MIGRATIONS = _load_migrations()
MIGRATION_COUNT = len(MIGRATIONS)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_sql_statements(sql: str) -> list[str]:
    """Split a migration script into complete statements.

    ``executescript`` would implicitly commit the open transaction, which
    would break migration atomicity, so statements are executed one by one
    inside the caller's transaction instead. ``complete_statement`` keeps
    semicolons inside string literals and comment blocks intact.
    """
    statements: list[str] = []
    buffer = ""
    for ch in sql:
        buffer += ch
        if ch == ";" and sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    tail = buffer.strip()
    if tail:
        statements.append(tail)
    usable: list[str] = []
    for statement in statements:
        if any(
            line.strip() and not line.strip().startswith("--")
            for line in statement.splitlines()
        ):
            usable.append(statement)
    return usable


@dataclass(frozen=True)
class JobRecord:
    """Stored job state (schema §13.1 ``jobs`` + P1 columns)."""

    job_id: str
    revision: int
    owner: str
    mode: str
    schedule: dict[str, Any]
    task: dict[str, Any]
    grant_refs: tuple[str, ...]
    delivery_policy: dict[str, Any]
    misfire_policy: str
    deadline_ms: int | None
    enabled: bool
    next_due_ms: int | None
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class RunLease:
    """Claim token for one run. ``fence`` increases monotonically per run
    on every claim; completion, failure and (later) tool effects must
    present the current fence or are rejected (SPEC §10.3)."""

    run_id: str
    event_id: str
    fence: int
    lease_until_ms: int


class Store:
    """Persistence for one profile. See module docstring for the rules.

    ``owner_destination`` is the bound personal notification channel
    reference; it is part of the database identity so a store file can
    never be silently reused by a different profile/destination pair.
    """

    def __init__(
        self,
        path: str,
        *,
        profile: str,
        owner_destination: str,
        clock: Clock | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", profile or ""):
            raise PASError(ErrorCode.INVALID_CONFIG, f"profile id {profile!r} fails naming rule")
        if not owner_destination:
            raise PASError(ErrorCode.INVALID_CONFIG, "owner_destination is mandatory")
        self.profile = profile
        self.owner_destination = owner_destination
        self.clock = clock if clock is not None else SystemClock()
        try:
            self.db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        except sqlite3.Error as exc:
            raise PASError(ErrorCode.INTERNAL_ERROR, f"cannot open database: {exc}") from exc
        self.db.row_factory = sqlite3.Row
        # SPEC §13.3: DELETE journal on the reference SQLite line; WAL is a
        # deliberate later upgrade gated on the linked-library check.
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        try:
            self._migrate()
            self._bind_identity()
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ #
    # Transactions and migrations
    # ------------------------------------------------------------------ #

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _migrate(self) -> None:
        with self.transaction():
            has_table = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            current = 0
            if has_table:
                row = self.db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                current = row[0] or 0
            if current > MIGRATION_COUNT:
                raise PASError(
                    ErrorCode.CONFLICT,
                    f"database schema v{current} is newer than this binary (v{MIGRATION_COUNT})"
                    "; refusing to open with an old binary",
                    scope="store",
                )
            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                self._apply_migration(version, sql)

    def _apply_migration(self, version: int, sql: str) -> None:
        """Apply one migration inside the caller's transaction.

        Separated from ``_migrate`` so fault-injection tests can drive a
        deliberately broken migration through the identical code path.
        """
        checksum = _sha256_text(sql)
        for statement in _split_sql_statements(sql):
            self.db.execute(statement)
        self.db.execute(
            "INSERT INTO schema_migrations(version, checksum, applied_at_ms) VALUES (?,?,?)",
            (version, checksum, self.clock.wall_now_ms()),
        )

    def _bind_identity(self) -> None:
        with self.transaction():
            identity = canonical_json({"profile": self.profile, "owner": self.owner_destination})
            row = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            if row is not None and row[0] != identity:
                raise PASError(
                    ErrorCode.CONFLICT,
                    "refusing to reuse another profile's database",
                    scope="store",
                )
            self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('identity', ?)", (identity,))

    # ------------------------------------------------------------------ #
    # Jobs API (SPEC §14.1 jobs subset, store level)
    # ------------------------------------------------------------------ #

    def upsert_job(
        self,
        spec: JobSpec,
        *,
        idempotency_key: str,
        now_ms: int | None = None,
        initial_next_due_ms: int | None = None,
    ) -> JobRecord:
        """Create or revise a job.

        - New job: stored at ``spec.revision`` (use 1 for a fresh job).
        - Existing job: ``spec.revision`` must equal current revision + 1
          (optimistic revision, SPEC §5.2); otherwise conflict.
        - ``idempotency_key``: replay with the same key and identical
          canonical content returns the stored record untouched; same key
          with different content is a conflict (SPEC §4.1).
        - On a real revision bump ``next_due_ms`` resets to *now* so the
          admission loop re-evaluates under the new revision immediately.
        """
        now = self.clock.wall_now_ms() if now_ms is None else now_ms
        if not idempotency_key or len(idempotency_key) > 256:
            raise PASError(ErrorCode.INVALID_CONFIG, "idempotency_key must be 1..256 chars")
        request_hash = content_hash(spec.to_dict())
        resolved_misfire = spec.misfire_policy or (
            "coalesce_latest" if spec.mode == "heartbeat" else "grace_once"
        )
        with self.transaction():
            prior = self.db.execute(
                "SELECT request_hash, job_id FROM jobs_idempotency WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise PASError(
                        ErrorCode.CONFLICT,
                        "idempotency_key reused with different content",
                        scope="jobs",
                    )
                record = self._read_job(prior["job_id"])
                if record is None:
                    raise PASError(
                        ErrorCode.INTERNAL_ERROR, "idempotency row references a missing job"
                    )
                return record
            row = self.db.execute(
                "SELECT revision FROM jobs WHERE job_id=?", (spec.job_id,)
            ).fetchone()
            if row is None:
                next_due = now if initial_next_due_ms is None else initial_next_due_ms
                self.db.execute(
                    """INSERT INTO jobs(
                           job_id, revision, enabled, mode, scheduler_owner,
                           schedule_json, task_json, next_due_ms, updated_at_ms,
                           created_at_ms, misfire_policy, deadline_ms,
                           grant_refs_json, delivery_policy_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        spec.job_id,
                        spec.revision,
                        int(spec.enabled),
                        spec.mode,
                        spec.owner,
                        canonical_json(spec.schedule),
                        canonical_json(spec.task),
                        next_due,
                        now,
                        now,
                        resolved_misfire,
                        self._deadline_to_ms(spec.deadline),
                        canonical_json(list(spec.grant_refs)),
                        canonical_json(spec.delivery_policy),
                    ),
                )
            else:
                if spec.revision != row["revision"] + 1:
                    raise PASError(
                        ErrorCode.CONFLICT,
                        f"job revision {spec.revision} does not follow stored revision"
                        f" {row['revision']}",
                        scope="jobs",
                    )
                self.db.execute(
                    """UPDATE jobs SET revision=?, enabled=?, mode=?, scheduler_owner=?,
                           schedule_json=?, task_json=?, next_due_ms=?, updated_at_ms=?,
                           misfire_policy=?, deadline_ms=?, grant_refs_json=?,
                           delivery_policy_json=?
                       WHERE job_id=? AND revision=?""",
                    (
                        spec.revision,
                        int(spec.enabled),
                        spec.mode,
                        spec.owner,
                        canonical_json(spec.schedule),
                        canonical_json(spec.task),
                        now,
                        now,
                        resolved_misfire,
                        self._deadline_to_ms(spec.deadline),
                        canonical_json(list(spec.grant_refs)),
                        canonical_json(spec.delivery_policy),
                        spec.job_id,
                        row["revision"],
                    ),
                )
            self.db.execute(
                "INSERT INTO jobs_idempotency(idempotency_key, request_hash, job_id, created_at_ms)"
                " VALUES (?,?,?,?)",
                (idempotency_key, request_hash, spec.job_id, now),
            )
            record = self._read_job(spec.job_id)
            assert record is not None
            return record

    def get_job(self, job_id: str) -> JobRecord | None:
        return self._read_job(job_id)

    def list_jobs(self, *, enabled: bool | None = None) -> list[JobRecord]:
        if enabled is None:
            rows = self.db.execute("SELECT job_id FROM jobs ORDER BY job_id").fetchall()
        else:
            rows = self.db.execute(
                "SELECT job_id FROM jobs WHERE enabled=? ORDER BY job_id", (int(enabled),)
            ).fetchall()
        records = [self._read_job(row["job_id"]) for row in rows]
        return [record for record in records if record is not None]

    def set_job_enabled(
        self, job_id: str, *, expected_revision: int, enabled: bool, now_ms: int | None = None
    ) -> JobRecord:
        """Pause or resume with optimistic revision.

        Pause freezes ``next_due_ms``; resume resets it to *now* so the
        admission loop re-evaluates the pause interval under the job's
        misfire policy (heartbeats coalesce, tasks catch up once within
        their grace window, strict jobs expire).
        """
        now = self.clock.wall_now_ms() if now_ms is None else now_ms
        with self.transaction():
            cursor = self.db.execute(
                "SELECT revision FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if cursor is None:
                raise PASError(ErrorCode.INVALID_CONFIG, f"unknown job {job_id!r}", scope="jobs")
            if cursor["revision"] != expected_revision:
                raise PASError(
                    ErrorCode.CONFLICT,
                    f"job revision moved: expected {expected_revision},"
                    f" stored {cursor['revision']}",
                    scope="jobs",
                )
            if enabled:
                self.db.execute(
                    "UPDATE jobs SET revision=?, enabled=?, next_due_ms=?, updated_at_ms=?"
                    " WHERE job_id=? AND revision=?",
                    (expected_revision + 1, 1, now, now, job_id, expected_revision),
                )
            else:
                self.db.execute(
                    "UPDATE jobs SET revision=?, enabled=?, updated_at_ms=?"
                    " WHERE job_id=? AND revision=?",
                    (expected_revision + 1, 0, now, job_id, expected_revision),
                )
            record = self._read_job(job_id)
            assert record is not None
            return record

    def delete_job(self, job_id: str) -> None:
        """Delete a job that never admitted anything.

        Jobs with admitted events keep their ledger: deleting a job must
        not cascade away the run/occurrence audit trail (SPEC §13.2).
        Pause those instead.
        """
        with self.transaction():
            referenced = self.db.execute(
                "SELECT 1 FROM events WHERE job_id=? LIMIT 1", (job_id,)
            ).fetchone()
            if referenced is not None:
                raise PASError(
                    ErrorCode.CONFLICT,
                    "job has admitted occurrences; pause it instead of deleting",
                    scope="jobs",
                )
            cursor = self.db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            if cursor.rowcount != 1:
                raise PASError(ErrorCode.INVALID_CONFIG, f"unknown job {job_id!r}", scope="jobs")

    # ------------------------------------------------------------------ #
    # Occurrence / event admission (used by the scheduler and by hooks in P2)
    # ------------------------------------------------------------------ #

    def due_job_ids(self, now_ms: int) -> list[str]:
        """Enabled pas-owned jobs whose due time has arrived.

        Reads the ``jobs_due(enabled, next_due_ms)`` index — the scan never
        walks the whole table (SPEC §5.2, §16.3).
        """
        rows = self.db.execute(
            """SELECT job_id FROM jobs
               WHERE enabled=1 AND scheduler_owner='pas'
                 AND next_due_ms IS NOT NULL AND next_due_ms<=?
               ORDER BY next_due_ms, job_id""",
            (now_ms,),
        ).fetchall()
        return [row["job_id"] for row in rows]

    def latest_admitted_slot(self, job_id: str) -> int | None:
        row = self.db.execute(
            "SELECT MAX(slot_ms) FROM job_occurrences WHERE job_id=? AND state='admitted'",
            (job_id,),
        ).fetchone()
        return row[0]

    def latest_known_occurrence(self, job_id: str) -> tuple[int, str] | None:
        """Most recent materialized slot (admitted or expired) with its state.

        The scheduler uses this as its episode cursor: expired episodes must
        not be re-counted into the next missed episode's reason.
        """
        row = self.db.execute(
            """SELECT slot_ms, state FROM job_occurrences
               WHERE job_id=? ORDER BY slot_ms DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return row["slot_ms"], row["state"]

    def update_next_due(
        self, job_id: str, *, expected_revision: int, next_due_ms: int | None, now_ms: int
    ) -> str:
        """Advance a job's due cursor without admitting anything.

        Used when the latest slot was already handled: this keeps the due
        index meaningful so idle jobs are not rescanned every tick.
        """
        with self.transaction():
            job = self.db.execute(
                "SELECT revision, enabled FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None:
                return "skipped_missing"
            if job["revision"] != expected_revision:
                return "skipped_revision"
            if not job["enabled"]:
                return "skipped_disabled"
            self.db.execute(
                "UPDATE jobs SET next_due_ms=?, updated_at_ms=? WHERE job_id=? AND revision=?",
                (next_due_ms, now_ms, job_id, expected_revision),
            )
            return "advanced"

    def admit_job_occurrence(
        self,
        job_id: str,
        *,
        expected_revision: int,
        kind: str,
        slot_ms: int,
        next_due_ms: int | None,
        now_ms: int,
    ) -> str:
        """Atomically admit one occurrence of a scheduled job.

        Re-verifies revision and enabled inside the transaction, then
        inserts occurrence + event + queued run and advances ``next_due_ms``
        (SPEC §5.2). Re-admitting the same occurrence is idempotent: the
        unique keys collapse it onto the existing event and run.
        Returns one of: ``admitted``, ``already_admitted``,
        ``skipped_disabled``, ``skipped_revision``, ``skipped_missing``.
        """
        with self.transaction():
            job = self.db.execute(
                "SELECT revision, enabled, mode, deadline_ms FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if job is None:
                return "skipped_missing"
            if job["revision"] != expected_revision:
                return "skipped_revision"
            if not job["enabled"]:
                return "skipped_disabled"
            occurrence_id = self._occurrence_id(job_id, expected_revision, slot_ms)
            payload = canonical_json(
                {
                    "job_id": job_id,
                    "revision": expected_revision,
                    "slot_ms": slot_ms,
                    "kind": kind,
                    "mode": job["mode"],
                }
            )
            payload_hash = _sha256_text(payload)
            event_id = self._event_id(occurrence_id)
            prior = self.db.execute(
                "SELECT payload_hash FROM events WHERE idempotency_key=?", (occurrence_id,)
            ).fetchone()
            if prior is not None and prior["payload_hash"] != payload_hash:
                raise PASError(
                    ErrorCode.CONFLICT,
                    "event dedupe key reused with different content",
                    scope="events",
                )
            expires_at_ms = (
                job["deadline_ms"] if job["deadline_ms"] is not None else slot_ms + _EVENT_TTL_MS
            )
            self.db.execute(
                """INSERT OR IGNORE INTO events(
                       event_id, idempotency_key, origin, job_id, job_revision,
                       occurrence_id, payload_hash, payload_ref, payload_json,
                       observed_at_ms, expires_at_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    occurrence_id,
                    "scheduler",
                    job_id,
                    expected_revision,
                    occurrence_id,
                    payload_hash,
                    "inline",
                    payload,
                    now_ms,
                    expires_at_ms,
                ),
            )
            run_id = self._run_id(event_id)
            run_deadline = (
                job["deadline_ms"] if job["deadline_ms"] is not None else now_ms + _RUN_DEFAULT_DEADLINE_MS
            )
            self.db.execute(
                """INSERT OR IGNORE INTO runs(
                       run_id, event_id, state, deadline_ms, policy_version,
                       created_at_ms, updated_at_ms)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_id, event_id, "queued", run_deadline, _POLICY_VERSION, now_ms, now_ms),
            )
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO job_occurrences(
                       occurrence_id, job_id, job_revision, kind, slot_ms,
                       state, reason, event_id, recorded_at_ms)
                   VALUES (?,?,?,?,?,'admitted',NULL,?,?)""",
                (occurrence_id, job_id, expected_revision, kind, slot_ms, event_id, now_ms),
            )
            outcome = "already_admitted" if cursor.rowcount == 0 else "admitted"
            self.db.execute(
                "UPDATE jobs SET next_due_ms=?, updated_at_ms=? WHERE job_id=? AND revision=?",
                (next_due_ms, now_ms, job_id, expected_revision),
            )
            return outcome

    def record_missed_occurrence(
        self,
        job_id: str,
        *,
        expected_revision: int,
        kind: str,
        slot_ms: int,
        reason: str,
        next_due_ms: int | None,
        now_ms: int,
    ) -> str:
        """Record a missed slot as expired with its queryable reason
        (SPEC §5.2) and advance ``next_due_ms`` past the episode."""
        if not reason or len(reason) > 500:
            raise PASError(ErrorCode.INVALID_CONFIG, "reason must be 1..500 chars")
        with self.transaction():
            job = self.db.execute(
                "SELECT revision, enabled FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None:
                return "skipped_missing"
            if job["revision"] != expected_revision:
                return "skipped_revision"
            if not job["enabled"]:
                return "skipped_disabled"
            occurrence_id = self._occurrence_id(job_id, expected_revision, slot_ms)
            self.db.execute(
                """INSERT OR IGNORE INTO job_occurrences(
                       occurrence_id, job_id, job_revision, kind, slot_ms,
                       state, reason, event_id, recorded_at_ms)
                   VALUES (?,?,?,?,?,'expired',?,NULL,?)""",
                (occurrence_id, job_id, expected_revision, kind, slot_ms, reason, now_ms),
            )
            self.db.execute(
                "UPDATE jobs SET next_due_ms=?, updated_at_ms=? WHERE job_id=? AND revision=?",
                (next_due_ms, now_ms, job_id, expected_revision),
            )
            return "recorded"

    def occurrences(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT occurrence_id, job_revision, kind, slot_ms, state, reason, event_id
               FROM job_occurrences WHERE job_id=? ORDER BY slot_ms""",
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def admit_event(
        self,
        dedupe_key: str,
        *,
        origin: str,
        payload: Any,
        observed_at_ms: int,
        expires_at_ms: int,
        occurrence_id: str | None = None,
        job_id: str | None = None,
        job_revision: int | None = None,
        create_run: bool = True,
    ) -> str:
        """Admit a wake event into the EventStore (SPEC §5 core chain).

        Idempotent on ``dedupe_key``: identical content returns the
        existing event id; different content under the same key is a
        conflict. Non-scheduler origins (hook, manual, host_delegate)
        arrive through this path.
        """
        if origin not in ("scheduler", "hook", "manual", "host_delegate"):
            raise PASError(ErrorCode.INVALID_CONFIG, f"unknown event origin {origin!r}")
        if not dedupe_key or len(dedupe_key) > 256:
            raise PASError(ErrorCode.INVALID_CONFIG, "dedupe_key must be 1..256 chars")
        payload_json = canonical_json(payload)
        if len(payload_json.encode("utf-8")) > _EVENT_PAYLOAD_MAX_BYTES:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "event payload exceeds the 64 KiB budget", scope="events"
            )
        payload_hash = _sha256_text(payload_json)
        event_id = self._event_id(dedupe_key)
        with self.transaction():
            prior = self.db.execute(
                "SELECT payload_hash FROM events WHERE idempotency_key=?", (dedupe_key,)
            ).fetchone()
            if prior is not None:
                if prior["payload_hash"] != payload_hash:
                    raise PASError(
                        ErrorCode.CONFLICT,
                        "event dedupe key reused with different content",
                        scope="events",
                    )
                return event_id
            self.db.execute(
                """INSERT INTO events(
                       event_id, idempotency_key, origin, job_id, job_revision,
                       occurrence_id, payload_hash, payload_ref, payload_json,
                       observed_at_ms, expires_at_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    dedupe_key,
                    origin,
                    job_id,
                    job_revision,
                    occurrence_id,
                    payload_hash,
                    "inline",
                    payload_json,
                    observed_at_ms,
                    expires_at_ms,
                ),
            )
            if create_run:
                run_id = self._run_id(event_id)
                self.db.execute(
                    """INSERT INTO runs(
                           run_id, event_id, state, deadline_ms, policy_version,
                           created_at_ms, updated_at_ms)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        event_id,
                        "queued",
                        observed_at_ms + _RUN_DEFAULT_DEADLINE_MS,
                        _POLICY_VERSION,
                        observed_at_ms,
                        observed_at_ms,
                    ),
                )
            return event_id

    # ------------------------------------------------------------------ #
    # Run claim / completion with fencing (SPEC §10.3, §13.2)
    # ------------------------------------------------------------------ #

    def claim_run(self, *, now_ms: int, ttl_ms: int, run_id: str | None = None) -> RunLease | None:
        """Claim one claimable run (queued, or running with an expired
        lease) by bumping its fence and setting the lease.

        Expired-lease runs are reclaimable because P1 workers do read-only
        decision work; effect-fencing around external writes arrives with
        the ToolBroker (P3/P4).
        """
        if ttl_ms <= 0:
            raise PASError(ErrorCode.INVALID_CONFIG, "lease TTL must be positive")
        with self.transaction():
            if run_id is not None:
                row = self.db.execute(
                    """SELECT run_id, event_id, fence FROM runs WHERE run_id=?
                       AND (state='queued' OR (state='running' AND lease_until_ms<=?))""",
                    (run_id, now_ms),
                ).fetchone()
            else:
                row = self.db.execute(
                    """SELECT run_id, event_id, fence FROM runs
                       WHERE state='queued' OR (state='running' AND lease_until_ms<=?)
                       ORDER BY rowid LIMIT 1""",
                    (now_ms,),
                ).fetchone()
            if row is None:
                return None
            fence = row["fence"] + 1
            self.db.execute(
                "UPDATE runs SET state='running', fence=?, lease_until_ms=?,"
                " attempt=attempt+1, updated_at_ms=? WHERE run_id=?",
                (fence, now_ms + ttl_ms, now_ms, row["run_id"]),
            )
            return RunLease(row["run_id"], row["event_id"], fence, now_ms + ttl_ms)

    def complete_run(self, lease: RunLease, *, now_ms: int) -> None:
        with self.transaction():
            row = self.db.execute(
                "SELECT state, fence, lease_until_ms FROM runs WHERE run_id=?", (lease.run_id,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "running"
                or row["fence"] != lease.fence
                or row["lease_until_ms"] <= now_ms
            ):
                raise PASError(
                    ErrorCode.CONFLICT,
                    "run fence expired or superseded; completion rejected",
                    scope="runs",
                )
            self.db.execute(
                "UPDATE runs SET state='planned', lease_until_ms=0, updated_at_ms=? WHERE run_id=?",
                (now_ms, lease.run_id),
            )

    def fail_run(self, lease: RunLease, *, error_class: str, now_ms: int) -> None:
        if not error_class or len(error_class) > 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "error_class must be 1..128 chars")
        with self.transaction():
            row = self.db.execute(
                "SELECT state, fence, lease_until_ms FROM runs WHERE run_id=?", (lease.run_id,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "running"
                or row["fence"] != lease.fence
                or row["lease_until_ms"] <= now_ms
            ):
                raise PASError(
                    ErrorCode.CONFLICT,
                    "run fence expired or superseded; failure recording rejected",
                    scope="runs",
                )
            self.db.execute(
                "UPDATE runs SET state='failed', lease_until_ms=0, error_class=?,"
                " updated_at_ms=? WHERE run_id=?",
                (error_class, now_ms, lease.run_id),
            )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _read_job(self, job_id: str) -> JobRecord | None:
        row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return JobRecord(
            job_id=row["job_id"],
            revision=row["revision"],
            owner=row["scheduler_owner"],
            mode=row["mode"],
            schedule=canonical_loads(row["schedule_json"]),
            task=canonical_loads(row["task_json"]),
            grant_refs=tuple(canonical_loads(row["grant_refs_json"])),
            delivery_policy=canonical_loads(row["delivery_policy_json"]),
            misfire_policy=row["misfire_policy"],
            deadline_ms=row["deadline_ms"],
            enabled=bool(row["enabled"]),
            next_due_ms=row["next_due_ms"],
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
        )

    @staticmethod
    def _deadline_to_ms(deadline: str | None) -> int | None:
        if deadline is None:
            return None
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00").replace("z", "+00:00"))
        if parsed.tzinfo is None:
            raise PASError(ErrorCode.INVALID_CONFIG, "deadline needs an explicit offset")
        return int(parsed.astimezone(timezone.utc).timestamp() * 1000)

    def _occurrence_id(self, job_id: str, revision: int, slot_ms: int) -> str:
        # Trigger-layer dedupe key of SPEC §10.1: profile + job + revision +
        # slot. The profile prefix lives in event_id hashing below.
        return f"{job_id}:{revision}:{slot_ms}"

    def _event_id(self, occurrence_key: str) -> str:
        digest = hashlib.sha256(f"{self.profile}|{occurrence_key}".encode("utf-8")).hexdigest()
        return f"evt{digest[:28]}"

    def _run_id(self, event_id: str) -> str:
        digest = hashlib.sha256(f"{self.profile}|run|{event_id}".encode("utf-8")).hexdigest()
        return f"run{digest[:28]}"


def canonical_loads(text: str) -> Any:
    return json.loads(text)
