"""W4a: generic RSS source, programmatic registration and shared feed parsing.

No third-party dependency, no real network in unit tests: feeds are inline
fixtures, and the one transport test uses a pinned loopback TLS server (the
documented test-only broker knobs), mirroring ``test_net.py``.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # p3_fixtures
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from p3_fixtures import FakeClock, T0  # noqa: E402

from proactive_sdk import (  # noqa: E402
    PASError,
    RssSource,
    SourceConnector,
    SourceRegistry,
)
from proactive_sdk.contracts import ErrorCode, SourceRequest  # noqa: E402
from proactive_sdk.net import EgressBroker  # noqa: E402
from proactive_sdk.sources.feed import cursor_for, parse_feed_items  # noqa: E402

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Museum news</title>
    <item>
      <title>New exhibition opens</title>
      <link>https://example.org/news/new-exhibition</link>
      <guid isPermaLink="false">news-1</guid>
      <description>&lt;p&gt;A &lt;b&gt;new&lt;/b&gt; exhibition.&lt;/p&gt;</description>
      <pubDate>Thu, 09 Oct 2025 08:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Journal issue published</title>
      <link>https://example.org/journal/42</link>
      <guid isPermaLink="false">journal-42</guid>
      <description>Issue 42 is out.</description>
      <pubDate>Wed, 08 Oct 2025 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Institute updates</title>
  <entry>
    <title>Excavation report</title>
    <id>tag:example.org,2025:report-7</id>
    <link rel="alternate" href="https://example.org/reports/7"/>
    <updated>2025-10-09T09:30:00Z</updated>
    <summary>Season summary published.</summary>
  </entry>
</feed>
"""

CHANGED_RSS_FEED = RSS_FEED.replace(
    "</channel>",
    """  <item>
      <title>Third item</title>
      <link>https://example.org/news/third</link>
      <guid isPermaLink="false">news-3</guid>
      <description>Third.</description>
      <pubDate>Fri, 10 Oct 2025 08:00:00 GMT</pubDate>
    </item>
  </channel>""",
)


def _deadline(seconds: int = 60) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _request(*, source_id="rss", account_ref="account:public", cursor=None, max_items=50):
    return SourceRequest(
        source_id=source_id,
        account_ref=account_ref,
        deadline=_deadline(),
        cursor_ref=cursor,
        max_items=max_items,
    )


class ScriptedBroker(EgressBroker):
    """EgressBroker double: real policy gates, scripted bodies."""

    def __init__(self, *bodies: str) -> None:
        super().__init__(allowed_hosts=("example.org",))
        self.bodies = list(bodies)
        self.calls: list[str] = []

    def fetch_text(self, url: str) -> str:
        self.calls.append(url)
        return self.bodies.pop(0)


class FeedParsingTests(unittest.TestCase):
    def test_rss_items_are_stable_and_deterministic(self):
        items = parse_feed_items(
            RSS_FEED,
            source_id="rss",
            account_ref="account:public",
            observed_at="2025-10-09T10:00:00Z",
            max_items=10,
        )
        self.assertEqual(len(items), 2)
        # Newest first.
        self.assertIn("New exhibition opens", items[0].content)
        self.assertTrue(items[0].fact_id.startswith("feed:"))
        self.assertTrue(items[0].revision.startswith("sha256:"))
        again = parse_feed_items(
            RSS_FEED,
            source_id="rss",
            account_ref="account:public",
            observed_at="2025-10-09T10:00:00Z",
            max_items=10,
        )
        self.assertEqual([i.fact_id for i in items], [i.fact_id for i in again])
        self.assertEqual([i.revision for i in items], [i.revision for i in again])
        self.assertEqual(items[0].sensitivity, "public")

    def test_atom_entries_parse(self):
        items = parse_feed_items(
            ATOM_FEED,
            source_id="atom",
            account_ref="account:public",
            observed_at="2025-10-09T10:00:00Z",
            max_items=10,
        )
        self.assertEqual(len(items), 1)
        payload = json.loads(items[0].content)
        self.assertEqual(payload["title"], "Excavation report")
        self.assertEqual(payload["url"], "https://example.org/reports/7")
        self.assertEqual(payload["published_at"], "2025-10-09T09:30:00Z")

    def test_malformed_feed_is_a_provider_error(self):
        for bad in ("not xml at all", "<html><body>x</body></html>"):
            with self.subTest(bad=bad):
                with self.assertRaises(PASError) as ctx:
                    parse_feed_items(
                        bad,
                        source_id="rss",
                        account_ref="account:public",
                        observed_at="2025-10-09T10:00:00Z",
                        max_items=10,
                    )
                self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_entries_without_title_or_link_are_skipped(self):
        feed = RSS_FEED.replace("<title>New exhibition opens</title>", "<title></title>")
        items = parse_feed_items(
            feed,
            source_id="rss",
            account_ref="account:public",
            observed_at="2025-10-09T10:00:00Z",
            max_items=10,
        )
        self.assertEqual(len(items), 1)


