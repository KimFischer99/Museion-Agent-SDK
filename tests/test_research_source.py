from __future__ import annotations

import asyncio
import html
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.contracts import ErrorCode, PASError, SourceRequest
from proactive_sdk.net import EgressBroker
from proactive_sdk.research import NewsSearchSource
from proactive_sdk import ProactiveAgent


class ScriptedBroker(EgressBroker):
    def __init__(self, *bodies: str) -> None:
        super().__init__(allowed_hosts=("www.bing.com",))
        self.bodies = list(bodies)
        self.calls: list[str] = []

    def fetch_text(self, url: str) -> str:
        self.calls.append(url)
        return self.bodies.pop(0)


def _date(delta: timedelta = timedelta(minutes=-5)) -> str:
    return format_datetime(datetime.now(timezone.utc) + delta, usegmt=True)


def _rss(*items: dict[str, str]) -> str:
    contents = []
    for item in items:
        contents.append(
            "<item>"
            f"<title>{html.escape(item.get('title', 'Headline'))}</title>"
            f"<link>{html.escape(item.get('link', 'https://news.example/story/1'))}</link>"
            f"<description>{html.escape(item.get('description', 'Summary'))}</description>"
            f"<pubDate>{html.escape(item.get('pubDate', _date()))}</pubDate>"
            "</item>"
        )
    return "<?xml version='1.0'?><rss version='2.0'><channel>" + "".join(contents) + "</channel></rss>"


def _request(source: NewsSearchSource, *, cursor: str | None = None, max_items: int = 100, deadline=None):
    if deadline is None:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    return SourceRequest(
        source_id=source.source_id,
        account_ref=source.account_ref,
        deadline=deadline.isoformat().replace("+00:00", "Z"),
        cursor_ref=cursor,
        max_items=max_items,
    )


def _source(broker: ScriptedBroker, topics=lambda: ["AI", "NASA"], **kwargs) -> NewsSearchSource:
    return NewsSearchSource(topics=topics, broker=broker, **kwargs)


