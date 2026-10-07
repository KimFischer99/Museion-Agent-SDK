"""Control-plane RPC server (SPEC §14.2; P7).

Binds the frozen §14.2 method set to the :class:`ProactiveAgent`
facade over a local Unix socket (newline-delimited JSON frames; the
generated TS client is transport-agnostic and plugs in via its
``Endpoint``).

Authentication, fail-closed (所有调用按 authenticated principal 绑定
profile，不相信请求体自报的 owner):

- same-UID Unix-socket peers are trusted via peer credentials
  (SO_PEERCRED on Linux, LOCAL_PEERCRED on macOS) — principal
  ``owner-local``;
- everyone else must present the control-plane token in
  ``system.hello.params.token`` (constant-time compare) — principal
  ``token:<prefix>``;
- when neither peer credentials nor a configured token can establish
  identity, every request fails with ``auth_required`` — the server
  never falls back to an anonymous principal.

The token never reaches the log stream (only its first four characters
as a principal prefix) and is not exposed to models or Skills — the
control-plane methods here are the only consumers. ``grants`` creation
is deliberately NOT part of the §14.2 method set: grant creation stays
a trusted-UI/facade action.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import socket
import struct
from pathlib import Path
from typing import Any

from .contracts import ErrorCode, PASError
from .facade import Job, ProactiveAgent
from .observability import Metrics, StructuredLogger
from .rpc import (
    RPC_APPLICATION_ERROR,
    PROACTIVE_RPC_METHODS,
    RpcDispatcher,
    RpcProtocolError,
    RpcSession,
    error_response,
    parse_frame,
)

__all__ = ["ControlPlaneServer", "peer_uid"]

_HANDLER_METHODS = tuple(
    m for m in PROACTIVE_RPC_METHODS if not m.startswith("system.")
)


def peer_uid(writer: asyncio.StreamWriter) -> int | None:
    """Peer UID of a Unix-socket connection, or None when the platform
    will not tell us (fail closed upstream)."""
    sock = writer.get_extra_info("socket")
    if sock is None or sock.family != socket.AF_UNIX:
        return None
    try:
        if hasattr(socket, "SO_PEERCRED"):
            data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", data)
            return uid
        if hasattr(socket, "LOCAL_PEERCRED"):  # macOS
            SOL_LOCAL = 0x0
            data = sock.getsockopt(SOL_LOCAL, socket.LOCAL_PEERCRED, 16)
            _version, uid = struct.unpack_from("II", data, 0)
            return uid
    except OSError:
        return None
    return None


class ControlPlaneServer:
    """Unix-socket JSON-RPC server over one agent."""

    def __init__(
        self,
        agent: ProactiveAgent,
        *,
        socket_path: str | Path,
        token: str | None = None,
        logger: StructuredLogger | None = None,
        metrics: Metrics | None = None,
        max_message_bytes: int = 1024 * 1024,
    ) -> None:
        self.agent = agent
        self.socket_path = Path(socket_path)
        self.token = token
        self.logger = logger or StructuredLogger(component="pas.rpc")
        self.metrics = metrics if metrics is not None else agent.metrics
        self.max_message_bytes = max_message_bytes
        self.dispatcher = RpcDispatcher(server_name="pas", max_message_bytes=max_message_bytes)
        self._register_handlers()
        self._server: asyncio.Server | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            # A leftover socket file from a crashed instance: probe it.
            if self._socket_alive():
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    f"control-plane socket {self.socket_path} is already served",
                    scope="rpc",
                )
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:  # pragma: no cover — platform without chmod semantics
            pass

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.socket_path.unlink(missing_ok=True)

    def _socket_alive(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.5)
            probe.connect(str(self.socket_path))
        except OSError:
            return False
        finally:
            probe.close()
        return True

    # ------------------------------------------------------------------ #
    # Connection handling (newline-delimited JSON frames)
    # ------------------------------------------------------------------ #

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = RpcSession(principal="unauthenticated")
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ConnectionResetError, asyncio.IncompleteReadError):
                    break
                if not line:
                    break
                if len(line) > self.max_message_bytes:
                    await self._write(
                        writer,
                        error_response(None, RPC_APPLICATION_ERROR, "frame exceeds message budget"),
                    )
                    break
                try:
                    message = parse_frame(line, max_bytes=self.max_message_bytes)
                except RpcProtocolError as exc:
                    if not await self._write(writer, error_response(None, exc.wire_code, str(exc))):
                        break
                    continue
                if session.principal == "unauthenticated":
                    # Identity is established on the FIRST frame and only
                    # via system.hello; everything else is refused and the
                    # connection closes (fail closed).
                    if message.get("method") != "system.hello" or not self._authenticate(
                        writer, message, session
                    ):
                        await self._write(
                            writer,
                            error_response(
                                message.get("id") if isinstance(message.get("id"), (str, int)) else None,
                                RPC_APPLICATION_ERROR,
                                "authentication failed",
                                ErrorCode.AUTH_REQUIRED.value,
                            ),
                        )
                        break
                response = await self.dispatcher.handle(message, session)
                if response is not None and not await self._write(writer, response):
                    break
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    def _authenticate(
        self, writer: asyncio.StreamWriter, message: dict[str, Any], session: RpcSession
    ) -> bool:
        """Same-UID Unix-socket peers are trusted via peer credentials;
        everyone else must present the control-plane token in
        ``params.token`` (constant-time compare). With neither, fail
        closed."""
        uid = peer_uid(writer)
        if uid is not None and uid == os.getuid():
            session.principal = "owner-local"
            return True
        if self.token is None:
            return False
        params = message.get("params")
        supplied = params.get("token") if isinstance(params, dict) else None
        if not isinstance(supplied, str) or not supplied:
            return False
        if hmac.compare_digest(supplied, self.token):
            session.principal = f"token:{supplied[:4]}"
            return True
        return False

    async def _write(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> bool:
        try:
            writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
            return True
        except (ConnectionResetError, BrokenPipeError, RuntimeError):
            return False

    # ------------------------------------------------------------------ #
    # §14.2 method bindings
    # ------------------------------------------------------------------ #

    def _register_handlers(self) -> None:
        agent = self.agent
        d = self.dispatcher

        async def params_of(params: dict[str, Any]) -> dict[str, Any]:
            return params if isinstance(params, dict) else {}

        def require(params: dict[str, Any], key: str) -> Any:
            value = params.get(key)
            if value is None or value == "":
                raise PASError(
                    ErrorCode.INVALID_CONFIG, f"params.{key} is required", scope="rpc"
                )
            return value

        async def jobs_create(params, session):
            p = await params_of(params)
            job = p.get("job")
            if not isinstance(job, dict):
                raise PASError(ErrorCode.INVALID_CONFIG, "params.job must be an object", scope="rpc")
            record = agent.jobs_upsert(
                Job(
                    id=str(require(job, "id")),
                    mode=str(require(job, "mode")),
                    schedule=dict(job.get("schedule") or {}),
                    instruction=str(job.get("instruction") or ""),
                    grant_refs=tuple(job.get("grant_refs") or ()),
                    notification_profile=str(job.get("notification_profile") or "owner-default"),
                    misfire_policy=job.get("misfire_policy"),
                    deadline=job.get("deadline"),
                    enabled=bool(job.get("enabled", True)),
                    delivery_policy=dict(job.get("delivery_policy") or {}),
                    reminder=(
                        dict(job["reminder"]) if isinstance(job.get("reminder"), dict) else None
                    ),
                    obligation=job.get("obligation"),
                ),
                idempotency_key=str(require(p, "idempotency_key")),
            )
            return {"job_id": record.job_id, "revision": record.revision}

        async def jobs_update(params, session):
            p = await params_of(params)
            job_id = str(require(p, "job_id"))
            existing = agent.jobs_get(job_id)
            if existing is None:
                raise PASError(ErrorCode.INVALID_CONFIG, f"unknown job {job_id!r}", scope="jobs")
            if "enabled" in p:
                record = agent.jobs_resume(job_id) if p["enabled"] else agent.jobs_pause(job_id)
                return {"job_id": job_id, "revision": record.revision, "enabled": bool(p["enabled"])}
            if "instruction" in p:
                # Instruction edits are a new revision of the same job id.
                from .contracts import JobSpec

                record = agent.jobs_upsert(
                    JobSpec(
                        job_id=job_id,
                        mode=existing.mode,
                        schedule=dict(existing.schedule),
                        task={"instruction": str(require(p, "instruction"))},
                        grant_refs=tuple(existing.grant_refs),
                        delivery_policy=dict(existing.delivery_policy),
                        misfire_policy=existing.misfire_policy,
                        deadline=existing.deadline,
                        enabled=bool(existing.enabled),
                        revision=existing.revision + 1,
                    ),
                    idempotency_key=f"update-{job_id}-{existing.revision + 1}",
                )
                return {"job_id": job_id, "revision": record.revision}
            raise PASError(
                ErrorCode.INVALID_CONFIG, "jobs.update needs 'enabled' or 'instruction'", scope="rpc"
            )

        async def jobs_list(params, session):
            p = await params_of(params)
            enabled = p.get("enabled")
            records = agent.jobs_list(enabled=enabled if isinstance(enabled, bool) else None)
            return {
                "jobs": [
                    {
                        "job_id": r.job_id,
                        "mode": r.mode,
                        "revision": r.revision,
                        "enabled": r.enabled,
                        "next_due_ms": r.next_due_ms,
                        "schedule": r.schedule,
                    }
                    for r in records
                ],
                "count": len(records),
            }

        async def jobs_pause(params, session):
            record = agent.jobs_pause(str(require(await params_of(params), "job_id")))
            return {"job_id": record.job_id, "enabled": False, "revision": record.revision}

        async def jobs_resume(params, session):
            record = agent.jobs_resume(str(require(await params_of(params), "job_id")))
            return {"job_id": record.job_id, "enabled": True, "revision": record.revision}

        async def jobs_stop(params, session):
            p = await params_of(params)
            return agent.jobs_stop(
                str(require(p, "job_id")), reason=p.get("reason")
            )

        async def jobs_delete(params, session):
            p = await params_of(params)
            # A job with audit history is *stopped*, never erased; the
            # response says which of the two happened so the client never
            # has to guess (SPEC §21.1 step 7).
            return agent.jobs_delete(
                str(require(p, "job_id")), reason=p.get("reason")
            )

        async def jobs_activity(params, session):
            p = await params_of(params)
            limit = p.get("limit", 50)
            rows = agent.activity_list(
                p.get("job_id"),
                phase=p.get("phase"),
                limit=int(limit) if isinstance(limit, int) else 50,
            )
            return {"activity": rows, "count": len(rows)}

        async def runs_get(params, session):
            run = agent.runs_get(str(require(await params_of(params), "run_id")))
            if run is None:
                raise PASError(ErrorCode.INVALID_CONFIG, "unknown run", scope="runs")
            return {"run": run}

        async def runs_list(params, session):
            p = await params_of(params)
            limit = p.get("limit", 50)
            runs = agent.runs_list(
                state=p.get("state"), limit=int(limit) if isinstance(limit, (int, float)) else 50
            )
            return {"runs": runs, "count": len(runs)}

        async def runs_cancel(params, session):
            return agent.runs_cancel(str(require(await params_of(params), "run_id")))

        async def runs_events(params, session):
            events = agent.run_events(str(require(await params_of(params), "run_id")))
            return {"events": events, "count": len(events)}

        async def skills_audit(params, session):
            p = await params_of(params)
            return agent.skills_audit(source=p.get("source"))

        async def skills_import(params, session):
            p = await params_of(params)
            return agent.skills_import(source=p.get("source"))

        async def skills_explain(params, session):
            return agent.skills_explain(str(require(await params_of(params), "name")))

        async def approvals_get(params, session):
            approval = agent.approvals_get(str(require(await params_of(params), "approval_id")))
            if approval is None:
                raise PASError(ErrorCode.INVALID_CONFIG, "unknown approval", scope="approvals")
            return {"approval_id": approval.approval_id, "state": approval.state,
                    "request_hash": approval.request_hash}

        async def approvals_resolve(params, session):
            p = await params_of(params)
            actor = session.principal
            if actor in ("unauthenticated", "anonymous"):
                raise PASError(ErrorCode.AUTH_REQUIRED, "no authenticated principal", scope="approvals")
            approve = p.get("approve")
            if not isinstance(approve, bool):
                raise PASError(ErrorCode.INVALID_CONFIG, "params.approve must be a boolean", scope="rpc")
            approval = agent.approvals_resolve(
                str(require(p, "approval_id")), approve=approve, actor=actor
            )
            return {"approval_id": approval.approval_id, "state": approval.state,
                    "resolved_by": approval.resolved_by}

        async def notifications_list(params, session):
            p = await params_of(params)
            unread_only = bool(p.get("unread_only", False))
            limit = p.get("limit", 100)
            rows = agent.inbox_list(
                unread_only=unread_only, limit=int(limit) if isinstance(limit, (int, float)) else 100
            )
            return {"notifications": rows, "count": len(rows)}

        async def notifications_feedback(params, session):
            p = await params_of(params)
            actor = session.principal
            if actor in ("unauthenticated", "anonymous"):
                raise PASError(ErrorCode.AUTH_REQUIRED, "no authenticated principal", scope="feedback")
            return agent.notifications_feedback(
                kind=str(require(p, "kind")),
                actor=actor,
                message_id=p.get("message_id"),
                scope=dict(p.get("scope") or {}),
            )

        d.register("jobs.create", jobs_create)
        d.register("jobs.update", jobs_update)
        d.register("jobs.list", jobs_list)
        d.register("jobs.pause", jobs_pause)
        d.register("jobs.resume", jobs_resume)
        async def input_note(params, session):
            p = await params_of(params)
            grant_refs = p.get("grant_refs") or []
            if not isinstance(grant_refs, list) or not grant_refs:
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    "input.note must bind at least one active grant_ref",
                    scope="rpc",
                )
            event_id = agent.note_user_input(
                str(require(p, "text")),
                grant_refs=tuple(str(ref) for ref in grant_refs),
                destination=p.get("destination"),
                idempotency_key=p.get("idempotency_key"),
            )
            return {"event_id": event_id}

        async def suggestions_list(params, session):
            p = await params_of(params)
            limit = p.get("limit", 50)
            rows = agent.suggestions_list(
                state=p.get("state"),
                limit=int(limit) if isinstance(limit, int) else 50,
            )
            return {"suggestions": rows, "count": len(rows)}

        async def suggestions_resolve(params, session):
            p = await params_of(params)
            actor = session.principal
            if actor in ("unauthenticated", "anonymous"):
                raise PASError(
                    ErrorCode.AUTH_REQUIRED, "no authenticated principal", scope="suggestions"
                )
            accept = p.get("accept")
            if not isinstance(accept, bool):
                raise PASError(
                    ErrorCode.INVALID_CONFIG, "suggestions.resolve needs accept=true|false",
                    scope="rpc",
                )
            record = agent.suggestions_resolve(
                str(require(p, "suggestion_id")), accept=accept, actor=actor
            )
            return {
                "suggestion_id": record["suggestion_id"],
                "state": record["state"],
                "job_name": record["job_name"],
                "created_job_id": record["created_job_id"],
            }

        d.register("suggestions.list", suggestions_list)
        d.register("suggestions.resolve", suggestions_resolve)
        d.register("input.note", input_note)
        d.register("jobs.stop", jobs_stop)
        d.register("jobs.delete", jobs_delete)
        d.register("jobs.activity", jobs_activity)
        d.register("runs.get", runs_get)
        d.register("runs.list", runs_list)
        d.register("runs.cancel", runs_cancel)
        d.register("runs.events", runs_events)
        d.register("skills.audit", skills_audit)
        d.register("skills.import", skills_import)
        d.register("skills.explain", skills_explain)
        d.register("approvals.get", approvals_get)
        d.register("approvals.resolve", approvals_resolve)
        d.register("notifications.list", notifications_list)
        d.register("notifications.feedback", notifications_feedback)
