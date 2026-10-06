"""Shared P4 test fixtures.

Fixtures only: the scripted webhook server is a local loopback HTTP
double (like the P3 model transport tests), NOT a real notification
provider, and nothing here proves compatibility with any deployed
push/mail service.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import (
    FakeClock,
    NOTIFY_SELF_CAPABILITY,
    JobSpec,
    OwnerChannelRegistry,
    PolicyConfig,
    PolicyEngine,
    Store,
)
from p3_fixtures import T0, make_broker, make_executor  # noqa: F401  (re-exported)

OWNER_CHANNEL = "local-inbox:demo"


def berlin_ms(day: str, hour: int, minute: int = 0) -> int:
    """Epoch ms for a wall-clock time in Europe/Berlin (tests reason in
    local time because quiet hours are a local-time policy)."""
    year, month, day_num = (int(part) for part in day.split("-"))
    dt = datetime(year, month, day_num, hour, minute, tzinfo=ZoneInfo("Europe/Berlin"))
    return int(dt.timestamp() * 1000)


def rfc3339(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("UTC"))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class P4TestCase(unittest.TestCase):
    def is_sqlite_memory(self):  # pragma: no cover - helper for future use
        return True


def make_store(clock: FakeClock | None = None, *, profile: str = "demo"):
    clock = clock or FakeClock(wall_ms=T0)
    store = Store(":memory:", profile=profile, owner_destination=OWNER_CHANNEL, clock=clock)
    return store, clock


def make_stack(
    *,
    delivery_policy: dict | None = None,
    grant_capability: str = NOTIFY_SELF_CAPABILITY,
    grant_capabilities: list[str] | None = None,
    with_grant: bool = True,
    channels: list[dict] | None = None,
    clock: FakeClock | None = None,
):
    """Store + owner channels + one owner-notification grant + policy
    engine + a job referencing the grant. The default delivery policy has
    no quiet hours and no quota."""
    store, clock = make_store(clock)
    now = clock.wall_now_ms()
    registry = OwnerChannelRegistry(store)
    registry.register(channel_ref=OWNER_CHANNEL, kind="local_inbox", now_ms=now)
    for channel in channels or []:
        registry.register(now_ms=now, **channel)

    grant_ids: list[str] = []
    if with_grant:
        from proactive_sdk import GrantManager

        grants = GrantManager(store)
        for capability in grant_capabilities or [grant_capability]:
            grant = grants.create(
                capability=capability,
                account_ref="account:primary",
                scope={"resource_ids": ["cal-a"]},
                consent_evidence_ref="consent:fixture-v1",
                now_ms=now,
            )
            grant_ids.append(grant.grant_id)

    policy = PolicyEngine(store, channels=registry, config=PolicyConfig())
    job = JobSpec(
        job_id="hb-1",
        mode="heartbeat",
        schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
        task={"instruction": "检查来源变化，只通知本人。"},
        grant_refs=tuple(grant_ids),
        delivery_policy=delivery_policy or {},
    )
    store.upsert_job(job, idempotency_key="fixture-job-v1")
    return store, clock, registry, policy, job


_RUN_SEQ = {"n": 0}


def make_proposed_run(
    store,
    clock,
    job,
    *,
    proposals: list[dict],
    summary: str = "有新的安排",
    instruction: str = "检查来源变化，只通知本人。",
) -> str:
    """Drive one run to the §4.3 ``proposed`` state by committing a
    decision directly (store-level; the L1 loop itself is P3's tests)."""
    now = clock.wall_now_ms()
    _RUN_SEQ["n"] += 1
    store.admit_event(
        f"job:{job.job_id}:{job.revision}:{now}:{_RUN_SEQ['n']}",
        origin="scheduler",
        payload={"job_id": job.job_id, "mode": job.mode},
        observed_at_ms=now,
        expires_at_ms=now + 7 * 24 * 3600 * 1000,
        job_id=job.job_id,
        job_revision=job.revision,
    )
    lease = store.claim_run(now_ms=now, ttl_ms=60_000)
    assert lease is not None, "fixture run must be claimable"
    pack = {
        "schema_version": "1.0",
        "task": {"goal_id": job.job_id, "scope": f"job:{job.job_id}"},
        "locale": "zh-CN",
        "timezone": "Europe/Berlin",
        "preferences_ref": "prefs",
        "sources": [],
        "pending_refs": [],
        "sent_fact_refs": [],
        "memory_refs": [],
        "untrusted_content_policy": "data_only",
    }
    decision = {
        "protocol_version": "1.0",
        "decision": "propose" if proposals else "silent",
        "summary": summary,
        "proposals": [],
    }
    store.record_run_decision(
        lease,
        context_pack=pack,
        decision=decision,
        proposals=proposals,
        usage=None,
        now_ms=now,
    )
    return lease.run_id


def notify_proposal(
    fact_id: str,
    *,
    revision: str = "1",
    body: str = "明早与后端团队的评审提前到 10:00。",
    arguments: dict | None = None,
    expires_at: str | None = None,
    evidence_refs: list[str] | None = None,
) -> dict:
    proposal = {
        "kind": "notify_self",
        "fact_id": fact_id,
        "revision": revision,
        "body": body,
        "arguments": arguments or {},
        "evidence_refs": evidence_refs or ["snapshot:snap-fixture"],
    }
    if expires_at:
        proposal["expires_at"] = expires_at
    return proposal


class _Handler(BaseHTTPRequestHandler):
    server_version = "ScriptedP4/1"

    def _record(self, body: bytes):
        self.server.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(body) if body else None,
            }
        )

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        self._record(raw)
        script = self.server.script_post
        if isinstance(script, dict) and script.get("drop"):
            # ACK loss: the request is read and processed, then the
            # connection dies without any response bytes.
            self.close_connection = True
            return
        script = script or {"status": 200, "body": {"external_id": "ext-1"}}
        payload = json.dumps(script["body"]).encode("utf-8")
        self.send_response(script["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 (http.server API)
        script = self.server.script_get
        status = script.get("status", 404) if script else 404
        body = json.dumps(script.get("body", {})).encode("utf-8") if script else b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence test output
        pass


class ScriptedWebhookServer:
    """Local loopback webhook: records requests, replays scripted
    responses. ``script_post = {"drop": True}`` simulates ACK loss."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.script_post = {"status": 200, "body": {"external_id": "ext-1"}}
        self.server.script_get = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/notify"

    @property
    def status_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/status"

    @property
    def requests(self) -> list[dict]:
        return self.server.requests

    def set_post_script(self, script: dict) -> None:
        self.server.script_post = script

    def set_get_script(self, script: dict | None) -> None:
        self.server.script_get = script

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def webhook_channel(channel_ref: str = "push:demo", *, summary_only: bool = True,
                    endpoint: dict | None = None) -> dict:
    return {
        "channel_ref": channel_ref,
        "kind": "webhook",
        "endpoint": endpoint or {},
        "push_summary_only": summary_only,
    }
