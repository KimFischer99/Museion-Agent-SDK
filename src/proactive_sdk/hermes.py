"""Hermes Runs host adapter (SPEC §12.1; P5 宿主适配).

PAS 驱动已有宿主（方式 C）：PAS 持有调度、策略与账本；Hermes 提供推理与
已审核工具。本模块只说 Hermes Runs HTTP 的话，不 import Hermes。

语义边界（对锁定版本 v0.21.5 真实服务验证，见 VALIDATION.md §5e）：

- ``start`` 返回只代表提交被接受（``status: started``），不是完成。
  完成只能来自 ``wait``/``status``/``events`` 的轮询确认。
- ``waiting_for_approval`` 与 ``stopping`` 都不是成功完成。
- ``cancel`` 先 POST stop，再轮询到宿主确认终态；超时上报
  ``cancellation_unconfirmed``，handle 不伪装成 cancelled（取消≠已停）。
- 提交后传输层失联属于 effect unknown：调用方用同一 operation_key 与
  完全相同的 payload 走 ``reconcile``；幂等 key 冲突（HTTP 409）永不
  静默改写。
- SSE 只用于观察；本适配器用状态轮询恢复，不把流断线当结果。
- 版本/能力不支持时 fail closed（拒绝启动，不降级猜测）。

原始来源文本是数据：本模块不解析 Hermes 输出的语义，只做 envelope 级
校验；提案 schema 校验、策略与审批都在 PAS 侧。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .contracts import ErrorCode, PASError, PAS_PROTOCOL_VERSION

__all__ = [
    "HermesAcceptanceUnknown",
    "HermesProtocolError",
    "HermesRunHandle",
    "HermesRunRequest",
    "HermesRunResult",
    "HermesRunsClient",
    "HermesRunsExecutor",
    "HermesTransportError",
    "HermesVersionUnsupported",
    "CancellationUnconfirmed",
    "REQUIRED_RUN_FEATURES",
]

#: Capabilities this adapter refuses to run without (fail closed).
REQUIRED_RUN_FEATURES = ("run_submission", "run_status", "run_stop", "runs_idempotency")

_RUN_HANDLE_STATES = frozenset(
    {"accepted", "running", "waiting_for_approval", "completed", "failed", "cancelled"}
)
# Real-service status vocabulary (Hermes v0.21.5): submit → "started";
# poll → running/waiting_for_approval/stopping/completed/failed/cancelled/interrupted.
_STATUS_MAP = {
    "started": "accepted",
    "queued": "accepted",
    "running": "running",
    "stopping": "running",
    "waiting_for_approval": "waiting_for_approval",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "cancelled",
}
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})

_OPERATION_KEY_RE = re.compile(r"[\x21-\x7e]{1,255}")
_MAX_OUTPUT_CHARS = 2 * 1024 * 1024
_MAX_BODY_BYTES = 2 * 1024 * 1024


class HermesTransportError(PASError):
    """Transport failure. A POST may already have been accepted remotely."""

    def __init__(self, message: str) -> None:
        super().__init__(
            ErrorCode.EFFECT_UNKNOWN, message, scope="hermes"
        )


class HermesAcceptanceUnknown(HermesTransportError):
    """Submit response lost. Reconcile with the same operation key + payload."""

    def __init__(self, operation_key: str, body_hash: str) -> None:
        self.operation_key = operation_key
        self.body_hash = body_hash
        super().__init__(
            "Hermes submit acceptance unknown; reconcile the same operation key "
            f"and payload hash {body_hash}"
        )


class HermesVersionUnsupported(PASError):
    """The host does not advertise the features this adapter requires."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "Hermes host is missing required features: " + ", ".join(missing),
            scope="hermes",
        )


