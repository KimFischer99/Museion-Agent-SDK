"""Hook runtime: sandbox runner, legacy protocol parser, staging + CAS
state machine (SPEC §6; P2 / HOOK-01).

The pipeline per invocation (SPEC §6.2, followed in this order):

1. ``Store.claim_hook`` leases the hook (fence bump) and returns the
   canonical state snapshot — one hook never has two parallel writers.
2. A fresh per-invocation staging directory is created and the canonical
   state is materialized into it; the child's ``HATCH_HOOK_STATE_DIR``
   points at staging, never at any production state directory. The
   staging state file is re-validated (regular file, not a symlink,
   bounded size, JSON object) after the child ran — a corrupt or
   tampered staging state is an error, never silently ``{}``, because
   silently resetting state can re-trigger first-run detections.
3. The child process runs in its own session/process group with an
   allowlisted environment (no inherited credentials), bounded pipes
   (throttled *while* reading, not buffered-then-checked), a wall-clock
   timeout and rlimit backstops. The sandbox layer wraps the spawn:
   on macOS the seatbelt profile denies network and file writes outside
   staging; on Linux bubblewrap is used when present. The plain
   subprocess sandbox provides neither and is refused unless the caller
   explicitly accepts unisolated execution for trusted local hooks.
4. ``Store.commit_hook_invocation`` commits everything in one
   transaction: invocation dedupe (idempotent replay / conflict),
   fence + version + enabled re-check, wake event admission, state
   update, ``disable_after_run``. A one-shot watch is disabled exactly
   when its wake event becomes durable — crash before the commit keeps
   the hook enabled with the old state (the retry re-detects); crash
   after it leaves event and queued run persisted. Stopping a probe
   never deletes pending notifications.
5. Staging is deleted after the commit; the Agent runs later (P3).

Failures — non-zero exit, timeout, oversize output, no/multiple/invalid
results, corrupt staging state — are recorded as hook errors with a
growing cooldown and never admit a wake event (SPEC §6.1: 失败不唤醒).
The cooldown throttles the failing probe's re-runs only; admin
diagnostics read the counters directly and are never throttled.

Honesty boundary (SPEC §6.3, AGENTS.md): ``HATCH_HOOK_DRY_RUN=1`` only
stops the legacy helper's own state write — it is not a security
boundary. Dry-run here additionally commits nothing to the database, but
a dry-run child can still touch the network and the filesystem wherever
the sandbox allows. Sandboxing is done by the platform facilities above,
not by environment variables.
"""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .contracts import ErrorCode, PASError, canonical_json, content_hash
from .pathsafe import PathSafetyError, safe_join
from .store import HookClaim, HookRecord, Store

__all__ = [
    "HookResult",
    "HookProtocolError",
    "HookSpec",
    "HookRunnerConfig",
    "HookRunReport",
    "HookRunSummary",
    "HookRunner",
    "HookSandbox",
    "PlainSubprocessSandbox",
    "SeatbeltSandbox",
    "BubblewrapSandbox",
    "platform_sandbox",
    "parse_hook_result",
    "parse_hook_logs",
    "hook_request_hash",
]

_RESULT_PREFIX = "HATCH_HOOK_RESULT:"
_LOG_PREFIX = "HATCH_HOOK_LOG:"
_HOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_ARGV_ITEMS = 64
_MAX_ARGV_ITEM_CHARS = 4096
_MAX_ARGV_TOTAL_CHARS = 65536
_KILL_GRACE_MS = 1000
_CHUNK_BYTES = 8192


class HookProtocolError(PASError):
    """A hook violated the legacy wire protocol or a runtime bound.

    ``error_class`` feeds the hook's error accounting; protocol
    violations must never be converted into wake decisions
    (SPEC §6.1)."""

    def __init__(self, safe_message: str, *, error_class: str = "hook_protocol_invalid") -> None:
        super().__init__(ErrorCode.INVALID_CONFIG, safe_message, scope="hooks")
        self.error_class = error_class


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HookProtocolError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise HookProtocolError(f"non-finite JSON number not allowed: {value!r}")


