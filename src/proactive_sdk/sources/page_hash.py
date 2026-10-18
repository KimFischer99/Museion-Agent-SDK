"""Page-change source connector (W4c).

Watches one HTML page and reports a change only when the *content* moved
beyond a noise threshold: ``shingle``-based near-duplicate detection on the
readable text, with the layout chrome (``script``/``style``/``nav``/
``header``/``footer``/``aside``) removed first. An ad rotation or a
timestamp in the footer must not wake the user.

No model call: extraction, normalisation, shingling, simhash and the
Hamming comparison all happen here. The bounded cursor carries
``v1:<simhash hex>:<text hash>``; the first fetch is a baseline with zero
items, so a new watch never fires a false alarm.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from ..contracts import ErrorCode, PASError, SourceBatch, SourceItem, SourceRequest
from ..net import EgressBroker
from .feed import rfc3339, sha256_hex

__all__ = ["PageHashSource"]

_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ACCOUNT_MAX = 256
_CURSOR_PREFIX = "v1:"
MAX_BYTES = 5 * 1024 * 1024  # owner decision: 5 MiB for pages
#: Bound on extracted text before shingling (a hostile page cannot force
#: unbounded CPU here; the broker already bounds the bytes).
MAX_TEXT_CHARS = 200_000
#: Hamming distance above which a simhash change counts as real. 8/64 ≈
#: 12.5% of bits: ad rotation and "last updated at" strings stay silent,
#: a rewritten paragraph does not.
DEFAULT_HAMMING_THRESHOLD = 8
DEFAULT_FRESH_TTL_S = 1800
DEFAULT_SHINGLE_SIZE = 5
#: Below this many shingles the page is too small for near-duplicate maths;
#: the connector then falls back to exact text-hash comparison.
MIN_SHINGLES = 8

_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "nav", "header", "footer", "aside", "svg", "template"}
)


class _ReadableText(HTMLParser):
    """Collect readable text, skipping non-content elements and the title."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        if name == "title":
            self._in_title = True
        elif name in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name == "title":
            self._in_title = False
        elif name in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth == 0:
            self.parts.append(data)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_TOKEN_RE = re.compile(r"[0-9a-z\u00c0-\u024f\u4e00-\u9fff]+")


def _tokens(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text.casefold())
    if len(tokens) >= 20:
        return tokens
    # Very short / heavily CJK pages: fall back to character 3-grams so the
    # shingle maths still has something to work with.
    compact = re.sub(r"\s+", "", text.casefold())
    return [compact[i : i + 3] for i in range(0, max(0, len(compact) - 2))]


def _shingle_hashes(text: str, shingle_size: int) -> list[int]:
    tokens = _tokens(text)
    if len(tokens) < shingle_size:
        return []
    hashes: list[int] = []
    for index in range(len(tokens) - shingle_size + 1):
        chunk = "\x1f".join(tokens[index : index + shingle_size])
        digest = hashlib.blake2b(chunk.encode("utf-8"), digest_size=8).digest()
        hashes.append(int.from_bytes(digest, "big"))
    return hashes


def _simhash(shingles: list[int]) -> int:
    """64-bit simhash over shingle hashes (deterministic, stdlib only)."""
    if not shingles:
        return 0
    weights = [0] * 64
    for value in shingles:
        for bit in range(64):
            if (value >> bit) & 1:
                weights[bit] += 1
            else:
                weights[bit] -= 1
    result = 0
    for bit in range(64):
        if weights[bit] > 0:
            result |= 1 << bit
    return result


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _parse_cursor(cursor: str | None) -> tuple[int, str] | None:
    if not cursor or not cursor.startswith(_CURSOR_PREFIX):
        return None
    parts = cursor[len(_CURSOR_PREFIX):].split(":")
    if len(parts) != 2:
        return None
    simhash_hex, text_hash = parts
    try:
        simhash_value = int(simhash_hex, 16)
    except ValueError:
        return None
    if not 0 <= simhash_value < (1 << 64) or not text_hash:
        return None
    return simhash_value, text_hash


