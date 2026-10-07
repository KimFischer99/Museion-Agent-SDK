"""P6 egress broker tests: allowlist, SSRF guard, IP pinning, no
redirects, bounded size/types. The happy path runs against a real
loopback TLS server with a generated self-signed certificate loaded as
its own CA (verification stays ON); refusals need no network at all."""

from __future__ import annotations

import http.server
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.net import EgressBlocked, EgressBroker


class RefusalTests(unittest.TestCase):
    def test_allowlist_is_mandatory(self):
        with self.assertRaises(Exception):
            EgressBroker(allowed_hosts=[])

    def test_plain_http_refused(self):
        broker = EgressBroker(allowed_hosts=("example.com",))
        with self.assertRaises(EgressBlocked):
            broker.fetch_text("http://example.com/feed")

    def test_non_allowlisted_host_refused_before_dns(self):
        broker = EgressBroker(allowed_hosts=("example.com",))
        with self.assertRaises(EgressBlocked):
            broker.fetch_text("https://attacker.example/feed")

    def test_policy_allows_subdomains_but_transport_still_gates(self):
        broker = EgressBroker(allowed_hosts=("example.com",))
        # Policy accepts the subdomain; the reserved .invalid TLD is
        # guaranteed NXDOMAIN, so the fetch blocks at transport — the
        # policy pass never bypasses transport gates.
        with self.assertRaises(EgressBlocked):
            broker.fetch_text("https://feed.example.invalid/x")

    def test_loopback_refused_without_explicit_flag(self):
        broker = EgressBroker(allowed_hosts=("localhost",))
        with self.assertRaises(EgressBlocked):
            broker.fetch_text("https://localhost/x")

    def test_non_allowed_port_refused(self):
        broker = EgressBroker(allowed_hosts=("example.com",), allowed_ports=(443,))
        with self.assertRaises(EgressBlocked):
            broker.fetch_text("https://example.com:8443/x")

    def test_url_credentials_refused(self):
        broker = EgressBroker(allowed_hosts=("example.com",))
        with self.assertRaises(EgressBlocked):
            broker.fetch_text("https://user:pass@example.com/x")


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


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Fixture/1"

    def do_GET(self):  # noqa: N802
        mode = self.path.strip("/")
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://localhost/elsewhere")
            self.end_headers()
        elif mode == "binary":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"\x00\x01")
        elif mode == "big":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"x" * 64)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("hello material".encode("utf-8"))

    def log_message(self, *args):
        pass


@unittest.skipUnless(
    subprocess.run(["which", "openssl"], capture_output=True).returncode == 0,
    "openssl not available; loopback TLS tests skipped, not faked",
)
class LoopbackTlsTests(unittest.TestCase):
    """Real TLS over the pinned loopback address (allow_loopback +
    explicit port are the documented test-only knobs)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        cert = _make_cert(self.tmp)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, self.tmp / "key.pem")
        self.httpd.socket = server_ctx.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_port
        client_ctx = ssl.create_default_context(cafile=cert)  # verification ON
        self.broker = EgressBroker(
            allowed_hosts=("localhost",), allowed_ports=(self.port,),
            max_bytes=32, allow_loopback=True, ssl_context=client_ctx,
        )

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _url(self, path: str) -> str:
        return f"https://localhost:{self.port}/{path}"

    def test_happy_path_fetches_through_pinned_tls(self):
        self.assertEqual(self.broker.fetch_text(self._url("hello")), "hello material")

    def test_redirect_refused(self):
        with self.assertRaises(EgressBlocked):
            self.broker.fetch_text(self._url("redirect"))

    def test_disallowed_content_type_refused(self):
        with self.assertRaises(EgressBlocked):
            self.broker.fetch_text(self._url("binary"))

    def test_oversized_body_refused(self):
        with self.assertRaises(EgressBlocked):
            self.broker.fetch_text(self._url("big"))


if __name__ == "__main__":
    unittest.main()
