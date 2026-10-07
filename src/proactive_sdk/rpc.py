"""PAS JSON-RPC 2.0 control-plane protocol (SPEC §14.2; P5 跨语言协议).

Strict, transport-agnostic framing shared by every language surface:

- one JSON object per message; ``jsonrpc`` must be exactly ``"2.0"``;
- notifications (no ``id``) carry no response and never create durable
  state — reliable writes require an id'd request (SPEC §14.2);
- unknown method / bad params / oversized messages fail closed;
- version negotiation via ``system.hello``; every other method on a
  session that has not negotiated is refused;
- PAS error codes ride in ``data.code``; wire codes use the JSON-RPC
  standard ranges (parse/invalid/method-not-found/params/internal) plus
  ``-32000`` for PAS application errors.

Method set (SPEC §14.2): system.hello / system.capabilities / system.health,
jobs.create|update|list|pause|resume|delete, runs.get|list|cancel|events,
skills.audit|import|explain, approvals.get|resolve,
notifications.list|feedback. The dispatcher does not implement them here —
the facade (P7 daemon) binds handlers; P5 ships the envelope, the
negotiation rule and the generated TS client (packages/client-ts).
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from .contracts import ErrorCode, PASError, PAS_PROTOCOL_VERSION

__all__ = [
    "MAX_MESSAGE_BYTES",
    "RPC_PARSE_ERROR",
    "RPC_INVALID_REQUEST",
    "RPC_METHOD_NOT_FOUND",
    "RPC_INVALID_PARAMS",
    "RPC_INTERNAL_ERROR",
    "RPC_APPLICATION_ERROR",
    "PROACTIVE_RPC_METHODS",
    "RpcProtocolError",
    "RpcDispatcher",
    "RpcSession",
    "parse_frame",
    "request_message",
    "notification_message",
    "result_response",
    "error_response",
]

#: Default single-message budget (SPEC §14.2: 协议带 max size).
MAX_MESSAGE_BYTES = 1024 * 1024

RPC_PARSE_ERROR = -32700
RPC_INVALID_REQUEST = -32600
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_INTERNAL_ERROR = -32603
RPC_APPLICATION_ERROR = -32000

#: The frozen §14.2 method set — the generated TS client and the plugin
#: client are checked against this list in the contract tests.
PROACTIVE_RPC_METHODS = (
    "system.hello",
    "system.capabilities",
    "system.health",
    "jobs.create",
    "jobs.update",
    "jobs.list",
    "jobs.pause",
    "jobs.resume",
    "jobs.delete",
    "runs.get",
    "runs.list",
    "runs.cancel",
    "runs.events",
    "skills.audit",
    "skills.import",
    "skills.explain",
    "approvals.get",
    "approvals.resolve",
    "notifications.list",
    "notifications.feedback",
)


class RpcProtocolError(PASError):
    def __init__(self, message: str, wire_code: int = RPC_INVALID_REQUEST) -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, message, scope="rpc")
        self.wire_code = wire_code


def parse_frame(raw: bytes | str, *, max_bytes: int = MAX_MESSAGE_BYTES) -> dict[str, Any]:
    """Parse one JSON-RPC frame; every violation is a protocol error, not a
    silent fallback."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="strict")
    if not isinstance(raw, (bytes, bytearray)) or not raw.strip():
        raise RpcProtocolError("empty frame", RPC_PARSE_ERROR)
    if len(raw) > max_bytes:
        raise RpcProtocolError(
            f"frame exceeds {max_bytes} bytes", RPC_INVALID_REQUEST
        )
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RpcProtocolError("invalid JSON", RPC_PARSE_ERROR) from exc
    if not isinstance(message, dict):
        raise RpcProtocolError("frame must be an object", RPC_INVALID_REQUEST)
    if message.get("jsonrpc") != "2.0":
        raise RpcProtocolError('jsonrpc must be "2.0"', RPC_INVALID_REQUEST)
    method = message.get("method")
    if message.get("method") is not None and (not isinstance(method, str) or not method):
        raise RpcProtocolError("method must be a non-empty string", RPC_INVALID_REQUEST)
    if "method" not in message and "result" not in message and "error" not in message:
        raise RpcProtocolError("frame is neither request nor response", RPC_INVALID_REQUEST)
    request_id = message.get("id")
    if "id" in message and request_id is not None and not isinstance(request_id, (str, int)):
        raise RpcProtocolError("id must be a string, number or null", RPC_INVALID_REQUEST)
    if isinstance(request_id, bool):
        raise RpcProtocolError("id must not be a boolean", RPC_INVALID_REQUEST)
    if "params" in message and not isinstance(message["params"], (dict, list)):
        raise RpcProtocolError("params must be an object or array", RPC_INVALID_REQUEST)
    return message


