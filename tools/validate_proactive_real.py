#!/usr/bin/env python3
"""Opt-in live canary: two host submissions, public RSS, isolated loopback push.

Run on the already configured validation host with --host pi|hermes --output FILE.
No credentials are copied or logged. No real user channel is contacted.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_sdk import ChannelSink, JobSpec, ProactiveAgent, RunBudget
from proactive_sdk.delivery import WebhookNotificationSink
from proactive_sdk.hermes import HermesRunsExecutor
from proactive_sdk.host_bridge import HostBridge
from proactive_sdk.host_drivers import HermesHostDriver, PiHostDriver
from proactive_sdk.net import EgressBroker
from proactive_sdk.pi_worker import PiWorkerConfig, PiWorkerExecutor
from proactive_sdk.proactive import ProactiveController
from proactive_sdk.research import NewsSearchSource
from push_receiver import build_receiver


def hermes_token() -> str:
    path = Path(os.environ.get("PAS_HERMES_PROFILE_CONFIG", "~/.hermes/profiles/pas-p5/config.yaml")).expanduser()
    lines = path.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "api_server:")
    indent = len(lines[start]) - len(lines[start].lstrip())
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if line.strip().startswith("key:"):
            return line.strip().split(":", 1)[1].strip().strip("\"'")
    raise ValueError("Hermes API key is missing")


class CountedHost:
    """Bound actual host submissions, including classifier calls outside the ledger."""
    def __init__(self, driver):
        self.driver, self.calls, self.usage, self.replies = driver, 0, [], []

    async def capabilities(self):
        return await self.driver.capabilities()

    async def submit(self, prompt, *, timeout_s):
        if self.calls >= 2:
            raise AssertionError("live canary submission budget exhausted")
        self.calls += 1
        print(f"host_submission={self.calls}/2", flush=True)
        reply = await self.driver.submit(prompt, timeout_s=timeout_s)
        self.usage.append(reply.usage)
        self.replies.append(reply.text)
        return reply

    async def cancel(self, key):
        return await self.driver.cancel(key)

    async def close(self):
        await self.driver.close()


async def validate(host: str, output: Path, *, skip_classification: bool = False) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    checks = []

    def check(name, ok, **detail):
        checks.append({"check": name, "passed": bool(ok), **detail})
        print(json.dumps(checks[-1], ensure_ascii=False), flush=True)
        if not ok:
            raise AssertionError(name)

    with tempfile.TemporaryDirectory(prefix="pas-proactive-") as root:
        scratch = Path(root) / "scratch"
        scratch.mkdir()
        server, receiver = build_receiver(Path(root) / "received.jsonl")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if host == "pi":
            driver = PiHostDriver(PiWorkerExecutor(PiWorkerConfig(
                command=(os.environ.get("PAS_NODE_BIN", str(Path.home() / "nodejs/bin/node")),
                         "--experimental-strip-types", str(ROOT / "src/proactive_sdk/pi_worker/pi_worker.ts")),
                pi_entry=os.environ.get("PAS_PI_ENTRY", str(Path.home() / "nodejs/lib/node_modules/@earendil-works/pi-coding-agent/dist/index.js")),
                allowed_tools=(), init_timeout_s=30, run_timeout_s=90,
            )), cwd=str(scratch))
        else:
            driver = HermesHostDriver(HermesRunsExecutor(
                os.environ.get("PAS_HERMES_URL", "http://127.0.0.1:8642/p/pas-p5"), hermes_token(),
                poll_interval_s=1, cancel_timeout_s=30,
            ), instructions="Analyze only supplied data; never use host tools. Return only the decision envelope.")
        counted = CountedHost(driver)
        bridge = HostBridge(counted, capabilities=("public.read", "memory.read"),
                            budget=RunBudget(max_model_turns=1, max_tool_calls=0, max_proposals=1, wall_time_s=90))
        state = Path(root) / "state"
        agent = ProactiveAgent(state_dir=state, executor=bridge, profile="proactive-canary", timezone="UTC")
        controller = ProactiveController(agent, interval_seconds=60)
        source = NewsSearchSource(
            topics=controller.public_topics,
            broker=EgressBroker(allowed_hosts=("news.google.com",), timeout_s=10, max_bytes=262144),
            url_template="https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
            max_results=3,
        )
        agent.registry.register(source_id=source.source_id, account_ref=source.account_ref,
                                source=source, required_capability=source.required_capability)
        try:
            info = await counted.capabilities()
            check("host_ready", bool(info))
            # Deliberately not a direct command: the real host extracts the interest.
            if not skip_classification:
                classified = await controller.handle_input("I keep up with public news on NASA and want a short interest note.")
                check("real_interest_extraction", bool(classified["applied_actions"]), classification=classified.get("classification"))
            # The explicit direct form freezes the query label for this test.
            await controller.handle_input("我关注NASA公开新闻")
            enabled = controller.enable(actor="canary-owner")
            check("consented_research_job", enabled.get("research_job") == "proactive-research")
            port = server.server_address[1]
            agent._attach_sink(ChannelSink(
                channel_ref="push:canary", kind="webhook", push_summary_only=False,
                sink=WebhookNotificationSink(),
                endpoint={"url": f"http://127.0.0.1:{port}/push", "status_url": f"http://127.0.0.1:{port}/status"},
            ))
            job = agent.jobs_get("proactive-research")
            anchor = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
            task = dict(job.task)
            task["instruction"] += (
                " This is an owner-requested live verification. The owner has not seen these headlines. "
                "Choose one supplied NASA development as useful for this verification and propose one notification, "
                "using exact candidate topic nasa. Do not invent missing content."
            )
            agent.jobs_upsert(JobSpec(job_id=job.job_id, revision=job.revision + 1, mode="heartbeat",
                schedule={"kind": "interval", "anchor": anchor, "every_seconds": 60},
                task=task, grant_refs=job.grant_refs,
                delivery_policy={**job.delivery_policy, "notification_profile": "push:canary"}))
            await asyncio.sleep(2.1)
            report = await agent.tick(max_runs=1)
            check("scheduled_public_research", bool(report["runs"]), report=report)
            records = list(receiver.by_key.values())
            check("real_http_notification", len(records) == 1, receipts=records)
            rows = agent.store.list_runs()
            pack = agent.store.get_context_pack(rows[0]["context_ref"])
            check("interest_in_run_context", bool(pack.get("memory_entries")))
            usage = agent.store.get_run(rows[0]["run_id"])["usage"]
            check("usage_recorded", bool(usage), usage=usage)
            before = counted.calls
            agent.trigger_job(job.job_id)
            replay = await agent.tick(max_runs=1)
            check("unchanged_zero_model_calls", counted.calls == before and len(receiver.by_key) == 1, report=replay)
            await controller.handle_input("以后别主动发消息")
            agent.trigger_job(job.job_id)
            stopped = await agent.tick(max_runs=1)
            check("disabled_zero_model_calls", counted.calls == before and len(receiver.by_key) == 1, report=stopped)
            await agent.close()
            reopened = ProactiveAgent(state_dir=state, executor=bridge, profile="proactive-canary", timezone="UTC")
            try:
                check("restart_preserves_context", bool(reopened.store.recall_memory()) and not reopened.proactive_preferences()["enabled"])
            finally:
                await reopened.close()
        finally:
            await agent.close()
            server.shutdown()
            server.server_close()
            output.write_text(json.dumps({"host": host, "checks": checks, "host_submissions": counted.calls,
                                          "host_usage": counted.usage, "public_canary_replies": counted.replies}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("pi", "hermes"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-classification", action="store_true", help="one-call research canary after extraction was verified separately")
    args = parser.parse_args()
    asyncio.run(validate(args.host, args.output, skip_classification=args.skip_classification))
