"""The two broadest host forms (SPEC §22.1 item 4).

Hermes and Pi are two *specific* hosts. Most agents are neither: they are
either an object you already have in-process, or a command you can pipe a
string into. Those two shapes cover almost everything else, so they ship as
first-class drivers rather than as documentation exercises.

Trust boundary, stated plainly (AGENTS.md: 原始 shell、网络和凭据都可能绕过
broker): a subprocess host runs whatever the operator configured, with the
operator's environment, and PAS does not sandbox it. The driver therefore
declares ``external_tool_broker=False`` in the context it hands the bridge,
so the run ledger can say honestly that the side effects of this run are
not covered by PAS authorization. If you need that guarantee, the host must
call back into PAS's broker — that is what ``external_tool_broker=True``
means, and it is a property of the host, not of this file.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from typing import Any, Callable

from .contracts import ErrorCode, PASError
from .executor import RunCancelled
from .host_bridge import (
    CANCEL_CONFIRMED,
    CANCEL_REQUESTED,
    CANCEL_UNSUPPORTED,
    HostPrompt,
    HostReply,
)

__all__ = ["extract_envelope", "CallableHostDriver", "SubprocessHostDriver"]


def extract_envelope(stdout: str) -> str:
    """Pull the decision envelope out of a chatty CLI's output.

    A black-box agent will print logs, banners and progress lines around its
    answer. The rule is deliberately narrow and testable: take the **last**
    line that is a JSON object carrying a ``decision`` key. If no line
    qualifies the raw text is returned unchanged, so the bridge reports the
    real parse error instead of this helper inventing one.
    """
    if not isinstance(stdout, str):
        return ""
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "decision" in parsed:
            return candidate
    return stdout


def _as_reply(result: Any) -> HostReply:
    """Normalise whatever an in-process host chose to return."""
    if isinstance(result, HostReply):
        return result
    if isinstance(result, str):
        return HostReply(text=result)
    if isinstance(result, dict):
        return HostReply(text=json.dumps(result, ensure_ascii=False))
    raise PASError(
        ErrorCode.INVALID_CONFIG,
        f"in-process host returned {type(result).__name__};"
        " expected str, dict or HostReply",
        scope="host-forms",
    )


class CallableHostDriver:
    """Library-level host: an in-process callable.

    ``fn`` may be sync or async and is called with the flattened prompt
    string, which is the least surprising signature for "I already have an
    agent object". It may return a ``str``, a ``dict`` envelope or a
    ``HostReply``.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        capabilities: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> None:
        if not callable(fn):
            raise PASError(ErrorCode.INVALID_CONFIG, "fn must be callable", scope="host-forms")
        self.fn = fn
        self.name = name or getattr(fn, "__name__", "callable-host")
        self._capabilities = dict(capabilities or {})
        self._inflight: dict[str, asyncio.Task] = {}
        self._settled: set[str] = set()

    async def capabilities(self) -> dict[str, Any]:
        return dict(self._capabilities, host=self.name, form="in_process")

    async def submit(self, prompt: HostPrompt, *, timeout_s: float) -> HostReply:
        call = self.fn(prompt.render())
        if inspect.isawaitable(call):
            task = asyncio.ensure_future(call)
            self._inflight[prompt.run_key] = task
            try:
                result = await asyncio.wait_for(task, timeout=timeout_s)
            except asyncio.CancelledError:
                raise RunCancelled() from None
            except asyncio.TimeoutError:
                raise PASError(
                    ErrorCode.DEADLINE_EXCEEDED,
                    f"in-process host {self.name} exceeded its deadline",
                    scope="host-forms",
                ) from None
            finally:
                self._inflight.pop(prompt.run_key, None)
        else:
            # A blocking callable still has to respect the run deadline; the
            # thread cannot be killed, so the deadline is enforced on the wait.
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(lambda: call), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                raise PASError(
                    ErrorCode.DEADLINE_EXCEEDED,
                    f"in-process host {self.name} exceeded its deadline",
                    scope="host-forms",
                ) from None
        self._settled.add(prompt.run_key)
        return _as_reply(result)

    async def cancel(self, run_key: str) -> str:
        if run_key in self._settled:
            return CANCEL_CONFIRMED
        task = self._inflight.get(run_key)
        if task is None:
            # Nothing of ours is running under that key, and we cannot reach
            # into a blocking callable's thread — say so rather than guess.
            return CANCEL_UNSUPPORTED
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001 - the host's own failure is not ours
            pass
        return CANCEL_CONFIRMED if task.cancelled() else CANCEL_REQUESTED

    async def close(self) -> None:
        for task in list(self._inflight.values()):
            task.cancel()
        self._inflight.clear()


class SubprocessHostDriver:
    """Black-box host: the prompt goes in on stdin, the answer comes out on stdout.

    One process per run. This is the shape of "I have a CLI agent and
    nothing else", which is how most third-party agents are actually
    reachable.

    ``command`` is executed with the operator's environment; PAS does not
    sandbox it (see the module docstring). ``capabilities`` should therefore
    declare ``external_tool_broker=False`` unless the host really does route
    its tool calls back through PAS.
    """

    def __init__(
        self,
        command: tuple[str, ...] | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        capabilities: dict[str, Any] | None = None,
        envelope_extractor: Callable[[str], str] = extract_envelope,
        extra_args: tuple[str, ...] = (),
    ) -> None:
        if not command or not all(isinstance(part, str) and part for part in command):
            raise PASError(
                ErrorCode.INVALID_CONFIG, "command must be a non-empty argv", scope="host-forms"
            )
        if cwd is not None and not os.path.isdir(cwd):
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"cwd {cwd!r} is not an existing directory",
                scope="host-forms",
            )
        self.command = tuple(command) + tuple(extra_args)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self._capabilities = dict(capabilities or {})
        self._extractor = envelope_extractor
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    async def capabilities(self) -> dict[str, Any]:
        return dict(self._capabilities, form="subprocess", command=self.command[0])

    async def submit(self, prompt: HostPrompt, *, timeout_s: float) -> HostReply:
        env = None
        if self.env is not None:
            env = {**os.environ, **self.env}
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise PASError(
                ErrorCode.DEPENDENCY_MISSING,
                f"cannot start host process ({type(exc).__name__})",
                scope="host-forms",
            ) from exc
        self._procs[prompt.run_key] = proc
        try:
            try:
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(prompt.render().encode("utf-8")), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                await self._terminate(prompt.run_key)
                raise PASError(
                    ErrorCode.DEADLINE_EXCEEDED,
                    "host process exceeded its deadline and was terminated",
                    scope="host-forms",
                ) from None
        finally:
            self._procs.pop(prompt.run_key, None)
        if proc.returncode != 0:
            # A non-zero exit is a host failure, not an empty answer.
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"host process exited with status {proc.returncode}",
                retryable=True,
                scope="host-forms",
            )
        text = self._extractor(stdout.decode("utf-8", errors="replace"))
        return HostReply(text=text, usage={"host_exit_code": proc.returncode})

    async def cancel(self, run_key: str) -> str:
        proc = self._procs.get(run_key)
        if proc is None:
            return CANCEL_UNSUPPORTED
        await self._terminate(run_key)
        return CANCEL_CONFIRMED if proc.returncode is not None else CANCEL_REQUESTED

    async def _terminate(self, run_key: str) -> None:
        proc = self._procs.get(run_key)
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:  # pragma: no cover - already gone
            pass

    async def close(self) -> None:
        for run_key in list(self._procs):
            await self._terminate(run_key)
        self._procs.clear()
