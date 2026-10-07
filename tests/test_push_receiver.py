"""SPEC §22.1 item 8: the push *receiver* we ship for real-network validation.

The SDK side of push delivery is already covered against a real HTTP server
(`ScriptedWebhookServer` in `test_delivery.py`): status mapping, the lost
ACK, `delivery_unknown`, no blind re-send, the provider's authoritative
answer, and lock-screen minimisation. Re-testing that here would be
duplication.

What is *not* covered is the piece this step adds: `tools/push_receiver.py`,
the endpoint a human reads on the experimental host. It is now part of how
the system is validated, so its contract has to hold — it must absorb
duplicate deliveries instead of double-recording them, it must not pretend
to know about a message it never saw, and it must refuse a delivery it
cannot identify.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from push_receiver import build_receiver  # noqa: E402


class PushReceiverContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "received.jsonl"
        self.server, self.state = build_receiver(self.log, port=0)
        host, port = self.server.server_address[:2]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, key: str | None, payload: dict, *, drop: bool = False):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        if drop:
            headers["X-Drop-Response"] = "1"
        request = urllib.request.Request(
            f"{self.base}/push",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def _received(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/received", timeout=5) as response:
            return json.loads(response.read())

    def _status(self, key: str) -> dict:
        with urllib.request.urlopen(f"{self.base}/status?provider_key={key}", timeout=5) as r:
            return json.loads(r.read())

    def test_a_repeat_delivery_is_absorbed_not_recorded_twice(self):
        status, first = self._post("k1", {"title": "hello"})
        self.assertEqual(status, 201)
        self.assertFalse(first["duplicate"])
        _status_code, again = self._post("k1", {"title": "hello"})
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["external_id"], first["external_id"])
        self.assertEqual(self._received()["count"], 1)

    def test_it_does_not_claim_to_know_a_message_it_never_saw(self):
        # "I have no record of it" must not read as "it was not delivered".
        self.assertEqual(self._status("never-seen"), {})

    def test_a_delivery_without_an_identity_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(None, {"title": "anonymous"})
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(self._received()["count"], 0)

    def test_a_non_json_body_is_refused(self):
        request = urllib.request.Request(
            f"{self.base}/push", data=b"not json", method="POST",
            headers={"Content-Type": "application/json", "Idempotency-Key": "k9"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_a_dropped_ack_records_the_message_and_breaks_the_connection(self):
        # The exception type depends on the Python version (`urllib` wraps
        # some connection errors as URLError and lets others through as raw
        # OSError subclasses — `RemoteDisconnected` is one). This is exactly
        # why `WebhookNotificationSink` catches the broad tuple instead of
        # URLError alone: narrowing it would turn a lost ACK into a crash.
        with self.assertRaises((urllib.error.URLError, OSError)):
            self._post("k2", {"title": "lost ack"}, drop=True)
        # Recorded anyway — that is exactly why the caller cannot assume.
        self.assertEqual(self._received()["count"], 1)
        self.assertEqual(self._status("k2")["delivered"], True)
        self.assertEqual(self._received()["dropped_acks"], 1)

    def test_the_log_file_is_the_human_readable_artefact(self):
        self._post("k3", {"title": "读我", "semantic": "notify_self"})
        lines = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["payload"]["title"], "读我")
        self.assertEqual(lines[0]["provider_key"], "k3")


if __name__ == "__main__":
    unittest.main()