class RssSourceTests(unittest.TestCase):
    def test_first_fetch_returns_items_and_second_reports_unchanged(self):
        broker = ScriptedBroker(RSS_FEED, RSS_FEED)
        source = RssSource(
            url="https://example.org/feed.xml",
            source_id="museum",
            allowed_hosts=("example.org",),
            broker=broker,
        )
        first = asyncio.run(source.fetch_delta(_request(source_id="museum")))
        self.assertEqual(len(first.items), 2)
        self.assertIsNotNone(first.cursor_ref)
        second = asyncio.run(
            source.fetch_delta(_request(source_id="museum", cursor=first.cursor_ref))
        )
        self.assertEqual(second.items, ())
        self.assertEqual(second.cursor_ref, first.cursor_ref)

    def test_changed_feed_produces_a_new_cursor_and_items(self):
        broker = ScriptedBroker(RSS_FEED, CHANGED_RSS_FEED)
        source = RssSource(
            url="https://example.org/feed.xml",
            source_id="museum",
            allowed_hosts=("example.org",),
            broker=broker,
        )
        first = asyncio.run(source.fetch_delta(_request(source_id="museum")))
        changed = asyncio.run(
            source.fetch_delta(_request(source_id="museum", cursor=first.cursor_ref))
        )
        self.assertNotEqual(changed.cursor_ref, first.cursor_ref)
        self.assertEqual(len(changed.items), 3)

    def test_binding_mismatch_is_rejected(self):
        source = RssSource(
            url="https://example.org/feed.xml",
            source_id="museum",
            allowed_hosts=("example.org",),
            broker=ScriptedBroker(RSS_FEED),
        )
        with self.assertRaises(PASError):
            asyncio.run(source.fetch_delta(_request(source_id="other")))

    def test_deadline_is_enforced(self):
        source = RssSource(
            url="https://example.org/feed.xml",
            source_id="museum",
            allowed_hosts=("example.org",),
            broker=ScriptedBroker(RSS_FEED),
        )
        with self.assertRaises(PASError) as ctx:
            asyncio.run(
                source.fetch_delta(
                    SourceRequest(
                        source_id="museum",
                        account_ref="account:public",
                        deadline="2020-01-01T00:00:00Z",
                    )
                )
            )
        self.assertEqual(ctx.exception.code, ErrorCode.DEADLINE_EXCEEDED)

    def test_describe_is_machine_readable(self):
        broker = ScriptedBroker(RSS_FEED)
        source = RssSource(
            url="https://example.org/feed.xml",
            source_id="museum",
            allowed_hosts=("example.org",),
            broker=broker,
        )
        described = source.describe()
        self.assertEqual(described["kind"], "rss")
        self.assertEqual(described["source_id"], "museum")
        self.assertEqual(described["allowed_hosts"], ["example.org"])
        self.assertTrue(described["requires_explicit_selection"])
        self.assertIsInstance(source, SourceConnector)

    def test_construction_validates_security_inputs(self):
        bad_cases = [
            {"url": "http://example.org/feed.xml", "allowed_hosts": ("example.org",)},
            {"url": "https://user:pass@example.org/feed.xml", "allowed_hosts": ("example.org",)},
            {"url": "https://example.org/feed.xml"},  # allowlist mandatory
            {"url": "https://other.example/feed.xml", "allowed_hosts": ("example.org",)},
            {"url": "https://example.org/feed.xml", "allowed_hosts": ()},
            {
                "url": "https://example.org/feed.xml",
                "allowed_hosts": ("example.org",),
                "source_id": "NotAValidId",
            },
        ]
        for kwargs in bad_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PASError):
                    RssSource(**{"source_id": "museum", **kwargs})

    def test_allowlist_must_cover_the_feed_host(self):
        with self.assertRaises(PASError):
            RssSource(
                url="https://elsewhere.example/rss",
                source_id="museum",
                allowed_hosts=("example.org",),
                broker=ScriptedBroker(RSS_FEED),
            )
        # A subdomain is covered by its parent allowlist entry.
        RssSource(
            url="https://feeds.example.org/rss",
            source_id="museum",
            allowed_hosts=("example.org",),
        )


