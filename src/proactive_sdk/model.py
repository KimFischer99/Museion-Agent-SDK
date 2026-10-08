"""ModelPort adapters (SPEC §4.2, §8.2).

``OpenAICompatibleModel`` is the one real provider implementation: it
speaks the OpenAI Chat Completions request/response shape over HTTP,
maps provider failures onto the unified error vocabulary (§4.4) and
normalizes usage (§4.1 Usage: unmeasurable values are ``None``, never
zero). The HTTP transport is injectable; the contract tests exercise the
full adapter against a local scripted HTTP server.

Honesty boundary: a scripted local server proves the adapter's
request/response logic, **not** compatibility with any deployed provider
release. Live-provider validation against a locked version stays an open
gate (SPEC §15.1 P5/P7) and docs/COMPATIBILITY.md records that boundary.
Provider
parameters travel in ``request.adapter_namespace`` and never leak into
the public protocol; tokens and unredacted HTTP bodies never appear in
error messages.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .contracts import ErrorCode, ModelRequest, ModelResponse, ModelToolCall, PASError

__all__ = [
    "HttpTransport",
    "UrllibTransport",
    "OpenAICompatibleModel",
]

_MAX_BODY_BYTES = 8 * 1024 * 1024


@runtime_checkable
class HttpTransport(Protocol):
    """Blocking JSON-over-HTTP POST. Implementations must not log headers
    or bodies (they carry credentials and possibly private content)."""

    def post_json(
        self, url: str, *, headers: dict[str, str], body: dict[str, Any], timeout_s: float
    ) -> tuple[int, dict[str, str], bytes]:
        """Return ``(status, response_headers, raw_body)``."""
        ...


class UrllibTransport:
    """stdlib transport; exists so the core has zero third-party deps."""

    def post_json(
        self, url: str, *, headers: dict[str, str], body: dict[str, Any], timeout_s: float
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return response.status, dict(response.headers), response.read(_MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(_MAX_BODY_BYTES + 1)
            return exc.code, dict(exc.headers or {}), raw
        # URLError / socket timeouts / TLS failures: surfaced as OSError-ish
        # failures to the adapter, which maps them to provider_unavailable.
        except OSError as exc:
            raise _TransportUnavailable(str(exc.__class__.__name__)) from exc


class _TransportUnavailable(Exception):
    """Carries only the exception class name — never endpoint URLs or
    payloads — into the unified error."""


def _usage_from_provider(raw: Any) -> dict[str, Any]:
    """Normalize provider usage into the Usage object (schemas/v1/usage.json
    shape without protocol_version). Missing numbers stay ``None``; the
    pricing basis is 'measured' only when the provider actually reported
    usage, otherwise 'unknown' — a fabricated zero would understate cost
    (SPEC §8.3: 使用保守上限而不是写成零费用)."""
    if not isinstance(raw, dict):
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "tool_calls": None,
            "elapsed_ms": None,
            "pricing_basis": "unknown",
        }
    details = raw.get("prompt_tokens_details")
    cache_read = details.get("cached_tokens") if isinstance(details, dict) else None
    usage: dict[str, Any] = {
        "input_tokens": raw.get("prompt_tokens") if isinstance(raw.get("prompt_tokens"), int) else None,
        "output_tokens": raw.get("completion_tokens") if isinstance(raw.get("completion_tokens"), int) else None,
        "cache_read_tokens": cache_read if isinstance(cache_read, int) else None,
        "tool_calls": None,
        "elapsed_ms": None,
        "pricing_basis": "measured",
    }
    return usage


class OpenAICompatibleModel:
    """Chat-completions adapter (provider label is configurable, default
    ``openai-compatible`` until a locked live release is validated).

    The API key comes from a caller-supplied ``api_key_provider`` so hosts
    can wire an OS keychain / secret service (§9.4); the adapter never
    persists or logs the key."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_provider: Any,
        provider: str = "openai-compatible",
        transport: HttpTransport | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        if not base_url or not isinstance(base_url, str):
            raise PASError(ErrorCode.INVALID_CONFIG, "base_url must be a non-empty string")
        if not model or not isinstance(model, str):
            raise PASError(ErrorCode.INVALID_CONFIG, "model must be a non-empty string")
        if not callable(api_key_provider):
            raise PASError(
                ErrorCode.INVALID_CONFIG, "api_key_provider must be a callable returning the credential"
            )
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.provider = provider
        self._api_key_provider = api_key_provider
        self._transport: HttpTransport = transport if transport is not None else UrllibTransport()
        self._timeout_s = timeout_s

    # ModelPort ---------------------------------------------------------

    async def generate(self, request: ModelRequest) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model_name,
            "messages": [_to_provider_message(message) for message in request.messages],
        }
        if request.tool_schemas:
            body["tools"] = [
                {"type": "function", "function": dict(schema)} for schema in request.tool_schemas
            ]
        namespace = request.adapter_namespace.get("openai")
        if namespace is not None:
            if not isinstance(namespace, dict):
                raise PASError(ErrorCode.INVALID_CONFIG, "adapter_namespace['openai'] must be an object")
            body.update(namespace)
        headers = {"Authorization": f"Bearer {self._api_key_provider()}"}
        try:
            status, response_headers, raw = await asyncio.to_thread(
                self._transport.post_json,
                f"{self.base_url}/chat/completions",
                headers=headers,
                body=body,
                timeout_s=self._timeout_s,
            )
        except _TransportUnavailable as exc:
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"model transport failed ({exc})",
                retryable=True,
                scope="model",
            ) from None
        except Exception as exc:  # transport contract violation
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "model transport raised an unexpected error",
                retryable=True,
                scope="model",
            ) from exc
        if status != 200:
            raise self._http_error(status, response_headers)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "model response was not valid UTF-8 JSON",
                retryable=True,
                scope="model",
            ) from exc
        return self._parse_response(payload)

    # Parsing / error mapping -------------------------------------------

    def _http_error(self, status: int, headers: dict[str, str]) -> PASError:
        """Map HTTP status to the unified vocabulary (§4.4). Messages stay
        generic; bodies are dropped, never forwarded."""
        if status in (401, 403):
            return PASError(
                ErrorCode.AUTH_REQUIRED,
                f"model provider rejected credentials (http {status})",
                retryable=False,
                scope="model",
            )
        if status == 429:
            retry_after: int | None = None
            raw_retry = headers.get("Retry-After") or headers.get("retry-after")
            if raw_retry is not None and raw_retry.strip().isdigit():
                retry_after = int(raw_retry.strip())
            return PASError(
                ErrorCode.RATE_LIMITED,
                "model provider rate limited the request",
                retryable=True,
                retry_after_s=retry_after,
                scope="model",
            )
        if 400 <= status < 500:
            return PASError(
                ErrorCode.INVALID_CONFIG,
                f"model provider rejected the request (http {status})",
                retryable=False,
                scope="model",
            )
        return PASError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            f"model provider unavailable (http {status})",
            retryable=True,
            scope="model",
        )

    def _parse_response(self, payload: Any) -> ModelResponse:
        if not isinstance(payload, dict):
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE, "model response shape unexpected", scope="model"
            )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE, "model response has no choices", scope="model"
            )
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE, "model response choice has no message", scope="model"
            )
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE, "model content must be a string or null", scope="model"
            )
        # Reasoning traces are dropped here: they are never parsed for
        # actions, never persisted and never re-sent (§8.2, EXEC-01).
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE, "model tool_calls must be a list", scope="model"
            )
        tool_calls: list[ModelToolCall] = []
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                raise PASError(
                    ErrorCode.PROVIDER_UNAVAILABLE, "model tool call shape unexpected", scope="model"
                )
            function = raw_call.get("function")
            if (
                not isinstance(raw_call.get("id"), str)
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
            ):
                raise PASError(
                    ErrorCode.PROVIDER_UNAVAILABLE, "model tool call missing id/name", scope="model"
                )
            raw_arguments = function.get("arguments", "{}")
            if isinstance(raw_arguments, dict):
                arguments: dict[str, Any] = raw_arguments
            elif isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments)
                except ValueError as exc:
                    raise PASError(
                        ErrorCode.PROVIDER_UNAVAILABLE,
                        "model tool call arguments were not valid JSON",
                        scope="model",
                    ) from exc
            else:
                arguments = None
            if not isinstance(arguments, dict):
                raise PASError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "model tool call arguments must be an object",
                    scope="model",
                )
            tool_calls.append(
                ModelToolCall(call_id=raw_call["id"], name=function["name"], arguments=arguments)
            )
        return ModelResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            usage=_usage_from_provider(payload.get("usage")),
        )


def _to_provider_message(message: dict[str, Any]) -> dict[str, Any]:
    """Translate the internal message convention (module docstring) into
    the chat-completions wire shape."""
    role = message.get("role")
    if role in ("system", "user"):
        return {"role": role, "content": message.get("content", "")}
    if role == "assistant":
        out: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        calls = message.get("tool_calls") or []
        out["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                },
            }
            for call in calls
        ]
        return out
    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.get("call_id"),
            "content": message.get("content", ""),
        }
    raise PASError(ErrorCode.INVALID_CONFIG, f"unknown internal message role {role!r}")
