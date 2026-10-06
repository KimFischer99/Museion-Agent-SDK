"""Original Hermes Runs HTTP transport example, checked against docs 2026-10-06.

Not a safety sandbox and not a production AgentExecutor. Use a dedicated restricted
Hermes profile. Persist the operation key/request hash BEFORE submit and the returned
run_id immediately afterwards. The caller owns polling, budgets, reconciliation,
approvals, and deadline cancellation. No network requests run on import.
"""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class TransportError(RuntimeError):
    """Transport failure; a POST might already have been accepted remotely."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HermesRunsClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0):
        url = urlsplit(base_url)
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = False  # Use literal loopback IP for local HTTP, not DNS.
        if (url.username or url.password or url.query or url.fragment or
            (url.scheme != "https" and not (url.scheme == "http" and loopback))):
            raise ValueError("Use HTTPS or literal loopback HTTP without URL credentials/query")
        if not url.hostname or not token or "\r" in token or "\n" in token or timeout <= 0:
            raise ValueError("Valid base URL, bearer token, and timeout required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # No implicit environment proxy; no credential-forwarding redirects.
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        request = Request(self.base_url + path, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise TransportError("Response exceeded the configured limit")
            data = json.loads(raw)
        except HTTPError as exc:
            # Avoid logging raw remote errors, which may contain prompt/credential data.
            raise TransportError(f"Hermes HTTP {exc.code}; reconcile before retrying writes") from exc
        except (URLError, TimeoutError, ValueError, UnicodeError) as exc:
            raise TransportError("Hermes transport/JSON error; acceptance may be unknown") from exc
        if not isinstance(data, dict):
            raise TransportError("Expected a JSON object")
        return data

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/v1/capabilities")

    def submit(self, *, operation_key: str, prompt: str, instructions: str,
               session_id: str | None = None) -> str:
        if not re.fullmatch(r"[\x21-\x7e]{1,255}", operation_key):
            raise ValueError("Use a 1-255 visible-ASCII operation key")
        if not prompt or not instructions:
            raise ValueError("Prompt and instructions are required")
        body: dict[str, Any] = {"input": prompt, "instructions": instructions}
        if session_id is not None:
            body["session_id"] = session_id
        data = self._request("POST", "/v1/runs", body, {"Idempotency-Key": operation_key})
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise TransportError("No run_id returned; reconcile the same operation key")
        return run_id  # Accepted only; not necessarily completed.

    def status(self, run_id: str) -> dict[str, Any]:
        if not run_id:
            raise ValueError("run_id is required")
        return self._request("GET", "/v1/runs/" + quote(run_id, safe=""))

    def stop(self, run_id: str) -> dict[str, Any]:
        if not run_id:
            raise ValueError("run_id is required")
        return self._request("POST", "/v1/runs/" + quote(run_id, safe="") + "/stop", {})

    @staticmethod
    def completed_output(status: dict[str, Any]) -> str | None:
        state = status.get("status")
        if state in {"failed", "cancelled", "interrupted"}:
            raise TransportError("Hermes ended without a successful final result: " + str(state))
        if state != "completed":
            return None  # Includes waiting_for_approval and stopping.
        output = status.get("output")
        if not isinstance(output, str):
            raise TransportError("Completed run did not provide a string output")
        return output  # Downstream must validate its proposal schema before actions.
