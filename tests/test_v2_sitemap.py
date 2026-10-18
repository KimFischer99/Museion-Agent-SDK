"""W4b: sitemap_diff connector.

No network in these tests: a scripted broker replays fixture bodies. The
connector is stateless — the diff state rides in the bounded cursor — so the
tests drive it exactly the way the coordinator does (feed the previous
cursor back in).
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import PASError  # noqa: E402
from proactive_sdk.contracts import ErrorCode, SourceRequest  # noqa: E402
from proactive_sdk.net import EgressBroker  # noqa: E402
from proactive_sdk.sources.sitemap_diff import SitemapDiffSource  # noqa: E402

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.org/a</loc><lastmod>2025-10-01T00:00:00Z</lastmod></url>
  <url><loc>https://example.org/b</loc><lastmod>2025-10-02T00:00:00Z</lastmod></url>
</urlset>
"""

SITEMAP_CHANGED = SITEMAP.replace(
    "<lastmod>2025-10-01T00:00:00Z</lastmod>",
    "<lastmod>2025-10-05T00:00:00Z</lastmod>",
)

SITEMAP_ADDED = SITEMAP.replace(
    "</urlset>",
    "  <url><loc>https://example.org/c</loc><lastmod>2025-10-06T00:00:00Z</lastmod></url>\n</urlset>",
)

SITEMAP_NO_LASTMOD = """<?xml version="1.0" encoding="UTF-8"?>
<urlset><url><loc>https://example.org/a</loc></url><url><loc>https://example.org/b</loc></url></urlset>
"""

SITEMAP_NO_LASTMOD_CHANGED = """<?xml version="1.0" encoding="UTF-8"?>
<urlset><url><loc>https://example.org/a</loc></url><url><loc>https://example.org/z</loc></url></urlset>
"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.org/child-1.xml</loc></sitemap>
  <sitemap><loc>https://example.org/child-2.xml</loc></sitemap>
</sitemapindex>
"""

CHILD_1 = """<?xml version="1.0" encoding="UTF-8"?>
<urlset><url><loc>https://example.org/one</loc><lastmod>2025-10-01T00:00:00Z</lastmod></url></urlset>
"""

CHILD_2 = """<?xml version="1.0" encoding="UTF-8"?>
<urlset><url><loc>https://example.org/two</loc><lastmod>2025-10-02T00:00:00Z</lastmod></url></urlset>
"""

CHILD_2_CHANGED = CHILD_2.replace("2025-10-02T00:00:00Z", "2025-10-09T00:00:00Z")


def _deadline() -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=60))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _request(*, source_id="sitemap", cursor=None, max_items=50):
    return SourceRequest(
        source_id=source_id,
        account_ref="account:public",
        deadline=_deadline(),
        cursor_ref=cursor,
        max_items=max_items,
    )


class ScriptedBroker(EgressBroker):
    def __init__(self, *bodies: str) -> None:
        super().__init__(allowed_hosts=("example.org",))
        self.bodies = list(bodies)
        self.calls: list[str] = []

    def fetch_text(self, url: str) -> str:
        self.calls.append(url)
        return self.bodies.pop(0)


def _source(broker, **over):
    kwargs = {
        "url": "https://example.org/sitemap.xml",
        "source_id": "sitemap",
        "allowed_hosts": ("example.org",),
        "broker": broker,
    }
    kwargs.update(over)
    return SitemapDiffSource(**kwargs)


class SitemapBaselineTests(unittest.TestCase):
    def test_first_fetch_is_a_baseline_with_zero_items(self):
        source = _source(ScriptedBroker(SITEMAP))
        batch = asyncio.run(source.fetch_delta(_request()))
        self.assertEqual(batch.items, ())
        self.assertIsNotNone(batch.cursor_ref)
        self.assertLessEqual(len(batch.cursor_ref), 256)

    def test_unchanged_sitemap_reports_nothing(self):
        source = _source(ScriptedBroker(SITEMAP, SITEMAP))
        first = asyncio.run(source.fetch_delta(_request()))
        second = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(second.items, ())
        self.assertEqual(second.cursor_ref, first.cursor_ref)

    def test_changed_lastmod_rechecks_current_entries(self):
        source = _source(ScriptedBroker(SITEMAP, SITEMAP_CHANGED))
        first = asyncio.run(source.fetch_delta(_request()))
        changed = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(len(changed.items), 2)
        payload = json.loads(changed.items[0].content)
        self.assertEqual(payload["loc"], "https://example.org/a")
        self.assertEqual(payload["kind"], "sitemap_diff")

    def test_added_url_rechecks_current_entries(self):
        source = _source(ScriptedBroker(SITEMAP, SITEMAP_ADDED))
        first = asyncio.run(source.fetch_delta(_request()))
        changed = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(
            [json.loads(item.content)["loc"] for item in changed.items],
            ["https://example.org/a", "https://example.org/b", "https://example.org/c"],
        )

    def test_added_url_without_lastmod_is_rechecked(self):
        added = SITEMAP.replace(
            "</urlset>", "<url><loc>https://example.org/new</loc></url></urlset>",
        )
        source = _source(ScriptedBroker(SITEMAP, added, added))
        first = asyncio.run(source.fetch_delta(_request()))
        changed = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(
            [json.loads(item.content)["loc"] for item in changed.items],
            ["https://example.org/a", "https://example.org/b", "https://example.org/new"],
        )
        unchanged = asyncio.run(source.fetch_delta(_request(cursor=changed.cursor_ref)))
        self.assertEqual(unchanged.items, ())

    def test_lastmod_less_sitemap_falls_back_to_set_hash(self):
        source = _source(ScriptedBroker(SITEMAP_NO_LASTMOD, SITEMAP_NO_LASTMOD_CHANGED))
        first = asyncio.run(source.fetch_delta(_request()))
        self.assertEqual(first.items, ())
        changed = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(len(changed.items), 2)  # bounded current set
        self.assertEqual(changed.items[0].sensitivity, "public")

    def test_max_items_bounds_the_fallback(self):
        source = _source(
            ScriptedBroker(SITEMAP_NO_LASTMOD, SITEMAP_NO_LASTMOD_CHANGED), max_items=1
        )
        first = asyncio.run(source.fetch_delta(_request()))
        changed = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref, max_items=1)))
        self.assertEqual(len(changed.items), 1)


