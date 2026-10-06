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
    validate_context_pack,
)

__all__ = [
    "Store",
    "JobRecord",
    "RunLease",
    "EventRecord",
    "SourceStateRecord",
    "SnapshotRecord",
    "HookRecord",
    "HookClaim",
    "HookCommitResult",
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


@dataclass(frozen=True)
class EventRecord:
    """One admitted event (schema §13.1 ``events`` + payload)."""

    event_id: str
    idempotency_key: str
    origin: str
    job_id: str | None
    job_revision: int | None
    occurrence_id: str | None
    payload: dict[str, Any]
    observed_at_ms: int
    expires_at_ms: int


@dataclass(frozen=True)
class SourceStateRecord:
    """Per-(source, account) delta cursor and detection watermark (§7.1:
    已读取来源的 cursor 是独立进度，不等于已通知)."""

    source_id: str
    account_ref: str
    cursor_ref: str | None
    detected_watermark: str | None
    version: int
    updated_at_ms: int


@dataclass(frozen=True)
class SnapshotRecord:
    """One observed source snapshot (content-addressed, §13.1)."""

    snapshot_id: str
    source_id: str
    account_ref: str
    content_ref: str
    content_hash: str
    observed_at_ms: int
    fresh_until_ms: int
    sensitivity: str
    tombstone: bool


@dataclass(frozen=True)
class HookRecord:
    """Stored hook state (schema §13.1 ``hooks`` + P2 error accounting)."""

    hook_id: str
    definition_hash: str
    definition: dict[str, Any]
    enabled: bool
    state_version: int
    state: dict[str, Any]
    lease_fence: int
    lease_until_ms: int
    next_due_ms: int | None
    poll_interval_ms: int | None
    timeout_ms: int | None
    created_at_ms: int
    updated_at_ms: int
    last_run_at_ms: int | None
    last_decision: str | None
    consecutive_errors: int
    last_error_class: str | None
    last_error_at_ms: int | None
    error_backoff_until_ms: int


@dataclass(frozen=True)
class HookClaim:
    """Lease token for one hook invocation (SPEC §6.2 step 1).

    ``fence`` is the hook-level exclusion primitive: a later claim bumps
    it, and only the current fence may commit state or record errors.
    The canonical state snapshot travels with the claim so the runner
    stages exactly the state this lease saw.
    """

    hook_id: str
    state_version: int
    state: dict[str, Any]
    fence: int
    lease_until_ms: int
    definition_hash: str
    definition: dict[str, Any]


@dataclass(frozen=True)
class HookCommitResult:
    """Outcome of one hook invocation commit.

    ``committed_wake`` / ``committed_silent`` advanced the hook state;
    ``idempotent_replay`` found the same invocation already committed
    with identical content and returns the original ``event_id``;
    ``skipped_*`` outcomes mean nothing was written (disabled hook, lost
    fence race, unexpected state move).
    """

    outcome: str
    event_id: str | None


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
        with self.transaction():
            return self._insert_event_tx(
                dedupe_key,
                origin=origin,
                payload=payload,
                observed_at_ms=observed_at_ms,
                expires_at_ms=expires_at_ms,
                occurrence_id=occurrence_id,
                job_id=job_id,
                job_revision=job_revision,
                create_run=create_run,
            )

    def _insert_event_tx(
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
        """Insert one event + optional queued run inside the caller's
        transaction (shared by :meth:`admit_event` and the hook commit
        path, SPEC §6.2 step 4)."""
        payload_json = canonical_json(payload)
        if len(payload_json.encode("utf-8")) > _EVENT_PAYLOAD_MAX_BYTES:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "event payload exceeds the 64 KiB budget", scope="events"
            )
        payload_hash = _sha256_text(payload_json)
        event_id = self._event_id(dedupe_key)
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
    # Hooks (P2 / SPEC §6, HOOK-01)
    # ------------------------------------------------------------------ #

    def register_hook(
        self,
        hook_id: str,
        *,
        definition_hash: str,
        definition: dict[str, Any],
        poll_interval_ms: int,
        timeout_ms: int | None = None,
        initial_state: dict[str, Any] | None = None,
        now_ms: int | None = None,
    ) -> HookRecord:
        """Register a hook definition. Re-registering the identical
        definition is idempotent; a different definition under the same
        hook id is a conflict (definitions are immutable — new behaviour
        means a new hook id). The stored definition is verified against
        ``definition_hash`` so the runnable definition cannot drift from
        its integrity anchor."""
        now = self.clock.wall_now_ms() if now_ms is None else now_ms
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", hook_id or ""):
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"hook_id {hook_id!r} fails naming rule", scope="hooks"
            )
        if not isinstance(definition, dict):
            raise PASError(ErrorCode.INVALID_CONFIG, "definition must be an object", scope="hooks")
        if content_hash(definition) != definition_hash:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "definition_hash does not match definition", scope="hooks"
            )
        if poll_interval_ms <= 0:
            raise PASError(ErrorCode.INVALID_CONFIG, "poll_interval_ms must be positive", scope="hooks")
        if timeout_ms is not None and timeout_ms <= 0:
            raise PASError(ErrorCode.INVALID_CONFIG, "timeout_ms must be positive", scope="hooks")
        state = initial_state if initial_state is not None else {}
        if not isinstance(state, dict):
            raise PASError(ErrorCode.INVALID_CONFIG, "initial_state must be an object", scope="hooks")
        with self.transaction():
            row = self.db.execute(
                "SELECT definition_hash FROM hooks WHERE hook_id=?", (hook_id,)
            ).fetchone()
            if row is not None:
                if row["definition_hash"] != definition_hash:
                    raise PASError(
                        ErrorCode.CONFLICT,
                        f"hook {hook_id!r} already registered with a different definition",
                        scope="hooks",
                    )
                return self._read_hook(hook_id)  # type: ignore[return-value]
            self.db.execute(
                """INSERT INTO hooks(
                       hook_id, definition_hash, definition_json, enabled, state_version,
                       state_json, poll_interval_ms, timeout_ms, created_at_ms, updated_at_ms,
                       next_due_ms)
                   VALUES (?,?,?,1,0,?,?,?,?,?,?)""",
                (
                    hook_id,
                    definition_hash,
                    canonical_json(definition),
                    canonical_json(state),
                    poll_interval_ms,
                    timeout_ms,
                    now,
                    now,
                    now,
                ),
            )
            record = self._read_hook(hook_id)
            assert record is not None
            return record

    def get_hook(self, hook_id: str) -> HookRecord | None:
        return self._read_hook(hook_id)

    def list_hooks(self, *, enabled: bool | None = None) -> list[HookRecord]:
        if enabled is None:
            rows = self.db.execute("SELECT hook_id FROM hooks ORDER BY hook_id").fetchall()
        else:
            rows = self.db.execute(
                "SELECT hook_id FROM hooks WHERE enabled=? ORDER BY hook_id", (int(enabled),)
            ).fetchall()
        records = [self._read_hook(row["hook_id"]) for row in rows]
        return [record for record in records if record is not None]

    def set_hook_enabled(self, hook_id: str, *, enabled: bool, now_ms: int | None = None) -> HookRecord:
        """Stop or resume a hook (admin/API action, SPEC §6.2).

        Disabling never deletes admitted events or queued runs — stopping
        a probe leaves the notifications it already created pending
        (SPEC §6.2). An in-flight invocation cannot commit afterwards:
        the commit re-checks ``enabled`` inside its transaction, so
        disable wins over in-flight work (mirrors job pause).
        """
        now = self.clock.wall_now_ms() if now_ms is None else now_ms
        with self.transaction():
            cursor = self.db.execute(
                "UPDATE hooks SET enabled=?, updated_at_ms=? WHERE hook_id=?",
                (int(enabled), now, hook_id),
            )
            if cursor.rowcount != 1:
                raise PASError(
                    ErrorCode.INVALID_CONFIG, f"unknown hook {hook_id!r}", scope="hooks"
                )
            record = self._read_hook(hook_id)
            assert record is not None
            return record

    def delete_hook(self, hook_id: str) -> None:
        """Delete a hook that never admitted anything.

        Hooks with committed invocations keep their audit trail
        (``hook_invocations`` references them); disable those instead.
        """
        with self.transaction():
            referenced = self.db.execute(
                "SELECT 1 FROM hook_invocations WHERE hook_id=? LIMIT 1", (hook_id,)
            ).fetchone()
            if referenced is not None:
                raise PASError(
                    ErrorCode.CONFLICT,
                    "hook has committed invocations; disable it instead of deleting",
                    scope="hooks",
                )
            cursor = self.db.execute("DELETE FROM hooks WHERE hook_id=?", (hook_id,))
            if cursor.rowcount != 1:
                raise PASError(ErrorCode.INVALID_CONFIG, f"unknown hook {hook_id!r}", scope="hooks")

    def due_hook_ids(self, now_ms: int) -> list[str]:
        """Enabled hooks whose poll time has arrived (uses ``hooks_due``)."""
        rows = self.db.execute(
            """SELECT hook_id FROM hooks
               WHERE enabled=1 AND next_due_ms IS NOT NULL AND next_due_ms<=?
               ORDER BY next_due_ms, hook_id""",
            (now_ms,),
        ).fetchall()
        return [row["hook_id"] for row in rows]

    def claim_hook(
        self, hook_id: str, *, now_ms: int, ttl_ms: int, force: bool = False
    ) -> HookClaim | None:
        """Claim one hook invocation lease (SPEC §6.2 step 1).

        Refuses while the hook is disabled, still leased, or — unless
        ``force`` (admin-triggered diagnostic run) — inside its error
        cooldown, so one hook never has two parallel writers. Bumping
        ``lease_fence`` is what makes later commits from stale claimants
        rejectable.
        """
        if ttl_ms <= 0:
            raise PASError(ErrorCode.INVALID_CONFIG, "lease TTL must be positive", scope="hooks")
        with self.transaction():
            row = self.db.execute(
                "SELECT enabled, lease_fence, lease_until_ms, error_backoff_until_ms"
                " FROM hooks WHERE hook_id=?",
                (hook_id,),
            ).fetchone()
            if row is None or not row["enabled"]:
                return None
            if row["lease_until_ms"] > now_ms:
                return None
            if not force and row["error_backoff_until_ms"] > now_ms:
                return None
            fence = row["lease_fence"] + 1
            self.db.execute(
                "UPDATE hooks SET lease_fence=?, lease_until_ms=? WHERE hook_id=?",
                (fence, now_ms + ttl_ms, hook_id),
            )
            record = self._read_hook(hook_id)
            assert record is not None
            return HookClaim(
                hook_id=record.hook_id,
                state_version=record.state_version,
                state=record.state,
                fence=fence,
                lease_until_ms=now_ms + ttl_ms,
                definition_hash=record.definition_hash,
                definition=record.definition,
            )

    def defer_hook(self, hook_id: str, *, next_due_ms: int, now_ms: int) -> str:
        """Push a hook's next poll without running it (error-cooldown
        skipping). Best-effort: races with a real run are resolved by
        whichever commit touches ``next_due_ms`` last."""
        with self.transaction():
            row = self.db.execute(
                "SELECT enabled FROM hooks WHERE hook_id=?", (hook_id,)
            ).fetchone()
            if row is None:
                return "skipped_missing"
            if not row["enabled"]:
                return "skipped_disabled"
            self.db.execute(
                "UPDATE hooks SET next_due_ms=?, updated_at_ms=? WHERE hook_id=?",
                (next_due_ms, now_ms, hook_id),
            )
            return "deferred"

    def commit_hook_invocation(
        self,
        hook_id: str,
        invocation_id: str,
        *,
        expected_state_version: int,
        fence: int,
        request_hash: str,
        new_state: dict[str, Any],
        decision: str,
        reason: str,
        payload: Any,
        disable_after_run: bool,
        next_due_ms: int,
        now_ms: int,
    ) -> HookCommitResult:
        """CAS-commit one hook invocation (SPEC §6.2 step 4, HOOK-01).

        One transaction: invocation dedupe (same id + same content is an
        idempotent replay returning the original event; same id with
        different content is a conflict), fence/version/enabled re-check,
        wake-event admission, state update and ``disable_after_run`` —
        so a one-shot watch is disabled exactly when its wake event is
        durable, never before and never without it.
        """
        if decision not in ("silent", "wake"):
            raise PASError(ErrorCode.INVALID_CONFIG, "decision must be silent|wake", scope="hooks")
        if not isinstance(new_state, dict):
            raise PASError(ErrorCode.INVALID_CONFIG, "hook state must be an object", scope="hooks")
        with self.transaction():
            prior = self.db.execute(
                "SELECT request_hash, event_id FROM hook_invocations WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise PASError(
                        ErrorCode.CONFLICT,
                        "invocation_id replayed with different content",
                        scope="hooks",
                    )
                return HookCommitResult("idempotent_replay", prior["event_id"])
            row = self.db.execute(
                "SELECT enabled, state_version, lease_fence FROM hooks WHERE hook_id=?",
                (hook_id,),
            ).fetchone()
            if row is None:
                return HookCommitResult("skipped_missing", None)
            if not row["enabled"]:
                return HookCommitResult("skipped_disabled", None)
            if row["lease_fence"] != fence:
                return HookCommitResult("skipped_fence", None)
            if row["state_version"] != expected_state_version:
                return HookCommitResult("skipped_version", None)
            event_id = None
            if decision == "wake":
                event_id = self._insert_event_tx(
                    f"hook:{invocation_id}",
                    origin="hook",
                    payload={
                        "hook_id": hook_id,
                        "invocation_id": invocation_id,
                        "reason": reason,
                        "payload": payload,
                    },
                    observed_at_ms=now_ms,
                    expires_at_ms=now_ms + _EVENT_TTL_MS,
                )
            new_enabled = 0 if disable_after_run else 1
            self.db.execute(
                """UPDATE hooks SET state_version=state_version+1, state_json=?,
                       enabled=?, next_due_ms=?, last_run_at_ms=?, last_decision=?,
                       consecutive_errors=0, last_error_class=NULL, last_error_at_ms=NULL,
                       error_backoff_until_ms=0, updated_at_ms=?
                   WHERE hook_id=? AND lease_fence=?""",
                (
                    canonical_json(new_state),
                    new_enabled,
                    next_due_ms,
                    now_ms,
                    decision,
                    now_ms,
                    hook_id,
                    fence,
                ),
            )
            self.db.execute(
                """INSERT INTO hook_invocations(
                       invocation_id, hook_id, request_hash, state_version,
                       event_id, committed_at_ms)
                   VALUES (?,?,?,?,?,?)""",
                (invocation_id, hook_id, request_hash, expected_state_version, event_id, now_ms),
            )
            outcome = "committed_wake" if decision == "wake" else "committed_silent"
            return HookCommitResult(outcome, event_id)

    def record_hook_error(
        self,
        hook_id: str,
        *,
        fence: int,
        error_class: str,
        error_detail: str,
        backoff_until_ms: int,
        next_due_ms: int,
        now_ms: int,
    ) -> str:
        """Count one failed invocation and start its cooldown.

        Errors never admit wake events and never advance hook state
        (SPEC §6.1: 失败不唤醒). The backoff is the probe's own retry
        cooldown; admin diagnostics read these fields directly and are
        not throttled by it.
        """
        if not error_class or len(error_class) > 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "error_class must be 1..128 chars", scope="hooks")
        if len(error_detail) > 500:
            raise PASError(ErrorCode.INVALID_CONFIG, "error_detail must be <=500 chars", scope="hooks")
        with self.transaction():
            row = self.db.execute(
                "SELECT enabled, lease_fence FROM hooks WHERE hook_id=?", (hook_id,)
            ).fetchone()
            if row is None:
                return "skipped_missing"
            if not row["enabled"]:
                return "skipped_disabled"
            if row["lease_fence"] != fence:
                return "skipped_fence"
            self.db.execute(
                """UPDATE hooks SET consecutive_errors=consecutive_errors+1,
                       last_error_class=?, last_error_at_ms=?, error_backoff_until_ms=?,
                       next_due_ms=?, updated_at_ms=?
                   WHERE hook_id=? AND lease_fence=?""",
                (error_class, now_ms, backoff_until_ms, next_due_ms, now_ms, hook_id, fence),
            )
            return "recorded"

    def hook_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT invocation_id, hook_id, request_hash, state_version, event_id,"
            " committed_at_ms FROM hook_invocations WHERE invocation_id=?",
            (invocation_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _read_hook(self, hook_id: str) -> HookRecord | None:
        row = self.db.execute("SELECT * FROM hooks WHERE hook_id=?", (hook_id,)).fetchone()
        if row is None:
            return None
        return HookRecord(
            hook_id=row["hook_id"],
            definition_hash=row["definition_hash"],
            definition=canonical_loads(row["definition_json"]) if row["definition_json"] else {},
            enabled=bool(row["enabled"]),
            state_version=row["state_version"],
            state=canonical_loads(row["state_json"]),
            lease_fence=row["lease_fence"],
            lease_until_ms=row["lease_until_ms"],
            next_due_ms=row["next_due_ms"],
            poll_interval_ms=row["poll_interval_ms"],
            timeout_ms=row["timeout_ms"],
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
            last_run_at_ms=row["last_run_at_ms"],
            last_decision=row["last_decision"],
            consecutive_errors=row["consecutive_errors"],
            last_error_class=row["last_error_class"],
            last_error_at_ms=row["last_error_at_ms"],
            error_backoff_until_ms=row["error_backoff_until_ms"],
        )

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
    # Events, sources, snapshots (P3 / SPEC §7)
    # ------------------------------------------------------------------ #

    def get_event(self, event_id: str) -> EventRecord | None:
        row = self.db.execute(
            """SELECT event_id, idempotency_key, origin, job_id, job_revision,
                      occurrence_id, payload_json, observed_at_ms, expires_at_ms
               FROM events WHERE event_id=?""",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return EventRecord(
            event_id=row["event_id"],
            idempotency_key=row["idempotency_key"],
            origin=row["origin"],
            job_id=row["job_id"],
            job_revision=row["job_revision"],
            occurrence_id=row["occurrence_id"],
            payload=canonical_loads(row["payload_json"]) if row["payload_json"] else {},
            observed_at_ms=row["observed_at_ms"],
            expires_at_ms=row["expires_at_ms"],
        )

    def get_source_state(self, source_id: str, account_ref: str) -> SourceStateRecord | None:
        row = self.db.execute(
            """SELECT cursor_ref, detected_watermark, version, updated_at_ms
               FROM source_state WHERE source_id=? AND account_ref=?""",
            (source_id, account_ref),
        ).fetchone()
        if row is None:
            return None
        return SourceStateRecord(
            source_id=source_id,
            account_ref=account_ref,
            cursor_ref=row["cursor_ref"],
            detected_watermark=row["detected_watermark"],
            version=row["version"],
            updated_at_ms=row["updated_at_ms"],
        )

    def set_source_state(
        self,
        source_id: str,
        account_ref: str,
        *,
        cursor_ref: str | None,
        watermark: str | None = None,
        now_ms: int,
    ) -> SourceStateRecord:
        """Persist the fetch cursor after a delta was consumed. The row
        version increases on every write; the detection watermark is what
        L0 compares against on the next tick."""
        if cursor_ref is not None and (
            not isinstance(cursor_ref, str) or not 1 <= len(cursor_ref) <= 256
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "cursor_ref must be 1..256 chars or None", scope="sources")
        if watermark is not None and (not isinstance(watermark, str) or not 1 <= len(watermark) <= 256):
            raise PASError(ErrorCode.INVALID_CONFIG, "watermark must be 1..256 chars or None", scope="sources")
        with self.transaction():
            self.db.execute(
                """INSERT INTO source_state(source_id, account_ref, cursor_ref,
                                            detected_watermark, version, updated_at_ms)
                   VALUES (?,?,?,?,1,?)
                   ON CONFLICT(source_id, account_ref) DO UPDATE SET
                     cursor_ref=excluded.cursor_ref,
                     detected_watermark=excluded.detected_watermark,
                     version=version+1,
                     updated_at_ms=excluded.updated_at_ms""",
                (source_id, account_ref, cursor_ref, watermark, now_ms),
            )
            row = self.db.execute(
                """SELECT cursor_ref, detected_watermark, version, updated_at_ms
                   FROM source_state WHERE source_id=? AND account_ref=?""",
                (source_id, account_ref),
            ).fetchone()
            return SourceStateRecord(
                source_id=source_id,
                account_ref=account_ref,
                cursor_ref=row["cursor_ref"],
                detected_watermark=row["detected_watermark"],
                version=row["version"],
                updated_at_ms=row["updated_at_ms"],
            )

    def put_snapshot(
        self,
        source_id: str,
        account_ref: str,
        *,
        content: str,
        observed_at_ms: int,
        fresh_until_ms: int,
        sensitivity: str,
        tombstone: bool = False,
    ) -> SnapshotRecord:
        """Content-address an observed item. Re-observing identical
        content refreshes the observation metadata (content itself is
        immutable); the snapshot ref is stable across runs."""
        if sensitivity not in ("public", "private", "sensitive"):
            raise PASError(ErrorCode.INVALID_CONFIG, "sensitivity must be public|private|sensitive", scope="sources")
        content_hash = _sha256_text(content)
        snapshot_id = f"snap{content_hash[:28]}"
        with self.transaction():
            self.db.execute(
                """INSERT INTO snapshots(snapshot_id, source_id, account_ref, content_ref,
                                         content_hash, observed_at_ms, fresh_until_ms,
                                         sensitivity, tombstone)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(snapshot_id) DO UPDATE SET
                     observed_at_ms=excluded.observed_at_ms,
                     fresh_until_ms=excluded.fresh_until_ms,
                     sensitivity=excluded.sensitivity,
                     tombstone=excluded.tombstone""",
                (
                    snapshot_id,
                    source_id,
                    account_ref,
                    "inline",
                    content_hash,
                    observed_at_ms,
                    fresh_until_ms,
                    sensitivity,
                    int(tombstone),
                ),
            )
        return SnapshotRecord(
            snapshot_id=snapshot_id,
            source_id=source_id,
            account_ref=account_ref,
            content_ref="inline",
            content_hash=content_hash,
            observed_at_ms=observed_at_ms,
            fresh_until_ms=fresh_until_ms,
            sensitivity=sensitivity,
            tombstone=tombstone,
        )

    def snapshots_for(self, source_id: str, account_ref: str, *, limit: int = 32) -> list[SnapshotRecord]:
        rows = self.db.execute(
            """SELECT * FROM snapshots WHERE source_id=? AND account_ref=?
               ORDER BY observed_at_ms DESC, snapshot_id LIMIT ?""",
            (source_id, account_ref, int(limit)),
        ).fetchall()
        return [
            SnapshotRecord(
                snapshot_id=row["snapshot_id"],
                source_id=row["source_id"],
                account_ref=row["account_ref"],
                content_ref=row["content_ref"],
                content_hash=row["content_hash"],
                observed_at_ms=row["observed_at_ms"],
                fresh_until_ms=row["fresh_until_ms"],
                sensitivity=row["sensitivity"],
                tombstone=bool(row["tombstone"]),
            )
            for row in rows
        ]

    def snapshot_content(self, snapshot_id: str) -> str | None:
        """Snapshot bodies stay with the source items that produced them
        in P3 (the coordinator keeps them for evidence); a shared blob
        store arrives with P4. Unknown ids return None."""
        row = self.db.execute(
            "SELECT content_ref FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return None if row is None else row["content_ref"]

    # ------------------------------------------------------------------ #
    # Run ledger extensions: observation events, suppression, decision
    # (P3 / SPEC §4.3, §8; fence-checked like every other commit)
    # ------------------------------------------------------------------ #

    _RUN_EVENT_KIND_RE = re.compile(r"^[a-z0-9_.]{1,64}$")

    def _require_active_claim(self, lease: RunLease, now_ms: int) -> None:
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
                "run fence expired or superseded; write rejected",
                scope="runs",
            )

    def append_run_event(
        self,
        lease: RunLease,
        *,
        kind: str,
        safe_summary: str | None = None,
        detail_ref: str | None = None,
        now_ms: int,
    ) -> int:
        """Append one observation event (§13.1 run_events). Summaries are
        machine-safe: counts, refs, reasons — never source bodies or
        reasoning text (§7.3). Stale-fence writers are rejected."""
        if not self._RUN_EVENT_KIND_RE.fullmatch(kind or ""):
            raise PASError(ErrorCode.INVALID_CONFIG, "run event kind must match [a-z0-9_.]{1,64}", scope="runs")
        if safe_summary is not None and (not isinstance(safe_summary, str) or len(safe_summary) > 500):
            raise PASError(ErrorCode.INVALID_CONFIG, "safe_summary must be at most 500 chars", scope="runs")
        with self.transaction():
            self._require_active_claim(lease, now_ms)
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id=?", (lease.run_id,)
            ).fetchone()[0]
            self.db.execute(
                """INSERT INTO run_events(run_id, seq, kind, safe_summary, detail_ref, created_at_ms)
                   VALUES (?,?,?,?,?,?)""",
                (lease.run_id, seq, kind, safe_summary, detail_ref, now_ms),
            )
            return seq

    def record_run_suppressed(self, lease: RunLease, *, reason: str, now_ms: int) -> None:
        """End a run before any model call with its machine reason (§5.3
        L0: 空清单、无变化、未授权等直接结束，记录机器原因但不通知)."""
        if not reason or len(reason) > 500:
            raise PASError(ErrorCode.INVALID_CONFIG, "reason must be 1..500 chars", scope="runs")
        with self.transaction():
            self._require_active_claim(lease, now_ms)
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id=?", (lease.run_id,)
            ).fetchone()[0]
            self.db.execute(
                """INSERT INTO run_events(run_id, seq, kind, safe_summary, detail_ref, created_at_ms)
                   VALUES (?,?, 'suppressed', ?, NULL, ?)""",
                (lease.run_id, seq, reason, now_ms),
            )
            self.db.execute(
                "UPDATE runs SET state='suppressed', lease_until_ms=0, updated_at_ms=? WHERE run_id=?",
                (now_ms, lease.run_id),
            )

    def record_run_decision(
        self,
        lease: RunLease,
        *,
        context_pack: dict[str, Any],
        decision: dict[str, Any],
        proposals: list[dict[str, Any]],
        usage: dict[str, Any] | None,
        now_ms: int,
    ) -> str:
        """Commit one finished analysis in a single transaction (§13.2:
        决策完成 = 校验 run fence + 保存审计 + 建 proposals + 更新 run state).

        ``context_pack`` / ``decision`` / ``proposals`` / ``usage`` are the
        validated wire dicts (schemas/v1). The run transitions to
        ``proposed`` — downstream policy evaluation, actions and delivery
        stay P4. Same-fence idempotency is NOT provided here: a committed
        decision finalizes the claim; a retried writer holds a stale fence
        and is rejected by ``_require_active_claim``."""
        problems = validate_context_pack(context_pack)
        if problems:
            raise PASError(ErrorCode.INVALID_CONFIG, "; ".join(problems), scope="runs")
        summary = decision.get("summary")
        if not isinstance(summary, str) or not 1 <= len(summary) <= 500:
            raise PASError(ErrorCode.INVALID_CONFIG, "decision.summary must be 1..500 chars", scope="runs")
        context_ref = f"ctx:{lease.run_id}"
        pack_json = canonical_json(context_pack)
        pack_hash = _sha256_text(pack_json)
        with self.transaction():
            self._require_active_claim(lease, now_ms)
            run = self.db.execute(
                "SELECT event_id FROM runs WHERE run_id=?", (lease.run_id,)
            ).fetchone()
            self.db.execute(
                """INSERT INTO context_packs(context_ref, run_id, event_id,
                                            pack_json, content_hash, created_at_ms)
                   VALUES (?,?,?,?,?,?)""",
                (context_ref, lease.run_id, run["event_id"], pack_json, pack_hash, now_ms),
            )
            self.db.execute(
                "UPDATE runs SET context_ref=? WHERE run_id=?", (context_ref, lease.run_id)
            )
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id=?", (lease.run_id,)
            ).fetchone()[0]
            seq += 1
            self.db.execute(
                """INSERT INTO run_events(run_id, seq, kind, safe_summary, detail_ref, created_at_ms)
                   VALUES (?,?, 'decision', ?, ?, ?)""",
                (
                    lease.run_id,
                    seq,
                    f"{decision.get('decision')}: {summary}"[:500],
                    context_ref,
                    now_ms,
                ),
            )
            seq += 1
            self.db.execute(
                """INSERT INTO run_events(run_id, seq, kind, safe_summary, detail_ref, created_at_ms)
                   VALUES (?,?, 'usage', ?, NULL, ?)""",
                (lease.run_id, seq, canonical_json(usage) if usage is not None else "unknown", now_ms),
            )
            for index, proposal in enumerate(proposals, start=1):
                kind = proposal.get("kind")
                if kind not in (
                    "notify_self",
                    "draft",
                    "internal_record",
                    "suggest_watch",
                    "request_external_action",
                ):
                    raise PASError(ErrorCode.INVALID_CONFIG, f"proposal kind {kind!r} invalid", scope="runs")
                fact_id = proposal.get("fact_id")
                if not isinstance(fact_id, str) or not 1 <= len(fact_id) <= 256:
                    raise PASError(ErrorCode.INVALID_CONFIG, "proposal fact_id must be 1..256 chars", scope="runs")
                body = proposal.get("body")
                if body is not None and (not isinstance(body, str) or len(body) > 20000):
                    raise PASError(ErrorCode.INVALID_CONFIG, "proposal body must be at most 20000 chars", scope="runs")
                self.db.execute(
                    """INSERT INTO run_proposals(
                           proposal_id, run_id, seq, kind, fact_id, revision, body,
                           arguments_json, evidence_refs_json, expires_at_ms, created_at_ms)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"prop{content_hash({'run': lease.run_id, 'seq': index, 'fact': fact_id, 'kind': kind})[:28]}",
                        lease.run_id,
                        index,
                        kind,
                        fact_id,
                        proposal.get("revision"),
                        body,
                        canonical_json(proposal["arguments"]) if proposal.get("arguments") is not None else None,
                        canonical_json(proposal.get("evidence_refs") or []),
                        self._deadline_to_ms(proposal.get("expires_at")),
                        now_ms,
                    ),
                )
            self.db.execute(
                """UPDATE runs SET state='proposed', lease_until_ms=0,
                       decision_summary=?, proposal_count=?, usage_json=?, updated_at_ms=?
                   WHERE run_id=?""",
                (summary, len(proposals), canonical_json(usage) if usage is not None else None, now_ms, lease.run_id),
            )
            return context_ref

    def run_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT seq, kind, safe_summary, detail_ref, created_at_ms
               FROM run_events WHERE run_id=? ORDER BY seq""",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def run_proposals(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT proposal_id, seq, kind, fact_id, revision, body,
                      arguments_json, evidence_refs_json, expires_at_ms, created_at_ms
               FROM run_proposals WHERE run_id=? ORDER BY seq""",
            (run_id,),
        ).fetchall()
        return [
            {
                "proposal_id": row["proposal_id"],
                "seq": row["seq"],
                "kind": row["kind"],
                "fact_id": row["fact_id"],
                "revision": row["revision"],
                "body": row["body"],
                "arguments": canonical_loads(row["arguments_json"]) if row["arguments_json"] else None,
                "evidence_refs": canonical_loads(row["evidence_refs_json"]),
                "expires_at_ms": row["expires_at_ms"],
                "created_at_ms": row["created_at_ms"],
            }
            for row in rows
        ]

    def get_context_pack(self, context_ref: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT pack_json, content_hash FROM context_packs WHERE context_ref=?", (context_ref,)
        ).fetchone()
        if row is None:
            return None
        if _sha256_text(row["pack_json"]) != row["content_hash"]:
            raise PASError(ErrorCode.INTERNAL_ERROR, "context pack content hash mismatch", scope="runs")
        return canonical_loads(row["pack_json"])

    def list_runs(self, *, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if state is not None:
            rows = self.db.execute(
                """SELECT run_id, event_id, state, attempt, fence, error_class,
                          decision_summary, proposal_count, context_ref, deadline_ms
                   FROM runs WHERE state=? ORDER BY rowid LIMIT ?""",
                (state, int(limit)),
            ).fetchall()
        else:
            rows = self.db.execute(
                """SELECT run_id, event_id, state, attempt, fence, error_class,
                          decision_summary, proposal_count, context_ref, deadline_ms
                   FROM runs ORDER BY rowid LIMIT ?""",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            """SELECT run_id, event_id, state, attempt, fence, lease_until_ms, deadline_ms,
                      error_class, decision_summary, proposal_count, usage_json, context_ref
               FROM runs WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        usage_json = record.pop("usage_json")
        record["usage"] = canonical_loads(usage_json) if usage_json else None
        return record

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