class RegistrationTests(unittest.TestCase):
    def _source(self):
        return RssSource(
            url="https://example.org/feed.xml",
            source_id="museum",
            allowed_hosts=("example.org",),
            broker=ScriptedBroker(RSS_FEED),
        )

    def test_registry_captures_describe(self):
        registry = SourceRegistry()
        registry.register(
            source_id="museum",
            account_ref="account:public",
            source=self._source(),
            required_capability="public.read",
        )
        entry = registry.entries()[0]
        self.assertIsNotNone(entry.description)
        self.assertEqual(entry.description["kind"], "rss")
        self.assertTrue(entry.source.requires_explicit_selection)

    def test_untargeted_heartbeat_does_not_scan_explicit_sources(self):
        from test_coordinator import make_store
        from proactive_sdk import CoordinatorConfig, ProactiveCoordinator
        from p3_fixtures import ScriptedModel, make_broker, make_executor, make_pack_builder

        store, _ = make_store()
        registry = SourceRegistry()
        registry.register(
            source_id="museum",
            account_ref="account:public",
            source=self._source(),
            required_capability="public.read",
        )
        coordinator = ProactiveCoordinator(
            store,
            registry=registry,
            pack_builder=make_pack_builder(),
            executor=make_executor(ScriptedModel([]), broker=make_broker()),
            config=CoordinatorConfig(),
        )
        self.assertEqual(coordinator._targeted_entries(None), ())

        class Job:
            task = {"refresh_source_ids": ["museum"]}

        targeted = coordinator._targeted_entries(Job())
        self.assertEqual([entry.source_id for entry in targeted], ["museum"])


def _make_cert(directory: Path) -> Path:
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
            "-out", str(cert), "-days", "1", "-nodes", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True, capture_output=True,
    )
    return cert


class _FeedHandler(http.server.BaseHTTPRequestHandler):
    server_version = "Fixture/1"

    def do_GET(self):  # noqa: N802
        body = RSS_FEED.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(
    subprocess.run(["which", "openssl"], capture_output=True).returncode == 0,
    "openssl not available; loopback TLS test skipped, not faked",
)
class LoopbackTlsFetchTests(unittest.TestCase):
    """One real HTTPS fetch through the production broker path."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        cert = _make_cert(self.tmp)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, self.tmp / "key.pem")
        self.httpd.socket = server_ctx.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_port

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_fetch_over_pinned_loopback_tls(self) -> None:
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.load_verify_locations(cafile=str(self.tmp / "cert.pem"))
        client_ctx.check_hostname = True
        broker = EgressBroker(
            allowed_hosts=("localhost",),
            allowed_ports=(self.port,),
            allow_loopback=True,
            ssl_context=client_ctx,
        )
        source = RssSource(
            url=f"https://localhost:{self.port}/feed.xml",
            source_id="museum",
            allowed_hosts=("localhost",),
            broker=broker,
        )
        batch = asyncio.run(source.fetch_delta(_request(source_id="museum")))
        self.assertEqual(len(batch.items), 2)
        self.assertIn("New exhibition opens", batch.items[0].content)


if __name__ == "__main__":
    unittest.main()
