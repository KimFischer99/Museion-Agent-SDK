"""W4c: page_hash connector, offline site e2e and transport safety.

Everything here stays offline: scripted bodies for the unit tests and a
pinned loopback TLS server for the end-to-end + security cases.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import PASError  # noqa: E402
from proactive_sdk.contracts import ErrorCode, SourceRequest  # noqa: E402
from proactive_sdk.net import EgressBlocked, EgressBroker  # noqa: E402
from proactive_sdk.sources.page_hash import PageHashSource  # noqa: E402
from proactive_sdk.sources.sitemap_diff import SitemapDiffSource  # noqa: E402

PAGE = """<!doctype html><html><head><title>Institute bulletin</title>
<style>body { color: #333 }</style></head>
<body>
<nav><a href="/">Home</a> <a href="/news">News</a></nav>
<header>Site header with a rotation banner</header>
<main>
  <h1>Excavation season opens</h1>
  <p>The institute has published its annual excavation report for the northern site,
     covering the full season and the conservation work that followed.</p>
  <p>Fieldwork continues next spring with an expanded team.</p>
</main>
<footer>Last updated 2025-10-01. Advertisement slot.</footer>
<script>window.ads = ["buy now"];</script>
</body></html>
"""

PAGE_CHANGED = PAGE.replace(
    "Fieldwork continues next spring with an expanded team.",
    "Fieldwork is paused indefinitely after the funding review, pending a new decision.",
)

PAGE_CHROME_CHANGED = PAGE.replace(
    '<nav><a href="/">Home</a> <a href="/news">News</a></nav>',
    '<nav><a href="/">Home</a> <a href="/archive">Archive</a> <a href="/news">News</a></nav>',
).replace('window.ads = ["buy now"];', 'window.ads = ["buy later", "sponsored"];')

TINY_PAGE = "<html><body><p>{0}</p></body></html>"


def _deadline() -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=60))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _request(*, source_id="page", cursor=None):
    return SourceRequest(
        source_id=source_id,
        account_ref="account:public",
        deadline=_deadline(),
        cursor_ref=cursor,
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
        "url": "https://example.org/page.html",
        "source_id": "page",
        "allowed_hosts": ("example.org",),
        "broker": broker,
    }
    kwargs.update(over)
    return PageHashSource(**kwargs)


class PageHashTests(unittest.TestCase):
    def test_first_fetch_is_a_baseline_and_same_page_is_unchanged(self):
        source = _source(ScriptedBroker(PAGE, PAGE))
        first = asyncio.run(source.fetch_delta(_request()))
        self.assertEqual(first.items, ())
        self.assertLessEqual(len(first.cursor_ref), 256)
        second = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(second.items, ())
        self.assertEqual(second.cursor_ref, first.cursor_ref)

    def test_chrome_change_alone_is_noise_not_a_candidate(self):
        source = _source(ScriptedBroker(PAGE, PAGE_CHROME_CHANGED))
        first = asyncio.run(source.fetch_delta(_request()))
        changed = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(changed.items, ())

    def test_substantive_content_change_is_exactly_one_candidate(self):
        source = _source(ScriptedBroker(PAGE, PAGE_CHANGED))
        first = asyncio.run(source.fetch_delta(_request()))
        changed = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(len(changed.items), 1)
        payload = json.loads(changed.items[0].content)
        self.assertEqual(payload["kind"], "page_hash")
        self.assertEqual(payload["url"], "https://example.org/page.html")
        self.assertIn("funding review", payload["excerpt"])
        self.assertGreater(payload["hamming_distance"], 0)
        self.assertTrue(changed.items[0].fact_id.startswith("page:"))

    def test_threshold_can_suppress_even_a_real_change(self):
        source = _source(ScriptedBroker(PAGE, PAGE_CHANGED), hamming_threshold=64)
        first = asyncio.run(source.fetch_delta(_request()))
        changed = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(changed.items, ())

    def test_tiny_page_falls_back_to_exact_comparison(self):
        source = _source(
            ScriptedBroker(TINY_PAGE.format("first"), TINY_PAGE.format("second"))
        )
        first = asyncio.run(source.fetch_delta(_request()))
        changed = asyncio.run(source.fetch_delta(_request(cursor=first.cursor_ref)))
        self.assertEqual(len(changed.items), 1)

    def test_unreadable_page_is_a_provider_error(self):
        source = _source(ScriptedBroker("<html><script>x</script><style>y</style></html>"))
        with self.assertRaises(PASError) as ctx:
            asyncio.run(source.fetch_delta(_request()))
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_cursor_is_deterministic_for_the_same_page(self):
        first_source = _source(ScriptedBroker(PAGE))
        second_source = _source(ScriptedBroker(PAGE))
        first = asyncio.run(first_source.fetch_delta(_request()))
        second = asyncio.run(second_source.fetch_delta(_request()))
        self.assertEqual(first.cursor_ref, second.cursor_ref)

    def test_constructor_and_describe(self):
        for kwargs in (
            dict(url="http://example.org/page.html", allowed_hosts=("example.org",)),
            dict(url="https://example.org/page.html"),  # allowlist mandatory
            dict(url="https://other.example/page.html", allowed_hosts=("example.org",)),
            dict(url="https://example.org/page.html", allowed_hosts=("example.org",), hamming_threshold=65),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PASError):
                    PageHashSource(source_id="page", **kwargs)
        described = _source(ScriptedBroker(PAGE)).describe()
        self.assertEqual(described["kind"], "page_hash")
        self.assertTrue(described["requires_explicit_selection"])

    def test_registration_captures_describe(self):
        from proactive_sdk import SourceRegistry

        registry = SourceRegistry()
        registry.register(
            source_id="page",
            account_ref="account:public",
            source=_source(ScriptedBroker(PAGE)),
            required_capability="public.read",
        )
        self.assertEqual(registry.entries()[0].description["kind"], "page_hash")


class _MutableSite(http.server.BaseHTTPRequestHandler):
    """Serve mutable fixtures over TLS; also exposes unsafe paths."""

    state: dict = {}
    server_version = "Fixture/1"

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://localhost/elsewhere")
            self.end_headers()
            return
        if path == "/big":
            body = b"x" * (5 * 1024 * 1024 + 10)
        elif path == "/xhtml":
            body = PAGE.replace("<!doctype html>", '<!doctype html><html xmlns="http://www.w3.org/1999/xhtml">').encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/xhtml+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif path == "/binary":
            body = b"\x00\x01\x02"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif path in self.state:
            body = self.state[path].encode("utf-8")
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


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


@unittest.skipUnless(
    subprocess.run(["which", "openssl"], capture_output=True).returncode == 0,
    "openssl not available; loopback TLS tests skipped, not faked",
)
class OfflineSiteEndToEndTests(unittest.TestCase):
    """首轮无变化 → 次轮改一个文件 → 恰好一条候选（page + sitemap）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        cert = _make_cert(self.tmp)
        _MutableSite.state = {
            "/page.html": PAGE,
            "/sitemap.xml": (
                '<?xml version="1.0"?><urlset>'
                "<url><loc>https://localhost/a</loc><lastmod>2025-10-01T00:00:00Z</lastmod></url>"
                "</urlset>"
            ),
        }
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MutableSite)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, self.tmp / "key.pem")
        self.httpd.socket = server_ctx.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_port
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.load_verify_locations(cafile=str(self.tmp / "cert.pem"))
        client_ctx.check_hostname = True
        self.broker = EgressBroker(
            allowed_hosts=("localhost",),
            allowed_ports=(self.port,),
            allow_loopback=True,
            ssl_context=client_ctx,
        )

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _page(self) -> PageHashSource:
        return PageHashSource(
            url=f"https://localhost:{self.port}/page.html",
            source_id="page",
            allowed_hosts=("localhost",),
            broker=self.broker,
        )

    def _sitemap(self) -> SitemapDiffSource:
        return SitemapDiffSource(
            url=f"https://localhost:{self.port}/sitemap.xml",
            source_id="sitemap",
            allowed_hosts=("localhost",),
            broker=self.broker,
        )

    def test_page_first_round_quiet_then_one_candidate(self):
        source = self._page()
        baseline = asyncio.run(source.fetch_delta(_request()))
        self.assertEqual(baseline.items, ())
        unchanged = asyncio.run(source.fetch_delta(_request(cursor=baseline.cursor_ref)))
        self.assertEqual(unchanged.items, ())
        _MutableSite.state["/page.html"] = PAGE_CHANGED
        changed = asyncio.run(source.fetch_delta(_request(cursor=unchanged.cursor_ref)))
        self.assertEqual(len(changed.items), 1)

    def test_sitemap_first_round_quiet_then_one_candidate(self):
        source = self._sitemap()
        baseline = asyncio.run(source.fetch_delta(_request(source_id="sitemap")))
        self.assertEqual(baseline.items, ())
        unchanged = asyncio.run(
            source.fetch_delta(_request(source_id="sitemap", cursor=baseline.cursor_ref))
        )
        self.assertEqual(unchanged.items, ())
        _MutableSite.state["/sitemap.xml"] = _MutableSite.state["/sitemap.xml"].replace(
            "2025-10-01T00:00:00Z", "2025-10-09T00:00:00Z"
        )
        changed = asyncio.run(
            source.fetch_delta(_request(source_id="sitemap", cursor=unchanged.cursor_ref))
        )
        self.assertEqual(len(changed.items), 1)

    def test_transport_refusals_propagate(self):
        # Redirect: the broker refuses to follow it, the source surfaces that.
        redirect_source = PageHashSource(
            url=f"https://localhost:{self.port}/redirect",
            source_id="page",
            allowed_hosts=("localhost",),
            broker=self.broker,
        )
        with self.assertRaises(EgressBlocked):
            asyncio.run(redirect_source.fetch_delta(_request()))
        # Oversized response (> 5 MiB cap): bounded before parsing.
        big_source = PageHashSource(
            url=f"https://localhost:{self.port}/big",
            source_id="page",
            allowed_hosts=("localhost",),
            broker=self.broker,
        )
        with self.assertRaises(EgressBlocked):
            asyncio.run(big_source.fetch_delta(_request()))
        # application/xhtml+xml is an allowed content type (owner decision),
        # while opaque binary types stay refused.
        xhtml_source = PageHashSource(
            url=f"https://localhost:{self.port}/xhtml",
            source_id="page",
            allowed_hosts=("localhost",),
            broker=self.broker,
        )
        batch = asyncio.run(xhtml_source.fetch_delta(_request()))
        self.assertEqual(batch.items, ())  # baseline
        binary_source = PageHashSource(
            url=f"https://localhost:{self.port}/binary",
            source_id="page",
            allowed_hosts=("localhost",),
            broker=self.broker,
        )
        with self.assertRaises(EgressBlocked):
            asyncio.run(binary_source.fetch_delta(_request()))


if __name__ == "__main__":
    unittest.main()