def strict_hook_json(text: str) -> Any:
    """Parse JSON strictly: duplicate keys and NaN/Infinity are protocol
    errors, not silent overwrites."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except HookProtocolError:
        raise
    except (ValueError, RecursionError) as exc:
        raise HookProtocolError(f"invalid JSON: {exc}") from exc


# --------------------------------------------------------------------------- #
# Legacy wire protocol parsing (SPEC §6.1)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HookResult:
    """One parsed terminal hook result."""

    decision: str
    reason: str
    payload: Any = None
    disable_after_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "payload": self.payload,
            "disable_after_run": self.disable_after_run,
        }


def parse_hook_result(
    stdout: bytes,
    exit_code: int,
    *,
    stdout_max_bytes: int = 65536,
    payload_max_bytes: int = 16384,
) -> HookResult:
    """Accept exactly one terminal ``HATCH_HOOK_RESULT`` line.

    Only exit code 0 with valid UTF-8/JSON is accepted; the result must
    be the last non-empty stdout line. Earlier diagnostic text is
    tolerated but never surfaced to the model (SPEC §6.1). Legacy mode
    keeps ``payload`` as any JSON value, bounded by ``payload_max_bytes``.
    """
    if exit_code != 0:
        raise HookProtocolError(
            f"hook exited with code {exit_code}", error_class="hook_exit_nonzero"
        )
    if len(stdout) > stdout_max_bytes:
        raise HookProtocolError(
            f"stdout exceeds {stdout_max_bytes} bytes", error_class="hook_output_oversize"
        )
    try:
        text = stdout.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise HookProtocolError("hook output must be UTF-8") from exc
    lines = text.splitlines()
    records = [
        (index, line[len(_RESULT_PREFIX):])
        for index, line in enumerate(lines)
        if line.startswith(_RESULT_PREFIX)
    ]
    if len(records) != 1:
        raise HookProtocolError(
            f"exactly one terminal result is required, found {len(records)}"
        )
    index, raw = records[0]
    if any(line.strip() for line in lines[index + 1:]):
        raise HookProtocolError("result must be the last non-empty stdout line")
    data = strict_hook_json(raw)
    allowed = {"decision", "reason", "payload", "disable_after_run"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise HookProtocolError("unexpected hook result fields")
    if data.get("decision") not in ("silent", "wake"):
        raise HookProtocolError("decision must be 'silent' or 'wake'")
    reason = data.get("reason")
    if not isinstance(reason, str) or len(reason) > 2048:
        raise HookProtocolError("reason must be a string of at most 2048 chars")
    disable = data.get("disable_after_run", False)
    if type(disable) is not bool:
        raise HookProtocolError("disable_after_run must be boolean")
    payload = data.get("payload")
    if payload is not None and len(canonical_json(payload).encode("utf-8")) > payload_max_bytes:
        raise HookProtocolError(
            f"payload exceeds {payload_max_bytes} bytes", error_class="hook_payload_oversize"
        )
    return HookResult(data["decision"], reason, payload, disable)


def parse_hook_logs(
    stderr: bytes,
    *,
    stderr_max_bytes: int = 65536,
    max_entries: int = 64,
    entry_max_bytes: int = 4096,
) -> tuple[dict[str, Any], ...]:
    """Collect structured ``HATCH_HOOK_LOG`` diagnostics from stderr.

    Logs are diagnostics only: they are bounded, never govern the
    outcome and never reach the model. Malformed or oversized entries
    are dropped, not fatal — the terminal result line governs.
    """
    if len(stderr) > stderr_max_bytes:
        return ()
    text = stderr.decode("utf-8", errors="replace")
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith(_LOG_PREFIX):
            continue
        if len(entries) >= max_entries:
            break
        try:
            data = strict_hook_json(line[len(_LOG_PREFIX):])
        except HookProtocolError:
            continue
        if isinstance(data, dict) and len(json.dumps(data).encode("utf-8")) <= entry_max_bytes:
            entries.append(data)
    return tuple(entries)


# --------------------------------------------------------------------------- #
# Hook definitions
# --------------------------------------------------------------------------- #


def hook_request_hash(
    hook_id: str,
    invocation_id: str,
    state_version: int,
    new_state: dict[str, Any],
    result: HookResult,
) -> str:
    """Content hash binding one invocation attempt: replaying the same
    invocation id with identical content is an idempotent no-op, with
    different content a conflict (SPEC §6.2)."""
    return content_hash(
        {
            "hook_id": hook_id,
            "invocation_id": invocation_id,
            "state_version": state_version,
            "new_state": new_state,
            **result.as_dict(),
        }
    )


@dataclass(frozen=True)
class HookSpec:
    """Typed hook definition. ``command`` is the full argv to execute —
    sourcing the legacy helper inside a ``bash -c`` script is the
    definition author's choice (the SDK never ships the helper).

    Definitions are immutable: changing behaviour means registering a
    new hook id."""

    hook_id: str
    command: tuple[str, ...]
    poll_seconds: int
    timeout_seconds: int | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.hook_id, str) or not _HOOK_ID_RE.fullmatch(self.hook_id):
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"hook_id {self.hook_id!r} fails naming rule", scope="hooks"
            )
        command = self.command
        if isinstance(command, str) or not isinstance(command, (tuple, list)):
            raise PASError(ErrorCode.INVALID_CONFIG, "command must be an argv tuple", scope="hooks")
        if not 1 <= len(command) <= _MAX_ARGV_ITEMS:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"command needs 1..{_MAX_ARGV_ITEMS} argv items",
                scope="hooks",
            )
        if any(not isinstance(item, str) or not item for item in command):
            raise PASError(
                ErrorCode.INVALID_CONFIG, "command items must be non-empty strings", scope="hooks"
            )
        if any(len(item) > _MAX_ARGV_ITEM_CHARS for item in command) or sum(
            len(item) for item in command
        ) > _MAX_ARGV_TOTAL_CHARS:
            raise PASError(ErrorCode.INVALID_CONFIG, "command exceeds size budget", scope="hooks")
        if not isinstance(self.poll_seconds, int) or isinstance(self.poll_seconds, bool):
            raise PASError(ErrorCode.INVALID_CONFIG, "poll_seconds must be an integer", scope="hooks")
        if self.poll_seconds < 1:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "poll_seconds must be >= 1", scope="hooks"
            )
        if self.timeout_seconds is not None:
            if (
                not isinstance(self.timeout_seconds, int)
                or isinstance(self.timeout_seconds, bool)
                or not 1 <= self.timeout_seconds <= 600
            ):
                raise PASError(
                    ErrorCode.INVALID_CONFIG, "timeout_seconds must be 1..600", scope="hooks"
                )
        if not isinstance(self.enabled, bool):
            raise PASError(ErrorCode.INVALID_CONFIG, "enabled must be boolean", scope="hooks")

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "command": list(self.command),
            "poll_seconds": self.poll_seconds,
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
        }

    def definition_hash(self) -> str:
        return content_hash(self.to_dict())


# --------------------------------------------------------------------------- #
# Sandbox layer (SPEC §6.3: the security boundary is the sandbox, not an
# environment variable)
# --------------------------------------------------------------------------- #


@runtime_checkable
class HookSandbox(Protocol):
    """Wraps a spawn argv with platform isolation facilities.

    ``provides_*`` are the sandbox's honest self-declarations; the
    runner refuses sandboxes that do not declare isolation when
    ``require_isolation`` is set."""

    provides_network_isolation: bool
    provides_file_write_isolation: bool

    def wrap(self, argv: list[str], *, staging_dir: Path) -> list[str]: ...

    def ensure_available(self) -> bool: ...


class PlainSubprocessSandbox:
    """Baseline spawn with no platform isolation.

    Provides process-group teardown, pipe bounds and rlimits (all in the
    runner itself) but neither network nor file-write isolation. Use only
    for trusted, locally-authored hooks — per AGENTS.md an uncontrolled
    host must never be used for security-relevant proactive execution."""

    provides_network_isolation: bool = False
    provides_file_write_isolation: bool = False

    def wrap(self, argv: list[str], *, staging_dir: Path) -> list[str]:
        return list(argv)

    def ensure_available(self) -> bool:
        return True


class SeatbeltSandbox:
    """macOS seatbelt (sandbox-exec) profile: deny network, deny file
    writes outside the invocation staging directory.

    sandbox-exec is deprecated by Apple but remains the only shipped
    per-process MAC facility; availability is probed lazily and the
    runner fails closed when the probe fails."""

    provides_network_isolation: bool = True
    provides_file_write_isolation: bool = True

    def __init__(self) -> None:
        self._available: bool | None = None

    @staticmethod
    def _profile_text(staging_dir: Path) -> str:
        resolved = str(staging_dir.resolve())
        escaped = resolved.replace("\\", "\\\\").replace('"', '\\"')
        return (
            "(version 1)\n"
            "(allow default)\n"
            "(deny network*)\n"
            "(deny file-write*)\n"
            f'(allow file-write* (subpath "{escaped}"))\n'
            '(allow file-write* (literal "/dev/null"))\n'
        )

    def wrap(self, argv: list[str], *, staging_dir: Path) -> list[str]:
        profile = staging_dir / ".sandbox.sb"
        profile.write_text(self._profile_text(staging_dir), encoding="utf-8")
        return ["sandbox-exec", "-f", str(profile), *argv]

    def ensure_available(self) -> bool:
        if self._available is None:
            if shutil.which("sandbox-exec") is None:
                self._available = False
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    probe_dir = Path(tmp) / "stage"
                    probe_dir.mkdir()
                    sandbox = SeatbeltSandbox()
                    try:
                        done = subprocess.run(
                            sandbox.wrap(["/usr/bin/true"], staging_dir=probe_dir),
                            capture_output=True,
                            timeout=10,
                        )
                        self._available = done.returncode == 0
                    except (OSError, subprocess.SubprocessError):
                        self._available = False
        return self._available


class BubblewrapSandbox:
    """Linux bubblewrap: network namespace + writable staging only.

    Implementation follows bwrap's documented flags; like the seatbelt
    path it probes availability lazily and fails closed. Verified by
    construction and argv tests — an end-to-end run on a Linux host is
    recorded separately (see VALIDATION.md)."""

    provides_network_isolation: bool = True
    provides_file_write_isolation: bool = True

    def __init__(self) -> None:
        self._available: bool | None = None

    def wrap(self, argv: list[str], *, staging_dir: Path) -> list[str]:
        resolved = str(staging_dir.resolve())
        return [
            "bwrap",
            "--unshare-net",
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--bind",
            resolved,
            resolved,
            "--",
            *argv,
        ]

    def ensure_available(self) -> bool:
        if self._available is None:
            if shutil.which("bwrap") is None:
                self._available = False
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    probe_dir = Path(tmp) / "stage"
                    probe_dir.mkdir()
                    try:
                        done = subprocess.run(
                            self.wrap(["/bin/true"], staging_dir=probe_dir),
                            capture_output=True,
                            timeout=10,
                        )
                        self._available = done.returncode == 0
                    except (OSError, subprocess.SubprocessError):
                        self._available = False
        return self._available


def platform_sandbox() -> HookSandbox:
    """Best sandbox for this platform; callers decide whether its
    isolation level is sufficient (``require_isolation``)."""
    if sys.platform == "darwin":
        return SeatbeltSandbox()
    if sys.platform.startswith("linux"):
        return BubblewrapSandbox()
    return PlainSubprocessSandbox()


# --------------------------------------------------------------------------- #
# Runner configuration and reports
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HookRunnerConfig:
    """Bounded defaults from SPEC §6.1. They are configurable suggested
    starting values, not facts about the original attachment."""

    timeout_ms: int = 5000
    stdout_max_bytes: int = 65536
    stderr_max_bytes: int = 65536
    payload_max_bytes: int = 16384
    state_max_bytes: int = 65536
    lease_ttl_ms: int = 30000
    backoff_base_ms: int = 30000
    backoff_max_ms: int = 3_600_000
    env_path: str | None = None
    fsize_rlimit_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        positive = (
            "timeout_ms",
            "stdout_max_bytes",
            "stderr_max_bytes",
            "payload_max_bytes",
            "state_max_bytes",
            "lease_ttl_ms",
            "backoff_base_ms",
            "backoff_max_ms",
            "fsize_rlimit_bytes",
        )
        for name in positive:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PASError(
                    ErrorCode.INVALID_CONFIG, f"{name} must be a positive integer", scope="hooks"
                )
        if self.timeout_ms > 600_000:
            raise PASError(ErrorCode.INVALID_CONFIG, "timeout_ms must be <= 600000", scope="hooks")
        if self.backoff_base_ms > self.backoff_max_ms:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "backoff_base_ms must be <= backoff_max_ms", scope="hooks"
            )


@dataclass(frozen=True)
class HookRunReport:
    """Outcome of one hook invocation attempt."""

    hook_id: str
    outcome: str
    invocation_id: str | None = None
    event_id: str | None = None
    decision: str | None = None
    reason: str | None = None
    payload: Any = None
    disable_after_run: bool | None = None
    error_class: str | None = None
    logs: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class HookRunSummary:
    """Aggregated pass over all due hooks."""

    reports: tuple[HookRunReport, ...] = ()

    @property
    def wakes(self) -> int:
        return sum(1 for r in self.reports if r.outcome == "committed_wake")

    @property
    def errors(self) -> int:
        return sum(1 for r in self.reports if r.outcome == "error")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": len(self.reports),
            "wakes": self.wakes,
            "errors": self.errors,
            "outcomes": sorted({r.outcome for r in self.reports}),
        }


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


class HookRunner:
    """Coordinates hook invocations against a :class:`Store`.

    Not thread-safe for concurrent ``run_hook`` calls on one instance
    (one flight at a time); independent runners on independent stores
    are excluded from each other by the hook lease fence. Importing or
    constructing never starts threads or processes; work happens in
    ``run_hook`` / ``run_due_hooks`` only.
    """

    def __init__(
        self,
        store: Store,
        staging_root: Path | str,
        *,
        sandbox: HookSandbox | None = None,
        require_isolation: bool = True,
        config: HookRunnerConfig | None = None,
    ) -> None:
        self.store = store
        self.clock = store.clock
        self.staging_root = Path(staging_root)
        self.sandbox = sandbox if sandbox is not None else platform_sandbox()
        self.require_isolation = require_isolation
        self.config = config if config is not None else HookRunnerConfig()
        if not isinstance(self.config, HookRunnerConfig):
            raise PASError(ErrorCode.INVALID_CONFIG, "config must be HookRunnerConfig", scope="hooks")
        if self.require_isolation and not (
            self.sandbox.provides_network_isolation and self.sandbox.provides_file_write_isolation
        ):
            raise PASError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "sandbox provides no network/file-write isolation;"
                " refusing isolation-required hook execution"
                " (pass require_isolation=False only for trusted local hooks)",
                scope="hooks",
            )
        self.staging_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Registration and sweeps
    # ------------------------------------------------------------------ #

    def register_hook(self, spec: HookSpec, *, now_ms: int | None = None) -> HookRecord:
        return self.store.register_hook(
            spec.hook_id,
            definition_hash=spec.definition_hash(),
            definition=spec.to_dict(),
            poll_interval_ms=spec.poll_seconds * 1000,
            timeout_ms=spec.timeout_seconds * 1000 if spec.timeout_seconds is not None else None,
            now_ms=now_ms,
        )

    def cleanup_staging(self) -> int:
        """Remove leftover staging directories.

        Call only while no invocation is in flight (e.g. coordinator
        startup after a crash): staged state is disposable by design —
        the database holds the canonical copy."""
        removed = 0
        for entry in self.staging_root.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def run_due_hooks(self, *, now_ms: int | None = None) -> HookRunSummary:
        now = self.clock.wall_now_ms() if now_ms is None else now_ms
        reports = [
            self.run_hook(hook_id, now_ms=now) for hook_id in self.store.due_hook_ids(now)
        ]
        return HookRunSummary(reports=tuple(reports))

    def run_hook(
        self, hook_id: str, *, now_ms: int | None = None, dry_run: bool = False, force: bool = False
    ) -> HookRunReport:
        """Run one hook through the full staging/CAS pipeline.

        ``dry_run`` executes the child with ``HATCH_HOOK_DRY_RUN=1`` and
        commits nothing (no state, no events, no error accounting) — it
        exists for testing hook definitions, and is not a security
        boundary (SPEC §6.3). ``force`` bypasses the error cooldown for
        admin-triggered diagnostic runs.
        """
        now = self.clock.wall_now_ms() if now_ms is None else now_ms
        record = self.store.get_hook(hook_id)
        if record is None:
            raise PASError(ErrorCode.INVALID_CONFIG, f"unknown hook {hook_id!r}", scope="hooks")
        if not record.enabled:
            return HookRunReport(hook_id=hook_id, outcome="skipped_disabled")
        if (
            not force
            and not dry_run
            and record.error_backoff_until_ms > now
        ):
            self.store.defer_hook(hook_id, next_due_ms=record.error_backoff_until_ms, now_ms=now)
            return HookRunReport(hook_id=hook_id, outcome="deferred_cooldown")
        if self.require_isolation and not self.sandbox.ensure_available():
            raise PASError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                f"sandbox {type(self.sandbox).__name__} unavailable on this platform;"
                " refusing to run hooks without isolation",
                scope="hooks",
            )
        claim = self.store.claim_hook(
            hook_id, now_ms=now, ttl_ms=self.config.lease_ttl_ms, force=force or dry_run
        )
        if claim is None:
            return HookRunReport(hook_id=hook_id, outcome="skipped_lease")
        return self._run_claim(claim, record, now=now, dry_run=dry_run)

    def _run_claim(
        self, claim: HookClaim, record: HookRecord, *, now: int, dry_run: bool
    ) -> HookRunReport:
        definition = claim.definition
        command = list(definition.get("command") or [])
        poll_ms = record.poll_interval_ms or int(definition.get("poll_seconds", 30)) * 1000
        timeout_ms = record.timeout_ms or self.config.timeout_ms
        invocation_id = f"{claim.hook_id}-v{claim.state_version}"
        staging_dir = Path(
            tempfile.mkdtemp(prefix=f"{claim.hook_id}-v{claim.state_version}-", dir=self.staging_root)
        )
        try:
            self._materialize_state(claim, staging_dir)
            env = self._child_env(claim, staging_dir, dry_run=dry_run)
            logs: tuple[dict[str, Any], ...] = ()
            try:
                outcome = self._spawn_bounded(
                    command, staging_dir=staging_dir, env=env, timeout_ms=timeout_ms
                )
                logs = parse_hook_logs(
                    outcome.stderr,
                    stderr_max_bytes=self.config.stderr_max_bytes,
                )
                if outcome.killed_reason is not None:
                    classes = {
                        "timeout": "hook_timeout",
                        "oversize_stdout": "hook_output_oversize",
                        "oversize_stderr": "hook_output_oversize",
                    }
                    raise HookProtocolError(
                        f"hook terminated: {outcome.killed_reason}",
                        error_class=classes[outcome.killed_reason],
                    )
                result = parse_hook_result(
                    outcome.stdout,
                    outcome.exit_code,
                    stdout_max_bytes=self.config.stdout_max_bytes,
                    payload_max_bytes=self.config.payload_max_bytes,
                )
                new_state = self._read_staged_state(claim, staging_dir)
            except HookProtocolError as exc:
                return self._record_error(
                    claim, record, exc, invocation_id, now=now, poll_ms=poll_ms, logs=logs
                )
            if dry_run:
                return HookRunReport(
                    hook_id=claim.hook_id,
                    outcome="dry_run",
                    invocation_id=invocation_id,
                    decision=result.decision,
                    reason=result.reason,
                    payload=result.payload,
                    disable_after_run=result.disable_after_run,
                    logs=logs,
                )
            request_hash = hook_request_hash(
                claim.hook_id, invocation_id, claim.state_version, new_state, result
            )
            commit = self.store.commit_hook_invocation(
                claim.hook_id,
                invocation_id,
                expected_state_version=claim.state_version,
                fence=claim.fence,
                request_hash=request_hash,
                new_state=new_state,
                decision=result.decision,
                reason=result.reason,
                payload=result.payload,
                disable_after_run=result.disable_after_run,
                next_due_ms=now + poll_ms,
                now_ms=now,
            )
            if commit.outcome in ("committed_wake", "committed_silent", "idempotent_replay"):
                return HookRunReport(
                    hook_id=claim.hook_id,
                    outcome=commit.outcome,
                    invocation_id=invocation_id,
                    event_id=commit.event_id,
                    decision=result.decision,
                    reason=result.reason,
                    payload=result.payload,
                    disable_after_run=result.disable_after_run,
                    logs=logs,
                )
            # Disable/fence/version won over in-flight work: nothing was
            # written and nothing is charged as an error.
            return HookRunReport(
                hook_id=claim.hook_id,
                outcome=f"discarded_{commit.outcome.removeprefix('skipped_')}",
                invocation_id=invocation_id,
                logs=logs,
            )
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def _record_error(
        self,
        claim: HookClaim,
        record: HookRecord,
        exc: HookProtocolError,
        invocation_id: str,
        *,
        now: int,
        poll_ms: int,
        logs: tuple[dict[str, Any], ...] = (),
    ) -> HookRunReport:
        """Count a failed invocation, apply the cooldown and never wake.

        Backoff doubles per consecutive failure, capped at
        ``backoff_max_ms``; ``next_due`` moves past the cooldown so the
        due scan leaves the failing probe alone (SPEC §6.1)."""
        attempt = record.consecutive_errors + 1
        backoff_ms = min(
            self.config.backoff_max_ms,
            self.config.backoff_base_ms * (2 ** min(attempt - 1, 20)),
        )
        self.store.record_hook_error(
            claim.hook_id,
            fence=claim.fence,
            error_class=exc.error_class,
            error_detail=exc.safe_message[:500],
            backoff_until_ms=now + backoff_ms,
            next_due_ms=now + max(poll_ms, backoff_ms),
            now_ms=now,
        )
        return HookRunReport(
            hook_id=claim.hook_id,
            outcome="error",
            invocation_id=invocation_id,
            error_class=exc.error_class,
            logs=logs,
        )

    # ------------------------------------------------------------------ #
    # Staging (HOOK-01)
    # ------------------------------------------------------------------ #

    def _materialize_state(self, claim: HookClaim, staging_dir: Path) -> None:
        """Write the canonical state snapshot into staging.

        Staging directories are created fresh per invocation, so the
        write cannot be diverted by a pre-planted symlink; the
        post-run read re-validates the file the child left behind."""
        path = safe_join(staging_dir, f"{claim.hook_id}.json")
        path.write_text(
            json.dumps(claim.state, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def _read_staged_state(self, claim: HookClaim, staging_dir: Path) -> dict[str, Any]:
        """Read the child's staged state with HOOK-01 validation.

        Absent file → the child did not touch state: canonical state
        stands. Present file → it must be a regular file (never a
        symlink), within the size budget and a JSON object; anything
        else is a tamper/corruption error, never silently ``{}``."""
        try:
            path = safe_join(staging_dir, f"{claim.hook_id}.json")
        except PathSafetyError as exc:
            raise HookProtocolError(
                f"staged hook state escaped staging containment: {exc}",
                error_class="hook_state_invalid",
            ) from exc
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return claim.state
        if stat.S_ISLNK(info.st_mode):
            raise HookProtocolError(
                "staged hook state is a symlink", error_class="hook_state_invalid"
            )
        if not stat.S_ISREG(info.st_mode):
            raise HookProtocolError(
                "staged hook state is not a regular file", error_class="hook_state_invalid"
            )
        try:
            with open(path, "rb") as handle:
                raw = handle.read(self.config.state_max_bytes + 1)
            if len(raw) > self.config.state_max_bytes:
                raise HookProtocolError(
                    f"staged hook state exceeds {self.config.state_max_bytes} bytes",
                    error_class="hook_state_invalid",
                )
            data = strict_hook_json(raw.decode("utf-8", errors="strict"))
        except UnicodeError as exc:
            raise HookProtocolError(
                "staged hook state must be UTF-8", error_class="hook_state_invalid"
            ) from exc
        except HookProtocolError as exc:
            raise HookProtocolError(
                f"staged hook state is invalid: {exc.safe_message}",
                error_class="hook_state_invalid",
            ) from exc
        if not isinstance(data, dict):
            raise HookProtocolError(
                "staged hook state must be a JSON object", error_class="hook_state_invalid"
            )
        return data

    def _child_env(self, claim: HookClaim, staging_dir: Path, *, dry_run: bool) -> dict[str, str]:
        """Allowlisted child environment — host credentials are never
        inherited (AGENTS.md). ``scratch`` catches stray HOME/TMPDIR
        writes inside the (sandbox-restricted) staging tree."""
        scratch = staging_dir / "scratch"
        scratch.mkdir(exist_ok=True)
        env_path = self.config.env_path or os.environ.get("PATH") or os.defpath
        return {
            "PATH": env_path,
            "HOME": str(scratch),
            "TMPDIR": str(scratch),
            "HATCH_HOOK_STATE_DIR": str(staging_dir),
            "HATCH_HOOK_ID": claim.hook_id,
            "HATCH_HOOK_INVOCATION_ID": f"{claim.hook_id}-v{claim.state_version}",
            "HATCH_HOOK_DRY_RUN": "1" if dry_run else "0",
        }

    # ------------------------------------------------------------------ #
    # Bounded subprocess (SPEC §6.1: throttle while reading; kill the
    # process group on timeout)
    # ------------------------------------------------------------------ #

    @dataclass
    class _SpawnOutcome:
        exit_code: int | None = None
        stdout: bytes = b""
        stderr: bytes = b""
        killed_reason: str | None = None

    def _spawn_bounded(
        self, command: list[str], *, staging_dir: Path, env: dict[str, str], timeout_ms: int
    ) -> "HookRunner._SpawnOutcome":
        argv = self.sandbox.wrap(command, staging_dir=staging_dir)
        outcome = self._SpawnOutcome()

        def apply_rlimits() -> None:  # child-side, after fork before exec
            limit = self.config.fsize_rlimit_bytes
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
            cpu = max(1, timeout_ms // 1000 + 1)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))

        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                cwd=str(staging_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                preexec_fn=apply_rlimits,
            )
        except OSError as exc:
            raise HookProtocolError(
                f"cannot spawn hook: {exc.strerror or exc}", error_class="hook_spawn_failed"
            ) from exc

        over_limit = threading.Event()
        limits = {"stdout": self.config.stdout_max_bytes, "stderr": self.config.stderr_max_bytes}
        buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}

        def drain(pipe: Any, name: str) -> None:
            limit = limits[name]
            buffer = buffers[name]
            while True:
                try:
                    chunk = pipe.read1(_CHUNK_BYTES)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                if len(buffer) + len(chunk) > limit:
                    over_limit.set()
                    outcome.killed_reason = f"oversize_{name}"
                    return
                buffer.extend(chunk)

        assert proc.stdout is not None and proc.stderr is not None
        threads = [
            threading.Thread(target=drain, args=(proc.stdout, "stdout"), daemon=True),
            threading.Thread(target=drain, args=(proc.stderr, "stderr"), daemon=True),
        ]
        for thread in threads:
            thread.start()

        # The kill deadline runs on the real monotonic clock: it measures
        # actual child runtime, which is infrastructure time, not profile
        # logical time — injected Clocks that freeze or roll back wall
        # time (and fakes with a frozen monotonic) must never stretch a
        # hung child's timeout.
        deadline = time.monotonic_ns() // 1_000_000 + timeout_ms
        timed_out = False
        while proc.poll() is None:
            if over_limit.is_set():
                break
            remaining = deadline - time.monotonic_ns() // 1_000_000
            if remaining <= 0:
                timed_out = True
                break
            over_limit.wait(min(remaining, 50) / 1000)
        if timed_out:
            outcome.killed_reason = "timeout"
        if outcome.killed_reason is not None:
            self._kill_group(proc)
        for thread in threads:
            thread.join(timeout=5)
        outcome.exit_code = proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        outcome.stdout = bytes(buffers["stdout"])
        outcome.stderr = bytes(buffers["stderr"])
        return outcome

    @staticmethod
    def _kill_group(proc: subprocess.Popen[Any]) -> None:
        """Terminate the whole process group: hooks may leave
        descendants and only a group kill reclaims them (SPEC §6.1)."""
        try:
            group = os.getpgid(proc.pid)
        except OSError:
            group = None
        if group is not None:
            try:
                os.killpg(group, signal.SIGTERM)
            except OSError:
                pass
        try:
            proc.wait(timeout=_KILL_GRACE_MS / 1000)
            return
        except subprocess.TimeoutExpired:
            pass
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