class NewsSearchSourceTests(unittest.TestCase):
    def test_topics_are_quoted_deduplicated_and_request_result_limit_is_honored(self):
        broker = ScriptedBroker(
            _rss(
                {"title": "One", "link": "https://news.example/1"},
                {"title": "Two", "link": "https://news.example/2"},
            )
        )
        source = _source(broker, topics=lambda: [" AI ", "ai", "NASA"])

        batch = asyncio.run(source.fetch_delta(_request(source, max_items=1)))

        self.assertEqual(len(broker.calls), 1)
        query = parse_qs(urlsplit(broker.calls[0]).query)["q"][0]
        self.assertEqual(query, '"AI" OR "NASA"')
        self.assertEqual(len(batch.items), 1)
        content = json.loads(batch.items[0].content)
        self.assertEqual(content["candidate_topics"], ["AI", "NASA"])
        self.assertEqual(batch.items[0].sensitivity, "public")

    def test_empty_topics_preserve_cursor_without_fetch(self):
        broker = ScriptedBroker()
        source = _source(broker, topics=lambda: [])

        batch = asyncio.run(source.fetch_delta(_request(source, cursor="old-cursor")))

        self.assertEqual(broker.calls, [])
        self.assertEqual(batch.items, ())
        self.assertEqual(batch.cursor_ref, "old-cursor")
        self.assertIsNotNone(batch.fresh_until)

    def test_cursor_is_order_independent_and_same_page_is_suppressed(self):
        one = {"title": "One", "link": "https://news.example/1"}
        two = {"title": "Two", "link": "https://news.example/2"}
        broker = ScriptedBroker(_rss(one, two), _rss(two, one))
        source = _source(broker)

        first = asyncio.run(source.fetch_delta(_request(source)))
        second = asyncio.run(source.fetch_delta(_request(source, cursor=first.cursor_ref)))

        self.assertEqual(len(broker.calls), 2)
        self.assertEqual(first.cursor_ref, second.cursor_ref)
        self.assertEqual(second.items, ())
        self.assertEqual([item.fact_id for item in first.items], sorted(item.fact_id for item in first.items))

    def test_fact_id_stays_stable_when_article_content_changes(self):
        broker = ScriptedBroker(
            _rss({"title": "One", "link": "https://news.example/1", "description": "First"}),
            _rss({"title": "One", "link": "https://news.example/1", "description": "Updated"}),
        )
        source = _source(broker)

        first = asyncio.run(source.fetch_delta(_request(source)))
        changed = asyncio.run(source.fetch_delta(_request(source, cursor=first.cursor_ref)))

        self.assertEqual(first.items[0].fact_id, changed.items[0].fact_id)
        self.assertNotEqual(first.items[0].revision, changed.items[0].revision)
        self.assertIn("Updated", changed.items[0].content)

    def test_old_future_invalid_date_and_non_http_links_are_skipped(self):
        broker = ScriptedBroker(
            _rss(
                {"link": "https://news.example/old", "pubDate": _date(timedelta(days=-9))},
                {"link": "https://news.example/future", "pubDate": _date(timedelta(days=1))},
                {"link": "https://news.example/bad-date", "pubDate": "not a date"},
                {"link": "https://user:secret@news.example/credential"},
                {"link": "javascript:alert(1)"},
            )
        )
        source = _source(broker)

        batch = asyncio.run(source.fetch_delta(_request(source, cursor="unchanged")))

        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(batch.items, ())
        self.assertEqual(batch.cursor_ref, "unchanged")

    def test_description_html_is_stripped_and_capped(self):
        broker = ScriptedBroker(
            _rss({"description": "<p>Useful <b>summary</b></p> " + "x" * 1200})
        )
        source = _source(broker)

        batch = asyncio.run(source.fetch_delta(_request(source)))

        content = json.loads(batch.items[0].content)
        self.assertNotIn("<", content["description"])
        self.assertIn("Useful summary", content["description"])
        self.assertLessEqual(len(content["description"]), 1000)

    def test_invalid_xml_and_html_captcha_are_provider_errors(self):
        for body in ("<rss>", "<html><body>captcha</body></html>"):
            broker = ScriptedBroker(body)
            source = _source(broker)
            with self.subTest(body=body), self.assertRaises(PASError) as ctx:
                asyncio.run(source.fetch_delta(_request(source)))
            self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
            self.assertEqual(len(broker.calls), 1)

    def test_expired_deadline_and_topic_budget_fail_before_fetch(self):
        broker = ScriptedBroker()
        source = _source(broker)
        expired = datetime.now(timezone.utc) - timedelta(seconds=1)

        with self.assertRaises(PASError) as ctx:
            asyncio.run(source.fetch_delta(_request(source, deadline=expired)))
        self.assertEqual(ctx.exception.code, ErrorCode.DEADLINE_EXCEEDED)

        too_many = _source(broker, topics=lambda: [str(i) for i in range(9)])
        with self.assertRaises(PASError) as ctx:
            asyncio.run(too_many.fetch_delta(_request(too_many)))
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)
        self.assertEqual(broker.calls, [])


class ExplicitResearchSelectionTests(unittest.TestCase):
    def test_news_is_not_fetched_by_unrelated_jobs_or_manual_wakes(self):
        with tempfile.TemporaryDirectory() as root:
            agent = ProactiveAgent(state_dir=root, model=SimpleNamespace(generate=lambda _: None))
            source = _source(ScriptedBroker())
            agent.registry.register(source_id=source.source_id, account_ref=source.account_ref,
                                    source=source, required_capability=source.required_capability)
            try:
                self.assertEqual(agent.coordinator._targeted_entries(None), ())
                self.assertEqual(agent.coordinator._targeted_entries(SimpleNamespace(task={})), ())
                selected = agent.coordinator._targeted_entries(SimpleNamespace(task={"refresh_source_ids": [source.source_id]}))
                self.assertEqual(selected[0].source, source)
            finally:
                agent.store.close()


if __name__ == "__main__":
    unittest.main()
