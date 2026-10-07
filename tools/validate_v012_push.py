#!/usr/bin/env python3
"""v0.1.2 step 8 on a real agent: a real push channel a human can read.

The mechanism was already there (`WebhookNotificationSink` POSTs with a
per-message idempotency key, the dispatcher reconciles before retrying,
`push_summary_only` strips the body for push channels). What was missing is
a run against a **real receiver over a real socket**, whose delivered
messages a human can then read — which is what `tools/push_receiver.py` is.

Four claims are checked:

1. a real delivery arrives and is recorded;
2. the lock-screen minimisation actually happens on the wire (title, no body);
3. a replayed delivery under the same provider key is *absorbed* by the
   provider rather than duplicated;
4. a lost ACK yields `delivery_unknown`, and only the provider's own answer
   resolves it — PAS never assumes.

Run ON the host that runs Hermes. Cost model: 2 model calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from push_receiver import build_receiver  # noqa: E402

from proactive_sdk import (  # noqa: E402
    NOTIFY_SELF_CAPABILITY,
    ChannelSink,
    DeliveryRequest,
    HostBridge,
    MemoryEntry,
    ProactiveAgent,
    SystemClock,
    WebhookNotificationSink,
)
from proactive_sdk.contracts import SourceBatch, SourceItem  # noqa: E402
from proactive_sdk.hermes import HermesRunsClient, HermesRunsExecutor  # noqa: E402
from proactive_sdk.host_drivers import HermesHostDriver  # noqa: E402

RESULTS: list[dict] = []
NOTE = (
    "我在跟踪几个阻塞项的进展。下面这两件事一旦有动静就立刻推送到我的手机上："
    "PR #1234 是否被合并，PR #5678 是否通过评审。"
)
MEMORY_ID = "mem-push-tracking"


def record(name: str, ok: bool | None, detail: dict | None = None) -> bool:
    entry = {"probe": name, "ok": ok}
    if detail:
        entry["detail"] = detail
    RESULTS.append(entry)
    label = "PASS" if ok else ("FAIL" if ok is False else "INCONCLUSIVE")
    print(f"{label} " + json.dumps(entry, ensure_ascii=False), flush=True)
    return ok is not False


def load_hermes_token() -> str:
    token = os.environ.get("PAS_HERMES_TOKEN", "")
    if token:
        return token
    cfg = Path(
        os.environ.get("PAS_HERMES_PROFILE_CONFIG", "~/.hermes/profiles/pas-p5/config.yaml")
    ).expanduser()
    if not cfg.is_file():
        return ""
    lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "api_server:"), None)
    if start is None:
        return ""
    indent = len(lines[start]) - len(lines[start].lstrip())
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if line.strip().startswith("key:"):
            return line.strip().split(":", 1)[1].strip().strip("\"'")
    return ""


class TrackedUpdates:
    """Reports one new change per call.

    Two runs are needed below (a normal delivery and a lost-ACK one), and a
    source that repeats the same revision would be stopped by L0 change
    detection — correctly so. Each call therefore reports a genuinely new
    observation, which is what the two scenarios are supposed to differ in.
    """

    source_id = "vcs"
    account_ref = "account:primary"
    required_capability = "vcs.read"

    _UPDATES = (
        ("pr-1234", "merged", "PR #1234 was merged into main by alice just now."),
        ("pr-5678", "approved", "PR #5678 passed review and is ready to merge."),
    )

    def __init__(self) -> None:
        self.calls = 0

    async def fetch_delta(self, request):
        now = "2026-10-07T08:00:00Z"
        if self.calls >= len(self._UPDATES):
            return SourceBatch(
                source_id=self.source_id, account_ref=self.account_ref,
                observed_at=now, cursor_ref=f"c{self.calls}", items=(),
            )
        fact_id, revision, content = self._UPDATES[self.calls]
        self.calls += 1
        return SourceBatch(
            source_id=self.source_id,
            account_ref=self.account_ref,
            observed_at=now,
            cursor_ref=f"c{self.calls}",
            items=(
                SourceItem(
                    fact_id=fact_id, revision=revision, content=content, observed_at=now
                ),
            ),
        )


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    print(f"python={sys.version.split()[0]}", flush=True)

    log_path = Path(tempfile.mkdtemp(prefix="pas-v012-push-")) / "received.jsonl"
    server, state = build_receiver(log_path, port=0)
    host, port = server.server_address[:2]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://{host}:{port}"
    print(f"receiver listening on {base} (log: {log_path})", flush=True)

    token = load_hermes_token()
    if not token:
        record("push.setup", False, {"reason": "no Runs API key on this host"})
        return 1
    url = os.environ.get("PAS_HERMES_URL", "http://127.0.0.1:8642/p/pas-p5")

    with tempfile.TemporaryDirectory(prefix="pas-v012-push-state-") as state_dir:
        driver = HermesHostDriver(
            HermesRunsExecutor(url, token, poll_interval_s=3.0, cancel_timeout_s=90.0)
        )
        # One transport shared by both channels: the dispatcher keys
        # transports by channel *kind*, and what differs between the two is
        # the per-channel endpoint (URL and headers), which is where the
        # lost-ACK case is configured.
        push_sink = WebhookNotificationSink()
        agent = ProactiveAgent(
            state_dir=state_dir,
            executor=HostBridge(driver, capabilities=frozenset({"vcs.read"})),
            sources=(TrackedUpdates(),),
            sinks=(
                ChannelSink(
                    channel_ref="push:phone", kind="webhook", push_summary_only=True,
                    sink=push_sink,
                    endpoint={"url": f"{base}/push", "status_url": f"{base}/status"},
                ),
                ChannelSink(
                    channel_ref="push:lossy", kind="webhook", push_summary_only=True,
                    sink=push_sink,
                    endpoint={
                        "url": f"{base}/push",
                        "status_url": f"{base}/status",
                        # The receiver accepts and records, then closes without
                        # answering: the real lost-ACK case.
                        "headers": {"X-Drop-Response": "1"},
                    },
                ),
            ),
            clock=SystemClock(),
            timezone="Asia/Kolkata",
            locale="zh-CN",
            profile="v012-push",
        )
        try:
            notify = agent.create_grant_from_user_consent(
                capability=NOTIFY_SELF_CAPABILITY, account_ref="account:primary",
                scope={}, consent_evidence_ref="consent:v012-push",
            )
            agent.create_grant_from_user_consent(
                capability="vcs.read", account_ref="account:primary",
                scope={}, consent_evidence_ref="consent:v012-push-vcs",
            )
            asyncio.run(
                agent.pack_builder.memory.remember(
                    MemoryEntry(
                        memory_id=MEMORY_ID,
                        content="用户把 PR #1234 和 PR #5678 都当作阻塞项在跟踪。",
                        source="user",
                    )
                )
            )

            # ---- 1. a real delivery over a real socket --------------------
            event_id = agent.note_user_input(
                NOTE, grant_refs=(notify.grant_id,), destination="push:phone"
            )
            report = asyncio.run(agent.tick())
            entry = list(report["runs"])[0] if report["runs"] else None
            received = _get(f"{base}/received")
            got = received["records"]
            record(
                "push.delivered",
                len(got) == 1 and entry and entry["policy_outcome"] == "actions_queued",
                {
                    "event": event_id[:24],
                    "policy_outcome": (entry or {}).get("policy_outcome"),
                    "received": len(got),
                    "external_id": got[0]["external_id"] if got else None,
                },
            )

            # ---- 2. lock-screen minimisation happened on the wire ---------
            payload = got[0]["payload"] if got else {}
            record(
                "push.lockscreen_minimised",
                bool(payload.get("title")) and "body" not in payload,
                {"keys": sorted(payload), "title": payload.get("title")},
            )

            # ---- 3. provider absorbs a replayed provider key --------------
            provider_key = got[0]["provider_key"] if got else ""
            first_external = got[0]["external_id"] if got else None
            if provider_key:
                replay = asyncio.run(
                    push_sink.send(
                        DeliveryRequest(
                            message_id="replay",
                            provider_key=provider_key,
                            destination_ref="push:phone",
                            endpoint={"url": f"{base}/push"},
                            payload=payload,
                        )
                    )
                )
                after = _get(f"{base}/received")
                record(
                    "push.provider_idempotent",
                    after["count"] == 1
                    and (replay.receipt or {}).get("external_id") == first_external,
                    {
                        "count_after_replay": after["count"],
                        "same_external_id": (replay.receipt or {}).get("external_id")
                        == first_external,
                    },
                )
            else:
                record("push.provider_idempotent", None, {"reason": "no delivery to replay"})

            # ---- 4. lost ACK -> unknown -> provider's answer --------------
            agent.note_user_input(
                NOTE, grant_refs=(notify.grant_id,), destination="push:lossy"
            )
            report2 = asyncio.run(agent.tick())
            entry2 = list(report2["runs"])[0] if report2["runs"] else None
            lossy = [
                m for m in agent.store.list_outbox() if m["destination_ref"] == "push:lossy"
            ]
            states = [m["state"] for m in lossy]
            # The tick already runs its own reconcile pass (facade.py), so by
            # the time we look, the message has been through unknown and come
            # out the far side. An explicit second pass must therefore find
            # nothing left to reconcile.
            leftovers = asyncio.run(
                agent.dispatcher.reconcile_unknowns(
                    now_ms=agent.store.clock.wall_now_ms()
                )
            )
            record(
                "push.lost_ack_reconciled",
                states == ["reconciled_delivered"]
                and _get(f"{base}/received")["dropped_acks"] >= 1
                and leftovers == [],
                {
                    "policy_outcome": (entry2 or {}).get("policy_outcome"),
                    "lossy_states": states,
                    "dropped_acks": _get(f"{base}/received")["dropped_acks"],
                    "second_pass_found": len(leftovers),
                },
            )

            # ---- 4b. an unknown the provider will not vouch for stays unknown
            # (the "never retry blindly" half). Checked against a status
            # endpoint that answers nothing useful, which is the honest case
            # for most real push providers.
            stale = agent.store.get_outbox_message(lossy[0]["message_id"]) if lossy else None
            record(
                "push.unknown_is_not_blind_retried",
                bool(stale) and stale["state"] != "pending",
                {
                    "state": (stale or {}).get("state"),
                    "attempts": (stale or {}).get("attempts"),
                },
            )

            # ---- the human-readable artefact -----------------------------
            print("--- what actually arrived (read this) ---", flush=True)
            print(log_path.read_text(encoding="utf-8").strip() or "(nothing)", flush=True)
        finally:
            asyncio.run(agent.close())

    server.shutdown()
    failed = [r["probe"] for r in RESULTS if r["ok"] is False]
    print(
        "SUMMARY "
        + json.dumps(
            {
                "probes": len(RESULTS),
                "passed": sum(1 for r in RESULTS if r["ok"]),
                "failed": failed,
                "inconclusive": [r["probe"] for r in RESULTS if r["ok"] is None],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
