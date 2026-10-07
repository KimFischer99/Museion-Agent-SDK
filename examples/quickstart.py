#!/usr/bin/env python3
"""The 20-line version: an agent that watches something and tells you.

This is the copy-paste starting point. It runs as-is with no API key, using
a tiny local stand-in for the model so you can see the whole loop before
spending anything; set PAS_MODEL_BASE_URL / PAS_MODEL_API_KEY /
PAS_MODEL_NAME and the very same code calls a real model instead.

What it shows, in order:

    1. an agent, assembled from `model=` plus the built-in tools
    2. the user granting one capability (the trusted, one-line form)
    3. a job: "every hour, look; speak only if something changed"
    4. one slot actually running, and the agent deciding *not* to speak
    5. a second pass that does nothing at all — zero model calls

Step 5 is the point. A proactive agent's most common output is silence,
and "why was it quiet?" is a first-class answer here, not a shrug.

    python3 examples/quickstart.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import (  # noqa: E402
    NOTIFY_SELF_CAPABILITY,
    FakeClock,
    Job,
    ModelRequest,
    ModelResponse,
    OpenAICompatibleModel,
    ProactiveAgent,
)

START_MS = 1_760_000_000_000  # 2025-10-09T08:53:20Z


def rfc3339(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class OfflineModel:
    """A stand-in that answers `silent`, so the demo needs no credentials.

    A real model looks at the context pack and decides whether anything is
    worth saying. This one always says "nothing to report" — a valid
    answer, and it lets the demo run anywhere.
    """

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content=json.dumps(
                {
                    "decision": "silent",
                    "summary": "Nothing here is worth interrupting the user for yet.",
                    "proposals": [],
                }
            ),
            usage={"turns": 1},
        )


def build_model():
    base_url = os.environ.get("PAS_MODEL_BASE_URL")
    api_key = os.environ.get("PAS_MODEL_API_KEY")
    if not (base_url and api_key):
        print("(no PAS_MODEL_BASE_URL/PAS_MODEL_API_KEY set — using the offline stub)")
        return OfflineModel(), "offline-stub"
    name = os.environ.get("PAS_MODEL_NAME", "gpt-4o-mini")
    return OpenAICompatibleModel(base_url=base_url, api_key=api_key, model=name), name


async def main() -> int:
    model, name = build_model()
    clock = FakeClock(wall_ms=START_MS)

    with tempfile.TemporaryDirectory() as state_dir:
        agent = ProactiveAgent(
            state_dir=state_dir,
            model=model,  # <- the built-in tool loop, tools included
            clock=clock,
            timezone=os.environ.get("PAS_TIMEZONE", "Asia/Shanghai"),
            profile="quickstart",
        )
        try:
            print(f"tools ready: {', '.join(agent.tool_names)}")

            # The user's own code grants one capability. Trusted entry: this
            # has to be something the user did — never model output.
            grant = agent.grant(NOTIFY_SELF_CAPABILITY)

            # "Every hour, look at this. Speak only if it matters."
            agent.jobs_upsert(
                Job(
                    id="watch-inbox",
                    mode="task",
                    schedule={
                        "kind": "interval",
                        "anchor": rfc3339(START_MS + 60_000),
                        "every_seconds": 3600,
                    },
                    instruction=(
                        "看看有没有需要我处理的事。只有在真的有事、并且你手上有证据时"
                        "才通知我；没有就保持沉默。"
                    ),
                    grant_refs=(grant.grant_id,),
                )
            )

            print(f"model: {name}")
            for label, jump_ms in (("slot due", 120_000), ("one minute later", 60_000)):
                clock.advance_wall(jump_ms)
                report = await agent.tick()
                print(f"-- {label}")
                print(
                    f"   admitted: {len(report['admitted'])}"
                    f"  runs processed: {len(report['runs'])}"
                )
                for run in report["runs"]:
                    print(
                        f"   run {run['run_id'][:12]}… -> {run['outcome']}"
                        f" ({run['reason']}) [policy: {run['policy_outcome']}]"
                    )

            print(f"notifications sent: {len(agent.store.list_outbox())}")
            print(f"inbox: {len(agent.store.list_inbox())}")
            # Why the agent stayed quiet is a first-class answer.
            for row in agent.store.job_activity(limit=5):
                print(f"   activity: {row['phase']}/{row['state']} — {row['reason']}")
            return 0
        finally:
            await agent.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