class HermesProtocolError(PASError):
    """The host answered outside the documented envelope; never guess."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, message, scope="hermes")


class CancellationUnconfirmed(PASError):
    """Stop was accepted but the host never reported a terminal state."""

    def __init__(self, host_run_id: str, last_status: str | None) -> None:
        self.host_run_id = host_run_id
        self.last_status = last_status
        super().__init__(
            ErrorCode.CONFLICT,
            f"cancellation_unconfirmed for host run {host_run_id}; "
            "the host may still be executing — do not start a replacement",
            scope="hermes",
        )


class HermesRunRequest:
    """One run to submit. ``operation_key`` is the caller's idempotency key:
    persist it (with ``body_hash``) BEFORE calling ``start``."""

    __slots__ = ("operation_key", "prompt", "instructions", "session_id", "body_hash")

    def __init__(
        self,
        *,
        operation_key: str,
        prompt: str,
        instructions: str,
        session_id: str | None = None,
    ) -> None:
        if not isinstance(operation_key, str) or not _OPERATION_KEY_RE.fullmatch(operation_key):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "operation_key must be 1..255 visible ASCII chars",
            )
        for name, value in (("prompt", prompt), ("instructions", instructions)):
            if not isinstance(value, str) or not value or len(value) > 256 * 1024:
                raise PASError(
                    ErrorCode.INVALID_CONFIG, f"{name} must be a non-empty string (<=256KiB)"
                )
        if session_id is not None and (
            not isinstance(session_id, str) or not 1 <= len(session_id) <= 256
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "session_id must be 1..256 chars")
        self.operation_key = operation_key
        self.prompt = prompt
        self.instructions = instructions
        self.session_id = session_id
        body = self.body()
        self.body_hash = "sha256:" + hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        ).hexdigest()

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"input": self.prompt, "instructions": self.instructions}
        if self.session_id is not None:
            body["session_id"] = self.session_id
        return body


@dataclass(frozen=True)
class HermesRunHandle:
    """Mirrors schemas/v1/run_handle.json; validated in __post_init__."""

    executor_id: str
    host_run_id: str | None
    state: str
    resume_token: str | None = None
    protocol_version: str = PAS_PROTOCOL_VERSION
    capabilities: tuple[str, ...] = ("cancellation", "usage_reporting")

    def __post_init__(self) -> None:
        if self.state not in _RUN_HANDLE_STATES:
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"run handle state {self.state!r} is not valid"
            )
        if self.host_run_id is not None and (
            not isinstance(self.host_run_id, str) or not 1 <= len(self.host_run_id) <= 256
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "host_run_id must be 1..256 chars")

    def to_dict(self) -> dict[str, Any]:
        """Wire form; shape-tested against schemas/v1/run_handle.json in the
        contract tests (structural checks live in __post_init__)."""
        return {
            "protocol_version": self.protocol_version,
            "executor_id": self.executor_id,
            "host_run_id": self.host_run_id,
            "state": self.state,
            "resume_token": self.resume_token,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class HermesRunResult:
    """Latest observed state plus the completion payload when terminal."""

    handle: HermesRunHandle
    output: str | None = None
    usage: dict[str, Any] | None = None
    host_status: str | None = None
    partial: bool = False
    interrupted: bool = False


def _usage_from_host(
    raw: dict[str, Any] | None, model: str | None, elapsed_ms: int | None
) -> dict[str, Any]:
    """Map the host usage object onto schemas/v1/usage.json. Unmeasurable
    values stay None, never zero."""

    def _int(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    usage = raw if isinstance(raw, dict) else {}
    data = {
        "protocol_version": PAS_PROTOCOL_VERSION,
        "provider": "hermes",
        "model": (model or "unknown")[:128],
        "input_tokens": _int(usage.get("input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "cache_read_tokens": _int(usage.get("cache_read_tokens")),
        "tool_calls": None,
        "elapsed_ms": elapsed_ms if elapsed_ms is None or elapsed_ms >= 0 else None,
        "pricing_basis": "measured" if usage else "unknown",
    }
    return data


class HermesRunsClient:
    """Hardened HTTP transport for the Hermes Runs surface.

    HTTPS or literal loopback HTTP only; no proxies, no redirects, bounded
    responses, bearer-token auth. One-shot connections; nothing retained
    between calls.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0) -> None:
        url = urlsplit(base_url)
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = False  # Use a literal loopback IP for local HTTP, not DNS.
        if (url.username or url.password or url.query or url.fragment
                or (url.scheme != "https" and not (url.scheme == "http" and loopback))):
            raise ValueError("Use HTTPS or literal loopback HTTP without URL credentials/query")
        if not url.hostname or not token or "\r" in token or "\n" in token or timeout <= 0:
            raise ValueError("Valid base URL, bearer token, and timeout required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        request = Request(self.base_url + path, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_BODY_BYTES + 1)
                if len(raw) > _MAX_BODY_BYTES:
                    raise HermesTransportError("Hermes response exceeded the configured limit")
            data = json.loads(raw)
        except HTTPError as exc:
            # 409 idempotency conflicts are reconcilable facts, not noise.
            if exc.code == 409:
                raise _HermesKeyConflict() from exc
            # Never surface raw remote error text (may carry prompt data).
            raise HermesTransportError(
                f"Hermes HTTP {exc.code}; reconcile before retrying writes"
            ) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise HermesTransportError(
                "Hermes transport/JSON error; acceptance may be unknown"
            ) from exc
        if not isinstance(data, dict):
            raise HermesProtocolError("Hermes returned a non-object JSON body")
        return data

    # -- thin sync primitives; the executor wraps them in a worker thread ----

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/v1/capabilities")

    def submit(self, body: dict[str, Any], operation_key: str) -> dict[str, Any]:
        return self._request(
            "POST", "/v1/runs", body, {"Idempotency-Key": operation_key}
        )

    def status(self, run_id: str) -> dict[str, Any]:
        if not run_id or not isinstance(run_id, str):
            raise ValueError("run_id is required")
        return self._request("GET", "/v1/runs/" + quote(run_id, safe=""))

    def stop(self, run_id: str) -> dict[str, Any]:
        if not run_id or not isinstance(run_id, str):
            raise ValueError("run_id is required")
        return self._request(
            "POST", "/v1/runs/" + quote(run_id, safe="") + "/stop", {}
        )

    def events(self, run_id: str) -> dict[str, Any]:
        if not run_id or not isinstance(run_id, str):
            raise ValueError("run_id is required")
        return self._request("GET", "/v1/runs/" + quote(run_id, safe="") + "/events")


class _HermesKeyConflict(Exception):
    """Internal: idempotency key reuse with a different payload (HTTP 409)."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HermesRunsExecutor:
    """AgentExecutor (SPEC §4.2) over the Hermes Runs surface.

    Lifecycle contract: ``capabilities`` pins the host feature set (fail
    closed); ``start`` only records acceptance; ``wait``/``status`` own the
    completion decision; ``cancel`` waits for a host-confirmed terminal
    state; ``reconcile`` is the only recovery path for a lost submit reply.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        executor_id: str = "hermes-runs",
        client: HermesRunsClient | None = None,
        poll_interval_s: float = 1.0,
        cancel_timeout_s: float = 30.0,
        required_features: tuple[str, ...] = REQUIRED_RUN_FEATURES,
    ) -> None:
        if not isinstance(executor_id, str) or not 1 <= len(executor_id) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "executor_id must be 1..128 chars")
        if poll_interval_s <= 0 or cancel_timeout_s <= 0:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "poll_interval_s and cancel_timeout_s must be positive"
            )
        self.executor_id = executor_id
        self.required_features = tuple(required_features)
        self.poll_interval_s = poll_interval_s
        self.cancel_timeout_s = cancel_timeout_s
        self._client = client if client is not None else HermesRunsClient(base_url, token)
        self._caps: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # Capabilities (fail closed)
    # ------------------------------------------------------------------ #

    async def capabilities(self) -> dict[str, Any]:
        """Fetch and pin host capabilities; raise on missing features."""
        if self._caps is None:
            caps = await asyncio.to_thread(self._client.capabilities)
            self._require_features(caps)
            self._caps = caps
        return self._caps

    def _require_features(self, caps: dict[str, Any]) -> None:
        features = caps.get("features")
        if not isinstance(features, dict):
            raise HermesProtocolError("Hermes capabilities response has no features object")
        missing: list[str] = []
        for name in self.required_features:
            value = features.get(name)
            if name == "runs_idempotency":
                if not (isinstance(value, dict) and value.get("supported") is True):
                    missing.append(name)
            elif value is not True:
                missing.append(name)
        if missing:
            raise HermesVersionUnsupported(missing)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self, request: HermesRunRequest) -> HermesRunHandle:
        """Submit one run. Returns acceptance only — never completion."""
        await self.capabilities()
        try:
            data = await asyncio.to_thread(self._client.submit, request.body(), request.operation_key)
        except _HermesKeyConflict as exc:
            raise PASError(
                ErrorCode.CONFLICT,
                f"idempotency key {request.operation_key!r} was already used with a "
                "different payload; reconcile manually, never rewrite",
                scope="hermes",
            ) from exc
        except HermesTransportError as exc:
            raise HermesAcceptanceUnknown(request.operation_key, request.body_hash) from exc
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 256:
            raise HermesAcceptanceUnknown(request.operation_key, request.body_hash)
        state = _STATUS_MAP.get(str(data.get("status", "started")))
        if state is None:
            raise HermesProtocolError(f"Hermes submit returned unknown status {data.get('status')!r}")
        return HermesRunHandle(
            executor_id=self.executor_id, host_run_id=run_id, state="accepted"
        )

    async def status(self, handle: HermesRunHandle) -> HermesRunResult:
        """One poll. Terminal completion requires a string output."""
        if handle.host_run_id is None:
            raise PASError(ErrorCode.INVALID_CONFIG, "handle has no host_run_id")
        data = await asyncio.to_thread(self._client.status, handle.host_run_id)
        return self._result_from_status(handle, data)

    def _result_from_status(self, handle: HermesRunHandle, data: dict[str, Any]) -> HermesRunResult:
        raw_status = data.get("status")
        state = _STATUS_MAP.get(str(raw_status)) if raw_status is not None else None
        if state is None:
            raise HermesProtocolError(f"Hermes returned unknown run status {raw_status!r}")
        output: str | None = None
        if state == "completed":
            output = data.get("output")
            if not isinstance(output, str):
                raise HermesProtocolError("Completed Hermes run did not provide a string output")
            if len(output) > _MAX_OUTPUT_CHARS:
                raise HermesProtocolError("Hermes output exceeds the configured limit")
        elif state in ("failed", "cancelled"):
            # Waiting for approval and stopping never map here; a run that
            # ended without a successful result stays non-completed.
            output = data.get("output") if isinstance(data.get("output"), str) else None
        elapsed_ms = None
        created_at, updated_at = data.get("created_at"), data.get("updated_at")
        if isinstance(created_at, (int, float)) and isinstance(updated_at, (int, float)):
            elapsed_ms = max(0, int((updated_at - created_at) * 1000))
        runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
        usage = _usage_from_host(
            data.get("usage") if isinstance(data.get("usage"), dict) else None,
            runtime.get("model") if isinstance(runtime.get("model"), str) else None,
            elapsed_ms,
        )
        new_handle = HermesRunHandle(
            executor_id=handle.executor_id,
            host_run_id=handle.host_run_id,
            state=state,
            resume_token=handle.resume_token,
            protocol_version=handle.protocol_version,
            capabilities=handle.capabilities,
        )
        return HermesRunResult(
            handle=new_handle,
            output=output,
            usage=usage,
            host_status=str(raw_status),
            partial=data.get("partial") is True,
            interrupted=data.get("interrupted") is True,
        )

    async def wait(
        self,
        handle: HermesRunResult | HermesRunHandle,
        *,
        timeout_s: float | None = None,
    ) -> HermesRunResult:
        """Poll until a terminal state or ``timeout_s`` elapses.

        A deadline hit raises DEADLINE_EXCEEDED; the run may still be
        executing remotely — cancel it explicitly if that matters."""
        current = handle if isinstance(handle, HermesRunResult) else HermesRunResult(handle=handle)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout_s if timeout_s is not None else 3600.0)
        while current.handle.state not in _TERMINAL_STATES:
            if loop.time() >= deadline:
                raise PASError(
                    ErrorCode.DEADLINE_EXCEEDED,
                    f"hermes run {current.handle.host_run_id} did not settle in time",
                    scope="hermes",
                )
            await asyncio.sleep(self.poll_interval_s)
            current = await self.status(current.handle)
        return current

    async def cancel(
        self, handle: HermesRunResult | HermesRunHandle, *, timeout_s: float | None = None
    ) -> HermesRunResult:
        """Request stop, then poll until the host reports a terminal state.

        取消≠已停: until the host confirms, the run is treated as running.
        On timeout raise CancellationUnconfirmed; the caller must not start
        a replacement worker (duplicate writes)."""
        current = handle if isinstance(handle, HermesRunResult) else HermesRunResult(handle=handle)
        if current.handle.host_run_id is None:
            raise PASError(ErrorCode.INVALID_CONFIG, "handle has no host_run_id")
        budget = self.cancel_timeout_s if timeout_s is None else timeout_s
        await asyncio.to_thread(self._client.stop, current.handle.host_run_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        while current.handle.state not in _TERMINAL_STATES:
            if loop.time() >= deadline:
                raise CancellationUnconfirmed(
                    current.handle.host_run_id or "", current.host_status
                )
            await asyncio.sleep(self.poll_interval_s)
            current = await self.status(current.handle)
        return current

    async def reconcile(self, request: HermesRunRequest) -> HermesRunHandle:
        """Recover a lost submit reply: re-POST the identical payload under
        the identical operation key. Replay returns the original run; a 409
        conflict raises (different payload under a used key is never
        rewritten automatically)."""
        await self.capabilities()
        try:
            data = await asyncio.to_thread(self._client.submit, request.body(), request.operation_key)
        except _HermesKeyConflict as exc:
            raise PASError(
                ErrorCode.CONFLICT,
                f"idempotency key {request.operation_key!r} conflicts with a different "
                "payload; investigate before any resubmission",
                scope="hermes",
            ) from exc
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 256:
            raise HermesProtocolError("Hermes reconcile reply had no run_id")
        return HermesRunHandle(
            executor_id=self.executor_id, host_run_id=run_id, state="accepted"
        )

    async def events(self, handle: HermesRunResult | HermesRunHandle, after_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Observation-only event snapshots (safe summaries: type + index).
        Not a completion signal; use wait/status for that."""
        base = handle.handle if isinstance(handle, HermesRunResult) else handle
        if base.host_run_id is None:
            raise PASError(ErrorCode.INVALID_CONFIG, "handle has no host_run_id")
        data = await asyncio.to_thread(self._client.events, base.host_run_id)
        items = data.get("events") if isinstance(data.get("events"), list) else []
        seq = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            seq += 1
            if seq <= after_seq:
                continue
            yield {"seq": seq, "type": str(item.get("event") or item.get("type") or "unknown")}

    async def close(self) -> None:
        """One-shot HTTP connections; nothing to release."""
        return None
