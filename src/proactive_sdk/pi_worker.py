"""Pi worker bridge (SPEC §12.2; P5 宿主适配).

Python 侧把 Pi worker（``pi_worker/pi_worker.ts``，Node 进程，JSON-RPC 2.0
over stdio）包装成受控 executor：

- worker 只负责执行，不持有任何调度真源；
- 每个请求带 id，经严格 JSON-RPC 2.0 信封收发；worker 输出里混入的任何
  非协议行都会让 worker 被判定失真并替换；
- ``run`` 直到 idle + 终稿 envelope 才返回（prompt ACK ≠ 结果）；
- ``cancel`` 让 worker abort 并等待 idle；worker 报
  ``cancellation_unconfirmed`` 时如实上抛（取消≠已停）；
- ``initialize`` 失败（缺 Pi 包/版本不符）直接 fail closed，不做降级。

本模块不 import Pi：Pi 只存在于 worker 进程内。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from .contracts import ErrorCode, PASError

__all__ = [
    "DecisionEnvelopeError",
    "PiWorkerConfig",
    "PiWorkerExecutor",
]

_ERR_PARSE = -32700
_ERR_INVALID_PARAMS = -32602
_ERR_INTERNAL = -32603

_MAX_LINE_BYTES = 1024 * 1024
_MAX_CONCURRENT_RUNS = 1  # 单 profile 最小版：一次一个推理任务（SPEC §3）


class DecisionEnvelopeError(PASError):
    """Worker final text was not a valid decision envelope."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            ErrorCode.INTERNAL_ERROR, f"decision envelope invalid: {reason}", scope="pi-worker"
        )


@dataclass(frozen=True)
class PiWorkerConfig:
    """How to spawn and bound the worker process.

    ``command`` must name the worker script explicitly (last argv element);
    the Pi package entry and the read-only tool allowlist travel inside the
    ``initialize`` request so every deployment states its own trust base.
    """

    command: tuple[str, ...]
    pi_entry: str
    allowed_tools: tuple[str, ...] = ("read", "grep", "find", "ls")
    init_timeout_s: float = 30.0
    run_timeout_s: float = 300.0
    cancel_timeout_s: float = 15.0

    def __post_init__(self) -> None:
        if not self.command or not all(isinstance(part, str) and part for part in self.command):
            raise PASError(ErrorCode.INVALID_CONFIG, "command must be a non-empty argv tuple")
        if not isinstance(self.pi_entry, str) or not self.pi_entry.endswith(".js"):
            raise PASError(
                ErrorCode.INVALID_CONFIG, "pi_entry must point at the Pi package entry index.js"
            )
        if not self.allowed_tools or any(
            not isinstance(t, str) or not t for t in self.allowed_tools
        ):
            raise PASError(
                ErrorCode.INVALID_CONFIG, "allowed_tools must be a non-empty tuple of tool names"
            )
        for name in ("init_timeout_s", "run_timeout_s", "cancel_timeout_s"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise PASError(ErrorCode.INVALID_CONFIG, f"{name} must be positive")
        if "pi_worker.ts" != os.path.basename(self.command[-1]):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "command must end with the pi_worker.ts script path",
            )


