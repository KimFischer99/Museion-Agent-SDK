#!/usr/bin/env python3
"""A real, human-readable push receiver (SPEC §22.1 item 8).

This is not a mock: it is a real HTTP server that a real dispatcher POSTs
to over the network, it records what actually arrived to a file a human can
read, and it answers the two questions PAS needs answered:

* ``POST /push`` — accepts a delivery. The ``Idempotency-Key`` header
  identifies the message, so a duplicate delivery is *absorbed*: the same
  ``external_id`` comes back and nothing new is recorded. That is what a
  well-behaved push provider does, and it is what makes "retry the same
  provider key" safe.
* ``GET /status?provider_key=...`` — the authoritative answer used to
  reconcile ``delivery_unknown``. It returns ``{"delivered": true|false}``
  only when this receiver actually knows; otherwise ``{}``, which leaves
  the message unknown rather than guessing.

It can also lose an ACK on purpose: with ``X-Drop-Response: 1`` the request
is accepted and recorded, then the connection is closed without a reply —
the real-world "the provider got it but we never heard back" case.

Usage:
    python3 push_receiver.py --port 8791 --log /tmp/push-received.jsonl

Read what arrived:
    cat /tmp/push-received.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

__all__ = ["PushReceiver", "build_receiver"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class _State:
    """Everything the receiver knows, guarded by one lock."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.lock = threading.Lock()
        self.by_key: dict[str, dict] = {}
        self.dropped_acks = 0

    def absorb(self, provider_key: str, payload: dict, headers: dict) -> tuple[dict, bool]:
        """Record one delivery. Returns (record, is_duplicate)."""
        with self.lock:
            existing = self.by_key.get(provider_key)
            if existing is not None:
                # A duplicate delivery is absorbed, not recorded twice: the
                # provider key is the identity of the message.
                return existing, True
            external_id = "ext-" + hashlib.sha256(provider_key.encode()).hexdigest()[:16]
            record = {
                "external_id": external_id,
                "provider_key": provider_key,
                "received_at": _now(),
                "payload": payload,
                "content_type": headers.get("Content-Type"),
            }
            self.by_key[provider_key] = record
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            return record, False


def build_receiver(log_path: str | Path, *, host: str = "127.0.0.1", port: int = 0):
    state = _State(Path(log_path))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "pas-push-receiver/1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: D102 - quiet
            return

        # -- POST /push -------------------------------------------------- #
        def do_POST(self) -> None:  # noqa: N802 - http.server contract
            if urlparse(self.path).path != "/push":
                self._json(404, {"error": "unknown path"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                self._json(400, {"error": "body is not JSON"})
                return
            provider_key = self.headers.get("Idempotency-Key") or ""
            if not provider_key:
                # A delivery without an identity cannot be deduplicated, so it
                # is refused rather than accepted ambiguously.
                self._json(400, {"error": "Idempotency-Key is required"})
                return
            record, duplicate = state.absorb(provider_key, payload, dict(self.headers))
            if self.headers.get("X-Drop-Response"):
                # Accepted and recorded, but the caller never learns that.
                state.dropped_acks += 1
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                self.close_connection = True
                return
            self._json(201 if not duplicate else 200, {
                "external_id": record["external_id"],
                "duplicate": duplicate,
            })

        # -- GET /status ------------------------------------------------- #
        def do_GET(self) -> None:  # noqa: N802 - http.server contract
            parsed = urlparse(self.path)
            if parsed.path == "/received":
                with state.lock:
                    records = list(state.by_key.values())
                self._json(200, {"count": len(records), "records": records,
                                 "dropped_acks": state.dropped_acks})
                return
            if parsed.path != "/status":
                self._json(404, {"error": "unknown path"})
                return
            key = (parse_qs(parsed.query).get("provider_key") or [""])[0]
            with state.lock:
                record = state.by_key.get(key)
            if record is None:
                # "I have no record of it" is NOT "it was not delivered".
                self._json(200, {})
                return
            self._json(200, {"delivered": True, "external_id": record["external_id"]})

        def _json(self, status: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer((host, port), Handler)
    return server, state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PAS push receiver (validation tool)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--log", default="/tmp/push-received.jsonl")
    args = parser.parse_args(argv)

    server, state = build_receiver(args.log, host=args.host, port=args.port)
    host, port = server.server_address[:2]
    print(json.dumps({"listening": f"http://{host}:{port}", "log": str(state.log_path)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
