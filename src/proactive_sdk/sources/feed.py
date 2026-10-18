"""Shared, dependency-free RSS/Atom parsing for source connectors.

Extracted from ``research.py`` so every feed-shaped connector (the Bing
news search and the generic ``RssSource``) shares one parser. The helpers
are deliberately identical to the ones the existing news source shipped
with: that source's behaviour must not drift while the generic connector
is added next to it.

Parsing is pure: no network, no store, no clock beyond ``observed_at``.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..contracts import ErrorCode, PASError, SourceItem

__all__ = [
    "MAX_FEED_CHARS",
    "canonical_url",
    "child_text",
    "cursor_for",
    "local_name",
    "parse_feed_items",
    "plain_text",
    "rfc3339",
    "sha256_hex",
]

#: A feed larger than this is refused before XML parsing (the egress broker
#: already bounds the response at 5 MiB; this parser budget sits above it so
#: the transport cap is the effective limit, not a lower parser surprise).
MAX_FEED_CHARS = 8 * 1024 * 1024

_FACT_PREFIX = "feed:"


def rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def child_text(element: ET.Element, *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for child in element:
        if local_name(child.tag) in wanted:
            if local_name(child.tag) == "link" and child.get("href"):
                return child.get("href", "").strip()
            return "".join(child.itertext()).strip()
    return ""


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _TextOnly()
    parser.feed(value)
    parser.close()
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:1000]


def canonical_url(value: str) -> str | None:
    try:
        parts = urlsplit(value.strip())
        if (
            parts.scheme.casefold() not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or any(char.isspace() for char in parts.netloc)
        ):
            return None
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.casefold()
    host = parts.hostname.casefold()
    netloc = host if port is None or (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def sha256_hex(value: bytes, size: int = 64) -> str:
    return hashlib.sha256(value).hexdigest()[:size]


def _entry_link(element: ET.Element) -> str:
    """First usable link: RSS ``<link>text</link>`` or Atom ``<link href>``."""
    for child in element:
        if local_name(child.tag) != "link":
            continue
        rel = (child.get("rel") or "alternate").casefold()
        if rel not in ("alternate", "self"):
            continue
        href = child.get("href")
        if href:
            return href.strip()
        text = "".join(child.itertext()).strip()
        if text:
            return text
    return ""


def _published_at(element: ET.Element) -> str | None:
    raw = child_text(element, "pubdate", "published", "updated", "date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return rfc3339(parsed)


def _atom_or_rss_entries(root: ET.Element) -> tuple[str, list[ET.Element]]:
    name = local_name(root.tag)
    if name == "rss":
        channel = next((node for node in root if local_name(node.tag) == "channel"), None)
        if channel is None:
            return "rss", []
        return "rss", [node for node in channel if local_name(node.tag) == "item"]
    if name == "feed":
        return "atom", [node for node in root if local_name(node.tag) == "entry"]
    raise PASError(
        ErrorCode.PROVIDER_UNAVAILABLE,
        "provider response is neither an RSS nor an Atom feed",
        scope="sources",
    )


def parse_feed_items(
    text: str,
    *,
    source_id: str,
    account_ref: str,
    observed_at: str,
    max_items: int,
) -> list[SourceItem]:
    """Parse one RSS/Atom document into stable, de-duplicated source items.

    Deterministic by construction: ``fact_id`` is derived from the entry's
    own identity (guid/id, else canonical URL), ``revision`` from the stable
    content fields, and the result is sorted newest-first then by fact id.
    Malformed entries are skipped rather than poisoning the whole fetch.
    """
    if not isinstance(text, str):
        raise PASError(ErrorCode.INVALID_CONFIG, "feed text must be a string", scope="sources")
    if len(text) > MAX_FEED_CHARS:
        raise PASError(
            ErrorCode.PROVIDER_UNAVAILABLE, "feed response exceeds parser budget", scope="sources"
        )
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise PASError(
            ErrorCode.PROVIDER_UNAVAILABLE, "provider response is not valid XML", scope="sources"
        ) from exc

    kind, entries = _atom_or_rss_entries(root)
    items_by_id: dict[str, tuple[tuple[str, str], SourceItem]] = {}
    for entry in entries:
        title = child_text(entry, "title").strip()[:1000]
        identity = child_text(entry, "guid", "id").strip()
        url = canonical_url(_entry_link(entry) or identity)
        if not title or url is None:
            # An entry without a usable title/link is not actionable data.
            continue
        published_at = _published_at(entry)
        summary = plain_text(
            child_text(entry, "description", "summary", "content", "encoded")
        )
        stable_fields = {
            "title": title,
            "url": url,
            "published_at": published_at,
            "summary": summary,
        }
        revision = "sha256:" + sha256_hex(
            json.dumps(stable_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
            32,
        )
        fact_id = _FACT_PREFIX + sha256_hex(
            json.dumps(
                {"kind": kind, "identity": identity or url},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            24,
        )
        content = json.dumps(
            {**stable_fields, "kind": kind, "source_id": source_id, "account_ref": account_ref},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        item = SourceItem(
            fact_id=fact_id,
            revision=revision,
            content=content,
            observed_at=observed_at,
            sensitivity="public",
        )
        rank = (published_at or "", revision)
        previous = items_by_id.get(fact_id)
        if previous is None or rank > previous[0]:
            items_by_id[fact_id] = (rank, item)

    ordered = sorted(
        items_by_id.values(),
        key=lambda pair: (pair[0][0], pair[0][1]),
        reverse=True,
    )
    return [item for _rank, item in ordered[: max(0, int(max_items))]]


def cursor_for(items: list[SourceItem] | tuple[SourceItem, ...]) -> str:
    """One deterministic cursor over the visible item set."""
    pairs = [(item.fact_id, item.revision) for item in items]
    return "sha256:" + sha256_hex(
        json.dumps(pairs, separators=(",", ":")).encode("utf-8"), 32
    )