def request_message(request_id: int | str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification_message(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def result_response(request_id: int | str | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(
    request_id: int | str | None,
    wire_code: int,
    message: str,
    pas_code: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"wire_code": wire_code}
    if pas_code:
        payload["code"] = pas_code
    if data:
        payload.update(data)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": wire_code, "message": message[:512], "data": payload},
    }


Handler = Callable[[dict[str, Any], "RpcSession"], Awaitable[Any]]


class RpcSession:
    """Per-connection state: the negotiation flag and authenticated
    principal binding (SPEC §14.2: principal 绑定 profile，不自报 owner)."""

    def __init__(self, principal: str = "anonymous") -> None:
        self.principal = principal
        self.negotiated = False
        self.client_name: str | None = None
        self.protocol_version: str | None = None


class RpcDispatcher:
    """Binds method names to async handlers and enforces the envelope rules.

    ``system.hello`` performs version negotiation and is always available;
    every other method requires a negotiated session. Notifications are
    acknowledged (None) without creating durable state."""

    def __init__(
        self,
        *,
        server_name: str = "pas",
        protocol_version: str = PAS_PROTOCOL_VERSION,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ) -> None:
        self.server_name = server_name
        self.protocol_version = protocol_version
        self.max_message_bytes = max_message_bytes
        self._handlers: dict[str, Handler] = {}

    def register(self, method: str, handler: Handler) -> None:
        if method not in PROACTIVE_RPC_METHODS:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"method {method!r} is not part of the frozen control-plane set",
            )
        self._handlers[method] = handler

    @property
    def methods(self) -> tuple[str, ...]:
        return ("system.hello", "system.capabilities", "system.health") + tuple(
            m for m in sorted(self._handlers) if not m.startswith("system.")
        )

    async def handle(self, message: dict[str, Any], session: RpcSession) -> dict[str, Any] | None:
        """Handle a parsed request frame; returns a response dict for
        requests, None for notifications. The built-in ``system.*`` methods
        are intercepted here; user handlers never shadow them."""
        method = message.get("method")
        if method is None:
            raise RpcProtocolError("dispatcher handles requests, not responses")
        request_id = message.get("id") if "id" in message else None
        is_notification = "id" not in message
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        async def respond(produce: Callable[[], Awaitable[Any]]) -> dict[str, Any] | None:
            try:
                result = await produce()
            except PASError as exc:
                if is_notification:
                    return None
                return error_response(request_id, RPC_APPLICATION_ERROR, str(exc), exc.code)
            except Exception as exc:  # noqa: BLE001 — the envelope must never leak internals
                if is_notification:
                    return None
                return error_response(
                    request_id, RPC_INTERNAL_ERROR, f"internal error: {exc.__class__.__name__}"
                )
            return result_response(request_id, result) if not is_notification else None

        if method == "system.hello":
            return await respond(lambda: self._system_hello(params, session))
        if method in ("system.capabilities", "system.health"):
            builtin = self._system_capabilities if method == "system.capabilities" else self._system_health
            return await respond(lambda: builtin(params, session))

        handler = self._handlers.get(method)
        if handler is None:
            return (
                error_response(request_id, RPC_METHOD_NOT_FOUND, f"unknown method {method!r}")
                if not is_notification
                else None
            )
        if not session.negotiated:
            return (
                error_response(
                    request_id,
                    RPC_APPLICATION_ERROR,
                    "session not negotiated; call system.hello first",
                    ErrorCode.AUTH_REQUIRED,
                )
                if not is_notification
                else None
            )
        return await respond(lambda: handler(params, session))

    async def handle_frame(self, raw: bytes | str | dict[str, Any], session: RpcSession) -> dict[str, Any] | None:
        message = raw if isinstance(raw, dict) else parse_frame(raw, max_bytes=self.max_message_bytes)
        if "method" not in message:
            raise RpcProtocolError("dispatcher received a response frame")
        return await self.handle(message, session)

    # -- built-in system methods -------------------------------------------

    async def _system_hello(self, params: dict[str, Any], session: RpcSession) -> dict[str, Any]:
        client_version = params.get("protocol_version")
        if not isinstance(client_version, str) or not client_version:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "system.hello requires protocol_version",
            )
        if client_version.split(".")[0] != self.protocol_version.split(".")[0]:
            raise PASError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                f"protocol major mismatch: client {client_version}, server {self.protocol_version}",
            )
        session.negotiated = True
        session.protocol_version = client_version
        session.client_name = (
            str(params["client"])[:64] if isinstance(params.get("client"), str) else None
        )
        return {
            "protocol_version": self.protocol_version,
            "server": self.server_name,
            "methods": self.methods,
        }

    async def _system_capabilities(self, params: dict[str, Any], session: RpcSession) -> dict[str, Any]:
        return {"protocol_version": self.protocol_version, "methods": self.methods}

    async def _system_health(self, params: dict[str, Any], session: RpcSession) -> dict[str, Any]:
        return {"ok": True, "server": self.server_name}