class SitemapIndexTests(unittest.TestCase):
    def test_index_is_expanded_and_diffed(self):
        broker = ScriptedBroker(INDEX, CHILD_1, CHILD_2, INDEX, CHILD_1, CHILD_2_CHANGED)
        source = _source(broker, url="https://example.org/index.xml")
        first = asyncio.run(source.fetch_delta(_request()))
        self.assertEqual(first.items, ())
        self.assertEqual(len(broker.calls), 3)  # index + two children
        changed = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(
            [json.loads(item.content)["loc"] for item in changed.items],
            ["https://example.org/one", "https://example.org/two"],
        )

    def test_index_shares_one_deadline(self):
        now = datetime.now(timezone.utc)

        class AdvancingBroker(ScriptedBroker):
            def fetch_text(self, url):
                nonlocal now
                now += timedelta(seconds=0.6)
                return super().fetch_text(url)

        broker = AdvancingBroker(INDEX, CHILD_1, CHILD_2)
        request = SourceRequest(
            source_id="sitemap", account_ref="account:public",
            deadline=(now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        )
        with patch("proactive_sdk.sources.sitemap_diff.datetime") as clock:
            clock.now.side_effect = lambda tz: now
            clock.fromisoformat.side_effect = datetime.fromisoformat
            with self.assertRaises(PASError) as caught:
                asyncio.run(_source(broker).fetch_delta(request))
        self.assertEqual(caught.exception.code, ErrorCode.DEADLINE_EXCEEDED)
        self.assertEqual(broker.calls, ["https://example.org/sitemap.xml", "https://example.org/child-1.xml"])

    def test_children_are_allowlist_checked(self):
        index = INDEX.replace("https://example.org/child-2.xml", "https://evil.example/x.xml")
        source = _source(ScriptedBroker(index, CHILD_1), url="https://example.org/index.xml")
        with self.assertRaises(PASError) as ctx:
            asyncio.run(source.fetch_delta(_request()))
        self.assertEqual(ctx.exception.code, ErrorCode.PERMISSION_DENIED)

    def test_children_are_capped(self):
        children = "".join(
            f"<sitemap><loc>https://example.org/child-{i}.xml</loc></sitemap>" for i in range(10)
        )
        index = f'<sitemapindex>{children}</sitemapindex>'
        broker = ScriptedBroker(index, *([CHILD_1] * 10))
        source = _source(broker, url="https://example.org/index.xml", max_children=3)
        asyncio.run(source.fetch_delta(_request()))
        self.assertEqual(len(broker.calls), 4)  # index + at most 3 children


class SitemapValidationTests(unittest.TestCase):
    def test_invalid_xml_and_unknown_roots_are_provider_errors(self):
        for body in ("not xml", "<html/>", "<feed/>"):
            with self.subTest(body=body):
                source = _source(ScriptedBroker(body))
                try:
                    asyncio.run(source.fetch_delta(_request()))
                except PASError as exc:
                    self.assertEqual(exc.code, ErrorCode.PROVIDER_UNAVAILABLE)
                else:
                    self.fail("expected a provider error")

    def test_non_https_locations_are_skipped_not_fatal(self):
        body = (
            '<?xml version="1.0"?><urlset><url><loc>http://example.org/a</loc></url>'
            "<url><loc>https://example.org/b</loc><lastmod>2025-10-01T00:00:00Z</lastmod></url>"
            "</urlset>"
        )
        source = _source(ScriptedBroker(body))
        batch = asyncio.run(source.fetch_delta(_request()))
        self.assertEqual(batch.items, ())  # baseline; only the https url counts

    def test_constructor_validates_inputs(self):
        bad = [
            dict(url="http://example.org/sitemap.xml", allowed_hosts=("example.org",)),
            dict(url="https://example.org/sitemap.xml"),  # allowlist mandatory
            dict(url="https://other.example/sitemap.xml", allowed_hosts=("example.org",)),
            dict(url="", allowed_hosts=("example.org",)),
            dict(url="https://example.org/sitemap.xml", allowed_hosts=("example.org",), max_children=0),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PASError):
                    SitemapDiffSource(source_id="sitemap", **kwargs)

    def test_describe_and_registration(self):
        from proactive_sdk import SourceRegistry

        source = _source(ScriptedBroker(SITEMAP))
        described = source.describe()
        self.assertEqual(described["kind"], "sitemap_diff")
        self.assertTrue(described["requires_explicit_selection"])
        registry = SourceRegistry()
        registry.register(
            source_id="sitemap",
            account_ref="account:public",
            source=source,
            required_capability="public.read",
        )
        self.assertEqual(registry.entries()[0].description["kind"], "sitemap_diff")


if __name__ == "__main__":
    unittest.main()
