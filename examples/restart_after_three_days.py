#!/usr/bin/env python3
"""关机三天后重启,会发生什么。

这是主动型 agent 最容易做错的地方。天真实现在重启时会补跑缺席的每一个
slot——用户回来面对三条一样的通知;另一个天真实现直接吞掉,用户永远
不知道漏了什么。

PAS 的做法是第三种:缺席是一段 **episode**,最多物化成一行的账;
补不补由任务的 misfire 策略决定;无论补还是丢,**为什么**都可查。

三种策略在同一个停机里跑一遍:

    heartbeat + coalesce_latest   补跑一次
    task      + grace_once(默认) 超出宽限 → 记为 missed,附 episode 大小
    reminder  + grace_once        从未兑现,同样落账

全程只用手动 tick 和 FakeClock,没有模型调用,也不需要网络。

    python3 examples/restart_after_three_days.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import (  # noqa: E402
    NOTIFY_SELF_CAPABILITY,
    FakeClock,
    Job,
    ModelResponse,
    ProactiveAgent,
)

DAY_MS = 24 * 3600 * 1000


def at_utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def stamp(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class SilentModel:
    """Answers `silent` — this demo is about the scheduler, not the model."""

    calls = 0

    async def generate(self, request) -> ModelResponse:
        SilentModel.calls += 1
        return ModelResponse(
            content=json.dumps(
                {"decision": "silent", "summary": "Nothing to report.", "proposals": []}
            ),
            usage={"turns": 1},
        )


async def drain(agent: ProactiveAgent, *, max_ticks: int = 5) -> list[dict]:
    """Tick until the queue is quiet — what a daemon does continuously."""
    processed: list[dict] = []
    for _ in range(max_ticks):
        report = await agent.tick()
        processed.extend(report["runs"])
        if not report["runs"]:
            break
    return processed


async def main() -> int:
    start = at_utc(2025, 10, 21, 8, 59)
    clock = FakeClock(wall_ms=start)
    with tempfile.TemporaryDirectory() as state_dir:
        agent = ProactiveAgent(
            state_dir=state_dir,
            model=SilentModel(),
            clock=clock,
            timezone="UTC",
            profile="outage",
        )
        try:
            grant = agent.grant(NOTIFY_SELF_CAPABILITY)
            daily = {"kind": "daily", "local_time": "09:00", "timezone": "UTC"}

            def add(job_id: str, mode: str, misfire: str | None = None, **extra):
                agent.jobs_upsert(
                    Job(
                        id=job_id,
                        mode=mode,
                        schedule=daily,
                        instruction=f"[{job_id}] 看看有没有值得说的事。",
                        grant_refs=(grant.grant_id,),
                        misfire_policy=misfire,
                        delivery_policy={"timezone": "UTC"},
                        **extra,
                    )
                )

            add("heartbeat-lite", "heartbeat", "coalesce_latest")
            add("daily-check", "task")  # default misfire: grace_once
            agent.jobs_upsert(
                Job(
                    id="standup",
                    mode="reminder",
                    schedule={"kind": "runonce", "at": stamp(at_utc(2025, 10, 22, 9, 0))},
                    grant_refs=(grant.grant_id,),
                    delivery_policy={"timezone": "UTC"},
                    reminder={"title": "站会", "body": "10:00 站会", "timezone": "UTC"},
                )
            )

            print("== 第一天 08:59,三个任务登记完毕 ==")
            clock.advance_wall(60_000)
            runs = await drain(agent)
            print(f"   09:00 正常跑了一轮,处理了 {len(runs)} 个 run")

            print()
            print("== 机器关机三天 ==")
            clock.advance_wall(3 * DAY_MS + 5 * 3600 * 1000)
            print(f"   现在是 {stamp(clock.wall_now_ms())}")
            print("   期间错过:10-22 / 10-23 / 10-24 三个 09:00,以及 10-22 的站会")

            print()
            print("== 重启后的第一次 tick ==")
            runs = await drain(agent)
            print(f"   补跑了 {len(runs)} 个 run —— 三个 slot,只补一次")

            print()
            print("== 账本:每段缺席都留下了理由 ==")
            print(f"   {'job':<15} {'phase':<9} {'state':<10} reason")
            for row in agent.store.job_activity(limit=20):
                print(
                    f"   {row['job_id']:<15} {row['phase']:<9} {row['state']:<10}"
                    f" {row['reason']}"
                )

            inbox = agent.store.list_inbox()
            print()
            print(f"== 用户看到的:{len(inbox)} 条 inbox(不是三条)==")
            for message in inbox:
                print(f"   {message['title']}")
            print(f"== 模型被调用 {SilentModel.calls} 次 ==")
            return 0
        finally:
            await agent.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