class _WorkerProcess:
    """One worker process with serialized JSON-RPC traffic."""

    def __init__(self, config: PiWorkerConfig) -> None:
        self._config = config
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._reader: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stderr_tail: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            raise PASError(ErrorCode.INTERNAL_ERROR, "worker already started", scope="pi-worker")
        try:
            self._proc = subprocess.Popen(
                list(self._config.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise PASError(
                ErrorCode.DEPENDENCY_MISSING,
                f"cannot spawn pi worker: {exc.__class__.__name__}",
                scope="pi-worker",
            ) from exc
        self._loop = asyncio.get_running_loop()
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="pi-worker-rpc")
        self._reader.start()

    async def stop(self, timeout_s: float = 5.0) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                self._send_raw({"jsonrpc": "2.0", "id": self._alloc_id(), "method": "shutdown"})
            except (PASError, OSError, ValueError):
                pass
            try:
                await asyncio.to_thread(proc.wait, timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait, 5)
        self._fail_all_pending("worker stopped")
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._proc = None

    # -- wire --------------------------------------------------------------

    def _alloc_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def _send_raw(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "worker stdin closed", scope="pi-worker")
        line = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(line) + 1 > _MAX_LINE_BYTES:
            raise PASError(ErrorCode.INVALID_CONFIG, "request exceeds worker line limit", scope="pi-worker")
        try:
            proc.stdin.write(line + b"\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE, "worker stdin write failed", scope="pi-worker"
            ) from exc

    def _read_loop(self) -> None:
        proc = self._proc
        loop = self._loop
        if proc is None or proc.stdout is None or loop is None:
            return
        while True:
            try:
                raw = self._readline_bounded(proc.stdout)
            except OSError:
                raw = b""
            if not raw:
                self._fail_all_pending("worker exited")
                return
            if len(raw) > _MAX_LINE_BYTES:
                self._fail_all_pending("worker protocol violation: oversized line")
                return
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._fail_all_pending("worker protocol violation: non-JSON line")
                return
            if not isinstance(message, dict) or "id" not in message or message.get("id") is None:
                continue  # non-protocol chatter is ignored, not trusted
            with self._lock:
                fut = self._pending.pop(message["id"], None)
            if fut is None:
                continue
            loop.call_soon_threadsafe(self._settle, fut, message)

    @staticmethod
    def _readline_bounded(stream: Any) -> bytes:
        """Bounded readline for a binary pipe (Popen has no ``limit``)."""
        chunks: list[bytes] = []
        total = 0
        while True:
            byte = stream.read(1)
            if not byte:
                break
            if byte == b"\n":
                return b"".join(chunks)
            chunks.append(byte)
            total += 1
            if total > _MAX_LINE_BYTES:
                return b"x" * (_MAX_LINE_BYTES + 1)
        return b"".join(chunks)

    def _settle(self, fut: "asyncio.Future[dict[str, Any]]", message: dict[str, Any]) -> None:
        if fut.done():
            return
        if "error" in message and isinstance(message["error"], dict):
            fut.set_exception(_WorkerRpcError(message["error"]))
        elif "result" in message:
            fut.set_result(message["result"] if isinstance(message["result"], dict) else {})
        else:
            fut.set_exception(_WorkerRpcError({"code": _ERR_INTERNAL, "message": "malformed reply"}))

    def _fail_all_pending(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for fut in pending:
            self._loop.call_soon_threadsafe(
                fut.set_exception,
                PASError(ErrorCode.PROVIDER_UNAVAILABLE, f"pi worker: {reason}", scope="pi-worker"),
            )

    # -- requests ----------------------------------------------------------

    async def request(
        self, method: str, params: dict[str, Any] | None = None, timeout_s: float = 30.0
    ) -> dict[str, Any]:
        request_id = self._alloc_id()
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                raise PASError(
                    ErrorCode.PROVIDER_UNAVAILABLE, "pi worker not running", scope="pi-worker"
                )
            self._pending[request_id] = fut
        try:
            self._send_raw(message)
        except PASError:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        try:
            return await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise PASError(
                ErrorCode.DEADLINE_EXCEEDED,
                f"pi worker {method} timed out",
                scope="pi-worker",
            ) from exc
        except _WorkerRpcError as exc:
            raise exc.as_pas_error() from None

    def stderr_snapshot(self) -> str:
        """Best-effort stderr tail for diagnostics; worker may still be alive."""
        return "".join(self._stderr_tail[-2000:])


class _WorkerRpcError(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        self.error = error
        super().__init__(str(error.get("message", "worker error")))

    def as_pas_error(self) -> PASError:
        error = self.error
        code = error.get("code")
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        reason = data.get("reason")
        if code == _ERR_INTERNAL and reason == "cancellation_unconfirmed":
            return PASError(
                ErrorCode.CONFLICT,
                "cancellation_unconfirmed: pi worker session did not reach idle",
                scope="pi-worker",
            )
        if code == _ERR_INTERNAL and reason == "cancelled":
            # The abort landed; the run ends without a decision.
            return PASError(
                ErrorCode.CONFLICT, "run cancelled before completion", scope="pi-worker"
            )
        if code == _ERR_INTERNAL and reason == "decision_invalid":
            return DecisionEnvelopeError(str(data.get("detail", reason)))
        if code == _ERR_INVALID_PARAMS:
            return PASError(ErrorCode.INVALID_CONFIG, str(error.get("message")), scope="pi-worker")
        return PASError(ErrorCode.INTERNAL_ERROR, str(error.get("message")), scope="pi-worker")


@dataclass(frozen=True)
class PiRunResult:
    """Completion payload of one worker run."""

    envelope: dict[str, Any]
    usage: dict[str, Any]
    host_session_ref: str | None = None


class PiWorkerExecutor:
    """Lifecycle wrapper around one Pi worker process.

    Pattern: ``async with`` or explicit ``start()``/``close()``. Every
    ``run`` gets a fresh in-memory session inside the worker; concurrent
    runs beyond the configured cap are refused instead of queued silently
    (budget honesty: the caller owns ordering)."""

    def __init__(self, config: PiWorkerConfig) -> None:
        self._config = config
        self._proc: _WorkerProcess | None = None
        self._info: dict[str, Any] | None = None
        self._active = 0
        self._guard = threading.Lock()

    @property
    def is_started(self) -> bool:
        """True once the worker process is running (SPEC §22.1 item 3: a
        host driver may start the session lazily on first use)."""
        return self._proc is not None

    @property
    def worker_info(self) -> dict[str, Any] | None:
        return dict(self._info) if self._info else None

    async def start(self) -> dict[str, Any]:
        if self._proc is not None:
            raise PASError(ErrorCode.INTERNAL_ERROR, "executor already started", scope="pi-worker")
        proc = _WorkerProcess(self._config)
        try:
            proc.start()
            info = await proc.request(
                "initialize",
                {
                    "pi_entry": self._config.pi_entry,
                    "allowed_tools": list(self._config.allowed_tools),
                },
                timeout_s=self._config.init_timeout_s,
            )
        except PASError:
            await proc.stop()
            self._proc = None
            raise
        self._proc = proc
        self._info = info
        return dict(info)

    async def run(
        self,
        *,
        run_id: str,
        instruction: str,
        cwd: str,
        agent_dir: str | None = None,
        timeout_s: float | None = None,
    ) -> PiRunResult:
        if self._proc is None:
            raise PASError(ErrorCode.INTERNAL_ERROR, "executor not started", scope="pi-worker")
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 128:
            raise PASError(ErrorCode.INVALID_CONFIG, "run_id must be 1..128 chars")
        if not isinstance(instruction, str) or not instruction:
            raise PASError(ErrorCode.INVALID_CONFIG, "instruction must be non-empty")
        if not isinstance(cwd, str) or not cwd:
            raise PASError(ErrorCode.INVALID_CONFIG, "cwd must be a non-empty vetted scratch dir")
        with self._guard:
            if self._active >= _MAX_CONCURRENT_RUNS:
                raise PASError(
                    ErrorCode.BUDGET_EXCEEDED,
                    "another pi run is already active on this executor",
                    scope="pi-worker",
                )
            self._active += 1
        try:
            params: dict[str, Any] = {
                "run_id": run_id,
                "instruction": instruction,
                "cwd": cwd,
                "timeout_ms": int(
                    (timeout_s if timeout_s is not None else self._config.run_timeout_s) * 1000
                ),
            }
            if agent_dir is not None:
                params["agent_dir"] = agent_dir
            result = await self._proc.request(
                "run", params, timeout_s=self._config.run_timeout_s + 30.0
            )
        finally:
            with self._guard:
                self._active -= 1
        envelope = result.get("envelope")
        if not isinstance(envelope, dict) or envelope.get("decision") not in ("silent", "propose"):
            raise DecisionEnvelopeError("missing or malformed envelope in worker result")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return PiRunResult(
            envelope=envelope,
            usage={
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_tokens": usage.get("cache_read_tokens"),
                "tool_calls": usage.get("tool_calls"),
                "pricing_basis": "measured" if usage else "unknown",
            },
        )

    async def cancel(self, run_id: str, *, timeout_s: float | None = None) -> None:
        """Ask the worker to abort the active run and wait for idle.

        Raises ``CONFLICT`` (cancellation_unconfirmed) when the session did
        not settle — never pretends the run stopped."""
        if self._proc is None:
            raise PASError(ErrorCode.INTERNAL_ERROR, "executor not started", scope="pi-worker")
        await self._proc.request(
            "cancel",
            {"run_id": run_id},
            timeout_s=timeout_s if timeout_s is not None else self._config.cancel_timeout_s,
        )

    async def close(self) -> None:
        if self._proc is not None:
            await self._proc.stop()
            self._proc = None
            self._info = None
