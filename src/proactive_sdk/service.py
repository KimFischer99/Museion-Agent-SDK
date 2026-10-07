"""``pas`` — the PAS command line (SPEC §14.1 daemon 模式 / P7 CLI 帮助).

Command groups:

- ``serve`` / ``tick``   run the daemon (foreground) or one full pass.
  These need an *app module* (``--app module:factory``) that wires the
  executor, sources and sinks into a :class:`ProactiveAgent` — the CLI
  owns the event loop, the user module owns the wiring (SPEC §14.1:
  daemon 模式由 pas serve 拥有).
- ``doctor`` / ``status`` environment and instance checks (no executor
  needed; doctor never creates state).
- ``jobs`` / ``runs`` / ``approvals`` / ``notifications`` / ``skills``
  / ``hooks`` operational commands against one state directory.
- ``backup`` / ``restore`` / ``export`` / ``delete-data`` data safety
  (restore and delete-data are destructive and demand ``--yes``).
- ``config`` / ``version`` / ``rpc`` configuration display, version and
  a raw control-plane client for smoke checks.

The CLI is a thin adapter: every mutation goes through the same facade
/ store code paths the daemon uses, with the same fencing, so running
``pas jobs pause`` against a live daemon is safe (SQLite locking +
revision checks), not a second scheduler owner.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .backup import read_backup_meta
from .config import PasConfig, config_to_yaml_subset, load_config
from .contracts import ErrorCode, PASError
from .facade import DB_FILENAME, LOCK_FILENAME, SOCKET_FILENAME, Job, ProactiveAgent
from .observability import free_disk_mb

__all__ = ["main", "build_parser"]

_DELETE_PHRASE = "DELETE PROFILE DATA"


def _common_options(parser: argparse.ArgumentParser) -> None:
    """Options accepted both before and after the subcommand."""
    parser.add_argument("--state-dir", default=argparse.SUPPRESS,
                        help="profile state directory (default from --config)")
    parser.add_argument("--config", default=argparse.SUPPRESS,
                        help="config file (accepted YAML subset, SPEC §14.3)")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output where supported")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pas",
        description="Proactive personal agent runtime (PAS) — one profile, one state directory.",
        epilog=(
            "serve/tick need an app module that builds a ProactiveAgent "
            "(--app module:factory). All other commands operate on the state "
            "directory directly and are safe alongside a running daemon."
        ),
    )
    parser.add_argument("--version", action="store_true", help="print the PAS version and exit")
    _common_options(parser)
    sub = parser.add_subparsers(dest="command")

    def add(name: str, *, help_text: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name, help=help_text)
        _common_options(child)
        return child

    serve = add("serve", help_text="run the daemon in the foreground (supervisor-friendly)")
    serve.add_argument("--app", required=True, help="module:factory returning a ProactiveAgent")
    serve.add_argument("--grace", type=float, default=None, help="shutdown grace seconds override")

    tick = add("tick", help_text="run one admission→analysis→delivery pass, then exit")
    tick.add_argument("--app", required=True, help="module:factory returning a ProactiveAgent")
    tick.add_argument("--max-runs", type=int, default=None, help="max runs to process in this pass")

    doctor = add("doctor", help_text="check environment, state dir, locks, socket and config")
    doctor.add_argument("--deep", action="store_true", help="also probe the control-plane socket")

    add("status", help_text="print a status summary (jobs/runs/outbox/health)")

    jobs = add("jobs", help_text="job operations: list|show|create|pause|resume|delete|trigger")
    jobs.add_argument("action", choices=["list", "show", "create", "pause", "resume", "delete", "trigger"])
    jobs.add_argument("job_id", nargs="?", help="job id (for show/pause/resume/delete/trigger)")
    jobs.add_argument("--mode", choices=["heartbeat", "task"], default="heartbeat")
    jobs.add_argument("--schedule-json", help="schedule object as JSON, e.g. {\"kind\":\"interval\",...}")
    jobs.add_argument("--instruction", help="task instruction (1..10000 chars)")
    jobs.add_argument("--grant-ref", action="append", default=[], help="grant ref (repeatable)")
    jobs.add_argument("--idempotency-key", help="idempotency key for create")
    jobs.add_argument("--enabled", dest="enabled", action="store_true", default=argparse.SUPPRESS)
    jobs.add_argument("--disabled", dest="enabled", action="store_false", default=argparse.SUPPRESS)

    runs = add("runs", help_text="run operations: list|show|cancel|events")
    runs.add_argument("action", choices=["list", "show", "cancel", "events"])
    runs.add_argument("run_id", nargs="?")
    runs.add_argument("--state", help="filter by run state (list)")
    runs.add_argument("--limit", type=int, default=20)

    approvals = add("approvals", help_text="frozen-parameter approvals: list|resolve")
    approvals.add_argument("action", choices=["list", "resolve"])
    approvals.add_argument("approval_id", nargs="?")
    approvals.add_argument("--approve", dest="approve", action="store_true", default=argparse.SUPPRESS)
    approvals.add_argument("--deny", dest="approve", action="store_false", default=argparse.SUPPRESS)

    notes = add("notifications", help_text="personal inbox: list|read|feedback")
    notes.add_argument("action", choices=["list", "read", "feedback"])
    notes.add_argument("inbox_id", nargs="?")
    notes.add_argument("--unread-only", action="store_true")
    notes.add_argument("--kind", required=False, help="feedback kind (mute_topic|unmute_topic|handled|seen)")
    notes.add_argument("--topic", help="topic for mute_topic/unmute_topic feedback")
    notes.add_argument("--actor", default="cli", help="authenticated actor string recorded with feedback")

    skills = add("skills", help_text="skill compatibility layer: audit|import|explain")
    skills.add_argument("action", choices=["audit", "import", "explain"])
    skills.add_argument("name", nargs="?", help="canonical skill name (explain)")
    skills.add_argument("--source", help="skills source directory (default from config)")

    hooks = add("hooks", help_text="hook runtime: list|enable|disable|run")
    hooks.add_argument("action", choices=["list", "enable", "disable", "run"])
    hooks.add_argument("hook_id", nargs="?")

    backup_cmd = add("backup", help_text="create a consistent backup of the profile database")
    backup_cmd.add_argument("path", help="backup file path (0600, plus .meta.json sidecar)")

    restore_cmd = add("restore", help_text="restore the profile database from a backup (destructive)")
    restore_cmd.add_argument("path", help="backup file path")
    restore_cmd.add_argument("--yes", action="store_true", help="confirm the destructive replace")

    export_cmd = add("export", help_text="export profile data as JSON (data portability)")
    export_cmd.add_argument("path", help="output JSON file")

    delete_cmd = add("delete-data", help_text="wipe all profile data rows (destructive)")
    delete_cmd.add_argument("--yes", action="store_true", help="confirm the deletion")

    config_cmd = add("config", help_text="config operations: check|print")
    config_cmd.add_argument("action", choices=["check", "print"])

    rpc_cmd = add("rpc", help_text="raw control-plane client (smoke checks)")
    rpc_cmd.add_argument("--method", required=True, help="JSON-RPC method, e.g. system.health")
    rpc_cmd.add_argument("--params", default="{}", help="params as JSON")
    rpc_cmd.add_argument("--token", help="control-plane token (else same-UID peer trust)")
    rpc_cmd.add_argument("--socket", help="socket path override")

    add("version", help_text="print version and protocol information")
    return parser


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _load_config(args: argparse.Namespace) -> PasConfig | None:
    path = getattr(args, "config", None)
    if path:
        return load_config(path)
    return None


def _state_dir(args: argparse.Namespace, config: PasConfig | None) -> Path:
    state_dir = getattr(args, "state_dir", None)
    if state_dir:
        return Path(state_dir).expanduser()
    if config is not None:
        return Path(config.state_dir).expanduser()
    raise PASError(
        ErrorCode.INVALID_CONFIG,
        "no state directory: pass --state-dir or --config",
        scope="cli",
    )


def _open_agent(args: argparse.Namespace, config: PasConfig | None) -> ProactiveAgent:
    """Open the facade for operational commands. The CLI never runs the
    model loop, so the executor is a fail-closed stub: any run attempt
    through this instance raises instead of pretending."""
    state_dir = _state_dir(args, config)
    profile = config.profile if config is not None else "personal"
    timezone = config.timezone if config is not None else "UTC"
    locale = config.locale if config is not None else "en"
    return ProactiveAgent(
        state_dir=state_dir,
        executor=_ControlOnlyGuard(),
        timezone=timezone,
        locale=locale,
        profile=profile,
        config=config,
    )


class _ControlOnlyGuard:
    """Executor placeholder for operational CLI commands. Existence of a
    run claim through a CLI-only instance is a bug — fail loudly."""

    broker = type("B", (), {"capabilities": frozenset(), "tool_names": staticmethod(lambda: ())})()

    @property
    def config(self):  # pragma: no cover - shape only
        raise PASError(ErrorCode.INVALID_CONFIG, "CLI instance cannot execute runs", scope="cli")

    @property
    def capabilities(self):
        return frozenset()

    async def execute(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - never called by ops
        raise PASError(ErrorCode.INVALID_CONFIG, "CLI instance cannot execute runs", scope="cli")


def _load_app(args: argparse.Namespace) -> ProactiveAgent:
    if ":" not in (args.app or ""):
        raise PASError(
            ErrorCode.INVALID_CONFIG, f"--app must be module:factory, got {args.app!r}", scope="cli"
        )
    module_name, attr = args.app.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, attr)
    except (ImportError, AttributeError) as exc:
        raise PASError(
            ErrorCode.INVALID_CONFIG, f"cannot load app {args.app!r}: {exc}", scope="cli"
        ) from None
    config = _load_config(args)
    try:
        agent = factory(config)
    except TypeError:
        agent = factory()
    if not isinstance(agent, ProactiveAgent):
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            f"app factory {args.app!r} did not return a ProactiveAgent",
            scope="cli",
        )
    return agent


def _json_flag(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True, default=str))


# --------------------------------------------------------------------------- #
# Command implementations
# --------------------------------------------------------------------------- #

def _cmd_serve(args: argparse.Namespace) -> int:
    agent = _load_app(args)
    try:
        asyncio.run(agent.serve())
    except KeyboardInterrupt:  # pragma: no cover — signal path handles drain
        pass
    return 0


def _cmd_tick(args: argparse.Namespace) -> int:
    async def _run() -> dict:
        agent = _load_app(args)
        try:
            report = await agent.tick(max_runs=args.max_runs)
            return report
        finally:
            await agent.close()

    report = asyncio.run(_run())
    _print(report, _json_flag(args))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    import platform

    py_ok = sys.version_info >= (3, 11)
    check("python", py_ok, f"{platform.python_version()} (need >= 3.11)")
    check("sqlite_runtime", True, f"linked SQLite {sqlite3.sqlite_version}")
    conn = sqlite3.connect(":memory:")
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.close()
    check("sqlite_foreign_keys", fk in (0, 1), f"PRAGMA foreign_keys={fk}")

    try:
        import zoneinfo

        zoneinfo.ZoneInfo("Europe/Berlin")
        check("zoneinfo_data", True, "ZoneInfo Europe/Berlin loads")
    except Exception as exc:  # noqa: BLE001
        check("zoneinfo_data", False, f"zoneinfo data missing: {exc}")

    config = _load_config(args)
    config_arg = getattr(args, "config", None)
    if config_arg:
        check("config", config is not None, str(config_arg))

    state_dir: Path | None = None
    try:
        state_dir = _state_dir(args, config)
        check("state_dir", state_dir.is_dir(), str(state_dir))
    except PASError as exc:
        check("state_dir", False, exc.safe_message)

    db_path = None
    if state_dir is not None:
        db_path = state_dir / DB_FILENAME
        if not db_path.is_file():
            check("database", True, "no database yet (first run creates it)")
        else:
            try:
                conn = sqlite3.connect(str(db_path))
                row = conn.execute("PRAGMA integrity_check").fetchone()
                version_row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                conn.close()
                check(
                    "database",
                    row is not None and row[0] == "ok",
                    f"integrity={row[0] if row else 'unknown'} schema_version={version_row[0] if version_row else '?'}",
                )
            except sqlite3.Error as exc:
                check("database", False, f"cannot open: {exc}")
        lock = state_dir / LOCK_FILENAME
        if lock.is_file():
            try:
                text = lock.read_text().strip()
                pid = int(text.split("pid=")[-1]) if "pid=" in text else -1
                os.kill(pid, 0)
                check("instance_lock", True, f"daemon alive (pid {pid})")
            except (ProcessLookupError, ValueError):
                check("instance_lock", True, "stale lock file (safe to remove)")
            except OSError:
                check("instance_lock", True, "lock present (owner not inspectable)")
        else:
            check("instance_lock", True, "no daemon running")
        sock = state_dir / SOCKET_FILENAME
        if args.deep and sock.exists():
            check("control_plane_socket", _probe_socket(str(sock)), str(sock))
        elif args.deep:
            check("control_plane_socket", True, "no socket (control plane disabled or daemon down)")
        free = free_disk_mb(state_dir)
        check(
            "disk_free",
            free is None or free > 50,
            f"{free} MB free" if free is not None else "unknown (statvfs unavailable)",
        )
        if config is not None and config.control_plane.token_file:
            token_path = Path(config.control_plane.token_file).expanduser()
            if token_path.is_file():
                mode = token_path.stat().st_mode & 0o777
                check("token_file_permissions", mode <= 0o600, f"{oct(mode)} on {token_path.name}")
            else:
                check("token_file_permissions", False, f"token file missing: {token_path.name}")

    ok = all(c["ok"] for c in checks)
    _print({"ok": ok, "checks": checks}, _json_flag(args))
    return 0 if ok else 1


def _probe_socket(path: str) -> bool:
    import socket as _socket

    probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _cmd_status(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = _open_agent(args, config)
    try:
        _print(agent.status(), _json_flag(args))
    finally:
        agent.store.close()
    return 0


def _cmd_jobs(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = _open_agent(args, config)
    try:
        if args.action == "list":
            rows = [
                {
                    "job_id": r.job_id,
                    "mode": r.mode,
                    "enabled": r.enabled,
                    "revision": r.revision,
                    "next_due_ms": r.next_due_ms,
                    "misfire_policy": r.misfire_policy,
                }
                for r in agent.jobs_list(enabled=getattr(args, "enabled", None))
            ]
            _print({"jobs": rows, "count": len(rows)}, _json_flag(args))
        elif args.action == "show":
            if not args.job_id:
                raise PASError(ErrorCode.INVALID_CONFIG, "jobs show needs a job_id", scope="cli")
            record = agent.jobs_get(args.job_id)
            if record is None:
                raise PASError(ErrorCode.INVALID_CONFIG, f"unknown job {args.job_id!r}", scope="cli")
            _print(
                {
                    "job_id": record.job_id,
                    "mode": record.mode,
                    "schedule": record.schedule,
                    "task": record.task,
                    "enabled": record.enabled,
                    "revision": record.revision,
                    "grant_refs": record.grant_refs,
                    "delivery_policy": record.delivery_policy,
                    "next_due_ms": record.next_due_ms,
                },
                args.json,
            )
        elif args.action == "create":
            if not args.job_id or not args.schedule_json or not args.instruction:
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    "jobs create needs job_id, --schedule-json and --instruction",
                    scope="cli",
                )
            try:
                schedule = json.loads(args.schedule_json)
            except ValueError as exc:
                raise PASError(
                    ErrorCode.INVALID_CONFIG, f"--schedule-json is not valid JSON: {exc}", scope="cli"
                ) from None
            record = agent.jobs_upsert(
                Job(
                    id=args.job_id,
                    mode=args.mode,
                    schedule=schedule,
                    instruction=args.instruction,
                    grant_refs=tuple(args.grant_ref),
                ),
                idempotency_key=args.idempotency_key or f"cli-{args.job_id}",
            )
            _print({"job_id": record.job_id, "revision": record.revision}, _json_flag(args))
        elif args.action == "pause":
            record = agent.jobs_pause(args.job_id)  # type: ignore[arg-type]
            _print({"job_id": record.job_id, "enabled": False, "revision": record.revision}, _json_flag(args))
        elif args.action == "resume":
            record = agent.jobs_resume(args.job_id)  # type: ignore[arg-type]
            _print({"job_id": record.job_id, "enabled": True, "revision": record.revision}, _json_flag(args))
        elif args.action == "delete":
            if not args.job_id:
                raise PASError(ErrorCode.INVALID_CONFIG, "jobs delete needs a job_id", scope="cli")
            agent.jobs_delete(args.job_id)
            _print({"deleted": args.job_id}, _json_flag(args))
        elif args.action == "trigger":
            if not args.job_id:
                raise PASError(ErrorCode.INVALID_CONFIG, "jobs trigger needs a job_id", scope="cli")
            event_id = agent.trigger_job(args.job_id)
            _print({"event_id": event_id, "note": "the daemon picks it up on its next pass"}, _json_flag(args))
    finally:
        agent.store.close()
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = _open_agent(args, config)
    try:
        if args.action == "list":
            runs = agent.runs_list(state=args.state, limit=args.limit)
            _print({"runs": runs, "count": len(runs)}, _json_flag(args))
        elif args.action == "show":
            run = agent.runs_get(args.run_id)  # type: ignore[arg-type]
            if run is None:
                raise PASError(ErrorCode.INVALID_CONFIG, f"unknown run {args.run_id!r}", scope="cli")
            _print(run, _json_flag(args))
        elif args.action == "cancel":
            outcome = agent.runs_cancel(args.run_id)  # type: ignore[arg-type]
            _print(outcome, _json_flag(args))
        elif args.action == "events":
            events = agent.run_events(args.run_id)  # type: ignore[arg-type]
            _print({"events": events, "count": len(events)}, _json_flag(args))
    finally:
        agent.store.close()
    return 0


def _cmd_approvals(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = _open_agent(args, config)
    try:
        if args.action == "list":
            rows = [
                {
                    "approval_id": a.approval_id,
                    "state": a.state,
                    "request_hash": a.request_hash,
                    "expires_at_ms": a.expires_at_ms,
                }
                for a in agent.approvals_pending()
            ]
            _print({"approvals": rows, "count": len(rows)}, _json_flag(args))
        else:
            args_approve = getattr(args, "approve", None)
            if not args.approval_id or args_approve is None:
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    "approvals resolve needs an approval id and --approve or --deny",
                    scope="cli",
                )
            approval = agent.approvals_resolve(
                args.approval_id, approve=args_approve, actor="cli"
            )
            _print({"approval_id": approval.approval_id, "state": approval.state}, _json_flag(args))
    finally:
        agent.store.close()
    return 0


def _cmd_notifications(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = _open_agent(args, config)
    try:
        if args.action == "list":
            rows = agent.inbox_list(unread_only=args.unread_only)
            _print({"notifications": rows, "count": len(rows)}, _json_flag(args))
        elif args.action == "read":
            if not args.inbox_id:
                raise PASError(ErrorCode.INVALID_CONFIG, "notifications read needs an id", scope="cli")
            marked = agent.inbox_mark_read(args.inbox_id)
            _print({"inbox_id": args.inbox_id, "marked": marked}, _json_flag(args))
        else:
            if not args.kind:
                raise PASError(
                    ErrorCode.INVALID_CONFIG, "notifications feedback needs --kind", scope="cli"
                )
            scope = {"topic": args.topic} if args.topic else {}
            record = agent.notifications_feedback(
                kind=args.kind, actor=args.actor, message_id=args.inbox_id, scope=scope
            )
            _print(record, _json_flag(args))
    finally:
        agent.store.close()
    return 0


def _cmd_skills(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = _open_agent(args, config)
    try:
        if args.action == "audit":
            _print(agent.skills_audit(source=args.source), _json_flag(args))
        elif args.action == "import":
            _print(agent.skills_import(source=args.source), _json_flag(args))
        else:
            if not args.name:
                raise PASError(ErrorCode.INVALID_CONFIG, "skills explain needs a name", scope="cli")
            _print(agent.skills_explain(args.name), _json_flag(args))
    finally:
        agent.store.close()
    return 0


def _cmd_hooks(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = _open_agent(args, config)
    try:
        if args.action == "list":
            rows = [
                {
                    "hook_id": r.hook_id,
                    "enabled": r.enabled,
                    "error_count": r.error_count,
                    "backoff_until_ms": r.error_backoff_until_ms,
                }
                for r in agent.hooks_list(enabled=None)
            ]
            _print({"hooks": rows, "count": len(rows)}, _json_flag(args))
        elif args.action in ("enable", "disable"):
            if not args.hook_id:
                raise PASError(ErrorCode.INVALID_CONFIG, f"hooks {args.action} needs a hook_id", scope="cli")
            record = agent.store.set_hook_enabled(
                args.hook_id, enabled=args.action == "enable", now_ms=agent.store.clock.wall_now_ms()
            )
            _print({"hook_id": record.hook_id, "enabled": record.enabled}, _json_flag(args))
        else:
            _print(agent.hooks_run_due(), _json_flag(args))
    finally:
        agent.store.close()
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = _open_agent(args, config)
    try:
        meta = agent.backup(args.path)
        _print({"backup": meta}, _json_flag(args))
    finally:
        agent.store.close()
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "restore replaces the profile database with the backup and is irreversible.\n"
            "Re-run with --yes after stopping the daemon.",
            file=sys.stderr,
        )
        return 2
    meta = read_backup_meta(args.path)
    config = _load_config(args)
    state_dir = _state_dir(args, config)
    from .backup import restore_backup

    result = restore_backup(
        state_dir / DB_FILENAME,
        args.path,
        expect_profile=meta.get("profile"),
        expect_owner_destination=meta.get("owner_destination"),
    )
    _print({"restored": result}, _json_flag(args))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = _open_agent(args, config)
    try:
        _print(agent.export_data(args.path), _json_flag(args))
    finally:
        agent.store.close()
    return 0


def _cmd_delete_data(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            f"delete-data wipes ALL profile data rows and is irreversible.\n"
            f"Re-run with --yes to confirm (expected phrase: {_DELETE_PHRASE}).",
            file=sys.stderr,
        )
        return 2
    config = _load_config(args)
    agent = _open_agent(args, config)
    try:
        deleted = agent.delete_data(confirm=_DELETE_PHRASE)
        _print({"deleted": deleted}, _json_flag(args))
    finally:
        agent.store.close()
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    if not args.config:
        raise PASError(ErrorCode.INVALID_CONFIG, "config commands need --config FILE", scope="cli")
    config = load_config(args.config)
    if args.action == "check":
        _print({"ok": True, "profile": config.profile, "source": config.source_path}, _json_flag(args))
    else:
        print(config_to_yaml_subset(config), end="")
        print("# redacted view:", file=sys.stderr)
        import json as _json

        print(_json.dumps(config.redacted(), ensure_ascii=False, indent=1, sort_keys=True), file=sys.stderr)
    return 0


def _cmd_rpc(args: argparse.Namespace) -> int:
    config = _load_config(args)
    state_dir = _state_dir(args, config)
    socket_path = args.socket or str(state_dir / SOCKET_FILENAME)
    try:
        params = json.loads(args.params)
    except ValueError as exc:
        raise PASError(ErrorCode.INVALID_CONFIG, f"--params is not valid JSON: {exc}", scope="cli") from None
    import socket as _socket

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        sock.settimeout(10.0)
        sock.connect(socket_path)

        def send_and_recv(frame: dict[str, Any]) -> dict[str, Any]:
            sock.sendall(json.dumps(frame).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            if not data:
                raise PASError(
                    ErrorCode.PROVIDER_UNAVAILABLE, "no reply from control plane", scope="cli"
                )
            return json.loads(data)

        # Identity is established per connection: hello first (the token,
        # when given, rides in hello params), then the actual request.
        hello_params: dict[str, Any] = {"protocol_version": "1.0", "client": "pas-cli"}
        if args.token is not None:
            hello_params["token"] = args.token
        hello = send_and_recv(
            {"jsonrpc": "2.0", "id": 0, "method": "system.hello", "params": hello_params}
        )
        if "error" in hello:
            print(json.dumps(hello, ensure_ascii=False, indent=1, sort_keys=True))
            return 2
        reply = send_and_recv({"jsonrpc": "2.0", "id": 1, "method": args.method, "params": params})
    finally:
        sock.close()
    print(json.dumps(reply, ensure_ascii=False, indent=1, sort_keys=True))
    return 0 if "result" in reply else 2


def _cmd_version(_args: argparse.Namespace) -> int:
    from . import PAS_PROTOCOL_VERSION

    print(f"pas {__version__} (protocol {PAS_PROTOCOL_VERSION})")
    return 0


_COMMANDS = {
    "serve": _cmd_serve,
    "tick": _cmd_tick,
    "doctor": _cmd_doctor,
    "status": _cmd_status,
    "jobs": _cmd_jobs,
    "runs": _cmd_runs,
    "approvals": _cmd_approvals,
    "notifications": _cmd_notifications,
    "skills": _cmd_skills,
    "hooks": _cmd_hooks,
    "backup": _cmd_backup,
    "restore": _cmd_restore,
    "export": _cmd_export,
    "delete-data": _cmd_delete_data,
    "config": _cmd_config,
    "rpc": _cmd_rpc,
    "version": _cmd_version,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version and not args.command:
        return _cmd_version(args)
    if args.command is None:
        parser.print_help()
        return 0
    handler = _COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover — argparse choices guard this
        parser.print_help()
        return 2
    try:
        return handler(args)
    except PASError as exc:
        print(f"error: [{exc.code.value}] {exc.safe_message}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # pragma: no cover — piped output early close
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
