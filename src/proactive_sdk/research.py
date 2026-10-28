"""Bounded public news search source over an RSS search endpoint."""

from __future__ import annotations

import asyncio
import json
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote, urlsplit

from .contracts import ErrorCode, PASError, SourceBatch, SourceItem, SourceRequest
from .net import EgressBroker

__all__ = ["NewsSearchSource"]

_DEFAULT_URL_TEMPLATE = "https://www.bing.com/news/search?q={query}&format=rss"
_MAX_FEED_CHARS = 1_048_576


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# The RSS/Atom parsing helpers live in ``sources/feed.py`` now: the
# generic RssSource and this news search must not drift apart.
from .sources.feed import (  # noqa: E402  (kept as aliases for readability)
    canonical_url as _canonical_url,
    child_text as _child_text,
    local_name as _local_name,
    plain_text as _plain_text,
    rfc3339 as _rfc3339,
    sha256_hex as _sha256,
)


class NewsSearchSource:
    """Search public headlines through one bounded RSS request per fetch."""

    source_id = "news-search"
    required_capability = "public.read"
    requires_explicit_selection = True

    def __init__(
        self,
        *,
        topics: Callable[[], tuple[str, ...] | list[str]],
        broker: EgressBroker,
        account_ref: str = "account:public",
        url_template: str = _DEFAULT_URL_TEMPLATE,
        max_results: int = 6,
        fresh_ttl_s: int = 600,
        max_age_hours: int | float = 168,
    ) -> None:
        if not callable(topics):
            raise PASError(ErrorCode.INVALID_CONFIG, "topics must be callable", scope="research")
        if not isinstance(broker, EgressBroker):
            raise PASError(ErrorCode.INVALID_CONFIG, "broker must be an EgressBroker", scope="research")
        if not isinstance(account_ref, str) or not 1 <= len(account_ref) <= 256:
            raise PASError(ErrorCode.INVALID_CONFIG, "account_ref must be 1..256 chars", scope="research")
        if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 20:
            raise PASError(ErrorCode.INVALID_CONFIG, "max_results must be 1..20", scope="research")
        if not isinstance(fresh_ttl_s, int) or isinstance(fresh_ttl_s, bool) or fresh_ttl_s <= 0:
            raise PASError(ErrorCode.INVALID_CONFIG, "fresh_ttl_s must be a positive integer", scope="research")
        if (
            not isinstance(max_age_hours, (int, float))
            or isinstance(max_age_hours, bool)
            or not math.isfinite(max_age_hours)
            or max_age_hours <= 0
        ):
            raise PASError(ErrorCode.INVALID_CONFIG, "max_age_hours must be positive and finite", scope="research")
        if not isinstance(url_template, str) or "{query}" not in url_template:
            raise PASError(ErrorCode.INVALID_CONFIG, "url_template must contain {query}", scope="research")
        try:
            template_parts = urlsplit(url_template)
            template_url = urlsplit(url_template.replace("{query}", "NASA"))
            _ = template_url.port
        except ValueError:
            template_parts = template_url = None
        if (
            template_parts is None
            or template_url is None
            or template_url.scheme.casefold() != "https"
            or not template_url.hostname
            or template_url.username is not None
            or template_url.password is not None
            or "{query}" not in template_parts.query
        ):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                "url_template must be HTTPS, credential-free, and place {query} in the query string",
                scope="research",
            )
        self.account_ref = account_ref
        self.topics = topics
        self.broker = broker
        self.url_template = url_template
        self.max_results = max_results
        self.fresh_ttl_s = fresh_ttl_s
        self.max_age_hours = float(max_age_hours)

    def describe(self) -> dict:
        """Machine-readable binding summary (registration/ledger/docs)."""
        return {
            "kind": "news-search",
            "source_id": self.source_id,
            "account_ref": self.account_ref,
            "required_capability": self.required_capability,
            "requires_explicit_selection": self.requires_explicit_selection,
            "max_results": self.max_results,
            "max_age_hours": self.max_age_hours,
        }

    async def fetch_delta(self, request: SourceRequest) -> SourceBatch:
        return await self._fetch(request, current=False)

    async def fetch_current(self, request: SourceRequest) -> SourceBatch:
        return await self._fetch(request, current=True)

    async def _fetch(self, request: SourceRequest, *, current: bool) -> SourceBatch:
        if request.source_id != self.source_id or request.account_ref != self.account_ref:
            raise PASError(ErrorCode.INVALID_CONFIG, "source binding does not match request", scope="research")
        now = _utc_now()
        deadline = datetime.fromisoformat(request.deadline.replace("Z", "+00:00"))
        remaining_s = (deadline - now).total_seconds()
        if remaining_s <= 0:
            raise PASError(ErrorCode.DEADLINE_EXCEEDED, "source request deadline has expired", scope="research")

        labels = request.research_topics or self.topics()
        if not isinstance(labels, (tuple, list)):
            raise PASError(ErrorCode.INVALID_CONFIG, "topics() must return a tuple or list", scope="research")
        if len(labels) > 8:
            raise PASError(ErrorCode.INVALID_CONFIG, "topics() may return at most 8 topics", scope="research")
        normalized: list[str] = []
        seen: set[str] = set()
        for label in labels:
            if not isinstance(label, str):
                raise PASError(ErrorCode.INVALID_CONFIG, "each topic must be a string", scope="research")
            topic = label.strip()
            if not 1 <= len(topic) <= 128:
                raise PASError(ErrorCode.INVALID_CONFIG, "each topic must be 1..128 chars", scope="research")
            key = topic.casefold()
            if key not in seen:
                normalized.append(topic)
                seen.add(key)

        observed = _rfc3339(now)
        if not normalized:
            if (deadline - _utc_now()).total_seconds() <= 0:
                raise PASError(ErrorCode.DEADLINE_EXCEEDED, "source request deadline has expired", scope="research")
            return SourceBatch(
                source_id=self.source_id,
                account_ref=self.account_ref,
                observed_at=observed,
                items=(),
                cursor_ref=request.cursor_ref,
                fresh_until=_rfc3339(now + timedelta(seconds=self.fresh_ttl_s)),
            )

        quoted = [json.dumps(topic, ensure_ascii=False) for topic in normalized]
        query = quote(" OR ".join(quoted), safe="")
        url = self.url_template.replace("{query}", query)
        remaining_s = (deadline - _utc_now()).total_seconds()
        if remaining_s <= 0:
            raise PASError(ErrorCode.DEADLINE_EXCEEDED, "source request deadline has expired", scope="research")
        try:
            feed_text = await asyncio.wait_for(asyncio.to_thread(self.broker.fetch_text, url), remaining_s)
        except TimeoutError as exc:
            raise PASError(ErrorCode.DEADLINE_EXCEEDED, "source fetch exceeded its deadline", scope="research") from exc

        observed_dt = _utc_now()
        observed = _rfc3339(observed_dt)
        if len(feed_text) > _MAX_FEED_CHARS:
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "RSS response exceeds 1 MiB", scope="research")
        try:
            root = ET.fromstring(feed_text)
        except ET.ParseError as exc:
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "provider response is not valid RSS XML", scope="research") from exc
        channel = next((node for node in root if _local_name(node.tag) == "channel"), None)
        if _local_name(root.tag) != "rss" or channel is None:
            raise PASError(ErrorCode.PROVIDER_UNAVAILABLE, "provider response is not an RSS feed", scope="research")

        items_by_id: dict[str, tuple[tuple[str, str], SourceItem]] = {}
        max_age = timedelta(hours=self.max_age_hours)
        for raw in channel:
            if _local_name(raw.tag) != "item":
                continue
            title = _child_text(raw, "title")[:1000]
            canonical_url = _canonical_url(_child_text(raw, "link"))
            date_text = _child_text(raw, "pubDate")
            if not title or canonical_url is None or not date_text:
                continue
            try:
                published = parsedate_to_datetime(date_text)
            except (TypeError, ValueError, OverflowError):
                continue
            if published.tzinfo is None:
                continue
            published = published.astimezone(timezone.utc)
            if published > observed_dt or observed_dt - published > max_age:
                continue
            published_at = _rfc3339(published)
            summary = _plain_text(_child_text(raw, "description"))
            stable_fields = {
                "description": summary,
                "published_at": published_at,
                "title": title,
                "url": canonical_url,
            }
            revision = "sha256:" + _sha256(
                json.dumps(stable_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                32,
            )
            fact_id = "news:" + _sha256(canonical_url.encode("utf-8"), 24)
            content = json.dumps(
                {
                    **stable_fields,
                    "candidate_topics": normalized,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            item = SourceItem(
                fact_id=fact_id,
                revision=revision,
                content=content,
                observed_at=observed,
                sensitivity="public",
            )
            rank = (published_at, revision)
            previous = items_by_id.get(fact_id)
            if previous is None or rank > previous[0]:
                items_by_id[fact_id] = (rank, item)

        items = sorted((entry[1] for entry in items_by_id.values()), key=lambda item: (item.fact_id, item.revision))
        items = items[: min(self.max_results, request.max_items)]
        if not items:
            cursor = request.cursor_ref
        else:
            pairs = [(item.fact_id, item.revision) for item in items]
            cursor = "sha256:" + _sha256(
                json.dumps(pairs, separators=(",", ":")).encode("utf-8"), 32
            )
        returned_items = () if not current and cursor == request.cursor_ref else tuple(items)
        return SourceBatch(
            source_id=self.source_id,
            account_ref=self.account_ref,
            observed_at=observed,
            items=returned_items,
            cursor_ref=cursor,
            fresh_until=_rfc3339(observed_dt + timedelta(seconds=self.fresh_ttl_s)),
        )
