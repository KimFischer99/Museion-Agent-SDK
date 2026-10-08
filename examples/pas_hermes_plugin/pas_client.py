"""Self-contained JSON-RPC 2.0 client for the PAS control plane.

The Hermes plugin process must not require the proactive_sdk package, so
this file carries the minimum hardened client: loopback HTTP (or HTTPS)
only, no proxies, no redirects, bounded replies, one ``system.hello``
negotiation per client instance, PAS error codes surfaced as
PluginRpcError. Mirrors proactive_sdk.rpc's envelope rules.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PROTOCOL_VERSION = "1.0"
MAX_REPLY_BYTES = 1024 * 1024


class PluginRpcError(RuntimeError):
    """A PAS RPC call failed. ``pas_code`` is the PAS error code (or None)."""

    def __init__(self, message: str, pas_code: str | None = None) -> None:
        super().__init__(message)
        self.pas_code = pas_code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PasRpcClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0) -> None:
        url = urlsplit(base_url)
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if (url.username or url.password or url.query or url.fragment
                or (url.scheme != "https" and not (url.scheme == "http" and loopback))):
            raise PluginRpcError("PAS_RPC_URL must be HTTPS or literal loopback HTTP")
        if not token or "\r" in token or "\n" in token:
            raise PluginRpcError("PAS_RPC_TOKEN must be a single-line bearer token")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._next_id = 0
        self._negotiated = False

    def _post(self, payload: dict) -> dict:
        request = Request(
            self.base_url,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_REPLY_BYTES + 1)
            if len(raw) > MAX_REPLY_BYTES:
                raise PluginRpcError("PAS reply exceeded the size limit")
            data = json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            raise PluginRpcError(f"PAS RPC HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise PluginRpcError(f"PAS RPC transport failure: {exc.__class__.__name__}") from exc
        if not isinstance(data, dict):
            raise PluginRpcError("PAS returned a non-object reply")
        return data

    def _call(self, method: str, params: dict | None) -> dict:
        if method != "system.hello" and not self._negotiated:
            raise PluginRpcError("PAS session not negotiated")
        self._next_id += 1
        payload: dict = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        data = self._post(payload)
        if "error" in data and isinstance(data["error"], dict):
            error = data["error"]
            pas_code = None
            if isinstance(error.get("data"), dict):
                pas_code = error["data"].get("code")
            raise PluginRpcError(str(error.get("message", "PAS RPC error")), pas_code)
        if "result" not in data:
            raise PluginRpcError("PAS reply carries neither result nor error")
        return data["result"]

    def hello(self) -> dict:
        result = self._call(
            "system.hello",
            {"protocol_version": PROTOCOL_VERSION, "client": "hermes-proactive-plugin"},
        )
        server_version = str(result.get("protocol_version", ""))
        if server_version.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
            raise PluginRpcError(
                f"PAS protocol major mismatch: server {server_version!r}"
            )
        self._negotiated = True
        return result

    def jobs_create(self, job: dict, idempotency_key: str) -> dict:
        return self._call(
            "jobs.create", {"job": job, "idempotency_key": idempotency_key}
        )

    def jobs_list(self) -> dict:
        return self._call("jobs.list", {})

    def jobs_pause(self, job_id: str) -> dict:
        return self._call("jobs.pause", {"job_id": job_id})

    def jobs_resume(self, job_id: str) -> dict:
        return self._call("jobs.resume", {"job_id": job_id})

    def skills_explain(self, skill_ref: str | None) -> dict:
        params: dict = {}
        if skill_ref:
            params["skill_ref"] = skill_ref
        return self._call("skills.explain", params)