class PageHashSource:
    """Report content changes of one HTML page above a noise threshold."""

    requires_explicit_selection = True

    def __init__(
        self,
        *,
        url: str,
        source_id: str = "page",
        account_ref: str = "account:public",
        allowed_hosts: tuple[str, ...] | list[str] | None = None,
        required_capability: str = "public.read",
        broker: EgressBroker | None = None,
        fresh_ttl_s: int = DEFAULT_FRESH_TTL_S,
        hamming_threshold: int = DEFAULT_HAMMING_THRESHOLD,
        max_excerpt_chars: int = 2000,
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
            ("hamming_threshold", hamming_threshold, 0, 64),
            ("max_excerpt_chars", max_excerpt_chars, 0, MAX_TEXT_CHARS),
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
        self.hamming_threshold = hamming_threshold
        self.max_excerpt_chars = max_excerpt_chars

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
                "allowed_hosts is mandatory for a new network source (no implicit allowlist)",
                scope="sources",
            )
        else:
            self.allowed_hosts = tuple(broker.allowed_hosts)

        host = urlsplit(self.url).hostname or ""
        if not any(host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts):
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"page host {host!r} is not covered by the allowlist {self.allowed_hosts!r}",
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
            "kind": "page_hash",
            "source_id": self.source_id,
            "account_ref": self.account_ref,
            "url": self.url,
            "allowed_hosts": list(self.allowed_hosts),
            "required_capability": self.required_capability,
            "requires_explicit_selection": self.requires_explicit_selection,
            "hamming_threshold": self.hamming_threshold,
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
            html = await asyncio.wait_for(
                asyncio.to_thread(self.broker.fetch_text, self.url), remaining_s
            )
        except TimeoutError as exc:
            raise PASError(
                ErrorCode.DEADLINE_EXCEEDED, "page fetch exceeded its deadline", scope="sources"
            ) from exc

        title, text = self._extract(html)
        observed_at = rfc3339(datetime.now(timezone.utc))
        shingles = _shingle_hashes(text, DEFAULT_SHINGLE_SIZE)
        shingle_hash = _simhash(shingles)
        text_hash = sha256_hex(text.encode("utf-8"), 32)
        cursor = f"{_CURSOR_PREFIX}{shingle_hash:016x}:{text_hash}"

        previous = _parse_cursor(request.cursor_ref)
        items: tuple[SourceItem, ...] = ()
        if previous is not None and request.cursor_ref != cursor:
            previous_shingle, previous_text = previous
            distance = _hamming(shingle_hash, previous_shingle)
            if len(shingles) < MIN_SHINGLES:
                # Too small for near-duplicate maths: exact change only.
                changed = text_hash != previous_text
            else:
                changed = distance > self.hamming_threshold
            if changed:
                items = (self._item_for(title, text, shingle_hash, text_hash, distance, observed_at),)

        return SourceBatch(
            source_id=self.source_id,
            account_ref=self.account_ref,
            observed_at=observed_at,
            items=items,
            cursor_ref=cursor,
            fresh_until=rfc3339(datetime.now(timezone.utc) + timedelta(seconds=self.fresh_ttl_s)),
        )

    def _extract(self, html: str) -> tuple[str, str]:
        parser = _ReadableText()
        try:
            parser.feed(html)
            parser.close()
        except Exception:  # noqa: BLE001 - hostile markup must not crash the source
            pass
        title = _normalize(" ".join(parser.title_parts))[:500]
        text = _normalize(" ".join(parser.parts))[:MAX_TEXT_CHARS]
        if not text:
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "page produced no readable text (empty or non-HTML response)",
                scope="sources",
            )
        return title, text

    def _item_for(
        self,
        title: str,
        text: str,
        shingle_hash: int,
        text_hash: str,
        distance: int,
        observed_at: str,
    ) -> SourceItem:
        excerpt = text[: self.max_excerpt_chars]
        content = json.dumps(
            {
                "kind": "page_hash",
                "url": self.url,
                "source_id": self.source_id,
                "account_ref": self.account_ref,
                "title": title,
                "excerpt": excerpt,
                "text_sha256": text_hash,
                "hamming_distance": distance,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        revision = "sha256:" + sha256_hex(
            json.dumps(
                {"simhash": f"{shingle_hash:016x}", "text": text_hash},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            32,
        )
        fact_id = "page:" + sha256_hex(self.url.encode("utf-8"), 24)
        return SourceItem(
            fact_id=fact_id,
            revision=revision,
            content=content,
            observed_at=observed_at,
            sensitivity="public",
        )
