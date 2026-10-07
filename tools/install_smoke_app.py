"""Embedded-mode smoke executed by tools/install_smoke.py INSIDE the
fresh venv — proves the installed wheel supports the facade lifecycle,
migrations, a scripted executor run and a clean close. Uses a scripted
executor (no model, no network); nothing here fakes delivery states."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from proactive_sdk import Job, ProactiveAgent
from proactive_sdk.contracts import Decision, RunRequest
from proactive_sdk.executor import ExecutorOutcome, ToolLoopExecutor
from proactive_sdk.tools import LocalToolBroker


class _NoModel:
    def generate(self, *_a, **_k):
        raise RuntimeError("smoke executor makes no model calls")


class SmokeExecutor(ToolLoopExecutor):
    def __init__(self) -> None:
        super().__init__(
            model=_NoModel(),
            broker=LocalToolBroker(capabilities=frozenset()),
        )

    async def execute(self, run_request: RunRequest, *args, **kwargs) -> ExecutorOutcome:  # noqa: ARG002
        return ExecutorOutcome(
            decision=Decision(decision="silent", summary="install smoke", proposals=()),
            events=(),
            usage={"protocol_version": "1.0", "model_turns": 0, "tool_calls": 0,
                   "wall_time_ms": 0, "pricing_basis": "unmeasured"},
            model_turns=0,
        )


async def main() -> int:
    state_dir = os.environ["PAS_SMOKE_STATE"]
    agent = ProactiveAgent(
        state_dir=state_dir,
        executor=SmokeExecutor(),
        timezone="UTC",
        locale="en",
        profile="install-smoke",
    )
    async with agent:
        agent.jobs_upsert(
            Job(
                id="smoke-heartbeat",
                mode="heartbeat",
                schedule={"kind": "runonce", "at": "2030-01-01T00:00:00Z"},
                instruction="no-op",
            ),
            idempotency_key="smoke-hb-1",
        )
        report = await agent.tick()
        status = agent.status()
        assert status["jobs"]["total"] == 1, status
        assert status["health"]["alive"] is True
        assert isinstance(report, dict)
        data = {
            "facade_tick": "ok",
            "jobs": status["jobs"],
            "metrics_keys": sorted(status["metrics"]),
        }
        print(json.dumps(data, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
