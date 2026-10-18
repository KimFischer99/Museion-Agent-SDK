"""Generic RSS/Atom source connector (W4a).

One HTTPS feed URL per source binding. The security boundary is the same
``EgressBroker`` every other source uses: explicit host allowlist, HTTPS
only, private/loopback addresses refused, bounded response size, bounded
timeouts, no redirect following.

Programmatic registration (the v2 decision: no config-file schema change)::

    from proactive_sdk.sources import RssSource

    registry.register(
        source_id="museum-news",
        account_ref="account:public",
        required_capability="public.read",
        source=RssSource(
            url="https://museum.example.org/feed.xml",
            source_id="museum-news",
            allowed_hosts=("museum.example.org",),
        ),
    )

``requires_explicit_selection`` is True: a new network source is never
scanned by an untargeted heartbeat; a job opts in with
``task.refresh_source_ids``. Registering nothing means no network surface.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from ..contracts import ErrorCode, PASError, SourceBatch, SourceRequest
from ..net import EgressBroker
from .feed import cursor_for, parse_feed_items, rfc3339

__all__ = ["RssSource"]

_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ACCOUNT_MAX = 256
#: Feed bodies are text; 2 MiB is the v2 recommendation for page-shaped
#: sources. The broker enforces it before the body reaches the parser.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_FRESH_TTL_S = 600
DEFAULT_MAX_ITEMS = 50


class RssSource:
    """Read one RSS/Atom feed through the bounded egress broker."""

    #: Never picked up by an untargeted heartbeat: a job must name it in
    #: ``task.refresh_source_ids`` (default-off network surface).
    requires_explicit_selection = True

    def __init__(
        self,
        *,
        url: str,
        source_id: str = "rss",
        account_ref: str = "account:public",
        allowed_hosts: tuple[str, ...] | list[str] | None = None,
        required_capability: str = "public.read",
        broker: EgressBroker | None = None,
        fresh_ttl_s: int = DEFAULT_FRESH_TTL_S,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> None:
        if not isinstance(source_id, str) or not _SOURCE_ID_RE.fullmatch(source_id):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"source_id {source_id!r} fails naming rule [a-z][a-z0-9_-]{{0,63}}",
                scope="sources",
            )
        if not isinstance(account_ref, str) or not 1 <= len(account_ref) <= _ACCOUNT_MAX:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"account_ref must be 1..{_ACCOUNT_MAX} chars",
                scope="sources",
            )
        if not isinstance(required_capability, str) or not required_capability:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "required_capability must be a non-empty string",
                scope="sources",
            )
        if not isinstance(fresh_ttl_s, int) or isinstance(fresh_ttl_s, bool) or fresh_ttl_s <= 0:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "fresh_ttl_s must be a positive integer", scope="sources"
            )
        if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 1000:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "max_items must be 1..1000", scope="sources"
            )
        self.url = self._validate_url(url)
        self.source_id = source_id
        self.account_ref = account_ref
        self.required_capability = required_capability
        self.fresh_ttl_s = fresh_ttl_s
        self.max_items = max_items

        if allowed_hosts is not None:
            hosts = tuple(allowed_hosts)
            if not hosts or any(not isinstance(host, str) or not host.strip() for host in hosts):
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    "allowed_hosts must be a non-empty list of hostnames",
                    scope="sources",
                )
            self.allowed_hosts = tuple(host.strip().lower().rstrip(".") for host in hosts)
        elif broker is None:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "allowed_hosts is mandatory for a new network source"
                " (register the host explicitly; there is no implicit allowlist)",
                scope="sources",
            )
        else:
            self.allowed_hosts = tuple(broker.allowed_hosts)

        host = urlsplit(self.url).hostname or ""
        if not any(host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"feed host {host!r} is not covered by the allowlist {self.allowed_hosts!r}",
                scope="sources",
            )
        if broker is not None:
            if not isinstance(broker, EgressBroker):
                raise PASError(ErrorCode.INVALID_CONFIG, "broker must be an EgressBroker", scope="sources")
            self.broker = broker
        else:
            self.broker = EgressBroker(
                allowed_hosts=self.allowed_hosts,
                max_bytes=DEFAULT_MAX_BYTES,
            )

    @staticmethod
    def _validate_url(url: str) -> str:
        if not isinstance(url, str) or not url.strip():
            raise PASError(ErrorCode.INVALID_CONFIG, "url must be a non-empty string", scope="sources")
        candidate = url.strip()
        try:
            parts = urlsplit(candidate)
            _ = parts.port
        except ValueError:
            raise PASError(ErrorCode.INVALID_CONFIG, f"url {url!r} is malformed", scope="sources") from None
        if (
            parts.scheme.casefold() != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or any(char.isspace() for char in parts.netloc)
        ):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "url must be an HTTPS URL with a bare hostname and no credentials",
                scope="sources",
            )
        return candidate

    def describe(self) -> dict[str, Any]:
        """Machine-readable binding summary (registration/ledger/docs)."""
        return {
            "kind": "rss",
            "source_id": self.source_id,
            "account_ref": self.account_ref,
            "url": self.url,
            "allowed_hosts": list(self.allowed_hosts),
            "required_capability": self.required_capability,
            "requires_explicit_selection": self.requires_explicit_selection,
            "fresh_ttl_s": self.fresh_ttl_s,
        }

    async def fetch_delta(self, request: SourceRequest) -> SourceBatch:
        if request.source_id != self.source_id or request.account_ref != self.account_ref:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "source binding does not match request", scope="sources"
            )
        now = datetime.now(timezone.utc)
        try:
            deadline = datetime.fromisoformat(request.deadline.replace("Z", "+00:00"))
        except ValueError:
            raise PASError(ErrorCode.INVALID_CONFIG, "request deadline is not RFC 3339", scope="sources") from None
        remaining_s = (deadline - now).total_seconds()
        if remaining_s <= 0:
            raise PASError(
                ErrorCode.DEADLINE_EXCEEDED, "source request deadline has expired", scope="sources"
            )
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(self.broker.fetch_text, self.url), remaining_s
            )
        except TimeoutError as exc:
            raise PASError(
                ErrorCode.DEADLINE_EXCEEDED, "feed fetch exceeded its deadline", scope="sources"
            ) from exc

        observed_at = rfc3339(datetime.now(timezone.utc))
        items = parse_feed_items(
            text,
            source_id=self.source_id,
            account_ref=self.account_ref,
            observed_at=observed_at,
            max_items=min(self.max_items, request.max_items),
        )
        cursor = cursor_for(items)
        changed = cursor != request.cursor_ref
        return SourceBatch(
            source_id=self.source_id,
            account_ref=self.account_ref,
            observed_at=observed_at,
            items=tuple(items) if changed else (),
            cursor_ref=cursor,
            fresh_until=rfc3339(datetime.now(timezone.utc) + timedelta(seconds=self.fresh_ttl_s)),
        )
