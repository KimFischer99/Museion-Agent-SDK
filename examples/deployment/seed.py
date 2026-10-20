"""Trusted, operator-invoked setup. No model or external notification calls."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from proactive_sdk import JobSpec


def seed(agent) -> None:
    notify = agent.grant("notify.self", evidence="consent:deployment-example")
    public = agent.grant("public.read", account_ref="account:public",
                         evidence="consent:deployment-example")
    agent.jobs_upsert(JobSpec(
        job_id="source-daily", mode="heartbeat",
        schedule={"kind": "daily", "local_time": "09:00", "timezone": "Asia/Shanghai"},
        task={"instruction": "检查已授权期刊页面；只有实质更新且有证据时通知本人。",
              "refresh_source_ids": ["journal-a-current", "journal-b-index"]},
        grant_refs=(notify.grant_id, public.grant_id),
    ))
    # Enable only after the host supplies authorized conversation/memory context.
    agent.jobs_upsert(JobSpec(
        job_id="proactive-hourly", mode="task", enabled=False,
        schedule={"kind": "interval", "anchor": "2026-10-19T00:00:00Z", "every_seconds": 3600},
        task={"instruction": "根据已授权的对话和记忆判断是否值得主动联系；无依据时保持静默。",
              "always_run": True},
        grant_refs=(notify.grant_id,),
    ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--consent", required=True, action="store_true",
                        help="authorize notify.self and public.read for this profile")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
    from app import build_agent
    from proactive_sdk.config import load_config

    agent = build_agent(load_config(args.config))
    try:
        seed(agent)
    finally:
        asyncio.run(agent.close())
