"""Source connectors for the §11.5 first-version loop: mail reading,
calendar reading, and public material tracking (P6).

实现契约：

- 每个 connector 实现 ``contracts.Source``（fetch_delta），喂给 P3 的
  SourceRegistry / L0-L1 coordinator；
- **未连接如实上抛 AUTH_REQUIRED**（携带机器原因；connect URL 属于要展示
  给用户的 payload，连接器不发明、不代点、不存凭据——不得模拟已授权）；
- provider 数据到 SourceItem 的映射是显式字段映射，缺失字段为 None /
  空，不编造；fact_id 带来源前缀，revision 用内容 hash，天然去重；
- 公开资料跟踪走 ``net.EgressBroker``（域名 allowlist + IP 钉扎），
  内容 hash 变化才产出新 revision。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .contracts import ErrorCode, PASError, SourceBatch, SourceItem, SourceRequest
from .gws import GwsAdapter, GwsResult
from .net import EgressBroker

__all__ = ["GmailMailSource", "CalendarAgendaSource", "PublicMaterialSource"]

_MAX_SNIPPET_CHARS = 4000


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cursor_for(items: list[SourceItem]) -> str:
    """Delta cursor: hash of the page's (fact_id, revision) pairs. A fetch
    whose cursor equals the stored one is unchanged — connectors return an
    empty delta and the coordinator's L0 suppresses without a model call."""
    pairs = ",".join(f"{item.fact_id}:{item.revision}" for item in items)
    return "sha256:" + hashlib.sha256(pairs.encode("utf-8")).hexdigest()[:32]


def _delta_response(
    request: SourceRequest, items: list[SourceItem], *, observed: str
) -> SourceBatch:
    cursor = _cursor_for(items)
    if request.cursor_ref == cursor:
        return SourceBatch(
            source_id=request.source_id,
            account_ref=request.account_ref,
            observed_at=observed,
            items=(),
            cursor_ref=cursor,
        )
    return SourceBatch(
        source_id=request.source_id,
        account_ref=request.account_ref,
        observed_at=observed,
        items=tuple(items),
        cursor_ref=cursor,
    )


def _require_connected(result: GwsResult, service: str) -> dict[str, Any]:
    """Status gate before any read. Not-connected raises honestly."""
    if not result.ok:
        raise PASError(
            ErrorCode.AUTH_REQUIRED if result.error_code == "reauth_required"
            else ErrorCode.PROVIDER_UNAVAILABLE,
            f"{service} status: {result.error_code}",
            scope="connectors",
        )
    data = result.data
    if data.get("connected") is not True:
        detail = "not connected"
        if data.get("unavailable"):
            detail = "connector unavailable (provider offered no connect URL)"
        elif data.get("connect_url"):
            detail = "not connected; a provider connect_url is available to surface to the owner"
        raise PASError(ErrorCode.AUTH_REQUIRED, f"{service} {detail}", scope="connectors")
    return data


class GmailMailSource:
    """Mail reading via ``gmail +triage`` / ``+read`` (read-only)."""

    source_id = "gmail"

    def __init__(self, *, adapter: GwsAdapter, account_ref: str) -> None:
        if not isinstance(adapter, GwsAdapter):
            raise PASError(ErrorCode.INVALID_CONFIG, "adapter must be a GwsAdapter", scope="connectors")
        self._adapter = adapter
        self.account_ref = account_ref

    async def fetch_delta(self, request: SourceRequest) -> SourceBatch:
        if request.account_ref != self.account_ref:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "account_ref does not match the binding", scope="connectors"
            )
        query = str(request.scope.get("query") or "is:unread newer_than:1d")
        max_items = int(request.scope.get("max") or min(request.max_items, 20))
        status = self._adapter.execute(["hatch_gws_cli", "gmail", "status"])
        _require_connected(status, "gmail")
        result = self._adapter.execute(
            ["hatch_gws_cli", "gmail", "+triage", "--query", query, "--max", str(max_items),
             "--format", "json"]
        )
        if not result.ok:
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE, f"gmail +triage: {result.error_code}", scope="connectors"
            )
        messages = result.data.get("messages") if isinstance(result.data.get("messages"), list) else []
        observed = _now_rfc3339()
        items: list[SourceItem] = []
        for raw in messages[: request.max_items]:
            if not isinstance(raw, dict):
                continue
            message_id = str(raw.get("id") or raw.get("threadId") or "")[:256]
            if not message_id:
                continue
            parts = [
                f"From: {raw.get('from', '')}",
                f"Subject: {raw.get('subject', '')}",
                f"Date: {raw.get('date', '')}",
            ]
            snippet = raw.get("snippet") or raw.get("body") or ""
            if isinstance(snippet, str) and snippet:
                parts.append(snippet[:_MAX_SNIPPET_CHARS])
            content = "\n".join(p for p in parts if p.split(": ", 1)[-1])
            items.append(
                SourceItem(
                    fact_id=f"gmail:{message_id}",
                    revision="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:32],
                    content=content,
                    observed_at=observed,
                    sensitivity="private",
                )
            )
        return _delta_response(request, items, observed=observed)


