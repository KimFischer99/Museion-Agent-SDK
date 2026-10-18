"""Sitemap-diff source connector (W4b).

Reads a ``sitemap.xml`` (or a one-level ``sitemapindex``) and reports the current
bounded entry set when the sitemap changes. No model call:
the whole diff is computed by this connector.

Stateless by design: the only state PAS keeps for a source is
``source_state.cursor_ref`` (bounded TEXT, passed back on the next
``SourceRequest``), so the cursor carries a compact summary instead of the
whole URL table::

    v1:<url-set hash>:<max lastmod epoch ms>

* first fetch (no cursor) → baseline only, zero items (never a false alarm);
* unchanged URL set + unchanged ``lastmod`` → zero items;
* any URL/``lastmod`` change → current entries (bounded by ``max_items``).

Removed URLs are not emitted, but removals can trigger a recheck of remaining
entries. This compact cursor does not identify an exact per-URL delta.
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from ..contracts import ErrorCode, PASError, SourceBatch, SourceItem, SourceRequest
from ..net import EgressBroker
from .feed import canonical_url, local_name, rfc3339, sha256_hex

__all__ = ["SitemapDiffSource"]

_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ACCOUNT_MAX = 256
_CURSOR_PREFIX = "v1:"
MAX_BYTES = 5 * 1024 * 1024  # owner decision: 5 MiB for sitemaps
DEFAULT_FRESH_TTL_S = 3600
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_CHILDREN = 5


def _parse_iso_ms(value: str) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1000)


def _parse_cursor(cursor: str | None) -> tuple[str, int] | None:
    if not cursor or not cursor.startswith(_CURSOR_PREFIX):
        return None
    parts = cursor[len(_CURSOR_PREFIX):].split(":")
    if len(parts) != 2:
        return None
    digest, raw_max = parts
    if not digest or len(digest) > 64:
        return None
    try:
        watermark = int(raw_max)
    except ValueError:
        return None
    if watermark < 0:
        return None
    return digest, watermark


def _cursor_for(url_hashes: str, watermark_ms: int) -> str:
    return f"{_CURSOR_PREFIX}{url_hashes}:{watermark_ms}"


class SitemapDiffSource:
    """Recheck current entries when a sitemap changes (zero model calls)."""

    requires_explicit_selection = True

    def __init__(
        self,
        *,
        url: str,
        source_id: str = "sitemap",
        account_ref: str = "account:public",
        allowed_hosts: tuple[str, ...] | list[str] | None = None,
        required_capability: str = "public.read",
        broker: EgressBroker | None = None,
        fresh_ttl_s: int = DEFAULT_FRESH_TTL_S,
        max_items: int = DEFAULT_MAX_ITEMS,
        max_children: int = DEFAULT_MAX_CHILDREN,
    ) -> None:
        if not isinstance(source_id, str) or not _SOURCE_ID_RE.fullmatch(source_id):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"source_id {source_id!r} fails naming rule [a-z][a-z0-9_-]{{0,63}}",
                scope="sources",
            )
        if not isinstance(account_ref, str) or not 1 <= len(account_ref) <= _ACCOUNT_MAX:
            raise PASError(
                ErrorCode.INVALID_CONFIG, f"account_ref must be 1..{_ACCOUNT_MAX} chars", scope="sources"
            )
        if not isinstance(required_capability, str) or not required_capability:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "required_capability must be a non-empty string", scope="sources"
            )
        for name, value, low, high in (
            ("fresh_ttl_s", fresh_ttl_s, 1, 10 ** 9),
            ("max_items", max_items, 1, 1000),
            ("max_children", max_children, 1, 20),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise PASError(
                    ErrorCode.INVALID_CONFIG, f"{name} must be an integer in {low}..{high}", scope="sources"
                )
        self.url = self._validate_url(url)
        self.source_id = source_id
        self.account_ref = account_ref
        self.required_capability = required_capability
        self.fresh_ttl_s = fresh_ttl_s
        self.max_items = max_items
        self.max_children = max_children

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
                " (no implicit allowlist; the sitemap and its children must be named)",
                scope="sources",
            )
        else:
            self.allowed_hosts = tuple(broker.allowed_hosts)

        host = urlsplit(self.url).hostname or ""
        if not any(host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"sitemap host {host!r} is not covered by the allowlist {self.allowed_hosts!r}",
                scope="sources",
            )
        if broker is not None:
            if not isinstance(broker, EgressBroker):
                raise PASError(ErrorCode.INVALID_CONFIG, "broker must be an EgressBroker", scope="sources")
            self.broker = broker
        else:
            self.broker = EgressBroker(allowed_hosts=self.allowed_hosts, max_bytes=MAX_BYTES)

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
        return {
            "kind": "sitemap_diff",
            "source_id": self.source_id,
            "account_ref": self.account_ref,
            "url": self.url,
            "allowed_hosts": list(self.allowed_hosts),
            "required_capability": self.required_capability,
            "requires_explicit_selection": self.requires_explicit_selection,
            "max_children": self.max_children,
        }

    # ------------------------------------------------------------------ #
    # Fetch
    # ------------------------------------------------------------------ #

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

        entries = await self._collect_entries(deadline)
        observed_at = rfc3339(datetime.now(timezone.utc))

        # URL-set hash and lastmod watermark: both fit comfortably in the
        # bounded cursor, unlike the URL table itself.
        canonical_pairs = sorted((loc, lastmod_ms or 0) for loc, lastmod_ms in entries.items())
        url_hashes = sha256_hex(
            json.dumps(canonical_pairs, separators=(",", ":")).encode("utf-8"), 32
        )
        watermark_ms = max((lastmod or 0) for lastmod in entries.values()) if entries else 0
        cursor = _cursor_for(url_hashes, watermark_ms)

        previous = _parse_cursor(request.cursor_ref)
        if previous is None:
            # First fetch (or an unreadable legacy cursor): baseline only.
            items: tuple[SourceItem, ...] = ()
        elif request.cursor_ref == cursor:
            items = ()
        else:
            # shortcut: compact cursors cannot identify exact URL deltas;
            # store per-URL state if exact incrementality becomes required.
            items = tuple(
                self._item_for(loc, lastmod, observed_at)
                for loc, lastmod in list(entries.items())[: self.max_items]
            )

        return SourceBatch(
            source_id=self.source_id,
            account_ref=self.account_ref,
            observed_at=observed_at,
            items=items,
            cursor_ref=cursor,
            fresh_until=rfc3339(datetime.now(timezone.utc) + timedelta(seconds=self.fresh_ttl_s)),
        )

    async def _collect_entries(self, deadline: datetime) -> dict[str, int | None]:
        text = await self._fetch_text(self.url, deadline)
        root = self._parse_xml(text)
        kind = local_name(root.tag)
        entries: dict[str, int | None] = {}
        if kind == "sitemapindex":
            children = [
                self._loc_of(node)
                for node in root
                if local_name(node.tag) == "sitemap"
            ]
            children = [child for child in children if child][: self.max_children]
            for child in children:
                child_entries = self._urlset_entries(self._parse_xml(await self._fetch_text(child, deadline)))
                for loc, lastmod in child_entries:
                    entries[loc] = max(
                        (existing for existing in (entries.get(loc), lastmod) if existing is not None),
                        default=None,
                    )
            return entries
        if kind != "urlset":
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "provider response is not a sitemap urlset or sitemapindex",
                scope="sources",
            )
        for loc, lastmod in self._urlset_entries(root):
            entries[loc] = max(
                (existing for existing in (entries.get(loc), lastmod) if existing is not None),
                default=None,
            )
        return entries

    @staticmethod
    def _parse_xml(text: str) -> ET.Element:
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE, "sitemap is not valid XML", scope="sources"
            ) from exc

    @staticmethod
    def _loc_of(element: ET.Element) -> str | None:
        for child in element:
            if local_name(child.tag) == "loc":
                return canonical_url("".join(child.itertext()).strip())
        return None

    @classmethod
    def _urlset_entries(cls, root: ET.Element) -> list[tuple[str, int | None]]:
        if local_name(root.tag) != "urlset":
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "child sitemap is not a urlset",
                scope="sources",
            )
        found: list[tuple[str, int | None]] = []
        for node in root:
            if local_name(node.tag) != "url":
                continue
            loc = cls._loc_of(node)
            if loc is None:
                continue
            lastmod: int | None = None
            for child in node:
                if local_name(child.tag) == "lastmod":
                    lastmod = _parse_iso_ms("".join(child.itertext()))
                    break
            found.append((loc, lastmod))
        return found

    async def _fetch_text(self, url: str, deadline: datetime) -> str:
        host = urlsplit(url).hostname or ""
        if not any(host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts):
            raise PASError(
                ErrorCode.PERMISSION_DENIED,
                f"sitemap child {url!r} is not covered by the allowlist",
                scope="sources",
            )
        remaining_s = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining_s <= 0:
            raise PASError(
                ErrorCode.DEADLINE_EXCEEDED, "source request deadline has expired", scope="sources"
            )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.broker.fetch_text, url), remaining_s
            )
        except TimeoutError as exc:
            raise PASError(
                ErrorCode.DEADLINE_EXCEEDED, "sitemap fetch exceeded its deadline", scope="sources"
            ) from exc

    def _item_for(self, loc: str, lastmod_ms: int | None, observed_at: str) -> SourceItem:
        stable_fields = {"loc": loc, "lastmod_ms": lastmod_ms, "kind": "sitemap_diff"}
        revision = "sha256:" + sha256_hex(
            json.dumps(stable_fields, sort_keys=True, separators=(",", ":")).encode("utf-8"), 32
        )
        fact_id = "sitemap:" + sha256_hex(loc.encode("utf-8"), 24)
        return SourceItem(
            fact_id=fact_id,
            revision=revision,
            content=json.dumps(
                {**stable_fields, "source_id": self.source_id, "account_ref": self.account_ref},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            observed_at=observed_at,
            sensitivity="public",
        )
