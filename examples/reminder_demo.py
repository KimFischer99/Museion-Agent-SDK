#!/usr/bin/env python3
"""v0.1.1 direct-reminder demo (SPEC §21.1 steps 2–8).

Runs the whole deterministic path on a throwaway state directory using
only the public facade:

    trusted jobs_upsert → scheduler admission at the scheduled instant →
    owner outbox (zero model calls, no run row) → dispatcher → local inbox
    → user-visible activity projection

Everything here is local: the executor is a counting stub that fails the
demo if it is ever called, and no notification leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import FakeClock, NOTIFY_SELF_CAPABILITY, Job, ProactiveAgent


class NeverCalledExecutor:
    """Stands in for the agent loop so the demo can *prove* it is unused."""

    calls = 0

    async def capabilities(self):
        return {"capabilities": []}

    async def start(self, request):
        NeverCalledExecutor.calls += 1
        raise AssertionError("a direct reminder must never reach the executor")

    async def events(self, handle, after_seq: int = 0):
        if False:  # pragma: no cover - generator shape only
            yield None

    async def status(self, handle):
        return {"state": "unused"}

    async def cancel(self, handle):
        return {"cancelled": True}

    async def close(self) -> None:
        return None


def rfc3339(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(wall_ms=1_760_000_000_000)  # 2025-10-09T08:53:20Z
        agent = ProactiveAgent(
            state_dir=tmp,
            executor=NeverCalledExecutor(),
            clock=clock,
            timezone="Europe/Berlin",
            locale="zh-CN",
            profile="demo",
        )
        try:
            grant = agent.create_grant_from_user_consent(
                capability=NOTIFY_SELF_CAPABILITY,
                account_ref="account:primary",
                scope={},
                consent_evidence_ref="consent:demo",
            )
            due_at = clock.wall_now_ms() + 60_000
            agent.jobs_upsert(
                Job(
                    id="standup",
                    mode="reminder",
                    schedule={"kind": "runonce", "at": rfc3339(due_at)},
                    grant_refs=(grant.grant_id,),
                    delivery_policy={"timezone": "Europe/Berlin"},
                    reminder={"title": "站会", "body": "10:00 站会", "timezone": "Europe/Berlin"},
                ),
                idempotency_key="demo-reminder-v1",
            )

            before = await agent.tick()  # not due yet
            clock.advance_wall(61_000)
            after = await agent.tick()  # due
            print(json.dumps({"before": before["admitted"], "after": after["admitted"]}, ensure_ascii=False))

            inbox = agent.inbox_list()
            print(json.dumps({"inbox": inbox}, ensure_ascii=False))
            activity = agent.activity_list("standup")
            print(json.dumps({"activity": activity}, ensure_ascii=False))

            assert inbox and inbox[0]["body"] == "10:00 站会", "the frozen body must arrive"
            assert NeverCalledExecutor.calls == 0, "zero model calls is the whole point"
            assert agent.store.list_runs() == [], "a reminder creates no analysis run"
            phases = {row["phase"] for row in activity}
            assert {"action", "delivery"} <= phases, phases
            print("REMINDER DEMO: PASS (0 model calls, 0 runs, 1 delivered message)")
            return 0
        finally:
            await agent.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