class CalendarAgendaSource:
    """Calendar reading via ``calendar +agenda`` (read-only)."""

    source_id = "google-calendar"

    def __init__(self, *, adapter: GwsAdapter, account_ref: str) -> None:
        if not isinstance(adapter, GwsAdapter):
            raise PASError(ErrorCode.INVALID_CONFIG, "adapter must be a GwsAdapter", scope="connectors")
        self._adapter = adapter
        self.account_ref = account_ref

    async def fetch_delta(self, request: SourceRequest) -> SourceBatch:
        if request.account_ref != self.account_ref:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "account_ref does not match the binding", scope="connectors"
            )
        days = int(request.scope.get("days") or 1)
        status = self._adapter.execute(["hatch_gws_cli", "calendar", "status"])
        _require_connected(status, "calendar")
        result = self._adapter.execute(
            ["hatch_gws_cli", "calendar", "+agenda", "--days", str(days), "--format", "json"]
        )
        if not result.ok:
            raise PASError(
                ErrorCode.PROVIDER_UNAVAILABLE, f"calendar +agenda: {result.error_code}", scope="connectors"
            )
        events = result.data.get("events") if isinstance(result.data.get("events"), list) else []
        observed = _now_rfc3339()
        items: list[SourceItem] = []
        for raw in events[: request.max_items]:
            if not isinstance(raw, dict):
                continue
            event_id = str(raw.get("id") or raw.get("eventId") or "")[:256]
            if not event_id:
                continue
            content = json.dumps(
                {
                    "start": raw.get("start"),
                    "end": raw.get("end"),
                    "title": raw.get("summary") or raw.get("title") or "",
                    "location": raw.get("location") or "",
                    "response": raw.get("responseStatus") or raw.get("selfStatus") or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            items.append(
                SourceItem(
                    fact_id=f"gcal:{event_id}",
                    revision="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:32],
                    content=content,
                    observed_at=observed,
                    sensitivity="private",
                )
            )
        return _delta_response(request, items, observed=observed)


class PublicMaterialSource:
    """公开资料跟踪：allowlist 限定的只读网页获取，内容 hash 变化才产出
    新 revision（资料没有改写权限；跟踪 ≠ 抓取任意域）。Delta 语义与其余
    连接器一致：cursor 是页面 (fact_id, revision) 的 hash，存于
    source_state，崩溃恢复后依旧正确。"""

    source_id = "public-material"

    def __init__(self, *, account_ref: str, broker: EgressBroker, url: str) -> None:
        if not isinstance(broker, EgressBroker):
            raise PASError(ErrorCode.INVALID_CONFIG, "broker must be an EgressBroker", scope="connectors")
        self._broker = broker
        self._url = url
        self.account_ref = account_ref

    async def fetch_delta(self, request: SourceRequest) -> SourceBatch:
        if request.account_ref != self.account_ref:
            raise PASError(
                ErrorCode.INVALID_CONFIG, "account_ref does not match the binding", scope="connectors"
            )
        text = self._broker.fetch_text(self._url)
        observed = _now_rfc3339()
        revision = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        fact_id = "material:" + hashlib.sha256(self._url.encode("utf-8")).hexdigest()[:24]
        item = SourceItem(
            fact_id=fact_id,
            revision=revision,
            content=text[: 100_000],
            observed_at=observed,
            sensitivity="public",
        )
        return _delta_response(request, [item], observed=observed)
