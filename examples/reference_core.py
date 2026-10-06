"""Original, dependency-free reference slice; NOT a complete production SDK.

Implemented: strict legacy hook parsing, interval coalescing, local-time policy,
atomic hook-state/event admission, fenced read-only runs, transactional outbox.
Deliberately absent: real sandbox, real connectors, daemon, approvals, DST cron.
This single-profile example uses SQLite DELETE journal, not WAL. See SPEC.md.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


class ContractError(ValueError):
    """Input violates a protocol or safety contract."""


class Conflict(RuntimeError):
    """Idempotency mismatch, invalid transition, or stale fence."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(*parts: Any) -> str:
    return hashlib.sha256(canonical(parts).encode("utf-8")).hexdigest()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(text: str) -> Any:
    def reject(value: str) -> None:
        raise ContractError(f"Non-finite JSON number: {value}")
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys,
                          parse_constant=reject)
    except (ValueError, RecursionError) as exc:
        raise ContractError("Invalid JSON") from exc


@dataclass(frozen=True)
class HookResult:
    decision: str
    reason: str
    payload: Any = None
    disable_after_run: bool = False


def parse_hook_output(stdout: bytes, exit_code: int, *, max_bytes: int = 65536) -> HookResult:
    """Accept exactly one HATCH_HOOK_RESULT terminal line; never wake on errors.

    A process runner must cap stdout while reading, not after buffering everything.
    It must also bound stderr, timeout, kill descendants, and isolate the process.
    """
    if exit_code != 0 or len(stdout) > max_bytes:
        raise ContractError("Hook failed or exceeded its output budget")
    try:
        lines = stdout.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as exc:
        raise ContractError("Hook output must be UTF-8") from exc
    prefix = "HATCH_HOOK_RESULT:"
    records = [(i, line[len(prefix):]) for i, line in enumerate(lines)
               if line.startswith(prefix)]
    if len(records) != 1:
        raise ContractError("Exactly one terminal result is required")
    index, text = records[0]
    if any(line.strip() for line in lines[index + 1:]):
        raise ContractError("Result must be the last non-empty stdout line")
    data = strict_json(text)
    allowed = {"decision", "reason", "payload", "disable_after_run"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ContractError("Unexpected hook result fields")
    if data.get("decision") not in ("silent", "wake"):
        raise ContractError("Unsupported decision")
    if not isinstance(data.get("reason"), str) or len(data["reason"]) > 2048:
        raise ContractError("Invalid reason")
    disable = data.get("disable_after_run", False)
    if type(disable) is not bool:
        raise ContractError("disable_after_run must be boolean")
    # Legacy payload intentionally accepts any valid JSON, not only objects.
    return HookResult(data["decision"], data["reason"], data.get("payload"), disable)


def normalized_skill_name(original: str) -> str:
    """Caller must additionally reject collisions and match the export folder."""
    result = re.sub(r"[-_]+", "-", original.lower()).strip("-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", result) or len(result) > 64:
        raise ContractError("Name needs explicit human mapping")
    return result


@dataclass(frozen=True)
class IntervalSlot:
    due_at: int | None
    next_at: int


def coalesce_interval(anchor: int, every: int, now: int, last_slot: int | None) -> IntervalSlot:
    """Integer UTC seconds. Coalesce missed heartbeats to the latest anchored slot."""
    if any(type(x) is not int for x in (anchor, every, now)) or every <= 0:
        raise ContractError("Intervals require integer seconds and positive period")
    if last_slot is not None:
        if type(last_slot) is not int or last_slot < anchor or (last_slot - anchor) % every:
            raise ContractError("last_slot is not an anchored occurrence")
    if now < anchor:
        return IntervalSlot(None, max(anchor, (last_slot + every) if last_slot is not None else anchor))
    latest = anchor + ((now - anchor) // every) * every
    if last_slot is not None and latest <= last_slot:
        return IntervalSlot(None, last_slot + every)
    return IntervalSlot(latest, latest + every)


def delivery_allowed(now: datetime, zone: str, start_minute: int, end_minute: int) -> bool:
    """Half-open local-time window; equal bounds mean disabled, not 24 hours.

    Both repeated fall-back hours are subject to the same local-time rule.
    Generating daily/weekly/monthly occurrences is a separate production concern.
    """
    if now.tzinfo is None or not (0 <= start_minute < 1440 and 0 <= end_minute < 1440):
        raise ContractError("An aware datetime and valid minute bounds are required")
    minute = now.astimezone(ZoneInfo(zone)).hour * 60 + now.astimezone(ZoneInfo(zone)).minute
    if start_minute == end_minute:
        return False
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    return minute >= start_minute or minute < end_minute


@dataclass(frozen=True)
class RunLease:
    run_id: str
    event_id: str
    token: int
    until: int


@dataclass(frozen=True)
class SelfNotification:
    fact_id: str
    revision: str
    body: str
    expires_at: int


@dataclass(frozen=True)
class DeliveryLease:
    message_id: str
    token: int
    recipient: str
    body: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hooks (
 hook_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0,
 state_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL, observed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS probe_commits (
 invocation_id TEXT PRIMARY KEY, hook_id TEXT NOT NULL REFERENCES hooks(hook_id),
 request_hash TEXT NOT NULL, event_id TEXT REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
 state TEXT NOT NULL CHECK(state IN ('queued','running','planned')),
 token INTEGER NOT NULL DEFAULT 0, lease_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outbox (
 message_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
 delivery_key TEXT NOT NULL UNIQUE, recipient TEXT NOT NULL, body TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','sending','delivered','unknown','expired')),
 token INTEGER NOT NULL DEFAULT 0, lease_until INTEGER NOT NULL DEFAULT 0,
 expires_at INTEGER NOT NULL, receipt TEXT
);
"""


class Ledger:
    """One profile and one SQLite connection per instance; not thread-shareable.

    All lease times are supplied by an injected clock in seconds. This example is
    safe to reclaim only because run workers perform read-only decision work.
    A production broker must fence every tool/effect, not only final completion.
    """
    def __init__(self, path: str | Path, *, profile: str, owner_destination: str):
        if not profile or not owner_destination:
            raise ContractError("Profile and owner destination are mandatory")
        self.profile = profile
        self.owner_destination = owner_destination
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        try:
            with self.transaction():
                identity = canonical({"profile": profile, "owner": owner_destination})
                row = self.db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
                if row is not None and row[0] != identity:
                    raise Conflict("Refusing to reuse another profile's database")
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('identity',?)", (identity,))
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

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

    def register_hook(self, hook_id: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", hook_id):
            raise ContractError("Unsafe hook ID")
        self.db.execute("INSERT OR IGNORE INTO hooks(hook_id) VALUES (?)", (hook_id,))

    def _admit_event(self, dedupe_key: str, payload: Any, now: int) -> str:
        event_id = digest(self.profile, dedupe_key)
        encoded = canonical(payload)
        row = self.db.execute("SELECT payload_json FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is not None and row[0] != encoded:
            raise Conflict("The same event key was reused with different content")
        self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                        (event_id, dedupe_key, encoded, now))
        self.db.execute("INSERT OR IGNORE INTO runs(run_id,event_id,state) VALUES (?,?,'queued')",
                        (digest("run", event_id), event_id))
        return event_id

    def admit_event(self, dedupe_key: str, payload: Any, now: int) -> str:
        with self.transaction():
            return self._admit_event(dedupe_key, payload, now)

    def commit_probe(self, hook_id: str, invocation_id: str, expected_version: int,
                     new_state: dict[str, Any], result: HookResult, now: int,
                     *, dry_run: bool = False) -> str | None:
        if type(new_state) is not dict or not invocation_id:
            raise ContractError("Expected a state object and invocation ID")
        if result.decision not in ("silent", "wake") or type(result.disable_after_run) is not bool:
            raise ContractError("Invalid hook result")
        encoded = canonical(new_state)
        request_hash = digest(hook_id, expected_version, new_state, result.__dict__)
        if dry_run:
            return None
        with self.transaction():
            prior = self.db.execute("SELECT * FROM probe_commits WHERE invocation_id=?", (invocation_id,)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise Conflict("Invocation replay has different content")
                return prior["event_id"]
            row = self.db.execute("SELECT * FROM hooks WHERE hook_id=?", (hook_id,)).fetchone()
            if row is None or not row["enabled"] or row["version"] != expected_version:
                raise Conflict("Hook is disabled, missing, or its state version changed")
            event_id = None
            if result.decision == "wake":
                event_id = self._admit_event("hook:" + hook_id + ":" + invocation_id,
                                             {"hook_id": hook_id, "reason": result.reason,
                                              "payload": result.payload}, now)
            self.db.execute("UPDATE hooks SET version=version+1,state_json=?,enabled=? WHERE hook_id=?",
                            (encoded, int(not result.disable_after_run), hook_id))
            self.db.execute("INSERT INTO probe_commits VALUES (?,?,?,?)",
                            (invocation_id, hook_id, request_hash, event_id))
            return event_id

    def claim_run(self, now: int, *, ttl: int = 60) -> RunLease | None:
        if ttl <= 0:
            raise ContractError("Lease TTL must be positive")
        with self.transaction():
            row = self.db.execute("""SELECT * FROM runs WHERE state='queued'
                OR (state='running' AND lease_until<=?) ORDER BY rowid LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            token = row["token"] + 1
            self.db.execute("UPDATE runs SET state='running',token=?,lease_until=? WHERE run_id=?",
                            (token, now + ttl, row["run_id"]))
            return RunLease(row["run_id"], row["event_id"], token, now + ttl)

    def complete_run(self, lease: RunLease, proposals: list[SelfNotification], now: int) -> None:
        with self.transaction():
            row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (lease.run_id,)).fetchone()
            if row is None or row["state"] != "running" or row["token"] != lease.token or row["lease_until"] <= now:
                raise Conflict("Run fence expired or is no longer current")
            for proposal in proposals:
                if not proposal.fact_id or not proposal.revision or not proposal.body or proposal.expires_at <= now:
                    raise ContractError("Notification lacks identity, content, or freshness")
                # The model cannot supply an arbitrary destination. Same fact + revision
                # + destination is one notification even across different wake-up runs.
                key = digest(self.profile, proposal.fact_id, proposal.revision,
                             self.owner_destination, "notify_self")
                self.db.execute("""INSERT OR IGNORE INTO outbox
                    (message_id,run_id,delivery_key,recipient,body,state,expires_at)
                    VALUES (?,?,?,?,?,'pending',?)""",
                    (digest("message", key), lease.run_id, key, self.owner_destination,
                     proposal.body, proposal.expires_at))
            self.db.execute("UPDATE runs SET state='planned',lease_until=0 WHERE run_id=?", (lease.run_id,))

    def claim_delivery(self, now: int, *, allowed: bool, ttl: int = 30) -> DeliveryLease | None:
        """Caller must recompute real grants/quiet hours/consent just before send.

        The boolean here is an injected policy result, not a production policy engine.
        A stale sending lease becomes UNKNOWN, never automatically PENDING.
        """
        if ttl <= 0:
            raise ContractError("Lease TTL must be positive")
        with self.transaction():
            self.db.execute("UPDATE outbox SET state='unknown' WHERE state='sending' AND lease_until<=?", (now,))
            self.db.execute("UPDATE outbox SET state='expired' WHERE state='pending' AND expires_at<=?", (now,))
            if not allowed:
                return None
            row = self.db.execute("SELECT * FROM outbox WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            token = row["token"] + 1
            self.db.execute("UPDATE outbox SET state='sending',token=?,lease_until=? WHERE message_id=?",
                            (token, now + ttl, row["message_id"]))
            return DeliveryLease(row["message_id"], token, row["recipient"], row["body"])

    def record_receipt(self, lease: DeliveryLease, receipt: str) -> None:
        if not receipt:
            raise ContractError("A provider receipt or durable local-inbox commit is required")
        # A late but definitive receipt may resolve UNKNOWN for the same attempt.
        with self.transaction():
            cursor = self.db.execute("""UPDATE outbox SET state='delivered',receipt=?
                WHERE message_id=? AND token=? AND state IN ('sending','unknown')""",
                (receipt, lease.message_id, lease.token))
            if cursor.rowcount != 1:
                raise Conflict("Receipt does not match an unresolved attempt")

    def mark_delivery_unknown(self, lease: DeliveryLease) -> None:
        with self.transaction():
            cursor = self.db.execute("""UPDATE outbox SET state='unknown'
                WHERE message_id=? AND token=? AND state='sending'""", (lease.message_id, lease.token))
            if cursor.rowcount != 1:
                raise Conflict("Cannot mark this attempt unknown")

    def reconcile(self, lease: DeliveryLease, *, receipt: str | None = None,
                  authoritative_not_sent: bool = False) -> None:
        """Only a trusted reconciler may invoke this, NEVER the model.

        not_sent requires proof the old sender terminated AND the provider did not
        accept the message. Mere timeout or a missing search result is insufficient.
        """
        if bool(receipt) == bool(authoritative_not_sent):
            raise ContractError("Provide exactly one authoritative reconciliation result")
        with self.transaction():
            new_state = "delivered" if receipt else "pending"
            cursor = self.db.execute("""UPDATE outbox SET state=?,receipt=?,token=token+1
                WHERE message_id=? AND token=? AND state='unknown'""",
                (new_state, receipt, lease.message_id, lease.token))
            if cursor.rowcount != 1:
                raise Conflict("Unknown attempt has changed")

    def count(self, table: str, state: str | None = None) -> int:
        if table not in {"events", "runs", "outbox", "probe_commits"}:
            raise ContractError("Unknown table")
        if state is not None:
            if table not in {"runs", "outbox"}:
                raise ContractError("Table has no state column")
            return self.db.execute(f"SELECT count(*) FROM {table} WHERE state=?", (state,)).fetchone()[0]
        return self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


if __name__ == "__main__":
    ledger = Ledger(":memory:", profile="demo", owner_destination="local-inbox:demo")
    ledger.register_hook("sample")
    ledger.commit_probe("sample", "poll-1", 0, {"revision": "v1"},
                        HookResult("wake", "A configured source changed", {"id": "item-1"}), 100)
    lease = ledger.claim_run(101)
    assert lease is not None
    # This stands in for a bounded read-only AgentExecutor. It does not call an LLM.
    ledger.complete_run(lease, [SelfNotification("item-1", "v1", "A source changed.", 1000)], 102)
    print(canonical({"events": ledger.count("events"), "runs": ledger.count("runs", "planned"),
                     "pending_notifications": ledger.count("outbox", "pending"),
                     "delivered_notifications": ledger.count("outbox", "delivered")}))
    ledger.close()
